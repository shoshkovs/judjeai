import logging

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import func, or_, select

from src.api.deps import AdminUser, CurrentUser, DbSession
from src.api.precedents import get_embedder
from src.application.precedent_cards import CARD_EXTRACTOR_VERSION
from src.application.similar_precedents import EmbeddingPort, IndexPrecedents, StoredCard
from src.db import session as db_session
from src.db.models import KnowledgeChunk, KnowledgeVerdict
from src.domain.documents import reading_warnings
from src.domain.practice import PRIVATE_SOURCE_TYPES
from src.infrastructure.precedent_store import SqlPrecedentStore
from src.infrastructure.verdict_reader import PracticeReader
from src.schemas.knowledge import (
    KnowledgeListResponse,
    KnowledgeUploadItem,
    KnowledgeUploadResponse,
    KnowledgeVerdictDetail,
    KnowledgeVerdictRead,
    PrecedentIndexRead,
)
from src.services.document_parser import extract_document
from src.services.file_reader import save_upload_file
from src.services.knowledge_service import create_knowledge_verdict_from_text

logger = logging.getLogger(__name__)

router = APIRouter()


def _precedent_read(stored: StoredCard | None) -> PrecedentIndexRead | None:
    if stored is None:
        return None
    card = stored.card
    return PrecedentIndexRead(
        card_id=stored.id,
        qualifications=[str(e.qualification) for e in card.episodes],
        punishment_months=card.imprisonment_months,
        conditional=card.conditional,
        recidivism=card.recidivism.value if card.recidivism else None,
    )


async def _index_cards(db, embedder: EmbeddingPort, verdict_ids: list[int]):
    """Build cards for the new verdicts (regex, fast) and return the cards of the uploaded ones."""
    store = SqlPrecedentStore(db)
    try:
        await IndexPrecedents(store, PracticeReader(), embedder).build_cards(CARD_EXTRACTOR_VERSION)
    except Exception:  # the verdicts are saved; `python -m src.cli.precedents index` retries
        logger.exception("precedent cards were not built on upload")
        await db.rollback()
    return await store.cards_for_verdicts(verdict_ids)


async def _embed_precedents(embedder: EmbeddingPort) -> None:
    async with db_session.async_session_factory() as db:
        store = SqlPrecedentStore(db)
        await IndexPrecedents(store, PracticeReader(), embedder).embed_pending()


@router.post("/verdicts/upload", response_model=KnowledgeUploadResponse)
async def upload_verdicts(
    db: DbSession,
    user: CurrentUser,
    background: BackgroundTasks,
    files: list[UploadFile] = File(default_factory=list),
    source_type: str = Form("manual"),
    embedder: EmbeddingPort = Depends(get_embedder),
):
    if not files:
        raise HTTPException(status_code=422, detail="Не переданы файлы")
    if source_type in PRIVATE_SOURCE_TYPES:  # set only by the analysis of a user's own case
        raise HTTPException(status_code=422, detail="Недопустимый источник документа")

    items: list[KnowledgeUploadItem] = []

    stored_paths = []
    try:
        for file in files:
            stored_path, _ = await save_upload_file(file)
            stored_paths.append(stored_path)
            document = await extract_document(stored_path)
            warnings = reading_warnings(document)
            if document.status not in {"ok", "partial"} or not document.text.strip():
                raise HTTPException(
                    422, "; ".join(warnings) or "В документе не найден пригодный текст."
                )
            verdict, duplicate = await create_knowledge_verdict_from_text(
                db=db,
                filename=file.filename or "file",
                raw_text=document.text,
                source_type=source_type,
            )
            items.append(
                KnowledgeUploadItem(
                    verdict_id=verdict.id,
                    filename=file.filename or "file",
                    status=verdict.status,
                    duplicate=duplicate,
                    case_number=verdict.case_number,
                    court_name=verdict.court_name,
                    verdict_date=verdict.verdict_date,
                    warnings=warnings,
                    extraction_status=document.status,
                    extraction_issues=list(document.issues),
                    unread_pages=list(document.unread_pages),
                )
            )
        await db.commit()
    except BaseException:
        await db.rollback()
        for path in stored_paths:
            path.unlink(missing_ok=True)
        raise

    cards = await _index_cards(db, embedder, [item.verdict_id for item in items])
    for item in items:
        item.precedent = _precedent_read(cards.get(item.verdict_id))
    background.add_task(_embed_precedents, embedder)
    return KnowledgeUploadResponse(uploaded=len(items), items=items)


@router.get("/verdicts", response_model=KnowledgeListResponse)
async def list_verdicts(
    db: DbSession,
    user: CurrentUser,
    page: int = 1,
    size: int = 20,
    q: str | None = None,
):
    shared = KnowledgeVerdict.source_type.not_in(PRIVATE_SOURCE_TYPES)
    base_stmt = select(KnowledgeVerdict).where(shared)
    count_stmt = select(func.count(KnowledgeVerdict.id)).where(shared)

    if q:
        filter_expr = or_(
            KnowledgeVerdict.case_number.ilike(f"%{q}%"),
            KnowledgeVerdict.court_name.ilike(f"%{q}%"),
            KnowledgeVerdict.source_filename.ilike(f"%{q}%"),
        )

        base_stmt = base_stmt.where(filter_expr)
        count_stmt = count_stmt.where(filter_expr)

    total = await db.scalar(count_stmt)

    stmt = (
        base_stmt.order_by(KnowledgeVerdict.created_at.desc()).offset((page - 1) * size).limit(size)
    )

    result = await db.scalars(stmt)
    items = [KnowledgeVerdictRead.model_validate(verdict) for verdict in result.all()]

    return KnowledgeListResponse(
        total=total or 0,
        page=page,
        size=size,
        items=items,
    )


@router.get("/verdicts/{verdict_id}", response_model=KnowledgeVerdictDetail)
async def get_verdict(verdict_id: int, db: DbSession, user: CurrentUser):
    verdict = await db.get(KnowledgeVerdict, verdict_id)

    if not verdict or verdict.source_type in PRIVATE_SOURCE_TYPES:
        raise HTTPException(status_code=404, detail="Приговор не найден")

    chunks_count = await db.scalar(
        select(func.count(KnowledgeChunk.id)).where(KnowledgeChunk.verdict_id == verdict_id)
    )

    return KnowledgeVerdictDetail(
        id=verdict.id,
        uid=verdict.uid,
        case_number=verdict.case_number,
        court_name=verdict.court_name,
        verdict_date=verdict.verdict_date,
        source_type=verdict.source_type,
        source_filename=verdict.source_filename,
        file_hash=verdict.file_hash,
        status=verdict.status,
        created_at=verdict.created_at,
        structured=verdict.structured,
        chunks_count=chunks_count or 0,
    )


@router.delete("/verdicts/{verdict_id}")
async def delete_verdict(verdict_id: int, db: DbSession, user: AdminUser):
    verdict = await db.get(KnowledgeVerdict, verdict_id)

    if not verdict:
        raise HTTPException(status_code=404, detail="Приговор не найден")

    await db.delete(verdict)
    await db.commit()

    return {"ok": True}
