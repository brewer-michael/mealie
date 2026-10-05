# Phase 2: Recipe card ingestion v2 (design, as built)

Implements the "Phase 2" row of [`AI_INTEGRATION_PLAN.md`](../AI_INTEGRATION_PLAN.md) §9: jobs, batch upload, a
review page, ingredient linking, the card kept as an asset, an inbox folder, events and the eval harness. The exit
criteria are **a 20-card eval set scored per provider** and **a 10-card batch reviewed and committed from a phone**.

| # | Piece | What it gives you |
|---|---|---|
| 1 | Inputs | Phone batch capture, `POST /api/ai/ingest` (multipart, raw image, JSON base64, image URLs when turned on) for Home Assistant (HA) and iOS Shortcuts, and a watched inbox folder; photos, PDFs and multi-page TIFFs |
| 2 | Intake | Every image becomes an upright, metadata-free JPEG inside the upload request, so GPS never reaches disk |
| 3 | Jobs | A queue in the job table that survives restarts, crashes, backup restores and several worker processes |
| 4 | Extraction | Upstream's import workflow with card prompts, Tesseract rotation, the OCR fallback, an optional second read, and flags with stated reasons |
| 5 | Ingredients | Card shorthand ("1 T.", "1/4 t.") fixed before Mealie's NLP parser (the AI parser for other languages), then linked to your foods and units |
| 6 | Review | Cards in capture order: one tap for a clean card, about two per flag; re-read a region; add a batch's clean cards at once; undo |
| 7 | Commit | Crash-safe and idempotent; the card photo attached with an unguessable name and used as the cover, unless the household's new recipes are created public |
| 8 | Events | One "recipe cards ready" notification per batch through Apprise, including HA, and one "not added" per burst of refused inbox files |
| 9 | Privacy | A fail-closed "keep cards on this server" policy covering every AI call a card causes |
| 10 | Eval | Scores exactly the production pipeline, its flags and provider chains, and decides D3 |

The guiding rule is **fewer moving parts**. The job row is both the review document and its one pending piece of
work. There is no task table, broker, SSE stream or new top-level data directory.

## Decisions that differ from the plan

- **`AIRecipeService.build_recipe()` isn't used.** It hardcodes `OpenAIService`, discards the transcription and step
  outcomes, and leaves an empty `data/recipes/<uuid>/` per draft (F1). The worker drives
  `RecipeImportWorkflow(steps)` itself, as the eval already does.
- **Confidence is deterministic flags with a stated cause**, not model scores: markers, the model's `unsure` list,
  numbers missing from the transcription, parser results and plausibility checks (F8). A second, independent read
  is a group setting, **off by default** until the eval shows it pays for its cost (§4.5).
- **The ready event is a fork event** sent through Apprise notifiers. Household webhooks never receive bus events,
  so the plan's "webhooks reach HA with no new code" is wrong: HA gets the event through an Apprise `jsons://`
  notifier (F22).
- **The inbox lives outside `DATA_DIR`** (`AI_INGEST_INBOX_DIR`, or a folder mounted at `/inbox`), one folder per
  household, scanned by the card dispatcher rather than the scheduler. Inside `/app/data` it would be zipped into
  backups, wiped by restores and re-owned by `entry.sh`'s `chown -R`.
