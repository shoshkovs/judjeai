"""Use case: a verdict text -> precedent card plus the registry of its extracted fields.

The same code reads the user's draft (the query) and every verdict of the practice base, so
both sides of a comparison are extracted identically. Reading the text is delegated to a
VerdictReader port; this module only assembles the domain card and its provenance.

A ruling of a higher court restates the sentences of several convicted persons; the practice
base keeps one card per person (defendant_no) together with the higher court's outcome.
"""

from dataclasses import dataclass
from typing import Protocol

from legal_core.entities import ImposedSentence, PunishmentTerm
from legal_core.precedents import (
    PrecedentCard,
    PrecedentEpisode,
    Recidivism,
    parse_qualifications,
)

from src.domain.extraction import ExtractedField, ExtractionMethod, input_hash

CARD_EXTRACTOR_VERSION = "precedent-card/2"


@dataclass(frozen=True)
class VerdictReading:
    """What a reader found in one verdict text; quotes are literal fragments of that text."""

    introduction: str
    description: str
    operative: str
    sentence: ImposedSentence
    fabula: str
    mitigating: dict[str, str]
    aggravating: dict[str, str]
    applied_general: dict[str, str]
    recidivism: Recidivism | None
    recidivism_quote: str
    # Set for a person's sentence restated in a ruling: "upheld" or "changed".
    outcome: str | None = None
    outcome_quote: str = ""


class VerdictReader(Protocol):
    def read(self, text: str) -> VerdictReading: ...


class MultiVerdictReader(VerdictReader, Protocol):
    def read_all(self, text: str) -> list[VerdictReading]: ...


@dataclass(frozen=True)
class CardExtraction:
    card: PrecedentCard
    fields: tuple[ExtractedField, ...]
    text_hash: str
    defendant_no: int = 0
    outcome: str | None = None


def _term_json(term: PunishmentTerm | None) -> dict | None:
    if term is None:
        return None
    return {
        "kind": term.kind.value,
        "amount": term.amount,
        "unit": term.unit.value if term.unit else None,
    }


def _episodes(reading: VerdictReading) -> tuple[PrecedentEpisode, ...]:
    episodes: list[PrecedentEpisode] = []
    for count in reading.sentence.counts:
        found = parse_qualifications(count.qualification) or parse_qualifications(count.quote)
        if found:
            episodes.append(PrecedentEpisode(found[0], count.main))
    if episodes:
        return tuple(episodes)
    qualifications = parse_qualifications(reading.operative) or parse_qualifications(
        reading.introduction
    )
    return tuple(PrecedentEpisode(q) for q in qualifications)


def build_card(reader: VerdictReader, text: str) -> CardExtraction:
    return _extraction(reader.read(text), text, 0)


def build_cards(reader: MultiVerdictReader, text: str) -> list[CardExtraction]:
    """One card per convicted person; a first-instance verdict gives exactly one."""
    return [_extraction(reading, text, n) for n, reading in enumerate(reader.read_all(text))]


def _extraction(reading: VerdictReading, text: str, defendant_no: int) -> CardExtraction:
    episodes = _episodes(reading)
    sentence = reading.sentence
    card = PrecedentCard(
        episodes=episodes,
        mitigating=frozenset(reading.mitigating),
        aggravating=frozenset(reading.aggravating),
        recidivism=reading.recidivism,
        applied_general=frozenset(reading.applied_general),
        final=sentence.final,
        conditional=sentence.conditional,
        fabula=reading.fabula,
    )
    source = input_hash(text)
    version = CARD_EXTRACTOR_VERSION

    def found(name: str, value, quote: str) -> ExtractedField:
        if value in (None, [], {}, "") or not quote.strip():
            return ExtractedField.absent(name, version, source)
        return ExtractedField(name, value, ExtractionMethod.REGEX, quote, version, source)

    count_quotes = " … ".join(c.quote for c in sentence.counts if c.quote)
    fields = (
        found(
            "qualifications",
            [e.qualification.key for e in episodes],
            count_quotes or reading.operative[:300] or reading.introduction[:300],
        ),
        found(
            "counts",
            [
                {"qualification": e.qualification.key, "punishment": _term_json(e.punishment)}
                for e in episodes
                if e.punishment is not None
            ],
            count_quotes,
        ),
        found("final_sentence", _term_json(sentence.final), sentence.final_quote),
        found("conditional", True if sentence.conditional else None, reading.operative[:300]),
        found("mitigating", sorted(reading.mitigating), " … ".join(reading.mitigating.values())),
        found("aggravating", sorted(reading.aggravating), " … ".join(reading.aggravating.values())),
        found(
            "applied_general",
            sorted(reading.applied_general),
            " … ".join(dict.fromkeys(reading.applied_general.values())),
        ),
        found(
            "recidivism",
            reading.recidivism.value if reading.recidivism else None,
            reading.recidivism_quote,
        ),
        found("fabula", reading.fabula or None, reading.fabula[:300]),
    )
    if reading.outcome is not None:
        fields += (found("ruling_outcome", reading.outcome, reading.outcome_quote),)
    return CardExtraction(card, fields, source, defendant_no, reading.outcome)


def query_card(reader: VerdictReader, texts: list[str]) -> PrecedentCard | None:
    """Card of a draft: the first document whose qualification was read, else the first one."""
    cards = [build_card(reader, text).card for text in texts if text.strip()]
    if not cards:
        return None
    return next((card for card in cards if card.episodes), cards[0])
