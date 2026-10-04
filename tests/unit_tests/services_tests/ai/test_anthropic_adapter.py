"""The native Claude adapter, against a mocked Anthropic API (docs/ai/PHASE1.md §2)"""

import base64
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import anthropic
import httpx2
import pytest
from PIL import Image

from mealie.schema import openai as response_schemas
from mealie.schema.group.ai_providers import AIProviderOut, AIProviderProtocol
from mealie.schema.openai import OpenAIRecipe, OpenAIText
from mealie.schema.openai._base import OpenAIBase
from mealie.services.ai import anthropic_adapter, listing
from mealie.services.ai.errors import (
    AIProviderOutputTruncatedError,
    AIProviderRefusedError,
    AIProviderUnsupportedError,
)
from mealie.services.ai.ingest.pipeline import llm_schemas as card_schemas
from mealie.services.ai.usage import AITokenUsage
from mealie.services.openai import OpenAIImageExternal, OpenAILocalImage, OpenAIService
from mealie.services.openai.openai import OpenAILocalAudio


def claude(
    model: str = "claude-test-1", base_url: str | None = None, api_key: str = "sk-ant-test", **kwargs: Any
) -> AIProviderOut:
    return AIProviderOut(
        id=uuid4(),
        name="Claude",
        base_url=base_url,
        api_key=api_key,
        model=model,
        protocol=AIProviderProtocol.anthropic,
        **kwargs,
    )


def message(
    *content: dict,
    stop_reason: str = "end_turn",
    tokens: tuple[int, int] = (12, 3),
    model: str = "claude-test-1",
    iterations: list[dict] | None = None,
) -> httpx2.Response:
    usage: dict[str, Any] = {"input_tokens": tokens[0], "output_tokens": tokens[1]}
    if iterations is not None:
        usage["iterations"] = iterations

    return httpx2.Response(
        200,
        json={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": list(content),
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": usage,
        },
    )


def text(value: str | dict) -> dict:
    return {"type": "text", "text": value if isinstance(value, str) else json.dumps(value)}


def bad_request(error_message: str) -> httpx2.Response:
    return httpx2.Response(
        400, json={"type": "error", "error": {"type": "invalid_request_error", "message": error_message}}
    )


