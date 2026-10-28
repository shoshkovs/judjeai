from legal_core.entities import ArticleReference, MeasureUnit, PunishmentKind

from src.services.punishment_extractor import extract_imposed_sentence, parse_term
from src.services.verdict_segmenter import segment_verdict
from tests.fixtures.synthetic_verdicts import (
    AMBIGUOUS,
    MULTI_COUNT,
    OTHER_KINDS,
    SINGLE_CONDITIONAL,
    WORDS_AND_DATIVE,
)


def _operative(text):
    return segment_verdict(text).operative


def test_parse_term_digits_words_and_dative():
    assert parse_term("2 (два) года 6 (шесть) месяцев") == (30, MeasureUnit.MONTHS)
    assert parse_term("один год шесть месяцев") == (18, MeasureUnit.MONTHS)
    assert parse_term("2 (двух) лет") == (24, MeasureUnit.MONTHS)
    assert parse_term("320 (триста двадцать) часов") == (320, MeasureUnit.HOURS)
    assert parse_term("на усмотрение суда") == (None, None)


def test_parse_term_nominative_teens_and_hundreds():
    # Restated sentences in appeal rulings use the nominative: "назначено пятнадцать лет".
    assert parse_term("пятнадцать лет два месяца") == (182, MeasureUnit.MONTHS)
    assert parse_term("восемнадцать лет") == (216, MeasureUnit.MONTHS)
    assert parse_term("двести сорок часов") == (240, MeasureUnit.HOURS)


def test_multi_count_with_aggregation():
    sentence = extract_imposed_sentence(_operative(MULTI_COUNT)).sentence
    assert [c.reference for c in sentence.counts] == [
        ArticleReference("158", 3),
        ArticleReference("158", 2),
    ]
    assert [c.main.imprisonment_months for c in sentence.counts] == [30, 20]
    assert sentence.final.imprisonment_months == 36
    assert sentence.aggregation_rule == "ч. 3 ст. 69 УК РФ"
    assert not sentence.conditional
    assert "окончательно" in sentence.final_quote
    assert "2 (два) года 6 (шесть) месяцев" in sentence.counts[0].quote
    # The quote of a count must not swallow the next count's points («в, г»).
    assert sentence.counts[1].qualification == "п.п. «в, г» ч. 2 ст. 158 УК РФ"


def test_single_count_conditional_with_probation():
    sentence = extract_imposed_sentence(_operative(SINGLE_CONDITIONAL)).sentence
    assert len(sentence.counts) == 1
    count = sentence.counts[0]
    assert count.reference == ArticleReference("158", 2)
    assert count.main.imprisonment_months == 54
    assert sentence.conditional and sentence.probation_months == 24
    assert sentence.final is None


def test_hours_and_fine():
    sentence = extract_imposed_sentence(_operative(OTHER_KINDS)).sentence
    kinds = [(c.reference, c.main.kind, c.main.amount, c.main.unit) for c in sentence.counts]
    assert kinds == [
        (ArticleReference("158", 1), PunishmentKind.COMPULSORY_WORKS, 320, MeasureUnit.HOURS),
        (ArticleReference("159", 1), PunishmentKind.FINE, 100_000, MeasureUnit.RUBLES),
    ]


def test_words_dative_and_additional_punishment():
    sentence = extract_imposed_sentence(_operative(WORDS_AND_DATIVE)).sentence
    first, second = sentence.counts
    assert first.main.imprisonment_months == 18
    assert [(a.kind, a.amount) for a in first.additional] == [
        (PunishmentKind.RESTRICTION_OF_FREEDOM, 6)
    ]
    assert second.reference == ArticleReference("161", 2)
    assert second.main.imprisonment_months == 24


def test_unparseable_term_is_reported_as_ambiguous_window():
    result = extract_imposed_sentence(_operative(AMBIGUOUS))
    assert result.sentence.counts == ()
    assert len(result.ambiguous_windows) == 1
    assert "лишения свободы" in result.ambiguous_windows[0]


def test_empty_operative_gives_empty_sentence():
    assert extract_imposed_sentence("").sentence.is_empty
