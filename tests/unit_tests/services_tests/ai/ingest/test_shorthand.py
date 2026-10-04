"""Recipe card shorthand written out before the NLP parser sees a line (docs/ai/PHASE2.md §5, F6)"""

import pytest

from mealie.services.ai.ingest.shorthand import SHORTHAND, UNITS, normalize_shorthand, split_size


@pytest.mark.parametrize(
    "line, normalized",
    [
        # the banana mug cake card
        ("1 T. coconut oil (melted)", "1 tbsp coconut oil (melted)"),
        ("1/4 t. salt", "1/4 tsp salt"),
        ("1/2 t. vanilla", "1/2 tsp vanilla"),
        ("1/3 C. almond flour", "1/3 cup almond flour"),
        # tablespoons in every spelling, terabytes in none
        ("2 TB. butter", "2 tbsp butter"),
        ("2 TBS butter", "2 tbsp butter"),
        ("2 TBSP. butter", "2 tbsp butter"),
        ("2 Tb butter", "2 tbsp butter"),
        ("2 Tbs. butter", "2 tbsp butter"),
        ("2 Tbsp sugar", "2 tbsp sugar"),
        ("1 ts. baking soda", "1 tsp baking soda"),
        ("1 tsp. baking soda", "1 tsp baking soda"),
        ("2 c water", "2 cup water"),
        ("1 pkg. yeast", "1 package yeast"),
        ("1 Pkg dry yeast", "1 package dry yeast"),
        # quantities
        ("1 1/2 C. milk", "1 1/2 cup milk"),
        ("1½ T. honey", "1½ tbsp honey"),
        ("½ t. cinnamon", "½ tsp cinnamon"),
        ("2.5 C flour", "2.5 cup flour"),
        ("2,5 C flour", "2,5 cup flour"),
        ("1-2 T. sugar", "1-2 tbsp sugar"),
        ("1 to 2 T sugar", "1 to 2 tbsp sugar"),
        ("- 1 T. oil", "- 1 tbsp oil"),
        ("• 1/4 t. pepper", "• 1/4 tsp pepper"),
        ("  3 T. cocoa", "  3 tbsp cocoa"),
        ("2 T", "2 tbsp"),
    ],
)
def test_shorthand_after_the_quantity_is_written_out(line: str, normalized: str):
    assert normalize_shorthand(line) == (normalized, True)


@pytest.mark.parametrize(
    "line",
    [
        "1 tsp salt",  # already a unit name
        "1 cup sugar",
        "don't overmix",  # no quantity
        "1 t-bone steak",  # the token isn't the whole word
        "2 Tomatoes",
        "2 Cups flour",  # a word, not shorthand
        "Ttarragon",
        "Salt to taste",
        "[illegible] T. butter",
        "",
    ],
)
def test_everything_else_is_left_alone(line: str):
    assert normalize_shorthand(line) == (line, False)


def test_case_matters():
    assert normalize_shorthand("1 T sugar")[0] == "1 tbsp sugar"
    assert normalize_shorthand("1 t sugar")[0] == "1 tsp sugar"


def test_only_the_token_after_the_leading_quantity_changes():
    assert normalize_shorthand("1 T. butter, or 1 t. oil") == ("1 tbsp butter, or 1 t. oil", True)


@pytest.mark.parametrize(
    "line, plain, size",
    [
        ("1 heaping T. flour", "1 T. flour", "heaping"),
        ("1 level t. soda", "1 t. soda", "level"),
        ("1 scant c. sugar", "1 c. sugar", "scant"),
        ("1/2 Rounded tsp. baking powder", "1/2 tsp. baking powder", "Rounded"),
        ("- 2 heaping cups flour", "- 2 cups flour", "heaping"),  # any unit: "heaping cups" would be a new unit
        ("1 heaping", "1 heaping", None),  # nothing after it
        ("1 levelled t. salt", "1 levelled t. salt", None),
        ("2 large eggs", "2 large eggs", None),  # a size of the food, which the parser reads as the note
        ("Salt, a scant pinch", "Salt, a scant pinch", None),
    ],
)
def test_a_size_word_after_the_quantity_is_taken_out(line: str, plain: str, size: str | None):
    assert split_size(line) == (plain, size)


def test_the_pattern_finds_shorthand_after_a_size_word():
    """`flags.shorthand_read` reads the card's line, size word and all"""
    match = SHORTHAND.match("1 heaping T. flour")
    assert match is not None and (match.group("unit"), match.group("size")) == ("T", "heaping ")
    assert normalize_shorthand(split_size("1 heaping T. flour")[0]) == ("1 tbsp flour", True)


def test_every_unit_the_pattern_matches_has_a_name():
    alternatives = SHORTHAND.pattern.split("(?P<unit>")[1].split(")")[0].split("|")
    assert set(alternatives) == set(UNITS)
