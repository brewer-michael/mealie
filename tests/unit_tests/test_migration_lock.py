"""
Fork: one process at a time migrates and seeds the database, so several workers can start together on a new or
outdated database (mealie/db/migration_lock.py). Runs against the test database's engine: SQLite (a file lock) or
PostgreSQL (an advisory lock).
"""

import os
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import sqlalchemy as sa

from mealie.core.config import get_app_settings
from mealie.core.settings.db_providers import PostgresProvider, SQLiteProvider
from mealie.db import migration_lock as lock_module
from mealie.db.migration_lock import MIGRATION_LOCK_ID, MigrationLockTimeout, migration_lock

REPO_ROOT = Path(__file__).parents[2]

START_WORKER = textwrap.dedent(
    """
    import os, time

    from mealie.db import init_db

    start = float(os.environ["START_AT"])
    while time.time() < start:
        time.sleep(0.005)
    init_db.main()
    """
)


def is_postgres() -> bool:
    return get_app_settings().DB_ENGINE == "postgres"


def test_a_second_holder_waits_and_gives_up_at_the_timeout():
    with migration_lock():
        started = time.monotonic()
        with pytest.raises(MigrationLockTimeout):
            with migration_lock(timeout=0.6):
                pytest.fail("took a lock another holder has")
        assert time.monotonic() - started >= 0.6

    with migration_lock(timeout=0.6):
        pass


def test_a_waiting_holder_gets_the_lock_once_it_is_released(caplog: pytest.LogCaptureFixture):
    acquired = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with migration_lock():
            acquired.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert acquired.wait(10)
        threading.Timer(0.8, release.set).start()
        started = time.monotonic()
        with migration_lock(timeout=10):
            assert release.is_set()
        assert time.monotonic() - started >= 0.7
    finally:
        release.set()
        holder.join(10)
    assert "waiting for it to finish" in caplog.text


def test_the_lock_is_released_when_the_holder_raises():
    with pytest.raises(RuntimeError), migration_lock():
        raise RuntimeError("migration failed")
    with migration_lock(timeout=0.6):
        pass


def test_the_lock_is_the_databases(monkeypatch: pytest.MonkeyPatch):
    """PostgreSQL: an advisory lock, so workers sharing the database but not a data folder exclude each other too"""
    if not is_postgres():
        pytest.skip("SQLite uses a file lock")

    engine = sa.create_engine(get_app_settings().DB_URL, poolclass=sa.pool.NullPool)
    try:
        with migration_lock(), engine.connect() as other:
            assert not other.scalar(sa.text("SELECT pg_try_advisory_lock(:key)"), {"key": MIGRATION_LOCK_ID})
        with engine.connect() as other:
            assert other.scalar(sa.text("SELECT pg_try_advisory_lock(:key)"), {"key": MIGRATION_LOCK_ID})
            other.execute(sa.text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK_ID})
    finally:
        engine.dispose()


