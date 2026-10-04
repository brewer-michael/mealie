"""
The flags that say what to check on a card (docs/ai/PHASE2.md §4.6). `compute_flags` is pure, and the server runs it
after extraction and on every save, so the flags always describe the current draft.

**Ids and fields.** A flag is keyed to a field plus the ingredient's `reference_id`, the step's `id` or the note's
`id` (never an index, F4), with the stable id `"<kind>:<field>:<ref>"` (`ref` empty for single fields and the card).
Fields are the draft's JSON names: `name`, `description`, `recipeYield`, `recipeServings`, `prepTime`, `performTime`,
`totalTime`, `attribution`, `ingredients`, `steps`, `notes` and `card` for the card-level flags. A resolution stored
by id stays with its item when it's edited or moved, and never moves to another. Flags stored before notes had ids
were keyed to the note's position and a digest of what it says (`"<kind>:notes:<position>#<digest>"`): their
resolutions still apply to the note at that position saying that, and their reading flags find the note saying it.

**Three kinds of flags.**
- *Content flags* follow the draft as it is now, edits included: markers, `missing_name`, `implausible_*`,
  `empty_section`, `new_food`, `new_unit` and the card-level `read_by_ocr`, `cross_read_failed`, `not_parsed`,
  `organizers_skipped`.
- *Parse flags* (`check_parse`, `unit_unclear`, `shorthand_read`, `linked_fuzzy`) describe the parser's reading of a
  line, so they drop off once the line is edited: each ingredient keeps an `extracted_hash` of its parsed fields.
- *Reading flags* compare the draft with what was read: `unsure`, `not_on_card`, `marker_dropped`, and the cross-read's
  `read_disagreement` and `blank`. Typing a number into a blank must not raise them, so on a save (`previous` given)
  a reading flag is kept only where it was raised before and still holds; only an extraction raises new ones.

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
import string
from bisect import bisect_left
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from uuid import UUID

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein
from text_unidecode import unidecode

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
from ..shorthand import (
    ABBREVIATIONS,
    FULL_SIZE_WORDS,
    ITEM_SIZE_WORDS,
    QTY,
    SIZE_WORDS,
    UNITS,
    prepare_line,
    unit_spellings,
)
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

PARSE_KINDS = frozenset(
    {CardFlagKind.check_parse, CardFlagKind.unit_unclear, CardFlagKind.shorthand_read, CardFlagKind.linked_fuzzy}
)
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
_LEADING_QUANTITY = re.compile(
    rf"^\s*+[-•*]?+\s*+{QTY}(?:\s*+(?:-|to)\s*+{QTY})?\s*+(?P<token>[^\W\d_]+)(?P<dot>\.)?"
)  # possessive: each run of spaces is read once (`QTY` itself may start with spaces before a "½")
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
        teaspoons tbsp packet packets bottle bottles carton cartons container containers jar jars box boxes tub tubs
        bag bags""".split(),
    }
)
"""
Words that make an amount's unit ("2 T.", "10 3/4 oz."), for what a note keeps of an amount the fields lost; and,
with the containers, what a dotted abbreviation after a quantity may stand for ("2 btls. ketchup": `unit_unclear`)
"""
_MULTIPLIER_WORDS = frozenset({"dozen", "doz"})
"""Words after a quantity that multiply it, which the parser drops: "1 dozen eggs" is read as 1 egg"""
_JOINER = r"(?:[+&,;]|\b(?:and|or|plus)\b)"
"""What joins a second amount to a line: "butter + 1 T. oil", "flour (or 1 1/2 c. bread flour)", "sugar, 1 c. flour" """
_JOINER_MARKS = "+&,;"
_JOINER_WORDS = ("plus", "and", "or")
_KEPT_JOINER = re.compile(rf"(?P<joiner>{_JOINER})\s*+\(?\s*+\d", re.IGNORECASE)
_JOINED_AMOUNT = re.compile(rf"(?P<joiner>{_JOINER})\s*+\(?+\s*+(?P<amount>{QTY})", re.IGNORECASE)
_UNIT_AFTER_AMOUNT = re.compile(r"\s*+-?\s*+(?P<word>[^\W\d_]+)(?P<dot>\.)?")
_FRACTION_GLYPHS = "½⅓⅔¼¾⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞"
_REST_OF_AMOUNT = re.compile(
    rf"(?:(?!{_JOINER})[^\d{_FRACTION_GLYPHS}()\s]|\s++(?!{_JOINER}|[()\d{_FRACTION_GLYPHS}]|$))*+", re.IGNORECASE
)
"""
The words after an amount and its unit, up to the next joiner, parenthesis or number (a fraction glyph too, or the
spaces before it). Each run of spaces is read once (possessive), and each amount's words only up to the next amount,
so a line's are all read in linear time: the spaces between amounts used to be tried every way, and "1 ½ ½ ½…" read
to the end of the line for each "½" (seconds for a few thousand characters).
"""
_LETTERS = re.compile(r"[^\W\d_]+")
_ALTERNATIVE_JOINER = re.compile(r"[+&/]|\b(?:and|or|plus)\b", re.IGNORECASE)
_ALTERNATIVE_LEAD = re.compile(
    rf"(?P<joiner>[+&/]|(?:and|or|plus)\b)\s*+\(?\s*+(?:{QTY}\s*+)?(?:[^\W\d_]+\.?\s*+){{0,2}}", re.IGNORECASE
)
"""
What leads an alternative or a second food on a line, up to its name: the joiner, an amount and a unit or a word or two
("or " of "butter or margarine", "and 1 t. " of "flour and 1 t. baking powder", "/" of "butter/margarine")
"""
_AMOUNT_AFTER_JOINER = re.compile(rf"\s*+\(?\s*+{QTY}")
_ALTERNATIVE_START = re.compile(rf"^(?P<joiner>[+&/]|(?:and|or|plus)\b)\s*+\(?\s*+(?P<amount>{QTY})?", re.IGNORECASE)
"""A note's part that keeps an alternative or a second food, from its joiner ("or margarine", "and 1 t. soda")"""
_ALTERNATIVE_LOOKBACK = 48
"""How far before an alternative's name its joiner and amount are looked for ("and 1 1/2 tbsp. " fits)"""
MAX_ANALYSED_LINE = 500
"""
An ingredient line longer than this (a card's never is: a misread page, or text pasted into one line) isn't searched
for amounts its fields lost, so a save's flags take bounded time whatever the draft holds; it keeps its text, and the
parser's confidence still decides `check_parse`
"""
_SECOND_FOOD_FILLER = frozenset({"of", "more", "each", "extra", "additional", "about"})
"""Words after a joined amount that name no food ("1 c. sugar plus 2 T. more"): the same ingredient again"""
_WORD = re.compile(r"[^\W\d_]+\.?")
_UNIT_NAMES = sorted(word for word in _UNIT_WORDS if len(word) >= 5)
"""Unit words written in full, which a card abbreviates in its own way ("tblsp." for "tablespoon")"""

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
    CardFlagKind.linked_fuzzy: CardFlagSeverity.warning,
    CardFlagKind.implausible_amount: CardFlagSeverity.warning,
    CardFlagKind.implausible_temperature: CardFlagSeverity.warning,
    CardFlagKind.empty_section: CardFlagSeverity.warning,
    CardFlagKind.read_by_ocr: CardFlagSeverity.warning,
    CardFlagKind.cross_read_failed: CardFlagSeverity.info,
    CardFlagKind.shorthand_read: CardFlagSeverity.info,
    CardFlagKind.not_parsed: CardFlagSeverity.info,
    CardFlagKind.organizers_skipped: CardFlagSeverity.info,
    CardFlagKind.new_food: CardFlagSeverity.info,
    CardFlagKind.new_unit: CardFlagSeverity.info,
}


