"""Adapter: a ruling of a higher court (appeal or cassation) -> one reading per convicted person.

A ruling imposes nothing itself. It restates the first-instance sentence of every convicted
person ("осужден по ч. 2 ст. 105 УК РФ к 15 годам …") and its resolution upholds, changes or
annuls it. Only sentences that survived are practice:

- annulled, partly annulled or unclear outcome -> no reading at all;
- changed, and the change touches the punishment -> qualifications only, the restated terms
  are stale;
- upheld, or changed in something else (credit of custody, a seized phone) -> full sentence.

Regex only. The rules follow the PDF text of real rulings: words broken over lines, page
numbers inside sentences ("лишения 2 свободы", "к 2 2 годам"), the article written before its
part ("ст. 166 ч. 2 п. п. «а, в»"), OCR letters for digits ("ч.З ст.ЗО"), terms in words in
the nominative ("назначено пятнадцать лет") and the resolution printed before "ОПРЕДЕЛИЛА:".
"""

import re
from enum import StrEnum

from legal_core.entities import ImposedSentence

from src.application.precedent_cards import VerdictReading
from src.infrastructure.circumstance_extractor import extract_circumstances
from src.infrastructure.fabula import extract_fabula
from src.services.punishment_extractor import extract_imposed_sentence


class RulingOutcome(StrEnum):
    UPHELD = "upheld"
    CHANGED = "changed"