class FakeClaude:
    """Answers the adapter's requests with the given responses, in order, and records the requests"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *responses: httpx2.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []
        transport = httpx2.MockTransport(self.handler)

        class MockedClient(anthropic.AsyncAnthropic):
            # The adapter's own client and settings, minus retries and the network
            def __init__(self, **kwargs: Any) -> None:
                kwargs["http_client"] = httpx2.AsyncClient(transport=transport)
                super().__init__(**kwargs, max_retries=0)

        monkeypatch.setattr(anthropic, "AsyncAnthropic", MockedClient)

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.responses.pop(0)

    @property
    def bodies(self) -> list[dict]:
        return [json.loads(request.content) for request in self.requests]


async def ask(provider: AIProviderOut, **kwargs: Any) -> tuple[OpenAIText | None, AITokenUsage]:
    return await anthropic_adapter.get_response(
        "system prompt", "hello", response_schema=OpenAIText, provider=provider, **kwargs
    )


@pytest.mark.asyncio
async def test_request_shape(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    api = FakeClaude(monkeypatch, message(text({"text": "hi"})))
    Image.new("RGB", (8, 8), "red").save(tmp_path / "card.png")
    attachments = [
        OpenAIImageExternal(url="https://images.test/card.jpg"),
        OpenAILocalImage(filename="card", path=tmp_path / "card.png"),
    ]
    provider = claude(base_url="https://claude-proxy.test", request_headers={"X-Org": "1"}, request_params={"q": "2"})

    result, usage = await ask(provider, attachments=attachments)

    assert result == OpenAIText(text="hi")
    assert usage == AITokenUsage(prompt_tokens=12, completion_tokens=3)

    (request,) = api.requests
    assert request.url.path == "/v1/messages"
    assert request.url.host == "claude-proxy.test"
    assert request.url.params["q"] == "2"
    assert request.headers["x-api-key"] == "sk-ant-test"
    assert request.headers["x-org"] == "1"
    assert "anthropic-beta" not in request.headers

    (body,) = api.bodies
    assert body["model"] == "claude-test-1"
    assert body["system"] == "system prompt"
    assert body["max_tokens"] == 16000
    assert body["output_config"] == {
        "format": {"type": "json_schema", "schema": anthropic_adapter.output_schema(OpenAIText)}
    }
    # Current models reject these; nor is there an assistant prefill
    assert not {"thinking", "temperature", "top_p", "top_k", "tool_choice", "fallbacks"} & body.keys()
    assert [turn["role"] for turn in body["messages"]] == ["user"]

    # Images first, then the text
    url_image, local_image, message_text = body["messages"][0]["content"]
    assert url_image == {"type": "image", "source": {"type": "url", "url": "https://images.test/card.jpg"}}
    assert local_image["type"] == "image"
    assert local_image["source"]["type"] == "base64"
    assert local_image["source"]["media_type"] == "image/jpeg"
    assert base64.b64decode(local_image["source"]["data"]).startswith(b"\xff\xd8")  # a JPEG
    assert message_text == {"type": "text", "text": "hello"}


@pytest.mark.parametrize(
    ("base_url", "model", "uses_fallbacks"),
    [
        (None, "claude-opus-5-5", True),
        (None, "claude-opus-5", True),
        ("https://api.anthropic.com", "claude-sonnet-5-5", True),
        # A base URL copied from an OpenAI-compatible setup still reaches /v1/messages
        ("https://api.anthropic.com/v1/", "claude-fable-5-1", True),
        # Only Anthropic's own API, and only the listed models
        ("https://claude-proxy.test", "claude-opus-5-5", False),
        (None, "claude-test-1", False),
    ],
)
@pytest.mark.asyncio
async def test_refusal_fallbacks_are_requested_for_first_party_listed_models(
    monkeypatch: pytest.MonkeyPatch, base_url: str | None, model: str, uses_fallbacks: bool
):
    api = FakeClaude(monkeypatch, message(text({"text": "hi"})))

    result, _ = await ask(claude(model, base_url))

    assert result == OpenAIText(text="hi")
    (request,) = api.requests
    (body,) = api.bodies
    assert request.url.path == "/v1/messages"
    if uses_fallbacks:
        assert body["fallbacks"] == "default"
        assert "server-side-fallback-2026-07-01" in request.headers["anthropic-beta"].split(",")
    else:
        assert "fallbacks" not in body
        assert "anthropic-beta" not in request.headers


@pytest.mark.asyncio
async def test_a_400_naming_fallbacks_is_retried_once_without_them(monkeypatch: pytest.MonkeyPatch):
    api = FakeClaude(
        monkeypatch,
        bad_request("fallbacks: Extra inputs are not permitted"),
        message(text({"text": "hi"})),
    )

    result, _ = await ask(claude("claude-opus-5-5"))

    assert result == OpenAIText(text="hi")
    first, retry = api.bodies
    assert first["fallbacks"] == "default"
    assert "fallbacks" not in retry
    assert "anthropic-beta" not in api.requests[1].headers
    assert {k: v for k, v in first.items() if k != "fallbacks"} == retry


@pytest.mark.asyncio
async def test_other_400s_are_not_retried(monkeypatch: pytest.MonkeyPatch):
    api = FakeClaude(monkeypatch, bad_request("messages: at least one message is required"))

    with pytest.raises(anthropic.BadRequestError):
        await ask(claude("claude-opus-5-5"))

    assert len(api.requests) == 1


@pytest.mark.asyncio
async def test_only_the_fallback_models_answer_is_parsed(monkeypatch: pytest.MonkeyPatch):
    fallback = {
        "type": "fallback",
        "from": {"model": "claude-opus-5-5"},
        "to": {"model": "claude-fallback-test"},
        "trigger": {"type": "refusal"},
    }
    FakeClaude(monkeypatch, message(text('{"text": "I can'), fallback, text({"text": "hi"})))

    result, _ = await ask(claude("claude-opus-5-5"))

    assert result == OpenAIText(text="hi")


@pytest.mark.asyncio
async def test_usage_covers_every_attempt_of_a_server_side_fallback(monkeypatch: pytest.MonkeyPatch):
    # The top-level counts cover only the attempt that produced the answer
    iterations = [
        {"type": "message", "model": "claude-opus-5-5", "input_tokens": 535, "output_tokens": 20},
        {"type": "fallback_message", "model": "claude-fallback-test", "input_tokens": 412, "output_tokens": 264},
    ]
    for iteration in iterations:
        iteration.update(cache_read_input_tokens=0, cache_creation_input_tokens=0)
    FakeClaude(
        monkeypatch,
        message(text({"text": "hi"}), tokens=(412, 264), model="claude-fallback-test", iterations=iterations),
    )

    _, usage = await ask(claude("claude-opus-5-5"))

    assert usage == AITokenUsage(prompt_tokens=947, completion_tokens=284, model="claude-fallback-test")


@pytest.mark.asyncio
async def test_the_servers_own_anthropic_settings_never_reach_a_provider(monkeypatch: pytest.MonkeyPatch):
    """A group manager can point a provider anywhere, so it must only ever send its own settings"""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://server-proxy.test")
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "X-Server-Secret: s3cret\nX-Org: server")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "server-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "server-token")
    api = FakeClaude(monkeypatch, message(text({"text": "hi"})), message(text({"text": "hi"})))

    first_party = claude("claude-opus-5-5", request_headers={"X-Org": "1"})
    elsewhere = claude(base_url="https://claude-proxy.test")
    await ask(first_party)
    await ask(elsewhere)

    to_anthropic, to_proxy = api.requests
    assert to_anthropic.url.host == "api.anthropic.com"
    assert anthropic_adapter.is_first_party(first_party)
    assert to_proxy.url.host == "claude-proxy.test"
    for request in api.requests:
        assert "x-server-secret" not in request.headers
        assert "authorization" not in request.headers
        assert request.headers["x-api-key"] == "sk-ant-test"
    assert to_anthropic.headers["x-org"] == "1"
    assert "x-org" not in to_proxy.headers

    # Nor does a blank key (one that can't be decrypted) fall back to the server's
    client = anthropic_adapter.get_client(claude(api_key=""))
    assert (client.api_key, client.auth_token) == ("", None)


@pytest.mark.parametrize(
    ("stop_reason", "error"),
    [("refusal", AIProviderRefusedError), ("max_tokens", AIProviderOutputTruncatedError)],
)
@pytest.mark.asyncio
async def test_stop_reasons_that_fail_the_attempt(
    monkeypatch: pytest.MonkeyPatch, stop_reason: str, error: type[Exception]
):
    FakeClaude(monkeypatch, message(text('{"text": "hel'), stop_reason=stop_reason, tokens=(40, 9)))
    usage = AITokenUsage()

    with pytest.raises(error):
        await ask(claude(), usage=usage)

    # The tokens were still spent
    assert usage == AITokenUsage(prompt_tokens=40, completion_tokens=9)


@pytest.mark.asyncio
async def test_an_answer_without_text_is_none(monkeypatch: pytest.MonkeyPatch):
    FakeClaude(monkeypatch, message())

    result, usage = await ask(claude())

    assert result is None
    assert usage.prompt_tokens == 12


@pytest.mark.asyncio
async def test_audio_is_unsupported(monkeypatch: pytest.MonkeyPatch):
    api = FakeClaude(monkeypatch)

    with pytest.raises(AIProviderUnsupportedError):
        await ask(claude(), attachments=[OpenAILocalAudio(data="AAAA", format="mp3")])

    assert api.requests == []


RESPONSE_SCHEMAS = [
    schema
    for name in response_schemas.__all__
    if isinstance(schema := getattr(response_schemas, name), type) and issubclass(schema, OpenAIBase)
] + [
    # the recipe card pipeline's own (docs/ai/PHASE2.md §4.2)
    card_schemas.OpenAIRecipeCardTranscription,
    card_schemas.OpenAIRecipeCardUnsure,
    card_schemas.OpenAIRecipeCardTranscript,
    card_schemas.OpenAIRecipeCardRegion,
]
"""
Every response schema upstream asks providers for (`mealie/schema/openai`), and the models they nest; and the recipe
card pipeline's
"""

MAX_UNION_PARAMETERS = 16
MAX_OPTIONAL_PARAMETERS = 24
"""
Claude's limits for a request's strict schemas, beyond which it answers 400 "Schema is too complex for
compilation" (platform.claude.com/docs/en/build-with-claude/structured-outputs)
"""


def schema_nodes(node: Any) -> Iterator[dict]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from schema_nodes(value)
    elif isinstance(node, list):
        for value in node:
            yield from schema_nodes(value)


def parameters(schema: dict) -> Iterator[tuple[dict, bool]]:
    """Every object property in the schema, its definitions included, and whether it's required"""
    for node in schema_nodes(schema):
        if node.get("type") == "object":
            required = set(node.get("required", []))
            for name, prop in node["properties"].items():
                yield prop, name in required


