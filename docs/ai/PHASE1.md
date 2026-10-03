# Phase 1: AI platform additions (design)

Implements the "Phase 1" row of [`AI_INTEGRATION_PLAN.md`](../AI_INTEGRATION_PLAN.md) §9, building on upstream's
per-group AI providers (`ai_providers`, `ai_provider_settings`, `OpenAIService`). Everything later in the plan (voice
tools, nutrition matching, the meal planner) runs on what this phase adds.

| # | Piece | What it gives you |
|---|---|---|
| 1 | Slots and fallback routes | Each task type has an ordered list of providers; if one fails, the next is tried |
| 2 | Native Claude provider | Claude through Anthropic's own API and SDK, not an OpenAI shim |
| 3 | Encrypted API keys | Keys are encrypted in the database and in backups |
| 4 | Usage log and monthly limits | Per-call token/latency records and an optional monthly token cap per provider |
| 5 | Tool registry | One definition of each kitchen action (search recipes, read a step, add to list…), callable over REST now and over MCP in Phase 3 |
| 6 | Settings UI | All of the above in Group Settings → AI providers |

## Decisions that differ from the plan

- **`protocol` and `monthly_token_limit` are columns on `ai_providers`**, not a side table. Fork-only migrations
  need a merge revision at every upstream sync either way, so a side table wouldn't avoid that. A column needs far
  less code and keeps `AIProviderOut` a plain `from_attributes` schema.
- **The Claude adapter uses the official `anthropic` SDK** (`anthropic==1.8.0`), in its own module.
- **The routing runtime lives in `mealie/services/ai/runtime.py`** (`AIRuntime`: candidate loop, usage recording,
  error mapping, transcription loop, model lists). Upstream's `openai.py` only gains small hooks into it, about 33
  changed lines. Its own `_get_provider` is left untouched so upstream merges stay easy.

## 1. Slots and fallback routes

```python
class AIProviderSlot(StrEnum):   # mealie/schema/group/ai_providers.py
    default = "default"     # text tasks (build recipe, scrape fallback)
    image = "image"         # anything with image attachments
    audio = "audio"         # transcription / audio attachments
    planner = "planner"     # tool-using agent work (Phase 5); falls back to default
    fast = "fast"           # cheap structured calls: ingredient parsing, organizers, translation; falls back to default
    embedding = "embedding" # embeddings (Phase 6); no fallback to default
```

**Table `ai_provider_routes`:** `id` (GUID PK), `settings_id` (FK `ai_provider_settings.id`, indexed), `slot`
(String), `position` (Integer), `provider_id` (FK `ai_providers.id`, indexed), `created_at`/`update_at`
(`BaseMixins`). Unique on (`settings_id`, `slot`, `position`) and on (`settings_id`, `slot`, `provider_id`).

**How a slot's candidates are resolved** (`mealie/services/ai/routing.py`):

| Slot | Candidates, in order |
|---|---|
| `default` / `image` / `audio` | upstream's primary for that slot (`*_provider_id`), then that slot's routes. **Routes are ignored while the slot has no primary**, because upstream's checks (`ai_enabled`, `image_provider_enabled`, `audio_provider_enabled`) only look at the primary. The settings UI disables these fallback pickers until a primary is chosen. |
| `planner` / `fast` | that slot's routes; if it has none, the `default` candidates |
| `embedding` | that slot's routes only |

Duplicates are removed, keeping the first. Providers at or over their monthly token limit are skipped. If every
candidate is over its limit, the call raises `AIProviderLimitReachedError` with a message saying so.

**Callers that move to `fast`** (one-line `slot=` change each):
- `parser_services/openai/parser.py` (ingredient parsing)
- `import_workflow/steps/translate_recipe.py`
- `import_workflow/steps/resolve_organizers.py`

All other callers keep inferring the slot from attachments, as upstream does.

