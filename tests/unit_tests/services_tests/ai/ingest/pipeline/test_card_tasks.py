"""
The task handlers (docs/ai/PHASE2.md §3.7): `handle_extract` and `handle_reread` on a real job, writing nothing but a
turned page's metadata (fenced on the lease)
"""

import io
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from mealie.db.db_setup import session_context
from mealie.db.models.recipe_ingest import RecipeIngestionJob
from mealie.repos.repository_recipe_ingest import IngestRepos
from mealie.schema.recipe.recipe_ingredient import SaveIngredientUnit
from mealie.schema.recipe_ingest import (
    CardDraft,
    CardDraftIngredient,
    CardDraftNote,
    CardProposalKind,
    ExtractionMeta,
    IngestErrorCode,
    IngestSource,
    IngestTaskKind,
    PageMeta,
    PageRotationSource,
    ProposalTarget,
    RereadRequest,
)
from mealie.services import ocr
from mealie.services.ai.ingest import images, storage, tasks
from mealie.services.ai.ingest.runner.types import TaskContext, TaskFailed
from tests.unit_tests.services_tests.ai.ingest.pipeline.card_fakes import (
    FakeCardAI,
    banana_answers,
    card_image,
    configure,
    create_provider,
    seed_foods_and_units,
)
from tests.utils.fixture_schemas import TestUser

PROGRESS = "recipe-ingest.progress."


def create_job(user: TestUser, *, pages: int = 1, **values: Any) -> tuple[UUID, UUID]:
    """A job with normalized pages, its extract task running under a fresh lease token; returns (job, token)"""
    group_id, household_id = user.repos.group_id, user.repos.household_id
    job_id, token = uuid4(), uuid4()
    storage.create_job_dir(group_id, job_id, pages)  # type: ignore[arg-type]
    metas = [
        images.normalize_page(
            io.BytesIO(card_image()),
            storage.page_dir(group_id, job_id, index),  # type: ignore[arg-type]
            index,
            original_filename="card.jpg",
        )
        for index in range(pages)
    ]
    with session_context() as session:
        ingest = IngestRepos(session, group_id, household_id)  # type: ignore[arg-type]
        batch_id = ingest.batches.create(source=IngestSource.app, created_by=user.user_id)
        ingest.jobs.create(
            {
                "id": job_id,
                "batch_id": batch_id,
                "source": "app",
                "status": "processing",
                "locale": "en-US",
                "pages": [meta.model_dump(mode="json") for meta in metas],
                "source_sha256": "0" * 64,
                "task_kind": IngestTaskKind.extract.value,
                "task_state": "running",
                "lease_token": token,
                **values,
            }
        )
    return job_id, token


def task(user: TestUser, job_id: UUID, token: UUID, progress: list[str], **kwargs: Any) -> TaskContext:
    async def report_progress(key: str) -> None:
        progress.append(key)

    values: dict[str, Any] = {
        "job_id": job_id,
        "group_id": user.repos.group_id,
        "household_id": user.repos.household_id,
        "kind": IngestTaskKind.extract,
        "payload": None,
        "token": token,
        "locale": "en-US",
        "local_only": False,
        "report_progress": report_progress,
    }
    return TaskContext(**{**values, **kwargs})


def row(job_id: UUID) -> dict[str, Any]:
    with session_context() as session:
        job = session.execute(sa.select(RecipeIngestionJob).where(RecipeIngestionJob.id == job_id)).scalar_one()
        return {"pages": job.pages, "row_version": job.row_version, "draft": job.draft, "status": job.status}


def _providers(user: TestUser) -> None:
    configure(user, image=create_provider(user, "Vision"), default=create_provider(user, "Text"))


