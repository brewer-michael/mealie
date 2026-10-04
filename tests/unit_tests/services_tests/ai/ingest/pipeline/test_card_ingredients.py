"""Card ingredient lines through the shorthand fix, the NLP parser and the matcher (docs/ai/PHASE2.md §5)"""

from uuid import uuid4

import pytest

from mealie.lang.providers import get_locale_provider
from mealie.schema.recipe.recipe import Recipe
from mealie.schema.recipe.recipe_ingredient import RecipeIngredient
from mealie.services.ai.ingest.matching import IngestMatcher
from mealie.services.ai.ingest.pipeline.flags import ingredient_hash
from mealie.services.ai.ingest.pipeline.ingredients import (
    IngredientLine,
    normalize_ingredients,
    normalize_lines,
    recipe_lines,
)
from mealie.services.parser_services.ingredient_parser import NLPParser
from tests.unit_tests.services_tests.ai.ingest.pipeline.card_fakes import job_session, seed_foods_and_units
from tests.utils.fixture_schemas import TestUser

translator = get_locale_provider("en-US")


async def _normalize(user: TestUser, lines: list[str], language: str | None = "English"):
    with job_session(user) as (session, repos):
        result = await normalize_lines(
            [IngredientLine(text=line) for line in lines],
            repos=repos,
            translator=translator,
            matcher=IngestMatcher(repos),
            language=language,
        )
        assert not session.in_transaction()
        return result


@pytest.mark.asyncio
async def test_the_banana_lines_are_parsed_and_linked(unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    seed_foods_and_units(user)
    lines = ["1 T. coconut oil (melted)", "1/4 t. salt", "1/2 t vanilla", "1/3 C. almond flour", "1 egg"]

    oil, salt, vanilla, flour, egg = await _normalize(user, lines)

    # (quantity, unit, food) as the parser would read "1 tbsp coconut oil (melted)" and so on
    read = [
        (line.quantity, line.unit and line.unit.name, line.food and line.food.name)
        for line in (oil, salt, vanilla, flour, egg)
    ]
    assert read == [
        (1, "tablespoon", "coconut oil"),
        (0.25, "teaspoon", "salt"),
        (0.5, "teaspoon", "vanilla"),
        (pytest.approx(1 / 3, abs=1e-3), "cup", "almond flour"),
        (1, None, "egg"),
    ]
    # linked to the group's own foods and units where it has them
    assert oil.unit is not None and oil.unit.id is not None
    assert oil.food is not None and oil.food.id is not None
    assert vanilla.food is not None and vanilla.food.id is None
    assert oil.note == "melted"
    # the card's line, not the parser's input, and a display rebuilt after linking
    assert [line.original_text for line in (oil, salt, vanilla, flour, egg)] == lines
    assert oil.display == "1 tablespoon coconut oil melted"
    assert salt.display == "¹/₄ teaspoon salt"  # Mealie's own fraction display
    assert all(line.parse_confidence and line.parse_confidence > 0.85 for line in (oil, salt, vanilla, flour, egg))
    assert all(line.extracted_hash == ingredient_hash(line) for line in (oil, salt, vanilla, flour, egg))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("line", "unit", "food"),
    [
        ("3 TB. butter", "tablespoon", "butter"),  # not terabytes (F6)
        ("2 Tbsp sugar", "tablespoon", "sugar"),
        ("1 pkg. yeast", "package", "yeast"),  # brute would read kilograms
        ("1 c. milk", "cup", "milk"),
    ],
)
async def test_card_shorthand(unique_user_fn_scoped: TestUser, line: str, unit: str, food: str):
    user = unique_user_fn_scoped
    seed_foods_and_units(user)

    (parsed,) = await _normalize(user, [line])

    assert (parsed.unit and parsed.unit.name, parsed.food and parsed.food.name) == (unit, food)
    assert parsed.original_text == line


@pytest.mark.asyncio
async def test_words_that_look_like_shorthand_are_left_alone(unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    seed_foods_and_units(user)

    tbone, dont = await _normalize(user, ["1 t-bone steak", "don't overmix"])

    assert tbone.unit is None or tbone.unit.name != "teaspoon"
    assert tbone.original_text == "1 t-bone steak"
    assert dont.original_text == "don't overmix"
    assert dont.unit is None


@pytest.mark.asyncio
async def test_blank_lines_markers_and_titles(unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    seed_foods_and_units(user)
    recipe = Recipe(
        name="Cake",
        recipe_ingredient=[
            RecipeIngredient(title="For the cake", note=""),
            RecipeIngredient(note="  1 egg  "),
            RecipeIngredient(note="   "),
            RecipeIngredient(note="2 cups [ Illegible ] flour"),
            RecipeIngredient(title="Frosting", note="1 c. sugar"),
        ],
    )
    assert [(line.text, line.title) for line in recipe_lines(recipe)] == [
        ("1 egg", "For the cake"),
        ("2 cups [ Illegible ] flour", None),
        ("1 c. sugar", "Frosting"),
    ]

    with job_session(user) as (_, repos):
        egg, flour, sugar = await normalize_ingredients(
            recipe, repos=repos, translator=translator, matcher=IngestMatcher(repos), language=None
        )

    assert (egg.title, egg.food and egg.food.name) == ("For the cake", "egg")
    # a line holding a marker stays as written, unparsed, with the marker written canonically
    assert (flour.original_text, flour.note, flour.display, flour.quantity, flour.parse_confidence) == (
        "2 cups [illegible] flour",
        "2 cups [illegible] flour",
        "2 cups [illegible] flour",
        None,
        None,
    )
    assert (sugar.title, sugar.unit and sugar.unit.name) == ("Frosting", "cup")


@pytest.mark.asyncio
async def test_cards_in_other_languages_are_kept_as_text(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped

    async def fail(*args, **kwargs):
        raise AssertionError("a French card isn't parsed")

    monkeypatch.setattr(NLPParser, "parse", fail)

    (flour,) = await _normalize(user, ["250 g de farine"], language="French")

    assert (flour.original_text, flour.note, flour.quantity, flour.unit, flour.food) == (
        "250 g de farine",
        "250 g de farine",
        None,
        None,
        None,
    )


@pytest.mark.asyncio
async def test_a_line_the_parser_chokes_on_is_kept_as_text(
    unique_user_fn_scoped: TestUser, monkeypatch: pytest.MonkeyPatch
):
    user = unique_user_fn_scoped
    real_parse_one = NLPParser.parse_one

    async def parse(self, ingredients):
        raise ValueError("one bad line fails the whole call")

    async def parse_one(self, line: str):
        if "bad" in line:
            raise ValueError("can't parse")
        return await real_parse_one(self, line)

    monkeypatch.setattr(NLPParser, "parse", parse)
    monkeypatch.setattr(NLPParser, "parse_one", parse_one)

    egg, bad = await _normalize(user, ["1 egg", "a bad line"])

    assert egg.food is not None and egg.food.name == "egg"
    assert (bad.note, bad.parse_confidence) == ("a bad line", None)


@pytest.mark.asyncio
async def test_a_reread_line_keeps_its_reference(unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    reference_id = uuid4()

    with job_session(user) as (_, repos):
        (line,) = await normalize_lines(
            [IngredientLine(text="1 egg", reference_id=reference_id)],
            repos=repos,
            translator=translator,
            matcher=IngestMatcher(repos),
            language="en",
        )
    assert line.reference_id == reference_id
