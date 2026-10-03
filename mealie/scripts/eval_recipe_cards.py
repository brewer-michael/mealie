"""
Scores how well each AI provider, and the OCR fallback, reads recipe cards.

Every card in the cards directory is run through the recipe import workflow, the same one behind
`/recipes/create/ai`, and the draft recipe is compared with the card's hand-checked JSON. Nothing is
saved. See `docs/ai/EVAL.md` for the fixture format, how to run this, and how to read the scores.
"""

import argparse
import asyncio
import json
import math
import re
import shutil
import statistics
import sys
import tempfile
import time
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, field_validator
from rapidfuzz import fuzz

from mealie import __version__
from mealie.core import root_logger
from mealie.db.db_setup import session_context
from mealie.lang import get_locale_provider
from mealie.lang.providers import Translator
from mealie.repos.all_repositories import get_repositories
from mealie.repos.repository_factory import AllRepositories
from mealie.schema.group.ai_providers import AIProviderOut
from mealie.schema.household.household import HouseholdInDB
from mealie.schema.openai.compiled_source import OpenAICompiledSource
from mealie.schema.recipe.recipe import Recipe
from mealie.schema.recipe.recipe_ingredient import RecipeIngredient
from mealie.services import ocr as ocr_service
from mealie.services.openai import OpenAIService
from mealie.services.recipe.import_workflow import (
    DEFAULT_WORKFLOW_STEPS,
    RecipeImportWorkflow,
    WorkflowContext,
    WorkflowInput,
    WorkflowOptions,
    WorkflowStep,
)
from mealie.services.recipe.import_workflow.compilers import ImageCompiler, OCRImageCompiler, SourceCompiler
from mealie.services.recipe.import_workflow.steps import CompileSourceStep

if TYPE_CHECKING:
    # the HTTP client library the OpenAI SDK is built on
    import httpx2
    from openai import AsyncOpenAI

logger = root_logger.get_logger()

DEFAULT_CARDS_DIR = Path("tests/data/cards")
DEFAULT_OUT = Path("recipe-card-eval.json")

INGREDIENT_MATCH_THRESHOLD = 0.75
"""
Similarity at which an extracted ingredient counts as the expected line, once their quantities and
units are written the same way. High enough that "1 egg" doesn't match "1 egg yolk", low enough that
"1 T. coconut oil" still matches "1 T. coconut oil (melted)". The quantity and unit must also agree.
"""

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

CASE_SENSITIVE_UNITS: dict[str, str] = {"T": "tbsp", "t": "tsp"}
"""On a handwritten card a capital "T" is a tablespoon and a lowercase "t" a teaspoon, three times smaller"""

_UNITS = {alias: unit for unit, aliases in UNIT_ALIASES.items() for alias in (unit, *aliases)}

_NUMBER = r"\d+\s+\d+/\d+|\d+-\d+/\d+|\d+/\d+|\d*\.\d+|\d+"
_QUANTITY_RE = re.compile(rf"\s*(?P<low>{_NUMBER})(?:\s*(?:-|–|—|to|or)\s*(?P<high>{_NUMBER}))?", re.IGNORECASE)
"""A leading quantity: "2", "1.5", "1/2", "1 1/2" or "1-1/2", or a range of two such as "1-2" or "1 to 2"."""
_UNIT_RE = re.compile(
    # a size or a qualifier may come between the quantity and the unit: "1 (15 oz) can", "1 heaping T."
    r"\s*(?P<size>(?:\([^)]*\)\s*)?(?:(?:heaping|heaped|scant|level|rounded|generous)\s+)?)"
    r"(?P<unit>fl\.?\s*oz|fluid\s+ounces?|[^\W\d_]+)\.?",
    re.IGNORECASE,
)

INSTRUCTION_MATCH_THRESHOLD = 0.6
"""
Similarity below which an expected step counts as missing rather than partly covered. Unrelated
cooking instructions still share words like "for", "minutes" and "until", and score around 0.5.
"""

SCORE_WEIGHTS: dict[str, float] = {
    "name": 0.10,
    "ingredient_recall": 0.30,
    "ingredient_precision": 0.20,
    "instruction_coverage": 0.25,
    "description": 0.05,
    "no_invention": 0.10,
}
"""Weights of the overall score. Components a card doesn't check are left out, and the rest reweighted."""

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
}
"""What a card's `must_not_invent` entries may name, and the recipe fields that must then stay empty"""


class EvalSetupError(Exception):
    """Raised when the eval can't start, e.g. an unknown group or provider."""

    pass


# ================================================================
# Fixtures


