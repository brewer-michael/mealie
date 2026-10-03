"""Read-only recipe tools: search, read a recipe or one of its steps, and suggest recipes from ingredients"""

import math
import re
from functools import cached_property, lru_cache
from typing import Annotated, Any, ClassVar, Literal

from pydantic import UUID4, BaseModel, Field, StringConstraints
from slugify import slugify
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.interfaces import LoaderOption

from mealie.core.exceptions import NoEntryFound
from mealie.db.models.recipe import IngredientFoodModel
from mealie.lang.providers import get_all_translations
from mealie.repos.repository_factory import RepositoryCategories, RepositoryTags
from mealie.schema.recipe.recipe import Recipe, RecipeSummary
from mealie.schema.recipe.recipe_category import CategoryOut, TagOut
from mealie.schema.recipe.recipe_ingredient import CreateIngredientFood, IngredientFood, RecipeIngredient
from mealie.schema.recipe.recipe_suggestion import RecipeSuggestionQuery
from mealie.schema.response.pagination import PaginationQuery
from mealie.services.matching import find_match
from mealie.services.parser_services._base import DataMatcher
from mealie.services.query_filter.builder import QueryFilterBuilder
from mealie.services.recipe.recipe_service import RecipeService
from mealie.services.scraper.cleaner import parse_duration

from .base import AITool, ToolArgs, ToolContext, ToolNotFoundError, ToolResult, run_blocking
from .speech import (
    count_of,
    end_sentence,
    first_sentences,
    join_words,
    number,
    plain_numbers,
    plain_text,
    spoken,
    spoken_minutes,
)

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Slug = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=250),
    Field(description="The recipe's slug, as returned by search_recipes (its id also works)"),
]

MAX_MINUTES = 7 * 24 * 60
ORGANIZER_FUZZY_MATCH_THRESHOLD = 85
SEARCH_PAGE_SIZE = 50
SEARCH_MAX_PAGES = 10
"""With a time limit, results are filtered after the query, so up to this many pages are read to fill `limit`"""
HAS_A_TIME = "total_time IS NOT NULL OR prep_time IS NOT NULL OR perform_time IS NOT NULL OR cook_time IS NOT NULL"

_DIGIT = re.compile(r"\d")
_RANGE_START = r"\d+(?:[.,]\d+)?\s*(?:-|–|to)\s*"
_AMOUNT = r"\d+(?:[.,]\d+)?(?:\s+\d+/\d+)?|\d+/\d+"


# ==================================================================================================================
# Shared helpers


@lru_cache
def _duration_pattern() -> tuple[re.Pattern[str], dict[str, int]]:
    """Matches `<number> <unit>` in English and every locale Mealie writes recipe times in"""
    minutes_per_unit = {"day": 24 * 60, "hour": 60, "minute": 1}
    words: dict[str, int] = {
        "d": 24 * 60,
        "h": 60,
        "hr": 60,
        "hrs": 60,
        "m": 1,
        "min": 1,
        "mins": 1,
    }
    for unit, minutes in minutes_per_unit.items():
        for translation in get_all_translations(f"datetime.{unit}").values():
            for word in re.split(r"\s*[|/]\s*|\s+-\s+", translation.lower()):
                if word and not any(c.isdigit() or c in "{}" for c in word):
                    words.setdefault(word.strip(), minutes)

    alternation = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    # a range (`30-40 minutes`) counts as its upper end
    pattern = re.compile(rf"(?:{_RANGE_START})?(?P<amount>{_AMOUNT})\s*(?P<unit>{alternation})(?![^\W\d])")
    return pattern, words


def _amount(text: str) -> float:
    """`2`, `1.5`, `1,5`, `1/2` or `1 1/2` as a number"""
    total = 0.0
    for part in text.split():
        numerator, _, denominator = part.partition("/")
        if denominator:
            total += int(numerator) / int(denominator) if int(denominator) else 0
        else:
            total += float(part.replace(",", "."))
    return total


