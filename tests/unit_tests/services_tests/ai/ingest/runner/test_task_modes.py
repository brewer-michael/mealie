"""
What an extract task does, by its payload's mode (docs/ai/PHASE2.md §3.1): read the card again (the default), build the
recipe again from the reviewer's edited transcription (`rebuild`), or parse chosen ingredient lines with the AI parser
(`parse_lines`, the review page's "Parse with AI"). The real handlers and finalize, with a fake provider.
"""

import io
from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from ingest_runner_testing import Jobs, run, settle

from mealie.schema.recipe.recipe_ingredient import SaveIngredientFood, SaveIngredientUnit
from mealie.schema.recipe_ingest import (
    CardDraft,
    CardDraftIngredient,
    CardDraftRef,
    CardFlag,
    CardFlagKind,
    CardFlagSeverity,
    CardFlagSource,
    CardProposalKind,
    CardProposalOrigin,
    ExtractionMeta,
    FlagResolution,
    IngestErrorCode,
    IngestReadPath,
    IngestStatus,
    IngestTaskKind,
    IngestTaskState,
)
from mealie.services import ocr
from mealie.services.ai.ingest import images, limits, storage, tasks
from mealie.services.ai.ingest.pipeline.flags import ingredient_hash
from mealie.services.ai.ingest.runner.dispatcher import IngestDispatcher
from tests.unit_tests.services_tests.ai.ingest.pipeline.card_fakes import (
    BANANA_RECIPE,
    BANANA_TRANSCRIPTION,
    FakeCardAI,
    banana_answers,
    card_image,
    configure,
    create_provider,
)
from tests.utils.fixture_schemas import TestUser

EDITED = BANANA_TRANSCRIPTION["content"].replace("[blank] minutes", "2 minutes")
REBUILT = {
    **BANANA_RECIPE,
    "instructions": [
        {"text": "Mash banana and mix ingredients thoroughly."},
        {"text": "Microwave in bowl or large mug for 2 minutes or until firm in center."},
    ],
}


@pytest.fixture(scope="module", autouse=True)
def providers(unique_user: TestUser) -> Iterator[None]:
    """The group's image and text providers, for every test here"""
    configure(unique_user, image=create_provider(unique_user, "Vision"), default=create_provider(unique_user, "Text"))
    yield
    configure(unique_user)


@pytest.fixture(autouse=True)
def no_tesseract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ocr, "is_available", lambda: False)
    monkeypatch.setattr(ocr, "binary_available", lambda: False)


@pytest.fixture()
def card(jobs: Jobs) -> Iterator[Any]:
    """`card(**columns)`: a `ready` job with a real page on disk and an extract task queued; removed afterwards"""
    created: list[UUID] = []
    group_id = jobs.repos.group_id

    def create(**values: Any) -> UUID:
        job_id = uuid4()
        storage.create_job_dir(group_id, job_id, 1)
        meta = images.normalize_page(
            io.BytesIO(card_image()), storage.page_dir(group_id, job_id, 0), 0, original_filename="card.jpg"
        )
        defaults: dict[str, Any] = {
            "id": job_id,
            "pages": [meta.model_dump(mode="json")],
            "transcription": BANANA_TRANSCRIPTION["content"],
            "extraction": ExtractionMeta(read_path=IngestReadPath.image, language="English", provider="Vision"),
            "kind": IngestTaskKind.extract,
            "state": IngestTaskState.queued,
        }
        created.append(jobs.ready(**{**defaults, **values}))
        return job_id

    yield create
    for job_id in created:
        storage.remove_job_dir(group_id, job_id)


def _read(dispatcher: IngestDispatcher) -> None:
    async def scenario() -> None:
        await dispatcher.run_once()
        await settle(dispatcher)

    run(scenario())


# ==========================================
# Rebuild from the edited transcription


def test_a_rebuild_replaces_a_draft_nobody_edited(
    dispatcher: IngestDispatcher, jobs: Jobs, card: Any, monkeypatch: pytest.MonkeyPatch
):
    fake = FakeCardAI(banana_answers(OpenAIRecipe=REBUILT)).install(monkeypatch)
    job_id = card(task_payload=tasks.rebuild_payload(EDITED))

    _read(dispatcher)

    assert fake.schemas() == ["OpenAIRecipe"]  # the build step only: no image is read
    assert "for 2 minutes" in fake.calls[0].message
    row = jobs.row(job_id)
    assert (row["status"], row["task_state"], row["error_code"]) == (IngestStatus.ready, None, None)
    draft = CardDraft.model_validate(row["draft"])
    assert draft.steps[1].text.endswith("for 2 minutes or until firm in center.")
    assert row["transcription"] == EDITED  # the card's text is now the reviewer's corrected one
    assert (row["draft_version"], row["extracted_version"]) == (2, 2)
    assert row["proposals"] in (None, [])


