"""
The flags that say what to check on a card (docs/ai/PHASE2.md §4.6). `compute_flags` is pure, and the server runs it
after extraction and on every save, so the flags always describe the current draft.

**Ids and fields.** A flag is keyed to a field plus the ingredient's `reference_id` or the step's `id` (never an
index, F4), with the stable id `"<kind>:<field>:<ref>"` (`ref` empty for single fields and the card). Fields are the
draft's JSON names: `name`, `description`, `recipeYield`, `recipeServings`, `prepTime`, `performTime`, `totalTime`,
`attribution`, `ingredients`, `steps`, `notes` (notes have no id, so their `ref` is the note's position), and `card`
for the card-level flags.

**Three kinds of flags.**
- *Content flags* follow the draft as it is now, edits included: markers, `missing_name`, `implausible_*`,
  `empty_section`, `new_food`, `new_unit` and the card-level `read_by_ocr`, `cross_read_failed`, `not_parsed`.
- *Parse flags* (`check_parse`, `unit_unclear`, `shorthand_read`) describe the parser's reading of a line, so they
  drop off once the line is edited: each ingredient keeps an `extracted_hash` of its parsed fields.
- *Reading flags* compare the draft with what was read: `unsure`, `not_on_card`, `marker_dropped`, and the cross-read's
  `read_disagreement` and `blank`. Typing a number into a blank must not raise them, so on a save (`previous` given)
  a reading flag is kept only where it was raised before and still holds; only an extraction raises new ones.

**Alternatives.** `unsure` alternatives replace `params.text` (the uncertain words) in the line; `implausible_amount`'s
replace `params.value`; `read_disagreement`'s single alternative is the second reading's whole line (`params.text`).
"""

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction

from rapidfuzz import fuzz

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
)

from ..flag_rules import KEEPABLE_KINDS, REVIEW_CONFIDENCE
from ..shorthand import QTY, SHORTHAND, UNITS
from .cardtext import (
    BLANK,
    describe_token,
    find_numbers,
    find_temperatures,
    format_number,
    markers_in,
    number_set,
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

_FRACTION_TYPO = re.compile(r"(?<![\d/.,])(?P<whole>[1-9])(?P<numerator>[1-9])/(?P<denominator>[2348])(?![\d/])")
"""`11/2` for "1 1/2": a whole number run into a proper fraction"""
_LEADING_QUANTITY = re.compile(rf"^\s*[-•*]?\s*{QTY}(?:\s*(?:-|to)\s*{QTY})?\s*(?P<token>[^\W\d_]+)(?P<dot>\.)?")
_NOT_UNIT_WORDS = frozenset({"or", "and", "to", "of", "x", "lg", "lge", "sm", "med", "md"})
"""Short words after a quantity that aren't a lost unit: joins ("2 or 3 eggs") and sizes ("1 lg onion")"""

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
        targets.append(_Target(FIELD_NOTES, str(index), note.text, [text for text in texts if text]))

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
    ) -> None:
        id = flag_id(kind, field, ref)
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
    if draft.ingredients and not is_english(extraction.language):
        flags.add(CardFlagKind.not_parsed, FIELD_CARD, source=CardFlagSource.parser, params={})


def _marker_flags(flags: _Flags, target: _Target) -> None:
    markers = {marker for text in target.marker_texts for marker in markers_in(text)}
    if "illegible" in markers:
        flags.add(CardFlagKind.illegible, target.field, target.ref, source=CardFlagSource.marker)
    if "blank" in markers:
        flags.add(CardFlagKind.blank, target.field, target.ref, source=CardFlagSource.marker)


def _unsure_targets(unsure: Sequence[ExtractionUnsure], targets: Sequence[_Target]) -> dict[int, ExtractionUnsure]:
    """The target each `unsure` entry matches best (by position in `targets`); the first entry wins a target"""
    matched: dict[int, ExtractionUnsure] = {}
    for entry in unsure:
        text = entry.text.strip()
        if not text:
            continue

        best: tuple[float, int] | None = None
        for index, target in enumerate(targets):
            if not target.text:
                continue
            if len(text) <= UNSURE_EXACT_MAX_LENGTH:
                pattern = rf"(?<![\w/]){re.escape(text)}(?![\w/])"
                score = 100.0 if re.search(pattern, target.text) else 0.0
            elif len(target.text) >= len(text):
                score = fuzz.partial_ratio(text.lower(), target.text.lower())
            else:
                # `partial_ratio` would find a short target inside the entry ("4" in "1/4 t. salt")
                score = fuzz.ratio(text.lower(), target.text.lower())
            if score >= UNSURE_MIN_SCORE and (best is None or score > best[0]):
                best = (score, index)

        if best is not None:
            matched.setdefault(best[1], entry)
    return matched


