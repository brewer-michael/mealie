"""
The flags that say what to check on a card (docs/ai/PHASE2.md §4.6). `compute_flags` is pure, and the server runs it
after extraction and on every save, so the flags always describe the current draft.

**Ids and fields.** A flag is keyed to a field plus the ingredient's `reference_id` or the step's `id` (never an
index, F4), with the stable id `"<kind>:<field>:<ref>"` (`ref` empty for single fields and the card). Fields are the
draft's JSON names: `name`, `description`, `recipeYield`, `recipeServings`, `prepTime`, `performTime`, `totalTime`,
`attribution`, `ingredients`, `steps`, `notes` and `card` for the card-level flags. Notes have no id, so their `ref`
is the note's position, and their flag ids add a digest of the note (`"<kind>:notes:<position>#<digest>"`): a
resolution stored by id then never moves to another note when one above it is deleted or the notes are reordered.

**Three kinds of flags.**
- *Content flags* follow the draft as it is now, edits included: markers, `missing_name`, `implausible_*`,
  `empty_section`, `new_food`, `new_unit` and the card-level `read_by_ocr`, `cross_read_failed`, `not_parsed`.
- *Parse flags* (`check_parse`, `unit_unclear`, `shorthand_read`) describe the parser's reading of a line, so they
  drop off once the line is edited: each ingredient keeps an `extracted_hash` of its parsed fields.
- *Reading flags* compare the draft with what was read: `unsure`, `not_on_card`, `marker_dropped`, and the cross-read's
  `read_disagreement` and `blank`. Typing a number into a blank must not raise them, so on a save (`previous` given)
  a reading flag is kept only where it was raised before and still holds; only an extraction raises new ones. A
  note's is found again by its digest wherever the note moved, and comes back unresolved.

**Alternatives.** `unsure` alternatives replace `params.text` (the uncertain words) in the line; `implausible_amount`'s
replace `params.value`; `read_disagreement`'s single alternative is the second reading's whole line (`params.text`).

**Positions.** A flag about one part of a field's text (a marker, a number, the uncertain words, a token) carries
`params.start` and `params.end`: where that part is, as character offsets into the text the flag was computed on. That
is the field's own text for single fields, steps and notes, and for an ingredient its line as `ingredient_line` reads
it (the card's line while the ingredient is as extracted). They point at the occurrence the rule matched, so a page
highlights or replaces that one, never another "2" in the same step.
"""

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

from mealie.schema.recipe_ingest import (
    CardDraft,
    CardDraftIngredient,
    CardFlag,
    CardFlagKind,
    CardFlagSeverity,
    CardFlagSource,
    ExtractionMeta,
    ExtractionUnsure,
    FlagResolution,
    IngestReadPath,
    PageOCR,
)

from ..flag_rules import KEEPABLE_KINDS, REVIEW_CONFIDENCE
from ..shorthand import ABBREVIATIONS, ITEM_SIZE_WORDS, QTY, SIZE_WORDS, UNITS, prepare_line
from .cardtext import (
    BLANK,
    MARKER_RE,
    NumberMatch,
    card_numbers,
    describe_token,
    find_numbers,
    find_temperatures,
    format_number,
    letters_only,
    markers_in,
    stripped_span,
    without_list_marker,
)
from .crossread import align_ingredient, align_step, compare

FIELD_NAME = "name"
FIELD_DESCRIPTION = "description"
FIELD_YIELD = "recipeYield"
FIELD_SERVINGS = "recipeServings"
FIELD_ATTRIBUTION = "attribution"
FIELD_INGREDIENTS = "ingredients"
FIELD_STEPS = "steps"
FIELD_NOTES = "notes"
FIELD_CARD = "card"

DRAFT_TEXT_FIELDS = {
    FIELD_NAME: "name",
    FIELD_DESCRIPTION: "description",
    FIELD_YIELD: "recipe_yield",
    "prepTime": "prep_time",
    "performTime": "perform_time",
    "totalTime": "total_time",
    FIELD_ATTRIBUTION: "attribution",
}
"""The draft's single text fields: the name flags and proposals use (the draft's JSON name) to its attribute"""

TIME_FIELDS = ("prepTime", "performTime", "totalTime")

READING_KINDS = frozenset(
    {
        CardFlagKind.unsure,
        CardFlagKind.not_on_card,
        CardFlagKind.marker_dropped,
        CardFlagKind.read_disagreement,
    }
)
"""Flags that compare the draft with what was read; a cross-read `blank` is one too (see the module docstring)"""

PARSE_KINDS = frozenset({CardFlagKind.check_parse, CardFlagKind.unit_unclear, CardFlagKind.shorthand_read})
"""Flags about the parser's reading of a line, which drop off once the line is edited"""

UNSURE_MIN_SCORE = 85
"""An `unsure` entry flags the line it matches best by `partial_ratio`, if it scores this much"""
UNSURE_EXACT_MAX_LENGTH = 3
"""Shorter `unsure` texts ("T", "1/4") must appear as a whole token: `partial_ratio` would match them anywhere"""

MAX_PLAIN_AMOUNT = 20
"""More than this many teaspoons, tablespoons or cups is flagged"""
SPOON_AND_CUP_UNITS = frozenset(
    {"teaspoon", "teaspoons", "tsp", "tablespoon", "tablespoons", "tbsp", "tbs", "cup", "cups", "c"}
)
FAHRENHEIT_RANGE = (200, 550)
CELSIUS_RANGE = (90, 290)
"""
Oven temperatures (frying and candy temperatures fall within them too), checked in every step: a card's terse
"350° - 30 min." names no oven, and its misread "35°" must still be caught. Only where the nearest word before the
temperature in its clause says it's for rising, cooling, warm liquids or a thermometer ("let rise in a warm place
(80°)", "cool to 70°", "warm water (110°F)", "until a thermometer reads 160°") do just the upper ends apply.
"""
_TEMPERATURE_CONTEXT = re.compile(
    r"\b(?:(?P<oven>bak(?:e|es|ed|ing)|preheat\w*|roast\w*|broil\w*|(?:deep[- ])?fr(?:y|ies|ied|ying)|water[- ]bath)"
    r"|ris(?:e|es|en|ing)|proof\w*|cool\w*|chill\w*|refrigerat\w*|room\s+temp\w*|lukewarm|warm(?:\s+oven)?|water|milk"
    r"|yeast|scald\w*|thermometer|internal|reach(?:es|ed)?|reads?)\b",
    re.IGNORECASE,
)
"""
Words that say what a temperature after them is for: the oven's (or the fryer's), or anything else. "Oven" itself
isn't one: "let rise in oven (85°)" is still about rising, and "preheat oven to" about the oven.
"""
_CLAUSE_END = re.compile(r"(?P<stop>[.!?;:])(?=\s)|\bthen\b|\n", re.IGNORECASE)
"""
Where what a temperature is for stops carrying over, whatever the case after it: "Bake at 350° for 1 hr; cool to
70°", "cool, then bake at 35°". A stop after a measure ("in 1/4 c. 110° water") isn't one.
"""
_MEASURE_ABBREVIATIONS = frozenset(
    {*(unit.lower() for unit in UNITS), "oz", "lb", "lbs", "pt", "qt", "gal", "doz", "sq", "env", "pkgs"}
)
_WORD_BEFORE = re.compile(r"[^\W\d_]+$")

