"""Scopes, lifetimes, random secrets and their hashes, and the PKCE check (docs/ai/PHASE3.md §3-4)"""

import base64
import hashlib
import hmac
import re
import secrets
from collections.abc import Iterable
from datetime import datetime, timedelta

SCOPE_READ = "mcp:read"
SCOPE_WRITE = "mcp:write"
SUPPORTED_SCOPES = (SCOPE_READ, SCOPE_WRITE)
"""In the order scope strings are written"""

ACCESS_TOKEN_PREFIX = "mmcp_at_"
REFRESH_TOKEN_PREFIX = "mmcp_rt_"
CLIENT_SECRET_PREFIX = "mmcp_cs_"
CLIENT_ID_PREFIX = "mmcp_"
"""Client ids aren't secret. The prefix also keeps them from ever reading as a UUID, which backups would convert."""

ACCESS_TOKEN_TTL = timedelta(hours=1)
REFRESH_TOKEN_TTL = timedelta(days=90)
"""Without use: every refresh issues a new refresh token with a new 90 days"""
CODE_TTL = timedelta(seconds=60)
REQUEST_TTL = timedelta(minutes=10)

SECRET_BYTES = 32
"""256 bits for tokens, codes, client secrets and request handles"""

_PKCE_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}")
"""RFC 7636 §4.2: BASE64URL(SHA256(verifier)) without padding is always 43 characters"""
_PKCE_VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}")
"""RFC 7636 §4.1"""


def new_secret(prefix: str = "") -> str:
    return prefix + secrets.token_urlsafe(SECRET_BYTES)


def new_client_id() -> str:
    return CLIENT_ID_PREFIX + secrets.token_hex(12)


def hash_secret(value: str) -> str:
    """SHA-256 (hex) of a token, code, handle or client secret, which is what's stored. These are random 256-bit
    values, so a fast hash is enough."""
    return hashlib.sha256(value.encode()).hexdigest()


def secret_matches(value: str, expected_hash: str | None) -> bool:
    """Compares in constant time"""
    if expected_hash is None:
        return False
    return hmac.compare_digest(hash_secret(value), expected_hash)


def is_s256_challenge(value: str) -> bool:
    return _PKCE_CHALLENGE.fullmatch(value) is not None


def pkce_matches(verifier: str, challenge: str) -> bool:
    """RFC 7636 §4.6: BASE64URL-ENCODE(SHA256(ASCII(code_verifier))) == code_challenge"""
    if _PKCE_VERIFIER.fullmatch(verifier) is None:
        return False

    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return hmac.compare_digest(computed, challenge)


def predates_password_change(issued_at: datetime, tokens_valid_after: datetime | None) -> bool:
    """
    Whether a code or token issued at `issued_at` predates the user's last password change (upstream's
    `tokens_valid_after`). That is floored to whole seconds, so all of its second counts as before it: a token
    refreshed earlier in the same second must not outlive the change. The change also revokes them in the database
    (`mealie.db.models.ai_mcp`); this is the backstop.
    """
    return tokens_valid_after is not None and issued_at < tokens_valid_after + timedelta(seconds=1)


def format_scopes(scopes: Iterable[str]) -> str:
    """The supported scopes among `scopes`, space-separated in a fixed order"""
    wanted = set(scopes)
    return " ".join(scope for scope in SUPPORTED_SCOPES if scope in wanted)


def parse_scopes(value: str | None) -> list[str]:
    """RFC 6749 §3.3: space-delimited, case-sensitive"""
    return value.split() if value else []
