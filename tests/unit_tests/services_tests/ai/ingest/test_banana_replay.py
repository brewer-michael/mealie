"""
The banana card's recorded provider answers, replayed through the eval's production card pipeline (docs/ai/PHASE2.md
§11, §18; docs/ai/EVAL.md): `run_eval` normalizes the fixture's photo as intake does, runs the real `extract_card` with
`EvalOpenAIService` behind the real OpenAI client (only the HTTP transport is replaced: it answers each request with
the recorded chat completion for its response schema), and scores the draft with `score_card` against golden scores.

With the cross-read on, a read that invents a number in the card's blank is caught by the second reading; without it
the invention is silent. An eval run, cross-read included, changes no foods, units, recipes, organizers, jobs or usage
rows, and asks only the providers it pins.

Tesseract's orientation probe (the eval's step 0) is off, since a replay doesn't depend on which way the photo is
turned, except in one test that runs it where Tesseract is installed: the sideways photo is turned and scores the same.
The probe itself has its own tests (`pipeline/test_card_orient.py`).
"""

import asyncio
import json
import shutil
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
import openai
import pytest
import sqlalchemy as sa

from mealie.core.config import get_app_dirs, get_app_settings
from mealie.db.db_setup import session_context
from mealie.db.models.group.ai_routing import AIUsageLog
from mealie.db.models.recipe import Category, IngredientFoodModel, IngredientUnitModel, RecipeModel, Tag, Tool
from mealie.db.models.recipe_ingest import RecipeIngestionJob
from mealie.repos.all_repositories import get_repositories
from mealie.schema.group.ai_providers import AIProviderOut, AIProviderSlot
from mealie.schema.recipe.recipe_category import CategorySave, TagSave
from mealie.schema.recipe_ingest import CardFlagKind, CardFlagSeverity, CardFlagSource
from mealie.scripts import eval_recipe_cards as ev
from mealie.services import ocr
from mealie.services.ai.ingest.matching import IngestMatcher
from mealie.services.openai import OpenAIService
from tests.unit_tests.services_tests.ai.ingest.pipeline.card_fakes import (
    BANANA_CONTENT,
    BANANA_ORGANIZERS,
    BANANA_RECIPE,
    BANANA_TRANSCRIPT,
    BANANA_TRANSCRIPTION,
    configure,
    create_provider,
    seed_foods_and_units,
)
from tests.utils.factories import random_string
from tests.utils.fixture_schemas import TestUser

CARDS_DIR = Path(__file__).parents[4] / "data" / "cards"

IS_AVAILABLE = ocr.is_available

# ==========================================
# The recorded answers, as the providers sent them (card_fakes holds their content)

TOKENS: dict[str, tuple[int, int]] = {
    "OpenAIRecipeCardTranscription": (1893, 212),
    "OpenAIRecipe": (1105, 341),
    "OpenAIOrganizers": (618, 38),
    "OpenAIRecipeCardTranscript": (1721, 148),
}
"""The prompt and completion tokens each answer reported"""

FENCED = {"OpenAIRecipe"}
"""Answers the provider wrapped in a Markdown code fence, as some OpenAI-compatible servers do"""

INVENTED_STEP = "Microwave in bowl or large mug for 2 minutes or until firm in center."

INVENTED: dict[str, Any] = {
    "OpenAIRecipeCardTranscription": {
        **BANANA_TRANSCRIPTION,
        "content": BANANA_CONTENT.replace("[blank]", "2"),
        "unsure": [],
    },
    "OpenAIRecipe": {**BANANA_RECIPE, "instructions": [BANANA_RECIPE["instructions"][0], {"text": INVENTED_STEP}]},
}
"""
A read that fills the card's blank with a "2" of its own, which the build step keeps; the second reading (the
transcript) still has `[blank]` there
"""


