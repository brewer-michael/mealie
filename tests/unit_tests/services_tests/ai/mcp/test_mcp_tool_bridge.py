"""
The MCP server's tool bridge and endpoint helpers (docs/ai/PHASE3.md §1-2), with stand-in tools: definitions,
result and error shapes, the write grant, the time limit, the limit on running tools, shutdown and logging.
"""

import asyncio
import json
import logging
import threading
import time
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import mcp.types as types
import pytest
import sqlalchemy as sa
from pydantic import Field, model_validator

from mealie.db import db_setup
from mealie.lang.providers import get_locale_provider
from mealie.services.ai.mcp import tool_bridge
from mealie.services.ai.mcp.auth import McpPrincipal
from mealie.services.ai.mcp.endpoint import _bearer_token, is_same_origin
from mealie.services.ai.mcp.server import REQUEST_STATE_KEY, McpRequestState, create_mcp_server, request_state
from mealie.services.ai.tools import AITool, ToolArgs, ToolContext, ToolError, ToolNotFoundError, ToolResult, registry
from mealie.services.ai.tools.base import run_blocking
from mealie.services.ai.tools.speech import first_sentences


class EchoArgs(ToolArgs):
    word: str = Field(min_length=1)
    times: int = Field(default=1, ge=1, le=3)

    @model_validator(mode="after")
    def _not_shouting(self) -> EchoArgs:
        if self.word.isupper():
            raise ValueError("Say it quietly")
        return self


class EchoResult(ToolResult):
    echoed: list[str]
    note: str | None = None


class LogRecorder:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def _record(self, level: str):
        def record(message: str, *args: Any, **kwargs: Any) -> None:
            self.records.append((level, message))

        return record

    def __getattr__(self, level: str):
        return self._record(level)


def principal(can_write: bool = False) -> McpPrincipal:
    user = SimpleNamespace(id=uuid4(), group_id=uuid4(), household_id=uuid4())
    return McpPrincipal(
        user=user,  # type: ignore[arg-type]
        group_id=user.group_id,
        household_id=user.household_id,
        client_name="Kitchen",
        client_id="mmcp_test",
        api_token_id=None,
        scopes=frozenset({"mcp:read", "mcp:write"} if can_write else {"mcp:read"}),
        can_write=can_write,
    )


def register(
    monkeypatch: pytest.MonkeyPatch, handler: Any, *, name: str = "echo", writes: bool = False
) -> AITool[Any, Any]:
    tool = AITool(
        name=name, description="Echoes a word", args=EchoArgs, result=EchoResult, writes=writes, handler=handler
    )
    monkeypatch.setitem(registry._TOOLS, name, tool)
    return tool


async def echo(ctx: ToolContext, args: EchoArgs) -> EchoResult:
    return EchoResult(speech=f"You said {args.word}.", echoed=[args.word] * args.times)


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> LogRecorder:
    recorder = LogRecorder()
    monkeypatch.setattr(tool_bridge, "logger", recorder)
    return recorder


async def call(
    name: str, arguments: dict[str, Any], who: McpPrincipal | None = None, arrived: float | None = None
) -> types.CallToolResult:
    return await tool_bridge.call_tool(who or principal(), name, arguments, get_locale_provider("en-US"), arrived)


async def wait_for(condition: Any, seconds: float = 3) -> None:
    for _ in range(int(seconds / 0.02)):
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out waiting")


def data(result: types.CallToolResult) -> dict[str, Any]:
    assert result.structuredContent is None and len(result.content) == 1
    content = result.content[0]
    assert isinstance(content, types.TextContent)
    return json.loads(content.text)


# ==========================================
# Definitions


def test_definitions():
    for tool in registry.all_tools():
        definition = tool_bridge.tool_definition(tool)
        assert definition.name == tool.name
        assert definition.title == tool_bridge.TITLES[tool.name], "every tool needs a title"
        assert definition.description == tool.description
        assert definition.inputSchema == tool.input_schema
        assert definition.outputSchema is None

        annotations = definition.annotations.model_dump(exclude_none=True)  # type: ignore[union-attr]
        if tool.writes:
            assert annotations == {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}
        else:
            assert annotations == {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}


def test_titles_fall_back_to_the_name(monkeypatch: pytest.MonkeyPatch):
    tool = register(monkeypatch, echo, name="echo_twice")
    assert tool_bridge.tool_definition(tool).title == "Echo twice"


