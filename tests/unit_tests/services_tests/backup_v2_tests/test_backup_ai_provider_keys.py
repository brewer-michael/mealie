"""AI provider API keys are encrypted in the database (and so in backups); restoring must keep them readable"""

import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from mealie.core.config import get_app_dirs, get_app_settings
from mealie.db.db_setup import session_context
from mealie.db.models._model_utils.encrypted import (
    ENCRYPTED_PREFIX,
    SecretDecryptionError,
    decrypt_value,
    is_encrypted,
)
from mealie.db.models._model_utils.guid import GUID
from mealie.repos.all_repositories import get_repositories
from mealie.services.backups_v2.alchemy_exporter import AlchemyExporter
from mealie.services.backups_v2.backup_v2 import BackupV2
from tests.utils import api_routes
from tests.utils.factories import random_string
from tests.utils.fixture_schemas import TestUser

PRE_ENCRYPTION_REVISION = "3527efeeec34"
"""The revision before API keys were encrypted"""


def _create_provider(api_client: TestClient, user: TestUser, api_key: str) -> UUID:
    response = api_client.post(
        api_routes.groups_ai_providers_providers,
        json={"name": random_string(), "model": "gpt-4o", "apiKey": api_key},
        headers=user.token,
    )
    assert response.status_code == 200
    return UUID(response.json()["id"])


def _read_api_key(user: TestUser, provider_id: UUID) -> str | None:
    with session_context() as session:
        repos = get_repositories(session, group_id=UUID(user.group_id), household_id=None)
        provider = repos.group_ai_providers.get_one(provider_id)
        return provider.api_key if provider else None


def _read_raw_api_key(provider_id: UUID) -> str:
    with session_context() as session:
        guid = GUID.convert_value_to_guid(provider_id, session.get_bind().dialect)
        return session.execute(sa.text("SELECT api_key FROM ai_providers WHERE id = :id"), {"id": guid}).scalar_one()


def _dumped_provider(dump: dict, provider_id: UUID) -> dict:
    return next(row for row in dump["ai_providers"] if UUID(row["id"]) == provider_id)


def _as_pre_encryption_dump(dump: dict, secret: str) -> dict:
    """Rewrites a current database dump into what a backup from before key encryption looks like"""
    dump["alembic_version"] = [{"version_num": PRE_ENCRYPTION_REVISION}]
    dump.pop("ai_provider_routes", None)
    dump.pop("ai_usage_log", None)
    for row in dump["ai_providers"]:
        row.pop("protocol", None)
        row.pop("monthly_token_limit", None)
        try:
            row["api_key"] = decrypt_value(row["api_key"], secret)
        except SecretDecryptionError:
            pass

    return dump


def test_backup_round_trip_keeps_provider_keys_readable(api_client: TestClient, unique_user: TestUser):
    provider_id = _create_provider(api_client, unique_user, "sk-backup-round-trip")

    backup_v2 = BackupV2()
    try:
        backup_path = backup_v2.backup()

        # The dump holds ciphertext only
        with zipfile.ZipFile(backup_path) as backup:
            database_json = backup.read("database.json").decode()
        assert "sk-backup-round-trip" not in database_json
        assert _dumped_provider(json.loads(database_json), provider_id)["api_key"].startswith(ENCRYPTED_PREFIX)

        backup_v2.restore(backup_path)

        assert _read_api_key(unique_user, provider_id) == "sk-backup-round-trip"
    finally:
        backup_v2.db_exporter.engine.dispose()
        api_client.delete(api_routes.groups_ai_providers_providers_provider_id(provider_id), headers=unique_user.token)


