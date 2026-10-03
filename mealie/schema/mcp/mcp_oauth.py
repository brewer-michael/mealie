"""
Fork-owned schemas for the MCP server's OAuth clients, consent, connected apps and API token write grants
(docs/ai/PHASE3.md §3-4, §6). The token endpoint's RFC 6749 responses aren't modelled here: they aren't Mealie API
objects and keep their snake_case names.
"""

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import UUID4, ConfigDict, Field, field_validator, model_validator

from mealie.schema._mealie import MealieModel
from mealie.services.oauth.urls import check_redirect_uri

MAX_REDIRECT_URIS = 10


class McpScope(StrEnum):
    read = "mcp:read"
    write = "mcp:write"


# ==========================================
# OAuth clients


class McpClientBase(MealieModel):
    name: str = Field(min_length=1, max_length=100)
    redirect_uris: list[str]
    """Matched exactly, except that loopback `http` URIs match on any port"""
    pkce_optional: bool = False
    """Lets a confidential client leave out PKCE (an OAuth 2.0 client, e.g. Home Assistant)"""
    allow_write_scope: bool = False
    """Whether the client may ask for `mcp:write`. Each user still chooses whether to allow it."""

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Name cannot be empty")
        return value

    @field_validator("redirect_uris")
    @classmethod
    def _check_redirect_uris(cls, value: list[str]) -> list[str]:
        # counted here rather than with `Field` limits, which would make the generated TypeScript a union of tuples
        if not 1 <= len(value) <= MAX_REDIRECT_URIS:
            raise ValueError(f"Register between 1 and {MAX_REDIRECT_URIS} redirect URIs")
        return list(dict.fromkeys(check_redirect_uri(uri) for uri in value))


class McpClientCreate(McpClientBase):
    is_confidential: bool = True
    """A confidential client has a secret. Public clients (no secret) must use PKCE."""

    @model_validator(mode="after")
    def _pkce_optional_needs_a_secret(self) -> Self:
        # OAuth 2.1 §7.5.1: only a client that authenticates may go without PKCE
        if self.pkce_optional and not self.is_confidential:
            raise ValueError("PKCE can only be optional for a confidential client")
        return self


class McpClientUpdate(McpClientBase):
    """Every editable field. Whether the client is confidential can't change; delete it and add it again."""


class McpClientOut(MealieModel):
    id: UUID4
    group_id: UUID4
    name: str
    client_id: str
    is_confidential: bool
    pkce_optional: bool
    allow_write_scope: bool
    redirect_uris: list[str]
    created_by: UUID4 | None = None
    created_at: datetime | None = None
    last_used_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class McpClientCreated(McpClientOut):
    """Only returned when the client is created: the secret is shown once, and only its hash is kept"""

    client_secret: str | None = None


class McpClientSecretOut(MealieModel):
    """A new client secret, shown once"""

    client_id: str
    client_secret: str


# ==========================================
# Consent


class McpOAuthRequestOut(MealieModel):
    """A pending authorization request, as the consent page shows it"""

    client_name: str
    scopes: list[McpScope]
    """The scopes the client asked for, among those it's allowed"""
    writes_offered: bool
    """Whether to offer "Allow changes": the client asked for `mcp:write` and is allowed it"""
    redirect_host: str
    """Where the browser goes next, so a lookalike stands out"""
    expires_at: datetime


class McpOAuthDecision(MealieModel):
    approve: bool
    allow_writes: bool = False


class McpOAuthDecisionOut(MealieModel):
    redirect_to: str
    """Where the SPA sends the browser: the client's redirect URI with a code, or with `error=access_denied`"""


# ==========================================
# Connected apps and API tokens


class McpConnectionOut(MealieModel):
    """An app the user has connected with OAuth, and not disconnected"""

    client_id: UUID4
    """The client's `id`"""
    client_name: str
    scopes: list[McpScope]
    created_at: datetime
    """When the user first approved this connection"""
    last_used_at: datetime | None = None


class McpApiTokenGrantOut(MealieModel):
    token_id: int
    allow_writes: bool
    """Whether AI assistants using this API token may make changes (shopping list, meal plan)"""


class McpApiTokenGrantUpdate(MealieModel):
    allow_writes: bool
