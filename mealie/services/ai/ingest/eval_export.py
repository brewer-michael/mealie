"""
Recipe card eval cases (docs/ai/PHASE2.md §11.6): the fixture format the eval harness reads
(`mealie/scripts/eval_recipe_cards.py`), how its ingredient lines are read, and saving a reviewed card as a case in the
group's private eval set, `DATA_DIR/groups/<group_id>/eval-cards/<slug>.json` with `<slug>-<n>.jpg`.

**The format** is backward compatible: a fixture without `schema_version` is version 1, and every version 1 key means
what it did. Every model forbids unknown keys, so a typo such as `"attributon"` is an error rather than a silently
dropped value.

**Saving a case** (`build_eval_case`, then `save_eval_case` inside the ingest write lock):
- the images are the job's normalized `page.jpg`s **turned back by their recorded rotation**, so the eval still
  exercises orientation, and they carry no metadata (an unturned page is copied as it is: intake already stripped it);
- the expected values come from the reviewed draft. A field whose text held a `[blank]` when the card was read keeps
  it, even when the reviewer has since filled it in, and is listed in `blanks`: the transcription still has the
  marker, and `restore_blanks` puts it back in the reviewed wording;
- `origin` records the job, the provider and model that drafted it and the Mealie commit, so the report can mark runs
  scored against a provider's own drafts.
"""

import io
import json
import math
import os
import re
import subprocess
import tempfile
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from fractions import Fraction
from functools import cache
from pathlib import Path
from typing import Any, Self
from uuid import UUID

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from rapidfuzz import fuzz

from mealie.core.config import get_app_settings
from mealie.core.root_logger import get_logger
from mealie.db.models.recipe_ingest import RecipeIngestionJob
from mealie.schema.recipe_ingest import (
    CardDraft,
    CardDraftIngredient,
    EvalCaseOut,
    EvalCaseSummary,
    ExtractionMeta,
    IngestStatus,
    PageMeta,
)
from mealie.schema.recipe_ingest.ingest_requests import EVAL_CASE_SLUG_PATTERN

from . import storage
from .images import PAGE_FILE
from .shorthand import UNITS as SHORTHAND_UNITS

logger = get_logger(__name__)

# ==========================================
# The fixture format

FIXTURE_SCHEMA_VERSION = 2
"""The newest fixture version; version 1 files (no `schema_version`) still load"""

FIXTURE_TAGS = ("handwritten", "printed", "sideways", "two-sided", "faded", "blank")
"""What a card's `tags` may say, for the eval's per-tag rows"""

TIME_FIELDS = ("total_time", "prep_time", "cook_time", "perform_time")
YIELD_FIELDS = ("recipe_yield", "recipe_servings", "recipe_yield_quantity")

INVENTION_CHECKS: dict[str, tuple[str, ...]] = {
    # a card's "cook time" can land in any of the recipe's times, depending on the model
    "cook time": TIME_FIELDS,
    "time": TIME_FIELDS,
    "prep time": ("prep_time",),
    "total time": ("total_time",),
    "yield": YIELD_FIELDS,
    "servings": YIELD_FIELDS,
    "description": ("description",),
    "notes": ("notes",),
    "nutrition": ("nutrition",),
    "attribution": ("attribution",),
}
"""What a card's `must_not_invent` entries may name, and the recipe fields that must then stay empty"""

LIST_BLANK_FIELDS = ("ingredients", "steps", "notes")
"""Fields with several items: a blank in one of them names its item by `text`"""
BLANK_FIELDS = ("name", "description", "attribution", "prep_time", "perform_time", "total_time", "recipe_yield")
"""Fields with one value that may hold a blank"""

BLANK_MARKER = "[blank]"
ILLEGIBLE_MARKER = "[illegible]"
MARKER_RE = re.compile(r"\[(?:blank|illegible)\]", re.IGNORECASE)
"""The two markers the card prompts use: a gap the writer left on purpose, and writing that can't be read"""
BLANK_RE = re.compile(r"\[blank\]", re.IGNORECASE)


def normalize_check(check: str) -> str:
    return " ".join(check.lower().replace("_", " ").replace("-", " ").split())