class ExpectedRecipe(BaseModel):
    name: str
    description_contains: list[str] = Field(default_factory=list)
    ingredients: list[str]
    """Ingredient lines, verbatim from the card"""
    instructions: list[str] = Field(default_factory=list)
    must_not_invent: list[str] = Field(default_factory=list)
    """Fields the card leaves blank, which a correct extraction leaves blank too. See `INVENTION_CHECKS`."""

    @field_validator("must_not_invent")
    @classmethod
    def validate_must_not_invent(cls, value: list[str]) -> list[str]:
        checks = [normalize_check(check) for check in value]
        if unknown := [check for check in checks if check not in INVENTION_CHECKS]:
            raise ValueError(f"unknown must_not_invent check(s) {unknown}, expected any of {list(INVENTION_CHECKS)}")
        return checks


class CardFixture(BaseModel):
    source: str | list[str]
    """Image file name(s), relative to the JSON file. Several images (front and back) are read as one card."""
    notes: str = ""
    verified_by_owner: bool = False
    expected: ExpectedRecipe


@dataclass
class Card:
    id: str
    images: list[Path]
    fixture: CardFixture


def load_cards(cards_dir: Path, only: Sequence[str] | None = None) -> list[Card]:
    """Loads every `<card>.json` in `cards_dir`, optionally only the named cards."""

    if not cards_dir.is_dir():
        raise EvalSetupError(f"Cards directory '{cards_dir}' doesn't exist")

    cards: list[Card] = []
    for path in sorted(cards_dir.glob("*.json")):
        if only and path.stem not in only:
            continue

        try:
            fixture = CardFixture.model_validate_json(path.read_text())
        except ValueError as e:
            raise EvalSetupError(f"Invalid card fixture '{path}': {e}") from e

        sources = [fixture.source] if isinstance(fixture.source, str) else fixture.source
        images = [path.parent / source for source in sources]
        if not images or (missing := [str(image) for image in images if not image.is_file()]):
            raise EvalSetupError(f"Card '{path.stem}' is missing its image(s): {missing or 'none listed'}")

        cards.append(Card(id=path.stem, images=images, fixture=fixture))

    if only and (unknown := sorted(set(only) - {card.id for card in cards})):
        raise EvalSetupError(f"No such card(s) in '{cards_dir}': {', '.join(unknown)}")
    if not cards:
        raise EvalSetupError(f"No cards found in '{cards_dir}'")

    return cards


# ================================================================
# Scoring


def normalize_check(check: str) -> str:
    return " ".join(check.lower().replace("_", " ").replace("-", " ").split())


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
    return unicodedata.normalize("NFKC", "".join(chars)).replace("\u2044", "/")


def normalize_text(text: str | None) -> str:
    """
    Normalizes text for fuzzy comparison: lowercased, unicode fractions spelled out in ASCII
    ("1½" becomes "1 1/2"), punctuation dropped except in numbers, and whitespace collapsed.
    """

    if not text:
        return ""

    text = ascii_fractions(text).lower()
    text = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", text)  # periods, but not decimal points
    text = re.sub(r"(?<!\d)/|/(?!\d)", " ", text)  # slashes, but not fractions
    text = re.sub(r"[^\w\s./]", " ", text)
    return " ".join(text.split())


def _similarity(a: str, b: str) -> float:
    """Similarity of two normalized lines, 0 to 1"""

    if not (a and b):
        return 1.0 if a == b else 0.0

    return max(fuzz.ratio(a, b), fuzz.token_sort_ratio(a, b)) / 100


def text_similarity(expected: str, actual: str) -> float:
    """Similarity of two lines, 0 to 1, forgiving word order but not missing or extra words."""

    return _similarity(normalize_text(expected), normalize_text(actual))


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


def canonical_unit(token: str) -> str | None:
    """The unit a word stands for ("T." and "Tbsp" both mean "tbsp"), or None if it isn't a unit."""

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
    quantity, and only from a known spelling (see `UNIT_ALIASES`).
    """

    text = ascii_fractions(line)
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


@dataclass(frozen=True)
class LineComparison:
    similarity: float
    """How alike the lines are, 0 to 1, with their quantities and units written the same way"""
    quantity_ok: bool
    unit_ok: bool

    @property
    def amount_ok(self) -> bool:
        return self.quantity_ok and self.unit_ok


def compare_ingredients(expected: str, actual: str) -> LineComparison:
    """Compares an ingredient line with the card's: how alike they are, and whether the amounts agree."""

    a, b = parse_amount(expected), parse_amount(actual)
    return LineComparison(
        similarity=_similarity(a.text, b.text),
        quantity_ok=quantities_agree(a.quantity, b.quantity),
        unit_ok=a.unit == b.unit,
    )


def ingredient_texts(ingredient: RecipeIngredient) -> list[str]:
    """Every rendering of an ingredient that could hold the card's line, best first."""

    texts = [ingredient.original_text, ingredient.display, ingredient.note]
    return list(dict.fromkeys(text for text in texts if text))


