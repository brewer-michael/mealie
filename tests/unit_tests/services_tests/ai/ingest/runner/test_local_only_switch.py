"""
A card being read when a manager switches on "Keep recipe card photos and text on this server" (docs/ai/PHASE2.md §10):
the worker's call policy reads the group's setting again before each provider call, so the card's remaining calls stay
on this server (a local provider, or none: `local_only_unavailable`). A change never loosens a job.
"""

import logging
from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from ingest_runner_testing import FakeHandlers, Jobs, extract_result, run, settle

from mealie.db.db_setup import session_context
from mealie.repos.all_repositories import get_repositories
from mealie.repos.repository_recipe_ingest import IngestSettingsRepo
from mealie.schema.group.ai_providers import AIProviderCreate, AIProviderOut, AIProviderSettingsUpdate, AIProviderSlot
from mealie.schema.openai.general import OpenAIText
from mealie.schema.recipe_ingest import IngestErrorCode, IngestStatus, RecipeIngestionSettingsUpdate
from mealie.services.ai.ingest import limits
from mealie.services.ai.ingest.pipeline.service import JobOpenAIService
from mealie.services.ai.ingest.runner import worker
from mealie.services.ai.ingest.runner.dispatcher import IngestDispatcher
from mealie.services.ai.ingest.runner.types import TaskContext
from tests.unit_tests.services_tests.ai.test_ai_provider_fallback import FakeProviders
from tests.utils.fixture_schemas import TestUser


@pytest.fixture(autouse=True)
def recheck_every_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "LOCAL_ONLY_RECHECK", 0)


@pytest.fixture()
def group_settings(jobs: Jobs) -> Iterator[IngestSettingsRepo]:
    """The group's recipe card settings, put back afterwards"""
    settings = jobs.repos.settings
    before = settings.get()
    settings.upsert(RecipeIngestionSettingsUpdate(local_only=False, cross_read=before.cross_read))
    yield settings
    settings.upsert(before)


def _provider(user: TestUser, name: str, **values: Any) -> AIProviderOut:
    return user.repos.group_ai_providers.create(AIProviderCreate(name=name, model="m", api_key="k", **values))


@pytest.fixture()
def providers(unique_user: TestUser) -> Iterator[Any]:
    """Sets the group's default provider (a cloud one) and its fallbacks; removed afterwards"""
    created: list[AIProviderOut] = []

    def configure(*, with_local: bool) -> None:
        cloud = _provider(unique_user, "Cloud")
        created.append(cloud)
        fallbacks = []
        if with_local:
            lan = _provider(unique_user, "Ollama", base_url="http://127.0.0.1:11434/v1", runs_locally=True)
            created.append(lan)
            fallbacks.append(lan.id)
        unique_user.repos.group_ai_provider_settings.update(
            unique_user.repos.group_id,
            AIProviderSettingsUpdate(default_provider_id=cloud.id, image_provider_id=None, audio_provider_id=None),
        )
        unique_user.repos.group_ai_provider_routes.replace_routes({AIProviderSlot.default: fallbacks})

    yield configure
    unique_user.repos.group_ai_provider_routes.replace_routes({})
    unique_user.repos.group_ai_provider_settings.update(
        unique_user.repos.group_id,
        AIProviderSettingsUpdate(default_provider_id=None, image_provider_id=None, audio_provider_id=None),
    )
    for provider in created:
        unique_user.repos.group_ai_providers.delete(provider.id)


def _switch_on_between_two_calls(settings: IngestSettingsRepo, answers: list[str]) -> Any:
    """A handler that asks the default slot twice, as reading then structuring a card do, with the switch between"""

    async def handler(ctx: TaskContext) -> Any:
        with session_context() as session:
            ai = JobOpenAIService(get_repositories(session, group_id=ctx.group_id, household_id=ctx.household_id))
            first = await ai.get_response("read the card", "card", response_schema=OpenAIText)
            answers.append(first.text if first else "")
            # a manager switches on "Keep recipe card photos and text on this server" meanwhile
            settings.upsert(RecipeIngestionSettingsUpdate(local_only=True, cross_read=False))
            second = await ai.get_response("build the recipe", "card", response_schema=OpenAIText)
            answers.append(second.text if second else "")
        return extract_result()

    return handler


@pytest.mark.parametrize("with_local", [True, False])
def test_switching_local_only_on_while_a_card_is_read_keeps_its_next_calls_on_this_server(
    dispatcher: IngestDispatcher,
    jobs: Jobs,
    handlers: FakeHandlers,
    group_settings: IngestSettingsRepo,
    providers: Any,
    monkeypatch: pytest.MonkeyPatch,
    with_local: bool,
):
    providers(with_local=with_local)
    fake = FakeProviders().install(monkeypatch)
    answers: list[str] = []
    handlers.default = _switch_on_between_two_calls(group_settings, answers)
    job_id = jobs.create(local_only=False)

    async def scenario() -> None:
        await dispatcher.run_once()
        await settle(dispatcher)

    run(scenario())
    row = jobs.row(job_id)
    if with_local:
        assert fake.calls == ["Cloud", "Ollama"]  # the build step went to the local provider
        assert answers == ["from Cloud", "from Ollama"]
        assert row["status"] == IngestStatus.ready
    else:
        assert fake.calls == ["Cloud"]  # no cloud call after the switch: the card fails closed
        assert (row["status"], row["error_code"]) == (IngestStatus.failed, IngestErrorCode.local_only_unavailable)


def test_a_change_never_loosens_a_task_and_a_failed_read_keeps_the_last_value(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    group_id = uuid4()
    answers: list[Any] = [RuntimeError("database restarting"), RuntimeError("still down"), True, False]

    def get(self: IngestSettingsRepo) -> RecipeIngestionSettingsUpdate:
        assert self.group_id == group_id
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return RecipeIngestionSettingsUpdate(local_only=answer)

    monkeypatch.setattr(IngestSettingsRepo, "get", get)
    check = worker._GroupLocalOnly(uuid4(), group_id, now_on=False)

    with caplog.at_level(logging.WARNING):
        assert check() is False  # the read failed: the last value read stays
        assert check() is False
    assert len([record for record in caplog.records if "local-only setting" in record.getMessage()]) == 1
    assert check() is True  # switched on
    assert check() is True  # switched off again: not for this task
    assert answers == [False]  # once on, it isn't read again


def test_the_setting_is_read_at_most_every_few_seconds(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(limits, "LOCAL_ONLY_RECHECK", 3600)
    reads: list[UUID] = []

    def get(self: IngestSettingsRepo) -> RecipeIngestionSettingsUpdate:
        reads.append(self.group_id)
        return RecipeIngestionSettingsUpdate(local_only=True)

    monkeypatch.setattr(IngestSettingsRepo, "get", get)
    check = worker._GroupLocalOnly(uuid4(), uuid4(), now_on=False)
    assert [check(), check(), check()] == [False, False, False]  # the worker read it when the task started
    assert reads == []
