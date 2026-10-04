"""Recipe card shorthand written out before the NLP parser sees a line (docs/ai/PHASE2.md §5, F6)"""

import time

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
    quantity_value,
    standard_abbreviation,
    unit_spellings,
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
        # longer and plural spellings of a spoon or a package, in any case: the parser would read "tbls. sugar" as the
        # food, with no unit and nothing to look at
        ("2 tbls. sugar", "2 tbsp sugar"),
        ("2 Tbls. sugar", "2 tbsp sugar"),
        ("1 TBL. butter", "1 tbsp butter"),
        ("1 tblsp. flour", "1 tbsp flour"),
        ("2 tbs. sugar", "2 tbsp sugar"),
        ("1 teasp. salt", "1 tsp salt"),
        ("2 pkgs. yeast", "2 package yeast"),
        ("2 pkts. yeast", "2 package yeast"),
        ("1 pkt. gelatin", "1 package gelatin"),
        ("2 Pkgs. yeast", "2 package yeast"),
        ("3 envs. gelatin", "3 envelope gelatin"),
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
        # "Big" capitalized before a capitalized word starts a name; the abbreviations start none
        ("1 c. Big Red soda", "1 c. Big Red soda", None),
        ("1 (12 oz.) can Big Red", "1 (12 oz.) can Big Red", None),
        ("1 Big onion", "1 onion", "Big"),
        ("1 Lg Onion", "1 Onion", "Lg"),
        ("2 Med. Potatoes", "2 Potatoes", "Med."),
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
        # a package's size without parentheses, before a container (the parser would read the food "pkg. cream cheese")
        ("1 8-oz. pkg. cream cheese", "1 package cream cheese", ("8-oz.",)),
        ("1 3-oz. pkg. Jello", "1 package Jello", ("3-oz.",)),
        ("2 8-oz. cans tomato sauce", "2 cans tomato sauce", ("8-oz.",)),
        ("1 8 oz. pkg. cream cheese", "1 package cream cheese", ("8 oz.",)),
        ("1 10 3/4 oz. can soup", "1 can soup", ("10 3/4 oz.",)),
        ("1 15.5-oz. can corn", "1 can corn", ("15.5-oz.",)),
        # or after a dash (the parser read "1 - 8" as a range of ounces, and "pkg. cream cheese" as the food)
        ("1 - 8 oz. pkg. cream cheese", "1 package cream cheese", ("8 oz.",)),
        ("1-8 oz. pkg. cream cheese", "1 package cream cheese", ("8 oz.",)),
        ("1 – 8 oz. pkg. cream cheese", "1 package cream cheese", ("8 oz.",)),
        ("2-15 oz. cans black beans", "2 cans black beans", ("15 oz.",)),
        ("2 - 10 3/4 oz. cans soup", "2 cans soup", ("10 3/4 oz.",)),
        ("1 - 3 oz. box Jello", "1 box Jello", ("3 oz.",)),
        # a range before a unit that isn't a container stays a range
        ("1-2 lb. ground beef", "1-2 lb. ground beef", ()),
        ("2 - 3 oz. cheese", "2 - 3 oz. cheese", ()),
        # one amount: a mixed number and its unit, or a unit and the food
        ("2 1/2 oz. pkg. yeast", "2 1/2 oz. pkg. yeast", ()),
        ("1 lb. ground beef", "1 lb. ground beef", ()),
        ("2 8 oz. steaks", "2 8 oz. steaks", ()),
        # a can's number with a fraction (the parser would make "#2 1/2" a food, in its own fraction code), or "No."
        ("1 #2 1/2 can peaches", "1 can peaches", ("#2 1/2",)),
        ("1 #2½ can peaches", "1 can peaches", ("#2½",)),
        ("1 #2-1/2 can peaches", "1 can peaches", ("#2 1/2",)),
        ("2 #2 1/2 cans tomatoes", "2 cans tomatoes", ("#2 1/2",)),
        ("1 No. 2 can corn", "1 can corn", ("No. 2",)),
        ("1 (#2 1/2) can peaches", "1 can peaches", ("(#2 1/2)",)),
        ("2 noodles", "2 noodles", ()),
    ],
)
def test_a_package_size_and_a_can_number_go_to_the_note(line: str, text: str, notes: tuple[str, ...]):
    prepared = prepare_line(line)
    assert (prepared.text, prepared.notes) == (text, notes)


@pytest.mark.parametrize(
    "line, text, notes",
    [
        # before a container the parser reads "1 large" as a second amount and drops it
        ("1 large can pineapple", "1 can pineapple", ("large",)),
        ("1 small can tomato sauce", "1 can tomato sauce", ("small",)),
        ("2 large cans tomatoes", "2 cans tomatoes", ("large",)),
        ("1 tall can evaporated milk", "1 can evaporated milk", ("tall",)),
        ("1 Medium can peas", "1 can peas", ("Medium",)),
        ("1 large jar salsa", "1 jar salsa", ("large",)),
        # and it hid the shorthand after it
        ("1 large pkg. Jello", "1 package Jello", ("large",)),
        ("1 small (3 oz.) pkg. Jello", "1 package Jello", ("small", "(3 oz.)")),
        # before the food it's the same note the parser makes of it
        ("3 large eggs", "3 eggs", ("large",)),
        ("1 extra large egg", "1 egg", ("extra large",)),
        # anywhere else it may be the food's, and a size alone, or a choice of sizes, stays
        ("2 c. small curd cottage cheese", "2 cup small curd cottage cheese", ()),
        ("1 small", "1 small", ()),
        ("2 large (or 3 small) eggs", "2 large (or 3 small) eggs", ()),
    ],
)
def test_a_size_written_in_full_after_the_quantity_goes_to_the_note(line: str, text: str, notes: tuple[str, ...]):
    prepared = prepare_line(line)
    assert (prepared.text, prepared.notes) == (text, notes)