class _FixtureModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExpectedIngredient(_FixtureModel):
    """An ingredient line with what it should be parsed and linked into"""

    text: str
    """The line verbatim from the card, as a plain-string line would be"""
    quantity: float | None = None
    unit: str | None = None
    """The unit's name or abbreviation ("tbsp"); checked against the linked unit when given"""
    food: str | None = None
    """The food's name; checked against the linked food when given"""
    note: str | None = None


class ExpectedTimes(_FixtureModel):
    """Times written on the card, as written ("10 min")"""

    prep_time: str | None = None
    perform_time: str | None = None
    """The cook time"""
    total_time: str | None = None


class ExpectedBlank(_FixtureModel):
    """A gap the writer left on purpose, which a correct extraction keeps as `[blank]`"""

    field: str
    """`ingredients`, `steps` or `notes` (with `text`), or a single field such as `perform_time`"""
    text: str | None = None
    """For a list field, the item holding the blank, as on the card, with `[blank]` where the gap is"""

    @model_validator(mode="after")
    def _check_field(self) -> Self:
        if self.field in LIST_BLANK_FIELDS:
            if not self.text or not BLANK_RE.search(self.text):
                raise ValueError(f"a blank in {self.field} needs the item's text, with [blank] where the gap is")
        elif self.field not in BLANK_FIELDS:
            raise ValueError(
                f"unknown blank field '{self.field}', expected any of {[*LIST_BLANK_FIELDS, *BLANK_FIELDS]}"
            )
        return self


class ExpectedRecipe(_FixtureModel):
    name: str
    description_contains: list[str] = Field(default_factory=list)
    ingredients: list[str | ExpectedIngredient]
    """Ingredient lines, verbatim from the card; a structured line also says how it should be parsed and linked"""
    instructions: list[str] = Field(default_factory=list)
    must_not_invent: list[str] = Field(default_factory=list)
    """Fields the card leaves blank, which a correct extraction leaves blank too. See `INVENTION_CHECKS`."""
    attribution: str | None = None
    """Who the recipe is from, as written ("From Grandma Jo")"""
    blanks: list[ExpectedBlank] = Field(default_factory=list)
    recipe_yield: str | None = None
    times: ExpectedTimes | None = None

    @field_validator("must_not_invent")
    @classmethod
    def validate_must_not_invent(cls, value: list[str]) -> list[str]:
        checks = [normalize_check(check) for check in value]
        if unknown := [check for check in checks if check not in INVENTION_CHECKS]:
            raise ValueError(f"unknown must_not_invent check(s) {unknown}, expected any of {list(INVENTION_CHECKS)}")
        return checks

    @property
    def ingredient_lines(self) -> list[str]:
        """Every ingredient line's text"""
        return [line if isinstance(line, str) else line.text for line in self.ingredients]

    @property
    def structured_ingredients(self) -> list[ExpectedIngredient | None]:
        """Each ingredient line's parsing and linking expectations; None for a plain-string line"""
        return [None if isinstance(line, str) else line for line in self.ingredients]


class FixtureDraftedBy(_FixtureModel):
    provider: str | None = None
    model: str | None = None


class FixtureOrigin(_FixtureModel):
    """Where a case saved from the review page came from"""

    job_id: str | None = None
    drafted_by: FixtureDraftedBy | None = None
    """The provider and model whose draft the expected values were reviewed from"""
    exported_at: datetime | None = None
    mealie_commit: str | None = None


class CardFixture(_FixtureModel):
    schema_version: int = 1
    source: str | list[str]
    """Image file name(s), relative to the JSON file. Several images (front and back) are read as one card."""
    notes: str = ""
    verified_by_owner: bool = False
    tags: list[str] = Field(default_factory=list)
    local_only: bool = False
    """The eval only sends this card to providers that run on your network"""
    origin: FixtureOrigin | None = None
    expected: ExpectedRecipe

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: int) -> int:
        if not 1 <= value <= FIXTURE_SCHEMA_VERSION:
            raise ValueError(f"schema_version {value} isn't one this Mealie reads (1 to {FIXTURE_SCHEMA_VERSION})")
        return value

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str]) -> list[str]:
        if unknown := [tag for tag in value if tag not in FIXTURE_TAGS]:
            raise ValueError(f"unknown tag(s) {unknown}, expected any of {list(FIXTURE_TAGS)}")
        return list(dict.fromkeys(value))

    @property
    def sources(self) -> list[str]:
        return [self.source] if isinstance(self.source, str) else list(self.source)


