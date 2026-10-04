"""
Recipe card shorthand ("1 T.", "1/4 t.", "1/3 C.") turned into unit names before Mealie's NLP parser sees a line
(docs/ai/PHASE2.md §5, F6). The parser lowercases units, so "1 T. coconut oil" would otherwise come out with no unit
and the food "T. coconut oil", and "TB." as terabytes.

Case matters (T is a tablespoon, t a teaspoon), and only the token right after the leading quantity (or after a second
amount joined to it: "plus 2 T.") is touched, so "don't" and "t-bone" are left alone. `prepare_line` does everything a
line needs before the parser reads it:

- a mixed number written with a dash ("2-1/4") is written with a space (`join_mixed_numbers`);
- size words ("heaping", "scant", "med", "lg", …) are taken out wherever they stand (`extract_size_words`): the
  parser would join them to the unit ("cup scant") or the food ("heaping flour");
- a size written in full right after the quantity ("1 large can", "1 small (3 oz.) pkg."): the parser reads "1
  large" as a second amount and drops it;
- a package size between the quantity and the unit, in parentheses, after a dash or neither ("1 (8 oz.) pkg.",
  "1 - 8 oz. pkg.", "1 8-oz. pkg."), and a can number ("1 #2 can", "1 #2 1/2 can", "1 No. 2 can") are taken out (the
  parser reads them as a second amount, a range, or the food);
- the shorthand unit is written out (`normalize_shorthand`), and "doz.", "env." and "sq." as dozen, envelope and
  square (with their longer and plural spellings: "tbls.", "pkgs."), and so is a case-sensitive shorthand unit after
  a second, joined amount ("1 c. plus 2 T. flour"), which the parser would read as the food "T. flour".

What was taken out leads the parsed line's note. Use it only for English cards, or cards whose language is unknown.
The eval and the cross-read import the shorthand table rather than keeping their own.
"""

import math
import re
import unicodedata
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

NAME_SIZE_WORDS = frozenset({"big"})
"""
Item size words that also start names: capitalized before a capitalized word ("1 c. Big Red soda"), they're the
name's, and stay. The abbreviations start none ("1 Lg Onion" is a size).
"""

FULL_SIZE_WORDS = ("extra-large", "extra large", "small", "medium", "large", "jumbo", "tall")
"""
An item's size written in full. Right after the quantity it's taken out too: before a container ("1 large can
pineapple") the parser reads "1 large" as a second amount and drops it, and keeps the reading's first amount only.
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
    "envs": "envelope",
    "sq": "square",
    "pkgs": "package",
    "pkt": "package",
    "pkts": "package",
    "tbs": "tbsp",
    "tbl": "tbsp",
    "tbls": "tbsp",
    "tblsp": "tbsp",
    "teasp": "tsp",
}
"""
Other abbreviated units after a quantity, matched ignoring case ("1 doz. eggs", "1 env. Dream Whip", "2 sq. chocolate",
and the longer or plural spellings of a spoon or a package: "2 tbls. sugar", "2 pkgs. yeast", "2 pkts. yeast"). The
parser doesn't know them: it reads "doz eggs" or "tbls. sugar" as the food.
"""

UNIT_SPELLINGS: tuple[tuple[str, ...], ...] = (
    ("teaspoon", "tsp", "ts", "teasp"),
    ("tablespoon", "tbsp", "tbs", "tbl", "tbls", "tblsp", "tb"),
    ("cup", "c"),
    ("fluid ounce", "fl oz"),
    ("ounce", "oz"),
    ("pound", "lb"),
    ("pint", "pt"),
    ("quart", "qt"),
    ("gallon", "gal"),
    ("gram", "g", "gr"),
    ("kilogram", "kg"),
    ("milligram", "mg"),
    ("milliliter", "ml", "millilitre"),
    ("liter", "l", "litre"),
    ("package", "pkg", "pack", "packet", "pk", "pkt"),
    ("dozen", "doz"),
    ("envelope", "env"),
    ("square", "sq"),
)
"""
The common units by every spelling a card or a group may give them (plurals aside: "lbs", "pkgs"): the name, then
its standard abbreviation, then the rest. A group's units may have only a name ("teaspoon": commit creates them so,
and only seeded groups have abbreviations), and a card writes "tsp." or "pkg.": that's an exact link (`flags`'
`linked_fuzzy`), and a written-out shorthand is linked to the group's unit by any of its spellings, never to another
unit by a near miss ("square" to "quart"). The case-sensitive "T", "t" and "C" aren't here: `prepare_line` writes them
out.
"""

DROPPED_UNITS = frozenset({"dozen"})
"""Units the parser reads and then drops ("1 dozen eggs" is read as 1 egg): the line's unit is set after parsing"""

