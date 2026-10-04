"""
The recipe card routes that write only the database (a draft save, the settings, queueing a task) during a backup
restore (docs/ai/PHASE2.md §3.9): while a restore is pending or running they answer 503 `paused_for_restore` and write
nothing, and a restore waits for one already in flight, as for the routes that write files. Runs on SQLite and
PostgreSQL.
"""

import threading
import time
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mealie.repos.repository_recipe_ingest import IngestBatchesRepo, IngestSettingsRepo
from mealie.schema.recipe_ingest import IngestErrorCode, IngestStatus
from mealie.services.ai.ingest import batches, limits, storage
from mealie.services.ai.ingest.review import ReviewService
from tests.integration_tests.ai_tests.ingest.test_jobs_api import banana_draft, job_row, job_url, seed_job
from tests.integration_tests.ai_tests.ingest.test_notifiers_api import create_notifier, events_url
from tests.utils.fixture_schemas import TestUser

SETTINGS = "/api/ai/ingest/settings"


@pytest.fixture(autouse=True)
def quick_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "RESTORE_LOCK_POLL", 0.02)
    monkeypatch.setattr(limits, "RESTORE_LOCK_WAIT", 10)


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


def assert_paused(response: Any) -> None:
    assert response.status_code == 503, response.text
    assert response.headers["Retry-After"] == str(limits.PAUSED_RETRY_AFTER)


