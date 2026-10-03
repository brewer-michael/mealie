"""Meal plan tools: what's planned, and planning a meal"""

from datetime import date
from typing import Annotated, Self

from pydantic import BaseModel, Field, model_validator

from mealie.schema.meal_plan.new_meal import PlanEntryType, ReadPlanEntry, SavePlanEntry
from mealie.schema.response.pagination import PaginationQuery
from mealie.services.event_bus_service.event_types import EventMealplanData, EventOperation, EventTypes

from .base import AITool, ToolArgs, ToolContext, ToolResult, local_today, run_blocking
from .recipes import find_recipe
from .speech import count_of, join_words, spoken, spoken_date, spoken_date_phrase

MAX_PLAN_RANGE_DAYS = 31
MEAL_ORDER = {meal: i for i, meal in enumerate(PlanEntryType)}


class PlannedMeal(BaseModel):
    id: int
    date: date
    meal: PlanEntryType
    title: str = Field(description="The recipe's name, or the note's title")
    recipe_slug: str | None = Field(description="Set when the entry is a recipe")
    text: str = Field(description="The entry's extra note text, if any")


def _planned_meal(entry: ReadPlanEntry) -> PlannedMeal:
    return PlannedMeal(
        id=entry.id,
        date=entry.date,
        meal=entry.entry_type,
        title=(entry.recipe.name if entry.recipe else None) or entry.title,
        recipe_slug=entry.recipe.slug if entry.recipe else None,
        text=entry.text,
    )


# ==================================================================================================================
# whats_planned


class WhatsPlannedArgs(ToolArgs):
    start: date | None = Field(default=None, description="First day, as YYYY-MM-DD. Defaults to today.")
    end: date | None = Field(
        default=None,
        description=(
            f"Last day, as YYYY-MM-DD, so that the range covers at most {MAX_PLAN_RANGE_DAYS} days "
            f"(end at most {MAX_PLAN_RANGE_DAYS - 1} days after start). Defaults to start."
        ),
    )
    meal: PlanEntryType | None = Field(default=None, description="Only this meal. Leave out for every meal.")

    @model_validator(mode="after")
    def _check_range(self) -> Self:
        start = self.start or local_today()
        if self.end is not None:
            if self.end < start:
                raise ValueError("end must be on or after start (which defaults to today)")
            if (self.end - start).days >= MAX_PLAN_RANGE_DAYS:
                raise ValueError(f"the range can cover at most {MAX_PLAN_RANGE_DAYS} days")
        return self


class WhatsPlannedResult(ToolResult):
    start: date
    end: date
    entries: list[PlannedMeal] = Field(description="By date, then meal")


def _describe(entries: list[PlannedMeal], with_meal: bool) -> list[str]:
    titles = [spoken(e.title or e.text, 40) or "an entry" for e in entries]
    return [f"{title} for {e.meal.value}" if with_meal else title for title, e in zip(titles, entries, strict=True)]


def _whats_planned(ctx: ToolContext, args: WhatsPlannedArgs) -> WhatsPlannedResult:
    today = ctx.today()
    start = args.start or today
    end = args.end or start

    # the same date filter upstream's GET /households/mealplans applies
    query = PaginationQuery(page=1, per_page=-1, query_filter=f"date >= {start} AND date <= {end}")
    entries = [
        _planned_meal(entry)
        for entry in ctx.repos.meals.page_all(query).items
        if args.meal is None or entry.entry_type == args.meal
    ]
    entries.sort(key=lambda e: (e.date, MEAL_ORDER[e.meal], e.id))

    when = spoken_date_phrase(start, today)
    if start != end:
        when = f"from {spoken_date(start, today)} to {spoken_date(end, today)}"

    with_meal = args.meal is None
    if not entries:
        speech = f"Nothing is planned{f' for {args.meal.value}' if args.meal else ''} {when}."
    elif start == end:
        described = _describe(entries[:4], with_meal)
        listed = f"{', '.join(described)} and {len(entries) - 4} more" if len(entries) > 4 else join_words(described)
        intro = f"For {args.meal.value} {when}" if args.meal else when[0].upper() + when[1:]
        speech = f"{intro}, you have {listed}."
    else:
        noun = args.meal.value if args.meal else "meal"
        plural = f"{noun}es" if noun.endswith("ch") else f"{noun}s"
        first = entries[0]
        speech = (
            f"You have {count_of(len(entries), noun, plural)} planned {when}. "
            f"First is {_describe([first], with_meal)[0]} {spoken_date_phrase(first.date, today)}."
        )

    return WhatsPlannedResult(speech=speech, start=start, end=end, entries=entries)


