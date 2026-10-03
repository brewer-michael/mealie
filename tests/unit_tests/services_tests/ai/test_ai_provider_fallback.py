"""OpenAIService trying a slot's providers in turn, and logging every attempt (docs/ai/PHASE1.md §1 and §4)"""

import json
from collections.abc import AsyncIterator, Collection
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import anthropic
import httpx
import httpx2
import openai
import pytest

from mealie.core import exceptions
from mealie.lang.providers import get_locale_provider
from mealie.repos.repository_ai_routing import GroupRepositoryAIUsage
from mealie.schema.group.ai_providers import (
    AIProviderCreate,
    AIProviderOut,
    AIProviderProtocol,
    AIProviderSettingsUpdate,
    AIProviderSlot,
)
from mealie.schema.group.ai_routing import AIUsageLogOut
from mealie.schema.openai.general import OpenAIText
from mealie.schema.openai.recipe_ingredient import OpenAIIngredients
from mealie.schema.recipe.recipe import Recipe
from mealie.services.ai import anthropic_adapter
from mealie.services.ai.errors import AIProviderLimitReachedError
from mealie.services.openai import OpenAINotEnabledException, OpenAIService
from mealie.services.parser_services import RegisteredParser, get_parser
from mealie.services.recipe.import_workflow.context import WorkflowContext, WorkflowInput, WorkflowOptions
from mealie.services.recipe.import_workflow.steps.resolve_organizers import ResolveOrganizersStep
from mealie.services.recipe.import_workflow.steps.translate_recipe import TranslateRecipeStep
from tests.utils.fixture_schemas import TestUser


def create_provider(user: TestUser, name: str, **kwargs: Any) -> AIProviderOut:
    return user.repos.group_ai_providers.create(AIProviderCreate(name=name, model="m", api_key="k", **kwargs))


def configure(
    user: TestUser,
    *,
    default: AIProviderOut | None = None,
    audio: AIProviderOut | None = None,
    routes: dict[AIProviderSlot, list[AIProviderOut]] | None = None,
) -> None:
    user.repos.group_ai_provider_settings.update(
        user.repos.group_id,
        AIProviderSettingsUpdate(
            default_provider_id=default.id if default else None,
            audio_provider_id=audio.id if audio else None,
            image_provider_id=None,
        ),
    )
    user.repos.group_ai_provider_routes.replace_routes(
        {slot: [provider.id for provider in providers] for slot, providers in (routes or {}).items()}
    )


def usage_rows(user: TestUser) -> dict[str, AIUsageLogOut]:
    """The group's usage rows by provider name (each test gives a provider at most one)"""
    rows = user.repos.group_ai_usage.get_all()
    assert len({row.provider_name for row in rows}) == len(rows)
    return {row.provider_name: row for row in rows}


def outcomes(user: TestUser) -> dict[str, tuple[bool, str | None]]:
    return {name: (row.success, row.error_type) for name, row in usage_rows(user).items()}


def rate_limit_error(sdk: str) -> Exception:
    if sdk == "openai":
        request = httpx.Request("POST", "https://api.example.test/v1/chat/completions")
        return openai.RateLimitError("Rate limited", response=httpx.Response(429, request=request), body=None)

    request2 = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.RateLimitError("Rate limited", response=httpx2.Response(429, request=request2), body=None)


