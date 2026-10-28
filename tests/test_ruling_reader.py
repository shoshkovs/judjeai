from legal_core.entities import PunishmentKind

from src.application.precedent_cards import build_cards
from src.infrastructure.ruling_reader import RulingOutcome, RulingReader, is_ruling
from src.infrastructure.verdict_reader import PracticeReader
from tests.fixtures.synthetic_rulings import (
    ACQUITTAL,
    ANNULLED,
    MODERN_UPHELD,
    OLD_CASSATION_TWO,
    PUNISHMENT_CHANGED,
)
from tests.fixtures.synthetic_verdicts import PRACTICE_THEFT

READER = RulingReader()


def _terms(reading):
    return [
        (count.reference.article if count.reference else None, count.main.kind, count.main.amount)
        for count in reading.sentence.counts
    ]


def test_rulings_are_told_apart_from_first_instance_verdicts():
    for text in (OLD_CASSATION_TWO, MODERN_UPHELD, PUNISHMENT_CHANGED, ANNULLED, ACQUITTAL):
        assert is_ruling(text)
    assert not is_ruling(PRACTICE_THEFT)
    # The military collegium and the Presidium of the Supreme Court decide on sentences too.
    assert is_ruling(OLD_CASSATION_TWO.replace("по уголовным делам", "по делам военнослужащих"))
    presidium = "ПОСТАНОВЛЕНИЕ ПРЕЗИДИУМА ВЕРХОВНОГО СУДА РОССИЙСКОЙ ФЕДЕРАЦИИ\nо возобновлении"
    assert is_ruling(presidium + " производства по делу ввиду новых обстоятельств")


def test_each_convicted_person_is_read_separately_without_prior_convictions():
    first, second = READER.read_all(OLD_CASSATION_TWO)
    imprisonment = PunishmentKind.IMPRISONMENT
    assert _terms(first) == [
        ("116", PunishmentKind.CORRECTIONAL_WORKS, 2),
        ("166", imprisonment, 48),
        ("105", imprisonment, 180),
    ]
    assert first.sentence.final.amount == 216  # "по совокупности … к 18 годам"
    # The page number inside "лишения свободы" is dropped; the 2000 conviction is not a count.
    assert _terms(second) == [("166", imprisonment, 72), ("105", imprisonment, 192)]
    assert second.sentence.final.amount == 252
    assert first.outcome == second.outcome == RulingOutcome.UPHELD
    assert "оставить без изменения" in first.outcome_quote


def test_modern_layout_page_numbers_ocr_and_nominative_numerals():
    [reading] = READER.read_all(MODERN_UPHELD)
    assert _terms(reading) == [
        ("105", PunishmentKind.IMPRISONMENT, 180),
        ("158", PunishmentKind.IMPRISONMENT, 24),  # "ч.З" is part 3; "к 2 2 годам" is 2 years
    ]
    assert reading.sentence.counts[1].reference.part == 3
    assert reading.sentence.final.kind is PunishmentKind.IMPRISONMENT
    assert reading.sentence.final.amount == 198  # not the additional restriction of freedom
    # The appellate change credited time in custody only, so the terms stay.
    assert reading.outcome == RulingOutcome.UPHELD


def test_names_of_the_convicted_never_reach_the_fabula():
    # Rulings name the convicted by bare surname in any case: "Рябинин и Дорохов признаны".
    for text, surnames in (
        (OLD_CASSATION_TWO, ("Рябинин", "Дорохов")),
        (MODERN_UPHELD, ("Лаврентьев",)),
    ):
        for reading in READER.read_all(text):
            assert reading.fabula
            assert not any(name in reading.fabula for name in surnames), reading.fabula


def test_names_written_irregularly_are_removed_too():
    text = (
        OLD_CASSATION_TWO.replace("РЯБИНИН С. Е.,", "Харитов А иг из Вагизович, несудимый,")
        .replace("ДОРОХОВ С. С.,", "Пух (до 2017 г. Астахов) Павел Львович,")
        .replace("Рябинин и Дорохов признаны", "Харитов, Пу\xad ха и Пуху признаны")
    )
    for reading in READER.read_all(text):
        assert not any(n in reading.fabula for n in ("Харитов", "Пух", "Пу ха")), reading.fabula


def test_names_introduced_after_the_head_are_removed():
    text = OLD_CASSATION_TWO.replace(
        "Рябинин и Дорохов признаны",
        "в отношении Ондара От-Даша Чыргал-ооловича, а также осужденного Клипа Иона. Они признаны",
    ).replace("Наказание назначено", "Ондар и Клипа ранее не судимы. Наказание назначено")
    for reading in READER.read_all(text):
        assert "Ондар" not in reading.fabula and "Клип" not in reading.fabula, reading.fabula


def test_changed_punishment_keeps_qualifications_and_drops_stale_terms():
    [reading] = READER.read_all(PUNISHMENT_CHANGED)
    assert reading.outcome == RulingOutcome.CHANGED
    assert "смягчить наказание" in reading.outcome_quote
    assert reading.sentence.is_empty
    [card] = build_cards(PracticeReader(), PUNISHMENT_CHANGED)
    assert card.card.qualification_keys == {"105/2/з", "162/4"}
    assert card.card.imprisonment_months is None and card.outcome == "changed"


def test_annulled_sentences_and_acquittals_give_no_precedent():
    assert READER.read_all(ANNULLED) == []
    assert READER.read_all(ACQUITTAL) == []


def test_practice_reader_builds_one_card_per_convicted_person():
    reader = PracticeReader()
    cards = build_cards(reader, OLD_CASSATION_TWO)
    assert [c.defendant_no for c in cards] == [0, 1]
    assert [c.card.imprisonment_months for c in cards] == [216, 252]
    assert cards[0].card.main_qualification.key == "116/1"
    assert {c.outcome for c in cards} == {"upheld"}
    outcome = next(f for f in cards[0].fields if f.name == "ruling_outcome")
    assert outcome.value == "upheld" and "без изменения" in outcome.quote
    # A first-instance verdict is still one card read by the verdict reader.
    [single] = build_cards(reader, PRACTICE_THEFT)
    assert single.defendant_no == 0 and single.outcome is None
    assert all(f.name != "ruling_outcome" for f in single.fields)
