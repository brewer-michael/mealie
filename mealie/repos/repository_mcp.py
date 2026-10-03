"""
Fork-owned repositories for the MCP server's OAuth tables and API token grants (docs/ai/PHASE3.md §5).

`RepositoryMcpOAuth` works on rows that never leave the server (requests, codes, tokens, grants) and returns the
ORM rows themselves. Its methods don't commit: the authorization server commits each grant as a whole, so a failed
step can't leave a code used without the tokens it pays for.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa
from pydantic import UUID4
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, joinedload

from mealie.db.models.ai_mcp import (
    McpApiTokenGrant,
    McpOAuthClient,
    McpOAuthCode,
    McpOAuthRequest,
    McpOAuthToken,
)
from mealie.db.models.users.users import LongLiveToken, User
from mealie.schema.mcp.mcp_oauth import McpClientOut

from .repository_generic import GroupRepositoryGeneric

TOKEN_RETENTION = timedelta(days=30)
"""How long expired or revoked tokens are kept, so reuse of a recent one is still recognised"""


def _rowcount(result: sa.Result) -> int:
    return result.rowcount if isinstance(result, CursorResult) else 0


class RepositoryMcpOAuthClients(GroupRepositoryGeneric[McpClientOut, McpOAuthClient]):
    """The OAuth clients of the repository's group"""

    def __init__(self, session: Session, *, group_id: UUID4 | None) -> None:
        super().__init__(session, "id", McpOAuthClient, McpClientOut, group_id=group_id)

    def get_row(self, client_pk: UUID4) -> McpOAuthClient | None:
        stmt = sa.select(McpOAuthClient).filter_by(**self._filter_builder(id=client_pk))
        return self.session.execute(stmt).scalars().one_or_none()

    def delete(self, value, match_key: str | None = None) -> McpClientOut:
        # Explicit deletes rather than loading every row for the ORM cascade. A client's tokens are revoked by
        # deleting them; callers drop them from the verifier's cache.
        client = self._query_one(value, match_key)
        for model in (McpOAuthToken, McpOAuthCode, McpOAuthRequest):
            self.session.execute(sa.delete(model).where(model.oauth_client_id == client.id))

        return super().delete(value, match_key)


@dataclass
class McpConnection:
    client: McpOAuthClient
    scopes: set[str]
    granted_at: datetime
    last_used_at: datetime | None


