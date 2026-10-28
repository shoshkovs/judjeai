"""Russian cardinal numbers in words ('восьмидесяти тысяч', 'полутора') -> numbers.

Shared by the statute parser and the verdict punishment extractor.
"""

_NUMBER_WORDS: dict[str, float] = {
    **dict.fromkeys(["один", "одна", "одного", "одной", "одну", "одному"], 1),
    **dict.fromkeys(["два", "две", "двух", "двум"], 2),
    **dict.fromkeys(["три", "трех", "трёх", "трем", "трём"], 3),
    **dict.fromkeys(["четыре", "четырех", "четырёх", "четырем", "четырём"], 4),
    **dict.fromkeys(["пять", "пяти"], 5),
    **dict.fromkeys(["шесть", "шести"], 6),
    **dict.fromkeys(["семь", "семи"], 7),
    **dict.fromkeys(["восемь", "восьми"], 8),
    **dict.fromkeys(["девять", "девяти"], 9),
    **dict.fromkeys(["десять", "десяти"], 10),
    **dict.fromkeys(["одиннадцать", "одиннадцати"], 11),
    **dict.fromkeys(["двенадцать", "двенадцати"], 12),
    **dict.fromkeys(["тринадцать", "тринадцати"], 13),
    **dict.fromkeys(["четырнадцать", "четырнадцати"], 14),
    **dict.fromkeys(["пятнадцать", "пятнадцати"], 15),
    **dict.fromkeys(["шестнадцать", "шестнадцати"], 16),
    **dict.fromkeys(["семнадцать", "семнадцати"], 17),
    **dict.fromkeys(["восемнадцать", "восемнадцати"], 18),
    **dict.fromkeys(["девятнадцать", "девятнадцати"], 19),
    **dict.fromkeys(["двадцать", "двадцати"], 20),
    **dict.fromkeys(["тридцать", "тридцати"], 30),
    **dict.fromkeys(["сорок", "сорока"], 40),
    **dict.fromkeys(["пятьдесят", "пятидесяти"], 50),
    **dict.fromkeys(["шестьдесят", "шестидесяти"], 60),
    **dict.fromkeys(["семьдесят", "семидесяти"], 70),
    **dict.fromkeys(["восемьдесят", "восьмидесяти"], 80),
    **dict.fromkeys(["девяносто", "девяноста"], 90),
    **dict.fromkeys(["сто", "ста"], 100),
    **dict.fromkeys(["двести", "двухсот"], 200),
    **dict.fromkeys(["триста", "трехсот", "трёхсот"], 300),
    **dict.fromkeys(["четыреста", "четырехсот", "четырёхсот"], 400),
    **dict.fromkeys(["пятьсот", "пятисот"], 500),
    **dict.fromkeys(["шестьсот", "шестисот"], 600),
    **dict.fromkeys(["семьсот", "семисот"], 700),
    **dict.fromkeys(["восемьсот", "восьмисот"], 800),
    **dict.fromkeys(["девятьсот", "девятисот"], 900),
    "полутора": 1.5,
}
_MULTIPLIERS = {
    **dict.fromkeys(["тысяча", "тысячи", "тысяч", "тысячу"], 1_000),
    **dict.fromkeys(["миллион", "миллиона", "миллионов"], 1_000_000),
}


def parse_russian_number(words: str) -> float | None:
    """'восьмидесяти тысяч' -> 80000; returns None for anything outside the vocabulary."""
    tokens = words.lower().split()
    if not tokens:
        return None
    total, current = 0.0, 0.0
    for token in tokens:
        if token.isdigit():
            current += int(token)
        elif token in _NUMBER_WORDS:
            current += _NUMBER_WORDS[token]
        elif token in _MULTIPLIERS:
            total += (current or 1) * _MULTIPLIERS[token]
            current = 0
        else:
            return None
    value = total + current
    return int(value) if float(value).is_integer() else value