_FRACTION_TYPO = re.compile(r"(?<![\d/.,])(?P<whole>[1-9])(?P<numerator>[1-9])/(?P<denominator>[2348])(?![\d/])")
"""`11/2` for "1 1/2": a whole number run into a proper fraction"""
_LEADING_QUANTITY = re.compile(rf"^\s*[-•*]?\s*{QTY}(?:\s*(?:-|to)\s*{QTY})?\s*(?P<token>[^\W\d_]+)(?P<dot>\.)?")
_NOT_UNIT_WORDS = frozenset({"or", "and", "to", "of", "x", *ITEM_SIZE_WORDS, *SIZE_WORDS})
"""Short words after a quantity that aren't a lost unit: joins ("2 or 3 eggs") and sizes ("1 lg onion")"""
_FOOD_WORDS = frozenset("bay bbq bok egg fig ham hot ice jam oat old pea pie red rye sea soy sun tea yam".split())
"""
Short words that begin common foods ("1 egg yolk", "1 bay leaf", "1 red pepper", "1 hot dog", "1 pie crust"), so
aren't a lost unit when written without a dot; the first words of Mealie's own seed foods, and a few more
"""
UNIT_VOCABULARY = frozenset(
    {
        *(unit.lower() for unit in UNITS),
        *ABBREVIATIONS,
        *"sq doz env pt qt oz lb pkg pk cn gal tbs tsp ml dl kg g c t".split(),
    }
)
"""
Short tokens a card writes for a unit, matched ignoring case. A 1-3 letter word after the quantity is a lost unit
(`unit_unclear`) only when it carries an abbreviation's dot, is one of these or of the group's own units, or is one
letter away from one of two letters or more ("tbl", "lbs"); any other word is part of the food ("2 new potatoes").
"""
_SIZE_WORD_TOKENS = frozenset({*SIZE_WORDS, *ITEM_SIZE_WORDS})
_UNIT_WORDS = frozenset(
    {
        *UNIT_VOCABULARY,
        *ABBREVIATIONS.values(),
        *"""ounce ounces pound pounds lbs ozs cup cups can cans package packages pkgs pint pints quart quarts gallon
        gallons gram grams liter liters litre litres stick sticks envelopes squares tablespoon tablespoons teaspoon
        teaspoons tbsp""".split(),
    }
)
"""Words that make an amount's unit ("2 T.", "10 3/4 oz."), for what a note keeps of an amount the fields lost"""
_MULTIPLIER_WORDS = frozenset({"dozen", "doz"})
"""Words after a quantity that multiply it, which the parser drops: "1 dozen eggs" is read as 1 egg"""
_JOINER = r"(?:[+&,;]|\b(?:and|or|plus)\b)"
"""What joins a second amount to a line: "butter + 1 T. oil", "flour (or 1 1/2 c. bread flour)", "sugar, 1 c. flour" """
_AFTER_JOINER = re.compile(rf"{_JOINER}\s*\(?\s*$", re.IGNORECASE)
_JOINED_AMOUNT = re.compile(rf"{_JOINER}\s*\(?\s*(?P<amount>{QTY})", re.IGNORECASE)
_UNIT_AFTER_AMOUNT = re.compile(r"\s*(?P<word>[^\W\d_]+)(?P<dot>\.)?")
_REST_OF_AMOUNT = re.compile(rf"(?:(?!{_JOINER})[^\d()])*?(?=\s*(?:{_JOINER}|[()]|\d|$))", re.IGNORECASE)
"""The words after an amount and its unit, up to the next joiner, parenthesis or number"""
_WORD = re.compile(r"[^\W\d_]+\.?")

_SEVERITY = {
    CardFlagKind.illegible: CardFlagSeverity.error,
    CardFlagKind.blank: CardFlagSeverity.error,
    CardFlagKind.missing_name: CardFlagSeverity.error,
    CardFlagKind.unsure: CardFlagSeverity.warning,
    CardFlagKind.not_on_card: CardFlagSeverity.warning,
    CardFlagKind.marker_dropped: CardFlagSeverity.warning,
    CardFlagKind.read_disagreement: CardFlagSeverity.warning,
    CardFlagKind.check_parse: CardFlagSeverity.warning,
    CardFlagKind.unit_unclear: CardFlagSeverity.warning,
    CardFlagKind.implausible_amount: CardFlagSeverity.warning,
    CardFlagKind.implausible_temperature: CardFlagSeverity.warning,
    CardFlagKind.empty_section: CardFlagSeverity.warning,
    CardFlagKind.read_by_ocr: CardFlagSeverity.warning,
    CardFlagKind.cross_read_failed: CardFlagSeverity.info,
    CardFlagKind.shorthand_read: CardFlagSeverity.info,
    CardFlagKind.not_parsed: CardFlagSeverity.info,
    CardFlagKind.new_food: CardFlagSeverity.info,
    CardFlagKind.new_unit: CardFlagSeverity.info,
}


def flag_id(kind: CardFlagKind, field: str, ref: str | None = None) -> str:
    """A flag's stable id: `"<kind>:<field>:<ref>"`"""
    return f"{kind.value}:{field}:{ref or ''}"


def is_english(language: str | None) -> bool:
    """Whether a card's language (as the reader named it) is English or unknown: only those lines are parsed (§5)"""
    if not language or not language.strip():
        return True
    language = language.strip().lower()
    return language.startswith("en") or "english" in language


# ==========================================
# Ingredient lines