_LEAD = rf"(?P<lead>\s*+[-•*]?\s*+{QTY}(?:\s*(?:-|to)\s*+{QTY})?\s*)"
"""
The line's leading quantity, or a range. The spaces before a quantity are taken whole (`*+`): `QTY` may start with
spaces too ("1 ½"), and a long run of them would be split every way (quadratic time).
"""
_SHORTHAND_UNITS = "|".join(sorted(UNITS, key=len, reverse=True))
_ABBREVIATED_UNITS = "|".join(sorted(ABBREVIATIONS, key=len, reverse=True))

SHORTHAND = re.compile(rf"^{_LEAD}(?P<unit>{_SHORTHAND_UNITS}|(?i:{_ABBREVIATED_UNITS}))\.?(?=\s|$)")
"""A shorthand unit right after the line's leading quantity (the line read without its size words)"""

_SIZE_WORD = (
    rf"(?:{'|'.join(SIZE_WORDS)}|(?:{'|'.join(sorted(ITEM_SIZE_WORDS, key=len, reverse=True))})(?:\.(?![^\W\d_]))?)"
)
_SIZE_WORD_RE = re.compile(rf"(?<![\w'’.-]){_SIZE_WORD}(?![\w'’-])", re.IGNORECASE)
_SIZE_WORDS_IN_PARENS = re.compile(rf"\(\s*+{_SIZE_WORD}(?:\s*+[,/]?+\s*+{_SIZE_WORD})*+\s*+\)", re.IGNORECASE)
# each run of spaces is read from its start only, once: a line of thousands of spaces took seconds
_LEFTOVER_COMMA = re.compile(r"(?:(?<!\s)\s++)?,\s*(?=[,;)]|$)|(?<=\()\s*,\s*|^\s*,\s*")
_SPACES = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE = re.compile(r"(?<![ \t])[ \t]++(?=[,;)])")
_LETTERS = re.compile(r"[^\W\d_]")

