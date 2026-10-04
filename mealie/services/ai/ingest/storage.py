"""
Where recipe card files live, how they're written, and how ingestion pauses for a backup restore
(docs/ai/PHASE2.md §2, §3.9).

Layout: `DATA_DIR/groups/<group_id>/ai-ingest/<job_id>/pages/<index>/{page.jpg,view.jpg,thumb.webp}`, and eval cases
in `DATA_DIR/groups/<group_id>/eval-cards/`. `groups/` exists from boot and is backed up with the rows, so a restore
never meets a missing top-level directory.

**The pause.** A restore deletes and copies `groups/` and `recipes/` while background work carries on, and a directory
or file created between the delete and the copy aborts it after the database was replaced. So:

- `is_paused()`: the marker `DATA_DIR/.ai-ingest-paused` exists and was refreshed less than `PAUSE_TTL` ago. It's a
  root-level file, which a restore leaves alone, and every worker process sees it.
- `ingest_write()`: the only way fork code writes under `groups/` or `recipes/`. It checks the marker, joins the
  process's shared `flock` on `DATA_DIR/.ai-ingest-lock` without waiting, checks the marker again, and raises
  `IngestPaused` if any of that fails. Writers never block on the lock.
- `pauses_ingest`: the decorator on `BackupV2.restore`. It writes the marker (refreshed every `PAUSE_REFRESH` by a
  daemon thread), then waits up to `RESTORE_LOCK_WAIT` for its own process's write sections to end and for an
  exclusive `flock`, which other processes' in-flight writers hold off until they finish. If they don't, it raises
  `IngestBusyError` before the restore has touched anything.

**One shared lock per process, and a gate in it.** The first write section a process opens takes the shared `flock`
and the last one to end releases it; a restore waits for its own process's sections through an in-process gate, not
the lock. So it also works where locks belong to the process rather than to an open file: Linux NFS clients emulate
`flock` with POSIX record locks (flock(2), "NFS details"), which never conflict between threads and are all dropped
when any descriptor of the file is closed. Where the filesystem doesn't support locks at all (`ENOLCK`,
`EOPNOTSUPP`), one warning is logged; the marker and the gate still apply, but a restore can't wait for other worker
processes' writes.

This module imports only the standard library and `mealie.core`, since the backup service imports it.
"""

import errno
import fcntl
import functools
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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
# The pause


def _data_dir() -> Path:
    return get_app_dirs().DATA_DIR


def pause_marker_path() -> Path:
    return _data_dir() / PAUSE_MARKER_NAME


def lock_path() -> Path:
    return _data_dir() / LOCK_FILE_NAME


def _marker_time(marker: Path) -> float | None:
    try:
        content = marker.read_text().strip()
    except FileNotFoundError:
        return None
    except OSError:
        # unreadable: count it from its modification time
        try:
            return marker.stat().st_mtime
        except OSError:
            return None

    try:
        return float(content)
    except ValueError:
        try:
            return marker.stat().st_mtime
        except OSError:
            return None


def is_paused() -> bool:
    """Whether a backup restore has paused ingestion: the marker exists and was refreshed under `PAUSE_TTL` ago"""
    refreshed = _marker_time(pause_marker_path())
    if refreshed is None:
        return False
    # a marker from the future (a clock change) is honoured for no longer than a fresh one
    return abs(time.time() - refreshed) < limits.PAUSE_TTL


def _write_marker() -> None:
    marker = pause_marker_path()
    temp = marker.with_name(f"{marker.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temp.write_text(f"{time.time():.3f}")
    os.replace(temp, marker)


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


def _refresh_marker(stop: threading.Event) -> None:
    while not stop.wait(limits.PAUSE_REFRESH):
        try:
            _write_marker()
        except OSError:
            logger.exception("Couldn't refresh the recipe card ingestion pause marker")


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
    Pauses recipe card ingestion around `func` (a backup restore): writes the pause marker and keeps it fresh, waits
    for in-flight writers (this process's through the gate, other processes' by taking the write lock exclusively),
    then calls `func`. However `func` ends, the marker is removed (once no other pause in this process needs it) and
    the lock released. Raises `IngestBusyError`, without calling `func`, when writers are still busy after
    `RESTORE_LOCK_WAIT`.
    """

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        global _active_pauses
        with _pause_guard:
            _write_marker()
            _active_pauses += 1

        stop = threading.Event()
        refresher = threading.Thread(target=_refresh_marker, args=(stop,), name="ai-ingest-pause", daemon=True)
        refresher.start()
        locked = False
        fd: int | None = None
        try:
            fd = _take_exclusive_lock()
            locked = True
            return func(*args, **kwargs)
        finally:
            stop.set()
            refresher.join(timeout=5)
            with _pause_guard:
                _active_pauses -= 1
                if _active_pauses == 0:
                    pause_marker_path().unlink(missing_ok=True)
            if locked:
                try:
                    if fd is not None:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                        os.close(fd)
                finally:
                    _end_restoring()

    return wrapper
