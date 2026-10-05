"""
The AI tools over MCP (docs/ai/PHASE3.md §1, §3, §7): what `tools/list` shows, results equal to REST's, write grants,
`isError` for every failure, household isolation, events and the time limit.
"""

import asyncio
import json
import threading
import time
from dataclasses import replace
from datetime import date, timedelta
from typing import Any
from uuid import UUID, uuid4

import mcp.types as types
import pytest
from fastapi.testclient import TestClient
from pydantic import UUID4

from mealie.schema.meal_plan.new_meal import PlanEntryType, SavePlanEntry
from mealie.schema.recipe.recipe_ingredient import RecipeIngredient, SaveIngredientFood
from mealie.schema.response.pagination import PaginationQuery
from mealie.services.ai.mcp import tool_bridge
from mealie.services.ai.tools import ToolContext, all_tools, get_tool, registry
from mealie.services.ai.tools.base import local_today, run_blocking
from mealie.services.event_bus_service.event_bus_service import EventBusService
from mealie.services.event_bus_service.event_types import EventTypes
from tests.integration_tests.ai_tests.mcp.mcp_helpers import (
    ALL_TOOLS,
    READ_TOOLS,
    api_token,
    create_list,
    create_recipe,
    http_client,
    mcp_running,
    mcp_session,
    oauth_token,
    payload,
    post,
    raw_payload,
    rest_result,
    set_api_token_writes,
    tool_call,
)
from tests.utils.factories import random_int, random_string
from tests.utils.fixture_schemas import TestUser
from tests.utils.mcp_oauth import connect, create_client


def far_date() -> date:
    """A day no other test plans anything on"""
    return local_today() + timedelta(days=random_int(400, 4000))


def list_notes(user: TestUser, list_id: UUID4) -> list[str]:
    query = PaginationQuery(per_page=-1, query_filter=f"shopping_list_id={list_id}")
    return sorted(item.note or "" for item in user.repos.group_shopping_list_item.page_all(query).items)


async def call_tools(token: str, calls: list[tuple[str, dict[str, Any]]]) -> list[types.CallToolResult]:
    async with mcp_running(), mcp_session(token) as (session, _):
        return [await session.call_tool(name, arguments) for name, arguments in calls]


async def list_tool_names(token: str) -> list[str]:
    async with mcp_running(), mcp_session(token) as (session, _):
        return [tool.name for tool in (await session.list_tools()).tools]


@pytest.fixture(scope="module")
def read_token(api_client: TestClient, unique_user: TestUser) -> str:
    return oauth_token(api_client, unique_user)


@pytest.fixture(scope="module")
def write_client_name() -> str:
    return f"Kitchen {random_string(6)}"


@pytest.fixture(scope="module")
def write_token(api_client: TestClient, unique_user: TestUser, write_client_name: str) -> str:
    return oauth_token(api_client, unique_user, writes=True, name=write_client_name)


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    events: list[dict] = []
    monkeypatch.setattr(EventBusService, "dispatch", lambda self, **kwargs: events.append(kwargs))
    return events


# ==========================================
# tools/list


def test_list_tools(read_token: str, write_token: str):
    async def scenario(token: str) -> list[types.Tool]:
        async with mcp_running(), mcp_session(token) as (session, _):
            return (await session.list_tools()).tools

    read_only = asyncio.run(scenario(read_token))
    assert [tool.name for tool in read_only] == READ_TOOLS

    tools = asyncio.run(scenario(write_token))
    assert [tool.name for tool in tools] == ALL_TOOLS == [tool.name for tool in all_tools()]
    for tool in tools:
        registered = get_tool(tool.name)
        assert registered is not None
        # the registry's schema verbatim: flat, no $ref, which every MCP client (and HA's converter) can read
        assert tool.inputSchema == registered.input_schema
        assert tool.description == registered.description
        assert tool.title == tool_bridge.TITLES[tool.name]
        assert tool.outputSchema is None
        assert tool.annotations is not None
        if registered.writes:
            assert (tool.annotations.readOnlyHint, tool.annotations.destructiveHint) == (False, False)
            assert tool.annotations.idempotentHint is None
        else:
            assert (tool.annotations.readOnlyHint, tool.annotations.idempotentHint) == (True, True)
            assert tool.annotations.destructiveHint is None
        assert tool.annotations.openWorldHint is False


# ==========================================
# Read tools


