"""
Fork: one process at a time migrates and seeds the database (docs/ai/PHASE2.md §17).

Every uvicorn worker runs `init_db.main` when it starts. Two workers starting on a database that needs migrations both
ran them: SQLite failed with "table ... already exists", PostgreSQL with a unique violation on `alembic_version`, and
uvicorn stopped. `init_db.main` now runs inside `migration_lock`, from waiting for the database until the seed data is
written, so a worker that waited finds the database at head and seeded, and does nothing.

- SQLite: an exclusive `flock` on `DATA_DIR/.mealie-migrate.lock`.
- PostgreSQL: `pg_try_advisory_xact_lock(MIGRATION_LOCK_ID)` on a connection of its own (advisory locks are per
  database), held by keeping that transaction open until the migration ends. A transaction's lock, rather than a
  session's, works behind PgBouncer in transaction pooling mode: PgBouncer keeps a client on one server connection
  while it's in a transaction, and drops that server connection when the client goes mid-transaction. (A session's
  lock taken and released in separate statements could land on different server connections there: two workers held
  it at once, and it outlived every Mealie process on a pooled connection.) The transaction turns off the server's
  `idle_in_transaction_session_timeout` and `transaction_timeout` for itself, since it sits idle while the migration
  runs on other connections; a pooler's own limit on idle transactions (PgBouncer's `idle_transaction_timeout`, off by
  default) must be longer than a migration.
  Which one is decided by the dialect of `DB_URL`, the database alembic migrates.
- Both are released when the holder ends, even by a crash. Where neither works (no `fcntl`, a filesystem without
  locks), nothing is locked, as before.

A process waiting for another's migration waits for as long as that process holds the lock, and logs every minute
that it's still waiting: a migration can take a long time (a large database on a small computer), and the lock goes
when its holder dies. Giving up would fail the worker's startup, and uvicorn then stops every worker.

A backup restore holds the lock too, from before it drops the tables until the restored database is migrated and
seeded (`BackupV2.restore`), so a process starting meanwhile waits and finds the restored database ready rather than
migrating and seeding a dropped one. The lock is re-entrant in the thread that holds it: the restore's own
`init_db.main` doesn't wait for itself.
"""

import contextlib
import errno
import os
import threading
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
LOCK_POLL = 0.5
PROGRESS_EVERY = 60.0
"""Seconds between the "still waiting" logs of a process waiting for another's migration"""
CONNECT_RETRIES = 10

# the server's limits that would end the lock's transaction, idle while the migration runs (those it has: a server
# refuses a setting it doesn't know, and `transaction_timeout` is PostgreSQL 17's)
_NO_TIMEOUTS = sa.text(
    "SELECT set_config(name, '0', true) FROM pg_settings "
    "WHERE name IN ('idle_in_transaction_session_timeout', 'transaction_timeout')"
)

_UNSUPPORTED_LOCK_ERRORS = {errno.ENOLCK, errno.EOPNOTSUPP, errno.ENOTSUP, errno.ENOSYS}

_holding = threading.local()
"""How deep in `migration_lock` blocks this thread is: inside one, it holds the lock already"""


class MigrationLockTimeout(TimeoutError):
    """Another process held the migration lock for longer than the `timeout` given to `migration_lock`"""

    def __init__(self, timeout: float) -> None:
        super().__init__(
            f"Another Mealie process has been migrating the database for over {timeout:.0f} seconds; "
            "giving up instead of migrating at the same time"
        )


class _Wait:
    """A wait for another process's migration, from when the lock was first asked for"""

    def __init__(self, timeout: float | None) -> None:
        self.timeout = timeout
        self.started = time.monotonic()
        self.logged: float | None = None

    def poll(self) -> None:
        """
        Sleeps before the next try; logs the first wait, and every `PROGRESS_EVERY` seconds after it. Raises
        `MigrationLockTimeout` once a `timeout` has passed (there's none by default).
        """
        now = time.monotonic()
        waited = now - self.started
        if self.timeout is not None and waited >= self.timeout:
            raise MigrationLockTimeout(self.timeout)
        if self.logged is None:
            logger.info("Another Mealie process is migrating the database; waiting for it to finish")
            self.logged = now
        elif now - self.logged >= PROGRESS_EVERY:
            logger.info(
                "Still waiting for another Mealie process to finish migrating the database "
                f"({waited / 60:.0f} min so far)"
            )
            self.logged = now
        time.sleep(LOCK_POLL)


def lock_path() -> Path:
    return get_app_dirs().DATA_DIR / LOCK_FILE_NAME


@contextmanager
def _file_lock(timeout: float | None) -> Iterator[None]:
    if fcntl is None:
        yield
        return

    fd = os.open(lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        wait = _Wait(timeout)
        locked = False
        while not locked:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except BlockingIOError:
                wait.poll()
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
def _advisory_lock(url: str, timeout: float | None) -> Iterator[None]:
    # a connection of its own, closed (which ends the transaction, and so the lock, however this ends) rather than
    # returned to a pool
    engine = sa.create_engine(url, poolclass=NullPool)
    try:
        with _connect(engine) as connection:
            wait = _Wait(timeout)
            while True:
                transaction = connection.begin()
                if connection.scalar(sa.text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": MIGRATION_LOCK_ID}):
                    break
                transaction.rollback()  # nothing held open while waiting, so a pooler can lend the server out
                wait.poll()
            try:
                connection.execute(_NO_TIMEOUTS)
                yield
            finally:
                # ending the transaction releases the lock; closing the connection does too, should this fail
                with contextlib.suppress(sa.exc.SQLAlchemyError):
                    transaction.rollback()
    finally:
        engine.dispose()


@contextmanager
def migration_lock(timeout: float | None = None) -> Iterator[None]:
    """
    Holds the database's migration lock, waiting for as long as another process (or thread) holds it; with a
    `timeout`, for up to that many seconds (then `MigrationLockTimeout`). In a thread that holds it already, it holds
    on without waiting.
    """
    depth: int = getattr(_holding, "depth", 0)
    if depth:
        _holding.depth = depth + 1
        try:
            yield
        finally:
            _holding.depth = depth
        return

    # by the database alembic migrates (`DB_URL`), which needn't be `DB_ENGINE`'s: a provider set in code wins
    url = get_app_settings().DB_URL
    if url and sa.make_url(url).get_backend_name() == "postgresql":
        lock = _advisory_lock(url, timeout)
    else:
        lock = _file_lock(timeout)
    with lock:
        _holding.depth = 1
        try:
            yield
        finally:
            _holding.depth = 0
