"""
Turning a page from the review page is crash-safe (docs/ai/PHASE2.md §4.4): the turned files are staged beside the
page, its metadata is stored, then the staged files are swapped in, all under the page's turn lock. A stop anywhere in
between is settled by the next look at the page (its image, another turn, a commit or an eval export), and a refused
turn leaves nothing behind. Runs on SQLite and PostgreSQL.
"""

import hashlib
import io
import threading
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from test_jobs_api import (
    assert_code,
    banana_draft,
    fake_compute_flags,
    job_row,
    job_url,
    seed_job,
    set_columns,
    use_fake_flags,
)

from mealie.core.config import get_app_dirs
from mealie.db.db_setup import session_context
from mealie.repos.all_repositories import get_repositories
from mealie.repos.repository_recipe_ingest import IngestJobsRepo, IngestRepos
from mealie.schema.recipe_ingest import CardFlag, FlagResolution, PageMeta, PageOut, PageRotationSource
from mealie.services.ai.ingest import images, review, storage
from mealie.services.ai.ingest.review import ReviewService
from tests.utils.fixture_schemas import TestUser


@pytest.fixture(autouse=True)
def _fake_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    use_fake_flags(monkeypatch)


class Crash(BaseException):
    """The process stopping mid-turn: nothing after the raise runs, and no `except Exception` sees it"""


def page_dir(user: TestUser, job_id: UUID, index: int = 0) -> Any:
    return storage.page_dir(UUID(user.group_id), job_id, index)


def staged_files(user: TestUser, job_id: UUID, index: int = 0) -> list[str]:
    directory = page_dir(user, job_id, index)
    return sorted(name for name in images.STAGED_FILES.values() if (directory / name).exists())


def stored_page(job_id: UUID, index: int = 0) -> dict[str, Any]:
    return next(page for page in job_row(job_id)["pages"] if page["index"] == index)


def page_sha256(user: TestUser, job_id: UUID, index: int = 0) -> str:
    return hashlib.sha256((page_dir(user, job_id, index) / images.PAGE_FILE).read_bytes()).hexdigest()


def assert_consistent(user: TestUser, job_id: UUID, index: int = 0) -> dict[str, Any]:
    """The page's files are the ones its stored metadata describes, and nothing is left staged"""
    stored = stored_page(job_id, index)
    assert staged_files(user, job_id, index) == []
    assert page_sha256(user, job_id, index) == stored["page_sha256"]
    with Image.open(page_dir(user, job_id, index) / images.PAGE_FILE) as page:
        assert page.size == (stored["width"], stored["height"])
    with Image.open(page_dir(user, job_id, index) / images.VIEW_FILE) as view:
        assert view.size == (stored["view_width"], stored["view_height"])
    return stored


def dark_corner(path: Any) -> str:
    """Which corner of an image holds the dark block `card_photo` draws in its top-left corner"""
    with Image.open(path) as image:
        rgb = image.convert("RGB")
    width, height = rgb.size
    corners = {
        "top-left": (2, 2),
        "top-right": (width - 3, 2),
        "bottom-left": (2, height - 3),
        "bottom-right": (width - 3, height - 3),
    }
    [corner] = [name for name, xy in corners.items() if sum(rgb.getpixel(xy)[:3]) < 200]  # type: ignore[index]
    return corner


def rotate(user: TestUser, job_id: UUID, degrees: int = 90, index: int = 0) -> PageOut:
    """A rotate request, as the route makes it (inside the ingest write lock)"""
    with session_context() as session:
        repos = IngestRepos(session, UUID(user.group_id), UUID(user.household_id))
        private = get_repositories(session, group_id=UUID(user.group_id), household_id=None).users.get_one(user.user_id)
        assert private is not None
        with storage.ingest_write():
            return ReviewService(repos, private).rotate(job_id, index, degrees)


def crash_once(monkeypatch: pytest.MonkeyPatch, owner: Any, name: str) -> None:
    """`owner.name` raises `Crash` the first time it's called, then works as before"""
    original: Callable[..., Any] = getattr(owner, name)
    calls = 0

    def crashing(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise Crash()
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, crashing)


def crash_after_the_metadata_is_stored(user: TestUser, job_id: UUID, monkeypatch: pytest.MonkeyPatch) -> None:
    """A turn whose metadata was stored, stopped before its staged files were swapped in"""
    crash_once(monkeypatch, images, "apply_staged")
    with pytest.raises(Crash):
        rotate(user, job_id, 90)
    monkeypatch.undo()

    stored = stored_page(job_id)
    assert (stored["rotation"], stored["rotation_source"]) == (90, "user")
    assert staged_files(user, job_id) == ["page.next.jpg", "thumb.next.webp", "view.next.jpg"]
    assert page_sha256(user, job_id) != stored["page_sha256"]  # still the page as it was


