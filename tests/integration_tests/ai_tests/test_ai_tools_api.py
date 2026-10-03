import asyncio
import re
import threading
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from mealie.app import app
from mealie.db.db_setup import engine
from mealie.schema.recipe.recipe import Recipe
from mealie.schema.recipe.recipe_settings import RecipeSettings
from tests.utils import api_routes
from tests.utils.factories import random_string
from tests.utils.fixture_schemas import TestUser

WRITE_TOOLS = {"add_to_shopping_list", "plan_meal"}
READ_TOOLS = {
    "search_recipes",
    "get_recipe",
    "get_cooking_step",
    "suggest_from_ingredients",
    "whats_planned",
    "get_shopping_list",
}


def test_list_tools(api_client: TestClient, unique_user: TestUser):
    response = api_client.get(api_routes.ai_tools, headers=unique_user.token)
    assert response.status_code == 200

    tools = {tool["name"]: tool for tool in response.json()}
    assert set(tools) == READ_TOOLS | WRITE_TOOLS
    for name, tool in tools.items():
        assert set(tool) == {"name", "description", "input_schema", "writes"}
        assert tool["writes"] is (name in WRITE_TOOLS)
        assert tool["description"]
        assert tool["input_schema"]["type"] == "object"
        assert tool["input_schema"]["additionalProperties"] is False

    assert tools["get_cooking_step"]["input_schema"]["required"] == ["slug", "step"]
    assert tools["whats_planned"]["input_schema"]["properties"]["meal"]["enum"][:3] == ["breakfast", "lunch", "dinner"]


def test_tools_require_login(api_client: TestClient):
    assert api_client.get(api_routes.ai_tools).status_code == 401
    assert api_client.post(api_routes.ai_tools_name("search_recipes"), json={}).status_code == 401


def test_unknown_tool(api_client: TestClient, unique_user: TestUser):
    response = api_client.post(api_routes.ai_tools_name("delete_everything"), json={}, headers=unique_user.token)
    assert response.status_code == 404
    assert response.json()["detail"]["message"] == "Unknown tool: delete_everything"


def test_call_tool(api_client: TestClient, unique_user: TestUser):
    # the body is optional for tools without required arguments
    response = api_client.post(api_routes.ai_tools_name("search_recipes"), headers=unique_user.token)
    assert response.status_code == 200
    body = response.json()
    assert body["tool"] == "search_recipes"
    assert set(body["result"]) >= {"speech", "recipes", "total", "unmatched"}


@pytest.mark.parametrize(
    "tool, args, loc, error_type",
    [
        ("search_recipes", {"limit": 0}, ["limit"], "greater_than_equal"),
        ("search_recipes", {"limit": 21}, ["limit"], "less_than_equal"),
        ("search_recipes", {"querry": "soup"}, ["querry"], "extra_forbidden"),
        ("search_recipes", {"include_foods": "rice"}, ["include_foods"], "list_type"),
        ("get_cooking_step", {"slug": "cake"}, ["step"], "missing"),
        ("get_recipe", {"slug": "cake", "part": "everything"}, ["part"], "literal_error"),
        ("get_recipe", {"slug": "cake", "servings": -1}, ["servings"], "greater_than"),
        ("plan_meal", {"date": "2030-01-01"}, [], "value_error"),
    ],
)
def test_invalid_arguments(
    api_client: TestClient, unique_user: TestUser, tool: str, args: dict, loc: list, error_type: str
):
    response = api_client.post(api_routes.ai_tools_name(tool), json=args, headers=unique_user.token)
    assert response.status_code == 422

    errors = response.json()["detail"]
    assert any(e["loc"] == loc and e["type"] == error_type for e in errors), errors
    assert all(set(e) == {"type", "loc", "msg", "input"} for e in errors)


def test_arguments_must_be_an_object(api_client: TestClient, unique_user: TestUser):
    response = api_client.post(api_routes.ai_tools_name("search_recipes"), json=["soup"], headers=unique_user.token)
    assert response.status_code == 422


def test_concurrent_calls_dont_block_the_event_loop(
    api_client: TestClient, unique_user: TestUser, monkeypatch: pytest.MonkeyPatch
):
    """
    More simultaneous calls than the connection pool holds (15 by default) all succeed. Tools must only touch the
    database in worker threads: a pool checkout that blocks the event loop stops every other request, including
    those that would give back the connection it's waiting for.
    """
    recipe = unique_user.repos.recipes.create(
        Recipe(
            user_id=unique_user.user_id,
            group_id=UUID(unique_user.group_id),
            name=random_string(),
            settings=RecipeSettings(),
        )
    )
    calls = [
        ("whats_planned", {}),
        ("search_recipes", {"query": recipe.name}),
        ("get_recipe", {"slug": recipe.slug}),
        ("get_cooking_step", {"slug": recipe.slug, "step": 1}),  # a 404, after reading the recipe
    ] * 6

    # fail a blocked checkout after a few seconds instead of 30
    monkeypatch.setattr(engine.pool, "_timeout", 3)

    async def burst() -> list[httpx.Response | BaseException]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", timeout=60) as client:
            requests = [
                client.post(api_routes.ai_tools_name(tool), json=args, headers=unique_user.token)
                for tool, args in calls
            ]
            return await asyncio.gather(*requests, return_exceptions=True)

    # the burst's event loop runs in this thread
    loop_thread = threading.get_ident()
    on_the_loop: list[str] = []

    def record(conn, cursor, statement: str, parameters, context, executemany) -> None:
        if threading.get_ident() == loop_thread:
            on_the_loop.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        responses = asyncio.run(burst())
    finally:
        event.remove(engine, "before_cursor_execute", record)

    statuses = [r.status_code if isinstance(r, httpx.Response) else repr(r) for r in responses]
    assert statuses == [404 if tool == "get_cooking_step" else 200 for tool, _ in calls]

    # the only query on the loop is upstream's login check, one per request, which hands its connection straight
    # back; the tools' own queries all run in worker threads
    assert len(on_the_loop) == len(calls) and all(re.search(r"\bFROM users\b", s) for s in on_the_loop), on_the_loop