def test_read_tools_return_what_rest_returns(api_client: TestClient, unique_user: TestUser, read_token: str):
    food = unique_user.repos.ingredient_foods.create(
        SaveIngredientFood(id=uuid4(), name=f"food{random_string(8)}", group_id=UUID(unique_user.group_id))
    )
    recipe = create_recipe(
        unique_user,
        steps=["Boil the water.", "Add the pasta."],
        recipe_ingredient=[RecipeIngredient(quantity=1, food_id=food.id, food=food)],
        total_time="20 minutes",
    )
    day = far_date()
    unique_user.repos.meals.create(
        SavePlanEntry(
            date=day,
            entry_type=PlanEntryType.dinner,
            recipe_id=recipe.id,
            group_id=UUID(unique_user.group_id),
            user_id=unique_user.user_id,
        )
    )
    shopping_list = create_list(unique_user, ["eggs", "milk"])

    calls: list[tuple[str, dict[str, Any]]] = [
        ("search_recipes", {"query": recipe.name}),
        ("get_recipe", {"slug": recipe.slug, "part": "all"}),
        ("get_cooking_step", {"slug": recipe.slug, "step": 2}),
        ("suggest_from_ingredients", {"foods": [food.name]}),
        ("whats_planned", {"start": day.isoformat()}),
        ("get_shopping_list", {"list_name": shopping_list.name}),
        ("recipe_card_queue", {}),
    ]
    assert [name for name, _ in calls] == READ_TOOLS

    results = asyncio.run(call_tools(read_token, calls))
    for (name, arguments), result in zip(calls, results, strict=True):
        assert result.isError is False, (name, result.content)
        assert payload(result) == rest_result(api_client, unique_user, name, arguments), name

    by_name = {name: payload(result) for (name, _), result in zip(calls, results, strict=True)}
    assert [r["slug"] for r in by_name["search_recipes"]["recipes"]] == [recipe.slug]
    assert by_name["get_cooking_step"]["text"] == "Add the pasta."
    assert [r["slug"] for r in by_name["suggest_from_ingredients"]["recipes"]] == [recipe.slug]
    assert [e["recipe_slug"] for e in by_name["whats_planned"]["entries"]] == [recipe.slug]
    assert sorted(item["text"] for item in by_name["get_shopping_list"]["items"]) == ["eggs", "milk"]
    assert set(by_name["recipe_card_queue"]) == {
        "speech",
        "ready",
        "needs_attention",
        "processing",
        "failed",
        "waiting",
    }


def test_results_are_compact_json(read_token: str):
    result = asyncio.run(call_tools(read_token, [("whats_planned", {"start": far_date().isoformat()})]))[0]
    text = result.content[0].text  # type: ignore[union-attr]
    assert text.startswith('{"speech":"Nothing is planned')
    assert text == json.dumps(json.loads(text), separators=(",", ":"), ensure_ascii=False)


def test_households_are_isolated(api_client: TestClient, unique_user: TestUser, h2_user: TestUser, g2_user: TestUser):
    """A token acts as its user: another household's plan and lists, in this group or another, aren't visible"""
    day = far_date()
    unique_user.repos.meals.create(
        SavePlanEntry(
            date=day,
            entry_type=PlanEntryType.lunch,
            title="Secret soup",
            group_id=UUID(unique_user.group_id),
            user_id=unique_user.user_id,
        )
    )
    mine = create_list(unique_user, ["secret sauce"])

    # another household of the group, connected through the group's OAuth client; and another group's API token
    client = create_client(api_client, unique_user, name=f"Kitchen {random_string(6)}", allowWriteScope=True)
    h2_token = connect(api_client, h2_user, client, allow_writes=True)["access_token"]
    g2_token_id, g2_token = api_token(api_client, g2_user)
    set_api_token_writes(api_client, g2_user, g2_token_id, True)

    for user, token in ((h2_user, h2_token), (g2_user, g2_token)):
        theirs = create_list(user)
        planned, listed, added = asyncio.run(
            call_tools(
                token,
                [
                    ("whats_planned", {"start": day.isoformat()}),
                    ("get_shopping_list", {"list_name": mine.name}),
                    ("add_to_shopping_list", {"items": ["vodka"], "list_name": mine.name}),
                ],
            )
        )
        assert planned.isError is False and payload(planned)["entries"] == []
        for result in (listed, added):
            assert result.isError is True
            data = payload(result)
            assert data["error"] == "not_found"
            # the lists it offers instead are the caller's own
            offered = data["speech"].partition("Your lists are:")[2]
            assert theirs.name in offered and mine.name not in offered

    assert list_notes(unique_user, mine.id) == ["secret sauce"]