def test_write_tools_are_listed_only_with_a_grant():
    names = [tool.name for tool in registry.all_tools()]
    reads = [tool.name for tool in registry.all_tools() if not tool.writes]
    assert [tool.name for tool in tool_bridge.list_tools(principal(can_write=True))] == names
    assert [tool.name for tool in tool_bridge.list_tools(principal(can_write=False))] == reads
    assert reads != names


# ==========================================
# Results


def test_results_are_compact_json_with_speech_first():
    result = tool_bridge.tool_result(EchoResult(speech="Hi. Bye.", echoed=["é"], note=None))
    assert result.isError is False
    assert result.content[0].text == '{"speech":"Hi. Bye.","echoed":["é"],"note":null}'  # type: ignore[union-attr]


def test_error_results():
    result = tool_bridge.error_result("Nope.", "tool_error", errors=[{"loc": ["x"]}])
    assert result.isError is True
    assert result.content[0].text == '{"speech":"Nope.","error":"tool_error","errors":[{"loc":["x"]}]}'  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_a_call(monkeypatch: pytest.MonkeyPatch, log: LogRecorder):
    register(monkeypatch, echo)
    who = principal()
    result = await call("echo", {"word": "hello", "times": 2}, who)
    assert result.isError is False
    assert data(result) == {"speech": "You said hello.", "echoed": ["hello", "hello"], "note": None}

    [(level, message)] = log.records
    assert level == "info"
    assert message.startswith(f"MCP tool echo for user {who.user.id} via 'Kitchen': ok (")


@pytest.mark.asyncio
async def test_client_names_are_quoted_in_the_logs(monkeypatch: pytest.MonkeyPatch, log: LogRecorder):
    """A client's name is free text a group manager typed: it can't start a log line of its own"""
    register(monkeypatch, echo)
    who = principal()
    who = McpPrincipal(**{**who.__dict__, "client_name": "Kitchen\nERROR forged"})
    await call("echo", {"word": "hi"}, who)
    [(_, message)] = log.records
    assert "\n" not in message and "via 'Kitchen\\nERROR forged'" in message


# ==========================================
# Failures


@pytest.mark.asyncio
async def test_unknown_tool(log: LogRecorder):
    result = await call("x" * 100, {})
    assert result.isError is True
    assert data(result) == {"speech": f"Mealie doesn't have a tool called {'x' * 64}.", "error": "unknown_tool"}
    assert log.records[0][1].startswith(f"MCP tool '{'x' * 64}' for user")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments, loc, speech",
    [
        ({}, ["word"], "I couldn't use those details. word: Field required"),
        (
            {"word": "hi", "times": 4},
            ["times"],
            "I couldn't use those details. times: Input should be less than or equal to 3",
        ),
        ({"word": "HI"}, [], "I couldn't use those details. Say it quietly"),
        ({"word": "hi", "loud": True}, ["loud"], "I couldn't use those details. loud: Extra inputs are not permitted"),
    ],
)
async def test_invalid_arguments(monkeypatch: pytest.MonkeyPatch, arguments: dict, loc: list, speech: str):
    register(monkeypatch, echo)
    result = await call("echo", arguments)
    assert result.isError is True
    body = data(result)
    assert (body["speech"], body["error"]) == (speech, "invalid_arguments")
    assert [e["loc"] for e in body["errors"]] == [loc]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error, code, speech",
    [
        (
            ToolNotFoundError("I couldn't find a recipe called cake."),
            "not_found",
            "I couldn't find a recipe called cake.",
        ),
        (ToolError("That list is full."), "tool_error", "That list is full."),
        (RuntimeError("secret path /var/lib/mealie"), "internal_error", tool_bridge.INTERNAL_ERROR_MESSAGE),
    ],
)
async def test_tool_errors(monkeypatch: pytest.MonkeyPatch, log: LogRecorder, error: Exception, code: str, speech: str):
    async def failing(ctx: ToolContext, args: EchoArgs) -> EchoResult:
        raise error

    register(monkeypatch, failing)
    result = await call("echo", {"word": "hi"})
    assert result.isError is True
    assert data(result) == {"speech": speech, "error": code}
    assert ("exception" in [level for level, _ in log.records]) is (code == "internal_error")


@pytest.mark.asyncio
async def test_writes_need_a_grant(monkeypatch: pytest.MonkeyPatch):
    calls: list[EchoArgs] = []

    async def write(ctx: ToolContext, args: EchoArgs) -> EchoResult:
        calls.append(args)
        return EchoResult(speech="Done.", echoed=[])

    register(monkeypatch, write, writes=True)
    refused = await call("echo", {"word": "hi"}, principal(can_write=False))
    assert data(refused) == {"speech": tool_bridge.WRITE_NOT_ALLOWED_MESSAGE, "error": "write_not_allowed"}
    assert calls == []

    allowed = await call("echo", {"word": "hi"}, principal(can_write=True))
    assert allowed.isError is False and len(calls) == 1