def parse_minutes(text: str | None) -> int | None:
    """
    Minutes in a recipe time as Mealie stores it: free text such as `1 hour 30 minutes`, `1 1/2 hours`,
    `45 min` or `1h30` (in any of Mealie's languages), `1:30`, an ISO 8601 duration, or a bare number of
    minutes. None if there's no time in it, or a number in it whose unit isn't known, since then the time
    can't be read reliably.
    """
    text = plain_numbers(text or "").strip().lower()
    if not text:
        return None

    if re.fullmatch(r"\d+(?:[.,]\d+)?", text):
        return round(float(text.replace(",", "."))) or None

    if clock := re.fullmatch(r"(\d+):([0-5]\d)", text):
        return int(clock[1]) * 60 + int(clock[2]) or None

    if text.startswith("p"):
        try:
            return math.ceil(parse_duration(text.upper()).total_seconds() / 60) or None
        except ValueError:
            pass

    pattern, words = _duration_pattern()
    total = 0.0
    end = 0
    last_unit = 0
    for match in pattern.finditer(text):
        if _DIGIT.search(text, end, match.start()):
            return None
        total += _amount(match["amount"]) * words[match["unit"]]
        last_unit = words[match["unit"]]
        end = match.end()

    if last_unit == 60 and (minutes := re.fullmatch(r"\s*(\d{1,2})", text[end:])):
        total += int(minutes[1])  # `1h30`, `1 hour 30`
    elif _DIGIT.search(text, end):
        return None

    return round(total) or None


def recipe_minutes(recipe: RecipeSummary) -> int | None:
    """The recipe's total time in minutes, or prep plus cooking time when no total is given"""
    if (total := parse_minutes(recipe.total_time)) is not None:
        return total

    parts = [parse_minutes(recipe.prep_time), parse_minutes(recipe.perform_time or recipe.cook_time)]
    known = [p for p in parts if p is not None]
    return sum(known) if known else None


def spoken_recipe_name(recipe: RecipeSummary) -> str:
    return spoken(recipe.name) or "That recipe"


def find_recipe(ctx: ToolContext, slug: str) -> Recipe:
    """
    A recipe in the caller's group, by slug or id, through upstream's recipe service. A name passed by
    mistake is tried as a slug too.
    """
    service = RecipeService(ctx.repos, ctx.user, ctx.household, translator=ctx.translator)
    for candidate in dict.fromkeys([slug, slugify(slug)]):
        if not candidate:
            continue
        try:
            return service.get_one(candidate)
        except NoEntryFound:
            continue

    raise ToolNotFoundError(f"I couldn't find a recipe called {spoken(slug.replace('-', ' '))}.")


class _FoodWithAliases(IngredientFood):
    @classmethod
    def loader_options(cls) -> list[LoaderOption]:
        return [*super().loader_options(), selectinload(IngredientFoodModel.aliases)]


class FoodMatcher(DataMatcher):
    """
    The ingredient parser's food matching (by name, plural or alias, then fuzzily), loading the group's foods
    and their aliases in a few queries, rather than one more for each food's aliases
    """

    @cached_property
    def foods_by_id(self) -> dict[UUID4, IngredientFood]:
        query = PaginationQuery(page=1, per_page=-1)
        return {food.id: food for food in self.repos.ingredient_foods.page_all(query, override=_FoodWithAliases).items}


def match_foods(matcher: DataMatcher, names: list[str]) -> tuple[dict[UUID4, str], list[str]]:
    """
    Free-text food names matched to the group's foods. Returns `{food id: food name}` and the names that
    matched nothing.
    """
    if not names:
        return {}, []

    matched: dict[UUID4, str] = {}
    unmatched: list[str] = []
    for name in names:
        if food := matcher.find_food_match(name):
            matched[food.id] = food.name
        else:
            unmatched.append(name)

    return matched, unmatched