ORGANIZERS_STEP = "resolve-organizers"
"""Upstream's organizer step (`ResolveOrganizersStep.name`), by which `ExtractionMeta.step_outcomes` names it"""
ORGANIZERS_SKIPPED_REASONS = ("local_only", "limit_reached")
"""Why the organizer step asked no provider: no local one for a local-only card, or all over their monthly limit"""


def organizers_outcome(reason: str) -> str:
    """The organizer step's outcome when it asked no provider (`ORGANIZERS_SKIPPED_REASONS`): `"skipped:<reason>"`"""
    return f"skipped:{reason}"


def organizers_skipped(extraction: ExtractionMeta | None) -> str | None:
    """
    Why the card got no tag, category or tool suggestions, from its organizer step's outcome: `local_only` or
    `limit_reached` (`organizers_outcome`), or `failed`. None when they were made, or weren't asked for (a group
    without organizers, or suggestions turned off: the step didn't run or was plainly `skipped`).
    """
    outcome = extraction.step_outcomes.get(ORGANIZERS_STEP) if extraction else None
    if outcome == "failed":
        return "failed"
    kind, _, reason = (outcome or "").partition(":")
    if kind == "skipped" and reason in ORGANIZERS_SKIPPED_REASONS:
        return reason
    return None


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


def _hashed(fields: list, *, split: bool) -> str:
    if split:
        fields = [*fields, "split"]
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode()).hexdigest()[:16]


def _hash_fields(ingredient: CardDraftIngredient) -> list:
    return [
        round(ingredient.quantity, 6) if ingredient.quantity is not None else None,
        [str(ingredient.unit.id) if ingredient.unit.id else None, ingredient.unit.name] if ingredient.unit else None,
        [str(ingredient.food.id) if ingredient.food.id else None, ingredient.food.name] if ingredient.food else None,
        ingredient.note,
    ]


def ingredient_hash(ingredient: CardDraftIngredient, *, split: bool = False) -> str:
    """
    A hash of an ingredient's parsed fields, stored as `extracted_hash` when it's extracted. `split`: the parser split
    an alternative or a second food off the line, which the note keeps (`keep_alternatives`); the hash says so, and
    `check_parse` names it while the line is as parsed (`split_off`). The draft needs no field for it: once the line
    is edited, its parse flags drop off anyway.
    """
    return _hashed(_hash_fields(ingredient), split=split)


def _parse_split(ingredient: CardDraftIngredient) -> bool | None:
    """Whether the parser split something off the line (`ingredient_hash`); None once its fields are edited"""
    if not ingredient.extracted_hash:
        return None
    fields = _hash_fields(ingredient)
    return next((split for split in (False, True) if _hashed(fields, split=split) == ingredient.extracted_hash), None)


def is_unedited(ingredient: CardDraftIngredient) -> bool:
    """Whether an ingredient's parsed fields are still as extracted"""
    return _parse_split(ingredient) is not None


def split_off(ingredient: CardDraftIngredient) -> bool:
    """
    Whether the parser split an alternative or a second food off the line (`keep_alternatives`), and its fields are
    still as it read them
    """
    return _parse_split(ingredient) is True


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
    legacy_ref: str | None = None
    """A note's position and digest: what flags stored before notes had ids were keyed to (`_legacy_note_ref`)"""


_LEGACY_NOTE_REF = re.compile(rf"^{FIELD_NOTES}:\d+#(?P<digest>[0-9a-f]{{8}})$")
"""The end of a note flag's id stored before notes had ids: `"notes:<position>#<digest>"`"""