@pytest.mark.asyncio
async def test_the_context(monkeypatch: pytest.MonkeyPatch):
    """The tool runs as the principal, scoped to its group and household, publishing as `mcp:<client>`"""
    contexts: list[ToolContext] = []

    async def inspect(ctx: ToolContext, args: EchoArgs) -> EchoResult:
        contexts.append(ctx)
        return EchoResult(speech="Ok.", echoed=[])

    register(monkeypatch, inspect)
    who = principal()
    await call("echo", {"word": "hi"}, who)

    [ctx] = contexts
    assert ctx.user is who.user
    assert (ctx.repos.group_id, ctx.repos.household_id) == (who.group_id, who.household_id)
    assert ctx.integration_id == "mcp:Kitchen"
    assert ctx.event_bus is not None and ctx.event_bus.bg is not None  # events go out in the background


# ==========================================
# Time limit


@pytest.fixture
def fast_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tool_bridge, "TOOL_TIMEOUT", 0.1)
    monkeypatch.setattr(tool_bridge, "MIN_TOOL_TIME", 0.1)


@pytest.mark.asyncio
@pytest.mark.parametrize("blocking", [False, True], ids=["awaiting", "in a worker thread"])
@pytest.mark.parametrize("writes", [False, True], ids=["read", "write"])
async def test_slow_tools(
    monkeypatch: pytest.MonkeyPatch, log: LogRecorder, fast_timeout: None, blocking: bool, writes: bool
):
    def slow_body(ctx: ToolContext, args: EchoArgs) -> EchoResult:
        time.sleep(0.6)
        return EchoResult(speech="Finally.", echoed=[])

    async def slow_await(ctx: ToolContext, args: EchoArgs) -> EchoResult:
        await asyncio.sleep(0.6)
        return EchoResult(speech="Finally.", echoed=[])

    register(monkeypatch, run_blocking(slow_body) if blocking else slow_await, writes=writes)

    started = time.perf_counter()
    result = await call("echo", {"word": "hi"}, principal(can_write=True))
    assert time.perf_counter() - started < 0.5
    if writes:
        # it may still go through: told to try again, the caller would make the change twice
        assert data(result) == {
            "speech": "Mealie is still saving that, and it may still go through. Check before asking again.",
            "error": "timeout_pending",
            "may_have_applied": True,
        }
    else:
        assert data(result) == {"speech": "Mealie took too long to answer. Try again.", "error": "timeout"}

    # the tool is left to finish, and that's logged
    await wait_for(lambda: any(level == "warning" for level, _ in log.records))
    messages = [message for level, message in log.records if level == "warning"]
    assert len(messages) == 1 and messages[0].startswith("MCP tool echo succeeded after timing out")


def test_messages_are_speakable():
    """At most two short sentences, as every tool's `speech` (PHASE1.md §5)"""
    for message in (
        tool_bridge.TIMEOUT_MESSAGE,
        tool_bridge.WRITE_TIMEOUT_MESSAGE,
        tool_bridge.BUSY_MESSAGE,
        tool_bridge.WRITE_NOT_ALLOWED_MESSAGE,
        tool_bridge.INTERNAL_ERROR_MESSAGE,
    ):
        assert first_sentences(message, 300, 2) == message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error, level, message",
    [
        (ToolError("That list is full."), "warning", "MCP tool echo failed (ToolError) after timing out"),
        (RuntimeError("boom"), "error", "MCP tool echo failed after timing out"),
    ],
)
async def test_late_failures_are_logged_once(
    monkeypatch: pytest.MonkeyPatch,
    log: LogRecorder,
    fast_timeout: None,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
    level: str,
    message: str,
):
    """By the tool bridge, and not again by asyncio (which logs a shielded task's exception at ERROR)"""

    async def late_failure(ctx: ToolContext, args: EchoArgs) -> EchoResult:
        await asyncio.sleep(0.3)
        raise error

    register(monkeypatch, late_failure)
    with caplog.at_level(logging.DEBUG, logger="asyncio"):
        await call("echo", {"word": "hi"})
        await wait_for(lambda: len(log.records) == 2)
        await asyncio.sleep(0.05)

    [late] = [(lvl, msg) for lvl, msg in log.records if lvl != "info"]
    assert late[0] == level and late[1].startswith(message)
    assert [r for r in caplog.records if r.name == "asyncio"] == []


