"""Recipe card shorthand written out before the NLP parser sees a line (docs/ai/PHASE2.md §5, F6)"""

import pytest

from mealie.services.ai.ingest.shorthand import (
    ABBREVIATIONS,
    SHORTHAND,
    UNITS,
    PreparedLine,
    extract_size_words,
    join_mixed_numbers,
    normalize_shorthand,
    prepare_line,
)


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
        # other abbreviations the parser doesn't know, in any case
        ("1 doz. eggs", "1 dozen eggs"),
        ("2 Doz eggs", "2 dozen eggs"),
        ("1 env. Dream Whip", "1 envelope Dream Whip"),
        ("1 Env unflavored gelatin", "1 envelope unflavored gelatin"),
        ("2 sq. chocolate", "2 square chocolate"),
        ("1 sq unsweetened chocolate", "1 square unsweetened chocolate"),
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
        # after the quantity
        ("1 heaping T. flour", "1 T. flour", "heaping"),
        ("1 level t. soda", "1 t. soda", "level"),
        ("1 scant c. sugar", "1 c. sugar", "scant"),
        ("1/2 Rounded tsp. baking powder", "1/2 tsp. baking powder", "Rounded"),
        ("- 2 heaping cups flour", "- 2 cups flour", "heaping"),  # any unit: "heaping cups" would be a new unit
        ("1 heaping", "1 heaping", None),  # nothing after it
        # an item's size, abbreviated: "med onion" would be fuzzy-matched to the group's "red onion"
        ("1 med onion", "1 onion", "med"),
        ("2 med. onions, chopped", "2 onions, chopped", "med."),
        ("1 md cabbage", "1 cabbage", "md"),
        ("1 LG onion", "1 onion", "LG"),
        ("1 lge onion", "1 onion", "lge"),
        ("1 sml onion", "1 onion", "sml"),
        ("1 sm. pkg. instant pudding", "1 pkg. instant pudding", "sm."),
        ("1 big onion", "1 onion", "big"),
        ("1 med", "1 med", None),
        ("1 medium onion", "1 medium onion", None),  # a word, which the parser reads as the note
        ("1 levelled t. salt", "1 levelled t. salt", None),
        ("2 large eggs", "2 large eggs", None),  # a size of the food, which the parser reads as the note
        ("1 bigger onion", "1 bigger onion", None),
        # anywhere else on the line: the parser would make "cup scant" a unit, or "scant sugar" a food
        ("scant 1 c. sugar", "1 c. sugar", "scant"),
        ("1 c. scant sugar", "1 c. sugar", "scant"),
        ("1 T. heaping flour", "1 T. flour", "heaping"),
        ("1 c. sugar scant", "1 c. sugar", "scant"),
        ("1 c. sugar (scant)", "1 c. sugar", "scant"),
        ("1 c. (scant) sugar", "1 c. sugar", "scant"),
        ("1 c. sugar, scant", "1 c. sugar", "scant"),
        ("1 c. sugar, scant, sifted", "1 c. sugar, sifted", "scant"),
        ("1 c. flour (scant, sifted)", "1 c. flour (sifted)", "scant"),
        ("1 heaping T. flour (level)", "1 T. flour", "heaping, level"),
        ("Salt, a scant pinch", "Salt, a pinch", "scant"),
        ("- 1 lg. onion, chopped", "- 1 onion, chopped", "lg."),
    ],
)
def test_size_words_are_taken_out_wherever_they_stand(line: str, plain: str, size: str | None):
    assert extract_size_words(line) == (plain, size)


def test_the_pattern_finds_shorthand_once_size_words_are_out():
    """`flags.shorthand_read` reads the card's line as `prepare_line` gets it ready"""
    assert prepare_line("1 heaping T. flour") == PreparedLine("1 tbsp flour", ("heaping",), ("T.", "tbsp"), None)
    assert prepare_line("1 sm. pkg. instant pudding") == PreparedLine(
        "1 package instant pudding", ("sm.",), ("pkg.", "package"), None
    )
    assert prepare_line("scant 1 c. sugar").text == "1 cup sugar"


