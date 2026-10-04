"""
"Keep recipe cards on this server" (docs/ai/PHASE2.md §10): which providers count as local, the per-task policy that
filters every slot and fails closed, the job id on usage rows, and the reader the privacy chip shows.
"""

import asyncio
import socket
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa

from mealie.db.db_setup import session_context
from mealie.repos.all_repositories import get_repositories
from mealie.schema.group.ai_providers import AIProviderCreate, AIProviderOut, AIProviderSettingsUpdate, AIProviderSlot
from mealie.schema.group.ai_routing import AIUsageLogCreate
from mealie.schema.openai.general import OpenAIText
from mealie.services import ocr
from mealie.services.ai import local
from mealie.services.ai.errors import AIProviderLocalOnlyError
from mealie.services.ai.ingest.pipeline.service import JobOpenAIService
from mealie.services.ai.local import card_reader, is_local_provider, local_readiness
from mealie.services.ai.policy import AICallPolicy, ai_call_policy, apply_policy, current_policy
from mealie.services.openai import OpenAINotEnabledException, OpenAIService
from tests.unit_tests.services_tests.ai.test_ai_provider_fallback import FakeProviders
from tests.utils.fixture_schemas import TestUser

LAN_HOSTS = {
    "ollama.lan": ["192.168.1.20"],
    "tailnet.box": ["100.101.102.103"],
    "mixed.example": ["10.0.0.5", "8.8.8.8"],
}
PUBLIC_HOSTS = {"api.example.com": ["93.184.216.34"]}


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Resolves the test host names without a network; IP literals resolve as they are"""
    lookups: list[str] = []
    real = socket.getaddrinfo

    def getaddrinfo(host: str, *args: Any, **kwargs: Any) -> list:
        lookups.append(host)
        addresses = {**LAN_HOSTS, **PUBLIC_HOSTS}.get(host)
        if addresses is None:
            try:
                return real(host, *args, **kwargs)
            except OSError:
                raise socket.gaierror(socket.EAI_NONAME, "Name or service not known") from None
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0)) for address in addresses]

    monkeypatch.setattr(local.socket, "getaddrinfo", getaddrinfo)
    local.clear_address_cache()
    yield lookups
    local.clear_address_cache()


def _provider(name: str = "p", *, base_url: str | None = None, runs_locally: bool = False) -> AIProviderOut:
    return AIProviderOut(id=uuid4(), name=name, model="m", api_key="k", base_url=base_url, runs_locally=runs_locally)


def _create(
    user: TestUser,
    name: str,
    *,
    base_url: str | None = None,
    runs_locally: bool = False,
    monthly_token_limit: int | None = None,
) -> AIProviderOut:
    return user.repos.group_ai_providers.create(
        AIProviderCreate(
            name=name,
            model="m",
            api_key="k",
            base_url=base_url,
            runs_locally=runs_locally,
            monthly_token_limit=monthly_token_limit,
        )
    )


def _spend(user: TestUser, provider: AIProviderOut, tokens: int) -> None:
    """This month's usage of `provider`"""
    user.repos.group_ai_usage.create(
        AIUsageLogCreate(
            provider_id=provider.id,
            provider_name=provider.name,
            model="m",
            protocol=provider.protocol,
            slot=AIProviderSlot.default,
            prompt_tokens=tokens,
            completion_tokens=0,
            success=True,
        )
    )


def _configure(
    user: TestUser,
    *,
    default: AIProviderOut | None = None,
    image: AIProviderOut | None = None,
    audio: AIProviderOut | None = None,
    routes: dict[AIProviderSlot, list[AIProviderOut]] | None = None,
) -> None:
    user.repos.group_ai_provider_settings.update(
        user.repos.group_id,
        AIProviderSettingsUpdate(
            default_provider_id=default.id if default else None,
            image_provider_id=image.id if image else None,
            audio_provider_id=audio.id if audio else None,
        ),
    )
    user.repos.group_ai_provider_routes.replace_routes(
        {slot: [provider.id for provider in providers] for slot, providers in (routes or {}).items()}
    )