_PACKAGE_SIZE = re.compile(rf"^{_LEAD}(?P<size>\(\s*+(?:#\s*+)?{QTY}[^()]*\))\s*(?=[^\W\d_])")
"""A package's size in parentheses between the quantity and the unit: "1 (8 oz.) pkg. cream cheese", "1 (#2) can" """
_CONTAINER = (
    r"(?i:cans?|pkgs?|packages?|packets?|pk|jars?|box(?:es)?|cartons?|bottles?|bags?|envs?|envelopes?|containers?"
    r"|tubs?|tins?|sticks?)\b"
)
_PLAIN_PACKAGE_SIZE = re.compile(
    rf"^{_LEAD}(?<=\s)(?P<size>\d+(?:[.,]\d+)?(?:\s+\d+/\d+|\s*[½⅓⅔¼¾⅛⅜⅝⅞])?\s*+-?\s*+"
    r"(?i:fl\.?\s*oz|oz|ounces?|lbs?|pounds?|g|grams?|kg|ml)\.?)\s+"
    rf"(?={_CONTAINER})"
)
"""
A package's size without parentheses between the quantity and a container: "1 8-oz. pkg. cream cheese", "2 15 oz. cans".
A whole number after the quantity and a space, so "2 1/2 oz. pkg." stays one amount (and a run of digits is never
split two ways, which would take quadratic time).
"""
_DASHED_PACKAGE_SIZE = re.compile(
    rf"^(?P<lead>\s*+[-•*]?\s*+\d+)\s*+[-–]\s*+(?P<size>\d+(?:[.,]\d+)?(?:\s+\d+/\d+|\s*[½⅓⅔¼¾⅛⅜⅝⅞])?\s*+-?\s*+"
    r"(?i:fl\.?\s*oz|oz|ounces?|lbs?|pounds?|g|grams?|kg|ml)\.?)\s+"
    rf"(?={_CONTAINER})"
)
"""
A package's size after a dash between the count and a container: "1 - 8 oz. pkg. cream cheese", "2-15 oz. cans" (two
15-ounce cans, not 2 to 15 ounces: a range never runs into a size before a container, so "2-3 c. flour" stays a range)
"""
_CAN_NUMBER = re.compile(
    rf"^{_LEAD}(?P<size>(?:#\s?|(?i:no)\.?\s*)\d+(?:\s+\d+/\d+|\s*[½⅓⅔¼¾⅛⅜⅝⅞])?)\s+(?=(?i:cans?)\b)"
)
"""
A can's size by its number, a fraction too: "1 #2 can pineapple", "1 #10 can", "1 #2 1/2 can peaches", "1 No. 2 can"
(the parser would read "#2 1/2" as a food, in its own fraction code)
"""
_LEAD_SIZE = re.compile(
    rf"^{_LEAD}(?P<size>(?i:{'|'.join(sorted(FULL_SIZE_WORDS, key=len, reverse=True))}))\s+(?=[^\W\d_]|\(\s*#?\d)"
)
"""A size written in full right after the quantity: "1 large can", "1 small (3 oz.) pkg." (`FULL_SIZE_WORDS`)"""
_JOINED_SHORTHAND = re.compile(
    rf"(?P<amount>(?:[+&]|\b(?i:and|or|plus)\b)\s*+(?:\(\s*+)?{QTY}\s*)(?P<unit>{_SHORTHAND_UNITS})\.?(?=\s+[^\W\d_])"
)
"""
Case-sensitive shorthand after a second amount joined to the line, before more words: "1 c. plus 2 T. flour",
"1 c. + 2 T. sugar". The parser would read "T. flour" as the food.
"""
_NAME_AFTER = re.compile(r"[ \t]+(?P<letter>[^\W\d_])")
_WORD_AFTER_LEAD = re.compile(rf"^{_LEAD}(?P<word>[^\W\d_½⅓⅔¼¾⅛⅜⅝⅞]+)\b")
"""The word after the leading quantity (not the "½" of "1½": a fraction glyph is a word character to `re`)"""
_LEAD_QUANTITY = re.compile(rf"\s*[-•*]?\s*(?P<quantity>{QTY})(?:\s*(?:-|to)\s*{QTY})?\s*")
"""A lead (`_LEAD`), matched whole, and its quantity: a range's start"""
_QUANTITY_PARTS = re.compile(
    r"(?:(?P<whole>\d+(?:[.,]\d+)?)\s*)?(?:(?P<numerator>\d+)/(?P<denominator>\d+)|(?P<glyph>[½⅓⅔¼¾⅛⅜⅝⅞]))?"
)

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


def _starts_a_name(match: re.Match[str]) -> bool:
    """Whether a size word starts a name (`NAME_SIZE_WORDS`): "Big" before "Red" in "1 c. Big Red soda" """
    word = match.group(0)
    if word.lower() not in NAME_SIZE_WORDS or not word[0].isupper():
        return False
    after = _NAME_AFTER.match(match.string, match.end())
    return after is not None and after.group("letter").isupper()


def extract_size_words(line: str) -> tuple[str, str | None]:
    """
    The line without its size words, wherever they stand ("scant 1 c. sugar", "1 heaping T. flour", "1 c. scant
    sugar", "1 c. sugar, scant", "1 c. sugar (scant)", "1 med onion"), and the words as the note keeps them ("scant",
    "heaping, med."), or the line as it is and None. A line that would be left with nothing but its amount ("1 med")
    is left as it is, and so is a size word that starts a name ("Big Red", `NAME_SIZE_WORDS`).
    """
    words = [match.group(0) for match in _SIZE_WORD_RE.finditer(line) if not _starts_a_name(match)]
    if not words:
        return line, None

    def take_out(match: re.Match[str]) -> str:
        return match.group(0) if _starts_a_name(match) else " "

    plain = _tidy(_SIZE_WORD_RE.sub(take_out, _SIZE_WORDS_IN_PARENS.sub(" ", line)))
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


