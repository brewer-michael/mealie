"""
Shared fakes for the recipe card pipeline tests: a fake AI answering by response schema at
`OpenAIService._get_raw_response` (so routing, policy, the usage log and the job runtime all run for real), the banana
card's recorded provider answers, page fixtures and provider setup.
"""

import asyncio
import io
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import openai
import pytest
from PIL import Image, ImageDraw
from sqlalchemy.orm import Session

from mealie.db.db_setup import session_context
from mealie.repos.all_repositories import get_repositories
from mealie.repos.repository_factory import AllRepositories
from mealie.schema.group.ai_providers import AIProviderCreate, AIProviderOut, AIProviderSettingsUpdate, AIProviderSlot
from mealie.schema.recipe.recipe_ingredient import SaveIngredientFood, SaveIngredientUnit
from mealie.schema.recipe_ingest import PageOCR
from mealie.services.ai.ingest import images
from mealie.services.ai.ingest.pipeline import CardPage
from mealie.services.openai import OpenAIService
from tests.utils.fixture_schemas import TestUser

# ==========================================
# The banana card's recorded answers (tests/data/cards/banana-mug-cake.jpg): the card leaves the microwave time
# blank on purpose

BANANA_CONTENT = """# Banana Mug Cake

No sugar, gluten free

## Ingredients
- 1 banana
- 1 T. coconut oil (melted)
- 1/4 t. salt
- 1/2 t vanilla
- 1/3 C. almond flour
- 1 egg
- Cinnamon to taste

## Directions
Mash banana and mix ingredients thoroughly.
Microwave in bowl or large mug for [blank] minutes or until firm in center."""

BANANA_TRANSCRIPTION = {
    "contains_recipe": True,
    "content": BANANA_CONTENT,
    "language": "English",
    "unsure": [{"text": "1/3 C.", "alternatives": ["1/2 C."], "reason": "faded"}],
}

BANANA_RECIPE = {
    "name": "Banana Mug Cake",
    "description": "No sugar, gluten free",
    "ingredients": [
        {"text": "1 banana"},
        {"text": "1 T. coconut oil (melted)"},
        {"text": "1/4 t. salt"},
        {"text": "1/2 t vanilla"},
        {"text": "1/3 C. almond flour"},
        {"text": "1 egg"},
        {"text": "Cinnamon to taste"},
    ],
    "instructions": [
        {"text": "Mash banana and mix ingredients thoroughly."},
        {"text": "Microwave in bowl or large mug for [blank] minutes or until firm in center."},
    ],
}

BANANA_ORGANIZERS = {"tags": ["Dessert", "Quick"], "categories": ["Snack"], "tools": ["Microwave"]}

BANANA_TRANSCRIPT = {
    "contains_recipe": True,
    "text": "\n".join(
        [
            "Banana Mug Cake",
            "No sugar, gluten free",
            "1 banana",
            "1 T. coconut oil (melted)",
            "1/4 t. salt",
            "1/2 t vanilla",
            "1/3 C. almond flour",
            "1 egg",
            "Cinnamon to taste",
            "Mash banana and mix ingredients",
            "thoroughly. Microwave in bowl or large",
            "mug for [blank] minutes or until firm",
            "in center.",
        ]
    ),
}


def banana_answers(**overrides: Any) -> dict[str, Any]:
    """The banana card's answers by response schema name"""
    answers: dict[str, Any] = {
        "OpenAIRecipeCardTranscription": BANANA_TRANSCRIPTION,
        "OpenAIRecipe": BANANA_RECIPE,
        "OpenAIOrganizers": BANANA_ORGANIZERS,
        "OpenAIRecipeCardTranscript": BANANA_TRANSCRIPT,
        "OpenAIRecipeCardRegion": {"readable": True, "text": "1/3 C. almond flour", "alternatives": []},
    }
    answers.update(overrides)
    return answers


# ==========================================
# The fake AI


@dataclass
class Call:
    provider: str
    schema: str
    message: str
    images: int
    service: OpenAIService


Answer = dict | Callable[..., Any] | BaseException | None