# ==========================================
# Reading ingredient lines

QUANTITY_TOLERANCE = 0.02
"""Relative difference at which two quantities still agree, so that "0.33" reads as "1/3"."""

UNIT_ALIASES: dict[str, tuple[str, ...]] = {
    "tbsp": ("tablespoon", "tablespoons", "tbsps", "tbs", "tbl", "tbls", "tblsp", "tb"),
    "tsp": ("teaspoon", "teaspoons", "tsps", "ts", "tspn"),
    "cup": ("cups", "c"),
    "oz": ("ounce", "ounces", "ozs"),
    "fl oz": ("fluid ounce", "fluid ounces", "floz"),
    "lb": ("pound", "pounds", "lbs"),
    "g": ("gram", "grams", "gr"),
    "kg": ("kilogram", "kilograms", "kilo", "kilos"),
    "ml": ("milliliter", "milliliters", "millilitre", "millilitres"),
    "l": ("liter", "liters", "litre", "litres"),
    "pt": ("pint", "pints"),
    "qt": ("quart", "quarts"),
    "gal": ("gallon", "gallons"),
    "pinch": ("pinches",),
    "dash": ("dashes",),
    "stick": ("sticks",),
    "can": ("cans",),
    "pkg": ("package", "packages", "pkgs"),
    "clove": ("cloves",),
    "slice": ("slices",),
}
"""Units an ingredient line may start with, after its quantity, and their other spellings, matched ignoring case"""

_UNITS = {alias: unit for unit, aliases in UNIT_ALIASES.items() for alias in (unit, *aliases)}

CASE_SENSITIVE_UNITS: dict[str, str] = {token: _UNITS[name] for token, name in SHORTHAND_UNITS.items()}
"""
Card shorthand, from the pipeline's own table (`shorthand.UNITS`): on a handwritten card a capital "T" is a
tablespoon and a lowercase "t" a teaspoon, three times smaller
"""

_NUMBER = r"\d+\s+\d+/\d+|\d+-\d+/\d+|\d+/\d+|\d*\.\d+|\d+"
_QUANTITY_RE = re.compile(rf"\s*(?P<low>{_NUMBER})(?:\s*(?:-|–|—|to|or)\s*(?P<high>{_NUMBER}))?", re.IGNORECASE)
"""A leading quantity: "2", "1.5", "1/2", "1 1/2" or "1-1/2", or a range of two such as "1-2" or "1 to 2"."""
_UNIT_RE = re.compile(
    # a size or a qualifier may come between the quantity and the unit: "1 (15 oz) can", "1 heaping T."
    r"\s*(?P<size>(?:\([^)]*\)\s*)?(?:(?:heaping|heaped|scant|level|rounded|generous)\s+)?)"
    r"(?P<unit>fl\.?\s*oz|fluid\s+ounces?|[^\W\d_]+)\.?",
    re.IGNORECASE,
)


def strip_markers(text: str) -> str:
    """The text without `[blank]` and `[illegible]`, whose presence is scored on its own"""
    return " ".join(MARKER_RE.sub(" ", text).split())


def ascii_fractions(text: str) -> str:
    """Writes unicode fractions in ASCII, so that "1½" becomes "1 1/2", keeping the text's case."""

    chars: list[str] = []
    for char in text:
        if unicodedata.name(char, "").startswith("VULGAR FRACTION"):
            # NFKC turns "½" into "1⁄2"; pad it so that "1½" doesn't become "11/2"
            chars.append(f" {unicodedata.normalize('NFKC', char)} ")
        else:
            chars.append(char)

    # NFKC writes fractions with U+2044 FRACTION SLASH rather than "/"
    return unicodedata.normalize("NFKC", "".join(chars)).replace("⁄", "/")