@pytest.mark.parametrize(
    "line, text, notes",
    [
        # a package's size between the quantity and the unit leads the note, and the unit is read
        ("1 (8 oz.) pkg. cream cheese", "1 package cream cheese", ("(8 oz.)",)),
        ("1 (10 3/4 oz.) can soup", "1 can soup", ("(10 3/4 oz.)",)),
        ("1 (10-3/4 oz.) can soup", "1 can soup", ("(10 3/4 oz.)",)),
        ("2 (15 oz.) cans black beans", "2 cans black beans", ("(15 oz.)",)),
        # a can's number
        ("1 #2 can pineapple", "1 can pineapple", ("#2",)),
        ("1 #10 can tomatoes", "1 can tomatoes", ("#10",)),
        ("2 #303 cans corn", "2 cans corn", ("#303",)),
        # both kinds of note, in the order they're written
        ("1 med. (8 oz.) pkg. cream cheese", "1 package cream cheese", ("med.", "(8 oz.)")),
        # parentheses that aren't a size, or come after the unit, stay for the parser
        ("1 can (10 3/4 oz.) soup", "1 can (10 3/4 oz.) soup", ()),
        ("2 (large) eggs", "2 (large) eggs", ()),
        ("1 #2 pencil", "1 #2 pencil", ()),
    ],
)
def test_a_package_size_and_a_can_number_go_to_the_note(line: str, text: str, notes: tuple[str, ...]):
    prepared = prepare_line(line)
    assert (prepared.text, prepared.notes) == (text, notes)


@pytest.mark.parametrize(
    "line, unit",
    [("1 doz. eggs", "dozen"), ("1 dozen eggs", "dozen"), ("2 Dozen rolls", "dozen"), ("1 c. sugar", None)],
)
def test_a_dozen_is_kept_as_the_unit(line: str, unit: str | None):
    """The parser reads "1 dozen eggs" as 1 egg; the unit is set after parsing"""
    assert prepare_line(line).unit == unit


@pytest.mark.parametrize(
    "line, joined",
    [
        # printed recipes write mixed numbers with a dash; the parser would keep only the fraction
        ("2-1/4 c. flour", "2 1/4 c. flour"),
        ("1-1/2 tsp baking soda", "1 1/2 tsp baking soda"),
        ("1 - 1/2 c. milk", "1 1/2 c. milk"),
        ("1–1/2 c. oats", "1 1/2 c. oats"),
        ("1-½ c. sugar", "1 ½ c. sugar"),
        ("1 can (10-3/4 oz.) soup", "1 can (10 3/4 oz.) soup"),
        ("Bake 1-1/2 hours", "Bake 1 1/2 hours"),
        # ranges go up, so stay as they are
        ("1/2-3/4 c. sugar", "1/2-3/4 c. sugar"),
        ("2-3 lb. roast", "2-3 lb. roast"),
        ("1-1 1/2 c. sugar", "1-1 1/2 c. sugar"),
        ("3-5/4 c. water", "3-5/4 c. water"),
        ("1 1/2 c. flour", "1 1/2 c. flour"),
    ],
)
def test_a_mixed_number_written_with_a_dash_is_written_with_a_space(line: str, joined: str):
    assert join_mixed_numbers(line) == joined
    kept = join_mixed_numbers(line, keep_length=True)
    assert len(kept) == len(line) and kept.split() == joined.split()


def test_every_unit_the_pattern_matches_has_a_name():
    alternatives = SHORTHAND.pattern.split("(?P<unit>")[1].split("|(?i:")[0].split("|")
    assert set(alternatives) == set(UNITS)
    abbreviations = SHORTHAND.pattern.split("|(?i:")[1].split(")")[0].split("|")
    assert set(abbreviations) == set(ABBREVIATIONS)