# ==========================================
# Write tools and grants


def test_writes_need_a_grant(api_client: TestClient, unique_user: TestUser, read_token: str):
    shopping_list = create_list(unique_user)
    day = far_date()
    recipe = create_recipe(unique_user)

    results = asyncio.run(
        call_tools(
            read_token,
            [
                ("add_to_shopping_list", {"items": ["eggs"], "list_name": shopping_list.name}),
                ("plan_meal", {"date": day.isoformat(), "recipe_slug": recipe.slug}),
                # refused before the arguments are even looked at
                ("plan_meal", {"nonsense": True}),
            ],
        )
    )
    for result in results:
        assert result.isError is True
        assert payload(result) == {"speech": tool_bridge.WRITE_NOT_ALLOWED_MESSAGE, "error": "write_not_allowed"}

    assert list_notes(unique_user, shopping_list.id) == []
    assert rest_result(api_client, unique_user, "whats_planned", {"start": day.isoformat()})["entries"] == []


def test_writes_with_a_grant(
    api_client: TestClient, unique_user: TestUser, write_token: str, write_client_name: str, published: list[dict]
):
    shopping_list = create_list(unique_user)
    day = far_date()
    recipe = create_recipe(unique_user)

    added, planned = asyncio.run(
        call_tools(
            write_token,
            [
                ("add_to_shopping_list", {"items": ["eggs", "milk"], "list_name": shopping_list.name}),
                ("plan_meal", {"date": day.isoformat(), "meal": "lunch", "recipe_slug": recipe.slug}),
            ],
        )
    )
    assert added.isError is False and payload(added)["added"] == 2
    assert planned.isError is False and payload(planned)["entry"]["recipe_slug"] == recipe.slug
    assert list_notes(unique_user, shopping_list.id) == ["eggs", "milk"]
    entries = rest_result(api_client, unique_user, "whats_planned", {"start": day.isoformat()})["entries"]
    assert [e["id"] for e in entries] == [payload(planned)["entry"]["id"]]

    # the same events as over REST, from this connection
    assert [(e["event_type"], e["integration_id"]) for e in published] == [
        (EventTypes.shopping_list_updated, f"mcp:{write_client_name}"),
        (EventTypes.mealplan_entry_created, f"mcp:{write_client_name}"),
    ]
    assert all(str(e["household_id"]) == unique_user.household_id for e in published)


def test_events_are_published_after_the_answer(
    api_client: TestClient, unique_user: TestUser, write_token: str, monkeypatch: pytest.MonkeyPatch
):
    """
    Notifications and webhooks are sent in the background, as REST sends them after its response: the answer comes
    while they're held up
    """
    shopping_list = create_list(unique_user)
    answered = threading.Event()
    order: list[str] = []
    sent: list[tuple[str, UUID4]] = []

    def publish(self: EventBusService, event: Any, group_id: UUID4, household_id: UUID4) -> None:
        # an event sent before the answer would hold up the answer, and this would time out
        if answered.wait(5):
            order.append("published")
            sent.append((event.integration_id, household_id))

    monkeypatch.setattr(EventBusService, "_publish_event", publish)

    async def scenario() -> types.CallToolResult:
        async with mcp_running(), mcp_session(write_token) as (session, _):
            result = await session.call_tool(
                "add_to_shopping_list", {"items": ["flour"], "list_name": shopping_list.name}
            )
            order.append("answered")
            answered.set()
            for _ in range(100):
                if sent:
                    break
                await asyncio.sleep(0.02)
            return result

    result = asyncio.run(scenario())
    assert result.isError is False
    assert order == ["answered", "published"]
    assert len(sent) == 1 and sent[0][0].startswith("mcp:")
    assert str(sent[0][1]) == unique_user.household_id


def test_api_token_write_grant(api_client: TestClient, unique_user: TestUser, published: list[dict]):
    """An API token can't change anything until its owner allows it (Profile → API Tokens)"""
    token_id, token = api_token(api_client, unique_user)
    shopping_list = create_list(unique_user)
    add = ("add_to_shopping_list", {"items": ["rice"], "list_name": shopping_list.name})

    assert asyncio.run(list_tool_names(token)) == READ_TOOLS
    refused = asyncio.run(call_tools(token, [add]))[0]
    assert refused.isError is True and payload(refused)["error"] == "write_not_allowed"
    assert list_notes(unique_user, shopping_list.id) == []

    set_api_token_writes(api_client, unique_user, token_id, True)
    assert asyncio.run(list_tool_names(token)) == ALL_TOOLS
    added = asyncio.run(call_tools(token, [add]))[0]
    assert added.isError is False
    assert list_notes(unique_user, shopping_list.id) == ["rice"]
    assert published[-1]["integration_id"] == "mcp:API token"

    set_api_token_writes(api_client, unique_user, token_id, False)
    assert asyncio.run(list_tool_names(token)) == READ_TOOLS