def _not_on_card(text: str, on_card: set[Fraction]) -> str | None:
    """The first number in `text` that isn't on the card, as written"""
    for number in find_numbers(text):
        values = [number.value] + ([number.end] if number.end is not None else [])
        if any(value not in on_card for value in values):
            return number.text
    return None


def _fraction_typo(ingredient: CardDraftIngredient, line: str) -> tuple[str, str] | None:
    """A `11/2`-style fraction in the line the parser read as such, and what it likely meant ("1 1/2")"""
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
    return match.group(0), f"{whole} {numerator}/{denominator}"


def _ingredient_flags(flags: _Flags, target: _Target) -> None:
    ingredient = target.ingredient
    assert ingredient is not None
    field, ref = target.field, target.ref
    parsed = ingredient.parse_confidence is not None
    unedited = is_unedited(ingredient)

    # the parser's reading, while the line is as it read it
    if parsed and unedited:
        if ingredient.parse_confidence is not None and ingredient.parse_confidence < REVIEW_CONFIDENCE:
            flags.add(
                CardFlagKind.check_parse,
                field,
                ref,
                source=CardFlagSource.parser,
                params={"confidence": round(ingredient.parse_confidence * 100)},
            )
        if ingredient.quantity and not ingredient.unit and (lead := _LEADING_QUANTITY.match(ingredient.original_text)):
            token = lead.group("token")
            food = (ingredient.food.name if ingredient.food else "").strip().lower()
            if len(token) <= 3 and token.lower() not in _NOT_UNIT_WORDS and food != token.lower():
                flags.add(
                    CardFlagKind.unit_unclear,
                    field,
                    ref,
                    source=CardFlagSource.parser,
                    params={"token": token + (lead.group("dot") or "")},
                )

    # what the line says now
    if ingredient.quantity and ingredient.unit and ingredient.quantity > MAX_PLAIN_AMOUNT:
        if ingredient.unit.name.strip().lower().rstrip(".") in SPOON_AND_CUP_UNITS:
            value = f"{format_number(Fraction(ingredient.quantity).limit_denominator(16))} {ingredient.unit.name}"
            flags.add(
                CardFlagKind.implausible_amount, field, ref, source=CardFlagSource.validator, params={"value": value}
            )
    if typo := _fraction_typo(ingredient, target.text):
        written, suggestion = typo
        flags.add(
            CardFlagKind.implausible_amount,
            field,
            ref,
            source=CardFlagSource.validator,
            params={"value": written, "suggestion": suggestion},
            alternatives=[suggestion],
        )

    shorthand = SHORTHAND.match(ingredient.original_text) if parsed and unedited else None
    if shorthand and shorthand.group("unit").lower() != UNITS[shorthand.group("unit")]:
        # "T." became "tbsp"; "tsp" or "Tbsp" was already the unit's name
        unit = shorthand.group("unit")
        written = ingredient.original_text[shorthand.start("unit") : shorthand.end()].strip()
        flags.add(
            CardFlagKind.shorthand_read,
            field,
            ref,
            source=CardFlagSource.parser,
            params={"from": written, "to": UNITS[unit]},
        )
    if ingredient.unit and ingredient.unit.name.strip() and ingredient.unit.id is None:
        flags.add(
            CardFlagKind.new_unit, field, ref, source=CardFlagSource.parser, params={"name": ingredient.unit.name}
        )
    if ingredient.food and ingredient.food.name.strip() and ingredient.food.id is None:
        flags.add(
            CardFlagKind.new_food, field, ref, source=CardFlagSource.parser, params={"name": ingredient.food.name}
        )


def _temperature_flags(flags: _Flags, target: _Target) -> None:
    for temperature in find_temperatures(target.text):
        match temperature.unit:
            case "F":
                low, high = FAHRENHEIT_RANGE
            case "C":
                low, high = CELSIUS_RANGE
            case _:
                low, high = CELSIUS_RANGE[0], FAHRENHEIT_RANGE[1]
        if not low <= temperature.value <= high:
            flags.add(
                CardFlagKind.implausible_temperature,
                target.field,
                target.ref,
                source=CardFlagSource.validator,
                params={"value": temperature.text},
            )
            return


