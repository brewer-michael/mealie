"""
Fork: a recipe made with Import with AI from photos gets the first photo as its cover. The image files were written but
`recipes.image` was never set, so the recipe showed no cover until it was edited (docs/ai/PHASE2.md F21).
"""

import json
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from mealie.schema.group.ai_providers import AIProviderCreate, AIProviderSettingsUpdate
from mealie.schema.openai.compiled_source import OpenAICompiledSource
from mealie.schema.openai.recipe import OpenAIRecipe, OpenAIRecipeIngredient, OpenAIRecipeInstruction
from mealie.services.openai import OpenAIService
from tests.utils import api_routes
from tests.utils.factories import random_string
from tests.utils.fixture_schemas import TestUser


@pytest.fixture(autouse=True)
def fake_provider(unique_user: TestUser, monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    provider = unique_user.repos.group_ai_providers.create(
        AIProviderCreate(name=random_string(), model="gpt-4o", api_key="test-key")
    )
    unique_user.repos.group_ai_provider_settings.update(
        unique_user.repos.group_id,
        AIProviderSettingsUpdate(
            default_provider_id=provider.id, audio_provider_id=None, image_provider_id=provider.id
        ),
    )

    recipe = OpenAIRecipe(
        name=random_string(),
        description="",
        ingredients=[OpenAIRecipeIngredient(text="1 banana")],
        instructions=[OpenAIRecipeInstruction(text="Mash the banana.")],
    )

    async def get_response(self, prompt, message, *args, response_schema=None, **kwargs):
        if response_schema is OpenAICompiledSource:
            return OpenAICompiledSource(contains_recipe=True, content="Banana mug cake", language=None, image_url=None)
        if response_schema is OpenAIRecipe:
            return recipe
        return None

    monkeypatch.setattr(OpenAIService, "get_response", get_response)
    yield
    unique_user.repos.group_ai_provider_settings.update(
        unique_user.repos.group_id,
        AIProviderSettingsUpdate(default_provider_id=None, audio_provider_id=None, image_provider_id=None),
    )


def test_a_recipe_made_from_photos_has_its_cover(api_client: TestClient, unique_user: TestUser, test_image_jpg: str):
    with open(test_image_jpg, "rb") as f:
        r = api_client.post(
            api_routes.recipes_create_ai,
            files=[("images", ("card.jpg", f, "image/jpeg"))],
            headers=unique_user.token,
        )
    assert r.status_code == 201
    slug = json.loads(r.text)

    recipe = api_client.get(api_routes.recipes_slug(slug), headers=unique_user.token).json()
    assert recipe["image"]
    assert unique_user.repos.recipes.get_one(slug).image == recipe["image"]

    cover = api_client.get(
        api_routes.media_recipes_recipe_id_images_file_name(recipe["id"], "original.webp"), headers=unique_user.token
    )
    assert cover.status_code == 200


def test_a_recipe_made_from_text_alone_has_no_cover(api_client: TestClient, unique_user: TestUser):
    r = api_client.post(api_routes.recipes_create_ai, data={"content": "1 banana, mashed"}, headers=unique_user.token)
    assert r.status_code == 201
    slug = json.loads(r.text)

    recipe = api_client.get(api_routes.recipes_slug(slug), headers=unique_user.token).json()
    assert not recipe["image"]
