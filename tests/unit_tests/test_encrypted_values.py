from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from pydantic import UUID4
from sqlalchemy.orm import Session

from mealie.db.models._model_utils import encrypted
from mealie.db.models._model_utils.encrypted import (
    ENCRYPTED_PREFIX,
    EncryptedString,
    SecretDecryptionError,
    decrypt_value,
    encrypt_value,
    is_encrypted,
)
from mealie.db.models._model_utils.guid import GUID
from mealie.schema.group.ai_providers import AIProviderCreate, AIProviderUpdate
from tests.utils.factories import random_string
from tests.utils.fixture_schemas import TestUser

SECRET = "first-secret"
OTHER_SECRET = "second-secret"


def _raw_api_key(session: Session, provider_id: UUID4) -> str:
    """The api_key column as stored, bypassing EncryptedString"""
    guid = GUID.convert_value_to_guid(provider_id, session.get_bind().dialect)
    return session.execute(sa.text("SELECT api_key FROM ai_providers WHERE id = :id"), {"id": guid}).scalar_one()


def _set_raw_api_key(session: Session, provider_id: UUID4, value: str) -> None:
    guid = GUID.convert_value_to_guid(provider_id, session.get_bind().dialect)
    session.execute(sa.text("UPDATE ai_providers SET api_key = :value WHERE id = :id"), {"value": value, "id": guid})
    session.commit()


@pytest.fixture
def reset_decrypt_failure_log(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(encrypted, "_logged_decrypt_failure", False)


# ==========================================
# Helpers


def test_round_trip():
    stored = encrypt_value("sk-test-key", SECRET)

    assert stored.startswith(ENCRYPTED_PREFIX)
    assert "sk-test-key" not in stored
    assert decrypt_value(stored, SECRET) == "sk-test-key"


def test_round_trip_non_ascii_and_empty():
    for value in ["", "clé-🔑-ключ"]:
        assert decrypt_value(encrypt_value(value, SECRET), SECRET) == value


def test_encryption_is_randomized():
    # Fernet tokens carry a random IV, so equal keys don't produce equal ciphertext
    assert encrypt_value("same", SECRET) != encrypt_value("same", SECRET)


def test_uses_the_app_secret_by_default():
    stored = encrypt_value("sk-test-key")
    assert decrypt_value(stored) == "sk-test-key"

    with pytest.raises(SecretDecryptionError):
        decrypt_value(stored, OTHER_SECRET)


@pytest.mark.parametrize("value", ["sk-plaintext", "", "enc:v2:not-ours"])
def test_plaintext_passthrough(value: str):
    assert not is_encrypted(value)
    assert decrypt_value(value, SECRET) == value


def test_wrong_secret_raises():
    stored = encrypt_value("sk-test-key", SECRET)

    with pytest.raises(SecretDecryptionError):
        decrypt_value(stored, OTHER_SECRET)


def test_garbage_token_raises():
    with pytest.raises(SecretDecryptionError):
        decrypt_value(f"{ENCRYPTED_PREFIX}not-a-fernet-token", SECRET)


# ==========================================
# Column type


def test_type_decorator_round_trip(session: Session):
    column_type = EncryptedString()
    dialect = session.get_bind().dialect

    stored = column_type.process_bind_param("sk-test-key", dialect)
    assert stored and stored.startswith(ENCRYPTED_PREFIX)
    assert column_type.process_result_value(stored, dialect) == "sk-test-key"

    assert column_type.process_bind_param(None, dialect) is None
    assert column_type.process_result_value(None, dialect) is None
    assert column_type.process_result_value("sk-plaintext", dialect) == "sk-plaintext"


def test_type_decorator_reads_undecryptable_value_as_empty_and_logs_once(
    session: Session, reset_decrypt_failure_log: None, monkeypatch: pytest.MonkeyPatch
):
    logger = MagicMock()
    monkeypatch.setattr(encrypted, "get_logger", lambda: logger)

    column_type = EncryptedString()
    dialect = session.get_bind().dialect
    foreign = encrypt_value("sk-test-key", OTHER_SECRET)

    assert column_type.process_result_value(foreign, dialect) == ""
    assert column_type.process_result_value(foreign, dialect) == ""

    assert logger.error.call_count == 1
    message = str(logger.error.call_args)
    assert "could not be decrypted" in message
    assert "sk-test-key" not in message
    assert foreign not in message


# ==========================================
# AI provider API keys in the database


def test_provider_api_key_is_encrypted_at_rest(unique_user: TestUser):
    repos = unique_user.repos
    session = repos.session
    provider = repos.group_ai_providers.create(
        AIProviderCreate(name=random_string(), model="gpt-4o", api_key="sk-at-rest")
    )

    try:
        raw = _raw_api_key(session, provider.id)
        assert raw.startswith(ENCRYPTED_PREFIX)
        assert "sk-at-rest" not in raw

        stored = repos.group_ai_providers.get_one(provider.id)
        assert stored and stored.api_key == "sk-at-rest"
    finally:
        repos.group_ai_providers.delete(provider.id)


def test_provider_plaintext_api_key_still_reads(unique_user: TestUser):
    """Rows written before encryption (or by an older build) keep working"""
    repos = unique_user.repos
    session = repos.session
    provider = repos.group_ai_providers.create(AIProviderCreate(name=random_string(), model="gpt-4o", api_key="x"))

    try:
        _set_raw_api_key(session, provider.id, "sk-legacy-plaintext")
        session.expire_all()

        stored = repos.group_ai_providers.get_one(provider.id)
        assert stored and stored.api_key == "sk-legacy-plaintext"
    finally:
        repos.group_ai_providers.delete(provider.id)


def test_provider_update_without_key_keeps_an_undecryptable_key(unique_user: TestUser, reset_decrypt_failure_log: None):
    """
    Editing a provider whose key can't be decrypted (e.g. `.secret` changed) mustn't overwrite the
    stored ciphertext, so restoring the original secret can still recover it
    """
    repos = unique_user.repos
    session = repos.session
    provider = repos.group_ai_providers.create(AIProviderCreate(name=random_string(), model="gpt-4o", api_key="x"))

    try:
        foreign = encrypt_value("sk-from-another-secret", OTHER_SECRET)
        _set_raw_api_key(session, provider.id, foreign)
        session.expire_all()

        stored = repos.group_ai_providers.get_one(provider.id)
        assert stored and stored.api_key == ""

        repos.group_ai_providers.update(provider.id, AIProviderUpdate(name=provider.name, model="gpt-4o-mini"))
        session.expire_all()

        assert _raw_api_key(session, provider.id) == foreign
        updated = repos.group_ai_providers.get_one(provider.id)
        assert updated and updated.model == "gpt-4o-mini"
    finally:
        repos.group_ai_providers.delete(provider.id)