def _legacy_note_ref(index: int, title: str, text: str) -> str:
    """
    What a note's flags were keyed to before notes had ids: its position and a digest of its title and text
    (`"<position>#<digest>"`)
    """
    digest = hashlib.sha256(json.dumps([title, text], ensure_ascii=False).encode()).hexdigest()[:8]
    return f"{index}#{digest}"


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
        targets.append(
            _Target(
                FIELD_NOTES,
                str(note.id),
                note.text,
                [text for text in texts if text],
                legacy_ref=_legacy_note_ref(index, note.title, note.text),
            )
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
    if reason := organizers_skipped(extraction):
        # so the reviewer knows why there are no suggestions, rather than seeing none
        flags.add(CardFlagKind.organizers_skipped, FIELD_CARD, source=CardFlagSource.model, params={"reason": reason})


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


def _is_word_character(character: str) -> bool:
    """Whether `re` takes a character for part of a word (`\\w`)"""
    return character.isalnum() or character == "_"


def _joiner_before(text: str, start: int) -> int | None:
    """
    Where the joiner (`_JOINER`) of an amount at `start` begins, when only spaces and an opening parenthesis stand
    between them ("+ 2 T.", "or (3", "flour, 1 c."); None when there's none. It looks back over those spaces only, so
    every amount of a line is checked in linear time (searching the line from its start for each took quadratic time).
    """
    index = start
    while index and text[index - 1].isspace():
        index -= 1
    if index and text[index - 1] == "(":
        index -= 1
        while index and text[index - 1].isspace():
            index -= 1
    if index and text[index - 1] in _JOINER_MARKS:
        return index - 1
    if index < len(text) and _is_word_character(text[index]):
        return None  # "and2": a joining word ends at a word's edge
    for word in _JOINER_WORDS:
        begin = index - len(word)
        if begin >= 0 and text[begin:index].lower() == word and not (begin and _is_word_character(text[begin - 1])):
            return begin
    return None


class _Parentheses:
    """Where a line's parentheses are, so the pair around an amount is found in logarithmic time"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.positions = [index for index, character in enumerate(text) if character in "()"]

    def around(self, start: int, end: int) -> tuple[int, int] | None:
        """The parentheses right around `start` to `end`, with no other parenthesis between: "(10 3/4 oz.)" """
        after = bisect_left(self.positions, end)
        before = bisect_left(self.positions, start) - 1
        if before < 0 or after >= len(self.positions):
            return None
        opening, closing = self.positions[before], self.positions[after]
        if self.text[opening] != "(" or self.text[closing] != ")":
            return None
        return opening, closing


def _words_at(text: str) -> list[tuple[int, int, str]]:
    """The words of `text` (letters only, as `letters_only` reads them) with where each is: `(start, end, lowercase)`"""
    blanked = MARKER_RE.sub(lambda marker: " " * len(marker.group(0)), text)
    return [(match.start(), match.end(), match.group(0).lower()) for match in _LETTERS.finditer(blanked)]


def _held_by(names: Iterable[str]) -> Callable[[str], bool]:
    """
    Whether a word (lowercase) is one of the words of `names`, but for a simple plural either way round
    (`_same_word`), checked in constant time
    """
    words = {word for name in names for word in letters_only(name).split()}
    forms = {form for word in words for form in _inflections(word)}
    return lambda word: word in forms or not words.isdisjoint(_inflections(word))


def _amounts(text: str, *, in_a_name: bool = False) -> list[Fraction]:
    """
    The amounts in `text`: each number, and a range's end. `in_a_name`: only the numbers that are part of a unit or
    food name ("2% milk", "V8 juice", "7-Up", '9" pie shell'), not a second amount run into it ("butter + 1 T. oil").
    """
    return [
        value
        for number in find_numbers(text)
        if not (in_a_name and _joiner_before(text, number.span[0]) is not None)
        for value in (number.value, number.end)
        if value is not None
    ]


def _merged_amount(line: str, food: str | None) -> re.Match[str] | None:
    """
    A second amount whose ingredient the parser ran into the food: "2 c. flour (or 1 1/2 c. bread flour)" read as the
    food "flour bread flour", "1 c. sugar, 1 c. flour" as "sugar flour". The food takes words from before the joined
    amount and from only after it; "2 T. + 1 t. sugar" or "1 pkg. yeast (or 2 1/4 tsp.)" don't. Each of the food's
    words is found once on the line, so a line with many joined amounts takes linear time.
    """
    food_words = set(letters_only(food).split()) if food else set()
    if not food_words:
        return None
    first_end: dict[str, int] = {}
    last_start: dict[str, int] = {}
    for start, end, word in _words_at(line):
        if word in food_words:
            first_end.setdefault(word, end)
            last_start[word] = start
    for joined in _JOINED_AMOUNT.finditer(line):
        before = {word for word, end in first_end.items() if end <= joined.start()}
        if before and any(last_start.get(word, -1) >= joined.end() for word in food_words - before):
            return joined
    return None


def _second_ingredient(line: str, food: str | None) -> LostAmount | None:
    """
    An amount joined to the line by "and", "plus", "+", "&" or a comma whose food the line's food isn't, wherever the
    parser put it: in the note ("2 c. flour and 1 t. soda" read as flour with the note "and 1 tsps soda"), or split off
    (`keep_alternatives`). The fields hold its amount, but it's no ingredient of the recipe, so the line is checked
    (`kept` None). The same food again ("1 c. plus 2 T. flour", "2 T. + 1 t. sugar", "1 c. sugar plus 2 T. more") is
    one ingredient; an alternative after "or" ("2 c. flour (or 1 1/2 c. bread flour)", "1 c. buttermilk (or 1 c. milk
    + 1 T. vinegar)"), which the note keeps whole, reads as written.
    """
    held = _held_by([food] if food else [])
    for joined in _JOINED_AMOUNT.finditer(line):
        if joined.group("joiner").lower() == "or":
            return None
        end = _unit_end(line, joined.end("amount"))
        if end >= len(line) or not line[end].isspace():
            continue  # no food after it ("+ 2 T."), or not an amount of one ("80% lean")
        rest = _REST_OF_AMOUNT.match(line, end)
        words = letters_only(rest.group(0)).split() if rest else []
        if any(not held(word) for word in words if word not in _SECOND_FOOD_FILLER and word not in _SIZE_WORD_TOKENS):
            span = stripped_span(line, joined.span("amount"))
            return LostAmount(value=line[span[0] : span[1]], span=span, kept=None)
    return None


def _unit_end(line: str, end: int) -> int:
    """
    Where an amount that ends at `end` ends with its unit ("2 T.", "10 3/4 oz.", "8-oz."); `end` when no unit follows
    it, or only a size ("or 2 lg.": a size word the note keeps on its own)
    """
    match = _UNIT_AFTER_AMOUNT.match(line, end)
    if match is None:
        return end
    word = match.group("word").lower()
    if word in _SIZE_WORD_TOKENS:
        return end
    if word in _UNIT_WORDS or (match.group("dot") and len(word) <= 4):
        return match.end()
    return end


def _kept_text(line: str, span: tuple[int, int], field_words: set[str], parentheses: _Parentheses) -> str:
    """
    What a note keeps of a lost amount at `span`: the parentheses around it ("(10 3/4 oz.)"), else the amount with the
    word that joins it to the line and its unit ("+ 2 T.", "or 3"), else the amount and its unit; with the words after
    them up to the next joiner when the fields don't hold them (the "brown sugar" of "1 c. sugar, 1 c. brown sugar")
    """
    start, end = span
    if group := parentheses.around(start, end):
        return line[group[0] : group[1] + 1].strip()

    end = _unit_end(line, end)
    rest = _REST_OF_AMOUNT.match(line, end)
    if rest and set(letters_only(rest.group(0)).split()) - field_words:
        end = rest.end()
    if (joiner := _joiner_before(line, start)) is not None:
        start = joiner
    return line[start:end].strip().lstrip(",;").strip()


def _close_to(value: Fraction, quantity: float) -> bool:
    """Whether a number on the line is the parsed quantity; a number too large for a float (300 digits) never is"""
    try:
        return math.isclose(float(value), quantity, abs_tol=1e-3)
    except OverflowError:
        return False


def lost_amounts(line: str, quantity: float | None, unit: str | None, food: str | None, note: str) -> list[LostAmount]:
    """
    The amounts on an ingredient's line (as read from the card) that its parsed fields lost, in order: the parser keeps
    one quantity, so a range's end ("2-3 T. milk" is read as 2), a second number ("2 or 3 eggs", the "10 3/4 oz." of
    "1 can (10 3/4 oz.) soup", the "2 T." of "1 c. sugar + 2 T."), a second ingredient run into the food or kept only in
    the note, or a "dozen" would be gone from the recipe commit writes from the fields. The amounts are counted: the
    quantity and each number the note or a name keeps account for one each. A line longer than `MAX_ANALYSED_LINE`
    isn't searched. Every step reads the line in linear time.
    """
    if len(line) > MAX_ANALYSED_LINE:
        return []
    in_fields = Counter(_amounts(note))
    for name in (unit, food):
        if name:
            in_fields.update(_amounts(name, in_a_name=True))
    field_words = set(letters_only(" ".join(text for text in (note, unit, food) if text)).split())
    parentheses = _Parentheses(line)

    lost: list[LostAmount] = []
    left = quantity
    for number in find_numbers(line):
        if line.startswith("%", number.span[1]):
            continue  # a share ("80% lean", "2% milk"), not an amount
        missing: list[bool] = []
        for value in (number.value, number.end):
            if value is None:
                continue
            if left is not None and _close_to(value, left):
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
            kept = _kept_text(line, number.span, field_words, parentheses)
        lost.append(LostAmount(value=number.text, span=number.span, kept=kept))
    if lost:
        return lost

    if merged := _merged_amount(line, food):
        span = stripped_span(line, merged.span("amount"))
        return [LostAmount(value=line[span[0] : span[1]], span=span, kept=None)]
    if second := _second_ingredient(line, food):
        return [second]

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
    flags find the amounts again from the note it made (`check_parse` still asks the reviewer to look, but for a
    measure in parentheses after the unit: `_equivalent`).
    """
    lost = lost_amounts(line, quantity, unit, food, note)
    kept = list(dict.fromkeys(amount.kept for amount in lost if amount.kept))
    if kept and (joiner := _KEPT_JOINER.match(kept[0])):
        # the parser kept the word that joins the amount on its own ("plus" of "1 c. sugar plus 2 T."): said once
        before, _, last = note.rpartition(", ")
        if last.strip().lower() == joiner.group("joiner").lower():
            note = before
    return ", ".join(part for part in (note, *kept) if part), lost


