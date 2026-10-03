"""Shopping list tools: read a list, and add free-text items or a recipe's ingredients to one"""

from typing import Annotated, Self

from pydantic import UUID4, BaseModel, Field, StringConstraints, model_validator
from rapidfuzz import fuzz, process
from slugify import slugify

from mealie.schema.household.group_shopping_list import (
    ShoppingListAddRecipeParamsBulk,
    ShoppingListItemCreate,
    ShoppingListItemOut,
    ShoppingListItemsCollectionOut,
    ShoppingListSummary,
)
from mealie.schema.response.pagination import OrderDirection, PaginationQuery
from mealie.services.event_bus_service.event_types import EventOperation, EventShoppingListItemBulkData, EventTypes
from mealie.services.household_services.shopping_lists import ShoppingListService

from .base import AITool, ToolArgs, ToolContext, ToolNotFoundError, ToolResult, run_blocking
from .recipes import find_recipe, scale_base, spoken_recipe_name
from .speech import count_of, join_words, spoken

LIST_FUZZY_MATCH_THRESHOLD = 80
SPOKEN_ITEMS = 5
_LEADING_FILLER = {"the", "my", "our", "your", "a"}
_GENERIC_WORDS = {"shopping", "grocery", "list"}

ListName = Annotated[
    str | None,
    Field(
        min_length=1,
        max_length=100,
        description=(
            "Which shopping list, by name, e.g. 'Costco'. Leave out, or give a generic name such as "
            "'the shopping list', for the household's first list."
        ),
    ),
]


def _singular(word: str) -> str:
    """A rough English singular, so `groceries` matches `grocery` and `lists` matches `list`"""
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 2 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _list_words(name: str | None) -> list[str]:
    """A list name's words for matching: singular, and without a leading `the`, `my` or `our`"""
    words = [_singular(word) for word in slugify(name or "", separator=" ").split()]
    while words and words[0] in _LEADING_FILLER:
        words.pop(0)
    return words


def _core_name(words: list[str]) -> str:
    """What sets a list apart: `costco` for `Costco Shopping List` and `the costco grocery list` alike"""
    return " ".join(word for word in words if word not in _GENERIC_WORDS)


def _full_name(words: list[str]) -> str:
    """A list name without a trailing `list`: `grocery` for both `Groceries` and `grocery list`"""
    return " ".join(words[:-1] if words and words[-1] == "list" else words)


def _closest_list(key: str, by_key: dict[str, ShoppingListSummary]) -> ShoppingListSummary | None:
    if key in by_key:
        return by_key[key]

    found = process.extractOne(key, by_key.keys(), scorer=fuzz.token_set_ratio, score_cutoff=LIST_FUZZY_MATCH_THRESHOLD)
    return by_key[found[0]] if found else None


def _list_phrase(shopping_list: ShoppingListSummary) -> str:
    """How a list is named in a sentence: `the Groceries list`, `Weekly Shopping List`"""
    name = spoken(shopping_list.name, 40)
    if not name:
        return "your shopping list"
    return name if "list" in name.lower() else f"the {name} list"


def _sentence_start(text: str) -> str:
    return text[:1].upper() + text[1:]


def find_shopping_list(ctx: ToolContext, name: str | None) -> ShoppingListSummary:
    """
    One of the caller's household's lists: the closest match by name, or the first created when no name is
    given or a generic one ("the shopping list", "my grocery list") that no list is called
    """
    query = PaginationQuery(page=1, per_page=-1, order_by="created_at", order_direction=OrderDirection.asc)
    lists: list[ShoppingListSummary] = ctx.repos.group_shopping_lists.page_all(
        query, override=ShoppingListSummary
    ).items
    if not lists:
        raise ToolNotFoundError("This household doesn't have a shopping list yet.")
    if name is None:
        return lists[0]

    words = _list_words(name)
    if core := _core_name(words):
        by_core: dict[str, ShoppingListSummary] = {}
        for shopping_list in lists:
            if key := _core_name(_list_words(shopping_list.name)):
                by_core.setdefault(key, shopping_list)
        if match := _closest_list(core, by_core):
            return match
    else:
        full = _full_name(words)
        return next((lst for lst in lists if _full_name(_list_words(lst.name)) == full), lists[0])

    names = join_words([spoken(lst.name, 40) or "unnamed" for lst in lists[:SPOKEN_ITEMS]])
    raise ToolNotFoundError(f"I couldn't find a shopping list called {spoken(name, 40)}. Your lists are: {names}.")


