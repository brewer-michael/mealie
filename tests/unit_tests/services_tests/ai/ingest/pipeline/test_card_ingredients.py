"""Card ingredient lines through the shorthand fix, the NLP parser and the matcher (docs/ai/PHASE2.md §5)"""

from uuid import UUID, uuid4

import pytest

from mealie.lang.providers import get_locale_provider
from mealie.repos.seed.seeders import IngredientFoodsSeeder, IngredientUnitsSeeder
from mealie.schema.recipe.recipe import Recipe
from mealie.schema.recipe.recipe_ingredient import (
    CreateIngredientFoodAlias,
    RecipeIngredient,
    SaveIngredientFood,
    SaveIngredientUnit,
)
from mealie.schema.recipe_ingest import CardDraft, CardDraftIngredient, CardFlagKind, CardFlagSeverity, ExtractionMeta
from mealie.services.ai.ingest.matching import IngestMatcher
from mealie.services.ai.ingest.pipeline.flags import compute_flags, is_unedited, split_off
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
    return (await _normalize_and_link(user, lines, language))[0]


async def _normalize_and_link(
    user: TestUser, lines: list[str], language: str | None = "English"
) -> tuple[list[CardDraftIngredient], dict[UUID, list[str]]]:
    """The lines as extraction parses and links them, and the names of what they link (`compute_flags`' `linked`)"""
    with job_session(user) as (session, repos):
        matcher = IngestMatcher(repos)
        result = await normalize_lines(
            [IngredientLine(text=line) for line in lines],
            repos=repos,
            translator=translator,
            matcher=matcher,
            language=language,
        )
        assert not session.in_transaction()
        return result, matcher.linked_names(result)


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
    assert all(is_unedited(line) and not split_off(line) for line in (oil, salt, vanilla, flour, egg))


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("line", "unit", "food", "note"),
    [
        ("1 c. sugar (scant)", "cup", "sugar", "scant"),
        ("1 c. (scant) sugar", "cup", "sugar", "scant"),
        ("1 c. scant sugar", "cup", "sugar", "scant"),
        ("1 T. heaping flour", "tablespoon", "flour", "heaping"),
        ("scant 1 c. sugar", "cup", "sugar", "scant"),
        ("1 c. sugar, scant", "cup", "sugar", "scant"),
        ("1 c. sugar scant", "cup", "sugar", "scant"),
    ],
)
async def test_a_size_word_anywhere_on_the_line_goes_to_the_note(
    unique_user_fn_scoped: TestUser, line: str, unit: str, food: str, note: str
):
    """Not the unit "cup scant" or the food "heaping flour", which commit would create, and nothing to look at"""
    user = unique_user_fn_scoped
    seed_foods_and_units(user)

    (parsed,) = await _normalize(user, [line])

    assert (parsed.unit and parsed.unit.name, parsed.food and parsed.food.name, parsed.note) == (unit, food, note)
    assert parsed.unit is not None and parsed.unit.id is not None
    assert (parsed.quantity, parsed.original_text) == (1, line)
    assert _highlighted([parsed]) == {line: []}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("line", "quantity", "unit", "food", "note"),
    [
        ("1 doz. eggs", 1, "dozen", "egg", ""),
        ("1 dozen eggs", 1, "dozen", "egg", ""),
        ("1 env. Dream Whip", 1, "envelope", "Dream Whip", ""),
        ("1 sq. chocolate", 1, "square", "chocolate", ""),
        ("1 (8 oz.) pkg. cream cheese", 1, "package", "cream cheese", "(8 oz.)"),
        ("1 (10 3/4 oz.) can soup", 1, "can", "soup", "(10 3/4 oz.)"),
        ("1 #2 can pineapple", 1, "can", "pineapple", "#2"),
        ("1 #10 can tomatoes", 1, "can", "tomatoes", "#10"),
        # a package's size without parentheses (the food was "pkg. cream cheese", the note "8-oz. pkg. cream cheese")
        ("1 8-oz. pkg. cream cheese", 1, "package", "cream cheese", "8-oz."),
        ("1 3-oz. pkg. Jello", 1, "package", "Jello", "3-oz."),
        ("2 8-oz. cans tomato sauce", 2, "cans", "tomato sauce", "8-oz."),
        ("1 8 oz. pkg. cream cheese", 1, "package", "cream cheese", "8 oz."),
        # or after a dash (read as 1 to 8 ounces of "pkg. cream cheese", or 1 can with the line in the note)
        ("1 - 8 oz. pkg. cream cheese", 1, "package", "cream cheese", "8 oz."),
        ("1-8 oz. pkg. cream cheese", 1, "package", "cream cheese", "8 oz."),
        ("2-15 oz. cans black beans", 2, "cans", "black beans", "15 oz."),
        ("2 - 15 oz. cans tomatoes", 2, "cans", "tomatoes", "15 oz."),
        ("1 - 3 oz. box Jello", 1, "box", "Jello", "3 oz."),
        ("2 - 10 oz. pkgs. frozen spinach", 2, "package", "frozen spinach", "10 oz."),
        # a can's number with a fraction (the food was "#2#1$2", the parser's own fraction code) or "No."
        ("1 #2 1/2 can peaches", 1, "can", "peaches", "#2 1/2"),
        ("1 #2½ can peaches", 1, "can", "peaches", "#2½"),
        ("2 #2 1/2 cans tomatoes", 2, "cans", "tomatoes", "#2 1/2"),
        ("1 No. 2 can corn", 1, "can", "corn", "No. 2"),
        # a size written in full before a container (it was dropped, and hid the "pkg." after it)
        ("1 large can pineapple", 1, "can", "pineapple", "large"),
        ("1 small can tomato sauce", 1, "can", "tomato sauce", "small"),
        ("2 large cans tomatoes", 2, "cans", "tomatoes", "large"),
        ("1 tall can evaporated milk", 1, "can", "evaporated milk", "tall"),
        ("1 large pkg. Jello", 1, "package", "Jello", "large"),
        ("1 small (3 oz.) pkg. Jello", 1, "package", "Jello", "small, (3 oz.)"),
        ("3 large eggs", 3, None, "egg", "large"),  # before the food, as the parser read it already
        # a fraction of a dozen (read as 1 dozen, the line in the note)
        ("1/2 doz. eggs", 0.5, "dozen", "egg", ""),
        ("1/2 dozen eggs", 0.5, "dozen", "egg", ""),
        ("½ doz. eggs", 0.5, "dozen", "egg", ""),
        ("1 1/2 doz. cookies", 1.5, "dozen", "cookies", ""),
        ("2 1/2 dozen rolls", 2.5, "dozen", "rolls", ""),
        # longer and plural spellings of a unit (the food was "tbls. sugar", with nothing to look at)
        ("2 tbls. sugar", 2, "tablespoon", "sugar", ""),
        ("2 Tbls. sugar", 2, "tablespoon", "sugar", ""),
        ("1 tblsp. flour", 1, "tablespoon", "flour", ""),
        ("1 teasp. salt", 1, "teaspoon", "salt", ""),
        ("2 pkgs. yeast", 2, "package", "yeast", ""),
        ("2 Pkgs. yeast", 2, "package", "yeast", ""),
        ("2 (10 oz.) pkgs. frozen peas", 2, "package", "frozen peas", "(10 oz.)"),
        ("3 envs. gelatin", 3, "envelopes", "gelatin", ""),
    ],
)
async def test_more_card_shorthand_is_read(
    unique_user_fn_scoped: TestUser, line: str, quantity: float, unit: str | None, food: str, note: str
):
    """
    "doz.", "env." and "sq." are units, and so are the longer spellings of a spoon or package; a package's size, a
    can's number and a size written in full before a container go to the note; a fraction of a dozen is the line's
    quantity; and none needs a look
    """
    user = unique_user_fn_scoped
    seed_foods_and_units(user)

    (parsed,) = await _normalize(user, [line])

    assert (parsed.quantity, parsed.unit and parsed.unit.name, parsed.food and parsed.food.name, parsed.note) == (
        quantity,
        unit,
        food,
        note,
    )
    assert parsed.original_text == line
    assert _highlighted([parsed]) == {line: []}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("line", "quantity", "note", "value", "start"),
    [
        ("1 can (8-10 oz.) beans", 1, "(8-10 oz.)", "8-10", 7),
        ("1 c. (or 2) eggs", 1, "(or 2)", "2", 9),
        ("2-3 c. flour", 2, "to 3", "2-3", 0),
        ("1 c. sugar + 2 T.", 1, "+ 2 T.", "2", 13),
        ("1 c. sugar, 1 c. brown sugar", 1, "1 c. brown sugar", "1", 12),
    ],
)
async def test_an_amount_the_fields_lose_is_kept_in_the_note_and_still_checked(
    unique_user_fn_scoped: TestUser, line: str, quantity: float, note: str, value: str, start: int
):
    """Whatever the reviewer taps, commit writes the fields: the note keeps the amount, and the line is still flagged"""
    user = unique_user_fn_scoped
    seed_foods_and_units(user)

    (parsed,) = await _normalize(user, [line])

    assert (parsed.quantity, parsed.note) == (quantity, note)
    assert note in parsed.display
    assert _highlighted([parsed]) == {
        line: [(CardFlagKind.check_parse, {"value": value, "start": start, "end": start + len(value)})]
    }


