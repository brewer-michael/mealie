"""
A backup restore while recipe cards are being read (docs/ai/PHASE2.md §3.9, §17, §18 "Restore"), on the real pieces:
`BackupV2.restore` of a backup holding queued jobs and their files, while an `IngestDispatcher` runs its loop (the real
task handler, reading cards from a fake provider) and an upload's intake is inside the ingest write lock. The pause
marker and the `flock` are the real ones, in this test process's own DATA_DIR (`tests/.temp/<worker>`).

The restore writes its marker and waits for the intake. Meanwhile new cards are refused, the dispatcher claims
nothing, and a task whose card was read waits to store it. Then the restore replaces the database and the files with
no dispatcher query in between, and removes its marker. The dispatcher carries on with nothing logged as an error and
no traceback: the tasks it was running are dropped (their rows came back without a lease), the restored rows are read
again from the queue, and a row the backup held as running is queued again by the restore itself, its attempt given
back. The files match the restored rows: the backup's job directories, and none for the card the restore wiped.

A backup taken while a task ran holds that task's live lease token: the restore queues every running row again, so
that task's result is refused by the fence rather than written to the restored row.

A second run has one provider answer land just after the restore dropped the tables (a read takes about 25 s, so in
production one often does): the task still waits for the restore and its result is still dropped, and its usage-log
and progress writes are skipped while paused, so nothing is logged as an error then either.
"""

import asyncio
import fcntl
import hashlib
import io
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from mealie.core.config import get_app_settings
from mealie.db.db_setup import session_context
from mealie.db.models.recipe_ingest import RecipeIngestionJob
from mealie.repos.repository_recipe_ingest import ExpiredLease, IngestQueue, utcnow
from mealie.schema.recipe_ingest import IngestSource, IngestStatus, IngestTaskState
from mealie.services import ocr
from mealie.services.ai.errors import IngestPaused
from mealie.services.ai.ingest import commit, events, inbox, limits, retention, storage
from mealie.services.ai.ingest.intake import IntakeAccepted, IntakeCard, IntakeOptions, IntakePage, IntakeService
from mealie.services.ai.ingest.runner import finalize
from mealie.services.ai.ingest.runner.dispatcher import IngestDispatcher
from mealie.services.ai.policy import current_policy
from mealie.services.backups_v2.alchemy_exporter import AlchemyExporter
from mealie.services.backups_v2.backup_v2 import BackupV2
from tests.unit_tests.services_tests.ai.ingest.pipeline.card_fakes import (
    BANANA_TRANSCRIPTION,
    Call,
    FakeCardAI,
    banana_answers,
    card_image,
    configure,
    create_provider,
)
from tests.unit_tests.services_tests.ai.ingest.runner.ingest_runner_testing import extract_result
from tests.utils.fixture_schemas import TestUser

Job = RecipeIngestionJob

TIMEOUT = 60
"""Seconds any one step may take before the test gives up"""

# ==========================================
# Helpers