@dataclass
class LineMatch:
    expected: str
    actual: str
    similarity: float
    quantity_ok: bool = True
    """Whether the quantities agree: "1/4", "¼" and "0.25" do, "1/4" and "1/2" don't"""
    unit_ok: bool = True
    """Whether the units agree: "T." and "tbsp" do, "T." and "t." (a teaspoon) don't"""


@dataclass
class LineMatches:
    matches: list[LineMatch]
    """Expected lines read correctly: the same ingredient, with the same quantity and unit"""
    misread: list[LineMatch]
    """Expected lines read as the right ingredient, but with the wrong quantity or unit"""
    missing: list[str]
    """Expected lines with no extracted counterpart"""
    extra: list[str]
    """Extracted lines matching no expected line: invented, merged, or misread beyond recognition"""
    recall: float
    """Share of the expected lines read correctly. A misread line counts against it, as a missing one does."""
    precision: float
    """Share of the extracted lines that read an expected line correctly. A misread line counts against it."""
    similarity: float
    """Mean similarity of each expected line to its closest extracted line, matched or not"""


def match_lines(
    expected: Sequence[str], actual: Sequence[Sequence[str]], threshold: float = INGREDIENT_MATCH_THRESHOLD
) -> LineMatches:
    """
    Pairs expected lines with extracted ones, one to one and most similar first. Each extracted
    line is given as its candidate texts, and matches on whichever is closest.

    A line only matches when its quantity and unit agree with the card's. Lines that are alike but
    for the amount are paired up afterwards, as misread, so the results show what went wrong.
    """

    comparisons = [
        [[compare_ingredients(line, text) for text in candidates] for candidates in actual] for line in expected
    ]
    taken_expected: set[int] = set()
    taken_actual: set[int] = set()

    def pair_up(*, amount_ok: bool) -> dict[int, tuple[int, LineComparison]]:
        pairs: list[tuple[int, int, LineComparison]] = []
        for i, row in enumerate(comparisons):
            for j, candidates in enumerate(row):
                eligible = [c for c in candidates if c.amount_ok or not amount_ok]
                closest = max(eligible, key=lambda c: c.similarity, default=None)
                if closest and closest.similarity >= threshold:
                    pairs.append((i, j, closest))

        paired: dict[int, tuple[int, LineComparison]] = {}
        for i, j, comparison in sorted(pairs, key=lambda pair: (-pair[2].similarity, pair[0], pair[1])):
            if i in taken_expected or j in taken_actual:
                continue
            paired[i] = (j, comparison)
            taken_expected.add(i)
            taken_actual.add(j)

        return paired

    def label(candidates: Sequence[str]) -> str:
        return candidates[0] if candidates else ""

    def line_matches(paired: dict[int, tuple[int, LineComparison]]) -> list[LineMatch]:
        return [
            LineMatch(
                expected=expected[i],
                actual=label(actual[j]),
                similarity=round(comparison.similarity, 3),
                quantity_ok=comparison.quantity_ok,
                unit_ok=comparison.unit_ok,
            )
            for i, (j, comparison) in sorted(paired.items())
        ]

    matches = line_matches(pair_up(amount_ok=True))
    misread = line_matches(pair_up(amount_ok=False))

    return LineMatches(
        matches=matches,
        misread=misread,
        missing=[line for i, line in enumerate(expected) if i not in taken_expected],
        extra=[label(candidates) for j, candidates in enumerate(actual) if j not in taken_actual],
        recall=len(matches) / len(expected) if expected else 1.0,
        precision=len(matches) / len(actual) if actual else (0.0 if expected else 1.0),
        similarity=statistics.fmean(
            max((c.similarity for candidates in row for c in candidates), default=0.0) for row in comparisons
        )
        if expected
        else 1.0,
    )


def score_name(expected: str, actual: str | None) -> float:
    a, b = normalize_text(expected), normalize_text(actual)
    if not (a and b):
        return 1.0 if a == b else 0.0

    return fuzz.token_sort_ratio(a, b) / 100


def score_instructions(
    expected: Sequence[str], actual: Sequence[str], threshold: float = INSTRUCTION_MATCH_THRESHOLD
) -> float | None:
    """
    How much of the card's instruction text the recipe covers, 0 to 1, weighted by length. Steps
    may be split or merged differently from the card; each expected step is looked for anywhere in
    the recipe's instructions.
    """

    steps = [normalize_text(step) for step in expected if normalize_text(step)]
    if not steps:
        return None

    haystack = " ".join(normalize_text(step) for step in actual)
    covered = 0.0
    for step in steps:
        # partial_ratio aligns the shorter string within the longer one, so it can only be used
        # one way round: a recipe shorter than the step it's compared with must not score 100%
        if len(haystack) >= len(step):
            similarity = fuzz.partial_ratio(step, haystack) / 100
        else:
            similarity = fuzz.ratio(step, haystack) / 100

        if similarity >= threshold:
            covered += similarity * len(step)

    return covered / sum(len(step) for step in steps)


