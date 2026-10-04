"""
Scores how well each AI provider, and the OCR fallback, reads recipe cards.

By default (`--pipeline card`) every card goes through exactly the production recipe card pipeline
(docs/ai/PHASE2.md §11): `images.normalize_page`, `pipeline.orient_page` and `pipeline.extract_card`, with the
provider under test pinned and each config reading the card one way only (`read_path`). `--pipeline import` runs the
older `/recipes/create/ai` import workflow instead, so earlier numbers stay reproducible. The draft is compared with
the card's hand-checked JSON, and its flags with what is actually wrong. Nothing is saved. See `docs/ai/EVAL.md` for
the fixture format, how to run this, and how to read the scores.

`--check` validates the fixtures with no settings or database (it runs before they're imported), and `--dry-run`
checks everything a run needs without calling a provider. Secrets given as `NAME_FILE` (Docker secrets) are read
first, as `docker/entry.sh` does for the server, since `docker exec` skips it.
"""

import argparse
import asyncio
import dataclasses
import hashlib
import json
import math
import os
import random
import re
import shutil
import statistics
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from functools import cached_property
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel
from rapidfuzz import fuzz

from mealie import __version__
from mealie.core import root_logger
from mealie.lang import get_locale_provider
from mealie.lang.providers import Translator
from mealie.schema.group.ai_providers import AIProviderOut, AIProviderSlot
from mealie.schema.household.household import HouseholdInDB
from mealie.schema.recipe.recipe import Recipe
from mealie.schema.recipe.recipe_ingredient import RecipeIngredient
from mealie.schema.recipe_ingest import CardDraft, CardDraftRef, CardFlag, CardFlagSeverity
from mealie.services import ocr as ocr_service
from mealie.services.ai.errors import AIProviderLocalOnlyError
from mealie.services.ai.ingest import eval_export
from mealie.services.ai.ingest.eval_export import (  # noqa: F401  (the fixture format, re-exported for callers)
    BLANK_RE,
    CASE_SENSITIVE_UNITS,
    FIXTURE_SCHEMA_VERSION,
    FIXTURE_TAGS,
    INVENTION_CHECKS,
    QUANTITY_TOLERANCE,
    TIME_FIELDS,
    UNIT_ALIASES,
    YIELD_FIELDS,
    CardFixture,
    ExpectedBlank,
    ExpectedIngredient,
    ExpectedRecipe,
    ExpectedTimes,
    FixtureDraftedBy,
    FixtureOrigin,
    IngredientAmount,
    ascii_fractions,
    canonical_unit,
    normalize_check,
    normalize_text,
    parse_amount,
    parse_number,
    quantities_agree,
    strip_markers,
)
from mealie.services.ai.ingest.flag_rules import HIGHLIGHTED_SEVERITIES, is_clean
from mealie.services.ai.ingest.images import PageRejected, normalize_page, sniff
from mealie.services.ai.local import is_local_provider
from mealie.services.ai.policy import ai_call_policy, apply_policy

logger = root_logger.get_logger()

# ================================================================
# Before the database: the fixtures check, the command line and the secrets
#
# `--check` needs no database and no settings (`PRODUCTION` may be unset), and secrets given as `NAME_FILE` must be
# read before the settings are; run as a script, both happen here, before the imports further down read them.

DEFAULT_CARDS_DIR = Path("tests/data/cards")
DEFAULT_OUT = Path("recipe-card-eval.json")

SECRET_FILE_VARS = (
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_SERVER",
    "POSTGRES_PORT",
    "POSTGRES_DB",
    "POSTGRES_URL_OVERRIDE",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "LDAP_SERVER_URL",
    "LDAP_QUERY_PASSWORD",
    "OIDC_CONFIGURATION_URL",
    "OIDC_CLIENT_ID",
    "OIDC_CLIENT_SECRET",
)
"""The settings `docker/entry.sh` reads from `NAME_FILE` (Docker secrets); `docker exec` skips `entry.sh`"""


def load_secret_files(environ: dict[str, str] | None = None) -> list[str]:
    """
    For each of `SECRET_FILE_VARS` whose `NAME` isn't set but `NAME_FILE` is, reads that file into `NAME`, without its
    trailing newlines, as `docker/entry.sh` does for the server. Returns the names set. A variable already set wins:
    `docker exec -e NAME=…` overrides a secret. Raises `EvalSetupError` for a file that can't be read.
    """
    env: Any = os.environ if environ is None else environ
    loaded: list[str] = []
    for name in SECRET_FILE_VARS:
        path = env.get(f"{name}_FILE")
        if not path or env.get(name):
            continue
        try:
            env[name] = Path(path).read_text().rstrip("\n")
        except OSError as e:
            raise EvalSetupError(f"Can't read {name}_FILE ('{path}'): {e.strerror or e}") from e
        loaded.append(name)
    return loaded


class EvalSetupError(Exception):
    """Raised when the eval can't start, e.g. an unknown group or provider."""

    pass


# ================================================================
# Fixtures


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

        images = [path.parent / source for source in fixture.sources]
        if not images or (missing := [str(image) for image in images if not image.is_file()]):
            raise EvalSetupError(f"Card '{path.stem}' is missing its image(s): {missing or 'none listed'}")

        cards.append(Card(id=path.stem, images=images, fixture=fixture))

    if only and (unknown := sorted(set(only) - {card.id for card in cards})):
        raise EvalSetupError(f"No such card(s) in '{cards_dir}': {', '.join(unknown)}")
    if not cards:
        raise EvalSetupError(f"No cards found in '{cards_dir}'")

    return cards


def check_cards(cards: Sequence[Card]) -> None:
    """
    What `--check` adds to loading the fixtures: every image must be a format a card can be uploaded in (by its
    content, not its name). Raises `EvalSetupError` listing the problems.
    """
    problems: list[str] = []
    for card in cards:
        for image in card.images:
            with open(image, "rb") as file:
                kind = sniff(file.read(16))
            if kind is None or kind == "pdf":
                problems.append(f"{card.id}: {image.name} isn't an image a card can be uploaded as")
    if problems:
        raise EvalSetupError("; ".join(problems))


def run_check(args: argparse.Namespace) -> None:
    """`--check`: loads and checks the fixtures and lists them. Raises `EvalSetupError` with the problems."""
    cards = load_cards(args.cards, args.only_cards)
    check_cards(cards)
    for card in cards:
        tags = f" [{', '.join(card.fixture.tags)}]" if card.fixture.tags else ""
        state = "verified" if card.fixture.verified_by_owner else "unverified"
        sys.stdout.write(f"{card.id}: v{card.fixture.schema_version}, {len(card.images)} image(s), {state}{tags}\n")
    sys.stdout.write(f"{len(cards)} card(s) in {args.cards} are valid\n")


def parse_chain(spec: str) -> list[str]:
    """`A>B[>C]`: the labels, in the order they're tried"""
    labels = [label.strip() for label in spec.split(">")]
    if len(labels) < 2 or not all(labels) or len(set(labels)) != len(labels):
        raise argparse.ArgumentTypeError(f"expected 'A>B' naming two or more different configs, got '{spec}'")
    return labels


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
            "Score how well the group's AI providers, and the OCR fallback, read recipe cards. Each card goes "
            "through the production recipe card pipeline (or, with --pipeline import, the /recipes/create/ai "
            "workflow), but nothing is saved."
        ),
    )
    parser.add_argument("--group", help="slug or id of the group whose AI providers to use (required to run)")
    parser.add_argument("--household", help="slug or id of a household in the group (optional)")
    parser.add_argument(
        "--provider",
        dest="providers",
        action="append",
        default=[],
        metavar="VISION[:TEXT]",
        help=(
            "an AI provider to evaluate, by name or id; VISION:TEXT reads the card with VISION and runs every other "
            "step on TEXT; repeatable (default: the group's image provider)"
        ),
    )
    parser.add_argument("--ocr", action="store_true", help="also evaluate the OCR fallback (needs Tesseract installed)")
    parser.add_argument(
        "--ocr-provider",
        dest="ocr_providers",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "a provider that turns OCR text into a recipe, one OCR config each; repeatable; implies --ocr "
            "(default: the group's default provider)"
        ),
    )
    parser.add_argument(
        "--pipeline",
        choices=["card", "import"],
        default="card",
        help="card: the recipe card pipeline (default); import: the /recipes/create/ai workflow, as before Phase 2",
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
    parser.add_argument("--check", action="store_true", help="only validate the fixtures (no --group, no providers)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "check everything a run needs (fixtures, providers, chains, prices) without calling a provider or "
            "writing anything; exits 1 listing the problems"
        ),
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
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="refuse providers that aren't marked as running on your network at a private address",
    )
    parser.add_argument(
        "--cross-read", action="store_true", help="read every card a second time and compare (card pipeline)"
    )
    parser.add_argument(
        "--no-intake-ocr",
        dest="intake_ocr",
        action="store_false",
        help="skip orienting the pages with Tesseract, to see what orientation is worth (card pipeline)",
    )
    parser.add_argument(
        "--baseline", metavar="LABEL", help="compare every config with this one, paired by card, with a 95%% interval"
    )
    parser.add_argument(
        "--chain",
        dest="chains",
        action="append",
        type=parse_chain,
        default=[],
        metavar="A>B",
        help="also report A falling back to B when A can't read a card itself, from the same results; repeatable",
    )
    parser.add_argument(
        "--reference",
        type=Path,
        metavar="FILE",
        help="an earlier run's JSON, for the cross-read rule (with --cross-read) and orientation (--no-intake-ocr)",
    )
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.check and not args.group:
        parser.error("--group is required (except with --check)")
    if args.check and args.dry_run:
        parser.error("--check and --dry-run don't go together: --dry-run checks the fixtures too")
    if args.pipeline == "import" and (args.cross_read or not args.intake_ocr):
        parser.error("--cross-read and --no-intake-ocr need --pipeline card")
    return args


def _before_imports(argv: Sequence[str]) -> None:
    """
    Run as a script, before the settings and the database are imported: the secrets are read, and `--check` (which
    needs neither) is done. The arguments are checked here too, so a mistake costs no start-up.
    """
    try:
        load_secret_files()
        args = parse_args(argv)
        if args.check:
            run_check(args)
            sys.exit(0)
    except EvalSetupError as e:
        logger.error(str(e))
        sys.exit(2)


if __name__ == "__main__":
    _before_imports(sys.argv[1:])

from mealie.db.db_setup import session_context  # noqa: E402
from mealie.repos.all_repositories import get_repositories  # noqa: E402
from mealie.repos.repository_factory import AllRepositories  # noqa: E402
from mealie.services.ai.ingest.matching import IngestMatcher  # noqa: E402
from mealie.services.ai.ingest.pipeline import (  # noqa: E402
    CardExtraction,
    CardPage,
    CardPipelineOptions,
    extract_card,
    options_for_group,
    orient_page,
)
from mealie.services.ai.ingest.pipeline.cardtext import strip_from_prefix  # noqa: E402
from mealie.services.ai.ingest.pipeline.compilers import CapturedError, capture_errors  # noqa: E402
from mealie.services.ai.runtime import AIRuntime  # noqa: E402
from mealie.services.ai.usage import AITokenUsage  # noqa: E402
from mealie.services.openai import OpenAINotEnabledException, OpenAIService  # noqa: E402
from mealie.services.recipe.import_workflow import (  # noqa: E402
    DEFAULT_WORKFLOW_STEPS,
    RecipeImportWorkflow,
    WorkflowContext,
    WorkflowInput,
    WorkflowOptions,
    WorkflowStep,
)
from mealie.services.recipe.import_workflow.compilers import ImageCompiler, OCRImageCompiler  # noqa: E402
from mealie.services.recipe.import_workflow.steps import CompileSourceStep  # noqa: E402

Pipeline = Literal["card", "import"]

INGREDIENT_MATCH_THRESHOLD = 0.75
"""
Similarity at which an extracted ingredient counts as the expected line, once their quantities and
units are written the same way. High enough that "1 egg" doesn't match "1 egg yolk", low enough that
"1 T. coconut oil" still matches "1 T. coconut oil (melted)". The quantity and unit must also agree.
"""

INSTRUCTION_MATCH_THRESHOLD = 0.6
"""
Similarity below which an expected step counts as missing rather than partly covered. Unrelated
cooking instructions still share words like "for", "minutes" and "until", and score around 0.5.
"""

NAME_CORRECT_THRESHOLD = 0.9
"""Name similarity at which the name counts as read correctly, for flag calibration"""

BLANK_MATCH_THRESHOLD = 0.6
"""Similarity at which a draft item is taken for the expected item holding a blank"""

LINK_NAME_THRESHOLD = 90
"""How alike (fuzzy ratio) a food's name and the expected one must be to count as the same food"""

SCORE_WEIGHTS: dict[str, float] = {
    "name": 0.10,
    "ingredient_recall": 0.30,
    "ingredient_precision": 0.20,
    "instruction_coverage": 0.25,
    "description": 0.05,
    "no_invention": 0.10,
}
"""Weights of the overall score. Components a card doesn't check are left out, and the rest reweighted."""

