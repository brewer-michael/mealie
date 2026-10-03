"""A backup restore pauses recipe card ingestion and waits for its in-flight writes (docs/ai/PHASE2.md §3.9)"""

import threading
import time
from pathlib import Path

import pytest

from mealie.core.config import get_app_settings
from mealie.services.ai.errors import IngestBusyError, IngestPaused
from mealie.services.ai.ingest import limits, storage
from mealie.services.backups_v2.backup_v2 import BackupV2


@pytest.fixture()
def backup_v2(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(limits, "RESTORE_LOCK_POLL", 0.01)
    backup = BackupV2(get_app_settings().DB_URL)
    try:
        yield backup
    finally:
        backup.db_exporter.engine.dispose()
        storage.pause_marker_path().unlink(missing_ok=True)


class StopRestore(Exception):
    """Raised where the restore would first change something, so the test database is left alone"""


def _stop_at_first_change(monkeypatch: pytest.MonkeyPatch, seen: list[str]) -> None:
    """The restore's first step (the safety copy of the database) records whether ingestion was paused, then stops"""

    def first_step(self: BackupV2) -> None:
        seen.append("paused" if storage.is_paused() else "running")
        raise StopRestore()

    monkeypatch.setattr(BackupV2, "_sqlite", first_step)
    monkeypatch.setattr(BackupV2, "_postgres", first_step)


def test_ingestion_is_paused_during_a_restore_and_resumes_when_it_fails(
    backup_v2: BackupV2, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    seen: list[str] = []
    _stop_at_first_change(monkeypatch, seen)

    with pytest.raises(StopRestore):
        backup_v2.restore(tmp_path / "backup.zip")

    assert seen == ["paused"]
    assert not storage.pause_marker_path().exists()
    with storage.ingest_write():
        pass


def test_a_restore_waits_for_a_writer_to_finish(backup_v2: BackupV2, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    seen: list[str] = []
    _stop_at_first_change(monkeypatch, seen)
    inside, release = threading.Event(), threading.Event()
    finished: list[float] = []

    def write() -> None:
        with storage.ingest_write():
            inside.set()
            release.wait(10)
            finished.append(time.monotonic())

    writer = threading.Thread(target=write)
    writer.start()
    assert inside.wait(5)
    threading.Timer(0.3, release.set).start()

    with pytest.raises(StopRestore):
        backup_v2.restore(tmp_path / "backup.zip")
    restored_at = time.monotonic()
    writer.join(5)

    assert seen == ["paused"]
    assert finished and finished[0] <= restored_at


def test_a_restore_gives_up_cleanly_when_writers_dont_finish(
    backup_v2: BackupV2, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(limits, "RESTORE_LOCK_WAIT", 0.2)
    seen: list[str] = []
    _stop_at_first_change(monkeypatch, seen)
    inside, release = threading.Event(), threading.Event()

    def write() -> None:
        with storage.ingest_write():
            inside.set()
            release.wait(10)

    writer = threading.Thread(target=write)
    writer.start()
    try:
        assert inside.wait(5)
        with pytest.raises(IngestBusyError):
            backup_v2.restore(tmp_path / "backup.zip")
    finally:
        release.set()
        writer.join(5)

    assert seen == []  # nothing was touched
    assert not storage.is_paused()


def test_writes_are_refused_while_a_restore_holds_the_lock(
    backup_v2: BackupV2, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    refused: list[bool] = []

    def first_step(self: BackupV2) -> None:
        # a write attempted while the restore runs, from another thread
        def write() -> None:
            try:
                with storage.ingest_write():
                    refused.append(False)
            except IngestPaused:
                refused.append(True)

        thread = threading.Thread(target=write)
        thread.start()
        thread.join(5)
        raise StopRestore()

    monkeypatch.setattr(BackupV2, "_sqlite", first_step)
    monkeypatch.setattr(BackupV2, "_postgres", first_step)

    with pytest.raises(StopRestore):
        backup_v2.restore(tmp_path / "backup.zip")

    assert refused == [True]
