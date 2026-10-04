"""
The review page's further actions on a card (docs/ai/PHASE2.md §6, §7, §9): reading a failed local-only card with
cloud providers on purpose, adding a card to another as its back, undoing a commit, and committing a batch's clean
cards. Runs on SQLite and PostgreSQL.
"""

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from test_commit import commit, kept, ready_to_commit, recipe_dir, recipe_of
from test_jobs_api import (
    assert_code,
    banana_draft,
    fake_compute_flags,
    household_member,
    job_row,
    job_url,
    seed_job,
    set_columns,
    use_fake_flags,
)

from mealie.db.db_setup import session_context
from mealie.repos.repository_recipe_ingest import IngestRepos, utcnow
from mealie.schema.recipe_ingest import (
    IngestErrorCode,
    IngestSource,
    IngestStatus,
    IngestTaskKind,
    IngestTaskState,
    RecipeIngestionSettingsUpdate,
)
from mealie.services.ai.ingest import intake, limits, storage
from mealie.services.ai.ingest.review import parse_pages
from mealie.services.event_bus_service.event_bus_service import EventBusService
from mealie.services.event_bus_service.event_types import EventTypes
from tests.utils import api_routes
from tests.utils.fixture_schemas import TestUser


@pytest.fixture(autouse=True)
def _fake_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    use_fake_flags(monkeypatch)


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[EventTypes, str]]:
    """Every recipe event dispatched: its type and the recipe's slug"""
    sent: list[tuple[EventTypes, str]] = []

    def dispatch(self: EventBusService, *args: Any, **kwargs: Any) -> None:
        if kwargs["event_type"] in (EventTypes.recipe_created, EventTypes.recipe_deleted):
            sent.append((kwargs["event_type"], kwargs["document_data"].recipe_slug))

    monkeypatch.setattr(EventBusService, "dispatch", dispatch)
    return sent


def _group_local_only(user: TestUser, local_only: bool) -> None:
    with session_context() as session:
        IngestRepos(session, UUID(user.group_id), UUID(user.household_id)).settings.upsert(
            RecipeIngestionSettingsUpdate(local_only=local_only)
        )


def _permissions(api_client: TestClient, user: TestUser, job_id: UUID) -> dict[str, bool]:
    return api_client.get(job_url(job_id), headers=user.token).json()["permissions"]


# ==================================================================================================================
# Read with cloud providers


def _failed_local_only(user: TestUser, **columns: Any) -> UUID:
    return seed_job(
        user,
        status=IngestStatus.failed,
        local_only=True,
        error_code=IngestErrorCode.local_only_unavailable.value,
        created_by=user.user_id,
        **columns,
    )


def test_the_uploader_reads_a_failed_local_card_with_cloud_providers(
    api_client: TestClient, admin_token: dict, unique_user_fn_scoped: TestUser
):
    user = unique_user_fn_scoped
    member = household_member(api_client, admin_token, user)
    job_id = _failed_local_only(member)
    assert _permissions(api_client, member, job_id)["canReadWithCloud"] is True
    assert _permissions(api_client, user, job_id)["canReadWithCloud"] is True  # a household manager

    response = api_client.post(job_url(job_id, "read-with-cloud"), headers=member.token)
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "processing"
    assert response.json()["task"]["kind"] == "extract"

    row = job_row(job_id)
    assert (row["status"], row["local_only"], row["error_code"]) == ("processing", False, None)
    assert (row["task_kind"], row["task_state"], row["task_payload"]) == ("extract", "queued", None)

    # it's no longer a failed local card: nothing more to do
    assert_code(api_client.post(job_url(job_id, "read-with-cloud"), headers=member.token), 409, "invalid_status")


