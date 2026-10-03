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
- **The Claude adapter uses the official `anthropic` SDK** (`anthropic==1.8.0`), in its own module. Upstream's
  `openai.py` only gains a dispatch on `provider.protocol`.

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
| `default` / `image` / `audio` | upstream's primary for that slot (`*_provider_id`), then that slot's routes |
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
  `monthly_token_limit: int | None`, validated to be ≥ 1 or unset.
- **Adapter:** `mealie/services/ai/anthropic_adapter.py`. `OpenAIService.get_response` hands it the prompt, message,
  attachments, `response_schema` and provider. It returns `(parsed result | None, usage)`.
  - **Client:** `anthropic.AsyncAnthropic(api_key=…, base_url=provider.base_url or None, timeout=provider.timeout,
    default_headers=provider.request_headers or None)`.
  - **Request:** `system=prompt`. The user content is image blocks first (base64 from `OpenAILocalImage`, URL source
    from `OpenAIImageExternal`), then the text block. `max_tokens=16000`. No `thinking` and no `temperature`: current
    models think adaptively by default and reject explicit sampling settings.
  - **Structured output** uses the SDK's structured-output helper with the Pydantic `response_schema`. The result is
    validated with `response_schema` (use `parse_openai_response` on the raw text if the helper doesn't return a
    parsed object). Check the helper's exact name and arguments against the installed SDK; don't guess.
  - **Model refusals:** for the first-party API (no `base_url`, or `api.anthropic.com`), requests to
    `claude-fable-5-1`, `claude-opus-5-5`, `claude-opus-5` and `claude-sonnet-5-5` send the beta
    `server-side-fallback-2026-07-01` with `fallbacks: "default"`. If the model declines on safety grounds, Anthropic
    then retries on its recommended fallback model itself. If a request with these fields gets a 400 that names them,
    retry once without them.
  - **Stop reasons:** `refusal` raises `AIProviderRefusedError`, and `max_tokens` raises
    `AIProviderOutputTruncatedError`. Both count as failures, so the router tries the next candidate.
  - **Audio attachments** raise `AIProviderUnsupportedError`; the router moves on.
  - **Usage** comes from `response.usage.input_tokens` / `output_tokens`.
- **Connection test:** upstream's `test_connection` and `_check_image_support` work for Claude unchanged, because
  they go through `get_response(provider=…)`.
- **Model lists:**
  - `POST /api/groups/ai-providers/providers/models` takes an `AIProviderCreate` body (for an unsaved provider).
  - `POST /api/groups/ai-providers/providers/{id}/models` uses the saved key.

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
- **Backups:** dumps hold ciphertext, and restores bring back the matching `.secret` (`RESTORE_FILES`), so keys stay
  readable. A round-trip test covers this.

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

- **Recording:** `OpenAIService` writes one row per provider attempt, whether it succeeded or failed. The OpenAI path
  reads `completion.usage`. A failure to write the log row is logged and never breaks the AI call.
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
- **Every result has a `speech: str`**: at most two short sentences, written to be read aloud.
- `ToolContext` carries `repos`, scoped to the caller's group and household, plus `user`, `household` and
  `translator`.
- A registry exposes `all_tools()` and `get_tool(name)`.

**First tools:**

| Tool | R/W | Arguments → result |
|---|---|---|
| `search_recipes` | R | `query?`, `max_total_minutes?`, `include_foods?`, `exclude_foods?`, `tags?`, `categories?`, `limit=5` (≤ 20) → `slug`, `name`, short `description`, `total_time`, `rating` |
| `get_recipe` | R | `slug`, `part` (`summary`, `ingredients`, `steps` or `all`), `servings?` (scales quantities) → `name`, `yield`, ingredient display lines, numbered steps, notes |
| `get_cooking_step` | R | `slug`, `step` (1-based) → that step, the step count, whether there's a next step |
| `whats_planned` | R | `start` (default today), `end` (default `start`), `meal?` (any upstream `PlanEntryType`) → plan entries |
| `get_shopping_list` | R | `list_name?` (default: the household's first list) → unchecked items |
| `add_to_shopping_list` | W | `items` (list of strings) **or** `recipe_slug` (+ `servings?`), `list_name?` → number of items added; recipe ingredients go through upstream's recipe-to-list service |
| `plan_meal` | W | `date`, `meal`, `recipe_slug` **or** `note_title` → the created entry |

**REST** (any logged-in user; tools run as that user, with the same permission checks as the matching REST
endpoints):
- `GET /api/ai/tools` returns `[{name, description, input_schema, writes}]`.
- `POST /api/ai/tools/{name}` takes a JSON body of arguments and returns the result. Unknown tool: 404. Invalid
  arguments: 422.

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
- **Usage:** a table for this month showing requests, failures, tokens, and percentage of the limit.
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