class RepositoryMcpOAuth:
    """Pending requests, codes, tokens and API token grants, for every group"""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ==========================================
    # Clients

    def get_client(self, client_id: str) -> McpOAuthClient | None:
        """By the public `client_id`"""
        stmt = sa.select(McpOAuthClient).where(McpOAuthClient.client_id == client_id)
        return self.session.execute(stmt).scalars().one_or_none()

    def get_user(self, user_id: UUID4) -> User | None:
        return self.session.get(User, user_id)

    # ==========================================
    # Pending requests

    def add_request(self, request: McpOAuthRequest, now: datetime) -> None:
        # Unauthenticated requests create these rows, so a client's expired ones go whenever it gets a new one
        self.session.execute(
            sa.delete(McpOAuthRequest).where(
                McpOAuthRequest.oauth_client_id == request.oauth_client_id, McpOAuthRequest.expires_at <= now
            )
        )
        self.session.add(request)

    def get_request(self, handle_hash: str) -> McpOAuthRequest | None:
        stmt = (
            sa.select(McpOAuthRequest)
            .options(joinedload(McpOAuthRequest.oauth_client))
            .where(McpOAuthRequest.handle_hash == handle_hash)
        )
        return self.session.execute(stmt).scalars().one_or_none()

    def delete_request(self, request_id: UUID4) -> bool:
        """Deletes a request (each is decided once). False if another decision got there first."""
        result = self.session.execute(sa.delete(McpOAuthRequest).where(McpOAuthRequest.id == request_id))
        return _rowcount(result) == 1

    # ==========================================
    # Authorization codes

    def add_code(self, code: McpOAuthCode) -> None:
        self.session.add(code)

    def get_code(self, code_hash: str) -> McpOAuthCode | None:
        stmt = sa.select(McpOAuthCode).where(McpOAuthCode.code_hash == code_hash)
        return self.session.execute(stmt).scalars().one_or_none()

    def claim_code(self, code_id: UUID4, family_id: UUID4) -> bool:
        """Marks a code used, recording the token family issued from it. False if it already was (single use)."""
        result = self.session.execute(
            sa.update(McpOAuthCode)
            .where(McpOAuthCode.id == code_id, McpOAuthCode.used == sa.false())
            .values(used=True, family_id=family_id)
            .execution_options(synchronize_session=False)
        )
        return _rowcount(result) == 1

    def code_family(self, code_id: UUID4) -> UUID4 | None:
        return self.session.execute(sa.select(McpOAuthCode.family_id).where(McpOAuthCode.id == code_id)).scalar()

    # ==========================================
    # Tokens

    def add_token(self, token: McpOAuthToken) -> None:
        self.session.add(token)

    def get_token(self, token_hash: str) -> McpOAuthToken | None:
        stmt = (
            sa.select(McpOAuthToken)
            .options(joinedload(McpOAuthToken.oauth_client))
            .where(McpOAuthToken.token_hash == token_hash)
        )
        return self.session.execute(stmt).scalars().one_or_none()

    def rotate(self, token_id: UUID4, now: datetime) -> bool:
        """Revokes a refresh token being exchanged. False if it already was (a concurrent refresh got there first)."""
        result = self.session.execute(
            sa.update(McpOAuthToken)
            .where(McpOAuthToken.id == token_id, McpOAuthToken.revoked_at.is_(None))
            .values(revoked_at=now)
            .execution_options(synchronize_session=False)
        )
        return _rowcount(result) == 1

    def revoke_token(self, token_id: UUID4, now: datetime) -> None:
        self.session.execute(
            sa.update(McpOAuthToken)
            .where(McpOAuthToken.id == token_id, McpOAuthToken.revoked_at.is_(None))
            .values(revoked_at=now)
            .execution_options(synchronize_session=False)
        )

    def revoke_family(self, family_id: UUID4, now: datetime) -> None:
        self.session.execute(
            sa.update(McpOAuthToken)
            .where(McpOAuthToken.family_id == family_id, McpOAuthToken.revoked_at.is_(None))
            .values(revoked_at=now)
            .execution_options(synchronize_session=False)
        )

    def revoke_user_client(self, user_id: UUID4, client_pk: UUID4, now: datetime) -> int:
        """Revokes every token the user has for that client, and its unused codes. Returns how many were live."""
        self.session.execute(
            sa.delete(McpOAuthCode).where(McpOAuthCode.user_id == user_id, McpOAuthCode.oauth_client_id == client_pk)
        )
        result = self.session.execute(
            sa.update(McpOAuthToken)
            .where(
                McpOAuthToken.user_id == user_id,
                McpOAuthToken.oauth_client_id == client_pk,
                McpOAuthToken.revoked_at.is_(None),
                McpOAuthToken.expires_at > now,
            )
            .values(revoked_at=now)
            .execution_options(synchronize_session=False)
        )
        return _rowcount(result)

    def touch(self, token_id: UUID4, client_pk: UUID4, now: datetime) -> None:
        self.session.execute(sa.update(McpOAuthToken).where(McpOAuthToken.id == token_id).values(last_used_at=now))
        self.session.execute(sa.update(McpOAuthClient).where(McpOAuthClient.id == client_pk).values(last_used_at=now))

    def connections(self, user_id: UUID4, now: datetime) -> list[McpConnection]:
        """The user's connections: clients with a live (unrevoked, unexpired) token, oldest first"""
        stmt = (
            sa.select(McpOAuthToken)
            .options(joinedload(McpOAuthToken.oauth_client))
            .where(
                McpOAuthToken.user_id == user_id,
                McpOAuthToken.revoked_at.is_(None),
                McpOAuthToken.expires_at > now,
            )
        )
        by_client: dict[UUID4, McpConnection] = {}
        for token in self.session.execute(stmt).scalars():
            # issuing a token counts as using the connection: a client refreshes when it's about to use it
            used = max(filter(None, [token.last_used_at, token.created_at]), default=None)
            connection = by_client.get(token.oauth_client_id)
            if connection is None:
                by_client[token.oauth_client_id] = McpConnection(
                    token.oauth_client, set(token.scopes.split()), token.granted_at, used
                )
                continue

            connection.scopes.update(token.scopes.split())
            connection.granted_at = min(connection.granted_at, token.granted_at)
            connection.last_used_at = max(filter(None, [connection.last_used_at, used]), default=None)

        return sorted(by_client.values(), key=lambda c: c.granted_at)

    # ==========================================
    # API token write grants

    def get_api_token(self, token_id: int, user_id: UUID4) -> LongLiveToken | None:
        stmt = sa.select(LongLiveToken).where(LongLiveToken.id == token_id, LongLiveToken.user_id == user_id)
        return self.session.execute(stmt).scalars().one_or_none()

    def api_token_grants(self, user_id: UUID4) -> dict[int, bool]:
        """Every API token of the user, with whether it may write"""
        stmt = (
            sa.select(LongLiveToken.id, McpApiTokenGrant.allow_writes)
            .outerjoin(McpApiTokenGrant, McpApiTokenGrant.long_live_token_id == LongLiveToken.id)
            .where(LongLiveToken.user_id == user_id)
            .order_by(LongLiveToken.id)
        )
        return {token_id: bool(allow_writes) for token_id, allow_writes in self.session.execute(stmt).all()}

    def allows_writes(self, token_id: int) -> bool:
        stmt = sa.select(McpApiTokenGrant.allow_writes).where(McpApiTokenGrant.long_live_token_id == token_id)
        return bool(self.session.execute(stmt).scalar())

    def set_api_token_grant(self, token_id: int, allow_writes: bool) -> None:
        grant = self.session.execute(
            sa.select(McpApiTokenGrant).where(McpApiTokenGrant.long_live_token_id == token_id)
        ).scalar_one_or_none()
        if grant is None:
            self.session.add(McpApiTokenGrant(long_live_token_id=token_id, allow_writes=allow_writes))
        else:
            grant.allow_writes = allow_writes

    # ==========================================
    # Purge

    def purge(self, now: datetime) -> dict[str, int]:
        """
        Deletes expired requests and codes, tokens expired or revoked more than `TOKEN_RETENTION` ago, and rows
        whose client, user or API token is gone (SQLite doesn't enforce foreign keys). Commits.
        """
        cutoff = now - TOKEN_RETENTION
        clients = sa.select(McpOAuthClient.id)
        users = sa.select(User.id)
        statements: dict[str, sa.Delete] = {
            "requests": sa.delete(McpOAuthRequest).where(
                sa.or_(McpOAuthRequest.expires_at <= now, McpOAuthRequest.oauth_client_id.not_in(clients))
            ),
            "codes": sa.delete(McpOAuthCode).where(
                sa.or_(
                    McpOAuthCode.expires_at <= now,
                    McpOAuthCode.oauth_client_id.not_in(clients),
                    McpOAuthCode.user_id.not_in(users),
                )
            ),
            "tokens": sa.delete(McpOAuthToken).where(
                sa.or_(
                    McpOAuthToken.expires_at <= cutoff,
                    McpOAuthToken.revoked_at <= cutoff,
                    McpOAuthToken.oauth_client_id.not_in(clients),
                    McpOAuthToken.user_id.not_in(users),
                )
            ),
            "api_token_grants": sa.delete(McpApiTokenGrant).where(
                McpApiTokenGrant.long_live_token_id.not_in(sa.select(LongLiveToken.id))
            ),
        }

        try:
            removed = {name: _rowcount(self.session.execute(stmt)) for name, stmt in statements.items()}
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise

        return removed