# The decisions fixed in advance (docs/ai/PHASE2.md §11.4)
SILENT_ERRORS_TARGET = 0.25
"""At most this many unflagged errors per card for the default config"""
FLAG_RECALL_TARGET = 0.8
"""At least this share of the errors flagged"""
BANANA_CARD = "banana-mug-cake"
BANANA_REPEATS = 3
"""The banana card's blank must be safe in 3 of 3 repeats (a Phase 2 release check)"""
CROSS_READ_SILENT_DROP = 0.1
"""Cross-read is turned on by default if it lowers silent errors per card by at least this much"""
D3_RESCUED_CARDS = 2
"""The OCR fallback is kept if the vision>OCR chain rescues at least this many cards (or its interval excludes 0)"""
WRONG_TURNS_LIMIT = 1
"""More upright cards turned by the orientation probe than this (in 20) raises `ORIENT_MIN_RATIO`"""

BOOTSTRAP_SAMPLES = 2000
BOOTSTRAP_SEED = 20261003
"""The bootstrap is seeded, so a report can be reproduced exactly"""
TIE_TOLERANCE = 0.005
"""Per-card score differences within this are ties"""

PER_CARD_TIMES = ("prep_time", "perform_time", "total_time")
"""The time fields a card draft has"""


# ================================================================
# Scoring


def _similarity(a: str, b: str) -> float:
    """Similarity of two normalized lines, 0 to 1"""

    if not (a and b):
        return 1.0 if a == b else 0.0

    return max(fuzz.ratio(a, b), fuzz.token_sort_ratio(a, b)) / 100


def text_similarity(expected: str, actual: str) -> float:
    """Similarity of two lines, 0 to 1, forgiving word order but not missing or extra words."""

    return _similarity(normalize_text(expected), normalize_text(actual))


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
    expected_index: int = -1
    """The expected line's position on the card"""
    actual_index: int = -1
    """The extracted line's position in the recipe, to join it with its flags"""


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
    extra_indices: list[int] = field(default_factory=list)
    """The positions of the `extra` lines in the recipe"""


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
                expected_index=i,
                actual_index=j,
            )
            for i, (j, comparison) in sorted(paired.items())
        ]

    matches = line_matches(pair_up(amount_ok=True))
    misread = line_matches(pair_up(amount_ok=False))
    extra_indices = [j for j in range(len(actual)) if j not in taken_actual]

    return LineMatches(
        matches=matches,
        misread=misread,
        missing=[line for i, line in enumerate(expected) if i not in taken_expected],
        extra=[label(actual[j]) for j in extra_indices],
        recall=len(matches) / len(expected) if expected else 1.0,
        precision=len(matches) / len(actual) if actual else (0.0 if expected else 1.0),
        similarity=statistics.fmean(
            max((c.similarity for candidates in row for c in candidates), default=0.0) for row in comparisons
        )
        if expected
        else 1.0,
        extra_indices=extra_indices,
    )


def score_name(expected: str, actual: str | None) -> float:
    a, b = normalize_text(expected), normalize_text(actual)
    if not (a and b):
        return 1.0 if a == b else 0.0

    return fuzz.token_sort_ratio(a, b) / 100


def _step_similarity(expected: str, actual: str) -> float:
    """
    How well one normalized step covers another, 0 to 1. partial_ratio aligns the shorter string within the longer,
    so it's only used when the second is at least as long: a recipe shorter than a step must not score 100%.
    """
    if len(actual) >= len(expected):
        return fuzz.partial_ratio(expected, actual) / 100
    return fuzz.ratio(expected, actual) / 100


def score_instructions(
    expected: Sequence[str], actual: Sequence[str], threshold: float = INSTRUCTION_MATCH_THRESHOLD
) -> float | None:
    """
    How much of the card's instruction text the recipe covers, 0 to 1, weighted by length. Steps
    may be split or merged differently from the card; each expected step is looked for anywhere in
    the recipe's instructions. Markers don't count: whether a blank was kept is scored on its own.
    """

    steps = [normalize_text(step) for step in expected if normalize_text(step)]
    if not steps:
        return None

    haystack = " ".join(normalize_text(step) for step in actual)
    covered = 0.0
    for step in steps:
        similarity = _step_similarity(step, haystack)
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


def find_inventions(checks: Sequence[str], recipe: Any) -> dict[str, dict[str, str]]:
    """
    The fields of a recipe (a `Recipe` or a card draft) filled in despite the card leaving them blank, by
    `must_not_invent` check.
    """

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


# ----------------------------------------------------------------
# What is scored, from either pipeline


@dataclass
class IngredientView:
    texts: list[str]
    """Every rendering of the line, best first"""
    ref: str | None = None
    """The draft's `reference_id`, which flags are keyed to"""
    food: CardDraftRef | None = None
    unit: CardDraftRef | None = None


@dataclass
class StepView:
    text: str
    ref: str | None = None
    """The draft step's `id`, which flags are keyed to"""


@dataclass
class RecipeView:
    """The parts of an extracted recipe the scores read, from a `Recipe` (import) or a `CardDraft` (card)"""

    name: str | None
    description: str | None
    attribution: str | None
    recipe_yield: str | None
    yield_numbers: list[float]
    times: dict[str, str | None]
    ingredients: list[IngredientView]
    steps: list[StepView]
    notes: list[str]
    """Every note's text, by position (flags on a note are keyed to its index)"""
    source: Any
    """The recipe or draft itself, for `find_inventions`"""


def _ref(value: Any) -> CardDraftRef | None:
    if value is None:
        return None
    return CardDraftRef(id=getattr(value, "id", None), name=getattr(value, "name", None) or "")


def recipe_view(recipe: Recipe) -> RecipeView:
    yield_numbers = numbers_in(recipe.recipe_yield) + [
        value for value in (recipe.recipe_yield_quantity, recipe.recipe_servings) if value
    ]
    return RecipeView(
        name=recipe.name,
        description=recipe.description,
        attribution=None,
        recipe_yield=recipe.recipe_yield,
        yield_numbers=yield_numbers,
        times={name: getattr(recipe, name, None) for name in TIME_FIELDS},
        ingredients=[
            IngredientView(
                texts=ingredient_texts(ingredient),
                ref=str(ingredient.reference_id) if ingredient.reference_id else None,
                food=_ref(ingredient.food),
                unit=_ref(ingredient.unit),
            )
            for ingredient in recipe.recipe_ingredient
        ],
        steps=[
            StepView(text=step.text or "", ref=str(step.id) if step.id else None)
            for step in recipe.recipe_instructions or []
        ],
        notes=[note.text or "" for note in recipe.notes or []],
        source=recipe,
    )


def draft_view(draft: CardDraft) -> RecipeView:
    yield_numbers = numbers_in(draft.recipe_yield) + [
        value for value in (draft.recipe_yield_quantity, draft.recipe_servings) if value
    ]
    return RecipeView(
        name=draft.name,
        description=draft.description,
        attribution=draft.attribution,
        recipe_yield=draft.recipe_yield,
        yield_numbers=yield_numbers,
        times={name: getattr(draft, name, None) for name in PER_CARD_TIMES},
        ingredients=[
            IngredientView(
                texts=list(
                    dict.fromkeys(text for text in (i.original_text, i.display, i.note) if text and text.strip())
                ),
                ref=str(i.reference_id),
                food=i.food,
                unit=i.unit,
            )
            for i in draft.ingredients
        ],
        steps=[StepView(text=step.text, ref=str(step.id)) for step in draft.steps],
        notes=[note.text for note in draft.notes],
        source=draft,
    )


_NUMBER_TOKEN = re.compile(r"\d+\s+\d+/\d+|\d+/\d+|\d*\.\d+|\d+")


def numbers_in(text: str | None) -> list[float]:
    """Every number in a text: "1 1/2" and "1½" read as 1.5, "350°F" as 350, "1-2" as 1 and 2. Markers ignored."""
    if not text:
        return []
    values: list[float] = []
    for match in _NUMBER_TOKEN.finditer(ascii_fractions(strip_markers(text))):
        if (value := parse_number(match.group())) is not None:
            values.append(value)
    return values


def _number_on(value: float, numbers: Iterable[float]) -> bool:
    return any(math.isclose(value, number, rel_tol=QUANTITY_TOLERANCE) for number in numbers)


def card_numbers(expected: ExpectedRecipe) -> list[float]:
    """Every number written on the card, as the fixture records it"""
    texts: list[str | None] = [
        expected.name,
        expected.attribution,
        expected.recipe_yield,
        *expected.ingredient_lines,
        *expected.instructions,
        *(blank.text for blank in expected.blanks),
    ]
    if expected.times:
        texts.extend(expected.times.model_dump().values())
    numbers = [number for text in texts for number in numbers_in(text)]
    numbers.extend(line.quantity for line in expected.structured_ingredients if line and line.quantity)
    return numbers


@dataclass
class Invention:
    """A number in a step, time or yield that isn't on the card"""

    field: str
    value: float
    ref: str | None = None
    text: str = ""


def find_number_inventions(view: RecipeView, numbers: Sequence[float]) -> list[Invention]:
    """
    Numbers in the steps, times and yield that aren't on the card: the model filled in a blank, or made up a time
    or yield. An invented "2 minutes" still covers a step almost fully, so it's counted on its own.
    """
    inventions: list[Invention] = []
    for step in view.steps:
        inventions.extend(
            Invention(field="steps", value=value, ref=step.ref, text=step.text)
            for value in numbers_in(step.text)
            if not _number_on(value, numbers)
        )
    for name, value in view.times.items():
        inventions.extend(
            Invention(field=name, value=number, text=str(value))
            for number in numbers_in(value)
            if not _number_on(number, numbers)
        )
    inventions.extend(
        Invention(field="recipe_yield", value=number, text=view.recipe_yield or "")
        for number in dict.fromkeys(view.yield_numbers)
        if not _number_on(number, numbers)
    )
    return inventions


def score_attribution(expected: str | None, actual: str | None) -> float | None:
    """
    How alike the attribution is to the card's ("From Grandma Jo"); None when the card has none. A leading "From" is
    left out on both sides: the draft keeps "Grandma Jo" under a field labelled "From", and older fixtures say it.
    """
    if not expected:
        return None
    return score_name(strip_from_prefix(expected), strip_from_prefix(actual) if actual else actual)


def score_yield(expected: str | None, view: RecipeView) -> float | None:
    """1 if the yield the card states was read (its numbers, or its words when it has none), else 0"""
    if not expected:
        return None
    numbers = numbers_in(expected)
    if numbers:
        return 1.0 if all(_number_on(number, view.yield_numbers) for number in numbers) else 0.0
    return 1.0 if text_similarity(expected, view.recipe_yield or "") >= INSTRUCTION_MATCH_THRESHOLD else 0.0


def _same_numbers(a: str | None, b: str | None) -> bool:
    x, y = sorted(numbers_in(a)), sorted(numbers_in(b))
    return bool(x) and len(x) == len(y) and all(_number_on(n, [m]) for n, m in zip(x, y, strict=True))


def _expected_times(expected: ExpectedRecipe) -> dict[str, str]:
    if not expected.times:
        return {}
    return {
        name: value
        for name, value in expected.times.model_dump().items()
        if value and not BLANK_RE.search(value) and strip_markers(value)
    }


def score_times(expected: ExpectedRecipe, view: RecipeView) -> float | None:
    """The share of the times written on the card that were read, in whichever time field they landed"""
    times = _expected_times(expected)
    if not times:
        return None
    found = sum(1 for value in times.values() if any(_same_numbers(value, actual) for actual in view.times.values()))
    return found / len(times)


# ----------------------------------------------------------------
# Linking (card pipeline)


class LinkCatalog(Protocol):
    """The group's foods and units, by name, plural or alias (`IngestMatcher`)"""

    def exact_food(self, name: str | None) -> Any: ...

    def exact_unit(self, name: str | None) -> Any: ...


@dataclass
class LinkingScores:
    """How matched ingredient lines were linked, relative to the group's foods and units"""

    food_checked: int = 0
    food_correct: int = 0
    food_wrong: int = 0
    """Linked to an existing food, but the wrong one: worse than not linked"""
    food_unlinked: int = 0
    """Not linked: commit creates the food, or keeps the line as text"""
    unit_checked: int = 0
    unit_correct: int = 0
    unit_wrong: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)

    @property
    def food_link_acc(self) -> float | None:
        return self.food_correct / self.food_checked if self.food_checked else None

    @property
    def unit_link_acc(self) -> float | None:
        return self.unit_correct / self.unit_checked if self.unit_checked else None

    @property
    def wrong_link_rate(self) -> float | None:
        checked = self.food_checked + self.unit_checked
        return (self.food_wrong + self.unit_wrong) / checked if checked else None

    @property
    def new_food_rate(self) -> float | None:
        return self.food_unlinked / self.food_checked if self.food_checked else None


