"""
A card's ingredient lines, parsed and linked to the group's foods and units (docs/ai/PHASE2.md §5), in the task's
thread. Nothing is written: `IngestMatcher` only reads, and commit links the names again.

1. Lines are stripped and empty ones dropped (one empty string makes the NLP parser fail the whole call); section
   titles carry over. Lines that still hold a marker aren't parsed: they stay as text, and the marker is flagged.
2. Card shorthand ("1 T.", "1/4 t.", "1/3 C.") is written out first, case-sensitively, for English cards and cards of
   unknown language (`shorthand.normalize_shorthand`).
3. Only Mealie's NLP parser, with the matcher as its `data_matcher`. The brute parser links "pkg." to kilogram, and
   the AI parser builds its own `OpenAIService` (escaping the eval's pinning). Other languages aren't parsed.
4. `original_text` is the card's line again (the parser stores its own input there), and `display` is rebuilt, since
   it goes stale once the matcher swaps in the group's units and foods.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID, uuid4

from mealie.core.root_logger import get_logger
from mealie.lang.providers import Translator
from mealie.repos.repository_factory import AllRepositories
from mealie.schema.recipe.recipe import Recipe
from mealie.schema.recipe.recipe_ingredient import (
    IngredientFood,
    IngredientUnit,
    ParsedIngredient,
    RecipeIngredient,
    RegisteredParser,
)
from mealie.schema.recipe_ingest import CardDraftIngredient, CardDraftRef
from mealie.services.parser_services import get_parser

from ..matching import IngestMatcher
from ..shorthand import normalize_shorthand
from .cardtext import canonical_markers, markers_in
from .flags import ingredient_hash, is_english
from .service import end_transaction

logger = get_logger(__name__)


@dataclass
class IngredientLine:
    text: str
    """The line as read from the card"""
    title: str | None = None
    """The section title shown above it"""
    reference_id: UUID | None = None
    """Kept when the line replaces an existing one (a re-read); new lines get a fresh id"""


def recipe_lines(recipe: Recipe) -> list[IngredientLine]:
    """A built recipe's ingredient lines (the build step keeps each line's text in its note), empty ones dropped"""
    lines: list[IngredientLine] = []
    pending_title: str | None = None
    for ingredient in recipe.recipe_ingredient:
        title = (ingredient.title or "").strip() or None
        text = (ingredient.note or ingredient.display or "").strip()
        if not text:
            # a title on an empty line belongs to the next line
            pending_title = title or pending_title
            continue

        lines.append(IngredientLine(text=text, title=title or pending_title))
        pending_title = None
    return lines


def _ref(item: object) -> CardDraftRef | None:
    """A parsed unit or food as a draft reference: linked when the matcher found the group's, else by name"""
    name = (getattr(item, "name", None) or "").strip()
    if not name:
        return None
    item_id = item.id if isinstance(item, IngredientUnit | IngredientFood) else None
    return CardDraftRef(id=item_id, name=name)


def _as_text(line: IngredientLine, text: str) -> CardDraftIngredient:
    ingredient = CardDraftIngredient(
        reference_id=line.reference_id or uuid4(),
        title=line.title,
        original_text=text,
        note=text,
        display=text,
    )
    ingredient.extracted_hash = ingredient_hash(ingredient)
    return ingredient


def _from_parsed(line: IngredientLine, text: str, parsed: ParsedIngredient) -> CardDraftIngredient:
    result = parsed.ingredient
    quantity = result.quantity or None
    note = (result.note or "").strip()
    # rebuilt rather than kept: the parser's display was made before the matcher linked the unit and food
    display = RecipeIngredient(quantity=quantity, unit=result.unit, food=result.food, note=note).display
    ingredient = CardDraftIngredient(
        reference_id=line.reference_id or uuid4(),
        title=line.title,
        original_text=text,
        quantity=quantity,
        unit=_ref(result.unit),
        food=_ref(result.food),
        note=note,
        display=display or text,
        parse_confidence=parsed.confidence.average,
    )
    ingredient.extracted_hash = ingredient_hash(ingredient)
    return ingredient


async def normalize_lines(
    lines: Sequence[IngredientLine],
    *,
    repos: AllRepositories,
    translator: Translator,
    matcher: IngestMatcher,
    language: str | None,
) -> list[CardDraftIngredient]:
    """`lines` as draft ingredients: parsed and linked where they can be, else kept as text"""
    english = is_english(language)
    texts: list[str] = []
    to_parse: dict[int, str] = {}
    for index, line in enumerate(lines):
        text = canonical_markers(line.text.strip())
        texts.append(text)
        if text and english and not markers_in(text):
            to_parse[index] = normalize_shorthand(text)[0]

    parsed: dict[int, ParsedIngredient] = {}
    if to_parse:
        if not isinstance(repos.group_id, UUID):
            raise ValueError("Parsing a card's ingredients needs repositories scoped to its group")
        parser = get_parser(RegisteredParser.nlp, repos.group_id, repos.session, translator)
        parser.data_matcher = matcher
        try:
            results = await parser.parse(list(to_parse.values()))
            parsed = dict(zip(to_parse, results, strict=True))
        except Exception:
            # one line the parser can't take fails the whole call; parse the rest one at a time
            logger.warning("Parsing a card's ingredients together failed; parsing them one at a time")
            for index, text in to_parse.items():
                try:
                    parsed[index] = await parser.parse_one(text)
                except Exception:
                    logger.warning("An ingredient line on a card couldn't be parsed; it's kept as text")
        finally:
            end_transaction(repos.session)  # the matcher read the group's foods and units

    ingredients: list[CardDraftIngredient] = []
    for index, (line, text) in enumerate(zip(lines, texts, strict=True)):
        if not text:
            continue
        if result := parsed.get(index):
            ingredients.append(_from_parsed(line, text, result))
        else:
            ingredients.append(_as_text(line, text))
    return ingredients


async def normalize_ingredients(
    recipe: Recipe,
    *,
    repos: AllRepositories,
    translator: Translator,
    matcher: IngestMatcher,
    language: str | None,
) -> list[CardDraftIngredient]:
    """The built recipe's ingredient lines as draft ingredients, parsed and linked (§5)"""
    return await normalize_lines(
        recipe_lines(recipe), repos=repos, translator=translator, matcher=matcher, language=language
    )