**API** (group managers, `checks.can_manage()`):
- `GET /api/groups/ai-providers/routes` returns `AIProviderRoutesOut { routes: dict[AIProviderSlot, list[UUID4]] }`.
- `PUT /api/groups/ai-providers/routes` takes `AIProviderRoutesUpdate`, with the same shape. It replaces the routes
  for every slot it includes. It rejects provider ids from another group with a 400, and drops duplicates within a
  slot.

**Deleting a provider** also deletes its route rows and sets `provider_id = NULL` on its usage rows. This is done
explicitly in `GroupRepositoryAIProvider.delete`, next to upstream's settings clean-up, rather than relying on
SQLite foreign keys.

## 2. Native Claude provider

- **Schema:** `AIProviderCreate`/`Out` gain `protocol: AIProviderProtocol = "openai"` (`openai` | `anthropic`) and
  `monthly_token_limit: int | None`, validated to be from 1 to 2,147,483,647 (PostgreSQL `INTEGER`) or unset. Base
  URLs containing `?` or `#` are rejected (422), because query parameters belong in `request_params`, and a `?` would
  turn the path the SDK appends into a query value.
- **Adapter:** `mealie/services/ai/anthropic_adapter.py`. `OpenAIService.get_response` hands it the prompt, message,
  attachments, `response_schema` and provider. It returns `(parsed result | None, usage)`.
  - **Client:** `anthropic.AsyncAnthropic(api_key=…, base_url=…, timeout=provider.timeout,
    default_headers=provider.request_headers or None)`.
    - An empty base URL becomes `https://api.anthropic.com` explicitly. That way server environment variables
      (`ANTHROPIC_BASE_URL`) can never redirect a group's key, and the first-party check stays accurate.
    - The SDK always merges `ANTHROPIC_CUSTOM_HEADERS` from the environment, with no option to turn that off. The
      adapter replaces those headers with the provider's own. This relies on a private SDK attribute, so an
      end-to-end test with the variables set guards it.
    - A trailing `/v1` is stripped from the base URL, because the SDK adds `/v1/...` itself.
  - **Request:** `system=prompt`. The user content is image blocks first (base64 from `OpenAILocalImage`, URL source
    from `OpenAIImageExternal`), then the text block. `max_tokens=16000`. No `thinking` and no `temperature`: current
    models think adaptively by default and reject explicit sampling settings.
  - **Structured output** is sent as `output_config.format`, built by `output_schema()`:
    - First, `anthropic.transform_schema(response_schema)`.
    - Then, for fields that aren't required, the `null` branch is removed, since omitting the field already gives
      `None`. This matters because Claude's structured outputs accept at most **16 union-typed parameters and 24
      optional parameters** per request; anything more is rejected with "Schema is too complex for compilation".
      Upstream's `OpenAIRecipe` had 20 unions and now has 0. A CI test checks every schema in
      `mealie.schema.openai` against both limits, so an upstream schema change fails tests rather than imports.
      `OpenAIRecipe` is at 23 of the 24 optional parameters.
    - The adapter calls `messages.create`, or `beta.messages.create` with `server-side-fallback-2026-07-01` and
      `structured-outputs-2025-12-15`, and parses the text itself with `response_schema.parse_openai_response`. It
      deliberately does **not** use the SDK's `parse()`, because `parse()` validates every text block before the
      stop reason or a fallback block can be checked. That would turn refusals and truncation into validation errors,
      and reject successful server-side fallbacks, where the declining model's partial text comes first. Only the
      text after the last `fallback` block is parsed.
  - **Model refusals:** for the first-party API (no `base_url`, or `api.anthropic.com`), requests to
    `claude-fable-5-1`, `claude-opus-5-5`, `claude-opus-5` and `claude-sonnet-5-5` send the beta
    `server-side-fallback-2026-07-01` with `fallbacks: "default"`. If the model declines on safety grounds, Anthropic
    then retries on its recommended fallback model itself. If a request with these fields gets a 400 that names them,
    retry once without them.
  - **Stop reasons:** `refusal` raises `AIProviderRefusedError`, and `max_tokens` raises
    `AIProviderOutputTruncatedError`. Both count as failures, so the router tries the next candidate.
  - **Audio attachments** raise `AIProviderUnsupportedError`; the router moves on.
  - **Usage** comes from `response.usage`. When a server-side fallback ran, tokens are summed over
    `usage.iterations`, and the model that actually answered is recorded.
