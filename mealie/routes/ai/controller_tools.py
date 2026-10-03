"""
The AI tool registry over REST (docs/ai/PHASE1.md §5). Tools run as the logged-in user, scoped to their
group and household, with the same checks as the REST endpoints they mirror.
"""

from typing import Any

from fastapi import APIRouter, Body, HTTPException, status
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ValidationError

from mealie.routes._base import controller
from mealie.routes._base.base_controllers import BaseCrudController
from mealie.schema.response import ErrorResponse
from mealie.services.ai.tools import AITool, ToolContext, ToolError, ToolNotFoundError, all_tools, get_tool

router = APIRouter(prefix="/ai/tools", tags=["AI: Tools"])


class AIToolInfo(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    writes: bool


class AIToolCallResult(BaseModel):
    tool: str
    result: dict[str, Any]


@controller(router)
class AIToolsController(BaseCrudController):
    def _tool(self, name: str) -> AITool[Any, Any]:
        tool = get_tool(name)
        if tool is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=ErrorResponse.respond(f"Unknown tool: {name}"))
        return tool

    @router.get("", response_model=list[AIToolInfo])
    def list_tools(self) -> list[AIToolInfo]:
        """Every tool, with a JSON schema for its arguments and whether it changes anything"""
        return [
            AIToolInfo(name=t.name, description=t.description, input_schema=t.input_schema, writes=t.writes)
            for t in all_tools()
        ]

    @router.post("/{name}", response_model=AIToolCallResult)
    async def call_tool(self, name: str, arguments: dict[str, Any] | None = Body(default=None)) -> AIToolCallResult:
        """
        Runs a tool with the JSON body as its arguments. Invalid arguments are a 422 listing pydantic's
        errors; something the arguments name that doesn't exist (a recipe, a shopping list) is a 404 whose
        message can be read to the user.
        """
        tool = self._tool(name)
        try:
            args = tool.args.model_validate(arguments or {})
        except ValidationError as e:
            errors = jsonable_encoder(e.errors(include_url=False, include_context=False))
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=errors) from e

        # this runs on the event loop, so nothing here may touch the database (not even `self.household` or
        # `self.group`): the context loads what it needs in the tool's worker thread
        ctx = ToolContext(
            repos=self.repos,
            user=self.user,
            translator=self.translator,
            event_bus=self.event_bus,
            integration_id=self.integration_id,
        )
        try:
            result = await tool.handler(ctx, args)
        except ToolNotFoundError as e:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=ErrorResponse.respond(str(e))) from e
        except ToolError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=ErrorResponse.respond(str(e))) from e

        return AIToolCallResult(tool=tool.name, result=result.model_dump(mode="json"))