def normalize_text(text: str | None) -> str:
    """
    Normalizes text for fuzzy comparison: the two markers removed, lowercased, unicode fractions spelled out in ASCII
    ("1½" becomes "1 1/2"), punctuation dropped except in numbers, and whitespace collapsed.
    """

    if not text:
        return ""

    text = ascii_fractions(strip_markers(text)).lower()
    text = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", text)  # periods, but not decimal points
    text = re.sub(r"(?<!\d)/|/(?!\d)", " ", text)  # slashes, but not fractions
    text = re.sub(r"[^\w\s./]", " ", text)
    return " ".join(text.split())


def parse_number(text: str) -> float | None:
    """Reads "2", "1.5", "1/2", "1 1/2" or "1-1/2" as a number. None for a fraction over zero."""

    total = 0.0
    for part in re.split(r"[\s-]+", text.strip()):
        numerator, slash, denominator = part.partition("/")
        if not slash:
            total += float(part)
        elif int(denominator):
            total += int(numerator) / int(denominator)
        else:
            return None

    return total


def canonical_unit(token: str | None) -> str | None:
    """The unit a word stands for ("T." and "Tbsp" both mean "tbsp"), or None if it isn't a unit."""

    if not token:
        return None
    token = " ".join(token.replace(".", " ").split())
    return CASE_SENSITIVE_UNITS.get(token) or _UNITS.get(token.lower())


@dataclass(frozen=True)
class IngredientAmount:
    quantity: tuple[float, ...] | None
    """The line's leading quantity, or both ends of a range such as "1-2". None if it has none."""
    unit: str | None
    """The unit after the quantity, as named in `UNIT_ALIASES`. None if there is none, or it isn't known."""
    text: str
    """The normalized line, with its quantity and unit always written the same way, for comparing lines"""


def parse_amount(line: str) -> IngredientAmount:
    """
    Reads the quantity and unit an ingredient line starts with. Case matters for the unit: on a
    handwritten card "1 T." is a tablespoon and "1 t." a teaspoon. A unit is only read after a
    quantity, and only from a known spelling (see `UNIT_ALIASES`). Markers are ignored.
    """

    text = ascii_fractions(strip_markers(line))
    quantity_match = _QUANTITY_RE.match(text)
    if not quantity_match:
        return IngredientAmount(quantity=None, unit=None, text=normalize_text(text))

    numbers = [parse_number(number) for number in quantity_match.group("low", "high") if number]
    quantity = tuple(number for number in numbers if number is not None)
    if len(quantity) != len(numbers):
        # a fraction over zero isn't a quantity
        return IngredientAmount(quantity=None, unit=None, text=normalize_text(text))

    rendered = "-".join(f"{number:g}" for number in quantity)
    unit_match = _UNIT_RE.match(text, quantity_match.end())
    if unit_match and (unit := canonical_unit(unit_match["unit"])):
        rest = f"{unit_match['size']} {text[unit_match.end() :]}"
        return IngredientAmount(quantity=quantity, unit=unit, text=normalize_text(f"{rendered} {unit} {rest}"))

    rest = text[quantity_match.end() :]
    return IngredientAmount(quantity=quantity, unit=None, text=normalize_text(f"{rendered} {rest}"))


def quantities_agree(expected: tuple[float, ...] | None, actual: tuple[float, ...] | None) -> bool:
    if expected is None or actual is None:
        return expected == actual

    return len(expected) == len(actual) and all(
        math.isclose(a, b, rel_tol=QUANTITY_TOLERANCE) for a, b in zip(expected, actual, strict=True)
    )


def format_quantity(quantity: float) -> str:
    """A quantity as a card would write it: "1/4", "1 1/2", "2", or a decimal when no small fraction fits"""
    fraction = Fraction(quantity).limit_denominator(16)
    if abs(float(fraction) - quantity) > QUANTITY_TOLERANCE * max(abs(quantity), 1e-9):
        return f"{quantity:g}"
    whole, rest = divmod(fraction, 1)
    if not rest:
        return str(whole)
    return f"{whole} {rest}" if whole else str(rest)


# ==========================================
# Saving a reviewed card as an eval case

EXPORTABLE_STATUSES = frozenset({IngestStatus.ready.value, IngestStatus.committed.value})
"""A draft exists (until a committed card's files are purged)"""