def _singular(word: str) -> str:
    for suffix in ("ies", "es", "s"):
        if word.endswith(suffix) and len(word) > len(suffix) + 2:
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def same_food(expected: str, actual: str | None) -> bool:
    a, b = normalize_text(expected), normalize_text(actual)
    if not a or not b:
        return False
    if " ".join(map(_singular, a.split())) == " ".join(map(_singular, b.split())):
        return True
    return fuzz.ratio(a, b) >= LINK_NAME_THRESHOLD


def same_unit(expected: str, actual: str | None) -> bool:
    a, b = canonical_unit(expected), canonical_unit(actual)
    if a or b:
        return a == b
    return same_food(expected, actual)


@dataclass(frozen=True)
class LinkOutcome:
    correct: bool
    wrong: bool
    """Linked to an existing item that isn't the expected one"""
    unlinked: bool


def link_outcome(
    expected: str,
    ref: CardDraftRef | None,
    existing: Any,
    *,
    catalog: bool,
    same: Callable[[str, str | None], bool],
) -> LinkOutcome:
    """
    Whether a line's food (or unit) was linked right. With the group's catalog: the expected food exists, and the
    line is linked to it; or it doesn't, and the line names it unlinked (commit creates it). Without one, by name.
    """
    if ref is None or (ref.id is None and not ref.name.strip()):
        return LinkOutcome(correct=False, wrong=False, unlinked=True)
    if ref.id is not None:
        if existing is not None:
            right = ref.id == getattr(existing, "id", None)
            return LinkOutcome(correct=right, wrong=not right, unlinked=False)
        if catalog:
            return LinkOutcome(correct=False, wrong=True, unlinked=False)
        right = same(expected, ref.name)
        return LinkOutcome(correct=right, wrong=not right, unlinked=False)
    # not linked
    if existing is not None:
        return LinkOutcome(correct=False, wrong=False, unlinked=True)  # commit would duplicate it
    return LinkOutcome(correct=same(expected, ref.name), wrong=False, unlinked=True)


def score_linking(
    expected: ExpectedRecipe, lines: LineMatches, view: RecipeView, catalog: LinkCatalog | None
) -> LinkingScores | None:
    """Linking on the lines read correctly whose fixture says which food or unit they are; None if none do"""
    structured = expected.structured_ingredients
    if not any(line and (line.food or line.unit) for line in structured):
        return None

    scores = LinkingScores()
    for match in lines.matches:
        line = structured[match.expected_index]
        if line is None or not 0 <= match.actual_index < len(view.ingredients):
            continue
        actual = view.ingredients[match.actual_index]
        detail: dict[str, Any] = {"line": line.text}
        if line.food:
            existing = catalog.exact_food(line.food) if catalog else None
            outcome = link_outcome(line.food, actual.food, existing, catalog=catalog is not None, same=same_food)
            scores.food_checked += 1
            scores.food_correct += outcome.correct
            scores.food_wrong += outcome.wrong
            scores.food_unlinked += outcome.unlinked
            detail["food"] = {
                "expected": line.food,
                "actual": actual.food.name if actual.food else None,
                **asdict(outcome),
            }
        if line.unit:
            existing = catalog.exact_unit(line.unit) if catalog else None
            outcome = link_outcome(line.unit, actual.unit, existing, catalog=catalog is not None, same=same_unit)
            scores.unit_checked += 1
            scores.unit_correct += outcome.correct
            scores.unit_wrong += outcome.wrong
            detail["unit"] = {
                "expected": line.unit,
                "actual": actual.unit.name if actual.unit else None,
                **asdict(outcome),
            }
        scores.details.append(detail)
    return scores


# ----------------------------------------------------------------
# Blanks and flag calibration (card pipeline)


def _snake(name: str) -> str:
    """A draft field as flags name it (`performTime`) to the draft's own name (`perform_time`)"""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


SEVERITY_RANK = {CardFlagSeverity.info: 1, CardFlagSeverity.warning: 2, CardFlagSeverity.error: 3}
"""An item's flag score for the AUROC: its highest unresolved flag's severity (0 without one)"""


class FlagIndex:
    """The draft's unresolved flags by the item they're on"""

    def __init__(self, flags: Sequence[CardFlag]) -> None:
        self.highlighted: set[tuple[str, str | None]] = set()
        self.errors: set[tuple[str, str | None]] = set()
        self.severities: dict[tuple[str, str | None], int] = {}
        for flag in flags:
            if flag.resolution is not None:
                continue
            key = (_snake(flag.field), flag.ref or None)
            if flag.severity in HIGHLIGHTED_SEVERITIES:
                self.highlighted.add(key)
            if flag.severity == CardFlagSeverity.error:
                self.errors.add(key)
            self.severities[key] = max(self.severities.get(key, 0), SEVERITY_RANK.get(flag.severity, 0))

    def severity(self, field_name: str, ref: str | None = None) -> int:
        """The highest severity of the item's unresolved flags, or its whole field's (`SEVERITY_RANK`)"""
        return max(self.severities.get((field_name, ref), 0), self.severities.get((field_name, None), 0))

    def flagged(self, field_name: str, ref: str | None = None) -> bool:
        """Whether the item (or its whole field) has an unresolved error or warning, as the review page highlights"""
        return (field_name, ref) in self.highlighted or (field_name, None) in self.highlighted

    def has_error(self, field_name: str, ref: str | None = None) -> bool:
        return (field_name, ref) in self.errors or (field_name, None) in self.errors


@dataclass
class BlankResult:
    field: str
    text: str | None
    kept: bool
    """The extraction left `[blank]` where the card has its gap"""
    safe: bool | None
    """Kept, or flagged with an error, so the reviewer can't miss it. None without flags (import pipeline)."""
    ref: str | None = None
    """The item it was found in"""
    found: str | None = None


def _blank_similarity(field_name: str, wanted: str, text: str) -> float:
    if field_name == "ingredients":
        return _similarity(wanted, text)
    # a step may be split or merged differently from the card
    return max(_step_similarity(wanted, text), _step_similarity(text, wanted))


def _blank_target(blank: ExpectedBlank, view: RecipeView) -> tuple[str | None, list[str]] | None:
    """The draft item the expected blank is in: its ref and texts; None if no item is like it"""
    wanted = normalize_text(blank.text)
    if blank.field == "ingredients":
        candidates = [(i.ref, i.texts) for i in view.ingredients]
    else:
        items = (
            view.steps if blank.field == "steps" else [StepView(text=n, ref=str(i)) for i, n in enumerate(view.notes)]
        )
        candidates = [(step.ref, [step.text]) for step in items]

    best: tuple[float, str | None, list[str]] | None = None
    for ref, texts in candidates:
        similarity = max((_blank_similarity(blank.field, wanted, normalize_text(text)) for text in texts), default=0.0)
        if similarity >= BLANK_MATCH_THRESHOLD and (best is None or similarity > best[0]):
            best = (similarity, ref, texts)
    return (best[1], best[2]) if best else None


def score_blanks(expected: ExpectedRecipe, view: RecipeView, flags: FlagIndex | None) -> list[BlankResult]:
    """Whether each gap the card leaves on purpose came out as `[blank]` (kept), or else flagged with an error"""
    results: list[BlankResult] = []
    for blank in expected.blanks:
        if blank.field in eval_export.LIST_BLANK_FIELDS:
            target = _blank_target(blank, view)
            if target is None:
                missing_safe = False if flags is not None else None
                results.append(BlankResult(field=blank.field, text=blank.text, kept=False, safe=missing_safe))
                continue
            ref, texts = target
            kept = any(BLANK_RE.search(text) for text in texts)
            safe = (kept or flags.has_error(blank.field, ref)) if flags is not None else None
            results.append(
                BlankResult(field=blank.field, text=blank.text, kept=kept, safe=safe, ref=ref, found=texts[0])
            )
        else:
            value = getattr(view.source, blank.field, None)
            value = value if isinstance(value, str) else None
            kept = not (value or "").strip() or bool(BLANK_RE.search(value or ""))
            safe = (kept or flags.has_error(blank.field)) if flags is not None else None
            results.append(BlankResult(field=blank.field, text=blank.text, kept=kept, safe=safe, found=value))
    return results


@dataclass
class CalibrationItem:
    kind: str
    """`name`, `ingredient`, `missing_ingredient`, `step`, a time field or `recipe_yield`"""
    correct: bool
    flagged: bool
    ref: str | None = None
    severity: int = 0
    """Its highest unresolved flag's severity (`SEVERITY_RANK`): the score the AUROC ranks items by"""


@dataclass
class FlagCalibration:
    """Each item of the draft, right or wrong per the scorer and highlighted or not (`HIGHLIGHTED_SEVERITIES`)"""

    items: list[CalibrationItem]
    clean: bool
    """No unresolved error or warning: the card would need one tap"""

    @property
    def wrong(self) -> int:
        return sum(1 for item in self.items if not item.correct)

    @property
    def flagged(self) -> int:
        return sum(1 for item in self.items if item.flagged)

    @property
    def wrong_flagged(self) -> int:
        return sum(1 for item in self.items if item.flagged and not item.correct)

    @property
    def silent_errors(self) -> int:
        """Wrong but not highlighted: what the review page relies on being zero"""
        return self.wrong - self.wrong_flagged

    @property
    def fully_correct(self) -> bool:
        return self.wrong == 0


def calibrate(
    expected: ExpectedRecipe,
    view: RecipeView,
    lines: LineMatches,
    inventions: Sequence[Invention],
    must_not_invent: Mapping[str, Mapping[str, str]],
    blanks: Sequence[BlankResult],
    flags: Sequence[CardFlag],
) -> FlagCalibration:
    index = FlagIndex(flags)
    invented_refs = {(invention.field, invention.ref) for invention in inventions}
    lost_blanks = {(blank.field, blank.ref) for blank in blanks if not blank.kept and blank.ref}
    invented_fields = {name for filled in must_not_invent.values() for name in filled}
    items: list[CalibrationItem] = []

    items.append(
        CalibrationItem(
            kind="name",
            correct=score_name(expected.name, view.name) >= NAME_CORRECT_THRESHOLD,
            flagged=index.flagged("name"),
            severity=index.severity("name"),
        )
    )

    matched = {match.actual_index for match in lines.matches}
    for j, ingredient in enumerate(view.ingredients):
        correct = j in matched and ("ingredients", ingredient.ref) not in lost_blanks
        items.append(
            CalibrationItem(
                kind="ingredient",
                correct=correct,
                flagged=index.flagged("ingredients", ingredient.ref),
                ref=ingredient.ref,
                severity=index.severity("ingredients", ingredient.ref),
            )
        )
    items.extend(
        CalibrationItem(
            kind="missing_ingredient",
            correct=False,
            flagged=index.flagged("ingredients"),
            severity=index.severity("ingredients"),
        )
        for _ in lines.missing
    )

    expected_steps = [normalize_text(step) for step in expected.instructions if normalize_text(step)]
    if expected_steps:
        for step in view.steps:
            text = normalize_text(step.text)
            similar = bool(text) and any(
                max(_step_similarity(e, text), _step_similarity(text, e)) >= INSTRUCTION_MATCH_THRESHOLD
                for e in expected_steps
            )
            correct = similar and ("steps", step.ref) not in invented_refs and ("steps", step.ref) not in lost_blanks
            items.append(
                CalibrationItem(
                    kind="step",
                    correct=correct,
                    flagged=index.flagged("steps", step.ref),
                    ref=step.ref,
                    severity=index.severity("steps", step.ref),
                )
            )

    times = _expected_times(expected)
    for name in PER_CARD_TIMES:
        wanted, value = times.get(name), view.times.get(name)
        if wanted:
            correct = _same_numbers(wanted, value)
        elif value and strip_markers(value):
            correct = (name, None) not in invented_refs and name not in invented_fields
        else:
            continue
        items.append(
            CalibrationItem(kind=name, correct=correct, flagged=index.flagged(name), severity=index.severity(name))
        )

    yield_flagged = index.flagged("recipe_yield") or index.flagged("recipe_servings")
    yield_severity = max(index.severity("recipe_yield"), index.severity("recipe_servings"))
    if expected.recipe_yield:
        items.append(
            CalibrationItem(
                kind="recipe_yield",
                correct=score_yield(expected.recipe_yield, view) == 1.0,
                flagged=yield_flagged,
                severity=yield_severity,
            )
        )
    elif view.recipe_yield or view.yield_numbers:
        correct = ("recipe_yield", None) not in invented_refs and not (invented_fields & set(YIELD_FIELDS))
        items.append(
            CalibrationItem(kind="recipe_yield", correct=correct, flagged=yield_flagged, severity=yield_severity)
        )

    return FlagCalibration(items=items, clean=is_clean(flags))


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
    # Phase 2 columns, outside the overall score so v1 and v2 numbers compare
    attribution: float | None = None
    recipe_yield: float | None = None
    times: float | None = None
    step_inventions: list[Invention] = field(default_factory=list)
    """Numbers in steps, times or yield that aren't on the card"""
    blanks: list[BlankResult] = field(default_factory=list)
    linking: LinkingScores | None = None
    calibration: FlagCalibration | None = None
    """Card pipeline only: the draft's flags against what is actually wrong"""

    @property
    def blanks_kept(self) -> float | None:
        return sum(blank.kept for blank in self.blanks) / len(self.blanks) if self.blanks else None

    @property
    def blanks_safe(self) -> float | None:
        if not self.blanks or any(blank.safe is None for blank in self.blanks):
            return None
        return sum(bool(blank.safe) for blank in self.blanks) / len(self.blanks)


