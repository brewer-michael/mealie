# Recipe card eval

`mealie/scripts/eval_recipe_cards.py` reads a set of recipe card photos with each AI provider you name (and,
optionally, the OCR fallback), compares each draft with a hand-checked JSON file of what the card says, and reports
accuracy, how well the flags point at mistakes, latency and cost. Use it to pick the models that read your cards, to
settle the decisions fixed in advance in [`PHASE2.md`](PHASE2.md) §11.4 (the cross-read default and D3, whether to
keep the Tesseract OCR fallback), and to catch prompt regressions.

Nothing is saved: no recipe, image, food, unit or organizer, and no row in the group's AI usage log.

## What it runs

By default (`--pipeline card`) every card goes through **exactly the production recipe card pipeline**, the code
behind the Recipe cards page:

1. `images.normalize_page`: the photo becomes an upright (by EXIF), metadata-free `page.jpg` and `view.jpg`, as at
   upload;
2. `pipeline.orient_page`: Tesseract turns a page that's sideways after EXIF transpose (a phone held flat over the
   table records the wrong orientation), with the 1.5× margin. Skipped without Tesseract, or with `--no-intake-ocr`;
3. `pipeline.extract_card`: the card read, the optional cross-read, the build and organizer steps, ingredient
   shorthand, parsing and linking to the group's foods and units (read-only), and the flags.

The group's own recipe card options are used (`options_for_group`: its cross-read setting, and organizer suggestions
only when it has tags, categories or tools). Two things differ from production, and only these:

- **Pinning.** Each config is pinned to the providers it names. The group's fallback routes and monthly limits are
  ignored, so a failing provider is scored as a failure and never as another provider's answer. Token use is
  tallied per provider and slot instead of being logged.
- **One reader per config (`read_path`).** Production reads the card with the image provider and falls back to OCR
  (`image_then_ocr`). Here a vision config runs with `read_path=image` and an OCR config with `read_path=ocr`, so a
  vision read that fails is scored as a failure, never as the OCR fallback. Each result records the
  `read_path` that produced it; `--chain` puts the two back together (see below).

Every run happens on temporary copies of the photos, under the same call policy the worker uses: a card whose fixture
says `local_only`, or every card with `--local-only`, can only reach providers that run on your network (§10). A
local-only card under a cloud config is refused before anything is sent, and reported as *refused* rather than as an
error.

`--pipeline import` runs the older `/recipes/create/ai` import workflow instead (no orientation, normalizer, linking
or flags), so numbers from before Phase 2 can be reproduced.

## Adding a card

Each card is a JSON file and its photos, with the same name in lowercase kebab-case:

```
tests/data/cards/
  grandmas-pancakes.jpg
  grandmas-pancakes.json
```

Use lowercase kebab-case names (`grandmas-pancakes.jpg`, not `IMG_2503.JPG`), as **Save as eval case** does. The
code generator (`dev/code-generation`) renames files under `tests/data/` to kebab-case, but leaves `tests/data/cards/`
alone, so a JSON's `source` keeps pointing at its photos.