EVAL_CASE_IMAGE_QUALITY = 95
"""JPEG quality of a page turned back by its rotation; an unturned page is copied as it is"""

BLANK_ALIGN_THRESHOLD = 70
"""How much of a transcription line holding a `[blank]` must match a draft item (partial ratio) to be its source"""

_SLUG_RE = re.compile(EVAL_CASE_SLUG_PATTERN)
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".avif", ".tif", ".tiff")
_UNDO_ROTATION = {
    # undoing a clockwise turn is turning counter-clockwise, which is what Pillow's ROTATE_* do
    90: Image.Transpose.ROTATE_90,
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_270,
}


class EvalCaseError(Exception):
    """A card that can't be saved as an eval case, with the error code the route answers with"""

    code = "eval_case_error"


class EvalCaseUnavailable(EvalCaseError):
    """The job isn't `ready` or `committed`, or its draft or files are gone"""

    code = "not_exportable"


class EvalCaseFilesMissing(EvalCaseError):
    code = "files_missing"


class EvalCaseExists(EvalCaseError):
    """An eval case with this slug, or a file it would write, already exists"""

    code = "eval_case_exists"


@dataclass
class EvalCase:
    """A card ready to be written as an eval case"""

    slug: str
    fixture: CardFixture
    images: list[tuple[str, bytes]]
    """File name and JPEG bytes of each page, front first, in the order `fixture.source` lists them"""


@cache
def mealie_commit() -> str | None:
    """The Mealie commit this server runs: the image's build commit, else the checkout's `HEAD`, else None"""
    commit = get_app_settings().GIT_COMMIT_HASH
    if commit and commit != "unknown":
        return commit
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None
    head = result.stdout.strip()
    return head if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", head) else None


def _token_key(token: str) -> str:
    """How a word is compared when aligning a card line with its reviewed wording; a marker equals nothing"""
    if MARKER_RE.fullmatch(token.strip(".,;:!?()")):
        return "\x00marker"
    return normalize_text(token)


def _is_number(token: str) -> bool:
    return bool(re.fullmatch(r"[\d½⅓⅔¼¾⅛⅜⅝⅞]+(?:[./-]\d+)*", ascii_fractions(token).strip(".,;:!?()").strip()))


def restore_blanks(card_text: str, reviewed: str) -> str:
    """
    The reviewed wording of an item with each `[blank]` the card has put back where it was: the reviewer's words are
    kept, and whatever filled a blank (a number, or a few words) is replaced by the marker. `card_text` is the item,
    or a physical line of it, as read from the card. Returns `reviewed` unchanged when it still holds every blank.
    """
    blanks = len(BLANK_RE.findall(card_text))
    if not blanks or len(BLANK_RE.findall(reviewed)) >= blanks:
        return reviewed

    card_tokens = card_text.split()
    reviewed_tokens = reviewed.split()
    matcher = SequenceMatcher(
        a=[_token_key(t) for t in card_tokens], b=[_token_key(t) for t in reviewed_tokens], autojunk=False
    )
    out: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        card_part = card_tokens[i1:i2]
        reviewed_part = reviewed_tokens[j1:j2]
        markers = [token for token in card_part if BLANK_RE.search(token)]
        if tag == "equal" or not markers:
            out.extend(reviewed_part)
            continue

        if any(_is_number(token) for token in reviewed_part):
            # the reviewer filled the blank with a number: the marker takes its place, the other words stay
            placed = False
            for token in reviewed_part:
                if _is_number(token):
                    if not placed:
                        out.extend(markers)
                        placed = True
                else:
                    out.append(token)
        elif len(reviewed_part) <= 3:
            out.extend(markers)  # a few words where the gap was
        else:
            out.extend([*markers, *reviewed_part])
    return " ".join(out)


def _marker_lines(transcription: str | None) -> list[str]:
    return [line.strip() for line in (transcription or "").splitlines() if BLANK_RE.search(line)]