def match_organizers(repo: RepositoryTags | RepositoryCategories, names: list[str]) -> tuple[list[UUID4], list[str]]:
    """Tag or category names matched to the group's by name or slug, then fuzzily; and the names that matched nothing"""
    if not names:
        return [], []

    by_key: dict[str, TagOut | CategoryOut] = {}
    for organizer in repo.page_all(PaginationQuery(page=1, per_page=-1)).items:
        by_key.setdefault(organizer.slug, organizer)
        by_key.setdefault(slugify(organizer.name), organizer)

    ids: list[UUID4] = []
    unmatched: list[str] = []
    for name in names:
        match = find_match(slugify(name), store_map=by_key, fuzzy_match_threshold=ORGANIZER_FUZZY_MATCH_THRESHOLD)
        if match is None:
            unmatched.append(name)
        else:
            ids.append(match.id)

    return ids, unmatched


def unmatched_sentence(unmatched: list[str]) -> str:
    if not unmatched:
        return ""
    names = [spoken(name, 40) for name in unmatched[:3]]
    if len(unmatched) > 3:
        names.append(f"{len(unmatched) - 3} more")
    return f"I couldn't match {join_words(names)}, so I left {'it' if len(unmatched) == 1 else 'them'} out."


def _sentences(*parts: str) -> str:
    return " ".join(p for p in parts if p)


# ==================================================================================================================
# search_recipes


class SearchRecipesArgs(ToolArgs):
    query: str | None = Field(
        default=None,
        max_length=200,
        description="Words to look for in recipe names, descriptions and ingredient text. Leave out to browse.",
    )
    max_total_minutes: int | None = Field(
        default=None,
        ge=1,
        le=MAX_MINUTES,
        description="Only recipes whose total time is known and at most this many minutes",
    )
    include_foods: list[Name] = Field(
        default_factory=list,
        max_length=10,
        description="Ingredients every result must use, e.g. ['chicken', 'rice'], matched to the group's foods",
    )
    exclude_foods: list[Name] = Field(
        default_factory=list,
        max_length=10,
        description="Ingredients no result may use, matched to the group's foods",
    )
    tags: list[Name] = Field(default_factory=list, max_length=10, description="Tag names every result must have")
    categories: list[Name] = Field(
        default_factory=list, max_length=10, description="Category names every result must be in"
    )
    limit: int = Field(default=5, ge=1, le=20, description="How many recipes to return")


class RecipeHit(BaseModel):
    slug: str
    name: str
    description: str
    total_time: str | None
    total_minutes: int | None
    rating: float | None


class SearchRecipesResult(ToolResult):
    recipes: list[RecipeHit]
    total: int | None = Field(
        description=(
            "How many recipes match, when known: not with max_total_minutes, which checks the times of at most "
            f"{SEARCH_MAX_PAGES * SEARCH_PAGE_SIZE} recipes"
        )
    )
    unmatched: list[str] = Field(description="Foods, tags or categories that matched nothing and were ignored")


def _recipe_hit(recipe: RecipeSummary, minutes: int | None) -> RecipeHit:
    return RecipeHit(
        slug=recipe.slug,
        name=recipe.name or "",
        description=first_sentences(recipe.description or "", 200),
        total_time=recipe.total_time,
        total_minutes=minutes,
        rating=recipe.rating,
    )


