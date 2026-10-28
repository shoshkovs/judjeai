"""Adapter: PostgreSQL/pgvector storage of precedent cards and the three search channels.

The vector channel filters by article inside the query, before LIMIT, and uses an exact scan
(no approximate index): an approximate index combined with such a filter can return fewer rows
than asked, while an exact scan over tens of thousands of cards takes tens of milliseconds.
"""

import re

from legal_core.entities import MeasureUnit, PunishmentKind, PunishmentTerm
from legal_core.precedents import PrecedentCard, PrecedentEpisode, Qualification, Recidivism
from sqlalchemy import String, cast, func, literal, select, update
from sqlalchemy.dialects.postgresql import ARRAY, insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.precedent_cards import CardExtraction
from src.application.similar_precedents import StoredCard, VerdictSource
from src.db.models import KnowledgeVerdict
from src.db.precedents import PrecedentCardField, PrecedentCardRow, PrecedentEmbedding
from src.domain.extraction import input_hash
from src.domain.practice import NOT_PRACTICE, PRIVATE_SOURCE_TYPES

_WORD = re.compile(r"[а-яё]{4,}", re.IGNORECASE)
MAX_QUERY_WORDS = 40


def _term_json(term: PunishmentTerm | None) -> dict | None:
    if term is None:
        return None
    unit = term.unit.value if term.unit else None
    return {"kind": term.kind.value, "amount": term.amount, "unit": unit}


def _term(data: dict | None) -> PunishmentTerm | None:
    if not data:
        return None
    unit = MeasureUnit(data["unit"]) if data.get("unit") else None
    return PunishmentTerm(PunishmentKind(data["kind"]), data.get("amount"), unit)


def _qualification_json(q: Qualification) -> dict:
    return {"article": q.article, "part": q.part, "point": q.point}


def _card(row: PrecedentCardRow) -> PrecedentCard:
    episodes = tuple(
        PrecedentEpisode(
            Qualification(
                e["qualification"]["article"],
                e["qualification"]["part"],
                e["qualification"]["point"],
            ),
            _term(e.get("punishment")),
        )
        for e in row.episodes
    )
    return PrecedentCard(
        episodes=episodes,
        mitigating=frozenset(row.mitigating),
        aggravating=frozenset(row.aggravating),
        recidivism=Recidivism(row.recidivism) if row.recidivism else None,
        applied_general=frozenset(row.applied_general),
        final=_term(row.final),
        conditional=row.conditional,
        fabula=row.fabula,
    )


def _stored(row: PrecedentCardRow) -> StoredCard:
    return StoredCard(
        row.id,
        _card(row),
        row.knowledge_verdict_id,
        row.uid,
        row.court_name,
        row.decision_date,
        row.source_url,
    )


