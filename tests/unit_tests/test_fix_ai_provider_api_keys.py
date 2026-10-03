"""The startup fix that encrypts AI provider API keys left in plaintext (docs/ai/PHASE1.md §3)"""

from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

from mealie.db import init_db
from mealie.db.db_setup import session_context
from mealie.db.fixes.fix_ai_provider_api_keys import fix_unencrypted_ai_provider_keys
from mealie.db.models._model_utils.encrypted import decrypt_value, is_encrypted
from mealie.db.models._model_utils.guid import GUID
from mealie.schema.group.ai_providers import AIProviderCreate
from tests.utils.factories import random_string
from tests.utils.fixture_schemas import TestUser


def _set_raw_api_key(session: Session, provider_id: UUID, value: str) -> None:
    guid = GUID.convert_value_to_guid(provider_id, session.get_bind().dialect)
    session.execute(sa.text("UPDATE ai_providers SET api_key = :value WHERE id = :id"), {"value": value, "id": guid})
    session.commit()


def _raw_api_key(session: Session, provider_id: UUID) -> str:
    guid = GUID.convert_value_to_guid(provider_id, session.get_bind().dialect)
    return session.execute(sa.text("SELECT api_key FROM ai_providers WHERE id = :id"), {"id": guid}).scalar_one()


def test_fix_encrypts_plaintext_keys_once(unique_user: TestUser):
    plaintext, unreadable = (
        unique_user.repos.group_ai_providers.create(AIProviderCreate(name=random_string(), model="m", api_key="k"))
        for _ in range(2)
    )

    try:
        with session_context() as session:
            # As left behind by a failed encryption step in the migration
            _set_raw_api_key(session, plaintext.id, "sk-plaintext")
            _set_raw_api_key(session, unreadable.id, "enc:v1:not-a-token")

            assert fix_unencrypted_ai_provider_keys(session) == 1

            raw = _raw_api_key(session, plaintext.id)
            assert is_encrypted(raw)
            assert decrypt_value(raw) == "sk-plaintext"
            # Already-encrypted values are left alone, readable or not
            assert _raw_api_key(session, unreadable.id) == "enc:v1:not-a-token"

            # Never encrypts twice
            assert fix_unencrypted_ai_provider_keys(session) == 0
            assert _raw_api_key(session, plaintext.id) == raw

        assert unique_user.repos.group_ai_providers.get_one(plaintext.id).api_key == "sk-plaintext"
    finally:
        for provider in (plaintext, unreadable):
            unique_user.repos.group_ai_providers.delete(provider.id)


def test_fix_runs_on_every_startup(unique_user: TestUser):
    """The database is already at head, so no migration (and none of upstream's post-migration fixes) runs"""
    provider = unique_user.repos.group_ai_providers.create(
        AIProviderCreate(name=random_string(), model="m", api_key="k")
    )

    try:
        with session_context() as session:
            _set_raw_api_key(session, provider.id, "sk-plaintext")

        init_db.main()

        with session_context() as session:
            raw = _raw_api_key(session, provider.id)
        assert is_encrypted(raw)
        assert decrypt_value(raw) == "sk-plaintext"
    finally:
        unique_user.repos.group_ai_providers.delete(provider.id)