def _highlighted(
    lines: list[CardDraftIngredient], linked: dict[UUID, list[str]] | None = None
) -> dict[str, list[tuple[CardFlagKind, dict]]]:
    """Each line's highlighted flags, by its card text"""
    card = CardDraft(name="Card", ingredients=lines)
    flags = compute_flags(card, ExtractionMeta(language="English"), {}, linked=linked)
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
    # a dozen, an envelope and a package's size are read whole now
    fine += ["1 dozen eggs", "1 doz. eggs", "1 env. yeast", "1 (16 oz.) can tomatoes"]
    fine += ["1 - 8 oz. pkg. cream cheese", "2-15 oz. cans black beans"]
    # short food words aren't lost units
    fine += ["2 TV dinners", "2 new potatoes", "2 dry figs", "1 wax bean", "1 big onion"]
    lost = ["2-3 T. milk", "1 to 2 c. water", "2 or 3 eggs"]
    unclear = ["2 pk yeast", "2 btls. ketchup"]

    parsed, linked = await _normalize_and_link(user, banana + fine + lost + unclear)
    assert linked  # the group's foods and units these lines link: none by a near miss
    flags = _highlighted(parsed, linked)

    assert {line: flags[line] for line in banana + fine} == {line: [] for line in banana + fine}
    assert {line: flags[line] for line in lost} == {
        "2-3 T. milk": [(CardFlagKind.check_parse, {"value": "2-3", "start": 0, "end": 3})],
        "1 to 2 c. water": [(CardFlagKind.check_parse, {"value": "1 to 2", "start": 0, "end": 6})],
        "2 or 3 eggs": [(CardFlagKind.check_parse, {"value": "3", "start": 5, "end": 6})],
    }
    # nothing read is lost: the note keeps what the fields don't
    notes = {line.original_text: line.note for line in parsed}
    assert [notes[line] for line in lost] == ["to 3", "to 2", "or 3 eggs"]
    assert flags["2 pk yeast"] == [(CardFlagKind.unit_unclear, {"token": "pk", "start": 2, "end": 4})]
    # a container's abbreviation the parser ran into the food ("btls. ketchup", a new food at commit)
    assert flags["2 btls. ketchup"] == [(CardFlagKind.unit_unclear, {"token": "btls.", "start": 2, "end": 7})]