class SqlPrecedentStore:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def pending_verdicts(self, extractor_version: str) -> list[VerdictSource]:
        has_card = select(PrecedentCardRow.knowledge_verdict_id)
        rows = await self.db.scalars(
            select(KnowledgeVerdict)
            .where(
                KnowledgeVerdict.raw_text.is_not(None),
                KnowledgeVerdict.id.not_in(has_card),
                # Documents saved from users' cases are private and never become practice.
                KnowledgeVerdict.source_type.not_in(PRIVATE_SOURCE_TYPES),
                KnowledgeVerdict.status.is_distinct_from(NOT_PRACTICE),
            )
            .order_by(KnowledgeVerdict.id)
        )
        return [
            VerdictSource(v.id, v.raw_text or "", v.uid, v.court_name, v.verdict_date, None)
            for v in rows
        ]

    async def mark_not_practice(self, knowledge_verdict_id: int) -> None:
        await self.db.execute(
            update(KnowledgeVerdict)
            .where(KnowledgeVerdict.id == knowledge_verdict_id)
            .values(status=NOT_PRACTICE)
        )
        await self.db.commit()

    async def is_duplicate(self, text_hash: str, uid: str | None, defendant_no: int = 0) -> bool:
        condition = PrecedentCardRow.text_sha256 == text_hash
        if uid:
            condition = condition | (PrecedentCardRow.uid == uid)
        condition = condition & (PrecedentCardRow.defendant_no == defendant_no)
        return await self.db.scalar(select(func.count()).where(condition)) > 0

    async def save_card(
        self, source: VerdictSource, extraction: CardExtraction, embedding_text: str
    ) -> int:
        card = extraction.card
        main = card.main_qualification
        row = PrecedentCardRow(
            knowledge_verdict_id=source.knowledge_verdict_id,
            defendant_no=extraction.defendant_no,
            outcome=extraction.outcome,
            text_sha256=extraction.text_hash,
            uid=source.uid,
            court_name=source.court_name,
            decision_date=source.decision_date,
            source_url=source.source_url,
            main_article=main.article if main else None,
            main_part=main.part if main else None,
            qualification_keys=sorted(card.qualification_keys),
            mitigating=sorted(card.mitigating),
            aggravating=sorted(card.aggravating),
            applied_general=sorted(card.applied_general),
            recidivism=card.recidivism.value if card.recidivism else None,
            conditional=card.conditional,
            imprisonment_months=card.imprisonment_months,
            episodes=[
                {
                    "qualification": _qualification_json(e.qualification),
                    "punishment": _term_json(e.punishment),
                }
                for e in card.episodes
            ],
            final=_term_json(card.final),
            fabula=embedding_text,
            embedding_text_hash=input_hash(embedding_text) if embedding_text else None,
            extractor_version=extraction.fields[0].extractor_version if extraction.fields else "",
        )
        self.db.add(row)
        await self.db.flush()
        for field in extraction.fields:
            await self.db.execute(
                insert(PrecedentCardField)
                .values(
                    card_id=row.id,
                    name=field.name,
                    value=field.value,
                    method=field.method.value,
                    quote=field.quote,
                    extractor_version=field.extractor_version,
                    input_hash=field.input_hash,
                )
                .on_conflict_do_nothing(constraint="uq_precedent_card_field")
            )
        await self.db.commit()
        return row.id

    async def texts_without_embedding(self, model: str) -> list[tuple[str, str]]:
        embedded = select(PrecedentEmbedding.text_hash).where(PrecedentEmbedding.model == model)
        rows = await self.db.execute(
            select(PrecedentCardRow.embedding_text_hash, func.min(PrecedentCardRow.fabula))
            .where(
                PrecedentCardRow.embedding_text_hash.is_not(None),
                PrecedentCardRow.embedding_text_hash.not_in(embedded),
            )
            .group_by(PrecedentCardRow.embedding_text_hash)
            .order_by(PrecedentCardRow.embedding_text_hash)
        )
        return [(h, t) for h, t in rows.all()]

    async def save_embeddings(self, model: str, vectors: list[tuple[str, list[float]]]) -> None:
        for text_hash, vector in vectors:
            await self.db.execute(
                insert(PrecedentEmbedding)
                .values(text_hash=text_hash, model=model, embedding=vector)
                .on_conflict_do_nothing(constraint="uq_precedent_embedding")
            )
        await self.db.commit()

    async def vector_channel(
        self, vector: list[float], model: str, article: str | None, limit: int
    ) -> list[tuple[int, float]]:
        distance = PrecedentEmbedding.embedding.cosine_distance(vector)
        stmt = (
            select(PrecedentCardRow.id, distance.label("distance"))
            .join(
                PrecedentEmbedding,
                (PrecedentEmbedding.text_hash == PrecedentCardRow.embedding_text_hash)
                & (PrecedentEmbedding.model == model),
            )
            .order_by(distance.asc(), PrecedentCardRow.id)
            .limit(limit)
        )
        if article is not None:
            # The prefilter matches any episode with this article, not only the main one.
            stmt = stmt.where(
                func.array_to_string(PrecedentCardRow.qualification_keys, " ").op("~")(
                    rf"(^| ){re.escape(article)}(/| |$)"
                )
            )
        rows = await self.db.execute(stmt)
        return [(card_id, round(1.0 - float(dist), 6)) for card_id, dist in rows.all()]

    async def qualification_channel(self, keys: frozenset[str], limit: int) -> list[int]:
        wanted = cast(sorted(keys), ARRAY(String(32)))
        rows = await self.db.execute(
            select(
                PrecedentCardRow.id,
                PrecedentCardRow.qualification_keys,
                PrecedentCardRow.decision_date,
            ).where(PrecedentCardRow.qualification_keys.op("&&")(wanted))
        )
        ranked = sorted(
            rows.all(),
            key=lambda r: (-len(keys & set(r[1])), -(r[2].toordinal() if r[2] else 0), r[0]),
        )
        return [card_id for card_id, _, _ in ranked[:limit]]

    async def fulltext_channel(self, text: str, limit: int) -> list[int]:
        words = list(dict.fromkeys(w.lower() for w in _WORD.findall(text)))[:MAX_QUERY_WORDS]
        if not words:
            return []
        query = func.to_tsquery("russian", literal(" | ".join(words)))
        document = func.to_tsvector("russian", PrecedentCardRow.fabula)
        rank = func.ts_rank(document, query)
        rows = await self.db.scalars(
            select(PrecedentCardRow.id)
            .where(document.op("@@")(query))
            .order_by(rank.desc(), PrecedentCardRow.id)
            .limit(limit)
        )
        return list(rows)

    async def load(self, ids: list[int]) -> dict[int, StoredCard]:
        if not ids:
            return {}
        rows = await self.db.scalars(select(PrecedentCardRow).where(PrecedentCardRow.id.in_(ids)))
        return {row.id: _stored(row) for row in rows}

    async def has_cards(self) -> bool:
        return await self.db.scalar(select(PrecedentCardRow.id).limit(1)) is not None

    async def cards_for_verdicts(self, verdict_ids: list[int]) -> dict[int, StoredCard]:
        if not verdict_ids:
            return {}
        # The first convicted person of a ruling stands for the uploaded document.
        rows = await self.db.scalars(
            select(PrecedentCardRow).where(
                PrecedentCardRow.knowledge_verdict_id.in_(verdict_ids),
                PrecedentCardRow.defendant_no == 0,
            )
        )
        return {row.knowledge_verdict_id: _stored(row) for row in rows}