def score_description(terms: Sequence[str], description: str | None) -> tuple[float | None, list[str]]:
    """The share of `terms` found in the description, and which ones were found."""

    if not terms:
        return None, []

    haystack = f" {normalize_text(description)} "
    hits = [term for term in terms if f" {normalize_text(term)} " in haystack]
    return len(hits) / len(terms), hits


def is_empty_value(value: Any) -> bool:
    if isinstance(value, BaseModel):
        return all(is_empty_value(v) for v in value.model_dump().values())
    if isinstance(value, list | tuple | dict):
        return all(is_empty_value(v) for v in (value.values() if isinstance(value, dict) else value))
    return not value


def find_inventions(checks: Sequence[str], recipe: Recipe) -> dict[str, dict[str, str]]:
    """The recipe fields filled in despite the card leaving them blank, by `must_not_invent` check."""

    inventions: dict[str, dict[str, str]] = {}
    for check in checks:
        filled = {
            field_name: str(value)
            for field_name in INVENTION_CHECKS[normalize_check(check)]
            if not is_empty_value(value := getattr(recipe, field_name, None))
        }
        if filled:
            inventions[check] = filled

    return inventions


def overall_score(components: dict[str, float | None], weights: dict[str, float] = SCORE_WEIGHTS) -> float:
    """Weighted mean of the scored components, ignoring those that weren't scored."""

    scored = {name: score for name, score in components.items() if score is not None and weights.get(name)}
    total_weight = sum(weights[name] for name in scored)
    if not total_weight:
        return 0.0

    return sum(weights[name] * score for name, score in scored.items()) / total_weight


@dataclass
class CardScores:
    overall: float
    name: float
    ingredient_recall: float
    ingredient_precision: float
    ingredient_similarity: float
    instruction_coverage: float | None
    description: float | None
    no_invention: float | None
    ingredients: LineMatches
    description_hits: list[str]
    inventions: dict[str, dict[str, str]]


def score_recipe(expected: ExpectedRecipe, recipe: Recipe) -> CardScores:
    ingredients = match_lines(expected.ingredients, [ingredient_texts(i) for i in recipe.recipe_ingredient])
    instructions = score_instructions(
        expected.instructions, [step.text for step in recipe.recipe_instructions or [] if step.text]
    )
    description, description_hits = score_description(expected.description_contains, recipe.description)
    inventions = find_inventions(expected.must_not_invent, recipe)
    no_invention = 1 - len(inventions) / len(expected.must_not_invent) if expected.must_not_invent else None
    name = score_name(expected.name, recipe.name)

    components: dict[str, float | None] = {
        "name": name,
        "ingredient_recall": ingredients.recall,
        "ingredient_precision": ingredients.precision,
        "instruction_coverage": instructions,
        "description": description,
        "no_invention": no_invention,
    }

    return CardScores(
        overall=overall_score(components),
        name=name,
        ingredient_recall=ingredients.recall,
        ingredient_precision=ingredients.precision,
        ingredient_similarity=ingredients.similarity,
        instruction_coverage=instructions,
        description=description,
        no_invention=no_invention,
        ingredients=ingredients,
        description_hits=description_hits,
        inventions=inventions,
    )


# ================================================================
# Running


@dataclass
class TokenUsage:
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class EvalConfig:
    """One way of reading the cards: a vision provider, or OCR followed by a text provider."""

    label: str
    image_provider: AIProviderOut | None
    """The provider that reads the image. None for the OCR path."""
    text_provider: AIProviderOut | None
    """The provider every other step runs on"""

    @property
    def is_ocr(self) -> bool:
        return self.image_provider is None

    def describe(self) -> dict[str, Any]:
        def provider(p: AIProviderOut | None) -> dict[str, Any] | None:
            # never the key, headers or params, which can hold credentials
            return {"id": str(p.id), "name": p.name, "model": p.model, "base_url": p.base_url} if p else None

        return {
            "label": self.label,
            "ocr": self.is_ocr,
            "image_provider": provider(self.image_provider),
            "text_provider": provider(self.text_provider),
        }


