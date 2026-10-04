"""Recipe card files, and pausing ingestion for a backup restore (docs/ai/PHASE2.md §2, §3.9)"""

import errno
import fcntl
import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from mealie.core.config import get_app_dirs
from mealie.services.ai.errors import IngestBusyError, IngestPaused
from mealie.services.ai.ingest import limits, storage
from mealie.services.ai.ingest.settings import get_ingest_settings


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The marker and the lock file in a directory of the test's own (and the fallback lock folder too)"""
    data = tmp_path / "data"
    data.mkdir()
    fallback = tmp_path / "tmp"
    fallback.mkdir()
    monkeypatch.setattr(storage, "_data_dir", lambda: data)
    monkeypatch.setattr(storage, "FALLBACK_LOCK_DIR", fallback)
    monkeypatch.setattr(storage, "_lock_locations", {})
    yield data


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
# Locks that belong to the process


@pytest.fixture()
def process_locks(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    `flock` as platforms that build it on POSIX record locks provide it: those locks belong to the process, not to the
    open file, so two threads never conflict, and closing any descriptor of the file drops every lock the process holds
    on it. `lockf` gives exactly that on a local filesystem. (Linux's own NFS emulation of `flock` keeps the open file
    description as the lock's owner, like a local `flock`; the in-process gate is defence in depth for the rest.)
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


def test_the_restore_wait_ends_before_a_reverse_proxy_gives_up():
    """A busy restore answers "try again" within the 60 s that reverse proxies commonly allow a request"""
    assert limits.RESTORE_LOCK_WAIT < 60
    assert limits.RESTORE_LOCK_WAIT >= 30  # write sections take seconds: a restore still waits for them


# ==========================================
# A marker whose restore is gone

_RESTORE_IN_ANOTHER_PROCESS = """
import sys, time
from pathlib import Path
from mealie.services.ai.ingest import storage

storage._data_dir = lambda: Path(sys.argv[1])


@storage.pauses_ingest
def restore() -> None:
    print("restoring", flush=True)
    if sys.argv[2] == "check":
        print("paused" if storage.is_paused() else "not paused", flush=True)
    time.sleep(120)


restore()
"""


def _next_line(process: subprocess.Popen[str]) -> str:
    """The next line a script printed, past the log lines settings may print first"""
    assert process.stdout is not None
    while line := process.stdout.readline():
        if not line.startswith("["):
            return line.strip()
    return ""


@pytest.fixture()
def restore_in_another_process(data_dir: Path) -> Iterator[Callable[..., subprocess.Popen[str]]]:
    """Starts the real `pauses_ingest` in another process (a worker restoring), and waits until it's restoring"""
    started: list[subprocess.Popen[str]] = []

    def start(mode: str = "sleep") -> subprocess.Popen[str]:
        process = subprocess.Popen(
            [sys.executable, "-c", _RESTORE_IN_ANOTHER_PROCESS, str(data_dir), mode],
            stdout=subprocess.PIPE,
            text=True,
        )
        started.append(process)
        assert process.stdout is not None
        assert _next_line(process) == "restoring"
        return process

    yield start
    for process in started:
        process.kill()
        process.wait(10)
        if process.stdout is not None:
            process.stdout.close()


def _age_marker(path: Path, seconds: float) -> None:
    """The marker as its restore last refreshed it `seconds` ago, its other fields kept"""
    content = json.loads(path.read_text())
    content["time"] = time.time() - seconds
    path.write_text(json.dumps(content))


def _finished_process_id() -> int:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(30)
    return process.pid


def _write_json_marker(path: Path, **fields: object) -> None:
    content: dict[str, object] = {
        "time": time.time(),
        "pid": os.getpid(),
        "host": storage._host_identity(),
        "started": storage._process_started(os.getpid()),
        "lock": None,
    }
    content.update(fields)
    path.write_text(json.dumps(content))


def test_a_restores_marker_names_its_process_and_its_lock(data_dir: Path, fast_pause: None):
    seen: list[dict] = []

    @storage.pauses_ingest
    def restore() -> None:
        seen.append(json.loads((data_dir / storage.PAUSE_MARKER_NAME).read_text()))
        assert storage.is_paused()
        assert not storage.clear_stale_pause()  # this process's own restore

    restore()
    (marker,) = seen
    assert marker["pid"] == os.getpid() and marker["host"] == storage._host_identity()
    assert marker["started"] == storage._process_started(os.getpid())
    assert marker["lock"] == str(data_dir / f"{storage.LOCK_FILE_NAME}{storage.RESTORE_LOCK_SUFFIX}")
    assert abs(marker["time"] - time.time()) < 5
    assert not (data_dir / storage.PAUSE_MARKER_NAME).exists()


def test_a_restore_in_another_process_pauses_however_old_its_marker(
    data_dir: Path, restore_in_another_process: Callable[..., subprocess.Popen[str]]
):
    restore_in_another_process()
    marker = data_dir / storage.PAUSE_MARKER_NAME
    assert storage.is_paused()

    _age_marker(marker, 3 * limits.PAUSE_REFRESH)  # its refresher stalled, but the restore holds its lock
    assert storage.is_paused()
    assert not storage.clear_stale_pause()
    assert marker.exists()
    with pytest.raises(IngestPaused), storage.ingest_write():
        pass


def test_the_marker_of_a_restore_that_crashed_is_removed_at_once(
    data_dir: Path, restore_in_another_process: Callable[..., subprocess.Popen[str]], caplog: pytest.LogCaptureFixture
):
    process = restore_in_another_process()
    marker = data_dir / storage.PAUSE_MARKER_NAME
    assert storage.is_paused()

    process.kill()  # a crash or a container stop mid-restore
    process.wait(10)
    assert marker.exists()  # fresh: written moments ago
    with caplog.at_level(logging.INFO):
        assert not storage.is_paused()
    assert not marker.exists()
    assert any("no longer running" in record.getMessage() for record in caplog.records)
    with storage.ingest_write():
        pass


def test_a_restore_in_this_process_is_seen_from_another_one(data_dir: Path, fast_pause: None):
    seen: list[str] = []

    @storage.pauses_ingest
    def restore() -> None:
        checked = subprocess.run(
            [sys.executable, "-c", _CHECK_IN_ANOTHER_PROCESS, str(data_dir)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        seen.append(checked.stdout.strip().splitlines()[-1])  # its last line: settings may log before it
        seen.append("marker" if (data_dir / storage.PAUSE_MARKER_NAME).exists() else "no marker")

    restore()
    assert seen == ["paused", "marker"]


_CHECK_IN_ANOTHER_PROCESS = """
import sys
from pathlib import Path
from mealie.services.ai.ingest import storage

storage._data_dir = lambda: Path(sys.argv[1])
print("paused" if storage.is_paused() else "not paused")
"""


def test_a_new_restore_waits_for_a_stale_check_and_keeps_its_own_marker(data_dir: Path, fast_pause: None):
    """A stale-marker check holds the restore lock while it removes the old marker; a restore starting then waits"""
    _write_json_marker(
        data_dir / storage.PAUSE_MARKER_NAME, pid=_finished_process_id(), lock=str(storage.restore_lock_path())
    )
    state, fd = storage._probe_restore_lock()  # the check, holding the lock
    assert state == storage._RestoreLock.free and fd is not None
    seen: list[bool] = []

    @storage.pauses_ingest
    def restore() -> None:
        seen.append(storage.is_paused())

    restoring = threading.Thread(target=restore)
    restoring.start()
    time.sleep(0.2)
    assert seen == []  # waiting for the restore lock, before writing its marker
    (data_dir / storage.PAUSE_MARKER_NAME).unlink()  # the check removes the stale marker
    storage._unlock(fd)
    restoring.join(10)
    assert seen == [True]


# Where file locks don't work, the marker's process tells


def test_without_locks_a_marker_whose_process_is_gone_is_removed(data_dir: Path):
    marker = data_dir / storage.PAUSE_MARKER_NAME
    _write_json_marker(marker, pid=_finished_process_id(), time=time.time() - 3 * limits.PAUSE_REFRESH)
    assert not storage.is_paused()
    assert not marker.exists()

    _write_json_marker(marker, pid=_finished_process_id())  # fresh, but its process is gone too
    assert storage.clear_stale_pause()
    assert not marker.exists()


def test_without_locks_a_marker_whose_process_id_was_reused_is_removed(data_dir: Path):
    """A restarted container's new process can have the crashed one's id: the start time tells them apart"""
    marker = data_dir / storage.PAUSE_MARKER_NAME
    started = storage._process_started(os.getpid())
    assert started is not None  # Linux
    _write_json_marker(marker, started=started - 100)
    assert not storage.is_paused()
    assert not marker.exists()


def test_without_locks_a_live_process_keeps_its_marker(data_dir: Path):
    marker = data_dir / storage.PAUSE_MARKER_NAME
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        _write_json_marker(marker, pid=process.pid, started=None, time=time.time() - 3 * limits.PAUSE_REFRESH)
        assert storage.is_paused()
        assert marker.exists()
    finally:
        process.kill()
        process.wait(10)


def test_a_marker_from_another_host_is_honoured_until_its_time_runs_out(data_dir: Path):
    marker = data_dir / storage.PAUSE_MARKER_NAME
    _write_json_marker(marker, pid=_finished_process_id(), host="another-host/boot/1")
    assert storage.is_paused()
    assert marker.exists()

    _write_json_marker(marker, host="another-host/boot/1", time=time.time() - limits.PAUSE_TTL - 1)
    assert not storage.is_paused()


def test_an_older_versions_marker_is_honoured_until_its_time_runs_out(data_dir: Path):
    marker = data_dir / storage.PAUSE_MARKER_NAME
    marker.write_text(f"{time.time() - 3 * limits.PAUSE_REFRESH:.3f}")  # its time only, and nobody holds a lock
    assert storage.is_paused()
    assert not storage.clear_stale_pause()
    assert marker.exists()

    marker.write_text(f"{time.time() - limits.PAUSE_TTL - 1:.3f}")
    assert not storage.is_paused()


def test_a_stale_check_without_locks_doesnt_remove_a_marker_written_meanwhile(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    marker = data_dir / storage.PAUSE_MARKER_NAME
    _write_json_marker(marker, pid=_finished_process_id())
    process_gone = storage._process_gone

    def a_restore_starts_meanwhile(pid: int, started: int | None) -> bool:
        _write_json_marker(marker)  # a new restore of this process's
        return process_gone(pid, started)

    monkeypatch.setattr(storage, "_process_gone", a_restore_starts_meanwhile)
    assert storage.is_paused()
    assert json.loads(marker.read_text())["pid"] == os.getpid()


def test_a_process_reading_its_own_old_marker_removes_it(data_dir: Path):
    """This process wrote no marker (no restore runs here): one naming it with another start time is a crashed one's"""
    marker = data_dir / storage.PAUSE_MARKER_NAME
    _write_json_marker(marker, started=-1)
    assert not storage.is_paused()
    assert not marker.exists()


def test_a_restore_that_gives_up_leaves_the_marker_of_one_running_in_another_process(
    data_dir: Path, fast_pause: None, restore_in_another_process: Callable[..., subprocess.Popen[str]]
):
    """Two restores at once in two workers: the second can't get the write lock and gives up; the first still runs"""
    restore_in_another_process()
    marker = data_dir / storage.PAUSE_MARKER_NAME

    @storage.pauses_ingest
    def second_restore() -> None:
        raise AssertionError("the write lock is the first restore's")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(limits, "RESTORE_LOCK_WAIT", 0.1)
        with pytest.raises(IngestBusyError):
            second_restore()
    assert marker.exists()
    assert storage.is_paused()


# ==========================================
# Where DATA_DIR doesn't support file locks


_NO_LOCKS_IN = """
import errno, fcntl, os

_flock = fcntl.flock


def flock(fd, operation):
    # DATA_DIR on a filesystem without locks (some NFS mounts): every lock there fails with ENOLCK
    if os.readlink(f"/proc/self/fd/{fd}").startswith(NO_LOCKS + "/"):
        raise OSError(errno.ENOLCK, "No locks available")
    return _flock(fd, operation)


fcntl.flock = flock
"""


def _no_locks_in(folder: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    namespace: dict[str, object] = {"NO_LOCKS": str(folder)}
    real = fcntl.flock
    exec(_NO_LOCKS_IN.replace("fcntl.flock = flock\n", ""), namespace)  # noqa: S102 (the same patch as the subprocess's)
    namespace["_flock"] = real
    monkeypatch.setattr(fcntl, "flock", namespace["flock"])


_WRITE_IN_ANOTHER_PROCESS = (
    """
import sys
from pathlib import Path

NO_LOCKS = sys.argv[1]
"""
    + _NO_LOCKS_IN
    + """
from mealie.services.ai.ingest import storage

storage._data_dir = lambda: Path(sys.argv[1])
storage.FALLBACK_LOCK_DIR = Path(sys.argv[2])
with storage.ingest_write():
    print(f"writing {storage.lock_path()}", flush=True)
    sys.stdin.readline()
print("done", flush=True)
"""
)


def test_where_data_dir_has_no_locks_a_shared_local_lock_is_used(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    _no_locks_in(data_dir, monkeypatch)
    with caplog.at_level(logging.WARNING):
        path = storage.lock_path()
        assert storage.lock_path() == path  # decided once
    digest = hashlib.sha1(str(data_dir.resolve()).encode(), usedforsecurity=False).hexdigest()[:12]
    assert path == storage.FALLBACK_LOCK_DIR / f"mealie-ai-ingest-{digest}.lock"
    assert storage.restore_lock_path() == path.with_name(f"{path.name}{storage.RESTORE_LOCK_SUFFIX}")
    [warning] = [record for record in caplog.records if "doesn't support file locks" in record.getMessage()]
    assert str(path) in warning.getMessage()
    assert storage.flock_supported()  # the lock works: no "the marker alone" warning
    assert not any("File locks aren't supported" in record.getMessage() for record in caplog.records)

    with storage.ingest_write():
        assert not _flock_from_another_process(path)  # a write section holds it
    assert _flock_from_another_process(path)


def _flock_from_another_process(path: Path) -> bool:
    """Whether another process could `flock` the file exclusively right now"""
    script = (
        "import fcntl, os, sys\n"
        "fd = os.open(sys.argv[1], os.O_RDWR)\n"
        "try:\n"
        "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "except OSError:\n"
        "    sys.exit(1)\n"
        "sys.exit(0)\n"
    )
    return subprocess.run([sys.executable, "-c", script, str(path)], timeout=30).returncode == 0


def test_a_restore_waits_for_another_processs_write_through_the_fallback_lock(
    data_dir: Path, fast_pause: None, monkeypatch: pytest.MonkeyPatch
):
    """
    Where DATA_DIR has no file locks, another worker process's write section still holds a restore off: both use the
    same lock under /tmp. Without it only the marker would apply, and the restore wouldn't wait for that write.
    """
    _no_locks_in(data_dir, monkeypatch)
    monkeypatch.setattr(limits, "RESTORE_LOCK_WAIT", 0.5)
    writer = subprocess.Popen(
        [sys.executable, "-c", _WRITE_IN_ANOTHER_PROCESS, str(data_dir), str(storage.FALLBACK_LOCK_DIR)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    ran: list[bool] = []

    @storage.pauses_ingest
    def restore() -> None:
        ran.append(storage.is_paused())

    try:
        assert _next_line(writer) == f"writing {storage.lock_path()}"
        with pytest.raises(IngestBusyError):  # it waited for the other process's write, then gave up
            restore()
        assert ran == []

        assert writer.stdin is not None
        writer.stdin.write("go on\n")
        writer.stdin.flush()
        assert _next_line(writer) == "done"
        restore()
        assert ran == [True]
    finally:
        writer.kill()
        writer.wait(10)
        for stream in (writer.stdin, writer.stdout):
            if stream is not None:
                stream.close()


def test_the_lock_folder_can_be_chosen(data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    chosen = tmp_path / "locks" / "ingest"
    monkeypatch.setattr(get_ingest_settings(), "LOCK_DIR", chosen)
    assert storage.lock_path() == chosen / storage.LOCK_FILE_NAME
    assert storage.restore_lock_path().parent == chosen
    with storage.ingest_write():
        pass
    assert (chosen / storage.LOCK_FILE_NAME).exists()


def test_a_lock_outside_data_dir_is_trusted_only_for_this_hosts_marker(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """
    Another host's restore holds its own /tmp lock, not this one: its marker isn't removed because this host's lock of
    that name is free, and is honoured until its time runs out
    """
    _no_locks_in(data_dir, monkeypatch)
    marker = data_dir / storage.PAUSE_MARKER_NAME
    lock = str(storage.restore_lock_path())
    _write_json_marker(marker, host="another-host/boot/1", lock=lock)
    assert storage.is_paused()
    assert marker.exists()

    _write_json_marker(marker, pid=_finished_process_id(), lock=lock)  # this host's, its restore gone
    assert not storage.is_paused()
    assert not marker.exists()