def _line_score(card_line: str, item: str) -> float:
    """
    How well a transcription line (a whole item, or one physical line of a longer step) matches a reviewed item: its
    partial ratio, or 0 when the item is much shorter than the line or shares too few of its words, so a short item
    is never taken for part of an unrelated line
    """
    line_text, item_text = normalize_text(card_line), normalize_text(item)
    if not line_text or not item_text or len(item_text) < 0.6 * len(line_text):
        return 0.0
    line_words = set(line_text.split())
    if len(line_words & set(item_text.split())) < 0.5 * len(line_words):
        return 0.0
    return fuzz.partial_ratio(line_text, item_text)


def _with_card_blanks(text: str, card_lines: Sequence[str]) -> str:
    """`text` (a reviewed item) with the blanks of the transcription line it came from put back"""
    if not text.strip() or not card_lines:
        return text
    score, best = max((_line_score(line, text), line) for line in card_lines)
    if score < BLANK_ALIGN_THRESHOLD:
        return text
    return restore_blanks(best, text)


def _ingredient_text(ingredient: CardDraftIngredient) -> str:
    """
    The line as the card has it: its original text while that agrees with the reviewed quantity and unit, else the
    reviewed fields written out (the reviewer corrected a misread line, so the text read from the card is wrong)
    """
    unit = ingredient.unit.name if ingredient.unit and ingredient.unit.name else None
    original = ingredient.original_text.strip()
    if original:
        amount = parse_amount(original)
        # the parser keeps the low end of a range ("1-2 T.")
        read_quantity = amount.quantity[:1] if amount.quantity else None
        reviewed_quantity = (ingredient.quantity,) if ingredient.quantity else None
        if quantities_agree(read_quantity, reviewed_quantity) and amount.unit == canonical_unit(unit):
            return original

    parts = [
        format_quantity(ingredient.quantity) if ingredient.quantity else "",
        unit or "",
        ingredient.food.name if ingredient.food and ingredient.food.name else "",
    ]
    text = " ".join(part for part in parts if part)
    note = ingredient.note.strip()
    if note:
        text = f"{text}, {note}" if text else note
    return text or ingredient.display.strip() or original


def _expected_ingredient(ingredient: CardDraftIngredient, text: str) -> ExpectedIngredient:
    return ExpectedIngredient(
        text=text,
        # a quantity the reviewer typed into a blank isn't on the card
        quantity=None if BLANK_RE.search(text) else ingredient.quantity or None,
        unit=ingredient.unit.name if ingredient.unit and ingredient.unit.name else None,
        food=ingredient.food.name if ingredient.food and ingredient.food.name else None,
        note=ingredient.note.strip() or None,
    )


def expected_from_draft(draft: CardDraft, transcription: str | None) -> ExpectedRecipe:
    """
    The expected values of an eval case from a reviewed draft. Text that held a `[blank]` when the card was read keeps
    it (from `transcription`, the card as read) and is listed in `blanks`.
    """
    card_lines = _marker_lines(transcription)
    blanks: list[ExpectedBlank] = []

    ingredients: list[str | ExpectedIngredient] = []
    for ingredient in draft.ingredients:
        text = _with_card_blanks(_ingredient_text(ingredient), card_lines)
        if not text.strip():
            continue
        ingredients.append(_expected_ingredient(ingredient, text))
        if BLANK_RE.search(text):
            blanks.append(ExpectedBlank(field="ingredients", text=text))

    instructions: list[str] = []
    for step in draft.steps:
        text = _with_card_blanks(step.text, card_lines)
        if not text.strip():
            continue
        instructions.append(text)
        if BLANK_RE.search(text):
            blanks.append(ExpectedBlank(field="steps", text=text))

    for note in draft.notes:
        if BLANK_RE.search(note.text):
            blanks.append(ExpectedBlank(field="notes", text=note.text))

    def single(field: str, value: str | None) -> str | None:
        if value and BLANK_RE.search(value):
            blanks.append(ExpectedBlank(field=field))
            # a blank time or yield has no value; text keeps the marker where it was
            return None if field in ("prep_time", "perform_time", "total_time", "recipe_yield") else value
        return value or None

    name = single("name", draft.name) or ""
    attribution = single("attribution", draft.attribution)
    recipe_yield = single("recipe_yield", draft.recipe_yield)
    times = ExpectedTimes(
        prep_time=single("prep_time", draft.prep_time),
        perform_time=single("perform_time", draft.perform_time),
        total_time=single("total_time", draft.total_time),
    )

    return ExpectedRecipe(
        name=name,
        ingredients=ingredients,
        instructions=instructions,
        attribution=attribution,
        blanks=blanks,
        recipe_yield=recipe_yield,
        times=times if any((times.prep_time, times.perform_time, times.total_time)) else None,
    )