_WS = re.compile(r"\s+")
_COURT = re.compile(
    r"судебн\w+\s+коллеги\w+\s+по\s+(?:уголовным\s+)?делам|президиум\w*\s+верховного\s+суда",
    re.IGNORECASE,
)
# A verdict, including an appellate one, has the heading "ПРИГОВОРИЛ(А)".
_SENTENCED = re.compile(r"П\s?Р\s?И\s?Г\s?О\s?В\s?О\s?Р\s?И\s?Л")
_HEAD_END = re.compile(r"заслушав|(?<![а-яё])у\s?с\s?т\s?а\s?н\s?о\s?в\s?и\s?л", re.IGNORECASE)
HEAD_LIMIT = 8000
# "осужден по …" starts a person's restated sentence; "осужден за кражу" only describes it.
_CONVICTED = re.compile(r"(?<![а-яё])осужд[её]н[аы]?\s+(?=по\b)", re.IGNORECASE)
# What follows a person's sentence: the next person's prior convictions, the appellate stage,
# civil claims, the hearing.
_SENTENCE_END = re.compile(
    r"(?:ранее\s+)?(?:не\s*)?судим\w*|заслушав|апелляционн\w+\s+(?:определени|приговор|постановлени)"
    r"|постановлено\s+взыскать|оправдан",
    re.IGNORECASE,
)
_AGGREGATE = re.compile(
    r"по\s+совокупности|на\s+основании\s+(?:ч\.\s*\d\s+)?ст\.\s*(?:69|70)\b|ст\.\s*(?:69|70)\s+ч\."
    r"|окончательно",
    re.IGNORECASE,
)
_REVERSED_REFERENCE = re.compile(
    r"ст\.\s*(\d{1,3}(?:\.\d{1,2})?)\s+ч\.\s*(\d{1,2})(?:\s+п\.\s*(?:п\.\s*)?(«[^»]{1,25}»))?"
)
_OCR_REFERENCE = re.compile(r"(?<![а-яё])(ч|ст)\.\s*([\dЗО][\dЗО.]*)", re.IGNORECASE)
_NUMERAL_WORD = (
    r"[а-яё]*(?:дцат|десят|сорок|девяност|один|одн|два|две|двух|три|трех|трёх|четыр|пят|шест"
    r"|сем|восем|девят)[а-яё]*"
)
_TERM_AHEAD = r"(?=(?:\d|(?:[а-яё]+\s+){1,3}(?:лет|год|месяц)))"
_FIXES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile("\xad\\s*"), ""),
    (re.compile(r"(?<=[а-яё])-\s+(?=[а-яё])"), ""),
    (re.compile(r'"([а-яё])"'), r"«\1»"),
    (re.compile(r"»\s*,\s*«"), ", "),
    (re.compile(r"\bпункт\w*\s+(?=«)", re.IGNORECASE), "п. "),
    (re.compile(r"\bчаст\w*\s+(?=\d)", re.IGNORECASE), "ч. "),
    (re.compile(r"\bстать\w*\s+(?=\d)", re.IGNORECASE), "ст. "),
    # "назначено [Иванову И.И.] 15 лет" -> "назначено к 15 лет", the form the extractor reads.
    (
        re.compile(
            r"\b(назначен[оаы]?|назначить|назначив|определен[оа]?)\s+"
            r"(?:[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.\s+)?" + _TERM_AHEAD,
            re.IGNORECASE,
        ),
        r"\1 к ",
    ),
    (
        re.compile(r"(лишени\w+\s+свободы)\s+на\s+" + _TERM_AHEAD, re.IGNORECASE),
        r"\1 на срок ",
    ),
    # Page numbers of the PDF that land inside a sentence.
    (re.compile(r"(?<=[а-яё])\s+\d{1,2}\s+(?=(?:лишени|свобод|срок)\w*)", re.IGNORECASE), " "),
    (re.compile(r"(?<=\bк)\s+\d{1,2}\s+(?=\d{1,2}\s+(?:лет|год|месяц))", re.IGNORECASE), " "),
    (
        re.compile(
            r"(?<=\bк)\s+\d{1,2}\s+(?=(?:" + _NUMERAL_WORD + r"\s+){1,2}(?:лет|год|месяц))",
            re.IGNORECASE,
        ),
        " ",
    ),
)
_RESOLUTION = re.compile(r"руководствуясь", re.IGNORECASE)
RESOLUTION_FALLBACK = 1500
_SPACED_WORD = re.compile(r"(?<![а-яё])(?:[а-яё] ){3,}[а-яё](?![а-яё])", re.IGNORECASE)
_ANNULLED = re.compile(r"\bотменить\b", re.IGNORECASE)
_CHANGED = re.compile(r"\bизменить\b", re.IGNORECASE)
_UPHELD = re.compile(r"без\s+изменения|без\s+удовлетворения", re.IGNORECASE)
_EARLIER_CHANGE = re.compile(r"приговор\w*[^.]{0,80}?\bизмен[её]н[аоы]?\b[^.]{0,300}", re.I)
_PUNISHMENT_CHANGE = re.compile(r"смягч|сниз|усил|назнач|наказани", re.IGNORECASE)
QUOTE_LIMIT = 300
# Names in the head of a ruling: "Рябинина С. Е.", "РЯБИНИН С Е", "Лаврентьева Елена
# Николаевна". The description then names the same people by bare surname in any case
# ("Рябинин и Дорохов признаны"), which the generic anonymiser cannot tell from other words.
_NAME_WITH_INITIALS = re.compile(
    r"(?<![А-ЯЁа-яё])([А-ЯЁ][А-ЯЁа-яё]+(?:-[А-ЯЁ][А-ЯЁа-яё]+)?)\s+[А-ЯЁ]\.?\s?[А-ЯЁ](?![а-яё])"
)
_FULL_NAME = re.compile(
    r"(?<![А-ЯЁа-яё])([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)\s+[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?\s+"
    r"[А-ЯЁ][а-яё]+(?:-[а-яё]+)?(?:вич|вн|чн)[а-яё]{0,3}(?![а-яё])"
)
# "осужденного Клипа Иона", "в отношении Стрючкова", "подсудимого Ондара".
_NAME_AFTER_ROLE = re.compile(
    r"(?:осужд[её]нн\w+|в\s+отношении|подсудим\w+|оправданн\w+)\s+"
    r"([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)"
)
# A convicted person is introduced right before "несудимый" / "судимый" / "осужден по": the
# first capitalised word after the preceding sentence ("…, которым Харисов А иг из Вагизович,
# несудимый, осужден"), however the rest of the name is written.
_PERSON_ANCHOR = re.compile(
    r",\s*(?:ранее\s+)?(?:не\s*)?судим|(?<![а-яё])осужд[её]н[аы]?\s+по\b", re.IGNORECASE
)
_NAME_BLOCK_START = re.compile(r"котор(?:ым|ому|ой|ых)|приговору|(?<=[а-яё]{3})\.\s|:\s|;\s")
_CAPITALISED = re.compile(r"(?<![А-ЯЁа-яё])[А-ЯЁ][А-ЯЁа-яё]{2,}(?![А-ЯЁа-яё])")
NAME_WINDOW = 160
_DOCUMENT_WORDS = frozenset(
    "верховный верховного суд суда российской федерации определение постановление президиума"
    " кассационное апелляционное дело".split()
)
MIN_SURNAME_STEM = 3


def _flat(text: str) -> str:
    return _WS.sub(" ", text.replace("\xad ", "").replace("\xad", "")).strip()


def is_ruling(text: str) -> bool:
    flat = _flat(text)
    return bool(_COURT.search(flat[:3000])) and not _SENTENCED.search(text)


def normalize_restated_sentence(text: str) -> str:
    """Rewrite one restated sentence into the form of a first-instance operative part."""
    text = _OCR_REFERENCE.sub(lambda m: f"{m[1]}. {m[2].replace('З', '3').replace('О', '0')}", text)
    for pattern, replacement in _FIXES:
        text = pattern.sub(replacement, text)
    return _REVERSED_REFERENCE.sub(
        lambda m: (f"п. {m[3]} " if m[3] else "") + f"ч. {m[2]} ст. {m[1]}", text
    )


def _head(flat: str) -> str:
    end = _HEAD_END.search(flat)
    return flat[: end.start()] if end and end.start() > 0 else flat[:HEAD_LIMIT]


