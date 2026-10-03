"""What users are told when a group's AI providers are all at their monthly token limit (docs/ai/PHASE1.md §4)"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from mealie.core import exceptions
from mealie.lang import get_locale_provider
from mealie.schema.group.ai_providers import AIProviderCreate, AIProviderSettingsUpdate, AIProviderSlot
from mealie.schema.group.ai_routing import AIUsageLogCreate
from mealie.schema.openai.compiled_source import OpenAICompiledSource
from mealie.services.ai.errors import AIProviderLimitReachedError
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
