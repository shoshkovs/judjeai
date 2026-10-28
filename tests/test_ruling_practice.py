"""Integration: rulings of higher courts in the practice base -> one card per convicted person."""

from sqlalchemy import select

from src.application.precedent_cards import CARD_EXTRACTOR_VERSION
from src.application.similar_precedents import IndexPrecedents
from src.db.models import KnowledgeVerdict
from src.db.precedents import PrecedentCardField, PrecedentCardRow
from src.db.session import async_session_factory
from src.infrastructure.precedent_store import SqlPrecedentStore
from src.infrastructure.verdict_reader import PracticeReader
from tests.fixtures.fake_embedder import HashingEmbedder
from tests.fixtures.synthetic_rulings import ANNULLED, OLD_CASSATION_TWO


async def _add(text, uid):
    async with async_session_factory() as db:
        db.add(KnowledgeVerdict(raw_text=text, source_type="vsrf", uid=uid))
        await db.commit()


async def _index():
    async with async_session_factory() as db:
        use_case = IndexPrecedents(SqlPrecedentStore(db), PracticeReader(), HashingEmbedder())
        return await use_case.execute(CARD_EXTRACTOR_VERSION)


async def _cards():
    async with async_session_factory() as db:
        rows = await db.scalars(select(PrecedentCardRow).order_by(PrecedentCardRow.defendant_no))
        return rows.all()


async def test_ruling_gives_one_anonymised_card_per_convicted_person():
    await _add(OLD_CASSATION_TWO, "vsrf-1")
    report = await _index()
    assert report.cards == 2
    cards = await _cards()
    assert [(c.defendant_no, c.outcome, c.imprisonment_months) for c in cards] == [
        (0, "upheld", 216),
        (1, "upheld", 252),
    ]
    assert cards[0].knowledge_verdict_id == cards[1].knowledge_verdict_id
    assert all("Рябинин" not in c.fabula and "Дорохов" not in c.fabula for c in cards)
    async with async_session_factory() as db:
        outcome = await db.scalar(
            select(PrecedentCardField).where(
                PrecedentCardField.card_id == cards[1].id,
                PrecedentCardField.name == "ruling_outcome",
            )
        )
    assert outcome.value == "upheld" and "без изменения" in outcome.quote


async def test_indexing_a_ruling_twice_adds_nothing():
    await _add(OLD_CASSATION_TWO, "vsrf-1")
    await _index()
    again = await _index()
    assert again.cards == 0
    assert len(await _cards()) == 2


async def test_annulled_ruling_gives_no_card():
    await _add(ANNULLED, "vsrf-2")
    assert (await _index()).cards == 0
    assert await _cards() == []
    async with async_session_factory() as db:
        assert await db.scalar(select(KnowledgeVerdict.status)) == "not_practice"
        assert await SqlPrecedentStore(db).pending_verdicts(CARD_EXTRACTOR_VERSION) == []