def recorded_answers(**overrides: Any) -> dict[str, Any]:
    """The recorded answers by response schema name"""
    answers: dict[str, Any] = {
        "OpenAIRecipeCardTranscription": BANANA_TRANSCRIPTION,
        "OpenAIRecipe": BANANA_RECIPE,
        "OpenAIOrganizers": BANANA_ORGANIZERS,
        "OpenAIRecipeCardTranscript": BANANA_TRANSCRIPT,
    }
    answers.update(overrides)
    return answers


def chat_completion(schema: str, answer: Any, model: str) -> dict[str, Any]:
    content = json.dumps(answer)
    if schema in FENCED:
        content = f"```json\n{content}\n```"
    prompt_tokens, completion_tokens = TOKENS[schema]
    return {
        "id": f"chatcmpl-{schema.lower()}",
        "object": "chat.completion",
        "created": 1_759_536_000,
        "model": model,
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


@dataclass
class Request:
    provider: str
    schema: str
    model: str
    images: int
    text: str


@dataclass
class Replay:
    """The providers' HTTP API behind the real OpenAI client: the recorded answer for each request's response schema"""

    answers: dict[str, Any]
    requests: list[Request] = field(default_factory=list)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> Replay:
        replay = self
        get_client = OpenAIService.get_client

        def client(service: OpenAIService, provider: AIProviderOut) -> openai.AsyncOpenAI:
            def handler(request: httpx2.Request) -> httpx2.Response:
                body = json.loads(request.content)
                schema = body["response_format"]["json_schema"]["name"]
                parts = body["messages"][-1]["content"]
                replay.requests.append(
                    Request(
                        provider=provider.name,
                        schema=schema,
                        model=body["model"],
                        images=sum(1 for part in parts if part["type"] == "image_url"),
                        text="\n".join(part["text"] for part in parts if part["type"] == "text"),
                    )
                )
                return httpx2.Response(200, json=chat_completion(schema, replay.answers[schema], body["model"]))

            http_client = openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))
            return get_client(service, provider).with_options(http_client=http_client, max_retries=0)

        monkeypatch.setattr(OpenAIService, "get_client", client)
        return self

    def asked(self) -> list[tuple[str, str]]:
        """Which provider was asked for which schema, sorted (the two readings run concurrently)"""
        return sorted((request.provider, request.schema) for request in self.requests)


# ==========================================
# Running the eval


@dataclass
class Group:
    user: TestUser
    vision: AIProviderOut
    text: AIProviderOut
    fallback: AIProviderOut
    """The group's default route: an eval never falls back to it"""

    @property
    def config(self) -> ev.EvalConfig:
        """`--provider "Vision:Text"`: the card read by one provider, every other step on another"""
        return ev.EvalConfig(
            label=f"{self.vision.name}:{self.text.name}", image_provider=self.vision, text_provider=self.text
        )


@pytest.fixture(autouse=True)
def no_orientation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ocr, "is_available", lambda: False)


@pytest.fixture()
def group(unique_user_fn_scoped: TestUser) -> Iterator[Group]:
    """A group with an image and a default provider, a fallback route, and foods and units to link the lines to"""
    user = unique_user_fn_scoped
    vision = create_provider(user, f"Vision {random_string(6)}")
    text = create_provider(user, f"Text {random_string(6)}")
    fallback = create_provider(user, f"Fallback {random_string(6)}")
    configure(user, image=vision, default=text, routes={AIProviderSlot.default: [fallback]})
    seed_foods_and_units(user)
    yield Group(user=user, vision=vision, text=text, fallback=fallback)


def run_eval(group: Group, *, cross_read: bool = False, repeat: int = 1) -> list[ev.RunResult]:
    """`run_eval` on the banana card, set up as `main` does: the group's options and its foods and units to link to"""
    cards = ev.load_cards(CARDS_DIR, [ev.BANANA_CARD])
    with session_context() as session:
        group_id, household_id = group.user.repos.group_id, group.user.repos.household_id
        repos = get_repositories(session, group_id=group_id, household_id=household_id)
        settings = ev.EvalSettings(cross_read=cross_read, group_options=ev.options_for_group(session, group_id))  # type: ignore[arg-type]
        results, _ = asyncio.run(
            ev.run_eval(repos, cards, [group.config], repeat=repeat, settings=settings, catalog=IngestMatcher(repos))
        )
    return results