def _search_recipes(ctx: ToolContext, args: SearchRecipesArgs) -> SearchRecipesResult:
    foods = FoodMatcher(ctx.repos)
    include, unmatched_include = match_foods(foods, args.include_foods)
    exclude, unmatched_exclude = match_foods(foods, args.exclude_foods)
    tag_ids, unmatched_tags = match_organizers(ctx.repos.tags, args.tags)
    category_ids, unmatched_categories = match_organizers(ctx.repos.categories, args.categories)
    unmatched = unmatched_include + unmatched_exclude + unmatched_tags + unmatched_categories

    exclude_filter = None
    if exclude:
        excluded_ids = ", ".join(f'"{food_id}"' for food_id in exclude)
        exclude_filter = f"recipe_ingredient.food.id NOT IN [{excluded_ids}]"

    # times are free text, so they're compared after the query; the query can still skip recipes without one
    time_filter = HAS_A_TIME if args.max_total_minutes else None
    query_filter = QueryFilterBuilder.combine_filters(exclude_filter, time_filter) or None

    # like upstream's recipe search: every household's recipes in the group, with the caller's ratings
    recipes = ctx.group_repos.recipes.by_user(ctx.user.id)
    page_size = SEARCH_PAGE_SIZE if args.max_total_minutes else args.limit

    hits: list[RecipeHit] = []
    total: int | None = None
    more_unchecked = False
    for page in range(1, SEARCH_MAX_PAGES + 1):
        results = recipes.page_all(
            PaginationQuery(page=page, per_page=page_size, query_filter=query_filter),
            tags=list(tag_ids) or None,
            categories=list(category_ids) or None,
            foods=list(include) or None,
            search=args.query or None,
        )
        for recipe in results.items:
            minutes = recipe_minutes(recipe)
            if args.max_total_minutes and (minutes is None or minutes > args.max_total_minutes):
                continue
            hits.append(_recipe_hit(recipe, minutes))

        if not args.max_total_minutes:
            total = results.total
            break
        if len(hits) >= args.limit or page >= results.total_pages:
            break
    else:
        more_unchecked = True

    hits = hits[: args.limit]
    names = [spoken(hit.name, 50) for hit in hits[:3]]
    if not hits:
        found = "I couldn't find any recipes matching that."
    elif len(hits) <= 3 and (total is None or total == len(hits)):
        found = f"I found {count_of(len(hits), 'recipe')}: {join_words(names)}."
    else:
        found = f"I found {count_of(total or len(hits), 'recipe')}, including {join_words(names)}."

    detail = unmatched_sentence(unmatched)
    if not detail and more_unchecked:
        detail = f"I only checked the times of {SEARCH_MAX_PAGES * SEARCH_PAGE_SIZE} recipes, so there may be more."

    return SearchRecipesResult(
        speech=_sentences(found, detail),
        recipes=hits,
        total=total,
        unmatched=unmatched,
    )


search_recipes = AITool(
    name="search_recipes",
    description=(
        "Search the recipe collection (every recipe in the user's group). Use it to find recipes by words in "
        "their name, description or ingredients, by maximum total time, by ingredients they must or must not use, "
        "or by tag and category. include_foods and exclude_foods only see ingredients linked to a food; for a "
        "looser match put the ingredient in query. Returns each recipe's slug (for get_recipe, get_cooking_step, "
        "plan_meal and add_to_shopping_list), name, short description, total time and rating."
    ),
    args=SearchRecipesArgs,
    result=SearchRecipesResult,
    writes=False,
    handler=run_blocking(_search_recipes),
)


# ==================================================================================================================
# get_recipe


class GetRecipeArgs(ToolArgs):
    slug: Slug
    part: Literal["summary", "ingredients", "steps", "all"] = Field(
        default="summary",
        description="What to return: 'summary' (name, total time, servings, notes), 'ingredients', 'steps' or 'all'",
    )
    servings: float | None = Field(
        default=None,
        gt=0,
        le=1000,
        description=(
            "Scale ingredient quantities to this many servings, from the recipe's own servings count (or, "
            "without one, its yield quantity, as the recipe page scales). Recipes with neither aren't scaled."
        ),
    )


class IngredientLine(BaseModel):
    section: str | None = Field(description="The heading of the section this ingredient starts, if any")
    text: str


class StepLine(BaseModel):
    number: int
    section: str | None = Field(description="The heading of the section this step starts, if any")
    text: str


class NoteLine(BaseModel):
    title: str
    text: str


class GetRecipeResult(ToolResult):
    slug: str
    name: str
    description: str
    total_time: str | None
    total_minutes: int | None
    servings: float | None = Field(description="Servings at the returned scale; None if the recipe doesn't say")
    recipe_yield: str | None = Field(
        description=(
            "What the recipe makes at the returned scale, e.g. '2 loaves'. None if it doesn't say, or if it's "
            "only free text with no quantity and the recipe was scaled."
        )
    )
    scale: float = Field(description="The factor ingredient quantities were multiplied by")
    ingredient_count: int
    step_count: int
    ingredients: list[IngredientLine] = Field(description="Only for part 'ingredients' or 'all'")
    steps: list[StepLine] = Field(description="Only for part 'steps' or 'all'")
    notes: list[NoteLine] = Field(description="Only for part 'summary' or 'all'")


