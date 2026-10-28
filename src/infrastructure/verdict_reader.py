"""Adapter: the regex VerdictReader used for both drafts and practice verdicts."""

from src.application.precedent_cards import VerdictReading
from src.infrastructure.circumstance_extractor import extract_circumstances
from src.infrastructure.fabula import extract_fabula
from src.infrastructure.ruling_reader import RulingReader, is_ruling
from src.services.punishment_extractor import extract_imposed_sentence
from src.services.verdict_segmenter import segment_verdict


class RegexVerdictReader:
    def read(self, text: str) -> VerdictReading:
        segments = segment_verdict(text)
        sentence = extract_imposed_sentence(segments.operative).sentence
        circumstances = extract_circumstances(
            segments.description, segments.operative, segments.introduction
        )
        return VerdictReading(
            introduction=segments.introduction,
            description=segments.description,
            operative=segments.operative,
            sentence=sentence,
            fabula=extract_fabula(segments.description),
            mitigating=circumstances.mitigating,
            aggravating=circumstances.aggravating,
            applied_general=circumstances.applied_general,
            recidivism=circumstances.recidivism,
            recidivism_quote=circumstances.recidivism_quote,
        )

    def read_all(self, text: str) -> list[VerdictReading]:
        return [self.read(text)]


class PracticeReader(RegexVerdictReader):
    """Reader of the practice base: a ruling gives a reading per convicted person."""

    def read_all(self, text: str) -> list[VerdictReading]:
        if is_ruling(text):
            return RulingReader().read_all(text)
        return [self.read(text)]
