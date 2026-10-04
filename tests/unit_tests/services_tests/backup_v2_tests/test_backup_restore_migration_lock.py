"""
Fork: a backup restore holds the database's migration lock from before it drops the tables until the restored database
is migrated and seeded (backup_v2.holds_migrations, docs/ai/PHASE2.md §17). A process starting meanwhile (a uvicorn
worker restarted mid-restore, a second container) used to take the free lock and migrate and seed the dropped
database while the restore rebuilt it: the restore failed and only the default admin was left, or `alembic_version`
ended up with two rows and Mealie wouldn't start again. Runs on the suite's engine: SQLite, or PostgreSQL in a
database of its own.
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
from fastapi.testclient import TestClient

from mealie.core.config import get_app_settings
from mealie.core.settings.db_providers import PostgresProvider
from mealie.core.settings.settings import determine_secrets
from mealie.db.migration_lock import migration_lock
from mealie.services.ai.ingest import storage
from mealie.services.backups_v2 import backup_v2
from mealie.services.backups_v2.alchemy_exporter import AlchemyExporter
from mealie.services.backups_v2.backup_v2 import BackupV2, MigrationBusyError
from tests.utils import api_routes

REPO_ROOT = Path(__file__).parents[4]

SETUP = textwrap.dedent(
    """
    from mealie.db import init_db
    from mealie.db.db_setup import session_context
    from mealie.repos.all_repositories import get_repositories
    from mealie.services.backups_v2.backup_v2 import BackupV2

    def rename(name, email):
        with session_context() as session:
            repos = get_repositories(session, group_id=None, household_id=None)
            [user] = repos.users.get_all()
            repos.users.patch(user.id, {"full_name": name, "email": email})

    init_db.main()
    rename("Restored Admin", "restored@example.com")
    print("BACKUP", BackupV2().backup())
    rename("Changed Later", "later@example.com")  # the restore puts the backup's back
    """
)

RESTORE = textwrap.dedent(
    """
    import os, time
    from pathlib import Path

    from mealie.services.backups_v2.alchemy_exporter import AlchemyExporter
    from mealie.services.backups_v2.backup_v2 import BackupV2

    drop_all = AlchemyExporter.drop_all

    def drop_all_then_linger(self):
        drop_all(self)
        Path(os.environ["DROPPED"]).write_text("dropped")
        time.sleep(2)  # the other process starts while the tables are gone

    AlchemyExporter.drop_all = drop_all_then_linger
    while not Path(os.environ["READY"]).exists():  # the other process has imported Mealie
        time.sleep(0.01)
    BackupV2().restore(Path(os.environ["BACKUP"]))
    print("RESTORED", time.time())
    """
)

START = textwrap.dedent(
    """
    import os, time
    from pathlib import Path

    from mealie.db import init_db

    Path(os.environ["READY"]).write_text("ready")
    dropped = Path(os.environ["DROPPED"])
    while not dropped.exists():
        time.sleep(0.01)
    init_db.main()  # as a worker's lifespan does
    print("STARTED", time.time())
    """
)


def _is_postgres() -> bool:
    return get_app_settings().DB_ENGINE == "postgres"


@contextmanager
def _instance(tmp_path: Path) -> Iterator[dict[str, str]]:
    """The environment of a Mealie instance with a data folder and an empty database of its own"""
    env = {**os.environ, "PRODUCTION": "True", "TESTING": "False", "DATA_DIR": str(tmp_path), "LOG_LEVEL": "info"}
    for secret in (".secret", ".session_secret"):
        determine_secrets(tmp_path, secret, production=True)
    if not _is_postgres():
        env["DB_ENGINE"] = "sqlite"
        yield env
        return

    name = f"mealie_restore_race_{uuid.uuid4().hex[:12]}"
    admin = sa.create_engine(PostgresProvider().db_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
        env["DB_ENGINE"] = "postgres"
        env["POSTGRES_DB"] = name
        yield env
    finally:
        with admin.connect() as connection:
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _run(script: str, env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _finish(process: subprocess.Popen[str]) -> str:
    output = process.communicate(timeout=300)[0]
    assert process.returncode == 0, output
    return output


def _printed(output: str, key: str) -> str:
    return next(line.split(" ", 1)[1] for line in output.splitlines() if line.startswith(f"{key} "))


def _database(env: dict[str, str]) -> tuple[list[tuple[str, str]], int, list[str]]:
    """Users (email, name), groups and Alembic revisions of the database `env` points at"""
    if env["DB_ENGINE"] == "sqlite":
        url = f"sqlite:///{Path(env['DATA_DIR']) / 'mealie.db'}"
    else:
        url = PostgresProvider(POSTGRES_DB=env["POSTGRES_DB"]).db_url
    engine = sa.create_engine(url)
    try:
        with engine.connect() as connection:
            users = [(row[0], row[1]) for row in connection.execute(sa.text("SELECT email, full_name FROM users"))]
            groups = connection.scalar(sa.text("SELECT COUNT(*) FROM groups")) or 0
            revisions = [row[0] for row in connection.execute(sa.text("SELECT version_num FROM alembic_version"))]
    finally:
        engine.dispose()
    return users, groups, revisions


def test_a_process_starting_during_a_restore_waits_for_it(tmp_path: Path):
    with _instance(tmp_path) as env:
        backup = _printed(_finish(_run(SETUP, env)), "BACKUP")
        _, _, [head] = _database(env)

        env = {**env, "BACKUP": backup, "READY": str(tmp_path / "ready"), "DROPPED": str(tmp_path / "dropped")}
        starting = _run(START, env)  # imports Mealie, then waits for the tables to be dropped
        restoring = _run(RESTORE, env)
        restored = _finish(restoring)
        started = _finish(starting)

        assert float(_printed(started, "STARTED")) >= float(_printed(restored, "RESTORED")), (restored, started)
        assert "Another Mealie process is migrating the database; waiting" in started, started
        assert "Migration needed" not in started, started  # it found the restored database at head
        assert "Database contains no users, initializing" not in started, started  # and seeded nothing
        assert _database(env) == ([("restored@example.com", "Restored Admin")], 1, [head])


@contextmanager
def _held_elsewhere() -> Iterator[None]:
    """The migration lock held by another thread, as a worker migrating at startup would hold it"""
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


def test_a_restore_gives_up_on_another_migration_before_changing_anything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(backup_v2, "MIGRATION_LOCK_WAIT", 0.3)
    changed: list[str] = []
    monkeypatch.setattr(BackupV2, "_sqlite", lambda self: changed.append("safety copy"))
    monkeypatch.setattr(BackupV2, "_postgres", lambda self: changed.append("safety copy"))
    monkeypatch.setattr(AlchemyExporter, "drop_all", lambda self: changed.append("dropped"))
    backup = BackupV2(get_app_settings().DB_URL)
    try:
        with _held_elsewhere():
            started = time.monotonic()
            with pytest.raises(MigrationBusyError):
                backup.restore(tmp_path / "never-read.zip")
            assert time.monotonic() - started < 10
    finally:
        backup.db_exporter.engine.dispose()

    assert changed == []
    assert not storage.pause_marker_path().exists()  # nothing was paused either


def test_the_restore_route_says_to_try_again(
    api_client: TestClient, admin_token: dict, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(backup_v2, "MIGRATION_LOCK_WAIT", 0.3)
    with _held_elsewhere():
        response = api_client.post(api_routes.admin_backups_file_name_restore("never-read.zip"), headers=admin_token)
    assert response.status_code == 503
    assert response.json()["detail"]["message"] == "Mealie is updating its database. Try the restore again in a minute."