Add the photo as it would really be uploaded: sideways, uncropped, with glare. For a card with writing on both sides,
add both photos and list them in `source`, front first; they are read together as one card. The easiest way to collect
cards is from the phone, which writes both files for you (see [Saving eval cases from the phone](#saving-eval-cases-from-the-phone)).
Clean a photo's metadata before committing it ([Cleaning a photo](#cleaning-a-photo)).

The JSON file (version 2):

```json
{
  "schema_version": 2,
  "source": "grandmas-pancakes.jpg",
  "notes": "Back of the card has a coffee stain over the oven temperature.",
  "verified_by_owner": false,
  "tags": ["handwritten", "faded", "blank"],
  "local_only": false,
  "expected": {
    "name": "Grandma's Pancakes",
    "attribution": "From Grandma Jo",
    "description_contains": ["fluffy"],
    "ingredients": [
      {"text": "2 C. flour", "quantity": 2, "unit": "cup", "food": "flour"},
      "2 T. sugar",
      "1½ C. milk",
      {"text": "2 eggs", "quantity": 2, "food": "egg"}
    ],
    "instructions": ["Mix dry ingredients.", "Bake on a hot griddle for [blank] minutes a side."],
    "recipe_yield": "12 pancakes",
    "times": {"prep_time": "10 min"},
    "blanks": [{"field": "steps", "text": "Bake on a hot griddle for [blank] minutes a side."}],
    "must_not_invent": ["cook time"]
  }
}
```

| Field | Meaning |
|---|---|
| `schema_version` | `2`. A file without it is version 1, which still loads: every version 1 key means what it did. |
| `source` | Image file name, or a list of names for a multi-sided card (front first). Relative to the JSON file. |
| `notes` | Anything a reviewer should know: what is hard about this card, what is deliberately blank. |
| `verified_by_owner` | `true` once the card's owner has checked the JSON against the physical card. Unverified cards are marked `(unverified)` in the results, so treat their scores as provisional. |
| `tags` | Any of `handwritten`, `printed`, `sideways`, `two-sided`, `faded`, `blank`, for the per-tag rows. `sideways` also tells the orientation check which cards should be turned. |
| `local_only` | `true` keeps the card away from any provider that doesn't run on your network, whatever the command line says. |
| `origin` | Written by **Save as eval case**: the job and its household, the provider and model that drafted it, when and from which Mealie commit. The report marks runs scored against a provider's own drafts. |
| `expected.name` | The recipe title as written. |
| `expected.attribution` | Who the recipe is from, as written. Optional. |
| `expected.description_contains` | Words the description should mention (whole words, case-insensitive). Optional. |
| `expected.ingredients` | Every ingredient line, **verbatim**, in card order: keep the card's abbreviations (`T.`, `t.`, `C.`) and their case, fractions and notes. A line is a string, or `{"text", "quantity", "unit", "food", "note"}` to also check how it's parsed and linked (`unit` and `food` are names, matched against the group's own foods and units by name, plural or alias). |
| `expected.instructions` | The method, verbatim. Split it into steps the way the card does; the score doesn't depend on how a model splits or merges steps. |
| `expected.recipe_yield`, `expected.times` | The yield and times **written on the card** (`prep_time`, `perform_time` for the cook time, `total_time`). Optional. |
| `expected.blanks` | Gaps the writer left on purpose, which a correct read keeps as `[blank]`: `{"field": "steps", "text": …}` (or `ingredients`, `notes`) with the item as on the card and `[blank]` where the gap is, or `{"field": "perform_time"}` for a single field. |
| `expected.must_not_invent` | Fields the card leaves blank, which a correct extraction leaves blank too: `cook time` (any of total, prep, cook or perform time), `time`, `prep time`, `total time`, `yield`, `servings`, `description`, `notes`, `nutrition`, `attribution`. Optional. |

Write the expected values from the card, not from a model's output. A blank on the card ("Microwave for ___
minutes") is `[blank]` in the JSON and listed in `blanks`; if it's a whole field, it goes in `must_not_invent` too.
Unknown keys are errors, so a typo can't silently drop a value.

Check the fixtures without running anything (no group, providers, settings or database needed):

```bash
uv run python -m mealie.scripts.eval_recipe_cards --check --cards tests/data/cards
```

A unit test also loads every committed fixture.

### Privacy

Recipe cards carry family names, handwriting, addresses and personal notes. Everything in `tests/data/cards/` is
committed to the repository, so only one to three redacted cards go there (the banana card is one). The 20-card eval
set stays private, in the group's data folder. Running the eval sends every photo to every provider you name: use
`--local-only` or `local_only` cards for anything that must not leave your server.

## Saving eval cases from the phone

On a card's review page, group managers have **⋯ → Save as eval case**: a name (lowercase letters, numbers and
dashes), a **verified** tick for when you've checked the draft against the physical card, what the card is
(**Handwritten**, **Printed**, **Faded**) and **Notes** (at most 2,000 characters). It writes
`DATA_DIR/groups/<group id>/eval-cards/<name>.json` and `<name>-1.jpg`, `<name>-2.jpg` …:

- the photos are the card's normalized pages **turned back by the rotation orientation applied**, so the eval
  exercises orientation again, and carry no EXIF, XMP or GPS;
- the expected values are the **reviewed** draft. A line the reviewer corrected is written from the corrected
  quantity, unit and food; otherwise the card's own wording is kept. A field that held `[blank]` when the card was
  read keeps it, even if the reviewer filled the gap in, and is listed in `blanks`;
- `tags` get the ones you ticked, plus `sideways`, `two-sided` and `blank` where they apply; `notes` gets your notes,
  `local_only` follows the card's, and `origin` records who drafted it and the card's household.

It works for cards that are ready or committed, until a committed card's files are purged. A name that exists is
refused, never overwritten. **Group Settings > Recipe cards** lists the cases: change **Verified**, the tags and the
notes in place, **Download** one as `<name>.zip` (its JSON and photos, to move into `tests/data/cards/` or run the
eval elsewhere), or **Delete** it (`/api/ai/ingest/eval-cases`). A manager sees the cases saved from their own
household's cards, plus any added by hand; an admin sees all. The folder is backed up with the group and never
purged.

## Running the eval

The script uses the AI providers configured for a group (group settings → **AI Providers**), and refers to them by the
name shown there.

| Flag | Meaning |
|---|---|
| `--group SLUG` | Group whose providers to use (slug or id). Required, except with `--check`. |
| `--household SLUG` | Household to run as (slug or id). Optional; nothing is saved either way. |
| `--provider VISION[:TEXT]` | A config to evaluate, by provider name or id; repeat for several. `qwen3-vl:Claude Sonnet` reads the card with `qwen3-vl` and runs every other step on `Claude Sonnet`; a single name does everything. Defaults to the group's image provider. |
| `--ocr` | Also evaluate the OCR path: Tesseract reads the card and a text provider structures the text. |
| `--ocr-provider NAME` | The text provider of an OCR config, labelled `OCR+NAME`; repeat for several (implies `--ocr`). Defaults to the group's default provider. |
| `--pipeline card\|import` | The recipe card pipeline (default), or the `/recipes/create/ai` workflow. |
| `--cards DIR` | Folder of cards. Defaults to `tests/data/cards`. |
| `--card ID` | Only evaluate this card (file name without extension); repeatable. |
| `--repeat N` | Read each card N times per config, to see how consistent a model is. Defaults to 1. |
| `--price NAME=IN,OUT` | Provider price in USD per million input and output tokens, to report cost; repeatable. |
| `--local-only` | Refuse every provider that isn't local (marked **Runs on my network** at a private address). |
| `--cross-read` | Read every card a second time and compare, whatever the group's setting (vision configs). |
| `--no-intake-ocr` | Skip orientation, to see what it's worth. The probe still runs on a copy, to report wrong turns. |
| `--baseline LABEL` | Compare every config (and chain) with this one, paired by card. |
| `--chain 'A>B'` | Also report "A, falling back to B when A can't read a card itself", from the same results; repeatable. |
| `--reference FILE` | An earlier run's JSON, for the cross-read rule (with `--cross-read`) and orientation (with `--no-intake-ocr`). |
| `--check` | Only validate the fixtures. Needs no settings or database. |
| `--dry-run` | Check everything the run needs (fixtures, group, providers and their slots, chains, prices) without calling a provider or writing anything. Lists every problem and exits 1, or exits 0. |
| `--out FILE` | Where to write the full JSON results. Defaults to `recipe-card-eval.json`. |

Configs are labelled by provider name: `Claude Sonnet`, `qwen3-vl:Claude Sonnet`, `OCR+qwen3-vl`. A chain or baseline
naming a label that isn't one of the run's configs stops the run before any provider is called, listing the labels
that exist. Each config has a **Local** column: every provider it uses runs on your network.

### Locally

From the repository root, with the same environment `task py` uses (it reads the dev database in `dev/data`):

```bash
PRODUCTION=false uv run python -m mealie.scripts.eval_recipe_cards \
  --group home --provider Gemini --provider Ollama --ocr \
  --price Gemini=0.30,2.50 --price Ollama=0,0 --repeat 3
```

Add `--dry-run` to the same command first: it finds a mistyped provider, a chain or baseline naming a config that
isn't there, or a provider without a `--price`, before anything is spent.

### In the Docker container

The image doesn't include `tests/`. Cards saved from the phone are already in the data volume; the repository's cards
can be copied there:

```bash
docker cp tests/data/cards/. mealie:/app/data/groups/<group id>/eval-cards/
docker exec -it mealie python -m mealie.scripts.eval_recipe_cards \
  --group home --provider Gemini --provider Ollama --ocr \
  --cards /app/data/groups/<group id>/eval-cards --out /app/data/recipe-card-eval.json
docker cp mealie:/app/data/recipe-card-eval.json .
```

- `--ocr` needs Tesseract in the image (`--build-arg INSTALL_OCR=true`, the fork image's default, see
  [`DEPLOY.md`](DEPLOY.md)) and `OCR_ENABLED` left on. Without them the script stops before reading any card; leave
  out `--ocr` instead. Orientation needs Tesseract and `AI_INGEST_ORIENT` left on, as in production; without them
  cards are read as intake leaves them.
- `docker exec` doesn't run the container's entry script, so the script reads the settings passed as `*_FILE` variables
  itself (for example `POSTGRES_PASSWORD_FILE`), the same list the entry script reads. As in the entry script, a set
  `*_FILE` wins over the plain variable; to override a secret, clear its file variable too:
  `docker exec -e POSTGRES_PASSWORD_FILE= -e POSTGRES_PASSWORD=…`.

## Reading the results

The script prints one row per config and chain, a second table of the card pipeline's own measures, a third ranking
the flags and their cost, each card's mean score per config, the per-tag rows, the paired comparisons, and the rules
of §11.4 as pass or fail.

```
Config         Model           Local  Runs  Errors  Score  Recall  Precision  Misread  Instr  Invented  Latency    p50  Tokens  Cost/card   Std
-------------  --------------  -----  ----  ------  -----  ------  ---------  -------  -----  --------  -------  -----  ------  ---------  ----
Claude Sonnet  claude-sonnet   no       60       0   0.93    0.95       0.95      0.3   0.91         0     9.2s   8.8s   6,210    $0.0181  0.02
OCR+qwen3-vl   qwen3-vl        yes      60       4   0.55    0.71       0.80      1.5   0.64         1    24.8s  24.1s   3,950    $0.0000  0.09
```

| Column | Meaning |
|---|---|
| **Score** | Overall score from 0 to 1, averaged over every run. A failed run scores 0, so unreliable providers rank lower. The weights are unchanged since version 1, so earlier numbers compare. |
| **Recall** | Share of the card's ingredient lines read correctly, with the right quantity and unit. Missing and misread lines lower it. |
| **Precision** | Share of the recipe's ingredient lines that correctly read a line on the card. Invented, split, merged and misread lines lower it. |
| **Misread** | Mean ingredient lines per run read as the right ingredient but with the wrong quantity or unit, such as `1 tsp` for the card's `1 T.`. They count against both Recall and Precision. |
| **Instr** | Share of the card's instruction text the recipe covers, weighted by length. Rewording lowers it; markers don't count. |
| **Invented** | Runs that filled in a field the card leaves blank (`must_not_invent`), such as a made-up cook time. Should be 0. |
| **Latency**, **p50** | Mean and median seconds per card for successful runs: orientation, reading the card and building the recipe. |
| **Tokens**, **Cost/card** | Mean tokens per run as the providers report them, and the mean cost of the runs that reported any, when every provider involved has a `--price`. The JSON splits tokens per provider and slot. |
| **Std** | Mean over cards of the score's standard deviation across `--repeat`. |
| **Errors** | Runs that produced no recipe. Recall, Precision, Misread, Instr and latency only count successful runs. Refused runs (local-only cards kept from cloud configs) are listed apart and not counted. |

The card pipeline's own measures:

| Column | Meaning |
|---|---|
| **Silent/card** | Items that are wrong and **not highlighted** (no unresolved error or warning on them), per card read. This is the number the review page relies on: what a reviewer can miss. |
| **FlagRecall** | P(flagged \| wrong): the share of wrong items the review page highlights. |
| **FlagPrec** | P(wrong \| flagged): the share of highlighted items that really are wrong. |
| **FlagRate** | The share of items highlighted. |
| **CleanPrec** | P(card fully correct \| clean): of the cards with nothing highlighted (one tap to commit), the share that were right. What **Add N clean cards** relies on. |
| **BlanksKept** | Expected blanks that came out as `[blank]`. |
| **BlanksSafe** | Expected blanks kept, or flagged with an **error** (which blocks commit), so the reviewer can't miss them. |
| **StepInv** | Numbers per run in the steps, times or yield that aren't on the card. An invented "2 minutes" in a blank still covers a step almost fully, so it's counted here. |
| **Attrib**, **Yield**, **Times** | The attribution (fuzzy), and whether the yield and times written on the card were read (a time counts in whichever time field it landed). |
| **Food**, **Unit** | On correctly read lines with a structured expectation: the share linked right. Right means linked to the group's food (by name, plural or alias) when the group has it, and left unlinked under the right name when it doesn't (commit creates it). |
| **WrongLink** | The share of those links made to an existing but wrong food or unit: worse than no link. |
| **NewFood** | The share of those lines whose food isn't linked: commit creates it, or keeps the line as text. |

A third table ranks the flags and their cost:

| Column | Meaning |
|---|---|
| **Cards**, **Items**, **Wrong** | Cards read, the items ranked, and how many of them are wrong |
| **AUROC** | How well the flags rank wrong items above right ones: the chance that a wrong item's highest flag (none, info, warning or error) is above a right item's, ties counting half. 0.5 is no better than chance, 1.0 perfect. |
| **Caught** | Wrong items that were highlighted |
| **Cost/caught** | The config's total cost divided by Caught: what each error the review page points at costs. Needs a `--price` for every provider. |

With fewer than 50 cards both AUROC and Cost/caught move a lot from one card to the next; the report says so. Read
them as rough until the set is larger.

**Items** for the flag columns are the name, each ingredient line, each step, and the times and yield the draft or
card has. Each is right or wrong per the scores above (a step with an invented number, or a lost blank, is wrong; an
expected line that's missing is a wrong item no flag can point at), and highlighted or not per
`HIGHLIGHTED_SEVERITIES` in `mealie/services/ai/ingest/flag_rules.py`, the definition the review page uses.
Card-level flags such as *read by OCR, check every line* aren't on an item, so they don't count as highlighting it.

### How ingredient lines are compared

An ingredient line matches a card line when it is the same ingredient **and** has the same quantity and unit.

- **Quantity:** the number a line starts with. `1/4`, `¼` and `0.25` are the same, as are `1½`, `1 1/2` and
  `1-1/2`; `0.33` is close enough to `1/3`. A range such as `1-2` or `1 to 2` must match at both ends. A line with no
  quantity on the card (`Cinnamon to taste`) must have none in the recipe either.
- **Unit:** the word right after the quantity. Abbreviations and plurals are the same unit: `T.`, `Tbsp`, `tbs` and
  `tablespoons`; `t.`, `tsp` and `teaspoon`; `C.`, `c` and `cups`; `pkg.` and `package`; and the usual `oz`, `lb`,
  `g`, `ml` and so on. Card shorthand comes from the pipeline's own table (`shorthand.UNITS`), and case matters for it
  as on a handwritten card: `T.` is a tablespoon and `t.` a teaspoon. A word that isn't a known unit (`1 banana`,
  `2 eggs`) is part of the ingredient, not a unit.
- **Ingredient:** the rest of the line is compared after normalizing it: lowercase, punctuation, markers and extra
  whitespace removed. It matches when it's at least 75% similar to the card's line, which accepts `1 T. coconut oil`
  for `1 T. coconut oil (melted)` but not `1 egg yolk` for `1 egg`.

Each line can only match once. A line that is the right ingredient with the wrong amount is listed as misread rather
than matched; the JSON gives every match the positions of both lines (`expected_index`, `actual_index`).

The overall score weights the components as follows; a component a card doesn't check, such as an empty
`description_contains`, is left out and the rest reweighted.

| Component | Weight |
|---|---|
| Ingredient recall | 0.30 |
| Instruction coverage | 0.25 |
| Ingredient precision | 0.20 |
| Name similarity | 0.10 |
| Nothing invented | 0.10 |
| Description words | 0.05 |

The JSON file has everything behind the tables: per run, the draft (`recipe`), its flags, every scored item and
blank, the linking details, `read_path`, the progress keys with their times, tokens per provider and slot, the models
that answered and the SHA-256 of every prompt used; per report, the Mealie commit, the settings and the decisions.
Look there first when a card scores lower than expected.

### Comparing configs

- `--baseline LABEL` pairs every other config with LABEL by card (each card's mean over its repeats): wins, ties
  (within 0.005), losses, the mean difference and a **seeded bootstrap 95% interval** (2,000 resamples of the cards),
  so the same results always print the same interval.
- `--chain 'A>B'` adds a row derived from results already in hand, with no extra calls: for each card and repeat, A's
  result if A read the card itself (no error, and by A's own `read_path`), else B's. Latencies, tokens and costs of the
  configs tried are added up, with orientation counted once. `qwen3-vl>OCR+qwen3-vl` is what production does with
  that provider: the image read, then the OCR fallback.

## Decisions fixed in advance

From [`PHASE2.md`](PHASE2.md) §11.4. The report prints each one as pass or fail; record the results and the decisions
below.

- **Targets** for the chosen default config on the 20 cards: silent errors at most **0.25** per card; flag recall at
  least **0.8**; on the banana card, `blanks_safe = 1.0` in **3 of 3** repeats (needs `--repeat 3`; a Phase 2 release
  check). A miss means tuning prompts or flags, not moving a threshold.
- **Cross-read default:** turned on if, with `--cross-read`, silent errors per card drop by at least **0.1**, or the
  banana blank is safe in 3 of 3 only with it. Run once without `--cross-read`, then again with it and
  `--reference <the first run's JSON>`.
- **D3, Tesseract's two roles:**
  1. *Last-resort reader:* keep the OCR fallback if `LocalVision>OCR+LocalText` rescues at least **two** cards, or its
     bootstrap interval against `LocalVision` excludes zero; otherwise drop the fallback. Pass the chain with
     `--chain`.
  2. *Orientation:* kept, since nothing else fixes flat-on-the-table shots. A run with `--no-intake-ocr` (and
     `--reference` to a normal run) reports what orientation is worth on `sideways` cards. Every run with Tesseract
     reports **wrong turns**: cards not tagged `sideways` that the probe turned. More than one in 20 raises
     `ORIENT_MIN_RATIO`.

## Runbook: 20 cards scored per provider

1. Review cards from the box on the phone and tap ⋯ → **Save as eval case** on each: at least 5 printed, 5 faded or
   pencil, some two-sided, some sideways. Tick **verified** after checking the draft against the card, and pick
   **Handwritten**, **Printed** or **Faded**.
2. Score every candidate, the mixed setup, the OCR configs and the D3 chain, three times each. Run the command once
   with `--dry-run` first:

   ```bash
   docker exec -it mealie python -m mealie.scripts.eval_recipe_cards --group home \
     --cards /app/data/groups/<group id>/eval-cards \
     --provider "Claude Sonnet" --provider "Gemini Flash" --provider qwen3-vl --provider "qwen3-vl:Claude Sonnet" \
     --ocr --ocr-provider qwen3-vl --ocr-provider "Claude Sonnet" \
     --chain "qwen3-vl>OCR+qwen3-vl" --baseline qwen3-vl --repeat 3 \
     --price "Claude Sonnet=3,15" --price "Gemini Flash=0.30,2.50" --price qwen3-vl=0,0 \
     --out /app/data/eval-base.json
   ```
3. Run it again with `--cross-read --reference /app/data/eval-base.json --out /app/data/eval-cross-read.json`, and
   with `--no-intake-ocr --reference /app/data/eval-base.json --out /app/data/eval-no-orient.json`.
4. Record the targets, the cross-read default and D3 in [Results](#results).

## Cleaning a photo

Phone photos carry GPS coordinates, the time and the camera in their metadata, and an iPhone saves an MPO: the photo
plus a second, smaller frame and an index to it. Clean a photo before committing it as a fixture. Photos saved from
the phone as eval cases are already clean.

**Without installing anything**, losslessly (the compressed image data is copied byte for byte):

```bash
uv run python -m mealie.scripts.strip_card_photo IMG_2503.jpg tests/data/cards/banana-mug-cake.jpg
```

It keeps the first frame only, keeps the colour profile (an `ICC_PROFILE` APP2 segment, such as Display P3), and drops
EXIF, XMP, the MPF index, Photoshop/IPTC data and comments. It writes a minimal EXIF holding only the orientation, so
the eval still exercises EXIF transpose (a phone held flat records the wrong orientation, which is what the
orientation probe is for); `--drop-orientation` leaves that out too, so the photo shows its raw pixels. Anything but a
JPEG is refused.

**With `jpegtran`**, which isn't installed by default on Ubuntu or WSL (`Command 'jpegtran' not found`):

```bash
sudo apt install libjpeg-turbo-progs
jpegtran -copy none -rotate 90 -perfect -outfile card.clean.jpg IMG_2503.jpg   # the sideways view the fixture exercises
jpegtran -copy none -perfect -outfile card.upright.jpg IMG_2503.jpg            # an upright copy (the raw pixels are upright)
```

`-copy none` drops every marker, the Display P3 profile included (a slight colour shift). `-copy icc` keeps the
profile but also the stale MPF index, so Pillow then reports a two-frame MPO; use `strip_card_photo` to keep the
profile.

**Re-encoding with Pillow**, which keeps the profile and needs only `uv` (add `.transpose(Image.Transpose.ROTATE_270)`
after `Image.open(...)` for the sideways view):

```bash
uv run --no-project --with pillow python -c "from PIL import Image; im = Image.open('IN.jpg'); im.save('OUT.jpg', quality=95, icc_profile=im.info.get('icc_profile'))"
```

## Results

Record each scoring run here: the date, the Mealie commit and the cards (both are in the JSON report), the
per-config rows, and the decisions with the numbers that made them.

| Date | Commit | Cards | Default config | Silent/card | Flag recall | Banana blank 3/3 | Cross-read default | D3 fallback | Wrong turns |
|---|---|---|---|---|---|---|---|---|---|

No run against real providers has been recorded yet. The repository's offline tests replay recorded provider answers
for the banana card through the whole pipeline (`tests/unit_tests/services_tests/ai/ingest/test_banana_replay.py`).
