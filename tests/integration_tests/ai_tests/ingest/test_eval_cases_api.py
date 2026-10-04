"""
Saving reviewed cards as eval cases from the phone, listing and deleting them (docs/ai/PHASE2.md §9, §11.6, §14):
group managers only, slugs checked, never overwritten, and paused during a backup restore. Runs on SQLite and
PostgreSQL.
"""

import io
import json
import shutil
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from mealie.db.db_setup import session_context
from mealie.repos.repository_recipe_ingest import IngestRepos
from mealie.schema.recipe_ingest import (
    CardDraft,
    CardDraftIngredient,
    CardDraftStep,
    ExtractionMeta,
    IngestSource,
    IngestStatus,
    PageRotationSource,
)
from mealie.services.ai.ingest import images, storage
from mealie.services.ai.ingest.eval_export import CardFixture
from mealie.services.ai.ingest.settings import IngestSettings
from tests.utils.fixture_schemas import TestUser

EVAL_CASES = "/api/ai/ingest/eval-cases"


def eval_case_url(job_id: UUID | str) -> str:
    return f"/api/ai/ingest/jobs/{job_id}/eval-case"


def eval_case_item(slug: str) -> str:
    return f"{EVAL_CASES}/{slug}"


def _photo() -> io.BytesIO:
    image = Image.new("RGB", (240, 320), (255, 255, 255))
    image.paste((255, 0, 0), (0, 0, 240, 100))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    buffer.seek(0)
    return buffer


def seed_job(user: TestUser, *, status: IngestStatus = IngestStatus.ready, pages: int = 2) -> UUID:
    """A reviewed two-sided card whose front orientation turned a quarter, with its files"""
    group_id, household_id = UUID(user.group_id), UUID(user.household_id)
    job_id = uuid4()
    job_dir = storage.create_job_dir(group_id, job_id, pages)
    metas = []
    for index in range(pages):
        page_dir = job_dir / "pages" / str(index)
        meta = images.normalize_page(_photo(), page_dir, index, original_filename=f"IMG_{index}.jpg")
        if index == 0:
            meta = images.rotate_page_files(page_dir, meta, 270, PageRotationSource.ocr)
        metas.append(meta)

    draft = CardDraft(
        name="Banana Mug Cake",
        ingredients=[CardDraftIngredient(original_text="1 banana", quantity=1)],
        steps=[CardDraftStep(text="Microwave for [blank] minutes.")],
    )
    with session_context() as session:
        repos = IngestRepos(session, group_id, household_id)
        batch_id = repos.batches.create(source=IngestSource.app, created_by=user.user_id)
        repos.jobs.create(
            {
                "id": job_id,
                "batch_id": batch_id,
                "position": 0,
                "source": IngestSource.app.value,
                "status": status.value,
                "pages": metas,
                "source_sha256": uuid4().hex * 2,
                "draft": draft,
                "transcription": "Banana Mug Cake\n1 banana\nMicrowave for [blank] minutes.",
                "extraction": ExtractionMeta(provider="Gemini", model="gemini-flash"),
            }
        )
    return job_id


@pytest.fixture()
def jobs(unique_user: TestUser) -> Iterator[list[UUID]]:
    created: list[UUID] = []
    yield created
    group_id = UUID(unique_user.group_id)
    for job_id in created:
        storage.remove_job_dir(group_id, job_id)
    shutil.rmtree(storage.eval_cards_dir(group_id), ignore_errors=True)


@pytest.fixture()
def not_a_manager(unique_user_fn_scoped: TestUser) -> TestUser:
    user = unique_user_fn_scoped.repos.users.get_one(unique_user_fn_scoped.user_id)
    assert user
    user.can_manage = False
    unique_user_fn_scoped.repos.users.update(user.id, user)
    return unique_user_fn_scoped


