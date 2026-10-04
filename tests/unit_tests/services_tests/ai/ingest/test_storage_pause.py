"""Recipe card files, and pausing ingestion for a backup restore (docs/ai/PHASE2.md §2, §3.9)"""

import errno
import fcntl
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from mealie.core.config import get_app_dirs
from mealie.services.ai.errors import IngestBusyError, IngestPaused
from mealie.services.ai.ingest import limits, storage


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The marker and the lock file in a directory of the test's own"""
    monkeypatch.setattr(storage, "_data_dir", lambda: tmp_path)
    yield tmp_path


@pytest.fixture()
def fast_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "RESTORE_LOCK_POLL", 0.01)
    monkeypatch.setattr(limits, "RESTORE_LOCK_WAIT", 5)


def _hold_exclusive(path: Path, release: threading.Event, held: threading.Event) -> None:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        held.set()
        release.wait(10)
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _write_in_thread(release: threading.Event, inside: threading.Event, done: threading.Event) -> threading.Thread:
    def write() -> None:
        with storage.ingest_write():
            inside.set()
            release.wait(10)
        done.set()

    thread = threading.Thread(target=write, daemon=True)
    thread.start()
    assert inside.wait(5)
    return thread


# ==========================================
# Paths and writes


def test_job_directories_live_under_the_groups_folder():
    group_id, job_id = uuid4(), uuid4()
    groups = get_app_dirs().GROUPS_DIR

    assert storage.ingest_root(group_id) == groups / str(group_id) / "ai-ingest"
    assert storage.job_dir(group_id, job_id) == groups / str(group_id) / "ai-ingest" / str(job_id)
    assert storage.page_dir(group_id, job_id, 1) == storage.job_dir(group_id, job_id) / "pages" / "1"
    assert storage.eval_cards_dir(group_id) == groups / str(group_id) / "eval-cards"


def test_creating_and_removing_a_job_directory():
    group_id, job_id = uuid4(), uuid4()
    try:
        path = storage.create_job_dir(group_id, job_id, 2)
        assert sorted(p.name for p in (path / "pages").iterdir()) == ["0", "1"]

        with pytest.raises(FileExistsError):
            storage.create_job_dir(group_id, job_id, 1)

        assert storage.remove_job_dir(group_id, job_id)
        assert not path.exists()
        assert not storage.remove_job_dir(group_id, job_id)
    finally:
        storage.remove_job_dir(group_id, job_id)
        (get_app_dirs().GROUPS_DIR / str(group_id) / "ai-ingest").rmdir()
        (get_app_dirs().GROUPS_DIR / str(group_id)).rmdir()


def test_atomic_writes_replace_files_and_never_create_directories(tmp_path: Path):
    target = tmp_path / "page.jpg"
    storage.atomic_write_bytes(target, b"one")
    storage.atomic_write_bytes(target, b"two")
    assert target.read_bytes() == b"two"
    assert [p.name for p in tmp_path.iterdir()] == ["page.jpg"]

    with pytest.raises(FileNotFoundError):
        storage.atomic_write_bytes(tmp_path / "missing" / "page.jpg", b"x")
    assert not (tmp_path / "missing").exists()


def test_a_failed_write_leaves_the_old_file_and_no_temporary_one(tmp_path: Path):
    target = tmp_path / "thumb.webp"
    target.write_bytes(b"old")

    class Broken:
        def save(self, file, format, **params):
            file.write(b"half")
            raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        storage.atomic_save_image(Broken(), target, "WEBP")
    assert target.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["thumb.webp"]


# ==========================================
# The marker


def test_no_marker_means_not_paused(data_dir: Path):
    assert not storage.is_paused()


def test_a_fresh_marker_pauses_and_an_old_one_doesnt(data_dir: Path):
    marker = data_dir / storage.PAUSE_MARKER_NAME
    marker.write_text(str(time.time()))
    assert storage.is_paused()

    marker.write_text(str(time.time() - limits.PAUSE_TTL - 1))
    assert not storage.is_paused()

    # a marker from the future counts no longer than a fresh one
    marker.write_text(str(time.time() + limits.PAUSE_TTL + 60))
    assert not storage.is_paused()


