from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from src.db import models  # noqa: F401  (registers tables on Base.metadata)
from src.db.base import Base
from tests.conftest import alembic_config


async def test_migrations_match_models():
    """`alembic upgrade head` must produce exactly the schema the models describe."""
    from src.db.session import engine

    async with engine.connect() as conn:
        diff = await conn.run_sync(
            lambda sync_conn: compare_metadata(MigrationContext.configure(sync_conn), Base.metadata)
        )
    assert diff == []


def test_downgrade_and_upgrade_roundtrip():
    cfg = alembic_config()
    command.downgrade(cfg, "0001")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


def test_legal_library_downgrade_keeps_existing_case_data():
    import asyncio

    from src.db.models import Chat
    from src.db.session import async_session_factory

    async def create_case():
        async with async_session_factory() as db:
            chat = Chat(title="Case survives legal-library rollback")
            db.add(chat)
            await db.commit()
            return chat.id

    async def read_case(chat_id):
        async with async_session_factory() as db:
            from sqlalchemy import text

            return await db.scalar(text("SELECT title FROM chats WHERE id = :id"), {"id": chat_id})

    chat_id = asyncio.run(create_case())
    cfg = alembic_config()
    command.downgrade(cfg, "0002")
    try:
        assert asyncio.run(read_case(chat_id)) == "Case survives legal-library rollback"
    finally:
        command.upgrade(cfg, "head")
    assert asyncio.run(read_case(chat_id)) == "Case survives legal-library rollback"


def test_auth_backfill_and_rollback_preserve_old_case():
    import asyncio

    from sqlalchemy import text

    from src.db.session import async_session_factory

    async def execute(sql):
        async with async_session_factory() as db:
            result = await db.execute(text(sql))
            value = result.first() if result.returns_rows else None
            await db.commit()
            return value

    cfg = alembic_config()
    command.downgrade(cfg, "0004_criminal_article_sanctions")
    try:
        row = asyncio.run(
            execute("INSERT INTO chats (title) VALUES ('Legacy protected') RETURNING id")
        )
        command.upgrade(cfg, "head")
        owner = asyncio.run(
            execute(
                "SELECT u.username, u.is_active, u.password_hash FROM chats c "
                f"JOIN users u ON u.id = c.owner_id WHERE c.id = {int(row[0])}"
            )
        )
        assert tuple(owner) == ("__legacy_locked__", False, None)
        command.downgrade(cfg, "0004_criminal_article_sanctions")
        assert (
            asyncio.run(execute(f"SELECT title FROM chats WHERE id = {int(row[0])}"))[0]
            == "Legacy protected"
        )
    finally:
        command.upgrade(cfg, "head")


def test_defendant_cards_downgrade_keeps_the_first_card_and_the_verdict():
    import asyncio

    from sqlalchemy import text

    from src.db.session import async_session_factory

    async def execute(sql):
        async with async_session_factory() as db:
            result = await db.execute(text(sql))
            rows = result.all() if result.returns_rows else None
            await db.commit()
            return rows

    card = (
        "INSERT INTO precedent_cards (knowledge_verdict_id, defendant_no, text_sha256, "
        "qualification_keys, mitigating, aggravating, applied_general, conditional, episodes, "
        "fabula, extractor_version) VALUES ({v}, {n}, 'h', '{{}}', '{{}}', '{{}}', '{{}}', false, "
        "'[]', '', 'precedent-card/2')"
    )
    [(verdict_id,)] = asyncio.run(
        execute(
            "INSERT INTO knowledge_verdicts (raw_text, source_type, status) "
            "VALUES ('ruling', 'vsrf', 'indexed') "
            "RETURNING id"
        )
    )
    asyncio.run(execute(card.format(v=verdict_id, n=0)))
    asyncio.run(execute(card.format(v=verdict_id, n=1)))
    cfg = alembic_config()
    command.downgrade(cfg, "0008_precedent_embeddings")
    try:
        assert asyncio.run(execute("SELECT count(*) FROM precedent_cards"))[0][0] == 1
        assert asyncio.run(execute("SELECT count(*) FROM knowledge_verdicts"))[0][0] == 1
    finally:
        command.upgrade(cfg, "head")
    assert asyncio.run(execute("SELECT defendant_no FROM precedent_cards"))[0][0] == 0