def scored(results: list[ev.RunResult]) -> tuple[ev.RunResult, ev.CardScores]:
    (result,) = results
    assert result.error is None, result.error
    assert result.scores is not None
    return result, result.scores


def flags(result: ev.RunResult, prefix: str) -> list[str]:
    return [flag for flag in result.flags if flag.startswith(prefix)]


def step_ref(scores: ev.CardScores, index: int) -> str | None:
    """The id of the draft's step `index`, as the scores refer to it"""
    assert scores.calibration is not None
    return [item.ref for item in scores.calibration.items if item.kind == "step"][index]


@pytest.fixture()
def extractions(monkeypatch: pytest.MonkeyPatch) -> list[ev.CardExtraction]:
    """Every extraction the eval scored, for what its report leaves out (flag sources, categories, tools)"""
    recorded: list[ev.CardExtraction] = []
    extract_card = ev.extract_card

    async def recording(*args: Any, **kwargs: Any) -> ev.CardExtraction:
        extraction = await extract_card(*args, **kwargs)
        recorded.append(extraction)
        return extraction

    monkeypatch.setattr(ev, "extract_card", recording)
    return recorded


# ==========================================
# Golden scores


def test_the_recorded_banana_card_scores_its_golden_scores(group: Group, monkeypatch: pytest.MonkeyPatch):
    replay = Replay(recorded_answers()).install(monkeypatch)

    result, scores = scored(run_eval(group))

    # read by the vision provider, built by the text provider; no organizer call (the group has none), no cross-read
    assert replay.asked() == sorted(
        [(group.vision.name, "OpenAIRecipeCardTranscription"), (group.text.name, "OpenAIRecipe")]
    )
    read = next(request for request in replay.requests if request.schema == "OpenAIRecipeCardTranscription")
    assert (read.model, read.images) == (group.vision.model, 1)
    build = next(request for request in replay.requests if request.schema == "OpenAIRecipe")
    assert BANANA_CONTENT in build.text  # the read's transcription, markers included

    assert (result.pipeline, result.read_path, result.label) == ("card", "image", group.config.label)
    assert result.usage_by_slot == {
        ev.usage_key(group.vision.name, AIProviderSlot.image): ev.TokenUsage(1, 1893, 212),
        ev.usage_key(group.text.name, AIProviderSlot.default): ev.TokenUsage(1, 1105, 341),
    }
    assert result.models == [group.vision.model, group.text.model]
    assert result.prompts and all(len(digest) == 64 for digest in result.prompts.values())

    # v1's components, on the same weights
    assert (scores.overall, scores.name, scores.description, scores.no_invention) == (1.0, 1.0, 1.0, 1.0)
    assert (scores.ingredient_recall, scores.ingredient_precision, scores.ingredient_similarity) == (1.0, 1.0, 1.0)
    assert scores.instruction_coverage == 1.0
    assert scores.description_hits == ["sugar", "gluten"]
    assert scores.inventions == {}
    # the card has no attribution, yield or times to score, and no number the card doesn't have
    assert (scores.attribution, scores.recipe_yield, scores.times) == (None, None, None)
    assert scores.step_inventions == []

    # the blank came out as [blank], and is flagged as an error
    assert (scores.blanks_kept, scores.blanks_safe) == (1.0, 1.0)
    assert result.recipe is not None and "[blank]" in result.recipe["instructions"][1]
    assert flags(result, "blank:") == [f"blank:steps:{step_ref(scores, 1)} (error)"]

    # linking: the four foods the group has are linked, and vanilla, almond flour and cinnamon, which it hasn't, are
    # left for the commit to create; every unit is linked
    linking = scores.linking
    assert linking is not None
    assert (linking.food_checked, linking.food_correct, linking.food_wrong, linking.food_unlinked) == (7, 7, 0, 3)
    assert (linking.unit_checked, linking.unit_correct, linking.unit_wrong) == (4, 4, 0)
    assert (linking.food_link_acc, linking.unit_link_acc, linking.wrong_link_rate) == (1.0, 1.0, 0.0)
    assert linking.new_food_rate == pytest.approx(3 / 7)

    # flag calibration: nothing wrong, two items highlighted (the unsure "1/3 C." and the blank), not a clean card
    calibration = scores.calibration
    assert calibration is not None
    expected_items = [("name", True)] + [("ingredient", True)] * 7 + [("step", True)] * 2
    assert [(item.kind, item.correct) for item in calibration.items] == expected_items
    assert (calibration.wrong, calibration.flagged, calibration.silent_errors) == (0, 2, 0)
    assert calibration.fully_correct and not calibration.clean
    assert len(flags(result, "unsure:ingredients:")) == 1
    assert {flag.split(" ")[-1] for flag in flags(result, "shorthand_read:")} == {"(info)"}


