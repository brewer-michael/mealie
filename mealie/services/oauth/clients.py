"""
Managing a group's MCP OAuth clients, and each user's connected apps and API token write grants
(docs/ai/PHASE3.md §3-4, §6)
"""

from datetime import UTC, datetime

from pydantic import UUID4
from sqlalchemy.orm import Session

from mealie.repos.repository_mcp import RepositoryMcpOAuth, RepositoryMcpOAuthClients
from mealie.schema.mcp.mcp_oauth import (
    McpApiTokenGrantOut,
    McpClientCreate,
    McpClientCreated,
    McpClientOut,
    McpClientSecretOut,
    McpClientUpdate,
    McpConnectionOut,
    McpScope,
)
from mealie.schema.user.user import PrivateUser
from mealie.services.ai.mcp.auth import invalidate_mcp_principals

from .tokens import CLIENT_SECRET_PREFIX, SUPPORTED_SCOPES, hash_secret, new_client_id, new_secret

HOME_ASSISTANT_REDIRECT_URIS = (
    # with Home Assistant's `my` integration, part of its default configuration
    "https://my.home-assistant.io/redirect/oauth",
    # without it: `<Home Assistant URL>/auth/external/callback`
    "http://homeassistant.local:8123/auth/external/callback",
)


def home_assistant_preset(home_assistant_url: str | None = None) -> McpClientCreate:
    """
    A client for Home Assistant's MCP integration: confidential (Home Assistant always sends a secret), without PKCE
    (it sends none), and read-only until a manager allows changes. `home_assistant_url` replaces the default
    `http://homeassistant.local:8123` in the second redirect URI.
    """
    redirect_uris = list(HOME_ASSISTANT_REDIRECT_URIS)
    if home_assistant_url:
        redirect_uris[1] = home_assistant_url.rstrip("/") + "/auth/external/callback"

    return McpClientCreate(
        name="Home Assistant",
        redirect_uris=redirect_uris,
        is_confidential=True,
        pkce_optional=True,
        allow_write_scope=False,
    )


class McpClientError(ValueError):
    """A change a client can't take; the message says why"""


class McpClientService:
    """A group's OAuth clients"""

    def __init__(self, session: Session, group_id: UUID4) -> None:
        self.group_id = group_id
        self.clients = RepositoryMcpOAuthClients(session, group_id=group_id)

    def get_all(self) -> list[McpClientOut]:
        return sorted(self.clients.get_all(), key=lambda client: (client.name.casefold(), client.client_id))

    def get_one(self, client_pk: UUID4) -> McpClientOut | None:
        return self.clients.get_one(client_pk)

    def create(self, data: McpClientCreate, created_by: UUID4 | None) -> McpClientCreated:
        secret = new_secret(CLIENT_SECRET_PREFIX) if data.is_confidential else None
        client = self.clients.create(
            {
                **data.model_dump(),
                "group_id": self.group_id,
                "client_id": new_client_id(),
                "client_secret_hash": hash_secret(secret) if secret else None,
                "created_by": created_by,
            }
        )
        return McpClientCreated(**client.model_dump(), client_secret=secret)

    def update(self, client_pk: UUID4, data: McpClientUpdate) -> McpClientOut | None:
        client = self.clients.get_row(client_pk)
        if client is None:
            return None
        if data.pkce_optional and not client.is_confidential:
            # OAuth 2.1 §7.5.1: only a client that authenticates may go without PKCE
            raise McpClientError("PKCE can only be optional for a confidential client")

        updated = self.clients.update(client_pk, data.model_dump())
        # whether tokens may write is checked against the client, so cached verifications must go
        invalidate_mcp_principals(oauth_client_id=client_pk)
        return updated

    def rotate_secret(self, client_pk: UUID4) -> McpClientSecretOut | None:
        """
        Replaces a confidential client's secret. Its tokens stay valid, but refreshing them takes the new secret.
        """
        client = self.clients.get_row(client_pk)
        if client is None:
            return None
        if not client.is_confidential:
            raise McpClientError("A public client has no secret")

        secret = new_secret(CLIENT_SECRET_PREFIX)
        client.client_secret_hash = hash_secret(secret)
        self.clients.session.commit()
        return McpClientSecretOut(client_id=client.client_id, client_secret=secret)

    def delete(self, client_pk: UUID4) -> McpClientOut | None:
        """Deletes a client, revoking all its tokens"""
        # locked first, so the delete waits for a grant in progress (see `RepositoryMcpOAuth.get_client`)
        if self.clients.get_row(client_pk, for_update=True) is None:
            return None

        deleted = self.clients.delete(client_pk)
        invalidate_mcp_principals(oauth_client_id=client_pk)
        return deleted


class McpConnectionService:
    """One user's connected apps and API token write grants"""

    def __init__(self, session: Session, user: PrivateUser) -> None:
        self.session = session
        self.user = user
        self.repo = RepositoryMcpOAuth(session)

    def get_connections(self) -> list[McpConnectionOut]:
        return [
            McpConnectionOut(
                client_id=connection.client.id,
                client_name=connection.client.name,
                scopes=[McpScope(scope) for scope in SUPPORTED_SCOPES if scope in connection.scopes],
                created_at=connection.granted_at,
                last_used_at=connection.last_used_at,
            )
            for connection in self.repo.connections(self.user.id, datetime.now(UTC))
        ]

    def disconnect(self, client_pk: UUID4) -> bool:
        """Revokes every token the user has for that client. False if they had none."""
        revoked = self.repo.revoke_user_client(self.user.id, client_pk, datetime.now(UTC))
        self.session.commit()
        invalidate_mcp_principals(user_id=self.user.id, oauth_client_id=client_pk)
        return revoked > 0

    def get_api_token_grants(self) -> list[McpApiTokenGrantOut]:
        return [
            McpApiTokenGrantOut(token_id=token_id, allow_writes=allow_writes)
            for token_id, allow_writes in self.repo.api_token_grants(self.user.id).items()
        ]

    def get_api_token_grant(self, token_id: int) -> McpApiTokenGrantOut | None:
        if self.repo.get_api_token(token_id, self.user.id) is None:
            return None
        return McpApiTokenGrantOut(token_id=token_id, allow_writes=self.repo.allows_writes(token_id))

    def set_api_token_grant(self, token_id: int, allow_writes: bool) -> McpApiTokenGrantOut | None:
        if self.repo.get_api_token(token_id, self.user.id) is None:
            return None

        self.repo.set_api_token_grant(token_id, allow_writes)
        self.session.commit()
        invalidate_mcp_principals(api_token_id=token_id)
        return McpApiTokenGrantOut(token_id=token_id, allow_writes=allow_writes)
