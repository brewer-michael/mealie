"""
The AI tool registry as MCP tools (docs/ai/PHASE3.md §1): what `tools/list` shows a caller, and how `tools/call`
runs a tool for them.

Tools run as they do over REST (`mealie/routes/ai/controller_tools.py`): the tool's pydantic model validates the
arguments, and the handler gets a `ToolContext` for the caller's group and household that does all its database work
in worker threads. Only the way outcomes are reported differs. Every failure a user could hear about (an unknown tool,
invalid arguments, something that doesn't exist, a missing write grant, a slow answer, a busy server) is a result with
`isError` and a sentence to read aloud, never a JSON-RPC or HTTP error, which MCP clients would turn into a generic
error.
"""

import asyncio
import json
import time
from collections.abc import Callable, Coroutine
from typing import Any

import mcp.types as types
from fastapi import BackgroundTasks
from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError

from mealie.core.root_logger import get_logger
from mealie.db import db_setup
from mealie.lang.providers import Translator
from mealie.repos.all_repositories import get_repositories
from mealie.services.ai.tools import AITool, ToolContext, ToolError, ToolNotFoundError, ToolResult, all_tools, get_tool
from mealie.services.event_bus_service.event_bus_service import EventBusService

from .auth import McpPrincipal

TOOL_TIMEOUT = 4.0
"""Seconds from a request's arrival until its caller is told the tool is taking too long. Home Assistant waits 5
seconds per request."""
MIN_TOOL_TIME = 1.0
"""Seconds a tool gets however long its request took to reach it"""
MAX_RUNNING_TOOLS = 16
"""Tools one process runs at once, counting those still running after timing out. A call beyond that is turned away
rather than queued, where it would only time out."""

TIMEOUT_MESSAGE = "Mealie took too long to answer. Try again."
WRITE_TIMEOUT_MESSAGE = "Mealie is still saving that, and it may still go through. Check before asking again."
BUSY_MESSAGE = "Mealie is busy right now. Try again in a moment."
WRITE_NOT_ALLOWED_MESSAGE = "This connection to Mealie isn't allowed to make changes. Allow changes for it in Mealie."
INTERNAL_ERROR_MESSAGE = "Something went wrong in Mealie. Try again later."

TITLES = {
    "search_recipes": "Search recipes",
    "get_recipe": "Get a recipe",
    "get_cooking_step": "Read a cooking step",
    "suggest_from_ingredients": "Suggest recipes from ingredients",
    "whats_planned": "What's planned",
    "get_shopping_list": "Read the shopping list",
    "recipe_card_queue": "Recipe card queue",
    "add_to_shopping_list": "Add to the shopping list",
    "plan_meal": "Plan a meal",
}

logger = get_logger()

_running: set[asyncio.Task[Any]] = set()
"""Tools being run, which can outlive the requests that started them"""
_publishing: set[asyncio.Task[Any]] = set()
"""Events of finished write tools being sent"""


# ==========================================
# tools/list


def tool_definition(tool: AITool[Any, Any]) -> types.Tool:
    if tool.writes:
        # no tool deletes or overwrites anything
        annotations = types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
    else:
        annotations = types.ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)

    return types.Tool(
        name=tool.name,
        title=TITLES.get(tool.name) or tool.name.replace("_", " ").capitalize(),
        description=tool.description,
        inputSchema=tool.input_schema,
        annotations=annotations,
    )


def list_tools(principal: McpPrincipal) -> list[types.Tool]:
    """The tools `principal` may call, in the registry's order: write tools only with a write grant. No database."""
    return [tool_definition(tool) for tool in all_tools() if principal.can_write or not tool.writes]


# ==========================================
# tools/call


def _result_text(payload: dict[str, Any]) -> list[types.ContentBlock]:
    return [types.TextContent(type="text", text=json.dumps(payload, separators=(",", ":"), ensure_ascii=False))]