def score_view(
    expected: ExpectedRecipe,
    view: RecipeView,
    *,
    flags: Sequence[CardFlag] | None = None,
    catalog: LinkCatalog | None = None,
    linking: bool = False,
) -> CardScores:
    ingredients = match_lines(expected.ingredient_lines, [ingredient.texts for ingredient in view.ingredients])
    instructions = score_instructions(expected.instructions, [step.text for step in view.steps if step.text])
    description, description_hits = score_description(expected.description_contains, view.description)
    inventions = find_inventions(expected.must_not_invent, view.source)
    no_invention = 1 - len(inventions) / len(expected.must_not_invent) if expected.must_not_invent else None
    name = score_name(expected.name, view.name)

    components: dict[str, float | None] = {
        "name": name,
        "ingredient_recall": ingredients.recall,
        "ingredient_precision": ingredients.precision,
        "instruction_coverage": instructions,
        "description": description,
        "no_invention": no_invention,
    }

    number_inventions = find_number_inventions(view, card_numbers(expected))
    flag_index = FlagIndex(flags) if flags is not None else None
    blanks = score_blanks(expected, view, flag_index)
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
        attribution=score_attribution(expected.attribution, view.attribution),
        recipe_yield=score_yield(expected.recipe_yield, view),
        times=score_times(expected, view),
        step_inventions=number_inventions,
        blanks=blanks,
        linking=score_linking(expected, ingredients, view, catalog) if linking else None,
        calibration=calibrate(expected, view, ingredients, number_inventions, inventions, blanks, flags)
        if flags is not None
        else None,
    )


def score_recipe(expected: ExpectedRecipe, recipe: Recipe) -> CardScores:
    """Scores a recipe from the import workflow (`--pipeline import`)"""
    return score_view(expected, recipe_view(recipe))


def score_card(
    expected: ExpectedRecipe, extraction: CardExtraction, *, catalog: LinkCatalog | None = None
) -> CardScores:
    """
    Scores a card pipeline extraction: `score_recipe`'s components (same weights, so the numbers compare), plus the
    attribution, yield and times, linking against the group's foods and units (`catalog`), invented numbers, blanks
    and how well the flags point at what is wrong.
    """
    view = draft_view(extraction.draft)
    if not view.attribution:
        view.attribution = extraction.extraction.attribution
    return score_view(expected, view, flags=extraction.flags, catalog=catalog, linking=True)


# ================================================================
# Running


@dataclass
class TokenUsage:
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    failures: int = 0


@dataclass
class EvalConfig:
    """One way of reading the cards: a vision provider (then a text provider), or OCR followed by a text provider."""

    label: str
    image_provider: AIProviderOut | None
    """The provider that reads the image. None for the OCR path."""
    text_provider: AIProviderOut | None
    """The provider every other step runs on"""
    local: bool = False
    """Every provider it uses runs on the group's network (`is_local_provider`)"""

    @property
    def is_ocr(self) -> bool:
        return self.image_provider is None

    @property
    def read_path(self) -> Literal["image", "ocr"]:
        """How the card pipeline reads the card for this config: one reader only, so a failure is scored as one"""
        return "ocr" if self.is_ocr else "image"

    def describe(self) -> dict[str, Any]:
        def provider(p: AIProviderOut | None) -> dict[str, Any] | None:
            # never the key, headers or params, which can hold credentials
            return {"id": str(p.id), "name": p.name, "model": p.model, "base_url": p.base_url} if p else None

        return {
            "label": self.label,
            "ocr": self.is_ocr,
            "read_path": self.read_path,
            "local": self.local,
            "image_provider": provider(self.image_provider),
            "text_provider": provider(self.text_provider),
        }


def usage_key(provider: str, slot: AIProviderSlot | str) -> str:
    return f"{provider} [{slot.value if isinstance(slot, AIProviderSlot) else slot}]"


class EvalAIRuntime(AIRuntime):
    """
    Sends every request to the provider under test for its slot, never to the group's fallback routes, under the
    current call policy (`apply_policy`: a local-only card never reaches a cloud provider), and tallies the tokens
    each provider reports using, to put a price on a run. An eval run isn't real usage, so nothing goes in the
    group's usage log.
    """

    service: EvalOpenAIService

    def __init__(self, service: EvalOpenAIService) -> None:
        super().__init__(service)
        self.usage: dict[str, TokenUsage] = {}
        self.usage_by_slot: dict[str, TokenUsage] = {}
        self.models: dict[str, int] = {}

    def candidates(self, slot: AIProviderSlot) -> list[AIProviderOut]:
        if slot is AIProviderSlot.image:
            provider = self.service.image_provider
        elif slot in (AIProviderSlot.default, AIProviderSlot.fast, AIProviderSlot.planner):
            provider = self.service.default_provider
        else:
            provider = None

        if not provider:
            raise OpenAINotEnabledException(f"No {slot.value} provider set")

        return apply_policy(slot, [provider])

    def record_attempt(
        self,
        provider: AIProviderOut,
        *,
        slot: AIProviderSlot,
        feature: str,
        usage: AITokenUsage,
        latency_ms: int,
        error: BaseException | None = None,
        error_type: str | None = None,
    ) -> None:
        for tally in (
            self.usage.setdefault(provider.name, TokenUsage()),
            self.usage_by_slot.setdefault(usage_key(provider.name, slot), TokenUsage()),
        ):
            tally.requests += 1
            tally.prompt_tokens += usage.prompt_tokens
            tally.completion_tokens += usage.completion_tokens
            tally.failures += 1 if error or error_type else 0
        if not error and not error_type:
            model = usage.model or provider.model
            self.models[model] = self.models.get(model, 0) + 1


class EvalOpenAIService(OpenAIService):
    """
    An `OpenAIService` pinned to the providers under test instead of the group's configured ones (see
    `EvalAIRuntime`), whichever API they speak. Records the SHA-256 of every prompt it loads.
    """

    def __init__(
        self, repos: AllRepositories, *, image_provider: AIProviderOut | None, text_provider: AIProviderOut | None
    ) -> None:
        super().__init__(repos)
        self.image_provider = image_provider
        self.default_provider = text_provider
        self.audio_provider = None
        self.prompt_hashes: dict[str, str] = {}

    @cached_property
    def runtime(self) -> EvalAIRuntime:
        return EvalAIRuntime(self)

    @property
    def usage(self) -> dict[str, TokenUsage]:
        """The tokens each provider reported using, by provider name"""
        return self.runtime.usage

    @property
    def usage_by_slot(self) -> dict[str, TokenUsage]:
        """The tokens each provider reported using, by provider and slot"""
        return self.runtime.usage_by_slot

    @property
    def models(self) -> list[str]:
        """The models that answered (a Claude refusal fallback reports its own)"""
        return list(self.runtime.models)

    def _load_prompt_from_file(self, name: str) -> str:
        content = super()._load_prompt_from_file(name)
        self.prompt_hashes[name] = hashlib.sha256(content.encode()).hexdigest()
        return content


def describe_captured(error: CapturedError) -> str:
    """A compiler's failure for the report: the eval runs from the server's shell, so the full error is shown"""
    return f"{error.compiler}: {type(error.error).__name__}: {error.error}"


def workflow_steps(config: EvalConfig, errors: list[CapturedError]) -> list[WorkflowStep]:
    """
    The import workflow's steps (`--pipeline import`), reading the card only with the config's compiler, whose
    errors go in `errors`.
    """

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