def minimal_answer(schema: dict, root: dict) -> Any:
    """The smallest answer the schema allows: only the required properties, with empty values"""
    if ref := schema.get("$ref"):
        return minimal_answer(root["$defs"][ref.removeprefix("#/$defs/")], root)
    if branches := schema.get("anyOf"):
        return minimal_answer(branches[0], root)

    match schema.get("type"):
        case "object":
            return {name: minimal_answer(schema["properties"][name], root) for name in schema.get("required", [])}
        case "array":
            return []
        case "string":
            return schema.get("enum", [""])[0]
        case "integer" | "number":
            return 0
        case "boolean":
            return False
    return None


def test_there_are_response_schemas_to_check():
    assert {OpenAIRecipe, OpenAIText} <= set(RESPONSE_SCHEMAS)


@pytest.mark.parametrize("response_schema", RESPONSE_SCHEMAS, ids=lambda schema: schema.__name__)
def test_upstreams_response_schemas_fit_claudes_strict_form_and_limits(response_schema: type[OpenAIBase]):
    schema = anthropic_adapter.output_schema(response_schema)

    objects = [node for node in schema_nodes(schema) if node.get("type") == "object"]
    assert objects
    for node in objects:
        assert node["additionalProperties"] is False
        assert set(node.get("required", [])) <= set(node["properties"])

    params = list(parameters(schema))
    unions = [prop for prop, _ in params if "anyOf" in prop or isinstance(prop.get("type"), list)]
    optional = [prop for prop, required in params if not required]
    assert len(unions) <= MAX_UNION_PARAMETERS
    assert len(optional) <= MAX_OPTIONAL_PARAMETERS

    # An answer with only what's required still parses, the rest reading as their defaults
    parsed = response_schema.parse_openai_response(json.dumps(minimal_answer(schema, schema)))
    assert isinstance(parsed, response_schema)


