"""What users are told when a group's AI providers are all at their monthly token limit (docs/ai/PHASE1.md §4)"""

from unittest.mock import AsyncMock, MagicMock

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from mealie.core import exceptions
from mealie.db.db_setup import session_context
from mealie.db.models.recipe_ingest import RecipeIngestionJob
from mealie.lang import get_locale_provider
from mealie.schema.group.ai_providers import AIProviderCreate, AIProviderOut, AIProviderSettingsUpdate, AIProviderSlot
from mealie.schema.group.ai_routing import AIUsageLogCreate
from mealie.schema.openai.compiled_source import OpenAICompiledSource
from mealie.schema.recipe_ingest import IngestErrorCode
from mealie.services.ai.errors import AIProviderLimitReachedError, AIProviderLocalOnlyError
from mealie.services.ai.policy import ai_call_policy
from mealie.services.openai import OpenAINotEnabledException, OpenAIService
from mealie.services.recipe.import_workflow.compilers import SourceCompiler, SourceType
from mealie.services.recipe.import_workflow.context import WorkflowContext, WorkflowInput, WorkflowOptions
from mealie.services.recipe.import_workflow.steps.compile_source import CompileSourceStep
from tests.utils import api_routes
from tests.utils.factories import random_string
from tests.utils.fixture_schemas import TestUser

translator = get_locale_provider("en-US")
LIMIT_REACHED = translator.t("recipe.import-errors.ai-limit-reached")


# ==========================================
# Compiling the source


def compiler(error: Exception | None = None) -> type[SourceCompiler]:
    """An image compiler that fails with `error`, or reads the images"""

    class Compiler(SourceCompiler):
        source_type = SourceType.IMAGES

        def can_compile(self) -> bool:
            return True

        async def compile(self) -> OpenAICompiledSource | None:
            if error:
                raise error
            return OpenAICompiledSource(contains_recipe=True, content="Soup\n\n1 onion", language=None, image_url=None)

    return Compiler


def image_import(tmp_path) -> WorkflowContext:
    keys = MagicMock()
    keys.t.side_effect = lambda key: key
    return WorkflowContext(
        input=WorkflowInput(images=[tmp_path / "card.jpg"]),
        options=WorkflowOptions(),
        repos=MagicMock(),
        translator=keys,
        ai=MagicMock(),
        on_progress=AsyncMock(),
    )


PROVIDER_ERRORS = [
    AIProviderLimitReachedError("all at their limit"),
    OpenAINotEnabledException("No image provider set"),
]


@pytest.mark.parametrize("error", PROVIDER_ERRORS, ids=lambda e: type(e).__name__)
@pytest.mark.asyncio
async def test_a_source_no_provider_may_read_reports_why(tmp_path, error: Exception):
    """Rather than reporting the source as unreadable"""
    with pytest.raises(type(error)):
        await CompileSourceStep(compilers=[compiler(error)]).run(image_import(tmp_path))


@pytest.mark.parametrize("error", PROVIDER_ERRORS, ids=lambda e: type(e).__name__)
@pytest.mark.asyncio
async def test_another_compiler_may_still_read_the_source(tmp_path, error: Exception):
    """e.g. OCR on the default providers, when the image providers are all at their limit"""
    ctx = image_import(tmp_path)

    await CompileSourceStep(compilers=[compiler(error), compiler()]).run(ctx)

    assert ctx.compiled_source
    assert "1 onion" in ctx.compiled_source.content


# ==========================================
# Through the API


@pytest.fixture()
def capped_provider(unique_user_fn_scoped: TestUser) -> None:
    """The group's only provider, already at its monthly token limit"""
    repos = unique_user_fn_scoped.repos
    provider = repos.group_ai_providers.create(
        AIProviderCreate(name="Capped", model="m", api_key="k", monthly_token_limit=100)
    )
    repos.group_ai_provider_settings.update(
        repos.group_id,
        AIProviderSettingsUpdate(default_provider_id=provider.id, audio_provider_id=None, image_provider_id=None),
    )
    repos.group_ai_usage.create(
        AIUsageLogCreate(
            provider_id=provider.id,
            provider_name=provider.name,
            model=provider.model,
            protocol=provider.protocol,
            slot=AIProviderSlot.default,
            prompt_tokens=60,
            completion_tokens=40,
            success=True,
        )
    )