def summarize_draft(draft: CardDraft) -> dict[str, Any]:
    return {
        "name": draft.name,
        "description": draft.description,
        "attribution": draft.attribution,
        "recipe_yield": draft.recipe_yield,
        **{field_name: getattr(draft, field_name) for field_name in PER_CARD_TIMES},
        "ingredients": [
            {
                "text": i.original_text or i.display,
                "quantity": i.quantity,
                "unit": i.unit.name if i.unit else None,
                "food": i.food.name if i.food else None,
                "linked": bool(i.food and i.food.id),
            }
            for i in draft.ingredients
        ],
        "instructions": [step.text for step in draft.steps],
        "notes": [note.text for note in draft.notes],
        "tags": [tag.name for tag in draft.tags],
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
    """Progress messages (card pipeline: progress keys) and when, in seconds into the run, they were reported"""
    pipeline: str = "card"
    read_path: str | None = None
    """How the card was read (`ExtractionMeta.read_path`): `image` or `ocr`"""
    refused: bool = False
    """The call policy refused it: a local-only card under a config that isn't local"""
    usage_by_slot: dict[str, TokenUsage] = field(default_factory=dict)
    models: list[str] = field(default_factory=list)
    """The models that answered"""
    prompts: dict[str, str] = field(default_factory=dict)
    """The SHA-256 of each prompt the run used, by name"""
    orient_s: float = 0.0
    """Seconds the orientation probe took (once per card; included in `latency_s`)"""
    rotations: list[int] = field(default_factory=list)
    """How far orientation turned each page"""
    tags: list[str] = field(default_factory=list)
    self_drafted: bool = False
    """The card's expected values were reviewed from this config's own draft, so the run is biased in its favour"""
    flags: list[str] = field(default_factory=list)
    chain_source: str | None = None
    """For a chain row: the config whose result it is"""
    fell_back: bool = False
    """For a chain row: an earlier config failed to read the card itself"""

    @property
    def score(self) -> float:
        return self.scores.overall if self.scores else 0.0


@dataclass
class PreparedCard:
    """A card's pages as intake leaves them: normalized, and turned upright unless `--no-intake-ocr`"""

    pages: list[CardPage]
    orient_s: float = 0.0
    rotations: list[int] = field(default_factory=list)
    probed_rotations: list[int] | None = None
    """How the orientation probe would turn each page (it runs under `--no-intake-ocr` too); None without Tesseract"""
    error: str | None = None

    def copy_to(self, directory: Path) -> list[CardPage]:
        """Copies of the pages for one run, so nothing a run does can reach the next"""
        pages: list[CardPage] = []
        for page in self.pages:
            target = directory / "pages" / str(page.meta.index)
            shutil.copytree(page.dir, target)
            pages.append(CardPage(dir=target, meta=page.meta.model_copy(deep=True)))
        return pages


def normalize_card(card: Card, directory: Path) -> list[CardPage]:
    """The card's images as intake normalizes them (upright by EXIF, metadata-free JPEGs), in `directory`"""
    pages: list[CardPage] = []
    for index, image in enumerate(card.images):
        page_dir = directory / "pages" / str(index)
        page_dir.mkdir(parents=True)
        with open(image, "rb") as raw:
            meta = normalize_page(raw, page_dir, index, original_filename=image.name)
        pages.append(CardPage(dir=page_dir, meta=meta))
    return pages


def orient_pages(pages: Sequence[CardPage]) -> tuple[list[CardPage], float]:
    """The pages turned upright by the production probe (`orient_page`), and the seconds it took"""
    started = time.perf_counter()
    oriented = [CardPage(dir=page.dir, meta=orient_page(page)) for page in pages]
    return oriented, round(time.perf_counter() - started, 3)


def prepare_card(card: Card, directory: Path, *, intake_ocr: bool = True) -> PreparedCard:
    """
    Normalizes the card's images and, with Tesseract and unless `intake_ocr` is off, orients them: once per card,
    since both are deterministic. Under `--no-intake-ocr` the probe still runs on a throwaway copy, to report the
    turns it would have made. Blocking; never raises.
    """
    try:
        pages = normalize_card(card, directory / "card")
    except PageRejected as e:
        return PreparedCard(pages=[], error=f"PageRejected: {e.reason.value}")
    except OSError as e:
        return PreparedCard(pages=[], error=f"{type(e).__name__}: {e}")

    unturned = [page.meta.rotation for page in pages]
    if not ocr_service.is_available():
        return PreparedCard(pages=pages, rotations=unturned)

    if intake_ocr:
        try:
            oriented, orient_s = orient_pages(pages)
        except Exception as e:
            return PreparedCard(pages=[], error=f"Orientation failed: {type(e).__name__}: {e}")
        rotations = [page.meta.rotation for page in oriented]
        return PreparedCard(pages=oriented, orient_s=orient_s, rotations=rotations, probed_rotations=rotations)

    try:
        probe, _ = orient_pages(PreparedCard(pages=pages).copy_to(directory / "probe"))
        probed: list[int] | None = [page.meta.rotation for page in probe]
    except Exception as e:
        logger.warning(f"The orientation probe failed on {card.id}: {type(e).__name__}")
        probed = None
    return PreparedCard(pages=pages, rotations=unturned, probed_rotations=probed)


@dataclass
class EvalSettings:
    """How a run reads the cards, beyond the configs"""

    pipeline: Pipeline = "card"
    cross_read: bool = False
    """`--cross-read`: read each card a second time and compare, whatever the group's setting"""
    intake_ocr: bool = True
    """Orient the pages with Tesseract, as intake does (`--no-intake-ocr` turns it off)"""
    local_only: bool = False
    """`--local-only`: every card is sent to local providers only"""
    group_options: CardPipelineOptions | None = None
    """The group's pipeline options (`options_for_group`); the defaults when None"""
    prices: dict[str, tuple[float, float]] = field(default_factory=dict)

    def options_for(self, config: EvalConfig) -> CardPipelineOptions:
        """The production options with the config's one reader; a cross-read needs an image provider"""
        base = self.group_options or CardPipelineOptions()
        cross_read = (self.cross_read or base.cross_read) and not config.is_ocr
        return base.model_copy(update={"read_path": config.read_path, "cross_read": cross_read})


def is_self_drafted(card: Card, config: EvalConfig) -> bool:
    """Whether the card's expected values were reviewed from a draft by the config's own reader"""
    origin = card.fixture.origin
    drafted = origin.drafted_by if origin else None
    reader = config.image_provider or config.text_provider
    if not drafted or not reader:
        return False
    if drafted.provider and drafted.provider == reader.name:
        return True
    return bool(drafted.model and drafted.model == reader.model)


def _new_result(card: Card, config: EvalConfig, attempt: int, pipeline: Pipeline) -> RunResult:
    return RunResult(
        card=card.id,
        label=config.label,
        attempt=attempt,
        verified_by_owner=card.fixture.verified_by_owner,
        latency_s=0,
        pipeline=pipeline,
        tags=list(card.fixture.tags),
        self_drafted=is_self_drafted(card, config),
    )


def _finish_usage(result: RunResult, ai: EvalOpenAIService, prices: dict[str, tuple[float, float]]) -> None:
    result.usage = ai.usage
    result.usage_by_slot = ai.usage_by_slot
    result.models = ai.models
    result.prompts = dict(sorted(ai.prompt_hashes.items()))
    result.cost_usd = run_cost(ai.usage, prices)


async def run_import_card(
    repos: AllRepositories,
    translator: Translator,
    card: Card,
    config: EvalConfig,
    *,
    attempt: int = 1,
    household: HouseholdInDB | None = None,
    prices: dict[str, tuple[float, float]] | None = None,
    local_only: bool = False,
) -> RunResult:
    """Reads one card with one config through the import workflow. Never raises; the error is on the result."""

    ai = EvalOpenAIService(repos, image_provider=config.image_provider, text_provider=config.text_provider)
    result = _new_result(card, config, attempt, "import")
    start = time.perf_counter()

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
        compile_errors: list[CapturedError] = []
        start = time.perf_counter()
        try:
            with ai_call_policy(local_only=local_only or card.fixture.local_only):
                recipe = (await RecipeImportWorkflow(workflow_steps(config, compile_errors)).run(ctx)).recipe
        except Exception as e:
            # a compiler's error is why there was nothing to build the recipe from, so it's the one to report
            result.error = describe_captured(compile_errors[0]) if compile_errors else f"{type(e).__name__}: {e}"
            result.refused = isinstance(compile_errors[0].error if compile_errors else e, AIProviderLocalOnlyError)
        finally:
            result.latency_s = round(time.perf_counter() - start, 3)

    _finish_usage(result, ai, prices or {})
    if recipe:
        result.scores = score_recipe(card.fixture.expected, recipe)
        result.recipe = summarize_recipe(recipe)

    return result


async def run_card_pipeline(
    repos: AllRepositories,
    translator: Translator,
    card: Card,
    config: EvalConfig,
    prepared: PreparedCard,
    *,
    attempt: int = 1,
    settings: EvalSettings | None = None,
    catalog: LinkCatalog | None = None,
) -> RunResult:
    """
    Reads one prepared card with one config through the production card pipeline (`extract_card`), under the call
    policy (`--local-only`, or the card's `local_only`). Never raises; the error is on the result.
    """
    settings = settings or EvalSettings()
    ai = EvalOpenAIService(repos, image_provider=config.image_provider, text_provider=config.text_provider)
    result = _new_result(card, config, attempt, "card")
    result.orient_s = prepared.orient_s
    result.rotations = list(prepared.rotations)
    if prepared.error:
        result.error = prepared.error
        return result

    start = time.perf_counter()

    async def on_progress(key: str) -> None:
        result.progress.append((round(time.perf_counter() - start, 2), key))

    extraction: CardExtraction | None = None
    with tempfile.TemporaryDirectory() as temp_dir:
        pages = prepared.copy_to(Path(temp_dir))
        start = time.perf_counter()
        try:
            with ai_call_policy(local_only=settings.local_only or card.fixture.local_only):
                extraction = await extract_card(
                    pages,
                    ai=ai,
                    repos=repos,
                    translator=translator,
                    options=settings.options_for(config),
                    on_progress=on_progress,
                )
        except Exception as e:
            result.error = f"{type(e).__name__}: {e}"
            result.refused = isinstance(e, AIProviderLocalOnlyError)
        finally:
            result.latency_s = round(prepared.orient_s + time.perf_counter() - start, 3)

    _finish_usage(result, ai, settings.prices)
    if extraction:
        read_path = extraction.extraction.read_path
        result.read_path = read_path.value if read_path else None
        result.scores = score_card(card.fixture.expected, extraction, catalog=catalog)
        result.recipe = summarize_draft(extraction.draft)
        result.flags = [f"{flag.id} ({flag.severity.value})" for flag in extraction.flags]

    return result


async def run_card(
    repos: AllRepositories,
    translator: Translator,
    card: Card,
    config: EvalConfig,
    *,
    attempt: int = 1,
    household: HouseholdInDB | None = None,
    settings: EvalSettings | None = None,
    prepared: PreparedCard | None = None,
    catalog: LinkCatalog | None = None,
) -> RunResult:
    """
    Reads one card with one config through `settings.pipeline` (the card pipeline by default), preparing its pages
    first when `prepared` isn't given. Never raises; the error is on the result.
    """
    settings = settings or EvalSettings()
    if settings.pipeline == "import":
        return await run_import_card(
            repos,
            translator,
            card,
            config,
            attempt=attempt,
            household=household,
            prices=settings.prices,
            local_only=settings.local_only,
        )

    if prepared is not None:
        return await run_card_pipeline(
            repos, translator, card, config, prepared, attempt=attempt, settings=settings, catalog=catalog
        )
    with tempfile.TemporaryDirectory() as temp_dir:
        prepared = await asyncio.to_thread(prepare_card, card, Path(temp_dir), intake_ocr=settings.intake_ocr)
        return await run_card_pipeline(
            repos, translator, card, config, prepared, attempt=attempt, settings=settings, catalog=catalog
        )


async def run_eval(
    repos: AllRepositories,
    cards: Sequence[Card],
    configs: Sequence[EvalConfig],
    *,
    repeat: int = 1,
    household: HouseholdInDB | None = None,
    prices: dict[str, tuple[float, float]] | None = None,
    settings: EvalSettings | None = None,
    catalog: LinkCatalog | None = None,
) -> tuple[list[RunResult], dict[str, PreparedCard]]:
    """
    Reads every card with every config, `repeat` times, one at a time so latencies don't interfere. Returns the
    results and, for the card pipeline, each card's prepared pages (their orientation).
    """

    settings = settings or EvalSettings(prices=prices or {})
    if prices is not None:
        settings.prices = prices
    translator = get_locale_provider("en-US")
    results: list[RunResult] = []
    prepared_cards: dict[str, PreparedCard] = {}
    with tempfile.TemporaryDirectory() as temp_dir:
        for card in cards:
            prepared: PreparedCard | None = None
            if settings.pipeline == "card":
                prepared = await asyncio.to_thread(
                    prepare_card, card, Path(temp_dir) / card.id, intake_ocr=settings.intake_ocr
                )
                prepared_cards[card.id] = prepared

            for config in configs:
                for attempt in range(1, repeat + 1):
                    result = await run_card(
                        repos,
                        translator,
                        card,
                        config,
                        attempt=attempt,
                        household=household,
                        settings=settings,
                        prepared=prepared,
                        catalog=catalog,
                    )
                    outcome = f"error: {result.error}" if result.error else f"score {result.score:.2f}"
                    logger.info(f"[{config.label}] {card.id} #{attempt}: {outcome} in {result.latency_s:.1f}s")
                    results.append(result)

    return results, prepared_cards


# ================================================================
# Configs


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


def parse_provider_spec(providers: Sequence[AIProviderOut], spec: str) -> tuple[AIProviderOut, AIProviderOut, bool]:
    """
    `VISION[:TEXT]`: the provider that reads the image and the one every other step runs on (the same one when
    TEXT is left out), and whether TEXT was given. A name holding a colon is tried whole first.
    """
    try:
        provider = find_provider(providers, spec)
        return provider, provider, False
    except EvalSetupError:
        if ":" not in spec:
            raise

    for i, char in enumerate(spec):
        if char != ":":
            continue
        try:
            return find_provider(providers, spec[:i].strip()), find_provider(providers, spec[i + 1 :].strip()), True
        except EvalSetupError:
            continue
    raise EvalSetupError(f"--provider '{spec}' isn't a provider, nor VISION:TEXT naming two of this group's providers")


def _is_local(*providers: AIProviderOut | None) -> bool:
    return all(provider is not None and is_local_provider(provider) for provider in providers)


def build_configs(
    repos: AllRepositories,
    provider_specs: Sequence[str],
    *,
    ocr: bool,
    ocr_provider_names: Sequence[str] | str | None = None,
    local_only: bool = False,
) -> list[EvalConfig]:
    """
    The configs to evaluate: the named providers (`VISION[:TEXT]`), or the group's image provider if none are named,
    plus the OCR path if asked for, once per `ocr_provider_names` or on the group's default provider. With
    `local_only`, a config using a provider that isn't local is refused.
    """

    if ocr and not ocr_service.is_available():
        # otherwise every OCR run would fail, only saying that the card couldn't be read
        raise EvalSetupError("OCR isn't available: install Tesseract and leave OCR_ENABLED on, or leave out --ocr")

    providers = repos.group_ai_providers.get_all()
    settings = repos.group_ai_provider_settings.get_one(repos.group_id)

    def configured(provider_id: Any) -> AIProviderOut | None:
        return next((provider for provider in providers if provider.id == provider_id), None) if provider_id else None

    pairs: list[tuple[AIProviderOut, AIProviderOut, bool]] = []
    if provider_specs:
        pairs = [parse_provider_spec(providers, spec) for spec in provider_specs]
    elif image_provider := configured(settings and settings.image_provider_id):
        pairs = [(image_provider, image_provider, False)]
    elif not ocr:
        raise EvalSetupError("The group has no image provider; pass --provider, or --ocr to evaluate only OCR")

    configs: dict[str, EvalConfig] = {}
    for vision, text, mixed in pairs:
        # one provider may be named twice, by name and by id, or in a different case
        label = f"{vision.name}:{text.name}" if mixed and vision.id != text.id else vision.name
        configs.setdefault(
            label, EvalConfig(label=label, image_provider=vision, text_provider=text, local=_is_local(vision, text))
        )

    if ocr:
        names = [ocr_provider_names] if isinstance(ocr_provider_names, str) else list(ocr_provider_names or [])
        text_providers = (
            [find_provider(providers, name) for name in names]
            if names
            else [configured(settings and settings.default_provider_id)]
        )
        for text_provider in text_providers:
            if not text_provider:
                raise EvalSetupError(
                    "OCR needs a text provider: set the group's default provider or pass --ocr-provider"
                )
            label = f"OCR+{text_provider.name}"
            configs.setdefault(
                label,
                EvalConfig(
                    label=label, image_provider=None, text_provider=text_provider, local=_is_local(text_provider)
                ),
            )

    if local_only and (cloud := [config.label for config in configs.values() if not config.local]):
        raise EvalSetupError(
            f"--local-only: {', '.join(cloud)} use(s) a provider that isn't marked as running on your network at a "
            "private address"
        )

    return list(configs.values())


# ================================================================
# Before spending: --dry-run


def _slots(config: EvalConfig) -> list[AIProviderSlot]:
    """The slots a config's runs ask: the image read (and the second reading), the build, and the fast steps"""
    slots = [] if config.is_ocr else [AIProviderSlot.image]
    return [*slots, AIProviderSlot.default, AIProviderSlot.fast]


def dry_run(args: argparse.Namespace) -> tuple[list[str], list[str]]:
    """
    `--dry-run`: everything a run checks before its first provider call, all of it rather than up to the first
    problem, and with no provider called and nothing written: the fixtures, the group and household, the providers
    each config names (and that a local-only run's are local, and Tesseract for OCR), the chains and baseline, each
    config's provider for every slot it asks under the run's call policy, and a price for every provider when any
    `--price` is given. Returns the problems, and notes that don't stop a run.
    """
    problems: list[str] = []
    notes: list[str] = []

    def problem(error: Exception) -> None:
        if str(error) not in problems:
            problems.append(str(error))

    cards: list[Card] = []
    try:
        cards = load_cards(args.cards, args.only_cards)
        check_cards(cards)
    except EvalSetupError as e:
        problem(e)
    if args.reference:
        try:
            load_reference(args.reference)
        except EvalSetupError as e:
            problem(e)

    with session_context() as session:
        group = get_repositories(session, group_id=None, household_id=None).groups.get_by_slug_or_id(args.group)
        if not group:
            problems.append(f"No group '{args.group}'")
            return problems, notes
        household: HouseholdInDB | None = None
        if args.household:
            household = get_repositories(session, group_id=group.id).households.get_by_slug_or_id(args.household)
            if not household:
                problems.append(f"No household '{args.household}' in group '{group.slug}'")
        repos = get_repositories(session, group_id=group.id, household_id=household.id if household else None)

        # each named provider on its own, so every unknown name is listed; the configs from those that exist
        providers = repos.group_ai_providers.get_all()
        specs: list[str] = []
        for spec in args.providers:
            try:
                parse_provider_spec(providers, spec)
                specs.append(spec)
            except EvalSetupError as e:
                problem(e)
        ocr_names: list[str] = []
        for name in args.ocr_providers:
            try:
                find_provider(providers, name)
                ocr_names.append(name)
            except EvalSetupError as e:
                problem(e)

        configs: list[EvalConfig] = []
        # named providers that all failed don't fall back to the group's: their names are the problem to fix first
        if (specs or not args.providers) and (ocr_names or not args.ocr_providers):
            try:
                configs = build_configs(
                    repos,
                    specs,
                    ocr=args.ocr or bool(args.ocr_providers),
                    ocr_provider_names=ocr_names,
                    local_only=args.local_only,
                )
                check_labels(args.chains, args.baseline, configs)
            except EvalSetupError as e:
                problem(e)

        for config in configs:
            ai = EvalOpenAIService(repos, image_provider=config.image_provider, text_provider=config.text_provider)
            for slot in _slots(config):
                try:
                    with ai_call_policy(local_only=args.local_only):
                        ai.runtime.candidates(slot)
                except (OpenAINotEnabledException, AIProviderLocalOnlyError) as e:
                    problems.append(f"{config.label}: the {slot.value} slot can't be asked: {e}")

        used = sorted({p.name for config in configs for p in (config.image_provider, config.text_provider) if p})
        prices = dict(args.prices)
        if prices:
            if missing := [name for name in used if name not in prices]:
                problems.append(f"No --price for {', '.join(missing)}: give every provider a price, or none")
            if unused := sorted(set(prices) - set(used)):
                notes.append(f"--price for {', '.join(unused)}, which no config uses")
        elif configs:
            notes.append("No --price given: the report will show no costs")
        if not_local := [config.label for config in configs if not config.local]:
            if local_cards := [card.id for card in cards if card.fixture.local_only]:
                notes.append(
                    f"Local-only cards ({', '.join(local_cards)}) won't be sent to {', '.join(not_local)}: refused"
                )
        session.rollback()  # it only read

    notes.insert(
        0,
        f"Dry run: {len(cards)} card(s), {len(configs)} config(s)"
        + (f" ({', '.join(config.label for config in configs)})" if configs else "")
        + f", {args.repeat} repeat(s); no provider was called",
    )
    return problems, notes


# ================================================================
# Comparing configs: chains and baselines


def check_labels(chains: Sequence[Sequence[str]], baseline: str | None, configs: Sequence[EvalConfig]) -> None:
    """
    Fails before any provider is called when a chain or the baseline names a label that isn't one of the run's
    configs, listing the labels that exist.
    """
    labels = [config.label for config in configs]
    available = ", ".join(labels)
    for chain in chains:
        if unknown := [label for label in chain if label not in labels]:
            raise EvalSetupError(
                f"--chain '{'>'.join(chain)}' names {', '.join(repr(u) for u in unknown)}, which isn't a config of "
                f"this run (available: {available})"
            )
    chain_labels = [">".join(chain) for chain in chains]
    if baseline and baseline not in labels and baseline not in chain_labels:
        raise EvalSetupError(
            f"--baseline '{baseline}' isn't a config or chain of this run (available: {available}"
            f"{', ' if chain_labels else ''}{', '.join(chain_labels)})"
        )


def read_itself(result: RunResult, config: EvalConfig | None) -> bool:
    """Whether the config read the card itself: no error, and (card pipeline) by its own read path"""
    if result.error is not None or result.refused or result.scores is None:
        return False
    if result.pipeline == "card" and config is not None:
        return result.read_path == config.read_path
    return True


def _merge_usage(tallies: Iterable[dict[str, TokenUsage]]) -> dict[str, TokenUsage]:
    merged: dict[str, TokenUsage] = {}
    for usage in tallies:
        for name, tally in usage.items():
            total = merged.setdefault(name, TokenUsage())
            total.requests += tally.requests
            total.prompt_tokens += tally.prompt_tokens
            total.completion_tokens += tally.completion_tokens
            total.failures += tally.failures
    return merged


def chain_results(results: Sequence[RunResult], chain: Sequence[str], configs: Sequence[EvalConfig]) -> list[RunResult]:
    """
    A chain's rows, derived from the results with no extra calls: per card and attempt, the first config that read
    the card itself (by `read_path`), else the last one's failure. Latencies, tokens and costs of the configs tried
    are summed (the orientation probe counted once).
    """
    by_label = {config.label: config for config in configs}
    by_key = {(result.card, result.attempt, result.label): result for result in results}
    keys = [(result.card, result.attempt) for result in results if result.label == chain[0]]
    label = ">".join(chain)

    rows: list[RunResult] = []
    for card, attempt in keys:
        tried: list[RunResult] = []
        chosen: RunResult | None = None
        for name in chain:
            if (result := by_key.get((card, attempt, name))) is None:
                continue
            tried.append(result)
            if read_itself(result, by_label.get(name)):
                chosen = result
                break
        if not tried:
            continue
        chosen = chosen or tried[-1]
        costs = [result.cost_usd for result in tried]
        orient_s = max(result.orient_s for result in tried)
        rows.append(
            dataclasses.replace(
                chosen,
                label=label,
                latency_s=round(orient_s + sum(result.latency_s - result.orient_s for result in tried), 3),
                usage=_merge_usage(result.usage for result in tried),
                usage_by_slot=_merge_usage(result.usage_by_slot for result in tried),
                cost_usd=sum(cost for cost in costs if cost is not None) if all(c is not None for c in costs) else None,
                refused=all(result.refused for result in tried),
                chain_source=chosen.label,
                fell_back=chosen is not tried[0],
            )
        )
    return rows


def card_means(results: Sequence[RunResult], label: str) -> dict[str, float]:
    """Each card's mean score for a config over its attempts (a failed read scores 0; refused runs left out)"""
    scores: dict[str, list[float]] = {}
    for result in results:
        if result.label == label and not result.refused:
            scores.setdefault(result.card, []).append(result.score)
    return {card: statistics.fmean(values) for card, values in scores.items()}


def bootstrap_interval(
    deltas: Sequence[float], *, samples: int = BOOTSTRAP_SAMPLES, seed: int = BOOTSTRAP_SEED
) -> tuple[float, float] | None:
    """A seeded bootstrap 95% interval of the mean of `deltas` (cards resampled with replacement)"""
    if not deltas:
        return None
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(deltas, k=len(deltas))) for _ in range(samples))
    return means[math.floor(0.025 * (samples - 1))], means[math.ceil(0.975 * (samples - 1))]


