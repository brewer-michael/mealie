"""
A call policy whose "local only" is asked again before every provider call (docs/ai/PHASE2.md §10): a manager who
switches the group's setting on while a card is being read keeps the card's remaining calls on the group's network.
"""

import logging
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest

from mealie.schema.group.ai_providers import AIProviderCreate, AIProviderOut, AIProviderSettingsUpdate, AIProviderSlot
from mealie.schema.openai.general import OpenAIText
from mealie.services.ai import local
from mealie.services.ai.errors import AIProviderLocalOnlyError
from mealie.services.ai.local import local_only_http_client
from mealie.services.ai.policy import NO_POLICY, AICallPolicy, ai_call_policy, apply_policy, is_local_only
from mealie.services.openai import OpenAIService
from tests.unit_tests.services_tests.ai.test_ai_provider_fallback import FakeProviders
from tests.utils.fixture_schemas import TestUser

LAN_URL = "http://127.0.0.1:11434/v1"
"""A loopback address: local without a DNS lookup"""


def _provider(name: str, *, base_url: str | None = None, runs_locally: bool = False) -> AIProviderOut:
    return AIProviderOut(id=uuid4(), name=name, model="m", api_key="k", base_url=base_url, runs_locally=runs_locally)


def _create(user: TestUser, name: str, **kwargs: Any) -> AIProviderOut:
    return user.repos.group_ai_providers.create(AIProviderCreate(name=name, model="m", api_key="k", **kwargs))


def _configure(user: TestUser, default: AIProviderOut, *routes: AIProviderOut) -> None:
    user.repos.group_ai_provider_settings.update(
        user.repos.group_id,
        AIProviderSettingsUpdate(default_provider_id=default.id, image_provider_id=None, audio_provider_id=None),
    )
    user.repos.group_ai_provider_routes.replace_routes({AIProviderSlot.default: [p.id for p in routes]})


class Switch:
    """The group's local-only setting as a check reads it, counting the reads"""

    def __init__(self, on: bool = False) -> None:
        self.on = on
        self.reads = 0

    def __call__(self) -> bool:
        self.reads += 1
        return self.on


def failing_check() -> Callable[[], bool]:
    def check() -> bool:
        raise RuntimeError("database is gone")

    return check


# ==========================================
# The check


def test_without_a_check_the_flag_decides():
    assert not is_local_only(NO_POLICY)
    assert is_local_only(AICallPolicy(local_only=True))


def test_the_check_is_asked_every_time_unless_the_flag_is_already_on():
    switch = Switch()
    policy = AICallPolicy(local_only_check=switch)

    assert not is_local_only(policy)
    switch.on = True
    assert is_local_only(policy)
    assert switch.reads == 2

    # a job that's local-only anyway never needs it
    always = Switch()
    assert is_local_only(AICallPolicy(local_only=True, local_only_check=always))
    assert always.reads == 0


def test_a_check_that_fails_counts_as_local_only_and_is_logged_once(caplog: pytest.LogCaptureFixture):
    job_id = uuid4()
    policy = AICallPolicy(job_id=job_id, local_only_check=failing_check())

    with caplog.at_level(logging.WARNING):
        assert is_local_only(policy)
        assert is_local_only(policy)

    warnings = [record for record in caplog.records if "local-only" in record.getMessage()]
    assert len(warnings) == 1
    assert str(job_id) in warnings[0].getMessage()
    assert "database is gone" not in warnings[0].getMessage()  # the error's type only


def test_the_check_takes_no_part_in_comparing_policies():
    """`AICallPolicy` stays a value: the same flags and job are the same policy, whatever reads the setting"""
    job_id = uuid4()
    assert AICallPolicy(job_id=job_id, local_only_check=Switch()) == AICallPolicy(job_id=job_id)


# ==========================================
# Routing


def test_a_switch_between_two_calls_filters_the_second():
    lan = _provider("LAN", base_url=LAN_URL, runs_locally=True)
    cloud = _provider("Cloud")
    switch = Switch()

    with ai_call_policy(AICallPolicy(local_only_check=switch)):
        assert apply_policy(AIProviderSlot.default, [cloud, lan]) == [cloud, lan]
        switch.on = True
        assert apply_policy(AIProviderSlot.default, [cloud, lan]) == [lan]
        with pytest.raises(AIProviderLocalOnlyError):
            apply_policy(AIProviderSlot.default, [cloud])


def test_a_failing_check_keeps_calls_local():
    lan = _provider("LAN", base_url=LAN_URL, runs_locally=True)
    with ai_call_policy(AICallPolicy(local_only_check=failing_check())):
        assert apply_policy(AIProviderSlot.default, [_provider("Cloud"), lan]) == [lan]


@pytest.mark.asyncio
async def test_a_task_obeys_a_switch_made_between_its_calls(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    cloud = _create(user, "Cloud")
    lan = _create(user, "LAN", base_url=LAN_URL, runs_locally=True)
    _configure(user, cloud, lan)
    fake = FakeProviders().install(monkeypatch)
    switch = Switch()
    service = OpenAIService(user.repos)

    with ai_call_policy(AICallPolicy(local_only_check=switch)):
        first = await service.get_response("prompt", "message", response_schema=OpenAIText)
        switch.on = True  # a manager switches local-only on while the card is read
        second = await service.get_response("prompt", "message", response_schema=OpenAIText)

    assert (first, second) == (OpenAIText(text="from Cloud"), OpenAIText(text="from LAN"))
    assert fake.calls == ["Cloud", "LAN"]


def test_the_sdk_clients_follow_the_check(monkeypatch: pytest.MonkeyPatch):
    """A call made after the switch also connects only to the address it checks (`private_http_client`)"""
    made: list[str] = []
    monkeypatch.setattr(local, "private_http_client", lambda provider: made.append(provider.name) or "private")
    lan = _provider("LAN", base_url=LAN_URL, runs_locally=True)
    switch = Switch()

    with ai_call_policy(AICallPolicy(local_only_check=switch)):
        assert local_only_http_client(lan) is None
        switch.on = True
        assert local_only_http_client(lan) == "private"
    assert made == ["LAN"]
