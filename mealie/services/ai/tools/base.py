"""
The pieces every AI tool is built from (docs/ai/PHASE1.md §5): its argument and result models, the
context it runs in, and the `AITool` definition the registry, the REST endpoints and (later) the MCP
server and planner agent all share.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from functools import cached_property
from typing import Any, ClassVar

from dateutil.tz import tzlocal
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaValue
from pydantic_core import CoreSchema, core_schema
from starlette.concurrency import run_in_threadpool

from mealie.lang.providers import Translator
from mealie.repos.all_repositories import get_repositories
from mealie.repos.repository_factory import AllRepositories
from mealie.schema.household.household import HouseholdInDB
from mealie.schema.user.user import DEFAULT_INTEGRATION_ID, PrivateUser
from mealie.services.event_bus_service.event_bus_service import EventBusService
from mealie.services.event_bus_service.event_types import EventDocumentDataBase, EventTypes

from .speech import first_sentences


class ToolError(Exception):
    """A tool couldn't do what it was asked. The message is ours, written to be shown or read to the user."""


class ToolNotFoundError(ToolError):
    """Something the arguments name doesn't exist for this user: a recipe, a shopping list, a step"""


class ToolArgs(BaseModel):
    """Base for tool arguments: unknown fields are rejected, so a misnamed argument isn't silently ignored"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ToolResult(BaseModel):
    """Base for tool results: `speech` for a voice assistant, plus structured data with ids for follow-up calls"""

    # a backstop: tools keep their speech within these by putting names and items in it through `spoken`
    max_speech_sentences: ClassVar[int | None] = 2
    max_speech_chars: ClassVar[int] = 300

    speech: str = Field(
        description="At most two short sentences to read aloud: plain words, no markdown, no URLs",
    )

    @field_validator("speech")
    @classmethod
    def _plain_speech(cls, value: str) -> str:
        return first_sentences(value, cls.max_speech_chars, cls.max_speech_sentences)


def local_today() -> date:
    """Today on the server's clock, as upstream's `GET /households/mealplans/today` reckons it"""
    return datetime.now(tzlocal()).date()


@dataclass
class ToolContext:
    """
    Who a tool runs as, and the repositories it may use.

    Building one never touches the database, since callers build it on the event loop: there, a query that has
    to wait for a pooled connection stops every other request, including the ones that would give a connection
    back. The database is only used by tool bodies, which `run_blocking` runs in a worker thread, and the
    attributes below that load on first use are only for those bodies.
    """

    repos: AllRepositories
    """Scoped to the caller's group and household"""
    user: PrivateUser
    translator: Translator
    event_bus: EventBusService | None = None
    """Publishes the same events the matching REST endpoints do; without one, writes publish nothing"""
    integration_id: str = DEFAULT_INTEGRATION_ID

    @cached_property
    def household(self) -> HouseholdInDB:
        """The caller's household, loaded on first use"""
        household = self.repos.households.get_one(self.user.household_id)
        if household is None:
            raise ToolNotFoundError("I couldn't find your household.")
        return household

    @cached_property
    def group_repos(self) -> AllRepositories:
        """
        Scoped to the caller's group, across households: recipes, like upstream's recipe endpoints, are
        shared by every household in a group
        """
        return get_repositories(self.repos.session, group_id=self.user.group_id, household_id=None)

    def today(self) -> date:
        return local_today()

    def publish_event(self, event_type: EventTypes, document_data: EventDocumentDataBase, message: str = "") -> None:
        if self.event_bus is None:
            return

        self.event_bus.dispatch(
            integration_id=self.integration_id,
            group_id=self.user.group_id,
            household_id=self.user.household_id,
            event_type=event_type,
            document_data=document_data,
            message=message,
        )


class _ToolSchemaGenerator(GenerateJsonSchema):
    """JSON schema for tool arguments, kept simple for language models"""

    def nullable_schema(self, schema: core_schema.NullableSchema) -> JsonSchemaValue:
        # an optional argument is one that's left out of `required`; a `null` alternative adds a union to
        # reason about and nothing to use
        return self.generate_inner(schema["schema"])

    def default_schema(self, schema: core_schema.WithDefaultSchema) -> JsonSchemaValue:
        # for the same reason, a `null` default would only contradict the argument's type
        json_schema = super().default_schema(schema)
        if "default" in json_schema and json_schema["default"] is None:
            del json_schema["default"]
        return json_schema

    def field_title_should_be_set(self, schema: CoreSchema | Any) -> bool:
        return False


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Replaces `$ref`s with their definitions: not every tool client resolves them"""
    definitions: dict[str, Any] = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, list):
            return [resolve(item) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            target = {k: v for k, v in definitions[node["$ref"].rsplit("/", 1)[-1]].items() if k != "title"}
            return resolve(target) | {k: resolve(v) for k, v in node.items() if k != "$ref"}
        return {k: resolve(v) for k, v in node.items()}

    return resolve(schema)


def tool_input_schema(args: type[BaseModel]) -> dict[str, Any]:
    """A self-contained JSON schema for a tool's arguments, with no titles, `$ref`s or null unions"""
    schema = args.model_json_schema(schema_generator=_ToolSchemaGenerator)
    schema.pop("title", None)
    schema.pop("description", None)
    return _inline_refs(schema)


def run_blocking[ArgsT: ToolArgs, ResultT: ToolResult](
    fn: Callable[[ToolContext, ArgsT], ResultT],
) -> Callable[[ToolContext, ArgsT], Awaitable[ResultT]]:
    """
    Wraps a tool body that works through the (synchronous) repositories as an async handler that runs it
    in a worker thread, so it doesn't hold up the event loop it's awaited on
    """

    def run(ctx: ToolContext, args: ArgsT) -> ResultT:
        try:
            return fn(ctx, args)
        finally:
            # repositories commit their own writes, so this only ends the read transaction: the connection goes
            # back to the pool now, from this thread, rather than when the caller closes the session on the loop
            ctx.repos.session.rollback()

    async def handler(ctx: ToolContext, args: ArgsT) -> ResultT:
        return await run_in_threadpool(run, ctx, args)

    return handler


@dataclass(frozen=True)
class AITool[ArgsT: ToolArgs, ResultT: ToolResult]:
    """One kitchen action, defined once and exposed over REST now and over MCP and to the planner later"""

    name: str
    description: str
    """Written for a language model: what the tool does, when to use it and what it returns"""
    args: type[ArgsT]
    result: type[ResultT]
    writes: bool
    """Whether the tool changes anything. Write tools never delete."""
    handler: Callable[[ToolContext, ArgsT], Awaitable[ResultT]]
    """Awaited on the event loop, so any database work in it must run in a worker thread (see `run_blocking`)"""

    @cached_property
    def input_schema(self) -> dict[str, Any]:
        return tool_input_schema(self.args)
