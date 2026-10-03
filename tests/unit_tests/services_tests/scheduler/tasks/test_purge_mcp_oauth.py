from datetime import UTC, datetime, timedelta
from uuid import uuid4

import sqlalchemy as sa

from mealie.db.db_setup import session_context
from mealie.db.models.ai_mcp import McpApiTokenGrant, McpOAuthCode, McpOAuthRequest, McpOAuthToken
from mealie.schema.mcp.mcp_oauth import McpClientCreate
from mealie.services.oauth.clients import McpClientService
from mealie.services.oauth.tokens import new_secret
from mealie.services.scheduler.tasks.purge_mcp_oauth import purge_mcp_oauth
from tests.utils.fixture_schemas import TestUser


def test_purge_with_nothing_to_purge():
    purge_mcp_oauth()


def test_purge(unique_user: TestUser):
    now = datetime.now(UTC)
    with session_context() as session:
        client = McpClientService(session, unique_user._group_id).create(
            McpClientCreate(name="Purge", redirect_uris=["https://ha.example/cb"], pkce_optional=True),
            created_by=unique_user.user_id,
        )

        def request(expires_at: datetime) -> McpOAuthRequest:
            return McpOAuthRequest(
                handle_hash=new_secret(),
                oauth_client_id=client.id,
                redirect_uri="https://ha.example/cb",
                redirect_uri_provided=True,
                scopes="mcp:read",
                resource="http://testserver/api/mcp",
                issuer="http://testserver",
                expires_at=expires_at,
            )

        def code(expires_at: datetime) -> McpOAuthCode:
            return McpOAuthCode(
                code_hash=new_secret(),
                oauth_client_id=client.id,
                user_id=unique_user.user_id,
                redirect_uri="https://ha.example/cb",
                redirect_uri_provided=True,
                scopes="mcp:read",
                resource="http://testserver/api/mcp",
                expires_at=expires_at,
            )

        def token(expires_at: datetime, revoked_at: datetime | None = None) -> McpOAuthToken:
            return McpOAuthToken(
                token_hash=new_secret(),
                kind="refresh",
                oauth_client_id=client.id,
                user_id=unique_user.user_id,
                family_id=uuid4(),
                scopes="mcp:read",
                resource="http://testserver/api/mcp",
                expires_at=expires_at,
                granted_at=now,
                revoked_at=revoked_at,
            )

        keep = [
            request(now + timedelta(minutes=5)),
            code(now + timedelta(seconds=30)),
            token(now + timedelta(days=1)),
            # expired or revoked recently: kept, so a replay is still recognised as one
            token(now - timedelta(days=29)),
            token(now + timedelta(days=60), revoked_at=now - timedelta(days=29)),
        ]
        purge: list[McpOAuthRequest | McpOAuthCode | McpOAuthToken | McpApiTokenGrant] = [
            request(now - timedelta(seconds=1)),
            code(now - timedelta(seconds=1)),
            token(now - timedelta(days=31)),
            token(now + timedelta(days=60), revoked_at=now - timedelta(days=31)),
        ]
        if session.get_bind().dialect.name == "sqlite":
            # left behind by an API token deleted without the ORM: SQLite doesn't enforce foreign keys
            purge.append(McpApiTokenGrant(long_live_token_id=10**9, allow_writes=True))
        session.add_all([*keep, *purge])
        session.commit()
        keep_ids = {row.id for row in keep}
        purge_ids = {row.id for row in purge}

    purge_mcp_oauth()

    with session_context() as session:
        remaining = {
            row_id
            for model in (McpOAuthRequest, McpOAuthCode, McpOAuthToken, McpApiTokenGrant)
            for row_id in session.execute(sa.select(model.id)).scalars()
        }
        assert keep_ids <= remaining
        assert not purge_ids & remaining

        McpClientService(session, unique_user._group_id).delete(client.id)
