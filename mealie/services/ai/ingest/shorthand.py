"""
Recipe card shorthand ("1 T.", "1/4 t.", "1/3 C.") turned into unit names before Mealie's NLP parser sees a line
(docs/ai/PHASE2.md §5, F6). The parser lowercases units, so "1 T. coconut oil" would otherwise come out with no unit
and the food "T. coconut oil", and "TB." as terabytes.

Case matters (T is a tablespoon, t a teaspoon), and only the token right after the leading quantity is touched, so
"don't" and "t-bone" are left alone. Use it only for English cards, or cards whose language is unknown. The eval and
the cross-read import this table rather than keeping their own.
"""

import re

QTY = r"(?:\d+\s+\d+/\d+|\d+/\d+|\d+(?:[.,]\d+)?|\d*\s*[½⅓⅔¼¾⅛⅜⅝⅞])"
"""A quantity: a mixed number, a fraction, a decimal, or a unicode fraction (with an optional whole number)"""

SHORTHAND = re.compile(
    rf"^(?P<lead>\s*[-•*]?\s*{QTY}(?:\s*(?:-|to)\s*{QTY})?\s*)"
    r"(?P<unit>TBSP|TBS|TB|Tbsp|Tbs|Tb|T|tsp|ts|t|C|c|pkg|Pkg)\.?(?=\s|$)"
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


def normalize_shorthand(line: str) -> tuple[str, bool]:
    """
    The line with a shorthand unit after its leading quantity written out ("1 T. coconut oil" becomes
    "1 tbsp coconut oil"), and whether the line changed.
    """
    match = SHORTHAND.match(line)
    if not match:
        return line, False

    normalized = line[: match.start("unit")] + UNITS[match.group("unit")] + line[match.end() :]
    return normalized, normalized != line
