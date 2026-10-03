from fastapi import APIRouter, HTTPException, status

from mealie.routes._base import BaseUserController, controller
from mealie.schema.recipe import ParsedIngredient
from mealie.schema.recipe.recipe_ingredient import IngredientRequest, IngredientsRequest
from mealie.schema.response import ErrorResponse
from mealie.services.ai.errors import AIProviderLimitReachedError
from mealie.services.parser_services import get_parser
from mealie.services.parser_services._base import ABCIngredientParser

router = APIRouter(prefix="/parser")


@controller(router)
class IngredientParserController(BaseUserController):
    @router.post("/ingredient", response_model=ParsedIngredient)
    async def parse_ingredient(self, ingredient: IngredientRequest):
        parser = get_parser(ingredient.parser, self.group_id, self.session, self.translator)
        response = await self._parse(parser, [ingredient.ingredient])
        return response[0]

    @router.post("/ingredients", response_model=list[ParsedIngredient])
    async def parse_ingredients(self, ingredients: IngredientsRequest):
        parser = get_parser(ingredients.parser, self.group_id, self.session, self.translator)
        return await self._parse(parser, ingredients.ingredients)

    async def _parse(self, parser: ABCIngredientParser, ingredients: list[str]) -> list[ParsedIngredient]:
        try:
            return await parser.parse(ingredients)
        except AIProviderLimitReachedError as e:
            # Fork: the group's own monthly token limits (docs/ai/PHASE1.md §4), not a server error
            message = self.t("recipe.import-errors.ai-limit-reached")
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail=ErrorResponse.respond(message)) from e