def test_save_list_and_delete_an_eval_case(api_client: TestClient, unique_user: TestUser, jobs: list[UUID]):
    job_id = seed_job(unique_user)
    jobs.append(job_id)

    response = api_client.post(
        eval_case_url(job_id), json={"slug": "banana-mug-cake", "verified": True}, headers=unique_user.token
    )

    assert response.status_code == 201, response.text
    assert response.json() == {
        "slug": "banana-mug-cake",
        "files": ["banana-mug-cake.json", "banana-mug-cake-1.jpg", "banana-mug-cake-2.jpg"],
    }
    directory = storage.eval_cards_dir(UUID(unique_user.group_id))
    fixture = CardFixture.model_validate_json((directory / "banana-mug-cake.json").read_text())
    assert fixture.source == ["banana-mug-cake-1.jpg", "banana-mug-cake-2.jpg"]
    assert fixture.verified_by_owner is True
    assert fixture.tags == ["sideways", "two-sided", "blank"]
    assert fixture.expected.instructions == ["Microwave for [blank] minutes."]
    assert fixture.origin is not None and fixture.origin.job_id == str(job_id)
    # the front is turned back the way it came in: portrait, as uploaded
    with Image.open(directory / "banana-mug-cake-1.jpg") as front:
        assert front.size == (240, 320)
        assert not front.getexif()

    listed = api_client.get(EVAL_CASES, headers=unique_user.token)
    assert listed.status_code == 200
    [case] = listed.json()
    assert case["slug"] == "banana-mug-cake"
    assert case["name"] == "Banana Mug Cake"
    assert case["pageCount"] == 2
    assert case["verified"] is True
    assert case["createdAt"]

    assert api_client.delete(eval_case_item("banana-mug-cake"), headers=unique_user.token).status_code == 204
    assert api_client.get(EVAL_CASES, headers=unique_user.token).json() == []
    assert list(directory.iterdir()) == []
    response = api_client.delete(eval_case_item("banana-mug-cake"), headers=unique_user.token)
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "not_found"


def test_a_committed_card_can_be_saved_too(api_client: TestClient, unique_user: TestUser, jobs: list[UUID]):
    job_id = seed_job(unique_user, status=IngestStatus.committed, pages=1)
    jobs.append(job_id)

    response = api_client.post(eval_case_url(job_id), json={"slug": "committed"}, headers=unique_user.token)

    assert response.status_code == 201, response.text
    fixture = json.loads((storage.eval_cards_dir(UUID(unique_user.group_id)) / "committed.json").read_text())
    assert fixture["verified_by_owner"] is False


@pytest.mark.parametrize("slug", ["Banana", "-banana", "banana mug", "../banana", "a" * 65, "", "banana_mug"])
def test_slugs_are_checked(api_client: TestClient, unique_user: TestUser, jobs: list[UUID], slug: str):
    job_id = seed_job(unique_user, pages=1)
    jobs.append(job_id)

    response = api_client.post(eval_case_url(job_id), json={"slug": slug}, headers=unique_user.token)

    assert response.status_code == 422
    assert not storage.eval_cards_dir(UUID(unique_user.group_id)).exists()


def test_an_existing_case_is_never_overwritten(api_client: TestClient, unique_user: TestUser, jobs: list[UUID]):
    first, second = seed_job(unique_user, pages=1), seed_job(unique_user, pages=1)
    jobs.extend([first, second])
    assert api_client.post(eval_case_url(first), json={"slug": "pie"}, headers=unique_user.token).status_code == 201
    saved = (storage.eval_cards_dir(UUID(unique_user.group_id)) / "pie.json").read_bytes()

    response = api_client.post(eval_case_url(second), json={"slug": "pie"}, headers=unique_user.token)

    assert response.status_code == 409
    # the page handles it itself, so there's no message to toast
    assert response.json() == {"detail": {"code": "eval_case_exists"}}
    assert (storage.eval_cards_dir(UUID(unique_user.group_id)) / "pie.json").read_bytes() == saved


