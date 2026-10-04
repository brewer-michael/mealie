"""
Recipe card shorthand ("1 T.", "1/4 t.", "1/3 C.") turned into unit names before Mealie's NLP parser sees a line
(docs/ai/PHASE2.md §5, F6). The parser lowercases units, so "1 T. coconut oil" would otherwise come out with no unit
and the food "T. coconut oil", and "TB." as terabytes.

Case matters (T is a tablespoon, t a teaspoon), and only the token right after the leading quantity is touched, so
"don't" and "t-bone" are left alone. `prepare_line` does everything a line needs before the parser reads it:

- a mixed number written with a dash ("2-1/4") is written with a space (`join_mixed_numbers`);
- size words ("heaping", "scant", "med", "lg", …) are taken out wherever they stand (`extract_size_words`): the
  parser would join them to the unit ("cup scant") or the food ("heaping flour");
- a package size in parentheses between the quantity and the unit ("1 (8 oz.) pkg.") and a can number ("1 #2 can")
  are taken out (the parser reads them as a second amount, or as the food);
- the shorthand unit is written out (`normalize_shorthand`), and "doz.", "env." and "sq." as dozen, envelope and
  square.

What was taken out leads the parsed line's note. Use it only for English cards, or cards whose language is unknown.
The eval and the cross-read import the shorthand table rather than keeping their own.
"""

import re
from dataclasses import dataclass

QTY = r"(?:\d+\s+\d+/\d+|\d+/\d+|\d+(?:[.,]\d+)?|\d*\s*[½⅓⅔¼¾⅛⅜⅝⅞])"
"""A quantity: a mixed number, a fraction, a decimal, or a unicode fraction (with an optional whole number)"""

SIZE_WORDS = ("heaping", "heaped", "level", "rounded", "scant", "generous")
"""
How full a measure is ("1 heaping T. flour"). The parser joins it to the unit ("heaping tbsp", a new unit at commit)
or the food ("heaping T. flour", a new food), wherever it is on the line, so it's parsed without it.
"""

ITEM_SIZE_WORDS = ("med", "md", "lg", "lge", "sm", "sml", "big")
"""
An item's size, abbreviated ("1 med onion", "1 sm. pkg.", "1 big onion"). The parser leaves some in the food, which
the group's foods are then fuzzy-matched against ("med onion" links "red onion"), so it's parsed without them too.
"""

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

ABBREVIATIONS = {
    "doz": "dozen",
    "env": "envelope",
    "sq": "square",
}
"""
Other abbreviated units after a quantity, matched ignoring case ("1 doz. eggs", "1 env. Dream Whip", "2 sq. chocolate").
The parser doesn't know them: it reads "doz eggs" or "env." as the food.
"""

DROPPED_UNITS = frozenset({"dozen"})
"""Units the parser reads and then drops ("1 dozen eggs" is read as 1 egg): the line's unit is set after parsing"""

_LEAD = rf"(?P<lead>\s*[-•*]?\s*{QTY}(?:\s*(?:-|to)\s*{QTY})?\s*)"
_SHORTHAND_UNITS = "|".join(sorted(UNITS, key=len, reverse=True))
_ABBREVIATED_UNITS = "|".join(sorted(ABBREVIATIONS, key=len, reverse=True))

SHORTHAND = re.compile(rf"^{_LEAD}(?P<unit>{_SHORTHAND_UNITS}|(?i:{_ABBREVIATED_UNITS}))\.?(?=\s|$)")
"""A shorthand unit right after the line's leading quantity (the line read without its size words)"""

_SIZE_WORD = (
    rf"(?:{'|'.join(SIZE_WORDS)}|(?:{'|'.join(sorted(ITEM_SIZE_WORDS, key=len, reverse=True))})(?:\.(?![^\W\d_]))?)"
)
_SIZE_WORD_RE = re.compile(rf"(?<![\w'’.-]){_SIZE_WORD}(?![\w'’-])", re.IGNORECASE)
_SIZE_WORDS_IN_PARENS = re.compile(rf"\(\s*{_SIZE_WORD}(?:\s*[,/]?\s*{_SIZE_WORD})*\s*\)", re.IGNORECASE)
_LEFTOVER_COMMA = re.compile(r"\s*,\s*(?=[,;)]|$)|(?<=\()\s*,\s*|^\s*,\s*")
_SPACES = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE = re.compile(r"[ \t]+(?=[,;)])")
_LETTERS = re.compile(r"[^\W\d_]")

