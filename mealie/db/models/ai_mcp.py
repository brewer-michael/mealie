"""
Fork-owned tables for the MCP server (docs/ai/PHASE3.md §5): the OAuth clients registered for it, pending
authorization requests, authorization codes, access and refresh tokens, and the write grants of Mealie API tokens.

Kept apart from upstream's model packages so upstream syncs don't conflict. The relationships back to upstream's
models are declared here as backrefs for the same reason. They are what delete these rows along with a group, a
user or an API token: SQLite doesn't enforce foreign keys, so the database itself won't.
"""

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy import orm
from sqlalchemy.types import TypeDecorator

from ._model_base import BaseMixins, SqlAlchemyBase
from ._model_utils.auto_init import auto_init
from ._model_utils.datetime import NaiveDateTime
from ._model_utils.guid import GUID
from .users.users import LongLiveToken, User

if TYPE_CHECKING:
    from .group import Group


class StringList(TypeDecorator):
    """
    A list of strings, stored as JSON text. Not `sa.JSON`: backups restore a list value as a list of rows
    (`AlchemyExporter.convert_types`), which would break on a list of plain strings.
    """

    impl = sa.Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: sa.Dialect) -> str | None:
        return None if value is None else json.dumps(list(value))

    def process_result_value(self, value: Any, dialect: sa.Dialect) -> list[str]:
        return [] if value is None else json.loads(value)


class McpOAuthClient(SqlAlchemyBase, BaseMixins):
    """An OAuth client registered by a group's managers. There is no dynamic client registration."""

    __tablename__ = "mcp_oauth_clients"

    id: orm.Mapped[GUID] = orm.mapped_column(GUID, primary_key=True, default=GUID.generate)

    group_id: orm.Mapped[GUID] = orm.mapped_column(GUID, sa.ForeignKey("groups.id"), nullable=False, index=True)
    # Deleting a group deletes its clients, and with them their requests, codes and tokens
    group: orm.Mapped[Group] = orm.relationship(
        "Group", backref=orm.backref("mcp_oauth_clients", cascade="all, delete-orphan")
    )

    name: orm.Mapped[str] = orm.mapped_column(sa.String, nullable=False)
    client_id: orm.Mapped[str] = orm.mapped_column(sa.String, nullable=False, unique=True, index=True)
    """Random and public"""
    client_secret_hash: orm.Mapped[str | None] = orm.mapped_column(sa.String, nullable=True)
    """SHA-256 of the secret; `None` for a public client"""
    is_confidential: orm.Mapped[bool] = orm.mapped_column(sa.Boolean, nullable=False, default=True)
    pkce_optional: orm.Mapped[bool] = orm.mapped_column(sa.Boolean, nullable=False, default=False)
    """Lets a confidential client leave out PKCE (an OAuth 2.0 client, e.g. Home Assistant)"""
    allow_write_scope: orm.Mapped[bool] = orm.mapped_column(sa.Boolean, nullable=False, default=False)
    redirect_uris: orm.Mapped[list[str]] = orm.mapped_column(StringList, nullable=False, default=list)

    # Nulled (by the relationship) when that user is deleted: the client belongs to the group, not to them
    created_by: orm.Mapped[GUID | None] = orm.mapped_column(GUID, sa.ForeignKey("users.id"), nullable=True, index=True)
    creator: orm.Mapped[User | None] = orm.relationship(User, backref=orm.backref("mcp_oauth_clients_created"))

    last_used_at: orm.Mapped[datetime | None] = orm.mapped_column(NaiveDateTime, nullable=True)

    @auto_init()
    def __init__(self, **_) -> None:
        pass


class McpOAuthRequest(SqlAlchemyBase, BaseMixins):
    """An authorization request waiting for the user's consent (10 minutes)"""

    __tablename__ = "mcp_oauth_requests"

    id: orm.Mapped[GUID] = orm.mapped_column(GUID, primary_key=True, default=GUID.generate)
    handle_hash: orm.Mapped[str] = orm.mapped_column(sa.String, nullable=False, unique=True, index=True)

    oauth_client_id: orm.Mapped[GUID] = orm.mapped_column(
        GUID, sa.ForeignKey("mcp_oauth_clients.id"), nullable=False, index=True
    )
    oauth_client: orm.Mapped[McpOAuthClient] = orm.relationship(
        McpOAuthClient, backref=orm.backref("requests", cascade="all, delete-orphan")
    )

    redirect_uri: orm.Mapped[str] = orm.mapped_column(sa.String, nullable=False)
    redirect_uri_provided: orm.Mapped[bool] = orm.mapped_column(sa.Boolean, nullable=False)
    scopes: orm.Mapped[str] = orm.mapped_column(sa.String, nullable=False)
    """Space-separated, as requested (and allowed for the client)"""
    state: orm.Mapped[str | None] = orm.mapped_column(sa.Text, nullable=True)
    code_challenge: orm.Mapped[str | None] = orm.mapped_column(sa.String, nullable=True)
    """An S256 challenge; the only method accepted"""
    resource: orm.Mapped[str] = orm.mapped_column(sa.String, nullable=False)
    issuer: orm.Mapped[str] = orm.mapped_column(sa.String, nullable=False)
    """The origin the request came in on, sent back as `iss` (RFC 9207)"""
    expires_at: orm.Mapped[datetime] = orm.mapped_column(NaiveDateTime, nullable=False, index=True)