def _list_items(
    ctx: ToolContext, list_id: UUID4, *, unchecked_only: bool = True, last_only: bool = False
) -> list[ShoppingListItemOut]:
    """The list's items in list order, or with `last_only` just the one furthest down"""
    query = PaginationQuery(
        page=1,
        per_page=1 if last_only else -1,
        query_filter=f"shopping_list_id={list_id}" + (" AND checked=false" if unchecked_only else ""),
        order_by="position",
        order_direction=OrderDirection.desc if last_only else OrderDirection.asc,
    )
    return ctx.repos.group_shopping_list_item.page_all(query).items


def _item_text(item: ShoppingListItemOut) -> str:
    # free-text items, as the list page and Home Assistant add them, carry a quantity of 0 or 1 nobody says aloud
    if item.note and not (item.food or item.unit) and (item.quantity or 0) <= 1:
        return item.note
    return item.display or item.note or ""


# ==================================================================================================================
# get_shopping_list


class GetShoppingListArgs(ToolArgs):
    list_name: ListName = None


class ShoppingItem(BaseModel):
    id: UUID4
    text: str
    label: str | None = Field(description="The item's label, e.g. the store section")


class GetShoppingListResult(ToolResult):
    list_id: UUID4
    list_name: str
    items: list[ShoppingItem] = Field(description="The unchecked items, in list order")


def _get_shopping_list(ctx: ToolContext, args: GetShoppingListArgs) -> GetShoppingListResult:
    shopping_list = find_shopping_list(ctx, args.list_name)
    items = [
        ShoppingItem(id=item.id, text=_item_text(item), label=item.label.name if item.label else None)
        for item in _list_items(ctx, shopping_list.id)
    ]

    phrase = _sentence_start(_list_phrase(shopping_list))
    said = [spoken(item.text, 40) for item in items[:SPOKEN_ITEMS]]
    if not items:
        speech = f"{phrase} is empty."
    elif len(items) <= SPOKEN_ITEMS:
        speech = f"{phrase} has {count_of(len(items), 'item')}: {join_words(said)}."
    else:
        speech = f"{phrase} has {count_of(len(items), 'item')}, including {join_words(said)}."

    return GetShoppingListResult(
        speech=speech, list_id=shopping_list.id, list_name=shopping_list.name or "", items=items
    )


get_shopping_list = AITool(
    name="get_shopping_list",
    description=(
        "Read what's still to buy on one of the household's shopping lists. Without list_name, reads the "
        "household's first list. Returns the list's id and name and its unchecked items."
    ),
    args=GetShoppingListArgs,
    result=GetShoppingListResult,
    writes=False,
    handler=run_blocking(_get_shopping_list),
)


# ==================================================================================================================
# add_to_shopping_list


class AddToShoppingListArgs(ToolArgs):
    items: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]] = Field(
        default_factory=list,
        max_length=50,
        description="Free-text items to add, e.g. ['eggs', '2 litres of milk']. Give this or recipe_slug.",
    )
    recipe_slug: str | None = Field(
        default=None,
        min_length=1,
        max_length=250,
        description="Add every ingredient of this recipe, by its slug from search_recipes, instead of items",
    )
    servings: float | None = Field(
        default=None,
        gt=0,
        le=1000,
        description=(
            "With recipe_slug: scale the ingredients to this many servings, from the recipe's servings count "
            "(or, without one, its yield quantity)"
        ),
    )
    list_name: ListName = None

    @model_validator(mode="after")
    def _items_or_recipe(self) -> Self:
        if bool(self.items) == bool(self.recipe_slug):
            raise ValueError("give either items or recipe_slug, not both")
        if self.servings is not None and not self.recipe_slug:
            raise ValueError("servings only applies with recipe_slug")
        return self