_PACKAGE_SIZE = re.compile(rf"^{_LEAD}(?P<size>\(\s*{QTY}[^()]*\))\s*(?=[^\W\d_])")
"""A package's size in parentheses between the quantity and the unit: "1 (8 oz.) pkg. cream cheese" """
_CAN_NUMBER = re.compile(rf"^{_LEAD}(?P<size>#\s?\d+)\s+(?=(?i:cans?)\b)")
"""A can's size by its number: "1 #2 can pineapple", "1 #10 can" """

_MIXED_NUMBER = re.compile(
    r"(?<![\d/.,])(?P<whole>\d+)(?P<dash>\s*[-–—]\s*)(?P<fraction>(?P<numerator>\d+)/(?P<denominator>\d+)|[½⅓⅔¼¾⅛⅜⅝⅞])"
    r"(?![\d/])"
)


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


def _tidy(text: str) -> str:
    """`text` after words were taken out of it: no doubled spaces, empty parentheses or commas left dangling"""
    text = re.sub(r"\(\s*\)", "", text)
    text = _LEFTOVER_COMMA.sub("", text)
    text = _SPACE_BEFORE.sub("", text)
    return _SPACES.sub(" ", text).strip()


def extract_size_words(line: str) -> tuple[str, str | None]:
    """
    The line without its size words, wherever they stand ("scant 1 c. sugar", "1 heaping T. flour", "1 c. scant
    sugar", "1 c. sugar, scant", "1 c. sugar (scant)", "1 med onion"), and the words as the note keeps them ("scant",
    "heaping, med."), or the line as it is and None. A line that would be left with nothing but its amount ("1 med")
    is left as it is.
    """
    words = [match.group(0) for match in _SIZE_WORD_RE.finditer(line)]
    if not words:
        return line, None

    plain = _tidy(_SIZE_WORD_RE.sub(" ", _SIZE_WORDS_IN_PARENS.sub(" ", line)))
    if not _LETTERS.search(plain):
        return line, None
    # the line's own leading space, list marker and all, as it was
    lead = line[: len(line) - len(line.lstrip())]
    return lead + plain, ", ".join(words)


def normalize_shorthand(line: str) -> tuple[str, bool]:
    """
    The line with a shorthand unit after its leading quantity written out ("1 T. coconut oil" becomes
    "1 tbsp coconut oil", "1 doz. eggs" "1 dozen eggs"), and whether the line changed. Size words go first:
    `extract_size_words`.
    """
    match = SHORTHAND.match(line)
    if not match:
        return line, False

    normalized = line[: match.start("unit")] + unit_name(match.group("unit")) + line[match.end() :]
    return normalized, normalized != line


def unit_name(token: str) -> str:
    """The unit a shorthand token stands for (`UNITS`, case-sensitive, then `ABBREVIATIONS`)"""
    return UNITS.get(token) or ABBREVIATIONS[token.lower()]


@dataclass(frozen=True)
class PreparedLine:
    """A card's ingredient line as the parser reads it, and what was taken out of it for the note"""

    text: str
    """What the parser reads"""
    notes: tuple[str, ...]
    """What was taken out, in the order the note shows it: size words, then a package size or can number"""
    shorthand: tuple[str, str] | None
    """The shorthand unit as written (with its dot) and the unit it was written out as, when there was one"""
    unit: str | None
    """
    The unit written right after the quantity, when the parser drops it from its reading: "dozen" (written so, or as
    "doz."). The parsed line's unit is set to it.
    """


def prepare_line(line: str) -> PreparedLine:
    """Everything an English card's line needs before the parser reads it (see the module docstring)"""
    text, size = extract_size_words(join_mixed_numbers(line))
    notes = [size] if size else []

    for pattern in (_PACKAGE_SIZE, _CAN_NUMBER):
        if match := pattern.match(text):
            notes.append(match.group("size").strip())
            text = match.group("lead") + text[match.end() :]
            break

    shorthand: tuple[str, str] | None = None
    if match := SHORTHAND.match(text):
        written = text[match.start("unit") : match.end()].strip()
        shorthand = (written, unit_name(match.group("unit")))
    text = normalize_shorthand(text)[0]

    unit = None
    if (after := re.match(rf"^{_LEAD}(?P<word>[^\W\d_]+)\b", text)) and after.group("word").lower() in DROPPED_UNITS:
        unit = after.group("word").lower()
    return PreparedLine(text=text, notes=tuple(notes), shorthand=shorthand, unit=unit)