def test_other_members_cant_send_someone_elses_card_to_the_cloud(
    api_client: TestClient, admin_token: dict, unique_user_fn_scoped: TestUser
):
    user = unique_user_fn_scoped
    uploader = household_member(api_client, admin_token, user)
    other = household_member(api_client, admin_token, user)
    job_id = _failed_local_only(uploader)

    assert _permissions(api_client, other, job_id)["canReadWithCloud"] is False
    assert_code(api_client.post(job_url(job_id, "read-with-cloud"), headers=other.token), 403, "forbidden")
    row = job_row(job_id)
    assert (row["status"], row["local_only"]) == ("failed", True)


def test_a_group_that_keeps_cards_local_keeps_them_local(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    job_id = _failed_local_only(user)
    _group_local_only(user, True)

    assert _permissions(api_client, user, job_id)["canReadWithCloud"] is False
    assert_code(api_client.post(job_url(job_id, "read-with-cloud"), headers=user.token), 409, "group_local_only")
    assert job_row(job_id)["local_only"] is True


@pytest.mark.parametrize(
    "columns",
    [
        {"error_code": IngestErrorCode.provider_failed.value},  # failed for another reason: Retry
        {"local_only": False},  # it was the group's setting, since switched off: Retry reads it
        {"status": IngestStatus.ready.value, "error_code": None},
    ],
)
def test_only_a_card_that_failed_for_staying_local(
    api_client: TestClient, unique_user_fn_scoped: TestUser, columns: dict[str, Any]
):
    user = unique_user_fn_scoped
    job_id = _failed_local_only(user)
    set_columns(job_id, **columns)

    assert _permissions(api_client, user, job_id)["canReadWithCloud"] is False
    assert_code(api_client.post(job_url(job_id, "read-with-cloud"), headers=user.token), 409, "invalid_status")


# ==================================================================================================================
# Add as the back of another card


def _pages_on_disk(user: TestUser, job_id: UUID) -> list[str]:
    pages = storage.job_dir(UUID(user.group_id), job_id) / "pages"
    return sorted(path.name for path in pages.iterdir()) if pages.is_dir() else []


def test_a_card_becomes_the_back_of_another(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    front = seed_job(user, created_by=user.user_id)
    back = seed_job(user, created_by=user.user_id, position=1)
    back_page = parse_pages(job_row(back)["pages"])[0]
    assert _permissions(api_client, user, back)["canMerge"] is True

    response = api_client.post(job_url(back, "merge"), json={"intoJobId": str(front)}, headers=user.token)
    assert response.status_code == 202, response.text
    assert response.json()["task"]["kind"] == "extract"

    assert job_row(back) == {}
    assert not storage.job_dir(UUID(user.group_id), back).exists()
    row = job_row(front)
    pages = parse_pages(row["pages"])
    assert [page.index for page in pages] == [0, 1]
    assert pages[1].page_sha256 == back_page.page_sha256
    assert pages[1].raw_sha256 == back_page.raw_sha256
    assert row["source_sha256"] == intake.source_sha256(pages)
    assert (row["status"], row["task_kind"], row["task_state"]) == ("ready", "extract", "queued")
    assert _pages_on_disk(user, front) == ["0", "1"]

    # the moved page is served from its new place
    image = api_client.get(job_url(front, "pages", 1, "view"), headers=user.token)
    assert image.status_code == 200


def test_a_failed_card_with_its_back_is_read_again(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    front = seed_job(user, status=IngestStatus.failed, error_code="no_recipe_found", created_by=user.user_id)
    back = seed_job(user, status=IngestStatus.failed, error_code="no_recipe_found", created_by=user.user_id)

    response = api_client.post(job_url(back, "merge"), json={"intoJobId": str(front)}, headers=user.token)
    assert response.status_code == 202, response.text
    row = job_row(front)
    assert (row["status"], row["error_code"], row["task_state"]) == ("processing", None, "queued")


def test_merges_that_cant_happen(api_client: TestClient, admin_token: dict, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    three = seed_job(user, page_count=3, created_by=user.user_id)
    two = seed_job(user, page_count=2, created_by=user.user_id)
    one = seed_job(user, created_by=user.user_id)
    committed = seed_job(user, status=IngestStatus.committed, created_by=user.user_id)
    busy = seed_job(user, created_by=user.user_id)
    set_columns(busy, task_kind=IngestTaskKind.reread.value, task_state=IngestTaskState.queued.value)

    def merge(source: UUID, target: UUID | str, headers: dict[str, str] = user.token) -> Any:
        return api_client.post(job_url(source, "merge"), json={"intoJobId": str(target)}, headers=headers)

    detail = assert_code(merge(two, three), 409, "too_many_pages")
    assert detail["max"] == limits.MAX_PAGES_PER_CARD
    assert_code(merge(one, committed), 409, "invalid_status")
    assert_code(merge(one, busy), 409, "busy")
    assert_code(merge(busy, one), 409, "busy")
    assert_code(merge(one, one), 422, "same_card")
    assert_code(merge(one, uuid4()), 404, "not_found")

    other = household_member(api_client, admin_token, user)
    assert _permissions(api_client, other, one)["canMerge"] is False
    assert_code(merge(one, two, other.token), 403, "forbidden")

    # nothing moved
    for job_id, count in ((three, 3), (two, 2), (one, 1)):
        assert len(parse_pages(job_row(job_id)["pages"])) == count
        assert len(_pages_on_disk(user, job_id)) == count


def test_a_merge_that_loses_the_race_puts_the_files_back(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    """Another write to either card between the read and the write: nothing changes, and the page goes home"""
    from mealie.services.ai.ingest import review

    user = unique_user_fn_scoped
    front, back = seed_job(user, created_by=user.user_id), seed_job(user, created_by=user.user_id)
    real = review.ReviewService._write_merge

    def racing(self: Any, source: Any, target: Any, pages: Any) -> bool:
        set_columns(target.id, row_version=target.row_version + 1)  # a save landed meanwhile
        return real(self, source, target, pages)

    monkeypatch.setattr(review.ReviewService, "_write_merge", racing)
    response = api_client.post(job_url(back, "merge"), json={"intoJobId": str(front)}, headers=user.token)
    assert response.status_code == 409
    assert len(parse_pages(job_row(front)["pages"])) == 1
    assert job_row(back)["status"] == "ready"
    assert _pages_on_disk(user, front) == ["0"]
    assert _pages_on_disk(user, back) == ["0"]
    assert api_client.get(job_url(back, "pages", 0, "view"), headers=user.token).status_code == 200


# ==================================================================================================================
# Undo a commit


def _uncommit(api_client: TestClient, user: TestUser, job_id: UUID, **body: Any) -> Any:
    return api_client.post(job_url(job_id, "uncommit"), json=body, headers=user.token)


def test_undo_deletes_the_recipe_and_brings_the_card_back(
    api_client: TestClient, unique_user_fn_scoped: TestUser, events: list
):
    user = unique_user_fn_scoped
    job_id = ready_to_commit(user)
    out = commit(api_client, user, job_id).json()
    before = job_row(job_id)
    assert _permissions(api_client, user, job_id)["canUncommit"] is True

    response = _uncommit(api_client, user, job_id)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ready"
    assert response.json()["draftVersion"] == before["draft_version"] + 1

    assert api_client.get(api_routes.recipes_slug(out["slug"]), headers=user.token).status_code == 404
    assert not recipe_dir(out["recipeId"]).exists()
    assert events == [(EventTypes.recipe_created, out["slug"]), (EventTypes.recipe_deleted, out["slug"])]

    row = job_row(job_id)
    assert row["status"] == "ready"
    assert row["draft"] == before["draft"]
    assert row["flags"] == before["flags"]
    for column in (
        "recipe_id",
        "commit_recipe_id",
        "commit_asset_token",
        "committed_by",
        "commit_started_at",
        "committed_at",
        "recipe_event_claimed_at",
        "recipe_event_sent_at",
    ):
        assert row[column] is None, column
    assert _pages_on_disk(user, job_id) == ["0"]

    # committed again, it's a new recipe with new asset names
    again = commit(api_client, user, job_id, version=row["draft_version"])
    assert again.status_code == 201, again.text
    assert again.json()["recipeId"] != out["recipeId"]
    assert job_row(job_id)["commit_asset_token"] != before["commit_asset_token"]


def test_a_recipe_edited_since_needs_force(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    job_id = ready_to_commit(user)
    out = commit(api_client, user, job_id).json()
    recipe = recipe_of(api_client, user, out["slug"])
    recipe["description"] = "Edited after the card was added"
    set_columns(job_id, committed_at=utcnow() - timedelta(minutes=1))
    edited = api_client.put(api_routes.recipes_slug(out["slug"]), json=recipe, headers=user.token)
    assert edited.status_code == 200, edited.text

    assert_code(_uncommit(api_client, user, job_id), 409, "recipe_edited")
    assert api_client.get(api_routes.recipes_slug(out["slug"]), headers=user.token).status_code == 200
    assert job_row(job_id)["status"] == "committed"

    assert _uncommit(api_client, user, job_id, force=True).status_code == 200
    assert api_client.get(api_routes.recipes_slug(out["slug"]), headers=user.token).status_code == 404
    assert job_row(job_id)["status"] == "ready"


def test_undo_after_the_recipe_was_deleted(api_client: TestClient, unique_user_fn_scoped: TestUser, events: list):
    user = unique_user_fn_scoped
    job_id = ready_to_commit(user)
    out = commit(api_client, user, job_id).json()
    assert api_client.delete(api_routes.recipes_slug(out["slug"]), headers=user.token).status_code == 200
    events.clear()

    assert _uncommit(api_client, user, job_id).status_code == 200
    assert job_row(job_id)["status"] == "ready"
    assert events == []


def test_undo_refusals(api_client: TestClient, admin_token: dict, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    job_id = ready_to_commit(user)
    out = commit(api_client, user, job_id).json()

    # another member: not the committer, not a manager
    member = household_member(api_client, admin_token, user)
    assert _permissions(api_client, member, job_id)["canUncommit"] is False
    assert_code(_uncommit(api_client, member, job_id), 403, "forbidden")

    # a household manager who may not delete someone else's recipe (only its owner or an admin may)
    manager = household_member(api_client, admin_token, user, canManageHousehold=True)
    assert _permissions(api_client, manager, job_id)["canUncommit"] is False
    assert_code(_uncommit(api_client, manager, job_id), 403, "forbidden")
    assert api_client.get(api_routes.recipes_slug(out["slug"]), headers=user.token).status_code == 200

    # a ready card has nothing to undo
    ready = ready_to_commit(user)
    assert _permissions(api_client, user, ready)["canUncommit"] is False
    assert_code(_uncommit(api_client, user, ready), 409, "invalid_status")

    # once the retention purge removed its photos and draft, it can't come back
    set_columns(job_id, draft=None, flags=None)
    assert _permissions(api_client, user, job_id)["canUncommit"] is False
    assert_code(_uncommit(api_client, user, job_id), 409, "purged")
    assert api_client.get(api_routes.recipes_slug(out["slug"]), headers=user.token).status_code == 200


def test_a_manager_who_owns_nothing_can_undo_when_the_recipe_is_gone(
    api_client: TestClient, admin_token: dict, unique_user_fn_scoped: TestUser
):
    user = unique_user_fn_scoped
    job_id = ready_to_commit(user)
    out = commit(api_client, user, job_id).json()
    assert api_client.delete(api_routes.recipes_slug(out["slug"]), headers=user.token).status_code == 200

    manager = household_member(api_client, admin_token, user, canManageHousehold=True)
    assert _permissions(api_client, manager, job_id)["canUncommit"] is True
    assert _uncommit(api_client, manager, job_id).status_code == 200


# ==================================================================================================================
# Commit a batch's clean cards


def _batch(user: TestUser) -> UUID:
    with session_context() as session:
        return IngestRepos(session, UUID(user.group_id), UUID(user.household_id)).batches.create(
            source=IngestSource.app, created_by=user.user_id
        )


def _commit_clean(api_client: TestClient, user: TestUser, batch_id: UUID, versions: dict[UUID, int]) -> Any:
    body = {"jobIds": [str(job_id) for job_id in versions], "draftVersions": {str(k): v for k, v in versions.items()}}
    return api_client.post(f"/api/ai/ingest/batches/{batch_id}/commit-clean", json=body, headers=user.token)


def test_the_clean_cards_of_a_batch_are_committed(
    api_client: TestClient, unique_user_fn_scoped: TestUser, events: list
):
    user = unique_user_fn_scoped
    batch_id = _batch(user)
    clean = [ready_to_commit(user, batch_id=batch_id, draft=banana_draft(name=f"Clean {n}")) for n in range(2)]
    warned_draft = banana_draft(name="Warned")
    warned_draft.ingredients[1].parse_confidence = 0.5
    warned = seed_job(
        user, batch_id=batch_id, draft=warned_draft, flags=kept(fake_compute_flags(warned_draft, None, {}))
    )
    errored = seed_job(user, batch_id=batch_id, draft=banana_draft(name="Errored"))
    stale = ready_to_commit(user, batch_id=batch_id, draft=banana_draft(name="Stale"))
    elsewhere = ready_to_commit(user, draft=banana_draft(name="Another batch"))
    done = ready_to_commit(user, batch_id=batch_id, draft=banana_draft(name="Done"))
    assert commit(api_client, user, done).status_code == 201
    assert job_row(warned)["warning_count"] == 1
    assert job_row(errored)["error_count"] == 1

    versions = dict.fromkeys([*clean, warned, errored, stale, elsewhere, done], 1)
    set_columns(stale, draft_version=2)  # saved on another device since the page listed it
    response = _commit_clean(api_client, user, batch_id, versions)
    assert response.status_code == 200, response.text
    out = response.json()

    assert [item["jobId"] for item in out["committed"]] == [str(job_id) for job_id in clean]
    assert [item["slug"] for item in out["committed"]] == ["clean-0", "clean-1"]
    assert {item["jobId"]: item["code"] for item in out["skipped"]} == {
        str(warned): "not_clean",
        str(errored): "not_clean",
        str(stale): "version_conflict",
        str(elsewhere): "not_found",
        str(done): "invalid_status",
    }
    for job_id in clean:
        assert job_row(job_id)["status"] == "committed"
    for job_id in (warned, errored, stale):
        assert job_row(job_id)["status"] == "ready"
    assert [slug for kind, slug in events if kind == EventTypes.recipe_created] == ["done", "clean-0", "clean-1"]


def test_a_warning_back_between_the_check_and_the_claim_skips_the_card(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    """The claim itself needs no unresolved warning, so a flag un-dismissed meanwhile is never committed in bulk"""
    from mealie.services.ai.ingest import commit as card_commit

    user = unique_user_fn_scoped
    batch_id = _batch(user)
    job_id = ready_to_commit(user, batch_id=batch_id)
    real_claim = card_commit._claim

    def claim(*args: Any, **kwargs: Any) -> bool:
        set_columns(job_id, warning_count=1)
        return real_claim(*args, **kwargs)

    monkeypatch.setattr(card_commit, "_claim", claim)
    out = _commit_clean(api_client, user, batch_id, {job_id: 1}).json()
    assert out == {"committed": [], "skipped": [{"jobId": str(job_id), "code": "not_clean"}]}
    assert job_row(job_id)["status"] == "ready"


def test_another_households_batch_is_not_found(
    api_client: TestClient, unique_user_fn_scoped: TestUser, h2_user: TestUser
):
    batch_id = _batch(unique_user_fn_scoped)
    job_id = ready_to_commit(unique_user_fn_scoped, batch_id=batch_id)
    assert_code(_commit_clean(api_client, h2_user, batch_id, {job_id: 1}), 404, "not_found")
    assert job_row(job_id)["status"] == "ready"
