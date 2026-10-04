"""
A card's ingredient lines, parsed and linked to the group's foods and units (docs/ai/PHASE2.md §5), in the task's
thread. Nothing is written: `IngestMatcher` only reads, and commit links the names again.

1. Lines are stripped and empty ones dropped (one empty string makes the NLP parser fail the whole call); section
   titles carry over. Lines that still hold a marker aren't parsed: they stay as text, and the marker is flagged.
2. English cards (and cards of unknown language) get their lines ready for the parser first
   (`shorthand.prepare_line`): mixed numbers written with a dash joined, size words wherever they stand, a package
   size and a can number taken out (they lead the note), and card shorthand ("1 T.", "1/4 t.", "1 doz.") written out,
   case-sensitively. A written-out unit is linked to the group's unit by any of its spellings, never by a near miss.
3. English lines go to Mealie's NLP parser, with the matcher as its `data_matcher`. The brute parser links "pkg." to
   kilogram, so it isn't used. Lines in other languages go to upstream's AI ingredient parser, asking the card's own
   routed service (`CardIngredientParser`), so the job's local-only policy and the eval's pinned providers apply; if
   it fails they stay as text (`not_parsed`). `parse_lines` parses chosen lines with it in any language.
4. Nothing read is lost: an amount the parsed fields don't hold (a range's end, "(10 3/4 oz.)", "+ 2 T.") is kept in
   the note (`flags.keep_lost_amounts`), and `check_parse` still asks the reviewer to look.
5. `original_text` is the card's line again (the parser stores its own input there), and `display` is rebuilt, since
   it goes stale once the matcher swaps in the group's units and foods.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID, uuid4

from mealie.core.root_logger import get_logger
from mealie.lang.providers import Translator
from mealie.repos.repository_factory import AllRepositories
from mealie.schema.group.ai_providers import AIProviderSlot
from mealie.schema.openai.recipe_ingredient import OpenAIIngredients
from mealie.schema.recipe.recipe import Recipe
from mealie.schema.recipe.recipe_ingredient import (
    CreateIngredientUnit,
    IngredientFood,
    IngredientUnit,
    ParsedIngredient,
    RecipeIngredient,
    RegisteredParser,
)
from mealie.schema.recipe_ingest import CardDraftIngredient, CardDraftRef
from mealie.services.ai.errors import describe_provider_error
from mealie.services.openai import OpenAIService
from mealie.services.parser_services import get_parser
from mealie.services.parser_services.openai.parser import OpenAIParser

from ..matching import IngestMatcher
from ..shorthand import PreparedLine, prepare_line, unit_spellings
from .cardtext import canonical_markers, markers_in
from .flags import ingredient_hash, is_english, keep_lost_amounts
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


def _written_out_unit(
    name: str, parsed: IngredientUnit | CreateIngredientUnit | None, matcher: IngestMatcher
) -> IngredientUnit | CreateIngredientUnit:
    """
    The unit for a shorthand the line had written out (`name`: "square" for "sq.", "package" for "pkg."), as the
    group has it by any of its spellings (`shorthand.unit_spellings`: the group's "pack" for "package"), else a new one.
    The parser's own reading stands when it is one of those, never a unit its matcher took for a near miss ("square"
    read as the group's "quart").
    """
    spellings = unit_spellings(name)
    found = [unit for spelling in spellings if (unit := matcher.exact_unit(spelling)) is not None]
    if isinstance(parsed, IngredientUnit):
        if any(unit.id == parsed.id for unit in found):
            return parsed
    elif parsed is not None and unit_spellings(parsed.name) == spellings:
        if not found:
            return parsed  # not the group's, as the parser spelled it ("squares")
    return found[0] if found else CreateIngredientUnit(name=name)


def _from_parsed(
    line: IngredientLine, text: str, parsed: ParsedIngredient, prepared: PreparedLine, matcher: IngestMatcher
) -> CardDraftIngredient:
    result = parsed.ingredient
    quantity = result.quantity or None
    unit = result.unit
    if unit is None and prepared.unit:
        # "1 dozen eggs": the parser reads the dozen and drops it, and reads "1/2 dozen" as 1
        unit = _written_out_unit(prepared.unit, None, matcher)
        quantity = prepared.quantity or quantity
    elif unit is not None and prepared.shorthand:
        # "2 sq. chocolate" is read as "2 square chocolate": the group's square, else a new one, never its quart
        unit = _written_out_unit(prepared.shorthand[1], unit, matcher)
    # what was taken out before parsing leads the note
    note = ", ".join(part for part in (*prepared.notes, (result.note or "").strip()) if part)
    unit_ref, food_ref = _ref(unit), _ref(result.food)
    note, _ = keep_lost_amounts(
        text, quantity, unit_ref.name if unit_ref else None, food_ref.name if food_ref else None, note
    )
    # rebuilt rather than kept: the parser's display was made before the matcher linked the unit and food
    display = RecipeIngredient(quantity=quantity, unit=unit, food=result.food, note=note).display
    ingredient = CardDraftIngredient(
        reference_id=line.reference_id or uuid4(),
        title=line.title,
        original_text=text,
        quantity=quantity,
        unit=unit_ref,
        food=food_ref,
        note=note,
        display=display or text,
        parse_confidence=parsed.confidence.average,
    )
    ingredient.extracted_hash = ingredient_hash(ingredient)
    return ingredient


class CardIngredientParser(OpenAIParser):
    """
    Upstream's AI ingredient parser (its prompt, schema, confidence and matching), asking the card's own routed
    `service` on the fast slot instead of building its own: the job's call policy, its usage tallies and the eval's
    pinned providers apply, and no provider is ever named.
    """

    service: OpenAIService

    async def _parse(self, ingredients: list[str]) -> OpenAIIngredients:
        prompt = self._get_prompt(self.service)
        end_transaction(self.session)  # the prompt read the group's units; no transaction stays open meanwhile
        response = await self.service.get_response(
            prompt,
            json.dumps(ingredients, separators=(",", ":")),
            response_schema=OpenAIIngredients,
            slot=AIProviderSlot.fast,
        )
        if not response:
            raise ValueError("The AI ingredient parser answered nothing")
        return OpenAIIngredients(ingredients=response.ingredients)


def _group_id(repos: AllRepositories) -> UUID:
    if not isinstance(repos.group_id, UUID):
        raise ValueError("Parsing a card's ingredients needs repositories scoped to its group")
    return repos.group_id


async def _parse_nlp(
    to_parse: dict[int, str], *, repos: AllRepositories, translator: Translator, matcher: IngestMatcher
) -> dict[int, ParsedIngredient]:
    """Mealie's NLP parser on prepared English lines; a line it can't take is left out (kept as text)"""
    parser = get_parser(RegisteredParser.nlp, _group_id(repos), repos.session, translator)
    parser.data_matcher = matcher
    parsed: dict[int, ParsedIngredient] = {}
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
    return parsed


async def _parse_ai(
    to_parse: dict[int, str],
    *,
    ai: OpenAIService,
    repos: AllRepositories,
    translator: Translator,
    matcher: IngestMatcher,
) -> dict[int, ParsedIngredient]:
    """The AI ingredient parser on `to_parse`, in one request. Raises the provider's error."""
    parser = CardIngredientParser(_group_id(repos), repos.session, translator)
    parser.data_matcher = matcher
    parser.service = ai
    try:
        results = await parser.parse(list(to_parse.values()))
    finally:
        end_transaction(repos.session)  # the matcher read the group's foods and units
    return dict(zip(to_parse, results, strict=True))


def _prepared(text: str, english: bool) -> PreparedLine:
    """A line ready for a parser: an English card's prepared (`shorthand.prepare_line`), any other's as it is"""
    if english:
        return prepare_line(text)
    return PreparedLine(text=text, notes=(), shorthand=None, unit=None)


def _ingredients(
    lines: Sequence[IngredientLine],
    texts: Sequence[str],
    parsed: dict[int, ParsedIngredient],
    prepared: dict[int, PreparedLine],
    matcher: IngestMatcher,
) -> list[CardDraftIngredient]:
    ingredients: list[CardDraftIngredient] = []
    for index, (line, text) in enumerate(zip(lines, texts, strict=True)):
        if not text:
            continue
        if result := parsed.get(index):
            ingredients.append(_from_parsed(line, text, result, prepared[index], matcher))
        else:
            ingredients.append(_as_text(line, text))
    return ingredients


async def normalize_lines(
    lines: Sequence[IngredientLine],
    *,
    repos: AllRepositories,
    translator: Translator,
    matcher: IngestMatcher,
    language: str | None,
    ai: OpenAIService | None = None,
) -> list[CardDraftIngredient]:
    """
    `lines` as draft ingredients: parsed and linked where they can be, else kept as text. English lines (and lines of
    unknown language) go to the NLP parser; others to the AI parser through `ai`, the card's own service, and stay as
    text when there is no `ai` or it fails.
    """
    english = is_english(language)
    texts = [canonical_markers(line.text.strip()) for line in lines]
    prepared = {index: _prepared(text, english) for index, text in enumerate(texts) if text and not markers_in(text)}
    to_parse = {index: line.text for index, line in prepared.items()}

    parsed: dict[int, ParsedIngredient] = {}
    if to_parse and english:
        parsed = await _parse_nlp(to_parse, repos=repos, translator=translator, matcher=matcher)
    elif to_parse and ai is not None:
        try:
            parsed = await _parse_ai(to_parse, ai=ai, repos=repos, translator=translator, matcher=matcher)
        except Exception as e:
            # the lines stay as written (`not_parsed`); the description only, never the provider's response body
            logger.warning(f"The AI parser couldn't parse a card's ingredients ({describe_provider_error(e)})")
    return _ingredients(lines, texts, parsed, prepared, matcher)


async def parse_lines(
    lines: Sequence[IngredientLine],
    *,
    ai: OpenAIService,
    repos: AllRepositories,
    translator: Translator,
    matcher: IngestMatcher,
    language: str | None,
) -> list[CardDraftIngredient]:
    """
    Chosen lines parsed by the AI ingredient parser through `ai` (the job's own service), in any language: the review
    page's "Parse with AI". An English card's lines are prepared first, as for the NLP parser; lines holding a marker
    stay as text. Raises the provider's error (the task reports it), never returning the lines unparsed for a failure.
    """
    texts = [canonical_markers(line.text.strip()) for line in lines]
    english = is_english(language)
    prepared = {index: _prepared(text, english) for index, text in enumerate(texts) if text and not markers_in(text)}
    to_parse = {index: line.text for index, line in prepared.items()}
    parsed = await _parse_ai(to_parse, ai=ai, repos=repos, translator=translator, matcher=matcher) if to_parse else {}
    return _ingredients(lines, texts, parsed, prepared, matcher)


async def normalize_ingredients(
    recipe: Recipe,
    *,
    repos: AllRepositories,
    translator: Translator,
    matcher: IngestMatcher,
    language: str | None,
    ai: OpenAIService | None = None,
) -> list[CardDraftIngredient]:
    """The built recipe's ingredient lines as draft ingredients, parsed and linked (§5)"""
    return await normalize_lines(
        recipe_lines(recipe), repos=repos, translator=translator, matcher=matcher, language=language, ai=ai
    )