@pytest.mark.parametrize(
    "line, text",
    [
        # a second amount's shorthand: the parser would read "T. flour" as the food
        ("1 c. plus 2 T. flour", "1 cup plus 2 tbsp flour"),
        ("1 c. + 2 T. sugar", "1 cup + 2 tbsp sugar"),
        ("1/2 c. plus 1 T. milk", "1/2 cup plus 1 tbsp milk"),
        ("1 c. & 2 T. butter", "1 cup & 2 tbsp butter"),
        ("2 c. flour (or 1 1/2 c. bread flour)", "2 cup flour (or 1 1/2 cup bread flour)"),
        # at the end of the line it's no food's, and the note keeps it as written
        ("1 c. sugar + 2 T.", "1 cup sugar + 2 T."),
        # only after a joined amount, and case still matters
        ("1 c. sugar, 1 c. flour", "1 cup sugar, 1 c. flour"),
        ("1 c. or 2 t-bone steaks", "1 cup or 2 t-bone steaks"),
        ("1 c. milk and Tabasco", "1 cup milk and Tabasco"),
    ],
)
def test_shorthand_after_a_joined_amount_is_written_out(line: str, text: str):
    assert prepare_line(line).text == text


@pytest.mark.parametrize(
    "line, unit",
    [("1 doz. eggs", "dozen"), ("1 dozen eggs", "dozen"), ("2 Dozen rolls", "dozen"), ("1 c. sugar", None)],
)
def test_a_dozen_is_kept_as_the_unit(line: str, unit: str | None):
    """The parser reads "1 dozen eggs" as 1 egg; the unit is set after parsing"""
    assert prepare_line(line).unit == unit


@pytest.mark.parametrize(
    "line, quantity",
    [
        ("1/2 doz. eggs", 0.5),
        ("1 1/2 doz. cookies", 1.5),
        ("½ doz. eggs", 0.5),
        ("1½ dozen eggs", 1.5),
        ("2 1/2 dozen rolls", 2.5),
        ("1.5 dozen", 1.5),
        ("1-2 dozen eggs", 1),  # a range's start; the note keeps its end
        ("3 dozen", 3),
        ("1 c. sugar", None),
    ],
)
def test_a_dozen_keeps_the_lines_own_quantity(line: str, quantity: float | None):
    """The parser folds the dozen into a fraction's amount ("#1$2 dozen") and reads "1/2 dozen eggs" as 1"""
    assert prepare_line(line).quantity == quantity


def test_quantities_read_as_numbers():
    assert [quantity_value(text) for text in ("1/2", "1 1/2", "1½", "½", "2.5", "2,5", "3")] == [
        0.5,
        1.5,
        1.5,
        0.5,
        2.5,
        2.5,
        3,
    ]
    assert [quantity_value(text) for text in ("1/0", "x", "")] == [None, None, None]


def test_a_units_spellings():
    """A group's unit may have only a name, and a card writes its abbreviation: the same unit"""
    assert unit_spellings("Teaspoons") == unit_spellings("tsp.") == ("teaspoon", "tsp", "ts", "teasp")
    assert unit_spellings("lbs") == unit_spellings("pound") == ("pound", "lb")
    assert unit_spellings("Pack") == unit_spellings("pkg") == ("package", "pkg", "pack", "packet", "pk", "pkt")
    assert unit_spellings("fl. oz.") == ("fluid ounce", "fl oz")
    assert unit_spellings("Splash") == ("splash",)
    assert unit_spellings("cup(s)") == unit_spellings("Cups") == ("cup", "c")  # an optional plural
    names = ("teaspoon", "Tablespoons", "pounds", "fluid ounce", "package", "pack", "tsp", "splash")
    assert [standard_abbreviation(name) for name in names] == ["tsp", "tbsp", "lb", "fl oz", "pkg", "", "", ""]


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


def test_long_runs_of_digits_or_spaces_are_prepared_in_linear_time():
    """No pattern splits a long run of digits or spaces every way (a package size in parentheses took minutes)"""
    space = " " * 5000
    for line in (
        "1" * 5000,
        f"1 ({space}8",
        f"1 ({space}#2",
        f"1 8{space}-",
        f"1{space}-{space}8{space}oz.",  # a package size after a dash
        "1" * 5000 + " - 8",
        f"1 c. +{space}2",
        f"-{space}1 T.",
        f"1 lg.{space}egg",  # the spaces left where a size word was taken out
        f"1 ({space}lg.{space}x",  # a size word in parentheses that don't close
        f"1 c. sugar,{space}scant{space},{space})",
    ):
        started = time.perf_counter()
        prepare_line(line)
        assert time.perf_counter() - started < 0.2, line[:8]


@pytest.mark.parametrize("text", ["9" * 400, "1 " + "9" * 400 + "/2", "9" * 400 + "/7", "9" * 400 + ".5"])
def test_a_quantity_too_large_for_a_float_is_none(text: str):
    assert quantity_value(text) is None
    assert prepare_line(f"{text} doz. eggs").quantity is None  # the dozen's quantity is read with it
