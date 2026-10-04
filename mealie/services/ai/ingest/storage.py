"""
Where recipe card files live, how they're written, and how ingestion pauses for a backup restore
(docs/ai/PHASE2.md §2, §3.9).

Layout: `DATA_DIR/groups/<group_id>/ai-ingest/<job_id>/pages/<index>/{page.jpg,view.jpg,thumb.webp}`, and eval cases
in `DATA_DIR/groups/<group_id>/eval-cards/`. `groups/` exists from boot and is backed up with the rows, so a restore
never meets a missing top-level directory.

**The pause.** A restore deletes and copies `groups/` and `recipes/` while background work carries on, and a directory
or file created between the delete and the copy aborts it after the database was replaced. So:

- `is_paused()`: the marker `DATA_DIR/.ai-ingest-paused` exists, was refreshed less than `PAUSE_TTL` ago, and its
  restore isn't known to be gone. It's a root-level file, which a restore leaves alone, and every worker process sees
  it.
- `ingest_write()`: the only way fork code writes under `groups/` or `recipes/`. It checks the marker, joins the
  process's shared `flock` on `DATA_DIR/.ai-ingest-lock` without waiting, checks the marker again, and raises
  `IngestPaused` if any of that fails. Writers never block on the lock.
- `pauses_ingest`: the decorator on `BackupV2.restore`. It holds the restore lock (`DATA_DIR/.ai-ingest-lock.restore`,
  shared) for its whole run and writes the marker (refreshed every `PAUSE_REFRESH` by a daemon thread), then waits up
  to `RESTORE_LOCK_WAIT` for its own process's write sections to end and for an exclusive `flock` on the write lock,
  which other processes' in-flight writers hold off until they finish. If they don't, it raises `IngestBusyError`
  before the restore has touched anything. Once the restore has run, still paused, every running task is queued
  again with no lease (`IngestQueue.requeue_all_running`): a restored row may carry the live token of a task that ran
  when the backup was taken, whose result must not land on it.

**A marker whose restore is gone** (a crash or a container stop mid-restore) is removed by the next `is_paused()` in
any process, so ingestion doesn't wait out `PAUSE_TTL`. The marker records its restore's process (host identity,
process id and start time) and the restore lock it holds. A check that takes that lock exclusively proves no restore
is running, and removes the marker while holding it, so a new restore (which takes the lock before writing its
marker) never loses its own. Where locks don't work, a marker whose process ran on this same host and is gone (or
was replaced under the same id) is removed instead. A marker an older version wrote (its time only) is honoured until
`PAUSE_TTL`. The dispatcher runs the check at start too (`clear_stale_pause`).

**One shared lock per process, and a gate in it.** The first write section a process opens takes the shared `flock`
and the last one to end releases it; a restore waits for its own process's sections through an in-process gate, not
the lock. On Linux, NFS included, `flock` locks belong to the open file description (NFS clients emulate them with
byte-range locks owned by it; flock(2), "NFS details"): two descriptors conflict even within one process, and closing
one never drops a lock held through another. The gate is defence in depth for platforms where `flock` locks belong
to the process instead, as POSIX record locks do: those never conflict between a process's own threads, and closing
any descriptor of the file drops them all. Where the filesystem doesn't support locks at all (`ENOLCK`,
`EOPNOTSUPP`), one warning is logged; the marker and the gate still apply, but a restore can't wait for other worker
processes' writes, and a stale marker is told only by its process.

**The dispatcher's presence file** `DATA_DIR/.ai-ingest-dispatcher`: every running dispatcher sets its modification time
at most once every `DISPATCHER_SEEN_INTERVAL` (one atomic `utime`, no content), paused or not, so
`dispatcher_seen_at()` tells whether any process reads cards.

This module imports only the standard library and `mealie.core`, since the backup service imports it.
"""

import errno
import fcntl
import functools
import json
import os
import shutil
import socket
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import IO, Any
from uuid import UUID

from mealie.core.config import get_app_dirs
from mealie.core.root_logger import get_logger
from mealie.services.ai.errors import IngestBusyError, IngestPaused

from . import limits

logger = get_logger(__name__)

