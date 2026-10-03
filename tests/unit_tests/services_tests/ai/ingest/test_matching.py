"""Linking card ingredients to the group's foods and units, read-only (docs/ai/PHASE2.md §5)"""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

import sqlalchemy as sa

from mealie.db.db_setup import session_context
from mealie.repos.all_repositories import get_repositories
from mealie.schema.recipe.recipe_ingredient import (
    CreateIngredientFoodAlias,
    CreateIngredientUnitAlias,
    SaveIngredientFood,
    SaveIngredientUnit,
)
from mealie.services.ai.ingest.matching import IngestMatcher
from tests.utils.fixture_schemas import TestUser


def _seed(user: TestUser) -> None:
    group_id = user.repos.group_id
    user.repos.ingredient_foods.create(
        SaveIngredientFood(
            name="coconut oil",
            plural_name=None,
            group_id=group_id,
            aliases=[CreateIngredientFoodAlias(name="virgin coconut oil")],
        )
    )
    user.repos.ingredient_foods.create(
        SaveIngredientFood(name="egg", plural_name="eggs", group_id=group_id, aliases=[])
    )
    user.repos.ingredient_units.create(
        SaveIngredientUnit(
            name="tablespoon",
            plural_name="tablespoons",
            abbreviation="tbsp",
            group_id=group_id,
            aliases=[CreateIngredientUnitAlias(name="tbs")],
        )
    )
    user.repos.ingredient_units.create(
        SaveIngredientUnit(name="package", abbreviation="pkg", group_id=group_id, aliases=[])
    )


@contextmanager
def _counting_queries(session) -> Iterator[list[str]]:
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(statement)

    engine = session.get_bind()
    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)


def test_exact_matches_by_name_plural_or_alias(unique_user_fn_scoped: TestUser):
    _seed(unique_user_fn_scoped)
    matcher = IngestMatcher(unique_user_fn_scoped.repos)

    assert matcher.exact_food("Coconut Oil").name == "coconut oil"
    assert matcher.exact_food("virgin coconut oil").name == "coconut oil"
    assert matcher.exact_food("eggs").name == "egg"
    assert matcher.exact_food("coconut oils") is None  # exact means exact
    assert matcher.exact_food("") is None
    assert matcher.exact_food(None) is None

    assert matcher.exact_unit("tablespoons").name == "tablespoon"
    assert matcher.exact_unit("tbsp").name == "tablespoon"
    assert matcher.exact_unit("TBS").name == "tablespoon"
    assert matcher.exact_unit("pkg").name == "package"
    assert matcher.exact_unit("teaspoon") is None


def test_parsing_falls_back_to_fuzzy_matches(unique_user_fn_scoped: TestUser):
    _seed(unique_user_fn_scoped)
    matcher = IngestMatcher(unique_user_fn_scoped.repos)

    assert matcher.find_food_match("cocnut oil").name == "coconut oil"
    assert matcher.find_unit_match("tablespon").name == "tablespoon"
    assert matcher.find_food_match("anchovies") is None


def test_ids_from_another_group_are_dropped(unique_user_fn_scoped: TestUser, unique_user: TestUser):
    _seed(unique_user_fn_scoped)
    _seed(unique_user)
    mine = IngestMatcher(unique_user_fn_scoped.repos)
    theirs = IngestMatcher(unique_user.repos)

    my_food = mine.exact_food("egg")
    their_food = theirs.exact_food("egg")
    their_unit = theirs.exact_unit("tbsp")
    assert my_food and their_food and their_unit

    assert mine.food_by_id(my_food.id) == my_food
    assert mine.food_by_id(their_food.id) is None
    assert mine.unit_by_id(their_unit.id) is None
    assert mine.unit_by_id(uuid4()) is None
    assert mine.food_by_id(None) is None


def test_foods_and_units_load_with_their_aliases_in_a_few_queries(unique_user_fn_scoped: TestUser):
    user = unique_user_fn_scoped
    for i in range(6):
        user.repos.ingredient_units.create(
            SaveIngredientUnit(
                name=f"unit {i}", group_id=user.repos.group_id, aliases=[CreateIngredientUnitAlias(name=f"u{i}")]
            )
        )
        user.repos.ingredient_foods.create(
            SaveIngredientFood(
                name=f"food {i}", group_id=user.repos.group_id, aliases=[CreateIngredientFoodAlias(name=f"f{i}")]
            )
        )

    with session_context() as session:
        matcher = IngestMatcher(get_repositories(session, group_id=user.repos.group_id, household_id=None))
        with _counting_queries(session) as statements:
            assert matcher.exact_unit("u5") is not None
            assert matcher.exact_food("f5") is not None

        # not one query per unit or food for its aliases
        assert len(statements) <= 12


def test_matching_never_writes(unique_user_fn_scoped: TestUser):
    _seed(unique_user_fn_scoped)
    with session_context() as session:
        matcher = IngestMatcher(
            get_repositories(session, group_id=unique_user_fn_scoped.repos.group_id, household_id=None)
        )
        with _counting_queries(session) as statements:
            matcher.exact_food("brand new food")
            matcher.exact_unit("brand new unit")
            matcher.find_food_match("another food")
            matcher.find_unit_match("another unit")

        assert statements
        assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
        assert not session.new and not session.dirty