def ingredient_hash(ingredient: CardDraftIngredient) -> str:
    """A hash of an ingredient's parsed fields, stored as `extracted_hash` when it's extracted"""
    fields = [
        round(ingredient.quantity, 6) if ingredient.quantity is not None else None,
        [str(ingredient.unit.id) if ingredient.unit.id else None, ingredient.unit.name] if ingredient.unit else None,
        [str(ingredient.food.id) if ingredient.food.id else None, ingredient.food.name] if ingredient.food else None,
        ingredient.note,
    ]
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode()).hexdigest()[:16]


def is_unedited(ingredient: CardDraftIngredient) -> bool:
    """Whether an ingredient's parsed fields are still as extracted"""
    return bool(ingredient.extracted_hash) and ingredient.extracted_hash == ingredient_hash(ingredient)


def ingredient_line(ingredient: CardDraftIngredient) -> str:
    """
    The line as it reads now: the card's line while the ingredient is as extracted, else its quantity, unit, food
    and note.
    """
    if ingredient.original_text and is_unedited(ingredient):
        return ingredient.original_text

    parts: list[str] = []
    if ingredient.quantity:
        parts.append(format_number(Fraction(ingredient.quantity).limit_denominator(16)))
    if ingredient.unit and ingredient.unit.name:
        parts.append(ingredient.unit.name)
    if ingredient.food and ingredient.food.name:
        parts.append(ingredient.food.name)
    if ingredient.note:
        parts.append(ingredient.note)
    return " ".join(parts) or ingredient.original_text


def _ingredient_texts(ingredient: CardDraftIngredient) -> list[str]:
    """The texts of an ingredient a reviewer edits, where a marker can be"""
    texts = [ingredient.title or "", ingredient.note]
    if ingredient.unit:
        texts.append(ingredient.unit.name)
    if ingredient.food:
        texts.append(ingredient.food.name)
    return [text for text in texts if text]


# ==========================================
# What a draft holds, in reading order


@dataclass
class _Target:
    field: str
    ref: str | None
    text: str
    """What it says now, for matching and the reading flags"""
    marker_texts: list[str]
    """Where its markers can be"""
    ingredient: CardDraftIngredient | None = None
    is_step: bool = False
    id_ref: str | None = None
    """What its flag ids are keyed to, when that isn't `ref` (a note's position and digest)"""


def _targets(draft: CardDraft) -> list[_Target]:
    targets: list[_Target] = []

    def single(field: str) -> None:
        text = getattr(draft, DRAFT_TEXT_FIELDS[field]) or ""
        targets.append(_Target(field, None, text, [text]))

    single(FIELD_NAME)
    single(FIELD_DESCRIPTION)
    single(FIELD_YIELD)
    if draft.recipe_servings:
        servings = format_number(Fraction(draft.recipe_servings).limit_denominator(16))
        targets.append(_Target(FIELD_SERVINGS, None, servings, []))
    for field in TIME_FIELDS:
        single(field)

    for ingredient in draft.ingredients:
        line = ingredient_line(ingredient)
        targets.append(
            _Target(
                FIELD_INGREDIENTS,
                str(ingredient.reference_id),
                line,
                _ingredient_texts(ingredient),
                ingredient=ingredient,
            )
        )
    for step in draft.steps:
        texts = [step.title or "", step.text]
        targets.append(_Target(FIELD_STEPS, str(step.id), step.text, [text for text in texts if text], is_step=True))
    for index, note in enumerate(draft.notes):
        texts = [note.title, note.text]
        digest = hashlib.sha256(json.dumps(texts, ensure_ascii=False).encode()).hexdigest()[:8]
        targets.append(
            _Target(FIELD_NOTES, str(index), note.text, [text for text in texts if text], id_ref=f"{index}#{digest}")
        )

    single(FIELD_ATTRIBUTION)
    return targets


# ==========================================
# Collecting flags


class _Flags:
    def __init__(self) -> None:
        self.flags: dict[str, CardFlag] = {}

    def add(
        self,
        kind: CardFlagKind,
        field: str,
        ref: str | None = None,
        *,
        source: CardFlagSource,
        params: dict | None = None,
        alternatives: Iterable[str] = (),
        id_ref: str | None = None,
    ) -> None:
        id = flag_id(kind, field, id_ref or ref)
        if existing := self.flags.get(id):
            for alternative in alternatives:
                if alternative not in existing.alternatives:
                    existing.alternatives.append(alternative)
            return

        self.flags[id] = CardFlag(
            id=id,
            kind=kind,
            severity=_SEVERITY[kind],
            source=source,
            field=field,
            ref=ref,
            params=params or {},
            alternatives=list(dict.fromkeys(alternatives)),
        )


def _card_flags(flags: _Flags, draft: CardDraft, extraction: ExtractionMeta | None) -> None:
    if extraction is None:
        return

    if extraction.read_path == IngestReadPath.ocr:
        params = {"confidence": round(extraction.ocr_confidence)} if extraction.ocr_confidence is not None else {}
        flags.add(CardFlagKind.read_by_ocr, FIELD_CARD, source=CardFlagSource.ocr, params=params)
    if extraction.cross_read_failed:
        flags.add(CardFlagKind.cross_read_failed, FIELD_CARD, source=CardFlagSource.cross_read)
    if not is_english(extraction.language) and any(_kept_as_text(line) for line in draft.ingredients):
        # the AI parser parses such a card's lines; the flag says when some stayed as text (it failed, or wasn't set)
        flags.add(CardFlagKind.not_parsed, FIELD_CARD, source=CardFlagSource.parser, params={})


def _kept_as_text(ingredient: CardDraftIngredient) -> bool:
    """Whether a line was kept as written, with no parse: not a marker line (flagged on its own) or an empty one"""
    if ingredient.parse_confidence is not None or ingredient.quantity is not None:
        return False
    if (ingredient.unit and ingredient.unit.name.strip()) or (ingredient.food and ingredient.food.name.strip()):
        return False
    text = ingredient.note or ingredient.original_text
    return bool(text.strip()) and not markers_in(text)


def _marker_flags(flags: _Flags, target: _Target) -> None:
    markers = {marker for text in target.marker_texts for marker in markers_in(text)}
    for marker, kind in (("illegible", CardFlagKind.illegible), ("blank", CardFlagKind.blank)):
        if marker not in markers:
            continue
        found = next((match for match in MARKER_RE.finditer(target.text) if match.group(1).lower() == marker), None)
        flags.add(
            kind,
            target.field,
            target.ref,
            source=CardFlagSource.marker,
            params=_position(found.span()) if found else None,
            id_ref=target.id_ref,
        )