class EvalOpenAIService(OpenAIService):
    """
    An `OpenAIService` pinned to the providers under test instead of the group's configured ones,
    which also tallies the tokens each provider reports using, to put a price on a run.
    """

    def __init__(
        self, repos: AllRepositories, *, image_provider: AIProviderOut | None, text_provider: AIProviderOut | None
    ) -> None:
        super().__init__(repos)
        self.image_provider = image_provider
        self.default_provider = text_provider
        self.audio_provider = None
        self.usage: dict[str, TokenUsage] = {}
        self._http_clients: list[httpx2.AsyncClient] = []

    def get_client(self, provider: AIProviderOut) -> AsyncOpenAI:
        from openai import DefaultAsyncHttpxClient

        async def record_usage(response: httpx2.Response) -> None:
            await self._record_usage(provider, response)

        http_client = DefaultAsyncHttpxClient(event_hooks={"response": [record_usage]})
        self._http_clients.append(http_client)
        return super().get_client(provider).with_options(http_client=http_client)

    async def aclose(self) -> None:
        for http_client in self._http_clients:
            await http_client.aclose()
        self._http_clients.clear()

    async def _record_usage(self, provider: AIProviderOut, response: httpx2.Response) -> None:
        if response.is_error:
            return

        try:
            # reading the body here leaves it cached on the response for the client to parse as usual
            await response.aread()
            usage = response.json().get("usage") or {}
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
        except Exception:
            return

        tally = self.usage.setdefault(provider.name, TokenUsage())
        tally.requests += 1
        tally.prompt_tokens += prompt_tokens
        tally.completion_tokens += completion_tokens


def capture_errors(compiler: type[SourceCompiler], errors: list[str]) -> type[SourceCompiler]:
    """
    Wraps a compiler so that the error it fails with is added to `errors`. The compile step logs a
    failed compiler and moves on, so the workflow itself only reports that the card couldn't be read.
    """

    class ErrorCapturingCompiler(SourceCompiler):
        wrapped = compiler
        source_type = compiler.source_type
        progress_key = compiler.progress_key

        def __init__(self, ctx: WorkflowContext, content: str | None = None) -> None:
            super().__init__(ctx, content)
            self.compiler = compiler(ctx, content)

        def can_compile(self) -> bool:
            return self.compiler.can_compile()

        async def compile(self) -> OpenAICompiledSource | None:
            try:
                return await self.compiler.compile()
            except Exception as e:
                errors.append(f"{compiler.__name__}: {type(e).__name__}: {e}")
                raise

    # the compile step logs failures by class name
    ErrorCapturingCompiler.__name__ = compiler.__name__
    return ErrorCapturingCompiler


def workflow_steps(config: EvalConfig, errors: list[str]) -> list[WorkflowStep]:
    """The import workflow's steps, reading the card only with the config's compiler, whose errors go in `errors`."""

    # Each config is scored on what it reads itself. Left to the default compilers, a provider that
    # failed to read the card would fall back to OCR, and the run would score OCR instead.
    compiler = OCRImageCompiler if config.is_ocr else ImageCompiler
    compile_step = CompileSourceStep(compilers=[capture_errors(compiler, errors)])
    return [compile_step if isinstance(step, CompileSourceStep) else step for step in DEFAULT_WORKFLOW_STEPS]


def summarize_recipe(recipe: Recipe) -> dict[str, Any]:
    return {
        "name": recipe.name,
        "description": recipe.description,
        "recipe_yield": recipe.recipe_yield,
        **{field_name: getattr(recipe, field_name) for field_name in TIME_FIELDS},
        "ingredients": [(ingredient_texts(i) or [""])[0] for i in recipe.recipe_ingredient],
        "instructions": [step.text for step in recipe.recipe_instructions or []],
        "notes": [note.text for note in recipe.notes or []],
    }


def run_cost(usage: dict[str, TokenUsage], prices: dict[str, tuple[float, float]]) -> float | None:
    """
    USD cost of a run's tokens. None if it reported no token usage, say because it failed before a
    provider answered, or if any provider it used has no price.
    """

    if not usage or not all(name in prices for name in usage):
        return None

    return sum(
        tally.prompt_tokens * prices[name][0] / 1e6 + tally.completion_tokens * prices[name][1] / 1e6
        for name, tally in usage.items()
    )


@dataclass
class RunResult:
    card: str
    label: str
    attempt: int
    verified_by_owner: bool
    latency_s: float
    error: str | None = None
    scores: CardScores | None = None
    usage: dict[str, TokenUsage] = field(default_factory=dict)
    cost_usd: float | None = None
    recipe: dict[str, Any] | None = None
    progress: list[tuple[float, str]] = field(default_factory=list)
    """Progress messages and when, in seconds into the run, they were reported"""

    @property
    def score(self) -> float:
        return self.scores.overall if self.scores else 0.0