@dataclass
class FakeCardAI:
    """
    Answers every provider request by its response schema's name (`answers`), unless the provider (by name) or the
    pair `(provider, schema)` is in `failures`. An answer may be a callable `(call) -> answer`, sync or async.
    `on_call(call)` runs at the start of every request, and again after the request yields to the event loop.
    """

    answers: dict[str, Answer]
    failures: dict[Any, BaseException] = field(default_factory=dict)
    on_call: Callable[[Call], None] | None = None
    calls: list[Call] = field(default_factory=list)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeCardAI:
        fake = self

        async def _get_raw_response(
            self: OpenAIService, prompt: str, content: list[dict], response_schema: type, provider: AIProviderOut
        ) -> Any:
            text = "\n".join(part["text"] for part in content if part.get("type") == "text")
            call = Call(
                provider=provider.name,
                schema=response_schema.__name__,
                message=text,
                images=sum(1 for part in content if part.get("type") == "image_url"),
                service=self,
            )
            fake.calls.append(call)
            if fake.on_call:
                fake.on_call(call)
            await asyncio.sleep(0)  # as a provider would, let the other read run meanwhile
            if fake.on_call:
                fake.on_call(call)

            failure = fake.failures.get((provider.name, call.schema)) or fake.failures.get(provider.name)
            if failure is not None:
                raise failure

            answer = fake.answers.get(call.schema)
            if callable(answer):
                answer = answer(call)
                if asyncio.iscoroutine(answer):
                    answer = await answer
            if isinstance(answer, BaseException):
                raise answer
            if answer is None:
                return None
            return response_schema.parse_openai_response(json.dumps(answer))

        monkeypatch.setattr(OpenAIService, "_get_raw_response", _get_raw_response)
        return self

    def schemas(self, provider: str | None = None) -> list[str]:
        return [call.schema for call in self.calls if provider is None or call.provider == provider]


def rate_limited() -> openai.RateLimitError:
    request = httpx.Request("POST", "https://api.example.test/v1/chat/completions")
    return openai.RateLimitError("Rate limited", response=httpx.Response(429, request=request), body=None)


def provider_failure(body: str = "SECRET PROVIDER BODY") -> openai.APIStatusError:
    request = httpx.Request("POST", "https://api.example.test/v1/chat/completions")
    return openai.InternalServerError(body, response=httpx.Response(500, request=request), body={"error": body})


# ==========================================
# Providers, foods and units


def create_provider(user: TestUser, name: str, **kwargs: Any) -> AIProviderOut:
    return user.repos.group_ai_providers.create(
        AIProviderCreate(name=name, model=f"{name}-model", api_key="k", **kwargs)
    )


def configure(
    user: TestUser,
    *,
    default: AIProviderOut | None = None,
    image: AIProviderOut | None = None,
    routes: dict[AIProviderSlot, list[AIProviderOut]] | None = None,
) -> None:
    user.repos.group_ai_provider_settings.update(
        user.repos.group_id,
        AIProviderSettingsUpdate(
            default_provider_id=default.id if default else None,
            image_provider_id=image.id if image else None,
            audio_provider_id=None,
        ),
    )
    user.repos.group_ai_provider_routes.replace_routes(
        {slot: [provider.id for provider in providers] for slot, providers in (routes or {}).items()}
    )


def seed_foods_and_units(user: TestUser) -> None:
    group_id = user.repos.group_id
    for name, plural in [("banana", "bananas"), ("coconut oil", None), ("egg", "eggs"), ("salt", None)]:
        user.repos.ingredient_foods.create(SaveIngredientFood(name=name, plural_name=plural, group_id=group_id))
    for name, plural, abbreviation in [
        ("tablespoon", "tablespoons", "tbsp"),
        ("teaspoon", "teaspoons", "tsp"),
        ("cup", "cups", "c"),
        ("package", "packages", "pkg"),
    ]:
        user.repos.ingredient_units.create(
            SaveIngredientUnit(name=name, plural_name=plural, abbreviation=abbreviation, group_id=group_id)
        )


@contextmanager
def job_session(user: TestUser) -> Iterator[tuple[Session, AllRepositories]]:
    """A dedicated session and household-scoped repositories, as a task's AI session"""
    with session_context() as session:
        yield session, get_repositories(session, group_id=user.repos.group_id, household_id=user.repos.household_id)


# ==========================================
# Pages


def card_image(size: tuple[int, int] = (600, 800), lines: tuple[str, ...] = ("Banana Mug Cake",)) -> bytes:
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(lines):
        draw.text((40, 40 + index * 40), line, fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


def make_pages(root: Path, count: int = 1, *, ocr_text: str | None = None, data: bytes | None = None) -> list[CardPage]:
    """`count` normalized pages under `root/pages/<n>/`, with stored OCR text when `ocr_text` is given"""
    pages: list[CardPage] = []
    for index in range(count):
        page_dir = root / "pages" / str(index)
        page_dir.mkdir(parents=True, exist_ok=True)
        meta = images.normalize_page(io.BytesIO(data or card_image()), page_dir, index, original_filename="card.jpg")
        if ocr_text is not None:
            meta = meta.model_copy(update={"ocr": PageOCR(text=ocr_text, confidence=49.0)})
        pages.append(CardPage(dir=page_dir, meta=meta))
    return pages
