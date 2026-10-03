# Recipe card eval

Photos of real recipe cards, each with a hand-checked JSON file of what a correct extraction contains, live in
`tests/data/cards/`. `mealie/scripts/eval_recipe_cards.py` reads every card with each AI provider you name (and,
optionally, the OCR fallback), compares the draft recipe with the JSON, and reports accuracy, latency and cost. Use it
to pick models for card reading, to settle decision D3 in [`AI_INTEGRATION_PLAN.md`](../AI_INTEGRATION_PLAN.md)
(whether to keep the Tesseract OCR fallback), and to catch prompt regressions.

The script runs the same import workflow as **Create recipe → AI** (`/api/recipes/create/ai`), but never saves a
recipe, an image or an organizer.

## Adding a card

Each card is two files with the same name, in lowercase kebab-case:

```
tests/data/cards/
  grandmas-pancakes.jpg
  grandmas-pancakes.json
```

> [!IMPORTANT]
> Use lowercase kebab-case names (`grandmas-pancakes.jpg`, not `IMG_2503.JPG`), and keep nothing but card photos and
> their JSON in the folder. The code generator (`dev/code-generation`) renames every file under `tests/data/` to
> kebab-case, which would leave the JSON's `source` pointing at a file that no longer exists.

Add the photo as it comes off the phone: sideways, uncropped, with glare, whatever you would really upload. JPEG or
PNG work best. For a card with writing on both sides, add both photos and list them in `source`, front first; they
are read together as one card.

The JSON file:

```json
{
  "source": "grandmas-pancakes.jpg",
  "notes": "Back of the card has a coffee stain over the oven temperature.",
  "verified_by_owner": false,
  "expected": {
    "name": "Grandma's Pancakes",
    "description_contains": ["fluffy"],
    "ingredients": ["2 C. flour", "2 T. sugar", "1½ C. milk", "2 eggs"],
    "instructions": ["Mix dry ingredients.", "Add milk and eggs and stir until just combined."],
    "must_not_invent": ["cook time", "yield"]
  }
}
```

| Field | Meaning |
|---|---|
| `source` | Image file name, or a list of names for a multi-sided card. Relative to the JSON file. |
| `notes` | Anything a reviewer should know: what is hard about this card, what is deliberately blank. |
| `verified_by_owner` | `true` once the card's owner has checked the JSON against the physical card. Unverified cards are marked `(unverified)` in the results, so treat their scores as provisional. |
| `expected.name` | The recipe title as written. |
| `expected.description_contains` | Words the description should mention (whole words, case-insensitive). Optional. |
| `expected.ingredients` | Every ingredient line, **verbatim**, in card order: keep the card's abbreviations (`T.`, `t.`, `C.`) and their case, fractions and notes. Don't fix spelling. |
| `expected.instructions` | The method, verbatim. Split it into steps the way the card does; the score doesn't depend on how a model splits or merges steps. |
| `expected.must_not_invent` | Fields the card leaves blank, which a correct extraction must leave blank too. One of `cook time` (any of total, prep, cook or perform time), `time`, `prep time`, `total time`, `yield`, `servings`, `description`, `notes`, `nutrition`. Optional. |

Write the expected values from the card, not from a model's output. A blank on the card (for example
"Microwave for __ minutes") stays blank in the JSON, and goes into `must_not_invent` if it is a field.

### Privacy

Recipe cards often carry family names, handwriting, addresses and personal notes. Everything in `tests/data/cards/` is
committed to the repository, so:

- Check each photo before committing it, especially if the fork is public. Crop or redact anything you don't want
  published, or keep the card out of the repository entirely (see `--cards` below).
- Running the eval sends every photo to every provider you name. Use only local providers (such as Ollama) and
  `--ocr` for cards that must not leave your server.

## Running the eval

The script uses the AI providers configured for a group (group settings → **AI Providers**), and refers to them by the
name shown there.

| Flag | Meaning |
|---|---|
| `--group SLUG` | Group whose providers to use (slug or id). Required. |
| `--household SLUG` | Household to run as (slug or id). Optional; nothing is saved either way. |
| `--provider NAME` | Provider to evaluate, by name or id; repeat for several. Defaults to the group's image provider. |
| `--ocr` | Also evaluate the OCR fallback: Tesseract reads the card and a text provider turns the text into a recipe. |
| `--ocr-provider NAME` | Text provider for the OCR path. Defaults to the group's default provider. |
| `--cards DIR` | Folder of cards. Defaults to `tests/data/cards`. |
| `--card ID` | Only evaluate this card (file name without extension); repeatable. |
| `--repeat N` | Read each card N times per provider, to see how consistent a model is. Defaults to 1. |
| `--price NAME=IN,OUT` | Provider price in USD per million input and output tokens, to report cost; repeatable. |
| `--out FILE` | Where to write the full JSON results. Defaults to `recipe-card-eval.json`. |

Each run is pinned to the provider it names. The group's fallback routes and monthly token limits are ignored, so a failing provider is scored as a failure and never as another provider's answer. Eval runs write nothing to the group's AI usage log.

Each provider is scored on its own: if a provider fails to read a card, the run counts as an error, and the error
recorded is the provider's own (for example an authentication failure). It does not fall back to OCR the way a real
import would. Likewise the OCR rows are scored on what Tesseract reads, never on a vision provider.

