"""
`extract_card` through upstream's real import workflow with the card compilers and steps (docs/ai/PHASE2.md §4.1),
against a fake AI at `OpenAIService._get_raw_response`: the banana card, the OCR fallback and when it may not run,
captured compiler errors, the cross-read, organizer suggestions, no writes, the local-only policy and transactions.
"""

import asyncio
import logging
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from mealie.core import exceptions
from mealie.core.config import get_app_dirs
from mealie.db.models.group.ai_routing import AIUsageLog
from mealie.db.models.recipe import Category, IngredientFoodModel, IngredientUnitModel, RecipeModel, Tag, Tool
from mealie.db.models.recipe_ingest import RecipeIngestionJob
from mealie.lang.providers import get_locale_provider
from mealie.repos.repository_recipe_ingest import IngestRepos
from mealie.schema.group.ai_providers import AIProviderSlot
from mealie.schema.openai.compiled_source import OpenAICompiledSource
from mealie.schema.recipe.recipe_category import CategorySave, TagSave
from mealie.schema.recipe.recipe_tool import RecipeToolSave
from mealie.schema.recipe_ingest import (
    CardFlagKind,
    CardFlagSeverity,
    CardFlagSource,
    ExtractionCompilerError,
    IngestReadPath,
    RecipeIngestionSettingsUpdate,
)
from mealie.services import ocr
from mealie.services.ai import runtime as ai_runtime
from mealie.services.ai.errors import AIProviderLocalOnlyError
from mealie.services.ai.ingest.pipeline import CardPipelineOptions, extract_card, options_for_group
from mealie.services.ai.ingest.pipeline.compilers import (
    CapturedError,
    CardImageCompiler,
    CardOCRCompiler,
    capture_errors,
)
from mealie.services.ai.ingest.pipeline.context import CardWorkflowContext
from mealie.services.ai.ingest.pipeline.service import JobAIRuntime, JobOpenAIService, end_transaction
from mealie.services.ai.ingest.pipeline.steps import CardBuildRecipeStep, card_workflow_steps
from mealie.services.ai.policy import ai_call_policy
from mealie.services.openai import OpenAINotEnabledException, OpenAIService
from mealie.services.recipe.import_workflow.exceptions import NoRecipeDataError
from mealie.services.recipe.import_workflow.steps import CompileSourceStep, ResolveOrganizersStep
from tests.unit_tests.services_tests.ai.ingest.pipeline.card_fakes import (
    BANANA_CONTENT,
    BANANA_RECIPE,
    BANANA_TRANSCRIPT,
    BANANA_TRANSCRIPTION,
    Call,
    FakeCardAI,
    banana_answers,
    configure,
    create_provider,
    job_session,
    make_pages,
    provider_failure,
    rate_limited,
    seed_foods_and_units,
)
from tests.utils.fixture_schemas import TestUser

translator = get_locale_provider("en-US")

PROGRESS = "recipe-ingest.progress."


def _vision_and_text(user: TestUser) -> None:
    configure(user, image=create_provider(user, "Vision"), default=create_provider(user, "Text"))


async def _extract(
    user: TestUser,
    pages: list,
    options: CardPipelineOptions | None = None,
    progress: list[str] | None = None,
    session_check: list[Session] | None = None,
):
    async def on_progress(key: str) -> None:
        if progress is not None:
            progress.append(key)

    with job_session(user) as (session, repos):
        if session_check is not None:
            session_check.append(session)
        ai = JobOpenAIService(repos)
        if options is None:
            options = options_for_group(session, repos.group_id)  # type: ignore[arg-type]
        end_transaction(session)
        return await extract_card(
            pages, ai=ai, repos=repos, translator=translator, options=options, on_progress=on_progress
        )


def _flags_by_kind(flags) -> dict[CardFlagKind, list]:
    found: dict[CardFlagKind, list] = {}
    for flag in flags:
        found.setdefault(flag.kind, []).append(flag)
    return found


# ==========================================
# The banana card


