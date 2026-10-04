"""
A recipe card task's AI usage while a backup restore pauses ingestion (docs/ai/PHASE2.md §3.5, §3.9): every attempt
still goes into the usage log, which the monthly token limits are summed from, so a restore that fails (an invalid
backup, or one busy for too long) leaves no tokens uncounted. A write that fails because the restore has the tables
is logged in one line, without a traceback.
"""

import asyncio
import logging
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

from mealie.repos.repository_ai_routing import GroupRepositoryAIUsage
from mealie.schema.openai.general import OpenAIText
from mealie.services.ai import runtime as runtime_module
from mealie.services.ai.errors import AIProviderLimitReachedError
from mealie.services.ai.ingest import storage
from mealie.services.ai.ingest.pipeline.service import JobOpenAIService, end_transaction
from tests.unit_tests.services_tests.ai.ingest.pipeline.card_fakes import (
    Call,
    FakeCardAI,
    configure,
    create_provider,
    job_session,
)
from tests.utils.fixture_schemas import TestUser

TOKENS = 600
"""Each answer's prompt tokens: two answers go over the provider's limit of 1000"""


@pytest.fixture(autouse=True)
def no_pause_left_behind() -> Iterator[None]:
    yield
    storage.pause_marker_path().unlink(missing_ok=True)


def _answer(call: Call) -> dict[str, Any]:
    usage = runtime_module._tracked_usage.get()
    assert usage is not None
    usage.prompt_tokens = TOKENS
    return {"text": "hi"}


def test_usage_during_a_restore_that_fails_counts_toward_the_limit(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    provider = create_provider(user, "Limited", monthly_token_limit=1000)
    configure(user, default=provider)
    FakeCardAI({"OpenAIText": _answer}).install(monkeypatch)

    started, release = threading.Event(), threading.Event()
    errors: list[BaseException] = []

    @storage.pauses_ingest
    def failing_restore() -> None:
        # as `BackupV2.restore` with an invalid backup: it fails before replacing anything
        started.set()
        release.wait(10)
        raise ValueError("Invalid backup file")

    def restore() -> None:
        try:
            failing_restore()
        except BaseException as e:
            errors.append(e)

    thread = threading.Thread(target=restore)
    thread.start()
    try:
        assert started.wait(10)
        assert storage.is_paused()
        with job_session(user) as (session, repos):
            ai = JobOpenAIService(repos)
            for _ in range(2):
                assert asyncio.run(ai.get_response("p", "m", response_schema=OpenAIText)) == OpenAIText(text="hi")
    finally:
        release.set()
        thread.join(10)
    assert [type(error) for error in errors] == [ValueError]
    assert not storage.is_paused()

    with job_session(user) as (session, repos):
        assert repos.group_ai_usage.monthly_tokens([provider.id])[provider.id] == 2 * TOKENS
        end_transaction(session)
        with pytest.raises(AIProviderLimitReachedError):
            asyncio.run(JobOpenAIService(repos).get_response("p", "m", response_schema=OpenAIText))


@pytest.mark.parametrize("paused", [True, False])
def test_a_usage_write_the_restore_breaks_is_logged_without_a_traceback(
    unique_user_fn_scoped: TestUser,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    paused: bool,
):
    user = unique_user_fn_scoped
    provider = create_provider(user, "Text")
    configure(user, default=provider)
    FakeCardAI({"OpenAIText": _answer}).install(monkeypatch)
    if paused:
        storage.pause_marker_path().write_text(f"{time.time():.3f}")

    def dropped(self: GroupRepositoryAIUsage, data: Any) -> Any:
        raise OperationalError("INSERT INTO ai_usage_log", {}, Exception("no such table: ai_usage_log"))

    monkeypatch.setattr(GroupRepositoryAIUsage, "create", dropped)
    caplog.set_level(logging.INFO)
    with job_session(user) as (session, repos):
        ai = JobOpenAIService(repos)
        assert asyncio.run(ai.get_response("p", "m", response_schema=OpenAIText)) == OpenAIText(text="hi")
        assert not session.in_transaction()
    assert [usage.prompt_tokens for usage in ai.runtime.usage] == [TOKENS]  # the job's own tally keeps them

    records = [record for record in caplog.records if "AI usage" in record.getMessage()]
    assert len(records) == 1
    (record,) = records
    if paused:
        assert record.levelno == logging.INFO and record.exc_info is None
        assert record.getMessage() == (
            "AI usage for provider 'Text' wasn't recorded during a backup restore: OperationalError"
        )
    else:
        # outside a restore it's a real failure, logged as before
        assert record.levelno == logging.ERROR and record.exc_info is not None