@dataclass
class Comparison:
    """A config against a baseline, paired by card"""

    label: str
    baseline: str
    cards: int
    wins: int
    ties: int
    losses: int
    mean_delta: float | None
    interval: tuple[float, float] | None
    deltas: dict[str, float] = field(default_factory=dict)

    @property
    def excludes_zero(self) -> bool:
        return self.interval is not None and (self.interval[0] > 0 or self.interval[1] < 0)


def compare(results: Sequence[RunResult], label: str, baseline: str) -> Comparison:
    """Per-card deltas of `label` against `baseline`, win/tie/loss, and the mean delta's bootstrap interval"""
    ours, theirs = card_means(results, label), card_means(results, baseline)
    deltas = {card: round(ours[card] - theirs[card], 6) for card in sorted(ours.keys() & theirs.keys())}
    values = list(deltas.values())
    return Comparison(
        label=label,
        baseline=baseline,
        cards=len(values),
        wins=sum(1 for delta in values if delta > TIE_TOLERANCE),
        ties=sum(1 for delta in values if abs(delta) <= TIE_TOLERANCE),
        losses=sum(1 for delta in values if delta < -TIE_TOLERANCE),
        mean_delta=statistics.fmean(values) if values else None,
        interval=bootstrap_interval(values),
        deltas=deltas,
    )


def rescued_cards(rows: Sequence[RunResult]) -> list[str]:
    """The cards a chain's later config read when the first couldn't"""
    return sorted({row.card for row in rows if row.fell_back and row.scores is not None})


# ================================================================
# Reporting


