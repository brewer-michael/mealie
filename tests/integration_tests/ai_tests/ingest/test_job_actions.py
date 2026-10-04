"""
The review page's further actions on a card (docs/ai/PHASE2.md §6, §7, §9): reading a failed local-only card with
cloud providers on purpose, adding a card to another as its back, undoing a commit, and committing a batch's clean
cards. Runs on SQLite and PostgreSQL.
"""

import os
import threading
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
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
from mealie.db.models.recipe_ingest import RecipeIngestionJob
from mealie.repos.repository_recipe_ingest import IngestQueue, IngestRepos, utcnow
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
        # a save landed meanwhile (in the merge's own transaction: the merge holds the household's lock)
        bump = sa.update(RecipeIngestionJob).where(RecipeIngestionJob.id == target.id)
        bump = bump.values(row_version=RecipeIngestionJob.row_version + 1)
        self.session.execute(bump, execution_options={"synchronize_session": False})
        return real(self, source, target, pages)

    monkeypatch.setattr(review.ReviewService, "_write_merge", racing)
    response = api_client.post(job_url(back, "merge"), json={"intoJobId": str(front)}, headers=user.token)
    assert response.status_code == 409
    assert len(parse_pages(job_row(front)["pages"])) == 1
    assert job_row(back)["status"] == "ready"
    assert _pages_on_disk(user, front) == ["0"]
    assert _pages_on_disk(user, back) == ["0"]
    assert _merge_notes(user, front) == []
    assert api_client.get(job_url(back, "pages", 0, "view"), headers=user.token).status_code == 200


def _merge_notes(user: TestUser, job_id: UUID) -> list[str]:
    return sorted(path.name for path in storage.job_dir(UUID(user.group_id), job_id).glob(".merge-*"))


def _merge(api_client: TestClient, user: TestUser, source: UUID, target: UUID) -> Any:
    return api_client.post(job_url(source, "merge"), json={"intoJobId": str(target)}, headers=user.token)


def _every_listed_page_on_disk(user: TestUser, *job_ids: UUID) -> None:
    for job_id in job_ids:
        row = job_row(job_id)
        if not row:
            continue
        for page in parse_pages(row["pages"]):
            page_dir = storage.page_dir(UUID(user.group_id), job_id, page.index)
            assert (page_dir / "page.jpg").is_file(), f"{job_id} page {page.index}"


