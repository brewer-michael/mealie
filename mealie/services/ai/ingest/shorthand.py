"""
Recipe card shorthand ("1 T.", "1/4 t.", "1/3 C.") turned into unit names before Mealie's NLP parser sees a line
(docs/ai/PHASE2.md §5, F6). The parser lowercases units, so "1 T. coconut oil" would otherwise come out with no unit
and the food "T. coconut oil", and "TB." as terabytes.

Case matters (T is a tablespoon, t a teaspoon), and only the token right after the leading quantity is touched, so
"don't" and "t-bone" are left alone. A size word between them ("1 heaping T. flour", "1 med onion") is taken out
first (`split_size`), and the caller puts it in the parsed line's note; a mixed number written with a dash ("2-1/4")
is written with a space first (`join_mixed_numbers`). Use it only for English cards, or cards whose language is
unknown. The eval and the cross-read import this table rather than keeping their own.
"""

import re

QTY = r"(?:\d+\s+\d+/\d+|\d+/\d+|\d+(?:[.,]\d+)?|\d*\s*[½⅓⅔¼¾⅛⅜⅝⅞])"
"""A quantity: a mixed number, a fraction, a decimal, or a unicode fraction (with an optional whole number)"""

SIZE_WORDS = ("heaping", "heaped", "level", "rounded", "scant", "generous")
"""
How full a measure is ("1 heaping T. flour"). The parser joins it to the unit ("heaping tbsp", a new unit at commit)
or the food ("heaping T. flour", a new food), wherever it is on the line, so it's parsed without it.
"""

ITEM_SIZE_WORDS = ("med", "md", "lg", "lge", "sm", "sml")
"""
An item's size, abbreviated ("1 med onion", "1 sm. pkg."). The parser leaves some in the food, which the group's
foods are then fuzzy-matched against ("med onion" links "red onion"), so it's parsed without them too.
"""

_LEAD = rf"(?P<lead>\s*[-•*]?\s*{QTY}(?:\s*(?:-|to)\s*{QTY})?\s*)"
_SIZE = rf"(?P<size>(?i:{'|'.join(SIZE_WORDS)}|(?:{'|'.join(ITEM_SIZE_WORDS)})\.?)\s+)"

SHORTHAND = re.compile(rf"^{_LEAD}{_SIZE}?" r"(?P<unit>TBSP|TBS|TB|Tbsp|Tbs|Tb|T|tsp|ts|t|C|c|pkg|Pkg)\.?(?=\s|$)")

_SIZED = re.compile(rf"^{_LEAD}{_SIZE}(?=\S)")

_MIXED_NUMBER = re.compile(
    r"(?<![\d/.,])(?P<whole>\d+)(?P<dash>\s*[-–—]\s*)(?P<fraction>(?P<numerator>\d+)/(?P<denominator>\d+)|[½⅓⅔¼¾⅛⅜⅝⅞])"
    r"(?![\d/])"
)

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


def join_mixed_numbers(text: str, *, keep_length: bool = False) -> str:
    """
    `text` with each mixed number written with a dash ("2-1/4 c. flour", "1 - 1/2 c. milk", as printed recipes write
    them) written with a space: the parser would keep only the fraction. A range never goes down to a proper fraction,
    so "2-3/4" is a mixed number, and "3-5/4" is left alone. `keep_length` puts a space for each character of the dash,
    so positions in `text` still hold.
    """

    def join(match: re.Match[str]) -> str:
        numerator, denominator = match.group("numerator"), match.group("denominator")
        if numerator is not None and int(numerator) >= int(denominator):
            return match.group(0)
        space = " " * len(match.group("dash")) if keep_length else " "
        return f"{match.group('whole')}{space}{match.group('fraction')}"

    return _MIXED_NUMBER.sub(join, text)


def split_size(line: str) -> tuple[str, str | None]:
    """
    The line without a size word right after its leading quantity ("1 heaping T. flour" becomes "1 T. flour", "1 med
    onion" "1 onion"), and the size word, or the line as it is and None
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
