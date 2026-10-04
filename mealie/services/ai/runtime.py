"""
The fork's side of `OpenAIService` (docs/ai/PHASE1.md §1, §2 and §4): trying each of a slot's providers in
turn, sending Claude providers through the native adapter, logging every attempt in the usage log, and listing
a provider's models.

Upstream's `mealie/services/openai/openai.py` only hands over to this module at a few points, so that it stays
easy to merge.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING

from mealie.core import exceptions
from mealie.core.root_logger import get_logger
from mealie.schema.group.ai_providers import AIProviderOut, AIProviderProtocol, AIProviderSlot
from mealie.schema.group.ai_routing import AIProviderModelInfo
from mealie.schema.openai._base import OpenAIBase

from .errors import AIProviderUnsupportedError, describe_provider_error, is_rate_limit_error
from .listing import MAX_LISTED_MODELS, take
from .policy import apply_policy
from .routing import AIProviderRouter
from .usage import AITokenUsage, record_ai_usage

if TYPE_CHECKING:
    from openai.types.chat import ChatCompletion

    from mealie.services.openai.openai import OpenAIAttachment, OpenAIService

logger = get_logger(__name__)

TRANSCRIPTION_FEATURE = "Transcription"
"""The usage log's `feature` for audio transcriptions, which have no response schema"""

EMPTY_RESPONSE = "EmptyResponse"
"""The usage log's `error_type` for a provider that answered with nothing"""

_tracked_usage: ContextVar[AITokenUsage | None] = ContextVar("ai_tracked_usage", default=None)


@contextmanager
def track_usage() -> Iterator[AITokenUsage]:
    """
    Collects the tokens a provider reports using for the request made inside the block, so an attempt can be
    logged without passing a usage object through upstream's code. Scoped to the current task.
    """
    usage = AITokenUsage()
    token = _tracked_usage.set(usage)
    try:
        yield usage
    finally:
        _tracked_usage.reset(token)


def capture_openai_usage(completion: ChatCompletion) -> None:
    """Called by `OpenAIService._get_raw_response` with each completion, before its finish reason is checked"""
    if (usage := _tracked_usage.get()) is not None and completion.usage:
        usage.prompt_tokens = completion.usage.prompt_tokens
        usage.completion_tokens = completion.usage.completion_tokens


async def close_client(client: object) -> None:
    """
    Closes an OpenAI client on the event loop that used it. Recipe card tasks run in threads with their own loops
    (docs/ai/PHASE2.md §3.2); an unclosed client is freed by the cyclic garbage collector, whose `__del__` schedules
    `aclose()` on whatever loop is running then, and logs "Event loop is closed". Test doubles may have no `close`.
    """
    if (close := getattr(client, "close", None)) is not None:
        await close()


async def get_claude_response[T: OpenAIBase](
    prompt: str,
    message: str,
    response_schema: type[T],
    provider: AIProviderOut,
    attachments: Sequence[OpenAIAttachment] | None = None,
) -> T | None:
    """One request to a Claude provider, through the native adapter, for upstream's single-provider path"""
    from . import anthropic_adapter

    response, _ = await anthropic_adapter.get_response(
        prompt,
        message,
        response_schema=response_schema,
        provider=provider,
        attachments=attachments,
        usage=_tracked_usage.get(),
    )
    return response


def slot_for(attachments: Sequence[OpenAIAttachment] | None) -> AIProviderSlot:
    """The slot whose primary provider upstream's `OpenAIService._get_provider` picks for these attachments"""
    from mealie.services.openai.openai import OpenAIImageBase, OpenAILocalAudio

    has_image = any(isinstance(a, OpenAIImageBase) for a in attachments or [])
    has_audio = any(isinstance(a, OpenAILocalAudio) for a in attachments or [])
    if has_image and has_audio:
        raise ValueError("Cannot process both images and audio in one request")

    if has_image:
        return AIProviderSlot.image
    if has_audio:
        return AIProviderSlot.audio
    return AIProviderSlot.default