def test_a_rebuild_of_an_edited_draft_is_a_proposal_marked_as_a_rebuild(
    dispatcher: IngestDispatcher, jobs: Jobs, card: Any, monkeypatch: pytest.MonkeyPatch
):
    FakeCardAI(banana_answers(OpenAIRecipe=REBUILT)).install(monkeypatch)
    job_id = card(task_payload=tasks.rebuild_payload(EDITED), draft_version=3, extracted_version=1)

    _read(dispatcher)

    row = jobs.row(job_id)
    assert CardDraft.model_validate(row["draft"]).name == "Banana Mug Cake"  # the reviewer's draft is kept
    assert row["draft_version"] == 3
    (proposal,) = row["proposals"]
    assert (proposal["kind"], proposal["origin"]) == (CardProposalKind.full.value, CardProposalOrigin.rebuild.value)
    assert proposal["draft"]["steps"][1]["text"].endswith("for 2 minutes or until firm in center.")


def test_a_payload_the_task_cant_use_is_an_internal_error_banner(dispatcher: IngestDispatcher, jobs: Jobs, card: Any):
    bad = [card(task_payload={"mode": "translate"}), card(task_payload={"mode": "rebuild", "transcription": " "})]
    _read(dispatcher)
    for job_id in bad:
        row = jobs.row(job_id)
        assert (row["status"], row["error_code"]) == (IngestStatus.ready, IngestErrorCode.internal_error)


# ==========================================
# Parse with AI


GERMAN_PARSE = {
    "ingredients": [
        {"quantity": 200, "unit": "g", "food": "Mehl", "note": "", "substitutes": []},
        {"quantity": 2, "unit": None, "food": "Eier", "note": "", "substitutes": []},
    ]
}


def _line(text: str) -> CardDraftIngredient:
    """A line the reading left as text (`not_parsed`)"""
    return CardDraftIngredient(original_text=text, note=text, display=text)


def test_parse_with_ai_fills_the_lines_still_as_sent_and_skips_a_changed_one(
    dispatcher: IngestDispatcher, jobs: Jobs, unique_user: TestUser, monkeypatch: pytest.MonkeyPatch
):
    unique_user.repos.ingredient_units.create(
        SaveIngredientUnit(
            name=f"gram {uuid4().hex[:6]}", abbreviation="g", plural_name=None, group_id=unique_user.repos.group_id
        )
    )
    fake = FakeCardAI(banana_answers(OpenAIIngredients=GERMAN_PARSE)).install(monkeypatch)
    flour, eggs, salt = _line("200 g Mehl"), _line("2 Eier"), _line("1 Prise Salz")
    draft = CardDraft(name="Rührkuchen", ingredients=[flour, eggs, salt])
    kept = CardFlag(
        id="empty_section:steps:",
        kind=CardFlagKind.empty_section,
        severity=CardFlagSeverity.warning,
        source=CardFlagSource.validator,
        field="steps",
        resolution=FlagResolution.dismissed,  # "Looks right": the card has no steps
    )
    job_id = jobs.ready(
        draft=draft,
        flags=[kept],
        extraction=ExtractionMeta(read_path=IngestReadPath.image, language="German"),
        transcription="# Rührkuchen\n\n- 200 g Mehl\n- 2 Eier\n- 1 Prise Salz",
        kind=IngestTaskKind.extract,
        state=IngestTaskState.queued,
        priority=limits.PRIORITY_REREAD,
        task_payload=tasks.parse_lines_payload(draft, [flour.reference_id, eggs.reference_id]),
    )
    # the reviewer changes the eggs' line while the task waits
    edited = draft.model_copy(update={"ingredients": [flour, _line("3 Eier"), salt]})
    edited.ingredients[1].reference_id = eggs.reference_id
    jobs.update(job_id, draft=edited.model_dump(mode="json"))

    _read(dispatcher)

    assert fake.schemas() == ["OpenAIIngredients"]
    assert '"200 g Mehl"' in fake.calls[0].message
    row = jobs.row(job_id)
    assert (row["status"], row["task_state"], row["error_code"]) == (IngestStatus.ready, None, None)
    assert row["draft_version"] == 2  # an open editor's next save reloads
    saved = CardDraft.model_validate(row["draft"])
    parsed, untouched, salt_line = saved.ingredients
    assert parsed.reference_id == flour.reference_id
    assert (parsed.quantity, parsed.food and parsed.food.name) == (200, "Mehl")
    assert parsed.original_text == "200 g Mehl"  # the card's reading of the line stays
    assert untouched.note == "3 Eier" and untouched.quantity is None  # changed meanwhile: the reviewer's edit stays
    assert salt_line == salt
    # the flags are computed again as a save does, keeping the stored resolution
    flags = {flag.id: flag for flag in (CardFlag.model_validate(flag) for flag in row["flags"])}
    assert flags["empty_section:steps:"].resolution == FlagResolution.dismissed
    assert f"new_food:ingredients:{flour.reference_id}" in flags  # "Mehl" is a food the group hasn't yet
    assert (row["error_count"], row["warning_count"]) == (0, 0)