# ==========================================
# Errors


@pytest.mark.parametrize(
    "name, arguments, error, loc, error_type",
    [
        ("search_recipes", {"limit": 0}, "invalid_arguments", ["limit"], "greater_than_equal"),
        ("search_recipes", {"querry": "soup"}, "invalid_arguments", ["querry"], "extra_forbidden"),
        ("get_cooking_step", {"slug": "cake"}, "invalid_arguments", ["step"], "missing"),
        ("get_recipe", {"slug": "cake", "part": "everything"}, "invalid_arguments", ["part"], "literal_error"),
    ],
)
def test_invalid_arguments(read_token: str, name: str, arguments: dict, error: str, loc: list, error_type: str):
    """The tool's pydantic model is the only validator: its errors come back for the model to correct"""
    result = asyncio.run(call_tools(read_token, [(name, arguments)]))[0]
    assert result.isError is True
    data = payload(result)
    assert data["error"] == error
    assert data["speech"].startswith("I couldn't use those details.")
    assert any(e["loc"] == loc and e["type"] == error_type for e in data["errors"]), data["errors"]
    assert all(set(e) == {"type", "loc", "msg", "input"} for e in data["errors"])


def test_model_validators_explain_themselves(write_token: str):
    result = asyncio.run(call_tools(write_token, [("add_to_shopping_list", {})]))[0]
    data = payload(result)
    assert data["error"] == "invalid_arguments" and data["errors"][0]["loc"] == []
    assert data["speech"] == "I couldn't use those details. " + data["errors"][0]["msg"].removeprefix("Value error, ")


def test_unknown_tool_and_things_that_dont_exist(read_token: str):
    unknown, missing_recipe, missing_step = asyncio.run(
        call_tools(
            read_token,
            [
                ("delete_everything", {}),
                ("get_recipe", {"slug": f"no-such-recipe-{random_string(6)}"}),
                ("get_cooking_step", {"slug": f"no-such-recipe-{random_string(6)}", "step": 1}),
            ],
        )
    )
    assert unknown.isError is True
    assert payload(unknown) == {
        "speech": "Mealie doesn't have a tool called delete_everything.",
        "error": "unknown_tool",
    }
    for result in (missing_recipe, missing_step):
        assert result.isError is True
        assert payload(result)["error"] == "not_found"
        assert payload(result)["speech"].startswith("I couldn't find a recipe called no such recipe")


def test_unexpected_failures_are_speakable(read_token: str, monkeypatch: pytest.MonkeyPatch):
    async def broken(ctx: ToolContext, args: Any) -> Any:
        raise RuntimeError("database exploded at /var/lib/secret")

    tool = get_tool("whats_planned")
    assert tool is not None
    monkeypatch.setitem(registry._TOOLS, "whats_planned", replace(tool, handler=broken))

    result = asyncio.run(call_tools(read_token, [("whats_planned", {})]))[0]
    assert result.isError is True
    assert payload(result) == {"speech": tool_bridge.INTERNAL_ERROR_MESSAGE, "error": "internal_error"}


def test_raw_calls_without_initialize(read_token: str):
    """Stateless: a `tools/call` on its own works too (what spec 2026-07-28 clients send)"""

    async def scenario() -> tuple[dict, bool]:
        async with mcp_running(), http_client(read_token) as http:
            return raw_payload(await post(http, tool_call("whats_planned", {"start": far_date().isoformat()})))

    data, is_error = asyncio.run(scenario())
    assert is_error is False and data["entries"] == []


# ==========================================
# Time limit


