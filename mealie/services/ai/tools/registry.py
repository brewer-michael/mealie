"""Every AI tool, by name"""

from typing import Any

from .base import AITool
from .mealplans import plan_meal, whats_planned
from .recipes import get_cooking_step, get_recipe, search_recipes, suggest_from_ingredients
from .shopping import add_to_shopping_list, get_shopping_list

_TOOLS: dict[str, AITool[Any, Any]] = {
    tool.name: tool
    for tool in [
        search_recipes,
        get_recipe,
        get_cooking_step,
        suggest_from_ingredients,
        whats_planned,
        get_shopping_list,
        add_to_shopping_list,
        plan_meal,
    ]
}


def all_tools() -> list[AITool[Any, Any]]:
    return list(_TOOLS.values())


def get_tool(name: str) -> AITool[Any, Any] | None:
    return _TOOLS.get(name)