@pytest.mark.skipif(lock_module.fcntl is None, reason="needs fcntl")
def test_the_lock_follows_the_database_alembic_migrates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """
    A SQLite `DB_URL` gets the file lock whatever `DB_ENGINE` says (the legacy-database tests point `DB_PROVIDER` at a
    SQLite file while the suite runs on PostgreSQL), and a PostgreSQL one the advisory lock
    """
    settings = get_app_settings()
    monkeypatch.setattr(settings, "DB_ENGINE", "postgres")
    monkeypatch.setattr(settings, "DB_PROVIDER", SQLiteProvider(data_dir=tmp_path))
    advisory: list[str] = []
    monkeypatch.setattr(lock_module, "_advisory_lock", lambda url, timeout: advisory.append(url))

    with migration_lock():
        fd = os.open(lock_module.lock_path(), os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                lock_module.fcntl.flock(fd, lock_module.fcntl.LOCK_EX | lock_module.fcntl.LOCK_NB)
        finally:
            os.close(fd)
    assert advisory == []

    monkeypatch.setattr(settings, "DB_ENGINE", "sqlite")
    monkeypatch.setattr(settings, "DB_PROVIDER", PostgresProvider(POSTGRES_SERVER="db.invalid", POSTGRES_PORT="5432"))

    @contextmanager
    def recorded(url: str, timeout: float) -> Iterator[None]:
        advisory.append(url)
        yield

    monkeypatch.setattr(lock_module, "_advisory_lock", recorded)
    with migration_lock():
        pass
    assert [sa.make_url(url).host for url in advisory] == ["db.invalid"]


@pytest.mark.skipif(lock_module.fcntl is None, reason="needs fcntl")
def test_without_file_lock_support_nothing_is_locked(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    if is_postgres():
        pytest.skip("PostgreSQL uses an advisory lock")

    def unsupported(fd: int, operation: int) -> None:
        raise OSError(lock_module.errno.ENOLCK, "No locks available")

    monkeypatch.setattr(lock_module.fcntl, "flock", unsupported)
    with migration_lock(), migration_lock(timeout=0.1):
        pass
    assert "aren't supported" in caplog.text


def _new_database(tmp_path: Path) -> tuple[dict[str, str], str | None]:
    """The environment for a Mealie process with an empty database of its own, and that database's name (PostgreSQL)"""
    env = {
        **os.environ,
        "PRODUCTION": "True",
        "TESTING": "False",
        "DATA_DIR": str(tmp_path),
        "LOG_LEVEL": "info",
    }
    if not is_postgres():
        env["DB_ENGINE"] = "sqlite"
        return env, None

    provider = PostgresProvider()
    name = f"mealie_migrate_race_{uuid.uuid4().hex[:12]}"
    engine = sa.create_engine(provider.db_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
    finally:
        engine.dispose()
    env["DB_ENGINE"] = "postgres"
    env["POSTGRES_DB"] = name
    return env, name


def _drop_database(name: str) -> None:
    engine = sa.create_engine(PostgresProvider().db_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        engine.dispose()


def _counts(env: dict[str, str]) -> tuple[int, int, int]:
    """Groups, users and Alembic revisions in the database `env` points at"""
    if env["DB_ENGINE"] == "sqlite":
        url = f"sqlite:///{Path(env['DATA_DIR']) / 'mealie.db'}"
    else:
        provider = PostgresProvider(POSTGRES_DB=env["POSTGRES_DB"])
        url = provider.db_url
    engine = sa.create_engine(url)
    try:
        with engine.connect() as connection:
            groups = connection.scalar(sa.text("SELECT COUNT(*) FROM groups"))
            users = connection.scalar(sa.text("SELECT COUNT(*) FROM users"))
            revisions = connection.scalar(sa.text("SELECT COUNT(*) FROM alembic_version"))
    finally:
        engine.dispose()
    return groups or 0, users or 0, revisions or 0


def test_two_workers_start_together_on_an_empty_database(tmp_path: Path):
    """Without the lock, one of them failed: "table groups already exists" (SQLite), a unique violation (PostgreSQL)"""
    env, database = _new_database(tmp_path)
    try:
        env["START_AT"] = str(time.time() + 15)  # both are importing Mealie by then, and start migrating together
        workers = [
            subprocess.Popen(
                [sys.executable, "-c", START_WORKER],
                cwd=REPO_ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            for _ in range(2)
        ]
        outputs = [worker.communicate(timeout=300)[0] for worker in workers]

        for worker, output in zip(workers, outputs, strict=True):
            assert worker.returncode == 0, output
        assert sum("Migration needed. Performing migration" in output for output in outputs) == 1, outputs
        assert sum("Database contains no users, initializing" in output for output in outputs) == 1, outputs
        assert _counts(env) == (1, 1, 1)
    finally:
        if database:
            _drop_database(database)