def _is_unit_word(word: str) -> bool:
    """Whether a word (lowercase, without its dot) names a unit: "oz", "cups", "tsps", "stick", "fl" """
    return word in _UNIT_WORDS or (word.endswith("s") and word[:-1] in _UNIT_WORDS) or len(unit_spellings(word)) > 1


_EQUIVALENT = re.compile(rf"\(\s*+(?P<amount>{QTY})\s*+-?\s*+(?P<unit>[^\W\d_]+)\.?\s*+\)")
"""A measure in parentheses: "(8 oz.)", "(10 3/4 oz.)", "(1 stick)", "(1/2 c.)" """
_SIZES = "|".join(sorted((*FULL_SIZE_WORDS, *ITEM_SIZE_WORDS, *SIZE_WORDS), key=len, reverse=True))
_UNIT_BEFORE_EQUIVALENT = re.compile(rf"^\s*+[-•*]?\s*+{QTY}\s*+(?:(?i:{_SIZES})\.?\s++)?(?P<unit>[^\W\d_]+)\.?\s*+$")
"""The line before such a measure: its quantity and unit only, a size word too ("1 pkg. ", "1/2 c. ", "1 large can ")"""


def _written_unit(token: str) -> tuple[str, ...]:
    """The spellings of a unit as a card writes it after the quantity: shorthand written out first ("T." is tbsp)"""
    token = token.rstrip(".")
    if token in UNITS or token.lower() in ABBREVIATIONS:
        token = UNITS.get(token) or ABBREVIATIONS[token.lower()]
    return unit_spellings(token)