class AddToShoppingListResult(ToolResult):
    list_id: UUID4
    list_name: str
    added: int = Field(description="How many list items were created or merged into existing ones")
    items: list[str] = Field(description="Those items, as they now read on the list")


def _publish_item_changes(ctx: ToolContext, list_id: UUID4, changes: ShoppingListItemsCollectionOut) -> None:
    """The events upstream's shopping list item endpoints publish for the same changes"""
    for operation, items in [
        (EventOperation.create, changes.created_items),
        (EventOperation.update, changes.updated_items),
    ]:
        if items:
            ctx.publish_event(
                EventTypes.shopping_list_updated,
                EventShoppingListItemBulkData(
                    operation=operation, shopping_list_id=list_id, shopping_list_item_ids=[i.id for i in items]
                ),
            )


def _add_to_shopping_list(ctx: ToolContext, args: AddToShoppingListArgs) -> AddToShoppingListResult:
    shopping_list = find_shopping_list(ctx, args.list_name)
    service = ShoppingListService(ctx.repos)
    phrase = _list_phrase(shopping_list)

    if args.recipe_slug:
        recipe = find_recipe(ctx, args.recipe_slug)
        if recipe.id is None:
            raise ToolNotFoundError(f"I couldn't find a recipe called {spoken_recipe_name(recipe)}.")

        scale, scale_note = 1.0, ""
        if args.servings is not None:
            if scale_base(recipe) > 0:
                scale = args.servings / scale_base(recipe)
            else:
                scale_note = "The recipe doesn't say how many it serves, so I added it unscaled."

        # upstream's recipe-to-list service, as POST /households/shopping/lists/{id}/recipe uses it: merges
        # with what's on the list and skips foods the household has on hand. (Upstream doesn't scale the
        # second amount of a food a recipe lists twice; that's left to upstream.)
        _, changes = service.add_recipe_ingredients_to_list(
            shopping_list.id, [ShoppingListAddRecipeParamsBulk(recipe_id=recipe.id, recipe_increment_quantity=scale)]
        )
        added = len(changes.created_items) + len(changes.updated_items)
        name = spoken_recipe_name(recipe)
        if added:
            speech = f"I added {count_of(added, 'item')} from {name} to {phrase}."
        else:
            speech = f"Nothing from {name} needed adding to {phrase}."
        speech = f"{speech} {scale_note}".strip()

    else:
        # added as the list page adds a typed item: a note, at the end of the list, labelled from its text
        last = _list_items(ctx, shopping_list.id, unchecked_only=False, last_only=True)
        position = last[0].position if last else -1
        changes = service.bulk_create_items(
            [
                ShoppingListItemCreate(shopping_list_id=shopping_list.id, note=text, quantity=0, position=position + i)
                for i, text in enumerate(args.items, start=1)
            ]
        )
        added = len(changes.created_items) + len(changes.updated_items)
        if len(args.items) <= SPOKEN_ITEMS:
            speech = f"I added {join_words([spoken(text, 40) for text in args.items])} to {phrase}."
        else:
            speech = f"I added {count_of(len(args.items), 'item')} to {phrase}."

    _publish_item_changes(ctx, shopping_list.id, changes)
    return AddToShoppingListResult(
        speech=speech,
        list_id=shopping_list.id,
        list_name=shopping_list.name or "",
        added=added,
        items=[_item_text(item) for item in changes.created_items + changes.updated_items],
    )


add_to_shopping_list = AITool(
    name="add_to_shopping_list",
    description=(
        "Add to one of the household's shopping lists: free-text items (items), or every ingredient of a recipe "
        "(recipe_slug, optionally scaled with servings). Items already on the list are merged rather than "
        "duplicated, and a recipe's foods the household has on hand are skipped. Without list_name, adds to the "
        "household's first list. Returns how many list items were added or updated."
    ),
    args=AddToShoppingListArgs,
    result=AddToShoppingListResult,
    writes=True,
    handler=run_blocking(_add_to_shopping_list),
)