- **Image URLs in the upload API are off by default** (plan §4.2 and §6 list them for HA's `rest_command`).
  `AI_INGEST_URL_FETCH=true` turns them on. The server then fetches each URL through safehttp with a fork-owned
  allow-list (`AI_INGEST_URL_ALLOW_HOSTS`, on top of `HTTP_ALLOW_LIST`), so the global list isn't widened, and
  redirects off http(s) or from https to http are refused (§1.2). HA can also use the inbox or `curl -F`, and iOS
  sends the file.
- **The 20-card eval set stays private** under `DATA_DIR/groups/<gid>/eval-cards/` (plan §4.2 puts 15-30 cards in
  `tests/data/cards/`). Cards carry family names and handwriting; one to three redacted cards go into the repo. This
  is a user question.
- **`ai_providers.runs_locally` is a column on an upstream table** (plan §5 and §10 ask for side tables), following
  Phase 1's precedent for `protocol` and `monthly_token_limit`: a fork migration needs a merge revision at each sync
  either way, and a column keeps `AIProviderOut` a plain `from_attributes` schema.
- **Tesseract joins the backend CI image now** (plan §8 ties that to D3). Orientation depends on it whatever D3
  decides (§4.4), so `test-backend.yml` gains `tesseract-ocr` and the orientation tests run in CI.
- **The OCR fallback stays on the `default` slot** (the Phase 0 code), not `fast`: structuring noisy OCR text needs
  the strongest text model.
- **Commit creates foods, tags, categories and tools only for users who can organize**; units are created freely, as
  upstream allows (F21). AI suggestions never create organizers: only names the reviewer adds do.
- **`is_ocr_recipe` is set** on committed card recipes, guarded so a Mealie without the (deprecated) column skips
  it. `recipe_ingestion_jobs.recipe_id` is the fork's own provenance.
- **The card asset is the normalized JPEG**, never the uploaded bytes, which carry GPS and may be HEIC.
- **The card photo is attached, and used as the cover, unless the household's new recipes are created public**
  (`recipe_public`; the plan always attaches the scan). Recipe assets and images are served without auth (F19), and
  explore shows a recipe created public once its household isn't private, so such a household would publish, now or
  later, the family's handwriting; the reviewer can turn either switch on for a card (§7).
- **No edits to upstream `repository_factory.py` or `mealie/schema/openai/`.** Ingest repos sit outside
  `AllRepositories`, settings are a fork `BaseSettings`, the daily purge runs from the dispatcher, and the card
  response schemas live in a fork module registered in the Claude limit test. The upstream files the fork does
  touch get commented hooks only (Upstream touch points below).
- **No "Enrich" step.** Nutrition (Phase 4) listens to upstream's `recipe_created`, which commit publishes.
- **No "same card" toggle at capture time.** One upload request is one card (front first). The phone groups photos
  into cards before uploading. A back sent on its own can be added to the previous card later (**Add as back of
  previous card**, §6.2).

## Facts this design depends on

Verified on 2026-10-03 against `ai-integration` (alembic head `970cf50b85f4`) by the Phase 2 research: ten reports,
two critics, three judges and two design reviewers. File references are to that tree.

**Pipeline and schemas**
- **F1.** `build_recipe` hardcodes `OpenAIService` (`ai_recipe_service.py:87`), drops `compiled_source` and step
  outcomes, and `finalize_scraped_recipe` creates `recipes/<uuid>/` (`scraper.py:80-84`). `RecipeImportWorkflow(steps)`
  with a `WorkflowContext` and an injected service runs without a request (the eval's `run_card`).
- **F2.** `OpenAIRecipe` uses 23 of Claude's 24 optional structured-output parameters. Claude allows 24 optional and
  16 union parameters per request, and the schemas must avoid `ge`/`le` and non-None defaults. Claude drops the
  description of a field typed as one nested model; list fields and class docstrings keep theirs.
  `test_anthropic_adapter.RESPONSE_SCHEMAS` only collects `mealie.schema.openai`'s exports (`:318-322`).
- **F3.** `CompileSourceStep` catches every compiler exception, `exceptions.RateLimitError` included, logs it with
  its traceback and falls through to `OCRImageCompiler`, which sends OCR text to the **default** slot
  (`compile_source.py:68-88`). Its `_merge` reads `.image_url` and `.language` off each compiled document
  (`:140-145`), so a compiler must return an `OpenAICompiledSource`. Upstream wraps provider errors as
  `"OpenAI Request Failed. {e}"`, which can hold the provider's response body (`openai.py:420-423`). The eval
  script gives each config one compiler on purpose, so a failed vision read isn't scored as OCR
  (`eval_recipe_cards.py:741`).
- **F4.** `cleaner.clean` drops blank steps and strips `<angle brackets>` (`cleaner.py:32,252`).
- **F5.** `ResolveOrganizersStep` takes no constructor arguments: `resolve_organizers`, `attach_organizers` and
  `create_new_organizers` (default false) are fields of `WorkflowOptions` on the context (`context.py:44-59`). With
  `attach_organizers=False` it only stores the names on `ctx.organizer_names`, and its early return for a group
  with no organizers never fires, so it always calls the fast slot (`resolve_organizers.py:96-117`).
- **F6.** The NLP parser mis-parses card shorthand silently: `1 T. coconut oil` gives no unit and the food
  "T. coconut oil" at confidence 0.997, and `TB.` becomes terabyte. Unit normalization lowercases, so aliases
  can't fix it.
- **F7.** The real card (`IMG_2503`, fixture `tests/data/cards/banana-mug-cake.jpg`) is an MPO with 2 frames, a
  Display P3 ICC profile, GPS EXIF and **Orientation 6, which is wrong**: the raw pixels are upright, because the
  phone was held flat. After EXIF transpose the card is sideways (the fixture is 1536x2048). `ocr.extract_text`
  returns rotation 270, text and mean confidence 49 in one run of about 2 s (`tesseract.py:200`). `_find_rotation`
  keeps the best of four probe scores with no margin (`:174-179`). Measured here: the sideways fixture scores
  {0°: 307, 90°: 3533, 180°: 310, 270°: 8234}; an upright copy {0°: 6776, 180°: 3594}; an upside-down copy
  {0°: 3209, 180°: 6719}; two printed cards win at 0° by 3.4×. Tesseract is in the fork's image
  (`INSTALL_OCR: "true"`) and this dev container, not in CI. The card leaves the microwave time blank on purpose,
  and models invent a number there.
- **F8.** Logprobs are unavailable (Claude has none; Gemini and Ollama have none over the OpenAI-compatible API) and
  self-reported confidence is weak. Model bounding boxes differ across providers; a user-drawn crop works with all.

**Execution**
- **F9.** Every uvicorn worker runs the router lifespans and the scheduler. The scheduler's "minutely" tier runs
  every 5 minutes.
- **F10.** `OpenAIService.__init__`, `AIRuntime`, Pillow and the NLP parser do synchronous work inside async code.
  `OpenAIService.runtime` is a property subclasses override (`EvalOpenAIService` does). The base
  `AIRuntime.get_response` also serves `/create/ai` and the AI parser on the request's session.
- **F11.** Writing a usage row commits or rolls back the caller's session (`usage.py:41-58`), and
  `repository_generic.create` then calls `session.refresh`, whose SELECT opens a new transaction
  (`repository_generic.py:179-191`). `AIRuntime.get_response` goes from one failed provider's `record_attempt`
  straight to awaiting the next (`runtime.py:170-197`). On PostgreSQL a session left idle in a transaction across a
  provider await blocks restore's `drop_all` and DDL.
- **F12.** A Core `UPDATE … WHERE id=:id AND state='queued'` on GUID columns with a rowcount check is race-free on
  SQLite and PostgreSQL (4 processes). Raw SQL with dashed UUIDs matches nothing on SQLite. A per-group cap via a
  count subquery overshoots on PostgreSQL without a lock. The SQLite engine uses pysqlite's default transaction
  handling (`db_setup.py:50-56`), which issues `BEGIN` only before DML, so `SELECT … FOR UPDATE` then `UPDATE` isn't
  atomic there: every check has to live in the `UPDATE`'s `WHERE`.
- **F13.** One provider attempt can take 300 s × 3 SDK tries, times the providers in the route.
- **F14.** `BackupV2.restore` (`backup_v2.py:133-177`) drops every table, imports the database, then `_copy_data`
  runs `rmtree` and `copytree` on every top-level directory in the backup (`:78-87`). `copytree` creates
  directories with `exist_ok=False`, so a directory that background work creates mid-copy aborts the restore with
  the data half replaced. Root-level files other than `.secret` are left alone. A top-level directory missing on the
  target crashes the restore; `groups/` exists at every boot. The restore route (`admin_backups.py:104-113`) calls
  `BackupV2().restore` synchronously. `backup_v2.py` is already fork-modified (Phase 1's `.secret` handling).

**Data and uploads**
- **F15.** `AlchemyExporter` corrupts `sa.JSON` dicts and crashes on JSON string lists; JSON stored as `Text` passes
  (`ai_mcp.StringList`). `is_uuid` turns any top-level string `uuid.UUID` accepts (32-character hex, braces,
  `urn:uuid:`) into the dialect's GUID form, reformatting it (`alchemy_exporter.py:95-128`). Integer primary keys
  break the first insert after a PostgreSQL restore. Datetime columns must be listed in `look_for_datetime`
  (`test_every_datetime_column_survives_a_backup` guards it). SQLite enforces no foreign keys.
- **F16.** HEIC isn't an allowed asset extension. `OpenAILocalImage` writes `<name>-min-original.jpg` beside its
  source on every attempt.
- **F17.** With `File`/`Body` parameters FastAPI reads the whole body before auth runs; a handler taking only
  `request: Request` authenticates first. Starlette (1.6.0) has no file-size limit, and its `MultiPartParser` spools
  file parts into `SpooledTemporaryFile`s (in memory up to 1 MiB, then an unnamed temp file), which have no path.
  Proxies cap bodies (nginx 1 MB by default, Cloudflare 100 MB) and time out at 60-100 s.
- **F18.** Auth falls back to the `mealie.access_token` cookie (`dependencies.py:80-87`), which is `SameSite=None` in
  embedded mode (`auth.py:85-95`). The frontend always sends `Authorization: Bearer` (`plugins/axios.ts:28-30`), and
  its error interceptor toasts any `detail.message` (`:98-99`).
- **F19.** Recipe assets and images (`/api/media/recipes/{id}/…`) are served with no auth or privacy check
  (`media_recipe.py`). `recipe_public` defaults to true as a column and schema default, but upstream creates every
  household with `recipe_public = not private_household` (group/household creation, registration, setup wizard), so a
  new install creates private recipes.

**Commit**
- **F20.** `create_one` keeps the given id, commits the recipe, then the rating and timeline rows separately
  (`recipe_service.py:210-253`). It injects placeholder text for empty ingredient or step lists, and uses `settings`
  as given when set. `repos.recipes.create` retries 10 renames on any `IntegrityError`
  (`repository_recipes.py:90-102`), which can't fix a primary-key clash. Organizers and food/unit ids are resolved
  **by id with no group filter** (`auto_init.py:83-104,177-193`). `update_one` unlinks asset files not listed in
  `recipe.assets` (`recipe_service.py:144-164`). `RecipeService(repos, user, household, translator)` needs the
  translator (`recipe_service.py:48`); `get_repositories(session, *, group_id, household_id)` takes keywords only.
  `RecipeDataService.__init__` creates `recipes/<id>/images` and `assets` (`recipe_data_service.py:76-77`).
- **F21.** Creating foods needs `can_organize` (`foods.py:50-51`), which is false for invited members; units need
  nothing. Upstream's `create_from_ai` never sets `recipes.image`; `repos.recipes.update_image(slug)` does.

**Events and frontend**
- **F22.** `class AIEvent(Event): event_type: AIEventTypes  # type: ignore[assignment]` validates, and
  `AppriseEventListener.update_urls_with_event_data` plus `ApprisePublisher` deliver it. `EventBusService.dispatch`
  would raise (`event_bus_listeners.py:83`). Household webhooks only send the scheduled meal plan. The notifiers
  page is advanced-only.
- **F23.** `RecipeOrganizerSelector` (with `showAdd`), `RecipeNotes` and `RecipeImageLightbox` work on plain data.
  `RecipePage`, its header and toolbar, `RecipeAssets` and `RecipePageInstructions`' image upload need a saved slug,
  and the ingredient editor's "create food" writes at once (403 without `can_organize`). `vue-advanced-cropper` is
  installed. Mobile Safari drops background streams, so pages poll. The frontend has eslint and vitest, but no
  `vue-tsc`.

## Architecture

```
Phone PWA ─────────┐ POST /api/ai/ingest   one card per request, Bearer header required
iOS Shortcut ──────┤ multipart │ raw image │ JSON base64 (or image URLs, off by default)
HA shell_command ──┘      auth → pause, AI and quota checks (one thread call) → byte-capped stream
/inbox/<group-slug>/<household-slug>/ ─ scan 30 s, rename-claim ─┐
                                                                  ▼
             IntakeService.ingest(): expand (PDF pages rendered in a sandboxed child, TIFF frames), then
             holding the shared ingest write lock: sniff → pixel cap → decode → EXIF transpose → strip metadata
             → page.jpg ≤4096 · view.jpg 2048 · thumb.webp in DATA_DIR/groups/<gid>/ai-ingest/<job>/
             → one transaction under the household's intake lock: duplicate check, caps, batch touch
               (unsealed only) + job row (processing, task queued) → wake()
                                                                  │
IngestDispatcher (one per worker process, router lifespan, its own thread limiter for DB calls)
   claim (groups take turns; Core conditional UPDATE + lease token) · heartbeat 20 s · sweep · inbox
   · housekeeping (seals, notifications, stale commits, recipe_created resends, limit retries) · daily purge
   paused while DATA_DIR/.ai-ingest-paused is fresh; a restore also waits for in-flight writers (flock)
                                                                  │
daemon thread × (AI_INGEST_CONCURRENCY + 1 re-read slot): own loop, own sessions, locale,
ai_call_policy(local_only or the group's setting, rechecked every 5 s, job_id)
   orient (Tesseract, 1.5×, staged) → extract_card(ai):  CardImageCompiler [image] | CardOCRCompiler [default]
                                         ∥ cross-read on the same ai [image, opt-in]
                       → CardBuildRecipeStep [default] → ResolveOrganizersStep [fast, suggestions only]
                       → normalize_ingredients (shorthand → NLP, or the AI parser for other languages → matcher)
                       → compute_flags
   fenced finalize (lease token) → status=ready → batch finished? → Apprise (fork listener) → HA
                                                                  │
Review (polls): GET · PUT draft (draftVersion) · reread / reextract / rebuild / parse-lines → proposal or draft
                · rotate · merge · read-with-cloud
Commit (request): ready → committing (server-owned recipe id + asset token) → files → create_one
                  → cover key → committed → upstream recipe_created (at least once) · uncommit back to ready
Every routed AI call: AIRuntime.candidates() = resolve → apply_policy(local_only) → within monthly limits:
                      fail closed on every slot
Eval: normalize_page → orient_page → extract_card (the same functions) with EvalOpenAIService pinning and
      read_path=image or ocr per config
```

**Fork-owned code:**
- **Backend:**
  - `mealie/services/ai/ingest/`: `settings.py`, `limits.py`, `storage.py`, `images.py`, `pdf_render.py`,
    `fetch_url.py`, `shorthand.py`, `matching.py`, `flag_rules.py`, `intake.py`, `upload.py`, `batches.py`,
    `inbox.py`, `runner/` (with `results.py`, `answers.py`, `retries.py`), `pipeline/` (with `regions.py`),
    `tasks.py`, `review.py`, `commit.py`, `events.py`, `retention.py`, `eval_export.py`, `i18n.py`,
    `restore_guard.py`;
  - `mealie/services/ai/policy.py`, `local.py`, `tools/ingest.py`;
  - `mealie/routes/ai/ingest/`: `__init__.py` (owns the dispatcher lifespan), `_deps.py`, `upload.py`, `jobs.py`,
    `settings.py`, `notifiers.py`, `eval_cases.py`, `about.py`;
  - `mealie/db/models/recipe_ingest.py`, `_model_utils/json_text.py`; `mealie/repos/repository_recipe_ingest.py`;
    `mealie/schema/recipe_ingest/`; `mealie/scripts/strip_card_photo.py`; `mealie/db/migration_lock.py`;
    `mealie/pkgs/safehttp/redirects.py`, `decoding.py`;
  - prompts `mealie/services/openai/prompts/recipes/card-*.txt` (new files, so `OPENAI_CUSTOM_PROMPT_DIR` can
    override them).
- **Frontend:** `pages/g/[groupSlug]/recipes/cards/` (`index.vue`, `[jobId].vue`, `review.vue`),
  `components/Domain/Ingest/`, `composables/use-recipe-ingest*.ts` (`-uploads`, `-upload-storage`, `-files`,
  `-review`), `lib/api/user/recipe-ingest.ts`, `components/Domain/Group/GroupRecipeCardSettings.vue`,
  `components/Domain/Household/HouseholdNotifierAIEvents.vue`, and `scripts/typecheck-fork.mjs` (`pnpm
  typecheck:fork`: vue-tsc errors in fork-owned files fail CI).
- **Already fork-owned and changed:** `services/ai/runtime.py`, `usage.py`, `errors.py`, `tools/registry.py`,
  `routes/ai/__init__.py`, `db/models/group/ai_routing.py`, `schema/group/ai_routing.py`,
  `services/ocr/tesseract.py`, `scripts/eval_recipe_cards.py`, `test_anthropic_adapter.py`, the AI tool and MCP
  tests that list every tool, `frontend/app/pages/group/index.test.ts`, `docker/docker-compose.ai.yml`,
  `docker/unraid/mealie-ai.xml`, `docs/ai/*`.

**Upstream touch points:**

| File | Change |
|---|---|
| `mealie/db/models/users/__init__.py` | `from .. import recipe_ingest  # noqa: F401`, beside the `ai_mcp` import |
| `mealie/db/models/group/ai_providers.py`, `mealie/schema/group/ai_providers.py` | `runs_locally` column and field (Phase 1 already added columns here) |
| `mealie/services/backups_v2/alchemy_exporter.py` | thirteen names in the fork's `look_for_datetime` block |
| `mealie/services/backups_v2/backup_v2.py` | `@holds_migrations` and `@pauses_ingest` on `restore`, and the runtime files and folders backups leave out (the file is already fork-modified) |
| `mealie/routes/admin/admin_backups.py` | a busy restore answers 503 with its message |
| `mealie/app.py` | one import and one `add_middleware(RestoreGuardMiddleware)` line (§3.9) |
| `mealie/db/init_db.py` | `main()` runs upstream's body under `migration_lock()` (§17) |
| `mealie/core/settings/settings.py` | `determine_secrets`: processes starting together agree on one secret |
| `mealie/services/event_bus_service/event_bus_listeners.py` | Apprise event fields are percent-encoded, and the notifier's own query is left as written (§8) |
| `mealie/services/event_bus_service/publisher.py` | a webhook refused for an unsafe redirect, or whose answer is refused (over 1 MiB or not decodable), is skipped; the household's other webhooks are still sent |
| `mealie/pkgs/safehttp/{__init__,fetch,transport}.py` | redirect checks (no scheme other than http(s), no https to http); one-line hooks into the fork's `decoding.py`: curl decodes nothing and stops at a cap, and a compressed body is decoded there within it (`resilient_fetch`: 50 MiB by default; `post`, for webhooks and recipe actions: 1 MiB, each request given up on after twice its timeout); an allowed network vouches only for the addresses inside it (other private addresses in the same DNS answer are left out of the pin, or refused through a proxy) |
| `mealie/routes/recipe/recipe_crud_routes.py`, `mealie/services/recipe/recipe_data_service.py` | a clear message for a refused redirect, and for an image over the cap |
| `mealie/services/recipe/ai_recipe_service.py` | `create_from_ai` sets the cover key (`update_image`) |
| `mealie/services/openai/openai.py` | local-only calls get the pinned HTTP client; clients closed on their own loop (already fork-modified) |
| `mealie/lang/messages/en-US.json`, `frontend/app/lang/messages/en-US.json` | a `recipe-ingest` namespace; the backend also gains two `recipe.import-errors` keys for refused redirects |
| `frontend/app/lib/api/client-user.ts` | 3 lines for `RecipeIngestAPI` |
| `frontend/app/components/Layout/DefaultLayout.vue` | sidebar entry with the ready count and a badge for failed uploads, Create-menu item, the upload queue's connection (~40 lines) |
| `frontend/app/components/Layout/LayoutParts/AppHeader.vue`, `AppSidebar.vue`, `frontend/app/types/application-types.ts` | Log out asks while photos are pending; an optional sidebar badge |
| `frontend/app/pages/admin/backups.vue` | a busy restore shows one message and keeps the dialog open |
| `frontend/app/pages/g/[groupSlug]/r/create/ai.vue` | one "Have a stack of cards? Scan them in a batch" link |
| `frontend/app/pages/household/notifiers.vue` | one `<HouseholdNotifierAIEvents :notifier-id>` line |
| `frontend/app/pages/group/index.vue` | one `<GroupRecipeCardSettings />` line plus its import (its fork-owned `index.test.ts` gains one `vi.mock` line) |
| `frontend/package.json`, `pnpm-lock.yaml` | dev dependencies `vue-tsc` and `fake-indexeddb`, and the `typecheck:fork` script |
| `pyproject.toml`, `uv.lock` | `pypdfium2` (PDF pages, §2) |
| `.github/workflows/test-backend.yml`, `test-frontend.yml` | `tesseract-ocr` added to the existing `apt-get install` line; a `typecheck:fork` step |
| `dev/code-generation/` | stable export order and output; eval card photos aren't renamed; formatters run on generated files only |
| `frontend/app/components/Domain/Group/GroupAIProviderDialog.vue` | a "Runs on my network" switch (~10 lines; already fork-modified) |
| `frontend/app/plugins/app-info.client.ts` | a page opened during a restore says so and opens once it's over (§3.9) |
| `frontend/app/pages/login.vue` | signing in during a restore keeps the server's restore message, not "Something went wrong" |
| `frontend/app/error.vue` | new: the restore page (§3.9); every other error renders Nuxt's own error page |
| `frontend/vitest.config.js` | one `#app` alias, so `error.vue`'s test resolves Nuxt's error page |
| `docker/healthcheck.sh` | `503 paused_for_restore` from `/api/app/about` counts as healthy (§3.9) |

## 1. Inputs

### 1.1 Phone batch capture

The **Recipe cards** page is `/g/<group>/recipes/cards`. `recipes/` has only static children, so no recipe slug is
shadowed. It's reached from a sidebar entry **Recipe cards (N)** (N = ready), a Create-menu item **Scan recipe cards**,
and a link on upstream's AI import page. The Create-menu item and the link show only when the group can read cards: a
default provider plus an image provider or OCR (upstream's `_validate_providers` rule). The sidebar entry also shows
while cards are open (processing, ready, failed or waiting for a monthly limit), and carries a red badge for uploads
that failed while no cards page was open (with one "Open recipe cards" toast).

- **One side / Front & back**, remembered per browser.
- **Take photo** (`capture="environment"`, one shot per tap on iOS). In Front & back mode the button relabels itself
  *Take photo* → *Back side* → *Next card*, with **No back** and **Retake** in the row below, so the shutter never
  moves. A photo taken while a front waits is its back, whatever the mode. The line under the buttons counts the open
  batch's cards, with those retrying (offline, a restore) and those that didn't upload counted apart. Take photo,
  **Choose** and **Done** share one row at 375 px.
- **Choose photos** (`image/*,application/pdf`, `multiple`, no `capture`): the fast path for a big stack. Shoot
  everything with the Camera app, then pick them all. Chosen photos go to a tray (320 px thumbnails, a placeholder
  where the browser can't show the photo) with **Upload**; **Done** uploads the tray too. In Front & back mode they
  pair in selection order, with **Swap** and **Split/Join**. A PDF or TIFF of several pages is a card of its own (a
  one-page one pairs like a photo); pages are counted in the browser, and Join never makes a card over the limit.
- A drop zone on desktop. Files are recognised by their first bytes (the server's rule); anything that isn't a photo
  or a PDF is skipped with a notice naming it. Inputs reset `value=''` after reading.
- **A card uploads as soon as it is complete**: one card per request, two at a time, with `onUploadProgress`, three
  retries with backoff, then **Retry**. Originals are sent as they are, which keeps full detail for re-reading
  faint pencil. A `413`, or a `400` refusing the card as `too_large` or `too_many_pixels`, re-encodes that card's
  photos once in the browser (`createImageBitmap`, at most 3072 px, JPEG 0.9) and retries. **Data saver** (off by
  default, remembered per browser) sends every photo at most 4096 px. PDFs and multi-page TIFFs are always sent as
  they are.
- **Batches:** the first card creates one (`POST /api/ai/ingest/batches`). Every card sends `batchId` and its capture
  `position`. **Done** marks the batch *sealing* in the queue; `POST …/seal` goes only once every card of that batch
  has uploaded or failed for good, so the last card, still uploading when you tap Done, stays in the batch. While the
  capture page is open and visible, its batch is touched every 3 minutes (`POST …/touch`); an unsealed app batch
  seals itself after 10 minutes without a card or a touch.
- A `duplicate` rejection marks the card done with an **Already scanned** chip linking to the earlier job (usually a
  resend after a lost response), and **Scan again** resends it with `allowDuplicate`. The duplicate check covers
  every page, so a front sent again with its back isn't a duplicate (§2).
- **The queue survives a reload or a closed tab.** It's kept in IndexedDB, one database per user, and resumes on the
  next visit; where storage fails it stays in memory and the page says so. One tab of the browser keeps the user's
  queue: a Web Lock, or without one (as over plain http) a lease in that database (`{tab, until}`), taken in one
  transaction, renewed every 5 s and let go when the tab closes; a closing tab also says so over a BroadcastChannel, and
  its word hands the lease over at once; a reloaded page takes its previous page's lease at once, from a note in
  sessionStorage. A waiting tab takes it once it's free or has run out (90 s without a renewal), and **Use this tab**
  takes it over after 2 s without an answer. Only the tab keeping it uploads and writes: each write names its tab, and
  the lock is asked again before each upload (the lease in a transaction), so a tab that lost the queue unawares sends
  nothing. Photos that tab holds and the stored queue doesn't (not written yet, or given to it by a camera or file
  picker still open as the queue left) are uploaded from it, in a batch of their own, and the user's other tabs count
  them for a logout. Another tab's cards page says the queue is kept elsewhere, with **Use this tab**; only a cards page
  that's shown asks for an idle queue, so one left open in a background tab doesn't pull it from the tab in use. A front
  waiting for its back stays with the tab whose camera took it: **Use this tab** waits (the asking tab says so) until
  the back is taken there or **No back** is tapped, then asks again while its cards page is shown. A queue taken from a
  tab that didn't let it go (stolen after 2 s without an answer, or its lease taken or run out) puts that tab's waiting
  front in the new tab's tray as a card of its own, so a front is never paired with another tab's photo. A hand-over
  writes what arrived during its last write (3 writes at most), and anything still unwritten is sent from the tab it
  leaves. A tab that lost the queue decides once every write on its way has landed or failed (2 s at most), counting a
  card as stored when its record and photos are, whatever photos it carries since. Files still being read when the queue
  moves go from the tab they were chosen in; only a sign-out drops them. A tab whose storage failed holds photos no
  other tab could read back: it doesn't hand its queue over (the asking tab is told why), and if it loses the queue
  anyway (frozen while another took it) it keeps sending those photos itself and says to keep it open. Each card's
  stored record carries the "Keep these cards on this server" setting it goes with, so a tab that takes the queue over
  sends it as queued until the switch changes there; tabs follow each other's remembered choices (`storage` events) and
  read them again when they take the queue. Over plain http the database exists from the first visit (it holds the
  lease); a logout deletes it. `beforeunload` warns while photos are pending, unless the session has gone. **Log out**
  asks first ("N photos haven't been uploaded"; a tab keeping the queue that doesn't answer within 0.5 s, suspended in
  the background, is counted from the stored queue), then stops the uploads and seals open batches.
- **A privacy chip** says where photos go, from `GET /ingest/settings`'s `reader` (every member gets it): *Read by
  Claude Sonnet (cloud)*, *Read with OCR, then Claude Sonnet (cloud)*, or a lock and *Stays on this server*. Tapping it
  opens a panel over the page (the capture buttons don't move) that offers **Keep these cards on this server** when
  `localOnlyAvailable`, remembered per user. The switch applies to every card not sent yet: each upload carries it as it
  is when the card goes. If cards of the open batch already went with the other setting, that batch is finished and the
  next card starts a new one. A card can opt in to local-only when the group doesn't, never out. When the group keeps
  cards local and nothing local can read them, capture is hidden behind a warning. Over the monthly limit with no reader
  named, it says *Read once the monthly limit allows*.
- **Warnings** from the settings answer: the reader isn't running (`readerRunning`), every provider is over its monthly
  limit ("read automatically when the limit resets on {date}, or within about 10 minutes after a group manager raises
  it"), optional features off for the limit (`limitedFeatures`), and the inbox status (§1.3).

### 1.2 `POST /api/ai/ingest`

A `BaseUserController` method whose only parameter is `request: Request`. Before any body byte is read:

1. Auth runs (the controller's dependency). Then **the `Authorization` header must carry a Bearer token**: a cookie
   alone, another scheme or an empty token gets `401`, which closes the cross-site POST an embedding page could make
   with the `SameSite=None` cookie (F18).
2. `503 paused_for_restore` with `Retry-After: 60` while the pause marker is set (§3.9); `503 ingest_disabled` when
   `AI_INGEST_ENABLED` is off.
3. `400 ai_not_enabled` when the group can't read cards (§1.1); `400 local_only_unavailable` when the group is
   local-only and lacks a local image provider (or OCR) plus a local default provider.
4. `429 too_many_jobs` with `Retry-After` when the group already has 200 `processing` jobs (counted in the
   database); then `429 user_quota` when `AI_INGEST_MAX_PROCESSING_PER_USER` is set and the uploader has that many
   of their own.
5. `413` when `Content-Length` exceeds `AI_INGEST_MAX_UPLOAD_MB` (100), or 45 MiB for `application/json`.
6. The body is read through a byte counter that also stops chunked bodies at the same caps.

The handler is `async` (it streams the body), so checks 3 and 4 run in **one `anyio.to_thread.run_sync` call**: the
quota counts, the provider settings and `is_local_provider`'s `getaddrinfo` never block the event loop (Phase 3's
rule). Those counts were read before the body, so intake counts again for every card, under the household's intake
lock inside the insert's transaction (§2): a request whose first card finds a cap reached gets the same `429` with
nothing in, and a later card that would pass a cap is refused on its own as `quota`. Requests running at once can't
each add more past a cap. Intake checks the pause again under the write lock (§3.9), so a restore that starts
during a slow upload still gets `503`. Error bodies are `{"detail": {"code": …, "message": …}, "summary": …}`: a
translated `message`, which the frontend toasts, and a top-level `summary` a Shortcut can show whatever the error
(the route has its own route class for this).

| Content type | Shape | Callers |
|---|---|---|
| `multipart/form-data` | every file part is an image or PDF, any field name (`files` documented); text fields `batchId`, `position`, `split`, `localOnly`, `allowDuplicate`, `done` | PWA, `curl -F`, Shortcuts *Form* |
| `image/*`, `application/pdf`, `application/octet-stream` | the body is one file, a photo or a PDF (intake reads the bytes, not the type); options in the query string | Shortcuts *File* |
| `application/json` | `{"images": [{"data": "<base64 or data: URL>", "filename": "front.jpg"} \| {"url": "http://…", "filename"?}], "split": false, "batchId": null, "localOnly": false, "done": false}` | Shortcuts *JSON*, scripts, HA `rest_command` |
| other | `415` | |

- Multipart is parsed by Starlette's `MultiPartParser` over the capped stream (`max_files=20`, `max_fields=20`). Its
  file parts spool to the system temp directory, never `DATA_DIR`, and are closed when the request ends.
- Base64 is decoded leniently (line breaks, a `data:` prefix, URL-safe letters), since Shortcuts wraps lines. A JSON
  body is decoded in one of the intake slots, each image into a spooled file of its own. A malformed body is `400
  invalid_body`.
- **Image URLs** (`{"url": …}`; a bare string is always base64) are fetched only when `AI_INGEST_URL_FETCH` is on, after
  every check above, one at a time in their place; otherwise each is refused `url_not_allowed`. `fetch_url.py` allows
  http(s) only, with no credentials in the URL, no cookies, no environment proxy or `.netrc`; public addresses only,
  unless a host is in `HTTP_ALLOW_LIST` or `AI_INGEST_URL_ALLOW_HOSTS` (Home Assistant's address), an allowed address or
  range vouching only for the addresses it covers (a host's other private addresses are left out), with
  `HTTP_DISALLOW_LIST` winning; the connection pinned to the checked address; at most 3 redirects, each checked again,
  never off http(s) or from https to http; the body asked for and taken uncompressed only, cut off at the request's size
  cap; `AI_INGEST_URL_TIMEOUT` (20 s) for the whole download. Logs name the host only (a camera proxy URL carries its
  token). A URL that can't be fetched is `url_fetch_failed`. The host is looked up in a worker thread, so slow DNS never
  blocks the event loop, and `AI_INGEST_URL_TIMEOUT` covers the lookup. A host that doesn't resolve is
  `url_fetch_failed`.
- `localOnly=true` per upload is checked after parsing, with the same `400` as the group setting.
- `done=true` seals the batch once this card is accepted, and schedules the batch's notification check (a refused
  upload seals nothing).

```json
202 {"batchId": "…",
     "jobs": [{"id": "…", "status": "processing", "pageCount": 2, "reviewPath": "/g/home/recipes/cards/…"}],
     "rejected": [{"index": 2, "filename": "IMG_0007.HEIC", "reason": "duplicate", "duplicateOf": "…"}],
     "summary": "1 recipe card queued. You'll be notified when it's ready."}
```

- `summary` is in the request's `Accept-Language` (else en-US), for a Shortcut's *Show Notification*. It counts cards,
  not photos, says when a card was already scanned, and promises a notification only when a household notifier sends
  "recipe cards ready" (else "queued for review in Mealie"), says while the monthly limit is reached that cards are read
  when it resets or within about 10 minutes after it's raised, and says why cards couldn't be used (one reason in words,
  several as counts). Nothing is named `message`, which the frontend's axios interceptor would toast.
- `400 nothing_accepted` if nothing was accepted, with the same body in `detail` and no `message`. Rejection reasons:
  `too_large`, `unsupported_format`, `pdf_not_supported` (a PDF that can't be opened: damaged, empty or
  password-protected; one that ran out of render time, and the request's later PDFs; or any PDF where the renderer can't
  be confined and `AI_INGEST_PDF_UNCONFINED` is off), `too_many_pixels`, `unreadable_image`, `too_many_pages`,
  `duplicate`, `url_not_allowed`, `url_fetch_failed`, `quota`. A raw image over 30 MiB is a `too_large` rejection, not a
  `413`. (`no_permission` is the inbox's, §1.3.)

**Home Assistant:**
- **Recommended, the inbox:** `camera.snapshot` to
  `/media/<share>/<group-slug>/<household-slug>/card_{{ now().strftime('%Y%m%d_%H%M%S') }}.jpg`, where `<share>` is
  the folder Mealie mounts at `/inbox`. On HA OS the share is added first under *Settings → System → Storage → Add
  network storage* (usage *Media*). No token is needed.
- **Without shared storage, `shell_command`** (60 s limit): `curl -sS --fail-with-body --max-time 55 -H
  @/config/mealie_auth_header -F files=@/media/mealie_card.jpg http://mealie:9000/api/ai/ingest`. The token sits in a
  header file, not the command, because HA logs a failing command in full; `--fail-with-body` makes a refused upload
  show in HA's log (verified against a real HA; see As built).
- **`rest_command` with an image URL**, when the server turns on `AI_INGEST_URL_FETCH` and allows HA's address:
  `{"images": [{"url": "http://homeassistant.local:8123/local/card.jpg"}]}`. `rest_command` can't send binary, and
  templates can't base64 a camera image.

**iOS Shortcuts** (`docs/ai/CARDS.md`):
- *Scan a card:* Take Photo → Get Contents of URL (POST `…/api/ai/ingest`, header `Authorization: Bearer <token>`,
  body **File**) → Show Notification of `summary`.
- *Scan a two-sided card:* Take Photo twice → Base64 Encode each → a **JSON** body `{"images": [{"data": …},
  {"data": …}]}`.
- *Share photos to Mealie* (share sheet): Repeat with Each, body **File**. Consecutive requests auto-join one batch
  (§1.4), so the stack sends one notification.

API tokens work unchanged; a dedicated household user is the recommended identity for HA (plan D6). MCP OAuth
tokens never reach REST (PHASE3).

### 1.3 Inbox folder

- **Off unless `AI_INGEST_INBOX_DIR` is set, or a folder is mounted at `/inbox`.** With the variable unset (or blank:
  Unraid passes unused variables as empty strings), a mount at `/inbox` turns the inbox on and is logged once.
  `AI_INGEST_INBOX_DIR=/inbox` with nothing there keeps it off, with a log line. A directory inside `DATA_DIR` or
  `/app` is refused at startup (logged, inbox disabled). The inbox is off on Windows (no directory-descriptor
  calls). `docker-compose.ai.yml` and the Unraid template gain an optional share mounted at `/inbox`.
- **Ownership is by folder:** `<inbox>/<group-slug>/<household-slug>/`. Each scan creates the folder for every
  household, and the cards page shows yours. Folders Mealie creates get `AI_INGEST_INBOX_DIR_MODE` (`2775`:
  group-writable and setgid, set on the open folder so the umask can't strip it; an invalid value is logged and
  2775 used); folders that already exist keep their mode. Unknown folders are logged once and ignored. Inbox jobs
  have no uploader; they belong to the household. Anyone who can write to a household's folder can queue cards for
  it; mounting each household's folder as its own share gives a device only its own household.
- **A file is one card; a first-level subfolder is one multi-page card** (pages in name order), taken once all its
  files are stable. A PDF or multi-page TIFF gives the card all its pages (§2).
- **Scan** every `AI_INGEST_INBOX_POLL_SECONDS` (30) from the dispatcher, in a thread, at most 20 files a tick.
  - Skipped: non-regular files by `lstat` (symlinks too, each logged once); names starting with `.` or `~`; `.tmp
    .part .crdownload .partial .download .filepart`; `Thumbs.db`, `desktop.ini`; the reserved `processed/`,
    `failed/` and `.mealie-claimed/`.
  - A file is taken once its `(size, mtime_ns)` is unchanged since the last scan and it's 10 s old:
    `camera.snapshot`, SMB and scanners write in place under the final name.
- **Never through a link:** every file operation is relative to a directory descriptor opened with `O_DIRECTORY |
  O_NOFOLLOW` from the root down. A group, household or reserved folder that is a link, or not a directory, is
  skipped and logged once. One bad entry or folder stops nothing else.
- **Claim** by `os.rename` into `<household>/.mealie-claimed/<claim_ms>__<uuid>__<name>`, on the share's own
  filesystem (a rename into `DATA_DIR` would raise `EXDEV`). Another process's scan gets `FileNotFoundError`. The
  claim time is in the name because `rename`, Syncthing, rsync and `cp -p` all keep the file's original mtime.
- **No write access** (`EACCES`/`EPERM` on the claim): a card folder another user made under umask 022 can't be
  moved. It stays where it is; the log names the fix (Mealie's group needs write access: umask 002), and the inbox
  status lists it as `no_permission`. The scanning process records it in `DATA_DIR/.ai-ingest-inbox/blocked.json`,
  so a web process that doesn't scan lists it too.
- **Open once:** each claimed file is opened with `O_NOFOLLOW`, checked with `fstat` to be a regular file inside the
  inbox root, and that open file object goes to intake. Nothing reopens it by path.
- **Then** the same `IntakeService` (`source="inbox"`, batch auto-join per household folder, §1.4), then a rename to
  a unique name in `processed/YYYY-MM/`, or an unlink when `AI_INGEST_INBOX_KEEP_PROCESSED=false`. A rejected file
  goes to `failed/` with `<name>.error.txt` (created `O_EXCL | O_NOFOLLOW`): `Not added (<code>): …`, from the
  `recipe-ingest.inbox-rejected` keys, its numbers from the limits.
- **In the household's language:** an inbox card, its batch's notifications and its notes take the language of the
  household's latest app or API batch, else en-US.
- **Refusals are told:** each refused file logs one INFO line (its folder and reason code, nothing of the file), and
  a burst sends the household's notifiers one "Recipe cards not added" (§8): when a scan of the folder took
  everything it found, or two minutes after the burst's first refusal.
- **The app sees the folder** (`GET /ingest/settings` → `inbox`): how many photos wait and why (the group can't
  read cards, nothing local can, or it's at its processing quota), the entries Mealie may not move, and the newest
  10 refusals in `failed/`.
- **`processed/` is purged** when `AI_INGEST_INBOX_PROCESSED_DAYS` is set: once a day per process (first 10 minutes
  after the first scan), through the same descriptors, at most 5,000 entries a run, the regular files and card folders
  processed longer ago than that, and the month folders it empties; a file's age counts from the later of its mtime and
  ctime (a photo copied with its old date keeps its mtime), and a card folder is dated by its own move into
  `processed/`. Unset keeps everything.
- **Crash safety:** a claimed file whose `claim_ms` is over 10 minutes old is re-claimed with a second atomic rename
  to a fresh `claim_ms`, so only one process retries it. Intake's duplicate check and insert share one transaction,
  and an inbox intake confirms its claimed file is still at its path before committing (a retry would have moved
  it). If the job exists, the content hash finds it and the file is just moved. Nothing is lost or ingested twice,
  Syncthing reverts included.
- **Skipped while paused** for a restore; the pause is checked again before each file (§3.9). A group that can't read
  cards, or is at its processing quota, keeps its files where they are until it can. The quota is counted again in each
  card's insert, so uploads and other processes' scans count too. A card that finds it reached keeps its claim, retried
  after 10 minutes once there's room, and the group takes nothing more in that scan.
- **PDFs:** once one of a group's PDFs runs out of render time in a scan, it goes to `failed/` (`pdf_not_supported`);
  the group's other PDFs stay where they are, unclaimed, and the next scan renders them with time of its own, so a group
  renders at most one PDF that runs out of time per scan.

### 1.4 Batches

A batch is one capture session, Shortcut run or inbox burst. It groups the queue, orders the review and sends the
single notification (§8). Every job belongs to a batch.

- **App batches** are explicit (`POST /batches`), sealed by **Done** or after 10 idle minutes. `POST
  /batches/{id}/touch` (the capture page's heartbeat) keeps one open; it answers `409 batch_sealed` once sealed and
  `404` for any batch that isn't an app batch the caller started.
- **API and inbox uploads auto-join.** A request without `batchId` joins the newest unsealed batch with the same
  household, uploader (none for the inbox), `source` and source key (the inbox folder), if that batch saw an upload
  in the last 2 minutes. Otherwise it starts a new one. Auto batches seal after 2 idle minutes. `batchId=new`
  forces a new batch, and `done=true` seals the batch with this card.
- A sealed batch is never reopened: the card goes into a new batch by the rules above, and the `202`'s `batchId`
  says which, so the PWA adopts it. An unknown or foreign `batchId` is `404`.
- **Batch writes wait for a restore.** Creating, sealing and touching a batch, and the seal `done=true` makes, write
  inside a write section (§3.9): a restore waits for them, and while one runs they answer `503 paused_for_restore`.
- **Sealing can't race an insert.** The job insert runs `UPDATE recipe_ingestion_batches SET last_upload_at=:now
  WHERE id=:b AND sealed_at IS NULL` in its own transaction and picks or creates another batch when that matches 0
  rows. Sealing is `UPDATE … SET sealed_at=:now WHERE id=:b AND sealed_at IS NULL`, plus `AND last_upload_at <
  :cutoff` for the idle seal. On SQLite the touch takes the write lock; on PostgreSQL it holds the row lock until
  commit, and a waiting seal re-checks its `WHERE`. So a job never joins a sealed or notified batch.
- A job's `source` is its batch's: `app` for batches the PWA creates, `api` for auto-joined API batches, `inbox`.
- Jobs carry `position`: the app's capture index, else arrival order. Ties sort by `created_at`.
- **Simultaneous uploads share one batch.** The batch choice, duplicate check, position and insert run under the
  household's intake lock (§2), so two cards sent at once join one batch, and the same card sent twice at once is
  one job and one `duplicate`.
- Batches with no cards (a duplicate-only or abandoned upload) are purged a day after their last upload (§16).

### 1.5 Grouping and limits

One request is one card (front first); `split=true` makes each image its own card. Limits are code constants unless
named:

| Limit | Value |
|---|---|
| Per file | 30 MiB |
| Per request | `AI_INGEST_MAX_UPLOAD_MB` (100); JSON bodies 45 MiB |
| Images per request / pages per card | 20 / 4 |
| Pixels | 100 megapixels, checked before decoding; a JPEG up to 260 megapixels, decoded reduced (1/2, 1/4 or 1/8) to fit |
| Progressive or multi-scan JPEG | refused above 600 MB of decode memory (`too_many_pixels`) or 100 scans (`unreadable_image`) |
| Processing jobs per group | 200, counted per card inside the insert |
| Processing jobs per uploader | `AI_INGEST_MAX_PROCESSING_PER_USER` (0: no cap) |
| PDF rendering | `AI_INGEST_PDF_CPU_SECONDS` (20) of CPU and 1.5 times that in all per PDF; one card at a time per process, at most one card per group waiting |

Formats are recognised by magic bytes only: JPEG/MPO, PNG, WebP, HEIF/HEIC, AVIF, TIFF (every page of a multi-page
one) and PDF (every page, rendered). A document with more than 4 pages is `too_many_pages`.

## 2. Intake normalization and storage

Intake runs **inside the upload request**, so a job row exists only once its pages are on disk, and the uploaded
bytes, GPS included, never outlive the request. It costs about 0.3 s per phone JPEG and 1.2 s per 12 MP HEIC. At
most two run per process: they go through an intake `CapacityLimiter(2)` (`anyio.to_thread.run_sync(…,
limiter=…)`), so waiting uploads wait on the event loop rather than holding the default thread pool's tokens. The
inbox and JSON-body decoding share those two slots.

**Documents first.** `images.expand_document(raw)` turns each file into its pages before a slot is taken:
- a multi-page TIFF gives one page per frame (reduced-resolution copies and masks skipped);
- a PDF is rendered by `pdf_render.py` (pypdfium2) in a child process: the server's Python in isolated mode (`python
  -I`), no Mealie imports, almost no environment, capped at `AI_INGEST_PDF_CPU_SECONDS` of CPU time (20 by default) and
  1.5 times that in all, 1 GiB of memory and output size. Before it parses the document it confines itself: no new
  privileges; Landlock (where the kernel has it) allowing only fonts to be read (and from ABI 4 no TCP); a seccomp
  filter (x86-64 and arm64) refusing sockets and io_uring, new processes (threads only), everything that reaches another
  process (ptrace, its memory, pidfds, signals, resource limits, priority, scheduling, memory placement), every change
  to a file short of writing it (unlink, rename, truncate, link, mknod, mkdir/rmdir, chmod, chown, utime, extended
  attributes), and where Landlock is missing opening any file; `RLIMIT_FSIZE` 0, so it writes no file; `RLIMIT_NPROC` 1
  (but for root); few file descriptors. It runs in a process group of its own: at its time limit the whole group is
  killed and the wait for its output is bounded, so nothing it started holds the render slot. Without the seccomp filter
  (another architecture, no seccomp) it renders nothing (`pdf_not_supported`) unless `AI_INGEST_PDF_UNCONFINED=true`:
  Landlock alone leaves UDP, TCP below ABI 4 and the server's process within reach. Each page is rendered at the
  resolution it holds and never below: a page that is only a scan or a photo (an image covering at least half the page;
  invisible OCR text aside) at that image's own (the finest of several), a page with visible text, paths, shadings or an
  image mask beside other images at 300 dpi at least, within the 4096 px long side and `MAX_PIXELS`. Pages come back on
  stdout as length-prefixed frames. The protections in force are logged once (an error when PDFs are refused, a warning
  when the setting renders them unconfined). PDFs render in a slot of their own, one card at a time per process, uploads
  and the inbox in one first-come queue, at most one card per group in it, so another group's PDF waits at most one card
  per group, and a slow PDF holds up other PDFs but no photo. Once one of a request's PDFs runs out of time, its later
  PDFs are refused without rendering; in an inbox scan the group's other PDFs wait in the folder for the next scan
  (§1.3);
- each page's `raw_sha256` is derived from the file's hash and the page number, so the same file sent again is a
  duplicate. A file's pages fill the card, 4 at most.

`images.normalize_page(raw: BinaryIO, page_dir, index, *, original_filename) -> PageMeta` takes an open binary file:
a multipart part's `SpooledTemporaryFile` (it has no path, F17), a spooled file of decoded base64, or the inbox's
`O_NOFOLLOW` file object.
1. Sniff 16 bytes; reject anything not listed in §1.5. Hash the stream (`raw_sha256`, `raw_bytes`), then seek back.
2. Open with the format's own Pillow opener (the default opener also tries EPS and PSD, and Pillow's global
   `MAX_IMAGE_PIXELS` and warning filters are never touched). Check `width × height` **before** `load()`; a JPEG
   over 100 MP is decoded reduced (1/2, 1/4 or 1/8) when that fits, up to 260 MP; a progressive or multi-scan JPEG
   is checked for its decode memory and scan count first (§1.5). Then `load()`, which is what catches truncated
   JPEGs. Importing `mealie.pkgs.img` registers HEIF.
3. Frame 0 (MPO), `ImageOps.exif_transpose`, transparency flattened onto white, `convert("RGB")`.
4. Write, each to a temporary name in the same directory and then `os.replace`d:
   - `page.jpg`: long side at most 4096 (never upscaled), quality 90, ICC kept, **no EXIF, XMP or GPS**;
   - `view.jpg`: long side 2048, quality 90. Both the model and the review page see it, so crop fractions line up;
   - `thumb.webp`: `Image.thumbnail` to 480 px, aspect kept (not `PillowMinifier`'s 300 px centre crop).
5. Return `PageMeta {index, width, height, view_width, view_height, rotation, rotation_source (none | ocr | user |
   model), oriented, raw_sha256, page_sha256, original_filename (sanitized), format, raw_bytes, ocr: {text,
   confidence, lines} | null}`.

**Layout:** `DATA_DIR/groups/<group_id>/ai-ingest/<job_id>/pages/<n>/{page.jpg,view.jpg,thumb.webp}`. `groups/` is
created at boot, backed up with the rows and restore-safe (F14). It's never served without auth and untouched by
admin maintenance. Never `.temp` (wiped), `recipes/` or `users/` (served without auth). Names are server-made; iOS
calls every capture `image.jpg`.

**Writes happen under the ingest write lock** (§3.9). Intake holds it from `create_job_dir` through the row insert;
tasks, rotate, merge, commit, uncommit, discard, eval-case saves and the purge hold it for their file work. Only intake
creates job directories; the others write into directories that exist, through a temporary name and `os.replace` (a
merge moves page folders between two existing job directories). A restore therefore never meets a directory or file
created mid-copy.

**One household's intakes take turns.** The insert's transaction starts with the household's intake lock
(`lock_household_intake`): a transaction-level advisory lock on PostgreSQL, the database's write lock on SQLite, plus
a lock per household in the process. So the batch choice, the duplicate check, the caps (with a per-group advisory
lock on PostgreSQL for the group cap), the position and the insert can't interleave with another upload or inbox
card of the household, in any worker process.

**Duplicates:** `source_sha256` is the SHA-256 of the card's ordered page `raw_sha256`s, so the same front sent
later with its back isn't a duplicate. It's checked against the household's jobs in the insert's transaction, unless
`allowDuplicate=true`; a committed card counts only while its recipe exists, so a card whose recipe was deleted can
be scanned again. Each page keeps its own `raw_sha256`.

`source_name` is stored with a prefix holding a `/` (`upload/IMG_0007.HEIC`, `inbox/<group>/<household>/card.jpg`),
which `uuid.UUID` never accepts, so a UUID-named file can't be reformatted by a backup restore (F15).

**Pillow, not `jpegtran`.** `jpegtran` is lossless, but it's a system binary in neither the image nor CI, handles
JPEG only, and the pages are re-encoded for the model anyway. One Pillow path covers every format and enforces the
pixel cap. (`strip_card_photo`, §11.6, covers lossless cleaning of fixture photos.)

## 3. Jobs and the runner

### 3.1 One row, two state machines

`recipe_ingestion_jobs` is both the review document and the queue. `status` is the review lifecycle; the `task_*`
columns are the job's single pending piece of work. **A job has at most one task**, so re-reads, re-extracts and
commits never race on the draft.

| `status` | Meaning | Allowed |
|---|---|---|
| `processing` | first extraction queued or running | cancel, discard |
| `ready` | a draft exists | edit, reread, reextract, rebuild, parse lines, rotate, commit (alone or with the batch's clean cards), merge into another card, discard, save as eval case |
| `failed` | first extraction failed | retry, read with cloud providers (a local-only failure), rotate, merge, discard |
| `committing` | commit in progress; `commit_started_at` is its lease | resumes automatically |
| `committed` | `recipe_id` set | view recipe, back to review (uncommit), save as eval case (until the files are purged) |

- `task_state` is `NULL` (idle), `queued` or `running` (held by `lease_token` until `lease_expires_at`). `task_kind`
  is `extract` or `reread`. An `extract` task's `task_payload.mode` says what it does: none (first extraction or
  retry), `reextract`, `rebuild` (build the recipe again from the reviewer's edited transcription, no image read) or
  `parse_lines` (parse chosen ingredient lines with the AI parser). Job DTOs expose `task.mode` and `task.refs`.
- **The first extraction creates the draft.** A re-extract or rebuild on a draft nobody edited (`draft_version =
  extracted_version`) replaces it; on an edited draft it becomes a whole-card **proposal**, replacing any older
  pending whole-card proposal. A re-read always adds a proposal (§6.6). Parse lines writes into the lines nobody
  changed meanwhile and bumps `draft_version`.
- **Every new task starts clean:** `enqueue_task` (retry, re-extract, re-read, rebuild, parse lines) sets
  `attempts=0`, `rate_limit_retries=0`, `not_before=NULL` and `cancel_requested=false`, conditional on `task_state IS
  NULL`. The counters belong to one task, not the job's history.
- **Commit and discard cancel any pending task** on a `ready` job by clearing the task columns (§3.5): a re-read's
  result is advice, so it never blocks a commit.
- Discard is a hard delete of the row and its directory.
- **Merge** (`POST …/merge {intoJobId}`, "Add as back of previous card") moves this card's pages to the other card as
  its next pages, deletes this card and reads the other again. Merges and discards are serialized per household (the
  intake lock); a note `.merge-<source>.json` in the target's folder lets a merge cut short by a crash be settled the
  next time either card is read, by a request or by a task (a retry or re-read of the source settles it first:
  `review.settle_merges_to_read`), and before the target's folder is removed (discard, the purge:
  `review.settle_merges_into`), so the source never loses its pages with it. The target becomes local-only if the source
  was. The target's `draft_version` and `extracted_version` both go up, so a save or commit made with the version seen
  before the merge gets 409 `version_conflict` (the page reloads) rather than cancelling the merged page's reading; an
  unedited draft stays unedited.

### 3.2 Dispatcher

`IngestDispatcher` (`runner/dispatcher.py`) starts from the ingest router's lifespan
(`APIRouter(lifespan=dispatcher.lifespan)`, the Phase 3 precedent; no `app.py` edit), one per worker process, unless
`AI_INGEST_WORKER` is off (it is under `TESTING`; tests call `run_once()`). Its loop runs on the main event loop, and
**every database call goes through `anyio.to_thread.run_sync` with the dispatcher's own `CapacityLimiter(4)`**, so
an upload burst can't starve heartbeats.

- **Wake:** an `asyncio.Event` set by intake (`wake()`), else a 5 s poll that also finds other processes' jobs and
  expired backoffs.
- **Claim:** select ids `WHERE task_state='queued' AND (not_before IS NULL OR not_before <= now) ORDER BY
  task_priority, created_at LIMIT <free slots>`. Then, for each, a Core `sa.update` on the GUID column:
  `SET task_state='running', lease_token=:new_uuid, lease_owner=:instance, lease_expires_at=now+120 s,
  task_started_at=now, attempts=attempts+1 WHERE id=:id AND task_state='queued'`, kept only when `rowcount == 1`
  (F12). Re-reads and parse-lines tasks have priority 0, extractions and rebuilds 10. Each process also keeps **one
  slot only re-reads use**, so a reviewer's re-read starts at once even when both extraction slots are busy with a
  batch (§3.4). **Groups take turns:** the candidates are ordered after counting each group's running tasks, so one
  group's 10-card backlog doesn't hold another group's single card. With `AI_INGEST_GROUP_CONCURRENCY` above 0, a
  group's cards beyond the cap aren't claimed; the claim's `UPDATE` re-checks the cap, and on PostgreSQL takes a
  per-group advisory lock first. Re-reads and parse-lines tasks are exempt (short, and a reviewer is waiting). A
  task that went back to the queue with a cancel request is never claimed again.
- **Run:** a **daemon thread** per task with its own event loop, registered as `(loop, asyncio_task, token,
  deadline)`. Not a `ThreadPoolExecutor`: its threads are joined at exit and would hold a container stop behind a
  provider call.
- **Heartbeat** every 20 s: `UPDATE … SET lease_expires_at=now+120 s WHERE lease_token IN (:held) AND
  task_state='running'`, then read `cancel_requested` and which tokens still exist. A token that's gone (commit,
  discard, sweep) cancels its task, except one a backup restore took: that task carries on and its result is kept
  for the card's next task (§3.9). Liveness comes from the dispatcher, not the task's loop, which can be busy in
  PIL, NLP or Tesseract for seconds.
- **Sweep** every tick: `running` rows past `lease_expires_at` go back to `queued` while `attempts < 3`. After three,
  the poison guard ends the task by status: a `processing` job becomes `failed` with `interrupted`; a `ready` job
  stays `ready` with its task cleared and `error_code=interrupted` (a banner). Each is its own transaction, fenced on
  the old token.
- **Housekeeping** every 60 s: seal idle batches, send due notifications and retry failed ones (§8), resume stale
  commits and resend a missed `recipe_created` (§7), and read again cards waiting for a monthly limit (§3.6).
- **Inbox scan** every 30 s (§1.3). **Purge** once a day per process, first run 10 minutes after boot (§16).
  Housekeeping, the inbox and the purge run in the background, so a slow scan never delays a claim or a heartbeat.
- **Presence:** every running dispatcher sets the modification time of `DATA_DIR/.ai-ingest-dispatcher` at most once
  a minute, paused or not, so the app can tell whether any process reads cards (`readerRunning`, `/api/ai/about`'s
  `worker`). With ingestion on and `AI_INGEST_WORKER` off, the lifespan logs a warning.
- **Paused** while the restore marker is set: no claims, heartbeats, sweeps, inbox scans, housekeeping or purges
  (§3.9). At start the dispatcher removes a marker whose restore is gone.
- **Every phase survives errors:** each tick phase runs in `try/except` that logs and backs off (doubling to 60 s), so
  a failed query never stops the dispatcher for the life of the process.

All of it is idempotent across processes. **Every time comparison binds `now` from Python** as
`datetime.now(UTC).replace(tzinfo=None)`, matching the naive-UTC `NaiveDateTime` columns; `func.now()` and
`CURRENT_TIMESTAMP` are never used, since PostgreSQL returns the server's local time when its time zone isn't UTC.

### 3.3 Fencing

Every write by a running task is fenced: `WHERE id=:job AND lease_token=:token AND task_state='running'`. Tokens are
new per claim, so a task reclaimed after a pause, cancelled by a commit or swept never applies a stale result.

**Checks live in the `UPDATE`'s `WHERE`, never in a prior `SELECT`** (F12: on SQLite a `SELECT … FOR UPDATE` locks
nothing). Two rules cover every write to a job row:
- Each `UPDATE` sets only the columns it owns: the dispatcher's claim, heartbeat and sweep touch only task and lease
  columns; reviews touch the draft side.
- Every read-modify-write of the JSON columns (`draft`, `flags`, `proposals`, `pages`, `error_*`) is optimistic: it
  reads `row_version` and writes `WHERE id=:id AND row_version=:rv` (plus its own conditions) with
  `row_version=row_version+1`. Zero rows means re-read and retry, up to 3 times. `draft_version` is separate: the
  client's concurrency token, bumped only when the draft changes.

**Finalizing** is one such write. For a re-extract it first tries `… AND lease_token=:t AND task_state='running' AND
draft_version=extracted_version`, replacing the draft (`draft_version` and `extracted_version` both become
`draft_version+1`, so an open editor's next save gets `409`); with 0 rows it re-reads and writes a proposal under the
fence alone. A first extraction writes the draft; a re-read adds a proposal; a failure writes the error; each clears
the task columns. A failed fence drops the result. After a long process pause a provider call may run twice; its
result is applied once. PostgreSQL may add `FOR UPDATE` as an extra; correctness never depends on it.

A backup taken while a task runs brings back that claim's live token. So a restore, before it ends its pause,
queues every running task again with no lease (`requeue_all_running`, attempt given back), and the old claim's
result is refused by the fence (§3.9).

### 3.4 Concurrency

`AI_INGEST_CONCURRENCY` (2) task threads per process for any task, plus one that only re-reads use. Worker
processes are `WORKER_PER_CORE × UVICORN_WORKERS` (both default to 1), so the total is that times
`(AI_INGEST_CONCURRENCY + 1)`. `AI_INGEST_GROUP_CONCURRENCY` (0: off) caps one group's cards read at once across
every process (§3.2). The 200-job quota bounds the queue. A task holds a pooled connection only between awaits
(§3.7).

### 3.5 Cancellation

- **`POST …/cancel`:** a queued task is cleared by a conditional update: a `processing` job becomes `failed` with
  `cancelled`, a `ready` job stays `ready`. A running task gets `cancel_requested=true`. The cards list has **Cancel**
  on a card being read, and the review page beside the progress line of a re-read, re-extract, rebuild or parse.
- **Commit and discard** clear the task columns, including `lease_token`.
- Within one heartbeat the dispatcher calls `loop.call_soon_threadsafe(asyncio_task.cancel)` for cancelled or vanished
  tokens. That aborts an in-flight `httpx` request in milliseconds (verified); the fenced finalize records
  `cancelled` or finds no row. Spent tokens stay in the usage log.
- A task past its **30-minute deadline** is cancelled the same way (`timeout`). A thread stuck in synchronous code
  keeps its slot until it returns, and is logged.

### 3.6 Errors and retries

The runtime has already tried every candidate with SDK retries (F13), so most errors are final.

| Error | Outcome |
|---|---|
| `exceptions.RateLimitError` (every provider answered 429) | requeue, `not_before = now + min(60·2ⁿ, 900) s`; after 6 (`rate_limit_retries`, not `attempts`) fail `rate_limited`. It reaches the runner even when Tesseract could read the card: a rate-limited image read never falls back to OCR (§4.1) |
| `IngestPaused`, or any error while the pause marker is set | the task waits for the pause to end, then releases its lease fenced on its token (`queued`, `attempts - 1`, `not_before = now + 60 s`); if the restore replaced the row, the fence fails and nothing is written (§3.9) |
| `FileNotFoundError` on the job's files, outside a pause | `files_missing` |
| `AIProviderLimitReachedError` | `limit_reached`; a first extraction's card gets `auto_retry_at` = the next reset (the first instant of next month, UTC) and is read again then, or sooner once the limit no longer applies (checked at most every 10 minutes per group, under the card's own policy); a raised limit that doesn't help the card (OCR stands in for an image slot over its limit but finds no text) queues it again after 10 minutes, then twice that, up to 6 hours, a backoff kept on the card (`lift_retries`, `lift_retry_at`) so every worker process waits the same; once such a card is read, its batch notifies again for it (§8) |
| `OpenAINotEnabledException`, or no image provider and no OCR | `ai_not_enabled` |
| `AIProviderLocalOnlyError` (no local provider at all; local providers over their limit are `limit_reached`) | `local_only_unavailable` |
| `NoRecipeDataError`, `contains_recipe=false` | `no_recipe_found` |
| other provider errors | `provider_failed` with `{detail: describe_provider_error(cause)}`, never `str(e)` (it can hold the provider's response body) |
| the job's household is gone | `owner_missing` |
| 3 lease expiries / cancel / deadline / anything else | `interrupted` / `cancelled` / `timeout` / `internal_error` (logged with the job id) |

Commit adds `commit_invalid` (the draft no longer validates into a `Recipe`) and `commit_interrupted` (§7). Those
fourteen are `IngestErrorCode`. Codes and params are stored, and the frontend translates `recipe-ingest.error.<code>`.
Failed first extractions get **Retry** (also **Retry failed** per batch, which leaves cards waiting for a monthly limit
to their automatic retry); a failed row says when it tries again ("Tries again on …, or sooner if the limit is raised")
or when it's removed (§16). A card that failed `local_only_unavailable` because it was sent local-only, in a group that
doesn't keep cards local, can be read with any provider on purpose (**Read with cloud providers**, §10): on its page and
on its row in the list, which then offers no Retry while nothing local reads cards. A failed re-read, re-extract,
rebuild or parse leaves the job `ready`, with the code on `error_code` shown as a dismissible banner.

### 3.7 Inside a task

`runner/worker.py` `run_task(job_id, token)` in the task thread:
1. Read the job in a short session; stop if the fence fails.
2. `set_locale_context(get_locale_provider(job.locale), get_locale_config(job.locale))`. `locale` is the uploader's
   `Accept-Language`; inbox jobs use the household's latest app or API language, else en-US (§1.3).
3. Settle what a crash left: a page turn half swapped (§4.4), a merge half done (§3.1).
4. `with ai_call_policy(AICallPolicy(local_only=job.local_only or <the group's setting>, job_id=job.id,
   local_only_check=…))` (§10). The check reads the group's setting again at most every 5 s, so switching it on
   covers the task's next calls; once on, it stays on for the task.
5. A dedicated **AI session**: `session_context()` → `get_repositories(session, group_id=…, household_id=…)` →
   `JobOpenAIService(repos)`, used only for routing reads, the usage log and read-only lookups. **Job-state writes use
   their own short sessions** (F11). The cross-read shares this service and session (§4.1): each synchronous block
   runs to completion between awaits on the task's one thread, so the two reads never use the session at once.
6. **No transaction stays open across an await** (F11). `JobOpenAIService.runtime` is a `JobAIRuntime` whose
   `candidates()` and `record_attempt()` call the base method and then commit the dedicated session. The second
   matters because the usage write's `refresh` reopens a transaction and the runtime goes straight from a failed
   provider to awaiting the next. The pipeline's other awaits that follow a read on that session (OCR in a thread,
   the parser) call `end_transaction(session)` (in `pipeline/service.py`) first, which commits only if
   `session.in_transaction()`. The base `AIRuntime` keeps its request-path behaviour: only the job's own session is
   ended, and nothing is ever rolled back.
7. The handler (`tasks.handle_extract`, by `task_payload.mode`, or `tasks.handle_reread`) returns a result and
   writes nothing. Then the fenced finalize; then, after a first extraction, `events.maybe_notify_batch(batch_id)`
   (§8). AI clients are closed on the event loop that used them (task threads have their own loops).

**Progress:** `CardWorkflowContext.report_progress` stores **keys**, at most one write a second:
`recipe-ingest.progress.orienting`, `reading-card`, `reading-card-ocr`, `cross-reading`, `structuring`,
`linking-ingredients`, `suggesting-organizers` (upstream step keys are mapped to these). Progress writes are skipped
while a restore pauses ingestion.

### 3.8 Shutdown, multiple workers, dev reload

- **Shutdown** (lifespan `finally`): stop claiming, cancel running tasks through their loops, wait up to 5 s (Docker
  allows 10), then release every lease this dispatcher holds, by its owner id (`queued`, lease cleared, `attempts -
  1`, so deploys don't burn retries). A claim still in flight takes no further task once shutdown begins, and gives
  back whatever it claimed rather than starting it.
- **Several workers:** every process runs a dispatcher; claims, sweeps, seals, notifications and purges are all
  conditional updates, so they don't conflict. Migrations run under a lock, so several workers can start a fresh install
  together, and a worker starting during a backup restore waits for it (§17).
- **Dev reload** kills threads on every save; lease expiry recovers their jobs.

### 3.9 Pausing for a backup restore

A restore replaces `groups/` and `recipes/` while background work continues (F14). A directory or file created
between `rmtree` and `copytree` aborts the restore after the database was already replaced. A check made when a
request starts isn't enough: an upload can take a minute to arrive, and only then does intake create its directory.
So the pause has two parts: a marker that stops new work, and a lock that waits for work already writing.

- **The marker:** `DATA_DIR/.ai-ingest-paused`, JSON holding the time it was last refreshed and its restore's
  process (host identity, process id and start time) and restore lock. It's a root-level file, so `_copy_data`
  leaves it alone, and every worker process sees it. The restore refreshes it every 60 s; it's honoured while
  younger than 5 minutes.
- **A marker whose restore is gone** (a crash or a container stop mid-restore) is removed at once by the next check
  in any process. Each restore holds `DATA_DIR/.ai-ingest-lock.restore` for its whole run; a check that can take
  that lock exclusively proves no restore runs, and removes the marker while holding it, so a new restore (which
  takes the lock before writing its marker) never loses its own. Where locks don't work, a marker whose process ran
  on this host and is gone is removed instead; another host's marker there, or an older version's (time only), is
  honoured until it's 5 minutes old. The dispatcher runs the check at start.
- **The write lock:** `DATA_DIR/.ai-ingest-lock`, taken with `fcntl.flock`. Every fork section that writes under
  `groups/` or `recipes/` runs inside `storage.ingest_write()`: check the marker, join the process's shared `LOCK_SH`
  without waiting (failing means a restore holds it), check the marker again, write, leave. Any failure raises
  `IngestPaused`. Writers never block on the lock. The first section a process opens takes the shared lock and the last
  one to end releases it; a restore waits for its own process's sections through an in-process gate. The sections are
  intake (from `create_job_dir` through the row insert), each inbox file, a task's page writes (orientation) and its
  settling of a merge a stop left, the card routes that write (a draft save, queueing or cancelling a task, the
  settings, rotate, merge, commit from the draft save through the finish, uncommit, discard, eval-case saves), the batch
  routes (create, seal, touch, and the seal an upload's `done=true` makes) and each purged job.
- **Where the locks live:** `AI_INGEST_LOCK_DIR` when set; else `DATA_DIR`, unless its filesystem has no file locks
  (`ENOLCK`, `EOPNOTSUPP`). Then a lock file in `/tmp/mealie-ai-ingest-<uid>/` is used, with a warning: a folder
  created owner-only, and refused (with a warning) when it isn't this user's own or is open to others, since in a
  shared `/tmp` another local user could otherwise hold the lock first. A lock outside `DATA_DIR` works within one
  host only, so the stale-marker check trusts it only for this host's own marker. Where no lock works at all, the
  marker and the gate apply alone, with one warning.
- **`storage.pauses_ingest`**, a decorator on `BackupV2.restore` (one line in an already fork-modified file): take
  the restore lock, write the marker and start its refresher, then wait up to 45 s for in-flight writers and take
  `LOCK_EX`. If they don't finish, it raises before the restore has touched anything (`503`, "Mealie is still saving
  changes. Try the restore again in a minute."); 45 s stays under the 60 s reverse proxies commonly allow a request.
  Then it runs the restore. Afterwards, whether the restore returned or raised and still paused, it records when the
  restore ended and queues every running task again with no lease (§3.3). In `finally` it removes the marker and
  releases the locks. It covers every caller of `restore`.
- **While paused:**
  - the dispatcher doesn't claim, heartbeat, sweep, scan the inbox, do housekeeping or purge;
  - every `/api/` request but the restore route's own gets `503 paused_for_restore` with `Retry-After: 60` from
    `RestoreGuardMiddleware`, before any route, sign-in check or database access (the tables are being dropped and
    imported again: a sign-in check would answer 401 and sign the app out). Writes carry a translated `message` (the
    upload also its `summary`); reads and the token refresh don't, so a page polling doesn't toast. Ingest routes that
    write (files or only the database, batch routes included) also answer it when their write section can't start, and a
    restore waits for one already running; the bulk commit takes a section per card, so a restore can start between two
    cards and the rest are skipped `paused_for_restore`;
  - the SPA's server-rendered recipe pages (`/g/<group>/r/<slug>`, `/g/<group>/shared/r/<token>`), which read the
    database for their meta tags, are served as the plain app page without reading it; the app then waits for the
    restore itself;
  - a running task raises `IngestPaused` at its next write section and writes nothing to the database: its
    finalize, or the release of its lease, waits (polling every 5 s, within its deadline) until the marker clears,
    then applies fenced on its token (§3.6). The restore queued the task again, so the fence fails.
- **The browser stays signed in:** a 503 `paused_for_restore` never signs the user out. A token refresh answered so
  fails its request with that 503 rather than the 401 (card uploads retry after `Retry-After`; the next request
  refreshes again), and the session check (`/api/users/self`) asks again every 5 s, for up to 5 minutes, before the user
  is treated as signed out.
- **A page opened while paused:** the app's first request (`GET /api/app/about`) is refused, so the app can't start. The
  fork's `app/error.vue` shows "A backup is being restored. This page will open when it's done." in Mealie's colours,
  with the tab titled "Mealie" and no status code. It asks again every 5 s whatever `Retry-After` says (a restore takes
  seconds) and reloads once answered. Every other error is Nuxt's own error page. Signing in meanwhile keeps the
  server's message (`login.vue`).
- **Docker's health check** (`docker/healthcheck.sh`, a fork hook) counts `503 paused_for_restore` from `/api/app/about`
  as healthy, so an orchestrator that restarts unhealthy containers doesn't kill a restore halfway.
- **A restore doesn't pay for a reading twice.** A task whose result is refused because of a restore keeps it, or
  else the provider answers it got, in `DATA_DIR/.ai-ingest-results/<job>.<kind>.json` (atomic, owner-only, with the
  task's kind, payload hash and page hashes). The card's next task of the same kind, payload and pages applies the
  result, or replays each answer whose request is identical, through the normal fenced finalize; a task still
  waiting for its provider is waited for (at most 5 minutes). A kept result for other pages (an older backup) is
  deleted and the card is read again. The purge removes kept files older than 24 hours.
- **Upstream's writes:** `RestoreGuardMiddleware` (one line in `app.py`) makes every upstream API write (POST, PUT,
  PATCH, DELETE under `/api/`, except `/api/auth/*`, the card routes and the restore route) a write section, held for
  the rest of the request, background tasks included. It never reads or copies a body. It finds the route the router
  will run (FastAPI's route contexts, remembered per path). A route that declares a body (FastAPI reads it before
  anything else, sign-in included), or an MCP OAuth token endpoint (it reads its form first), enters its section as it
  takes the body's last message: the body streams to the route's own parser, a client still sending one never holds a
  restore off, and a section refused then is answered `503` and the route never runs. Any other route enters before it
  runs (it reads no body, or, like the MCP endpoint, checks its token before reading one); a request no route takes
  (404, 405, a redirect, the SPA's files) enters nothing. It checks for the pause and enters sections on threads of its
  own, not the event loop's default pool.
- **Missing files** are transient only while paused. Outside a pause a missing job file fails the task with
  `files_missing` (§3.6), so a job can't stay `processing` forever and hold its batch's notification.

## 4. Extraction pipeline and confidence

### 4.1 Steps

```python
@dataclass
class CardPage:
    dir: Path           # pages/<n>/ holding page.jpg, view.jpg, thumb.webp
    meta: PageMeta

class CardPipelineOptions(BaseModel):
    cross_read: bool = False
    suggest_organizers: bool = True
    read_path: Literal["image_then_ocr", "image", "ocr"] = "image_then_ocr"

async def extract_card(pages: list[CardPage], *, ai: OpenAIService, repos: AllRepositories,
                       translator: Translator, options: CardPipelineOptions,
                       on_progress: ProgressCallback | None = None) -> CardExtraction
```

`extract_card` makes **no database writes** and does everything from the read to the flags, so the worker and the
eval (§11) call exactly this. `options_for_group(session, group_id)` builds the options from the group's settings;
the eval also uses it, then sets `read_path` per config.

0. **Orient** (`pipeline.decide_orientation(page) -> OrientDecision(rotation, ocr, settled)`, worker and eval, once per
   page while `oriented` is false; it writes no files): with `pipeline.orientation_available()` (`AI_INGEST_ORIENT` on
   and the Tesseract binary installed, whatever `OCR_ENABLED` says), `ocr.extract_text(page.jpg,
   min_ratio=ORIENT_MIN_RATIO)` returns rotation, text, mean word confidence and line boxes in one run (2-4 s). It turns
   the page only when the best probe score is at least 1.5× the upright score and at least 500 (§4.4). The runner then
   stages the turned files, stores the metadata fenced on its lease, and swaps the files in (§4.4). Text, confidence and
   up to 300 line boxes go into `PageMeta.ocr`. A failed or undecided Tesseract run leaves the page unsettled
   (undecided: an upright score under 500, or under 1.5× another way up), so the reader's rotation applies (step 2).
   Without Tesseract (upstream builds) only the reader's rotation turns pages; the review page offers **Rotate** (§4.4).
1. **Cross-read starts** (only with `options.cross_read` and an image provider): an `asyncio` task reading the same
   `view.jpg`s with `card-transcribe.txt` into `OpenAIRecipeCardTranscript`, **on the same `ai`**. In production
   that's the job's service and session (§3.7); in the eval it's the pinned `EvalOpenAIService`, so both reads go to
   the provider under test and neither writes usage rows. It runs **concurrently** with steps 2-4, so it adds cost,
   not wait.
2. **`CompileSourceStep(compilers=…)`** from `card_workflow_steps(options, errors)`: `image_then_ocr` gives
   `[capture_errors(CardImageCompiler), capture_errors(CardOCRCompiler)]`, `image` and `ocr` give one of them.
   - `CardImageCompiler(ImageCompiler)`: image slot; upstream's compile prompt plus `card-compile-rules.txt`; schema
     `OpenAIRecipeCardTranscription`; images labelled "Image 1 (front)", "Image 2 (back)". Attachments are a fork
     `CardImage(OpenAIImageBase)` that reads `view.jpg` once and caches the data URL: no re-encode per attempt and no
     `-min-original.jpg` side file (F16). The answer's `rotation_clockwise` (one per image) turns pages Tesseract
     didn't settle (`rotation_source="model"`). **One image at a time:** if a multi-page read fails with a provider
     error (not a rate limit, monthly limit or policy refusal), each page is read on its own and the readings are
     joined under "Front:" and "Back:". A provider that fails this way on 2 cards in a row is read page by page for
     the rest of the process; a two-image answer resets the count, and a malformed, cut-off or refused answer never
     counts. The cross-read does the same.
   - `CardOCRCompiler(OCRImageCompiler)`: reuses `PageMeta.ocr` (running Tesseract only if it's missing), with the
     same rules and schema, on the **default** slot. Its `can_compile()` is false when the image compiler failed
     with `exceptions.RateLimitError` (a rate-limited card waits and is read properly, rather than read now with
     OCR), and when `OCR_ENABLED=false` (text stored by orientation doesn't switch the fallback back on). A monthly
     limit, a missing image provider or a local-only refusal still falls back, since the default slot may have
     other providers.
   - **Both return a plain `OpenAICompiledSource(contains_recipe, content, language)`** (F3: `_merge` reads
     `image_url` and `language` off every document). `attribution`, `unsure` and `read_path` go on the
     `CardWorkflowContext`. `image_url` stays out of the card schema, where it would cost an optional parameter.
   - `capture_errors(compiler, errors)` (moved from the eval script to `pipeline/compilers.py`) wraps a compiler so
     that an exception is **recorded and turned into `None`**, never re-raised. The step then moves on without
     logging a traceback, which could hold the provider's response body (F3). Each `CapturedError` keeps the
     compiler, the exception and `describe_provider_error`'s safe text.
   - When the step yields nothing (`NoRecipeDataError`), `extract_card` raises the most specific recorded error in
     this order: `RateLimitError` (the runner backs off), `AIProviderLocalOnlyError`, `AIProviderLimitReachedError`,
     `OpenAINotEnabledException`, other provider errors; with nothing recorded, `no_recipe_found`.
3. **`CardBuildRecipeStep(BuildRecipeStep)`**: default slot, upstream's `OpenAIRecipe` and prompt plus
   `card-build-rules.txt`, then upstream's `to_recipe` and `cleaner.clean`. A leading "From" is stripped from the
   attribution (in the card's language only with a colon after it, so surname particles such as "Van der" stay), and
   a description sentence that only repeats the attribution is dropped, keeping the rest's line breaks.
4. **`CardResolveOrganizersStep`** (fast slot, optional), configured through the context's
   `WorkflowOptions(resolve_organizers=options.suggest_organizers, attach_organizers=False,
   create_new_organizers=False)` (F5). `options_for_group` sets `suggest_organizers=False` when the group has no
   tags, categories or tools (`OrganizerResolver(repos).existing_names()`), so card text isn't sent for nothing.
   Names matching an existing organizer go into the draft as suggestions; the rest are dropped. A local-only card
   with no local fast provider, or a fast slot over its limit, skips it without a traceback, and the card gets the
   info flag `organizers_skipped` with the reason.
5. **No `TranslateRecipeStep`:** it always runs when enabled, rebuilds every ingredient and sends text to the fast
   slot.
6. Await the cross-read (a failure becomes the info flag `cross_read_failed`), then `normalize_ingredients` (§5)
   with an `IngestMatcher`, which only reads, then `compute_flags` (§4.6).

`pipeline.rebuild_from_transcription(...)` runs only steps 3, 4 and 6 on a transcription the reviewer edited (the
`rebuild` task mode), keeping the previous reading's metadata; its step outcome says `transcription`.
`pipeline.parse_lines(...)` parses chosen ingredient lines with the AI parser in any language (the `parse_lines`
mode).

Per card: one image call (one per page when a provider takes one image at a time), one default call and one fast
call (none without organizers), plus one image call with cross-read on, and one fast call to parse the ingredients
of a card not in English. `finalize_scraped_recipe` is never called, so no `recipes/<uuid>/` appears before commit.

### 4.2 Response schemas

In `mealie/services/ai/ingest/pipeline/llm_schemas.py`, not `mealie/schema/openai/` (no codegen or upstream package
edit). `test_anthropic_adapter.py` (fork-owned) appends them to `RESPONSE_SCHEMAS`, so the limit test and the
minimal-answer parse cover them.

```python
class OpenAIRecipeCardUnsure(OpenAIBase):
    """Something on the card you could read but aren't sure of"""

    text: str  # exactly as written in `content`
    alternatives: list[str] = Field(default_factory=list)
    reason: Literal["faded", "ambiguous", "cut_off", "smudged", "other"]


class OpenAIRecipeCardTranscription(OpenAIBase):
    contains_recipe: bool
    content: str
    language: str | None = None
    attribution: str | None = None
    unsure: list[OpenAIRecipeCardUnsure] = Field(default_factory=list, description="...")
    rotation_clockwise: list[int] = Field(default_factory=list, description="...")  # per image: 0, 90, 180 or 270


class OpenAIRecipeCardTranscript(OpenAIBase):
    """Everything written on the card as plain text, one physical line of writing per line"""

    contains_recipe: bool
    text: str


class OpenAIRecipeCardRegion(OpenAIBase):
    readable: bool
    text: str
    alternatives: list[str] = Field(default_factory=list)
```

On Claude (measured with `anthropic_adapter.output_schema`): the transcription has 5 optional parameters and 0
unions, the region 1 and 0, the transcript 0 and 0. There are no numeric constraints or non-None defaults; guidance
lives in the prompts, list-field descriptions and docstrings (F2). The class names label the usage log's `feature`.

### 4.3 Prompt rules

`card-compile-rules.txt` (appended to upstream's compile prompt; the card is data, not instructions):
- The images are one recipe card; Image 1 is the front.
- Copy every line exactly, abbreviations and capitals included ("1 T.", "1/4 t.", "1/3 C.", "pkg."), fractions as
  written. Don't expand, convert or correct.
- Exactly two markers, always in square brackets: **`[illegible]`** for writing you can't read, **`[blank]`** for a
  gap the writer left on purpose (an empty space or line where a number, time or word would go). Never fill a blank.
- When something could be read two ways (1/4 or 1/2, T or t, 350 or 380), write your best reading and add it to
  `unsure` with the alternatives.
- Who the recipe is from ("From Grandma Jo") goes in `attribution` exactly as written, and stays in the content.
- Keep side-by-side columns apart: title, ingredient column, then method.
- Skip crossed-out words. Never add quantities, times, temperatures or steps.
- For each image, set `rotation_clockwise` to how far it must turn clockwise to read upright (0, 90, 180 or 270),
  and transcribe the writing as it reads once upright.

`card-build-rules.txt`: keep `[illegible]` and `[blank]` where they are and never replace them; keep ingredient lines
exactly as transcribed; an oven temperature or a pan written among the ingredients ("350°", "9x13 pan") goes to the
steps as written, never to the ingredients; don't put the attribution in the description.

`card-transcribe.txt` (cross-read): every physical line of writing, top to bottom and column by column, exactly as
written, with the same two markers and case rules; no structure, no corrections.

`card-reread.txt`: the image is a crop around one field; its label and the previous reading come as quoted data;
transcribe exactly, with the same markers; `readable=false` if nothing is legible.

### 4.4 Rotation

- EXIF transpose always runs at intake, but phones held flat over a table record the wrong orientation (F7). The
  Tesseract probe at orientation is what turns such cards upright.
- **A turn needs a margin.** `_find_rotation` keeps the best of four scores with no threshold, and handwriting scores
  low. `extract_text` (fork code) gains `rotation_scores` on `OCRResult` and a `min_ratio` argument (default 1.0,
  today's behaviour, for the OCR fallback): the page turns only when the best score is at least `ORIENT_MIN_RATIO`
  (1.5) times the 0° score, and the winner also scores at least 500 (`MIN_TURN_SCORE`), so a blank page is never
  turned. Measured on the banana card (F7): sideways 27×, upside down 2.1×, upright and printed cards stay put. Text
  is read at the chosen rotation, so it matches the stored pages. `AI_INGEST_ORIENT=false` turns orientation off;
  `OCR_ENABLED=false` turns off only the OCR fallback reader.
- **The reader's rotation is the fallback.** The transcription carries `rotation_clockwise` per image (a number of
  degrees, not "turned left", which providers read differently). It turns only pages Tesseract didn't settle: none
  installed, a failed run, or an undecided one: an upright reading that didn't clear the bar a turn must (500 or more,
  and at least 1.5× every other way up). Pages Tesseract oriented, or a reviewer turned, are left alone.
- **Turns are crash-safe.** A turn stages `page.next.jpg`, the view and the thumb beside the page's files, stores the
  new metadata (a task's write is fenced on its lease and on the page's old hash), then swaps the staged files in,
  page last. A refused write discards them, leaving the page byte for byte as stored. Whatever reads a page's files
  next settles a turn a stop left half done, from the stored metadata: the next task, the page image, another
  rotate, a commit and an eval export. A per-page lock (`.turn.lock` in the page folder plus an in-process lock)
  serializes a manual rotate and the runner, so two turns of one page both land. An image that can't be settled yet
  is served `Cache-Control: no-store`, so no browser caches a mismatched page.
- The review page offers **Rotate** (a synchronous threadpool call, under a second; `409` while a task is active),
  then **Read again**. The docs say Tesseract (`INSTALL_OCR=true`, the fork image's default) is the reliable way to
  turn flat-on-the-table batch captures. CI installs it, so the orientation tests run there.
- Region fractions always refer to the upright page, so cropper, server crop and views agree.

### 4.5 Cross-read (opt-in)

The most damaging error is a number the vision read invents in a blank: the transcription itself contains it, so no
check against that transcription can see it (F7). A second read is the only signal that can.

- **Setting:** `recipe_ingestion_settings.cross_read`, **off by default**. Re-extracts honour it; re-reads don't use
  it. The eval decides whether the default changes (§11.4).
- **Alignment** (`pipeline/crossread.py`, pure): the transcript is split into lines. Each draft ingredient takes its
  best transcript line by `token_set_ratio` on letters only (at least 60), preferring a line shaped like it
  (starting with an amount as it does); lines that read alike but for their amounts go in order. Each step is
  compared with windows of 1 to 4 consecutive transcript lines starting at every line (`partial_ratio` at least 70),
  since steps wrap, and with longer windows grown while still shorter than the step (`ratio` at least 70). To bound
  the cost, long runs of very short lines are grouped into chunks for the longer windows only, and windows that
  can't win are pruned first: a one-word-per-line page takes 741 scorer calls (16 ms), not 21,090. Lines with no
  match are left alone.
- **Salient tokens:** numbers (unicode fractions and mixed numbers as rationals, ranges kept), units compared by
  meaning (C and c, t and tsp, Tbs and Tbsp agree; T and t don't), temperatures, and the two markers. A marker
  counts as a number position, so a gap raises one flag, not two.
- **Flags:** a number, unit or temperature in the draft line that the aligned window lacks raises
  `read_disagreement` (warning, the window's text offered as an alternative). A window holding `[blank]` where the
  draft has a number raises `blank` (error, `source: cross_read`, `params.value` the number as written). Agreement
  never lowers a flag; both reads can share a mistake.
- **A free check on printed cards:** when every page read with Tesseract confidence 80 or more and its text is at least
  half the transcription's length, clearly different whole numbers raise `read_disagreement` with source `ocr` ("Text
  recognition read "…" here"). A number that differs only by digits Tesseract confuses (1/7, 0/8, 3/8, 5/6, 6/8, 0/6,
  0/9, or a stroke more beside a 1 or 7: "1" read as "71") is read again: its line alone, cropped from `page.jpg` by its
  box and scaled to 40 px high, in single-line mode (`ocr.read_line`, `pipeline/ocrcheck.py`). It is flagged when that
  reading says what the first did, not when it says what the draft does, and not without one. This needs no second
  provider call.
- The review page's checks line says so: "Read by Claude Sonnet 5.5 · checked against a second reading" (§6.4).

### 4.6 Flags: how confidence is computed

`pipeline/flags.py` `compute_flags(draft, extraction, resolutions, *, transcription, previous, units, ocr_lines, linked,
ocr_reread) -> list[CardFlag]` is pure. The server runs it after extraction **and on every save** (with the stored
transcription, the group's units and Tesseract's lines, as finalize does), so flags always describe the current draft.
Flags are keyed to a field plus the ingredient's `reference_id`, the step's `id` or the note's `id`, never an index
(F4), with a stable id `"<kind>:<field>:<ref>"` (a unit's `linked_fuzzy` adds `#unit`, an OCR disagreement `#ocr`).
Flags that point at a value carry `params.start` and `params.end`, the offsets of the exact occurrence, so a highlight
or a one-tap fix changes that occurrence and not a "2" inside "1/2".

| Kind | Severity | Source | Raised when |
|---|---|---|---|
| `illegible`, `blank` | error | marker | a field contains `[illegible]` / `[blank]` (the real card's microwave time) |
| `blank` | error | cross_read | the second read has `[blank]` where the draft has a number (§4.5) |
| `missing_name` | error | validator | the name is empty |
| `unsure` | warning | model | an `unsure` entry matches the line (partial ratio ≥ 85); carries the alternatives |
| `not_on_card` | warning | validator | a number in the line (digits, fractions, ½-style glyphs, normalized) is nowhere in the transcription; a step's own list number ("2. Microwave") doesn't count |
| `marker_dropped` | warning (card) | validator | the transcription has more markers than the draft: the structuring step filled a gap |
| `read_disagreement` | warning | cross_read, ocr | §4.5 |
| `check_parse` | warning | parser | NLP average confidence < 0.85 (`flag_rules.REVIEW_CONFIDENCE`, matching the parse dialog's `confidenceThreshold` in `use-parse-ingredients-dialog.ts`); or the parsed fields lose an amount on the line (a range's end, a second amount, "dozen"), with `params.value` (numbers in a food's name, "2% milk", don't count); or a size word ended up in the unit or food; or a second ingredient's amount joined by and, plus, +, & or a comma that only the note keeps ("2 c. flour and 1 t. soda"); or the parser split off an alternative or second food, which the note keeps as written ("or margarine"), named in `params.alternative`. Amounts a word describes ("110 degrees", "21 to 25 count") don't count, and only amounts parsing kept in the note are checked, never ones the parser kept itself (the line's hash records which). A package size before the unit isn't flagged, nor is a measure in parentheses that is the same amount in another unit, right after the unit ("2 cans (15 oz.) beans") or ending the line after its food ("1/2 c. butter (1 stick)", "1 can tomatoes, 16 oz."); one with a joiner, a range, "about", the line's own unit or an implausible size ("1 c. sugar (2 tbsp.)") is. Also flagged: an oven temperature or a pan read as an ingredient (`params.not_ingredient`: `temperature` or `pan`), and a line over 500 characters still as parsed with amounts kept (`params.too_long`) |
| `unit_unclear` | warning | parser | a quantity, no unit, and a token after the number that looks like a unit: an abbreviation dot, a known or group unit, one letter away from one, or a dotted 4-6 letter plural or abbreviation of a unit or container word ("pkges.", "Tbsps.", "btls."); not a short food word or a linked food the token starts |
| `linked_fuzzy` | warning | parser | the linked food or unit isn't on the line under any of its names, plurals, aliases or common spellings ("2 rd onions" linked to "red onion") |
| `implausible_amount` | warning | validator | more than 20 tsp, tbsp or cups, or `11/2`-style fractions (offers "1 1/2") |
| `implausible_temperature` | warning | validator | 2 to 4 digits: °F outside 200-550 or °C outside 90-290 in a step, unless the clause is about rising, cooling, warm liquids or a thermometer (then only the upper bound) |
| `empty_section` | warning (card) | validator | no ingredients or no steps (commit would add upstream's placeholder) |
| `read_by_ocr` | warning (card) | ocr | the OCR fallback read the card; params carry its confidence |
| `cross_read_failed` | info (card) | cross_read | the second read failed, so fewer checks ran |
| `organizers_skipped` | info (card) | parser | tag suggestions were skipped (`params.reason`: `local_only`, `limit_reached` or `failed`) |
| `shorthand_read` | info | parser | the pre-normalizer expanded "T.", "t." or "C." |
| `not_parsed` | info (card) | parser | the card isn't in English and the AI parser failed, so its lines stay as text (§5) |
| `new_food`, `new_unit` | info | parser | the name isn't linked; §5 says what commit does |

**Resolving flags:**
- **Errors block commit** (`422` listing them) until each is fixed or **kept**. **Keep as written** is one tap. For a
  marker, commit turns `[blank]` into `___` and `[illegible]` into "(unreadable)" in the job's locale; for a cross-read
  `blank` it keeps the number. `missing_name` can only be fixed. A line kept with a marker is still parsed around it, on
  save and at commit: "1 C. [illegible]" becomes 1 cup with the note "(unreadable)". Its parse is checked as a freshly
  read line's, with what parsing did kept in its hash: an amount the parser kept in its own note ("1 c. [illegible]
  sugar or 3/4 c. honey") is never asked about, only amounts parsing appended. When the marker stands for the amount,
  the unit goes into the note beside it, so the recipe reads "sugar ___ cup" (upstream shows a unit only next to an
  amount).
- **Warnings are highlighted** and can be dismissed with **Looks right**; they don't block. Infos show quietly.
  "Check this ingredient" shows the parsed reading and what was lost ("The 3 of 2-3 is kept in the note."), with
  **Keep as text** (the line becomes its note) and **Parse with AI**; "Check the link" offers **Keep as new food**
  (or unit) for members who can add foods.
- Resolutions are stored by flag id. A marker flag whose marker is gone resolves itself; parse flags drop off a line
  once it's edited (each ingredient keeps an `extracted_hash` of its parsed fields). Editing a flagged note's text
  means keeping its flag again; a note's `unsure` flag stays while the uncertain words still roughly match, as a
  step's does.
- `HIGHLIGHTED_SEVERITIES = {error, warning}` is the one definition the page (through the DTO) and the eval use.
  A card with no unresolved error or warning is **clean**.

### 4.7 Region re-read

- `POST …/reread {page, x, y, width, height, target: {field, ref}}` takes fractions of the upright page
  (`x+width ≤ 1`, `y+height ≤ 1`, sides ≥ 0.02), queues a `reread` task and returns `202`. It runs in the re-read
  slot (§3.4), so it starts at once. A job with an active task answers `409 {detail: {code: "busy"}}`; the page
  queues further re-reads itself (§6.5). A target with no `ref` reads a new ingredient, step or note.
- **Where to start:** `GET …/region-hint?field=&ref=` answers where on the upright page that field's text probably is
  (`pipeline/regions.py`): the best-matching Tesseract line box (`partial_ratio` at least 70, neighbours added when the
  text runs over several lines) widened to a band, else a 12%-high band at the line's position in the transcription
  (using its "Front:"/"Back:" headers), else `404 not_found`. A text under 8 characters once its markers are out is
  never looked for inside longer lines: it's placed between the Tesseract lines most like, as whole lines, the
  transcription's lines before and after it; else (no marker) at a Tesseract line saying about what it says; else just
  below the line before it or just above the line after when Tesseract read only one; else at its own transcription
  line. The service passes which of the field's lines saying the same text the target is (case and spacing aside,
  `occurrence`), so a short line the card says twice (the frosting's "1 egg") is found as the card's second; a longer
  one still gets the first one's place (Known limitations).
- `pipeline.reread_region` crops `page.jpg` (4096 px: up to twice the detail of the whole-card read) with a 3%
  margin, upscales a crop under 1000 px with LANCZOS (at most 3×), and sends **only the crop** to the image slot with
  `card-reread.txt` and `OpenAIRecipeCardRegion`. With no image provider but Tesseract present, the crop is OCR'd and
  the proposal says so. The crop is built in memory; nothing is written to disk.
- A user's crop works with every provider, local ones included. Model bounding boxes differ in format and accuracy,
  so none are used; the hints come from Tesseract and the transcription.

## 5. Ingredient normalization and linking

`pipeline/ingredients.py` `normalize_ingredients(recipe, *, repos, translator, matcher, language)`, in the task
thread:

1. Strip lines and drop empty ones (one empty string makes NLP raise for the whole call). Keep section titles. Lines
   that still hold a marker aren't parsed at extraction; they stay as notes, flagged (once kept, they're parsed
   around the marker, §4.6).
2. **Shorthand pre-normalizer** (`shorthand.py`, `prepare_line`), only on the token right after the leading quantity
   ("don't" and "t-bone" are untouched), and only when the card's language is English or unknown. The case-sensitive
   table:

   ```python
   QTY = r"(?:\d+\s+\d+/\d+|\d+/\d+|\d+(?:[.,]\d+)?|\d*\s*[½⅓⅔¼¾⅛⅜⅝⅞])"
   UNITS = {
       "T": "tbsp",
       "Tb": "tbsp",
       "Tbs": "tbsp",
       "Tbsp": "tbsp",
       "TB": "tbsp",
       "TBS": "tbsp",
       "TBSP": "tbsp",
       "t": "tsp",
       "ts": "tsp",
       "tsp": "tsp",
       "C": "cup",
       "c": "cup",
       "pkg": "package",
       "Pkg": "package",
   }
   ```

   The banana card becomes "1 tbsp coconut oil (melted)", "1/4 tsp salt", "1/2 tsp vanilla" and "1/3 cup almond
   flour". The eval and the cross-read import this table rather than keeping their own. Beside it:
   - `ABBREVIATIONS`, matched ignoring case: `doz`, `env(s)`, `sq`, `pkgs`, `pkt(s)`, `tbs`, `tbl`, `tbls`, `tblsp`,
     `teasp` (dozen, envelope, square, package, tbsp, tsp), which the parser doesn't know. "dozen" stays the unit, and a
     fractional dozen keeps its amount ("1/2 doz." is 0.5 dozen).
   - Shorthand after a joined second amount is written out too ("1 c. plus 2 T. flour"), and so is shorthand in a
     measure in parentheses ("1 env. (1 T.) gelatin"), which the parser read as tesla.
   - `UNIT_SPELLINGS`: each common unit's spellings (teaspoon/tsp/ts/teasp, pound/lb, package/pkg/pack/packet/pk/pkt,
     …). A written-out shorthand links the group's unit only by one of these spellings or their plurals ("Tablespoons"),
     or keeps the parser's link when its names are one ("cup(s)"), else it becomes a new unit ("sq." is never linked to
     quart); `linked_fuzzy` accepts them as exact.
   - Size words go to the note: heaping, scant, level, med, lg and the like wherever they stand, and small, medium,
     large, extra large or jumbo right after the amount. So "1 c. sugar (scant)" isn't the unit "cup scant"; "Big Red"
     keeps "Big" in the name. A package size or can number ("1 (8 oz.) pkg.", "1 8-oz. pkg.", "1 - 8 oz. pkg.", "2-15
     oz. cans", "1 #2 1/2 can") goes to the note. A mixed number written with a dash or "and" ("2-1/4", "2 and 1/2", "1
     & 1/2") is joined first.
   - Every pattern runs in linear time on long runs of spaces or digits.
3. **The parser:** English (or unknown) cards use NLP: `get_parser(RegisteredParser.nlp, …)`, `parser.data_matcher =
   matcher`, `await parser.parse(lines)`. Other languages use `CardIngredientParser`, upstream's AI parser asking the
   card's own routed service on the fast slot (so the job's policy, the usage log and the eval's pinning apply); if it
   fails, lines stay as text with the `not_parsed` info flag. Brute links "pkg." to kilogram, so it isn't used. When
   parsing loses an amount that was on the line (a range's end, a second amount, a second ingredient run into the food),
   the note keeps it ("to 3", "+ 2 T.", "or 3 eggs") and `check_parse` fires. An alternative or second food the parser
   splits off as a substitution (which nothing stored) is kept in the note as the line writes it ("or margarine", "and 1
   t. baking powder"), and `check_parse` names it. A measure in parentheses that is the same amount in another unit is
   kept as written, without a flag (§4.6). This holds on a line of any length. The parser's renderings of the line's
   amounts are written in the line's words ("or 1 T. oil", "plus 2 T., sifted": two amounts of one food, which reads
   right and isn't flagged), and nothing the line doesn't say is ever added ("or 1 tesla").
4. Restore `original_text` to the raw card line (NLP overwrites it with its input) and the titles; recompute
   `display` (it goes stale after matching).
5. Store `{reference_id, title, original_text, quantity, unit: {id|null, name}, food: {id|null, name}, note,
   display, parse_confidence, extracted_hash}`.

**On save**, a line whose text changed with no amount, unit or food set (a filled blank, an edited or new line) is
parsed like a freshly read line; fields the reviewer set are never overwritten, and the save's answer returns the
lines it parsed (§6.6). **Parse with AI** (`POST …/parse-lines {refs}`) parses chosen lines with the AI parser in any
language, as a `parse_lines` task; the result is written into the lines nobody changed meanwhile, and their parse
flags are judged again.

`IngestMatcher` (`matching.py`) extends Phase 1's alias-eager `FoodMatcher` (8 queries, not one per food) with
alias-eager units. One per task, and a fresh one per commit.

**At commit:** every food and unit id is checked against the group's matcher; unknown or foreign ids become names
(F20). Names are matched again exactly (name, plural or alias), so foods created since extraction aren't duplicated.
A missing **unit** is created, with its standard abbreviation. A missing **food** is created only if the committer
can organize; otherwise the line keeps quantity and unit and the food name leads its note. The review page labels
these chips "New food" or "Kept as text" from `permissions.canCreateFoods`.

## 6. Review page

### 6.1 Routes and order

- **Review:** `/g/<group>/recipes/cards/<jobId>`, with `definePageMeta({middleware: ["group-only"], key: route =>
  route.fullPath})` so each card remounts.
- **Start of a batch:** `/g/<group>/recipes/cards/review?batch=<id>` redirects to the batch's first `ready` card, in
  `position` order, that has an unresolved error or warning, else its first `ready` card, else the queue filtered to
  the batch. Notifications open this URL.
- Cards are reviewed in **capture order**, so the screen matches the stack in your hand. **Commit & next** goes to
  the batch's next `ready` card (wrapping round to skipped ones). After the batch's last card it goes to the queue
  filtered to the batch while cards of it are still being read ("2 cards are still being read"), else on to the
  next batch with a ready card ("· Next batch"), else to the queue.

### 6.2 Phone layout (xs/sm)

1. **Header:** "Card 3 of 10 · 2 to check", previous/next, and ⋯ (Rotate, Read whole card again, Re-read an area,
   What the card says, Save as eval case (managers), Add as back of previous card, Read with cloud providers (a
   failed local-only card), Back to review (an added card), Discard). A menu item that can't be used now is off,
   with its reason shown under it.
2. **Card strip:** sticky, about 32% of the height; tap for full screen (upstream's `RecipeImageLightbox` on
   `page.jpg`), swipe down to collapse it to a 56 px bar. Front/Back toggle.
3. **Checks line:** "Read by Claude Sonnet 5.5 · checked against a second reading", or "Read with OCR (confidence
   49%). Check every line."
4. **Needs a look (2):** one item per unresolved error or warning, in reading order. Each shows the line as read with
   the problem highlighted (the flag's own occurrence, §4.6), then:
   - alternative chips (tap to apply);
   - a one-line input for a blank ("___ minutes": the typed value replaces the marker);
   - **Re-read** (the region dialog, targeted at the line);
   - **Keep as written** (errors) or **Looks right** (warnings);
   - for ingredient lines, **Keep as text**, **Parse with AI** or **Keep as new food** where they apply;
   - **Edit** (jump to the row).

   Resolved items collapse with a check mark; a re-read's proposal appears inside its item with **Use** and
   **Dismiss**.
5. **The recipe:** collapsible sections. Name and details; ingredient rows that read like the card ("1 tbsp coconut oil
   (melted)") with link-status icons, tapping one expands quantity, unit and food autocompletes (from the group's unit
   and food stores, with no create button) and the note, plus the card's original line, its own **Re-read** and **Parse
   with AI**; steps, each with **Re-read**; ingredients and steps reorder with **Move up**/**Move down** (and a drag
   handle on desktop), keeping their ids so flags follow; tags, categories and tools in the fork's
   `IngestOrganizerSelector` (suggestions preselected; for members who can organize, a **Create "X"** item adds a
   name-only one, created at commit; plain Enter only picks a match); notes in the fork's `IngestNoteList`, flagged like
   steps and keyed by note id; the attribution; **Use the card photo as the recipe image** and **Attach the card photo
   to the recipe** switches, which start at the household's default (off when its new recipes are created public, §7).
6. **Bottom bar** (fixed, safe-area inset): **Skip** · **Commit & next**. While errors remain the primary reads **1 to
   fix** and scrolls to it. Notices ("Added *Banana Mug Cake*" with **Undo**, naming the recipe as made from the commit
   answer's `name`, e.g. "Added *Banana Mug Cake (2)*" when the name was taken, the rotate hint, "Re-read queued",
   errors) show in one dismissible strip inside the bar; the page's bottom padding follows the bar's height. A task's
   progress line has **Cancel**. After commit, `router.replace` to the next card, whose bar shows the "Added" notice.

A clean card costs **one tap**; a flagged card about two per flag.

### 6.3 Desktop layout (md+)

The card viewer is on the left (5/12, sticky: `top: 48px; height: calc(100dvh - 48px)`) with Front/Back, **Rotate**,
**Re-read an area** and the transcription toggle. The editor (7/12) has the same parts as the phone, with "Needs a
look" first and the proposal banner under it.

**Keyboard** (`useMagicKeys`; ignored while typing, except the first): `Ctrl/⌘+Enter` commit and next;
`Alt+↓`/`Alt+↑` next or previous flagged field; `R` re-read an area; `[` `]` previous or next page; `Esc` closes the
selector. Enter never submits the transcription, eval-note, re-read or discard dialogs (Enter on the selection
sends a re-read).

**What the card says** shows the transcription with **Edit**; **Rebuild from this text** saves pending edits, then
builds the recipe again from the corrected text with no photo read (`POST …/rebuild`). An unedited draft is
replaced ("Rebuilt from your text"); an edited one gets a whole-card proposal.

### 6.4 What is highlighted, and why

- A field with an unresolved error (red, blocks commit) or warning (amber) gets a coloured edge **and** an icon, with
  its explanation in the "Needs a look" item ("The card leaves a gap here. Fill it in or keep it blank."). The
  reasons are exactly §4.6's kinds; nothing is highlighted without a stated cause.
- The checks line (§6.2) says which reader produced the draft and whether a second read checked it, from
  `extraction.read`.
- **Possible duplicates** (on each GET and in each save's answer): a group recipe holding the slug the name would get
  ("A recipe called "X" already exists. Adding this card makes "X (2)"."), with the name commit would give it
  (`duplicateName`, the first free "Name (n)"); else the household's recipe with the most similar name (ratio at
  least 90); and another card waiting or being read with the same name, with **Open card**.
- When the household's recipes are seen without a login (not private, and created public) and the card photo is the
  cover or attached, a note says the card photo will be visible to anyone (asset and cover are served without auth,
  F19).
- A failed card shows when it tries again and when it's removed.

### 6.5 Re-read a region

`IngestRegionDialog` uses `vue-advanced-cropper`'s `Cropper` directly (`:canvas="false"`, `:check-orientation="false"`,
since the images are upright and EXIF-free) in a `BaseDialog` (full screen on phones), `refresh()`ed after the
transition, with the fork's `IngestRegionStencil`, so a touch drag moves the selection from the first pixel. It sends
fractions and the chosen field, preselected when opened from a field or flag (from the menu or toolbar nothing is
preselected, and Send waits for a target). An ingredient is listed by its line as on the card (`originalText`, the same
for a parsed line and one kept as text), else, for a line added in review, as its row shows it. Opened from a flag or
line, it asks for the region hint (§4.7) and starts the selection there, 5% wider on each side, waiting at most 1.5 s;
with no hint it starts where the last area read on that page was. Arrow keys move the selection by 2% and Shift+arrows
resize it, with a screen-reader line saying where it is. The result arrives as a proposal. While a task is active,
further re-reads wait in a client-side queue and are sent one by one as the state poll shows the job idle.

### 6.6 Edits, autosave, versions

- A debounced `PUT` (1.5 s) carries `draftVersion`, the draft, flag resolutions, resolved proposal ids and
  `clientDraftSchema` (3, §13). The server validates the draft as a `CardDraft` (the whole whitelist: no id, slug,
  assets, settings, rating or extras exist on it; numbers must be finite), recomputes flags and writes with `WHERE
  id=:id AND draft_version=:v AND row_version=:rv` (§3.3), bumping both. A stale `draftVersion` is **409 `{detail:
  {code: "version_conflict", current}}`**, with no `message`, so the axios interceptor doesn't toast over the page's own
  **Reload this card** dialog (at most 1.5 s of typing lost). A commit refused as stale with nothing unsaved (a page
  merged into the card meanwhile, §3.1, or another device saved) doesn't ask: the page reloads the card and says "This
  card changed somewhere else, so it was reloaded. Check it, then commit again." in the review bar; when that reload
  fails, the **Reload this card** dialog asks instead and the commit waits for it. The dialog is kept for a refusal that
  would drop unsaved edits. A changed `row_version` alone (a proposal landed) is retried on the server. The response
  carries the new version and flags, the lines this save parsed (merged into the page where the line still matches what
  was sent) and the duplicate state, with a "Saved" indicator. `draftVersion` changes only when the draft does;
  resolving a flag or settling a proposal leaves it.
- A save that failed for the network, a 5xx or a 429 is retried after 2, 4, 8, 16 and 32 s, then every 60 s while
  the page is open. Leaving the card, or moving to another card, saves first and asks "Your last changes aren't
  saved. Leave anyway?" if that fails. A logout the user chooses saves a pending edit first; once signed out the page
  sends nothing and doesn't ask.
- Tasks never touch the draft or its version, except a re-extract or rebuild on an unedited draft, which replaces it,
  and Parse with AI, which writes its lines (§3.1). The editor is read-only while one of those runs, so they can't
  conflict. A re-read finishing mid-edit causes no conflict. Accepting a proposal is an ordinary edit, and brings that
  reading's flags with it, the OCR check's included: a number Tesseract may have misread is read again from the page
  (§4.5), before the save's write and with no transaction open, and again if the card's pages change meanwhile;
  accepting one another device already settled is a `409 version_conflict`.
- Commit waits for the pending save and sends the version it returned.
- While a task is active the page polls `GET …/state` every 2 s. The task's `mode` and `refs` let a reload, or
  another device, show "Rebuilding the recipe from your text" or "Parsing with AI…" on the right lines.

### 6.7 Queue page

Below capture (§1.1): batches newest first, each with its cards as rows (thumbnail, name or file name, status chip:
"Reading", "Ready · 2 to check", "Failed" with the reason as a caption) and **Retry**, **Cancel**, **Discard** or
**Review**. Per batch: **Review** (opens `review?batch=`), **Retry failed**, a summary ("2 added, 2 still being read, 1
waiting for the monthly limit, 1 failed", "Batch done: …" only when nothing is still being read or waiting for a limit)
and **Add N clean cards** for two or more ready cards with no error or warning. That lists them by title, then commits
them 5 per request with progress ("Adding 10 of 40 cards…"), stops on a refusal, and says what was added and why any
card was left; while the household's recipes are seen without a login it warns that a card set to use or attach its
photo shows it to anyone. **Recently added** lists cards by commit time over 7 days, with **Load older cards**; its
"Added" line has **Undo**. Failed rows say "Tries again on …, or sooner if the limit is raised" or "Removed on …"; a
card that must stay local while nothing local reads cards offers **Read with cloud providers** (its card page's
permission, asking first) and no Retry. **Review** is off while the batch's clean cards are being added. **Recently
added** shows each card under its recipe's name ("Banana Mug Cake (2)"). Over 1,000 open cards, the list says "Showing
the newest 1000 cards." with **Load older cards**.

It polls `GET /api/ai/ingest/jobs?batchId=…` every 3 s only while something is processing or uploading and the page
is visible (the shopping list's pattern), and at once after uploads and commits; while visible it also checks the
shared counts every 20 s and reloads on a change, so it follows changes made elsewhere. Items carry no top-level
`message`. The sidebar count refreshes on focus, on returning to the tab and on route changes, at most every 30 s.
The cards page also shows the reader, limited-features and inbox panels (§1.1, §1.3) and reloads the settings every
minute while visible.

### 6.8 Components

All fork components in `components/Domain/Ingest/` edit the narrow `CardDraft`, never a `Recipe`:

| Component | Does |
|---|---|
| `IngestCapture`, `IngestCapturePhoto`, `IngestPrivacyChip`, `IngestUploadQueue` | capture, the tray, thumbnails, the privacy chip, the upload queue |
| `IngestBatchList`, `IngestBatchListNotice`, `IngestJobListItem`, `IngestInboxStatus` | the queue page, its notices, rows and the inbox panel |
| `IngestCardViewer` | pages, Front/Back, Rotate, zoom through `RecipeImageLightbox` |
| `IngestNeedsALook`, `IngestFlagItem` | the flag list and its one-tap fixes |
| `IngestProposalBanner` | Replace / Add to the end / Dismiss; Use the new reading / Keep mine |
| `IngestRecipeFields` | name, description, yield, servings, times, attribution |
| `IngestIngredientList`, `IngestIngredientRow` | compact card-like rows that expand into an editor without "create" |
| `IngestStepList` | plain title and text fields (upstream's step editor needs a saved slug for images) |
| `IngestNoteList` | notes keyed by id, flagged like steps |
| `IngestOrganizerSelector` | tags, categories and tools, with Create for members who can organize |
| `IngestRegionDialog`, `IngestRegionStencil` | the cropper and its touch- and keyboard-friendly selection |
| `IngestReviewBar`, `IngestTranscription`, `IngestEvalCaseDialog` | the bar with its notice strip; the transcription and its editor; the eval-case dialog with tags and notes |

Reused upstream parts: `RecipeImageLightbox`, `BaseDialog` (F23).

## 7. Commit

`POST …/commit {draftVersion}` runs in the request (FastAPI's threadpool), with no AI; it normally takes under a
second. A body may include the final draft, which is saved first (version check).

The whole commit, from the claim to the finish, runs inside the ingest write lock (§3.9).

1. **Check:** not paused (`503`); `status='ready'`; version matches; no unresolved error flag (`422 {detail: {code:
   "unresolved_flags", flags}}`). Otherwise `409 {detail: {code}}`.
2. **Claim** (committed on its own): `UPDATE … SET status='committing', commit_recipe_id=COALESCE(commit_recipe_id,
   :uuid4), commit_asset_token=COALESCE(commit_asset_token, :token_urlsafe16), committed_by=:user,
   commit_started_at=now, task_state=NULL, task_kind=NULL, lease_token=NULL, task_payload=NULL WHERE id=:id AND
   household_id=:h AND status='ready' AND draft_version=:v`. The recipe id and asset names are **server-owned, fresh
   and persisted first**; no client or draft id is ever used. Clearing the task cancels a pending re-read or
   re-extract (§3.5).
3. **If a recipe with `commit_recipe_id` exists** (`repos.recipes.get_one(id, "id")`), skip to step 6. `create_one`
   commits more than once (F20), so a partial create leaves a row, and creating again would only clash.
4. **Files** into `recipes/<id>/`, once a page turn a stop left half done is settled (§4.4). **The card photo** is
   attached (each `page.jpg` as `assets/recipe-card-<token>-<n>.jpg`) when the draft's `attachCardPhoto` says so; unset,
   it's attached unless the household's new recipes are created public (`recipe_public`, which commit copies into
   `settings.public`; `cardPhotoDefault`). **The cover** (`RecipeDataService(id).write_image(view.jpg of page 1,
   "jpg")`, a portrait card letterboxed to 4:3 on the page's border colour) follows `useCardAsCover` by the same rule
   (`cardCoverDefault`); a cover left by an earlier attempt is deleted when none is wanted now. The directory can't
   belong to another recipe. These are the only directories commit creates, and only under `recipes/<new id>/`.
5. **Build and create** (`draft_to_recipe`):
   - from the draft: name, description, yield and servings, times, ingredients (re-linked per §5; lines kept with a
     marker that no save parsed are parsed around it), steps (title, text), notes with the attribution first (a
     note titled "From", the attribution without a leading "From", by the draft's rule), and markers of **kept**
     flags converted (§4.6);
   - organizers looked up **group-scoped by id**; ones named without an id are found by slug or, for a committer
     who can organize, created; names holding a marker, and unknown ids, are dropped with a warning;
   - `assets` listing the card files when attached ("Recipe card", "Recipe card (back)", `mdi-file-image`), named
     from the stored token, so a resumed commit names them identically;
   - `settings` built from the household preferences as `create_one` would, with `show_assets=True` when the card
     is attached (`public` untouched);
   - the name: if another recipe of the group has it, the first free "Name (n)" up to 1000, picked here while the
     card can still go back to review (upstream's create only tries "(1)" to "(9)"); none free is `422
     commit_invalid` with `fields: ["name"]`;
   - `id` from step 2, `slug=""`;
   - never from the draft: id, slug, assets, settings, rating, extras, owner ids.

   Then `RecipeService(repos, user, household, translator).create_one(recipe)`, with the committer's translator (the
   job's locale when housekeeping resumes it) and `get_repositories(session, group_id=…, household_id=…)`.
6. **Cover key:** `repos.recipes.update_image(slug)` when the cover was written (F21), and upstream's
   `is_ocr_recipe` set (skipped if the column is gone).
7. **Finish:** `UPDATE … SET status='committed', recipe_id=:id, committed_at=now WHERE id=:id AND status='committing'
   AND commit_recipe_id=:id AND commit_started_at=<the lease this caller set>`. Respond `201 {recipeId, slug, name,
   nextJobId, warnings}` (`name`: the name the recipe got, "Name (n)" when its own was taken). **Whichever call wins
   this update** (`rowcount == 1`) publishes upstream's `recipe_created`, after the write lock is released: a request
   through `BackgroundTasks`, as `_publish_recipe_created` does; housekeeping inline in its thread. It holds
   `recipe_event_claimed_at` from the finish and records `recipe_event_sent_at` once the event went out. Housekeeping
   sends it for a committed card whose event wasn't recorded a minute on (waiting 5 minutes on a claim, skipping commits
   over 24 hours old, at most 50 a run), so it's sent **at least once**, even when the process stops between the finish
   and the send. The request's send renews the finish's claim as it starts and sends nothing when the claim is gone
   (housekeeping took over after `RECIPE_EVENT_LEASE`, or the event went out), so a send queued behind slow notifiers
   doesn't repeat it. While a send runs (the request's or housekeeping's), its claim is renewed every third of
   `RECIPE_EVENT_LEASE` (`events.Heartbeat`), so however long the notifiers take, housekeeping doesn't send it again;
   only a process that stops mid-send lets the lease run out.

**Failure and recovery:**
- A validation error **before** `create_one` (the draft no longer validates into a `Recipe`) returns the job to
  `ready` with `commit_invalid` and the field errors, and removes `recipes/<id>` **only if no recipe row has that
  id**. A directory with a row is never deleted.
- A `create_one` that fails without making the recipe also returns the job to `ready`, with `commit_interrupted`
  (`409` for an `IntegrityError`, else the original error), so housekeeping doesn't retry the same failure forever.
  **Once a recipe row with the reserved id exists, the job never goes back to `ready`:** any exception from there
  leaves it `committing` for recovery.
- `commit_started_at` is a lease, and fences every write after the claim: the renewal, the return to `ready` and the
  finish all match the value their caller set. A repeat request, or the dispatcher's housekeeping, wins `UPDATE … SET
  commit_started_at=now WHERE id=:id AND status='committing' AND commit_started_at < now - 120 s` and resumes at
  step 3 as `committed_by`; a committer whose lease was taken over gets `409 invalid_status` and can't undo the new
  owner's work. If `committed_by` is gone the job returns to `ready` with `commit_interrupted` (only possible when no
  recipe row exists). Foods and units created before a crash are found by the exact re-match.
- A double tap gets `409` while `committing` (the page polls) or `200` with the same recipe once `committed`.
- A name collision isn't an error: the recipe becomes "Banana Mug Cake (1)", as the review page said it would.

**Add N clean cards** (`POST /batches/{id}/commit-clean {jobIds, draftVersions}`) commits the listed cards one by
one as above. The claim itself checks the card is ready at the version the page showed with no unresolved error or
warning, so a card that changed meanwhile is skipped with its reason (`not_clean`, `version_conflict`, …); a card
whose commit fails goes back to review.

**Back to review** (`POST …/uncommit {force}`, the committer or a household manager who may also delete the recipe:
its owner or an admin): the guarded update back to `ready` with the draft runs first, committed together with the
recipe's deletion through upstream's own delete (files and event). If the delete fails the card goes back to
`committed`. `409 recipe_edited` when the recipe was edited since, unless `force`; `409 purged` once the card's
photos are gone.

**Assets:** EXIF-free JPEGs named like `recipe-card-Zk3…Q-1.jpg`. The media route has no auth or privacy check (F19), so
the name is the capability. `show_assets` makes the card visible on the recipe; publicity follows the recipe's
`settings.public` and, when read, the household's privacy, which is why photo and cover default to off where new recipes
are created public: such a recipe shows on explore as soon as the household isn't private, now or later. The cover has
the same exposure as any recipe image, which the review page says while the household's recipes are seen without a login
(§6.4).

The row is kept after commit (provenance, the duplicate hash). Its files go after the retention period (§16).

## 8. Events and notifications

```python
class AIEventTypes(Enum):
    recipe_ingestion_ready = "recipe_ingestion_ready"
    recipe_ingestion_rejected = "recipe_ingestion_rejected"  # inbox files that weren't added


class AIEvent(Event):
    event_type: AIEventTypes  # type: ignore[assignment]


class EventIngestionReadyData(EventDocumentDataBase):
    document_type: EventDocumentType = EventDocumentType.generic
    operation: EventOperation = EventOperation.info
    batch_id: UUID4
    job_ids: list[UUID4]  # the cards it tells of, in capture order (a wave: its read and still-waiting cards)
    ready_count: int
    needs_attention_count: int  # ready cards with an unresolved error or warning
    failed_count: int  # not counting cards waiting for a monthly limit
    waiting_count: int = 0  # failed limit_reached, read again automatically
    review_url: str  # BASE_URL + /g/<group-slug>/recipes/cards/review?batch=<id>


class EventIngestionRejectedData(EventDocumentDataBase):  # {count, reasons: {reason: n}, reviewUrl}, no file names
    count: int
    ...
```

- **Opt-in per notifier** in `ai_event_notifier_options` (a side table with a backref cascade on
  `GroupEventNotifierModel`). `AIEventAppriseListener(AppriseEventListener)` keeps the household's enabled notifiers
  whose row is on and sends each AI event type to them, applying upstream's `update_urls_with_event_data`. The message
  is an `EventBusMessage(title, body)` built from the backend `recipe-ingest` namespace in the batch's locale: "Recipe
  cards ready" / "10 cards are ready to review (2 need a look, 1 failed)."; titled "Recipe cards not read" when no card
  is ready ("No cards are ready to review (2 failed)."), or "Recipe cards waiting" when its only cards to tell of wait
  for a monthly limit, all with the same event type, so a Home Assistant automation matches them all. It never goes
  through `EventBusService.dispatch` (F22).
- **Fields are percent-encoded** in the Apprise URL, with `%` escaped first (Apprise's json, form and xml notifiers
  decode a `:key` value twice), and the notifier's own query is left as written. This is a fix in upstream's
  `AppriseEventListener` (a commented hook), so upstream's own events, `recipe_created` included, also reach Home
  Assistant's `from_json` intact; before it, spaces became `+`.
- **Once per batch, at least once per notifier:** a batch's notification is due once it's sealed, none of its cards is
  still processing and one was written in the last 24 hours. `maybe_notify_batch` (called after a first extraction, when
  a batch is sealed, and by housekeeping) claims it with one conditional `UPDATE` that takes a 5-minute lease
  (`notify_claimed_at`) and counts the attempt (`notify_attempts`), so two processes finishing the last two cards at
  once can't both send it. It then sends to each notifier on its own, checks Apprise's answer, and records each notifier
  that got it (a hash in `notify_delivered`, never a URL) before the next send; `notified_at` is set once every notifier
  has it. The lease is renewed every third of it while the sends run (`events.Heartbeat`), so a slow notifier never lets
  housekeeping start the same notification over; every write of an attempt is fenced on its attempt number and its
  lease's start (`notify_claimed_at`). Housekeeping tries a failed notifier, or a process that died part way, again once
  the lease has passed, skipping the notifiers that already have it; after 5 attempts the batch is given up on with an
  error logged. Failed-only batches notify too. A job never joins a sealed batch (§1.4), so a notified batch never gains
  a card, but a card of it that failed `limit_reached` is read again later (§3.6).
- **Cards waiting for a monthly limit** (§3.6) aren't failed: a batch's notification says they wait, never that they
  failed ("2 cards are waiting for the monthly limit. They'll be read when it resets on Nov 1, or sooner if it's
  raised.", after what the batch's other cards came to; `waiting_count`, apart from `failed_count`). The retry queues a
  batch's due cards together (one transaction, one intake lock), arming a wave (`events.arm_limit_wave`): `notified_at`
  and the attempts are cleared and the cards' ids are kept in `notify_delivered` (`limit-reset:<id>` for a card its
  reset queued, `limit-wave:<id>` for one a lift queued). Once none of the batch's cards is still being read, the wave
  goes out as above, at least once per notifier and with the same event type, telling of its cards only: those read,
  then those that still wait with their new date ("1 card that waited for the monthly limit was read. 1 card is ready to
  review. 2 cards still wait for the monthly limit. They'll be read when it resets on Dec 1, or sooner if it's
  raised."). With none read, it's sent ("Recipe cards waiting") only when a card its reset queued waits again; a lift
  that didn't help tells nobody. Cards queued while the batch's notification is being sent don't restart it: they're its
  next wave (`next:<entry>`), started by its last record or by giving up on it, so no notifier hears of a card twice.
- **The 24 hours count from the cards' last activity**, not the batch's creation: a batch read over days still
  notifies, while one whose cards were last written over 24 hours ago (a restored backup's) never does, and
  housekeeping settles it (marked notified, nothing sent), so a later edit doesn't either.
- **Logs** name a failing notifier by id and name, never by its URL, which holds its secrets. Apprise blocks, so it
  runs in the task thread or the dispatcher's thread limiter, with no database transaction open.
- **Counts and a link only**, no names or card text: notifications leave the server.
- **"Recipe cards not added"** (`recipe_ingestion_rejected`) goes to the same notifiers, one per burst of refused
  inbox files (§1.3): "2 recipe cards from the inbox weren't added (1 already scanned, 1 not a supported image).",
  with `document_data` `{count, reasons, reviewUrl}` (the cards page).
- Commit publishes upstream's `recipe_created` at least once (§7), so existing notifiers (and Phase 4's nutrition
  listener) see it.
- **UI:** `HouseholdNotifierAIEvents.vue` adds **Recipe cards ready to review** under each notifier on the
  (advanced-only) notifiers page, saved at once through `GET/PUT /api/ai/notifiers/{id}/events` (a hint says so), with
  **Send test notification** (`POST …/events/test`). The test uses the same event type, titled "Recipe cards ready
  (test)", and is sent whatever the switch says, so it also fires the Home Assistant automation; its result shows
  under the button. A delivery failure is `502 notification_failed`, but only to household or group managers and
  admins; others get `204` whatever happened ("Only household managers are told when one isn't delivered"), so the
  test isn't a reachability probe. When `BASE_URL` is unset or local, the card and the settings card warn that links
  won't open on a phone. The group's Recipe cards settings card links there.

**Home Assistant:** a Mealie notifier with URL `jsons://homeassistant.local:8123/api/webhook/mealie_cards`
(`json://` over plain HTTP) and the box ticked, then:

```yaml
automation:
  - alias: "Mealie: recipe cards ready"
    triggers:
      - trigger: webhook
        webhook_id: mealie_cards
        allowed_methods: [POST]
        local_only: true
    conditions:
      - condition: template
        value_template: "{{ trigger.json.event_type == 'recipe_ingestion_ready' }}"
    actions:
      - variables:
          cards: "{{ trigger.json.document_data | from_json }}"
      - action: notify.mobile_app_kitchen_phone
        data:
          title: "{{ trigger.json.title }}"
          message: "{{ trigger.json.message }}"
          data:
            url: "{{ cards.reviewUrl }}"          # iOS: a tap opens the first card to review
            clickAction: "{{ cards.reviewUrl }}"  # Android
```

`document_data` is a JSON string with camelCase keys. Match on the event name, never a number. A second automation
on the same webhook ID, matching `recipe_ingestion_rejected`, reports refused inbox files (`docs/ai/CARDS.md` has
it). A `rest` sensor on `GET /api/ai/ingest/jobs/counts` (with the HA user's token) gives a "cards waiting"
dashboard tile. All three were run against a real Home Assistant (As built, Verified).

## 9. Permissions and household scoping

| Action | Who |
|---|---|
| Upload (app, API, Shortcut) | any household member, with a Bearer `Authorization` header |
| View, edit, re-read, rotate, re-extract, rebuild, parse lines, commit (alone or the batch's clean cards) | any member of the job's household; the committer owns the recipe |
| Discard | the uploader; any member for inbox cards and cards sent with an API token (Home Assistant, a Shortcut); otherwise `can_manage_household` |
| Merge into another card, read with cloud providers | the uploader or `can_manage_household` (merge: of both cards) |
| Back to review (uncommit) | the committer or `can_manage_household`, who may also delete the recipe as upstream allows (its owner, or an admin) |
| Create foods, tags, categories and tools at commit | `can_organize`; units need nothing |
| Page images | the job's household, through the fork route only (`Cache-Control: private`, `nosniff`; `<img src>` authenticates with the cookie) |
| Card settings | everyone reads; group managers (`checks.can_manage()`) write |
| Notifier toggles and test | the same checks as upstream's notifier routes, with the notifier loaded through the household-scoped repo; a failed test is reported only to household or group managers and admins |
| Eval cases (save, list, edit, download, delete) | group managers, for the cases saved from their household's cards and the ones added by hand; admins see every case |

`IngestRepos` (fork, not in `AllRepositories`) filters every query by the controller's group and household: another
household's job, images included, is a `404`. Group managers in other households don't see your cards. The worker
loads the job's household; if it's gone the task fails `owner_missing`.

## 10. Privacy: local only

**A per-job policy that fails closed.**

- **Setting it:** `recipe_ingestion_settings.local_only` (group managers) makes every card job local-only. Otherwise an
  upload can ask for it (`localOnly=true`, the privacy chip). The job stores `local_only` at intake, so a later settings
  change or provider deletion never loosens a queued job. Each task applies the job's flag **or** the group's current
  setting, and reads the setting again at most every 5 s while it runs (`local_only_check`; a failed read keeps the last
  value), so a switch-on covers re-reads, retries and the next calls of a running task. A card merged into another makes
  it local-only if it was.
- **Lifting it is explicit:** a card that failed `local_only_unavailable` because it was sent local-only, in a group
  that doesn't keep cards local, can be read with any of the group's providers through **Read with cloud
  providers** (`POST …/read-with-cloud`), after a confirmation that its photos leave the network. Nothing else
  lifts it.
- **"Local" means both:** a manager switched on `ai_providers.runs_locally` ("Runs on my network"), **and** the base
  URL is set and every address it resolves to is non-public (`safehttp.transport.is_blocked_ip`: loopback, RFC 1918,
  link-local, CGNAT/Tailscale), cached 60 s. An empty base URL is never local: the OpenAI SDK reads
  `OPENAI_BASE_URL`, and Claude's default is `api.anthropic.com`. The address check alone can't tell a LAN proxy to a
  cloud API from a local model; the flag alone could be a mistake.
- **Enforcement:** `mealie/services/ai/policy.py` keeps `AICallPolicy(local_only, job_id, local_only_check)` in a
  `ContextVar`. The base `AIRuntime.candidates()` is `router.within_limits(apply_policy(slot,
  router.resolve(slot)), slot)`: `apply_policy` filters **every slot** through `is_local_provider` under the policy
  and raises `AIProviderLocalOnlyError` when nothing is left, **before** the monthly limits are applied, so a
  local-only card whose local providers are over their limit fails `limit_reached`, never falls to a cloud provider
  still within its limit. Being in the runtime, it covers every routed call a card causes, including code that
  builds its own `OpenAIService`. `ContextVar`s follow awaits and `asyncio.to_thread`. `EvalAIRuntime.candidates`
  applies it too.
- **Connections are pinned.** Under a local-only policy the SDK clients (OpenAI and Claude) get a fork HTTP client
  (`local.private_http_client`) that, when a connection opens, looks the host up again, refuses it if any address is
  public, and connects to a checked address; TLS SNI, the certificate check and the Host header still use the name.
  It ignores environment proxies. So DNS rebinding between the check and the SDK's own lookup can't reach a public
  address.
- **No leak through the fallback** (F3): with no local image provider the compile step tries `CardOCRCompiler`, whose
  default-slot call is filtered the same way. With no local default provider the job fails `local_only_unavailable`
  before any card text leaves.
- **Pipeline code never passes `provider=`** (that bypasses the runtime). A test runs the pipeline under the policy
  and asserts every call went through `candidates()`. The cross-read uses the same service, so it's filtered too.
- **The eval runs under the policy:** each fixture's run is wrapped in `ai_call_policy(local_only=fixture.local_only
  or --local-only)`, so a `local_only` fixture can't reach a cloud provider even through a mis-set config.
- `record_ai_usage` stores the policy's `job_id` on each usage row (a new nullable column): per-job cost and which
  provider read each card, with no content. The row records the model that answered (the provider's own name for
  it), not only the configured one; the eval reports it too.

**UI:** the provider dialog's **Runs on my network (local model)** switch, disabled with an explanation for an empty
base URL. `GroupRecipeCardSettings` (Group Settings) has **Keep recipe card photos and text on this server**, with
a readiness list for managers: the local providers per image, default and fast slot, a warning when image and
default both lack one, and providers marked local whose address isn't private ("won't be used"). The cards page has
the privacy chip; the review page shows a "Local only" badge (the effective policy, for a card that can still be read
again). Re-reads, re-extracts, rebuilds and parses inherit the job's policy.

**Elsewhere:** metadata is stripped at intake; raw uploads never outlive the request; job images are household-only;
asset names are unguessable; notifications and the voice tool carry counts only (§8, §12); logs carry job ids and
error codes, never card text, transcriptions or provider bodies (compiler failures are recorded with
`describe_provider_error`, never logged with a traceback, §4.1).

## 11. Eval harness

### 11.1 One pipeline

`eval_recipe_cards.py` gains `--pipeline card|import` (default `card`; `import` keeps today's numbers reproducible).
Per fixture, on temporary copies, under `ai_call_policy` (§10): `images.normalize_page` → `pipeline.orient_page`
(when `orientation_available()`, unless `--no-intake-ocr`) → `pipeline.extract_card` with `EvalOpenAIService` and
`options_for_group()` (or `--cross-read`). `extract_card` already runs the normalizer, the matcher (which only
reads) and `compute_flags`, so the eval doesn't repeat them.

Two things differ from production, and only these: pinning (no fallback routes, no usage rows), and
**`read_path`**. A vision config runs with `read_path="image"` and an OCR config with `read_path="ocr"`, so a vision
read that fails is scored as a failure, never as the OCR fallback (F3; today's eval does the same on purpose).
`ExtractionMeta.read_path` is stored on every result. Tests assert that an eval run, `--cross-read` included, leaves
foods, units, recipes and the usage log unchanged and calls only the pinned provider, and that a failed vision read
returns an error, never an OCR result.

### 11.2 Configs

- `--provider VISION[:TEXT]` for mixed setups (e.g. `qwen3-vl:Claude Sonnet`); `--ocr` as now, and `--ocr-provider`
  may repeat, so `OCR+qwen3-vl` and `OCR+Claude Sonnet` run together. Without `--ocr-provider` the OCR config uses the
  group's default provider, as today.
- `--check` validates fixtures without running and doesn't need `--group`, a database or `PRODUCTION` (it imports
  only the fixture models).
- `--dry-run` validates the configs, resolves each one's provider chain in the group (pinned names exist, prices
  known) and the fixtures, lists every problem, calls no provider and writes nothing; it exits 0 or 1.
- Secrets given as `*_FILE` variables (the list `docker/entry.sh` reads) are read before the settings and, as in
  `entry.sh`, win over the plain variable; to override one, clear its file variable too (`-e NAME_FILE= -e NAME=…`).
- `--local-only` refuses non-local providers (the §10 predicate), and a `local_only` fixture is never sent to a
  non-local config. Each config reports a `local` column.
- Ablations: `--cross-read` (on), `--no-intake-ocr` (skip orientation).

### 11.3 Scores

`score_card(expected, extraction)` wraps `score_recipe` (`SCORE_WEIGHTS` unchanged, so v1 and v2 compare) and adds
separate columns:
- `attribution` (fuzzy); `recipe_yield` and times;
- **linking** on matched lines: `food_link_acc`, `unit_link_acc`, `wrong_link_rate`, `new_food_rate`, relative to the
  group's foods by name, plural or alias;
- `step_inventions`: numbers in steps, times or yield that aren't on the card (an invented "2 minutes" scores 0.93
  coverage today);
- `blanks_kept` (expected blanks that came out as `[blank]`) and `blanks_safe` (kept, or flagged with an error);
- **flag calibration:** `LineMatch` gains `expected_index`/`actual_index` and `LineMatches.extra_indices`. Each item
  (line, step, name, time, yield) is right or wrong per the scorer, and highlighted or not per
  `HIGHLIGHTED_SEVERITIES`. Reported: flag recall P(flagged | wrong), flag precision P(wrong | flagged), flag rate,
  **silent errors per card** (the number the review page relies on), and **clean-card precision** P(card fully
  correct | clean), the number **Add N clean cards** (§6.7) relies on;
- per config, **AUROC** of the flags against field errors (a field scored by its highest flag severity, a rank
  statistic) and **cost per caught error** (total cost over errors flagged), each with its counts and a note that
  both are noisy under 50 cards;
- tokens per (provider, slot), the answering model (as the provider names it), prompt SHA-256s, the git commit,
  per-card standard deviation.
- Attribution scoring ignores a leading "From"; `UNIT_ALIASES` knows dozen, envelope and square and their
  abbreviations, checked against the shorthand table.

### 11.4 Decisions fixed in advance

- **Targets** for the chosen default config on the 20 cards: silent errors at most 0.25 per card; flag recall at least
  0.8; on the banana card, `blanks_safe = 1.0` in 3 of 3 repeats. A miss means tuning prompts or flags, not moving a
  threshold. The banana-card target is a Phase 2 release check.
- **Cross-read default:** turned on by default if, with `--cross-read`, silent errors per card drop by at least 0.1,
  or the banana blank is safe in 3 of 3 only with it.
- **D3, Tesseract's two roles:**
  1. *Last-resort reader:* keep the OCR fallback if `LocalVision>OCR+LocalText` rescues at least two cards, or its
     bootstrap interval against `LocalVision` excludes zero; otherwise drop the fallback.
  2. *Orientation:* kept, since nothing else fixes flat-on-the-table shots (F7). `--no-intake-ocr` runs on all
     cards and reports what orientation is worth in accuracy on `sideways` cards, and **wrong turns**: upright cards
     the probe turned. More than one wrong turn in 20 raises `ORIENT_MIN_RATIO`.
- Results and decisions go in `docs/ai/EVAL.md`.

### 11.5 Comparison

`--baseline LABEL`: paired per-card deltas, win/tie/loss and a seeded bootstrap 95% interval. `--chain 'A>B'`: rows
derived from existing results with no extra calls (A's result if A read the card itself, by `read_path`, else B's;
latencies summed). A label that isn't one of the run's configs fails before any call, listing the labels that
exist. Per-tag breakdown from fixture `tags`.

### 11.6 Fixtures

- **Fixture v2** (backward compatible; `schema_version` defaults to 1): `extra="forbid"` on every fixture model;
  `tags` (`handwritten`, `printed`, `sideways`, `two-sided`, `faded`, `blank`), `local_only`, `origin {job_id,
  drafted_by {provider, model}, exported_at, mealie_commit}`; `expected.ingredients: list[str |
  ExpectedIngredient{text, quantity, unit, food, note}]`, `expected.attribution`, `expected.blanks`,
  `expected.recipe_yield`, `expected.times`. `"attribution"` joins `INVENTION_CHECKS`. `--check` validates without
  running, and a CI test loads every committed fixture. The banana fixture's JSON moves to v2 under the same name,
  with structured lines and its blank; its JPEG stays untouched (`tests/unit_tests/services_tests/test_ocr.py` reads
  it).
- **Save as eval case** (review page ⋯, managers): `POST …/jobs/{id}/eval-case {slug, verified, tags, notes}`
  writes `DATA_DIR/groups/<group_id>/eval-cards/<slug>.json` and `<slug>-<n>.jpg`. The slug must match
  `^[a-z0-9][a-z0-9-]{0,63}$` and not exist yet (`409`). The dialog has Handwritten, Printed and Faded chips and a
  Notes field (2,000 characters at most); sideways, two-sided and blank are found from the card. Images are the
  normalized pages **turned back by their recorded rotation**, so orientation is still exercised, and EXIF-free.
  Expected values come from the reviewed draft; a field that had a `[blank]` at extraction keeps it and is listed in
  `blanks`. `verified_by_owner` comes from the tick; `origin.drafted_by` and `origin.household_id` are recorded, and
  the report marks runs scored against the same provider's own drafts. It works for `ready` and `committed` jobs
  until their files are purged (`409 busy` while a running task is turning one of its pages).
- **Managing them:** `GET /api/ai/ingest/eval-cases` lists them, `PUT /eval-cases/{slug}` changes the tags, notes
  and verified tick, `GET /eval-cases/{slug}/download` gives a zip, and `DELETE /eval-cases/{slug}` removes one. The
  group's Recipe cards settings card shows the list with those controls. A manager sees the cases saved from their
  household's cards and the ones added by hand; an admin sees every case. Code generation no longer renames the
  eval card photos. They sit under `groups/`, so they're backed up and restore-safe, and the retention purge never
  touches them.
- **Cleaning a raw photo for the repo:** `python -m mealie.scripts.strip_card_photo IN OUT` removes metadata
  losslessly in pure Python: it keeps only APP2 segments starting `ICC_PROFILE\0` (so the Display P3 profile stays
  and the MPF index goes), drops APP1 (EXIF, XMP), APP13, COM and MPO trailing frames, and writes a minimal EXIF
  holding only Orientation (`--drop-orientation` to leave even that out), so the eval still exercises EXIF transpose.
  No `jpegtran` or `exiftool` is needed. With `jpegtran` (Ubuntu and WSL: `sudo apt install libjpeg-turbo-progs`),
  `jpegtran -copy none -rotate 90 -perfect -outfile OUT IN` reproduces the sideways view the fixture exercises
  (libjpeg-turbo's `jpegtran` takes one input file), and without `-rotate 90` it gives an upright copy (the raw
  pixels are upright, F7). `-copy none` also drops the Display P3
  profile (a slight colour shift); `-copy icc` keeps it but also the stale MPF index, so Pillow then reports a
  2-frame MPO. A re-encoding alternative that keeps the profile and needs only `uv`: `uv run --no-project --with
  pillow python -c "from PIL import Image; im = Image.open('IN.jpg'); im.save('OUT.jpg', quality=95,
  icc_profile=im.info.get('icc_profile'))"`.
- Only one to three redacted cards go into the repo; the 20-card set stays private.

### 11.7 CI stays offline

`StubAI` learns the card schemas; a replay test runs recorded provider JSON for the banana card through the whole
pipeline against golden scores; the DB-unchanged assertion (§11.1). `test-backend.yml` installs `tesseract-ocr`, so
the orientation and OCR tests run in CI; they keep their `skipif` for machines without it.

## 12. MCP and voice

One read tool joins the Phase 1 registry (`tools/ingest.py`, one registry line), so MCP and `/api/ai/tools` both get it:
`recipe_card_queue()` → `{ready, needs_attention, processing, failed, waiting}` with speech like "7 recipe cards are
ready to review. 2 need a closer look and 2 are waiting for the monthly limit." The result keys are snake_case like
every tool's (REST's counts are camelCase); the speech goes through the caller's language, from `recipe-ingest.voice.*`,
with English fallback, and counts stay digits.

- **Counts only:** card names are card text, and the MCP client or HA conversation agent may be cloud-hosted, so
  names never go out, local-only or not.
- Household-scoped `ToolContext`, database work through `run_blocking`, `writes=False`.
- No write tool: tool arguments are generated by the model, which can't reproduce a photo's bytes, voice clients
  have no images, and HA's inbox and REST paths already cover cameras.

## 13. Data model

Three migrations on `970cf50b85f4`, in upstream's hex style: `cc5357be7e71` (the tables and columns below),
`0c2bef734816` (notification delivery, automatic retry and the recipe event columns; it marks cards committed before it
as sent, so old `recipe_created` events aren't sent again) and `0f77cc21b216` (a waiting card's lift backoff, §3.6).
GUID primary keys; JSON stored as `Text` through a fork `JsonText` TypeDecorator (generalizing `ai_mcp.StringList`);
`NaiveDateTime` timestamps; backref cascades declared in the fork model. **No string value the fork generates is one
`uuid.UUID` accepts** (F15): tokens are GUID columns, asset tokens are base64url, hashes are 64 hex characters,
`source_name` carries a `/` prefix (§2), and `integration_id` is display-only. `title` is card text and shares
upstream's exposure for recipe names: a title that is a bare UUID would come back reformatted after a restore, and the
next save rewrites it from the draft.

**`recipe_ingestion_jobs`**

| Column | Type | Notes |
|---|---|---|
| `id` | GUID PK | the job directory name |
| `group_id`, `household_id` | GUID FK, indexed | backref cascades from `Group` and `Household` |
| `batch_id` | GUID FK `recipe_ingestion_batches.id`, indexed | not null; cascade |
| `position` | Integer | capture order within the batch |
| `created_by`, `committed_by` | GUID, nullable, **no FK** | deleting a user is never blocked; the job stays with the household |
| `source` | String(16) | `app`, `api` or `inbox` |
| `source_name` | String(255), nullable | `upload/<sanitized file name>` or `inbox/<group>/<household>/<path>` |
| `integration_id`, `locale` | String, nullable | the API token's integration; `Accept-Language` |
| `local_only` | Boolean | snapshot at intake |
| `status` | String(16), indexed | §3.1 |
| `title` | String(255), nullable | the draft's name, for lists |
| `draft_version`, `extracted_version` | Integer | the client's concurrency token; the version the last extraction wrote |
| `row_version` | Integer | bumped by every read-modify-write of the JSON columns (§3.3) |
| `error_count`, `warning_count` | Integer | unresolved highlighted flags, for lists |
| `task_kind`, `task_state` | String(16), nullable | |
| `task_priority`, `attempts`, `rate_limit_retries` | Integer | |
| `task_payload` | JsonText, nullable | `mode` (§3.1), region and target, the rebuild's text, the lines to parse |
| `not_before`, `lease_expires_at`, `task_started_at` | NaiveDateTime, nullable | |
| `lease_token` | GUID, nullable | new per claim |
| `lease_owner` | String(64), nullable | `host:pid:instance`, for logs |
| `cancel_requested` | Boolean | |
| `progress_key` | String(64), nullable | |
| `pages` | JsonText | `list[PageMeta]` |
| `source_sha256` | String(64) | SHA-256 of the ordered page hashes; duplicates (§2) |
| `transcription` | Text, nullable | |
| `extraction` | JsonText, nullable | `ExtractionMeta`: read path, language, attribution, `unsure`, cross-read lines, OCR confidence, step outcomes, safe compiler errors, answering provider and model, tokens, `pipeline_version` |
| `draft` | JsonText, nullable | `CardDraft`, with its own `schema_version` |
| `flags`, `proposals` | JsonText, nullable | `list[CardFlag]` with resolutions; `list[CardProposal]` |
| `error_code`, `error_params` | String(64) / JsonText, nullable | |
| `commit_recipe_id`, `recipe_id` | GUID, nullable, **no FK** | deleting a recipe is never blocked on PostgreSQL |
| `commit_asset_token` | String(32), nullable | |
| `commit_started_at`, `committed_at` | NaiveDateTime, nullable | |
| `auto_retry_at` | NaiveDateTime, nullable | when a `limit_reached` card is read again (§3.6) |
| `lift_retries`, `lift_retry_at` | Integer (default 0) / NaiveDateTime, nullable | a waiting card's lift backoff (§3.6) |
| `recipe_event_claimed_at`, `recipe_event_sent_at` | NaiveDateTime, nullable | `recipe_created` delivery (§7) |
| `created_at`, `update_at` | `BaseMixins` | |

Indexes: `(task_state, task_priority, created_at)`, `(household_id, status, created_at)`, `(household_id,
source_sha256)`, `(batch_id, position)`, `recipe_id`, `(status, auto_retry_at)`, `(status, recipe_event_sent_at)`.

**`recipe_ingestion_batches`:** `id`; `group_id`, `household_id` (FKs, indexed, cascades); `created_by` (GUID, nullable,
no FK); `source` (String(16)); `source_key` (String(255), nullable: the inbox folder); `locale`; `last_upload_at`,
`sealed_at`, `notified_at`; `notify_claimed_at` (the notification's lease), `notify_attempts` (Integer),
`notify_delivered` (Text: hashes of the notifiers that got it, and a wave's cards as `limit-reset:<id>`,
`limit-wave:<id>` and `next:<entry>`, §8); `BaseMixins`. Index `(household_id, source, sealed_at)`.

**`recipe_ingestion_settings`:** `id`; `group_id` (FK, unique, backref `uselist=False`, cascade); `local_only` (false);
`cross_read` (false); `BaseMixins`. **No row means defaults**; PUT upserts. A fork table, not a column on upstream's
`ai_provider_settings`: a client PUTting those settings without the field would switch local-only off.

**`ai_event_notifier_options`:** `id`; `notifier_id` (FK `group_events_notifiers.id`, unique, backref
`uselist=False`, `cascade="all, delete-orphan"`); `recipe_ingestion_ready` (false); `BaseMixins`.

**New columns:** `ai_providers.runs_locally` (Boolean, not null, `server_default` false, so existing providers fail
closed); `ai_usage_log.job_id` (GUID, nullable, indexed, no FK).

**`look_for_datetime` gains:** `not_before`, `lease_expires_at`, `task_started_at`, `commit_started_at`, `committed_at`,
`last_upload_at`, `sealed_at`, `notified_at`, `notify_claimed_at`, `auto_retry_at`, `recipe_event_claimed_at`,
`recipe_event_sent_at`, `lift_retry_at`.

**Schemas** (`mealie/schema/recipe_ingest/`, codegen → `frontend/app/lib/api/types/recipe-ingest.ts`):
- pages and drafts: `PageMeta`, `PageOCR` (with `OCRLine` boxes), `PageOut`; `CardDraft`, `CardDraftIngredient`,
  `CardDraftStep`, `CardDraftNote`, `CardDraftRef {id: UUID4 | None, name}`; `ExtractionMeta`;
- flags and proposals: `CardFlag {id, kind, severity, source, field, ref, params, alternatives, resolution}`,
  `FlagResolution` (`kept`, `dismissed`); `CardProposal {id, kind: region | full, origin: reextract | rebuild, target,
  text, readable, viaOcr, alternatives, draft, createdAt}`;
- jobs and batches: `RecipeIngestionJobSummary`, `RecipeIngestionJobOut`, `RecipeIngestionJobState`,
  `RecipeIngestionJobTask {kind, state, mode, refs, progressKey, cancelRequested}`, `RecipeIngestionJobPermissions`,
  `RecipeIngestionJobCounts`, `RecipeIngestionBatchOut`; `IngestResponse`, `IngestRejected`;
- requests: `CardDraftUpdate` → `CardDraftSaved`, `RereadRequest`, `RotateRequest`, `RebuildRequest`,
  `ParseLinesRequest`, `MergeRequest`, `UncommitRequest`, `CommitRequest`, `CommitOut`, `BulkCommitRequest` →
  `BulkCommitOut`, `EvalCaseRequest`, `EvalCaseUpdate`, `EvalCaseOut`, `EvalCaseSummary`, `RegionHintOut`;
- settings: `RecipeIngestionSettingsOut/Update` (with `IngestLimits`, `ReaderInfo`, `LocalReadiness`,
  `IngestInboxInfo`, `IngestInboxRejection`), `AINotifierEventsOut/Update`, `IngestAbout`;
- enums: `IngestStatus`, `IngestSource`, `IngestTaskKind`, `IngestTaskMode`, `IngestTaskState`, `IngestErrorCode`,
  `IngestRejectReason`, `CardFlagKind`, `CardFlagSeverity`, `CardFlagSource`, `CardProposalOrigin`,
  `PageRotationSource` (`none`, `ocr`, `user`, `model`), `IngestReadPath` (`image`, `ocr`), `EvalCaseTag`,
  `RegionHintSource`, `IngestLimitedFeature` (`suggestions`, `cross_read`), `InboxWaitingReason` (`cannot_read`,
  `local_only_unavailable`, `quota`).
  - `IngestErrorCode` (14): `ai_not_enabled`, `local_only_unavailable`, `limit_reached`, `rate_limited`,
    `provider_failed`, `no_recipe_found`, `files_missing`, `owner_missing`, `interrupted`, `cancelled`, `timeout`,
    `internal_error`, `commit_invalid`, `commit_interrupted`.
  - `CardFlagKind` (20): `illegible`, `blank`, `missing_name`, `unsure`, `not_on_card`, `marker_dropped`,
    `read_disagreement`, `check_parse`, `unit_unclear`, `linked_fuzzy`, `implausible_amount`,
    `implausible_temperature`, `empty_section`, `read_by_ocr`, `cross_read_failed`, `organizers_skipped`,
    `shorthand_read`, `not_parsed`, `new_food`, `new_unit`.
  - `CardFlagSource`: `marker`, `model`, `validator`, `parser`, `ocr`, `cross_read`.
  - `IngestRejectReason`: `too_large`, `unsupported_format`, `pdf_not_supported`, `too_many_pixels`,
    `unreadable_image`, `too_many_pages`, `duplicate`, `url_not_allowed`, `url_fetch_failed`, `no_permission`,
    `quota`.

**Drafts survive upstream syncs:** `CardDraft` is the fork's own model, read leniently (`extra="ignore"`, migrations
keyed by `schema_version`) and turned into an upstream `Recipe` only at commit, where a validation error is shown to
the user.

```
CardDraft { schemaVersion: 3, name, description, recipeYield?, recipeYieldQuantity?, recipeServings?,
  prepTime?, performTime?, totalTime?, attribution?, useCardAsCover?, attachCardPhoto?,
  ingredients: [{referenceId, title?, originalText, quantity?, unit?: {id?, name}, food?: {id?, name}, note,
                 display, parseConfidence?, extractedHash?}],
  steps: [{id, title?, text}], notes: [{id, title, text}],
  tags | categories | tools: [{id?, name}] }
```

Version 2 gave notes an `id` (a stored version-1 draft reads with the same ids every time). `useCardAsCover` and
`attachCardPhoto` unset mean the household's default (§7). Version 3 made `useCardAsCover` optional: versions 1 and 2
stored `true` on every draft, so a `true` from them, or from a draft sent without `schemaVersion` (read as version 1),
reads as unset. A client sets it with `schemaVersion: 3`. `CardDraftUpdate` also takes `clientDraftSchema`, the draft
schema the page was built for (the review page sends 3). A save without it, or below 3, comes from a page loaded before
version 3, whose build set `useCardAsCover: true` on every draft, so that `true` is stored unset. A commit's draft is
read as the current schema.

## 14. API surface

Under `/api/ai`, household-scoped, camelCase. Every route answers `503 paused_for_restore` during a restore (§3.9), and
a restore waits for a route already writing (files or the database).

| Method | Path | Body → response |
|---|---|---|
| `POST` | `/ingest` | §1.2 → `202 IngestResponse`; every error also carries a top-level `summary` |
| `POST` | `/ingest/batches` | → `201 RecipeIngestionBatchOut` |
| `POST` | `/ingest/batches/{id}/seal` | → `200` |
| `POST` | `/ingest/batches/{id}/touch` | → `200` (the capture page's heartbeat) · `409 {detail: {code: "batch_sealed"}}` · `404` for anything but the caller's app batch |
| `GET` | `/ingest/batches/{id}` | → batch with counts and its jobs `{id, position, status, errorCount, warningCount}` in order |
| `POST` | `/ingest/batches/{id}/commit-clean` | `{jobIds, draftVersions}` → `200 {committed, skipped: [{jobId, code}]}` |
| `GET` | `/ingest/jobs?status=&batchId=&committedSince=&orderBy=committedAt&page=&perPage=` | → paginated `RecipeIngestionJobSummary {id, batchId, position, status, source, sourceName, title, pageCount, thumbUrl, draftVersion, errorCount, warningCount, task, error, recipe, localOnly, canDiscard, createdAt, committedAt, autoRetryAt, expiresAt, householdRecipesPublic}`; `perPage=-1` gives all; the pagination keys stay snake_case, like upstream's |
| `GET` | `/ingest/jobs/counts` | → `{processing, ready, needsAttention, failed, waiting}` (`waiting`: cards waiting for a monthly limit, not counted in `failed`) |
| `GET` | `/ingest/jobs/{id}` | → `RecipeIngestionJobOut` (summary + `pages`, `transcription`, `read`, `draft`, `flags`, `proposals`, `permissions {canCreateFoods, canCreateOrganizers, canDiscard, canExportEval, canReadWithCloud, canUncommit, canMerge}`, `duplicateOf`, `duplicateJob`, `duplicateName`, `cardPhotoDefault`, `cardCoverDefault`) |
| `GET` | `/ingest/jobs/{id}/state` | → `{draftVersion, status, task {kind, state, mode, refs, progressKey, cancelRequested}, proposalIds, error}` |
| `PUT` | `/ingest/jobs/{id}` | `{draftVersion, draft, flagResolutions, resolvedProposalIds, clearError, clientDraftSchema}` → `{draftVersion, flags, errorCount, warningCount, ingredients, duplicateOf, duplicateJob, duplicateName}` · `409 {detail: {code: "version_conflict", current}}` |
| `POST` | `/ingest/jobs/{id}/reextract` · `/reread` · `/retry` · `/rebuild {transcription}` · `/parse-lines {refs}` | → `202 RecipeIngestionJobState` · `409 {detail: {code: "busy"}}` · `422` (`unknown_target` for a line the draft hasn't) |
| `POST` | `/ingest/jobs/{id}/read-with-cloud` | → `202` · `403` · `409 group_local_only` or `invalid_status` |
| `POST` | `/ingest/jobs/{id}/merge` | `{intoJobId}` → `202` (the other card's state) · `409 too_many_pages {max}` · `409 busy` |
| `POST` | `/ingest/jobs/{id}/cancel` | → `200` |
| `POST` | `/ingest/jobs/{id}/pages/{n}/rotate` | `{degrees: 90 \| 180 \| 270}` → `200 PageOut` · `409` while a task is active |
| `GET` | `/ingest/jobs/{id}/region-hint?field=&ref=` | → `RegionHintOut {page, x, y, width, height, source: ocr \| position}` · `404 not_found` |
| `GET` | `/ingest/jobs/{id}/pages/{n}/{view\|thumb\|page}` | → JPEG/WebP, ETag includes the rotation; `no-store` while a turn may still be swapping in |
| `POST` | `/ingest/jobs/{id}/commit` | `{draftVersion, draft?}` → `201 {recipeId, slug, name, nextJobId, warnings}` · `200` if done · `409 {detail: {code}}` · `422 {detail: {code: "unresolved_flags", flags}}` |
| `POST` | `/ingest/jobs/{id}/uncommit` | `{force}` → `200 RecipeIngestionJobState` · `409 recipe_edited` · `409 purged` · `403` |
| `DELETE` | `/ingest/jobs/{id}` | → `204` |
| `POST` | `/ingest/jobs/{id}/eval-case` | `{slug, verified, tags, notes}` → `201 {slug, files}` · `409` exists, not exportable, files missing or busy |
| `GET` | `/ingest/eval-cases` | → `[{slug, name, pageCount, verified, tags, notes, createdAt}]` (managers) |
| `PUT` | `/ingest/eval-cases/{slug}` | `{verified, tags, notes}` → `200` (managers) |
| `GET` | `/ingest/eval-cases/{slug}/download` | → a zip of the case (managers) |
| `DELETE` | `/ingest/eval-cases/{slug}` | → `204` (managers) |
| `GET`/`PUT` | `/ingest/settings` | `{enabled, localOnly, crossRead, canReadCards, limitReached, limitedFeatures, baseUrlSet, readerRunning, ocrAvailable, reader {name, local, viaOcr} \| null, localOnlyAvailable, localReadiness (managers), limits {maxUploadBytes, maxFileBytes, maxImagesPerRequest, maxPagesPerCard, maxPixels, maxJpegPixels}, inbox {enabled, folder, waiting, waitingReason, rejections}}`; `200` with `enabled: false` when ingestion is off · PUT `{localOnly, crossRead}` (managers) |
| `GET`/`PUT` | `/notifiers/{notifierId}/events` | `{recipeIngestionReady}` |
| `POST` | `/notifiers/{notifierId}/events/test` | → `204` · `502 notification_failed` (managers and admins only) |
| `GET` | `/about` | public: `{version, features: {ingest: {enabled, maxUploadBytes, maxImagesPerRequest, maxPagesPerCard, inbox, worker}, mcp: true}}` (plan §10); `worker` is true when a dispatcher marked itself running within 180 s |

Route order: `/ingest/jobs/counts` is declared before `/ingest/jobs/{id}`. Frontend: `RecipeIngestAPI`
(`lib/api/user/recipe-ingest.ts`) as `useUserApi().recipeIngest`.

**Error bodies:** errors the page handles itself (`version_conflict`, `busy`, `unresolved_flags`, `batch_sealed`,
`not_found` for a region hint) carry a `code` and no `message`, because the axios interceptor toasts any
`detail.message` (F18). Errors it doesn't handle (`503`, `429`, `413`, `415`, `401`) carry a translated `message`,
which the interceptor toasts and Shortcuts can show; every server-side message falls back to en-US
(`ingest/i18n.py`). `reader` is the first provider the card's read path would use under the group's policy (`viaOcr`
when that's the OCR fallback), so every member's privacy chip has data (§1.1). `limitReached` is true when every
provider the group's reading would use is over its monthly limit (uploads are still accepted; the capture page and
settings card warn). `baseUrlSet` is false for the default, `localhost`, loopback addresses and `0.0.0.0`.

## 15. Settings and environment

A fork `IngestSettings` (`pydantic_settings.BaseSettings`, `env_prefix="AI_INGEST_"`, in
`mealie/services/ai/ingest/settings.py`, cached by `get_ingest_settings()`). `mealie/core/config.py` already loads
`.env` into the environment, so upstream's `AppSettings` is untouched. An empty variable counts as unset (Unraid and
compose files pass unused ones as `''`).

| Variable | Default | Meaning |
|---|---|---|
| `AI_INGEST_ENABLED` | `true` | off: ingest routes answer `503` (the settings answer says `enabled: false`), no dispatcher |
| `AI_INGEST_WORKER` | `true` (`false` under `TESTING`) | run the dispatcher in this process; off with ingestion on logs a warning |
| `AI_INGEST_CONCURRENCY` | `2` | task threads per worker process (plus one re-read slot) |
| `AI_INGEST_GROUP_CONCURRENCY` | `0` (no cap) | at most this many cards of one group read at once, across processes (§3.2) |
| `AI_INGEST_MAX_UPLOAD_MB` | `100` | per request (JSON bodies are capped at 45 MiB regardless) |
| `AI_INGEST_MAX_PROCESSING_PER_USER` | `0` (no cap) | an uploader's cards waiting to be read; more are refused (§1.2) |
| `AI_INGEST_RETENTION_DAYS` | `14` | §16 |
| `AI_INGEST_ORIENT` | `true` | turn sideways cards upright with Tesseract when it's installed, whatever `OCR_ENABLED` says |
| `AI_INGEST_INBOX_DIR` | unset (`/inbox` when a folder is mounted there) | outside `DATA_DIR` and `/app` |
| `AI_INGEST_INBOX_POLL_SECONDS` | `30` | |
| `AI_INGEST_INBOX_KEEP_PROCESSED` | `true` | move to `processed/` rather than delete |
| `AI_INGEST_INBOX_PROCESSED_DAYS` | unset | delete photos from `processed/` this many days after they were read (once a day) |
| `AI_INGEST_INBOX_DIR_MODE` | `2775` | octal mode of the inbox folders Mealie creates; an invalid value is logged and 2775 used |
| `AI_INGEST_LOCK_DIR` | unset | where the ingest write lock lives, for a `DATA_DIR` without file locks (§3.9) |
| `AI_INGEST_URL_FETCH` | `false` | accept image URLs in the upload API's JSON (§1.2) |
| `AI_INGEST_URL_ALLOW_HOSTS` | empty | hosts, addresses or CIDR ranges image URLs may reach though private (Home Assistant's), on top of `HTTP_ALLOW_LIST` |
| `AI_INGEST_URL_TIMEOUT` | `20` | seconds one image URL's download may take in all (1-300) |
| `AI_INGEST_PDF_CPU_SECONDS` | `20` | CPU seconds one PDF may take to render; wall clock 1.5x (1-600) |
| `AI_INGEST_PDF_UNCONFINED` | `false` | render PDFs where the renderer's seccomp filter can't apply (another architecture, no seccomp), with only the protections that do (§2) |

Code constants in `limits.py` (tests patch them): poll 5 s, lease 120 s, heartbeat 20 s, deadline 30 min, 3 attempts, 6
rate-limit retries, housekeeping 60 s, commit lease 120 s, app batch idle 10 min, auto batch idle 2 min, notification
cutoff 24 h after the cards' last activity, notification lease 5 min and 5 attempts, local-only recheck 5 s, limit
recheck 10 min, pause marker refreshed every 60 s and honoured for 5 min, restore lock wait 45 s, 4 guard threads, kept
results 24 h (waited for 5 min), inbox settle 10 s, claim retry 10 min and 20 files a tick, 2 intakes and 1 PDF render
(one card per group waiting for it), 4 resolver threads for image URLs, 4 dispatcher DB threads and 1 re-read slot per
process, empty batches 24 h, orphan folders 1 h, presence every 60 s, `ORIENT_MIN_RATIO` 1.5, and the §1.5 limits.

`docs/ai/DEPLOY.md` documents these settings, the `/inbox` mount (also in `docker-compose.ai.yml` and the Unraid
template, where mapping the folder is enough), per-household mounts and folder permissions, nginx
`client_max_body_size 100m`, `OLLAMA_CONTEXT_LENGTH` ≥ 16384 (two 2048 px pages overflow Ollama's 4k default on
small GPUs), the PgBouncer note for the migration lock (§17), and that Tesseract (`INSTALL_OCR=true`) is the
reliable way to turn flat shots upright.

## 16. Retention and cleanup

The purge (`retention.purge_once`) runs from the dispatcher once a day per process, first 10 minutes after boot. It's
idempotent, so running in every worker is harmless, and it needs no `app.py` or scheduler hook.
- **Committed**, older than `AI_INGEST_RETENTION_DAYS`: first clear `transcription`, `draft`, `flags`, `proposals`,
  `extraction` and `title` in an update guarded on the row's version, keeping the row (recipe link, duplicate hash);
  then delete the directory, only if that update matched (so a card that just went back to review keeps its
  photos). A removal that fails is retried by the orphan purge.
- **Failed**, older than the retention counted from `coalesce(auto_retry_at, update_at, created_at)`: delete row and
  directory, under the household's merge lock. A card waiting for a monthly limit is kept past its retry, and
  failed rows show their removal date.
- Every folder the purge removes (a slimmed committed card's, a failed card's, an orphan) first settles a merge into its
  card that a stop left half done (`review.settle_merges_into`), so the other card keeps its pages.
- **Ready** jobs are never purged (unreviewed cards); the queue shows their age.
- **Discard** deletes row and directory at once. **Committing** jobs are never touched. Each purged job's file work
  runs in the write lock (§3.9).
- **Empty batches**, sealed or not, a day after their last upload; **orphan directories** under
  `groups/*/ai-ingest/` with no row, older than an hour (a crash before insert, discard racing a worker, a restore
  mismatch); **kept results** (§3.9) older than 24 hours.
- **Eval cases** stay until a manager deletes them. The inbox's `processed/` is kept unless
  `AI_INGEST_INBOX_PROCESSED_DAYS` is set (§1.3); the rest of the inbox is the user's.
- Nothing touches `recipes/` except a commit and an uncommit.

## 17. Backup, restore and upgrades

**Backup and restore:**
- The tables follow every exporter rule (F15). A round-trip test (like `test_backup_mcp_oauth.py`) runs on SQLite and
  PostgreSQL with jobs in every state, JSON holding dashed UUIDs and `created_at` keys, and a UUID-named upload in
  `source_name`.
- Job files and eval cases are under `groups/`: a backup holds rows and files from the same moment, and a restore
  never meets a missing top-level directory.
- The inbox is outside `DATA_DIR`: never backed up, wiped or re-ingested by a restore. The duplicate hash stops
  re-ingestion either way.
- **Runtime files stay out of backups:** `BackupV2.RUNTIME_FILES` and `RUNTIME_DIRS` leave out the write lock, the
  pause marker and its temporary files, the restore lock, the presence file, the migration lock, the kept results
  (`.ai-ingest-results/`) and the inbox state (`.ai-ingest-inbox/`). A restore of an older backup holding them
  leaves the live ones alone.
- During a restore, every API request gets `503` and the restore waits for writes in progress (§3.9). Afterwards, every
  `running` row is already queued again (a kept result is applied without a second provider call), `committing` rows
  resume against the restored database, the notification rules prevent repeat notifications (§8), and a marker left by a
  crash mid-restore is removed at once.

**Upgrade and migration:**
- Three revisions: `cc5357be7e71` (`down_revision = "970cf50b85f4"`: four tables and two columns, with
  `batch_alter_table`), `0c2bef734816` and `0f77cc21b216` (the columns and indexes of §13). Downgrade drops them. A
  merge revision at the next upstream sync (plan §10).
- **Migrations run under a lock** (`mealie/db/migration_lock.py`, a hook in `init_db.main`), so several workers can
  start a fresh install together: an exclusive `flock` on `DATA_DIR/.mealie-migrate.lock` for SQLite; for
  PostgreSQL a polled transaction-level advisory lock on a connection of its own, which works behind PgBouncer in
  session or transaction mode (PgBouncer's `idle_transaction_timeout` must be off or longer than a migration). The
  kind of lock follows the database being migrated. A waiting worker waits as long as the migration runs and logs
  every minute. Processes starting together on a new `DATA_DIR` also agree on one secret.
- **A restore holds the migration lock** from before it drops the tables until the restored database is migrated and
  seeded (`holds_migrations` on `BackupV2.restore`; the lock is re-entrant in the thread holding it, so the restore's
  own `init_db.main` doesn't wait for itself). A worker starting mid-restore (a uvicorn restart, a second container)
  waits and finds the restored database at head. A restore asked for while another process migrates waits 10 s, then
  answers 503 "Mealie is updating its database. Try the restore again in a minute." before anything changed.
- **Work in flight:** shutdown releases leases and the new version continues. Drafts carry `schema_version` and are
  migrated on read; `extraction.pipeline_version` records which pipeline produced a draft.
- No backfill: `/create/ai` recipes are untouched; old `add-ocr-recipe` databases are repaired by `fork_compat.py`.

## 18. Testing

**Backend** (offline; `tests/unit_tests/services_tests/ai/ingest/`, `tests/integration_tests/ai_tests/ingest/`;
SQLite and PostgreSQL):
- **Intake:** a tiny generated HEIC with orientation 6 and GPS comes out upright with no EXIF; MPO frame 0; truncated
  JPEG and damaged, empty or password-protected PDF rejected; PDF and multi-page TIFF pages; the PDF renderer's sandbox
  (no file outside its input, no socket); pixel cap before `load()`; magic bytes over extension; thumbnail aspect kept;
  a `SpooledTemporaryFile` and a `BytesIO` as input; duplicates by the ordered page hashes (a front re-sent with its
  back isn't one); nothing left in `DATA_DIR` after a failed request; no file under `groups/` carries GPS after intake
  returns.
- **Upload API:** an unauthenticated 5 MB upload gets `401` with zero body bytes read; cookie-only auth gets `401`;
  `413` by header and mid-stream; a 46 MiB JSON body gets `413`; `415`; the three shapes; partial success; `429`;
  `split`; `503` while paused; `503` and no directory when the marker appears after the body was read; the pre-body
  checks run off the event loop; auto-join within 2 minutes, `batchId=new`, and a sealed batch starting a new one;
  a seal racing an insert in two sessions never leaves a job in a sealed batch; error bodies carry `code`, and
  `message` only where §14 says.
- **Runner:** concurrent claims by threads with separate sessions are unique; expiry requeues; three expiries fail a
  `processing` job and leave a `ready` one ready with a banner; a new task resets `attempts`; fencing drops stale
  results; heartbeat; cancel queued and running; a cleared token cancels its task; deadline; rate-limit backoff;
  shutdown release; the re-read slot runs a re-read while both extraction slots are busy; pause (no claims,
  heartbeats or inbox; a task's result waits and is released without burning an attempt); `FileNotFoundError`
  outside a pause fails `files_missing`; a phase that raises doesn't stop the dispatcher; the lifespan entered with
  `init_db.main` and `start_scheduler` patched (Phase 3 precedent); no database work on the event loop; the base
  `AIRuntime` on a request session is unchanged; time comparisons correct on PostgreSQL with a non-UTC session
  `TimeZone`.
- **Concurrency on SQLite and PostgreSQL:** two threads saving the same `draftVersion` give one `200` and one `409`;
  a finalize choosing between replace and proposal while a save lands loses neither; a proposal added during a save
  survives.
- **Pipeline** (a fake AI at `_get_raw_response` or the runtime): the banana card through the real
  `CompileSourceStep` and steps; card compilers return `OpenAICompiledSource`; markers survive `cleaner.clean`; every
  flag kind; cross-read alignment flags an invented "2" when the second read has `[blank]`, runs concurrently, and
  goes through the same service; the OCR path with Tesseract patched; a 429 from the image provider with Tesseract
  available requeues and never calls the OCR compiler; no compiler failure logs a traceback; `read_path` selects the
  compilers; no organizer call when the group has none, and suggestions write nothing; `extract_card` writes
  nothing; no open transaction at any provider await, including a fallback where provider 1 raises and provider 2 is
  awaited; `orient_page` keeps an upright card upright and turns a sideways one (the 1.5× margin), with Tesseract
  where installed.
- **Ingredients:** the shorthand table (banana lines, "TB.", "don't", "t-bone"), linking, `original_text`, `display`,
  blank lines, marker lines left unparsed.
- **Privacy:** every slot filtered; fail closed through the OCR fallback; empty URL, public IP and flag-off cases; no
  `provider=`; `job_id` on usage rows; the voice tool returns counts only.
- **Commit:** double commit; a crash injected after each step and resumed (no duplicate recipe, foods or asset
  names); a recipe row left by a partial `create_one` is reused, never recreated; foreign food, unit and organizer
  ids dropped; whitelist; `show_assets` on and `public` from the household; the photo and cover defaults in public
  and private households; cover key; asset files EXIF-free with the stored token; Keep-as-written conversions; a
  pending re-read cancelled; `recipe_created` sent once, and again after a crash between the finish and the send; a
  taken name and every "(n)" taken; undo, merge and the batch's clean cards, including their crash and race cases.
- **Review:** `409`; a task's proposal doesn't conflict with a save; a re-extract replaces an unedited draft and
  proposes on an edited one; errors block commit until kept; `404` across households for every route and image;
  discard permissions.
- **Events:** only flagged notifiers; Apprise URL params read back through Apprise; one notification when two jobs
  finish together; a failed notifier retried and the others not sent twice; 5 attempts; auto-seal; failed-only
  batches; batches whose cards were last written over 24 h ago never notify; the "not added" event; the test
  notification and its 502 for managers only; never via `EventBusService.dispatch`.
- **Inbox:** two scanners on one file; settle; symlinks, dotfiles and temp names; a symlink swapped in after the
  claim; subfolders; `processed/` and `failed/`; crash after insert; a claimed file whose mtime is a month old isn't
  retried by a second scanner; refusal inside `DATA_DIR`.
- **Restore:** a restore with jobs queued and the dispatcher running completes, the dispatcher survives with no
  traceback, and the marker is removed even when the restore raises; a restore waits for a writer holding the lock, and
  gives up cleanly after the wait limit; a write section refused while the lock is held exclusively; a process whose
  `init_db.main` starts while the tables are dropped waits and finds the restored database at head with no extra seed
  (SQLite and PostgreSQL); every `/api/` request gets 503 before any database access while paused; no request body is
  copied (a 404 reads none, an upload is spooled once), and no slow body, signed in or not, holds a restore off.
- **Data:** backup round trip, migration up and down, schema limits (`RESPONSE_SCHEMAS` includes the card schemas),
  eval (§11), `strip_card_photo` (output opens as a 1-frame JPEG with ICC, no GPS, and identical pixels).

**Frontend** (vitest, stubbed Vuetify): pure helpers (`groupPhotosIntoCards`, `regionFromCropResult`,
`firstCardToReview`, `nextCardInBatch`, `flagsForField`, `applyAlternative`, `fillBlank`); the upload queue
(pairing, retry, 413 re-encode, duplicate shown as Already scanned, concurrency, adopting a new `batchId`, Done
sealing only after the batch's uploads drain); the review page (flags panel and one-tap fixes, proposals, the
`version_conflict` reload, commit disabled by errors, the re-read queue); the settings and notifier components.

**PostgreSQL locally:** a private cluster, for example:

```bash
PGBIN=/usr/lib/postgresql/16/bin; PGD=$(runuser -u postgres -- mktemp -d); PORT=55471
runuser -u postgres -- $PGBIN/initdb -D $PGD/data -U mealie --auth=trust -E UTF8 --locale=C.UTF-8
runuser -u postgres -- $PGBIN/pg_ctl -D $PGD/data -o "-p $PORT -k $PGD -c listen_addresses=127.0.0.1" -l $PGD/log -w start
runuser -u postgres -- $PGBIN/createdb -h 127.0.0.1 -p $PORT -U mealie mealie
DB_ENGINE=postgres POSTGRES_SERVER=127.0.0.1 POSTGRES_PORT=$PORT PYTEST_XDIST_WORKER=pg1 \
  uv run --frozen pytest -p no:cacheprovider -q tests/…
runuser -u postgres -- $PGBIN/pg_ctl -D $PGD/data -m fast stop && rm -rf $PGD
```

**Frontend checks in CI:** `pnpm lint`, `pnpm typecheck:fork` (vue-tsc, failing only on errors in fork-owned
files) and the vitest suite, which also fails if a fork `.vue` file uses Vuetify 3 typography classes (Vuetify 4
doesn't define them, so captions rendered at body size). `test_code_generation_exports.py` checks that generating
the exports again changes nothing and that eval card photos are never renamed.

**Outside CI:** a production-mode Mealie with a stub provider, driven in Chromium at 375 px and on desktop; a
restore while a batch is processing; two workers (`UVICORN_WORKERS=2`) for duplicate claims and notifications; the
HA automation, sensor, snapshot script and `shell_command` on a real HA 2026.10; the inbox with other users' files
and modes. Left for the user: the 10-card batch on a real iPhone, and the eval against real providers.

## 19. Exit criteria walkthrough

**20 cards scored per provider** (`docs/ai/EVAL.md`, "Runbook: 20 cards scored per provider", has the exact
commands):
1. Review cards from the box on the phone, as below, and tap ⋯ → **Save as eval case** on each (at least 5 printed,
   5 faded or pencil, some two-sided, some sideways), with the Handwritten, Printed or Faded chips. Tick "I checked
   the recipe against the card" after checking the draft against the card.
2. Check the set and the configs first, with no provider call: `python -m mealie.scripts.eval_recipe_cards --check
   --cards …` and the full command with `--dry-run`.
3. Run `python -m mealie.scripts.eval_recipe_cards --group home --cards /app/data/groups/<group-id>/eval-cards
   --provider "Claude Sonnet" --provider "Gemini Flash" --provider qwen3-vl --provider "qwen3-vl:Claude Sonnet"
   --ocr --ocr-provider qwen3-vl --chain "qwen3-vl>OCR+qwen3-vl" --repeat 3 --price …`, then again with
   `--cross-read` and with `--no-intake-ocr` (each with `--reference` to the first run's results).
4. Record the targets (§11.4), the cross-read default and D3 in `docs/ai/EVAL.md`.

**A 10-card batch from a phone:**
1. Open **Recipe cards**, turn on **Front & back**, and shoot 10 cards (about 3 taps a side in the app, or the Camera
   app plus one multi-select). Cards upload and are read while you shoot; with concurrency 2 and about 25 s a card,
   most are ready when you tap **Done**.
2. The HA notification "10 cards are ready to review (2 need a look)" → tap → the first card to review.
3. Clean cards: **Commit & next**, one tap, or **Add N clean cards** for all of them on the cards page. Flagged
   ones: about two taps a flag, such as typing "2" into the banana card's blank, or **Keep as written**.

## Out of scope

- **Reading:** model bounding boxes. Providers differ in their format and accuracy; region hints come from
  Tesseract's line boxes and the transcription instead (§4.7).
- **Parsing:** the brute ingredient parser, which links "pkg." to kilogram.
- **Eval:** region-level scoring (noise at n=20).
- The plan's other items that weren't built are listed, each with its reason, under As built, "Not built, by
  decision".

## Known limitations

Only what can't be removed is listed, with the reason.

- **A provider call may run twice after a process is frozen past its lease** (a VM pause, `SIGSTOP`). A frozen
  process can't be told from a dead one without exactly-once calls from the provider, so the task is claimed again.
  Fencing applies one result (§3.3), and the frozen task is cancelled at its next heartbeat.
- **A notification, or `recipe_created`, can reach a notifier twice** if the process dies between sending it and
  recording the delivery. Apprise and HTTP can't make the send and the record one step, so delivery is at least
  once rather than at most once (§7, §8).
- **Anyone who can write to a household's inbox folder can queue cards for it.** A file on a share carries no
  identity, so the folder is the identity. Mounting each household's folder on its own limits a device to its
  household, and every card is reviewed before it becomes a recipe (§1.3).
- **Where `DATA_DIR` has no file locks, another host's dead restore keeps ingestion paused for up to 5 minutes.**
  Nothing on this host can tell whether a process on another host is alive, so its marker is honoured until it's 5
  minutes old (§3.9).
- **Where the browser can't store photos** (a full disk, some private windows), the upload queue lives in memory, so a
  reload loses photos not yet uploaded. The page says so, and the queue stays in that tab rather than moving to another
  one.
- **The PDF sandbox needs Linux with seccomp on x86-64 or arm64.** Elsewhere (another architecture, a kernel or
  container without seccomp) PDFs are refused `pdf_not_supported` unless `AI_INGEST_PDF_UNCONFINED=true`, which renders
  them with only what applies (Landlock where the kernel has it, an isolated interpreter, a time limit, no file writes,
  capped memory and CPU); the log says which protections apply (§2). Without Landlock the filter refuses opening any
  file, so PDFium draws fonts a PDF doesn't embed with its own. Run unconfined as root, a process the renderer starts
  can leave its process group and outlive the time limit. The server closes its end of the renderer's output at the time
  limit plus 5 s, so no server thread or pipe stays with it, but that process is left running.

### Found in the last review, not fixed

A seventh review round was stopped before its findings were fixed. It found:

**Re-read hints and parsing**
- A second ingredient starting with a word like "old", "long" or "minute" ("1 c. old-fashioned oats") goes unflagged.
- Hedge words ("about", "a few") and words the matcher drops ("red" in "red food coloring") are lost from the note.
- "1/2 & 1/2" (half-and-half) is read as a junk food name.
- A long line the card says twice gets the first one's re-read hint; only short lines are told apart.
- A kept marker between the amount and the unit raises a false `check_parse`.
- Some equivalent-measure forms are still flagged, and some are accepted too loosely.
- An oven line without a number ("Moderate oven") and "Serves 8" among the ingredients aren't flagged.
- The review page shows the generic check text for `check_parse` flags with `not_ingredient` or `too_long`.

**Upload queue and cards page**
- Over plain http, a front waiting for its back lands in the tray after the phone kills the tab and it's reloaded later.
- **Use this tab** can be refused while a restored front waits in a tab with no camera.
- Cards waiting for the monthly limit still show a red "Failed" chip and **Retry** on their row.
- A tab that regains the queue while settling its writes can drop it.
- The note that the other tab has a front waiting is cramped at 375 px.

**Webhooks**
- A 30 s timeout hit while an answer is arriving stops the household's later webhooks.
- The timeout applies to each redirect hop, not to the whole request.
- A recipe action answered with over 1 MiB logs a traceback.

## As built

Built as designed above. The design text was updated to what was built, so this section records what changed from the
original design, how it was verified, what wasn't built and why, and what needs you. The changes came from two building
rounds: the first with two review rounds, the integration seam tests and a live run of the production build; the second
(the completion round) built every open item from an inventory of 201 entries and the limitations and out-of-scope list
that code could remove, then had a third review round. Review rounds 4 to 6 followed, each with its fixes; a seventh was
stopped before its findings were fixed (Known limitations).

### Changes from the design

**Inputs and intake (§1, §2)**
- Uploads need a Bearer token; another scheme or an empty token is a 401. Every refused upload carries a top-level
  `summary` for Shortcuts. A malformed body is `400 invalid_body`; a raw image over 30 MiB is a `too_large`
  rejection; `400 nothing_accepted` carries a `summary` and no `message`.
- Image URLs in JSON uploads, off by default (`AI_INGEST_URL_FETCH`), fetched with their own allow-list, pinned
  addresses, at most 3 checked redirects, no compressed bodies and a byte cap (§1.2).
- PDFs (rendered by pypdfium2 in a sandboxed child process, in a render slot of their own) and multi-page TIFFs
  become a card's pages. JPEGs up to 260 MP are decoded reduced; progressive JPEGs are bounded by decode memory
  (600 MB) and scan count (100).
- `done=true` seals a batch with its card; `POST /batches/{id}/touch` keeps an app batch open while the capture page
  is.
- The group's 200-job cap and the optional per-user cap (`AI_INGEST_MAX_PROCESSING_PER_USER`, 429 `user_quota`) are
  counted again for every card inside its insert; a later card is refused as `quota`.
- One household's intakes take turns under an intake lock, so simultaneous uploads share one batch and the same card
  sent twice at once is one job.
- A committed card counts as a duplicate only while its recipe exists.
- The summary counts cards, says when one was already scanned, promises a notification only when a notifier will send
  one, says while the monthly limit is reached that cards are read when it resets or within about 10 minutes after it's
  raised, and says why cards couldn't be used (one reason in words, several as counts).
- The inbox turns on from a folder mounted at `/inbox`; a blank `AI_INGEST_INBOX_DIR` counts as unset; it's off on
  Windows. Folders it creates get `AI_INGEST_INBOX_DIR_MODE` (2775). Every file operation goes through directory
  descriptors opened with `O_NOFOLLOW`; one bad entry stops nothing else.
- Inbox cards take the household's language; refusal notes come from en-US.json; each refusal logs one line and a
  burst sends one "Recipe cards not added"; a folder Mealie may not move is listed as `no_permission` (shared across
  processes); `processed/` can be purged (`AI_INGEST_INBOX_PROCESSED_DAYS`); the app shows waiting photos and
  refusals.
- The inbox and JSON decoding share the intake slots, and JSON images are decoded to spooled files: a 100 MP JPEG
  peaks at about 250 MB, not 1.4 GB.
- Capture: chosen photos go to a tray with Upload; Take photo, Choose and Done share one row at 375 px; 320 px
  thumbnails (a 40-photo tray adds about 105 MB, not 680 MB); the drop zone takes photos and PDFs only, counting PDF and
  TIFF pages; Data saver; a `400` for size re-encodes like a `413`; Scan again; the queue survives a reload in
  IndexedDB, one tab keeps it (a Web Lock, else a lease in IndexedDB, so over plain http too), and Log out asks first;
  failed uploads show a sidebar badge.
- The local-only switch applies to every card not sent yet, remembered per user; a group that keeps cards local
  with nothing local to read them hides capture behind a warning.

**Runner (§3)**
- Groups take turns, with an optional cross-process per-group cap (`AI_INGEST_GROUP_CONCURRENCY`).
- `limit_reached` cards are read again at the next monthly reset, or sooner once the limit is lifted, and are counted
  and told as waiting, not failed.
- A running task re-reads its group's local-only setting every 5 s.
- Shutdown releases leases by owner, including claims still in flight.
- A task requeued with a cancel request is never claimed again.
- Task modes: rebuild from an edited transcription and AI line parsing; tasks expose `mode` and `refs`.
- A presence file tells the app whether a reader runs; `AI_INGEST_WORKER=false` with ingestion on logs a warning.
- The restore pause: one shared lock per process plus a gate; a JSON marker with its own restore lock, removed at
  once when its restore is gone; a 45 s wait instead of 120 s; every running task queued again after a restore;
  kept results and answers so a restore never pays for a reading twice; `AI_INGEST_LOCK_DIR` and a private `/tmp`
  fallback; progress writes skipped while paused.
- Every API request gets 503 during a restore, and upstream API writes wait for one (`RestoreGuardMiddleware`), entering
  their section with their body's last message rather than reading or copying the body, on threads of their own; the
  SPA's recipe pages are served plain meanwhile. The design left upstream's writers racing a restore.
- Empty batches are purged after a day; a card whose recipe was deleted can be scanned again; a newer whole-card
  reading replaces older pending ones.
- AI clients are closed on the event loop that used them.

**Extraction and flags (§4, §5)**
- Orientation: a turn also needs a winning score of 500; `AI_INGEST_ORIENT` is its own switch (`OCR_ENABLED=false`
  turns off only the OCR fallback); turns are staged, stored, then swapped, crash-safe for the runner and for manual
  Rotate, with a per-page lock and `no-store` images until settled.
- The reader's `rotation_clockwise` turns pages Tesseract didn't settle. The design didn't use model rotation.
- Providers that take one image per request read two-sided cards page by page.
- Cards in other languages are parsed by the AI parser on the card's own service. The design kept them as text.
- Cross-read: shaped-line preference, step windows from every line plus grown ones, bounded cost (741 scorer calls for a
  one-word-per-line page, not 21,090), units compared by meaning, one flag for a gap. A free OCR check of whole numbers
  on printed cards; a number Tesseract may have confused is read again from its line alone before it's flagged.
- Flags: `linked_fuzzy` and `organizers_skipped` are new; `check_parse` also fires on lost amounts, size words, second
  ingredients and split-off alternatives, oven or pan lines among the ingredients and over-long lines; `unit_unclear`
  only for unit-like tokens; `implausible_temperature` knows rising and cooling; `not_on_card` ignores list numbers;
  flags carry the position of their value; note flags are keyed by note id.
- Shorthand: more abbreviations (doz., env., sq., tbls., pkgs., pkt(s). …), the common units' spellings, size words
  anywhere, package sizes and can numbers to the note, dashed mixed numbers, fractional dozens, shorthand after a second
  amount, linear-time patterns. A written-out shorthand never links to another unit by a near miss.
- A leading "From" leaves the attribution (a translated one only with a colon); a description that repeats it is
  dropped; region hints from Tesseract's line boxes.

**Review and commit (§6, §7)**
- Saves recompute flags as finalize does, parse changed text-only lines and return them with the duplicate state;
  `draftVersion` changes only with the draft; numbers must be finite; a save that accepts a proposal another device
  settled is a 409.
- Lines kept with a marker are parsed around it; a kept amount marker puts the unit in the note ("sugar ___ cup").
- Review page: notices in a strip inside the review bar; Commit & next carries on to the next batch; autosave
  retries and a leave prompt; reorder; Cancel; keyboard and touch-friendly re-read selection that starts at the
  flagged line; Re-read per line, step and note; Rebuild from this text; Parse with AI; Keep as text; Keep as new
  food; Create "X" for tags, categories and tools (plain Enter creates nothing); notes in a fork list keyed by id;
  duplicate banners for near names and waiting cards; failed-card dates; logout saves first; Enter never submits a
  dialog by accident; Vuetify 4 typography in the fork's files.
- The card photo and the cover are switches that default to off in households whose new recipes are created public; a
  portrait cover is letterboxed to 4:3. The design always attached the card and used it as the cover.
- Commit picks the first free "Name (n)" itself (up to 1000), returns a card whose create made no recipe to review,
  creates organizers named by the reviewer, sets `is_ocr_recipe`, gives new units their standard abbreviation, and
  fences every write on its lease. `recipe_created` is sent at least once, after the write lock is released. The commit
  answer carries the name the recipe got (`name`), and the "Added …" notice uses it.
- New actions: Add N clean cards (5 per request), Back to review (undo, which updates the card before deleting the
  recipe), Add as back of previous card (merge, serialized per household, crash-safe, keeping local-only), Read
  with cloud providers.
- Discard: any member may discard cards sent with an API token. The cards page lists added cards by commit time,
  can load older cards, follows changes made elsewhere and shows reader, limit and inbox panels.

**Events (§8)**
- At least once per notifier, with a lease, per-notifier delivery records and 5 attempts. The design was at most
  once.
- Batches notify by their cards' last activity, and old restored batches are settled without a send.
- "Recipe cards not added" for refused inbox files.
- A batch with no ready card is titled "Recipe cards not read"; cards waiting for a monthly limit are told as waiting
  ("Recipe cards waiting" when nothing else is ready or failed), and a wave notification follows once they're read.
  Sends renew their lease while they run.
- Apprise fields are percent-encoded in upstream's listener (a hook), so upstream's own events reach HA intact too.
- Send test notification uses the real event type, shows its result, and reports a failed delivery (502) only to
  managers and admins.
- The notifier card says the switch saves at once and warns when `BASE_URL` is local.

**Privacy (§10)**
- The policy is applied before the monthly limits, so local-only cards fail `limit_reached` rather than reach a
  cloud provider within its limit.
- Local-only connections are pinned to the checked private address and ignore proxies.
- The policy re-checks the group's setting during a task; merged cards keep local-only; Read with cloud providers
  lifts it for one failed card, on purpose.
- The usage log and the eval record the model that answered.

**Eval and voice (§11, §12)**
- `--check` needs no database or `PRODUCTION`; `*_FILE` secrets are read; `--dry-run`; AUROC and cost per caught
  error columns; attribution ignores "From"; eval cases get tags, notes, editing, download and household scoping;
  code generation no longer renames eval photos; `jpegtran` takes `-outfile`.
- The voice tool speaks digits in the caller's language with English fallback; its keys are snake_case.

**Settings, backups and upstream (§13 to §17)**
- Two more migrations (`0c2bef734816`, `0f77cc21b216`); `CardDraft` version 3 (note ids, photo switches, a cover choice
  old pages can't set by mistake); `GET /ingest/settings` answers 200 when ingestion is off and reports `limitReached`,
  `limitedFeatures`, `baseUrlSet`, `readerRunning` and the inbox status; `/about` reports `worker`; eleven new
  `AI_INGEST_*` settings (§15).
- Retention: committed cards are slimmed before their files go; failed cards count from their retry; failed purges
  take the merge lock; kept results go after 24 hours.
- Backups leave out the runtime files and folders.
- Migrations run under a lock that works on SQLite and PostgreSQL (PgBouncer included), so several workers can start a
  fresh install; processes starting together agree on one secret. A backup restore holds the lock until the restored
  database is migrated, so a worker starting mid-restore waits. The design left `init_db` unlocked.
- safehttp refuses redirects off http(s) and **from https to http**, deliberately: a downgrade lets anyone on the
  network swap the page or image. Upstream URL imports, image downloads and webhooks now say so ("The page redirected to
  an insecure http:// address") instead of "not an allowed domain", and one webhook's refusal no longer stops the
  others. safehttp also keeps the impersonated browser's Accept-Encoding, reads every body within a cap and decodes a
  compressed one itself (`safehttp/decoding.py`; curl decodes nothing): gzip, deflate, zstd and br, a slice at a time
  without holding the event loop, at most 64 frames or members per body. A fetched page or image stops at 50 MiB by
  default (an image over it is a 400), and the answer to a webhook or recipe action at 1 MiB, refused past it. A body
  that doesn't decode (corrupt, truncated, trailing data, another coding) fails the fetch; a gzip or zlib body missing
  only its checksum trailer is read, as curl reads it. A webhook or recipe action is given up on after twice its
  15-second timeout, however slowly the answer comes, and a webhook whose answer is refused (over 1 MiB or not
  decodable) doesn't stop the household's other webhooks. An allowed IP or range in `HTTP_ALLOW_LIST` vouches only for
  itself: a host's other private addresses (its ULA or link-local IPv6) are left out of the connection, not refused
  (upstream let the whole answer through).
- `create_from_ai` sets its cover key; a busy restore shows one message on the restore page, and a restore refused
  because another process is migrating says "Mealie is updating its database. Try the restore again in a minute."
- During a restore the browser stays signed in, a page opened meanwhile shows Mealie's own wait page (`app/error.vue`)
  and opens once it's over, and Docker's health check counts the restore's 503 as healthy.
- The frontend has a fork type check in CI (`pnpm typecheck:fork`).

### Verified

- **Final full checks:** the whole backend suite on SQLite and the frontend suite (lint, 1,402 tests, the fork type
  check) passed at c9e2bf5f0. The full PostgreSQL 16 suite passed at c22db33ef, except upstream's three `test_config.py`
  tests that expect port 5432 (the cluster ran on a private port). Later commits passed their own areas' tests only.
  Tesseract runs in backend CI, so the orientation and OCR tests run there.
- **Regression tests:** every fix of both rounds has a regression test, and nearly all were seen failing on the code
  before the fix (layout fixes were measured in Chromium instead).
- **Review rounds.** Round 1 raised 75 findings across eight dimensions; its high and medium ones were checked by
  separate verifiers, and the fix round handled 68. Round 2 reviewed those fixes and confirmed 17 more. Round 3 reviewed
  the completion round's code across five dimensions (backend, security, frontend, pipeline, upstream hooks): 47
  findings, each checked by a separate verifier, 44 confirmed (2 high, 16 medium, 26 low) and 3 rejected. All 44 were
  fixed, each with a regression test. Review rounds 4 to 6 were run, and every confirmed finding of them was fixed. A
  seventh round was stopped before its findings were fixed; they're listed under Known limitations.
- **Seam tests**, each passing 10 times in a row on SQLite and once on PostgreSQL in the first round, and in every
  full run since:
  - `test_card_flow_e2e.py`: a two-sided card from a Bearer upload to a committed recipe, with one ready
    notification that Home Assistant can parse, and one batch and one notification when Done is tapped while the
    last card uploads;
  - `test_two_dispatchers.py`: no card read twice, one notification per batch;
  - `test_restore_while_processing.py`: the restore waits, completes and leaves files matching rows, nothing is
    logged as an error, and each card is read by a provider once, including when the answer lands mid-restore;
  - `test_banana_replay.py`: the banana card through the real eval, golden scores, the invented "2" caught only
    with cross-read on, and no database rows written.
- **Against real components, outside the suites:** HA 2026.10 for the CARDS.md setup (below); PgBouncer 1.22 in
  transaction mode for the migration lock (three workers on a fresh database: one migrated, two waited); Apprise posting
  to a local server for the encoding; curl against local servers for image URLs, gzip bombs and redirects (a 1 GiB gzip
  body is refused in 0.1 s using 3 MiB); real Chromium Web Locks and the IndexedDB lease fallback for the upload queue
  (two tabs opened at once: one keeper in 5 of 5 runs, the other taking over 9–29 ms after it closed); the PDF sandbox
  with every protection active on this kernel (Landlock ABI 7).
- **Live, outside CI** (a production build, a stub provider answering the card schemas, Chromium), first round:
  - **Exit criterion 2:** a 10-card two-sided batch captured, reviewed and committed at 375 px in 71 s (75 s on
    the final re-run), at machine speed with the stub answering in about 2.5 s. One "Recipe cards ready"
    notification arrived, and its link opens the first card. Clean cards took one tap, flagged ones two to four.
  - The local-only switch under a slow network: every card went with the switch as it was when the card was sent,
    across ON, OFF, ON and after Done.
  - Every review action at 375 px and 1366 px: filling a blank, Keep as written, a region re-read, Commit & next,
    Already scanned, logout with uploads in flight, the restore pause and retry.
  - A restore while cards are read (SQLite and PostgreSQL); two workers on SQLite and PostgreSQL: every card read
    once, one notification, a double commit making one recipe.
  - The inbox (cards, card folders, rejects, links); Home Assistant's REST counts and the `recipe_card_queue` tool
    through HA's own MCP client code; iOS Shortcut-style raw and JSON uploads.
- **Completion round in Chromium**, with real Vuetify at 375 px and 1366 px against the fork's pages and a stub API:
  each wave's frontend items (review page 15/15 and 11/11, 54/54, 69/69, 36/36 checks; the finisher's 76/76),
  layout measurements (capture buttons on one row, notices clear of the header, rows unclipped, a 120 px touch drag
  moving the selection 120 px), and the re-read hint covering the right line on the real test card.
- **A real Home Assistant** (2026.10.0b0, with Mealie at d71636679 and the stub provider), with CARDS.md's YAML
  copied as written: four batches (a `curl -F` upload, `shell_command`, a camera snapshot into the inbox, and the
  safer `shell_command`) each fired the "ready" automation exactly once; `document_data | from_json` parsed and
  `reviewUrl` opened the batch; the "not added" event arrived and the "ready" automation ignored it; the `rest`
  sensor matched `/jobs/counts`; **Send test notification** fired the automation; a `recipe_created` for "Mug cake
  + banana & 100% co" parsed intact. Mealie logged no error. The run's five corrections to CARDS.md (batches per
  uploader and source, a `shell_command` that keeps the token out of HA's log and fails on refusals, one `script:`
  key, what a sent test proves, the "not added" automation) are in CARDS.md.
- **The inbox with other users** (Mealie at d71636679): per-household folders, mode 2775 on new folders and
  existing ones left alone, setgid giving new files Mealie's group, writers outside the group refused, the settle
  delay, ignored names and links, notes in `failed/`, one "not added" per household burst, the `processed/` purge
  with the clock moved a day, and `/inbox` detection with a real mount. It found two bugs, both fixed since: a card
  folder from another user under umask 022 stayed stuck silently (now `no_permission` in the status, with the fix
  in the log), and an invalid `AI_INGEST_INBOX_DIR_MODE` stopped startup (now logged, 2775 used).

### Not built, by decision

Each of these was weighed and left out for the reason given. Any of them can be built if you ask for it.

- **Web Push for "Recipe cards ready"**, and a push switch per device: it means replacing upstream's generated
  service worker (workbox `generateSW`) with a custom one, a deep upstream change, and the notification already
  reaches phones through Apprise and Home Assistant.
- **Translating a card into another language:** a new feature beyond Phase 2. Cards in other languages are parsed in
  their own language (§5).
- **Confidence from token log-probabilities**, and unsure flags from low-probability digits: only OpenAI's own models
  return log-probabilities (Claude doesn't, nor Gemini or Ollama over the OpenAI-compatible API). The flags (§4.6)
  work with every provider.
- **Step photos cropped from the card:** cards are attached whole; cropping per step is a feature beyond Phase 2.
- **Batch-price reading** of inbox and API cards (Anthropic Message Batches, OpenAI Batch): it roughly halves the
  provider cost but can take up to 24 hours, which conflicts with reviewing cards as they're read and with the
  runner's 30-minute deadline and leases.
- **An Android or desktop share target** for card photos: it needs upstream's PWA manifest and service worker
  changed (it would replace upstream's share target for URLs). iOS has no Web Share Target, and there the Shortcuts
  share sheet covers sharing (`docs/ai/CARDS.md`).
- **An MCP upload tool:** tool arguments are written by the model, which can't reproduce a photo's bytes (a 3 MB
  photo is about 4 MB of base64). Voice clients have no images, and agents with file access use `POST
  /api/ai/ingest`.
- **SSE progress:** iOS Safari drops `EventSource` streams in the background and when the screen locks, so the phone
  needs the polling it has; SSE would be a second transport with no visible change.
- **Nutrition:** Phase 4, which listens to `recipe_created` (now sent at least once).
- **Translations of the fork's strings into other languages:** the project allows only en-US strings; Crowdin owns
  the other locales. Everything falls back to English.

### Needs you

Exactly these can't be done here:

- **Exit criterion 1, a 20-card eval set scored per provider.** It needs your cards and your provider keys; this
  environment has neither. Ready for it: **Save as eval case** with tags and notes on the review page, the eval-case
  list with editing and download in the group's Recipe cards settings, `--check` (no database needed) and
  `--dry-run` (no provider call) to check the set and the configs before spending, the AUROC and cost-per-caught-
  error columns, and the banana replay as a known-good baseline. [`EVAL.md`](EVAL.md) has the runbook; record the
  targets, the cross-read default and D3 in its Results.
- **Trying it on your iPhone:** a 10-card Front & back stack, the Shortcuts, and tapping the Home Assistant
  notification through to the first card. The phone flow was driven here in Chromium at 375 px; iOS Safari can't
  be emulated in this environment. [`CARDS.md`](CARDS.md) has the steps.
- **The bind-mount inbox setup:** mounting each household's folder as its own share, then checking that each
  household gets only its own cards, that the mount points aren't treated as links, and the mode of folders created
  inside a mount. The mount commands were refused by this environment's permission system; the detection and the
  folder modes are tested. [`DEPLOY.md`](DEPLOY.md#6-recipe-cards) describes the setup.
