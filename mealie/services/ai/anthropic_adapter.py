"""
Claude through Anthropic's native Messages API, using the official `anthropic` SDK (docs/ai/PHASE1.md §2).

`OpenAIService` hands requests for providers whose protocol is `anthropic` to `get_response` here, and
lists their models with `list_models`. The rest of Mealie only ever sees the parsed response schema.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from mealie.core.root_logger import get_logger
from mealie.schema.group.ai_providers import AIProviderOut
from mealie.schema.group.ai_routing import AIProviderModelInfo
from mealie.schema.openai._base import OpenAIBase
from mealie.services.openai.openai import OpenAIAttachment, OpenAIImageBase, OpenAILocalAudio

from .errors import AIProviderOutputTruncatedError, AIProviderRefusedError, AIProviderUnsupportedError
from .listing import MAX_LISTED_MODELS, take
from .local import local_only_http_client
from .usage import AITokenUsage

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

logger = get_logger(__name__)

MAX_TOKENS = 16000
FIRST_PARTY_HOST = "api.anthropic.com"
FIRST_PARTY_URL = f"https://{FIRST_PARTY_HOST}"

REFUSAL_FALLBACK_BETA = "server-side-fallback-2026-07-01"
REFUSAL_FALLBACK_MODELS = frozenset({"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"})
"""
Models whose safety refusals Anthropic can retry on its recommended fallback model itself, when asked
to with `fallbacks: "default"` and the beta above
"""

STRUCTURED_OUTPUTS_BETA = "structured-outputs-2025-12-15"
"""Sent alongside other betas, as the SDK's own `beta.messages.parse` helper does"""


def get_client(provider: AIProviderOut) -> AsyncAnthropic:
    """
    A client for the provider's settings only. The SDK would otherwise also read the server's own environment:
    `ANTHROPIC_BASE_URL` for a blank base URL, and the headers in `ANTHROPIC_CUSTOM_HEADERS`, which would then
    go to whatever host a group manager sets. (It reads credentials from the environment only when no
    `api_key` is given, and the provider's key is always passed, even when it's blank.)

    Under a local-only call policy it connects only to the private addresses it checks (`local_only_http_client`).
    """
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(
        api_key=provider.api_key,
        base_url=_base_url(provider),
        timeout=provider.timeout,
        default_headers=provider.request_headers or None,
        default_query=provider.request_params or None,
        http_client=local_only_http_client(provider),
    )
    # The SDK has no option to skip ANTHROPIC_CUSTOM_HEADERS, which it merges into these
    client._custom_headers = dict(provider.request_headers or {})
    return client


def _base_url(provider: AIProviderOut) -> str:
    if not provider.base_url:
        return FIRST_PARTY_URL

    # The SDK adds `/v1/...` itself; drop it from a base URL copied from an OpenAI-compatible setup
    # (e.g. `https://api.anthropic.com/v1/`), which would otherwise request `/v1/v1/messages`
    return provider.base_url.rstrip("/").removesuffix("/v1") or provider.base_url


def is_first_party(provider: AIProviderOut) -> bool:
    """Whether the provider talks to Anthropic's own API, rather than a proxy or another host"""
    return not provider.base_url or urlparse(provider.base_url).hostname == FIRST_PARTY_HOST


def uses_refusal_fallback(provider: AIProviderOut) -> bool:
    return is_first_party(provider) and provider.model in REFUSAL_FALLBACK_MODELS


def build_content(message: str, attachments: Iterable[OpenAIAttachment] | None = None) -> list[dict[str, Any]]:
    """The user turn's content blocks: images first, then the message"""
    attachments = list(attachments or [])
    for attachment in attachments:
        if isinstance(attachment, OpenAILocalAudio):
            raise AIProviderUnsupportedError("Anthropic (Claude) providers can't process audio.")
        if not isinstance(attachment, OpenAIImageBase):
            raise AIProviderUnsupportedError(
                f"Anthropic (Claude) providers can't process {type(attachment).__name__} attachments."
            )

    blocks = [_image_block(a.get_image_url()) for a in attachments if isinstance(a, OpenAIImageBase)]
    blocks.append({"type": "text", "text": message})
    return blocks


def _image_block(url: str) -> dict[str, Any]:
    if url.startswith("data:"):
        # Local images arrive as the downscaled JPEG data URL OpenAILocalImage already builds
        header, _, data = url.partition(",")
        media_type = header.removeprefix("data:").split(";")[0] or "image/jpeg"
        return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}

    return {"type": "image", "source": {"type": "url", "url": url}}


def output_schema(response_schema: type[OpenAIBase]) -> dict[str, Any]:
    """
    `response_schema` in the strict form Claude's structured outputs take: the SDK's own transform (as its
    `parse` helpers use), with `null` dropped from optional fields.

    Claude compiles at most 16 parameters with union types (`anyOf`, including `X | None`) and 24 optional
    parameters per request, and answers "Schema is too complex for compilation" otherwise. Upstream's
    schemas mark most fields both optional and nullable (`OpenAIRecipe` alone has 20 such unions), but an
    optional field the model leaves out reads as None all the same.
    """
    import anthropic

    return _without_optional_nulls(anthropic.transform_schema(response_schema))


def _without_optional_nulls(schema: dict[str, Any]) -> dict[str, Any]:
    schema = dict(schema)
    if isinstance(defs := schema.get("$defs"), dict):
        schema["$defs"] = {name: _without_optional_nulls(definition) for name, definition in defs.items()}

    if isinstance(properties := schema.get("properties"), dict):
        required = set(schema.get("required", []))
        schema["properties"] = {
            name: _without_optional_nulls(prop if name in required else _non_nullable(prop))
            for name, prop in properties.items()
        }

    if isinstance(items := schema.get("items"), dict):
        schema["items"] = _without_optional_nulls(items)

    for keyword in ("anyOf", "allOf"):
        if isinstance(branches := schema.get(keyword), list):
            schema[keyword] = [_without_optional_nulls(branch) for branch in branches]

    return schema


