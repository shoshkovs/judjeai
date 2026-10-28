"""Import of Supreme Court rulings from the registry database (acts.db) and their texts."""

import json
import sqlite3
import zipfile
from datetime import date

from sqlalchemy import func, select

from src.cli.vsrf_import import run_import
from src.db.models import KnowledgeVerdict
from src.db.precedents import PrecedentCardRow
from src.db.session import async_session_factory
from tests.fixtures.fake_embedder import HashingEmbedder
from tests.fixtures.synthetic_rulings import ANNULLED, MODERN_UPHELD, OLD_CASSATION_TWO
from tests.fixtures.synthetic_verdicts import PRACTICE_THEFT

# The registry schema as exported from the Supreme Court site.
SCHEMA = """CREATE TABLE acts (claim_id TEXT PRIMARY KEY, case_number TEXT, act_kind TEXT,
instance TEXT, department TEXT, register_date TEXT, first_court TEXT, first_case_number TEXT,
first_act_type TEXT, first_judge TEXT, final_act_type TEXT, final_date TEXT,
final_judgement TEXT, final_reporter TEXT, pdf_id TEXT, articles TEXT,
parsed_json TEXT NOT NULL, updated_at TEXT)"""


def _registry(tmp_path, texts: dict[str, str]):
    db_path = tmp_path / "acts.db"
    con = sqlite3.connect(db_path)
    con.execute(SCHEMA)
    rows = [
        ("18-1", "4-О06-81СП", "1", OLD_CASSATION_TWO, "2006-04-11"),
        ("18-2", "5-УД24-1-К1", "2", MODERN_UPHELD, "2023-02-02"),
        ("18-3", "1-О10-1", "3", ANNULLED, "2010-03-01"),
        ("18-4", "1-Д20-1", "4", PRACTICE_THEFT, "2020-01-01"),  # not a ruling
        ("18-5", "1-О11-1", "5", None, "2011-01-01"),  # no text downloaded
        ("18-6", "4-О06-81СП", "1", None, "2006-04-11"),  # second registry row of the same PDF
    ]
    for claim_id, case_number, pdf_id, text, first_date in rows:
        parsed = {
            "first_court": "Условный областной суд",
            "first_date": first_date,
            "pdf_url": f"https://example.invalid/stor_pdf/{pdf_id}",
            "personalities": [{"persons": [{"short_name": "Иванов Иван Иванович"}]}],
        }
        con.execute(
            "INSERT INTO acts (claim_id, case_number, act_kind, first_court, pdf_id, "
            "parsed_json) VALUES (?, ?, 'Определение', 'Условный областной суд', ?, ?)",
            (claim_id, case_number, pdf_id, json.dumps(parsed, ensure_ascii=False)),
        )
        if text is not None:
            texts[pdf_id] = text
    con.commit()
    con.close()
    archive = tmp_path / "texts.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for pdf_id, text in texts.items():
            zf.writestr(f"texts/{pdf_id}.txt", text)
            zf.writestr(f"texts/{pdf_id}.meta.json", "{}")
    return db_path, archive


async def test_import_keeps_only_rulings_with_metadata_and_no_registry_names(tmp_path):
    db_path, archive = _registry(tmp_path, {})
    report = await run_import(db_path, archive, index=True, embedder=HashingEmbedder())
    assert report["imported"] == 3  # two upheld rulings and the annulled one (no card)
    assert report["skipped"] == {"not_a_ruling": 1, "no_text": 1, "same_text": 1}
    assert report["index"]["cards"] == 3  # two persons + one person
    async with async_session_factory() as db:
        verdicts = (await db.scalars(select(KnowledgeVerdict).order_by(KnowledgeVerdict.uid))).all()
        cards = await db.scalar(select(func.count()).select_from(PrecedentCardRow))
    assert [v.uid for v in verdicts] == ["vsrf:18-1", "vsrf:18-2", "vsrf:18-3"]
    first = verdicts[0]
    assert first.source_type == "vsrf" and first.case_number == "4-О06-81СП"
    assert first.court_name == "Условный областной суд"
    assert first.verdict_date == date(2006, 4, 11)  # the sentence, not the ruling
    assert first.source_filename == "https://example.invalid/stor_pdf/1"
    assert "Иванов Иван" not in json.dumps(first.structured, ensure_ascii=False)
    assert cards == 3


async def test_import_is_idempotent(tmp_path):
    db_path, archive = _registry(tmp_path, {})
    await run_import(db_path, archive)
    again = await run_import(db_path, archive)
    assert again["imported"] == 0 and again["skipped"]["already_imported"] == 3
    assert again["skipped"]["same_text"] == 1