def test_the_banana_target_holds_in_three_of_three_repeats(group: Group, monkeypatch: pytest.MonkeyPatch):
    """The release check of §11.4, on the recorded answers: `blanks_safe = 1.0` in 3 of 3 repeats"""
    Replay(recorded_answers()).install(monkeypatch)

    results = run_eval(group, repeat=3)

    assert [(result.attempt, result.error) for result in results] == [(1, None), (2, None), (3, None)]
    assert ev.banana_target(results, group.config.label) is True
    summary = ev.summarize_runs(group.config.label, group.vision.model, results)
    assert (summary.runs, summary.errors) == (3, 0)


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract is not installed")
def test_with_orientation_the_sideways_photo_is_turned_and_scores_the_same(
    group: Group, monkeypatch: pytest.MonkeyPatch
):
    """The eval's step 0 as well (§11.1): Tesseract turns the sideways photo upright before it's read"""
    monkeypatch.setattr(ocr, "is_available", IS_AVAILABLE)
    for key, value in {"OCR_ENABLED": "true", "OCR_LANGUAGES": "eng", "OCR_TIMEOUT": "60"}.items():
        monkeypatch.setenv(key, value)
    get_app_settings.cache_clear()
    replay = Replay(recorded_answers()).install(monkeypatch)
    try:
        result, scores = scored(run_eval(group))
    finally:
        get_app_settings.cache_clear()

    assert result.rotations in ([90], [270])
    assert result.orient_s > 0
    assert replay.asked() == sorted(
        [(group.vision.name, "OpenAIRecipeCardTranscription"), (group.text.name, "OpenAIRecipe")]
    )
    assert (scores.overall, scores.blanks_kept, scores.blanks_safe) == (1.0, 1.0, 1.0)
    assert scores.calibration is not None and scores.calibration.silent_errors == 0


# ==========================================
# The cross-read


def test_with_the_cross_read_an_invented_2_is_flagged(
    group: Group, monkeypatch: pytest.MonkeyPatch, extractions: list[ev.CardExtraction]
):
    replay = Replay(recorded_answers(**INVENTED)).install(monkeypatch)

    result, scores = scored(run_eval(group, cross_read=True))

    # the second reading runs on the image provider too
    assert replay.asked() == sorted(
        [
            (group.vision.name, "OpenAIRecipeCardTranscription"),
            (group.vision.name, "OpenAIRecipeCardTranscript"),
            (group.text.name, "OpenAIRecipe"),
        ]
    )
    assert result.recipe is not None and result.recipe["instructions"][1] == INVENTED_STEP
    invented_step = step_ref(scores, 1)
    # the "2" is on the read's own transcription, but where the second reading has a blank: flagged as a blank
    assert flags(result, "blank:") == [f"blank:steps:{invented_step} (error)"]
    (extraction,) = extractions
    (blank,) = [flag for flag in extraction.flags if flag.kind == CardFlagKind.blank]
    assert (blank.ref, blank.severity, blank.source, blank.params) == (
        invented_step,
        CardFlagSeverity.error,
        CardFlagSource.cross_read,
        {"value": "2"},
    )
    assert extraction.extraction.cross_read_lines == BANANA_TRANSCRIPT["text"].splitlines()

    # scored as an invented number that is flagged: the blank is lost, but safe
    assert [(i.field, i.value, i.ref) for i in scores.step_inventions] == [("steps", 2.0, invented_step)]
    assert (scores.blanks_kept, scores.blanks_safe) == (0.0, 1.0)
    calibration = scores.calibration
    assert calibration is not None
    assert (calibration.wrong, calibration.wrong_flagged, calibration.silent_errors) == (1, 1, 0)