def test_restoring_pre_encryption_backup_encrypts_keys_with_the_backups_secret(
    api_client: TestClient, unique_user: TestUser, tmp_path: Path
):
    """
    A backup from before encryption holds plaintext keys, and the restore's migrations encrypt them.
    They must be encrypted with the secret the instance runs with afterwards (the backup's `.secret`),
    not the one it had before the restore, or the keys become unreadable.
    """
    provider_id = _create_provider(api_client, unique_user, "sk-legacy-backup")
    original_secret = get_app_settings().SECRET
    backup_secret = "secret-of-the-instance-that-made-the-backup"

    backup_v2 = BackupV2()
    original_backup = backup_v2.backup()
    try:
        legacy_backup = tmp_path / "legacy-backup.zip"
        with zipfile.ZipFile(original_backup) as src, zipfile.ZipFile(legacy_backup, "w") as dst:
            for item in src.infolist():
                if item.filename == "data/.secret":
                    continue

                data = src.read(item)
                if item.filename == "database.json":
                    legacy_dump = _as_pre_encryption_dump(json.loads(data), original_secret)
                    assert _dumped_provider(legacy_dump, provider_id)["api_key"] == "sk-legacy-backup"
                    data = json.dumps(legacy_dump).encode()

                dst.writestr(item, data)
            dst.writestr("data/.secret", backup_secret)

        backup_v2.restore(legacy_backup)

        assert get_app_settings().SECRET == backup_secret
        assert _read_api_key(unique_user, provider_id) == "sk-legacy-backup"
        raw = _read_raw_api_key(provider_id)
        assert is_encrypted(raw)
        assert decrypt_value(raw, backup_secret) == "sk-legacy-backup"
    finally:
        # Puts the suite's database and .secret back
        backup_v2.restore(original_backup)
        backup_v2.db_exporter.engine.dispose()

    assert get_app_settings().SECRET == original_secret
    assert _read_api_key(unique_user, provider_id) == "sk-legacy-backup"
    api_client.delete(api_routes.groups_ai_providers_providers_provider_id(provider_id), headers=unique_user.token)


def test_failed_import_puts_the_original_secret_back(
    api_client: TestClient, unique_user: TestUser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """
    The backup's `.secret` is restored before its database. If the import then fails, the original
    secret must come back: it's the one the SQLite safety copy (`mealie_<date>.bak.db`) needs.
    """
    provider_id = _create_provider(api_client, unique_user, "sk-before-failed-restore")
    data_dir = get_app_dirs().DATA_DIR
    original_secret = get_app_settings().SECRET
    original_secret_file = (data_dir / ".secret").read_bytes()

    backup_v2 = BackupV2()
    original_backup = backup_v2.backup()
    try:
        other_backup = tmp_path / "other-instance.zip"
        with zipfile.ZipFile(original_backup) as src, zipfile.ZipFile(other_backup, "w") as dst:
            for item in src.infolist():
                if item.filename != "data/.secret":
                    dst.writestr(item, src.read(item))
            dst.writestr("data/.secret", "secret-of-another-instance")

        real_import = AlchemyExporter.restore

        def failing_import(self: AlchemyExporter, db_dump: dict) -> None:
            assert get_app_settings().SECRET == "secret-of-another-instance"
            # Fails at the very end, so the tables exist for the clean-up below: on PostgreSQL, upstream's
            # drop_all() can't run twice in a row (it drops the enum types unconditionally)
            real_import(self, db_dump)
            raise RuntimeError("import failed")

        with monkeypatch.context() as mp:
            mp.setattr(AlchemyExporter, "restore", failing_import)
            with pytest.raises(RuntimeError, match="import failed"):
                backup_v2.restore(other_backup)

        assert (data_dir / ".secret").read_bytes() == original_secret_file
        assert get_app_settings().SECRET == original_secret

        # A copy of the replaced secret is kept next to the SQLite safety copy as well
        today = datetime.now(UTC).strftime("%Y.%m.%d")
        assert (data_dir / f"mealie_{today}.bak.secret").read_bytes() == original_secret_file
    finally:
        # Puts the suite's database back
        backup_v2.restore(original_backup)
        backup_v2.db_exporter.engine.dispose()

    assert _read_api_key(unique_user, provider_id) == "sk-before-failed-restore"
    api_client.delete(api_routes.groups_ai_providers_providers_provider_id(provider_id), headers=unique_user.token)