def _page_image(group_id: UUID, job_id: UUID, meta: PageMeta) -> bytes:
    """The page's `page.jpg` turned back by its recorded rotation, without metadata. Raises `FileNotFoundError`."""
    path = storage.page_dir(group_id, job_id, meta.index) / PAGE_FILE
    data = path.read_bytes()
    rotation = meta.rotation % 360
    if rotation not in _UNDO_ROTATION:
        return data  # intake wrote it without EXIF, XMP or GPS

    with Image.open(io.BytesIO(data), formats=["JPEG"]) as image:
        image.load()
        icc = image.info.get("icc_profile")
        turned = image.convert("RGB").transpose(_UNDO_ROTATION[rotation])
    turned.info = {}
    buffer = io.BytesIO()
    params: dict[str, Any] = {"quality": EVAL_CASE_IMAGE_QUALITY, "optimize": True}
    if icc:
        params["icc_profile"] = icc
    turned.save(buffer, format="JPEG", **params)
    return buffer.getvalue()


def _tags(pages: Sequence[PageMeta], expected: ExpectedRecipe) -> list[str]:
    tags: list[str] = []
    if any(page.rotation % 180 == 90 for page in pages):
        tags.append("sideways")
    if len(pages) > 1:
        tags.append("two-sided")
    if expected.blanks:
        tags.append("blank")
    return tags


def build_eval_case(job: RecipeIngestionJob, slug: str, verified: bool, *, now: datetime | None = None) -> EvalCase:
    """
    A reviewed card as an eval case: its pages turned back by their recorded rotation (so orientation is still
    exercised) and free of metadata, and the fixture with the reviewed draft as the expected values, blanks kept,
    `origin` and `verified_by_owner`. Reads the job's files, so callers hold the ingest write lock.

    Raises `EvalCaseUnavailable` unless the job is `ready` or `committed` with its draft, and `EvalCaseFilesMissing`
    once its files are gone.
    """
    if not _SLUG_RE.match(slug):
        raise ValueError(f"Invalid eval case name '{slug}'")
    if job.status not in EXPORTABLE_STATUSES or not job.draft:
        raise EvalCaseUnavailable()

    draft = CardDraft.model_validate(job.draft)
    extraction = ExtractionMeta.model_validate(job.extraction) if job.extraction else None
    pages = sorted((PageMeta.model_validate(page) for page in job.pages or []), key=lambda page: page.index)
    if not pages:
        raise EvalCaseFilesMissing()

    try:
        images = [(f"{slug}-{n}.jpg", _page_image(job.group_id, job.id, page)) for n, page in enumerate(pages, 1)]
    except FileNotFoundError as e:
        raise EvalCaseFilesMissing() from e

    expected = expected_from_draft(draft, job.transcription)
    names = [name for name, _ in images]
    fixture = CardFixture(
        schema_version=FIXTURE_SCHEMA_VERSION,
        source=names[0] if len(names) == 1 else names,
        notes="Saved from the review page.",
        verified_by_owner=verified,
        tags=_tags(pages, expected),
        local_only=bool(job.local_only),
        origin=FixtureOrigin(
            job_id=str(job.id),
            drafted_by=FixtureDraftedBy(provider=extraction.provider, model=extraction.model) if extraction else None,
            exported_at=now or datetime.now(UTC),
            mealie_commit=mealie_commit(),
        ),
        expected=expected,
    )
    return EvalCase(slug=slug, fixture=fixture, images=images)


def fixture_json(fixture: CardFixture) -> bytes:
    """A fixture as written to disk: indented, without empty optional values"""
    data = fixture.model_dump(mode="json", exclude_none=True)
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode()


