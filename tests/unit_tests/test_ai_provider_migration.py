"""Upgrades and downgrades 7f3d2a91c6e8 (AI provider routes, usage log, encrypted API keys) on a scratch DB"""

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
from mealie.db.models._model_utils.encrypted import ENCRYPTED_PREFIX, decrypt_value, encrypt_value

REVISION = "7f3d2a91c6e8"
DOWN_REVISION = "3527efeeec34"

GROUP_ID = "0f6a1c2b3d4e4f5a8b9c0d1e2f3a4b5c"
SETTINGS_ID = "1a2b3c4d5e6f4a7b8c9d0e1f2a3b4c5d"
PLAINTEXT_ID = "2b3c4d5e6f7a4b8c9d0e1f2a3b4c5d6e"
EMPTY_ID = "3c4d5e6f7a8b4c9d0e1f2a3b4c5d6e7f"
ENCRYPTED_ID = "4d5e6f7a8b9c4d0e1f2a3b4c5d6e7f8a"


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


def _api_keys(db: Path) -> dict[str, str]:
    with _connect(db) as conn:
        return dict(conn.execute(sa.text("SELECT id, api_key FROM ai_providers")).tuples().all())


@pytest.fixture(scope="module")
def upstream_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A database at the revision before this one"""
    data_dir = tmp_path_factory.mktemp("ai_provider_migration")
    with pytest.MonkeyPatch.context() as mp:
        _target(mp, data_dir)
        command.upgrade(_alembic_cfg(), DOWN_REVISION)

    return data_dir / "mealie.db"


@pytest.fixture()
def db(upstream_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A copy of `upstream_db` with providers, with alembic pointed at it"""
    db = tmp_path / "mealie.db"
    shutil.copy(upstream_db, db)
    _target(monkeypatch, tmp_path)

    with _connect(db) as conn:
        conn.execute(
            sa.text("INSERT INTO groups (id, name, slug) VALUES (:id, 'Migration Group', 'migration-group')"),
            {"id": GROUP_ID},
        )
        conn.execute(
            sa.text("INSERT INTO ai_provider_settings (id, group_id) VALUES (:id, :group_id)"),
            {"id": SETTINGS_ID, "group_id": GROUP_ID},
        )
        for provider_id, name, api_key in [
            (PLAINTEXT_ID, "plaintext", "sk-plaintext-key"),
            (EMPTY_ID, "empty", ""),
            # e.g. a database restored from a newer build, then migrated
            (ENCRYPTED_ID, "encrypted", encrypt_value("sk-already-encrypted")),
        ]:
            conn.execute(
                sa.text(
                    "INSERT INTO ai_providers (id, settings_id, name, api_key, model, timeout) "
                    "VALUES (:id, :settings_id, :name, :api_key, 'gpt-4o', 300)"
                ),
                {"id": provider_id, "settings_id": SETTINGS_ID, "name": name, "api_key": api_key},
            )

    return db


def test_upgrade_encrypts_existing_keys(db: Path):
    before = _api_keys(db)

    command.upgrade(_alembic_cfg(), REVISION)

    after = _api_keys(db)
    assert after[PLAINTEXT_ID].startswith(ENCRYPTED_PREFIX)
    assert "sk-plaintext-key" not in after[PLAINTEXT_ID]
    assert decrypt_value(after[PLAINTEXT_ID]) == "sk-plaintext-key"

    assert after[EMPTY_ID].startswith(ENCRYPTED_PREFIX)
    assert decrypt_value(after[EMPTY_ID]) == ""

    # never encrypted twice
    assert after[ENCRYPTED_ID] == before[ENCRYPTED_ID]
    assert decrypt_value(after[ENCRYPTED_ID]) == "sk-already-encrypted"