def _non_nullable(prop: dict[str, Any]) -> dict[str, Any]:
    branches = prop.get("anyOf")
    if not isinstance(branches, list):
        return prop

    others = [branch for branch in branches if branch != {"type": "null"}]
    if not others or len(others) == len(branches):
        return prop
    if len(others) > 1:
        return {**prop, "anyOf": others}

    (branch,) = others
    if "$ref" in branch:
        # A reference alone, as the SDK's transform sends every reference (dropping e.g. the field's description)
        return branch

    return {**branch, **{key: value for key, value in prop.items() if key != "anyOf"}}


def _read_usage(response: Any, provider: AIProviderOut, usage: AITokenUsage) -> None:
    response_usage = getattr(response, "usage", None)
    if response_usage is None:
        return

    # With a server-side fallback the top-level counts cover only the attempt that produced the answer, and
    # `iterations` has an entry for each attempt, the declined ones included (which may or may not be billed,
    # depending on why they were declined; they're counted all the same)
    attempts = getattr(response_usage, "iterations", None) or [response_usage]
    usage.prompt_tokens = sum(getattr(attempt, "input_tokens", 0) or 0 for attempt in attempts)
    usage.completion_tokens = sum(getattr(attempt, "output_tokens", 0) or 0 for attempt in attempts)

    # e.g. the model Anthropic fell back to
    if (model := getattr(response, "model", None)) and model != provider.model:
        usage.model = model


def _final_text(content: Iterable[Any]) -> str:
    """
    The text of the response. With a server-side fallback, only what follows the last `fallback`
    block counts: what precedes it is the output of the model that declined.
    """
    texts: list[str] = []
    for block in content:
        block_type = getattr(block, "type", None)
        if block_type == "fallback":
            texts.clear()
        elif block_type == "text":
            texts.append(block.text)

    return "".join(texts)


async def _create(client: AsyncAnthropic, provider: AIProviderOut, request: dict[str, Any]) -> Any:
    import anthropic

    if not uses_refusal_fallback(provider):
        return await client.messages.create(**request)

    try:
        return await client.beta.messages.create(
            **request, betas=[REFUSAL_FALLBACK_BETA, STRUCTURED_OUTPUTS_BETA], fallbacks="default"
        )
    except anthropic.BadRequestError as e:
        if "fallback" not in str(e).lower():
            raise

    logger.warning(f"AI provider '{provider.name}' rejected server-side refusal fallbacks; retrying without them")
    return await client.messages.create(**request)


async def get_response[T: OpenAIBase](
    prompt: str,
    message: str,
    *,
    response_schema: type[T],
    provider: AIProviderOut,
    attachments: Iterable[OpenAIAttachment] | None = None,
    usage: AITokenUsage | None = None,
) -> tuple[T | None, AITokenUsage]:
    """
    Sends one request to a Claude provider and parses the answer as `response_schema`.

    Token usage is written to `usage` (when given) as soon as the response arrives, so it's known
    even when the response is then rejected. Raises `AIProviderRefusedError` if the model declined,
    `AIProviderOutputTruncatedError` if it ran out of output tokens, `AIProviderUnsupportedError` for
    audio attachments, and the SDK's own errors for failed requests.
    """
    usage = usage if usage is not None else AITokenUsage()

    # No `thinking`, sampling settings (`temperature` etc.) or assistant prefill: current models think
    # adaptively by default and reject the others. Structured output uses the schema the SDK's `parse`
    # helpers would send (see `output_schema`), but the response is parsed here: those helpers validate
    # every text block before the stop reason or a fallback can be looked at.
    request: dict[str, Any] = {
        "model": provider.model,
        "max_tokens": MAX_TOKENS,
        "system": prompt,
        "messages": [{"role": "user", "content": build_content(message, attachments)}],
        "output_config": {"format": {"type": "json_schema", "schema": output_schema(response_schema)}},
    }

    async with get_client(provider) as client:
        response = await _create(client, provider, request)

    _read_usage(response, provider, usage)

    if response.stop_reason == "refusal":
        raise AIProviderRefusedError(f"The model ({provider.model}) declined to answer this request.")
    if response.stop_reason == "max_tokens":
        raise AIProviderOutputTruncatedError(
            f"The model ({provider.model}) ran out of output tokens before finishing its answer."
        )

    text = _final_text(response.content or [])
    if not text:
        return None, usage

    return response_schema.parse_openai_response(text), usage


def _capability(capabilities: Any, name: str) -> bool | None:
    capability = capabilities.get(name) if isinstance(capabilities, dict) else getattr(capabilities, name, None)
    supported = capability.get("supported") if isinstance(capability, dict) else getattr(capability, "supported", None)
    return supported if isinstance(supported, bool) else None


async def list_models(provider: AIProviderOut) -> list[AIProviderModelInfo]:
    """The provider's models, sorted by id. Provider errors propagate unchanged."""
    async with get_client(provider) as client:
        models = [
            AIProviderModelInfo(
                id=model.id,
                display_name=getattr(model, "display_name", None) or None,
                supports_images=_capability(getattr(model, "capabilities", None), "image_input"),
            )
            async for model in take(client.models.list(), MAX_LISTED_MODELS)
        ]

    return sorted(models, key=lambda model: model.id)
