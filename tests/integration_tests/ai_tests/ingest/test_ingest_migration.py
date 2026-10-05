"""
Upgrades and downgrades cc5357be7e71 (recipe card ingestion's tables, `ai_providers.runs_locally` and
`ai_usage_log.job_id`), 0c2bef734816 (notification delivery, automatic retry and `recipe_created` columns) and
0f77cc21b216 (a waiting card's lift backoff) on a scratch database of the engine the suite runs on: a SQLite file, or
a PostgreSQL database created for the test.
"""

import os
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from mealie.core.config import get_app_settings
from mealie.core.settings.db_providers import PostgresProvider, SQLiteProvider
from mealie.db.init_db import ALEMBIC_DIR
from mealie.db.models._model_utils.guid import GUID

REVISION = "cc5357be7e71"
DOWN_REVISION = "970cf50b85f4"
DELIVERY_REVISION = "0c2bef734816"
LIFT_REVISION = "0f77cc21b216"
TABLES = {
    "recipe_ingestion_batches",
    "recipe_ingestion_jobs",
    "recipe_ingestion_settings",
    "ai_event_notifier_options",
}
POSTGRES = os.environ.get("DB_ENGINE") == "postgres"

GROUP_ID, HOUSEHOLD_ID, NOTIFIER_ID = uuid4(), uuid4(), uuid4()
SETTINGS_ID, PROVIDER_ID, USAGE_ID = uuid4(), uuid4(), uuid4()


def _alembic_cfg() -> Config:
    return Config(str(ALEMBIC_DIR / "alembic.ini"))


@contextmanager
def _connect(url: str) -> Generator[sa.Connection]:
    engine = sa.create_engine(url)
    try:
        with engine.begin() as conn:
            yield conn
    finally:
        engine.dispose()


def _guid(conn: sa.Connection, value: UUID) -> Any:
    return GUID.convert_value_to_guid(value, conn.dialect)