async def run_card(
    repos: AllRepositories,
    translator: Translator,
    card: Card,
    config: EvalConfig,
    *,
    attempt: int = 1,
    household: HouseholdInDB | None = None,
    prices: dict[str, tuple[float, float]] | None = None,
) -> RunResult:
    """Reads one card with one config. Never raises for a failed read; the error is on the result."""

    ai = EvalOpenAIService(repos, image_provider=config.image_provider, text_provider=config.text_provider)
    result = RunResult(
        card=card.id, label=config.label, attempt=attempt, verified_by_owner=card.fixture.verified_by_owner, latency_s=0
    )

    async def on_progress(message: str) -> None:
        result.progress.append((round(time.perf_counter() - start, 2), message))

    with tempfile.TemporaryDirectory() as temp_dir:
        # the workflow writes resized copies next to the images it reads, so it never gets the fixtures themselves
        images = [Path(shutil.copy(image, temp_dir)) for image in card.images]
        ctx = WorkflowContext(
            input=WorkflowInput(images=images),
            # organizers aren't scored, so skip the extra provider call
            options=WorkflowOptions(resolve_organizers=False),
            repos=repos,
            translator=translator,
            ai=ai,
            household=household,
            on_progress=on_progress,
        )

        recipe: Recipe | None = None
        compile_errors: list[str] = []
        start = time.perf_counter()
        try:
            recipe = (await RecipeImportWorkflow(workflow_steps(config, compile_errors)).run(ctx)).recipe
        except Exception as e:
            # a compiler's error is why there was nothing to build the recipe from, so it's the one to report
            result.error = compile_errors[0] if compile_errors else f"{type(e).__name__}: {e}"
        finally:
            result.latency_s = round(time.perf_counter() - start, 3)
            await ai.aclose()

    result.usage = ai.usage
    result.cost_usd = run_cost(ai.usage, prices or {})
    if recipe:
        result.scores = score_recipe(card.fixture.expected, recipe)
        result.recipe = summarize_recipe(recipe)

    return result


async def run_eval(
    repos: AllRepositories,
    cards: Sequence[Card],
    configs: Sequence[EvalConfig],
    *,
    repeat: int = 1,
    household: HouseholdInDB | None = None,
    prices: dict[str, tuple[float, float]] | None = None,
) -> list[RunResult]:
    """Reads every card with every config, `repeat` times, one at a time so latencies don't interfere."""

    translator = get_locale_provider("en-US")
    results: list[RunResult] = []
    for card in cards:
        for config in configs:
            for attempt in range(1, repeat + 1):
                result = await run_card(
                    repos, translator, card, config, attempt=attempt, household=household, prices=prices
                )
                outcome = f"error: {result.error}" if result.error else f"score {result.score:.2f}"
                logger.info(f"[{config.label}] {card.id} #{attempt}: {outcome} in {result.latency_s:.1f}s")
                results.append(result)

    return results


def find_provider(providers: Sequence[AIProviderOut], name_or_id: str) -> AIProviderOut:
    """Finds a provider by id or name, falling back to a case-insensitive name match."""

    for provider in providers:
        if name_or_id in (str(provider.id), provider.name):
            return provider

    matches = [provider for provider in providers if provider.name.casefold() == name_or_id.casefold()]
    if len(matches) == 1:
        return matches[0]

    available = ", ".join(sorted(provider.name for provider in providers)) or "none"
    raise EvalSetupError(f"No AI provider named '{name_or_id}' in this group (available: {available})")


def build_configs(
    repos: AllRepositories, provider_names: Sequence[str], *, ocr: bool, ocr_provider_name: str | None = None
) -> list[EvalConfig]:
    """
    The configs to evaluate: the named providers, or the group's image provider if none are named,
    plus the OCR path if asked for, which runs on `ocr_provider_name` or the group's default provider.
    """

    if ocr and not ocr_service.is_available():
        # otherwise every OCR run would fail, only saying that the card couldn't be read
        raise EvalSetupError("OCR isn't available: install Tesseract and leave OCR_ENABLED on, or leave out --ocr")

    providers = repos.group_ai_providers.get_all()
    settings = repos.group_ai_provider_settings.get_one(repos.group_id)

    def configured(provider_id: Any) -> AIProviderOut | None:
        return next((provider for provider in providers if provider.id == provider_id), None) if provider_id else None

    if provider_names:
        # one provider may be named twice, by name and by id, or in a different case
        resolved = [find_provider(providers, name) for name in provider_names]
        selected = list({provider.id: provider for provider in resolved}.values())
    elif image_provider := configured(settings and settings.image_provider_id):
        selected = [image_provider]
    else:
        selected = []
        if not ocr:
            raise EvalSetupError("The group has no image provider; pass --provider, or --ocr to evaluate only OCR")

    configs = [EvalConfig(label=p.name, image_provider=p, text_provider=p) for p in selected]
    if ocr:
        text_provider = (
            find_provider(providers, ocr_provider_name)
            if ocr_provider_name
            else configured(settings and settings.default_provider_id)
        )
        if not text_provider:
            raise EvalSetupError("OCR needs a text provider: set the group's default provider or pass --ocr-provider")

        configs.append(EvalConfig(label=f"OCR+{text_provider.name}", image_provider=None, text_provider=text_provider))

    return configs


