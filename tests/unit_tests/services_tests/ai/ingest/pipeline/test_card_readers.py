"""
The card's readers and what they hand over (docs/ai/PHASE2.md §4, §5), against a fake AI at
`OpenAIService._get_raw_response`: two-sided cards read page by page for a provider that takes one image per request,
the turns the image reader asks for, the attribution kept out of "From" and the description, cards in other languages
parsed by the AI parser, AI parsing of chosen lines, a rebuild from an edited transcription, and the free OCR check of
a printed card's numbers.
"""

from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pydantic
import pytest
import sqlalchemy as sa

from mealie.core import exceptions
from mealie.db.models.group.ai_routing import AIUsageLog
from mealie.lang.providers import get_locale_provider
from mealie.schema.recipe.recipe_ingredient import SaveIngredientUnit
from mealie.schema.recipe_ingest import (
    CardFlagKind,
    CardFlagSource,
    ExtractionMeta,
    IngestReadPath,
    PageOCR,
)
from mealie.services import ocr
from mealie.services.ai.ingest.matching import IngestMatcher
from mealie.services.ai.ingest.pipeline import (
    CardPage,
    CardPipelineOptions,
    IngredientLine,
    extract_card,
    parse_lines,
    rebuild_from_transcription,
)
from mealie.services.ai.ingest.pipeline import compilers as compilers_module
from mealie.services.ai.ingest.pipeline.cardtext import strip_from_prefix
from mealie.services.ai.ingest.pipeline.llm_schemas import OpenAIRecipeCardTranscription
from mealie.services.ai.ingest.pipeline.service import JobOpenAIService, end_transaction
from tests.unit_tests.services_tests.ai.ingest.pipeline.card_fakes import (
    BANANA_RECIPE,
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

NO_ORGANIZERS = CardPipelineOptions(suggest_organizers=False)


@pytest.fixture(autouse=True)
def no_tesseract_and_fresh_providers(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(ocr, "is_available", lambda: False)
    compilers_module.ONE_IMAGE_PROVIDERS.clear()
    compilers_module.MULTI_IMAGE_FAILURES.clear()
    yield
    compilers_module.ONE_IMAGE_PROVIDERS.clear()
    compilers_module.MULTI_IMAGE_FAILURES.clear()


def _vision_and_text(user: TestUser) -> None:
    configure(user, image=create_provider(user, "Vision"), default=create_provider(user, "Text"))


async def _extract(user: TestUser, pages: list[CardPage], options: CardPipelineOptions = NO_ORGANIZERS):
    with job_session(user) as (session, repos):
        ai = JobOpenAIService(repos)
        end_transaction(session)
        return await extract_card(pages, ai=ai, repos=repos, translator=translator, options=options)


def _transcription(**overrides: Any) -> dict[str, Any]:
    return {**BANANA_TRANSCRIPTION, **overrides}


# ==========================================
# Two-sided cards on a provider that takes one image per request

FRONT = "# Banana Mug Cake\n\n- 1 banana\n- 1 T. coconut oil (melted)"
BACK = "Microwave in bowl or large mug for [blank] minutes."


def _one_image_at_a_time(call: Call) -> Any:
    """A local vision model that fails any request with more than one image"""
    if call.images > 1:
        return provider_failure("only one image per request is supported")
    if "Image 1 (front)" in call.message:
        return _transcription(content=FRONT, unsure=[])
    return _transcription(content=BACK, unsure=[], attribution="From Grandma Jo")


def _usage(user: TestUser, feature: str) -> list[bool]:
    with job_session(user) as (session, _):
        rows = session.execute(
            sa.select(AIUsageLog.success)
            .where(AIUsageLog.group_id == user.repos.group_id, AIUsageLog.feature == feature)
            .order_by(AIUsageLog.created_at)
        ).scalars()
        return list(rows)


@pytest.mark.asyncio
async def test_a_two_sided_card_is_read_page_by_page_when_both_pages_fail_together(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    user = unique_user_fn_scoped
    _vision_and_text(user)
    fake = FakeCardAI(banana_answers(OpenAIRecipeCardTranscription=_one_image_at_a_time)).install(monkeypatch)

    result = await _extract(user, make_pages(tmp_path, 2))

    # the card is ready, with both pages' text, joined under words (a number would count as on the card)
    assert result.transcription == f"Front:\n{FRONT}\n\nBack:\n{BACK}"
    assert result.draft.attribution == "Grandma Jo"
    assert [call.images for call in fake.calls if call.schema == "OpenAIRecipeCardTranscription"] == [2, 1, 1]
    # one failed attempt with both images, then one for each page
    assert _usage(user, "OpenAIRecipeCardTranscription") == [False, True, True]
    assert result.extraction.read_path == IngestReadPath.image

    # once is no proof (a passing error), twice in a row is: the card after that is read page by page at once
    fake.calls.clear()
    second = await _extract(user, make_pages(tmp_path / "second", 2))
    assert [call.images for call in fake.calls if call.schema == "OpenAIRecipeCardTranscription"] == [2, 1, 1]
    fake.calls.clear()
    third = await _extract(user, make_pages(tmp_path / "third", 2))
    assert [call.images for call in fake.calls if call.schema == "OpenAIRecipeCardTranscription"] == [1, 1]
    assert second.transcription == third.transcription == result.transcription


def _malformed_answer() -> pydantic.ValidationError:
    """What reading a cut-off answer raises"""
    try:
        OpenAIRecipeCardTranscription.parse_openai_response('{"contains_recipe": true, "content": "1 banana')
    except pydantic.ValidationError as e:
        return e
    raise AssertionError("a cut-off answer was read")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "two_images_fail", "reads"),
    [
        # a passing server error (one the SDK's retries didn't outlast): a card read in one request starts over
        (provider_failure, [True, False, True, False], [[2, 1, 1], [2], [2, 1, 1], [2]]),
        # the model's own trouble (a cut-off answer) never counts, however often
        (_malformed_answer, [True, True, True], [[2, 1, 1], [2, 1, 1], [2, 1, 1]]),
    ],
    ids=["server error", "malformed answer"],
)
async def test_a_provider_that_reads_two_images_isnt_remembered_for_a_passing_failure(
    unique_user_fn_scoped: TestUser,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: Callable[[], BaseException],
    two_images_fail: list[bool],
    reads: list[list[int]],
):
    """
    A two-image request that fails once, read page by page, doesn't make every later two-sided card take two
    requests (`ONE_IMAGE_STRIKES` cards in a row do)
    """
    user = unique_user_fn_scoped
    _vision_and_text(user)
    schedule = iter(two_images_fail)

    def answer(call: Call) -> Any:
        if call.images > 1 and next(schedule):
            return failure()
        return _transcription()

    fake = FakeCardAI(banana_answers(OpenAIRecipeCardTranscription=answer)).install(monkeypatch)
    for index, expected in enumerate(reads):
        fake.calls.clear()
        result = await _extract(user, make_pages(tmp_path / str(index), 2))
        assert [call.images for call in fake.calls if call.schema == "OpenAIRecipeCardTranscription"] == expected
        assert result.extraction.read_path == IngestReadPath.image
    assert compilers_module.ONE_IMAGE_PROVIDERS == set()


@pytest.mark.asyncio
async def test_a_rate_limit_or_a_one_page_card_isnt_read_page_by_page(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    user = unique_user_fn_scoped
    _vision_and_text(user)

    def busy(call: Call) -> Any:
        return rate_limited() if call.images > 1 else _transcription()

    fake = FakeCardAI(banana_answers(OpenAIRecipeCardTranscription=busy)).install(monkeypatch)
    with pytest.raises(exceptions.RateLimitError):
        await _extract(user, make_pages(tmp_path, 2))
    assert [call.images for call in fake.calls] == [2]  # the card waits and is read properly later
    assert compilers_module.ONE_IMAGE_PROVIDERS == set()

    # a provider that fails one page fails it whatever: nothing to split
    fake = FakeCardAI(banana_answers(OpenAIRecipeCardTranscription=provider_failure())).install(monkeypatch)
    with pytest.raises(Exception, match="OpenAI Request Failed"):
        await _extract(user, make_pages(tmp_path / "one", 1), CardPipelineOptions(suggest_organizers=False))
    assert [call.images for call in fake.calls] == [1]


@pytest.mark.asyncio
async def test_the_cross_read_is_read_page_by_page_too(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    user = unique_user_fn_scoped
    _vision_and_text(user)

    def transcript(call: Call) -> Any:
        if call.images > 1:
            return provider_failure()
        return {"contains_recipe": True, "text": "Front line" if "Image 1 (front)" in call.message else "Back line"}

    answers = banana_answers(OpenAIRecipeCardTranscription=_one_image_at_a_time, OpenAIRecipeCardTranscript=transcript)
    FakeCardAI(answers).install(monkeypatch)

    result = await _extract(
        user, make_pages(tmp_path, 2), CardPipelineOptions(cross_read=True, suggest_organizers=False)
    )

    assert result.extraction.cross_read_lines == ["Front line", "Back line"]
    assert result.extraction.cross_read_failed is False


# ==========================================
# How far each page must turn


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rotation", "expected"),
    [([90], {0: 90}), ([270], {0: 270}), ([], {}), ([0], {}), ([45], {})],
)
async def test_the_image_reader_says_how_far_a_page_must_turn(
    unique_user_fn_scoped: TestUser,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    rotation: list[int],
    expected: dict[int, int],
):
    user = unique_user_fn_scoped
    _vision_and_text(user)
    answer = _transcription(rotation_clockwise=rotation) if rotation else _transcription()
    FakeCardAI(banana_answers(OpenAIRecipeCardTranscription=answer)).install(monkeypatch)

    result = await _extract(user, make_pages(tmp_path, 1))

    assert result.rotations == expected  # an odd answer costs the turn, never the reading


@pytest.mark.asyncio
async def test_only_pages_not_yet_oriented_are_turned_by_the_reader(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    user = unique_user_fn_scoped
    _vision_and_text(user)
    front, back = make_pages(tmp_path, 2)
    front.meta = front.meta.model_copy(update={"oriented": True})  # Tesseract or the reviewer settled it
    FakeCardAI(banana_answers(OpenAIRecipeCardTranscription=_transcription(rotation_clockwise=[90, 180]))).install(
        monkeypatch
    )

    result = await _extract(user, [front, back])
    assert result.rotations == {1: 180}

    # read page by page, each page's answer turns that page
    compilers_module.ONE_IMAGE_PROVIDERS.clear()

    def page_by_page(call: Call) -> Any:
        if call.images > 1:
            return provider_failure()
        turn = [90] if "Image 1 (front)" in call.message else [270]
        return _transcription(rotation_clockwise=turn)

    FakeCardAI(banana_answers(OpenAIRecipeCardTranscription=page_by_page)).install(monkeypatch)
    result = await _extract(user, make_pages(tmp_path / "sideways", 2))
    assert result.rotations == {0: 90, 1: 270}


# ==========================================
# The attribution: no "From: From", and not in the description


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("written", "kept"),
    [
        ("From Grandma Jo", "Grandma Jo"),
        ("From: Aunt Mae", "Aunt Mae"),
        ("from Mom", "Mom"),
        ("Aunt May's", "Aunt May's"),
    ],
)
async def test_the_attribution_doesnt_repeat_from(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, written: str, kept: str
):
    """The review page labels the field "From", and commit makes it a note titled "From" """
    user = unique_user_fn_scoped
    _vision_and_text(user)
    FakeCardAI(banana_answers(OpenAIRecipeCardTranscription=_transcription(attribution=written))).install(monkeypatch)

    result = await _extract(user, make_pages(tmp_path))

    assert result.draft.attribution == result.extraction.attribution == kept


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("description", "kept"),
    [
        ("From Grandma Jo", ""),
        ("Grandma Jo", ""),
        ("from grandma jo!", ""),
        ("A family favorite. From Grandma Jo.", "A family favorite."),
        ("From Grandma Jo. A family favorite.", "A family favorite."),
        ("Grandma Jo's favorite cake.", "Grandma Jo's favorite cake."),  # more than the attribution: kept
        ("No sugar, gluten free", "No sugar, gluten free"),
        # the rest as written, line breaks too
        (
            "No sugar, gluten free.\n\nGreat for breakfast!\nKeeps 3 days.",
            "No sugar, gluten free.\n\nGreat for breakfast!\nKeeps 3 days.",
        ),
        ("A family favorite.\n\nGreat warm.\n\nFrom Grandma Jo", "A family favorite.\n\nGreat warm."),
        ("From Grandma Jo.\nA family favorite.\nServes 4.", "A family favorite.\nServes 4."),
    ],
)
async def test_an_attribution_copied_into_the_description_is_removed(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, description: str, kept: str
):
    user = unique_user_fn_scoped
    _vision_and_text(user)
    answers = banana_answers(
        OpenAIRecipeCardTranscription=_transcription(attribution="From Grandma Jo"),
        OpenAIRecipe={**BANANA_RECIPE, "description": description},
    )
    FakeCardAI(answers).install(monkeypatch)

    result = await _extract(user, make_pages(tmp_path))

    assert result.draft.description == kept


@pytest.mark.parametrize(
    ("written", "title", "kept"),
    [
        ("From Grandma Jo", "Van", "Grandma Jo"),
        ("Van: Oma", "Van", "Oma"),
        ("van : Oma", "Van", "Oma"),
        # the job's language's "From" begins names too: only with its colon is it the label's word
        ("Van der Berg", "Van", "Van der Berg"),
        ("van Dijk family", "Van", "van Dijk family"),
        ("De la Torre", "De", "De la Torre"),
        ("Von Trapp", "Von", "Von Trapp"),
        ("Da Silva", "Da", "Da Silva"),
        # English as before
        ("From: Aunt Mae", "From", "Aunt Mae"),
        ("from Mom", "From", "Mom"),
        ("Fromage Family", "From", "Fromage Family"),
    ],
)
def test_a_translated_from_is_taken_off_only_with_its_colon(written: str, title: str, kept: str):
    assert strip_from_prefix(written, title) == kept
    ctx = SimpleNamespace(translator=SimpleNamespace(t=lambda key, *_: title))
    assert compilers_module.attribution_text(cast(Any, ctx), written) == kept


# ==========================================
# Cards in other languages, and AI parsing of chosen lines

GERMAN_CONTENT = "# Rührkuchen\n\n- 200 g Mehl\n- 2 Eier\n\nAlles verrühren und 30 Minuten backen."


def _german(ingredients: Any) -> dict[str, Any]:
    return banana_answers(
        OpenAIRecipeCardTranscription=_transcription(content=GERMAN_CONTENT, language="German", unsure=[]),
        OpenAIRecipe={
            "name": "Rührkuchen",
            "ingredients": [{"text": "200 g Mehl"}, {"text": "2 Eier"}],
            "instructions": [{"text": "Alles verrühren und 30 Minuten backen."}],
        },
        OpenAIIngredients=ingredients,
    )


GERMAN_PARSE = {
    "ingredients": [
        {"quantity": 200, "unit": "g", "food": "Mehl", "note": "", "substitutes": []},
        {"quantity": 2, "unit": None, "food": "Eier", "note": "", "substitutes": []},
    ]
}


def _seed_gram(user: TestUser) -> None:
    user.repos.ingredient_units.create(
        SaveIngredientUnit(name="gram", plural_name="grams", abbreviation="g", group_id=user.repos.group_id)
    )


@pytest.mark.asyncio
async def test_a_card_in_another_language_is_parsed_by_the_ai_parser(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    user = unique_user_fn_scoped
    _vision_and_text(user)
    _seed_gram(user)
    fake = FakeCardAI(_german(GERMAN_PARSE)).install(monkeypatch)

    result = await _extract(user, make_pages(tmp_path))

    flour, eggs = result.draft.ingredients
    assert (flour.quantity, flour.unit and flour.unit.name, flour.food and flour.food.name) == (200, "gram", "Mehl")
    assert flour.unit is not None and flour.unit.id is not None  # linked to the group's unit
    assert (eggs.quantity, eggs.unit, eggs.food and eggs.food.name) == (2, None, "Eier")
    assert [line.original_text for line in result.draft.ingredients] == ["200 g Mehl", "2 Eier"]
    assert all(line.parse_confidence is not None for line in result.draft.ingredients)
    assert CardFlagKind.not_parsed not in {flag.kind for flag in result.flags}

    # through the card's own service, on the fast slot (the default provider here), with the lines as written
    (parse,) = [call for call in fake.calls if call.schema == "OpenAIIngredients"]
    assert parse.provider == "Text" and '["200 g Mehl","2 Eier"]' in parse.message
    assert {(usage.feature, usage.slot) for usage in result.extraction.usage} >= {("OpenAIIngredients", "fast")}


@pytest.mark.asyncio
async def test_a_failed_ai_parse_keeps_the_lines_as_text(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    user = unique_user_fn_scoped
    _vision_and_text(user)
    FakeCardAI(_german(provider_failure("SECRET PARSER BODY"))).install(monkeypatch)

    result = await _extract(user, make_pages(tmp_path))

    assert [(line.note, line.quantity, line.parse_confidence) for line in result.draft.ingredients] == [
        ("200 g Mehl", None, None),
        ("2 Eier", None, None),
    ]
    assert CardFlagKind.not_parsed in {flag.kind for flag in result.flags}
    assert "SECRET PARSER BODY" not in caplog.text


@pytest.mark.asyncio
async def test_chosen_lines_are_parsed_by_the_ai_parser_in_any_language(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    """The review page's "Parse with AI": an English card's lines are prepared as for the NLP parser first"""
    user = unique_user_fn_scoped
    _vision_and_text(user)
    seed_foods_and_units(user)
    answer = {"ingredients": [{"quantity": 1, "unit": "tbsp", "food": "butter", "note": "", "substitutes": []}]}
    fake = FakeCardAI(banana_answers(OpenAIIngredients=answer)).install(monkeypatch)
    line = IngredientLine(text="1 heaping T. butter", title="Cake", reference_id=uuid4())

    with job_session(user) as (_, repos):
        ai = JobOpenAIService(repos)
        (parsed,) = await parse_lines(
            [line], ai=ai, repos=repos, translator=translator, matcher=IngestMatcher(repos), language="English"
        )

    assert (parsed.quantity, parsed.unit and parsed.unit.name, parsed.food and parsed.food.name) == (
        1,
        "tablespoon",
        "butter",
    )
    assert (parsed.note, parsed.title, parsed.original_text) == ("heaping", "Cake", "1 heaping T. butter")
    assert parsed.reference_id == line.reference_id
    assert '["1 tbsp butter"]' in fake.calls[-1].message

    # a failure is the task's to report: nothing comes back unparsed as if it were parsed
    FakeCardAI(banana_answers(OpenAIIngredients=provider_failure())).install(monkeypatch)
    with job_session(user) as (_, repos):
        with pytest.raises(Exception, match="OpenAI Request Failed"):
            await parse_lines(
                [IngredientLine(text="200 g Mehl")],
                ai=JobOpenAIService(repos),
                repos=repos,
                translator=translator,
                matcher=IngestMatcher(repos),
                language="German",
            )


# ==========================================
# Rebuilding from an edited transcription

EDITED = BANANA_TRANSCRIPTION["content"].replace("[blank] minutes", "2 minutes")


@pytest.mark.asyncio
async def test_the_draft_is_rebuilt_from_an_edited_transcription(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    user = unique_user_fn_scoped
    _vision_and_text(user)
    seed_foods_and_units(user)
    recipe = {
        **BANANA_RECIPE,
        "instructions": [
            {"text": "Mash banana and mix ingredients thoroughly."},
            {"text": "Microwave in bowl or large mug for 2 minutes or until firm in center."},
        ],
    }
    fake = FakeCardAI(banana_answers(OpenAIRecipe=recipe)).install(monkeypatch)
    previous = ExtractionMeta(
        read_path=IngestReadPath.image, language="English", attribution="Grandma Jo", provider="Vision", model="v1"
    )

    with job_session(user) as (session, repos):
        ai = JobOpenAIService(repos)
        end_transaction(session)
        result = await rebuild_from_transcription(
            make_pages(tmp_path),
            EDITED,
            ai=ai,
            repos=repos,
            translator=translator,
            options=NO_ORGANIZERS,
            previous=previous,
        )

    # no image is read: only the build step, on the edited text
    assert [call.schema for call in fake.calls] == ["OpenAIRecipe"]
    assert "for 2 minutes" in fake.calls[0].message and "[blank]" not in fake.calls[0].message
    assert result.transcription == EDITED
    assert result.draft.steps[1].text.endswith("for 2 minutes or until firm in center.")
    # the flags are against the edited text: its "2" is on the card now
    assert CardFlagKind.not_on_card not in {flag.kind for flag in result.flags}
    # the card was still read as before; the outcomes say where this draft came from
    meta = result.extraction
    assert (meta.read_path, meta.provider, meta.model, meta.attribution) == (
        IngestReadPath.image,
        "Vision",
        "v1",
        "Grandma Jo",
    )
    assert meta.step_outcomes == {"transcription": "completed", "build-recipe": "completed"}
    assert [usage.feature for usage in meta.usage] == ["OpenAIRecipe"]
    assert [line.food and line.food.name for line in result.draft.ingredients][:2] == ["banana", "coconut oil"]


@pytest.mark.asyncio
async def test_an_empty_transcription_builds_nothing(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from mealie.services.recipe.import_workflow.exceptions import NoRecipeDataError

    user = unique_user_fn_scoped
    _vision_and_text(user)
    fake = FakeCardAI(banana_answers()).install(monkeypatch)

    with job_session(user) as (_, repos), pytest.raises(NoRecipeDataError):
        await rebuild_from_transcription(
            make_pages(tmp_path),
            "  \n ",
            ai=JobOpenAIService(repos),
            repos=repos,
            translator=translator,
            options=NO_ORGANIZERS,
        )
    assert fake.calls == []


# ==========================================
# The OCR check of a printed card's numbers

PRINTED = "# Pound Cake\n\n- 2 c. flour\n- 1 c. butter\n\nBake at 375° for 60 minutes."
PRINTED_OCR = "Pound Cake\n2 c. flour\n1 c. butter\nBake at 350° for 60 minutes."


def _printed(user: TestUser, monkeypatch: pytest.MonkeyPatch) -> None:
    answers = banana_answers(
        OpenAIRecipeCardTranscription=_transcription(content=PRINTED, unsure=[]),
        OpenAIRecipe={
            "name": "Pound Cake",
            "ingredients": [{"text": "2 c. flour"}, {"text": "1 c. butter"}],
            "instructions": [{"text": "Bake at 375° for 60 minutes."}],
        },
    )
    FakeCardAI(answers).install(monkeypatch)


@pytest.mark.asyncio
async def test_a_printed_cards_numbers_are_checked_against_tesseract(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    user = unique_user_fn_scoped
    _vision_and_text(user)
    _printed(user, monkeypatch)
    (page,) = make_pages(tmp_path)
    page.meta = page.meta.model_copy(update={"ocr": PageOCR(text=PRINTED_OCR, confidence=91.0), "oriented": True})

    result = await _extract(user, [page])

    (flag,) = [flag for flag in result.flags if flag.source == CardFlagSource.ocr]
    step = result.draft.steps[0]
    assert (flag.kind, flag.field, flag.ref, flag.id) == (
        CardFlagKind.read_disagreement,
        "steps",
        str(step.id),
        f"read_disagreement:steps:{step.id}#ocr",
    )
    assert flag.params == {"text": "Bake at 350° for 60 minutes.", "value": "375", "read": "350", "start": 8, "end": 11}
    assert flag.alternatives == ["Bake at 350° for 60 minutes."]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("confidence", "text"),
    [(49.0, PRINTED_OCR), (91.0, "Pound Cake")],  # handwriting, and a corner of the card
)
async def test_no_ocr_check_without_a_clear_printed_reading(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, confidence: float, text: str
):
    user = unique_user_fn_scoped
    _vision_and_text(user)
    _printed(user, monkeypatch)
    (page,) = make_pages(tmp_path)
    page.meta = page.meta.model_copy(update={"ocr": PageOCR(text=text, confidence=confidence), "oriented": True})

    result = await _extract(user, [page])

    assert [flag for flag in result.flags if flag.source == CardFlagSource.ocr] == []