def _equivalent(line: str, lost: LostAmount, unit: str | None) -> bool:
    """
    Whether a lost amount is the same amount in another measure, in parentheses right after the line's quantity and
    unit, which the note keeps as written: "1 pkg. (8 oz.) cream cheese", "1 can (10 3/4 oz.) soup", "1/2 c. (1 stick)
    butter". The fields read the line right, as for a package size before the unit ("1 (8 oz.) pkg.", taken out
    before parsing), so it isn't checked. One with a joiner or a range ("1 c. (or 2) eggs", "(8-10 oz.)"), in the
    line's own unit ("2 c. (3 c.) flour"), after a unit the parser didn't read, or anywhere else is a second amount.
    """
    if not unit or not lost.kept or not (measure := _EQUIVALENT.fullmatch(lost.kept)):
        return False
    if not _is_unit_word(measure.group("unit").lower()):
        return False
    opening = line.rfind("(", 0, lost.span[0])
    if opening < 0 or not line.startswith(lost.kept, opening):
        return False
    written = _UNIT_BEFORE_EQUIVALENT.match(line, 0, opening)
    if written is None:
        return False
    parsed = unit_spellings(unit)
    return _written_unit(written.group("unit")) == parsed != _written_unit(measure.group("unit"))


def _lost_amount(ingredient: CardDraftIngredient, *, english: bool) -> LostAmount | None:
    """
    The first amount the parsed line lost: one the fields still lack (a draft parsed before notes kept them), or one
    its note keeps at its end exactly as `keep_lost_amounts` appended it at parse time, but for the same amount in
    another measure right after the unit (`_equivalent`). The note parsing started from began with what was taken out
    of an English card's line before parsing (`prepare_line`): a package size "(8 oz.)" there was never lost. Only the
    last few of the note's parts can be what was appended (one per number on the line, a parenthesis or two over), so
    a long note is never read again for each of its parts.
    """
    line, quantity, note = ingredient.original_text, ingredient.quantity, ingredient.note
    unit = ingredient.unit.name if ingredient.unit else None
    food = ingredient.food.name if ingredient.food else None
    if len(line) > MAX_ANALYSED_LINE:
        return None
    if lost := lost_amounts(line, quantity, unit, food, note):
        return lost[0]

    taken_out = ", ".join(prepare_line(line).notes) if english else ""
    starts = [0, *(separator.start() for separator in re.finditer(", ", note))]
    # the longest note parsing may have started from first: the fewest amounts said to be lost
    for start in reversed(starts[-(2 * len(find_numbers(line)) + 2) :]):
        base = note[:start]
        if not base.startswith(taken_out):
            continue
        kept, lost = keep_lost_amounts(line, quantity, unit, food, base)
        if lost and kept == note:
            checked = [amount for amount in lost if not _equivalent(line, amount, unit)]
            return checked[0] if checked else None
    return None


# ==========================================
# Alternatives the parser split off a line


def _alternative_part(line: str, start: int, end: int, held: Callable[[str], bool]) -> str:
    """
    What a note keeps of an alternative or a second food at `start` to `end` on the line: from the word that joins it
    ("or margarine", "and 1 t. baking powder"), a slash as "or" ("butter/margarine"); on a card in another language,
    from the short word before it that the fields don't hold ("oder Margarine"), else the words alone
    """
    offset = max(0, start - _ALTERNATIVE_LOOKBACK)
    # the nearest joiner from which only an amount and a word or two lead to the name
    for joiner in reversed(list(_ALTERNATIVE_JOINER.finditer(line, offset, start))):
        if _ALTERNATIVE_LEAD.fullmatch(line, joiner.start(), start):
            if joiner.group(0) == "/":
                return f"or {line[start:end]}"
            return line[joiner.start() : end]
    if before := _words_at(line[offset:start]):
        word_start, word_end, word = before[-1]
        if len(word) <= 4 and not held(word) and not line[offset + word_end : start].strip():
            return line[offset + word_start : end]
    return line[start:end]


def _alternative_words(name: str) -> list[str]:
    """
    The words of an alternative's name as the parser gave it, without an amount's unit: "8 ounce yogurt" is "yogurt",
    "10 ³/₄ ounce" nothing (the line's amount, which `keep_lost_amounts` keeps)
    """
    words = [word for word in letters_only(name).split() if word.isalpha()]
    if any(character.isnumeric() for character in name):
        words = [word for word in words if not _is_unit_word(word)]
    return words


def _unheld_after_joiner(line: str, held: Callable[[str], bool]) -> tuple[int, int] | None:
    """
    Where the first words after a joiner (and an amount) on the line that the fields don't hold are: the "oleo" of "1
    c. butter or oleo" when the parser linked it to the group's "margarine"
    """
    for joiner in _ALTERNATIVE_JOINER.finditer(line):
        start = joiner.end()
        if amount := _AMOUNT_AFTER_JOINER.match(line, start):
            start = _unit_end(line, amount.end())
        rest = _REST_OF_AMOUNT.match(line, start)
        if rest is None:
            continue
        found = _words_at(rest.group(0))
        if found and not all(held(word) for _, _, word in found):
            return start + found[0][0], start + found[-1][1]
    return None


