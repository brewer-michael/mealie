"""The group's MCP OAuth clients, for group managers (docs/ai/PHASE3.md §4 "Clients", §6)"""

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import UUID4, ValidationError

from mealie.routes._base import BaseUserController, controller
from mealie.schema.mcp.mcp_oauth import (
    McpClientCreate,
    McpClientCreated,
    McpClientOut,
    McpClientSecretOut,
    McpClientUpdate,
)
from mealie.schema.response import ErrorResponse
from mealie.services.oauth.clients import McpClientError, McpClientService, home_assistant_preset

router = APIRouter(prefix="/groups/mcp", tags=["Groups: MCP Clients"])


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, detail=ErrorResponse.respond(message="Not found."))


def _bad_request(message: str) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, detail=ErrorResponse.respond(message=message))


@controller(router)
class GroupMcpClientsController(BaseUserController):
    @property
    def service(self) -> McpClientService:
        return McpClientService(self.session, self.group_id)

    @router.get("/presets/home-assistant", response_model=McpClientCreate)
    def get_home_assistant_preset(
        self, home_assistant_url: str | None = Query(None, alias="homeAssistantUrl")
    ) -> McpClientCreate:
        """
        A client for Home Assistant, to review and create: confidential, PKCE optional (Home Assistant sends
        none), read-only, with both of its redirect URIs. `homeAssistantUrl` (e.g. `http://192.168.1.20:8123`)
        replaces the default `http://homeassistant.local:8123` in the second one.
        """
        self.checks.can_manage()

        try:
            return home_assistant_preset(home_assistant_url)
        except ValidationError as e:
            raise _bad_request(e.errors()[0]["msg"].removeprefix("Value error, ")) from None

    @router.get("/clients", response_model=list[McpClientOut])
    def get_all(self) -> list[McpClientOut]:
        self.checks.can_manage()
        return self.service.get_all()

    @router.post("/clients", response_model=McpClientCreated, status_code=status.HTTP_201_CREATED)
    def create_one(self, data: McpClientCreate) -> McpClientCreated:
        """Registers a client. A confidential client's `clientSecret` is in this response only."""
        self.checks.can_manage()
        return self.service.create(data, self.user.id)

    @router.get("/clients/{item_id}", response_model=McpClientOut)
    def get_one(self, item_id: UUID4) -> McpClientOut:
        self.checks.can_manage()
        if (client := self.service.get_one(item_id)) is None:
            raise _not_found()
        return client

    @router.put("/clients/{item_id}", response_model=McpClientOut)
    def update_one(self, item_id: UUID4, data: McpClientUpdate) -> McpClientOut:
        """Every field but `isConfidential`, which can't change. Taking away writes applies to issued tokens too."""
        self.checks.can_manage()
        try:
            client = self.service.update(item_id, data)
        except McpClientError as e:
            raise _bad_request(str(e)) from None
        if client is None:
            raise _not_found()
        return client

    @router.post("/clients/{item_id}/rotate-secret", response_model=McpClientSecretOut)
    def rotate_secret(self, item_id: UUID4) -> McpClientSecretOut:
        """A new secret for a confidential client, shown once. The old one stops working."""
        self.checks.can_manage()
        try:
            secret = self.service.rotate_secret(item_id)
        except McpClientError as e:
            raise _bad_request(str(e)) from None
        if secret is None:
            raise _not_found()
        return secret

    @router.delete("/clients/{item_id}", response_model=McpClientOut)
    def delete_one(self, item_id: UUID4) -> McpClientOut:
        """Deletes a client and revokes all its tokens"""
        self.checks.can_manage()
        if (client := self.service.delete(item_id)) is None:
            raise _not_found()
        return client