def _unsure_targets(
    unsure: Sequence[ExtractionUnsure], targets: Sequence[_Target]
) -> dict[int, tuple[ExtractionUnsure, tuple[int, int] | None]]:
    """
    The target each `unsure` entry matches best (by position in `targets`), and where in its text; the first entry
    wins a target
    """
    matched: dict[int, tuple[ExtractionUnsure, tuple[int, int] | None]] = {}
    for entry in unsure:
        text = entry.text.strip()
        if not text:
            continue

        best: tuple[float, int, tuple[int, int] | None] | None = None
        for index, target in enumerate(targets):
            if not target.text:
                continue
            span: tuple[int, int] | None = None
            if len(text) <= UNSURE_EXACT_MAX_LENGTH:
                found = re.search(rf"(?<![\w/]){re.escape(text)}(?![\w/])", target.text)
                score, span = (100.0, found.span()) if found else (0.0, None)
            elif len(target.text) >= len(text):
                alignment = fuzz.partial_ratio_alignment(text.lower(), target.text.lower())
                score = alignment.score if alignment else 0.0
                exact = target.text.lower().find(text.lower())
                if exact >= 0:
                    span = (exact, exact + len(text))
                elif alignment:
                    span = (alignment.dest_start, alignment.dest_end)
            else:
                # `partial_ratio` would find a short target inside the entry ("4" in "1/4 t. salt")
                score, span = fuzz.ratio(text.lower(), target.text.lower()), (0, len(target.text))
            if score >= UNSURE_MIN_SCORE and (best is None or score > best[0]):
                best = (score, index, span)

        if best is not None:
            matched.setdefault(best[1], (entry, best[2]))
    return matched


def _not_on_card(text: str, on_card: set[Fraction], *, skip: int = 0) -> NumberMatch | None:
    """The first number in `text`, after its first `skip` characters (a step's list number), that isn't on the card"""
    for number in find_numbers(text):
        if number.span[0] < skip:
            continue
        values = [number.value] + ([number.end] if number.end is not None else [])
        if any(value not in on_card for value in values):
            return number
    return None


def _position(span: tuple[int, int]) -> dict[str, int]:
    """A flag's `params.start` and `params.end` (see the module docstring)"""
    return {"start": span[0], "end": span[1]}


def _fraction_typo(ingredient: CardDraftIngredient, line: str) -> tuple[str, str, tuple[int, int]] | None:
    """A `11/2`-style fraction in the line the parser read as such, what it likely meant ("1 1/2"), and where it is"""
    match = _FRACTION_TYPO.search(line)
    if not match:
        return None
    whole, numerator, denominator = (int(match.group(name)) for name in ("whole", "numerator", "denominator"))
    if numerator >= denominator:
        return None
    if ingredient.quantity is not None:
        read_as = Fraction(whole * 10 + numerator, denominator)
        if abs(ingredient.quantity - float(read_as)) > 0.01:
            return None
    return match.group(0), f"{whole} {numerator}/{denominator}", match.span()


# ==========================================
# Amounts a parsed line lost


@dataclass(frozen=True)
class LostAmount:
    """An amount on an ingredient's line that its parsed fields (quantity, unit, food and note) don't hold"""

    value: str
    """As written ("2-3", "10 3/4", "1 dozen"): `check_parse`'s `params.value`"""
    span: tuple[int, int]
    """Where `value` is on the line"""
    kept: str | None
    """
    What the line's note keeps of it, so the recipe doesn't lose it: "to 3", "(10 3/4 oz.)", "+ 2 T."; None for a second
    ingredient run into the food, whose amount the fields do hold
    """


def _amounts(text: str, *, in_a_name: bool = False) -> list[Fraction]:
    """
    The amounts in `text`: each number, and a range's end. `in_a_name`: only the numbers that are part of a unit or
    food name ("2% milk", "V8 juice", "7-Up", '9" pie shell'), not a second amount run into it ("butter + 1 T. oil").
    """
    return [
        value
        for number in find_numbers(text)
        if not (in_a_name and _AFTER_JOINER.search(text, 0, number.span[0]))
        for value in (number.value, number.end)
        if value is not None
    ]


def _merged_amount(line: str, food: str | None) -> re.Match[str] | None:
    """
    A second amount whose ingredient the parser ran into the food: "2 c. flour (or 1 1/2 c. bread flour)" read as the
    food "flour bread flour", "1 c. sugar, 1 c. flour" as "sugar flour". The food takes words from before the joined
    amount and from only after it; "2 T. + 1 t. sugar" or "1 pkg. yeast (or 2 1/4 tsp.)" don't.
    """
    food_words = set(letters_only(food).split()) if food else set()
    for joined in _JOINED_AMOUNT.finditer(line):
        before = set(letters_only(line[: joined.start()]).split())
        after = set(letters_only(line[joined.end() :]).split())
        if food_words & before and (food_words - before) & after:
            return joined
    return None


def _unit_end(line: str, end: int) -> int:
    """Where an amount that ends at `end` ends with its unit ("2 T.", "10 3/4 oz."); `end` when no unit follows it"""
    match = _UNIT_AFTER_AMOUNT.match(line, end)
    if match is None:
        return end
    word = match.group("word").lower()
    if word in _UNIT_WORDS or (match.group("dot") and len(word) <= 4):
        return match.end()
    return end


def _kept_text(line: str, span: tuple[int, int], field_words: set[str]) -> str:
    """
    What a note keeps of a lost amount at `span`: the parentheses around it ("(10 3/4 oz.)"), else the amount with the
    word that joins it to the line and its unit ("+ 2 T.", "or 3"), else the amount and its unit; with the words after
    them up to the next joiner when the fields don't hold them (the "brown sugar" of "1 c. sugar, 1 c. brown sugar")
    """
    start, end = span
    opening = line.rfind("(", 0, start)
    if opening >= 0 and ")" not in line[opening:start]:
        closing = line.find(")", end)
        if closing >= 0 and "(" not in line[end:closing]:
            return line[opening : closing + 1].strip()

    end = _unit_end(line, end)
    rest = _REST_OF_AMOUNT.match(line, end)
    if rest and set(letters_only(rest.group(0)).split()) - field_words:
        end = rest.end()
    if joiner := _AFTER_JOINER.search(line, 0, start):
        start = joiner.start()
    return line[start:end].strip().lstrip(",;").strip()