def test_merges_of_one_household_take_turns(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    """
    Card 3 added as the back of card 2 while card 2 is added as the back of card 1: the second merge waits for the
    first, then reads card 2 as the first left it (with card 3's page, being read again), so no photo is lost
    """
    from mealie.services.ai.ingest import review

    user = unique_user_fn_scoped
    one, two, three = (seed_job(user, created_by=user.user_id, position=n) for n in range(3))
    hashes = [parse_pages(job_row(job_id)["pages"])[0].page_sha256 for job_id in (one, two, three)]
    real = review.ReviewService._write_merge
    answers: dict[str, int] = {}

    def second() -> None:
        answers["2 into 1"] = _merge(api_client, user, two, one).status_code

    other = threading.Thread(target=second)

    def interleaved(self: Any, source: Any, target: Any, pages: Any) -> bool:
        if not other.is_alive() and "2 into 1" not in answers:
            # card 3's page is in card 2's folder now: the other merge starts, and gets as far as it can
            other.start()
            other.join(timeout=1.5)
        return real(self, source, target, pages)

    monkeypatch.setattr(review.ReviewService, "_write_merge", interleaved)
    answers["3 into 2"] = _merge(api_client, user, three, two).status_code
    other.join(timeout=30)

    assert answers == {"3 into 2": 202, "2 into 1": 409}  # card 2 is being read again with its new back
    assert job_row(three) == {}
    assert [page.page_sha256 for page in parse_pages(job_row(two)["pages"])] == hashes[1:]
    assert [page.page_sha256 for page in parse_pages(job_row(one)["pages"])] == hashes[:1]
    _every_listed_page_on_disk(user, one, two)
    assert _pages_on_disk(user, two) == ["0", "1"]
    assert not storage.job_dir(UUID(user.group_id), three).exists()
    assert _merge_notes(user, one) == _merge_notes(user, two) == []

    # once card 2 is read, it can become card 1's back, with all its pages
    set_columns(two, task_kind=None, task_state=None)
    assert _merge(api_client, user, two, one).status_code == 202
    assert [page.page_sha256 for page in parse_pages(job_row(one)["pages"])] == hashes
    _every_listed_page_on_disk(user, one)
    assert not storage.job_dir(UUID(user.group_id), two).exists()


def test_opposite_merges_at_once_lose_nothing(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    """Card X added to card Y while Y is added to X: one wins, the other is refused, both photos stay"""
    from mealie.services.ai.ingest import review

    user = unique_user_fn_scoped
    x, y = seed_job(user, created_by=user.user_id), seed_job(user, created_by=user.user_id, position=1)
    hashes = [parse_pages(job_row(job_id)["pages"])[0].page_sha256 for job_id in (y, x)]
    real = review.ReviewService._write_merge
    answers: dict[str, int] = {}

    def second() -> None:
        answers["y into x"] = _merge(api_client, user, y, x).status_code

    other = threading.Thread(target=second)

    def interleaved(self: Any, source: Any, target: Any, pages: Any) -> bool:
        if not other.is_alive() and "y into x" not in answers:
            other.start()
            other.join(timeout=1.5)
        return real(self, source, target, pages)

    monkeypatch.setattr(review.ReviewService, "_write_merge", interleaved)
    answers["x into y"] = _merge(api_client, user, x, y).status_code
    other.join(timeout=30)

    assert answers["x into y"] == 202
    assert answers["y into x"] in (404, 409)
    assert job_row(x) == {}
    assert [page.page_sha256 for page in parse_pages(job_row(y)["pages"])] == hashes
    _every_listed_page_on_disk(user, y)


def _stopped_merge(user: TestUser, source: UUID, target: UUID) -> None:
    """What a stop between moving the source's page and writing the merge leaves: the note and the moved page"""
    from mealie.services.ai.ingest import review

    group_id = UUID(user.group_id)
    first = len(parse_pages(job_row(target)["pages"]))
    with storage.ingest_write():
        review._MergeMarker.write(group_id, source, target, [(0, first)])
        os.rename(storage.page_dir(group_id, source, 0), storage.page_dir(group_id, target, first))


def test_a_merge_a_stop_left_before_its_write_is_undone(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    front, back = seed_job(user, created_by=user.user_id), seed_job(user, created_by=user.user_id, position=1)
    _stopped_merge(user, back, front)
    assert _pages_on_disk(user, back) == [] and _pages_on_disk(user, front) == ["0", "1"]

    # the back's photo is found where the merge left it, and put back before it's served
    assert api_client.get(job_url(back, "pages", 0, "view"), headers=user.token).status_code == 200
    assert _pages_on_disk(user, back) == ["0"] and _pages_on_disk(user, front) == ["0"]
    assert _merge_notes(user, front) == []

    # and the merge can be made again
    assert _merge(api_client, user, back, front).status_code == 202
    assert _pages_on_disk(user, front) == ["0", "1"]
    _every_listed_page_on_disk(user, front)


def test_a_merge_into_a_card_settles_what_a_stop_left_there_first(
    api_client: TestClient, unique_user_fn_scoped: TestUser
):
    """A page left in the target's folder no longer blocks every later merge into it (`files_missing`)"""
    user = unique_user_fn_scoped
    front = seed_job(user, created_by=user.user_id)
    back, other = (seed_job(user, created_by=user.user_id, position=n) for n in (1, 2))
    _stopped_merge(user, back, front)

    assert _merge(api_client, user, other, front).status_code == 202
    _every_listed_page_on_disk(user, front, back)
    assert _pages_on_disk(user, back) == ["0"]  # home again
    assert _pages_on_disk(user, front) == ["0", "1"]  # the other card's page took the place
    assert _merge_notes(user, front) == []


def test_a_merge_a_stop_left_after_its_write_is_finished(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    front, back = seed_job(user, created_by=user.user_id), seed_job(user, created_by=user.user_id, position=1)
    assert _merge(api_client, user, back, front).status_code == 202
    set_columns(front, task_kind=None, task_state=None)  # read again since

    # the stop came before the note and the back's empty folder were removed
    from mealie.services.ai.ingest import review

    group_id = UUID(user.group_id)
    with storage.ingest_write():
        review._MergeMarker.write(group_id, back, front, [(0, 1)])
        (storage.job_dir(group_id, back) / "pages").mkdir(parents=True)

    assert api_client.get(job_url(front, "pages", 1, "view"), headers=user.token).status_code == 200
    third = seed_job(user, created_by=user.user_id, position=2)
    assert _merge(api_client, user, third, front).status_code == 202
    assert _pages_on_disk(user, front) == ["0", "1", "2"]
    _every_listed_page_on_disk(user, front)
    assert _merge_notes(user, front) == []
    assert not storage.job_dir(group_id, back).exists()


def test_the_page_of_a_card_discarded_after_a_stop_left_its_merge_goes(
    api_client: TestClient, unique_user_fn_scoped: TestUser
):
    user = unique_user_fn_scoped
    front, back = seed_job(user, created_by=user.user_id), seed_job(user, created_by=user.user_id, position=1)
    _stopped_merge(user, back, front)
    assert api_client.delete(job_url(back), headers=user.token).status_code == 204

    other = seed_job(user, created_by=user.user_id, position=2)
    assert _merge(api_client, user, other, front).status_code == 202
    pages = parse_pages(job_row(front)["pages"])
    assert [page.index for page in pages] == [0, 1]
    _every_listed_page_on_disk(user, front)
    assert _merge_notes(user, front) == []


@pytest.mark.parametrize("failed", [False, True])
def test_a_card_kept_local_keeps_the_card_it_joins_local(
    api_client: TestClient, admin_token: dict, unique_user_fn_scoped: TestUser, failed: bool
):
    """
    Photos sent to stay on this server never reach a cloud provider through the card they're added to: the card
    becomes local only, and only "Read with cloud" (the user's consent) lifts that (§10)
    """
    from mealie.services.ai.ingest.runner import worker

    user = unique_user_fn_scoped
    member = household_member(api_client, admin_token, user)
    front = seed_job(user, created_by=user.user_id, local_only=False)
    back = (
        _failed_local_only(member, position=1)  # nothing local could read it
        if failed
        else seed_job(user, created_by=member.user_id, local_only=True, position=1)  # read locally
    )
    _group_local_only(user, False)

    assert _merge(api_client, user, back, front).status_code == 202  # a household manager
    row = job_row(front)
    assert (row["local_only"], row["task_state"]) == (True, "queued")
    assert api_client.get(job_url(front), headers=user.token).json()["localOnly"] is True

    token = uuid4()
    with session_context() as session:
        assert IngestQueue(session).claim(front, token=token, owner="test", now=utcnow(), group_cap=0)
        session.commit()
    claimed = worker._load_job(front, token)
    assert claimed is not None and claimed.local_only is True


def test_a_card_not_kept_local_leaves_the_card_it_joins_as_it_was(
    api_client: TestClient, unique_user_fn_scoped: TestUser
):
    user = unique_user_fn_scoped
    local_front = seed_job(user, created_by=user.user_id, local_only=True)
    back = seed_job(user, created_by=user.user_id, local_only=False, position=1)
    assert _merge(api_client, user, back, local_front).status_code == 202
    assert job_row(local_front)["local_only"] is True


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


def test_an_undo_the_purge_overtakes_leaves_the_recipe(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, events: list
):
    """
    The retention purge removing the card's photos and draft between the undo's checks and its update: the recipe is
    kept (the card can't come back), never deleted with nothing to show for it
    """
    from mealie.services.recipe.recipe_service import RecipeService

    user = unique_user_fn_scoped
    job_id = ready_to_commit(user)
    out = commit(api_client, user, job_id).json()
    events.clear()
    real = RecipeService.can_delete

    def purged_meanwhile(self: RecipeService, slugs: list[str]) -> bool:
        set_columns(job_id, draft=None, flags=None)  # the purge's own update, committed meanwhile
        return real(self, slugs)

    monkeypatch.setattr(RecipeService, "can_delete", purged_meanwhile)
    assert_code(_uncommit(api_client, user, job_id, force=True), 409, "purged")
    assert api_client.get(api_routes.recipes_slug(out["slug"]), headers=user.token).status_code == 200
    assert recipe_dir(out["recipeId"]).exists()
    row = job_row(job_id)
    assert (row["status"], row["recipe_id"]) == ("committed", UUID(out["recipeId"]))
    assert events == []


def test_an_undo_whose_recipe_isnt_deleted_leaves_the_card_committed(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, events: list
):
    """Upstream's delete commits in steps: one failing after the first leaves the card committed to its recipe"""
    from mealie.repos.repository_recipes import RepositoryRecipes

    user = unique_user_fn_scoped
    job_id = ready_to_commit(user)
    out = commit(api_client, user, job_id).json()
    before = job_row(job_id)
    events.clear()

    def fails_halfway(self: RepositoryRecipes, recipe: Any) -> Any:
        self.session.commit()  # its first step (the ratings) is committed, with the card's update
        raise RuntimeError("the delete failed")

    real = RepositoryRecipes._delete_recipe
    monkeypatch.setattr(RepositoryRecipes, "_delete_recipe", fails_halfway)
    with pytest.raises(RuntimeError):
        _uncommit(api_client, user, job_id, force=True)

    assert api_client.get(api_routes.recipes_slug(out["slug"]), headers=user.token).status_code == 200
    row = job_row(job_id)
    for column in ("status", "draft_version", "recipe_id", "commit_recipe_id", "commit_asset_token", "committed_at"):
        assert row[column] == before[column], column
    assert events == []

    # and it can still be undone
    monkeypatch.setattr(RepositoryRecipes, "_delete_recipe", real)
    assert _uncommit(api_client, user, job_id, force=True).status_code == 200
    assert job_row(job_id)["status"] == "ready"


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


def test_a_batchs_clean_cards_are_committed_in_chunks(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, events: list
):
    """
    The page sends a large batch's clean cards a few at a time, one request after another: each answers for its own
    cards, a chunk sent again (after a timeout) commits nothing twice, and a card whose commit fails is left ready for
    review, never stuck being added
    """
    from mealie.services.ai.ingest import commit as card_commit

    user = unique_user_fn_scoped
    batch_id = _batch(user)
    cards = [ready_to_commit(user, batch_id=batch_id, draft=banana_draft(name=f"Card {n}")) for n in range(7)]
    real_create = card_commit._create_recipe

    def create(*args: Any) -> Any:
        if args[-1].name == "Card 3":
            raise RuntimeError("the insert failed")
        return real_create(*args)

    monkeypatch.setattr(card_commit, "_create_recipe", create)
    answers = [_commit_clean(api_client, user, batch_id, dict.fromkeys(chunk, 1)) for chunk in (cards[:5], cards[5:])]
    assert [response.status_code for response in answers] == [200, 200]
    first, second = (response.json() for response in answers)
    assert [item["jobId"] for item in first["committed"]] == [str(job_id) for job_id in cards[:5] if job_id != cards[3]]
    assert first["skipped"] == [{"jobId": str(cards[3]), "code": "internal_error"}]
    assert [item["jobId"] for item in second["committed"]] == [str(job_id) for job_id in cards[5:]]
    assert second["skipped"] == []

    statuses = [job_row(job_id)["status"] for job_id in cards]
    assert statuses == ["committed"] * 3 + ["ready"] + ["committed"] * 3
    assert job_row(cards[3])["error_code"] == "commit_interrupted"

    # the first chunk sent again: nothing is committed twice, and the failed card is added now
    monkeypatch.setattr(card_commit, "_create_recipe", real_create)
    again = _commit_clean(api_client, user, batch_id, dict.fromkeys(cards[:5], 1)).json()
    assert [item["jobId"] for item in again["committed"]] == [str(cards[3])]
    assert {item["code"] for item in again["skipped"]} == {"invalid_status"}
    assert len([slug for kind, slug in events if kind == EventTypes.recipe_created]) == 7


def test_clean_cards_added_in_a_public_household_dont_show_the_card(
    api_client: TestClient, unique_user_fn_scoped: TestUser
):
    """Nobody opened these cards, so nobody saw the public-photo warning: the card is neither the image nor attached"""
    user = unique_user_fn_scoped
    preferences = api_client.get(api_routes.households_preferences, headers=user.token).json()
    preferences.update({"privateHousehold": False, "recipePublic": True})
    assert api_client.put(api_routes.households_preferences, json=preferences, headers=user.token).status_code == 200
    batch_id = _batch(user)
    cards = [ready_to_commit(user, batch_id=batch_id, draft=banana_draft(name=f"Public {n}")) for n in range(2)]

    out = _commit_clean(api_client, user, batch_id, dict.fromkeys(cards, 1)).json()
    assert len(out["committed"]) == 2
    for item in out["committed"]:
        recipe = api_client.get(api_routes.recipes_slug(item["slug"]), headers=user.token).json()
        assert (recipe["image"], recipe["assets"], recipe["settings"]["public"]) == (None, [], True)
        assert not (recipe_dir(item["recipeId"]) / "images" / "original.webp").exists()
        anonymous = api_client.get(f"/api/media/recipes/{item['recipeId']}/images/original.webp")
        assert anonymous.status_code == 404