# ================================================================
# Reporting


def _mean(values: Iterable[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return statistics.fmean(present) if present else None


@dataclass
class ConfigSummary:
    label: str
    model: str
    runs: int
    errors: int
    score: float
    """Mean overall score, counting failed runs as 0"""
    recall: float | None
    precision: float | None
    misread: float | None
    """Mean ingredient lines per run read as the right ingredient, but with the wrong quantity or unit"""
    instructions: float | None
    inventions: int
    """Runs that filled in a field the card leaves blank"""
    latency_s: float | None
    latency_p50_s: float | None
    tokens: float | None
    """Mean tokens per run"""
    cost_usd: float | None
    """Mean cost of the runs that reported token usage, if every provider the config used has a price"""


def summarize(results: Sequence[RunResult], configs: Sequence[EvalConfig]) -> list[ConfigSummary]:
    summaries: list[ConfigSummary] = []
    for config in configs:
        runs = [result for result in results if result.label == config.label]
        if not runs:
            continue

        succeeded = [result for result in runs if result.scores]
        latencies = [result.latency_s for result in succeeded]
        tokens = [sum(t.prompt_tokens + t.completion_tokens for t in result.usage.values()) for result in runs]
        costs = [result.cost_usd for result in runs]
        models = [p.model for p in (config.image_provider, config.text_provider) if p]

        summaries.append(
            ConfigSummary(
                label=config.label,
                model="+".join(dict.fromkeys(models)),
                runs=len(runs),
                errors=len(runs) - len(succeeded),
                score=statistics.fmean(result.score for result in runs),
                recall=_mean(result.scores.ingredient_recall for result in succeeded if result.scores),
                precision=_mean(result.scores.ingredient_precision for result in succeeded if result.scores),
                misread=_mean(len(result.scores.ingredients.misread) for result in succeeded if result.scores),
                instructions=_mean(result.scores.instruction_coverage for result in succeeded if result.scores),
                inventions=sum(1 for result in succeeded if result.scores and result.scores.inventions),
                latency_s=_mean(latencies),
                latency_p50_s=statistics.median(latencies) if latencies else None,
                tokens=_mean(tokens) if any(tokens) else None,
                cost_usd=_mean(costs),
            )
        )

    return summaries


def _fmt(value: float | None, spec: str = ".2f", suffix: str = "") -> str:
    return "-" if value is None else f"{value:{spec}}{suffix}"


def format_table(rows: Sequence[Sequence[str]], text_columns: int = 1) -> str:
    """Formats rows under the first row's headings: text columns left-aligned, numbers right-aligned"""

    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines = [
        "  ".join(
            cell.ljust(width) if i < text_columns else cell.rjust(width)
            for i, (cell, width) in enumerate(zip(row, widths, strict=True))
        )
        for row in rows
    ]
    lines.insert(1, "  ".join("-" * width for width in widths))
    return "\n".join(lines)


def format_report(results: Sequence[RunResult], configs: Sequence[EvalConfig], cards: Sequence[Card]) -> str:
    """A table per provider, then a table of each card's mean score per provider"""

    summary_rows = [
        [
            *("Provider", "Model", "Runs", "Errors", "Score", "Recall", "Precision", "Misread", "Instr", "Invented"),
            *("Latency", "p50", "Tokens", "Cost/card"),
        ]
    ]
    for s in summarize(results, configs):
        summary_rows.append(
            [
                *(s.label, s.model, str(s.runs), str(s.errors)),
                *(_fmt(s.score), _fmt(s.recall), _fmt(s.precision), _fmt(s.misread, ".1f"), _fmt(s.instructions)),
                str(s.inventions),
                *(_fmt(s.latency_s, ".1f", "s"), _fmt(s.latency_p50_s, ".1f", "s"), _fmt(s.tokens, ",.0f")),
                "-" if s.cost_usd is None else f"${s.cost_usd:.4f}",
            ]
        )

    labels = [config.label for config in configs]
    card_rows = [["Card", *labels]]
    for card in cards:
        row = [card.id if card.fixture.verified_by_owner else f"{card.id} (unverified)"]
        for label in labels:
            runs = [result for result in results if result.card == card.id and result.label == label]
            if runs and all(result.error for result in runs):
                row.append("error")
            else:
                row.append(_fmt(_mean(result.score for result in runs)))
        card_rows.append(row)

    return f"{format_table(summary_rows, text_columns=2)}\n\nMean score per card:\n{format_table(card_rows)}\n"


def build_report(
    results: Sequence[RunResult],
    configs: Sequence[EvalConfig],
    cards: Sequence[Card],
    *,
    group: str,
    cards_dir: Path,
    repeat: int,
) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "mealie_version": __version__,
        "group": group,
        "cards_dir": str(cards_dir),
        "cards": [{"id": card.id, "verified_by_owner": card.fixture.verified_by_owner} for card in cards],
        "repeat": repeat,
        "thresholds": {
            "ingredient": INGREDIENT_MATCH_THRESHOLD,
            "quantity_tolerance": QUANTITY_TOLERANCE,
            "instruction": INSTRUCTION_MATCH_THRESHOLD,
        },
        "weights": SCORE_WEIGHTS,
        "configs": [config.describe() for config in configs],
        "summary": [asdict(summary) for summary in summarize(results, configs)],
        "runs": [asdict(result) for result in results],
    }


# ================================================================
# CLI


def parse_price(value: str) -> tuple[str, tuple[float, float]]:
    """Parses `NAME=INPUT,OUTPUT`, in USD per million tokens"""

    name, sep, rates = value.rpartition("=")
    try:
        input_rate, output_rate = (float(rate) for rate in rates.split(","))
    except ValueError:
        input_rate = output_rate = -1

    if not (sep and name and input_rate >= 0 and output_rate >= 0):
        raise argparse.ArgumentTypeError(f"expected NAME=INPUT,OUTPUT in USD per million tokens, got '{value}'")

    return name, (input_rate, output_rate)


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mealie.scripts.eval_recipe_cards",
        description=(
            "Score how well the group's AI providers, and the OCR fallback, read recipe cards. "
            "Each card goes through the same import workflow as /recipes/create/ai, but nothing is saved."
        ),
    )
    parser.add_argument("--group", required=True, help="slug or id of the group whose AI providers to use")
    parser.add_argument("--household", help="slug or id of a household in the group (optional)")
    parser.add_argument(
        "--provider",
        dest="providers",
        action="append",
        default=[],
        metavar="NAME",
        help="name (or id) of an AI provider to evaluate; repeatable (default: the group's image provider)",
    )
    parser.add_argument("--ocr", action="store_true", help="also evaluate the OCR fallback (needs Tesseract installed)")
    parser.add_argument(
        "--ocr-provider",
        metavar="NAME",
        help="the provider that turns OCR text into a recipe (default: the group's default provider)",
    )
    parser.add_argument(
        "--cards", type=Path, default=DEFAULT_CARDS_DIR, help=f"card fixtures directory (default: {DEFAULT_CARDS_DIR})"
    )
    parser.add_argument(
        "--card",
        dest="only_cards",
        action="append",
        default=[],
        metavar="ID",
        help="only evaluate this card, by file name without extension; repeatable",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"JSON results file (default: {DEFAULT_OUT})")
    parser.add_argument(
        "--repeat", type=positive_int, default=1, help="read each card this many times per provider (default: 1)"
    )
    parser.add_argument(
        "--price",
        dest="prices",
        action="append",
        type=parse_price,
        default=[],
        metavar="NAME=IN,OUT",
        help="a provider's price in USD per million input and output tokens, to report cost; repeatable",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    try:
        cards = load_cards(args.cards, args.only_cards)
        with session_context() as session:
            group = get_repositories(session, group_id=None, household_id=None).groups.get_by_slug_or_id(args.group)
            if not group:
                raise EvalSetupError(f"No group '{args.group}'")

            household: HouseholdInDB | None = None
            if args.household:
                household = get_repositories(session, group_id=group.id).households.get_by_slug_or_id(args.household)
                if not household:
                    raise EvalSetupError(f"No household '{args.household}' in group '{group.slug}'")

            repos = get_repositories(session, group_id=group.id, household_id=household.id if household else None)
            configs = build_configs(repos, args.providers, ocr=args.ocr, ocr_provider_name=args.ocr_provider)

            if unverified := [card.id for card in cards if not card.fixture.verified_by_owner]:
                logger.warning(f"Not yet verified by the owner, so their expected values may be wrong: {unverified}")

            logger.info(f"Evaluating {len(cards)} card(s) with: {', '.join(config.label for config in configs)}")
            results = asyncio.run(
                run_eval(repos, cards, configs, repeat=args.repeat, household=household, prices=dict(args.prices))
            )
    except EvalSetupError as e:
        logger.error(str(e))
        sys.exit(2)

    report = build_report(results, configs, cards, group=group.slug, cards_dir=args.cards, repeat=args.repeat)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=str))

    sys.stdout.write(f"\n{format_report(results, configs, cards)}\nFull results: {args.out}\n")


if __name__ == "__main__":
    main()
