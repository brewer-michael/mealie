"""Upgrades and downgrades 970cf50b85f4 (the MCP server's OAuth tables and API token grants) on a scratch DB"""

import shutil
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from mealie.core.config import get_app_settings
from mealie.core.settings.db_providers import SQLiteProvider
from mealie.db.init_db import ALEMBIC_DIR

REVISION = "970cf50b85f4"
DOWN_REVISION = "7f3d2a91c6e8"
TABLES = {"mcp_oauth_clients", "mcp_oauth_requests", "mcp_oauth_codes", "mcp_oauth_tokens", "mcp_api_token_grants"}

GROUP_ID = "8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e"
CLIENT_ID = "9c0d1e2f3a4b4c5d6e7f8a9b0c1d2e3f"


def _alembic_cfg() -> Config:
    return Config(str(ALEMBIC_DIR / "alembic.ini"))


def _target(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    """Points alembic's env.py at `<data_dir>/mealie.db` instead of the test database"""
    monkeypatch.setattr(get_app_settings(), "DB_PROVIDER", SQLiteProvider(data_dir=data_dir))


@contextmanager
def _connect(db: Path) -> Generator[sa.Connection]:
    engine = sa.create_engine(f"sqlite:///{db}")
    try:
        with engine.begin() as conn:
            yield conn
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def previous_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A database at the revision before this one"""
    data_dir = tmp_path_factory.mktemp("mcp_oauth_migration")
    with pytest.MonkeyPatch.context() as mp:
        _target(mp, data_dir)
        command.upgrade(_alembic_cfg(), DOWN_REVISION)

    return data_dir / "mealie.db"


@pytest.fixture()
def db(previous_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "mealie.db"
    shutil.copy(previous_db, db)
    _target(monkeypatch, tmp_path)

    with _connect(db) as conn:
        conn.execute(
            sa.text("INSERT INTO groups (id, name, slug) VALUES (:id, 'MCP Group', 'mcp-group')"), {"id": GROUP_ID}
        )

    return db


def test_upgrade_creates_the_tables(db: Path):
    command.upgrade(_alembic_cfg(), REVISION)

    with _connect(db) as conn:
        inspector = sa.inspect(conn)
        assert TABLES <= set(inspector.get_table_names())

        assert {c["name"] for c in inspector.get_columns("mcp_oauth_tokens")} >= {
            "token_hash",
            "kind",
            "oauth_client_id",
            "user_id",
            "family_id",
            "scopes",
            "resource",
            "expires_at",
            "granted_at",
            "revoked_at",
            "last_used_at",
        }
        unique_indexes = {
            index["name"] for table in TABLES for index in inspector.get_indexes(table) if index["unique"]
        }
        assert unique_indexes == {
            "ix_mcp_oauth_clients_client_id",
            "ix_mcp_oauth_requests_handle_hash",
            "ix_mcp_oauth_codes_code_hash",
            "ix_mcp_oauth_tokens_token_hash",
            "ix_mcp_api_token_grants_long_live_token_id",
        }
        assert {fk["referred_table"] for fk in inspector.get_foreign_keys("mcp_oauth_tokens")} == {
            "mcp_oauth_clients",
            "users",
        }
        assert {fk["referred_table"] for fk in inspector.get_foreign_keys("mcp_api_token_grants")} == {
            "long_live_tokens"
        }

        conn.execute(
            sa.text(
                "INSERT INTO mcp_oauth_clients (id, group_id, name, client_id, is_confidential, pkce_optional, "
                "allow_write_scope, redirect_uris) VALUES (:id, :group_id, 'HA', 'mmcp_x', 1, 1, 0, :uris)"
            ),
            {"id": CLIENT_ID, "group_id": GROUP_ID, "uris": '["https://my.home-assistant.io/redirect/oauth"]'},
        )
        assert conn.execute(sa.text("SELECT redirect_uris FROM mcp_oauth_clients")).scalar_one() == (
            '["https://my.home-assistant.io/redirect/oauth"]'
        )


def test_downgrade_drops_the_tables(db: Path):
    cfg = _alembic_cfg()
    command.upgrade(cfg, REVISION)
    with _connect(db) as conn:
        conn.execute(
            sa.text(
                "INSERT INTO mcp_oauth_clients (id, group_id, name, client_id, is_confidential, pkce_optional, "
                "allow_write_scope, redirect_uris) VALUES (:id, :group_id, 'HA', 'mmcp_x', 1, 1, 0, '[]')"
            ),
            {"id": CLIENT_ID, "group_id": GROUP_ID},
        )

    command.downgrade(cfg, DOWN_REVISION)

    with _connect(db) as conn:
        assert not TABLES & set(sa.inspect(conn).get_table_names())
        assert conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one() == DOWN_REVISION

    # and back again
    command.upgrade(cfg, REVISION)
    with _connect(db) as conn:
        assert TABLES <= set(sa.inspect(conn).get_table_names())
        assert conn.execute(sa.text("SELECT count(*) FROM mcp_oauth_clients")).scalar_one() == 0