@pytest.mark.parametrize("blocking", [False, True], ids=["awaiting", "in a worker thread"])
def test_slow_tools_time_out(read_token: str, monkeypatch: pytest.MonkeyPatch, blocking: bool):
    """
    A tool that takes too long gets a speakable answer at the deadline, even while it waits on a worker thread, so
    Home Assistant's 5 s per request holds. The tool is left to finish.
    """
    finished: list[bool] = []

    def slow_body(ctx: ToolContext, args: Any) -> Any:
        time.sleep(1.5)
        finished.append(True)
        raise RuntimeError("too late to matter")

    async def slow_await(ctx: ToolContext, args: Any) -> Any:
        await asyncio.sleep(1.5)
        finished.append(True)
        raise RuntimeError("too late to matter")

    tool = get_tool("whats_planned")
    assert tool is not None
    handler = run_blocking(slow_body) if blocking else slow_await
    monkeypatch.setitem(registry._TOOLS, "whats_planned", replace(tool, handler=handler))
    monkeypatch.setattr(tool_bridge, "TOOL_TIMEOUT", 0.3)
    monkeypatch.setattr(tool_bridge, "MIN_TOOL_TIME", 0.3)

    async def scenario() -> tuple[types.CallToolResult, float]:
        async with mcp_running(), mcp_session(read_token) as (session, _):
            started = time.perf_counter()
            result = await session.call_tool("whats_planned", {})
            elapsed = time.perf_counter() - started
            for _ in range(200):  # let the abandoned tool finish before the loop closes
                if finished:
                    break
                await asyncio.sleep(0.02)
            return result, elapsed

    result, elapsed = asyncio.run(scenario())
    assert elapsed < 1.2
    assert result.isError is True
    assert payload(result) == {"speech": "Mealie took too long to answer. Try again.", "error": "timeout"}
    assert finished == [True]


def test_slow_writes_may_still_apply(
    api_client: TestClient, unique_user: TestUser, write_token: str, monkeypatch: pytest.MonkeyPatch
):
    """
    A write that misses the deadline isn't undone, and the answer says so: told to try again, the caller would plan
    the meal twice
    """
    recipe = create_recipe(unique_user)
    day = far_date()
    tool = get_tool("plan_meal")
    assert tool is not None
    plan = tool.handler

    async def slow_plan(ctx: ToolContext, args: Any) -> Any:
        await asyncio.sleep(0.6)
        return await plan(ctx, args)

    monkeypatch.setitem(registry._TOOLS, "plan_meal", replace(tool, handler=slow_plan))
    monkeypatch.setattr(tool_bridge, "TOOL_TIMEOUT", 0.3)
    monkeypatch.setattr(tool_bridge, "MIN_TOOL_TIME", 0.3)

    # the lifespan waits for the tool when it ends
    result = asyncio.run(
        call_tools(write_token, [("plan_meal", {"date": day.isoformat(), "recipe_slug": recipe.slug})])
    )
    assert result[0].isError is True
    assert payload(result[0]) == {
        "speech": "Mealie is still saving that, and it may still go through. Check before asking again.",
        "error": "timeout_pending",
        "may_have_applied": True,
    }
    entries = rest_result(api_client, unique_user, "whats_planned", {"start": day.isoformat()})["entries"]
    assert [e["recipe_slug"] for e in entries] == [recipe.slug]


def test_busy(read_token: str, monkeypatch: pytest.MonkeyPatch):
    """Beyond `MAX_RUNNING_TOOLS`, a call is turned away at once with a sentence to read, rather than queued"""
    tool = get_tool("whats_planned")
    assert tool is not None
    planned = tool.handler
    release = threading.Event()

    def wait_for_release(ctx: ToolContext, args: Any) -> None:
        release.wait(5)

    async def held_up(ctx: ToolContext, args: Any) -> Any:
        await run_blocking(wait_for_release)(ctx, args)
        return await planned(ctx, args)

    monkeypatch.setitem(registry._TOOLS, "whats_planned", replace(tool, handler=held_up))
    monkeypatch.setattr(tool_bridge, "MAX_RUNNING_TOOLS", 1)

    async def scenario() -> tuple[tuple[dict, bool], tuple[dict, bool], float]:
        async with mcp_running(), http_client(read_token) as http:
            first = asyncio.ensure_future(post(http, tool_call("whats_planned", {"start": far_date().isoformat()})))
            for _ in range(100):
                if tool_bridge._running:
                    break
                await asyncio.sleep(0.02)
            started = time.perf_counter()
            second = raw_payload(await post(http, tool_call("search_recipes", {"query": "soup"})))
            elapsed = time.perf_counter() - started
            release.set()
            return raw_payload(await first), second, elapsed

    (first, first_is_error), (second, second_is_error), elapsed = asyncio.run(scenario())
    assert first_is_error is False and first["entries"] == []
    assert second_is_error is True
    assert second == {"speech": "Mealie is busy right now. Try again in a moment.", "error": "busy"}
    assert elapsed < 1
