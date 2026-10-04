"""
Reading card text the way the flags and the cross-read compare it (docs/ai/PHASE2.md §4.5, §4.6): the two markers,
numbers (digits, fractions, mixed numbers and ½-style glyphs, as exact rationals, ranges kept), the case-sensitive
shorthand units after a number, and temperatures. Pure functions, no I/O.
"""

import re
import unicodedata
from dataclasses import dataclass
from fractions import Fraction

from ..shorthand import UNITS

ILLEGIBLE = "[illegible]"
BLANK = "[blank]"

MARKER_RE = re.compile(r"\[\s*(illegible|blank)\s*\]", re.IGNORECASE)
"""Either marker, as a reader may write it (`[Blank]`, `[ illegible ]`)"""

_GLYPHS = "½⅓⅔¼¾⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞"
_NUMBER = rf"(?:\d+\s*[{_GLYPHS}]|[{_GLYPHS}]|\d+\s+\d+/\d+|\d+/\d+|\d+(?:[.,]\d+)?)"
_NUMBER_RE = re.compile(rf"(?<![\d/.,])(?P<first>{_NUMBER})(?:\s*(?:-|–|—|to)\s*(?P<second>{_NUMBER}))?(?![\d/])")
_UNIT_AFTER_NUMBER_RE = re.compile(rf"\s*(?P<unit>{'|'.join(sorted(UNITS, key=len, reverse=True))})\.?(?=\s|$|[,;)])")
_TEMPERATURE_RE = re.compile(
    r"(?<![\d/.,])(?P<value>\d{2,3})\s*"
    r"(?:[°º˚]\s*(?:(?P<unit>[FfCc])\b)?|degrees?\b(?:\s*(?P<unit2>[FfCc])\b)?|(?P<unit3>F)\b)"
)
"""`350°`, `350 °F`, `180°C`, `350 degrees F`, `350F`. A bare `C` after a number is cups ("12 C. flour")."""
_LETTERS_RE = re.compile(r"[^\W\d_]+")


def canonical_markers(text: str) -> str:
    """`text` with every marker written exactly `[illegible]` or `[blank]`, which is what commit converts"""
    return MARKER_RE.sub(lambda match: f"[{match.group(1).lower()}]", text)


def markers_in(text: str | None) -> list[str]:
    """The markers in `text`, in order: `illegible` or `blank` for each"""
    return [match.group(1).lower() for match in MARKER_RE.finditer(text or "")]


def letters_only(text: str) -> str:
    """The words of `text` (letters only, lowercased, markers removed), for aligning lines by what they say"""
    return " ".join(_LETTERS_RE.findall(MARKER_RE.sub(" ", text).lower()))


def _number_value(token: str) -> tuple[Fraction, list[Fraction]] | None:
    """A number token's value, and its parts for a mixed number ("1 1/2" is 3/2, and has the parts 1 and 1/2)"""
    token = token.strip()
    try:
        if token[-1] in _GLYPHS:
            glyph = Fraction(unicodedata.numeric(token[-1])).limit_denominator(16)
            whole = token[:-1].strip()
            if not whole:
                return glyph, []
            return int(whole) + glyph, [Fraction(int(whole)), glyph]

        if "/" in token:
            whole, _, fraction = token.rpartition(" ")
            numerator, _, denominator = fraction.partition("/")
            if int(denominator) == 0:
                return None
            value = Fraction(int(numerator), int(denominator))
            if whole.strip():
                return int(whole) + value, [Fraction(int(whole)), value]
            return value, []

        return Fraction(token.replace(",", ".")), []
    except ValueError, ZeroDivisionError:
        return None


@dataclass(frozen=True)
class NumberMatch:
    value: Fraction
    """A single number, or a range's first number"""
    end: Fraction | None
    """A range's second number"""
    text: str
    """As written"""
    parts: tuple[Fraction, ...]
    """Every number it consists of: the value, a mixed number's parts and a range's ends"""
    span: tuple[int, int]


def find_numbers(text: str | None) -> list[NumberMatch]:
    """Every number and range in `text`, in order"""
    found: list[NumberMatch] = []
    for match in _NUMBER_RE.finditer(text or ""):
        first = _number_value(match.group("first"))
        if first is None:
            continue
        second = _number_value(match.group("second")) if match.group("second") else None
        parts = [first[0], *first[1]]
        if second is not None:
            parts += [second[0], *second[1]]
        found.append(
            NumberMatch(
                value=first[0],
                end=second[0] if second else None,
                text=match.group(0).strip(),
                parts=tuple(parts),
                span=match.span(),
            )
        )
    return found


def number_set(text: str | None) -> set[Fraction]:
    """Every number in `text` and every part of one: what "a number on the card" is compared with"""
    return {part for number in find_numbers(text) for part in number.parts}


def format_number(value: Fraction) -> str:
    """A rational as a card writes it: `2`, `1/4`, `1 1/2`"""
    if value.denominator == 1:
        return str(value.numerator)
    whole, rest = divmod(value.numerator, value.denominator)
    fraction = f"{rest}/{value.denominator}"
    return f"{whole} {fraction}" if whole else fraction


@dataclass(frozen=True)
class Temperature:
    value: int
    unit: str | None
    """`F`, `C`, or None when the text only says degrees"""
    text: str


def find_temperatures(text: str | None) -> list[Temperature]:
    """Temperatures in `text`: `350°`, `350 °F`, `180°C`, `350 degrees`, `350F`"""
    found: list[Temperature] = []
    for match in _TEMPERATURE_RE.finditer(text or ""):
        unit = match.group("unit") or match.group("unit2") or match.group("unit3")
        found.append(Temperature(int(match.group("value")), unit.upper() if unit else None, match.group(0).strip()))
    return found


SalientToken = tuple[str, ...]
"""A token the cross-read compares: `("number", "1/4")`, `("range", "2", "3")`, `("unit", "T")` or `("marker", "blank")`
"""


def salient_tokens(text: str | None) -> list[SalientToken]:
    """
    The tokens of a line that a second reading has to agree on (§4.5): numbers (as rationals; ranges kept whole;
    temperatures are their numbers), the case-sensitive shorthand units right after a number, and the markers.
    """
    text = text or ""
    tokens: list[SalientToken] = []
    for number in find_numbers(text):
        if number.end is not None:
            tokens.append(("range", str(number.value), str(number.end)))
        else:
            tokens.append(("number", str(number.value)))
        if unit := _UNIT_AFTER_NUMBER_RE.match(text, number.span[1]):
            tokens.append(("unit", unit.group("unit")))
    tokens.extend(("marker", marker) for marker in markers_in(text))
    return tokens


def describe_token(token: SalientToken) -> str:
    """A salient token as the review page shows it"""
    match token:
        case ("number", value):
            return format_number(Fraction(value))
        case ("range", first, second):
            return f"{format_number(Fraction(first))}-{format_number(Fraction(second))}"
        case ("marker", marker):
            return f"[{marker}]"
        case (_, value, *_):
            return value
    return ""