def wrapped_limit_error() -> Exception:
    """As the video importers report it"""
    try:
        raise AIProviderLimitReachedError("all at their limit")
    except AIProviderLimitReachedError as e:
        try:
            raise exceptions.OpenAIServiceError(f"Failed to extract recipe from video: {e}") from e
        except exceptions.OpenAIServiceError as wrapped:
            return wrapped


@pytest.mark.usefixtures("capped_provider")
def test_a_recipe_import_says_the_limit_was_reached(api_client: TestClient, unique_user_fn_scoped: TestUser):
    response = api_client.post(
        api_routes.recipes_create_ai, data={"content": random_string()}, headers=unique_user_fn_scoped.token
    )

    assert response.status_code == 400
    assert response.json()["detail"]["message"] == LIMIT_REACHED


@pytest.mark.usefixtures("capped_provider")
def test_a_wrapped_limit_error_is_reported_the_same(
    api_client: TestClient, unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    async def get_response(self, *args, **kwargs):
        raise wrapped_limit_error()

    monkeypatch.setattr(OpenAIService, "get_response", get_response)

    response = api_client.post(
        api_routes.recipes_create_ai, data={"content": random_string()}, headers=unique_user_fn_scoped.token
    )

    assert response.status_code == 400
    assert response.json()["detail"]["message"] == LIMIT_REACHED


@pytest.mark.usefixtures("capped_provider")
@pytest.mark.parametrize(
    ("route", "body"),
    [
        (api_routes.parser_ingredient, {"parser": "openai", "ingredient": "1 cup flour"}),
        (api_routes.parser_ingredients, {"parser": "openai", "ingredients": ["1 cup flour"]}),
    ],
)
def test_ingredient_parsing_says_the_limit_was_reached(
    api_client: TestClient, unique_user_fn_scoped: TestUser, route: str, body: dict
):
    response = api_client.post(route, json=body, headers=unique_user_fn_scoped.token)

    assert response.status_code == 429
    assert response.json()["detail"]["message"] == LIMIT_REACHED


# ==========================================
# Under a local-only policy (docs/ai/PHASE2.md §10): the policy picks the providers, then their limits apply


LOCAL_URL = "http://127.0.0.1:11434/v1"
"""A loopback address: local without a DNS lookup"""


def _local_and_cloud(user: TestUser, *, local_used: int, local_limit: int = 100) -> tuple[AIProviderOut, AIProviderOut]:
    """A local primary (with `local_used` tokens of `local_limit` used) and a cloud fallback well within its limit"""
    repos = user.repos
    local = repos.group_ai_providers.create(
        AIProviderCreate(
            name="Ollama",
            model="m",
            api_key="k",
            base_url=LOCAL_URL,
            runs_locally=True,
            monthly_token_limit=local_limit,
        )
    )
    cloud = repos.group_ai_providers.create(
        AIProviderCreate(name="Cloud", model="m", api_key="k", monthly_token_limit=1_000_000)
    )
    repos.group_ai_provider_settings.update(
        repos.group_id,
        AIProviderSettingsUpdate(default_provider_id=local.id, image_provider_id=local.id, audio_provider_id=None),
    )
    repos.group_ai_provider_routes.replace_routes(
        {AIProviderSlot.default: [cloud.id], AIProviderSlot.image: [cloud.id]}
    )
    for provider, tokens in ((local, local_used), (cloud, 10)):
        repos.group_ai_usage.create(
            AIUsageLogCreate(
                provider_id=provider.id,
                provider_name=provider.name,
                model=provider.model,
                protocol=provider.protocol,
                slot=AIProviderSlot.default,
                prompt_tokens=tokens,
                completion_tokens=0,
                success=True,
            )
        )
    return local, cloud


@pytest.mark.parametrize("slot", [AIProviderSlot.default, AIProviderSlot.image, AIProviderSlot.fast])
def test_local_only_with_the_local_provider_over_its_limit_is_limit_reached(
    unique_user_fn_scoped: TestUser, slot: AIProviderSlot
):
    """Not "no local provider": there is one, it's just used up this month (the cloud fallback is never a candidate)"""
    _local_and_cloud(unique_user_fn_scoped, local_used=100)
    runtime = OpenAIService(unique_user_fn_scoped.repos).runtime

    with ai_call_policy(local_only=True), pytest.raises(AIProviderLimitReachedError) as e:
        runtime.candidates(slot)

    assert "(Ollama)" in str(e.value)


def test_local_only_without_a_local_provider_is_local_only_unavailable(unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    local, cloud = _local_and_cloud(user, local_used=100)
    user.repos.group_ai_provider_settings.update(
        user.repos.group_id,
        AIProviderSettingsUpdate(default_provider_id=cloud.id, image_provider_id=None, audio_provider_id=None),
    )
    user.repos.group_ai_provider_routes.replace_routes({})

    with ai_call_policy(local_only=True), pytest.raises(AIProviderLocalOnlyError):
        OpenAIService(user.repos).runtime.candidates(AIProviderSlot.default)


def test_local_only_within_the_limit_keeps_the_local_provider(unique_user_fn_scoped: TestUser):
    local, _ = _local_and_cloud(unique_user_fn_scoped, local_used=10)

    with ai_call_policy(local_only=True):
        assert OpenAIService(unique_user_fn_scoped.repos).runtime.candidates(AIProviderSlot.default) == [local]


def test_without_a_policy_the_limit_filter_is_unchanged(unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    local, cloud = _local_and_cloud(user, local_used=100)
    assert OpenAIService(user.repos).runtime.candidates(AIProviderSlot.default) == [cloud]

    user.repos.group_ai_usage.create(
        AIUsageLogCreate(
            provider_id=cloud.id,
            provider_name=cloud.name,
            model=cloud.model,
            protocol=cloud.protocol,
            slot=AIProviderSlot.default,
            prompt_tokens=1_000_000,
            completion_tokens=0,
            success=True,
        )
    )
    with pytest.raises(AIProviderLimitReachedError) as e:
        OpenAIService(user.repos).runtime.candidates(AIProviderSlot.default)
    assert "(Ollama, Cloud)" in str(e.value)


@pytest.mark.asyncio
async def test_a_local_only_card_over_the_local_limit_fails_limit_reached(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    """Through the runner: the card fails `limit_reached` (with its advice), not `local_only_unavailable`"""
    from mealie.services import ocr
    from mealie.services.ai.ingest.runner import worker
    from mealie.services.ai.ingest.runner.finalize import Applied
    from tests.unit_tests.services_tests.ai.ingest.pipeline.card_fakes import FakeCardAI, banana_answers
    from tests.unit_tests.services_tests.ai.ingest.pipeline.test_card_tasks import create_job, row

    user = unique_user_fn_scoped
    _local_and_cloud(user, local_used=100)
    fake = FakeCardAI(banana_answers()).install(monkeypatch)
    monkeypatch.setattr(ocr, "is_available", lambda: False)
    job_id, token = create_job(user, local_only=True)

    assert await worker.run_task(job_id, token) == Applied.failed

    with session_context() as session:
        job = session.execute(sa.select(RecipeIngestionJob).where(RecipeIngestionJob.id == job_id)).scalar_one()
        assert (job.status, job.error_code) == ("failed", IngestErrorCode.limit_reached.value)
    assert row(job_id)["draft"] is None
    assert fake.calls == []  # neither the local provider over its limit nor the cloud fallback was asked
