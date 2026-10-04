"""
Fork: one process at a time migrates and seeds the database (docs/ai/PHASE2.md §17).

Every uvicorn worker runs `init_db.main` when it starts. Two workers starting on a database that needs migrations both
ran them: SQLite failed with "table ... already exists", PostgreSQL with a unique violation on `alembic_version`, and
uvicorn stopped. `init_db.main` now runs inside `migration_lock`, from waiting for the database until the seed data is
written, so a worker that waited finds the database at head and seeded, and does nothing.

- SQLite: an exclusive `flock` on `DATA_DIR/.mealie-migrate.lock`.
- PostgreSQL: `pg_advisory_lock(MIGRATION_LOCK_ID)` on a connection of its own (advisory locks are per database).
  Which one is decided by the dialect of `DB_URL`, the database alembic migrates.
- Both are released when the holder ends, even by a crash. Where neither works (no `fcntl`, a filesystem without
  locks), nothing is locked, as before.
"""

import contextlib
import errno
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.pool import NullPool

from mealie.core import root_logger
from mealie.core.config import get_app_dirs, get_app_settings

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]

logger = root_logger.get_logger("init_db")

LOCK_FILE_NAME = ".mealie-migrate.lock"
MIGRATION_LOCK_ID = 0x6D65616C6965  # "mealie"
"""The PostgreSQL advisory lock key"""
LOCK_TIMEOUT = 600.0
"""Seconds to wait for another process's migration before giving up"""
LOCK_POLL = 0.5
CONNECT_RETRIES = 10

_UNSUPPORTED_LOCK_ERRORS = {errno.ENOLCK, errno.EOPNOTSUPP, errno.ENOTSUP, errno.ENOSYS}


class MigrationLockTimeout(TimeoutError):
    """Another process held the migration lock for longer than `LOCK_TIMEOUT`"""

    def __init__(self) -> None:
        super().__init__(
            f"Another Mealie process has been migrating the database for over {LOCK_TIMEOUT:.0f} seconds; "
            "giving up instead of migrating at the same time"
        )


def lock_path() -> Path:
    return get_app_dirs().DATA_DIR / LOCK_FILE_NAME


def _waiting(waited: bool, deadline: float) -> bool:
    """Logs the first wait and raises once `deadline` has passed; True from then on"""
    if time.monotonic() >= deadline:
        raise MigrationLockTimeout()
    if not waited:
        logger.info("Another Mealie process is migrating the database; waiting for it to finish")
    time.sleep(LOCK_POLL)
    return True


@contextmanager
def _file_lock(timeout: float) -> Iterator[None]:
    if fcntl is None:
        yield
        return

    fd = os.open(lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout
        waited = locked = False
        while not locked:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except BlockingIOError:
                waited = _waiting(waited, deadline)
            except OSError as e:
                if e.errno not in _UNSUPPORTED_LOCK_ERRORS:
                    raise
                logger.warning(f"File locks aren't supported for {lock_path()}: database migrations aren't locked")
                break
        try:
            yield
        finally:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _connect(engine: sa.Engine) -> sa.Connection:
    """A connection, retried as `init_db.main` retries its own: the database may still be starting"""
    attempts = 0
    while True:
        try:
            return engine.connect()
        except sa.exc.OperationalError:
            attempts += 1
            if attempts >= CONNECT_RETRIES:
                raise
            logger.error("Database connection failed. Retrying in 1 second...")
            time.sleep(1)


@contextmanager
def _advisory_lock(url: str, timeout: float) -> Iterator[None]:
    # a connection of its own, closed (which releases the lock however this ends) rather than returned to a pool
    engine = sa.create_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        with _connect(engine) as connection:
            deadline = time.monotonic() + timeout
            waited = False
            while not connection.scalar(sa.text("SELECT pg_try_advisory_lock(:key)"), {"key": MIGRATION_LOCK_ID}):
                waited = _waiting(waited, deadline)
            try:
                yield
            finally:
                # closing the connection releases the lock too, should this fail (a dropped connection)
                with contextlib.suppress(sa.exc.SQLAlchemyError):
                    connection.execute(sa.text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK_ID})
    finally:
        engine.dispose()


@contextmanager
def migration_lock(timeout: float = LOCK_TIMEOUT) -> Iterator[None]:
    """
    Holds the database's migration lock, waiting up to `timeout` seconds for another process to release it (then
    `MigrationLockTimeout`)
    """
    # by the database alembic migrates (`DB_URL`), which needn't be `DB_ENGINE`'s: a provider set in code wins
    url = get_app_settings().DB_URL
    if url and sa.make_url(url).get_backend_name() == "postgresql":
        with _advisory_lock(url, timeout):
            yield
    else:
        with _file_lock(timeout):
            yield