def ingredient_text(ingredient: RecipeIngredient, scale: float = 1) -> str:
    """
    An ingredient as the recipe page shows it, at `scale`. Upstream's `display` leaves out a sub-recipe's name,
    so that's put where a food's name would go, as the recipe page puts it.
    """
    update: dict[str, Any] = {}
    if scale != 1 and ingredient.quantity:
        update["quantity"] = ingredient.quantity * scale
    if ingredient.referenced_recipe and not ingredient.food:
        update["food"] = CreateIngredientFood(name=ingredient.referenced_recipe.name or "")
    if not update:
        return ingredient.display

    # display is computed when the model is validated, which a copy isn't
    return ingredient.model_copy(update=update | {"display": ""}).format_display().display


def _is_unparsed(ingredient: RecipeIngredient) -> bool:
    """Free text whose amount, if any, is part of the note and can't be scaled"""
    return not (ingredient.quantity or ingredient.unit or ingredient.food or ingredient.referenced_recipe)


def scale_base(recipe: Recipe) -> float:
    """What a requested number of servings is divided by to scale a recipe, as the recipe page reckons it"""
    return recipe.recipe_servings or recipe.recipe_yield_quantity


def _spoken_time(recipe: Recipe) -> str:
    if (minutes := parse_minutes(recipe.total_time)) is not None:
        return spoken_minutes(minutes)
    return spoken(recipe.total_time, 40)


def _get_recipe(ctx: ToolContext, args: GetRecipeArgs) -> GetRecipeResult:
    recipe = find_recipe(ctx, args.slug)
    name = spoken_recipe_name(recipe)
    ingredients = recipe.recipe_ingredient
    steps = recipe.recipe_instructions or []

    scale = 1.0
    scale_note = ""
    if args.servings is not None:
        if scale_base(recipe) > 0:
            scale = args.servings / scale_base(recipe)
            if scale != 1 and any(_is_unparsed(i) and i.note for i in ingredients):
                scale_note = "Some ingredient amounts are plain text, so I couldn't scale those."
        else:
            scale_note = "The recipe doesn't say how many it serves, so I couldn't scale it."

    servings = recipe.recipe_servings * scale or None
    recipe_yield: str | None = None
    if recipe.recipe_yield_quantity:
        recipe_yield = f"{number(recipe.recipe_yield_quantity * scale)} {recipe.recipe_yield or ''}".strip()
    elif scale == 1:
        recipe_yield = recipe.recipe_yield or None

    details = []
    if time := _spoken_time(recipe):
        details.append(f"takes {time}")
    if servings:
        details.append(f"serves {number(servings)}")
    elif recipe_yield:
        details.append(f"makes {spoken(recipe_yield, 40)}")
    summary = f"{name} {join_words(details)}." if details else f"{name}."

    lines: list[IngredientLine] = []
    section: str | None = None
    for ingredient in ingredients:
        section = ingredient.title or section
        if text := ingredient_text(ingredient, scale):
            lines.append(IngredientLine(section=section, text=text))
            section = None

    step_lines = [StepLine(number=n, section=s.title or None, text=s.text) for n, s in enumerate(steps, start=1)]

    match args.part:
        case "ingredients":
            first = f", starting with {spoken(lines[0].text, 80)}" if lines else ""
            speech = _sentences(f"{name} has {count_of(len(lines), 'ingredient')}{first}.", scale_note)
        case "steps":
            first = f"Step 1: {end_sentence(first_sentences(step_lines[0].text, 160, 1))}" if step_lines else ""
            speech = _sentences(f"{name} has {count_of(len(step_lines), 'step')}.", first)
        case "all":
            speech = _sentences(
                summary,
                scale_note or f"It has {count_of(len(lines), 'ingredient')} and {count_of(len(step_lines), 'step')}.",
            )
        case _:
            speech = _sentences(summary, scale_note)

    return GetRecipeResult(
        speech=speech,
        slug=recipe.slug,
        name=recipe.name or "",
        description=plain_text(recipe.description),
        total_time=recipe.total_time,
        total_minutes=recipe_minutes(recipe),
        servings=servings,
        recipe_yield=recipe_yield,
        scale=scale,
        ingredient_count=len(lines),
        step_count=len(step_lines),
        ingredients=lines if args.part in ("ingredients", "all") else [],
        steps=step_lines if args.part in ("steps", "all") else [],
        notes=[NoteLine(title=n.title, text=n.text) for n in recipe.notes or []]
        if args.part in ("summary", "all")
        else [],
    )