# ==========================================
# What counts as local


@pytest.mark.parametrize(
    "base_url, runs_locally, expected",
    [
        ("http://127.0.0.1:11434/v1", True, True),
        ("http://localhost:11434/v1", True, True),
        ("http://[::1]:11434/v1", True, True),
        ("http://ollama.lan:11434/v1", True, True),
        ("http://tailnet.box/v1", True, True),  # CGNAT, as Tailscale uses
        ("http://ollama.lan:11434/v1", False, False),  # the manager didn't say so
        (None, True, False),  # the SDK's default is the cloud
        ("", True, False),
        ("https://api.example.com/v1", True, False),  # a public address
        ("http://mixed.example/v1", True, False),  # one public address is enough to refuse
        ("http://no-such-host.invalid/v1", True, False),
    ],
)
def test_local_means_marked_local_and_resolving_only_to_private_addresses(
    base_url: str | None, runs_locally: bool, expected: bool
):
    assert is_local_provider(_provider(base_url=base_url, runs_locally=runs_locally)) is expected


def test_address_lookups_are_cached(fake_dns: list[str], monkeypatch: pytest.MonkeyPatch):
    provider = _provider(base_url="http://ollama.lan/v1", runs_locally=True)
    assert is_local_provider(provider)
    assert is_local_provider(provider)
    assert fake_dns.count("ollama.lan") == 1

    now = local.time.monotonic()
    monkeypatch.setattr(local.time, "monotonic", lambda: now + local.ADDRESS_CACHE_SECONDS + 1)
    assert is_local_provider(provider)
    assert fake_dns.count("ollama.lan") == 2


# ==========================================
# The policy


def test_without_a_policy_every_provider_is_allowed():
    providers = [_provider("cloud"), _provider("lan", base_url="http://ollama.lan/v1", runs_locally=True)]
    assert current_policy() == AICallPolicy()
    assert apply_policy(AIProviderSlot.default, providers) == providers


def test_local_only_keeps_local_providers_in_order_and_fails_closed():
    lan = _provider("lan", base_url="http://ollama.lan/v1", runs_locally=True)
    cloud = _provider("cloud", base_url="https://api.example.com/v1", runs_locally=True)
    loopback = _provider("loopback", base_url="http://127.0.0.1:8080/v1", runs_locally=True)

    with ai_call_policy(local_only=True):
        assert apply_policy(AIProviderSlot.image, [cloud, loopback, lan]) == [loopback, lan]
        with pytest.raises(AIProviderLocalOnlyError) as e:
            apply_policy(AIProviderSlot.default, [cloud, _provider("unmarked")])
    assert "default" in str(e.value)

    # the block ended: the policy with it
    assert apply_policy(AIProviderSlot.default, [cloud]) == [cloud]


def test_the_policy_follows_awaits_and_threads():
    async def inside() -> AICallPolicy:
        await asyncio.sleep(0)
        return await asyncio.to_thread(current_policy)

    job_id = uuid4()
    with ai_call_policy(AICallPolicy(local_only=True, job_id=job_id)):
        assert asyncio.run(inside()) == AICallPolicy(local_only=True, job_id=job_id)