def _find_run(words: Sequence[tuple[int, int, str]], name: Sequence[str]) -> tuple[int, int] | None:
    """Where the words of `name` stand in a row among `words` (`_words_at`), but for simple plurals: the last place"""
    found: tuple[int, int] | None = None
    for index in range(len(words) - len(name) + 1):
        if all(_same_word(words[index + offset][2], word) for offset, word in enumerate(name)):
            found = (words[index][0], words[index + len(name) - 1][1])
    return found


def _renders(part: str, kept: str) -> bool:
    """
    Whether a note's part is the parser's own rendering of an alternative's joiner and amount, without its food: its
    "and 1 tsps" for the line's "and 1 t. baking powder"
    """
    mine, theirs = _ALTERNATIVE_START.match(kept), _ALTERNATIVE_START.match(part)
    if not (mine and theirs and mine.group("amount") and theirs.group("amount")):
        return False
    if mine.group("joiner").lower() != theirs.group("joiner").lower():
        return False
    if [number.value for number in find_numbers(part)] != [number.value for number in find_numbers(mine.group(0))]:
        return False
    return all(_is_unit_word(word) for word in letters_only(part[theirs.end() :]).split())


def keep_alternatives(
    line: str, note: str, alternatives: Iterable[Sequence[str]], fields: Iterable[str | None]
) -> tuple[str, bool]:
    """
    The note with each alternative or second food the parser split off the line (its substitutions) kept as the line
    writes it, from the word that joins it: "1/2 c. butter or margarine, softened" gets "softened, or margarine", "2 c.
    flour and 1 t. baking powder" "and 1 t. baking powder" (in place of the parser's "and 1 tsps"); and whether one
    was. `alternatives`: each split-off ingredient by the names the parser gave it (its text, or the linked food's name
    and plural). One whose words `fields` (the food, unit and note as parsed, lost amounts kept) already hold, or that
    is only an amount ("8 ounce" for "(8 oz.)", which `keep_lost_amounts` keeps), adds nothing. Nothing read from the
    card is lost, and `check_parse` asks the reviewer to look while the line is as parsed (`ingredient_hash`'s `split`).
    """
    known = [text for text in fields if text]
    held = _held_by(known)
    words = _words_at(line)
    parts = note.split(", ") if note else []
    split = False
    for names in alternatives:
        part: str | None = None
        named: str | None = None
        for name in names:
            name_words = _alternative_words(name)
            if not name_words or all(held(word) for word in name_words):
                break  # only an amount ("8 ounce" for "(8 oz.)", which the note keeps), or nothing lost
            missing = [word for word in name_words if not held(word)]
            # by its name as the line writes it, else by the words the fields lack ("turkey" for "ground turkey")
            if run := _find_run(words, name_words) or _find_run(words, missing):
                part = _alternative_part(line, *run, held)
                break
            named = named or name.strip()
        else:
            # the parser named it otherwise (a food linked by an alias): the line's words after a joiner that the
            # fields lack ("or oleo"), else the parser's name
            if run := _unheld_after_joiner(line, held):
                part = _alternative_part(line, *run, held)
            elif named:
                part = f"or {named}"
        if not part:
            continue
        if parts and _renders(parts[-1], part):
            parts[-1] = part
        else:
            parts.append(part)
        held = _held_by([*known, *parts])
        split = True
    return ", ".join(parts), split


def _split_alternative(ingredient: CardDraftIngredient) -> tuple[str, tuple[int, int] | None]:
    """
    The alternative or second food the parser split off a line (`keep_alternatives`), as its note keeps it, and where
    its name is on the line: the last of the note's parts that a joiner starts and whose food the line's food isn't
    ("margarine" of "softened, or margarine"), else the note's last part
    """
    line, note = ingredient.original_text, ingredient.note
    held = _held_by([ingredient.food.name] if ingredient.food else [])
    parts = [part for part in note.split(", ") if part.strip()]
    for part in reversed(parts):
        start = _ALTERNATIVE_START.match(part)
        if start is None:
            continue
        name_start = _unit_end(part, start.end("amount")) if start.group("amount") else start.end()
        name = part[name_start:].strip(" ()")
        if not name or all(held(word) for word in letters_only(name).split()):
            continue
        found = line.lower().find(name.lower())
        return name, ((found, found + len(name)) if found >= 0 else None)
    last = parts[-1].strip() if parts else ""
    found = line.lower().find(last.lower()) if last else -1
    return last, ((found, found + len(last)) if found >= 0 else None)


def _size_word_in_names(ingredient: CardDraftIngredient) -> str | None:
    """A size word the parser put in the unit or food's name ("cup scant", "heaping flour"), as written there"""
    for ref in (ingredient.unit, ingredient.food):
        for word in _WORD.findall(ref.name if ref else ""):
            if word.lower().rstrip(".") in _SIZE_WORD_TOKENS:
                return word
    return None


def _abbreviates_a_unit(word: str) -> bool:
    """
    Whether a longer word after a quantity (4-6 letters, lowercase) is a unit's plural ("pkgs", "ozs") or the card's
    own abbreviation of a unit word: its first letter, then letters of it in order ("tblsp", "teasp", "envs"). Foods
    aren't ("eggs", "pears", "lemon").
    """
    if word in _UNIT_WORDS or (word.endswith("s") and word[:-1] in UNIT_VOCABULARY | _UNIT_WORDS):
        return True
    for name in _UNIT_NAMES:
        if name[0] != word[0] or len(name) <= len(word):
            continue
        letters = iter(name[1:])
        if all(letter in letters for letter in word[1:]):
            return True
    return False


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