def _spelling_key(name: str) -> str:
    """A unit's name as spellings are looked up: lowercase, without dots or an optional plural ("cup(s)")"""
    return " ".join(name.lower().replace("(s)", "").replace(".", " ").split())


_SPELLINGS: dict[str, tuple[str, ...]] = {
    form: spellings
    for spellings in UNIT_SPELLINGS
    for spelling in spellings
    for form in (spelling, f"{spelling}s")  # "lbs", "pkgs", "teaspoons"
}


def unit_spellings(name: str) -> tuple[str, ...]:
    """
    Every spelling of the unit `name` is a spelling of (`UNIT_SPELLINGS`: "tsp." and "teaspoons" give the teaspoon's),
    or just `name` (lowercase, without dots) for a unit not in it
    """
    key = _spelling_key(name)
    return _SPELLINGS.get(key) or ((key,) if key else ())


def standard_abbreviation(name: str) -> str:
    """The standard abbreviation of a unit by its name or plural ("tsp" for "teaspoon", "lb" for "pounds"), or "" """
    key = _spelling_key(name)
    spellings = _SPELLINGS.get(key)
    if not spellings or key not in (spellings[0], f"{spellings[0]}s"):
        return ""
    return spellings[1]


def quantity_value(text: str) -> float | None:
    """
    A quantity (`QTY`) as a number: "1/2", "1 1/2", "1½", "2.5" or "2,5"; None when `text` isn't one, or is too
    large for a float (300 digits read off a card are no quantity)
    """
    parts = _QUANTITY_PARTS.fullmatch(text.strip())
    if parts is None or not any(parts.groupdict().values()):
        return None
    try:
        value = float(parts.group("whole").replace(",", ".")) if parts.group("whole") else 0.0
        if parts.group("numerator"):
            if not int(parts.group("denominator")):
                return None
            value += int(parts.group("numerator")) / int(parts.group("denominator"))
        elif parts.group("glyph"):
            value += unicodedata.numeric(parts.group("glyph"))
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


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
    quantity: float | None = None
    """
    With `unit`, the line's leading quantity (a range's start): the parser folds the dozen into a fraction's amount,
    and reads "1/2 dozen eggs" as 1
    """


def prepare_line(line: str) -> PreparedLine:
    """Everything an English card's line needs before the parser reads it (see the module docstring)"""
    text, size = extract_size_words(join_mixed_numbers(line))
    notes = [size] if size else []

    if match := _LEAD_SIZE.match(text):
        notes.append(match.group("size"))
        text = match.group("lead") + text[match.end() :]
    for pattern in (_PACKAGE_SIZE, _DASHED_PACKAGE_SIZE, _PLAIN_PACKAGE_SIZE, _CAN_NUMBER):
        if match := pattern.match(text):
            notes.append(match.group("size").strip())
            # the dashed size's lead has no space after it ("2-15 oz. cans": "2 cans", not "2cans")
            text = match.group("lead").rstrip() + " " + text[match.end() :]
            break

    shorthand: tuple[str, str] | None = None
    if match := SHORTHAND.match(text):
        written = text[match.start("unit") : match.end()].strip()
        shorthand = (written, unit_name(match.group("unit")))
    text = normalize_shorthand(text)[0]
    text = _JOINED_SHORTHAND.sub(lambda joined: joined.group("amount") + UNITS[joined.group("unit")], text)

    unit, quantity = None, None
    if (after := _WORD_AFTER_LEAD.match(text)) and after.group("word").lower() in DROPPED_UNITS:
        unit = after.group("word").lower()
        if lead := _LEAD_QUANTITY.fullmatch(after.group("lead")):
            quantity = quantity_value(lead.group("quantity"))
    return PreparedLine(text=text, notes=tuple(notes), shorthand=shorthand, unit=unit, quantity=quantity)
