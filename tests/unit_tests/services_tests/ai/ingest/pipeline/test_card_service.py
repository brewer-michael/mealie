"""The task's AI service and runtime tallies, card attachments and the context's progress keys"""

import base64
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from mealie.lang.providers import get_locale_provider
from mealie.schema.group.ai_providers import AIProviderSlot
from mealie.schema.openai.general import OpenAIText
from mealie.schema.recipe_ingest import ExtractionUsage
from mealie.services.ai.ingest.pipeline import CardPipelineOptions
from mealie.services.ai.ingest.pipeline.attachments import CardImage
from mealie.services.ai.ingest.pipeline.context import CardWorkflowContext
from mealie.services.ai.ingest.pipeline.service import JobAIRuntime, JobOpenAIService, end_transaction
from tests.unit_tests.services_tests.ai.ingest.pipeline.card_fakes import (
    FakeCardAI,
    configure,
    create_provider,
    job_session,
    make_pages,
    provider_failure,
)
from tests.utils.fixture_schemas import TestUser


@pytest.mark.asyncio
async def test_the_job_runtime_tallies_usage_per_feature(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    first, second = create_provider(user, "First"), create_provider(user, "Second")
    configure(user, default=first, routes={AIProviderSlot.default: [second]})
    FakeCardAI({"OpenAIText": {"text": "hi"}}, failures={"First": provider_failure()}).install(monkeypatch)

    with job_session(user) as (session, repos):
        ai = JobOpenAIService(repos)
        assert isinstance(ai.runtime, JobAIRuntime)
        end_transaction(session)
        for _ in range(2):
            assert await ai.get_response("prompt", "message", response_schema=OpenAIText) == OpenAIText(text="hi")
        assert not session.in_transaction()

        usage = {tally.provider: tally for tally in ai.runtime.usage}
        assert usage["First"].model_dump(exclude={"latency_ms"}) == ExtractionUsage(
            feature="OpenAIText", slot="default", provider="First", model="First-model", requests=2, failures=2
        ).model_dump(exclude={"latency_ms"})
        assert (usage["Second"].requests, usage["Second"].failures, usage["Second"].model) == (2, 0, "Second-model")
        assert ai.runtime.answered_by("OpenAIText") == ("Second", "Second-model")
        assert ai.runtime.answered_by("OpenAIRecipe") is None
        # the tallies are copies
        ai.runtime.usage[0].requests = 99
        assert ai.runtime.usage[0].requests == 2


def test_end_transaction_commits_only_an_open_transaction(unique_user_fn_scoped: TestUser):
    with job_session(unique_user_fn_scoped) as (session, _):
        end_transaction(session)  # nothing open: nothing happens
        session.execute(sa.select(1))
        assert session.in_transaction()
        end_transaction(session)
        assert not session.in_transaction()


def test_a_card_image_is_read_once_and_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (page,) = make_pages(tmp_path)
    files = sorted(tmp_path.rglob("*"))
    reads: list[Path] = []
    real = Path.read_bytes

    def read_bytes(self: Path) -> bytes:
        reads.append(self)
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    image = CardImage(path=page.view_path)

    first = image.get_image_url()
    assert image.get_image_url() == first  # a second provider attempt reuses it
    assert reads == [page.view_path]
    assert first == "data:image/jpeg;base64," + base64.b64encode(real(page.view_path)).decode()
    assert image.build_message() == {"type": "image_url", "image_url": {"url": first}}
    assert sorted(tmp_path.rglob("*")) == files  # no `-min-original.jpg` beside it (F16)

    assert (
        CardImage(jpeg=b"\xff\xd8crop").get_image_url()
        == "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8crop").decode()
    )
    with pytest.raises(ValueError):
        CardImage().get_image_url()


@pytest.mark.asyncio
async def test_progress_is_reported_as_the_jobs_keys(unique_user_fn_scoped: TestUser, tmp_path: Path):
    reported: list[tuple[str, bool]] = []

    with job_session(unique_user_fn_scoped) as (session, repos):

        async def on_progress(key: str) -> None:
            reported.append((key, session.in_transaction()))

        ctx = CardWorkflowContext.for_card(
            make_pages(tmp_path),
            ai=JobOpenAIService(repos),
            repos=repos,
            translator=get_locale_provider("en-US"),
            options=CardPipelineOptions(suggest_organizers=False),
            errors=[],
            on_progress=on_progress,
        )
        assert ctx.options.model_dump() == {
            "translate_language": None,
            "resolve_organizers": False,
            "attach_organizers": False,
            "create_new_organizers": False,
        }
        assert ctx.input.images == [page.view_path for page in ctx.pages]

        keys: list[Any] = [
            "recipe.create-progress.reading-images-with-ai",
            "recipe.create-progress.reading-images-with-ai",  # a repeat isn't reported again
            "recipe.create-progress.fetching-webpage",  # nothing a card does
            "recipe.create-progress.creating-recipe",
            "recipe-ingest.progress.linking-ingredients",
            "recipe.create-progress.organizing-recipe",
        ]
        session.execute(sa.select(1))  # a read on the session, then a progress await
        for key in keys:
            await ctx.report_progress(key)

    assert reported == [
        ("recipe-ingest.progress.reading-card", False),
        ("recipe-ingest.progress.structuring", False),
        ("recipe-ingest.progress.linking-ingredients", False),
        ("recipe-ingest.progress.suggesting-organizers", False),
    ]