def error_result(speech: str, error: str, **details: Any) -> types.CallToolResult:
    """A failure the caller can read aloud, shaped like a tool's result: `speech` first"""
    return types.CallToolResult(content=_result_text({"speech": speech, "error": error, **details}), isError=True)


def tool_result(result: ToolResult) -> types.CallToolResult:
    """
    The tool's result as compact JSON in one text block (`speech` first, then the data with ids for follow-up
    calls), and no `structuredContent`: Home Assistant sends the whole result to the model, so that would send it twice
    """
    return types.CallToolResult(content=[types.TextContent(type="text", text=result.model_dump_json())])


def _invalid_arguments_speech(errors: list[dict[str, Any]]) -> str:
    first = errors[0]
    where = ".".join(str(part) for part in first.get("loc", ()))
    message = str(first.get("msg", "")).removeprefix("Value error, ")
    return f"I couldn't use those details. {where}: {message}" if where else f"I couldn't use those details. {message}"


def _detach[T](coroutine: Coroutine[Any, Any, T], tasks: set[asyncio.Task[Any]], name: str) -> asyncio.Task[T]:
    """Runs `coroutine` in a task kept in `tasks` until it's done, so it isn't garbage collected while it runs"""
    task = asyncio.get_running_loop().create_task(coroutine, name=name)
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return task


async def finish_detached(timeout: float) -> None:
    """
    Waits up to `timeout` seconds for the tools and event deliveries that outlived their requests, for when the
    server stops: the event loop then cancels what's left, and a cancelled tool's outcome and events are lost (its
    worker thread runs on regardless)
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    # a tool that finishes meanwhile can start sending its events, so look again after every wait
    while pending := {task for task in _running | _publishing if task.get_loop() is loop and not task.done()}:
        if (remaining := deadline - loop.time()) <= 0:
            names = ", ".join(sorted(task.get_name() for task in pending))
            logger.warning(f"Stopping with {len(pending)} MCP task(s) unfinished: {names}")
            return
        await asyncio.wait(pending, timeout=remaining)


def _caller(principal: McpPrincipal) -> str:
    """Who called, for the logs: never the token. A client's name is whatever a group manager typed, so it's quoted."""
    return f"for user {principal.user.id} via {principal.client_name!r}"


async def _publish(background: BackgroundTasks, tool: AITool[Any, Any], principal: McpPrincipal) -> None:
    try:
        await background()
    except Exception:
        # over REST, the server logs a failed background task; here nothing else would
        logger.exception(f"MCP tool {tool.name} couldn't publish its events, {_caller(principal)}")


async def _run(tool: AITool[Any, Any], args: Any, principal: McpPrincipal, translator: Translator) -> ToolResult:
    background = BackgroundTasks()
    # Like the REST endpoint, nothing here touches the database: the context loads what it needs in the tool's worker
    # threads (PHASE1.md §5). Those threads also end the session's transaction, so closing it on the way out has no
    # connection to give back.
    session = db_setup.SessionLocal()
    ctx = ToolContext(
        repos=get_repositories(session, group_id=principal.group_id, household_id=principal.household_id),
        user=principal.user,
        translator=translator,
        event_bus=EventBusService(background, session, translator),
        integration_id=principal.integration_id,
    )
    close = True
    try:
        result = await tool.handler(ctx, args)
    except asyncio.CancelledError:
        # Only when the server stops before the tool finished. A worker thread the tool waits on can't be stopped and
        # may still be using the session, so the session is left to that thread, which gives the connection back
        # when it ends its transaction, rather than closed from under it (as `session_context()` would).
        close = False
        raise
    finally:
        if close:
            session.close()

    if background.tasks:
        # notifications and webhooks go out next to the answer, not before it, as REST sends them after its response
        _detach(_publish(background, tool, principal), _publishing, f"MCP events of {tool.name}")
    return result


def _log_late_outcome(
    tool: AITool[Any, Any], principal: McpPrincipal, started: float, after: str
) -> Callable[[asyncio.Task[Any]], None]:
    """A done callback for a tool's task, logging how it ended once nobody waits for it"""

    def log(task: asyncio.Task[Any]) -> None:
        elapsed = time.perf_counter() - started
        error = None if task.cancelled() else task.exception()
        if error is not None and not isinstance(error, ToolError):
            logger.error(f"MCP tool {tool.name} failed after {after}, {_caller(principal)}", exc_info=error)
            return

        outcome = "cancelled" if task.cancelled() else (f"failed ({type(error).__name__})" if error else "succeeded")
        logger.warning(f"MCP tool {tool.name} {outcome} after {after} ({elapsed:.1f} s), {_caller(principal)}")

    return log


