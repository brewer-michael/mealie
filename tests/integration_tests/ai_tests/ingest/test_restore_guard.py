"""
Upstream's requests during, and writes around, a backup restore (restore_guard.py, docs/ai/PHASE2.md §3.9): while a
restore is pending or running every API request but the restore's gets 503 `paused_for_restore` and runs nothing, a
restore waits for the upstream writes already in flight, background tasks included, and no request body is copied or
holds a restore off while it arrives.
"""

import asyncio
import json
import os
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any

import anyio
import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient

from mealie.app import app
from mealie.core.config import get_app_dirs, get_app_settings
from mealie.db.db_setup import generate_session
from mealie.routes.admin import admin_backups
from mealie.services.ai.ingest import limits, restore_guard, storage
from mealie.services.ai.ingest.restore_guard import SectionStart
from mealie.services.recipe.recipe_data_service import RecipeDataService
from mealie.services.scraper.recipe_bulk_scraper import RecipeBulkScraperService
from tests.utils import api_routes
from tests.utils.factories import random_string
from tests.utils.fixture_schemas import TestUser

WRITES_PAUSED = "A backup is being restored. Try again in a minute."
CARDS_PAUSED = "Recipe card uploads are paused while a backup is restored. Try again in a minute."
MIB = 1024 * 1024


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


@pytest.mark.parametrize(
    ("path", "paused"),
    [
        ("/api/recipes", True),
        ("/api/auth/refresh", True),
        ("/api/auth/token", True),
        ("/api/ai/ingest", True),
        ("/api/ai/ingest/jobs", True),
        ("/api/mcp", True),
        ("/api/oauth/token", True),
        ("/api/media/recipes/x/images/original.webp", True),
        ("/api/does-not-exist", True),
        ("/api/admin/backups/mealie_2026.10.04.zip/restore", False),
        ("/.well-known/oauth-authorization-server", False),
        ("/g/home", False),
        ("/docs", False),
    ],
)
def test_every_api_request_but_the_restores_waits_out_a_restore(path: str, paused: bool):
    assert restore_guard.is_paused_for(path) is paused


def _app_scope(method: str, path: str, *, body: bytes | None = None, content_type: str | None = None) -> dict:
    headers: list[tuple[bytes, bytes]] = []
    if body is not None:
        headers.append((b"content-length", str(len(body)).encode()))
    if content_type is not None:
        headers.append((b"content-type", content_type.encode()))
    return {"type": "http", "method": method, "path": path, "root_path": "", "headers": headers, "app": app}


JSON, MULTIPART, FORM = "application/json", "multipart/form-data; boundary=x", "application/x-www-form-urlencoded"


@pytest.mark.parametrize(
    ("method", "path", "body", "content_type", "start"),
    [
        # a declared body: FastAPI reads it before anything else runs
        ("POST", "/api/recipes", b"{}", JSON, SectionStart.with_the_last_body_message),
        ("POST", "/api/recipes", b"{}", None, SectionStart.with_the_last_body_message),
        ("PUT", "/api/recipes/banana/image", b"--x--", MULTIPART, SectionStart.with_the_last_body_message),
        ("POST", "/api/admin/backups/upload", b"--x--", MULTIPART, SectionStart.with_the_last_body_message),
        ("POST", "/api/oauth/token", b"grant_type=x", FORM, SectionStart.with_the_last_body_message),
        # a form route Starlette won't read (another content type), or nothing to read: before the route runs
        ("PUT", "/api/recipes/banana/image", b"{}", JSON, SectionStart.before_the_route),
        ("POST", "/api/oauth/token", b"{}", JSON, SectionStart.before_the_route),
        ("POST", "/api/recipes", None, None, SectionStart.before_the_route),
        # a body the route doesn't declare (it may never read it), or a route that isn't FastAPI's
        ("POST", "/api/admin/backups", b"junk", JSON, SectionStart.before_the_route),
        ("DELETE", "/api/recipes/banana", b"junk", JSON, SectionStart.before_the_route),
        ("POST", "/api/mcp", b"{}", JSON, SectionStart.before_the_route),
        # no route takes it: it runs nothing
        ("POST", "/api/does-not-exist", b"{}", JSON, SectionStart.never),
        ("PATCH", "/api/admin/backups/upload", b"{}", JSON, SectionStart.never),
        ("POST", "/api/recipes/", b"{}", JSON, SectionStart.never),
    ],
)
def test_when_a_write_enters_its_section(
    method: str, path: str, body: bytes | None, content_type: str | None, start: SectionStart
):
    assert restore_guard.section_start(_app_scope(method, path, body=body, content_type=content_type)) is start