def lost_amounts(line: str, quantity: float | None, unit: str | None, food: str | None, note: str) -> list[LostAmount]:
    """
    The amounts on an ingredient's line (as read from the card) that its parsed fields lost, in order: the parser keeps
    one quantity, so a range's end ("2-3 T. milk" is read as 2), a second number ("2 or 3 eggs", the "10 3/4 oz." of
    "1 can (10 3/4 oz.) soup", the "2 T." of "1 c. sugar + 2 T."), a second ingredient run into the food, or a "dozen"
    would be gone from the recipe commit writes from the fields. The amounts are counted: the quantity and each number
    the note or a name keeps account for one each.
    """
    in_fields = Counter(_amounts(note))
    for name in (unit, food):
        if name:
            in_fields.update(_amounts(name, in_a_name=True))
    field_words = set(letters_only(" ".join(text for text in (note, unit, food) if text)).split())

    lost: list[LostAmount] = []
    left = quantity
    for number in find_numbers(line):
        missing: list[bool] = []
        for value in (number.value, number.end):
            if value is None:
                continue
            if left is not None and math.isclose(float(value), left, abs_tol=1e-3):
                left = None  # the parsed quantity accounts for this one
                missing.append(False)
            elif in_fields[value]:
                in_fields[value] -= 1
                missing.append(False)
            else:
                missing.append(True)
        if not any(missing):
            continue
        if missing == [False, True] and number.end_text:
            kept = f"to {number.end_text}"  # the range's end; its start is the quantity
        else:
            kept = _kept_text(line, number.span, field_words)
        lost.append(LostAmount(value=number.text, span=number.span, kept=kept))
    if lost:
        return lost

    if merged := _merged_amount(line, food):
        span = stripped_span(line, merged.span("amount"))
        return [LostAmount(value=line[span[0] : span[1]], span=span, kept=None)]

    lead = _LEADING_QUANTITY.match(line)
    if lead and lead.group("token").lower() in _MULTIPLIER_WORDS:
        token = lead.group("token")
        if not any(token.lower() in (text or "").lower() for text in (note, unit, food)):
            span = stripped_span(line, lead.span())
            return [LostAmount(value=line[span[0] : span[1]], span=span, kept=token)]
    return []


def keep_lost_amounts(
    line: str, quantity: float | None, unit: str | None, food: str | None, note: str
) -> tuple[str, list[LostAmount]]:
    """
    The note with what it keeps of each amount the fields lost appended ("to 3", "(10 3/4 oz.)"), so nothing read
    from the card is lost from the recipe whatever the reviewer taps, and those amounts. Parsing does this once; the
    flags find the amounts again from the note it made (`check_parse` still asks the reviewer to look).
    """
    lost = lost_amounts(line, quantity, unit, food, note)
    kept = list(dict.fromkeys(amount.kept for amount in lost if amount.kept))
    return ", ".join(part for part in (note, *kept) if part), lost


def _lost_amount(ingredient: CardDraftIngredient, *, english: bool) -> LostAmount | None:
    """
    The first amount the parsed line lost: one the fields still lack (a draft parsed before notes kept them), or one
    its note keeps at its end exactly as `keep_lost_amounts` appended it at parse time. The note parsing started from
    began with what was taken out of an English card's line before parsing (`prepare_line`): a package size "(8 oz.)"
    there was never lost.
    """
    line, quantity, note = ingredient.original_text, ingredient.quantity, ingredient.note
    unit = ingredient.unit.name if ingredient.unit else None
    food = ingredient.food.name if ingredient.food else None
    if lost := lost_amounts(line, quantity, unit, food, note):
        return lost[0]

    taken_out = ", ".join(prepare_line(line).notes) if english else ""
    for base in ["", *(note[: separator.start()] for separator in re.finditer(", ", note))]:
        if not base.startswith(taken_out):
            continue
        kept, lost = keep_lost_amounts(line, quantity, unit, food, base)
        if lost and kept == note:
            return lost[0]
    return None


def _size_word_in_names(ingredient: CardDraftIngredient) -> str | None:
    """A size word the parser put in the unit or food's name ("cup scant", "heaping flour"), as written there"""
    for ref in (ingredient.unit, ingredient.food):
        for word in _WORD.findall(ref.name if ref else ""):
            if word.lower().rstrip(".") in _SIZE_WORD_TOKENS:
                return word
    return None


def _looks_like_unit(token: str, dot: bool, units: Collection[str]) -> bool:
    """
    Whether a short word after a quantity is a unit the parser lost: it has an abbreviation's dot ("Tb.", "doz."), is
    in `UNIT_VOCABULARY` or the group's `units`, or is one letter from one of two letters or more ("tbl", "lbs"). A
    word in capitals without a dot ("TV") is part of the food unless it is a unit itself ("TB").
    """
    if dot:
        return True
    word = token.lower()
    if word in UNIT_VOCABULARY or word in units:
        return True
    if token.isupper():
        return False
    return any(
        2 <= len(entry) <= len(word) + 1 and Levenshtein.distance(word, entry, score_cutoff=1) <= 1
        for entry in (*UNIT_VOCABULARY, *units)
    )