async def _call(
    principal: McpPrincipal, tool: AITool[Any, Any], arguments: dict[str, Any], translator: Translator, budget: float
) -> tuple[types.CallToolResult, str]:
    if tool.writes and not principal.can_write:
        return error_result(WRITE_NOT_ALLOWED_MESSAGE, "write_not_allowed"), "refused (no write grant)"

    try:
        args = tool.args.model_validate(arguments)
    except ValidationError as e:
        errors = jsonable_encoder(e.errors(include_url=False, include_context=False))
        return error_result(_invalid_arguments_speech(errors), "invalid_arguments", errors=errors), "invalid arguments"

    if len(_running) >= MAX_RUNNING_TOOLS:
        return error_result(BUSY_MESSAGE, "busy"), "refused (busy)"

    # The tool runs in its own task, so the answer can come at the deadline even while the tool waits on a worker
    # thread (a thread can't be cancelled, so awaiting it directly would hold the answer until the thread finishes).
    # A tool nobody waits for any more is left to finish, and its outcome is logged.
    started = time.perf_counter()
    task = _detach(_run(tool, args, principal, translator), _running, f"MCP tool {tool.name}")
    try:
        done, _ = await asyncio.wait({task}, timeout=budget)
    except BaseException:
        # the request was cancelled: its client went away, or the server is stopping
        task.add_done_callback(_log_late_outcome(tool, principal, started, "its request was cancelled"))
        raise

    if not done:
        task.add_done_callback(_log_late_outcome(tool, principal, started, "timing out"))
        if tool.writes:
            # the change may still be made: "try again" would make it twice
            result = error_result(WRITE_TIMEOUT_MESSAGE, "timeout_pending", may_have_applied=True)
            return result, "timed out (may still apply)"
        return error_result(TIMEOUT_MESSAGE, "timeout"), "timed out"

    try:
        return tool_result(task.result()), "ok"
    except ToolNotFoundError as e:
        return error_result(str(e), "not_found"), "not found"
    except ToolError as e:
        return error_result(str(e), "tool_error"), "tool error"


async def call_tool(
    principal: McpPrincipal,
    name: str,
    arguments: dict[str, Any],
    translator: Translator,
    arrived: float | None = None,
) -> types.CallToolResult:
    """
    Runs the tool `name` as `principal`, answering within `TOOL_TIMEOUT` of `arrived` (when the request came in, as
    a `time.monotonic()`; now if not given) while giving the tool at least `MIN_TOOL_TIME`. Always returns a result:
    failures come back with `isError` and a message for the user, never as exceptions.
    """
    started = time.perf_counter()
    waited = time.monotonic() - arrived if arrived is not None else 0.0
    budget = max(TOOL_TIMEOUT - waited, MIN_TOOL_TIME)

    tool = get_tool(name)
    if tool is None:
        result = error_result(f"Mealie doesn't have a tool called {name[:64]}.", "unknown_tool")
        outcome = "unknown tool"
    else:
        try:
            result, outcome = await _call(principal, tool, arguments, translator, budget)
        except Exception:
            logger.exception(f"MCP tool {tool.name} failed, {_caller(principal)}")
            result, outcome = error_result(INTERNAL_ERROR_MESSAGE, "internal_error"), "failed"

    tool_name = tool.name if tool else repr(name[:64])
    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.info(f"MCP tool {tool_name} {_caller(principal)}: {outcome} ({elapsed_ms:.0f} ms)")
    return result