def test_the_routes_reading_a_form_by_hand_declare_no_body():
    """`READS_FORM_FIRST` names real routes, which FastAPI wouldn't read for the guard"""
    routes = {
        context.path: context
        for context in iter_route_contexts(app.router.routes)
        if context.path in restore_guard.READS_FORM_FIRST
    }
    assert set(routes) == restore_guard.READS_FORM_FIRST
    for context in routes.values():
        assert context.methods == {"POST"}
        assert context.body_field is None


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
    assert response.json()["detail"] == {"code": "paused_for_restore", "message": WRITES_PAUSED}  # en-US for now
    assert response.headers["Retry-After"] == str(limits.PAUSED_RETRY_AFTER)

    recipe = api_client.get(api_routes.recipes_slug(recipe_slug), headers=unique_user.token).json()
    assert not recipe["image"]
    assert not (RecipeDataService(recipe["id"]).dir_image / "original.webp").exists()

    # and once the restore is done
    response = upload_image(api_client, unique_user, recipe_slug, Path(test_image_jpg))
    assert response.status_code == 200, response.text


def test_every_api_request_gets_503_during_a_restore_before_anything_runs(
    api_client: TestClient, unique_user: TestUser, recipe_slug: str, monkeypatch: pytest.MonkeyPatch
):
    """
    The restore drops and imports every table: a sign-in check would answer 401 (and the app sign out), anything else
    500. So reads, signing in, the token refresh and the card routes all wait it out, before any database access.
    """
    settings = get_app_settings()

    def no_database() -> Iterator[Any]:
        raise AssertionError("a request used the database during the restore")
        yield

    with Restore():
        wait_until_paused()
        with monkeypatch.context() as m:
            m.setitem(app.dependency_overrides, generate_session, no_database)
            reads = [
                api_client.get(api_routes.recipes_slug(recipe_slug), headers=unique_user.token),
                api_client.get(api_routes.users_self),  # no account: not a 401
                api_client.get(api_routes.ai_ingest_jobs, headers=unique_user.token),
                api_client.get("/api/does-not-exist"),
                api_client.post(api_routes.auth_refresh, headers=unique_user.token),  # the app's own: no toast
            ]
            writes = [
                api_client.post(
                    api_routes.auth_token,
                    data={"username": settings._DEFAULT_EMAIL, "password": settings._DEFAULT_PASSWORD},
                ),
                api_client.post(api_routes.recipes, json={"name": random_string()}, headers=unique_user.token),
            ]
            card = api_client.post(api_routes.ai_ingest, json={"images": []}, headers=unique_user.token)

    for response in [*reads, *writes, card]:
        assert response.status_code == 503, response.text
        assert response.headers["Retry-After"] == str(limits.PAUSED_RETRY_AFTER)
    assert [r.json()["detail"] for r in reads] == [{"code": "paused_for_restore"}] * len(reads)
    assert [r.json()["detail"] for r in writes] == [{"code": "paused_for_restore", "message": WRITES_PAUSED}] * 2
    # as the card routes say, with the upload's `summary` for a Shortcut
    assert card.json() == {"detail": {"code": "paused_for_restore", "message": CARDS_PAUSED}, "summary": CARDS_PAUSED}

    # and once the restore is done
    assert api_client.get(api_routes.recipes_slug(recipe_slug), headers=unique_user.token).status_code == 200


def test_pages_outside_the_api_are_served_during_a_restore(api_client: TestClient):
    with Restore():
        wait_until_paused()
        response = api_client.get("/.well-known/oauth-authorization-server")
    assert response.status_code == 200


@pytest.mark.parametrize(
    ("method", "path", "page"),
    [
        ("GET", "/g/home/r/banana-mug-cake", True),
        ("HEAD", "/g/home/r/banana-mug-cake/", True),
        ("GET", "/g/home/shared/r/0b0e5e8c-6d2b-4c63-9e4f-1d2a3b4c5d6e", True),
        ("POST", "/g/home/r/banana-mug-cake", False),
        ("GET", "/g/home/r", False),
        ("GET", "/g/home/recipes/cards", False),
        ("GET", "/api/recipes/banana-mug-cake", False),
    ],
)
def test_recipe_pages_the_server_fills_in(method: str, path: str, page: bool):
    assert restore_guard.is_recipe_page(method, path) is page


