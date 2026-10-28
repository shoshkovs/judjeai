"""Practice base: precedent cards, their extracted-field registry and fabula embeddings.

A card is derived from a verdict of the knowledge base and keeps its provenance (verdict,
text SHA-256, court, date, URL when known). A ruling of a higher court gives one card per
convicted person (defendant_no) with the court's outcome; a verdict gives card 0. The stored
fabula is already anonymised; the raw text stays only in knowledge_verdicts.
"""

from datetime import date, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.config import settings
from src.db.base import Base


class PrecedentCardRow(Base):
    __tablename__ = "precedent_cards"
    __table_args__ = (
        Index("ix_precedent_cards_main", "main_article", "main_part"),
        Index("ix_precedent_cards_qualifications", "qualification_keys", postgresql_using="gin"),
        Index(
            "ix_precedent_cards_fabula_tsv",
            text("to_tsvector('russian', fabula)"),
            postgresql_using="gin",
        ),
        UniqueConstraint(
            "knowledge_verdict_id", "defendant_no", name="uq_precedent_card_verdict_defendant"
        ),
        UniqueConstraint("text_sha256", "defendant_no", name="uq_precedent_card_text_defendant"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    knowledge_verdict_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_verdicts.id", ondelete="CASCADE")
    )
    defendant_no: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # "upheld" / "changed" for a sentence restated in a ruling; None for a verdict.
    outcome: Mapped[str | None] = mapped_column(String(16))
    text_sha256: Mapped[str] = mapped_column(String(64))
    uid: Mapped[str | None] = mapped_column(String(64), index=True)
    court_name: Mapped[str | None] = mapped_column(Text)
    decision_date: Mapped[date | None] = mapped_column(Date)
    source_url: Mapped[str | None] = mapped_column(Text)
    main_article: Mapped[str | None] = mapped_column(String(16))
    main_part: Mapped[int | None] = mapped_column(Integer)
    qualification_keys: Mapped[list[str]] = mapped_column(ARRAY(String(32)))
    mitigating: Mapped[list[str]] = mapped_column(ARRAY(String(16)))
    aggravating: Mapped[list[str]] = mapped_column(ARRAY(String(16)))
    applied_general: Mapped[list[str]] = mapped_column(ARRAY(String(8)))
    recidivism: Mapped[str | None] = mapped_column(String(32))
    conditional: Mapped[bool] = mapped_column(Boolean)
    imprisonment_months: Mapped[int | None] = mapped_column(Integer)
    episodes: Mapped[list] = mapped_column(JSONB)
    final: Mapped[dict | None] = mapped_column(JSONB)
    fabula: Mapped[str] = mapped_column(Text)
    embedding_text_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    extractor_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PrecedentCardField(Base):
    __tablename__ = "precedent_card_fields"
    __table_args__ = (
        UniqueConstraint(
            "card_id", "name", "extractor_version", "input_hash", name="uq_precedent_card_field"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    card_id: Mapped[int] = mapped_column(
        ForeignKey("precedent_cards.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(64))
    value: Mapped[object | None] = mapped_column(JSONB)
    method: Mapped[str] = mapped_column(String(16))
    quote: Mapped[str] = mapped_column(Text)
    extractor_version: Mapped[str] = mapped_column(String(64))
    input_hash: Mapped[str] = mapped_column(String(64))


class PrecedentEmbedding(Base):
    """Vector of an anonymised fabula; cards with the same text share one vector."""

    __tablename__ = "precedent_embeddings"
    __table_args__ = (UniqueConstraint("text_hash", "model", name="uq_precedent_embedding"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    text_hash: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(255))
    embedding = mapped_column(Vector(settings.EMBEDDING_DIM))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