- **Connection test:** upstream's `test_connection` and `_check_image_support` work for Claude unchanged, because
  they go through `get_response(provider=…)`.
- **Model lists:**
  - `POST /api/groups/ai-providers/providers/models` takes an `AIProviderModelsQuery` body (for an unsaved
    provider; name and model optional).
  - `POST /api/groups/ai-providers/providers/{id}/models` uses the saved provider. Fields left out of the body keep
    their saved values.
  - **A blank key reuses the saved key only while protocol, base URL and request headers are unchanged.** Otherwise
    the endpoint returns 400, "Enter the API key again…", so a group manager can't send someone else's key to a host
    of their choosing. The same rule now applies to upstream's `POST /providers/{id}/test`.
  - These endpoints return data read from whatever host the base URL points at. Only model-like ids
    (`[\w.:/@+-]{1,128}`) and display names are passed on, at most 500 of them. The SDKs' pagination is cut off
    after 1,000 models (`mealie/services/ai/listing.py`).

  Both return `list[AIProviderModelInfo { id, display_name: str | None, supports_images: bool | None }]`. OpenAI
  protocol uses `client.models.list()`, with `supports_images` set to `None` (unknown). Claude uses the SDK's models
  list, with `supports_images` taken from `capabilities["image_input"]["supported"]`. Errors return the same
  type-and-status-only message as upstream's test endpoint, never the provider's response body.

## 3. Encrypted API keys

- `mealie/db/models/_model_utils/encrypted.py`: `EncryptedString(TypeDecorator[str])` (impl `String`,
  `cache_ok=True`), used for `AIProvider.api_key`.
- **Key:** HKDF-SHA256 over `get_app_settings().SECRET` (info `b"mealie:ai-provider-api-key:v1"`, 32 bytes), wrapped
  as a Fernet key. `cryptography` is already installed with authlib.
- **Stored format:** `enc:v1:<fernet token>`.
  - Values without the prefix are read as plaintext, so un-migrated rows keep working.
  - A token that won't decrypt (for example after `.secret` changed) is logged once per process and read as `""`. The
    call then fails authentication and the router moves to the next provider.
- **Migration:** encrypts existing plaintext keys; downgrade decrypts them.
- **Startup fix:** `mealie/db/fixes/fix_ai_provider_api_keys.py` runs on every boot and encrypts any key still
  stored in plaintext. That can happen if the migration's data step failed once, because saving a provider without
  re-entering its key doesn't rewrite it.
- **What encryption protects, and what it doesn't:** it protects keys when the database leaks on its own, such as a
  PostgreSQL dump or replica. It does **not** protect a backup zip. Backups include `.secret`, so the zip alone is
  enough to decrypt the keys, and on SQLite installs `.secret` sits next to `mealie.db`. Treat backups as secret.
- **Backups:**
  - Dumps hold ciphertext.
  - Restores bring back the backup's `.secret` before importing the database, so its keys stay readable and older
    plaintext backups are encrypted with the right secret.
  - The previous `.secret` is kept as `mealie_<date>.bak.secret`, next to upstream's `.bak.db`. If the import
    fails, it is put back.
- **Unreadable keys are visible:** `AIProviderOut.apiKeySet` is false when the stored key is empty or can't be
  decrypted. The settings page reads it from a new manager-only `GET /api/groups/ai-providers/providers` and shows
  "This provider's API key can't be read. Enter it again." `GET /api/groups/self`, which every member can read,
  still carries no key information (an upstream test guards that).

## 4. Usage log and monthly limits