def _ingredient_flags(flags: _Flags, target: _Target, *, units: Collection[str], english: bool) -> None:
    ingredient = target.ingredient
    assert ingredient is not None
    field, ref = target.field, target.ref
    parsed = ingredient.parse_confidence is not None
    unedited = is_unedited(ingredient)

    # the parser's reading, while the line is as it read it
    if parsed and unedited:
        params: dict = {}
        if ingredient.parse_confidence is not None and ingredient.parse_confidence < REVIEW_CONFIDENCE:
            params["confidence"] = round(ingredient.parse_confidence * 100)
        if lost := _lost_amount(ingredient, english=english):
            params.update(value=lost.value, **_position(lost.span))
        elif size := _size_word_in_names(ingredient):
            # a size word the parser joined to the unit or food ("cup scant"): commit would create that unit or food
            params["value"] = size
            if found := re.search(rf"(?<![\w.]){re.escape(size)}(?!\w)", ingredient.original_text, re.IGNORECASE):
                params.update(_position(found.span()))
        if params:
            flags.add(CardFlagKind.check_parse, field, ref, source=CardFlagSource.parser, params=params)

        if ingredient.quantity and not ingredient.unit and (lead := _LEADING_QUANTITY.match(ingredient.original_text)):
            token, dot = lead.group("token"), lead.group("dot") or ""
            word = token.lower()
            food = (ingredient.food.name if ingredient.food else "").strip().lower()
            # a word that begins a food ("1 egg yolk"): a common one, or the first word of the group's food it links to
            # ("1 sq chocolate" never names a group food, and "doz." or "env." has the dot of an abbreviation)
            begins_food = not dot and (
                word in _FOOD_WORDS
                or (ingredient.food is not None and ingredient.food.id is not None and food.split()[:1] == [word])
            )
            if (
                len(token) <= 3
                and word not in _NOT_UNIT_WORDS
                and food != word
                and not begins_food
                and _looks_like_unit(token, bool(dot), units)
            ):
                flags.add(
                    CardFlagKind.unit_unclear,
                    field,
                    ref,
                    source=CardFlagSource.parser,
                    params={"token": token + dot, "start": lead.start("token"), "end": lead.end()},
                )

    # what the line says now
    if ingredient.quantity and ingredient.unit and ingredient.quantity > MAX_PLAIN_AMOUNT:
        if ingredient.unit.name.strip().lower().rstrip(".") in SPOON_AND_CUP_UNITS:
            value = f"{format_number(Fraction(ingredient.quantity).limit_denominator(16))} {ingredient.unit.name}"
            params = {"value": value}
            amount = next((n for n in find_numbers(target.text) if math.isclose(n.value, ingredient.quantity)), None)
            if amount is not None:
                params.update(_position(amount.span))
            flags.add(CardFlagKind.implausible_amount, field, ref, source=CardFlagSource.validator, params=params)
    if typo := _fraction_typo(ingredient, target.text):
        written, suggestion, span = typo
        flags.add(
            CardFlagKind.implausible_amount,
            field,
            ref,
            source=CardFlagSource.validator,
            params={"value": written, "suggestion": suggestion, **_position(span)},
            alternatives=[suggestion],
        )

    shorthand = prepare_line(ingredient.original_text).shorthand if parsed and unedited and english else None
    if shorthand and shorthand[0].rstrip(".").lower() != shorthand[1]:
        # "T." became "tbsp"; "tsp" or "Tbsp" was already the unit's name
        flags.add(
            CardFlagKind.shorthand_read,
            field,
            ref,
            source=CardFlagSource.parser,
            params={"from": shorthand[0], "to": shorthand[1]},
        )
    if ingredient.unit and ingredient.unit.name.strip() and ingredient.unit.id is None:
        flags.add(
            CardFlagKind.new_unit, field, ref, source=CardFlagSource.parser, params={"name": ingredient.unit.name}
        )
    if ingredient.food and ingredient.food.name.strip() and ingredient.food.id is None:
        flags.add(
            CardFlagKind.new_food, field, ref, source=CardFlagSource.parser, params={"name": ingredient.food.name}
        )


def _about_the_oven(text: str, span: tuple[int, int]) -> bool:
    """
    Whether a temperature (at `span`) may be the oven's: unless the nearest word before it in its clause says it's for
    something else ("let rise in a warm place (80°)", "bake at 350° for 1 hr; cool to 70°")
    """
    clause_start = 0
    for end in _CLAUSE_END.finditer(text, 0, span[0]):
        word = _WORD_BEFORE.search(text, 0, end.start()) if end.group("stop") == "." else None
        if not (word and word.group(0).lower() in _MEASURE_ABBREVIATIONS):
            clause_start = end.end()
    context = list(_TEMPERATURE_CONTEXT.finditer(text, clause_start, span[0]))
    return not context or context[-1].group("oven") is not None


def _temperature_flags(flags: _Flags, target: _Target) -> None:
    for temperature in find_temperatures(target.text):
        match temperature.unit:
            case "F":
                low, high = FAHRENHEIT_RANGE
            case "C":
                low, high = CELSIUS_RANGE
            case _:
                low, high = CELSIUS_RANGE[0], FAHRENHEIT_RANGE[1]
        if not _about_the_oven(target.text, temperature.span):
            low = 0  # rising, cooling, warm liquids or a thermometer: only too hot is implausible
        if not low <= temperature.value <= high:
            start = temperature.span[0]
            flags.add(
                CardFlagKind.implausible_temperature,
                target.field,
                target.ref,
                source=CardFlagSource.validator,
                params={"value": temperature.text, **_position((start, start + len(temperature.text)))},
            )
            return


def _cross_read_flags(flags: _Flags, target: _Target, lines: Sequence[str], after: int) -> int | None:
    """
    The cross-read's flags for a target; for an ingredient, returns the transcript line it aligned with, which the
    next ingredient's alignment prefers to be after (`after`)
    """
    if not target.text.strip() or target.field not in (FIELD_INGREDIENTS, FIELD_STEPS):
        return None

    index: int | None = None
    if target.is_step:
        window = align_step(target.text, lines)
        if window is None:
            return None
        aligned = " ".join(lines[window[0] : window[1]])
    else:
        index = align_ingredient(target.text, lines, after)
        if index is None:
            return None
        aligned = lines[index]

    disagreement = compare(target.text, aligned)
    if disagreement is None:
        return index

    missing = list(zip(disagreement.missing, disagreement.spans, strict=True))
    numbers = [(token, span) for token, span in missing if token[0] in ("number", "range")]
    if disagreement.window_blank and numbers and BLANK not in target.text:
        # the second reading saw a gap where this one has a number: possibly invented. The number as written is what
        # the reviewer types over
        start, end = numbers[0][1]
        flags.add(
            CardFlagKind.blank,
            target.field,
            target.ref,
            source=CardFlagSource.cross_read,
            params={"value": target.text[start:end], "start": start, "end": end},
        )
        missing = [(token, span) for token, span in missing if token[0] not in ("number", "range")]

    if missing:
        token, span = missing[0]
        flags.add(
            CardFlagKind.read_disagreement,
            target.field,
            target.ref,
            source=CardFlagSource.cross_read,
            params={"text": aligned, "value": describe_token(token), **_position(span)},
            alternatives=[aligned],
        )
    return index


# ==========================================
# Printed cards: the numbers against Tesseract's reading

OCR_CHECK_MIN_CONFIDENCE = 80
"""Tesseract's mean word confidence from which a page reads as printed, and its numbers can be trusted"""
OCR_CHECK_MIN_SHARE = 0.5
"""Tesseract's text must be at least this share of the transcription's length: it read the card, not a corner of it"""
OCR_ID_SUFFIX = "#ocr"
"""Added to an OCR check's flag id, so it stands beside a second reading's flag on the same line"""