get_recipe = AITool(
    name="get_recipe",
    description=(
        "Read a recipe by slug. part='summary' gives its name, total time and servings; 'ingredients' the "
        "ingredient list; 'steps' the numbered method; 'all' everything. Pass servings to scale the ingredient "
        "quantities. To read the method one step at a time while cooking, use get_cooking_step instead."
    ),
    args=GetRecipeArgs,
    result=GetRecipeResult,
    writes=False,
    handler=run_blocking(_get_recipe),
)


# ==================================================================================================================
# get_cooking_step


class GetCookingStepArgs(ToolArgs):
    slug: Slug
    step: int = Field(ge=1, le=500, description="The step number, starting at 1")


class GetCookingStepResult(ToolResult):
    # a step is read out whole, however many sentences it has, up to a length
    max_speech_sentences: ClassVar[int | None] = None
    max_speech_chars: ClassVar[int] = 400

    slug: str
    name: str
    step: int
    step_count: int
    section: str | None = Field(description="The heading of the section this step starts, if any")
    text: str = Field(description="The step's full text")
    ingredients: list[str] = Field(description="The ingredients the recipe links to this step")
    has_next: bool


def _get_cooking_step(ctx: ToolContext, args: GetCookingStepArgs) -> GetCookingStepResult:
    recipe = find_recipe(ctx, args.slug)
    name = spoken_recipe_name(recipe)
    steps = recipe.recipe_instructions or []
    if not steps:
        raise ToolNotFoundError(f"{name} doesn't have any steps.")
    if args.step > len(steps):
        raise ToolNotFoundError(f"{name} only has {count_of(len(steps), 'step')}.")

    step = steps[args.step - 1]
    by_reference = {i.reference_id: ingredient_text(i) for i in recipe.recipe_ingredient}
    ingredients = [
        by_reference[ref.reference_id] for ref in step.ingredient_references if ref.reference_id in by_reference
    ]

    has_next = args.step < len(steps)
    speech = f"Step {args.step} of {len(steps)}: {end_sentence(first_sentences(step.text, 300))}"
    if not has_next:
        speech += " That's the last step."

    return GetCookingStepResult(
        speech=speech,
        slug=recipe.slug,
        name=recipe.name or "",
        step=args.step,
        step_count=len(steps),
        section=step.title or None,
        text=step.text,
        ingredients=ingredients,
        has_next=has_next,
    )


get_cooking_step = AITool(
    name="get_cooking_step",
    description=(
        "Read one step of a recipe's method, for hands-free cooking: 'next step', 'repeat that', 'go back'. "
        "Steps are numbered from 1. Returns the step's text, the ingredients it uses, the number of steps and "
        "whether there is a next one."
    ),
    args=GetCookingStepArgs,
    result=GetCookingStepResult,
    writes=False,
    handler=run_blocking(_get_cooking_step),
)


# ==================================================================================================================
# suggest_from_ingredients