- **Table `ai_usage_log`:**

  | Column | Notes |
  |---|---|
  | `id` | GUID, primary key |
  | `group_id` | FK `groups.id`, indexed |
  | `provider_id` | FK `ai_providers.id`, nullable, indexed |
  | `provider_name`, `model`, `protocol`, `slot` | |
  | `feature` | The `response_schema` class name, e.g. `OpenAIRecipe` |
  | `prompt_tokens`, `completion_tokens`, `latency_ms` | |
  | `success` | Boolean |
  | `error_type` | Exception class name; nullable |
  | `created_at` | Indexed, plus `BaseMixins` |

- **Recording:** `AIRuntime` writes one row per provider attempt on routed calls, whether it succeeded or failed.
  - Calls with an explicit provider (connection tests, pings) write no rows. Unsaved providers have made-up ids.
  - The OpenAI path reads `completion.usage`.
  - An empty answer counts as a failed attempt (`error_type` `EmptyResponse`), and the next provider is tried.
  - A failure to write the log row is logged and never breaks the AI call.
- **When every candidate is over its limit:**
  - AI recipe import shows `recipe.import-errors.ai-limit-reached`, also when the error is wrapped by the video
    importers.
  - The ingredient-parser endpoints return 429 with that message.
  - The import's compile step keeps trying other compilers first, so an image provider that's over its limit can
    still fall back to OCR.
- **Limits:** a provider's monthly usage is the sum of prompt and completion tokens in the current calendar month
  (UTC). The router compares it with `monthly_token_limit`.
- **API:** `GET /api/groups/ai-providers/usage?start=&end=` (group managers; default is the current month) returns
  `AIUsageSummary`:
  - `by_provider: [{provider_id, provider_name, model, requests, failures, prompt_tokens, completion_tokens,
    monthly_token_limit, last_used_at}]`
  - `by_day: [{date, requests, prompt_tokens, completion_tokens}]`
- **Retention:** a daily scheduler task deletes rows older than 400 days.

## 5. Tool registry

**Package `mealie/services/ai/tools/`:**
- `AITool`: `name`, `description`, `args` (a Pydantic model), `writes: bool`, and an async `handler(ctx, args)` that
  returns a Pydantic result.