def test_without_the_cross_read_the_invented_2_is_silent(group: Group, monkeypatch: pytest.MonkeyPatch):
    """What the cross-read is for (§11.4's decision): the same invention, unflagged, is a silent error"""
    Replay(recorded_answers(**INVENTED)).install(monkeypatch)

    result, scores = scored(run_eval(group))

    assert flags(result, "blank:") == []
    assert (scores.blanks_kept, scores.blanks_safe) == (0.0, 0.0)
    calibration = scores.calibration
    assert calibration is not None
    assert (calibration.wrong, calibration.silent_errors) == (1, 1)
    assert ev.banana_target([result, result, result], group.config.label) is False


# ==========================================
# Nothing written


def _counts() -> dict[str, int]:
    models = [
        IngredientFoodModel,
        IngredientUnitModel,
        RecipeModel,
        Tag,
        Category,
        Tool,
        RecipeIngestionJob,
        AIUsageLog,
    ]
    with session_context() as session:
        return {
            model.__tablename__: session.execute(sa.select(sa.func.count()).select_from(model)).scalar_one()
            for model in models
        }


def test_an_eval_run_with_the_cross_read_writes_nothing_and_asks_only_the_pinned_providers(
    group: Group, monkeypatch: pytest.MonkeyPatch, extractions: list[ev.CardExtraction]
):
    user = group.user
    # organizers to suggest from, so the organizer step runs too: its other names must not be created
    user.repos.tags.create(TagSave(name="Dessert", group_id=user.repos.group_id))
    user.repos.categories.create(CategorySave(name="Snack", group_id=user.repos.group_id))
    user.repos.session.commit()
    replay = Replay(recorded_answers()).install(monkeypatch)
    recipes_dir = get_app_dirs().RECIPE_DATA_DIR
    recipe_dirs = sorted(recipes_dir.iterdir()) if recipes_dir.exists() else []
    fixture_files = {path.name: path.read_bytes() for path in CARDS_DIR.iterdir()}
    before = _counts()

    results = run_eval(group, cross_read=True, repeat=2)

    assert [result.error for result in results] == [None, None]
    assert _counts() == before
    assert (sorted(recipes_dir.iterdir()) if recipes_dir.exists() else []) == recipe_dirs
    assert {path.name: path.read_bytes() for path in CARDS_DIR.iterdir()} == fixture_files

    # each run: the read and the second reading on the vision provider, the build and the organizers on the text one
    assert replay.asked() == sorted(
        2
        * [
            (group.vision.name, "OpenAIRecipeCardTranscription"),
            (group.vision.name, "OpenAIRecipeCardTranscript"),
            (group.text.name, "OpenAIRecipe"),
            (group.text.name, "OpenAIOrganizers"),
        ]
    )
    assert group.fallback.name not in {request.provider for request in replay.requests}
    # only the group's own organizers are suggested ("Quick" and "Microwave" were not created)
    for extraction in extractions:
        draft = extraction.draft
        assert ([t.name for t in draft.tags], [c.name for c in draft.categories], draft.tools) == (
            ["Dessert"],
            ["Snack"],
            [],
        )
    for result in results:
        assert result.usage_by_slot == {
            ev.usage_key(group.vision.name, AIProviderSlot.image): ev.TokenUsage(2, 1893 + 1721, 212 + 148),
            ev.usage_key(group.text.name, AIProviderSlot.default): ev.TokenUsage(1, 1105, 341),
            ev.usage_key(group.text.name, AIProviderSlot.fast): ev.TokenUsage(1, 618, 38),
        }
