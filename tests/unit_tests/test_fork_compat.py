import copy
import json
import shutil
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError
from sqlalchemy.orm import Session

from mealie.core.config import get_app_settings
from mealie.core.settings.db_providers import SQLiteProvider
from mealie.db import init_db
from mealie.db.db_setup import sql_global_init
from mealie.db.fork_compat import (
    LEGACY_DOWN_REVISION,
    LEGACY_REVISION,
    LEGACY_TABLE,
    fix_legacy_fork_backup,
    fix_legacy_fork_revision,
)
from mealie.db.init_db import ALEMBIC_DIR, db_is_at_head
from mealie.services.backups_v2.alchemy_exporter import AlchemyExporter

LEGACY_GROUP = {"id": "6e0b4b9c2f6d4a8e9d3b1c5a7f2e4d6b", "name": "Legacy Group", "slug": "legacy-group"}


def _alembic_cfg() -> Config:
    return Config(str(ALEMBIC_DIR / "alembic.ini"))


def _target(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    """Points alembic's env.py (and `db_is_at_head`) at `<data_dir>/mealie.db` instead of the test database"""
    monkeypatch.setattr(get_app_settings(), "DB_PROVIDER", SQLiteProvider(data_dir=data_dir))


@contextmanager
def _connect(db: Path) -> Generator[sa.Connection]:
    engine = sa.create_engine(f"sqlite:///{db}")
    try:
        with engine.begin() as conn:
            yield conn
    finally:
        engine.dispose()


def _fix(db: Path) -> bool:
    engine = sa.create_engine(f"sqlite:///{db}")
    try:
        with Session(engine) as session:
            return fix_legacy_fork_revision(session)
    finally:
        engine.dispose()


@contextmanager
def _init_db_on(db: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    """Points `init_db.main()`'s session at `db` instead of the test database"""
    session_maker, engine = sql_global_init(f"sqlite:///{db}")

    @contextmanager
    def session_context() -> Generator[Session]:
        with session_maker() as session:
            yield session

    monkeypatch.setattr(init_db, "session_context", session_context)
    try:
        yield
    finally:
        engine.dispose()


def _versions(db: Path) -> list[str]:
    with _connect(db) as conn:
        return list(conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalars())


def _has_table(db: Path, table: str) -> bool:
    with _connect(db) as conn:
        return sa.inspect(conn).has_table(table)


@pytest.fixture(scope="module")
def upstream_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A database migrated to the upstream revision the old fork build branched from"""
    data_dir = tmp_path_factory.mktemp("fork_compat_upstream")
    with pytest.MonkeyPatch.context() as mp:
        _target(mp, data_dir)
        command.upgrade(_alembic_cfg(), LEGACY_DOWN_REVISION)

    return data_dir / "mealie.db"


@pytest.fixture()
def legacy_db(upstream_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A database as the old fork build left it, with alembic pointed at it"""
    db = tmp_path / "mealie.db"
    shutil.copy(upstream_db, db)
    _target(monkeypatch, tmp_path)

    # Mirrors the fork's `add_admin_settings` migration
    with _connect(db) as conn:
        sa.Table(
            LEGACY_TABLE,
            sa.MetaData(),
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("image_scanning_enable_ocr_fallback", sa.Boolean(), nullable=False),
            sa.Column("openai_api_key", sa.Text(), nullable=True),
        ).create(conn)
        conn.execute(
            sa.text(f"INSERT INTO {LEGACY_TABLE} (image_scanning_enable_ocr_fallback) VALUES (:enabled)"),
            {"enabled": True},
        )
        conn.execute(sa.text("UPDATE alembic_version SET version_num = :rev"), {"rev": LEGACY_REVISION})

    return db


def test_startup_upgrades_legacy_database(legacy_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _alembic_cfg()
    with pytest.raises(CommandError, match=LEGACY_REVISION):
        command.upgrade(cfg, "head")

    # Runs the real startup path (fix, upgrade, seeding) against the legacy database
    with _init_db_on(legacy_db, monkeypatch):
        init_db.main()

    assert db_is_at_head(cfg)
    assert not _has_table(legacy_db, LEGACY_TABLE)


def test_restore_legacy_backup(legacy_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _connect(legacy_db) as conn:
        conn.execute(sa.text("INSERT INTO groups (id, name, slug) VALUES (:id, :name, :slug)"), LEGACY_GROUP)

    # What the old build wrote to a backup's database.json
    exporter = AlchemyExporter(f"sqlite:///{legacy_db}")
    try:
        db_dump = json.loads(json.dumps(exporter.dump()))
    finally:
        exporter.engine.dispose()
    assert db_dump["alembic_version"] == [{"version_num": LEGACY_REVISION}]
    assert db_dump[LEGACY_TABLE]

    # Restore into an empty database, as `BackupV2.restore` does after dropping every table
    restored_db = tmp_path / "restored" / "mealie.db"
    restored_db.parent.mkdir()
    _target(monkeypatch, restored_db.parent)
    with _init_db_on(restored_db, monkeypatch):
        AlchemyExporter(f"sqlite:///{restored_db}").restore(db_dump)

    assert db_is_at_head(_alembic_cfg())
    assert not _has_table(restored_db, LEGACY_TABLE)
    with _connect(restored_db) as conn:
        names = conn.execute(sa.text("SELECT name FROM groups WHERE slug = :slug"), LEGACY_GROUP).scalars().all()
    assert names == [LEGACY_GROUP["name"]]


def test_fix_is_idempotent(legacy_db: Path) -> None:
    assert _fix(legacy_db)
    assert not _fix(legacy_db)

    assert _versions(legacy_db) == [LEGACY_DOWN_REVISION]
    assert not _has_table(legacy_db, LEGACY_TABLE)


def test_fix_is_atomic(legacy_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Failing the commit must roll back both statements; on SQLite that only holds if the DROP
    # runs inside the transaction the UPDATE opened
    def failing_commit(self: Session) -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(Session, "commit", failing_commit)
    with pytest.raises(RuntimeError):
        _fix(legacy_db)

    assert _versions(legacy_db) == [LEGACY_REVISION]
    assert _has_table(legacy_db, LEGACY_TABLE)


def test_fix_is_noop_on_fresh_database(tmp_path: Path) -> None:
    db = tmp_path / "mealie.db"

    assert not _fix(db)
    with _connect(db) as conn:
        assert sa.inspect(conn).get_table_names() == []


def test_fix_is_noop_on_upstream_database(upstream_db: Path, tmp_path: Path) -> None:
    db = tmp_path / "mealie.db"
    shutil.copy(upstream_db, db)
    # The fix keys off the revision, never off a table that happens to share the name
    with _connect(db) as conn:
        conn.execute(sa.text(f"CREATE TABLE {LEGACY_TABLE} (id INTEGER PRIMARY KEY)"))

    assert not _fix(db)
    assert _versions(db) == [LEGACY_DOWN_REVISION]
    assert _has_table(db, LEGACY_TABLE)


def test_fix_is_noop_on_app_database(session: Session) -> None:
    """Runs against the suite's own database, so it also covers PostgreSQL when DB_ENGINE=postgres"""
    before = session.execute(sa.text("SELECT version_num FROM alembic_version")).scalars().all()

    assert not fix_legacy_fork_revision(session)
    assert session.execute(sa.text("SELECT version_num FROM alembic_version")).scalars().all() == before


def test_backup_fix_is_idempotent() -> None:
    groups = [{"id": LEGACY_GROUP["id"], "name": LEGACY_GROUP["name"]}]
    db_dump: dict[str, list[dict]] = {
        "alembic_version": [{"version_num": LEGACY_REVISION}],
        LEGACY_TABLE: [{"id": 1, "image_scanning_enable_ocr_fallback": True}],
        "groups": copy.deepcopy(groups),
    }

    assert fix_legacy_fork_backup(db_dump)
    assert not fix_legacy_fork_backup(db_dump)

    assert db_dump == {"alembic_version": [{"version_num": LEGACY_DOWN_REVISION}], "groups": groups}


def test_backup_fix_is_noop_on_upstream_backup() -> None:
    # Like the database fix, it keys off the revision, never off a table that happens to share the name
    db_dump: dict[str, list[dict]] = {
        "alembic_version": [{"version_num": LEGACY_DOWN_REVISION}],
        LEGACY_TABLE: [{"id": 1}],
    }
    before = copy.deepcopy(db_dump)

    assert not fix_legacy_fork_backup(db_dump)
    assert db_dump == before


def test_backup_fix_is_noop_on_app_backup() -> None:
    """Dumps the suite's own database, so it also covers PostgreSQL when DB_ENGINE=postgres"""
    exporter = AlchemyExporter(get_app_settings().DB_URL)
    try:
        db_dump = exporter.dump()
    finally:
        exporter.engine.dispose()
    before = copy.deepcopy(db_dump)

    assert not fix_legacy_fork_backup(db_dump)
    assert db_dump == before