def _cross_read_flags(flags: _Flags, target: _Target, lines: Sequence[str]) -> None:
    if not target.text.strip() or target.field not in (FIELD_INGREDIENTS, FIELD_STEPS):
        return

    if target.is_step:
        window = align_step(target.text, lines)
        if window is None:
            return
        aligned = " ".join(lines[window[0] : window[1]])
    else:
        index = align_ingredient(target.text, lines)
        if index is None:
            return
        aligned = lines[index]

    disagreement = compare(target.text, aligned)
    if disagreement is None:
        return

    missing = disagreement.missing
    numbers = [token for token in missing if token[0] in ("number", "range")]
    if disagreement.window_blank and numbers and BLANK not in target.text:
        # the second reading saw a gap where this one has a number: possibly invented
        flags.add(
            CardFlagKind.blank,
            target.field,
            target.ref,
            source=CardFlagSource.cross_read,
            params={"value": describe_token(numbers[0])},
        )
        missing = [token for token in missing if token not in numbers]

    if missing:
        flags.add(
            CardFlagKind.read_disagreement,
            target.field,
            target.ref,
            source=CardFlagSource.cross_read,
            params={"text": aligned, "value": describe_token(missing[0])},
            alternatives=[aligned],
        )


def _reading_flags(
    flags: _Flags,
    draft: CardDraft,
    targets: Sequence[_Target],
    extraction: ExtractionMeta | None,
    transcription: str | None,
) -> None:
    """`unsure`, `not_on_card`, `marker_dropped` and the cross-read's flags, against what was read"""
    unsure = _unsure_targets(extraction.unsure, targets) if extraction else {}
    on_card = number_set(transcription) if transcription is not None else None
    lines = extraction.cross_read_lines if extraction else None

    for index, target in enumerate(targets):
        if entry := unsure.get(index):
            flags.add(
                CardFlagKind.unsure,
                target.field,
                target.ref,
                source=CardFlagSource.model,
                params={"text": entry.text, "reason": entry.reason},
                alternatives=entry.alternatives,
            )
        if on_card is not None and target.field in (
            FIELD_INGREDIENTS,
            FIELD_STEPS,
            FIELD_YIELD,
            FIELD_SERVINGS,
            *TIME_FIELDS,
        ):
            if target.ingredient is not None and not is_unedited(target.ingredient):
                pass  # a line the reviewer edited says what they typed
            elif value := _not_on_card(target.text, on_card):
                flags.add(
                    CardFlagKind.not_on_card,
                    target.field,
                    target.ref,
                    source=CardFlagSource.validator,
                    params={"value": value},
                )
        if lines:
            _cross_read_flags(flags, target, lines)

    if transcription is not None:
        read = len(markers_in(transcription))
        kept = sum(len(markers_in(text)) for target in targets for text in target.marker_texts)
        if read > kept:
            flags.add(CardFlagKind.marker_dropped, FIELD_CARD, source=CardFlagSource.validator)


def _is_reading_flag(flag: CardFlag) -> bool:
    return flag.kind in READING_KINDS or (flag.kind == CardFlagKind.blank and flag.source == CardFlagSource.cross_read)


def _still_holds(flag: CardFlag, targets: Sequence[_Target]) -> bool:
    """Whether a reading flag raised before still describes the draft, judged without the transcription"""
    if flag.field == FIELD_CARD:
        return True
    for target in targets:
        if target.field == flag.field and target.ref == (flag.ref or None):
            value = str(flag.params.get("value", ""))
            if flag.kind == CardFlagKind.not_on_card and value:
                return value in target.text
            return True
    return False


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
) -> list[CardFlag]:
    """
    Every flag the draft raises, in reading order (the card's own flags first), keyed `"<kind>:<field>:<ref>"`, with
    the stored resolutions (by flag id) applied.

    `transcription` is what the card was read as (the job's `transcription`): `not_on_card` and `marker_dropped`
    compare with it. `previous` is the job's stored flags, on a save: reading flags are then kept only where they
    were raised before and still hold, so a reviewer's own edits (a number typed into a blank) never raise them.
    Without it, as after an extraction, every reading flag the draft and `extraction` raise is returned.
    """
    targets = _targets(draft)
    flags = _Flags()

    _card_flags(flags, draft, extraction)
    if not draft.name.strip():
        flags.add(CardFlagKind.missing_name, FIELD_NAME, source=CardFlagSource.validator)

    for target in targets:
        _marker_flags(flags, target)
        if target.ingredient is not None:
            _ingredient_flags(flags, target)
        if target.is_step:
            _temperature_flags(flags, target)

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
    _reading_flags(reading, draft, targets, extraction, transcription)
    if previous is None:
        reading_flags = list(reading.flags.values())
    else:
        before = {(flag.id, flag.source): flag for flag in previous if _is_reading_flag(flag)}
        reading_flags = [flag for flag in reading.flags.values() if (flag.id, flag.source) in before]
        if transcription is None:
            # the transcription-based flags can't be recomputed: carry them over while their line still says so
            for key, flag in before.items():
                if flag.kind in (CardFlagKind.not_on_card, CardFlagKind.marker_dropped) and key[0] not in reading.flags:
                    if _still_holds(flag, targets):
                        reading_flags.append(flag.model_copy(update={"resolution": None}, deep=True))

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