_PLAIN_NUMBER = re.compile(r"^\d+(?:[.,]\d+)?$")


def ocr_check_lines(
    pages: Sequence[PageOCR | None], read_path: IngestReadPath | None, transcription: str | None
) -> list[str] | None:
    """
    Tesseract's lines to check a card's numbers against (`compute_flags`' `ocr_lines`), or None: only when the image
    provider read the card, every page was read by Tesseract at `OCR_CHECK_MIN_CONFIDENCE` or more (printed), and that
    text is at least `OCR_CHECK_MIN_SHARE` of the transcription. It costs nothing: orientation already read the pages.
    """
    if read_path != IngestReadPath.image or not transcription or not pages:
        return None
    if any(page is None or page.confidence < OCR_CHECK_MIN_CONFIDENCE for page in pages):
        return None
    text = "\n".join(page.text for page in pages if page is not None)
    if len(text.strip()) < OCR_CHECK_MIN_SHARE * len(transcription.strip()):
        return None
    return [line.strip() for line in text.splitlines() if line.strip()]


def _clean_numbers(text: str) -> list[NumberMatch]:
    """
    The numbers Tesseract read in `text`, or none at all when one of them touches a letter or digit it may have
    misread ("3S0", "l/2", "35O"): then it can't be told which number is which
    """
    numbers = find_numbers(text)
    for number in numbers:
        start, end = number.span
        if (start and text[start - 1].isalnum()) or (end < len(text) and text[end].isalnum()):
            return []
    return numbers


def _ocr_flags(flags: _Flags, target: _Target, lines: Sequence[str], after: int) -> int | None:
    """
    The OCR check's flag for a target: the first number of the draft's line that Tesseract read, clearly, as another
    whole number in the same place ("375" where Tesseract read "350"). Only plain numbers are compared (Tesseract
    misreads fractions), and only when both say as many numbers. Returns an ingredient's aligned line, as
    `_cross_read_flags` does.
    """
    if not target.text.strip() or target.field not in (FIELD_INGREDIENTS, FIELD_STEPS):
        return None

    index: int | None = None
    if target.is_step:
        window = align_step(target.text, lines)
        if window is None:
            return None
        aligned = " ".join(lines[window[0] : window[1]])
    else:
        index = align_ingredient(target.text, lines, after)
        if index is None:
            return None
        aligned = lines[index]

    _, skip = without_list_marker(target.text) if target.is_step else (target.text, 0)
    drafted = [number for number in find_numbers(target.text) if number.span[0] >= skip]
    read = _clean_numbers(without_list_marker(aligned)[0])
    if not drafted or len(drafted) != len(read):
        return index

    for mine, theirs in zip(drafted, read, strict=True):
        if not (_PLAIN_NUMBER.match(mine.text) and _PLAIN_NUMBER.match(theirs.text)):
            continue
        if mine.value == theirs.value:
            continue
        start, end = mine.span
        flags.add(
            CardFlagKind.read_disagreement,
            target.field,
            target.ref,
            source=CardFlagSource.ocr,
            params={"text": aligned, "value": mine.text, "read": theirs.text, "start": start, "end": end},
            alternatives=[target.text[:start] + theirs.text + target.text[end:]],
            id_ref=f"{target.id_ref or target.ref}{OCR_ID_SUFFIX}",
        )
        break
    return index


def _reading_flags(
    flags: _Flags,
    draft: CardDraft,
    targets: Sequence[_Target],
    extraction: ExtractionMeta | None,
    transcription: str | None,
    ocr_lines: Sequence[str] | None,
) -> None:
    """`unsure`, `not_on_card`, `marker_dropped`, the cross-read's and the OCR check's flags, against what was read"""
    unsure = _unsure_targets(extraction.unsure, targets) if extraction else {}
    on_card = card_numbers(transcription) if transcription is not None else None
    lines = extraction.cross_read_lines if extraction else None
    last_ingredient = -1  # the transcript line the ingredient before aligned with
    last_ocr_line = -1  # and Tesseract's line

    for index, target in enumerate(targets):
        if matched := unsure.get(index):
            entry, span = matched
            flags.add(
                CardFlagKind.unsure,
                target.field,
                target.ref,
                source=CardFlagSource.model,
                params={"text": entry.text, "reason": entry.reason, **(_position(span) if span else {})},
                alternatives=entry.alternatives,
                id_ref=target.id_ref,
            )
        if on_card is not None and target.field in (
            FIELD_INGREDIENTS,
            FIELD_STEPS,
            FIELD_YIELD,
            FIELD_SERVINGS,
            *TIME_FIELDS,
        ):
            # a step's own list number ("3. Bake…", left by the build step or a re-read) isn't an amount
            skip = without_list_marker(target.text)[1] if target.is_step else 0
            if target.ingredient is not None and not is_unedited(target.ingredient):
                pass  # a line the reviewer edited says what they typed
            elif number := _not_on_card(target.text, on_card, skip=skip):
                flags.add(
                    CardFlagKind.not_on_card,
                    target.field,
                    target.ref,
                    source=CardFlagSource.validator,
                    params={"value": number.text, **_position(number.span)},
                )
        if lines:
            aligned = _cross_read_flags(flags, target, lines, last_ingredient)
            if aligned is not None:
                last_ingredient = aligned
        if ocr_lines:
            aligned = _ocr_flags(flags, target, ocr_lines, last_ocr_line)
            if aligned is not None:
                last_ocr_line = aligned

    if transcription is not None:
        read = len(markers_in(transcription))
        kept = sum(len(markers_in(text)) for target in targets for text in target.marker_texts)
        if read > kept:
            flags.add(CardFlagKind.marker_dropped, FIELD_CARD, source=CardFlagSource.validator)


def _is_reading_flag(flag: CardFlag) -> bool:
    return flag.kind in READING_KINDS or (flag.kind == CardFlagKind.blank and flag.source == CardFlagSource.cross_read)


def _reading_key(flag: CardFlag) -> tuple[str, ...]:
    """
    What a reading flag raised before is matched by on a save: its id, but for a note its digest without the position,
    so a note's flag stays raised when a note above it is deleted (its resolution, stored by id, doesn't follow it)
    """
    if flag.field == FIELD_NOTES and "#" in flag.id:
        return (flag.kind.value, FIELD_NOTES, flag.id.rpartition("#")[2], flag.source.value)
    return (flag.id, flag.source.value)