def test_parse_with_ai_judges_the_lines_it_parsed_as_new_readings(
    dispatcher: IngestDispatcher, jobs: Jobs, unique_user: TestUser, monkeypatch: pytest.MonkeyPatch
):
    """
    A line parsed again gets the parse flags of its new reading, judged as extraction judges them: the stored ones,
    and the reviewer's "Looks right" on them, were about the reading it replaced. A food the new reading links by a
    near-miss name is `linked_fuzzy` again; one whose name is on the line isn't.
    """
    group_id = unique_user.repos.group_id
    foods = {
        name: unique_user.repos.ingredient_foods.create(
            SaveIngredientFood(name=name, plural_name=f"{name}s", group_id=group_id)
        )
        for name in ("shallot", "red shallot")
    }
    answer = {
        "ingredients": [
            {"quantity": 2, "unit": None, "food": "shallot", "note": "rd", "substitutes": []},
            {"quantity": 1, "unit": None, "food": "red shallot", "note": "", "substitutes": []},
        ]
    }
    FakeCardAI(banana_answers(OpenAIIngredients=answer)).install(monkeypatch)

    def parsed(text: str, quantity: int) -> CardDraftIngredient:
        """A line the NLP parser read at extraction, linked to "red shallot" by a near-miss name"""
        red = foods["red shallot"]
        line = CardDraftIngredient(
            original_text=text,
            quantity=quantity,
            food=CardDraftRef(id=red.id, name=red.name),
            display=text,
            parse_confidence=0.9,
        )
        line.extracted_hash = ingredient_hash(line)
        return line

    two, one = parsed("2 rd shallots", 2), parsed("1 rd shallot", 1)
    looked_right = [
        CardFlag(
            id=f"linked_fuzzy:ingredients:{line.reference_id}",
            kind=CardFlagKind.linked_fuzzy,
            severity=CardFlagSeverity.warning,
            source=CardFlagSource.parser,
            field="ingredients",
            ref=str(line.reference_id),
            params={"name": "red shallot", "kind": "food"},
            resolution=FlagResolution.dismissed,
        )
        for line in (two, one)
    ]
    draft = CardDraft(name="Shallot Tart", ingredients=[two, one])
    job_id = jobs.ready(
        draft=draft,
        flags=looked_right,
        extraction=ExtractionMeta(read_path=IngestReadPath.image, language="English"),
        transcription="# Shallot Tart\n\n- 2 rd shallots\n- 1 rd shallot",
        kind=IngestTaskKind.extract,
        state=IngestTaskState.queued,
        priority=limits.PRIORITY_REREAD,
        task_payload=tasks.parse_lines_payload(draft, [two.reference_id, one.reference_id]),
    )

    _read(dispatcher)

    row = jobs.row(job_id)
    first, second = CardDraft.model_validate(row["draft"]).ingredients
    assert (first.food and first.food.name, second.food and second.food.name) == ("shallot", "red shallot")
    fuzzy = {
        flag.ref: flag
        for flag in (CardFlag.model_validate(flag) for flag in row["flags"])
        if flag.kind == CardFlagKind.linked_fuzzy
    }
    assert str(two.reference_id) not in fuzzy  # "shallots" is on its line now
    again = fuzzy[str(one.reference_id)]
    assert (again.params["name"], again.resolution) == ("red shallot", None)
    assert row["warning_count"] >= 1


def test_parse_with_ai_sends_the_lines_as_they_read_now():
    parsed = CardDraftIngredient(original_text="1 T. coconut oil", quantity=2, note="melted")
    plain = _line("Cinnamon to taste")
    payload = tasks.parse_lines_payload(
        CardDraft(ingredients=[parsed, plain]), [plain.reference_id, parsed.reference_id]
    )
    assert payload == {
        "mode": "parse_lines",
        "lines": [
            {"ref": str(plain.reference_id), "text": "Cinnamon to taste"},
            {"ref": str(parsed.reference_id), "text": "2 melted"},  # edited: its fields, not the card's reading
        ],
    }
    with pytest.raises(KeyError):
        tasks.parse_lines_payload(CardDraft(ingredients=[plain]), [uuid4()])