@pytest.mark.asyncio
async def test_tools_get_what_is_left_of_the_time_limit(monkeypatch: pytest.MonkeyPatch, log: LogRecorder):
    """The limit counts from the request's arrival (authentication included), but a tool always gets a little time"""

    async def nap(ctx: ToolContext, args: EchoArgs) -> EchoResult:
        await asyncio.sleep(float(args.word))
        return EchoResult(speech="Done.", echoed=[])

    register(monkeypatch, nap)
    monkeypatch.setattr(tool_bridge, "TOOL_TIMEOUT", 0.5)
    monkeypatch.setattr(tool_bridge, "MIN_TOOL_TIME", 0.15)

    started = time.perf_counter()
    late = await call("echo", {"word": "1"}, arrived=time.monotonic() - 0.3)
    assert data(late)["error"] == "timeout"
    assert 0.15 <= time.perf_counter() - started < 0.35

    # arrived long ago: the tool still gets MIN_TOOL_TIME
    assert (await call("echo", {"word": "0.05"}, arrived=time.monotonic() - 10)).isError is False


# ==========================================
# Running tools


@pytest.mark.asyncio
async def test_running_tools_are_limited(monkeypatch: pytest.MonkeyPatch, log: LogRecorder, fast_timeout: None):
    """Tools that timed out but still run count too; a call beyond the limit is turned away at once"""
    release = asyncio.Event()

    async def stuck(ctx: ToolContext, args: EchoArgs) -> EchoResult:
        await release.wait()
        return EchoResult(speech="Done.", echoed=[])

    register(monkeypatch, stuck)
    monkeypatch.setattr(tool_bridge, "MAX_RUNNING_TOOLS", 2)

    first, second = await asyncio.gather(call("echo", {"word": "hi"}), call("echo", {"word": "hi"}))
    assert data(first)["error"] == data(second)["error"] == "timeout"

    started = time.perf_counter()
    busy = await call("echo", {"word": "hi"})
    assert time.perf_counter() - started < 0.05
    assert busy.isError is True
    assert data(busy) == {"speech": "Mealie is busy right now. Try again in a moment.", "error": "busy"}
    assert log.records[-1][1].endswith(" refused (busy) (0 ms)")

    release.set()
    await wait_for(lambda: not tool_bridge._running)
    assert (await call("echo", {"word": "hi"})).isError is False


@pytest.mark.asyncio
async def test_a_cancelled_request_logs_the_late_outcome(monkeypatch: pytest.MonkeyPatch, log: LogRecorder):
    release = asyncio.Event()

    async def stuck(ctx: ToolContext, args: EchoArgs) -> EchoResult:
        await release.wait()
        return EchoResult(speech="Done.", echoed=[])

    register(monkeypatch, stuck, writes=True)
    request = asyncio.ensure_future(call("echo", {"word": "hi"}, principal(can_write=True)))
    await wait_for(lambda: tool_bridge._running)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request

    release.set()
    await wait_for(lambda: log.records)
    [(level, message)] = log.records
    assert level == "warning"
    assert message.startswith("MCP tool echo succeeded after its request was cancelled")


@pytest.mark.asyncio
async def test_event_failures_are_logged(monkeypatch: pytest.MonkeyPatch, log: LogRecorder):
    def deliver() -> None:
        raise RuntimeError("the webhook is down")

    async def write(ctx: ToolContext, args: EchoArgs) -> EchoResult:
        assert ctx.event_bus is not None and ctx.event_bus.bg is not None
        ctx.event_bus.bg.add_task(deliver)
        return EchoResult(speech="Done.", echoed=[])

    register(monkeypatch, write, writes=True)
    who = principal(can_write=True)
    assert (await call("echo", {"word": "hi"}, who)).isError is False

    await wait_for(lambda: any(level == "exception" for level, _ in log.records))
    [message] = [message for level, message in log.records if level == "exception"]
    assert message == f"MCP tool echo couldn't publish its events, for user {who.user.id} via 'Kitchen'"