def test_optional_fields_are_not_nullable_but_still_read_as_none():
    schema = anthropic_adapter.output_schema(OpenAIRecipe)
    properties = schema["properties"]

    assert schema["required"] == ["name"]
    assert properties["description"]["type"] == "string"
    assert properties["description"]["description"].startswith("A brief description of the recipe")
    assert properties["nutrition"] == {"$ref": "#/$defs/OpenAIRecipeNutrition"}
    assert schema["$defs"]["OpenAIRecipeNutrition"]["properties"]["calories"]["type"] == "string"
    assert schema["$defs"]["OpenAIRecipeIngredient"]["properties"]["title"]["type"] == "string"

    recipe = OpenAIRecipe.parse_openai_response('{"name": "Soup", "ingredients": [{"text": "1 onion"}]}')
    assert (recipe.description, recipe.nutrition, recipe.ingredients[0].title) == (None, None, None)


# ==========================================
# Through OpenAIService


def service() -> OpenAIService:
    repos = MagicMock()
    repos.group_ai_provider_settings.get_one.return_value = None
    return OpenAIService(repos)


def models_page(*models: dict) -> httpx2.Response:
    return httpx2.Response(200, json={"data": list(models), "has_more": False, "first_id": None, "last_id": None})


@pytest.mark.asyncio
async def test_list_models(monkeypatch: pytest.MonkeyPatch):
    def model(model_id: str, **kwargs: Any) -> dict:
        return {"type": "model", "id": model_id, "created_at": "2026-01-01T00:00:00Z", **kwargs}

    api = FakeClaude(
        monkeypatch,
        models_page(
            model("claude-c", display_name="Claude C"),
            model("claude-a", display_name="Claude A", capabilities={"image_input": {"supported": True}}),
            model("claude-b", display_name="Claude B", capabilities={"image_input": {"supported": False}}),
        ),
    )

    models = await service().list_models(claude())

    assert [(m.id, m.display_name, m.supports_images) for m in models] == [
        ("claude-a", "Claude A", True),
        ("claude-b", "Claude B", False),
        ("claude-c", "Claude C", None),
    ]
    assert api.requests[0].url.path == "/v1/models"