@pytest.mark.asyncio
async def test_mixed_numbers_names_and_second_ingredients_through_the_parser(unique_user_fn_scoped: TestUser):
    """
    A mixed number written with a dash is read whole, a number in a food's name is the food's, and a second
    ingredient the parser runs into the food is checked, while a substitution its note keeps isn't. Shorthand after
    "or" or "+" is written out too, so the parser reads a substitution as one ("2 c. flour (or 1 1/2 c. bread flour)"
    was the food "flour bread flour" while its "c." stayed).
    """
    user = unique_user_fn_scoped
    seed_foods_and_units(user)
    mixed = ["2-1/4 c. flour", "1-1/2 tsp baking soda", "1 - 1/2 c. milk", "1–1/2 c. oats", "3-1/2 oz. coconut"]
    named = ["1 c. 2% milk", "2 c. V8 juice", "1 c. 7-Up", "1/2 tsp. 5-spice powder", '1 9" pie shell, baked']
    named += ["1 lb. 80/20 ground beef"]
    kept = ["1 c. butter (or 1 c. margarine)", "1 pkg. yeast (or 2 1/4 tsp.)"]
    substituted = {
        "2 c. flour (or 1 1/2 c. bread flour)": ("flour", "1 1/2"),
        "1 c. buttermilk (or 1 c. milk + 1 T. vinegar)": ("buttermilk", "1 tbsp"),
        "1 stick butter or 1/2 c. oleo": ("butter", "1/2"),
    }
    merged = {"1 c. sugar, 1 c. flour": "1", "1 c. sugar, 1 c. brown sugar": "1"}

    parsed, linked = await _normalize_and_link(user, mixed + named + kept + list(substituted) + list(merged))
    flags = _highlighted(parsed, linked)

    assert [line.quantity for line in parsed[: len(mixed)]] == [2.25, 1.5, 1.5, 1.5, 3.5]
    assert [line.original_text for line in parsed[: len(mixed)]] == mixed
    kept += list(substituted)
    assert {line: flags[line] for line in mixed + named + kept} == {line: [] for line in mixed + named + kept}
    read = {line.original_text: line for line in parsed}
    for line, (food, amount) in substituted.items():
        assert read[line].food is not None and read[line].food.name == food
        assert amount in read[line].note
    starts = [12, 12]  # the second ingredient's amount, not the first one's
    assert {line: flags[line] for line in merged} == {
        line: [(CardFlagKind.check_parse, {"value": value, "start": start, "end": start + len(value)})]
        for (line, value), start in zip(merged.items(), starts, strict=True)
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

    parsed, linked = await _normalize_and_link(user, lines)

    assert [(line.food and line.food.name, line.food and line.food.id is not None, line.note) for line in parsed] == [
        ("onion", True, "med"),
        ("onion", True, "med., chopped"),
        ("cabbage", True, "med"),
        ("red onion", True, "med"),
        ("onion", True, "lg"),
        ("onion", True, "sml"),
    ]
    assert [line.original_text for line in parsed] == lines
    assert _highlighted(parsed, linked) == {line: [] for line in lines}  # no `linked_fuzzy` either


@pytest.mark.asyncio
async def test_a_food_linked_by_a_near_miss_name_is_flagged(unique_user_fn_scoped: TestUser):
    """
    The matcher links "rd onions" to the group's "red onion" fuzzily: the line is flagged with the food it linked.
    Links by name, plural or alias, and lines whose size word was taken out, aren't.
    """
    user = unique_user_fn_scoped
    seed_foods_and_units(user)
    group_id = user.repos.group_id
    for name, plural in [("onion", "onions"), ("red onion", "red onions")]:
        user.repos.ingredient_foods.create(SaveIngredientFood(name=name, plural_name=plural, group_id=group_id))
    user.repos.ingredient_foods.create(
        SaveIngredientFood(name="green onion", group_id=group_id, aliases=[CreateIngredientFoodAlias(name="scallion")])
    )
    lines = ["2 rd onions", "2 red onions", "3 scallions, sliced", "1 med onion", "1 T. coconut oil", "2 eggs"]

    parsed, linked = await _normalize_and_link(user, lines)

    rd = parsed[0]
    assert rd.food is not None and rd.food.id is not None and rd.food.name == "red onion"  # the fuzzy link
    assert all(line.food is not None and line.food.id is not None for line in parsed)
    flags = _highlighted(parsed, linked)
    # (the parser isn't sure of that line either: `check_parse` with its confidence)
    assert [params for kind, params in flags["2 rd onions"] if kind == CardFlagKind.linked_fuzzy] == [
        {"name": "red onion", "kind": "food", "start": 2, "end": 11}
    ]
    assert {line: flags[line] for line in lines[1:]} == {line: [] for line in lines[1:]}


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


@pytest.mark.asyncio
async def test_units_with_only_a_name_are_linked_exactly(unique_user_fn_scoped: TestUser):
    """
    A group whose units have only a name ("teaspoon", no abbreviation: it didn't seed them, or commit created them):
    the card's "tsp.", "T." or "lb." still links its unit by name, and no line gets `linked_fuzzy`
    """
    user = unique_user_fn_scoped
    seed_foods_and_units(user)  # its foods; its units, besides, have abbreviations
    for name in ("pound", "ounce", "quart"):
        user.repos.ingredient_units.create(SaveIngredientUnit(name=name, group_id=user.repos.group_id))
    with job_session(user) as (_, repos):
        for unit in IngestMatcher(repos).units_by_id.values():  # the seeded spoons and cups lose theirs
            repos.ingredient_units.update(unit.id, unit.model_copy(update={"abbreviation": "", "plural_name": None}))
    banana = ["1 T. coconut oil (melted)", "1/4 t. salt", "1/2 t vanilla", "1/3 C. almond flour"]
    printed = ["1 tsp. salt", "2 Tbsp. butter", "1 lb. ground beef", "8 oz. cheese", "1 qt. milk", "2 lbs. potatoes"]

    parsed, linked = await _normalize_and_link(user, banana + printed)

    assert [(line.unit and line.unit.name, line.unit is not None and line.unit.id is not None) for line in parsed] == [
        ("tablespoon", True),
        ("teaspoon", True),
        ("teaspoon", True),
        ("cup", True),
        ("teaspoon", True),
        ("tablespoon", True),
        ("pound", True),
        ("ounce", True),
        ("quart", True),
        ("pound", True),
    ]
    flags = _highlighted(parsed, linked)
    fuzzy = {line: [kind for kind, _ in flags[line] if kind == CardFlagKind.linked_fuzzy] for line in flags}
    assert fuzzy == {line: [] for line in banana + printed}


@pytest.mark.asyncio
async def test_a_measure_after_the_unit_is_kept_and_not_checked(unique_user_fn_scoped: TestUser):
    """
    The parser drops "(8 oz.)" of "1 pkg. (8 oz.) cream cheese" (it reads it as a substitution): the note keeps it as
    written and, the fields being right, it needs no tap, as the same size before the unit doesn't
    """
    user = unique_user_fn_scoped
    seed_foods_and_units(user)
    measures = {
        "1 pkg. (8 oz.) cream cheese": "(8 oz.)",
        "1 can (10 3/4 oz.) soup": "(10 3/4 oz.)",
        "1/2 c. (1 stick) butter": "(1 stick)",
        "1 c. (8 oz.) sour cream": "(8 oz.)",
        "2 c. (16 oz.) cottage cheese": "(16 oz.)",
        "1 pkg. (1/4 oz.) yeast": "(1/4 oz.)",
        "1 (8 oz.) pkg. cream cheese": "(8 oz.)",
    }
    checked = {"2 c. (3 c.) flour": "3", "1 c. (or 2) eggs": "2"}

    parsed = await _normalize(user, [*measures, *checked])

    assert {line.original_text: line.note for line in parsed[: len(measures)]} == measures
    flags = _highlighted(parsed)
    assert {line: flags[line] for line in measures} == {line: [] for line in measures}
    assert {line: [params["value"] for _, params in flags[line]] for line in checked} == {
        line: [value] for line, value in checked.items()
    }


@pytest.mark.asyncio
async def test_alternatives_and_second_ingredients_are_kept_and_checked(unique_user_fn_scoped: TestUser):
    """
    The parser split "or margarine" off a line (its substitutions, which nothing kept: the card was clean and committed
    without it), or kept "and 1 t. soda" only in its note: the note keeps each as the line has it, and the line is
    checked. An alternative with its own amount, which the parser keeps whole in its note, reads as written.
    """
    user = unique_user_fn_scoped
    IngredientUnitsSeeder(user.repos).seed("en-US")
    IngredientFoodsSeeder(user.repos).seed("en-US")  # a linked food, "walnut", is split off by its id
    split = {
        "1/2 c. butter or margarine, softened": ("softened, or margarine", "margarine"),
        "1 c. chopped pecans or walnuts": ("chopped, or walnuts", "walnuts"),
        "2 c. flour and 1 t. baking powder": ("and 1 t. baking powder", "baking powder"),
        "1 c. (8 oz.) sour cream or yogurt": ("or yogurt, (8 oz.)", "yogurt"),
    }
    second = {
        "2 c. flour and 1 t. soda": "1",
        "1 t. salt and 1/2 t. pepper": "1/2",
        "2 cups flour and 1 teaspoon soda": "1",
    }
    kept = ["1 c. butter (or 1 c. margarine)", "2 c. flour (or 1 1/2 c. bread flour)", "1 c. sugar plus 2 T. more"]

    parsed, linked = await _normalize_and_link(user, [*split, *second, *kept])

    read = {line.original_text: line for line in parsed}
    assert {line: read[line].note for line in split} == {line: note for line, (note, _) in split.items()}
    assert all(split_off(read[line]) for line in split)
    assert all(note in read[line].display for line, (note, _) in split.items())
    flags = _highlighted(parsed, linked)
    for line, (_, alternative) in split.items():
        (params,) = [params for kind, params in flags[line] if kind == CardFlagKind.check_parse]
        assert params["alternative"] == alternative
        assert line[params["start"] : params["end"]] in (alternative, params.get("value"))
    assert {line: [params.get("value") for _, params in flags[line]] for line in second} == {
        line: [value] for line, value in second.items()
    }
    assert all(
        not split_off(read[line]) and second_line in read[line].note
        for line, second_line in [("2 c. flour and 1 t. soda", "soda"), ("1 t. salt and 1/2 t. pepper", "pepper")]
    )
    assert {line: flags[line] for line in kept} == {line: [] for line in kept}


@pytest.mark.asyncio
async def test_a_written_out_unit_links_the_groups_own_never_a_near_miss(unique_user_fn_scoped: TestUser):
    """
    Mealie's own units have no "square" or "package", but "quart" and "pack": "sq." is a new unit, never 2 quarts of
    chocolate, and "pkg." is the group's pack, with nothing to look at
    """
    user = unique_user_fn_scoped
    IngredientUnitsSeeder(user.repos).seed("en-US")
    squares = ["2 sq. unsweetened chocolate", "1 Sq. chocolate", "2 sq chocolate"]
    packages = ["1 pkg. dry yeast", "1 (8 oz.) pkg. cream cheese", "1 Pkg. yeast", "1 sm. pkg. instant pudding"]
    packages += ["2 pkgs. yeast"]

    parsed, linked = await _normalize_and_link(user, [*squares, *packages, "1 T. sugar", "1 qt. milk"])

    units = {line.original_text: line.unit for line in parsed}
    assert {line: units[line] and (units[line].name, units[line].id is None) for line in squares} == dict.fromkeys(
        squares, ("square", True)
    )
    assert {line: units[line] and (units[line].name, units[line].id is not None) for line in packages} == dict.fromkeys(
        packages, ("pack", True)
    )
    # shorthand the group's units name, and a unit written as the group has it, as before
    assert [(units[line].name, units[line].id is not None) for line in ("1 T. sugar", "1 qt. milk")] == [
        ("tablespoon", True),
        ("quart", True),
    ]
    flags = _highlighted(parsed, linked)
    assert [line for line in flags if any(kind == CardFlagKind.linked_fuzzy for kind, _ in flags[line])] == []


@pytest.mark.asyncio
async def test_a_written_out_unit_links_the_groups_unit_named_in_the_plural(unique_user_fn_scoped: TestUser):
    """
    A group whose units are named only in the plural ("Tablespoons", "Packages", as typed by hand or imported) or
    with an optional plural ("cup(s)"): card shorthand links them, as lines written in full do, rather than becoming
    new units "tbsp", "package" or "square" beside them, which "Add all clean cards" would create
    """
    user = unique_user_fn_scoped
    seed_foods_and_units(user)  # its foods; its units are replaced
    with job_session(user) as (_, repos):
        for unit in IngestMatcher(repos).units_by_id.values():
            repos.ingredient_units.delete(unit.id)
    names = ("Tablespoons", "Teaspoons", "cup(s)", "Packages", "Squares", "Dozens")
    for name in names:
        user.repos.ingredient_units.create(SaveIngredientUnit(name=name, group_id=user.repos.group_id))
    lines = ["1 T. coconut oil (melted)", "1/4 t. salt", "1/3 C. almond flour", "1 pkg. yeast", "2 pkgs. yeast"]
    lines += ["2 sq. chocolate", "1 doz. eggs", "1 tablespoon sugar", "1 cup milk"]

    parsed, linked = await _normalize_and_link(user, lines)

    units = [(line.unit and line.unit.name, line.unit is not None and line.unit.id is not None) for line in parsed]
    assert units == [
        ("Tablespoons", True),
        ("Teaspoons", True),
        ("cup(s)", True),
        ("Packages", True),
        ("Packages", True),
        ("Squares", True),
        ("Dozens", True),
        ("Tablespoons", True),
        ("cup(s)", True),
    ]
    card = CardDraft(name="Card", ingredients=parsed)
    flags = compute_flags(card, ExtractionMeta(language="English"), {}, linked=linked)
    assert [flag.kind for flag in flags if flag.kind in (CardFlagKind.new_unit, CardFlagKind.linked_fuzzy)] == []


@pytest.mark.asyncio
async def test_a_second_amounts_shorthand_isnt_read_as_the_food(unique_user_fn_scoped: TestUser):
    """
    "1 c. plus 2 T. flour" was the food "T. flour" (which commit would create) and the note "plus, plus 2 T.": its
    shorthand is written out too. The line still asks for a look, and the note keeps the second amount once.
    """
    user = unique_user_fn_scoped
    seed_foods_and_units(user)
    lines = [
        "1 c. plus 2 T. flour",
        "1 c. + 2 T. sugar",
        "1/2 c. plus 1 T. milk",
        "1 c. sugar plus 2 T.",
        "3 eggs or 2 lg.",
    ]

    parsed = await _normalize(user, lines)

    assert [line.food and line.food.name for line in parsed] == ["flour", "sugar", "milk", "sugar", "egg"]
    notes = [line.note for line in parsed]
    assert [note.count("2") for note in notes[:2]] == [1, 1] and "1" in notes[2]
    assert notes[3:] == ["plus 2 T.", "lg., or 2"]  # the joiner once, and the size once
    flags = _highlighted(parsed)
    assert all(CardFlagKind.check_parse in [kind for kind, _ in flags[line]] for line in lines)


@pytest.mark.asyncio
async def test_a_size_word_that_starts_a_name_stays_in_it(unique_user_fn_scoped: TestUser):
    """ "Big Red" soda isn't a big "Red soda", made a food silently: the parser reads the name, and it gets a look"""
    user = unique_user_fn_scoped
    seed_foods_and_units(user)

    soda, can, onion = await _normalize(user, ["1 c. Big Red soda", "1 (12 oz.) can Big Red", "1 big onion"])

    assert [line.food and line.food.name for line in (soda, can, onion)] == ["Big Red soda", "Big Red", "onion"]
    assert (soda.note, can.note, onion.note) == ("", "(12 oz.)", "big")
    flags = _highlighted([soda, can, onion])
    assert [kind for kind, _ in flags["1 c. Big Red soda"]] == [CardFlagKind.check_parse]
    assert flags["1 big onion"] == []