def test_upgrade_adds_columns_and_tables(db: Path):
    command.upgrade(_alembic_cfg(), REVISION)

    with _connect(db) as conn:
        rows = conn.execute(sa.text("SELECT protocol, monthly_token_limit FROM ai_providers")).all()
        assert rows and all(row == ("openai", None) for row in rows)

        inspector = sa.inspect(conn)
        assert {c["name"] for c in inspector.get_unique_constraints("ai_provider_routes")} == {
            "ai_provider_routes_settings_id_slot_position_key",
            "ai_provider_routes_settings_id_slot_provider_id_key",
        }
        assert {i["name"] for i in inspector.get_indexes("ai_provider_routes")} >= {
            "ix_ai_provider_routes_settings_id",
            "ix_ai_provider_routes_provider_id",
        }
        assert {i["name"] for i in inspector.get_indexes("ai_usage_log")} >= {
            "ix_ai_usage_log_group_id",
            "ix_ai_usage_log_provider_id",
            "ix_ai_usage_log_created_at",
        }

        # New rows get the server default protocol
        conn.execute(
            sa.text(
                "INSERT INTO ai_providers (id, settings_id, name, api_key, model, timeout) "
                "VALUES ('5e6f7a8b9c0d4e1f2a3b4c5d6e7f8a9b', :settings_id, 'new', 'k', 'gpt-4o', 300)"
            ),
            {"settings_id": SETTINGS_ID},
        )
        protocol = conn.execute(
            sa.text("SELECT protocol FROM ai_providers WHERE id = '5e6f7a8b9c0d4e1f2a3b4c5d6e7f8a9b'")
        ).scalar_one()
        assert protocol == "openai"


def test_downgrade_decrypts_keys_and_drops_additions(db: Path):
    cfg = _alembic_cfg()
    command.upgrade(cfg, REVISION)

    with _connect(db) as conn:
        conn.execute(
            sa.text(
                "INSERT INTO ai_provider_routes (id, settings_id, slot, position, provider_id) "
                "VALUES ('6f7a8b9c0d1e4f2a3b4c5d6e7f8a9b0c', :settings_id, 'default', 0, :provider_id)"
            ),
            {"settings_id": SETTINGS_ID, "provider_id": PLAINTEXT_ID},
        )
        conn.execute(
            sa.text(
                "INSERT INTO ai_usage_log (id, group_id, provider_id, provider_name, model, protocol, slot, "
                "prompt_tokens, completion_tokens, latency_ms, success) VALUES "
                "('7a8b9c0d1e2f4a3b4c5d6e7f8a9b0c1d', :group_id, :provider_id, 'plaintext', 'gpt-4o', 'openai', "
                "'default', 10, 20, 300, 1)"
            ),
            {"group_id": GROUP_ID, "provider_id": PLAINTEXT_ID},
        )

    command.downgrade(cfg, DOWN_REVISION)

    assert _api_keys(db) == {
        PLAINTEXT_ID: "sk-plaintext-key",
        EMPTY_ID: "",
        ENCRYPTED_ID: "sk-already-encrypted",
    }
    with _connect(db) as conn:
        inspector = sa.inspect(conn)
        assert not inspector.has_table("ai_provider_routes")
        assert not inspector.has_table("ai_usage_log")
        columns = {c["name"] for c in inspector.get_columns("ai_providers")}
        assert "protocol" not in columns
        assert "monthly_token_limit" not in columns


def test_downgrade_keeps_keys_it_cannot_decrypt(db: Path):
    cfg = _alembic_cfg()
    command.upgrade(cfg, REVISION)

    foreign = encrypt_value("sk-other-secret", "some-other-secret")
    with _connect(db) as conn:
        conn.execute(sa.text("UPDATE ai_providers SET api_key = :key WHERE id = :id"), {"key": foreign, "id": EMPTY_ID})

    command.downgrade(cfg, DOWN_REVISION)

    keys = _api_keys(db)
    assert keys[EMPTY_ID] == foreign
    assert keys[PLAINTEXT_ID] == "sk-plaintext-key"


def test_upgrade_after_downgrade(db: Path):
    cfg = _alembic_cfg()
    command.upgrade(cfg, REVISION)
    command.downgrade(cfg, DOWN_REVISION)
    command.upgrade(cfg, REVISION)

    assert {key: decrypt_value(value) for key, value in _api_keys(db).items()} == {
        PLAINTEXT_ID: "sk-plaintext-key",
        EMPTY_ID: "",
        ENCRYPTED_ID: "sk-already-encrypted",
    }