def _introduced_names(text: str) -> list[str]:
    names = []
    for anchor in _PERSON_ANCHOR.finditer(text):
        window = text[max(0, anchor.start() - NAME_WINDOW) : anchor.start()]
        starts = [m.end() for m in _NAME_BLOCK_START.finditer(window)]
        first = _CAPITALISED.search(window, starts[-1] if starts else 0)
        if first:
            names.append(first.group(0))
    return names


def _surname_stems(text: str) -> set[str]:
    patterns = (_NAME_WITH_INITIALS, _FULL_NAME, _NAME_AFTER_ROLE)
    names = [m[1] for pattern in patterns for m in pattern.finditer(text)]
    stems = set()
    for raw in names + _introduced_names(text):
        name = "-".join(part.capitalize() for part in raw.split("-"))
        if name.lower() in _DOCUMENT_WORDS:
            continue
        # Case endings vary: "Рябинин/Рябинина", "Лаврентьева/Лаврентьевой", "Пух/Пуха".
        stem = name[:-2] if len(name) >= 7 else name[:-1] if len(name) >= 5 else name
        if len(stem) >= MIN_SURNAME_STEM:
            stems.add(stem)
    return stems


def _without_names(text: str, stems: set[str]) -> str:
    for stem in sorted(stems, key=len, reverse=True):
        text = re.sub(rf"(?<![А-ЯЁа-яё]){re.escape(stem)}[а-яё]{{0,4}}(?![а-яё])", "лицо", text)
    return text


def _restated_sentences(head: str) -> list[str]:
    starts = [m.start() for m in _CONVICTED.finditer(head)]
    segments = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(head)
        stop = _SENTENCE_END.search(head, start + 8, end)
        segments.append(normalize_restated_sentence(head[start : stop.start() if stop else end]))
    return segments


def _sentence(segment: str) -> ImposedSentence:
    """Counts come before the first aggregate; the final term is read from the last one."""
    aggregates = list(_AGGREGATE.finditer(segment))
    counts_text = segment[: aggregates[0].start()] if aggregates else segment
    counts = extract_imposed_sentence(counts_text).sentence
    if not aggregates:
        return counts
    final_text = segment[aggregates[-1].start() :]
    aggregate = extract_imposed_sentence(final_text).sentence
    final = aggregate.final or (aggregate.counts[0].main if aggregate.counts else None)
    return ImposedSentence(
        counts=counts.counts,
        final=final,
        aggregation_rule=aggregate.aggregation_rule,
        conditional=counts.conditional or aggregate.conditional,
        probation_months=aggregate.probation_months or counts.probation_months,
        final_quote=final_text[:QUOTE_LIMIT].strip(),
    )


def _quote_around(text: str, match: re.Match) -> str:
    start = text.rfind(". ", 0, match.start()) + 1
    return text[start : start + QUOTE_LIMIT].strip()


def _outcome(flat: str) -> tuple[RulingOutcome | None, bool, str]:
    """(outcome, the punishment was changed, quote) of the resolution; None means skip."""
    found = list(_RESOLUTION.finditer(flat))
    resolution = flat[found[-1].start() :] if found else flat[-RESOLUTION_FALLBACK:]
    resolution = _SPACED_WORD.sub(lambda m: m[0].replace(" ", ""), resolution)
    if _ANNULLED.search(resolution):
        return None, False, ""
    changed = _CHANGED.search(resolution)
    if changed:
        punishment = bool(_PUNISHMENT_CHANGE.search(resolution[changed.start() :]))
        return RulingOutcome.CHANGED, punishment, _quote_around(resolution, changed)
    upheld = _UPHELD.search(resolution)
    if upheld:
        return RulingOutcome.UPHELD, False, _quote_around(resolution, upheld)
    return None, False, ""


class RulingReader:
    def read_all(self, text: str) -> list[VerdictReading]:
        flat = _flat(text)
        outcome, punishment_changed, quote = _outcome(flat)
        if outcome is None:
            return []
        head = _head(flat)
        earlier = _EARLIER_CHANGE.search(head)
        if earlier and _PUNISHMENT_CHANGE.search(earlier.group(0)):
            punishment_changed = True
        description = flat[len(head) :]
        found = list(_RESOLUTION.finditer(description))
        if found:
            description = description[: found[-1].start()]
        fabula = _without_names(extract_fabula(description), _surname_stems(flat))
        readings = []
        for segment in _restated_sentences(head):
            sentence = ImposedSentence() if punishment_changed else _sentence(segment)
            circumstances = extract_circumstances(description, segment, "")
            readings.append(
                VerdictReading(
                    introduction=head[:QUOTE_LIMIT],
                    description=description,
                    operative=segment,
                    sentence=sentence,
                    fabula=fabula,
                    mitigating=circumstances.mitigating,
                    aggravating=circumstances.aggravating,
                    applied_general=circumstances.applied_general,
                    recidivism=circumstances.recidivism,
                    recidivism_quote=circumstances.recidivism_quote,
                    outcome=outcome.value,
                    outcome_quote=quote,
                )
            )
        return readings