def test_an_unreadable_marker_counts_from_its_modification_time(data_dir: Path):
    marker = data_dir / storage.PAUSE_MARKER_NAME
    marker.write_text("not a time")
    assert storage.is_paused()

    old = time.time() - limits.PAUSE_TTL - 10
    os.utime(marker, (old, old))
    assert not storage.is_paused()


def test_a_long_restore_keeps_its_marker_fresh(data_dir: Path, fast_pause: None, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(limits, "PAUSE_REFRESH", 0.05)
    monkeypatch.setattr(limits, "PAUSE_TTL", 0.3)
    seen: list[bool] = []

    @storage.pauses_ingest
    def restore() -> str:
        for _ in range(10):
            time.sleep(0.08)
            seen.append(storage.is_paused())
        return "restored"

    assert restore() == "restored"
    # 0.8 s is well past the 0.3 s the marker is honoured for without a refresh
    assert all(seen)
    assert not (data_dir / storage.PAUSE_MARKER_NAME).exists()
    assert not storage.is_paused()


# ==========================================
# The write lock


def test_a_write_section_runs_and_releases_the_lock(data_dir: Path):
    with storage.ingest_write():
        pass

    fd = os.open(data_dir / storage.LOCK_FILE_NAME, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # nobody holds it any more
    finally:
        os.close(fd)


def test_write_sections_share_the_lock(data_dir: Path):
    with storage.ingest_write(), storage.ingest_write():
        pass


def test_a_write_section_is_refused_while_the_marker_is_set(data_dir: Path):
    (data_dir / storage.PAUSE_MARKER_NAME).write_text(str(time.time()))
    ran = False
    with pytest.raises(IngestPaused), storage.ingest_write():
        ran = True
    assert not ran


def test_a_write_section_is_refused_while_another_thread_holds_the_lock_exclusively(data_dir: Path):
    release, held = threading.Event(), threading.Event()
    thread = threading.Thread(target=_hold_exclusive, args=(data_dir / storage.LOCK_FILE_NAME, release, held))
    thread.start()
    try:
        assert held.wait(5)
        started = time.monotonic()
        with pytest.raises(IngestPaused), storage.ingest_write():
            pass
        assert time.monotonic() - started < 1  # it never waits
    finally:
        release.set()
        thread.join(5)

    with storage.ingest_write():
        pass


def test_a_marker_written_while_the_lock_was_taken_still_refuses(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    answers = iter([False, True])
    monkeypatch.setattr(storage, "is_paused", lambda: next(answers))
    ran = False
    with pytest.raises(IngestPaused), storage.ingest_write():
        ran = True
    assert not ran

    # and the shared lock was released
    fd = os.open(data_dir / storage.LOCK_FILE_NAME, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


# ==========================================
# pauses_ingest


def test_a_restore_waits_for_a_writer_and_pauses_new_ones(data_dir: Path, fast_pause: None):
    release, inside, done = threading.Event(), threading.Event(), threading.Event()
    writer = _write_in_thread(release, inside, done)
    events: list[str] = []

    @storage.pauses_ingest
    def restore() -> None:
        events.append("restore" if done.is_set() else "restore while writing")

    restoring = threading.Thread(target=restore)
    restoring.start()
    try:
        deadline = time.monotonic() + 5
        while not storage.is_paused() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert storage.is_paused()
        # new writers are refused while the restore waits
        with pytest.raises(IngestPaused), storage.ingest_write():
            pass
        time.sleep(0.1)
        assert events == []
    finally:
        release.set()
        writer.join(5)
        restoring.join(5)

    assert events == ["restore"]
    assert not storage.is_paused()


def test_a_restore_gives_up_without_restoring_when_writers_dont_finish(
    data_dir: Path, fast_pause: None, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(limits, "RESTORE_LOCK_WAIT", 0.2)
    release, inside, done = threading.Event(), threading.Event(), threading.Event()
    writer = _write_in_thread(release, inside, done)
    restored = False

    @storage.pauses_ingest
    def restore() -> None:
        nonlocal restored
        restored = True

    try:
        with pytest.raises(IngestBusyError):
            restore()
    finally:
        release.set()
        writer.join(5)

    assert not restored
    assert not (data_dir / storage.PAUSE_MARKER_NAME).exists()
    with storage.ingest_write():
        pass


def test_the_marker_is_removed_and_the_lock_released_when_the_restore_fails(data_dir: Path, fast_pause: None):
    @storage.pauses_ingest
    def restore() -> None:
        assert storage.is_paused()
        raise RuntimeError("broken backup")

    with pytest.raises(RuntimeError, match="broken backup"):
        restore()

    assert not (data_dir / storage.PAUSE_MARKER_NAME).exists()
    with storage.ingest_write():
        pass


def test_overlapping_pauses_keep_the_marker_until_the_last_ends(data_dir: Path, fast_pause: None):
    first_inside, finish_first = threading.Event(), threading.Event()
    results: list[str] = []

    @storage.pauses_ingest
    def slow_restore() -> None:
        first_inside.set()
        finish_first.wait(5)
        results.append("first")

    # the second restore can't get the lock while the first holds it, so it gives up; the first is still running
    thread = threading.Thread(target=slow_restore)
    thread.start()
    try:
        assert first_inside.wait(5)

        @storage.pauses_ingest
        def quick_restore() -> None:
            results.append("second")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(limits, "RESTORE_LOCK_WAIT", 0.05)
            with pytest.raises(IngestBusyError):
                quick_restore()
        assert storage.is_paused()
    finally:
        finish_first.set()
        thread.join(5)

    assert results == ["first"]
    assert not storage.is_paused()


def test_without_file_locks_the_marker_alone_applies(
    data_dir: Path, fast_pause: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    def unsupported(fd: int, operation: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(fcntl, "flock", unsupported)
    monkeypatch.setattr(storage, "_lock_warning_logged", False)

    with storage.ingest_write():
        pass
    assert not storage.flock_supported()

    ran = []

    @storage.pauses_ingest
    def restore() -> None:
        ran.append(storage.is_paused())

    restore()
    assert ran == [True]
    assert not storage.is_paused()
    warnings = [record for record in caplog.records if "File locks aren't supported" in record.getMessage()]
    assert len(warnings) == 1


# ==========================================
# Locks that belong to the process (NFS)


@pytest.fixture()
def process_locks(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    `flock` as Linux NFS clients emulate it: POSIX record locks (flock(2), "NFS details"). Those belong to the
    process, not to the open file, so two threads never conflict, and closing any descriptor of the file drops every
    lock the process holds on it. `lockf` gives exactly that on a local filesystem.
    """
    monkeypatch.setattr(fcntl, "flock", fcntl.lockf)


def _exclusive_from_another_process(path: Path) -> bool:
    """Whether another process could take the lock exclusively right now (a restore in another worker)"""
    script = (
        "import fcntl, os, sys\n"
        "fd = os.open(sys.argv[1], os.O_RDWR)\n"
        "try:\n"
        "    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "except OSError:\n"
        "    sys.exit(1)\n"
        "sys.exit(0)\n"
    )
    return subprocess.run([sys.executable, "-c", script, str(path)], timeout=30).returncode == 0


def test_with_process_locks_a_restore_still_waits_for_its_own_processs_writer(
    data_dir: Path, fast_pause: None, process_locks: None
):
    release, inside, done = threading.Event(), threading.Event(), threading.Event()
    writer = _write_in_thread(release, inside, done)
    events: list[str] = []

    @storage.pauses_ingest
    def restore() -> None:
        events.append("restore" if done.is_set() else "restore while writing")

    restoring = threading.Thread(target=restore)
    restoring.start()
    try:
        time.sleep(0.2)
        assert events == []
        with pytest.raises(IngestPaused), storage.ingest_write():
            pass
    finally:
        release.set()
        writer.join(5)
        restoring.join(5)

    assert events == ["restore"]
    assert not storage.is_paused()


def test_with_process_locks_a_section_ending_doesnt_drop_another_ones_lock(data_dir: Path, process_locks: None):
    release, inside, done = threading.Event(), threading.Event(), threading.Event()
    writer = _write_in_thread(release, inside, done)
    try:
        with storage.ingest_write():
            pass  # ends while the other thread is still writing
        assert not _exclusive_from_another_process(data_dir / storage.LOCK_FILE_NAME)
    finally:
        release.set()
        writer.join(5)

    assert done.is_set()
    assert _exclusive_from_another_process(data_dir / storage.LOCK_FILE_NAME)


def test_lock_support_is_detected(data_dir: Path):
    assert storage.flock_supported()
