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
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from mealie.core.config import get_app_settings
from mealie.core.settings.db_providers import PostgresProvider, SQLiteProvider
from mealie.core.settings.settings import determine_secrets
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


@contextmanager
def held_elsewhere() -> Iterator[None]:
    """The lock held by another thread, as another process would hold it: the holding thread itself takes it freely"""
    acquired, release = threading.Event(), threading.Event()

    def hold() -> None:
        with migration_lock():
            acquired.set()
            release.wait(60)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert acquired.wait(30)
        yield
    finally:
        release.set()
        holder.join(30)


def test_a_second_holder_waits_and_gives_up_at_the_timeout():
    with held_elsewhere():
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


class _StopWaiting(Exception):
    pass


def test_a_waiting_process_never_gives_up_while_the_holder_migrates(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """
    A migration can outlast any deadline (a large database on a Raspberry Pi), and the lock goes when its holder
    dies, so a waiting worker waits for as long as the holder migrates, saying so every minute. Giving up failed the
    worker's startup, and uvicorn then stopped every worker, the migrating one included.
    """
    clock = SimpleNamespace(now=1000.0, polls=0)

    def sleep(seconds: float) -> None:
        clock.polls += 1
        clock.now += 30  # half a minute a poll
        if clock.polls == 240:  # two hours on
            raise _StopWaiting

    with held_elsewhere():
        monkeypatch.setattr(lock_module, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
        with caplog.at_level("INFO"), pytest.raises(_StopWaiting), migration_lock():
            pytest.fail("took a lock another holder has")

    messages = [record.getMessage() for record in caplog.records]
    assert sum("waiting for it to finish" in message for message in messages) == 1
    progress = [message for message in messages if "Still waiting" in message]
    assert len(progress) == 119  # every minute after the first
    assert "(1 min" in progress[0]
    assert "(119 min" in progress[-1]


def test_the_holding_thread_takes_it_again_without_waiting():
    """A backup restore holds it around its own `init_db.main`, which would otherwise wait for the restore forever"""
    others: list[str] = []

    def another_thread_tries() -> None:
        try:
            with migration_lock(timeout=0.3):
                others.append("took it")
        except MigrationLockTimeout:
            others.append("waited")

    with migration_lock():
        with migration_lock(timeout=0.1), migration_lock(timeout=0.1):  # waiting would time out
            pass
        # still held once the nested blocks end: another thread (or process) still waits
        trying = threading.Thread(target=another_thread_tries)
        trying.start()
        trying.join(10)
    assert others == ["waited"]

    trying = threading.Thread(target=another_thread_tries)
    trying.start()
    trying.join(10)
    assert others == ["waited", "took it"]


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


def _holders() -> list[tuple[int, str]]:
    """The PostgreSQL backends holding the migration lock, and their state"""
    engine = sa.create_engine(get_app_settings().DB_URL, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                sa.text(
                    "SELECT l.pid, a.state FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                    "WHERE l.locktype = 'advisory' AND l.granted AND l.database = "
                    "(SELECT oid FROM pg_database WHERE datname = current_database()) "
                    "AND l.classid = :high AND l.objid = :low AND l.objsubid = 1"
                ),
                {"high": MIGRATION_LOCK_ID >> 32, "low": MIGRATION_LOCK_ID & 0xFFFFFFFF},
            ).all()
    finally:
        engine.dispose()
    return [(row[0], row[1]) for row in rows]


def test_the_lock_is_held_by_an_open_transaction():
    """
    PostgreSQL: a transaction's advisory lock, held by keeping that transaction open, works behind PgBouncer in
    transaction pooling mode, which keeps a client on one server connection only while it's in a transaction. A
    session's lock taken and released in separate statements could land on different server connections there: two
    workers both held it, and the lock outlived every Mealie process on a pooled connection.
    """
    if not is_postgres():
        pytest.skip("SQLite uses a file lock")

    with migration_lock():
        [(_, state)] = _holders()
        assert state == "idle in transaction"
    assert _holders() == []


def test_a_server_timeout_for_idle_transactions_doesnt_release_it(monkeypatch: pytest.MonkeyPatch):
    """
    PostgreSQL: the lock's transaction sits idle while the migration runs on other connections, so a database set to
    end idle transactions (`idle_in_transaction_session_timeout`) would end it mid-migration, and let another worker
    migrate too. The lock's transaction turns that off for itself.
    """
    if not is_postgres():
        pytest.skip("SQLite uses a file lock")

    url = sa.make_url(get_app_settings().DB_URL).update_query_dict(
        {"options": "-c idle_in_transaction_session_timeout=200"}
    )
    monkeypatch.setattr(
        lock_module, "get_app_settings", lambda: SimpleNamespace(DB_URL=url.render_as_string(hide_password=False))
    )
    with held_elsewhere():
        [(holder, _)] = _holders()
        time.sleep(0.8)
        assert _holders() == [(holder, "idle in transaction")]
        with pytest.raises(MigrationLockTimeout):
            with migration_lock(timeout=0.3):
                pytest.fail("took a lock another holder has")
    assert _holders() == []


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
    # as uvicorn's parent does before it starts the workers (mealie/main.py imports the settings): two processes
    # creating the secrets at once is another race, of upstream's, that only bare processes like these meet
    for secret in (".secret", ".session_secret"):
        determine_secrets(tmp_path, secret, production=True)
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
