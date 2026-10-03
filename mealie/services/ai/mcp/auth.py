"""
Bearer token verification for the MCP endpoint (docs/ai/PHASE3.md §2-3).

`verify_mcp_token` accepts exactly two kinds of token:
- access tokens issued by Mealie's MCP authorization server (`mmcp_at_…`), for this server's MCP URL;
- Mealie long-lived API tokens (Profile → API Tokens), checked as upstream checks them, with writes allowed only
  when the token's grant says so.

Anything else is refused, session JWTs and cookies included: a browser session can't drive MCP, and an MCP
server must not accept tokens that weren't issued for it.

It's synchronous and does its own database work, so callers run it in a worker thread, never on the event loop;
`cached_mcp_principal` is the part that may run there. Results are cached for `PRINCIPAL_CACHE_TTL` seconds, keyed by
the token's SHA-256, because Home Assistant authenticates four requests per tool call. Revoking a token,
disconnecting an app, deleting a client, an API token or a user, changing a user (their password, household, group
or permissions), or changing a write grant drops the affected entries from this process's cache once the change is
committed; other worker processes notice within `PRINCIPAL_CACHE_TTL`.
"""

import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any

import jwt
from fastapi import HTTPException
from jwt.exceptions import PyJWTError
from pydantic import UUID4
from sqlalchemy import event, orm, select
from sqlalchemy.exc import SQLAlchemyError

from mealie.core.config import get_app_settings
from mealie.core.dependencies.dependencies import validate_long_live_token
from mealie.core.root_logger import get_logger
from mealie.core.security.tokens import ALGORITHM
from mealie.db.db_setup import session_context
from mealie.db.models.ai_mcp import McpOAuthClient
from mealie.db.models.users.users import LongLiveToken, User
from mealie.repos.all_repositories import get_repositories
from mealie.repos.repository_mcp import RepositoryMcpOAuth
from mealie.schema.user.user import PrivateUser
from mealie.services.oauth.tokens import (
    ACCESS_TOKEN_PREFIX,
    CLIENT_ID_PREFIX,
    SCOPE_READ,
    SCOPE_WRITE,
    SUPPORTED_SCOPES,
    hash_secret,
    predates_password_change,
)
from mealie.services.oauth.urls import canonical_resource, protected_resource_metadata_url

PRINCIPAL_CACHE_TTL = 60.0
"""Seconds a verified token is trusted without looking at the database again"""
LAST_USED_INTERVAL = timedelta(minutes=5)
"""How stale a token's `last_used_at` may get before verification records a new one"""
API_TOKEN_CLIENT_NAME = "API token"
MAX_TOKEN_LENGTH = 4096

logger = get_logger()


@dataclass(frozen=True)
class McpPrincipal:
    """Who an MCP request acts as, and what it may do"""

    user: PrivateUser
    group_id: UUID4
    household_id: UUID4
    client_name: str
    """The OAuth client's name, or "API token\""""
    client_id: str | None
    """The OAuth client's public `client_id`; `None` for an API token"""
    api_token_id: int | None
    """The API token's id; `None` for an OAuth token"""
    scopes: frozenset[str]
    can_write: bool
    """Whether write tools are allowed: the token has `mcp:write` and its client may still use it, or the API
    token has a write grant"""

    @property
    def integration_id(self) -> str:
        """For the events write tools publish"""
        return f"mcp:{self.client_name}"


# ==========================================
# Cache


@dataclass(frozen=True)
class _CacheEntry:
    principal: McpPrincipal
    deadline: float
    """`monotonic()` after which the entry is stale"""
    resource: str | None
    """The audience the token was checked against; `None` for API tokens, which aren't bound to one"""
    user_id: UUID4
    oauth_client_id: UUID4 | None = None
    family_id: UUID4 | None = None
    api_token_id: int | None = None