# ==========================================
# Links that aren't an exact name match

LINK_SPAN_MIN_SCORE = 60
"""A `linked_fuzzy` flag points at the part of the line its linked name matches best, if it scores this much"""
UNIT_LINK_ID_SUFFIX = "#unit"
"""Added to the id of a unit's `linked_fuzzy` flag, so it stands beside the food's on the same line"""

_LINK_SEPARATORS = str.maketrans(dict.fromkeys(string.punctuation + "‘’“”", " "))


def _link_words(text: str) -> list[str]:
    """
    `text` as linked names are compared: ASCII and lower case, as the matcher's `normalize` makes it, and split into
    words at spaces and punctuation (apostrophes too: "confectioners' sugar" is "confectioners sugar")
    """
    return unidecode(text).translate(_LINK_SEPARATORS).lower().split()


def _inflections(word: str) -> set[str]:
    """A word and its simple English plurals ("onions", "tomatoes", "berries", "leaves")"""
    forms = {word, f"{word}s", f"{word}es"}
    if word.endswith("y"):
        forms.add(f"{word[:-1]}ies")
    if word.endswith("f"):
        forms.add(f"{word[:-1]}ves")
    if word.endswith("fe"):
        forms.add(f"{word[:-2]}ves")
    return forms


def _same_word(a: str, b: str) -> bool:
    """Whether two words are the same but for a simple plural, either way round ("egg" on the line "2 eggs")"""
    return a == b or b in _inflections(a) or a in _inflections(b)


def _named_on_line(names: Iterable[str], lines: Iterable[list[str]]) -> bool:
    """Whether one of `names` is on one of the `lines` (`_link_words` each) as a run of whole words"""
    for name in names:
        words = _link_words(name)
        if not words:
            continue
        for line in lines:
            for start in range(len(line) - len(words) + 1):
                if all(_same_word(word, line[start + offset]) for offset, word in enumerate(words)):
                    return True
    return False


def _fuzzy_links(
    ingredient: CardDraftIngredient, linked: Mapping[UUID, Collection[str]], *, english: bool
) -> list[tuple[str, str]]:
    """
    The food and unit the line links that none of their names is on: `("food" | "unit", the linked name)`. A linked
    item goes by its name, plural and aliases (a unit by its abbreviations too, `linked`, and by the common unit's
    every spelling, `shorthand.UNIT_SPELLINGS`: "lb." is a pound's); the line is read as written and, on an English
    card, with its shorthand written out ("1 T. sugar" says "tbsp"). An id `linked` doesn't hold (no longer the
    group's) isn't judged.
    """
    line = ingredient.original_text
    lines = [_link_words(line)]
    if english:
        lines.append(_link_words(prepare_line(line).text))

    fuzzy: list[tuple[str, str]] = []
    for kind, ref in (("food", ingredient.food), ("unit", ingredient.unit)):
        if ref is None or ref.id is None or not ref.name.strip():
            continue
        names = linked.get(ref.id)
        if names is None:
            continue
        known = [ref.name, *names]
        if kind == "unit":
            # a group's unit may have no abbreviation ("teaspoon": commit creates them so), and the line says "tsp."
            known += [spelling for name in known for spelling in unit_spellings(name)]
        if not _named_on_line(known, lines):
            fuzzy.append((kind, ref.name))
    return fuzzy


def _link_span(name: str, line: str) -> tuple[int, int] | None:
    """
    Where on the line the words a linked name was matched from probably are ("rd onions" for "red onion"): the part
    of the line most like the name, widened to whole words
    """
    alignment = fuzz.partial_ratio_alignment(name.lower(), line.lower())
    if alignment is None or alignment.score < LINK_SPAN_MIN_SCORE or alignment.dest_end <= alignment.dest_start:
        return None
    start, end = stripped_span(line, (alignment.dest_start, alignment.dest_end))
    if start >= end:
        return None
    while start > 0 and line[start].isalnum() and line[start - 1].isalnum():
        start -= 1
    while end < len(line) and line[end - 1].isalnum() and line[end].isalnum():
        end += 1
    return start, end


