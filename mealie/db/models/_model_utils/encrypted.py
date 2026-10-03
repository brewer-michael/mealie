"""
Encryption at rest for secrets stored in the database (currently `ai_providers.api_key`).

Values are stored as `enc:v1:<fernet token>`. The Fernet key is derived with HKDF-SHA256 from the
app's `SECRET` (the `.secret` file in the data directory), so it travels with backups, which restore
that file alongside the database.

- Values without the prefix are read as plaintext, so rows written before encryption keep working.
- A token that won't decrypt (e.g. after `.secret` changed) is logged once per process and read as
  `""`; the caller then fails authentication instead of crashing on every read.
"""

import base64
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import Dialect, String
from sqlalchemy.types import TypeDecorator

from mealie.core.config import get_app_settings
from mealie.core.root_logger import get_logger

ENCRYPTED_PREFIX = "enc:v1:"
HKDF_INFO = b"mealie:ai-provider-api-key:v1"

_logged_decrypt_failure = False


class SecretDecryptionError(ValueError):
    """An encrypted value couldn't be decrypted with the current secret."""


@lru_cache(maxsize=4)
def _fernet_for(secret: str) -> Fernet:
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=HKDF_INFO).derive(secret.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(key))


def _current_secret() -> str:
    # Read on every call rather than cached here: restoring a backup swaps `.secret` and clears the
    # settings cache, and values must follow the new secret from then on.
    return get_app_settings().SECRET


def is_encrypted(value: str) -> bool:
    return value.startswith(ENCRYPTED_PREFIX)


def encrypt_value(plaintext: str, secret: str | None = None) -> str:
    """Encrypts `plaintext` with a key derived from `secret` (default: the app's SECRET)"""
    token = _fernet_for(secret or _current_secret()).encrypt(plaintext.encode("utf-8"))
    return ENCRYPTED_PREFIX + token.decode("ascii")


def decrypt_value(stored: str, secret: str | None = None) -> str:
    """
    Returns the plaintext of a stored value. Values without the `enc:v1:` prefix are returned as-is.

    Raises `SecretDecryptionError` if the value is encrypted but can't be decrypted with `secret`.
    """
    if not is_encrypted(stored):
        return stored

    try:
        token = stored.removeprefix(ENCRYPTED_PREFIX).encode("ascii")
        return _fernet_for(secret or _current_secret()).decrypt(token).decode("utf-8")
    except (InvalidToken, UnicodeError) as e:
        raise SecretDecryptionError("Stored value could not be decrypted with the current secret") from e


def _log_decrypt_failure_once() -> None:
    global _logged_decrypt_failure
    if _logged_decrypt_failure:
        return

    _logged_decrypt_failure = True
    get_logger().error(
        "An encrypted value in the database (e.g. an AI provider API key) could not be decrypted with the current "
        "secret, so it is being read as empty. This usually means the data directory's .secret file changed. "
        "Restore the original .secret, or re-enter the affected API keys. (Logged once per process.)"
    )


class EncryptedString(TypeDecorator[str]):
    """A string column that is encrypted at rest. See the module docstring for the format."""

    impl = String
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        if value is None:
            return None

        return encrypt_value(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> str | None:
        if value is None:
            return None

        try:
            return decrypt_value(value)
        except SecretDecryptionError:
            _log_decrypt_failure_once()
            return ""