class Timeline:
    """When things happened, by `time.monotonic()`, from any thread"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events: list[tuple[str, float]] = []

    def mark(self, name: str) -> None:
        with self._lock:
            self.events.append((name, time.monotonic()))

    def at(self, name: str) -> float:
        with self._lock:
            times = [when for event, when in self.events if event == name]
        assert len(times) == 1, f"{name}: {times}"
        return times[0]

    def between(self, start: float, end: float, prefix: str) -> list[str]:
        with self._lock:
            return [event for event, when in self.events if event.startswith(prefix) and start <= when <= end]


@dataclass
class ProviderGates:
    """The card's first reading (the image read) waits, per job, until the test lets the provider answer"""

    gates: dict[UUID, threading.Event] = field(default_factory=dict)
    reads: dict[UUID, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def gate(self, job_id: UUID) -> threading.Event:
        with self._lock:
            return self.gates.setdefault(job_id, threading.Event())

    def release(self, *job_ids: UUID) -> None:
        for job_id in job_ids:
            self.gate(job_id).set()

    def release_all(self) -> None:
        with self._lock:
            gates = list(self.gates.values())
        for gate in gates:
            gate.set()

    def started(self, job_id: UUID) -> int:
        with self._lock:
            return self.reads.get(job_id, 0)

    async def read(self, call: Call) -> dict[str, Any]:
        job_id = current_policy().job_id
        assert job_id is not None
        with self._lock:
            self.reads[job_id] = self.reads.get(job_id, 0) + 1
        gate = self.gate(job_id)
        while not gate.is_set():
            await asyncio.sleep(0.01)  # cancellable, as a provider request is
        return BANANA_TRANSCRIPTION


class SlowUpload(io.BytesIO):
    """An upload still arriving: intake's first read of it waits until the test lets the rest of the body in"""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.arriving = threading.Event()
        self.arrived = threading.Event()

    def read(self, size: int | None = -1, /) -> bytes:
        if not self.arrived.is_set():
            self.arriving.set()
            self.arrived.wait(TIMEOUT)
        return super().read(size)


def _intake(user: TestUser, number: int, upload: io.BytesIO | None = None) -> IntakeAccepted:
    """One card through the real intake, as the upload route hands it over"""
    data = upload or io.BytesIO(card_image(lines=("Banana Mug Cake", f"card {number}")))
    card = IntakeCard(pages=[IntakePage(file=data, filename=f"card-{number}.jpg")], source_name=f"upload/{number}")
    with session_context() as session:
        service = IntakeService(session, UUID(user.group_id), UUID(user.household_id))
        outcome = service.ingest(card, IntakeOptions(source=IngestSource.api, created_by=user.user_id, locale="en-US"))
    assert isinstance(outcome, IntakeAccepted), outcome
    return outcome


def _row(job_id: UUID) -> dict[str, Any] | None:
    with session_context() as session:
        row = session.execute(sa.select(*Job.__table__.columns).where(Job.id == job_id)).mappings().one_or_none()
        return dict(row) if row is not None else None


def _rows(job_ids: list[UUID]) -> dict[UUID, tuple[Any, ...] | None]:
    """Each job's (status, task_state, lease_token, attempts), or None when it has no row"""
    summary: dict[UUID, tuple[Any, ...] | None] = {}
    for job_id in job_ids:
        row = _row(job_id)
        summary[job_id] = (row["status"], row["task_state"], row["lease_token"], row["attempts"]) if row else None
    return summary


def _files(root: Path) -> dict[str, str]:
    """Every file under `root`, by relative path, with its SHA-256"""
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _lock_is_free() -> bool:
    """Whether another holder could take the ingest write lock exclusively now (no writer or restore holds it)"""
    fd = os.open(storage.lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


async def _wait_for(condition: Callable[[], bool], what: str, timeout: float = TIMEOUT) -> None:
    """Polls `condition` on the event loop (so the dispatcher's loop keeps running) until it holds"""
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting: {what}"
        await asyncio.sleep(0.01)


async def _join(thread: threading.Thread, what: str) -> None:
    await asyncio.to_thread(thread.join, TIMEOUT)
    assert not thread.is_alive(), f"timed out waiting: {what}"


def _logged(caplog: pytest.LogCaptureFixture, text: str) -> bool:
    return any(text in record.getMessage() for record in list(caplog.records))


# ==========================================
# Fixtures


@pytest.fixture()
def timeline() -> Timeline:
    return Timeline()


@pytest.fixture()
def visible() -> set[UUID]:
    """The jobs the dispatcher may see; it claims and sweeps across households, and other tests may leave jobs"""
    return set()


@pytest.fixture(autouse=True)
def recorded_queue(monkeypatch: pytest.MonkeyPatch, visible: set[UUID], timeline: Timeline) -> None:
    """The dispatcher's queue queries, kept to this test's jobs and recorded on the timeline"""
    queued_ids, expired = IngestQueue.queued_ids, IngestQueue.expired
    claim, heartbeat = IngestQueue.claim, IngestQueue.heartbeat

    def own_queued_ids(self: IngestQueue, now: datetime, limit: int, *, max_priority: int | None = None) -> list[UUID]:
        timeline.mark("db: queued ids")
        ids = queued_ids(self, now, 100_000, max_priority=max_priority) if limit > 0 else []
        return [job_id for job_id in ids if job_id in visible][:limit]

    def own_expired(self: IngestQueue, now: datetime) -> list[ExpiredLease]:
        timeline.mark("db: expired")
        return [lease for lease in expired(self, now) if lease.job_id in visible]

    def recorded_claim(self: IngestQueue, job_id: UUID, **kwargs: Any) -> bool:
        timeline.mark("db: claim")
        return claim(self, job_id, **kwargs)

    def recorded_heartbeat(self: IngestQueue, tokens: Any, now: datetime) -> dict[UUID, bool]:
        timeline.mark("db: heartbeat")
        return heartbeat(self, tokens, now)

    monkeypatch.setattr(IngestQueue, "queued_ids", own_queued_ids)
    monkeypatch.setattr(IngestQueue, "expired", own_expired)
    monkeypatch.setattr(IngestQueue, "claim", recorded_claim)
    monkeypatch.setattr(IngestQueue, "heartbeat", recorded_heartbeat)


@pytest.fixture(autouse=True)
def recorded_phases(monkeypatch: pytest.MonkeyPatch, timeline: Timeline) -> None:
    """
    The dispatcher's other periodic work, recorded instead of run: it reaches across every group (notifications to
    other tests' notifiers), and this test is about the pause
    """

    def recorder(name: str) -> Callable[..., int]:
        def record(*_: Any) -> int:
            timeline.mark(f"phase: {name}")
            return 0

        return record

    monkeypatch.setattr(events, "housekeeping", recorder("housekeeping"))
    monkeypatch.setattr(commit, "resume_stale_commits", recorder("stale commits"))
    monkeypatch.setattr(inbox, "scan_once", recorder("inbox"))
    monkeypatch.setattr(retention, "purge_once", recorder("purge"))


@pytest.fixture(autouse=True)
def quick(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(limits, "POLL_INTERVAL", 0.05)
    monkeypatch.setattr(limits, "HEARTBEAT_INTERVAL", 0.25)
    monkeypatch.setattr(limits, "HOUSEKEEPING_INTERVAL", 0.2)
    monkeypatch.setattr(limits, "PAUSED_TASK_POLL", 0.02)
    monkeypatch.setattr(limits, "RESTORE_LOCK_POLL", 0.02)
    monkeypatch.setattr(limits, "SHUTDOWN_GRACE", 2)
    # the pages stay as intake wrote them (no Tesseract turns), so their files can be compared with the backup's
    monkeypatch.setattr(ocr, "is_available", lambda: False)
    yield
    storage.pause_marker_path().unlink(missing_ok=True)


@pytest.fixture()
def restore_steps(monkeypatch: pytest.MonkeyPatch, timeline: Timeline) -> dict[str, Any]:
    """
    The restore's steps on the timeline, and what the restored rows, the files and the pause looked like inside it:
    after the database was imported, and after the files were copied. An `after drop` callable runs once the tables
    are dropped, before the import.
    """
    seen: dict[str, Any] = {}
    drop_all, restore, copy_data = AlchemyExporter.drop_all, AlchemyExporter.restore, BackupV2._copy_data

    def recorded_drop_all(self: AlchemyExporter) -> None:
        timeline.mark("restore: drop tables")
        drop_all(self)
        if (after_drop := seen.get("after drop")) is not None:
            after_drop()

    def recorded_restore(self: AlchemyExporter, db_dump: dict) -> None:
        restore(self, db_dump)
        timeline.mark("restore: tables imported")

    def recorded_copy_data(self: BackupV2, data_path: Path) -> None:
        seen["rows"] = _rows(seen["jobs"])
        seen["paused while copying"] = storage.is_paused()
        copy_data(self, data_path)
        seen["files"] = _files(seen["root"])
        timeline.mark("restore: files copied")

    monkeypatch.setattr(AlchemyExporter, "drop_all", recorded_drop_all)
    monkeypatch.setattr(AlchemyExporter, "restore", recorded_restore)
    monkeypatch.setattr(BackupV2, "_copy_data", recorded_copy_data)
    return seen


# ==========================================
# The test


@pytest.mark.parametrize(
    "answer_mid_restore",
    [
        pytest.param(False, id="answers-around-the-restore"),
        pytest.param(True, id="an-answer-lands-mid-restore"),
    ],
)
def test_a_restore_while_cards_are_read_waits_completes_and_the_dispatcher_carries_on(
    unique_user_fn_scoped: TestUser,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    timeline: Timeline,
    visible: set[UUID],
    restore_steps: dict[str, Any],
    answer_mid_restore: bool,
):
    """
    `answer_mid_restore`: the second card's provider answers just after the restore dropped the tables (a provider
    call takes about 25 s, so in production one often does), rather than after the restore
    """
    caplog.set_level(logging.INFO)
    user = unique_user_fn_scoped
    configure(user, image=create_provider(user, "Vision"), default=create_provider(user, "Text"))
    gates = ProviderGates()
    FakeCardAI(banana_answers(OpenAIRecipeCardTranscription=gates.read)).install(monkeypatch)
    root = storage.ingest_root(UUID(user.group_id))

    # Four cards uploaded. The fourth was being read by another worker process when the backup was taken: the backup
    # holds it running, its lease long expired. This dispatcher doesn't see it until after the restore (that process
    # is gone by then).
    j1, j2, j3, j4 = (_intake(user, number).job_id for number in range(1, 5))
    with session_context() as session:
        assert IngestQueue(session).claim(j4, token=uuid4(), owner="gone:1:x", now=utcnow())
        session.execute(sa.update(Job).where(Job.id == j4).values(lease_expires_at=utcnow() - timedelta(minutes=5)))
        session.commit()
    visible.update((j1, j2, j3))
    gates.release(j3, j4)
    seeded = _files(root)
    assert sorted({path.split("/")[0] for path in seeded}) == sorted(str(job) for job in (j1, j2, j3, j4))
    assert {path.split("/", 1)[1] for path in seeded} == {"pages/0/page.jpg", "pages/0/view.jpg", "pages/0/thumb.webp"}

    user.repos.session.commit()  # no transaction of the test's left open: on PostgreSQL it would hold the DROP
    backup_v2 = BackupV2(get_app_settings().DB_URL)
    backup_path = backup_v2.backup()
    # upstream's backup first runs fix_migration_data over the whole shared test database, which may log its own error
    # for other tests' rows; only what's logged from here on counts below
    logged_before = len(caplog.records)
    restore_steps.update(jobs=[j1, j2, j3, j4], root=root)

    def answer_after_the_drop() -> None:
        # in the restore's thread: the second card's provider answers, and its task meets the dropped tables
        gates.release(j2)
        deadline = time.monotonic() + TIMEOUT
        while not _logged(caplog, f"Recipe card job {j2}: waiting for the backup restore to finish"):
            assert time.monotonic() < deadline, "the second card's task didn't wait for the restore"
            time.sleep(0.02)

    if answer_mid_restore:
        restore_steps["after drop"] = answer_after_the_drop

    dispatcher = IngestDispatcher(concurrency=2, instance="restore")
    upload = SlowUpload(card_image(lines=("Banana Mug Cake", "card 5")))
    intake_outcome: list[IntakeAccepted | BaseException] = []
    restore_errors: list[BaseException] = []
    j5_dir: list[str] = []

    def intake_in_thread() -> None:
        try:
            intake_outcome.append(_intake(user, 5, upload))
            timeline.mark("intake: done")
        except BaseException as e:
            intake_outcome.append(e)

    def restore_in_thread() -> None:
        try:
            backup_v2.restore(backup_path)
        except BaseException as e:
            restore_errors.append(e)

    intake = threading.Thread(target=intake_in_thread, name="intake", daemon=True)
    restore = threading.Thread(target=restore_in_thread, name="restore", daemon=True)

    async def scenario() -> None:
        await dispatcher.start()
        try:
            # the dispatcher claims the first two cards; their reads wait at the provider
            await _wait_for(lambda: gates.started(j1) == 1 and gates.started(j2) == 1, "the first two reads")
            await _wait_for(lambda: bool(timeline.between(0, time.monotonic(), "db: heartbeat")), "a heartbeat")
            assert _rows([j3])[j3] == (IngestStatus.processing, IngestTaskState.queued, None, 0)

            # a fifth card's upload is still arriving, inside the intake's write section (its directory exists)
            intake.start()
            await _wait_for(upload.arriving.is_set, "the fifth card's intake")
            new_dirs = sorted({path.name for path in root.iterdir()} - {str(job) for job in (j1, j2, j3, j4)})
            assert len(new_dirs) == 1
            j5_dir.extend(new_dirs)

            # the restore pauses ingestion and waits for the intake
            restore.start()
            await _wait_for(storage.is_paused, "the pause marker")
            with pytest.raises(IngestPaused):
                await asyncio.to_thread(_intake, user, 6)  # a new card is refused at once
            gates.release(j1)  # the first card's provider answers: its task waits to store the result
            await _wait_for(
                lambda: _logged(caplog, f"Recipe card job {j1}: waiting for the backup restore to finish"),
                "the first card's task to wait",
            )
            await asyncio.sleep(0.3)  # a dozen restore polls and dispatcher ticks
            assert restore.is_alive()
            assert timeline.between(0, time.monotonic(), "restore:") == []  # nothing replaced yet
            row = _row(j1)
            assert row is not None
            assert (row["status"], row["task_state"]) == (IngestStatus.processing, IngestTaskState.running)
            assert row["draft"] is None  # the result waits for the restore to end

            # the upload arrives; the intake finishes, and the restore goes ahead
            upload.arrived.set()
            await _join(intake, "the fifth card's intake")
            await _join(restore, "the restore")
            assert restore_errors == []

            # the old tasks end without writing: the first's result (and, if it answered mid-restore, the second's)
            # is refused by the restored row's fence; the second, still waiting for its provider, is stopped once its
            # lease is found gone
            await _wait_for(
                lambda: _logged(caplog, f"Recipe card job {j1}: its task's outcome was dropped"),
                "the first card's old result to be dropped",
            )
            second = "its task's outcome was dropped" if answer_mid_restore else "its task was stopped (vanished)"
            await _wait_for(
                lambda: _logged(caplog, f"Recipe card job {j2}: {second}"), "the second card's old task to end"
            )

            # every restored card is read again, once; the fourth too, which the restore queued again with its attempt
            # given back (the backup held it running)
            visible.add(j4)
            gates.release(j2)
            await _wait_for(
                lambda: all((_row(job) or {}).get("status") == IngestStatus.ready for job in (j1, j2, j3, j4)),
                "every card to be read again",
            )
            assert dispatcher.running
        finally:
            upload.arrived.set()
            gates.release_all()
            await dispatcher.stop()

    try:
        asyncio.run(scenario())
    finally:
        for thread in (intake, restore):
            if thread.is_alive():
                thread.join(TIMEOUT)
        backup_v2.db_exporter.engine.dispose()
        backup_path.unlink(missing_ok=True)

    # the restore waited for the intake, then replaced the database and the files
    (outcome,) = intake_outcome
    assert isinstance(outcome, IntakeAccepted) and str(outcome.job_id) == j5_dir[0]
    drop, copied = timeline.at("restore: drop tables"), timeline.at("restore: files copied")
    assert timeline.at("intake: done") < drop < timeline.at("restore: tables imported") < copied

    # the pause: set while the files were copied, gone afterwards, and the lock free
    assert restore_steps["paused while copying"] is True
    assert not storage.pause_marker_path().exists()
    assert not storage.is_paused()
    assert _lock_is_free()

    # no dispatcher query and no periodic work while the database and the files were replaced; both before and after
    # (a heartbeat after it only when a task of the old claims was still held: the one waiting for its provider)
    assert timeline.between(drop, copied, "db:") == []
    assert timeline.between(drop, copied, "phase:") == []
    for kind in ("db: heartbeat", "db: claim", "phase: housekeeping"):
        assert timeline.between(0, drop, kind), kind
    for kind in ["db: claim", "phase: housekeeping"] + ([] if answer_mid_restore else ["db: heartbeat"]):
        assert timeline.between(copied, time.monotonic(), kind), kind

    # the rows came back as the backup held them: three queued, one running on another process's expired lease, and
    # no fifth card
    restored = restore_steps["rows"]
    for job in (j1, j2, j3):
        assert restored[job] == (IngestStatus.processing, IngestTaskState.queued, None, 0)
    status, state, token, attempts = restored[j4]
    assert (status, state, attempts) == (IngestStatus.processing, IngestTaskState.running, 1) and token is not None
    assert _row(outcome.job_id) is None

    # the files match the restored rows: the backup's, and none for the fifth card; reading wrote none
    assert restore_steps["files"] == seeded
    assert _files(root) == seeded

    # each card read again exactly once after the restore: one claim each from the queue (the fourth's earlier claim,
    # which the backup held, given back by the restore)
    for job, claims in ((j1, 1), (j2, 1), (j3, 1), (j4, 1)):
        row = _row(job)
        assert row is not None
        assert (row["status"], row["task_state"], row["lease_token"], row["attempts"], row["error_code"]) == (
            IngestStatus.ready,
            None,
            None,
            claims,
            None,
        )
        assert row["draft"]["name"] == "Banana Mug Cake"
    assert {job: gates.started(job) for job in (j1, j2, j3, j4)} == {j1: 2, j2: 2, j3: 1, j4: 1}
    assert _logged(caplog, "Recipe card ingestion is paused while a backup is restored")
    assert _logged(caplog, "Recipe card ingestion resumed after the backup restore")

    # the dispatcher and the tasks carried on with nothing logged as an error, and no traceback
    problems = [
        f"{record.levelname} {record.name}: {record.getMessage().splitlines()[0]}"
        for record in caplog.records[logged_before:]
        if record.levelno >= logging.ERROR
        or record.exc_info
        or "Traceback" in record.getMessage()
        or '\n  File "' in record.getMessage()
    ]
    assert problems == []


# ==========================================
# A backup holding a live lease


def test_a_restore_queues_the_tasks_its_backup_held_running(unique_user_fn_scoped: TestUser):
    """
    A backup taken while a task ran holds that task's live lease token. The restore queues every running row again
    with no lease, so the task's result is refused by the restored row's fence instead of landing on it.
    """
    user = unique_user_fn_scoped
    job_id = _intake(user, 1).job_id
    token = uuid4()
    with session_context() as session:
        assert IngestQueue(session).claim(job_id, token=token, owner="worker:1:live", now=utcnow())
        session.execute(sa.update(Job).where(Job.id == job_id).values(progress_key="recipe-ingest.progress.x"))
        session.commit()

    user.repos.session.commit()  # no transaction of the test's left open: on PostgreSQL it would hold the DROP
    backup_v2 = BackupV2(get_app_settings().DB_URL)
    backup_path = backup_v2.backup()
    try:
        backup_v2.restore(backup_path)
    finally:
        backup_v2.db_exporter.engine.dispose()
        backup_path.unlink(missing_ok=True)

    row = _row(job_id)
    assert row is not None
    assert (row["status"], row["task_state"], row["lease_token"], row["lease_owner"], row["lease_expires_at"]) == (
        IngestStatus.processing,
        IngestTaskState.queued,
        None,
        None,
        None,
    )
    assert (row["task_started_at"], row["progress_key"], row["attempts"]) == (None, None, 0)

    with session_context() as session:
        assert finalize.finalize_extract(session, job_id, token, extract_result()) == finalize.DROPPED
    row = _row(job_id)
    assert row is not None
    assert (row["status"], row["task_state"], row["draft"]) == (IngestStatus.processing, IngestTaskState.queued, None)


def test_a_task_in_flight_when_its_backup_was_taken_doesnt_write_to_the_restored_row(
    unique_user_fn_scoped: TestUser,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    visible: set[UUID],
):
    """
    The dispatcher's task holds the same token as the restored row (the backup was taken while it ran). Its result
    is dropped, and the card is read again from the queue, once, with its attempt given back.
    """
    caplog.set_level(logging.INFO)
    user = unique_user_fn_scoped
    configure(user, image=create_provider(user, "Vision"), default=create_provider(user, "Text"))
    gates = ProviderGates()
    FakeCardAI(banana_answers(OpenAIRecipeCardTranscription=gates.read)).install(monkeypatch)
    job_id = _intake(user, 1).job_id
    visible.add(job_id)
    user.repos.session.commit()
    backup_v2 = BackupV2(get_app_settings().DB_URL)
    dispatcher = IngestDispatcher(concurrency=2, instance="live-token")
    restore_errors: list[BaseException] = []

    def restore_in_thread(path: Path) -> None:
        try:
            backup_v2.restore(path)
        except BaseException as e:
            restore_errors.append(e)

    async def scenario() -> Path:
        await dispatcher.start()
        try:
            await _wait_for(lambda: gates.started(job_id) == 1, "the card's read")
            row = _row(job_id)
            assert row is not None and row["task_state"] == IngestTaskState.running
            token = row["lease_token"]
            path = await asyncio.to_thread(backup_v2.backup)  # the backup holds the running row and its token

            restore = threading.Thread(target=restore_in_thread, args=(path,), name="restore", daemon=True)
            restore.start()
            await _join(restore, "the restore")
            assert restore_errors == []
            restored = _row(job_id)
            assert restored is not None
            assert (restored["task_state"], restored["lease_token"]) == (IngestTaskState.queued, None)
            assert token is not None

            gates.release(job_id)  # the old read answers now
            await _wait_for(
                lambda: (
                    _logged(caplog, f"Recipe card job {job_id}: its task's outcome was dropped")
                    or _logged(caplog, f"Recipe card job {job_id}: its task was stopped (vanished)")
                ),
                "the old task to end without writing",
            )
            await _wait_for(lambda: (_row(job_id) or {}).get("status") == IngestStatus.ready, "the card read again")
            return path
        finally:
            gates.release_all()
            await dispatcher.stop()

    path = asyncio.run(scenario())
    backup_v2.db_exporter.engine.dispose()
    path.unlink(missing_ok=True)

    row = _row(job_id)
    assert row is not None
    assert (row["status"], row["task_state"], row["lease_token"], row["attempts"], row["error_code"]) == (
        IngestStatus.ready,
        None,
        None,
        1,
        None,
    )
    assert gates.started(job_id) == 2