class _PrincipalCache:
    def __init__(self, max_entries: int = 4096) -> None:
        self._entries: dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()
        self._max_entries = max_entries
        self._generation = 0
        """Bumped by every invalidation, so a verification that read the database before one isn't cached after it"""

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def get(self, key: str, resource: str) -> McpPrincipal | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.deadline <= monotonic():
                del self._entries[key]
                return None
            if entry.resource is not None and entry.resource != resource:
                return None
            return entry.principal

    def put(self, key: str, entry: _CacheEntry, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            if len(self._entries) >= self._max_entries:
                now = monotonic()
                self._entries = {k: v for k, v in self._entries.items() if v.deadline > now}
                if len(self._entries) >= self._max_entries:
                    self._entries.clear()
            self._entries[key] = entry

    def invalidate(
        self,
        *,
        token_hash: str | None,
        user_id: UUID4 | None,
        oauth_client_id: UUID4 | None,
        family_id: UUID4 | None,
        api_token_id: int | None,
    ) -> None:
        def matches(key: str, entry: _CacheEntry) -> bool:
            return (
                (token_hash is not None and key == token_hash)
                or (user_id is not None and entry.user_id == user_id)
                or (oauth_client_id is not None and entry.oauth_client_id == oauth_client_id)
                or (family_id is not None and entry.family_id == family_id)
                or (api_token_id is not None and entry.api_token_id == api_token_id)
            )

        with self._lock:
            self._generation += 1
            self._entries = {k: v for k, v in self._entries.items() if not matches(k, v)}

    def clear(self) -> None:
        with self._lock:
            self._generation += 1
            self._entries.clear()


_cache = _PrincipalCache()


def invalidate_mcp_principals(
    *,
    token_hash: str | None = None,
    user_id: UUID4 | None = None,
    oauth_client_id: UUID4 | None = None,
    family_id: UUID4 | None = None,
    api_token_id: int | None = None,
) -> None:
    """
    Drops cached verifications matching any of the arguments: a token (by `hash_secret` of it), everything of a
    user, of an OAuth client (its `id`), of a token family, or of an API token. Call it after committing the change.
    """
    _cache.invalidate(
        token_hash=token_hash,
        user_id=user_id,
        oauth_client_id=oauth_client_id,
        family_id=family_id,
        api_token_id=api_token_id,
    )


def clear_mcp_principal_cache() -> None:
    _cache.clear()


def _deadline(expires_at: datetime) -> float:
    remaining = (expires_at - datetime.now(UTC)).total_seconds()
    return monotonic() + min(PRINCIPAL_CACHE_TTL, remaining)


# ==========================================
# Verification


def _cache_lookup(token: str, resource: str) -> tuple[str, str] | None:
    """`(cache key, audience)` to verify `token` at `resource` with, or `None` if it can't be valid there"""
    audience = canonical_resource(resource)
    if not token or len(token) > MAX_TOKEN_LENGTH or audience is None:
        return None
    return hash_secret(token), audience


def cached_mcp_principal(token: str, resource: str) -> McpPrincipal | None:
    """
    What `verify_mcp_token` would return from this process's cache, without it: the principal if `token` was
    verified for `resource` in the last `PRINCIPAL_CACHE_TTL` seconds and nothing has invalidated it, else `None`
    (which says nothing about the token). Never uses the database, so it may run on the event loop, sparing a
    cached request the hop to a worker thread.
    """
    lookup = _cache_lookup(token, resource)
    return _cache.get(*lookup) if lookup is not None else None


def verify_mcp_token(token: str, resource: str) -> McpPrincipal | None:
    """
    The principal `token` authenticates at the MCP endpoint `resource` (this server's `/api/mcp` URL, see
    `mealie.services.oauth.urls.mcp_url`), or `None` if it doesn't. Synchronous and uses the database: run it in a
    worker thread.
    """
    lookup = _cache_lookup(token, resource)
    if lookup is None:
        return None

    key, audience = lookup
    if (principal := _cache.get(key, audience)) is not None:
        return principal

    generation = _cache.generation

    if token.startswith(ACCESS_TOKEN_PREFIX):
        entry = _verify_access_token(key, audience)
    elif token.startswith(CLIENT_ID_PREFIX):
        # a refresh token, a client secret: never a bearer token
        return None
    else:
        entry = _verify_api_token(token)

    if entry is None:
        return None

    _cache.put(key, entry, generation)
    return entry.principal


def _verify_access_token(token_hash: str, audience: str) -> _CacheEntry | None:
    now = datetime.now(UTC)
    with session_context() as session:
        try:
            repo = RepositoryMcpOAuth(session)
            token = repo.get_token(token_hash)
            if token is None or token.kind != "access" or token.revoked_at is not None or token.expires_at <= now:
                return None

            # Audience: the token must have been issued for this MCP server (RFC 8707 §2, MCP authorization
            # "Token Audience Binding and Validation")
            if token.resource != audience:
                return None

            client = token.oauth_client
            user = get_repositories(session, group_id=None, household_id=None).users.get_one(token.user_id)
            if user is None or user.group_id != client.group_id:
                return None

            # A password change evicts every token issued before it, as upstream does for its own tokens. It revokes
            # them too; this covers a token issued while the change was being committed.
            if predates_password_change(token.created_at, user.tokens_valid_after):
                return None

            scopes = frozenset(token.scopes.split())
            principal = McpPrincipal(
                user=user,
                group_id=user.group_id,
                household_id=user.household_id,
                client_name=client.name,
                client_id=client.client_id,
                api_token_id=None,
                scopes=scopes,
                # the client's write permission is checked now too, so taking it away applies to issued tokens
                can_write=SCOPE_WRITE in scopes and client.allow_write_scope,
            )
            entry = _CacheEntry(
                principal=principal,
                deadline=_deadline(token.expires_at),
                resource=audience,
                user_id=user.id,
                oauth_client_id=client.id,
                family_id=token.family_id,
            )

            if token.last_used_at is None or now - token.last_used_at >= LAST_USED_INTERVAL:
                try:
                    repo.touch(token.id, client.id, now)
                    session.commit()
                except SQLAlchemyError:
                    # best effort: a busy database mustn't fail the request
                    session.rollback()
                    logger.debug("Could not record the MCP token's last use", exc_info=True)

            return entry
        finally:
            # end the read transaction here, in this thread
            session.rollback()


def _verify_api_token(token: str) -> _CacheEntry | None:
    try:
        payload = jwt.decode(token, get_app_settings().SECRET, algorithms=[ALGORITHM])
    except PyJWTError:
        return None

    # Only API tokens: a session token (the browser's) is refused, cookie or not
    if not payload.get("long_token") or not payload.get("id"):
        return None

    with session_context() as session:
        try:
            try:
                user = validate_long_live_token(session, token, payload["id"])
            except HTTPException:
                return None

            # Two API tokens given the same name in the same second are the same JWT (upstream doesn't make
            # `token` unique). Upstream authenticates with the first row it finds; here the first row's grant applies.
            token_id = session.execute(
                select(LongLiveToken.id)
                .where(LongLiveToken.token == token, LongLiveToken.user_id == user.id)
                .order_by(LongLiveToken.id)
                .limit(1)
            ).scalar()
            if token_id is None:
                return None

            allow_writes = RepositoryMcpOAuth(session).allows_writes(token_id)
        finally:
            session.rollback()

    scopes = frozenset(SUPPORTED_SCOPES if allow_writes else (SCOPE_READ,))
    principal = McpPrincipal(
        user=user,
        group_id=user.group_id,
        household_id=user.household_id,
        client_name=API_TOKEN_CLIENT_NAME,
        client_id=None,
        api_token_id=token_id,
        scopes=scopes,
        can_write=allow_writes,
    )
    expires_at = datetime.fromtimestamp(payload["exp"], UTC) if payload.get("exp") else datetime.max.replace(tzinfo=UTC)
    return _CacheEntry(
        principal=principal,
        deadline=_deadline(expires_at),
        resource=None,
        user_id=user.id,
        api_token_id=token_id,
    )


def mcp_www_authenticate(origin: str, error: str | None = "invalid_token") -> str:
    """
    The `WWW-Authenticate` value of the MCP endpoint's 401 (RFC 6750 §3, RFC 9728 §5.1): where its protected
    resource metadata is, and the scopes to ask for. Home Assistant reads both.
    """
    params = [f'error="{error}"'] if error else []
    params.append(f'resource_metadata="{protected_resource_metadata_url(origin)}"')
    params.append(f'scope="{" ".join(SUPPORTED_SCOPES)}"')
    return "Bearer " + ", ".join(params)


# ==========================================
# Invalidation on changes made anywhere (upstream's routes included), for this process. The changes are seen at flush
# and applied once committed: dropping entries any earlier would let a verification that reads the database before
# the commit cache what it's about to replace.

_PENDING_INVALIDATIONS = "mcp_principal_invalidations"
"""`Session.info` key of the invalidations waiting for the session's commit"""


def _invalidate_on_commit(target: object, **kwargs: Any) -> None:
    session = orm.object_session(target)
    if session is None:
        invalidate_mcp_principals(**kwargs)
        return
    session.info.setdefault(_PENDING_INVALIDATIONS, []).append(kwargs)


@event.listens_for(orm.Session, "after_commit")
def _apply_invalidations(session: orm.Session) -> None:
    for kwargs in session.info.pop(_PENDING_INVALIDATIONS, ()):
        invalidate_mcp_principals(**kwargs)


@event.listens_for(orm.Session, "after_transaction_end")
def _discard_invalidations(session: orm.Session, transaction: orm.SessionTransaction) -> None:
    # after a commit, nothing is left; after a rollback (or a close without commit), the changes never happened
    if transaction.parent is None:
        session.info.pop(_PENDING_INVALIDATIONS, None)


@event.listens_for(User, "after_delete")
def _user_deleted(_mapper, _connection, target: User) -> None:
    _invalidate_on_commit(target, user_id=target.id)


@event.listens_for(User, "after_update")
def _user_updated(_mapper, _connection, target: User) -> None:
    # Any change: a new password evicts their tokens, and their household, group and permissions are in the
    # principal. Users change rarely, so this costs next to nothing.
    _invalidate_on_commit(target, user_id=target.id)


@event.listens_for(LongLiveToken, "after_delete")
def _api_token_deleted(_mapper, _connection, target: LongLiveToken) -> None:
    _invalidate_on_commit(target, api_token_id=target.id)


@event.listens_for(McpOAuthClient, "after_delete")
def _client_deleted(_mapper, _connection, target: McpOAuthClient) -> None:
    _invalidate_on_commit(target, oauth_client_id=target.id)
