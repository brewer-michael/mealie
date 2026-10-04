"""
Upstream's writes wait for, and stop during, a backup restore (restore_guard.py, docs/ai/PHASE2.md §3.9): while a
restore is pending or running an upstream API write gets 503 and runs nothing, and a restore waits for the upstream
writes already in flight, background tasks included.
"""

import asyncio
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import anyio
import pytest
from fastapi.testclient import TestClient

from mealie.core.config import get_app_settings
from mealie.services.ai.ingest import limits, restore_guard, storage
from mealie.services.recipe.recipe_data_service import RecipeDataService
from mealie.services.scraper.recipe_bulk_scraper import RecipeBulkScraperService
from tests.utils import api_routes
from tests.utils.factories import random_string
from tests.utils.fixture_schemas import TestUser

WRITES_PAUSED = "A backup is being restored. Try again in a minute."


@pytest.fixture(autouse=True)
def quick_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "RESTORE_LOCK_POLL", 0.02)
    monkeypatch.setattr(limits, "RESTORE_LOCK_WAIT", 10)


@pytest.fixture()
def recipe_slug(api_client: TestClient, unique_user: TestUser) -> str:
    response = api_client.post(api_routes.recipes, json={"name": random_string()}, headers=unique_user.token)
    assert response.status_code == 201
    return response.json()


def upload_image(api_client: TestClient, user: TestUser, slug: str, image: Path, **headers: str) -> Any:
    with image.open("rb") as f:
        return api_client.put(
            api_routes.recipes_slug_image(slug),
            files={"image": ("cover.jpg", f, "image/jpeg")},
            data={"extension": "jpg"},
            headers={**user.token, **headers},
        )


class Restore:
    """A backup restore's pause (`storage.pauses_ingest`, as on `BackupV2.restore`) in a thread of its own"""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.restored_at: float | None = None
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _restore(self) -> None:
        self.restored_at = time.monotonic()
        self.started.set()
        self.release.wait(10)

    def _run(self) -> None:
        try:
            storage.pauses_ingest(self._restore)()
        except BaseException as e:
            self.error = e
            self.started.set()

    def __enter__(self) -> Restore:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release.set()
        self.thread.join(10)
        assert not self.thread.is_alive()