INGEST_DIR_NAME = "ai-ingest"
EVAL_CARDS_DIR_NAME = "eval-cards"
PAUSE_MARKER_NAME = ".ai-ingest-paused"
LOCK_FILE_NAME = ".ai-ingest-lock"
DISPATCHER_SEEN_NAME = ".ai-ingest-dispatcher"
RESTORE_LOCK_SUFFIX = ".restore"
"""The restore lock is named after the write lock, with this suffix (`.ai-ingest-lock.restore`)"""
RESTORE_LOCK_HOLD_WAIT = 5.0
"""How long a restore waits for a stale-marker check to let go of the restore lock (it holds it for moments)"""

_UNSUPPORTED_LOCK_ERRORS = {errno.ENOLCK, errno.EOPNOTSUPP, errno.ENOTSUP, errno.ENOSYS}

# ==========================================
# Paths


def ingest_root(group_id: UUID) -> Path:
    """`DATA_DIR/groups/<group_id>/ai-ingest`"""
    return get_app_dirs().GROUPS_DIR / str(group_id) / INGEST_DIR_NAME


def job_dir(group_id: UUID, job_id: UUID) -> Path:
    return ingest_root(group_id) / str(job_id)


def page_dir(group_id: UUID, job_id: UUID, index: int) -> Path:
    """A page's directory; `index` is 0 for the front"""
    return job_dir(group_id, job_id) / "pages" / str(index)


def eval_cards_dir(group_id: UUID) -> Path:
    """`DATA_DIR/groups/<group_id>/eval-cards`: the group's private eval set (§11.6). Never purged."""
    return get_app_dirs().GROUPS_DIR / str(group_id) / EVAL_CARDS_DIR_NAME


