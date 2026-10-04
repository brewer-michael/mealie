"""
Linking a card's ingredients to the group's foods and units (docs/ai/PHASE2.md §5). One matcher per task, and a fresh
one per commit; it only ever reads.
"""

from collections.abc import Iterable
from functools import cached_property

from pydantic import UUID4
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.interfaces import LoaderOption

from mealie.db.models.recipe import IngredientFoodModel, IngredientUnitModel
from mealie.schema.recipe.recipe_ingredient import IngredientFood, IngredientUnit
from mealie.schema.recipe_ingest import CardDraftIngredient
from mealie.schema.response.pagination import PaginationQuery
from mealie.services.ai.tools.recipes import FoodMatcher


class _UnitWithAliases(IngredientUnit):
    @classmethod
    def loader_options(cls) -> list[LoaderOption]:
        return [*super().loader_options(), selectinload(IngredientUnitModel.aliases)]


class _FoodWithAliases(IngredientFood):
    @classmethod
    def loader_options(cls) -> list[LoaderOption]:
        return [*super().loader_options(), selectinload(IngredientFoodModel.aliases)]


def _ids_filter(ids: Iterable[UUID4]) -> str:
    """A query filter for the rows with these ids"""
    return "id IN [" + ", ".join(f'"{item}"' for item in sorted(str(item) for item in ids)) + "]"


def _food_names(food: IngredientFood) -> list[str]:
    return [name for name in (food.name, food.plural_name, *(alias.name for alias in food.aliases or [])) if name]


def _unit_names(unit: IngredientUnit) -> list[str]:
    names = (unit.name, unit.plural_name, unit.abbreviation, unit.plural_abbreviation)
    return [name for name in (*names, *(alias.name for alias in unit.aliases or [])) if name]


class IngestMatcher(FoodMatcher):
    """
    Phase 1's `FoodMatcher` (foods and their aliases in a few queries) with units loaded the same way.

    - For parsing (the NLP parser's `data_matcher`): the inherited `find_food_match` and `find_unit_match`, which fall
      back to fuzzy matching.
    - For commit: `exact_food` and `exact_unit` (name, plural or alias; for units also the abbreviations), so a food
      created since extraction is found rather than created twice; and `food_by_id` / `unit_by_id`, which drop ids
      that aren't the group's.
    - For the flags: `unit_names`, and `linked_names` (every name of the foods and units a draft links), so a link
      the parser made by a near-miss name is flagged (`linked_fuzzy`).
    """

    @cached_property
    def units_by_id(self) -> dict[UUID4, IngredientUnit]:
        query = PaginationQuery(page=1, per_page=-1)
        return {unit.id: unit for unit in self.repos.ingredient_units.page_all(query, override=_UnitWithAliases).items}

    def exact_food(self, name: str | None) -> IngredientFood | None:
        """The group's food with this name, plural name or alias (normalized), if any"""
        if not name or not name.strip():
            return None
        return self.foods_by_alias.get(IngredientFoodModel.normalize(name))

    def exact_unit(self, name: str | None) -> IngredientUnit | None:
        """The group's unit with this name, plural, abbreviation or alias (normalized), if any"""
        if not name or not name.strip():
            return None
        return self.units_by_alias.get(IngredientUnitModel.normalize(name))

    def unit_names(self) -> list[str]:
        """
        Every name, plural, abbreviation and alias of the group's units (normalized): what `flags.compute_flags` takes
        as `units`, so a short word on a card that is one of the group's own units counts as a lost unit
        """
        return list(self.units_by_alias)

    def food_by_id(self, food_id: UUID4 | None) -> IngredientFood | None:
        """The group's food with this id; None for an id that isn't one of the group's"""
        return self.foods_by_id.get(food_id) if food_id else None

    def unit_by_id(self, unit_id: UUID4 | None) -> IngredientUnit | None:
        """The group's unit with this id; None for an id that isn't one of the group's"""
        return self.units_by_id.get(unit_id) if unit_id else None

    def linked_names(self, ingredients: Iterable[CardDraftIngredient]) -> dict[UUID4, list[str]]:
        """
        Every name the group's foods and units the lines link go by, by id: name, plural and aliases, and a unit's
        abbreviations; what `flags.compute_flags` takes as `linked`. Ids that aren't the group's aren't in it. Uses the
        foods and units parsing already loaded, else reads only the linked ones (a save needs no more).
        """
        ingredients = list(ingredients)
        food_ids = {line.food.id for line in ingredients if line.food and line.food.id}
        unit_ids = {line.unit.id for line in ingredients if line.unit and line.unit.id}

        names: dict[UUID4, list[str]] = {}
        if food_ids:
            if "foods_by_id" in self.__dict__:  # loaded by parsing
                foods = [self.foods_by_id[food_id] for food_id in food_ids if food_id in self.foods_by_id]
            else:
                query = PaginationQuery(page=1, per_page=-1, query_filter=_ids_filter(food_ids))
                foods = self.repos.ingredient_foods.page_all(query, override=_FoodWithAliases).items
            names.update((food.id, _food_names(food)) for food in foods)
        if unit_ids:
            if "units_by_id" in self.__dict__:
                units = [self.units_by_id[unit_id] for unit_id in unit_ids if unit_id in self.units_by_id]
            else:
                query = PaginationQuery(page=1, per_page=-1, query_filter=_ids_filter(unit_ids))
                units = self.repos.ingredient_units.page_all(query, override=_UnitWithAliases).items
            names.update((unit.id, _unit_names(unit)) for unit in units)
        return names