@pytest.mark.asyncio
async def test_stopping_waits_for_tools_and_their_events(
    monkeypatch: pytest.MonkeyPatch, log: LogRecorder, fast_timeout: None
):
    delivered: list[bool] = []

    def deliver() -> None:
        time.sleep(0.2)
        delivered.append(True)

    async def slow_write(ctx: ToolContext, args: EchoArgs) -> EchoResult:
        await asyncio.sleep(0.3)
        ctx.event_bus.bg.add_task(deliver)  # type: ignore[union-attr]
        return EchoResult(speech="Done.", echoed=[])

    register(monkeypatch, slow_write, writes=True)
    result = await call("echo", {"word": "hi"}, principal(can_write=True))
    assert data(result)["error"] == "timeout_pending"

    # the tool finishes during the wait, and then sends its event
    await tool_bridge.finish_detached(3)
    assert delivered == [True]
    assert not tool_bridge._running and not tool_bridge._publishing

    # what doesn't finish in time is named
    release = asyncio.Event()

    async def stuck(ctx: ToolContext, args: EchoArgs) -> EchoResult:
        await release.wait()
        return EchoResult(speech="Done.", echoed=[])

    register(monkeypatch, stuck)
    await call("echo", {"word": "hi"})
    await tool_bridge.finish_detached(0.05)
    assert log.records[-1] == ("warning", "Stopping with 1 MCP task(s) unfinished: MCP tool echo")
    release.set()
    await tool_bridge.finish_detached(3)


@pytest.mark.asyncio
async def test_a_cancelled_tool_leaves_its_session_to_its_thread(monkeypatch: pytest.MonkeyPatch):
    """
    Cancelled (the event loop ends while the tool waits on its worker thread), a tool's session isn't closed from
    the loop while the thread may still use it: the thread ends its transaction itself, giving the connection back
    """
    closed: list[str] = []
    sessions: list[Any] = []
    make_session = db_setup.SessionLocal

    def tracked_session() -> Any:
        session = make_session()
        close = session.close

        def tracked_close() -> None:
            closed.append(threading.current_thread().name)
            close()

        session.close = tracked_close  # type: ignore[method-assign]
        sessions.append(session)
        return session

    monkeypatch.setattr(db_setup, "SessionLocal", tracked_session)
    release = threading.Event()
    queried: list[bool] = []

    def body(ctx: ToolContext, args: EchoArgs) -> EchoResult:
        release.wait(5)
        ctx.repos.session.execute(sa.text("SELECT 1"))
        queried.append(not closed)
        return EchoResult(speech="Done.", echoed=[])

    tool = register(monkeypatch, run_blocking(body))
    run = asyncio.ensure_future(tool_bridge._run(tool, EchoArgs(word="hi"), principal(), get_locale_provider("en-US")))
    await asyncio.sleep(0.1)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    release.set()
    await wait_for(lambda: queried)
    assert queried == [True] and closed == []
    await wait_for(lambda: not sessions[0].in_transaction())  # `run_blocking` rolled back in its thread

    # finished normally, it's closed as usual
    finished = await tool_bridge._run(tool, EchoArgs(word="hi"), principal(), get_locale_provider("en-US"))
    assert finished.speech == "Done." and closed == [threading.current_thread().name]


# ==========================================
# Server and endpoint helpers


def test_request_state_requires_the_endpoint():
    with pytest.raises(RuntimeError):
        request_state(None)

    state = McpRequestState(principal=principal(), translator=get_locale_provider("en-US"), arrived=time.monotonic())
    request = SimpleNamespace(scope={"state": {REQUEST_STATE_KEY: state}})
    assert request_state(request) is state  # type: ignore[arg-type]


def test_server_offers_tools_only():
    capabilities = create_mcp_server().create_initialization_options().capabilities
    assert capabilities.tools is not None
    assert (capabilities.prompts, capabilities.resources, capabilities.logging) == (None, None, None)


@pytest.mark.parametrize(
    "header, token",
    [
        (None, None),
        ("", None),
        ("Bearer", None),
        ("Bearer   ", None),
        ("Basic dXNlcjpwYXNz", None),
        ("Bearer abc", "abc"),
        ("bearer  abc ", "abc"),
        ("BEARER mmcp_at_x", "mmcp_at_x"),
    ],
)
def test_bearer_token(header: str | None, token: str | None):
    assert _bearer_token(header) == token


@pytest.mark.parametrize(
    "header, same",
    [
        (None, True),
        ("http://mealie.lan:9925", True),
        ("HTTP://Mealie.LAN:9925", True),
        ("http://mealie.lan:9925/", True),
        ("https://mealie.lan:9925", False),
        ("http://mealie.lan", False),
        ("http://evil.example", False),
        ("null", False),
        ("", False),
    ],
)
def test_same_origin(header: str | None, same: bool):
    assert is_same_origin(header, "http://mealie.lan:9925") is same
