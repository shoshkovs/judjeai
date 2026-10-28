"""One precedent card per convicted person of a ruling, with the higher court's outcome.

Backfill: existing cards are verdicts and become defendant 0 (server default). Downgrade
deletes the cards of the second and further persons (derived data: `python -m
src.cli.precedents index` rebuilds them after an upgrade) and restores the one-card-per-verdict
constraints; verdicts in knowledge_verdicts are untouched.
"""
import sqlalchemy as sa
from alembic import op

revision = "0009_precedent_defendants"
down_revision = "0008_precedent_embeddings"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "precedent_cards",
        sa.Column("defendant_no", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("precedent_cards", sa.Column("outcome", sa.String(16)))
    op.drop_constraint(
        "precedent_cards_knowledge_verdict_id_key", "precedent_cards", type_="unique"
    )
    op.drop_constraint("precedent_cards_text_sha256_key", "precedent_cards", type_="unique")
    op.create_unique_constraint(
        "uq_precedent_card_verdict_defendant",
        "precedent_cards",
        ["knowledge_verdict_id", "defendant_no"],
    )
    op.create_unique_constraint(
        "uq_precedent_card_text_defendant", "precedent_cards", ["text_sha256", "defendant_no"]
    )


def downgrade():
    op.execute("DELETE FROM precedent_cards WHERE defendant_no > 0")
    op.drop_constraint("uq_precedent_card_text_defendant", "precedent_cards", type_="unique")
    op.drop_constraint("uq_precedent_card_verdict_defendant", "precedent_cards", type_="unique")
    op.create_unique_constraint(
        "precedent_cards_text_sha256_key", "precedent_cards", ["text_sha256"]
    )
    op.create_unique_constraint(
        "precedent_cards_knowledge_verdict_id_key", "precedent_cards", ["knowledge_verdict_id"]
    )
    op.drop_column("precedent_cards", "outcome")
    op.drop_column("precedent_cards", "defendant_no")