def test_every_slot_is_filtered(unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    cloud = _create(user, "Cloud", base_url="https://api.example.com/v1")
    cloud_image = _create(user, "Cloud vision")
    cloud_audio = _create(user, "Cloud audio")
    lan = _create(user, "Ollama", base_url="http://ollama.lan:11434/v1", runs_locally=True)
    _configure(
        user,
        default=cloud,
        image=cloud_image,
        audio=cloud_audio,
        routes={AIProviderSlot.default: [lan], AIProviderSlot.embedding: [cloud]},
    )
    runtime = OpenAIService(user.repos).runtime

    # unfiltered
    assert [p.name for p in runtime.candidates(AIProviderSlot.default)] == ["Cloud", "Ollama"]

    with ai_call_policy(local_only=True):
        assert [p.name for p in runtime.candidates(AIProviderSlot.default)] == ["Ollama"]
        # fast and planner fall back to the default slot's providers, filtered the same way
        assert [p.name for p in runtime.candidates(AIProviderSlot.fast)] == ["Ollama"]
        assert [p.name for p in runtime.candidates(AIProviderSlot.planner)] == ["Ollama"]
        for slot in (AIProviderSlot.image, AIProviderSlot.audio, AIProviderSlot.embedding):
            with pytest.raises(AIProviderLocalOnlyError):
                runtime.candidates(slot)


@pytest.mark.asyncio
async def test_a_local_only_request_never_reaches_a_cloud_provider(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    cloud = _create(user, "Cloud")
    backup = _create(user, "Backup", base_url="https://api.example.com/v1", runs_locally=True)
    _configure(user, default=cloud, routes={AIProviderSlot.default: [backup]})
    fake = FakeProviders().install(monkeypatch)

    with ai_call_policy(local_only=True), pytest.raises(AIProviderLocalOnlyError):
        await OpenAIService(user.repos).get_response("prompt", "message", response_schema=OpenAIText)

    assert fake.calls == []
    assert user.repos.group_ai_usage.get_all() == []


@pytest.mark.asyncio
async def test_usage_rows_carry_the_job_id(unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch):
    user = unique_user_fn_scoped
    lan = _create(user, "Ollama", base_url="http://ollama.lan/v1", runs_locally=True)
    _configure(user, default=lan)
    FakeProviders().install(monkeypatch)
    job_id = uuid4()

    with ai_call_policy(AICallPolicy(local_only=True, job_id=job_id)):
        answer = await OpenAIService(user.repos).get_response("prompt", "message", response_schema=OpenAIText)
    assert answer == OpenAIText(text="from Ollama")
    await OpenAIService(user.repos).get_response("prompt", "message", response_schema=OpenAIText)

    rows = sorted(user.repos.group_ai_usage.get_all(), key=lambda row: row.created_at or 0)
    assert [row.job_id for row in rows] == [job_id, None]


# ==========================================
# Sessions


def test_the_base_runtime_leaves_a_request_session_alone(unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    _configure(user, default=_create(user, "Cloud"))

    with session_context() as session:
        repos = get_repositories(session, group_id=user.repos.group_id, household_id=None)
        service = OpenAIService(repos)
        session.execute(sa.select(1))
        assert session.in_transaction()

        service.runtime.candidates(AIProviderSlot.default)
        assert session.in_transaction()

        # a card task's runtime ends its own session's transaction before every provider await
        job_service = JobOpenAIService(repos)
        job_service.runtime.candidates(AIProviderSlot.default)
        assert not session.in_transaction()


# ==========================================
# The privacy chip's reader and the managers' readiness list


def test_the_reader_is_the_image_slots_first_provider(unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    _configure(user, default=_create(user, "Claude Sonnet"), image=_create(user, "Claude Vision"))
    service = OpenAIService(user.repos)

    reader = card_reader(service, local_only=False)
    assert reader is not None
    assert (reader.name, reader.local, reader.via_ocr) == ("Claude Vision", False, False)

    assert card_reader(service, local_only=True) is None


def test_local_only_falls_back_to_ocr_and_a_local_text_provider(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    lan = _create(user, "Ollama", base_url="http://ollama.lan/v1", runs_locally=True)
    _configure(user, default=lan, image=_create(user, "Claude Vision"))
    service = OpenAIService(user.repos)

    monkeypatch.setattr(ocr, "is_available", lambda: True)
    assert card_reader(service, local_only=False).name == "Claude Vision"  # type: ignore[union-attr]
    reader = card_reader(service, local_only=True)
    assert reader is not None
    assert (reader.name, reader.local, reader.via_ocr) == ("Ollama", True, True)

    monkeypatch.setattr(ocr, "is_available", lambda: False)
    assert card_reader(service, local_only=True) is None


def test_a_local_image_provider_reads_local_only_cards(unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    vision = _create(user, "qwen3-vl", base_url="http://127.0.0.1:11434/v1", runs_locally=True)
    text = _create(user, "Ollama", base_url="http://ollama.lan/v1", runs_locally=True)
    _configure(user, default=text, image=vision)

    reader = card_reader(OpenAIService(user.repos), local_only=True)
    assert reader is not None
    assert (reader.name, reader.local, reader.via_ocr) == ("qwen3-vl", True, False)


def test_no_text_provider_means_no_reader(unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ocr, "is_available", lambda: True)
    service = OpenAIService(unique_user_fn_scoped.repos)
    with pytest.raises(OpenAINotEnabledException):
        service.runtime.candidates(AIProviderSlot.default)
    assert card_reader(service, local_only=False) is None


def test_readiness_lists_local_providers_per_slot_and_those_that_wont_be_used(unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    lan = _create(user, "Ollama", base_url="http://ollama.lan/v1", runs_locally=True)
    proxy = _create(user, "LAN proxy", base_url="https://api.example.com/v1", runs_locally=True)
    no_url = _create(user, "No URL", runs_locally=True)
    cloud = _create(user, "Cloud")
    _configure(user, default=cloud, image=proxy, routes={AIProviderSlot.default: [lan, no_url]})

    readiness = local_readiness(OpenAIService(user.repos))

    assert readiness.default == ["Ollama"]
    assert readiness.fast == ["Ollama"]
    assert readiness.image == []
    assert readiness.not_private == ["LAN proxy", "No URL"]
    # it doesn't leave a policy behind
    assert current_policy() == AICallPolicy()


# ==========================================
# Over the monthly limit, the reader and the providers are still named (LO4): `limitReached` reports the limit


def test_over_the_monthly_limit_the_reader_is_still_named(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    lan = _create(user, "Ollama", base_url="http://ollama.lan/v1", runs_locally=True, monthly_token_limit=100)
    vision = _create(user, "qwen3-vl", base_url="http://127.0.0.1:11434/v1", runs_locally=True, monthly_token_limit=100)
    _configure(user, default=lan, image=vision)
    _spend(user, lan, 500)
    _spend(user, vision, 500)
    service = OpenAIService(user.repos)
    monkeypatch.setattr(ocr, "is_available", lambda: False)

    for local_only in (False, True):
        reader = card_reader(service, local_only=local_only)
        assert reader is not None
        assert (reader.name, reader.local, reader.via_ocr) == ("qwen3-vl", True, False)

    readiness = local_readiness(service)
    assert (readiness.image, readiness.default, readiness.fast) == (["qwen3-vl"], ["Ollama"], ["Ollama"])

    # with OCR, an image slot over its limit is stood in for: the card is read from Tesseract's text
    monkeypatch.setattr(ocr, "is_available", lambda: True)
    reader = card_reader(service, local_only=True)
    assert reader is not None
    assert (reader.name, reader.via_ocr) == ("Ollama", True)


def test_the_reader_is_the_first_provider_within_its_limit(unique_user_fn_scoped: TestUser):
    # the one that reads: a primary over its limit hands over to the next route
    user = unique_user_fn_scoped
    capped = _create(user, "Capped", monthly_token_limit=100)
    fallback = _create(user, "Fallback")
    _configure(user, default=_create(user, "Text"), image=capped, routes={AIProviderSlot.image: [fallback]})
    _spend(user, capped, 500)

    reader = card_reader(OpenAIService(user.repos), local_only=False)
    assert reader is not None and reader.name == "Fallback"
    assert OpenAIService(user.repos).runtime.allowed(AIProviderSlot.image) == [capped, fallback]