@pytest.mark.asyncio
async def test_handle_extract_reads_the_card_and_writes_nothing(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    _providers(user)
    seed_foods_and_units(user)
    fake = FakeCardAI(banana_answers()).install(monkeypatch)
    monkeypatch.setattr(ocr, "is_available", lambda: False)  # no OCR fallback
    monkeypatch.setattr(ocr, "binary_available", lambda: False)  # and no orientation
    job_id, token = create_job(user, pages=2)
    before = row(job_id)
    progress: list[str] = []

    result = await tasks.handle_extract(task(user, job_id, token, progress))

    assert result.draft.name == "Banana Mug Cake"
    assert result.transcription and "[blank]" in result.transcription
    assert result.extraction.read_path == "image"
    assert [page.index for page in result.pages] == [0, 1]
    assert result.pages == [PageMeta.model_validate(page) for page in before["pages"]]
    assert fake.calls[0].images == 2
    assert progress[0] == f"{PROGRESS}reading-card"
    assert row(job_id) == before


@pytest.mark.asyncio
async def test_a_turned_page_is_saved_at_once_fenced_on_the_lease(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    _providers(user)
    # every read fails after the page turned: the turn must not be lost with it
    failures = {"Vision": RuntimeError("down"), "Text": RuntimeError("down")}
    FakeCardAI(banana_answers(), failures=failures).install(monkeypatch)
    monkeypatch.setattr(ocr, "binary_available", lambda: True)  # orientation on
    monkeypatch.setattr(ocr, "extract_text", lambda path, **_: ocr.OCRResult(text="Banana Mug Cake", rotation=90))
    job_id, token = create_job(user)
    before = row(job_id)
    progress: list[str] = []

    with pytest.raises(Exception, match="OpenAI Request Failed"):
        await tasks.handle_extract(task(user, job_id, token, progress))

    after = row(job_id)
    (page,) = [PageMeta.model_validate(page) for page in after["pages"]]
    assert (page.rotation, page.rotation_source, page.oriented) == (90, PageRotationSource.ocr, True)
    assert page.ocr is not None and page.ocr.text == "Banana Mug Cake"
    assert (page.width, page.height) == (before["pages"][0]["height"], before["pages"][0]["width"])
    assert after["row_version"] == before["row_version"] + 1
    assert after["draft"] is None and after["status"] == "processing"
    assert progress[0] == f"{PROGRESS}orienting"


@pytest.mark.asyncio
async def test_a_lost_lease_stops_the_task(unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch):
    user = unique_user_fn_scoped
    _providers(user)
    fake = FakeCardAI(banana_answers()).install(monkeypatch)
    monkeypatch.setattr(ocr, "binary_available", lambda: True)  # orientation on
    monkeypatch.setattr(ocr, "extract_text", lambda path, **_: ocr.OCRResult(text="x", rotation=180))
    job_id, _ = create_job(user)
    before = row(job_id)

    with pytest.raises(TaskFailed) as failed:
        await tasks.handle_extract(task(user, job_id, uuid4(), []))

    assert failed.value.code == IngestErrorCode.interrupted
    assert row(job_id) == before
    assert fake.calls == []


@pytest.mark.asyncio
async def test_a_missing_job_household_or_file(unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch):
    user = unique_user_fn_scoped
    monkeypatch.setattr(ocr, "is_available", lambda: False)  # no OCR fallback
    monkeypatch.setattr(ocr, "binary_available", lambda: False)  # and no orientation

    with pytest.raises(TaskFailed) as gone:
        await tasks.handle_extract(task(user, uuid4(), uuid4(), []))
    assert gone.value.code == IngestErrorCode.interrupted

    with pytest.raises(TaskFailed) as no_household:
        await tasks.handle_extract(task(user, uuid4(), uuid4(), [], household_id=uuid4()))
    assert no_household.value.code == IngestErrorCode.owner_missing

    job_id, token = create_job(user)
    storage.page_dir(user.repos.group_id, job_id, 0).joinpath(images.VIEW_FILE).unlink()  # type: ignore[arg-type]
    with pytest.raises(FileNotFoundError):
        await tasks.handle_extract(task(user, job_id, token, []))


@pytest.mark.asyncio
async def test_handle_reread_parses_an_ingredient_reading(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    _providers(user)
    seed_foods_and_units(user)
    region = {"readable": True, "text": "1/3 C. almond flour", "alternatives": []}
    fake = FakeCardAI(banana_answers(OpenAIRecipeCardRegion=region)).install(monkeypatch)
    line = CardDraftIngredient(original_text="1/8 C. almond fluor", note="1/8 C. almond fluor", title="Dry")
    draft = CardDraft(name="Banana Mug Cake", ingredients=[line])
    job_id, token = create_job(
        user,
        status="ready",
        draft=draft.model_dump(mode="json"),
        extraction=ExtractionMeta(language="English").model_dump(mode="json"),
    )
    target = ProposalTarget(field="ingredients", ref=str(line.reference_id))
    payload = RereadRequest(page=0, x=0.1, y=0.2, width=0.5, height=0.1, target=target).model_dump(mode="json")
    before = row(job_id)

    result = await tasks.handle_reread(task(user, job_id, token, [], kind=IngestTaskKind.reread, payload=payload))

    proposal = result.proposal
    assert (proposal.kind, proposal.target, proposal.text) == (CardProposalKind.region, target, "1/3 C. almond flour")
    assert '"""\n1/8 C. almond fluor\n"""' in fake.calls[0].message
    assert proposal.draft is not None
    (parsed,) = proposal.draft.ingredients
    assert (parsed.reference_id, parsed.title, parsed.original_text) == (
        line.reference_id,
        "Dry",
        "1/3 C. almond flour",
    )
    assert parsed.unit is not None and parsed.unit.name == "cup"
    assert row(job_id) == before


@pytest.mark.asyncio
async def test_handle_reread_parses_a_line_in_another_language_with_the_ai_parser(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    """A German card's re-read line goes to the AI parser on the card's own service, as extraction's lines do"""
    user = unique_user_fn_scoped
    _providers(user)
    user.repos.ingredient_units.create(
        SaveIngredientUnit(name="gram", plural_name="grams", abbreviation="g", group_id=user.repos.group_id)
    )
    region = {"readable": True, "text": "200 g Mehl", "alternatives": []}
    answer = {"ingredients": [{"quantity": 200, "unit": "g", "food": "Mehl", "note": "", "substitutes": []}]}
    fake = FakeCardAI(banana_answers(OpenAIRecipeCardRegion=region, OpenAIIngredients=answer)).install(monkeypatch)
    line = CardDraftIngredient(original_text="20 g Mehl", note="20 g Mehl")
    job_id, token = create_job(
        user,
        status="ready",
        draft=CardDraft(name="Rührkuchen", ingredients=[line]).model_dump(mode="json"),
        extraction=ExtractionMeta(language="German").model_dump(mode="json"),
    )
    target = ProposalTarget(field="ingredients", ref=str(line.reference_id))
    payload = RereadRequest(page=0, x=0.1, y=0.2, width=0.5, height=0.1, target=target).model_dump(mode="json")

    result = await tasks.handle_reread(task(user, job_id, token, [], kind=IngestTaskKind.reread, payload=payload))

    assert result.proposal.draft is not None
    (parsed,) = result.proposal.draft.ingredients
    assert (parsed.quantity, parsed.unit and parsed.unit.name, parsed.food and parsed.food.name) == (
        200,
        "gram",
        "Mehl",
    )
    assert parsed.reference_id == line.reference_id
    assert [call.schema for call in fake.calls] == ["OpenAIRecipeCardRegion", "OpenAIIngredients"]


@pytest.mark.asyncio
async def test_handle_reread_of_a_step(unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch):
    user = unique_user_fn_scoped
    _providers(user)
    region = {"readable": True, "text": "Microwave for [blank] minutes.", "alternatives": []}
    FakeCardAI(banana_answers(OpenAIRecipeCardRegion=region)).install(monkeypatch)
    job_id, token = create_job(user, status="ready")
    payload = {"page": 0, "region": {"x": 0, "y": 0.5, "width": 1, "height": 0.2}, "target": {"field": "steps"}}

    result = await tasks.handle_reread(task(user, job_id, token, [], kind=IngestTaskKind.reread, payload=payload))

    assert result.proposal.text == "Microwave for [blank] minutes."
    assert result.proposal.draft is None


@pytest.mark.asyncio
@pytest.mark.parametrize("by_position", [False, True], ids=["by-id", "by-position"])
async def test_handle_reread_of_a_note_compares_with_that_note(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, by_position: bool
):
    """
    A note's re-read names it by id, as its flags do (PL-08), or by position from a client older than note ids: the
    model is shown that note's text to compare with, not another note's
    """
    user = unique_user_fn_scoped
    _providers(user)
    region = {"readable": True, "text": "Keeps 3 days.", "alternatives": []}
    fake = FakeCardAI(banana_answers(OpenAIRecipeCardRegion=region)).install(monkeypatch)
    notes = [CardDraftNote(title="Serving", text="Serve warm."), CardDraftNote(title="Storage", text="Keeps 2 days.")]
    draft = CardDraft(name="Banana Mug Cake", notes=notes)
    job_id, token = create_job(user, status="ready", draft=draft.model_dump(mode="json"))
    target = ProposalTarget(field="notes", ref="1" if by_position else str(notes[1].id))
    payload = RereadRequest(page=0, x=0.1, y=0.6, width=0.8, height=0.1, target=target).model_dump(mode="json")

    result = await tasks.handle_reread(task(user, job_id, token, [], kind=IngestTaskKind.reread, payload=payload))

    assert (result.proposal.target, result.proposal.text) == (target, "Keeps 3 days.")
    assert '"""\nKeeps 2 days.\n"""' in fake.calls[0].message
    assert "Serve warm." not in fake.calls[0].message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"page": 0, "x": 0, "y": 0, "width": 1, "height": 1},
        {"page": 3, "x": 0, "y": 0, "width": 1, "height": 1, "target": {"field": "name"}},
    ],
)
async def test_a_bad_reread_payload_is_an_internal_error(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, payload: dict | None
):
    user = unique_user_fn_scoped
    _providers(user)
    FakeCardAI(banana_answers()).install(monkeypatch)
    job_id, token = create_job(user, status="ready")

    with pytest.raises(TaskFailed) as failed:
        await tasks.handle_reread(task(user, job_id, token, [], kind=IngestTaskKind.reread, payload=payload))
    assert failed.value.code == IngestErrorCode.internal_error


def test_job_files_are_where_the_runner_expects(unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    job_id, _ = create_job(user)
    page = storage.page_dir(user.repos.group_id, job_id, 0)  # type: ignore[arg-type]
    assert all(isinstance(path, Path) and path.is_file() for path in (page / images.PAGE_FILE, page / images.VIEW_FILE))