@pytest.fixture()
def db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A scratch database at the revision before this one, with a group, a notifier, a provider and a usage row"""
    settings = get_app_settings()
    if POSTGRES:
        name = f"ingest_migration_{uuid4().hex[:12]}"
        admin = sa.create_engine(settings.DB_URL, isolation_level="AUTOCOMMIT")  # type: ignore[arg-type]
        with admin.connect() as conn:
            conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
        provider: Any = PostgresProvider(POSTGRES_DB=name)
    else:
        provider = SQLiteProvider(data_dir=tmp_path)

    monkeypatch.setattr(settings, "DB_PROVIDER", provider)
    url = provider.db_url
    try:
        command.upgrade(_alembic_cfg(), DOWN_REVISION)
        _seed(url)
        yield url
    finally:
        if POSTGRES:
            with admin.connect() as conn:
                conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            admin.dispose()


def _seed(url: str) -> None:
    with _connect(url) as conn:
        ids = {
            name: _guid(conn, value)
            for name, value in {
                "group": GROUP_ID,
                "household": HOUSEHOLD_ID,
                "notifier": NOTIFIER_ID,
                "settings": SETTINGS_ID,
                "provider": PROVIDER_ID,
                "usage": USAGE_ID,
            }.items()
        }
        conn.execute(sa.text("INSERT INTO groups (id, name, slug) VALUES (:group, 'Cards', 'cards')"), ids)
        conn.execute(
            sa.text(
                "INSERT INTO households (id, name, slug, group_id) VALUES (:household, 'Family', 'family', :group)"
            ),
            ids,
        )
        conn.execute(
            sa.text(
                "INSERT INTO group_events_notifiers (id, name, enabled, apprise_url, group_id, household_id) "
                "VALUES (:notifier, 'HA', :enabled, 'json://ha', :group, :household)"
            ),
            {**ids, "enabled": True},
        )
        conn.execute(sa.text("INSERT INTO ai_provider_settings (id, group_id) VALUES (:settings, :group)"), ids)
        conn.execute(
            sa.text(
                "INSERT INTO ai_providers (id, settings_id, name, api_key, model, timeout) "
                "VALUES (:provider, :settings, 'Ollama', 'k', 'qwen3-vl', 300)"
            ),
            ids,
        )
        conn.execute(
            sa.text(
                "INSERT INTO ai_usage_log (id, group_id, provider_id, provider_name, model, protocol, slot, "
                "prompt_tokens, completion_tokens, latency_ms, success) "
                "VALUES (:usage, :group, :provider, 'Ollama', 'qwen3-vl', 'openai', 'image', 1, 2, 3, :success)"
            ),
            {**ids, "success": True},
        )


def _insert_job(
    conn: sa.Connection,
    *,
    status: str = "processing",
    committed_at: datetime | None = None,
    job_id: UUID | None = None,
    with_settings: bool = True,
) -> None:
    batch_id, job_id = uuid4(), job_id or uuid4()
    values = {
        "batch": _guid(conn, batch_id),
        "job": _guid(conn, job_id),
        "group": _guid(conn, GROUP_ID),
        "household": _guid(conn, HOUSEHOLD_ID),
        "false": False,
    }
    conn.execute(
        sa.text(
            "INSERT INTO recipe_ingestion_batches (id, group_id, household_id, source) "
            "VALUES (:batch, :group, :household, 'app')"
        ),
        values,
    )
    conn.execute(
        sa.text(
            "INSERT INTO recipe_ingestion_jobs (id, group_id, household_id, batch_id, position, source, local_only, "
            "status, draft_version, extracted_version, row_version, error_count, warning_count, task_priority, "
            "attempts, rate_limit_retries, cancel_requested, pages, source_sha256, draft, committed_at) "
            "VALUES (:job, :group, :household, :batch, 0, 'app', :false, :status, 0, 0, 0, 0, 0, 10, 0, 0, "
            ":false, '[]', :sha, :draft, :committed_at)"
        ),
        {
            **values,
            "status": status,
            "committed_at": committed_at,
            "sha": "a" * 64,
            "draft": '{"name": "Banana Mug Cake"}',
        },
    )
    if not with_settings:
        return
    conn.execute(
        sa.text(
            "INSERT INTO recipe_ingestion_settings (id, group_id, local_only, cross_read) "
            "VALUES (:id, :group, :false, :false)"
        ),
        {**values, "id": _guid(conn, uuid4())},
    )
    conn.execute(
        sa.text(
            "INSERT INTO ai_event_notifier_options (id, notifier_id, recipe_ingestion_ready) "
            "VALUES (:id, :notifier, :true)"
        ),
        {"id": _guid(conn, uuid4()), "notifier": _guid(conn, NOTIFIER_ID), "true": True},
    )


def test_upgrade_creates_the_tables_and_columns(db_url: str):
    command.upgrade(_alembic_cfg(), REVISION)

    with _connect(db_url) as conn:
        inspector = sa.inspect(conn)
        assert TABLES <= set(inspector.get_table_names())

        job_columns = {column["name"] for column in inspector.get_columns("recipe_ingestion_jobs")}
        assert {
            "row_version",
            "draft_version",
            "extracted_version",
            "lease_token",
            "lease_expires_at",
            "task_payload",
            "pages",
            "source_sha256",
            "commit_recipe_id",
            "commit_asset_token",
        } <= job_columns
        job_indexes = {index["name"]: index["column_names"] for index in inspector.get_indexes("recipe_ingestion_jobs")}
        assert job_indexes["ix_recipe_ingestion_jobs_task_state_priority_created"] == [
            "task_state",
            "task_priority",
            "created_at",
        ]
        assert job_indexes["ix_recipe_ingestion_jobs_household_status_created"] == [
            "household_id",
            "status",
            "created_at",
        ]
        assert job_indexes["ix_recipe_ingestion_jobs_household_source_sha256"] == ["household_id", "source_sha256"]
        assert job_indexes["ix_recipe_ingestion_jobs_batch_position"] == ["batch_id", "position"]
        assert job_indexes["ix_recipe_ingestion_jobs_recipe_id"] == ["recipe_id"]
        assert {fk["referred_table"] for fk in inspector.get_foreign_keys("recipe_ingestion_jobs")} == {
            "groups",
            "households",
            "recipe_ingestion_batches",
        }
        unique = {index["name"] for table in TABLES for index in inspector.get_indexes(table) if index["unique"]}
        assert unique == {
            "ix_recipe_ingestion_settings_group_id",
            "ix_ai_event_notifier_options_notifier_id",
        }

        assert "job_id" in {column["name"] for column in inspector.get_columns("ai_usage_log")}
        assert "ix_ai_usage_log_job_id" in {index["name"] for index in inspector.get_indexes("ai_usage_log")}

        # an existing provider isn't local until a manager says so
        runs_locally = conn.execute(
            sa.text("SELECT runs_locally FROM ai_providers WHERE id = :id"), {"id": _guid(conn, PROVIDER_ID)}
        ).scalar_one()
        assert not runs_locally
        assert conn.execute(sa.text("SELECT job_id FROM ai_usage_log")).scalar_one() is None

        _insert_job(conn)
        assert conn.execute(sa.text("SELECT draft FROM recipe_ingestion_jobs")).scalar_one() == (
            '{"name": "Banana Mug Cake"}'
        )


def test_downgrade_removes_them_and_keeps_everything_else(db_url: str):
    cfg = _alembic_cfg()
    command.upgrade(cfg, REVISION)
    with _connect(db_url) as conn:
        _insert_job(conn)
        conn.execute(sa.text("UPDATE ai_usage_log SET job_id = :job"), {"job": _guid(conn, uuid4())})

    command.downgrade(cfg, DOWN_REVISION)

    with _connect(db_url) as conn:
        inspector = sa.inspect(conn)
        assert not TABLES & set(inspector.get_table_names())
        assert "runs_locally" not in {column["name"] for column in inspector.get_columns("ai_providers")}
        assert "job_id" not in {column["name"] for column in inspector.get_columns("ai_usage_log")}
        assert conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one() == DOWN_REVISION
        assert conn.execute(sa.text("SELECT name FROM ai_providers")).scalar_one() == "Ollama"
        assert conn.execute(sa.text("SELECT count(*) FROM ai_usage_log")).scalar_one() == 1
        assert conn.execute(sa.text("SELECT count(*) FROM group_events_notifiers")).scalar_one() == 1

    # and back again
    command.upgrade(cfg, REVISION)
    with _connect(db_url) as conn:
        assert TABLES <= set(sa.inspect(conn).get_table_names())
        assert conn.execute(sa.text("SELECT count(*) FROM recipe_ingestion_jobs")).scalar_one() == 0


# ==================================================================================================================
# 0c2bef734816: notification delivery, automatic retry and recipe_created


COMMITTED_ID, READY_ID = uuid4(), uuid4()
COMMITTED_AT = datetime(2026, 10, 3, 18, 30, tzinfo=UTC).replace(tzinfo=None)  # stored naive, in UTC


def _job_columns(conn: sa.Connection, job_id: UUID) -> dict[str, Any]:
    row = conn.execute(
        sa.text(
            "SELECT status, auto_retry_at, recipe_event_claimed_at, recipe_event_sent_at FROM recipe_ingestion_jobs "
            "WHERE id = :id"
        ),
        {"id": _guid(conn, job_id)},
    ).mappings()
    return dict(row.one())


def test_delivery_columns_upgrade_backfills_and_downgrades(db_url: str):
    cfg = _alembic_cfg()
    command.upgrade(cfg, REVISION)
    with _connect(db_url) as conn:
        _insert_job(conn, status="committed", committed_at=COMMITTED_AT, job_id=COMMITTED_ID)
        _insert_job(conn, status="ready", job_id=READY_ID, with_settings=False)

    command.upgrade(cfg, DELIVERY_REVISION)

    with _connect(db_url) as conn:
        inspector = sa.inspect(conn)
        batch_columns = {column["name"]: column for column in inspector.get_columns("recipe_ingestion_batches")}
        assert {"notify_claimed_at", "notify_attempts", "notify_delivered"} <= set(batch_columns)
        assert not batch_columns["notify_attempts"]["nullable"]
        job_columns = {column["name"] for column in inspector.get_columns("recipe_ingestion_jobs")}
        assert {"auto_retry_at", "recipe_event_claimed_at", "recipe_event_sent_at"} <= job_columns
        job_indexes = {index["name"]: index["column_names"] for index in inspector.get_indexes("recipe_ingestion_jobs")}
        assert job_indexes["ix_recipe_ingestion_jobs_status_auto_retry"] == ["status", "auto_retry_at"]
        assert job_indexes["ix_recipe_ingestion_jobs_status_event_sent"] == ["status", "recipe_event_sent_at"]

        # existing batches start with no attempts; cards committed before the upgrade don't send recipe_created again
        assert conn.execute(sa.text("SELECT notify_attempts FROM recipe_ingestion_batches")).scalars().all() == [0, 0]
        committed = _job_columns(conn, COMMITTED_ID)
        assert committed["recipe_event_sent_at"] is not None
        assert str(committed["recipe_event_sent_at"]).startswith("2026-10-03 18:30")
        ready = _job_columns(conn, READY_ID)
        assert ready["recipe_event_sent_at"] is None
        assert ready["auto_retry_at"] is None and ready["recipe_event_claimed_at"] is None

        # a batch inserted without the new columns gets the default
        conn.execute(
            sa.text(
                "INSERT INTO recipe_ingestion_batches (id, group_id, household_id, source) "
                "VALUES (:id, :group, :household, 'inbox')"
            ),
            {"id": _guid(conn, uuid4()), "group": _guid(conn, GROUP_ID), "household": _guid(conn, HOUSEHOLD_ID)},
        )
        assert conn.execute(sa.text("SELECT max(notify_attempts) FROM recipe_ingestion_batches")).scalar_one() == 0

    command.downgrade(cfg, REVISION)

    with _connect(db_url) as conn:
        inspector = sa.inspect(conn)
        assert not {"notify_claimed_at", "notify_attempts", "notify_delivered"} & {
            column["name"] for column in inspector.get_columns("recipe_ingestion_batches")
        }
        assert not {"auto_retry_at", "recipe_event_claimed_at", "recipe_event_sent_at"} & {
            column["name"] for column in inspector.get_columns("recipe_ingestion_jobs")
        }
        assert conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one() == REVISION
        assert conn.execute(sa.text("SELECT count(*) FROM recipe_ingestion_jobs")).scalar_one() == 2
        assert conn.execute(sa.text("SELECT count(*) FROM recipe_ingestion_batches")).scalar_one() == 3

    # and back again
    command.upgrade(cfg, DELIVERY_REVISION)
    with _connect(db_url) as conn:
        assert _job_columns(conn, COMMITTED_ID)["recipe_event_sent_at"] is not None


# ==================================================================================================================
# 0f77cc21b216: a waiting card's lift backoff


def test_lift_backoff_columns_upgrade_and_downgrade(db_url: str):
    cfg = _alembic_cfg()
    command.upgrade(cfg, DELIVERY_REVISION)
    with _connect(db_url) as conn:
        _insert_job(conn, status="failed", job_id=READY_ID)

    command.upgrade(cfg, LIFT_REVISION)

    with _connect(db_url) as conn:
        columns = {column["name"]: column for column in sa.inspect(conn).get_columns("recipe_ingestion_jobs")}
        assert {"lift_retries", "lift_retry_at"} <= set(columns)
        assert not columns["lift_retries"]["nullable"] and columns["lift_retry_at"]["nullable"]
        # a card waiting before the upgrade starts with no backoff
        row = conn.execute(
            sa.text("SELECT lift_retries, lift_retry_at FROM recipe_ingestion_jobs WHERE id = :id"),
            {"id": _guid(conn, READY_ID)},
        ).one()
        assert (row.lift_retries, row.lift_retry_at) == (0, None)
        # and a card inserted without them gets the default
        _insert_job(conn, with_settings=False)
        assert conn.execute(sa.text("SELECT max(lift_retries) FROM recipe_ingestion_jobs")).scalar_one() == 0

    command.downgrade(cfg, DELIVERY_REVISION)

    with _connect(db_url) as conn:
        columns = {column["name"] for column in sa.inspect(conn).get_columns("recipe_ingestion_jobs")}
        assert not {"lift_retries", "lift_retry_at"} & columns
        assert conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one() == DELIVERY_REVISION
        assert conn.execute(sa.text("SELECT count(*) FROM recipe_ingestion_jobs")).scalar_one() == 2

    # and back again
    command.upgrade(cfg, LIFT_REVISION)
    with _connect(db_url) as conn:
        assert conn.execute(sa.text("SELECT sum(lift_retries) FROM recipe_ingestion_jobs")).scalar_one() == 0