# ==================================================================================================================
# The turn


def test_a_turn_swaps_its_staged_files_in(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    job_id = seed_job(user)

    response = api_client.post(job_url(job_id, "pages", 0, "rotate"), json={"degrees": 90}, headers=user.token)
    assert response.status_code == 200
    stored = assert_consistent(user, job_id)
    assert (stored["rotation"], stored["width"], stored["height"]) == (90, 640, 480)
    assert dark_corner(page_dir(user, job_id) / images.PAGE_FILE) == "top-right"


def test_a_stop_before_the_metadata_is_stored_leaves_the_page_as_it_was(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    job_id = seed_job(user)
    before = stored_page(job_id)
    page_bytes = (page_dir(user, job_id) / images.PAGE_FILE).read_bytes()

    crash_once(monkeypatch, IngestJobsRepo, "update_job_json")
    with pytest.raises(Crash):
        rotate(user, job_id, 90)
    monkeypatch.undo()

    # the files the stored metadata names are untouched; the turn waits beside them, unstored
    assert stored_page(job_id) == before
    assert (page_dir(user, job_id) / images.PAGE_FILE).read_bytes() == page_bytes
    assert staged_files(user, job_id) == ["page.next.jpg", "thumb.next.webp", "view.next.jpg"]

    # the next look at the page discards it
    view = api_client.get(job_url(job_id, "pages", 0, "view"), headers=user.token)
    assert view.status_code == 200
    assert view.headers["etag"].endswith('-r0-view"')
    with Image.open(io.BytesIO(view.content)) as image:
        assert image.size == (480, 640)
    assert assert_consistent(user, job_id) == before


def test_a_stop_after_the_metadata_is_stored_is_finished_by_the_next_image_request(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    job_id = seed_job(user)
    crash_after_the_metadata_is_stored(user, job_id, monkeypatch)

    thumb = api_client.get(job_url(job_id, "pages", 0, "thumb"), headers=user.token)
    assert thumb.status_code == 200
    assert thumb.headers["etag"].endswith('-r90-thumb"')
    with Image.open(io.BytesIO(thumb.content)) as image:
        assert image.size == (480, 360)
    stored = assert_consistent(user, job_id)
    assert dark_corner(page_dir(user, job_id) / images.PAGE_FILE) == "top-right"

    page = api_client.get(job_url(job_id), headers=user.token).json()["pages"][0]
    assert (page["rotation"], page["width"], page["height"]) == (stored["rotation"], 640, 480)


def test_a_stop_after_the_metadata_is_stored_is_finished_by_the_next_turn(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    job_id = seed_job(user)
    crash_after_the_metadata_is_stored(user, job_id, monkeypatch)

    response = api_client.post(job_url(job_id, "pages", 0, "rotate"), json={"degrees": 90}, headers=user.token)
    assert response.status_code == 200
    assert response.json()["rotation"] == 180
    stored = assert_consistent(user, job_id)
    assert (stored["rotation"], stored["width"], stored["height"]) == (180, 480, 640)
    # both quarter turns happened: the first finished, then the second turned that
    assert dark_corner(page_dir(user, job_id) / images.PAGE_FILE) == "bottom-right"


def test_a_partial_swap_is_finished(api_client: TestClient, unique_user_fn_scoped: TestUser):
    """A stop part-way through the swap itself: the thumbnail already moved, the view and the page not"""
    user = unique_user_fn_scoped
    job_id = seed_job(user)
    directory = page_dir(user, job_id)
    before = stored_page(job_id)
    with storage.ingest_write():
        turned = images.stage_rotation(directory, PageMeta.model_validate(before), 270, PageRotationSource.user)
        (directory / images.STAGED_FILES[images.THUMB_FILE]).replace(directory / images.THUMB_FILE)
    pages = [turned.model_dump(mode="json")]
    set_columns(job_id, pages=pages)

    view = api_client.get(job_url(job_id, "pages", 0, "view"), headers=user.token)
    assert view.status_code == 200
    with Image.open(io.BytesIO(view.content)) as image:
        assert image.size == (640, 480)
    assert assert_consistent(user, job_id)["rotation"] == 270
    assert dark_corner(directory / images.PAGE_FILE) == "bottom-left"


# ==================================================================================================================
# Refusals


def test_a_refused_turn_leaves_no_staged_files(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    """A task queued while the page was being turned: the metadata write is refused and the staged files go"""
    user = unique_user_fn_scoped
    job_id = seed_job(user)
    before = stored_page(job_id)
    page_bytes = (page_dir(user, job_id) / images.PAGE_FILE).read_bytes()
    stage = images.stage_rotation

    def stage_then_queue(*args: Any, **kwargs: Any) -> Any:
        turned = stage(*args, **kwargs)
        set_columns(job_id, task_kind="extract", task_state="queued")
        return turned

    monkeypatch.setattr(images, "stage_rotation", stage_then_queue)
    response = api_client.post(job_url(job_id, "pages", 0, "rotate"), json={"degrees": 90}, headers=user.token)
    assert_code(response, 409, "busy")

    assert stored_page(job_id) == before
    assert staged_files(user, job_id) == []
    # no turn-back rewrite: the page is byte for byte the one stored
    assert (page_dir(user, job_id) / images.PAGE_FILE).read_bytes() == page_bytes
    assert_consistent(user, job_id)


def test_a_page_changed_meanwhile_is_not_overwritten(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    """The metadata write only lands on the page that was turned: one written meanwhile wins, and the turn is undone"""
    user = unique_user_fn_scoped
    job_id = seed_job(user)
    stage = images.stage_rotation

    def stage_then_change(*args: Any, **kwargs: Any) -> Any:
        turned = stage(*args, **kwargs)
        set_columns(job_id, pages=[{**stored_page(job_id), "page_sha256": "f" * 64}])
        return turned

    monkeypatch.setattr(images, "stage_rotation", stage_then_change)
    response = api_client.post(job_url(job_id, "pages", 0, "rotate"), json={"degrees": 90}, headers=user.token)
    assert_code(response, 409, "busy")
    assert stored_page(job_id)["page_sha256"] == "f" * 64
    assert staged_files(user, job_id) == []


# ==================================================================================================================
# Everything that reads a page's files settles it first


def test_commit_copies_the_page_a_stop_left_staged(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    draft = banana_draft(attach_card_photo=True)
    flags: list[CardFlag] = [
        flag.model_copy(update={"resolution": FlagResolution.kept}) if flag.severity == "error" else flag
        for flag in fake_compute_flags(draft, None, {})
    ]
    job_id = seed_job(user, draft=draft, flags=flags)
    crash_after_the_metadata_is_stored(user, job_id, monkeypatch)

    response = api_client.post(job_url(job_id, "commit"), json={"draftVersion": 1}, headers=user.token)
    assert response.status_code == 201, response.text
    out = response.json()
    token = job_row(job_id)["commit_asset_token"]
    asset = get_app_dirs().RECIPE_DATA_DIR / out["recipeId"] / "assets" / f"recipe-card-{token}-1.jpg"
    with Image.open(asset) as image:
        assert image.size == (640, 480)  # the turned page, as its metadata says
    assert dark_corner(asset) == "top-right"
    assert_consistent(user, job_id)


def test_an_eval_case_is_saved_from_the_page_a_stop_left_staged(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    job_id = seed_job(user)
    crash_after_the_metadata_is_stored(user, job_id, monkeypatch)

    response = api_client.post(
        f"/api/ai/ingest/jobs/{job_id}/eval-case", json={"slug": f"turned-{job_id.hex[:8]}"}, headers=user.token
    )
    assert response.status_code == 201, response.text
    assert_consistent(user, job_id)
    # turned back by its recorded rotation: the photo as it was uploaded
    case = storage.eval_cards_dir(UUID(user.group_id)) / f"turned-{job_id.hex[:8]}-1.jpg"
    try:
        with Image.open(case) as image:
            assert image.size == (480, 640)
        assert dark_corner(case) == "top-left"
    finally:
        for path in storage.eval_cards_dir(UUID(user.group_id)).glob(f"turned-{job_id.hex[:8]}*"):
            path.unlink()


def test_a_page_a_task_is_turning_is_served_uncached_and_left_to_it(
    api_client: TestClient, unique_user_fn_scoped: TestUser
):
    """The runner owns a page's files while its task runs: an image request doesn't settle them, and isn't cached"""
    user = unique_user_fn_scoped
    job_id = seed_job(user, task_kind="extract", task_state="running", lease_token=uuid4())
    with storage.ingest_write():
        images.stage_rotation(
            page_dir(user, job_id), PageMeta.model_validate(stored_page(job_id)), 90, PageRotationSource.ocr
        )

    view = api_client.get(job_url(job_id, "pages", 0, "view"), headers=user.token)
    assert view.status_code == 200
    assert view.headers["cache-control"] == "no-store"
    assert '-r0-view"' not in view.headers.get("etag", "")
    assert staged_files(user, job_id) == ["page.next.jpg", "thumb.next.webp", "view.next.jpg"]

    # the task ended without storing the turn (a crash): the next request settles it, and caches again
    set_columns(job_id, task_kind=None, task_state=None)
    view = api_client.get(job_url(job_id, "pages", 0, "view"), headers=user.token)
    assert view.headers["cache-control"] == "private, no-cache"
    assert view.headers["etag"].endswith('-r0-view"')
    assert_consistent(user, job_id)


def test_an_image_is_served_uncached_while_a_restore_pauses_ingestion(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    job_id = seed_job(user)
    crash_after_the_metadata_is_stored(user, job_id, monkeypatch)

    marker = storage.pause_marker_path()
    marker.write_text(f"{time.time():.3f}")
    try:
        view = api_client.get(job_url(job_id, "pages", 0, "view"), headers=user.token)
        assert view.status_code == 200
        assert view.headers["cache-control"] == "no-store"
        assert len(staged_files(user, job_id)) == 3  # nothing is written while paused
    finally:
        marker.unlink(missing_ok=True)

    view = api_client.get(job_url(job_id, "pages", 0, "view"), headers=user.token)
    assert view.headers["etag"].endswith('-r90-view"')
    assert_consistent(user, job_id)


# ==================================================================================================================
# Concurrency


def test_an_image_request_during_a_turn_waits_for_it(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    """
    An image request that finds a turn staged, while the turn is still being stored, waits for the page's turn lock
    instead of settling (and so discarding) the staged files under it
    """
    user = unique_user_fn_scoped
    job_id = seed_job(user)
    staged, release = threading.Event(), threading.Event()
    store = IngestJobsRepo.update_job_json

    def slow_store(self: IngestJobsRepo, *args: Any, **kwargs: Any) -> Any:
        staged.set()
        assert release.wait(20)
        return store(self, *args, **kwargs)

    monkeypatch.setattr(IngestJobsRepo, "update_job_json", slow_store)
    results: dict[str, Any] = {}

    def turn() -> None:
        results["turn"] = api_client.post(
            job_url(job_id, "pages", 0, "rotate"), json={"degrees": 90}, headers=user.token
        )

    def look() -> None:
        results["view"] = api_client.get(job_url(job_id, "pages", 0, "view"), headers=user.token)

    turning = threading.Thread(target=turn)
    turning.start()
    assert staged.wait(20)
    looking = threading.Thread(target=look)
    looking.start()
    looking.join(0.5)
    assert looking.is_alive()  # waiting for the turn
    assert staged_files(user, job_id) == ["page.next.jpg", "thumb.next.webp", "view.next.jpg"]

    release.set()
    turning.join(30)
    looking.join(30)
    assert results["turn"].status_code == 200
    view = results["view"]
    assert view.status_code == 200
    assert view.headers["etag"].endswith('-r90-view"')
    with Image.open(io.BytesIO(view.content)) as image:
        assert image.size == (640, 480)
    assert assert_consistent(user, job_id)["rotation"] == 90


def test_two_turns_of_one_page_both_land(api_client: TestClient, unique_user_fn_scoped: TestUser):
    """Two devices turning the same page at once: one waits for the other, so the page ends half a turn round"""
    user = unique_user_fn_scoped
    job_id = seed_job(user)
    barrier = threading.Barrier(2, timeout=20)
    results: list[Any] = []

    def turn() -> None:
        barrier.wait()
        results.append(api_client.post(job_url(job_id, "pages", 0, "rotate"), json={"degrees": 90}, headers=user.token))

    threads = [threading.Thread(target=turn) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert sorted(response.status_code for response in results) == [200, 200]
    assert sorted(response.json()["rotation"] for response in results) == [90, 180]
    assert assert_consistent(user, job_id)["rotation"] == 180
    assert dark_corner(page_dir(user, job_id) / images.PAGE_FILE) == "bottom-right"


def test_the_turn_lock_is_released_after_a_stop(unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch):
    user = unique_user_fn_scoped
    job_id = seed_job(user)
    crash_after_the_metadata_is_stored(user, job_id, monkeypatch)
    with review.page_turn_lock(page_dir(user, job_id), wait=1):
        pass