def test_a_recipe_page_is_served_plain_during_a_restore(monkeypatch: pytest.MonkeyPatch):
    """
    The SPA's recipe pages read the database for their meta tags (and who's signed in) before their route runs: during
    a restore they're served as the SPA serves any page, and the app waits for the restore through the API
    """
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    def recipe_page(request: Any) -> PlainTextResponse:
        return PlainTextResponse("recipe with meta tags")

    def index(request: Any) -> PlainTextResponse:
        return PlainTextResponse("index")

    spa = Starlette(routes=[Route("/g/{group}/r/{slug}", recipe_page), Route("/", index)])
    client = TestClient(restore_guard.RestoreGuardMiddleware(spa))

    assert client.get("/g/home/r/banana-mug-cake").text == "recipe with meta tags"
    with Restore():
        wait_until_paused()
        paused = client.get("/g/home/r/banana-mug-cake")
    assert (paused.status_code, paused.text) == (200, "index")
    assert client.get("/g/home/r/banana-mug-cake").text == "recipe with meta tags"


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


# ======================================================================================================================
# Bodies are never copied


class _TempFiles:
    """The unnamed temporary files the process makes: Starlette spools uploads to one (the guard used to, too)"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.files: list[IO[bytes]] = []
        real = tempfile.TemporaryFile

        def counted(*args: Any, **kwargs: Any) -> IO[bytes]:
            file = real(*args, **kwargs)
            self.files.append(file)
            return file

        monkeypatch.setattr(tempfile, "TemporaryFile", counted)

    def live_bytes(self) -> int:
        return sum(os.fstat(file.fileno()).st_size for file in self.files if not file.closed)


def _counting_client() -> tuple[TestClient, dict[str, int]]:
    """A client for the app that counts the body bytes the app took from the client"""
    consumed = {"bytes": 0}

    async def counted(scope: Any, receive: Any, send: Any) -> None:
        async def counting_receive() -> Any:
            message = await receive()
            consumed["bytes"] += len(message.get("body", b""))
            return message

        await app(scope, counting_receive, send)

    return TestClient(counted), consumed


@pytest.mark.parametrize(
    ("path", "content_type", "status", "read"),
    [
        ("/api/does-not-exist", "application/octet-stream", 404, False),  # no route: nothing read at all
        (api_routes.admin_backups, "application/json", 401, False),  # declares no body: never read
        (api_routes.recipes, "application/json", 422, True),  # FastAPI reads a declared body into memory first
    ],
)
def test_an_anonymous_body_is_never_copied(
    path: str, content_type: str, status: int, read: bool, monkeypatch: pytest.MonkeyPatch
):
    temp = _TempFiles(monkeypatch)
    client, consumed = _counting_client()
    body = b"x" * (8 * MIB)

    response = client.post(path, content=body, headers={"content-type": content_type})

    assert response.status_code == status
    assert consumed["bytes"] == (len(body) if read else 0)
    assert temp.files == []


def test_an_upload_is_spooled_once(api_client: TestClient, admin_token: dict, monkeypatch: pytest.MonkeyPatch):
    """The route's own parser is the only copy: the guard used to keep a second one for the whole request"""
    temp = _TempFiles(monkeypatch)
    live_in_the_route: list[int] = []
    copy = admin_backups.shutil.copyfileobj

    def copy_measured(source: Any, destination: Any, *args: Any) -> None:
        live_in_the_route.append(temp.live_bytes())
        copy(source, destination, *args)

    monkeypatch.setattr(admin_backups.shutil, "copyfileobj", copy_measured)
    name = f"{random_string()}.zip"
    archive = b"x" * (4 * MIB)
    try:
        response = api_client.post(
            api_routes.admin_backups_upload, files={"archive": (name, archive, "application/zip")}, headers=admin_token
        )
        assert response.status_code == 200, response.text
        assert (get_app_dirs().BACKUP_DIR / name).read_bytes() == archive
    finally:
        (get_app_dirs().BACKUP_DIR / name).unlink(missing_ok=True)

    assert len(temp.files) == 1  # Starlette's spool of the file part
    assert live_in_the_route == [len(archive)]