@pytest.mark.asyncio
async def test_the_banana_card_through_the_card_workflow(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    user = unique_user_fn_scoped
    _vision_and_text(user)
    seed_foods_and_units(user)
    dessert = user.repos.tags.create(TagSave(name="Dessert", group_id=user.repos.group_id))
    fake = FakeCardAI(banana_answers()).install(monkeypatch)
    pages = make_pages(tmp_path)
    progress: list[str] = []

    result = await _extract(user, pages, progress=progress)

    # one image read, one build, one organizer call; nothing else
    assert fake.schemas() == ["OpenAIRecipeCardTranscription", "OpenAIRecipe", "OpenAIOrganizers"]
    read = fake.calls[0]
    assert (read.provider, read.images) == ("Vision", 1)
    assert "Image 1 (front)" in read.message
    assert BANANA_CONTENT in fake.calls[1].message
    assert progress == [
        f"{PROGRESS}reading-card",
        f"{PROGRESS}structuring",
        f"{PROGRESS}suggesting-organizers",
        f"{PROGRESS}linking-ingredients",
    ]

    draft = result.draft
    assert draft.name == "Banana Mug Cake"
    assert draft.description == "No sugar, gluten free"
    assert [step.text for step in draft.steps] == [step["text"] for step in BANANA_RECIPE["instructions"]]
    assert "[blank]" in draft.steps[1].text
    assert [ingredient.original_text for ingredient in draft.ingredients] == [
        line["text"] for line in BANANA_RECIPE["ingredients"]
    ]

    by_line = {ingredient.original_text: ingredient for ingredient in draft.ingredients}
    oil = by_line["1 T. coconut oil (melted)"]
    assert oil.quantity == 1
    assert oil.unit is not None and oil.unit.id is not None and oil.unit.name == "tablespoon"
    assert oil.food is not None and oil.food.id is not None and oil.food.name == "coconut oil"
    assert oil.display.startswith("1 tablespoon coconut oil")
    salt = by_line["1/4 t. salt"]
    assert salt.quantity == pytest.approx(0.25)
    assert salt.unit is not None and salt.unit.name == "teaspoon"
    flour = by_line["1/3 C. almond flour"]
    assert flour.unit is not None and flour.unit.name == "cup"
    assert flour.food is not None and flour.food.id is None and flour.food.name == "almond flour"

    # only the group's own organizers are suggested, linked by id
    assert [(tag.id, tag.name) for tag in draft.tags] == [(dessert.id, "Dessert")]
    assert draft.categories == [] and draft.tools == []

    flags = _flags_by_kind(result.flags)
    (blank,) = flags[CardFlagKind.blank]
    assert (blank.id, blank.severity, blank.source) == (
        f"blank:steps:{draft.steps[1].id}",
        CardFlagSeverity.error,
        CardFlagSource.marker,
    )
    (unsure,) = flags[CardFlagKind.unsure]
    assert unsure.ref == str(flour.reference_id) and unsure.alternatives == ["1/2 C."]
    assert {flag.params["from"] for flag in flags[CardFlagKind.shorthand_read]} == {"T.", "t.", "t", "C."}
    assert {flag.params["name"] for flag in flags[CardFlagKind.new_food]} >= {"almond flour"}
    for kind in (CardFlagKind.not_on_card, CardFlagKind.marker_dropped, CardFlagKind.read_by_ocr):
        assert kind not in flags

    assert result.transcription == BANANA_CONTENT
    meta = result.extraction
    assert meta.read_path == IngestReadPath.image
    assert (meta.provider, meta.model, meta.language) == ("Vision", "Vision-model", "English")
    assert meta.unsure[0].text == "1/3 C."
    assert meta.cross_read_lines is None and meta.cross_read_failed is False
    assert meta.step_outcomes == {
        "compile-source": "completed",
        "build-recipe": "completed",
        "resolve-organizers": "completed",
    }
    assert {(usage.feature, usage.slot, usage.provider, usage.requests) for usage in meta.usage} == {
        ("OpenAIRecipeCardTranscription", "image", "Vision", 1),
        ("OpenAIRecipe", "default", "Text", 1),
        ("OpenAIOrganizers", "fast", "Text", 1),
    }


@pytest.mark.asyncio
async def test_card_compilers_return_a_plain_compiled_source(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    user = unique_user_fn_scoped
    _vision_and_text(user)
    transcription = {**BANANA_TRANSCRIPTION, "attribution": "From Grandma Jo"}
    fake = FakeCardAI(banana_answers(OpenAIRecipeCardTranscription=transcription)).install(monkeypatch)
    pages = make_pages(tmp_path, 2, ocr_text="Banana Mug Cake\n1 T. coconut oil")
    monkeypatch.setattr(ocr, "is_available", lambda: False)  # the stored OCR text is enough

    with job_session(user) as (session, repos):
        ai = JobOpenAIService(repos)
        for compiler, read_path in ((CardImageCompiler, IngestReadPath.image), (CardOCRCompiler, IngestReadPath.ocr)):
            ctx = CardWorkflowContext.for_card(
                pages, ai=ai, repos=repos, translator=translator, options=CardPipelineOptions(), errors=[]
            )
            assert compiler(ctx).can_compile()
            compiled = await compiler(ctx).compile()

            # the compile step's `_merge` reads `image_url` and `language` off every document
            assert type(compiled) is OpenAICompiledSource
            assert (compiled.contains_recipe, compiled.content, compiled.language) == (True, BANANA_CONTENT, "English")
            assert compiled.image_url is None
            assert ctx.read_path == read_path
            assert ctx.attribution == "Grandma Jo"  # the field is labelled "From" already
            assert [entry.alternatives for entry in ctx.unsure] == [["1/2 C."]]

    image_read, ocr_read = fake.calls
    assert (image_read.provider, image_read.images) == ("Vision", 2)
    assert "Image 1 (front), Image 2 (back)" in image_read.message
    assert (ocr_read.provider, ocr_read.images) == ("Text", 0)
    assert 'Text from Image 1 (front):\n"""\nBanana Mug Cake\n1 T. coconut oil\n"""' in ocr_read.message
    assert "Text from Image 2 (back)" in ocr_read.message


@pytest.mark.asyncio
async def test_markers_survive_the_cleaner(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    user = unique_user_fn_scoped
    _vision_and_text(user)
    recipe = {
        "name": "Mystery Bread",
        "ingredients": [{"text": "2 cups [illegible] flour"}, {"text": "1 tsp salt"}],
        "instructions": [
            {"text": "Bake for [blank] minutes."},
            {"text": "   "},
            {"text": "Cool on a <blank> rack, then slice."},
        ],
        "notes": [{"text": "From the [illegible] cookbook"}],
    }
    FakeCardAI(banana_answers(OpenAIRecipe=recipe)).install(monkeypatch)

    result = await _extract(user, make_pages(tmp_path), CardPipelineOptions(suggest_organizers=False))

    draft = result.draft
    # square brackets survive cleaner.clean; it drops blank steps and strips <angle brackets>
    assert [step.text for step in draft.steps] == ["Bake for [blank] minutes.", "Cool on a rack, then slice."]
    flour = draft.ingredients[0]
    assert (flour.note, flour.original_text, flour.quantity, flour.parse_confidence) == (
        "2 cups [illegible] flour",
        "2 cups [illegible] flour",
        None,
        None,
    )
    assert draft.notes[0].text == "From the [illegible] cookbook"
    targets = {(flag.kind, flag.field, flag.ref) for flag in result.flags}
    assert {
        (CardFlagKind.illegible, "ingredients", str(flour.reference_id)),
        (CardFlagKind.blank, "steps", str(draft.steps[0].id)),
        (CardFlagKind.illegible, "notes", "0"),
    } <= targets


@pytest.mark.asyncio
async def test_the_attribution_s_markers_are_written_as_everywhere_else(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The review page fills, and commit converts, exactly `[illegible]`; the reader may write `[Illegible]`"""
    user = unique_user_fn_scoped
    _vision_and_text(user)
    transcription = {**BANANA_TRANSCRIPTION, "attribution": " From [ Illegible ] "}
    FakeCardAI(banana_answers(OpenAIRecipeCardTranscription=transcription)).install(monkeypatch)

    result = await _extract(user, make_pages(tmp_path), CardPipelineOptions(suggest_organizers=False))

    assert result.draft.attribution == result.extraction.attribution == "[illegible]"
    assert any(flag.kind == CardFlagKind.illegible and flag.field == "attribution" for flag in result.flags)


# ==========================================
# The OCR fallback, and when it may not run


@pytest.mark.asyncio
async def test_the_ocr_path_with_tesseract_patched(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    user = unique_user_fn_scoped
    configure(user, default=create_provider(user, "Text"))  # no image provider
    fake = FakeCardAI(banana_answers()).install(monkeypatch)
    read: list[Path] = []

    def extract_text(path: Path) -> ocr.OCRResult:
        read.append(path)
        return ocr.OCRResult(text="Banana Mug Cake\n1 T. coconut oil", confidence=55.0)

    monkeypatch.setattr(ocr, "is_available", lambda: True)
    monkeypatch.setattr(ocr, "extract_text", extract_text)
    pages = make_pages(tmp_path)
    progress: list[str] = []

    result = await _extract(user, pages, CardPipelineOptions(suggest_organizers=False), progress=progress)

    assert read == [pages[0].page_path]  # the full page, read once
    assert fake.schemas("Text")[0] == "OpenAIRecipeCardTranscription"
    assert "1 T. coconut oil" in fake.calls[0].message
    assert progress[0] == f"{PROGRESS}reading-card-ocr"
    assert result.extraction.read_path == IngestReadPath.ocr
    assert result.extraction.ocr_confidence == 55.0
    (flag,) = [flag for flag in result.flags if flag.kind == CardFlagKind.read_by_ocr]
    assert (flag.field, flag.severity, flag.params) == ("card", CardFlagSeverity.warning, {"confidence": 55})


@pytest.mark.asyncio
async def test_a_rate_limited_image_read_is_never_read_by_ocr(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    user = unique_user_fn_scoped
    _vision_and_text(user)
    fake = FakeCardAI(banana_answers(), failures={"Vision": rate_limited()}).install(monkeypatch)

    def extract_text(path: Path) -> ocr.OCRResult:
        raise AssertionError("a rate-limited card waits; it isn't read by OCR")

    monkeypatch.setattr(ocr, "is_available", lambda: True)
    monkeypatch.setattr(ocr, "extract_text", extract_text)
    compiled: list[str] = []
    real_compile = CardOCRCompiler.compile

    async def spy(self: CardOCRCompiler):
        compiled.append("ocr")
        return await real_compile(self)

    monkeypatch.setattr(CardOCRCompiler, "compile", spy)

    with pytest.raises(exceptions.RateLimitError):
        await _extract(user, make_pages(tmp_path), CardPipelineOptions(suggest_organizers=False))

    assert compiled == []
    assert fake.schemas() == ["OpenAIRecipeCardTranscription"]
    assert fake.calls[0].provider == "Vision"


@pytest.mark.asyncio
async def test_compiler_failures_are_recorded_without_a_traceback(
    unique_user_fn_scoped: TestUser,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    user = unique_user_fn_scoped
    _vision_and_text(user)
    FakeCardAI(banana_answers(), failures={"Vision": provider_failure("SECRET PROVIDER BODY")}).install(monkeypatch)
    monkeypatch.setattr(ocr, "is_available", lambda: False)

    with caplog.at_level(logging.DEBUG):
        result = await _extract(
            user, make_pages(tmp_path, ocr_text="Banana Mug Cake"), CardPipelineOptions(suggest_organizers=False)
        )

    # the image read failed and the OCR fallback read the card
    assert result.extraction.read_path == IngestReadPath.ocr
    assert result.extraction.compiler_errors == [
        ExtractionCompilerError(compiler="CardImageCompiler", error="InternalServerError (HTTP 500)")
    ]
    assert not [record for record in caplog.records if record.exc_info]
    assert not [record for record in caplog.records if "SECRET" in record.getMessage()]


@pytest.mark.asyncio
async def test_when_nothing_reads_the_card_the_most_specific_error_is_raised(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    user = unique_user_fn_scoped
    _vision_and_text(user)
    FakeCardAI(banana_answers(), failures={"Vision": provider_failure(), "Text": rate_limited()}).install(monkeypatch)
    monkeypatch.setattr(ocr, "is_available", lambda: False)
    options = CardPipelineOptions(suggest_organizers=False)

    # the image read failed, then the OCR fallback was rate limited: the runner backs off
    with pytest.raises(exceptions.RateLimitError):
        await _extract(user, make_pages(tmp_path / "a", ocr_text="Banana Mug Cake"), options)

    # a provider error alone is raised as it is, with the provider's error as its cause
    with pytest.raises(Exception, match="OpenAI Request Failed") as failed:
        await _extract(user, make_pages(tmp_path / "b"), options)
    assert getattr(failed.value.__cause__, "status_code", None) == 500


@pytest.mark.asyncio
async def test_no_reader_at_all_is_ai_not_enabled(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    user = unique_user_fn_scoped
    configure(user, default=create_provider(user, "Text"))  # no image provider
    fake = FakeCardAI(banana_answers()).install(monkeypatch)
    monkeypatch.setattr(ocr, "is_available", lambda: False)

    with pytest.raises(OpenAINotEnabledException):
        await _extract(user, make_pages(tmp_path), CardPipelineOptions(suggest_organizers=False))
    assert fake.calls == []


@pytest.mark.asyncio
async def test_a_card_without_a_recipe_is_no_recipe_data(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    user = unique_user_fn_scoped
    _vision_and_text(user)
    nothing = {"contains_recipe": False, "content": ""}
    fake = FakeCardAI(banana_answers(OpenAIRecipeCardTranscription=nothing)).install(monkeypatch)

    with pytest.raises(NoRecipeDataError):
        await _extract(user, make_pages(tmp_path, ocr_text="a shopping list"), CardPipelineOptions())
    # the reader said there's no recipe: that's the answer, not a reason to try OCR
    assert fake.schemas() == ["OpenAIRecipeCardTranscription"]


@pytest.mark.parametrize(
    ("read_path", "expected"),
    [
        ("image_then_ocr", ["CardImageCompiler", "CardOCRCompiler"]),
        ("image", ["CardImageCompiler"]),
        ("ocr", ["CardOCRCompiler"]),
    ],
)
def test_read_path_selects_the_compilers(read_path: Any, expected: list[str]):
    errors: list[CapturedError] = []
    steps = card_workflow_steps(CardPipelineOptions(read_path=read_path), errors)

    compile_step, build_step, organizer_step = steps
    assert isinstance(compile_step, CompileSourceStep)
    assert [compiler.__name__ for compiler in compile_step.compilers] == expected
    assert [compiler.wrapped for compiler in compile_step.compilers] == [  # type: ignore[attr-defined]
        {"CardImageCompiler": CardImageCompiler, "CardOCRCompiler": CardOCRCompiler}[name] for name in expected
    ]
    assert isinstance(build_step, CardBuildRecipeStep)
    assert isinstance(organizer_step, ResolveOrganizersStep)

    without_organizers = card_workflow_steps(CardPipelineOptions(suggest_organizers=False), errors)
    assert len(without_organizers) == 2
    assert not any(type(step).__name__ == "TranslateRecipeStep" for step in steps)


@pytest.mark.asyncio
async def test_a_vision_only_read_path_never_falls_back(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    user = unique_user_fn_scoped
    _vision_and_text(user)
    fake = FakeCardAI(banana_answers(), failures={"Vision": provider_failure()}).install(monkeypatch)

    with pytest.raises(Exception, match="OpenAI Request Failed"):
        await _extract(
            user,
            make_pages(tmp_path, ocr_text="Banana Mug Cake"),
            CardPipelineOptions(read_path="image", suggest_organizers=False),
        )
    assert fake.schemas() == ["OpenAIRecipeCardTranscription"]


@pytest.mark.asyncio
async def test_capture_errors_records_and_returns_none(caplog: pytest.LogCaptureFixture):
    class Failing(CardImageCompiler):
        async def compile(self):
            raise RuntimeError("provider said: SECRET")

    errors: list[CapturedError] = []
    wrapped = capture_errors(Failing, errors)
    ctx: Any = type("Ctx", (), {"input": None})()

    with caplog.at_level(logging.DEBUG):
        assert await wrapped(ctx).compile() is None

    assert wrapped.__name__ == "Failing"
    (captured,) = errors
    assert (captured.compiler, captured.description) == ("Failing", "RuntimeError")
    assert isinstance(captured.error, RuntimeError)
    assert not [record for record in caplog.records if record.exc_info or "SECRET" in record.getMessage()]


# ==========================================
# The cross-read


@pytest.mark.asyncio
async def test_the_cross_read_flags_an_invented_number_and_runs_alongside_on_the_same_service(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    user = unique_user_fn_scoped
    _vision_and_text(user)
    invented = "Microwave in bowl or large mug for 2 minutes or until firm in center."
    transcription = {**BANANA_TRANSCRIPTION, "content": BANANA_CONTENT.replace("[blank]", "2"), "unsure": []}
    recipe = {**BANANA_RECIPE, "instructions": [BANANA_RECIPE["instructions"][0], {"text": invented}]}

    read_started, transcript_started = asyncio.Event(), asyncio.Event()

    async def read(call: Call) -> dict:
        read_started.set()
        # the transcript request is already in flight: the two reads overlap
        await asyncio.wait_for(transcript_started.wait(), 5)
        return transcription

    async def transcribe(call: Call) -> dict:
        transcript_started.set()
        await asyncio.wait_for(read_started.wait(), 5)
        return BANANA_TRANSCRIPT

    fake = FakeCardAI(
        banana_answers(OpenAIRecipeCardTranscription=read, OpenAIRecipe=recipe, OpenAIRecipeCardTranscript=transcribe)
    ).install(monkeypatch)
    progress: list[str] = []

    result = await _extract(
        user, make_pages(tmp_path), CardPipelineOptions(cross_read=True, suggest_organizers=False), progress=progress
    )

    step = result.draft.steps[1]
    assert step.text == invented
    (blank,) = [flag for flag in result.flags if flag.kind == CardFlagKind.blank]
    assert (blank.id, blank.severity, blank.source, blank.params) == (
        f"blank:steps:{step.id}",
        CardFlagSeverity.error,
        CardFlagSource.cross_read,
        {"value": "2", "start": 35, "end": 36},  # the invented "2", where the step has it
    )
    assert result.extraction.cross_read_lines == BANANA_TRANSCRIPT["text"].splitlines()
    assert result.extraction.read_info().cross_read is True

    transcript_call = next(call for call in fake.calls if call.schema == "OpenAIRecipeCardTranscript")
    read_call = fake.calls[0]
    assert transcript_call.provider == "Vision" and transcript_call.images == 1
    assert transcript_call.service is read_call.service  # the same service, so the same policy and session
    assert f"{PROGRESS}cross-reading" not in progress  # it had finished by the time it was needed


@pytest.mark.asyncio
async def test_a_failed_cross_read_is_an_info_flag(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    user = unique_user_fn_scoped
    _vision_and_text(user)
    FakeCardAI(banana_answers(), failures={("Vision", "OpenAIRecipeCardTranscript"): provider_failure()}).install(
        monkeypatch
    )

    with caplog.at_level(logging.DEBUG):
        result = await _extract(
            user, make_pages(tmp_path), CardPipelineOptions(cross_read=True, suggest_organizers=False)
        )

    assert result.extraction.cross_read_failed is True
    assert result.extraction.cross_read_lines is None
    (flag,) = [flag for flag in result.flags if flag.kind == CardFlagKind.cross_read_failed]
    assert (flag.field, flag.severity) == ("card", CardFlagSeverity.info)
    assert not [record for record in caplog.records if record.exc_info or "SECRET" in record.getMessage()]


# ==========================================
# Organizers, and writing nothing


@pytest.mark.asyncio
async def test_no_organizer_call_for_a_group_without_organizers(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    user = unique_user_fn_scoped
    _vision_and_text(user)
    fake = FakeCardAI(banana_answers()).install(monkeypatch)

    with job_session(user) as (session, _):
        options = options_for_group(session, user.repos.group_id)  # type: ignore[arg-type]
    assert options == CardPipelineOptions(cross_read=False, suggest_organizers=False)

    result = await _extract(user, make_pages(tmp_path))

    assert "OpenAIOrganizers" not in fake.schemas()
    assert (result.draft.tags, result.draft.categories, result.draft.tools) == ([], [], [])
    assert "resolve-organizers" not in result.extraction.step_outcomes


def test_options_follow_the_group_settings(unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    group_id = user.repos.group_id
    user.repos.tools.create(RecipeToolSave(name="Microwave", group_id=group_id))

    with job_session(user) as (session, _):
        assert options_for_group(session, group_id) == CardPipelineOptions(cross_read=False, suggest_organizers=True)  # type: ignore[arg-type]
        IngestRepos(session, group_id, None).settings.upsert(  # type: ignore[arg-type]
            RecipeIngestionSettingsUpdate(local_only=False, cross_read=True)
        )
        assert options_for_group(session, group_id).cross_read is True  # type: ignore[arg-type]


def _counts(session: Session) -> dict[str, int]:
    models = [RecipeModel, IngredientFoodModel, IngredientUnitModel, Tag, Category, Tool, RecipeIngestionJob]
    counts = {
        model.__tablename__: session.execute(sa.select(sa.func.count()).select_from(model)).scalar_one()
        for model in models
    }
    session.commit()
    return counts


@pytest.mark.asyncio
async def test_extract_card_writes_nothing(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    user = unique_user_fn_scoped
    _vision_and_text(user)
    seed_foods_and_units(user)
    group_id = user.repos.group_id
    user.repos.tags.create(TagSave(name="Dessert", group_id=group_id))
    user.repos.categories.create(CategorySave(name="Snack", group_id=group_id))
    # the organizer step's suggestions include names the group doesn't have: none of them may be created
    organizers = {"tags": ["Dessert", "Brand New Tag"], "categories": ["Snack", "New Category"], "tools": ["New Tool"]}
    fake = FakeCardAI(banana_answers(OpenAIOrganizers=organizers)).install(monkeypatch)
    recipes_dir = get_app_dirs().RECIPE_DATA_DIR
    recipe_dirs = set(recipes_dir.iterdir()) if recipes_dir.exists() else set()

    with job_session(user) as (session, _):
        before = _counts(session)
        usage_before = session.execute(sa.select(sa.func.count()).select_from(AIUsageLog)).scalar_one()
        session.commit()

    result = await _extract(user, make_pages(tmp_path), CardPipelineOptions(cross_read=True))

    with job_session(user) as (session, _):
        assert _counts(session) == before
        # the usage log is the only thing written: one row per provider attempt
        usage_after = session.execute(sa.select(sa.func.count()).select_from(AIUsageLog)).scalar_one()
        assert usage_after - usage_before == len(fake.calls)
    assert (set(recipes_dir.iterdir()) if recipes_dir.exists() else set()) == recipe_dirs
    assert [tag.name for tag in result.draft.tags] == ["Dessert"]
    assert [category.name for category in result.draft.categories] == ["Snack"]
    assert result.draft.tools == []


# ==========================================
# Privacy and transactions


_in_runtime: ContextVar[bool] = ContextVar("in_runtime", default=False)


@pytest.mark.asyncio
async def test_local_only_fails_closed_through_the_ocr_fallback(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    user = unique_user_fn_scoped
    cloud_vision = create_provider(user, "Cloud vision")
    local_text = create_provider(user, "Ollama", base_url="http://192.168.1.20:11434/v1", runs_locally=True)
    configure(user, image=cloud_vision, default=local_text)
    user.repos.tags.create(TagSave(name="Dessert", group_id=user.repos.group_id))
    fake = FakeCardAI(banana_answers()).install(monkeypatch)
    monkeypatch.setattr(ocr, "is_available", lambda: False)

    candidates: list[AIProviderSlot] = []
    real_candidates = JobAIRuntime.candidates

    def spy_candidates(self: JobAIRuntime, slot: AIProviderSlot):
        candidates.append(slot)
        return real_candidates(self, slot)

    real_runtime_response = ai_runtime.AIRuntime.get_response

    async def runtime_response(self, *args: Any, **kwargs: Any):
        token = _in_runtime.set(True)
        try:
            return await real_runtime_response(self, *args, **kwargs)
        finally:
            _in_runtime.reset(token)

    real_get_response = OpenAIService.get_response
    pipeline_calls: list[str] = []

    async def get_response(self, prompt: str, message: str, **kwargs: Any):
        if kwargs.get("provider") is None:
            pipeline_calls.append(kwargs["response_schema"].__name__)
        else:
            # only the runtime names a provider, after the policy filtered its candidates
            assert _in_runtime.get(), "pipeline code passed provider="
        return await real_get_response(self, prompt, message, **kwargs)

    monkeypatch.setattr(JobAIRuntime, "candidates", spy_candidates)
    monkeypatch.setattr(ai_runtime.AIRuntime, "get_response", runtime_response)
    monkeypatch.setattr(OpenAIService, "get_response", get_response)

    with ai_call_policy(local_only=True):
        result = await _extract(user, make_pages(tmp_path, ocr_text="Banana Mug Cake"), CardPipelineOptions())

    # the cloud image provider was refused, so the card was read by OCR on the local text provider
    assert result.extraction.read_path == IngestReadPath.ocr
    assert {call.provider for call in fake.calls} == {"Ollama"}
    assert candidates == [AIProviderSlot.image, AIProviderSlot.default, AIProviderSlot.default, AIProviderSlot.fast]
    assert pipeline_calls == [
        "OpenAIRecipeCardTranscription",
        "OpenAIRecipeCardTranscription",
        "OpenAIRecipe",
        "OpenAIOrganizers",
    ]
    assert [error.compiler for error in result.extraction.compiler_errors] == ["CardImageCompiler"]

    # with no local text provider either, nothing leaves the server
    configure(user, image=cloud_vision, default=create_provider(user, "Cloud text"))
    fake.calls.clear()
    with ai_call_policy(local_only=True), pytest.raises(AIProviderLocalOnlyError):
        await _extract(user, make_pages(tmp_path / "again", ocr_text="Banana Mug Cake"), CardPipelineOptions())
    assert fake.calls == []


@pytest.mark.asyncio
async def test_no_transaction_is_open_at_any_provider_await(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    user = unique_user_fn_scoped
    seed_foods_and_units(user)
    user.repos.tags.create(TagSave(name="Dessert", group_id=user.repos.group_id))
    first, second = create_provider(user, "First"), create_provider(user, "Second")
    configure(
        user,
        image=create_provider(user, "Vision"),
        default=first,
        routes={AIProviderSlot.default: [second]},
    )
    sessions: list[Session] = []
    checked: list[str] = []

    def no_transaction(call: Call) -> None:
        assert not sessions[0].in_transaction(), f"a transaction was open while {call.provider} answered"
        checked.append(call.provider)

    fake = FakeCardAI(banana_answers(), failures={"First": provider_failure()}, on_call=no_transaction)
    fake.install(monkeypatch)

    result = await _extract(user, make_pages(tmp_path), CardPipelineOptions(cross_read=True), session_check=sessions)

    # provider 1 raised and provider 2 was awaited next, for the build and organizer requests alike
    assert [call.provider for call in fake.calls if call.schema == "OpenAIRecipe"] == ["First", "Second"]
    assert [call.provider for call in fake.calls if call.schema == "OpenAIOrganizers"] == ["First", "Second"]
    assert len(checked) == 2 * len(fake.calls)
    assert result.draft.name == "Banana Mug Cake"
    assert not sessions[0].in_transaction()
