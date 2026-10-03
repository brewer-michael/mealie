"""Which providers each AI slot tries, and in what order (docs/ai/PHASE1.md §1 and §4)"""

from typing import Any

import pytest

from mealie.schema.group.ai_providers import AIProviderCreate, AIProviderOut, AIProviderSettingsUpdate, AIProviderSlot
from mealie.schema.group.ai_routing import AIUsageLogCreate
from mealie.services.ai.errors import AIProviderLimitReachedError, describe_provider_error
from mealie.services.openai import OpenAINotEnabledException, OpenAIService
from tests.utils.factories import random_string
from tests.utils.fixture_schemas import TestUser


def create_provider(user: TestUser, **kwargs: Any) -> AIProviderOut:
    return user.repos.group_ai_providers.create(
        AIProviderCreate(name=kwargs.pop("name", random_string()), model="m", api_key="k", **kwargs)
    )


def set_primaries(
    user: TestUser,
    *,
    default: AIProviderOut | None = None,
    image: AIProviderOut | None = None,
    audio: AIProviderOut | None = None,
) -> None:
    user.repos.group_ai_provider_settings.update(
        user.repos.group_id,
        AIProviderSettingsUpdate(
            default_provider_id=default.id if default else None,
            image_provider_id=image.id if image else None,
            audio_provider_id=audio.id if audio else None,
        ),
    )


def set_routes(user: TestUser, **routes: list[AIProviderOut]) -> None:
    user.repos.group_ai_provider_routes.replace_routes(
        {AIProviderSlot(slot): [provider.id for provider in providers] for slot, providers in routes.items()}
    )


def log_usage(user: TestUser, provider: AIProviderOut, tokens: int) -> None:
    user.repos.group_ai_usage.create(
        AIUsageLogCreate(
            provider_id=provider.id,
            provider_name=provider.name,
            model=provider.model,
            protocol=provider.protocol,
            slot=AIProviderSlot.default,
            prompt_tokens=tokens // 2,
            completion_tokens=tokens - tokens // 2,
            success=True,
        )
    )


def candidates(user: TestUser, slot: AIProviderSlot) -> list[AIProviderOut]:
    # A new service each time: like upstream, it reads the primary providers when it's created
    return OpenAIService(user.repos).runtime.candidates(slot)


def test_primary_comes_first_then_routes_without_duplicates(unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    a, b, c = (create_provider(user) for _ in range(3))
    set_primaries(user, default=a)
    set_routes(user, default=[b, a, c])

    assert candidates(user, AIProviderSlot.default) == [a, b, c]


def test_image_and_audio_have_their_own_primary_and_routes(unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    a, b, c = (create_provider(user) for _ in range(3))
    set_primaries(user, default=a, image=b)
    set_routes(user, image=[c], audio=[a])

    assert candidates(user, AIProviderSlot.image) == [b, c]
    assert candidates(user, AIProviderSlot.default) == [a]


@pytest.mark.parametrize("slot", [AIProviderSlot.default, AIProviderSlot.image, AIProviderSlot.audio])
def test_a_slot_with_routes_but_no_primary_has_no_providers(unique_user_fn_scoped: TestUser, slot: AIProviderSlot):
    """Upstream's checks (e.g. whether AI is enabled at all) only look at the primary providers"""
    user = unique_user_fn_scoped
    a, b = create_provider(user), create_provider(user)
    set_primaries(user, **{slot.value: None})
    set_routes(user, **{slot.value: [a, b]})

    with pytest.raises(OpenAINotEnabledException, match=f"No {slot.value} provider set"):
        candidates(user, slot)


@pytest.mark.parametrize("slot", [AIProviderSlot.fast, AIProviderSlot.planner])
def test_fast_and_planner_fall_back_to_default_until_they_have_routes(
    unique_user_fn_scoped: TestUser, slot: AIProviderSlot
):
    user = unique_user_fn_scoped
    a, b, c = (create_provider(user) for _ in range(3))
    set_primaries(user, default=a)
    set_routes(user, default=[b])

    assert candidates(user, slot) == [a, b]

    set_routes(user, **{slot.value: [c]})
    assert candidates(user, slot) == [c]


def test_embedding_never_falls_back_to_default(unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    a, b = create_provider(user), create_provider(user)
    set_primaries(user, default=a)

    with pytest.raises(OpenAINotEnabledException, match="No embedding provider set"):
        candidates(user, AIProviderSlot.embedding)

    set_routes(user, embedding=[b])
    assert candidates(user, AIProviderSlot.embedding) == [b]


@pytest.mark.parametrize(
    ("slot", "message"),
    [
        (AIProviderSlot.default, "No default provider set"),
        (AIProviderSlot.image, "No image provider set"),
        (AIProviderSlot.audio, "No audio provider set"),
        # fast and planner borrow the default slot's providers, so it's the default slot that's missing
        (AIProviderSlot.fast, "No default provider set"),
        (AIProviderSlot.planner, "No default provider set"),
    ],
)
def test_a_slot_without_providers_raises_upstreams_exception(
    unique_user_fn_scoped: TestUser, slot: AIProviderSlot, message: str
):
    with pytest.raises(OpenAINotEnabledException) as e:
        candidates(unique_user_fn_scoped, slot)

    assert e.value.message == message


def test_providers_at_their_monthly_limit_are_skipped(unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    at_limit = create_provider(user, monthly_token_limit=100)
    under_limit = create_provider(user, monthly_token_limit=1000)
    unlimited = create_provider(user)
    set_primaries(user, default=at_limit)
    set_routes(user, default=[under_limit, unlimited])

    log_usage(user, at_limit, 60)
    log_usage(user, at_limit, 40)
    log_usage(user, under_limit, 999)
    log_usage(user, unlimited, 10_000)

    assert candidates(user, AIProviderSlot.default) == [under_limit, unlimited]


def test_every_provider_at_its_limit_raises(unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    a = create_provider(user, name="Primary", monthly_token_limit=10)
    b = create_provider(user, name="Backup", monthly_token_limit=5)
    set_primaries(user, default=a)
    set_routes(user, default=[b])
    log_usage(user, a, 10)
    log_usage(user, b, 50)

    with pytest.raises(AIProviderLimitReachedError) as e:
        candidates(user, AIProviderSlot.default)

    # Our own message, so it's safe to show as-is
    assert describe_provider_error(e.value) == (
        "Every AI provider for default tasks has reached its monthly token limit (Primary, Backup)."
    )
