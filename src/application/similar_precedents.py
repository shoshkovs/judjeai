"""Use cases: index verdicts of the practice base as precedent cards, find precedents similar
to a draft.

Search runs three channels (fabula vector, qualification, full text), merges them by
Reciprocal Rank Fusion and then applies tri-state filters and the axis score of legal_core.
The vector only orders candidates; legal comparability is decided by filters and axes.
"""

import logging
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from legal_core.precedents import PrecedentCard
from legal_core.rules.precedent_matching import (
    FilterAction,
    FilterResult,
    MatchConfig,
    evaluate_candidate,
    reciprocal_rank_fusion,
)

from src.application.precedent_cards import CardExtraction, MultiVerdictReader, build_cards
from src.domain.embedding_text import build_embedding_text

logger = logging.getLogger(__name__)


class EmbeddingPort(Protocol):
    model_name: str

    async def embed_passages(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float] | None: ...


@dataclass(frozen=True)
class VerdictSource:
    """A verdict of the practice base waiting for its card."""

    knowledge_verdict_id: int
    text: str
    uid: str | None
    court_name: str | None
    decision_date: date | None
    source_url: str | None


@dataclass(frozen=True)
class StoredCard:
    id: int
    card: PrecedentCard
    knowledge_verdict_id: int
    uid: str | None
    court_name: str | None
    decision_date: date | None
    source_url: str | None


class PrecedentStore(Protocol):
    async def pending_verdicts(self, extractor_version: str) -> list[VerdictSource]: ...

    async def is_duplicate(
        self, text_hash: str, uid: str | None, defendant_no: int = 0
    ) -> bool: ...

    async def mark_not_practice(self, knowledge_verdict_id: int) -> None: ...

    async def save_card(
        self, source: VerdictSource, extraction: CardExtraction, embedding_text: str
    ) -> int: ...

    async def texts_without_embedding(self, model: str) -> list[tuple[str, str]]: ...

    async def save_embeddings(self, model: str, vectors: list[tuple[str, list[float]]]) -> None: ...

    async def vector_channel(
        self, vector: list[float], model: str, article: str | None, limit: int
    ) -> list[tuple[int, float]]: ...

    async def qualification_channel(self, keys: frozenset[str], limit: int) -> list[int]: ...

    async def fulltext_channel(self, text: str, limit: int) -> list[int]: ...

    async def load(self, ids: list[int]) -> dict[int, StoredCard]: ...

    async def cards_for_verdicts(self, verdict_ids: list[int]) -> dict[int, StoredCard]: ...

    async def has_cards(self) -> bool: ...


@dataclass(frozen=True)
class IndexReport:
    cards: int
    duplicates: int
    vectors: int
    vector_error: str | None = None


@dataclass(frozen=True)
class CardsReport:
    cards: int
    duplicates: int


@dataclass(frozen=True)
class VectorsReport:
    vectors: int
    error: str | None = None


class IndexPrecedents:
    """Cards are cheap (regex) and built first; vectors need a model and may fail on their own."""

    def __init__(self, store: PrecedentStore, reader: MultiVerdictReader, embedder: EmbeddingPort):
        self.store, self.reader, self.embedder = store, reader, embedder

    async def build_cards(self, extractor_version: str) -> CardsReport:
        cards = duplicates = 0
        for source in await self.store.pending_verdicts(extractor_version):
            extractions = build_cards(self.reader, source.text)
            if not extractions:  # an annulled sentence or an acquittal: never read it again
                await self.store.mark_not_practice(source.knowledge_verdict_id)
            for extraction in extractions:
                if await self.store.is_duplicate(
                    extraction.text_hash, source.uid, extraction.defendant_no
                ):
                    duplicates += 1
                    continue
                embedding_text = build_embedding_text(extraction.card.fabula)
                await self.store.save_card(source, extraction, embedding_text)
                cards += 1
        return CardsReport(cards, duplicates)

    async def embed_pending(self, batch: int = 32) -> VectorsReport:
        model = self.embedder.model_name
        pending = await self.store.texts_without_embedding(model)
        vectors = 0
        try:
            for start in range(0, len(pending), batch):
                chunk = pending[start : start + batch]
                embedded = await self.embedder.embed_passages([text for _, text in chunk])
                await self.store.save_embeddings(
                    model, [(h, v) for (h, _), v in zip(chunk, embedded, strict=True)]
                )
                vectors += len(chunk)
        except Exception as exc:  # the cards stay usable through the other two channels
            logger.warning("precedent embeddings failed", exc_info=True)
            return VectorsReport(vectors, f"{type(exc).__name__}: {exc}")
        return VectorsReport(vectors)

    async def execute(self, extractor_version: str, batch: int = 32) -> IndexReport:
        cards = await self.build_cards(extractor_version)
        vectors = await self.embed_pending(batch)
        return IndexReport(cards.cards, cards.duplicates, vectors.vectors, vectors.error)


@dataclass(frozen=True)
class SimilarPrecedent:
    stored: StoredCard
    score: float
    breakdown: dict[str, dict[str, float]]
    filters: tuple[FilterResult, ...]
    fusion_score: float
    channels: tuple[str, ...]
    text_similarity: float | None


@dataclass(frozen=True)
class SimilarPrecedents:
    items: list[SimilarPrecedent]
    candidates: int
    excluded: int
    vector_used: bool


class FindSimilarPrecedents:
    def __init__(
        self,
        store: PrecedentStore,
        embedder: EmbeddingPort,
        config: MatchConfig | None = None,
        channel_limit: int = 50,
    ):
        self.store, self.embedder = store, embedder
        self.config = config or MatchConfig()
        self.channel_limit = channel_limit

    async def _query_vector(self, text: str) -> list[float] | None:
        if not text:
            return None
        try:
            return await self.embedder.embed_query(text)
        except Exception:  # search continues on the qualification and full-text channels
            logger.warning("query embedding failed", exc_info=True)
            return None

    async def execute(self, query: PrecedentCard, limit: int = 10) -> SimilarPrecedents:
        text = build_embedding_text(query.fabula)
        vector = await self._query_vector(text)
        main = query.main_qualification
        prefilter = None
        if main is not None and self.config.action("same_article") is FilterAction.EXCLUDE:
            prefilter = main.article
        channels: dict[str, list[int]] = {}
        similarity: dict[int, float] = {}
        if vector is not None:
            found = await self.store.vector_channel(
                vector, self.embedder.model_name, prefilter, self.channel_limit
            )
            channels["vector"] = [card_id for card_id, _ in found]
            similarity = dict(found)
        if query.qualification_keys:
            channels["qualification"] = await self.store.qualification_channel(
                query.qualification_keys, self.channel_limit
            )
        if text:
            channels["fulltext"] = await self.store.fulltext_channel(text, self.channel_limit)
        fused = reciprocal_rank_fusion(list(channels.values()))
        stored = await self.store.load([card_id for card_id, _ in fused])
        results, excluded = [], 0
        for card_id, fusion in fused:
            if card_id not in stored:
                continue
            match = evaluate_candidate(
                query, stored[card_id].card, similarity.get(card_id), self.config
            )
            if match.excluded:
                excluded += 1
                continue
            results.append(
                SimilarPrecedent(
                    stored[card_id],
                    match.score,
                    match.breakdown,
                    match.filters,
                    round(fusion, 6),
                    tuple(name for name, ids in channels.items() if card_id in ids),
                    similarity.get(card_id),
                )
            )
        results.sort(key=lambda r: (-r.score, -r.fusion_score, r.stored.id))
        return SimilarPrecedents(results[:limit], len(fused), excluded, vector is not None)
