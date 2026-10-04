"""A line kept with a marker, parsed around it (`review.KeptLine`): what its stored hash says about the parse"""

from mealie.schema.recipe_ingest import CardDraftIngredient, CardDraftRef
from mealie.services.ai.ingest import review
from mealie.services.ai.ingest.pipeline import flags as card_flags


def _parsed(note: str, *, split: bool) -> CardDraftIngredient:
    line = CardDraftIngredient(
        original_text="1/2 c. [illegible] or margarine",
        quantity=0.5,
        unit=CardDraftRef(name="cup"),
        food=CardDraftRef(name="butter"),
        note=note,
    )
    line.extracted_hash = card_flags.ingredient_hash(line, split=split)
    return line


def test_a_kept_line_whose_alternative_was_split_off_says_so():
    """The parser split "or margarine" off: the kept line keeps it in its note, and its hash still says so"""
    kept = review.KeptLine(
        text="1/2 c. [illegible] or margarine",
        parse_text="1/2 c. butter or margarine",
        markers=("[illegible]",),
        amount_marker=False,
    )
    line = kept.ingredient(_parsed("or margarine", split=True))
    assert line is not None
    assert card_flags.split_off(line)
    assert card_flags.is_unedited(line)


def test_a_kept_line_nothing_was_split_off_says_so_too():
    kept = review.KeptLine(
        text="1/2 c. [illegible]", parse_text="1/2 c. butter", markers=("[illegible]",), amount_marker=False
    )
    line = kept.ingredient(_parsed("", split=False))
    assert line is not None
    assert not card_flags.split_off(line)
    assert card_flags.is_unedited(line)