class McpOAuthCode(SqlAlchemyBase, BaseMixins):
    """An authorization code (hashed, 60 seconds, single use)"""

    __tablename__ = "mcp_oauth_codes"

    id: orm.Mapped[GUID] = orm.mapped_column(GUID, primary_key=True, default=GUID.generate)
    code_hash: orm.Mapped[str] = orm.mapped_column(sa.String, nullable=False, unique=True, index=True)

    oauth_client_id: orm.Mapped[GUID] = orm.mapped_column(
        GUID, sa.ForeignKey("mcp_oauth_clients.id"), nullable=False, index=True
    )
    oauth_client: orm.Mapped[McpOAuthClient] = orm.relationship(
        McpOAuthClient, backref=orm.backref("codes", cascade="all, delete-orphan")
    )
    user_id: orm.Mapped[GUID] = orm.mapped_column(GUID, sa.ForeignKey("users.id"), nullable=False, index=True)
    user: orm.Mapped[User] = orm.relationship(
        User, backref=orm.backref("mcp_oauth_codes", cascade="all, delete-orphan")
    )

    redirect_uri: orm.Mapped[str] = orm.mapped_column(sa.String, nullable=False)
    redirect_uri_provided: orm.Mapped[bool] = orm.mapped_column(sa.Boolean, nullable=False)
    scopes: orm.Mapped[str] = orm.mapped_column(sa.String, nullable=False)
    """Space-separated, as granted"""
    resource: orm.Mapped[str] = orm.mapped_column(sa.String, nullable=False)
    code_challenge: orm.Mapped[str | None] = orm.mapped_column(sa.String, nullable=True)
    expires_at: orm.Mapped[datetime] = orm.mapped_column(NaiveDateTime, nullable=False, index=True)

    used: orm.Mapped[bool] = orm.mapped_column(sa.Boolean, nullable=False, default=False)
    family_id: orm.Mapped[GUID | None] = orm.mapped_column(GUID, nullable=True)
    """The token family issued from this code, revoked if the code is used again"""


class McpOAuthToken(SqlAlchemyBase, BaseMixins):
    """An access or refresh token (hashed). Every token from one authorization shares a `family_id`."""

    __tablename__ = "mcp_oauth_tokens"

    id: orm.Mapped[GUID] = orm.mapped_column(GUID, primary_key=True, default=GUID.generate)
    token_hash: orm.Mapped[str] = orm.mapped_column(sa.String, nullable=False, unique=True, index=True)
    kind: orm.Mapped[str] = orm.mapped_column(sa.String, nullable=False)
    """`access` or `refresh`"""

    oauth_client_id: orm.Mapped[GUID] = orm.mapped_column(
        GUID, sa.ForeignKey("mcp_oauth_clients.id"), nullable=False, index=True
    )
    oauth_client: orm.Mapped[McpOAuthClient] = orm.relationship(
        McpOAuthClient, backref=orm.backref("tokens", cascade="all, delete-orphan")
    )
    user_id: orm.Mapped[GUID] = orm.mapped_column(GUID, sa.ForeignKey("users.id"), nullable=False, index=True)
    user: orm.Mapped[User] = orm.relationship(
        User, backref=orm.backref("mcp_oauth_tokens", cascade="all, delete-orphan")
    )

    family_id: orm.Mapped[GUID] = orm.mapped_column(GUID, nullable=False, index=True)
    scopes: orm.Mapped[str] = orm.mapped_column(sa.String, nullable=False)
    """Space-separated"""
    resource: orm.Mapped[str] = orm.mapped_column(sa.String, nullable=False)
    """The MCP server URL this token is for (its audience)"""

    expires_at: orm.Mapped[datetime] = orm.mapped_column(NaiveDateTime, nullable=False, index=True)
    granted_at: orm.Mapped[datetime] = orm.mapped_column(NaiveDateTime, nullable=False)
    """When the user approved the family's authorization; kept across refreshes"""
    revoked_at: orm.Mapped[datetime | None] = orm.mapped_column(NaiveDateTime, nullable=True, index=True)
    """Set on revocation, and on a refresh token when it's rotated"""
    last_used_at: orm.Mapped[datetime | None] = orm.mapped_column(NaiveDateTime, nullable=True)


class McpApiTokenGrant(SqlAlchemyBase, BaseMixins):
    """Whether a Mealie API token may use the MCP server's write tools (off unless a row says so)"""

    __tablename__ = "mcp_api_token_grants"

    id: orm.Mapped[GUID] = orm.mapped_column(GUID, primary_key=True, default=GUID.generate)

    long_live_token_id: orm.Mapped[int] = orm.mapped_column(
        sa.Integer, sa.ForeignKey("long_live_tokens.id"), nullable=False, unique=True, index=True
    )
    # Deleting the API token (or its user) deletes its grant
    long_live_token: orm.Mapped[LongLiveToken] = orm.relationship(
        LongLiveToken, backref=orm.backref("mcp_grant", uselist=False, cascade="all, delete-orphan")
    )

    allow_writes: orm.Mapped[bool] = orm.mapped_column(sa.Boolean, nullable=False, default=False)
