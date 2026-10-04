"""Card ingredient lines through the shorthand fix, the NLP parser and the matcher (docs/ai/PHASE2.md §5)"""

from uuid import uuid4

import pytest

from mealie.lang.providers import get_locale_provider
from mealie.schema.recipe.recipe import Recipe
from mealie.schema.recipe.recipe_ingredient import RecipeIngredient, SaveIngredientFood
from mealie.schema.recipe_ingest import CardDraft, CardDraftIngredient, CardFlagKind, CardFlagSeverity, ExtractionMeta
from mealie.services.ai.ingest.matching import IngestMatcher
from mealie.services.ai.ingest.pipeline.flags import compute_flags, ingredient_hash
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
@pytest.mark.parametrize(
    ("line", "unit", "food", "note"),
    [
        ("1 heaping T. flour", "tablespoon", "flour", "heaping"),
        ("1 level t. soda", "teaspoon", "soda", "level"),
        ("1 scant c. sugar", "cup", "sugar", "scant"),
        ("1 heaping T. coconut oil (melted)", "tablespoon", "coconut oil", "heaping, melted"),
        ("2 heaping cups flour", "cup", "flour", "heaping"),
    ],
)
async def test_a_size_word_before_shorthand_becomes_the_note(
    unique_user_fn_scoped: TestUser, line: str, unit: str, food: str, note: str
):
    """Not the food "heaping T. flour" (which commit would create) or the unit "scant cup" """
    user = unique_user_fn_scoped
    seed_foods_and_units(user)

    (parsed,) = await _normalize(user, [line])

    assert (parsed.unit and parsed.unit.name, parsed.food and parsed.food.name, parsed.note) == (unit, food, note)
    assert parsed.unit is not None and parsed.unit.id is not None
    assert parsed.original_text == line


def _highlighted(lines: list[CardDraftIngredient]) -> dict[str, list[tuple[CardFlagKind, dict]]]:
    """Each line's highlighted flags, by its card text"""
    flags = compute_flags(CardDraft(name="Card", ingredients=lines), ExtractionMeta(language="English"), {})
    by_ref: dict[str, list[tuple[CardFlagKind, dict]]] = {str(line.reference_id): [] for line in lines}
    for flag in flags:
        if flag.severity != CardFlagSeverity.info and flag.ref in by_ref:
            by_ref[flag.ref].append((flag.kind, flag.params))
    return {line.original_text: by_ref[str(line.reference_id)] for line in lines}


@pytest.mark.asyncio
async def test_realistic_card_lines_raise_what_needs_a_look_and_nothing_else(unique_user_fn_scoped: TestUser):
    """Card lines through the real parser and `compute_flags`: what it loses or misreads is flagged, the rest isn't"""
    user = unique_user_fn_scoped
    seed_foods_and_units(user)
    banana = [
        "1 banana",
        "1 T. coconut oil (melted)",
        "1/4 t. salt",
        "1/2 t vanilla",
        "1/3 C. almond flour",
        "1 egg",
        "Cinnamon to taste",
    ]
    fine = ["1 egg yolk", "2 egg whites", "1 red pepper, chopped", "1 bay leaf", "1 pie crust", "1 hot dog"]
    fine += ["1 heaping T. flour", "1 1/2 c. flour", "2 eggs, beaten", "1 9-inch pie shell"]
    lost = ["2-3 T. milk", "1 to 2 c. water", "2 or 3 eggs", "1 dozen eggs", "1 (16 oz.) can tomatoes"]
    unclear = ["1 doz. eggs", "1 env. yeast"]

    flags = _highlighted(await _normalize(user, banana + fine + lost + unclear))

    assert {line: flags[line] for line in banana + fine} == {line: [] for line in banana + fine}
    assert {line: flags[line] for line in lost} == {
        "2-3 T. milk": [(CardFlagKind.check_parse, {"value": "2-3"})],
        "1 to 2 c. water": [(CardFlagKind.check_parse, {"value": "1 to 2"})],
        "2 or 3 eggs": [(CardFlagKind.check_parse, {"value": "3"})],
        "1 dozen eggs": [(CardFlagKind.check_parse, {"value": "1 dozen"})],
        "1 (16 oz.) can tomatoes": [(CardFlagKind.check_parse, {"value": "16"})],
    }
    assert all(CardFlagKind.unit_unclear in [kind for kind, _ in flags[line]] for line in unclear)


@pytest.mark.asyncio
async def test_mixed_numbers_names_and_second_ingredients_through_the_parser(unique_user_fn_scoped: TestUser):
    """
    A mixed number written with a dash is read whole, a number in a food's name is the food's, and a second
    ingredient the parser runs into the food is checked, while a substitution its note keeps isn't
    """
    user = unique_user_fn_scoped
    seed_foods_and_units(user)
    mixed = ["2-1/4 c. flour", "1-1/2 tsp baking soda", "1 - 1/2 c. milk", "1–1/2 c. oats", "3-1/2 oz. coconut"]
    named = ["1 c. 2% milk", "2 c. V8 juice", "1 c. 7-Up", "1/2 tsp. 5-spice powder", '1 9" pie shell, baked']
    named += ["1 lb. 80/20 ground beef"]
    kept = ["1 c. butter (or 1 c. margarine)", "1 pkg. yeast (or 2 1/4 tsp.)"]
    merged = {
        "2 c. flour (or 1 1/2 c. bread flour)": "1 1/2",
        "1 c. buttermilk (or 1 c. milk + 1 T. vinegar)": "1",
        "1 c. sugar, 1 c. flour": "1",
        "1 c. sugar, 1 c. brown sugar": "1",
        "1 stick butter or 1/2 c. oleo": "1/2",
    }

    parsed = await _normalize(user, mixed + named + kept + list(merged))
    flags = _highlighted(parsed)

    assert [line.quantity for line in parsed[: len(mixed)]] == [2.25, 1.5, 1.5, 1.5, 3.5]
    assert [line.original_text for line in parsed[: len(mixed)]] == mixed
    assert {line: flags[line] for line in mixed + named + kept} == {line: [] for line in mixed + named + kept}
    assert {line: flags[line] for line in merged} == {
        line: [(CardFlagKind.check_parse, {"value": value})] for line, value in merged.items()
    }


@pytest.mark.asyncio
async def test_an_item_size_is_parsed_out_of_the_food(unique_user_fn_scoped: TestUser):
    """ "1 med onion" isn't the food "med onion", which the group's "red onion" would match fuzzily, linked silently"""
    user = unique_user_fn_scoped
    seed_foods_and_units(user)
    for name, plural in [("onion", "onions"), ("red onion", "red onions"), ("cabbage", None), ("red cabbage", None)]:
        user.repos.ingredient_foods.create(
            SaveIngredientFood(name=name, plural_name=plural, group_id=user.repos.group_id)
        )
    lines = ["1 med onion", "2 med. onions, chopped", "1 med cabbage", "1 med red onion", "1 lg onion", "1 sml onion"]

    parsed = await _normalize(user, lines)

    assert [(line.food and line.food.name, line.food and line.food.id is not None, line.note) for line in parsed] == [
        ("onion", True, "med"),
        ("onion", True, "med., chopped"),
        ("cabbage", True, "med"),
        ("red onion", True, "med"),
        ("onion", True, "lg"),
        ("onion", True, "sml"),
    ]
    assert [line.original_text for line in parsed] == lines
    assert _highlighted(parsed) == {line: [] for line in lines}


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
