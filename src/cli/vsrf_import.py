"""Import rulings of the Supreme Court into the practice base from a registry export.

Usage: python -m src.cli.vsrf_import --db acts.db --texts texts.zip [--index]

acts.db is the registry (table `acts`, one row per case, metadata in `parsed_json`); texts are
`<pdf_id>.txt` files in a ZIP archive or a directory. Only texts recognised as rulings are
imported; the court and date are those of the first-instance sentence the ruling decided on.
Names of persons from the registry are never stored: the raw text stays in knowledge_verdicts
like any uploaded verdict, and the cards keep only anonymised fabulas. Idempotent by
`vsrf:<claim_id>`. --index then builds the cards and vectors (python -m src.cli.precedents
index does the same later).
"""

import argparse
import asyncio
import json
import sqlite3
import zipfile
from collections import Counter
from datetime import date
from pathlib import Path

from sqlalchemy import select

from src.cli.precedents import run_index
from src.db.models import KnowledgeChunk, KnowledgeVerdict
from src.db.session import async_session_factory
from src.domain.practice import PRIVATE_SOURCE_TYPES
from src.infrastructure.ruling_reader import is_ruling
from src.services.knowledge_service import build_chunks, calculate_text_hash

SOURCE_TYPE = "vsrf"
UID_PREFIX = "vsrf:"
BATCH = 200


class _Texts:
    """`<pdf_id>.txt` from a ZIP archive (any folder inside it) or from a directory."""

    def __init__(self, path: Path):
        self.zip = zipfile.ZipFile(path) if zipfile.is_zipfile(path) else None
        if self.zip is not None:
            self.names = {
                Path(n).name[: -len(".txt")]: n for n in self.zip.namelist() if n.endswith(".txt")
            }
        else:
            self.names = {p.stem: p for p in Path(path).glob("*.txt")}

    def get(self, pdf_id: str) -> str | None:
        name = self.names.get(pdf_id)
        if name is None:
            return None
        if self.zip is not None:
            return self.zip.read(name).decode("utf-8", errors="replace")
        return Path(name).read_text(encoding="utf-8", errors="replace")


def _date(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _rows(db_path: Path):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        yield from con.execute(
            "SELECT claim_id, case_number, first_court, pdf_id, parsed_json FROM acts "
            "WHERE pdf_id IS NOT NULL AND pdf_id != '' ORDER BY claim_id"
        )
    finally:
        con.close()


def _verdict(claim_id, case_number, first_court, parsed: dict, text: str) -> KnowledgeVerdict:
    verdict = KnowledgeVerdict(
        uid=f"{UID_PREFIX}{claim_id}"[:64],
        case_number=(case_number or "")[:64] or None,
        court_name=first_court or parsed.get("first_court"),
        verdict_date=_date(parsed.get("first_date")),
        source_type=SOURCE_TYPE,
        source_filename=(parsed.get("pdf_url") or "")[:512] or None,
        file_hash=calculate_text_hash(text),
        raw_text=text,
        # Only what describes the document; persons of the registry stay out.
        structured={"registry": {"claim_id": claim_id, "instance": parsed.get("instance")}},
        status="indexed_no_vectors",
    )
    verdict.chunks = [KnowledgeChunk(chunk_type="text", content=c) for c in build_chunks(text)]
    return verdict


async def run_import(db_path: Path, texts_path: Path, index: bool = False, embedder=None) -> dict:
    texts = _Texts(Path(texts_path))
    skipped: Counter[str] = Counter()
    imported = 0
    async with async_session_factory() as db:
        known = set(
            await db.scalars(
                select(KnowledgeVerdict.uid).where(KnowledgeVerdict.source_type == SOURCE_TYPE)
            )
        )
        # Several registry rows can point to one PDF; a text is kept once in the shared base.
        hashes = set(
            await db.scalars(
                select(KnowledgeVerdict.file_hash).where(
                    KnowledgeVerdict.source_type.not_in(PRIVATE_SOURCE_TYPES)
                )
            )
        )
        pending = 0
        for claim_id, case_number, first_court, pdf_id, parsed_json in _rows(Path(db_path)):
            if f"{UID_PREFIX}{claim_id}" in known:
                skipped["already_imported"] += 1
                continue
            text = texts.get(str(pdf_id))
            if text is None or not text.strip():
                skipped["no_text"] += 1
                continue
            if not is_ruling(text):
                skipped["not_a_ruling"] += 1
                continue
            text_hash = calculate_text_hash(text)
            if text_hash in hashes:
                skipped["same_text"] += 1
                continue
            hashes.add(text_hash)
            parsed = json.loads(parsed_json or "{}")
            db.add(_verdict(claim_id, case_number, first_court, parsed, text))
            imported += 1
            pending += 1
            if pending >= BATCH:
                await db.commit()
                pending = 0
        await db.commit()
    report = {"imported": imported, "skipped": dict(skipped)}
    if index:
        report["index"] = await run_index(embedder)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="registry database acts.db")
    parser.add_argument("--texts", type=Path, required=True, help="texts.zip or a directory")
    parser.add_argument("--index", action="store_true", help="build cards and vectors after")
    args = parser.parse_args()
    report = asyncio.run(run_import(args.db, args.texts, index=args.index))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