def _create_exclusive(path: Path, data: bytes) -> None:
    """
    Writes a new file atomically, never over an existing one: a temporary file in the same directory, hard-linked to
    `path` (which fails if it exists). Raises `FileExistsError`.
    """
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        try:
            os.link(temp_name, path)
        except FileExistsError:
            raise
        except OSError:
            # a filesystem without hard links: an exclusive create is still never an overwrite
            with open(path, "xb") as file:
                file.write(data)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def save_eval_case(group_id: UUID, case: EvalCase) -> EvalCaseOut:
    """
    Writes an eval case into the group's eval set: the images first, then the JSON (whose presence is the case).
    Nothing is ever overwritten: raises `EvalCaseExists`, having written nothing, when the case or one of its files
    exists. Callers hold the ingest write lock.
    """
    directory = storage.eval_cards_dir(group_id)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / f"{case.slug}.json"
    if json_path.exists():
        raise EvalCaseExists()

    written: list[Path] = []
    try:
        for name, data in case.images:
            path = directory / name
            _create_exclusive(path, data)
            written.append(path)
        _create_exclusive(json_path, fixture_json(case.fixture))
    except FileExistsError as e:
        for path in written:
            path.unlink(missing_ok=True)
        raise EvalCaseExists() from e
    except BaseException:
        for path in written:
            path.unlink(missing_ok=True)
        raise

    return EvalCaseOut(slug=case.slug, files=[json_path.name, *(name for name, _ in case.images)])


def _read_fixture(path: Path) -> CardFixture | None:
    try:
        return CardFixture.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as e:
        logger.warning(f"Eval case {path.name} can't be read: {type(e).__name__}")
        return None


def _case_jsons(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    paths = [path for path in directory.glob("*.json") if path.is_file() and _SLUG_RE.match(path.stem)]
    return sorted(paths, key=lambda path: path.stem)


def list_eval_cases(group_id: UUID) -> list[EvalCaseSummary]:
    """The group's eval cases, by slug. A case whose JSON doesn't validate is listed without a name, so it can be
    deleted."""
    summaries: list[EvalCaseSummary] = []
    for path in _case_jsons(storage.eval_cards_dir(group_id)):
        fixture = _read_fixture(path)
        created_at = fixture.origin.exported_at if fixture and fixture.origin else None
        if created_at is None:
            try:
                created_at = datetime.fromtimestamp(path.stat().st_mtime, UTC)
            except OSError:
                created_at = None
        summaries.append(
            EvalCaseSummary(
                slug=path.stem,
                name=fixture.expected.name if fixture else None,
                page_count=len(fixture.sources) if fixture else 0,
                verified=fixture.verified_by_owner if fixture else False,
                created_at=created_at,
            )
        )
    return summaries


def _own_image_names(slug: str, names: Iterable[str]) -> set[str]:
    """The names an eval case's images may have: `<slug>.<ext>` (added by hand) or `<slug>-<n>.<ext>` (saved here)"""
    pattern = re.compile(rf"^{re.escape(slug)}(?:-\d+)?(?:{'|'.join(map(re.escape, _IMAGE_SUFFIXES))})$", re.I)
    return {name for name in names if pattern.match(name)}


def delete_eval_case(group_id: UUID, slug: str) -> bool:
    """
    Deletes an eval case: its JSON and its images, but never a file another case lists. Whether there was anything
    to delete. Callers hold the ingest write lock.
    """
    if not _SLUG_RE.match(slug):
        return False
    directory = storage.eval_cards_dir(group_id)
    if not directory.is_dir():
        return False

    json_path = directory / f"{slug}.json"
    fixture = _read_fixture(json_path) if json_path.is_file() else None
    files = {path.name for path in directory.iterdir() if path.is_file()}
    candidates = _own_image_names(slug, files)
    if fixture:
        candidates |= {name for name in fixture.sources if name in files and _own_image_names(slug, [name])}

    others: set[str] = set()
    for path in _case_jsons(directory):
        if path.stem != slug and (other := _read_fixture(path)):
            others.update(other.sources)

    removed = False
    for name in sorted(candidates - others):
        (directory / name).unlink(missing_ok=True)
        removed = True
    if json_path.is_file():
        json_path.unlink()
        removed = True
    return removed
