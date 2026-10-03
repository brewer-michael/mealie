"""
The user's own MCP access (docs/ai/PHASE3.md §3, §6): apps connected with OAuth, and which of their API tokens
may make changes
"""

from fastapi import APIRouter, HTTPException, status
from pydantic import UUID4

from mealie.routes._base import BaseUserController, controller
from mealie.schema.mcp.mcp_oauth import McpApiTokenGrantOut, McpApiTokenGrantUpdate, McpConnectionOut
from mealie.schema.response import ErrorResponse
from mealie.services.oauth.clients import McpConnectionService

router = APIRouter(prefix="/users/self/mcp", tags=["Users: MCP"])


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, detail=ErrorResponse.respond(message="Not found."))


@controller(router)
class UserMcpController(BaseUserController):
    @property
    def service(self) -> McpConnectionService:
        return McpConnectionService(self.session, self.user)

    # ==========================================
    # Connected apps

    @router.get("/connections", response_model=list[McpConnectionOut])
    def get_connections(self) -> list[McpConnectionOut]:
        """Apps you've connected with OAuth, with their scopes, when you connected them and when they were last used"""
        return self.service.get_connections()

    @router.delete("/connections/{client_id}", response_model=McpConnectionOut)
    def disconnect(self, client_id: UUID4) -> McpConnectionOut:
        """Disconnects an app (by its client's `id`): every token you gave it stops working"""
        service = self.service
        connection = next((c for c in service.get_connections() if c.client_id == client_id), None)
        if connection is None or not service.disconnect(client_id):
            raise _not_found()
        return connection

    # ==========================================
    # API token write grants

    @router.get("/api-tokens", response_model=list[McpApiTokenGrantOut])
    def get_api_token_grants(self) -> list[McpApiTokenGrantOut]:
        """Each of your API tokens, and whether AI assistants using it may make changes"""
        return self.service.get_api_token_grants()

    @router.get("/api-tokens/{token_id}", response_model=McpApiTokenGrantOut)
    def get_api_token_grant(self, token_id: int) -> McpApiTokenGrantOut:
        if (grant := self.service.get_api_token_grant(token_id)) is None:
            raise _not_found()
        return grant

    @router.put("/api-tokens/{token_id}", response_model=McpApiTokenGrantOut)
    def update_api_token_grant(self, token_id: int, data: McpApiTokenGrantUpdate) -> McpApiTokenGrantOut:
        """Lets AI assistants using this API token make changes (shopping list, meal plan), or stops them"""
        if (grant := self.service.set_api_token_grant(token_id, data.allow_writes)) is None:
            raise _not_found()
        return grant