def _mean(values: Iterable[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return statistics.fmean(present) if present else None


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


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
    local: bool | None = None
    refused: int = 0
    """Runs the call policy refused (a local-only card under a config that isn't local); not counted elsewhere"""
    chain: bool = False
    card_std: float | None = None
    """Mean over cards of the score's standard deviation across repeats"""
    self_drafted: int = 0
    attribution: float | None = None
    recipe_yield: float | None = None
    times: float | None = None
    step_inventions: float | None = None
    """Mean numbers per run in steps, times or yield that aren't on the card"""
    blanks_kept: float | None = None
    blanks_safe: float | None = None
    flag_recall: float | None = None
    """P(flagged | wrong)"""
    flag_precision: float | None = None
    """P(wrong | flagged)"""
    flag_rate: float | None = None
    silent_errors: float | None = None
    """Unflagged errors per card read"""
    clean_precision: float | None = None
    """P(card fully correct | clean)"""
    food_link_acc: float | None = None
    unit_link_acc: float | None = None
    wrong_link_rate: float | None = None
    new_food_rate: float | None = None
    tokens_by_slot: dict[str, float] = field(default_factory=dict)
    """Mean tokens per run, by provider and slot"""
    models: list[str] = field(default_factory=list)
    cards: int = 0
    """Cards read: AUROC and cost per caught error are noisy under `NOISY_UNDER_CARDS`"""
    auroc: float | None = None
    """How well the flags rank wrong items above right ones (`flag_auroc`)"""
    auroc_items: int = 0
    auroc_wrong: int = 0
    """The items the AUROC ranks (its n), and how many of them are wrong"""
    caught: int = 0
    """Wrong items that were highlighted"""
    cost_per_caught: float | None = None
    """Total cost over the errors highlighted, in USD; None without a price for every provider, or nothing caught"""


NOISY_UNDER_CARDS = 50
"""Below this many cards, the AUROC and the cost per caught error move a lot from card to card"""


def flag_auroc(items: Sequence[CalibrationItem]) -> float | None:
    """
    How well the flags rank a card's wrong items above its right ones: the chance that a wrong item's score (its
    highest unresolved flag's severity: none, info, warning or error) is above a right item's, ties counting half (the
    Mann-Whitney rank statistic). 0.5 is no better than chance, 1.0 perfect. None unless there are both.
    """
    wrong = [item.severity for item in items if not item.correct]
    right = Counter(item.severity for item in items if item.correct)
    if not wrong or not right:
        return None
    above = sum(sum(count for score, count in right.items() if score < severity) for severity in wrong)
    ties = sum(right.get(severity, 0) for severity in wrong)
    return (above + 0.5 * ties) / (len(wrong) * right.total())


def _card_std(runs: Sequence[RunResult]) -> float | None:
    by_card: dict[str, list[float]] = {}
    for run in runs:
        by_card.setdefault(run.card, []).append(run.score)
    return _mean(statistics.stdev(scores) for scores in by_card.values() if len(scores) > 1)


def summarize_runs(label: str, model: str, all_runs: Sequence[RunResult], **extra: Any) -> ConfigSummary:
    runs = [run for run in all_runs if not run.refused]
    succeeded = [run for run in runs if run.scores]
    scores = [run.scores for run in succeeded if run.scores]
    latencies = [run.latency_s for run in succeeded]
    tokens = [sum(t.prompt_tokens + t.completion_tokens for t in run.usage.values()) for run in runs]
    calibrations = [s.calibration for s in scores if s.calibration]
    linkings = [s.linking for s in scores if s.linking]
    blanks = [blank for s in scores for blank in s.blanks]
    clean = [c for c in calibrations if c.clean]

    by_slot: dict[str, list[int]] = {}
    for run in runs:
        for key, tally in run.usage_by_slot.items():
            by_slot.setdefault(key, []).append(tally.prompt_tokens + tally.completion_tokens)

    items = [item for calibration in calibrations for item in calibration.items]
    caught = sum(c.wrong_flagged for c in calibrations)
    costs = [run.cost_usd for run in runs]
    priced = [cost for cost in costs if cost is not None]
    total_cost = sum(priced) if costs and len(priced) == len(costs) else None

    return ConfigSummary(
        label=label,
        model=model,
        runs=len(runs),
        errors=len(runs) - len(succeeded),
        score=statistics.fmean(run.score for run in runs) if runs else 0.0,
        recall=_mean(s.ingredient_recall for s in scores),
        precision=_mean(s.ingredient_precision for s in scores),
        misread=_mean(len(s.ingredients.misread) for s in scores),
        instructions=_mean(s.instruction_coverage for s in scores),
        inventions=sum(1 for s in scores if s.inventions),
        latency_s=_mean(latencies),
        latency_p50_s=statistics.median(latencies) if latencies else None,
        tokens=_mean(tokens) if any(tokens) else None,
        cost_usd=_mean(run.cost_usd for run in runs),
        refused=len(all_runs) - len(runs),
        card_std=_card_std(runs),
        self_drafted=sum(1 for run in runs if run.self_drafted),
        attribution=_mean(s.attribution for s in scores),
        recipe_yield=_mean(s.recipe_yield for s in scores),
        times=_mean(s.times for s in scores),
        step_inventions=_mean(len(s.step_inventions) for s in scores) if scores else None,
        blanks_kept=_ratio(sum(b.kept for b in blanks), len(blanks)),
        blanks_safe=_ratio(sum(bool(b.safe) for b in blanks), len(blanks))
        if blanks and all(b.safe is not None for b in blanks)
        else None,
        flag_recall=_ratio(sum(c.wrong_flagged for c in calibrations), sum(c.wrong for c in calibrations)),
        flag_precision=_ratio(sum(c.wrong_flagged for c in calibrations), sum(c.flagged for c in calibrations)),
        flag_rate=_ratio(sum(c.flagged for c in calibrations), sum(len(c.items) for c in calibrations)),
        silent_errors=_mean(c.silent_errors for c in calibrations),
        clean_precision=_ratio(sum(c.fully_correct for c in clean), len(clean)),
        food_link_acc=_ratio(sum(x.food_correct for x in linkings), sum(x.food_checked for x in linkings)),
        unit_link_acc=_ratio(sum(x.unit_correct for x in linkings), sum(x.unit_checked for x in linkings)),
        wrong_link_rate=_ratio(
            sum(x.food_wrong + x.unit_wrong for x in linkings), sum(x.food_checked + x.unit_checked for x in linkings)
        ),
        new_food_rate=_ratio(sum(x.food_unlinked for x in linkings), sum(x.food_checked for x in linkings)),
        tokens_by_slot={key: statistics.fmean(values) for key, values in sorted(by_slot.items())},
        models=sorted({model for run in runs for model in run.models}),
        cards=len({run.card for run in runs}),
        auroc=flag_auroc(items),
        auroc_items=len(items),
        auroc_wrong=sum(1 for item in items if not item.correct),
        caught=caught,
        cost_per_caught=total_cost / caught if total_cost is not None and caught else None,
        **extra,
    )


def summarize(
    results: Sequence[RunResult], configs: Sequence[EvalConfig], chains: Sequence[Sequence[str]] = ()
) -> list[ConfigSummary]:
    """One summary per config, then one per chain (from its derived rows, which `results` must include)"""
    summaries: list[ConfigSummary] = []
    for config in configs:
        runs = [result for result in results if result.label == config.label]
        if not runs:
            continue
        models = [p.model for p in (config.image_provider, config.text_provider) if p]
        summaries.append(summarize_runs(config.label, "+".join(dict.fromkeys(models)), runs, local=config.local))

    by_label = {config.label: config for config in configs}
    for chain in chains:
        label = ">".join(chain)
        runs = [result for result in results if result.label == label]
        if not runs:
            continue
        members = [by_label[name] for name in chain if name in by_label]
        models = [p.model for config in members for p in (config.image_provider, config.text_provider) if p]
        summaries.append(
            summarize_runs(
                label,
                "+".join(dict.fromkeys(models)),
                runs,
                local=all(config.local for config in members),
                chain=True,
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


def _yes_no(value: bool | None) -> str:
    return "-" if value is None else ("yes" if value else "no")


def per_tag_rows(
    results: Sequence[RunResult], labels: Sequence[str], cards: Sequence[Card]
) -> list[tuple[str, str, int, float | None]]:
    """Each tag's mean score per config: (tag, label, cards with the tag, mean score)"""
    rows: list[tuple[str, str, int, float | None]] = []
    for tag in FIXTURE_TAGS:
        tagged = {card.id for card in cards if tag in card.fixture.tags}
        if not tagged:
            continue
        for label in labels:
            runs = [r for r in results if r.label == label and r.card in tagged and not r.refused]
            rows.append((tag, label, len(tagged), _mean(r.score for r in runs)))
    return rows


@dataclass
class WrongTurns:
    upright: int
    """Cards not tagged `sideways` the probe ran on"""
    turned: list[str]
    """Of those, the ones the probe turned"""

    @property
    def passed(self) -> bool:
        return len(self.turned) <= WRONG_TURNS_LIMIT * max(1, math.ceil(self.upright / 20))


def wrong_turns(cards: Sequence[Card], prepared: Mapping[str, PreparedCard]) -> WrongTurns | None:
    """Upright cards the orientation probe turned; None when it didn't run (no Tesseract, or the import pipeline)"""
    upright = [card for card in cards if "sideways" not in card.fixture.tags]
    probed = [card for card in upright if card.id in prepared and prepared[card.id].probed_rotations is not None]
    if not probed:
        return None
    return WrongTurns(
        upright=len(probed),
        turned=[card.id for card in probed if any(prepared[card.id].probed_rotations or [])],
    )


@dataclass
class ReferenceRun:
    """A run from an earlier report (`--reference`), for the ablation rules"""

    card: str
    label: str
    attempt: int
    score: float
    error: str | None
    refused: bool
    silent_errors: int | None
    blanks_safe: float | None
    tags: list[str]


def load_reference(path: Path) -> list[ReferenceRun]:
    """The runs of an earlier JSON report"""
    try:
        report = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise EvalSetupError(f"Can't read the reference report '{path}': {e}") from e

    runs: list[ReferenceRun] = []
    for run in report.get("runs", []):
        scores = run.get("scores") or {}
        calibration = scores.get("calibration") or None
        blanks = scores.get("blanks") or []
        silent = None
        if calibration:
            items = calibration.get("items", [])
            silent = sum(1 for item in items if not item.get("correct") and not item.get("flagged"))
        safe = None
        if blanks and all(blank.get("safe") is not None for blank in blanks):
            safe = sum(1 for blank in blanks if blank.get("safe")) / len(blanks)
        runs.append(
            ReferenceRun(
                card=run.get("card", ""),
                label=run.get("label", ""),
                attempt=run.get("attempt", 1),
                score=scores.get("overall", 0.0) if scores else 0.0,
                error=run.get("error"),
                refused=bool(run.get("refused")),
                silent_errors=silent,
                blanks_safe=safe,
                tags=list(run.get("tags") or []),
            )
        )
    return runs


def _banana_runs(runs: Iterable[Any], label: str) -> list[Any]:
    return [run for run in runs if run.card == BANANA_CARD and run.label == label and not run.refused]


def _run_blanks_safe(run: Any) -> float | None:
    if isinstance(run, ReferenceRun):
        return run.blanks_safe
    return run.scores.blanks_safe if run.scores else 0.0


def banana_target(runs: Iterable[Any], label: str) -> bool | None:
    """The banana card's blank safe (`blanks_safe` 1.0) in 3 of 3 repeats; None without 3 repeats of it"""
    banana = _banana_runs(runs, label)
    if len(banana) < BANANA_REPEATS:
        return None
    return all(_run_blanks_safe(run) == 1.0 for run in banana)


def _silent_per_card(runs: Iterable[Any], label: str) -> float | None:
    values: list[float] = []
    for run in runs:
        if run.label != label or run.refused:
            continue
        if isinstance(run, ReferenceRun):
            if run.silent_errors is not None:
                values.append(run.silent_errors)
        elif run.scores and run.scores.calibration:
            values.append(run.scores.calibration.silent_errors)
    return _mean(values)


@dataclass
class Decisions:
    """The rules of docs/ai/PHASE2.md §11.4, evaluated on this run"""

    targets: list[dict[str, Any]]
    cross_read: list[dict[str, Any]]
    last_resort: list[dict[str, Any]]
    orientation: dict[str, Any]


def _pass(value: bool | None) -> str:
    return "n/a" if value is None else ("pass" if value else "fail")


def decide(
    results: Sequence[RunResult],
    summaries: Sequence[ConfigSummary],
    configs: Sequence[EvalConfig],
    chains: Sequence[Sequence[str]],
    cards: Sequence[Card],
    prepared: Mapping[str, PreparedCard],
    settings: EvalSettings,
    reference: Sequence[ReferenceRun] | None = None,
) -> Decisions:
    targets = []
    for summary in summaries:
        targets.append(
            {
                "label": summary.label,
                "silent_errors": summary.silent_errors,
                "silent_errors_ok": None
                if summary.silent_errors is None
                else summary.silent_errors <= SILENT_ERRORS_TARGET,
                "flag_recall": summary.flag_recall,
                "flag_recall_ok": None if summary.flag_recall is None else summary.flag_recall >= FLAG_RECALL_TARGET,
                "banana_blank_safe": banana_target(results, summary.label),
            }
        )

    cross_read: list[dict[str, Any]] = []
    if settings.cross_read and reference is not None:
        for summary in summaries:
            if not any(run.label == summary.label for run in reference):
                continue
            without, with_ = _silent_per_card(reference, summary.label), summary.silent_errors
            drop = without - with_ if without is not None and with_ is not None else None
            banana_with, banana_without = banana_target(results, summary.label), banana_target(reference, summary.label)
            only_with = bool(banana_with) and banana_without is False
            turn_on = (drop is not None and drop >= CROSS_READ_SILENT_DROP) or only_with
            cross_read.append(
                {
                    "label": summary.label,
                    "silent_without": without,
                    "silent_with": with_,
                    "drop": drop,
                    "banana_safe_only_with": only_with,
                    "turn_on": turn_on,
                }
            )

    by_label = {config.label: config for config in configs}
    last_resort: list[dict[str, Any]] = []
    for chain in chains:
        first, last = by_label.get(chain[0]), by_label.get(chain[-1])
        if not first or not last or first.is_ocr or not last.is_ocr:
            continue
        label = ">".join(chain)
        rows = [result for result in results if result.label == label]
        comparison = compare(results, label, chain[0])
        rescued = rescued_cards(rows)
        last_resort.append(
            {
                "chain": label,
                "local": first.local and last.local,
                "rescued_cards": rescued,
                "mean_delta": comparison.mean_delta,
                "interval": comparison.interval,
                "keep_ocr_fallback": len(rescued) >= D3_RESCUED_CARDS or comparison.excludes_zero,
            }
        )

    turns = wrong_turns(cards, prepared)
    orientation: dict[str, Any] = {
        "intake_ocr": settings.intake_ocr,
        "upright_cards": turns.upright if turns else None,
        "wrong_turns": turns.turned if turns else None,
        "passed": turns.passed if turns else None,
    }
    if not settings.intake_ocr and reference is not None:
        sideways = {card.id for card in cards if "sideways" in card.fixture.tags}
        worth = {}
        for summary in summaries:
            ours = [r.score for r in results if r.label == summary.label and r.card in sideways and not r.refused]
            theirs = [r.score for r in reference if r.label == summary.label and r.card in sideways and not r.refused]
            if ours and theirs:
                worth[summary.label] = statistics.fmean(theirs) - statistics.fmean(ours)
        orientation["worth_on_sideways"] = worth

    return Decisions(targets=targets, cross_read=cross_read, last_resort=last_resort, orientation=orientation)


def format_decisions(decisions: Decisions, settings: EvalSettings) -> str:
    rows = [
        [
            "Config",
            f"Silent/card <= {SILENT_ERRORS_TARGET}",
            f"Flag recall >= {FLAG_RECALL_TARGET}",
            f"Banana blank safe {BANANA_REPEATS}/{BANANA_REPEATS}",
        ]
    ]
    for target in decisions.targets:
        rows.append(
            [
                target["label"],
                f"{_pass(target['silent_errors_ok'])} ({_fmt(target['silent_errors'])})",
                f"{_pass(target['flag_recall_ok'])} ({_fmt(target['flag_recall'])})",
                _pass(target["banana_blank_safe"]),
            ]
        )
    lines = ["Decisions (docs/ai/PHASE2.md §11.4)", "", "Targets for the default config:", format_table(rows)]

    lines.append("")
    if decisions.cross_read:
        for rule in decisions.cross_read:
            verdict = "turn cross-read on by default" if rule["turn_on"] else "leave cross-read off"
            lines.append(
                f"Cross-read, {rule['label']}: silent errors per card {_fmt(rule['silent_without'])} without, "
                f"{_fmt(rule['silent_with'])} with (drop {_fmt(rule['drop'])}, needs {CROSS_READ_SILENT_DROP})"
                f"{'; banana blank safe only with it' if rule['banana_safe_only_with'] else ''}: {verdict}"
            )
    elif settings.cross_read:
        lines.append("Cross-read default: n/a (pass --reference with the JSON of the same run without --cross-read)")
    else:
        lines.append("Cross-read default: n/a (run again with --cross-read --reference <this run's JSON>)")

    if decisions.last_resort:
        for rule in decisions.last_resort:
            interval = rule["interval"]
            span = f"[{interval[0]:+.3f}, {interval[1]:+.3f}]" if interval else "-"
            verdict = "keep the OCR fallback" if rule["keep_ocr_fallback"] else "drop the OCR fallback"
            lines.append(
                f"D3 last-resort reader, {rule['chain']}{' (local)' if rule['local'] else ''}: rescued "
                f"{len(rule['rescued_cards'])} card(s) (needs {D3_RESCUED_CARDS}), mean delta "
                f"{_fmt(rule['mean_delta'], '+.3f')} {span}: {verdict}"
            )
    else:
        lines.append("D3 last-resort reader: n/a (pass --chain 'LocalVision>OCR+LocalText')")

    orientation = decisions.orientation
    if orientation["wrong_turns"] is None:
        lines.append("D3 orientation: kept; wrong turns n/a (needs Tesseract and the card pipeline)")
    else:
        turned = orientation["wrong_turns"]
        lines.append(
            f"D3 orientation: kept; wrong turns {len(turned)} of {orientation['upright_cards']} upright card(s)"
            f"{' (' + ', '.join(turned) + ')' if turned else ''}: "
            f"{'pass' if orientation['passed'] else 'fail, raise ORIENT_MIN_RATIO'}"
        )
    for label, worth in (orientation.get("worth_on_sideways") or {}).items():
        lines.append(f"Orientation is worth {worth:+.3f} score on sideways cards for {label}")

    return "\n".join(lines)


def format_report(
    results: Sequence[RunResult],
    configs: Sequence[EvalConfig],
    cards: Sequence[Card],
    *,
    chains: Sequence[Sequence[str]] = (),
    comparisons: Sequence[Comparison] = (),
    decisions: Decisions | None = None,
    settings: EvalSettings | None = None,
) -> str:
    """Tables per config, each card's mean score per config, per-tag rows, comparisons and the §11.4 rules"""

    settings = settings or EvalSettings()
    summaries = summarize(results, configs, chains)
    summary_rows = [
        [
            *("Config", "Model", "Local", "Runs", "Errors", "Score", "Recall", "Precision", "Misread", "Instr"),
            *("Invented", "Latency", "p50", "Tokens", "Cost/card", "Std"),
        ]
    ]
    for s in summaries:
        summary_rows.append(
            [
                *(s.label, s.model, _yes_no(s.local), str(s.runs), str(s.errors)),
                *(_fmt(s.score), _fmt(s.recall), _fmt(s.precision), _fmt(s.misread, ".1f"), _fmt(s.instructions)),
                str(s.inventions),
                *(_fmt(s.latency_s, ".1f", "s"), _fmt(s.latency_p50_s, ".1f", "s"), _fmt(s.tokens, ",.0f")),
                "-" if s.cost_usd is None else f"${s.cost_usd:.4f}",
                _fmt(s.card_std),
            ]
        )
    sections = [format_table(summary_rows, text_columns=3)]

    if any(s.refused for s in summaries):
        sections.append(
            "Refused (local-only cards kept from providers that aren't local): "
            + ", ".join(f"{s.label} {s.refused}" for s in summaries if s.refused)
        )

    if settings.pipeline == "card":
        card_rows = [
            [
                *("Config", "Silent/card", "FlagRecall", "FlagPrec", "FlagRate", "CleanPrec", "BlanksKept"),
                *("BlanksSafe", "StepInv", "Attrib", "Yield", "Times", "Food", "Unit", "WrongLink", "NewFood"),
            ]
        ]
        for s in summaries:
            card_rows.append(
                [
                    s.label,
                    *(_fmt(s.silent_errors), _fmt(s.flag_recall), _fmt(s.flag_precision), _fmt(s.flag_rate)),
                    *(
                        _fmt(s.clean_precision),
                        _fmt(s.blanks_kept),
                        _fmt(s.blanks_safe),
                        _fmt(s.step_inventions, ".1f"),
                    ),
                    *(_fmt(s.attribution), _fmt(s.recipe_yield), _fmt(s.times), _fmt(s.food_link_acc)),
                    *(_fmt(s.unit_link_acc), _fmt(s.wrong_link_rate), _fmt(s.new_food_rate)),
                ]
            )
        sections.append(f"Flags, blanks and linking:\n{format_table(card_rows)}")

        ranking_rows = [["Config", "Cards", "Items", "Wrong", "AUROC", "Caught", "Cost/caught"]]
        for s in summaries:
            ranking_rows.append(
                [
                    *(s.label, str(s.cards), str(s.auroc_items), str(s.auroc_wrong), _fmt(s.auroc)),
                    str(s.caught),
                    "-" if s.cost_per_caught is None else f"${s.cost_per_caught:.4f}",
                ]
            )
        cards_read = max((s.cards for s in summaries), default=0)
        noisy = (
            f" Under {NOISY_UNDER_CARDS} cards (here {cards_read}) both are noisy: read them as rough."
            if cards_read < NOISY_UNDER_CARDS
            else ""
        )
        sections.append(
            "How well flags rank wrong items first (AUROC over Items, of which Wrong are wrong), and the cost of each "
            f"error flagged (total cost / Caught).{noisy}\n{format_table(ranking_rows)}"
        )

    labels = [s.label for s in summaries]
    card_rows = [["Card", *labels]]
    for card in cards:
        row = [card.id if card.fixture.verified_by_owner else f"{card.id} (unverified)"]
        for label in labels:
            runs = [result for result in results if result.card == card.id and result.label == label]
            if runs and all(result.refused for result in runs):
                row.append("refused")
            elif runs and all(result.error for result in runs):
                row.append("error")
            else:
                mark = "*" if any(result.self_drafted for result in runs) else ""
                row.append(_fmt(_mean(result.score for result in runs if not result.refused)) + mark)
        card_rows.append(row)
    note = (
        "\n* scored against a draft by the same provider, so biased in its favour"
        if any(r.self_drafted for r in results)
        else ""
    )
    sections.append(f"Mean score per card:\n{format_table(card_rows)}{note}")

    if tag_rows := per_tag_rows(results, labels, cards):
        tags = list(dict.fromkeys(tag for tag, *_ in tag_rows))
        counts = {tag: count for tag, _, count, _ in tag_rows}
        values = {(tag, label): score for tag, label, _, score in tag_rows}
        table = [["Tag", "Cards", *labels]]
        for tag in tags:
            table.append([tag, str(counts[tag]), *(_fmt(values.get((tag, label))) for label in labels)])
        sections.append(f"Mean score per tag:\n{format_table(table)}")

    if comparisons:
        table = [["Config", "Baseline", "Cards", "Win", "Tie", "Loss", "Mean delta", "95% interval"]]
        for c in comparisons:
            span = f"[{c.interval[0]:+.3f}, {c.interval[1]:+.3f}]" if c.interval else "-"
            table.append(
                [c.label, c.baseline, str(c.cards), str(c.wins), str(c.ties), str(c.losses)]
                + [_fmt(c.mean_delta, "+.3f"), span]
            )
        sections.append(f"Paired by card (seeded bootstrap, {BOOTSTRAP_SAMPLES} samples):\n{format_table(table, 2)}")

    if decisions is not None and settings.pipeline == "card":
        sections.append(format_decisions(decisions, settings))

    return "\n\n".join(sections) + "\n"


def build_report(
    results: Sequence[RunResult],
    configs: Sequence[EvalConfig],
    cards: Sequence[Card],
    *,
    group: str,
    cards_dir: Path,
    repeat: int,
    settings: EvalSettings | None = None,
    chains: Sequence[Sequence[str]] = (),
    comparisons: Sequence[Comparison] = (),
    decisions: Decisions | None = None,
    prepared: Mapping[str, PreparedCard] | None = None,
) -> dict[str, Any]:
    settings = settings or EvalSettings()
    prompts: dict[str, list[str]] = {}
    for result in results:
        for name, digest in result.prompts.items():
            if digest not in prompts.setdefault(name, []):
                prompts[name].append(digest)

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "mealie_version": __version__,
        "mealie_commit": eval_export.mealie_commit(),
        "group": group,
        "cards_dir": str(cards_dir),
        "pipeline": settings.pipeline,
        "settings": {
            "cross_read": settings.cross_read,
            "intake_ocr": settings.intake_ocr,
            "local_only": settings.local_only,
            "group_options": settings.group_options.model_dump() if settings.group_options else None,
        },
        "cards": [
            {
                "id": card.id,
                "verified_by_owner": card.fixture.verified_by_owner,
                "schema_version": card.fixture.schema_version,
                "tags": card.fixture.tags,
                "local_only": card.fixture.local_only,
                "rotations": prepared[card.id].rotations if prepared and card.id in prepared else None,
                "probed_rotations": prepared[card.id].probed_rotations if prepared and card.id in prepared else None,
            }
            for card in cards
        ],
        "repeat": repeat,
        "thresholds": {
            "ingredient": INGREDIENT_MATCH_THRESHOLD,
            "quantity_tolerance": QUANTITY_TOLERANCE,
            "instruction": INSTRUCTION_MATCH_THRESHOLD,
            "name_correct": NAME_CORRECT_THRESHOLD,
        },
        "weights": SCORE_WEIGHTS,
        "prompts": prompts,
        "configs": [config.describe() for config in configs],
        "chains": [">".join(chain) for chain in chains],
        "summary": [asdict(summary) for summary in summarize(results, configs, chains)],
        "comparisons": [asdict(comparison) for comparison in comparisons],
        "decisions": asdict(decisions) if decisions else None,
        "runs": [asdict(result) for result in results],
    }


# ================================================================
# CLI


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)

    try:
        if args.check:
            run_check(args)
            return
        if args.dry_run:
            problems, notes = dry_run(args)
            sys.stdout.write("".join(f"{note}\n" for note in notes))
            if problems:
                sys.stdout.write("Problems:\n" + "".join(f"- {problem}\n" for problem in problems))
                sys.exit(1)
            sys.stdout.write("No problems found: the run can start.\n")
            return

        cards = load_cards(args.cards, args.only_cards)
        reference = load_reference(args.reference) if args.reference else None
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
            configs = build_configs(
                repos,
                args.providers,
                ocr=args.ocr or bool(args.ocr_providers),
                ocr_provider_names=args.ocr_providers,
                local_only=args.local_only,
            )
            # before any provider is called
            check_labels(args.chains, args.baseline, configs)

            settings = EvalSettings(
                pipeline=args.pipeline,
                cross_read=args.cross_read,
                intake_ocr=args.intake_ocr,
                local_only=args.local_only,
                group_options=options_for_group(session, group.id) if args.pipeline == "card" else None,
                prices=dict(args.prices),
            )
            catalog = IngestMatcher(repos) if args.pipeline == "card" else None

            if unverified := [card.id for card in cards if not card.fixture.verified_by_owner]:
                logger.warning(f"Not yet verified by the owner, so their expected values may be wrong: {unverified}")

            logger.info(f"Evaluating {len(cards)} card(s) with: {', '.join(config.label for config in configs)}")
            results, prepared = asyncio.run(
                run_eval(
                    repos, cards, configs, repeat=args.repeat, household=household, settings=settings, catalog=catalog
                )
            )
    except EvalSetupError as e:
        logger.error(str(e))
        sys.exit(2)

    for chain in args.chains:
        results.extend(chain_results(results, chain, configs))
    labels = [config.label for config in configs] + [">".join(chain) for chain in args.chains]
    comparisons = (
        [compare(results, label, args.baseline) for label in labels if label != args.baseline] if args.baseline else []
    )
    summaries = summarize(results, configs, args.chains)
    decisions = decide(results, summaries, configs, args.chains, cards, prepared, settings, reference)

    report = build_report(
        results,
        configs,
        cards,
        group=group.slug,
        cards_dir=args.cards,
        repeat=args.repeat,
        settings=settings,
        chains=args.chains,
        comparisons=comparisons,
        decisions=decisions,
        prepared=prepared,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=str))

    text = format_report(
        results, configs, cards, chains=args.chains, comparisons=comparisons, decisions=decisions, settings=settings
    )
    sys.stdout.write(f"\n{text}\nFull results: {args.out}\n")


if __name__ == "__main__":
    main()