# ======================================================================================================================
# A request still arriving holds nothing


def _multipart(filename: str, content: bytes) -> tuple[bytes, bytes]:
    """A one-file multipart body for an `archive` field, in two halves"""
    head = f'--b\r\nContent-Disposition: form-data; name="archive"; filename="{filename}"\r\n\r\n'.encode()
    return head + content[: len(content) // 2], content[len(content) // 2 :] + b"\r\n--b--\r\n"


async def _stalled_request(
    path: str, first: bytes, rest: bytes, headers: dict[str, str], while_stalled: Callable[[], Any]
) -> httpx.Response:
    """Sends `first`, runs `while_stalled` (in a thread) while the body stalls, then sends `rest`"""
    sent_first, finish = asyncio.Event(), asyncio.Event()

    async def body() -> AsyncIterator[bytes]:
        yield first
        sent_first.set()
        await finish.wait()
        yield rest

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        request = asyncio.create_task(client.post(path, content=body(), headers=headers))
        await sent_first.wait()
        await asyncio.sleep(0.2)
        try:
            assert storage._writers == 0, "a section is held while the body arrives"
            await asyncio.to_thread(while_stalled)
        finally:
            finish.set()
        return await request


def _restore_at_once() -> None:
    restored: list[bool] = []
    started = time.monotonic()
    storage.pauses_ingest(lambda: restored.append(True))()
    assert restored == [True]
    assert time.monotonic() - started < 1  # at once, not after RESTORE_LOCK_WAIT


@pytest.mark.parametrize(
    ("path", "content_type", "first", "rest"),
    [
        (api_routes.recipes, "application/json", b'{"name": "', b'stalled"}'),
        (api_routes.admin_backups_upload, "multipart/form-data; boundary=b", *_multipart("slow.zip", b"PK" * 512)),
        (api_routes.oauth_token, "application/x-www-form-urlencoded", b"grant_type=authorization_", b"code&code=x"),
    ],
    ids=["json", "upload", "oauth form"],
)
def test_an_anonymous_body_still_arriving_doesnt_hold_a_restore_off(
    path: str, content_type: str, first: bytes, rest: bytes, monkeypatch: pytest.MonkeyPatch
):
    """
    A client with no account that sends half a body and stalls (uvicorn has no body timeout) would otherwise hold
    every restore off, and make every other write 503 while each one waited
    """
    monkeypatch.setattr(limits, "RESTORE_LOCK_WAIT", 2)
    response = asyncio.run(_stalled_request(path, first, rest, {"content-type": content_type}, _restore_at_once))
    assert response.status_code in (400, 401), response.text  # refused once its body was in, as before


def test_an_upload_with_an_account_holds_nothing_until_its_body_is_in(
    admin_token: dict, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(limits, "RESTORE_LOCK_WAIT", 2)
    name = f"{random_string()}.zip"
    first, rest = _multipart(name, b"PK" * 512)
    headers = {**admin_token, "content-type": "multipart/form-data; boundary=b"}
    try:
        response = asyncio.run(
            _stalled_request(api_routes.admin_backups_upload, first, rest, headers, _restore_at_once)
        )
        assert response.status_code == 200, response.text
        assert (get_app_dirs().BACKUP_DIR / name).read_bytes() == b"PK" * 512
    finally:
        (get_app_dirs().BACKUP_DIR / name).unlink(missing_ok=True)


def test_an_upload_whose_body_ends_during_a_restore_is_refused(admin_token: dict):
    name = f"{random_string()}.zip"
    first, rest = _multipart(name, b"PK" * 512)
    headers = {**admin_token, "content-type": "multipart/form-data; boundary=b"}
    restore = Restore()

    def start_a_restore() -> None:
        restore.__enter__()
        wait_until_paused()

    try:
        response = asyncio.run(_stalled_request(api_routes.admin_backups_upload, first, rest, headers, start_a_restore))
    finally:
        restore.__exit__()

    assert response.status_code == 503, response.text
    assert response.json()["detail"] == {"code": "paused_for_restore", "message": WRITES_PAUSED}
    assert not (get_app_dirs().BACKUP_DIR / name).exists()
    assert restore.error is None
    assert storage._writers == 0


def _echo_app(calls: list[int]) -> FastAPI:
    """A route with a declared body behind the guard; it records how many sections were open as it ran"""
    echo_app = FastAPI()

    @echo_app.post("/api/echo")
    async def echo(body: dict[str, Any]) -> dict[str, Any]:
        calls.append(storage._writers)
        return body

    echo_app.add_middleware(restore_guard.RestoreGuardMiddleware)
    return echo_app


def _http_scope(body_length: int, path: str = "/api/echo") -> dict[str, Any]:
    return {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "server": ("testserver", 80),
        "path": path,
        "root_path": "",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(body_length).encode())],
    }