@pytest.mark.asyncio
async def test_connection_test_for_claude(monkeypatch: pytest.MonkeyPatch):
    """Upstream's connection test, image check included, works for Claude unchanged"""
    api = FakeClaude(
        monkeypatch,
        message(text({"text": "Hello!"})),
        message(text({"text": "Tomato & Egg Stir-Fry"})),
    )

    result = await service().test_connection(claude())

    assert result.success is True
    assert result.supports_images is True
    text_check, image_check = api.bodies
    assert [block["type"] for block in text_check["messages"][0]["content"]] == ["text"]
    assert [block["type"] for block in image_check["messages"][0]["content"]] == ["image", "text"]


@pytest.mark.asyncio
async def test_connection_test_failure_never_returns_claudes_response_body(monkeypatch: pytest.MonkeyPatch):
    body = {"type": "error", "error": {"type": "authentication_error", "message": "top secret detail"}}
    FakeClaude(monkeypatch, httpx2.Response(401, json=body))

    result = await service().test_connection(claude())

    assert result.success is False
    assert result.message == "AuthenticationError (HTTP 401)"


@pytest.mark.asyncio
async def test_list_models_stops_at_the_limit_when_the_server_never_stops_paging(monkeypatch: pytest.MonkeyPatch):
    """A base URL pointing somewhere hostile can say there's always another page; listing must still end"""

    class EndlessClaude(FakeClaude):
        def handler(self, request: httpx2.Request) -> httpx2.Response:
            self.requests.append(request)
            page = len(self.requests)
            ids = [f"claude-{page:04d}-{n:03d}" for n in range(100)]
            data = [{"type": "model", "id": i, "created_at": "2026-01-01T00:00:00Z"} for i in ids]
            return httpx2.Response(200, json={"data": data, "has_more": True, "first_id": ids[0], "last_id": ids[-1]})

    api = EndlessClaude(monkeypatch)

    models = await service().list_models(claude())

    assert len(models) == listing.MAX_LISTED_MODELS
    assert len(api.requests) == listing.MAX_LISTED_MODELS // 100


@pytest.mark.asyncio
async def test_take_reads_no_further_than_the_limit():
    read: list[int] = []

    async def endless() -> AsyncIterator[int]:
        n = 0
        while True:
            read.append(n)
            yield n
            n += 1

    assert [n async for n in listing.take(endless(), 3)] == [0, 1, 2]
    assert read == [0, 1, 2]
    assert [n async for n in listing.take(endless(), 0)] == []