whats_planned = AITool(
    name="whats_planned",
    description=(
        "Read the household's meal plan for a day or a range of days, for questions like 'what's for dinner?' "
        "or 'what are we eating this week?'. start defaults to today and end to start. Returns each entry's "
        "date, meal, title and, for recipes, the slug."
    ),
    args=WhatsPlannedArgs,
    result=WhatsPlannedResult,
    writes=False,
    handler=run_blocking(_whats_planned),
)


# ==================================================================================================================
# plan_meal


class PlanMealArgs(ToolArgs):
    # annotated rather than assigned, so the field named `date` doesn't shadow the type it's annotated with
    date: Annotated[date, Field(description="The day, as YYYY-MM-DD")]
    meal: PlanEntryType = Field(default=PlanEntryType.dinner, description="Which meal of the day")
    recipe_slug: str | None = Field(
        default=None,
        min_length=1,
        max_length=250,
        description="The recipe to plan, by its slug from search_recipes. Give this or note_title.",
    )
    note_title: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="A free-text entry instead of a recipe, e.g. 'Leftovers' or 'Eating out'",
    )

    @model_validator(mode="after")
    def _recipe_or_note(self) -> Self:
        if (self.recipe_slug is None) == (self.note_title is None):
            raise ValueError("give exactly one of recipe_slug or note_title")
        return self


class PlanMealResult(ToolResult):
    entry: PlannedMeal


def _plan_meal(ctx: ToolContext, args: PlanMealArgs) -> PlanMealResult:
    recipe = find_recipe(ctx, args.recipe_slug) if args.recipe_slug else None

    # as upstream's POST /households/mealplans: any member of the household may plan; the entry belongs to
    # the caller, and so to the caller's household
    created = ctx.repos.meals.create(
        SavePlanEntry(
            date=args.date,
            entry_type=args.meal,
            title=args.note_title or "",
            recipe_id=recipe.id if recipe else None,
            group_id=ctx.user.group_id,
            user_id=ctx.user.id,
        )
    )

    t = ctx.translator.t
    ctx.publish_event(
        EventTypes.mealplan_entry_created,
        EventMealplanData(
            operation=EventOperation.create,
            mealplan_id=created.id,
            recipe_id=recipe.id if recipe else None,
            recipe_name=recipe.name if recipe else None,
            recipe_slug=recipe.slug if recipe else None,
            date=created.date,
        ),
        message=t(
            "notifications.mealplan-entry-created",
            date=created.date,
            entry_type=t(f"mealplan.entry-type.{created.entry_type.value}", default=created.entry_type.value),
        ),
    )

    entry = _planned_meal(created)
    speech = f"I planned {spoken(entry.title)} for {entry.meal.value} {spoken_date_phrase(entry.date, ctx.today())}."
    return PlanMealResult(speech=speech, entry=entry)


plan_meal = AITool(
    name="plan_meal",
    description=(
        "Add an entry to the household's meal plan: a recipe (recipe_slug) or a free-text note (note_title, e.g. "
        "'Leftovers') on a date, for a meal. Existing entries are kept; this never replaces or deletes anything. "
        "Returns the created entry."
    ),
    args=PlanMealArgs,
    result=PlanMealResult,
    writes=True,
    handler=run_blocking(_plan_meal),
)