def create_job_dir(group_id: UUID, job_id: UUID, page_count: int) -> Path:
    """
    Creates a new job's directory with an empty folder for each page, and returns it. The only place directories are
    created under `ai-ingest/`: everything else writes into ones that exist. Callers hold `ingest_write()`.

    Raises `FileExistsError` if the job directory already exists.
    """
    path = job_dir(group_id, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir()
    for index in range(page_count):
        (path / "pages" / str(index)).mkdir(parents=True)
    return path


def remove_job_dir(group_id: UUID, job_id: UUID) -> bool:
    """Deletes a job's directory and everything in it; whether there was one. Callers hold `ingest_write()`."""
    path = job_dir(group_id, job_id)
    if not path.exists():
        return False
    shutil.rmtree(path)
    return True


def _atomic_write(path: Path, write: Callable[[IO[bytes]], None]) -> None:
    # The temporary file is in the destination's own directory, which must exist: nothing here creates directories,
    # so a restore that replaced the directory makes this fail rather than leave a stray file behind
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            write(file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Writes `data` to `path` through a temporary file in the same directory, then `os.replace`. Never creates
    directories: a missing parent raises `FileNotFoundError`."""

    def write(file: IO[bytes]) -> None:
        file.write(data)

    _atomic_write(path, write)


def atomic_save_image(image: Any, path: Path, format: str, **params: Any) -> None:
    """`image.save(path, format, **params)` (a Pillow image), atomically, like `atomic_write_bytes`"""

    def write(file: IO[bytes]) -> None:
        image.save(file, format=format, **params)

    _atomic_write(path, write)


# ==========================================
# The dispatcher's presence


def dispatcher_seen_path() -> Path:
    return _data_dir() / DISPATCHER_SEEN_NAME


def mark_dispatcher_seen() -> None:
    """A running dispatcher's sign of life: sets the presence file's modification time to now, creating it if needed"""
    path = dispatcher_seen_path()
    now = time.time()
    try:
        os.utime(path, (now, now))
    except FileNotFoundError:
        path.touch()
        os.utime(path, (now, now))


def dispatcher_seen_at() -> float | None:
    """When a dispatcher, in any process, last marked itself running (a Unix time); None when none ever did"""
    try:
        return dispatcher_seen_path().stat().st_mtime
    except OSError:
        return None


# ==========================================
# The pause


def _data_dir() -> Path:
    return get_app_dirs().DATA_DIR


def pause_marker_path() -> Path:
    return _data_dir() / PAUSE_MARKER_NAME


def lock_path() -> Path:
    return _data_dir() / LOCK_FILE_NAME


def restore_lock_path() -> Path:
    """The lock a restore holds (shared) for as long as its marker exists: next to the write lock"""
    path = lock_path()
    return path.with_name(f"{path.name}{RESTORE_LOCK_SUFFIX}")


@dataclass(frozen=True)
class _Marker:
    """The pause marker as read"""

    raw: str
    time: float
    """When it was last refreshed"""
    pid: int | None = None
    host: str | None = None
    """`_host_identity()` of the restore's process"""
    started: int | None = None
    """The restore process's start time (Linux clock ticks since boot), telling a reused process id apart"""
    lock: str | None = None
    """The restore lock its restore holds, if it could take one"""

    def fresh(self) -> bool:
        # a marker from the future (a clock change) is honoured for no longer than a fresh one
        return abs(time.time() - self.time) < limits.PAUSE_TTL


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _read_marker(path: Path) -> _Marker | None:
    """
    The marker, or None when there's none. Reads this version's JSON and an older version's bare time; anything else
    (unreadable, half written) counts from its modification time.
    """
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    except OSError:
        mtime = _mtime(path)
        return None if mtime is None else _Marker(raw="", time=mtime)

    content = raw.strip()
    try:
        return _Marker(raw=raw, time=float(content))
    except ValueError:
        pass
    try:
        data = json.loads(content)
        return _Marker(
            raw=raw,
            time=float(data["time"]),
            pid=int(data["pid"]) if data.get("pid") is not None else None,
            host=str(data["host"]) if data.get("host") is not None else None,
            started=int(data["started"]) if data.get("started") is not None else None,
            lock=str(data["lock"]) if data.get("lock") is not None else None,
        )
    except ValueError, TypeError, KeyError, AttributeError:
        mtime = _mtime(path)
        return None if mtime is None else _Marker(raw=raw, time=mtime)


def _marker_time(marker: Path) -> float | None:
    """When the marker was last refreshed, in either format; None when there's none"""
    read = _read_marker(marker)
    return None if read is None else read.time


def _process_started(pid: int) -> int | None:
    """A process's start time in clock ticks since boot (Linux `/proc/<pid>/stat`), or None when it can't be read"""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        # the command name (field 2) may hold spaces and parentheses: the fields after its closing one start at 3
        return int(stat.rsplit(")", 1)[1].split()[19])
    except OSError, IndexError, ValueError:
        return None


@functools.cache
def _host_identity() -> str:
    """
    The host name with the kernel's boot id and this process's PID namespace, where Linux tells them: process ids are
    only comparable between processes of the same identity (a restarted container has a new PID namespace)
    """
    parts = [socket.gethostname()]
    try:
        parts.append(Path("/proc/sys/kernel/random/boot_id").read_text().strip())
    except OSError:
        pass
    try:
        parts.append(str(os.stat("/proc/self/ns/pid").st_ino))
    except OSError:
        pass
    return "/".join(parts)


def _process_gone(pid: int, started: int | None) -> bool:
    """Whether the process that wrote a marker on this host is gone: no such process, or another one under its id"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False  # it exists (another user's)
    if started is None:
        return False
    current = _process_started(pid)
    return current is None or current != started


def _write_marker(lock: Path | None = None) -> None:
    """
    Writes the marker atomically: the time, the restore's process (host identity, process id and start time) and the
    restore lock it holds (`lock`), if any
    """
    marker = pause_marker_path()
    pid = os.getpid()
    content = {
        "time": round(time.time(), 3),
        "pid": pid,
        "host": _host_identity(),
        "started": _process_started(pid),
        "lock": str(lock) if lock is not None else None,
    }
    temp = marker.with_name(f"{marker.name}.{pid}.{threading.get_ident()}.tmp")
    temp.write_text(json.dumps(content))
    os.replace(temp, marker)


_stale_guard = threading.Lock()
"""One stale-marker check at a time in this process"""


def is_paused() -> bool:
    """
    Whether a backup restore has paused ingestion: the marker exists, was refreshed under `PAUSE_TTL` ago, and its
    restore isn't known to be gone. A marker whose restore is gone is removed (`clear_stale_pause`).
    """
    marker = _read_marker(pause_marker_path())
    if marker is None or not marker.fresh():
        return False
    if marker.lock is None and marker.pid is None:
        return True  # an older version's marker (its time only): honoured until `PAUSE_TTL`
    return not _remove_if_stale()


def clear_stale_pause() -> bool:
    """
    Removes the pause marker when its restore is no longer running (a crash or a container stop mid-restore), and
    says whether it did. The dispatcher runs it at start; `is_paused` does the same check.
    """
    marker = _read_marker(pause_marker_path())
    if marker is None or (marker.lock is None and marker.pid is None):
        return False
    return _remove_if_stale() and not pause_marker_path().exists()


class _RestoreLock(StrEnum):
    """What a check of the restore lock found"""

    held = "held"
    """A restore holds it: its marker is live"""
    free = "free"
    """Nobody holds it: taken exclusively, so no restore can start while the check finishes"""
    unknown = "unknown"
    """Locks don't work here"""


def _probe_restore_lock() -> tuple[_RestoreLock, int | None]:
    """The restore lock's state, and the descriptor holding it exclusively when it's free (the caller releases it)"""
    try:
        fd = os.open(restore_lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return _RestoreLock.unknown, None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _RestoreLock.free, fd
    except BlockingIOError:
        os.close(fd)
        return _RestoreLock.held, None
    except OSError as e:
        os.close(fd)
        # where locks don't work (or this one can't be taken) the marker's process tells instead
        logger.debug(f"Couldn't check the recipe card restore lock: {type(e).__name__}: {e}")
        return _RestoreLock.unknown, None


def _unlock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _remove_stale(marker: _Marker, why: str) -> None:
    try:
        pause_marker_path().unlink(missing_ok=True)
    except OSError as e:
        logger.warning(f"Couldn't remove the stale recipe card pause marker ({type(e).__name__}): ingestion carries on")
        return
    age = max(time.time() - marker.time, 0.0)
    logger.info(
        f"Removed the recipe card ingestion pause marker of a backup restore that is no longer running ({why}; last "
        f"refreshed {age:.0f} s ago): ingestion carries on"
    )


def _remove_if_stale() -> bool:
    """
    Whether the marker's restore is gone (the marker is then removed, or was already): its restore lock is free, or,
    where locks don't work, its process on this host is gone. False while it may still be running.
    """
    path = pause_marker_path()
    with _stale_guard:
        marker = _read_marker(path)
        if marker is None:
            return True
        with _pause_guard:
            if _active_pauses > 0:
                return False  # this process's own restore

        if marker.lock is not None and marker.lock == str(restore_lock_path()):
            state, fd = _probe_restore_lock()
            if state == _RestoreLock.held:
                return False
            if fd is not None:
                try:
                    # every restore holds the lock before it writes its marker, until after it removes it: none runs,
                    # and none can write a new marker while this check holds it
                    if _read_marker(path) is not None:
                        _remove_stale(marker, "its restore lock is free")
                finally:
                    _unlock(fd)
                return True

        if marker.pid is not None and marker.host == _host_identity() and _process_gone(marker.pid, marker.started):
            # no lock to hold a new restore off: only a marker unchanged since it was read is removed
            current = _read_marker(path)
            if current is not None and current.raw == marker.raw:
                _remove_stale(marker, f"its process {marker.pid} is gone")
            return current is None or current.raw == marker.raw
        return False


def _hold_restore_lock() -> int | None:
    """
    The restore lock, shared, for as long as this restore's marker exists: its descriptor, or None where locks don't
    work (the marker then says so). Waits for a stale-marker check that holds it exclusively, which takes moments.
    """
    try:
        fd = os.open(restore_lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as e:
        logger.warning(f"Couldn't open the recipe card restore lock: {type(e).__name__}: {e}")
        return None
    deadline = time.monotonic() + RESTORE_LOCK_HOLD_WAIT
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.01)
        except OSError as e:
            os.close(fd)
            if e.errno in _UNSUPPORTED_LOCK_ERRORS:
                _warn_lock_unsupported(e)
            else:
                logger.warning(f"Couldn't take the recipe card restore lock: {type(e).__name__}: {e}")
            return None


_lock_warning_logged = False
_lock_warning_guard = threading.Lock()


def _warn_lock_unsupported(error: OSError) -> None:
    global _lock_warning_logged
    with _lock_warning_guard:
        if _lock_warning_logged:
            return
        _lock_warning_logged = True
    logger.warning(
        f"File locks aren't supported for {lock_path()} ({errno.errorcode.get(error.errno or 0, error.errno)}): "
        "a backup restore pauses recipe card ingestion with its marker file, and can't wait for other worker "
        "processes' writes already in progress"
    )


def _open_lock_file() -> int:
    return os.open(lock_path(), os.O_RDWR | os.O_CREAT, 0o600)


_gate = threading.Condition()
"""Guards the process's write sections and its restore (below)"""
_writers = 0
"""Write sections open in this process"""
_writers_fd: int | None = None
"""The process's shared lock, held while `_writers > 0`; None where locks aren't supported"""
_restoring = False
"""A restore in this process holds the lock exclusively: none of the process's write sections may start"""


def flock_supported() -> bool:
    """Whether `flock` works on `DATA_DIR`; logs one warning when it doesn't. For a startup check."""
    with _gate:
        if _writers or _restoring:
            # the process holds the lock: closing another descriptor of the file could drop it (POSIX locks)
            return not _lock_warning_logged
        fd = _open_lock_file()
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True  # a restore holds it: locks work
            except OSError as e:
                if e.errno in _UNSUPPORTED_LOCK_ERRORS:
                    _warn_lock_unsupported(e)
                    return False
                raise
            fcntl.flock(fd, fcntl.LOCK_UN)
            return True
        finally:
            os.close(fd)


def _take_shared_lock() -> int | None:
    """
    The process's shared lock, without waiting: its descriptor, or None where locks aren't supported.
    `IngestPaused` when a restore holds the lock. Called under `_gate` by the first write section.
    """
    fd = _open_lock_file()
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return fd
    except BlockingIOError as e:
        os.close(fd)
        raise IngestPaused() from e
    except OSError as e:
        os.close(fd)
        if e.errno not in _UNSUPPORTED_LOCK_ERRORS:
            raise
        _warn_lock_unsupported(e)
        return None
    except BaseException:
        os.close(fd)
        raise


def _enter_write_section() -> None:
    global _writers, _writers_fd
    with _gate:
        if _restoring:
            raise IngestPaused()
        if _writers == 0:
            _writers_fd = _take_shared_lock()
        _writers += 1


def _leave_write_section() -> None:
    global _writers, _writers_fd
    with _gate:
        _writers -= 1
        if _writers > 0:
            return
        fd, _writers_fd = _writers_fd, None
        try:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
        finally:
            _gate.notify_all()


@contextmanager
def ingest_write() -> Iterator[None]:
    """
    Holds the ingest write lock (shared) for a section that writes under `groups/` or `recipes/`. Raises
    `IngestPaused`, having written nothing, while a restore is pending or running. Never waits.
    """
    if is_paused():
        raise IngestPaused()

    _enter_write_section()
    try:
        # a restore may have written its marker while this one was taking the lock
        if is_paused():
            raise IngestPaused()
        yield
    finally:
        _leave_write_section()


_pause_guard = threading.Lock()
_active_pauses = 0
"""Pauses in progress in this process: the marker stays until the last one ends"""


def _refresh_marker(stop: threading.Event, lock: Path | None) -> None:
    while not stop.wait(limits.PAUSE_REFRESH):
        try:
            _write_marker(lock)
        except OSError:
            logger.exception("Couldn't refresh the recipe card ingestion pause marker")


def _remove_marker_unless_restoring() -> None:
    """
    The last pause of this process ended, its restore lock released: the marker goes, unless a restore in another
    process holds the restore lock (this one gave up while that one runs, and the marker is that one's too)
    """
    state, fd = _probe_restore_lock()
    try:
        if state != _RestoreLock.held:
            pause_marker_path().unlink(missing_ok=True)
    finally:
        if fd is not None:
            _unlock(fd)


def _requeue_running_tasks() -> None:
    """
    After a restore, still paused: every running task back in the queue with no lease, so no task that ran before
    the restore applies its result to a restored row (one holding its token came back from a backup taken while it
    ran), and restored rows the backup held running are read again at once. A failure is logged: those rows' leases
    then expire and the sweep queues them, as before.
    """
    # imported here: this module stays importable by the backup service without the database layer
    from mealie.db.db_setup import session_context
    from mealie.repos.repository_recipe_ingest import IngestQueue

    try:
        with session_context() as session:
            requeued = IngestQueue(session).requeue_all_running()
    except Exception as e:
        logger.warning(f"Couldn't queue the running recipe card tasks again after the restore: {type(e).__name__}")
        return
    if requeued:
        logger.info(f"Backup restore: {requeued} recipe card task(s) that were running are queued again")


def _lock_exclusively(deadline: float) -> int | None:
    """
    Waits until `deadline` for the exclusive lock (other processes' writers holding it shared) and returns its file
    descriptor, or None where locks aren't supported. Raises `IngestBusyError` if writers still hold it.
    """
    fd = _open_lock_file()
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise IngestBusyError() from None
                time.sleep(limits.RESTORE_LOCK_POLL)
            except OSError as e:
                if e.errno not in _UNSUPPORTED_LOCK_ERRORS:
                    raise
                _warn_lock_unsupported(e)
                os.close(fd)
                return None
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _end_restoring() -> None:
    global _restoring
    with _gate:
        _restoring = False
        _gate.notify_all()


def _take_exclusive_lock() -> int | None:
    """
    Waits up to `RESTORE_LOCK_WAIT` for this process's write sections to end (and any other restore of this process),
    closes the gate to new ones, then waits for the exclusive lock within the same limit. Returns its file
    descriptor, or None where locks aren't supported; the caller then ends with `_end_restoring`. Raises
    `IngestBusyError`, holding nothing, if writers are still busy.
    """
    global _restoring
    deadline = time.monotonic() + limits.RESTORE_LOCK_WAIT
    with _gate:
        while _writers > 0 or _restoring:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise IngestBusyError()
            _gate.wait(min(remaining, limits.RESTORE_LOCK_POLL))
        _restoring = True

    try:
        return _lock_exclusively(deadline)
    except BaseException:
        _end_restoring()
        raise


def pauses_ingest[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """
    Pauses recipe card ingestion around `func` (a backup restore): holds the restore lock, writes the pause marker and
    keeps it fresh, waits for in-flight writers (this process's through the gate, other processes' by taking the
    write lock exclusively), then calls `func`. After `func`, returned or raised, it queues every running task again
    while still paused. However it ends, the marker is removed (once no other pause in this process needs it, and no
    restore in another process holds the restore lock) and the locks released. Raises `IngestBusyError`, without
    calling `func`, when writers are still busy after `RESTORE_LOCK_WAIT`.
    """

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        global _active_pauses
        # held from before the marker is written until the restore is over: a marker nobody holds the restore lock
        # for is stale (`is_paused`)
        holder = _hold_restore_lock()
        holder_path = restore_lock_path() if holder is not None else None
        try:
            with _pause_guard:
                _write_marker(holder_path)
                _active_pauses += 1

            stop = threading.Event()
            refresher = threading.Thread(
                target=_refresh_marker, args=(stop, holder_path), name="ai-ingest-pause", daemon=True
            )
            refresher.start()
            locked = False
            fd: int | None = None
            try:
                fd = _take_exclusive_lock()
                locked = True
                # written again now that writers are done: where locks don't work, a stale-marker check racing the
                # first write may have removed it
                _write_marker(holder_path)
                try:
                    return func(*args, **kwargs)
                finally:
                    _requeue_running_tasks()
            finally:
                stop.set()
                refresher.join(timeout=5)
                if holder is not None:
                    _unlock(holder)
                    holder = None
                with _pause_guard:
                    _active_pauses -= 1
                    if _active_pauses == 0:
                        _remove_marker_unless_restoring()
                if locked:
                    try:
                        if fd is not None:
                            fcntl.flock(fd, fcntl.LOCK_UN)
                            os.close(fd)
                    finally:
                        _end_restoring()
        finally:
            if holder is not None:
                _unlock(holder)

    return wrapper