- **Every result has a `speech: str`**, written to be read aloud.
  - `ToolResult` enforces at most 2 sentences and 300 characters. `get_cooking_step` reads the whole step, up to
    400 characters.
  - Every name, title or item taken from user data (recipe names, list notes, plan titles) goes through `spoken()`,
    which keeps at most 60 characters, cuts at a word and removes sentence punctuation. Recipe text can't inject long
    instructions into what a voice assistant reads back.
  - Markup and fraction glyphs are made speakable ("1/2"), and times are read from parsed minutes ("1 hour 30
    minutes").
- `ToolContext` carries `repos`, scoped to the caller's group and household, plus `user` and `translator`.
  `household` loads on first use.
  - **Building a context never touches the database.** Database work happens only inside the handlers' worker
    threads (`run_blocking`), which also end their read transaction there. A database query on the event loop
    froze the server under load (above about 15 concurrent calls). A regression test with 24 concurrent calls guards
    this.
  - The Phase 3 MCP server must follow the same rule, including doing its auth lookups off the event loop.
- A registry exposes `all_tools()` and `get_tool(name)`.

**First tools:**

| Tool | R/W | Arguments → result |
|---|---|---|
| `search_recipes` | R | `query?`, `max_total_minutes?`, `include_foods?`, `exclude_foods?`, `tags?`, `categories?`, `limit=5` (≤ 20) → `slug`, `name`, short `description`, `total_time`, `rating`. Times are free text, so `max_total_minutes` is applied after a "has any time" SQL prefilter, over at most 500 recipes; the speech says so when it hits that cap. Food names are matched in one eager query per call. |
| `get_recipe` | R | `slug`, `part` (`summary`, `ingredients`, `steps` or `all`), `servings?` → `name`, `yield`, ingredient display lines, numbered steps, notes. Scales from servings, or else the yield quantity, as the recipe page does. A free-text-only yield comes back as `None` when scaled. Sub-recipe ingredients show the sub-recipe's name. |
| `get_cooking_step` | R | `slug`, `step` (1-based) → that step, the step count, whether there's a next step |
| `whats_planned` | R | `start` (default today, server time as upstream), `end` (default `start`; the range covers at most 31 days), `meal?` (any upstream `PlanEntryType`) → plan entries |
| `suggest_from_ingredients` | R | `foods`, `max_total_minutes?`, `limit` → recipes you can make (slug, name, total time), with missing foods and substitutions, using upstream's recipe finder |
| `get_shopping_list` | R | `list_name?` → unchecked items. A generic name ("shopping list", "grocery list", "the list") means the list actually called that, or else the household's first list. Matching ignores "the/my/our", uses singular forms and fuzzy token matching. |
| `add_to_shopping_list` | W | `items` (list of strings) **or** `recipe_slug` (+ `servings?`), `list_name?` → number of items added; recipe ingredients go through upstream's recipe-to-list service |
| `plan_meal` | W | `date`, `meal`, `recipe_slug` **or** `note_title` → the created entry |

**REST** (any logged-in user; tools run as that user, with the same permission checks as the matching REST
endpoints):
- `GET /api/ai/tools` returns `[{name, description, input_schema, writes}]`.
- `POST /api/ai/tools/{name}` takes a JSON body of arguments and returns `{tool, result}`. Errors:
  - unknown tool: 404, `{detail: {message}}`;
  - something the arguments name doesn't exist (recipe, list, step): 404, with a message safe to read aloud;
  - invalid arguments: 422, `{detail: [{type, loc, msg, input}]}`;
  - any other tool error: 400.
- Write tools publish the same events as the matching REST endpoints (`mealplan_entry_created`,
  `shopping_list_updated`).

Per-client write grants arrive with the MCP server in Phase 3. Tool argument and result models live in the tools
package, not `mealie/schema`, because the frontend doesn't call them.

## 6. Settings UI

In **Group Settings → AI providers** (`GroupAIProviderSettingsEditor.vue`, `GroupAIProviderDialog.vue`):

- **Provider dialog:**
  - An **API type** select: OpenAI-compatible, or Anthropic (Claude).
  - An optional **Monthly token limit**.
  - A **Load models** button that fills an autocomplete for the model field from the models endpoint, with a badge
    on models that read images.
- **Slots:** each of Default, Image and Audio keeps its upstream primary select, plus an ordered **Fallbacks** list.
  An **Advanced** section adds Planner, Fast tasks and Embeddings, labelled as used by upcoming features.
- **Usage:** a table for this month showing requests, failures, tokens, and the limit as "x% of N". It reloads after
  provider changes and says so when it can't load, rather than showing an empty month.
- **Fallback pickers** are disabled until the slot has a primary, matching the backend.
- **Saving** writes the settings first, and writes routes only if that succeeded.
- **Warnings:**
  - providers whose key can't be read (from the manager-only provider list);
  - a blank key with a changed base URL, API type or headers;
  - base URLs containing `?` or `#`.
- **Where the fork code lives:** the model picker (`GroupAIProviderModelField.vue`) and the Advanced routes
  (`GroupAIProviderAdvancedRoutes.vue`) are fork-owned components, which keeps the diffs to upstream's dialog and
  editor small. The admin pages reuse the editor without routes, so they show no fallback UI: those endpoints only
  serve the logged-in user's own group.
- **Strings:** new strings go in `en-US` only. API types come from codegen.

## Testing

- **No network in tests:** OpenAI calls are mocked at `_get_raw_response` or the HTTP client, and the Claude SDK is
  mocked with `respx`/httpx2 transport mocks or by patching the adapter.
- **Routing:** primary then routes; dedupe; `planner`/`fast` falling back to default; `embedding` not falling back;
  the limit skip and the all-limited error.
- **Fallback:** the first provider raising moves to the next; everything failing raises the last error; usage rows
  are written for each attempt.
- **Encryption:** round trip, plaintext passthrough, wrong secret, migration up and down, backup round trip.
- **Tools:** each tool against seeded data, household isolation, argument validation, write permissions.