class SuggestFromIngredientsArgs(ToolArgs):
    foods: list[Name] = Field(
        min_length=1,
        max_length=20,
        description="Ingredients the user has, e.g. ['rice', 'eggs'], matched to the group's foods",
    )
    max_total_minutes: int | None = Field(
        default=None,
        ge=1,
        le=MAX_MINUTES,
        description="Only recipes whose total time is known and at most this many minutes",
    )
    limit: int = Field(default=5, ge=1, le=20, description="How many recipes to return")


class SuggestedRecipe(BaseModel):
    slug: str
    name: str
    total_time: str | None
    total_minutes: int | None
    missing_foods: list[str] = Field(description="Ingredients the recipe needs that the user doesn't have")
    substitutions: list[str] = Field(description="Ingredients the user can swap in, as 'substitute for original'")


class SuggestFromIngredientsResult(ToolResult):
    recipes: list[SuggestedRecipe]
    matched_foods: list[str] = Field(description="The group's foods the given ingredients were matched to")
    unmatched: list[str] = Field(description="Given ingredients that matched none of the group's foods")


def _suggest_from_ingredients(ctx: ToolContext, args: SuggestFromIngredientsArgs) -> SuggestFromIngredientsResult:
    matched, unmatched = match_foods(FoodMatcher(ctx.repos), args.foods)
    if not matched:
        unknown = join_words([spoken(name, 40) for name in unmatched], "or") if len(unmatched) <= 3 else "any of those"
        return SuggestFromIngredientsResult(
            speech=f"I don't know {unknown} as an ingredient, so I can't suggest anything.",
            recipes=[],
            matched_foods=[],
            unmatched=unmatched,
        )

    # upstream's recipe finder, as GET /api/recipes/suggestions runs it: the whole group, as this user
    recipes = ctx.group_repos.recipes.by_user(ctx.user.id)
    fetch = min(args.limit * 5, 100) if args.max_total_minutes else args.limit
    suggestions = recipes.find_suggested_recipes(RecipeSuggestionQuery(limit=fetch), food_ids=list(matched))

    results: list[SuggestedRecipe] = []
    for suggestion in suggestions:
        minutes = recipe_minutes(suggestion.recipe)
        if args.max_total_minutes and (minutes is None or minutes > args.max_total_minutes):
            continue

        results.append(
            SuggestedRecipe(
                slug=suggestion.recipe.slug,
                name=suggestion.recipe.name or "",
                total_time=suggestion.recipe.total_time,
                total_minutes=minutes,
                missing_foods=[food.name for food in suggestion.missing_foods],
                substitutions=[f"{s.substitute_food.name} for {s.food.name}" for s in suggestion.substituted_foods],
            )
        )
        if len(results) >= args.limit:
            break

    have = [spoken(name, 40) for name in list(matched.values())[:3]]
    if len(matched) > 3:
        have.append(f"{len(matched) - 3} more")
    if not results:
        found = f"I couldn't find anything to make with {join_words(have)}."
    else:
        found = (
            f"With {join_words(have)}, you could make {join_words([spoken(r.name, 50) for r in results[:3]], 'or')}."
        )

    detail = unmatched_sentence(unmatched)
    if not detail and results:
        top = results[0]
        top_name = spoken(top.name, 50)
        if not top.missing_foods:
            detail = f"You have everything for {top_name}."
        else:
            detail = f"{top_name} needs {count_of(len(top.missing_foods), 'more ingredient')}."

    return SuggestFromIngredientsResult(
        speech=_sentences(found, detail),
        recipes=results,
        matched_foods=list(matched.values()),
        unmatched=unmatched,
    )


suggest_from_ingredients = AITool(
    name="suggest_from_ingredients",
    description=(
        "Suggest recipes the user can make with ingredients they have, e.g. 'what can I make with leftover rice "
        "and eggs?'. Recipes missing the fewest ingredients come first, and foods the household has marked as on "
        "hand count as available. Returns each recipe's slug, name, total time and the ingredients still missing."
    ),
    args=SuggestFromIngredientsArgs,
    result=SuggestFromIngredientsResult,
    writes=False,
    handler=run_blocking(_suggest_from_ingredients),
)