def test_database_writes_answer_503_during_a_restore(api_client: TestClient, unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    ready = seed_job(user)
    failed = seed_job(user, status=IngestStatus.failed, error_code=IngestErrorCode.limit_reached.value)
    local = seed_job(
        user, status=IngestStatus.failed, error_code=IngestErrorCode.local_only_unavailable.value, local_only=True
    )
    queued = seed_job(user, task_kind="extract", task_state="queued")
    rows = {job_id: job_row(job_id) for job_id in (ready, failed, local, queued)}
    settings = api_client.get(SETTINGS, headers=user.token).json()
    draft = banana_draft(name="Edited during the restore").model_dump(mode="json", by_alias=True)
    ref = str(banana_draft().ingredients[0].reference_id)
    region = {"page": 0, "x": 0.1, "y": 0.1, "width": 0.5, "height": 0.2, "target": {"field": "name"}}
    notifier = create_notifier(user)

    with Restore() as restore:
        wait_until_paused()
        assert restore.started.wait(5)
        responses = {
            "save": api_client.put(job_url(ready), json={"draftVersion": 1, "draft": draft}, headers=user.token),
            "reextract": api_client.post(job_url(ready, "reextract"), headers=user.token),
            "reread": api_client.post(job_url(ready, "reread"), json=region, headers=user.token),
            "rebuild": api_client.post(job_url(ready, "rebuild"), json={"transcription": "Cake"}, headers=user.token),
            "parse-lines": api_client.post(job_url(ready, "parse-lines"), json={"refs": [ref]}, headers=user.token),
            "retry": api_client.post(job_url(failed, "retry"), headers=user.token),
            "read-with-cloud": api_client.post(job_url(local, "read-with-cloud"), headers=user.token),
            "cancel": api_client.post(job_url(queued, "cancel"), headers=user.token),
            "settings": api_client.put(
                SETTINGS, json={"localOnly": not settings["localOnly"], "crossRead": True}, headers=user.token
            ),
        }
        toggle = api_client.put(events_url(notifier), json={"recipeIngestionReady": True}, headers=user.token)
        assert restore.error is None

    for route, response in responses.items():
        assert response.status_code == 503, f"{route}: {response.status_code} {response.text}"
        assert_paused(response)
        assert response.json()["detail"]["code"] == "paused_for_restore", route
    # a notifier's recipe card toggle is under /api/ai/notifiers, which the restore guard holds like upstream's writes
    assert_paused(toggle)
    assert api_client.get(events_url(notifier), headers=user.token).json()["recipeIngestionReady"] is False

    # nothing was written
    for job_id, row in rows.items():
        after = job_row(job_id)
        for column in ("draft", "draft_version", "status", "task_kind", "task_state", "local_only", "title"):
            assert after[column] == row[column], (job_id, column)
    assert api_client.get(SETTINGS, headers=user.token).json()["localOnly"] == settings["localOnly"]


def _blocked(
    monkeypatch: pytest.MonkeyPatch, owner: Any, name: str, done: dict[str, float]
) -> tuple[threading.Event, threading.Event]:
    """
    Holds `owner.name` once it's called until released, so a request is in flight while a restore starts; `done` gets
    the time it returned
    """
    inside, release = threading.Event(), threading.Event()
    original: Callable[..., Any] = getattr(owner, name)

    def held(*args: Any, **kwargs: Any) -> Any:
        inside.set()
        assert release.wait(10)
        result = original(*args, **kwargs)
        done["at"] = time.monotonic()
        return result

    monkeypatch.setattr(owner, name, held)
    return inside, release


@pytest.mark.parametrize("route", ["save", "settings"])
def test_a_restore_waits_for_a_database_write_in_flight(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, route: str
):
    user = unique_user_fn_scoped
    job_id = seed_job(user)
    draft = banana_draft(name="Saved before the restore").model_dump(mode="json", by_alias=True)
    written: dict[str, float] = {}
    if route == "save":
        inside, release = _blocked(monkeypatch, ReviewService, "save_draft", written)
    else:
        inside, release = _blocked(monkeypatch, IngestSettingsRepo, "upsert", written)

    statuses: list[int] = []

    def write() -> None:
        if route == "save":
            response = api_client.put(job_url(job_id), json={"draftVersion": 1, "draft": draft}, headers=user.token)
        else:
            response = api_client.put(SETTINGS, json={"localOnly": False, "crossRead": True}, headers=user.token)
        statuses.append(response.status_code)

    writer = threading.Thread(target=write)
    writer.start()
    try:
        assert inside.wait(10)
        with Restore() as restore:
            wait_until_paused()
            time.sleep(0.4)
            assert restore.restored_at is None, "the restore didn't wait for the write"
            release.set()
            assert restore.started.wait(10)
            assert restore.error is None
    finally:
        release.set()
        writer.join(10)

    assert statuses == [200]
    assert restore.restored_at is not None and restore.restored_at >= written["at"]
    if route == "save":
        assert job_row(job_id)["title"] == "Saved before the restore"


@pytest.mark.parametrize("route", ["create", "seal", "touch"])
def test_a_restore_waits_for_a_batch_write_in_flight(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, route: str
):
    user = unique_user_fn_scoped
    batch_id = None
    if route != "create":
        created = api_client.post("/api/ai/ingest/batches", headers=user.token)
        assert created.status_code == 201, created.text
        batch_id = created.json()["id"]
    written: dict[str, float] = {}
    if route == "create":
        inside, release = _blocked(monkeypatch, IngestBatchesRepo, "create", written)
    else:
        inside, release = _blocked(monkeypatch, batches, "seal" if route == "seal" else "heartbeat", written)

    statuses: list[int] = []

    def write() -> None:
        url = "/api/ai/ingest/batches" if route == "create" else f"/api/ai/ingest/batches/{batch_id}/{route}"
        statuses.append(api_client.post(url, headers=user.token).status_code)

    writer = threading.Thread(target=write)
    writer.start()
    try:
        assert inside.wait(10)
        with Restore() as restore:
            wait_until_paused()
            time.sleep(0.4)
            assert restore.restored_at is None, "the restore didn't wait for the batch write"
            release.set()
            assert restore.started.wait(10)
            assert restore.error is None
    finally:
        release.set()
        writer.join(10)

    assert statuses == [201 if route == "create" else 200]
    assert restore.restored_at is not None and restore.restored_at >= written["at"]