class _Client:
    """The server's side of a request: its body in chunks (then the client leaves, or stays until answered)"""

    def __init__(self, chunks: list[bytes], *, leaves: bool = False) -> None:
        self.chunks = list(chunks)
        self.leaves = leaves
        self.reads = 0
        self.writers_at_reads: list[int] = []
        self.sent: list[dict[str, Any]] = []
        self.answered = asyncio.Event()

    async def receive(self) -> dict[str, Any]:
        self.reads += 1
        self.writers_at_reads.append(storage._writers)
        if self.chunks:
            chunk = self.chunks.pop(0)
            return {"type": "http.request", "body": chunk, "more_body": bool(self.chunks) or self.leaves}
        if not self.leaves:
            await self.answered.wait()
        return {"type": "http.disconnect"}

    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body"):
            self.answered.set()


def test_the_route_reads_its_body_from_the_client_and_enters_with_the_last_message():
    chunks = [b'{"first": "chunk", ', b"", b'"second": "and the last one"}']
    client, calls = _Client(chunks), []

    asyncio.run(_echo_app(calls)(_http_scope(len(b"".join(chunks))), client.receive, client.send))

    assert client.sent[0]["status"] == 200
    assert json.loads(client.sent[1]["body"]) == {"first": "chunk", "second": "and the last one"}
    assert client.writers_at_reads[:3] == [0, 0, 0]  # nothing held while the body arrives, its last message included
    assert calls == [1]  # the route ran inside its section
    assert storage._writers == 0


def test_a_client_that_leaves_while_sending_its_body_runs_nothing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(limits, "RESTORE_LOCK_WAIT", 0.5)
    client, calls = _Client([b'{"half": '], leaves=True), []
    asyncio.run(_echo_app(calls)(_http_scope(100), client.receive, client.send))
    assert calls == []
    assert storage._writers == 0
    assert _a_restore_proceeds_at_once()


def test_a_write_during_a_restore_is_refused_before_its_body_is_read():
    client, calls = _Client([b"{}"]), []
    with Restore():
        wait_until_paused()
        asyncio.run(_echo_app(calls)(_http_scope(2), client.receive, client.send))
    assert client.reads == 0
    assert calls == []
    assert client.sent[0]["status"] == 503


def test_a_restore_that_starts_while_the_body_arrives_refuses_the_write():
    restore = Restore()
    chunks = [b'{"a": ', b"1}"]
    calls: list[int] = []
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        chunk = chunks.pop(0)
        if not chunks:
            restore.__enter__()
            await asyncio.to_thread(wait_until_paused)
        return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    try:
        asyncio.run(_echo_app(calls)(_http_scope(8), receive, send))
    finally:
        restore.__exit__()
    assert calls == []
    assert [message["type"] for message in sent] == ["http.response.start", "http.response.body"]  # the 503 alone
    assert sent[0]["status"] == 503
    assert json.loads(sent[1]["body"])["detail"] == {"code": "paused_for_restore", "message": WRITES_PAUSED}
    assert restore.error is None
    assert storage._writers == 0


# ======================================================================================================================
# The guard's own threads


def test_writes_dont_wait_for_the_event_loops_default_threads():
    """
    Upstream's video imports and OCR image imports can fill the loop's default thread pool for minutes
    (`asyncio.to_thread`): a write waiting for one of those threads to let it into its section would wait as long
    """
    app = _App()

    async def scenario() -> float:
        loop = asyncio.get_running_loop()
        busy = ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(busy)
        release = threading.Event()
        blocker = loop.run_in_executor(None, release.wait, 10)
        try:
            started = time.monotonic()
            await asyncio.wait_for(restore_guard.RestoreGuardMiddleware(app)(_scope(), _no_receive, _ignore), timeout=5)
            return time.monotonic() - started
        finally:
            release.set()
            await blocker

    took = asyncio.run(scenario())
    assert app.calls == 1
    assert took < 1