class FakeProviders:
    """
    Stands in for the OpenAI-compatible providers' APIs at `OpenAIService.get_client`, so that upstream's own
    request code runs. Each provider, by name, either fails with the given error or answers, reporting
    `tokens` (prompt, completion) of usage. `empty` providers answer without a choice and `truncated` ones run
    out of output tokens. Schemas other than `OpenAIText` and `OpenAIIngredients` get an empty answer.
    """

    def __init__(
        self,
        failures: dict[str, Exception] | None = None,
        *,
        empty: Collection[str] = (),
        truncated: Collection[str] = (),
        tokens: tuple[int, int] = (12, 5),
        transcription_failures: dict[str, Exception] | None = None,
    ) -> None:
        self.failures = failures or {}
        self.empty = empty
        self.truncated = truncated
        self.tokens = tokens
        self.transcription_failures = transcription_failures or {}
        self.calls: list[str] = []
        self.transcriptions: list[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeProviders:
        fake = self

        def get_client(self: OpenAIService, provider: AIProviderOut) -> Any:
            def parse(**kwargs: Any) -> Any:
                return fake.chat(provider, kwargs["response_format"])

            async def create(model: str, file: Any) -> Any:
                return fake.transcribe(provider)

            return SimpleNamespace(
                chat=SimpleNamespace(completions=SimpleNamespace(with_streaming_response=SimpleNamespace(parse=parse))),
                audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create)),
            )

        monkeypatch.setattr(OpenAIService, "get_client", get_client)
        return self

    def answer(self, provider: AIProviderOut, response_schema: type) -> dict | None:
        if provider.name in self.empty:
            return None
        if response_schema is OpenAIText:
            return {"text": f"from {provider.name}"}
        if response_schema is OpenAIIngredients:
            return {"ingredients": [{"quantity": 1, "unit": "cup", "food": "flour", "note": ""}]}
        return None

    @asynccontextmanager
    async def chat(self, provider: AIProviderOut, response_schema: type) -> AsyncIterator[Any]:
        self.calls.append(provider.name)
        if error := self.failures.get(provider.name):
            raise error

        answer = self.answer(provider, response_schema)
        choice = {
            "index": 0,
            "finish_reason": "length" if provider.name in self.truncated else "stop",
            "message": {"role": "assistant", "content": json.dumps(answer)},
        }
        prompt_tokens, completion_tokens = self.tokens
        completion = {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": provider.model,
            "choices": [choice] if answer else [],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
        yield SimpleNamespace(text=AsyncMock(return_value=json.dumps(completion)))

    def transcribe(self, provider: AIProviderOut) -> Any:
        self.transcriptions.append(provider.name)
        if error := self.transcription_failures.get(provider.name):
            raise error

        usage = SimpleNamespace(type="tokens", input_tokens=40, output_tokens=8, total_tokens=48)
        return SimpleNamespace(text=f"transcribed by {provider.name}", usage=usage)


class FakeClaude:
    """Stands in for the Claude adapter, recording calls by provider name"""

    def __init__(self, failure: Exception | None = None, tokens: tuple[int, int] = (30, 7)) -> None:
        self.failure = failure
        self.tokens = tokens
        self.calls: list[dict[str, Any]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeClaude:
        async def get_response(prompt, message, *, response_schema, provider, attachments=None, usage=None):
            self.calls.append({"prompt": prompt, "message": message, "provider": provider.name})
            if usage is not None:
                usage.prompt_tokens, usage.completion_tokens = self.tokens
            if self.failure:
                raise self.failure
            return response_schema(text=f"from {provider.name}"), usage

        monkeypatch.setattr(anthropic_adapter, "get_response", get_response)
        return self


async def ask(user: TestUser, **kwargs: Any) -> OpenAIText | None:
    return await OpenAIService(user.repos).get_response("prompt", "message", response_schema=OpenAIText, **kwargs)


@pytest.mark.asyncio
async def test_a_failing_provider_hands_over_to_the_next(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    primary, backup = create_provider(user, "Primary"), create_provider(user, "Backup")
    configure(user, default=primary, routes={AIProviderSlot.default: [backup]})
    fake = FakeProviders({"Primary": RuntimeError("down")}, tokens=(12, 5)).install(monkeypatch)

    response = await ask(user)

    assert response == OpenAIText(text="from Backup")
    assert fake.calls == ["Primary", "Backup"]

    rows = usage_rows(user)
    failed, succeeded = rows["Primary"], rows["Backup"]
    assert len(rows) == 2
    assert (failed.provider_id, failed.success, failed.error_type) == (primary.id, False, "RuntimeError")
    assert (succeeded.provider_id, succeeded.success, succeeded.error_type) == (backup.id, True, None)
    # The failed request never got an answer, so it reported no tokens
    assert (failed.prompt_tokens, failed.completion_tokens) == (0, 0)
    assert (succeeded.prompt_tokens, succeeded.completion_tokens) == (12, 5)
    for row in (failed, succeeded):
        assert row.slot == AIProviderSlot.default
        assert row.feature == "OpenAIText"
        assert row.protocol == AIProviderProtocol.openai
        assert row.model == "m"
        assert row.latency_ms >= 0


@pytest.mark.asyncio
async def test_a_truncated_answer_hands_over_and_its_tokens_are_logged(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    primary, backup = create_provider(user, "Primary"), create_provider(user, "Backup")
    configure(user, default=primary, routes={AIProviderSlot.default: [backup]})
    FakeProviders(truncated={"Primary"}, tokens=(50, 16000)).install(monkeypatch)

    assert await ask(user) == OpenAIText(text="from Backup")

    truncated = usage_rows(user)["Primary"]
    assert (truncated.success, truncated.error_type) == (False, "LengthFinishReasonError")
    assert (truncated.prompt_tokens, truncated.completion_tokens) == (50, 16000)


@pytest.mark.asyncio
async def test_when_every_provider_fails_the_last_error_is_raised_as_upstream_does(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    primary, backup = create_provider(user, "Primary"), create_provider(user, "Backup")
    configure(user, default=primary, routes={AIProviderSlot.default: [backup]})
    last_error = ValueError("bad answer")
    FakeProviders({"Primary": RuntimeError("down"), "Backup": last_error}).install(monkeypatch)

    with pytest.raises(Exception, match="OpenAI Request Failed. ValueError: bad answer") as e:
        await ask(user)

    assert e.value.__cause__ is last_error
    assert outcomes(user) == {"Primary": (False, "RuntimeError"), "Backup": (False, "ValueError")}


@pytest.mark.asyncio
async def test_an_empty_answer_hands_over_to_the_next_provider(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    primary, backup = create_provider(user, "Primary"), create_provider(user, "Backup")
    configure(user, default=primary, routes={AIProviderSlot.default: [backup]})
    fake = FakeProviders(empty={"Primary"}).install(monkeypatch)

    assert await ask(user) == OpenAIText(text="from Backup")
    assert fake.calls == ["Primary", "Backup"]
    assert outcomes(user) == {"Primary": (False, "EmptyResponse"), "Backup": (True, None)}


@pytest.mark.asyncio
async def test_an_empty_answer_from_the_last_provider_is_returned_as_none(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    primary, backup = create_provider(user, "Primary"), create_provider(user, "Backup")
    configure(user, default=primary, routes={AIProviderSlot.default: [backup]})
    FakeProviders({"Primary": RuntimeError("down")}, empty={"Backup"}).install(monkeypatch)

    # As upstream returns it for a provider that answers with nothing
    assert await ask(user) is None
    assert outcomes(user) == {"Primary": (False, "RuntimeError"), "Backup": (False, "EmptyResponse")}


@pytest.mark.parametrize("protocol", [AIProviderProtocol.openai, AIProviderProtocol.anthropic])
@pytest.mark.asyncio
async def test_a_rate_limit_as_the_last_error_is_raised_as_a_rate_limit(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, protocol: AIProviderProtocol
):
    user = unique_user_fn_scoped
    primary, backup = create_provider(user, "Primary"), create_provider(user, "Backup", protocol=protocol)
    configure(user, default=primary, routes={AIProviderSlot.default: [backup]})
    FakeProviders({"Primary": RuntimeError("down"), "Backup": rate_limit_error("openai")}).install(monkeypatch)
    FakeClaude(rate_limit_error("anthropic")).install(monkeypatch)

    with pytest.raises(exceptions.RateLimitError):
        await ask(user)

    assert outcomes(user)["Backup"] == (False, "RateLimitError")


@pytest.mark.asyncio
async def test_an_explicit_provider_gets_one_unlogged_attempt(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    primary, backup = create_provider(user, "Primary"), create_provider(user, "Backup")
    configure(user, default=primary, routes={AIProviderSlot.default: [backup]})
    fake = FakeProviders({"Backup": RuntimeError("down")}).install(monkeypatch)

    with pytest.raises(Exception, match="OpenAI Request Failed. RuntimeError: down"):
        await ask(user, provider=backup)

    # No fallback to the slot's providers, and nothing logged: connection tests may use unsaved providers
    assert fake.calls == ["Backup"]
    assert usage_rows(user) == {}

    assert await ask(user, provider=primary) == OpenAIText(text="from Primary")
    assert usage_rows(user) == {}


@pytest.mark.asyncio
async def test_connection_tests_and_pings_never_write_usage_rows(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    """Their providers may be unsaved (the usage log's provider_id is a foreign key), or not the group's at all"""
    user = unique_user_fn_scoped
    configure(user, default=create_provider(user, "Primary"))
    fake = FakeProviders().install(monkeypatch)
    claude = FakeClaude().install(monkeypatch)
    unsaved = AIProviderOut(id=uuid4(), name="Unsaved", model="m", api_key="k")
    unsaved_claude = AIProviderOut(
        id=uuid4(), name="Unsaved Claude", model="m", api_key="k", protocol=AIProviderProtocol.anthropic
    )
    service = OpenAIService(user.repos)

    assert (await service.test_connection(unsaved)).success
    assert (await service.test_connection(unsaved_claude)).success
    assert await service.ping(unsaved, "hello") == OpenAIText(text="from Unsaved")

    # The text check and the image check, for each provider, then the ping
    assert fake.calls == ["Unsaved", "Unsaved", "Unsaved"]
    assert [call["provider"] for call in claude.calls] == ["Unsaved Claude", "Unsaved Claude"]
    assert usage_rows(user) == {}


@pytest.mark.asyncio
async def test_an_explicit_slot_uses_that_slots_providers(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    primary, fast = create_provider(user, "Primary"), create_provider(user, "Fast")
    configure(user, default=primary, routes={AIProviderSlot.fast: [fast]})
    fake = FakeProviders().install(monkeypatch)

    assert await ask(user, slot=AIProviderSlot.fast) == OpenAIText(text="from Fast")
    assert fake.calls == ["Fast"]
    assert [row.slot for row in usage_rows(user).values()] == [AIProviderSlot.fast]


@pytest.mark.asyncio
async def test_slot_errors_are_raised_before_any_attempt(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    fake = FakeProviders().install(monkeypatch)

    with pytest.raises(OpenAINotEnabledException, match="No default provider set"):
        await ask(user)

    capped = create_provider(user, "Capped", monthly_token_limit=10)
    configure(user, default=capped)
    await ask(user)  # 17 tokens: over the limit from now on

    with pytest.raises(AIProviderLimitReachedError):
        await ask(user)

    assert fake.calls == ["Capped"]


@pytest.mark.asyncio
async def test_a_usage_log_failure_never_breaks_the_call(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    configure(user, default=create_provider(user, "Primary"))
    FakeProviders().install(monkeypatch)

    def broken_create(self, data):
        raise RuntimeError("database is down")

    monkeypatch.setattr(GroupRepositoryAIUsage, "create", broken_create)

    assert await ask(user) == OpenAIText(text="from Primary")


@pytest.mark.asyncio
async def test_claude_providers_go_through_the_adapter(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    claude = create_provider(user, "Claude", protocol=AIProviderProtocol.anthropic)
    configure(user, default=claude)
    fake = FakeProviders().install(monkeypatch)
    adapter = FakeClaude(tokens=(30, 7)).install(monkeypatch)

    assert await ask(user) == OpenAIText(text="from Claude")
    assert adapter.calls == [{"prompt": "prompt", "message": "message", "provider": "Claude"}]
    assert fake.calls == []

    (row,) = usage_rows(user).values()
    assert (row.protocol, row.prompt_tokens, row.completion_tokens, row.success) == ("anthropic", 30, 7, True)


@pytest.mark.asyncio
async def test_the_model_that_answered_is_logged(unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch):
    """e.g. the model Anthropic fell back to when the configured one declined"""
    user = unique_user_fn_scoped
    configure(user, default=create_provider(user, "Claude", protocol=AIProviderProtocol.anthropic))

    async def get_response(prompt, message, *, response_schema, provider, attachments=None, usage=None):
        usage.prompt_tokens, usage.completion_tokens, usage.model = 30, 7, "claude-fallback-test"
        return response_schema(text="hi"), usage

    monkeypatch.setattr(anthropic_adapter, "get_response", get_response)

    await ask(user)

    assert usage_rows(user)["Claude"].model == "claude-fallback-test"


# ==========================================
# Audio transcription


@pytest.fixture()
def audio_file(tmp_path: Path) -> Path:
    path = tmp_path / "clip.mp3"
    path.write_bytes(b"not really audio")
    return path


@pytest.mark.asyncio
async def test_transcription_tries_each_openai_compatible_audio_provider(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, audio_file: Path
):
    user = unique_user_fn_scoped
    primary = create_provider(user, "Primary")
    claude = create_provider(user, "Claude", protocol=AIProviderProtocol.anthropic)
    backup = create_provider(user, "Backup")
    configure(user, default=primary, audio=primary, routes={AIProviderSlot.audio: [claude, backup]})
    fake = FakeProviders(transcription_failures={"Primary": RuntimeError("down")}).install(monkeypatch)

    assert await OpenAIService(user.repos).transcribe_audio(audio_file) == "transcribed by Backup"

    # Claude has no transcription endpoint, so it's skipped here
    assert fake.transcriptions == ["Primary", "Backup"]
    rows = usage_rows(user)
    assert {name: (row.success, row.feature, row.slot) for name, row in rows.items()} == {
        "Primary": (False, "Transcription", "audio"),
        "Backup": (True, "Transcription", "audio"),
    }
    assert (rows["Backup"].prompt_tokens, rows["Backup"].completion_tokens) == (40, 8)


@pytest.mark.asyncio
async def test_transcription_falls_back_to_chat_completion(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, audio_file: Path
):
    user = unique_user_fn_scoped
    primary = create_provider(user, "Primary")
    configure(user, default=primary, audio=primary)
    fake = FakeProviders(transcription_failures={"Primary": RuntimeError("no transcription endpoint")})
    fake.install(monkeypatch)

    assert await OpenAIService(user.repos).transcribe_audio(audio_file) == "from Primary"
    assert fake.transcriptions == ["Primary"]
    assert fake.calls == ["Primary"]


@pytest.mark.asyncio
async def test_a_rate_limited_transcription_is_raised_without_the_chat_fallback(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch, audio_file: Path
):
    user = unique_user_fn_scoped
    primary = create_provider(user, "Primary")
    configure(user, default=primary, audio=primary)
    fake = FakeProviders(transcription_failures={"Primary": rate_limit_error("openai")}).install(monkeypatch)

    with pytest.raises(exceptions.RateLimitError):
        await OpenAIService(user.repos).transcribe_audio(audio_file)

    assert fake.calls == []


@pytest.mark.asyncio
async def test_transcription_without_an_audio_provider_raises_upstreams_exception(unique_user_fn_scoped: TestUser):
    with pytest.raises(OpenAINotEnabledException, match="No audio provider set"):
        await OpenAIService(unique_user_fn_scoped.repos).transcribe_audio(Path("unused.mp3"))


# ==========================================
# Callers on the `fast` slot


@pytest.fixture()
def fast_route(unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch) -> FakeProviders:
    """A group whose `fast` slot has its own provider, separate from the default one"""
    user = unique_user_fn_scoped
    configure(
        user,
        default=create_provider(user, "Default"),
        routes={AIProviderSlot.fast: [create_provider(user, "Fast")]},
    )
    return FakeProviders().install(monkeypatch)


def workflow_context(user: TestUser) -> WorkflowContext:
    return WorkflowContext(
        input=WorkflowInput(content="Soup"),
        options=WorkflowOptions(translate_language="fr", create_new_organizers=True),
        repos=user.repos,
        translator=get_locale_provider(),
        ai=OpenAIService(user.repos),
        draft_recipe=Recipe(name="Soup", description="A soup"),
    )


@pytest.mark.asyncio
async def test_ingredient_parsing_uses_the_fast_slot(unique_user_fn_scoped: TestUser, fast_route: FakeProviders):
    user = unique_user_fn_scoped
    parser = get_parser(RegisteredParser.openai, user.repos.group_id, user.repos.session, get_locale_provider())

    await parser.parse(["1 cup flour"])

    assert fast_route.calls == ["Fast"]


@pytest.mark.asyncio
async def test_translation_uses_the_fast_slot(unique_user_fn_scoped: TestUser, fast_route: FakeProviders):
    await TranslateRecipeStep().run(workflow_context(unique_user_fn_scoped))

    assert fast_route.calls == ["Fast"]


@pytest.mark.asyncio
async def test_organizer_resolution_uses_the_fast_slot(unique_user_fn_scoped: TestUser, fast_route: FakeProviders):
    await ResolveOrganizersStep().run(workflow_context(unique_user_fn_scoped))

    assert fast_route.calls == ["Fast"]


@pytest.mark.asyncio
async def test_other_callers_keep_inferring_their_slot(unique_user_fn_scoped: TestUser, fast_route: FakeProviders):
    assert await ask(unique_user_fn_scoped) == OpenAIText(text="from Default")
    assert fast_route.calls == ["Default"]