def test_cards_without_a_draft_or_files_are_refused(api_client: TestClient, unique_user: TestUser, jobs: list[UUID]):
    processing = seed_job(unique_user, status=IngestStatus.processing, pages=1)
    purged = seed_job(unique_user, status=IngestStatus.committed, pages=1)
    jobs.extend([processing, purged])
    storage.remove_job_dir(UUID(unique_user.group_id), purged)

    response = api_client.post(eval_case_url(processing), json={"slug": "x"}, headers=unique_user.token)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "not_exportable"

    response = api_client.post(eval_case_url(purged), json={"slug": "y"}, headers=unique_user.token)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "files_missing"

    response = api_client.post(eval_case_url(uuid4()), json={"slug": "z"}, headers=unique_user.token)
    assert response.status_code == 404
    assert not storage.eval_cards_dir(UUID(unique_user.group_id)).exists()


def test_another_households_card_is_not_found(
    api_client: TestClient, unique_user: TestUser, h2_user: TestUser, jobs: list[UUID]
):
    job_id = seed_job(unique_user, pages=1)
    jobs.append(job_id)
    h2 = h2_user.repos.users.get_one(h2_user.user_id)
    assert h2
    h2.can_manage = True
    h2_user.repos.users.update(h2.id, h2)

    response = api_client.post(eval_case_url(job_id), json={"slug": "theirs"}, headers=h2_user.token)

    assert response.status_code == 404


def test_only_group_managers(api_client: TestClient, unique_user: TestUser, not_a_manager: TestUser, jobs: list[UUID]):
    job_id = seed_job(unique_user, pages=1)
    jobs.append(job_id)

    assert api_client.post(eval_case_url(job_id), json={"slug": "x"}, headers=not_a_manager.token).status_code == 403
    assert api_client.get(EVAL_CASES, headers=not_a_manager.token).status_code == 403
    assert api_client.delete(eval_case_item("x"), headers=not_a_manager.token).status_code == 403


def test_paused_for_a_restore(
    api_client: TestClient, unique_user: TestUser, jobs: list[UUID], monkeypatch: pytest.MonkeyPatch
):
    job_id = seed_job(unique_user, pages=1)
    jobs.append(job_id)
    assert api_client.post(eval_case_url(job_id), json={"slug": "kept"}, headers=unique_user.token).status_code == 201
    monkeypatch.setattr(storage, "is_paused", lambda: True)

    for response in (
        api_client.post(eval_case_url(job_id), json={"slug": "paused"}, headers=unique_user.token),
        api_client.delete(eval_case_item("kept"), headers=unique_user.token),
    ):
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "60"
        assert response.json()["detail"]["code"] == "paused_for_restore"
        assert response.json()["detail"]["message"]

    # reading the list writes nothing, so it still works
    assert [case["slug"] for case in api_client.get(EVAL_CASES, headers=unique_user.token).json()] == ["kept"]
    assert sorted(path.name for path in storage.eval_cards_dir(UUID(unique_user.group_id)).iterdir()) == [
        "kept-1.jpg",
        "kept.json",
    ]


def test_a_restore_starting_during_the_save(
    api_client: TestClient, unique_user: TestUser, jobs: list[UUID], monkeypatch: pytest.MonkeyPatch
):
    """The write section refuses when the restore's marker appears after the route's first check"""
    job_id = seed_job(unique_user, pages=1)
    jobs.append(job_id)
    checks = iter([False])
    monkeypatch.setattr(storage, "is_paused", lambda: next(checks, True))

    response = api_client.post(eval_case_url(job_id), json={"slug": "late"}, headers=unique_user.token)

    assert response.status_code == 503
    assert not storage.eval_cards_dir(UUID(unique_user.group_id)).exists()


def test_saving_needs_ingestion_on(
    api_client: TestClient, unique_user: TestUser, jobs: list[UUID], monkeypatch: pytest.MonkeyPatch
):
    from mealie.routes.ai.ingest import _deps

    job_id = seed_job(unique_user, pages=1)
    jobs.append(job_id)
    monkeypatch.setattr(_deps, "get_ingest_settings", lambda: IngestSettings(ENABLED=False, WORKER=False))

    response = api_client.post(eval_case_url(job_id), json={"slug": "off"}, headers=unique_user.token)

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "ingest_disabled"