def _ingredient_flags(
    flags: _Flags,
    target: _Target,
    *,
    units: Collection[str],
    english: bool,
    linked: Mapping[UUID, Collection[str]] | None = None,
) -> None:
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
        if split_off(ingredient):
            # an alternative or a second food the parser split off, which the note keeps: the reviewer sees the split
            name, span = _split_alternative(ingredient)
            params["alternative"] = name
            if span is not None and "start" not in params:
                params.update(_position(span))
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
            # a 1-3 letter word, or a longer abbreviation of a unit with its dot ("2 tblsp. sugar", "2 pkgs. yeast")
            if (
                (len(token) <= 3 or (dot and len(token) <= 6 and _abbreviates_a_unit(word)))
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

        # a food or unit the matcher linked by a near-miss name ("2 rd onions" -> "red onion"); a unit's flag gets an
        # id of its own, so both can stand on one line
        for kind, name in _fuzzy_links(ingredient, linked, english=english) if linked is not None else []:
            params = {"name": name, "kind": kind}
            if span := _link_span(name, ingredient.original_text):
                params.update(_position(span))
            flags.add(
                CardFlagKind.linked_fuzzy,
                field,
                ref,
                source=CardFlagSource.parser,
                params=params,
                id_ref=f"{ref}{UNIT_LINK_ID_SUFFIX}" if kind == "unit" else None,
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
OCR_DIGIT_CONFUSIONS = frozenset(frozenset(pair) for pair in ("17", "08", "38", "56", "68", "06", "09"))
"""
Digits Tesseract reads one for the other on printed cards: an italic "1" as "7" (3 of 10 printed cards in a live run
were flagged so, "Use '7 onion'"), "0" and "8", "5" and "6". Its word confidence doesn't tell them apart (the "7"s
scored 69 to 92), so a number that differs from the draft's only by them isn't a disagreement worth a look.
"""
_STROKE_DIGITS = "17"
"""A stroke Tesseract splits off a "1" or reads beside one: the italic "1" read as "71" """


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


def _misread_digits(mine: str, theirs: str) -> bool:
    """
    Whether Tesseract's number may be the draft's misread: each digit the same or one it confuses with it
    (`OCR_DIGIT_CONFUSIONS`), with at most one stroke beside a "1" or "7" more ("1" read as "71")
    """
    if len(theirs) == len(mine) + 1:
        return any(
            theirs[index] in _STROKE_DIGITS
            and any(0 <= beside < len(theirs) and theirs[beside] in _STROKE_DIGITS for beside in (index - 1, index + 1))
            and _misread_digits(mine, theirs[:index] + theirs[index + 1 :])
            for index in range(len(theirs))
        )
    return len(theirs) == len(mine) and all(
        a == b or frozenset((a, b)) in OCR_DIGIT_CONFUSIONS for a, b in zip(mine, theirs, strict=True)
    )


def _ocr_flags(flags: _Flags, target: _Target, lines: Sequence[str], after: int) -> int | None:
    """
    The OCR check's flag for a target: the first number of the draft's line that Tesseract read, clearly, as another
    whole number in the same place ("375" where Tesseract read "350"). Only plain numbers are compared (Tesseract
    misreads fractions), only when both say as many numbers, and never digits Tesseract confuses ("1" read as "7":
    `_misread_digits`). Returns an ingredient's aligned line, as `_cross_read_flags` does.
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
        if mine.value == theirs.value or _misread_digits(mine.text, theirs.text):
            continue
        start, end = mine.span
        flags.add(
            CardFlagKind.read_disagreement,
            target.field,
            target.ref,
            source=CardFlagSource.ocr,
            params={"text": aligned, "value": mine.text, "read": theirs.text, "start": start, "end": end},
            alternatives=[target.text[:start] + theirs.text + target.text[end:]],
            id_ref=f"{target.ref}{OCR_ID_SUFFIX}",
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


def _reading_key(flag: CardFlag, notes_by_digest: Mapping[str, str]) -> tuple[str, str]:
    """
    What a reading flag raised before is matched by on a save: its id and source. A note's flag stored before notes
    had ids (`"<kind>:notes:<position>#<digest>"`) is matched as the flag of the note that says the same now, wherever
    it is (`notes_by_digest`: a note's id by its digest).
    """
    if flag.field == FIELD_NOTES and (legacy := _LEGACY_NOTE_REF.match(flag.id.removeprefix(f"{flag.kind.value}:"))):
        note_id = notes_by_digest.get(legacy.group("digest"))
        if note_id is not None:
            return flag_id(flag.kind, FIELD_NOTES, note_id), flag.source.value
    return flag.id, flag.source.value


def _legacy_note_ids(flag: CardFlag, targets: Sequence[_Target]) -> str | None:
    """The id a note's flag had before notes had ids (`_legacy_note_ref`), for the resolutions stored then"""
    if flag.field != FIELD_NOTES or not flag.ref:
        return None
    target = next((target for target in targets if target.field == FIELD_NOTES and target.ref == flag.ref), None)
    if target is None or target.legacy_ref is None:
        return None
    suffix = flag.id.removeprefix(flag_id(flag.kind, FIELD_NOTES, flag.ref))  # an OCR check's, if any
    return flag_id(flag.kind, FIELD_NOTES, target.legacy_ref) + suffix


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
    linked: Mapping[UUID, Collection[str]] | None = None,
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

    `linked` holds every name of the group's foods and units the draft links, by id (`IngestMatcher.linked_names`):
    a line still as parsed whose food or unit none of them is on gets `linked_fuzzy`. Without it, `linked_fuzzy`
    flags are kept from `previous` while their lines are as extracted.
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
            _ingredient_flags(flags, target, units=unit_names, english=english, linked=linked)
        if target.is_step:
            _temperature_flags(flags, target)

    if previous is not None:
        # a lost unit judged by the group's units at extraction, or a link judged by the linked names when they aren't
        # at hand: kept while its line is as the parser read it
        kept_kinds = (
            {CardFlagKind.unit_unclear}
            if linked is not None
            else {CardFlagKind.unit_unclear, CardFlagKind.linked_fuzzy}
        )
        unedited = {target.ref for target in targets if target.ingredient and is_unedited(target.ingredient)}
        for flag in previous:
            if flag.kind in kept_kinds and flag.id not in flags.flags and flag.ref in unedited:
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
        notes_by_digest = {
            target.legacy_ref.rpartition("#")[2]: target.ref
            for target in reversed(targets)  # the first note saying it, when two say the same
            if target.field == FIELD_NOTES and target.legacy_ref and target.ref
        }
        before = {_reading_key(flag, notes_by_digest): flag for flag in previous if _is_reading_flag(flag)}
        reading_flags = [flag for flag in reading.flags.values() if _reading_key(flag, {}) in before]
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

    def resolution(flag: CardFlag) -> FlagResolution | None:
        if (stored := resolutions.get(flag.id)) is None and (legacy := _legacy_note_ids(flag, targets)):
            stored = resolutions.get(legacy)  # stored before notes had ids, for the note still there saying that
        return stored

    return [_resolved(flag, resolution(flag)) for flag in _in_reading_order(flags.flags.values(), targets)]


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
