"""
Recipe card shorthand ("1 T.", "1/4 t.", "1/3 C.") turned into unit names before Mealie's NLP parser sees a line
(docs/ai/PHASE2.md §5, F6). The parser lowercases units, so "1 T. coconut oil" would otherwise come out with no unit
and the food "T. coconut oil", and "TB." as terabytes.

Case matters (T is a tablespoon, t a teaspoon), and only the token right after the leading quantity is touched, so
"don't" and "t-bone" are left alone. A size word between them ("1 heaping T. flour") is taken out first
(`split_size`), and the caller puts it in the parsed line's note. Use it only for English cards, or cards whose
language is unknown. The eval and the cross-read import this table rather than keeping their own.
"""

import re

QTY = r"(?:\d+\s+\d+/\d+|\d+/\d+|\d+(?:[.,]\d+)?|\d*\s*[½⅓⅔¼¾⅛⅜⅝⅞])"
"""A quantity: a mixed number, a fraction, a decimal, or a unicode fraction (with an optional whole number)"""

SIZE_WORDS = ("heaping", "heaped", "level", "rounded", "scant", "generous")
"""
How full a measure is ("1 heaping T. flour"). The parser joins it to the unit ("heaping tbsp", a new unit at commit)
or the food ("heaping T. flour", a new food), wherever it is on the line, so it's parsed without it.
"""

_LEAD = rf"(?P<lead>\s*[-•*]?\s*{QTY}(?:\s*(?:-|to)\s*{QTY})?\s*)"
_SIZE = rf"(?P<size>(?i:{'|'.join(SIZE_WORDS)})\s+)"

SHORTHAND = re.compile(rf"^{_LEAD}{_SIZE}?" r"(?P<unit>TBSP|TBS|TB|Tbsp|Tbs|Tb|T|tsp|ts|t|C|c|pkg|Pkg)\.?(?=\s|$)")

_SIZED = re.compile(rf"^{_LEAD}{_SIZE}(?=\S)")

UNITS = {
    "T": "tbsp",
    "Tb": "tbsp",
    "Tbs": "tbsp",
    "Tbsp": "tbsp",
    "TB": "tbsp",
    "TBS": "tbsp",
    "TBSP": "tbsp",
    "t": "tsp",
    "ts": "tsp",
    "tsp": "tsp",
    "C": "cup",
    "c": "cup",
    "pkg": "package",
    "Pkg": "package",
}
"""Case-sensitive card shorthand to the unit name the parser knows"""


def split_size(line: str) -> tuple[str, str | None]:
    """
    The line without a size word right after its leading quantity ("1 heaping T. flour" becomes "1 T. flour"), and
    the size word, or the line as it is and None
    """
    sized = _SIZED.match(line)
    if not sized:
        return line, None
    return sized.group("lead") + line[sized.end() :], sized.group("size").strip()


def normalize_shorthand(line: str) -> tuple[str, bool]:
    """
    The line with a shorthand unit after its leading quantity written out ("1 T. coconut oil" becomes
    "1 tbsp coconut oil"), and whether the line changed. A size word before the unit stays: `split_size` first.
    """
    match = SHORTHAND.match(line)
    if not match:
        return line, False

    normalized = line[: match.start("unit")] + UNITS[match.group("unit")] + line[match.end() :]
    return normalized, normalized != line