### Locally

From the repository root, with the same environment `task py` uses (it reads the dev database in `dev/data`):

```bash
PRODUCTION=false uv run python -m mealie.scripts.eval_recipe_cards \
  --group home --provider Gemini --provider Ollama --ocr \
  --price Gemini=0.30,2.50 --price Ollama=0,0 --repeat 3
```

### In the Docker container

The image doesn't include `tests/`, so copy the cards into the data volume first. Keeping private cards there,
rather than in the repository, works too.

```bash
docker cp tests/data/cards mealie:/app/data/eval-cards
docker exec -it mealie python -m mealie.scripts.eval_recipe_cards \
  --group home --provider Gemini --provider Ollama --ocr \
  --cards /app/data/eval-cards --out /app/data/eval-cards/results.json
docker cp mealie:/app/data/eval-cards/results.json .
```

- `--ocr` needs Tesseract in the image (`--build-arg INSTALL_OCR=true`, see [`DEPLOY.md`](DEPLOY.md)) and
  `OCR_ENABLED` left on. Without them the script stops before reading any card; leave out `--ocr` instead.
- `docker exec` doesn't run the container's entry script, so settings passed as `*_FILE` variables (for example
  `POSTGRES_PASSWORD_FILE`) aren't loaded. Pass them with `docker exec -e` if the script can't reach the database.

## Reading the results

The script prints one row per provider (OCR rows are labelled `OCR+<text provider>`), then each card's mean score per
provider:

```
Provider    Model             Runs  Errors  Score  Recall  Precision  Misread  Instr  Invented  Latency    p50  Tokens  Cost/card
----------  ----------------  ----  ------  -----  ------  ---------  -------  -----  --------  -------  -----  ------  ---------
Gemini      gemini-2.5-flash     3       0   0.93    0.95       0.95      0.3   0.91         0     6.2s   6.0s   3,210    $0.0021
OCR+Ollama  qwen3:8b             3       1   0.55    0.71       0.80      1.5   0.64         1    14.8s  14.1s   2,950    $0.0000
```

| Column | Meaning |
|---|---|
| **Score** | Overall score from 0 to 1, averaged over every run. A failed run scores 0, so unreliable providers rank lower. |
| **Recall** | Share of the card's ingredient lines read correctly, with the right quantity and unit. Missing and misread lines lower it. |
| **Precision** | Share of the recipe's ingredient lines that correctly read a line on the card. Invented, split, merged and misread lines lower it. |
| **Misread** | Mean ingredient lines per run read as the right ingredient but with the wrong quantity or unit, such as `1 tsp` for the card's `1 T.`. They count against both Recall and Precision. |
| **Instr** | Share of the card's instruction text the recipe covers, weighted by length. Rewording lowers it. |
| **Invented** | Runs that filled in a field the card leaves blank (`must_not_invent`), such as a made-up cook time. Should be 0. |
| **Latency**, **p50** | Mean and median seconds per card for successful runs: reading the card and building the recipe. |
| **Tokens**, **Cost/card** | Mean tokens per run as the provider reports them, Claude included (per attempt, summed over any server-side fallback), and the mean cost of the runs that reported any, when every provider involved has a `--price`. |
| **Errors** | Runs that produced no recipe. Recall, Precision, Misread, Instr and latency only count successful runs. |

### How ingredient lines are compared

An ingredient line matches a card line when it is the same ingredient **and** has the same quantity and unit.

- **Quantity:** the number a line starts with. `1/4`, `¼` and `0.25` are the same, as are `1½`, `1 1/2` and
  `1-1/2`; `0.33` is close enough to `1/3`. A range such as `1-2` or `1 to 2` must match at both ends. A line with no
  quantity on the card (`Cinnamon to taste`) must have none in the recipe either.
- **Unit:** the word right after the quantity. Abbreviations and plurals are the same unit: `T.`, `Tbsp`, `tbs` and
  `tablespoons`; `t.`, `tsp` and `teaspoon`; `C.`, `c` and `cups`; and the usual `oz`, `lb`, `g`, `ml` and so on.
  Case matters for a single letter, as on a handwritten card: `T.` is a tablespoon and `t.` a teaspoon. A word that
  isn't a known unit (`1 banana`, `2 eggs`) is part of the ingredient, not a unit.
- **Ingredient:** the rest of the line is compared after normalizing it: lowercase, punctuation and extra whitespace
  removed. It matches when it's at least 75% similar to the card's line, which accepts `1 T. coconut oil` for
  `1 T. coconut oil (melted)` but not `1 egg yolk` for `1 egg`.

Each line can only match once. A line that is the right ingredient with the wrong amount is listed as misread rather
than matched.

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

The JSON file has everything behind the table. For each run, `scores.ingredients.missing`, `.extra` and `.misread`
list the lines that didn't match (each misread line says whether its quantity, its unit or both were wrong),
`scores.inventions` shows invented values, `recipe` is what the provider produced, and `progress` times each workflow
step. Look there first when a card scores lower than expected.

The eval doesn't score organizers (tags, categories, tools), translation, or how ingredients are parsed into foods and
units; it skips the organizer step entirely.