class AIRuntime:
    """
    What `OpenAIService` hands over to (as `OpenAIService.runtime`). Subclasses can pin the providers or keep
    usage out of the log, as the recipe-card eval script does.
    """

    def __init__(self, service: OpenAIService) -> None:
        self.service = service

    def candidates(self, slot: AIProviderSlot) -> list[AIProviderOut]:
        """
        The providers to try for `slot`, in order (see `mealie.services.ai.routing`), as the current call policy
        allows (`mealie.services.ai.policy`). Raises upstream's `OpenAINotEnabledException` if the slot has none,
        `AIProviderLimitReachedError` if all of them are over their monthly token limit, and
        `AIProviderLocalOnlyError` if the policy is "local only" and none of them is local.
        """
        primaries = {
            AIProviderSlot.default: self.service.default_provider,
            AIProviderSlot.image: self.service.image_provider,
            AIProviderSlot.audio: self.service.audio_provider,
        }
        return apply_policy(slot, AIProviderRouter(self.service.repos, primaries).candidates(slot))

    def record_attempt(
        self,
        provider: AIProviderOut,
        *,
        slot: AIProviderSlot,
        feature: str,
        usage: AITokenUsage,
        latency_ms: int,
        error: BaseException | None = None,
        error_type: str | None = None,
    ) -> None:
        """Logs one provider attempt in the usage log. Never raises."""
        record_ai_usage(
            self.service.repos,
            provider,
            slot=slot,
            feature=feature,
            usage=usage,
            latency_ms=latency_ms,
            error=error,
            error_type=error_type,
        )

    async def get_response[T: OpenAIBase](
        self,
        prompt: str,
        message: str,
        response_schema: type[T],
        attachments: list[OpenAIAttachment] | None = None,
        slot: AIProviderSlot | None = None,
    ) -> T | None:
        """
        `OpenAIService.get_response` without an explicit provider: asks each of the slot's providers in turn
        (by default the slot upstream infers from the attachments), logging every attempt.

        Each attempt goes through upstream's own single-provider path. A provider that fails, or answers with
        nothing, hands over to the next one, and the last attempt decides the outcome: its error is raised as
        upstream raises it, or None is returned. Errors in choosing the providers (none set, all over their
        limit, images with audio) are raised as they are, before any attempt.
        """
        slot = slot or slot_for(attachments)
        candidates = self.candidates(slot)

        error: Exception | None = None
        for i, provider in enumerate(candidates):
            started = time.perf_counter()
            response: T | None = None
            with track_usage() as usage:
                try:
                    response = await self.service.get_response(
                        prompt, message, response_schema=response_schema, attachments=attachments, provider=provider
                    )
                    error = None
                except Exception as e:
                    error = e

            # upstream wraps the provider's error in its own; the log wants the provider's
            self.record_attempt(
                provider,
                slot=slot,
                feature=response_schema.__name__,
                usage=usage,
                latency_ms=int((time.perf_counter() - started) * 1000),
                error=(error.__cause__ or error) if error else None,
                error_type=EMPTY_RESPONSE if error is None and response is None else None,
            )
            if response is not None:
                return response

            next_attempt = "; trying the next provider" if i + 1 < len(candidates) else ""
            reason = describe_provider_error(error) if error else "no response"
            logger.warning(f"AI provider '{provider.name}' failed ({reason}){next_attempt}")

        if error is None:
            return None

        cause = error.__cause__ or error
        if is_rate_limit_error(cause) and not isinstance(error, exceptions.RateLimitError):
            # upstream only recognises OpenAI's rate limits, not Anthropic's
            raise exceptions.RateLimitError(str(cause)) from cause
        raise error

    async def transcribe_audio(self, audio_file_path: Path) -> str:
        """
        Transcribes the audio with each of the audio slot's OpenAI-compatible providers in turn, logging every
        attempt. Raises the last provider's error if none succeeds, for `OpenAIService.transcribe_audio` to fall
        back to a chat completion.
        """
        # Only the OpenAI API has a transcription endpoint; other providers take part in the chat fallback
        error: Exception = AIProviderUnsupportedError("None of the audio providers can transcribe audio.")
        for provider in self.candidates(AIProviderSlot.audio):
            if provider.protocol != AIProviderProtocol.openai:
                continue

            usage = AITokenUsage()
            started = time.perf_counter()
            try:
                client = self.service.get_client(provider)
                try:
                    with open(audio_file_path, "rb") as audio_file:
                        transcript = await client.audio.transcriptions.create(model=provider.model, file=audio_file)
                finally:
                    await close_client(client)
            except Exception as e:
                error = e
                latency_ms = int((time.perf_counter() - started) * 1000)
                self.record_attempt(
                    provider,
                    slot=AIProviderSlot.audio,
                    feature=TRANSCRIPTION_FEATURE,
                    usage=usage,
                    latency_ms=latency_ms,
                    error=e,
                )
                logger.warning(f"Transcribing with AI provider '{provider.name}' failed ({describe_provider_error(e)})")
                continue

            # Token-billed transcription models report usage; others report the audio's duration only
            transcript_usage = getattr(transcript, "usage", None)
            usage.prompt_tokens = getattr(transcript_usage, "input_tokens", 0) or 0
            usage.completion_tokens = getattr(transcript_usage, "output_tokens", 0) or 0
            latency_ms = int((time.perf_counter() - started) * 1000)
            self.record_attempt(
                provider, slot=AIProviderSlot.audio, feature=TRANSCRIPTION_FEATURE, usage=usage, latency_ms=latency_ms
            )
            return transcript.text

        raise error

    async def list_models(self, provider: AIProviderOut) -> list[AIProviderModelInfo]:
        """
        The models a provider offers, sorted by id, for the model picker when setting one up.

        Provider errors propagate unchanged; report them to users with
        `mealie.services.ai.errors.describe_provider_error`, never with the error's own message.
        """
        if provider.protocol == AIProviderProtocol.anthropic:
            from . import anthropic_adapter

            return await anthropic_adapter.list_models(provider)

        client = self.service.get_client(provider)
        try:
            # OpenAI-compatible model lists don't say which models read images
            models = [
                AIProviderModelInfo(id=model.id, display_name=None, supports_images=None)
                async for model in take(client.models.list(), MAX_LISTED_MODELS)
            ]
        finally:
            await close_client(client)
        return sorted(models, key=lambda model: model.id)