def wait_until_paused(timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while not storage.is_paused():
        assert time.monotonic() < deadline, "the restore never paused"
        time.sleep(0.01)


# ======================================================================================================================
# Which requests


@pytest.mark.parametrize(
    ("method", "path", "guarded"),
    [
        ("PUT", "/api/recipes/banana/image", True),
        ("POST", "/api/recipes", True),
        ("PATCH", "/api/recipes/banana", True),
        ("DELETE", "/api/recipes/banana", True),
        ("post", "/api/groups/migrations", True),
        ("POST", "/api/admin/backups", True),
        ("POST", "/api/admin/backups/upload", True),
        ("POST", "/api/mcp", True),
        ("GET", "/api/recipes", False),
        ("HEAD", "/api/recipes", False),
        ("OPTIONS", "/api/recipes", False),
        ("POST", "/api/admin/backups/mealie_2026.10.04.zip/restore", False),
        ("POST", "/api/auth/token", False),
        ("POST", "/api/auth/refresh", False),
        ("POST", "/api/ai/ingest", False),
        ("POST", "/api/ai/ingest/jobs/x/commit", False),
        ("POST", "/api/ai/ingestion-other", True),
        ("POST", "/api/ai/tools/x", True),
        ("POST", "/oauth/token", False),
        ("POST", "/g/home", False),
    ],
)
def test_upstream_api_writes_are_guarded(method: str, path: str, guarded: bool):
    assert restore_guard.is_guarded(method, path) is guarded


# ======================================================================================================================
# While a restore runs


def test_a_recipe_image_upload_during_a_restore_is_refused(
    api_client: TestClient, unique_user: TestUser, recipe_slug: str, test_image_jpg: str
):
    with Restore() as restore:
        wait_until_paused()
        response = upload_image(
            api_client, unique_user, recipe_slug, Path(test_image_jpg), **{"Accept-Language": "de-DE"}
        )
        assert restore.error is None

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["message"] == WRITES_PAUSED  # in en-US until the locale has it
    assert response.headers["Retry-After"] == str(limits.PAUSED_RETRY_AFTER)

    recipe = api_client.get(api_routes.recipes_slug(recipe_slug), headers=unique_user.token).json()
    assert not recipe["image"]
    assert not (RecipeDataService(recipe["id"]).dir_image / "original.webp").exists()

    # and once the restore is done
    response = upload_image(api_client, unique_user, recipe_slug, Path(test_image_jpg))
    assert response.status_code == 200, response.text


def test_reads_sign_in_and_card_routes_pass_during_a_restore(
    api_client: TestClient, unique_user: TestUser, recipe_slug: str
):
    settings = get_app_settings()
    with Restore():
        wait_until_paused()
        read = api_client.get(api_routes.recipes_slug(recipe_slug), headers=unique_user.token)
        sign_in = api_client.post(
            api_routes.auth_token,
            data={"username": settings._DEFAULT_EMAIL, "password": settings._DEFAULT_PASSWORD},
        )
        card = api_client.post(api_routes.ai_ingest, json={"images": []}, headers=unique_user.token)

    assert read.status_code == 200
    assert sign_in.status_code == 200
    # the card routes answer for themselves (a `code`), not with this guard's message
    assert card.status_code != 200
    assert "code" in card.json()["detail"], card.text


def test_the_restore_route_isnt_held_by_the_guard(api_client: TestClient, admin_token: dict, monkeypatch):
    """A second restore while one runs is the route's own busy answer, not "a backup is being restored" """
    monkeypatch.setattr(limits, "RESTORE_LOCK_WAIT", 0.2)
    with Restore():
        wait_until_paused()
        response = api_client.post(
            api_routes.admin_backups_file_name_restore("never-restored.zip"), headers=admin_token
        )

    assert response.status_code == 503
    assert response.json()["detail"]["message"] != WRITES_PAUSED


# ======================================================================================================================
# A restore waits for writes in flight


@contextmanager
def blocked_image_writes(
    monkeypatch: pytest.MonkeyPatch, written: dict[str, float]
) -> Iterator[tuple[threading.Event, threading.Event]]:
    inside, release = threading.Event(), threading.Event()
    write_image = RecipeDataService.write_image

    def slow_write_image(self: RecipeDataService, *args: Any, **kwargs: Any) -> Any:
        inside.set()
        assert release.wait(10)
        path = write_image(self, *args, **kwargs)
        written["at"] = time.monotonic()
        return path

    monkeypatch.setattr(RecipeDataService, "write_image", slow_write_image)
    try:
        yield inside, release
    finally:
        release.set()


def test_a_restore_waits_for_an_image_upload_in_flight(
    api_client: TestClient,
    unique_user: TestUser,
    recipe_slug: str,
    test_image_jpg: str,
    monkeypatch: pytest.MonkeyPatch,
):
    statuses: list[int] = []
    written: dict[str, float] = {}

    def upload() -> None:
        statuses.append(upload_image(api_client, unique_user, recipe_slug, Path(test_image_jpg)).status_code)

    with blocked_image_writes(monkeypatch, written) as (inside, release):
        uploader = threading.Thread(target=upload)
        uploader.start()
        assert inside.wait(10)

        with Restore() as restore:
            wait_until_paused()
            time.sleep(0.4)
            assert restore.restored_at is None, "the restore didn't wait for the upload"

            # a write that starts now is refused rather than queued behind the restore
            other = api_client.post(api_routes.recipes, json={"name": random_string()}, headers=unique_user.token)
            assert other.status_code == 503
            assert other.json()["detail"]["message"] == WRITES_PAUSED

            release.set()
            uploader.join(10)
            assert restore.started.wait(10)
            assert restore.error is None

    assert statuses == [200]
    assert restore.restored_at is not None and restore.restored_at >= written["at"]
    recipe = api_client.get(api_routes.recipes_slug(recipe_slug), headers=unique_user.token).json()
    assert recipe["image"]


def test_a_restore_waits_for_a_writes_background_task(
    api_client: TestClient, unique_user: TestUser, monkeypatch: pytest.MonkeyPatch
):
    inside, release = threading.Event(), threading.Event()
    finished: dict[str, float] = {}

    async def scrape(self: RecipeBulkScraperService, urls: Any) -> None:
        inside.set()
        await anyio.to_thread.run_sync(release.wait, 10)
        finished["at"] = time.monotonic()

    monkeypatch.setattr(RecipeBulkScraperService, "scrape", scrape)
    statuses: list[int] = []

    def bulk_import() -> None:
        response = api_client.post(
            api_routes.recipes_create_url_bulk,
            json={"imports": [{"url": "https://example.com/recipe"}]},
            headers=unique_user.token,
        )
        statuses.append(response.status_code)

    importer = threading.Thread(target=bulk_import)
    importer.start()
    try:
        assert inside.wait(10)  # the response is out; its background task is running
        with Restore() as restore:
            wait_until_paused()
            time.sleep(0.4)
            assert restore.restored_at is None, "the restore didn't wait for the background task"
            release.set()
            assert restore.started.wait(10)
    finally:
        release.set()
        importer.join(10)

    assert statuses == [202]
    assert restore.error is None
    assert restore.restored_at is not None and restore.restored_at >= finished["at"]


# ======================================================================================================================
# The write section


def test_a_section_entered_on_one_thread_can_be_left_on_another():
    section = storage.ingest_write()
    entering = threading.Thread(target=section.__enter__)
    entering.start()
    entering.join()

    restored: list[bool] = []
    restore = threading.Thread(target=storage.pauses_ingest(lambda: restored.append(True)))
    restore.start()
    time.sleep(0.3)
    assert not restored  # the restore waits for the section

    leaving = threading.Thread(target=section.__exit__, args=(None, None, None))
    leaving.start()
    leaving.join()
    restore.join(10)
    assert restored == [True]


class _App:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls = 0

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.calls += 1
        if self.error:
            raise self.error


def _scope(method: str = "POST", path: str = "/api/recipes") -> dict[str, Any]:
    return {"type": "http", "method": method, "path": path, "headers": []}


async def _no_receive() -> dict[str, Any]:
    return {"type": "http.disconnect"}


async def _ignore(message: Any) -> None:
    pass


def _a_restore_proceeds_at_once() -> bool:
    restored: list[bool] = []
    storage.pauses_ingest(lambda: restored.append(True))()
    return restored == [True]


def test_a_request_that_fails_leaves_its_section(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(limits, "RESTORE_LOCK_WAIT", 0.5)
    app = _App(RuntimeError("the route failed"))
    with pytest.raises(RuntimeError):
        asyncio.run(restore_guard.RestoreGuardMiddleware(app)(_scope(), _no_receive, _ignore))
    assert app.calls == 1
    assert _a_restore_proceeds_at_once()


def test_a_request_cancelled_while_entering_leaves_its_section(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(limits, "RESTORE_LOCK_WAIT", 0.5)
    entered, left = threading.Event(), threading.Event()

    class SlowSection:
        def __enter__(self) -> None:
            time.sleep(0.3)
            entered.set()

        def __exit__(self, *exc: object) -> None:
            left.set()

    monkeypatch.setattr(storage, "ingest_write", SlowSection)
    app = _App()

    async def cancelled_request() -> None:
        task = asyncio.create_task(restore_guard.RestoreGuardMiddleware(app)(_scope(), _no_receive, _ignore))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.5)

    asyncio.run(cancelled_request())
    assert app.calls == 0
    assert entered.is_set() and left.is_set()


def test_a_lock_file_that_cant_be_opened_leaves_writes_unguarded(
    api_client: TestClient, unique_user: TestUser, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    class Unopenable:
        def __enter__(self) -> None:
            raise PermissionError(13, "Permission denied", str(storage.lock_path()))

        def __exit__(self, *exc: object) -> None:
            raise AssertionError("never entered")

    monkeypatch.setattr(storage, "ingest_write", Unopenable)
    monkeypatch.setattr(restore_guard, "_lock_warning_logged", False)
    response = api_client.post(api_routes.recipes, json={"name": random_string()}, headers=unique_user.token)
    assert response.status_code == 201
    assert "Writes can't wait for a backup restore" in caplog.text
