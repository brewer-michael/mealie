"""
Encrypts AI provider API keys still stored in plaintext (docs/ai/PHASE1.md §3).

Migration 7f3d2a91c6e8 encrypts existing keys, but if that step fails it only logs the error, and a
key is only re-encrypted when a new one is entered. This runs on every startup and catches up.
"""

import sqlalchemy as sa
from sqlalchemy.orm import Session

from mealie.core import root_logger
from mealie.db.models._model_utils.encrypted import is_encrypted
from mealie.db.models.group.ai_providers import AIProvider

logger = root_logger.get_logger("init_db")


def fix_unencrypted_ai_provider_keys(session: Session) -> int:
    """Encrypts every stored API key without the `enc:v1:` prefix; returns how many it encrypted"""
    # Read the column as stored: the ORM type would decrypt it
    stored_key = sa.type_coerce(AIProvider.api_key, sa.String)

    try:
        plaintext = [
            (provider_id, api_key)
            for provider_id, api_key in session.execute(sa.select(AIProvider.id, stored_key)).all()
            if api_key and not is_encrypted(api_key)
        ]
        for provider_id, api_key in plaintext:
            # Written through the column's type, which encrypts it
            session.execute(sa.update(AIProvider).where(AIProvider.id == provider_id).values(api_key=api_key))

        session.commit()
    except Exception:
        session.rollback()
        raise

    if plaintext:
        logger.info(f"Encrypted {len(plaintext)} AI provider API key(s) that were stored in plaintext")
    else:
        logger.debug("No plaintext AI provider API keys found; skipping fix")

    return len(plaintext)