def _carried_over(flag: CardFlag, targets: Sequence[_Target]) -> CardFlag | None:
    """
    A reading flag raised before, as it describes the draft now, judged without what it was raised against (the
    transcription, Tesseract's text), or None when it no longer holds. A number it names must still be in the line,
    and its position is where it is now.
    """
    copy = flag.model_copy(update={"resolution": None}, deep=True)
    if flag.field == FIELD_CARD:
        return copy
    for target in targets:
        if target.field != flag.field or target.ref != (flag.ref or None):
            continue
        value = str(flag.params.get("value", ""))
        if flag.kind == CardFlagKind.not_on_card or flag.source == CardFlagSource.ocr:
            number = next((number for number in find_numbers(target.text) if number.text == value), None)
            if number is None:
                return None
            copy.params.update(_position(number.span))
        return copy
    return None


def _resolved(flag: CardFlag, resolution: FlagResolution | None) -> CardFlag:
    """`flag` with a stored resolution, where it applies: keep for errors that can be kept, dismiss for the rest"""
    if resolution == FlagResolution.kept and flag.severity == CardFlagSeverity.error and flag.kind in KEEPABLE_KINDS:
        flag.resolution = resolution
    elif resolution == FlagResolution.dismissed and flag.severity != CardFlagSeverity.error:
        flag.resolution = resolution
    return flag


def compute_flags(
    draft: CardDraft,
    extraction: ExtractionMeta | None,
    resolutions: Mapping[str, FlagResolution],
    *,
    transcription: str | None = None,
    previous: Sequence[CardFlag] | None = None,
    units: Iterable[str] = (),
    ocr_lines: Sequence[str] | None = None,
) -> list[CardFlag]:
    """
    Every flag the draft raises, in reading order (the card's own flags first), keyed `"<kind>:<field>:<ref>"`, with
    the stored resolutions (by flag id) applied.

    `transcription` is what the card was read as (the job's `transcription`): `not_on_card` and `marker_dropped`
    compare with it. `previous` is the job's stored flags, on a save: reading flags are then kept only where they
    were raised before and still hold, so a reviewer's own edits (a number typed into a blank) never raise them.
    Without it, as after an extraction, every reading flag the draft and `extraction` raise is returned.

    Extraction also passes `units`, the names, plurals, abbreviations and aliases of the group's units (a short word
    that is one of them is a lost unit, `unit_unclear`), and `ocr_lines` (`ocr_check_lines`) for a printed card's
    OCR check. A save passes neither: the flags they raised are kept from `previous` while they still hold (a
    `unit_unclear` while its line is as extracted).
    """
    targets = _targets(draft)
    flags = _Flags()
    english = is_english(extraction.language if extraction else None)
    unit_names = frozenset(unit.lower() for unit in units)

    _card_flags(flags, draft, extraction)
    if not draft.name.strip():
        flags.add(CardFlagKind.missing_name, FIELD_NAME, source=CardFlagSource.validator)

    for target in targets:
        _marker_flags(flags, target)
        if target.ingredient is not None:
            _ingredient_flags(flags, target, units=unit_names, english=english)
        if target.is_step:
            _temperature_flags(flags, target)

    if previous is not None:
        # a lost unit judged by the group's units at extraction: kept while its line is as the parser read it
        unedited = {target.ref for target in targets if target.ingredient and is_unedited(target.ingredient)}
        for flag in previous:
            if flag.kind == CardFlagKind.unit_unclear and flag.id not in flags.flags and flag.ref in unedited:
                flags.flags[flag.id] = flag.model_copy(update={"resolution": None}, deep=True)

    if not any(ingredient_line(ingredient).strip() for ingredient in draft.ingredients):
        flags.add(
            CardFlagKind.empty_section,
            FIELD_INGREDIENTS,
            source=CardFlagSource.validator,
            params={"section": FIELD_INGREDIENTS},
        )
    if not any(step.text.strip() for step in draft.steps):
        flags.add(
            CardFlagKind.empty_section, FIELD_STEPS, source=CardFlagSource.validator, params={"section": FIELD_STEPS}
        )

    reading = _Flags()
    _reading_flags(reading, draft, targets, extraction, transcription, ocr_lines)
    if previous is None:
        reading_flags = list(reading.flags.values())
    else:
        before = {_reading_key(flag): flag for flag in previous if _is_reading_flag(flag)}
        reading_flags = [flag for flag in reading.flags.values() if _reading_key(flag) in before]
        for flag in before.values():
            if flag.id in reading.flags:
                continue
            # flags whose source isn't at hand (the transcription, Tesseract's text) are carried over while they hold
            against_transcription = flag.kind in (CardFlagKind.not_on_card, CardFlagKind.marker_dropped)
            against_ocr = flag.source == CardFlagSource.ocr
            if (against_transcription and transcription is None) or (against_ocr and not ocr_lines):
                if carried := _carried_over(flag, targets):
                    reading_flags.append(carried)

    for flag in reading_flags:
        if flag.id not in flags.flags:
            flags.flags[flag.id] = flag

    return [_resolved(flag, resolutions.get(flag.id)) for flag in _in_reading_order(flags.flags.values(), targets)]


def _in_reading_order(flags: Iterable[CardFlag], targets: Sequence[_Target]) -> list[CardFlag]:
    """The card's flags first, then each target's in the draft's order (an empty section where its lines would be)"""
    position = {(target.field, target.ref): float(index) for index, target in enumerate(targets)}
    times_end = max(index for index, target in enumerate(targets) if target.field in TIME_FIELDS)
    sections = {FIELD_INGREDIENTS: times_end + 0.25, FIELD_STEPS: times_end + 0.5}
    for index, target in enumerate(targets):
        if target.field == FIELD_STEPS:
            sections[FIELD_STEPS] = index - 0.5
            break
    kinds = {kind: index for index, kind in enumerate(_SEVERITY)}

    def key(flag: CardFlag) -> tuple[float, int]:
        if flag.field == FIELD_CARD:
            return -1.0, kinds[flag.kind]
        if flag.ref is None and flag.field in sections:
            return sections[flag.field], kinds[flag.kind]
        return position.get((flag.field, flag.ref), float(len(position))), kinds[flag.kind]

    return sorted(flags, key=key)
