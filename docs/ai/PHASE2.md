# Phase 2: Recipe card ingestion v2 (design)

Implements the "Phase 2" row of [`AI_INTEGRATION_PLAN.md`](../AI_INTEGRATION_PLAN.md) §9: jobs, batch upload, a
review page, ingredient linking, the card kept as an asset, an inbox folder, events and the eval harness. The exit
criteria are **a 20-card eval set scored per provider** and **a 10-card batch reviewed and committed from a phone**.

| # | Piece | What it gives you |
|---|---|---|
| 1 | Inputs | Phone batch capture, `POST /api/ai/ingest` (multipart, raw image, JSON base64) for Home Assistant (HA) and iOS Shortcuts, and a watched inbox folder |
| 2 | Intake | Every image becomes an upright, metadata-free JPEG inside the upload request, so GPS never reaches disk |
| 3 | Jobs | A queue in the job table that survives restarts, crashes, backup restores and several worker processes |
| 4 | Extraction | Upstream's import workflow with card prompts, Tesseract rotation, the OCR fallback, an optional second read, and flags with stated reasons |
| 5 | Ingredients | Card shorthand ("1 T.", "1/4 t.") fixed before Mealie's NLP parser, then linked to your foods and units |
| 6 | Review | Cards in capture order: one tap for a clean card, about two per flag; re-read a region |
| 7 | Commit | Crash-safe and idempotent; the card kept as an asset with an unguessable name, and as the cover |
| 8 | Events | One "recipe cards ready" notification per batch through Apprise, including HA |
| 9 | Privacy | A fail-closed "keep cards on this server" policy covering every AI call a card causes |
| 10 | Eval | Scores exactly the production pipeline, its flags and provider chains, and decides D3 |

The guiding rule is **fewer moving parts**. The job row is both the review document and its one pending piece of
work. There is no task table, broker, SSE stream, image-URL fetching or new top-level data directory.

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
- **The inbox lives outside `DATA_DIR`** (`AI_INGEST_INBOX_DIR`, e.g. `/inbox`), one folder per household. Inside
  `/app/data` it would be zipped into backups, wiped by restores and re-owned by `entry.sh`'s `chown -R`.
- **No image URLs in the upload API** (plan §4.2 and §6 list them for HA's `rest_command`). HA uses the inbox or
  `curl -F`, and iOS sends the file. URL fetching would widen the global `HTTP_ALLOW_LIST`, and the safe-fetch
  transport doesn't restrict schemes on redirects. This is a user question.
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
- **Commit never creates tags, categories or tools.** It creates foods only for users who can organize; units are
  created freely, as upstream allows (F21).
- **`is_ocr_recipe` isn't set** (no schema exposes it). `recipe_ingestion_jobs.recipe_id` is the provenance.
- **The card asset is the normalized JPEG**, never the uploaded bytes, which carry GPS and may be HEIC.
- **No edits to upstream `app.py`, `repository_factory.py`, `settings.py` or `mealie/schema/openai/`.** Ingest
  repos sit outside `AllRepositories`, settings are a fork `BaseSettings`, the daily purge runs from the dispatcher,
  and the card response schemas live in a fork module registered in the Claude limit test.
- **No "Enrich" step.** Nutrition (Phase 4) listens to upstream's `recipe_created`, which commit publishes.
- **No "same card" toggle after upload.** One upload request is one card (front first). The phone groups photos
  into cards before uploading.

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
  (`media_recipe.py`). `recipe_public` defaults to true.

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
iOS Shortcut ──────┤ multipart │ raw image │ JSON base64
HA shell_command ──┘      auth → pause, AI and quota checks (one thread call) → byte-capped stream
/inbox/<group-slug>/<household-slug>/ ─ scan 30 s, rename-claim ─┐
                                                                  ▼
             IntakeService.ingest(), holding the shared ingest write lock:
             sniff → pixel cap → decode → EXIF transpose → strip metadata
             → page.jpg ≤4096 · view.jpg 2048 · thumb.webp in DATA_DIR/groups/<gid>/ai-ingest/<job>/
             → one transaction: batch touch (unsealed only) + job row (processing, task queued) → wake()
                                                                  │
IngestDispatcher (one per worker process, router lifespan, its own thread limiter for DB calls)
   claim (Core conditional UPDATE + lease token) · heartbeat 20 s · sweep · inbox · housekeeping · daily purge
   paused while DATA_DIR/.ai-ingest-paused is fresh; a restore also waits for in-flight writers (flock)
                                                                  │
daemon thread × (AI_INGEST_CONCURRENCY + 1 re-read slot): own loop, own sessions, locale,
ai_call_policy(local_only, job_id)
   orient (Tesseract, 1.5× margin) → extract_card(ai):  CardImageCompiler [image] | CardOCRCompiler [default]
                                         ∥ cross-read on the same ai [image, opt-in]
                       → CardBuildRecipeStep [default] → ResolveOrganizersStep [fast, suggestions only]
                       → normalize_ingredients (shorthand → NLP → matcher) → compute_flags
   fenced finalize (lease token) → status=ready → batch finished? → Apprise (fork listener) → HA
                                                                  │
Review (polls): GET · PUT draft (draftVersion) · reread / reextract → proposal · rotate
Commit (request): ready → committing (server-owned recipe id + asset token) → files → create_one
                  → cover key → committed → upstream recipe_created
Every routed AI call: AIRuntime.candidates() then apply_policy(local_only): fail closed on every slot
Eval: normalize_page → orient_page → extract_card (the same functions) with EvalOpenAIService pinning and
      read_path=image or ocr per config
```

**Fork-owned code:**
- **Backend:**
  - `mealie/services/ai/ingest/`: `settings.py`, `limits.py`, `storage.py`, `images.py`, `shorthand.py`,
    `matching.py`, `flag_rules.py`, `intake.py`, `upload.py`, `batches.py`, `inbox.py`, `runner/`, `pipeline/`,
    `tasks.py`, `review.py`, `commit.py`, `events.py`, `retention.py`, `eval_export.py`;
  - `mealie/services/ai/policy.py`, `local.py`, `tools/ingest.py`;
  - `mealie/routes/ai/ingest/`: `__init__.py` (owns the dispatcher lifespan), `_deps.py`, `upload.py`, `jobs.py`,
    `settings.py`, `notifiers.py`, `eval_cases.py`, `about.py`;
  - `mealie/db/models/recipe_ingest.py`, `_model_utils/json_text.py`; `mealie/repos/repository_recipe_ingest.py`;
    `mealie/schema/recipe_ingest/`; `mealie/scripts/strip_card_photo.py`;
  - prompts `mealie/services/openai/prompts/recipes/card-*.txt` (new files, so `OPENAI_CUSTOM_PROMPT_DIR` can
    override them).
- **Frontend:** `pages/g/[groupSlug]/recipes/cards/` (`index.vue`, `[jobId].vue`, `review.vue`),
  `components/Domain/Ingest/`, `composables/use-recipe-ingest*.ts`, `lib/api/user/recipe-ingest.ts`, and
  `components/Domain/Group/GroupRecipeCardSettings.vue`, `components/Domain/Household/HouseholdNotifierAIEvents.vue`.
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
| `mealie/services/backups_v2/alchemy_exporter.py` | eight names in the fork's `look_for_datetime` block |
| `mealie/services/backups_v2/backup_v2.py` | `@pauses_ingest` on `restore` (1 line plus the import; the file is already fork-modified) |
| `mealie/lang/messages/en-US.json`, `frontend/app/lang/messages/en-US.json` | a `recipe-ingest` namespace |
| `frontend/app/lib/api/client-user.ts` | 3 lines for `RecipeIngestAPI` |
| `frontend/app/components/Layout/DefaultLayout.vue` | sidebar entry with the ready count, Create-menu item (~20 lines) |
| `frontend/app/pages/g/[groupSlug]/r/create/ai.vue` | one "Have a stack of cards? Scan them in a batch" link |
| `frontend/app/pages/household/notifiers.vue` | one `<HouseholdNotifierAIEvents :notifier-id>` line |
| `frontend/app/pages/group/index.vue` | one `<GroupRecipeCardSettings />` line plus its import (its fork-owned `index.test.ts` gains one `vi.mock` line) |
| `.github/workflows/test-backend.yml` | `tesseract-ocr` added to the existing `apt-get install` line |
| `frontend/app/components/Domain/Group/GroupAIProviderDialog.vue` | a "Runs on my network" switch (~10 lines; already fork-modified) |

## 1. Inputs

### 1.1 Phone batch capture

The **Recipe cards** page is `/g/<group>/recipes/cards`. `recipes/` has only static children, so no recipe slug is
shadowed. It's reached from a sidebar entry **Recipe cards (N)** (N = ready), a Create-menu item **Scan recipe
cards**, and a link on upstream's AI import page. All three show only when the group can read cards: a default
provider plus an image provider or OCR (upstream's `_validate_providers` rule).

- **One side / Front & back**, remembered per browser.
- **Take photo** (`capture="environment"`, one shot per tap on iOS). In Front & back mode the button relabels itself
  *Take photo* → *Back side* → *Next card*, with **No back** and **Retake**.
- **Choose photos** (`multiple`, no `capture`): the fast path for a big stack. Shoot everything with the Camera app,
  then pick them all. In Front & back mode they pair in selection order, with **Swap** and **Split/Join** on the
  thumbnails.
- A drop zone on desktop. Inputs reset `value=''` after reading.
- **A card uploads as soon as it is complete**: one card per request, two at a time, with `onUploadProgress`, three
  retries with backoff, then **Retry**. Originals are sent as they are, which keeps full detail for re-reading
  faint pencil. A `413` re-encodes that card's photos once in the browser (`createImageBitmap`, at most 3072 px,
  JPEG 0.9) and retries.
- **Batches:** the first card creates one (`POST /api/ai/ingest/batches`). Every card sends `batchId` and its capture
  `position`. **Done** marks the batch *sealing* in the queue; `POST …/seal` goes only once every card of that batch
  has uploaded or failed for good, so the last card, still uploading when you tap Done, stays in the batch. An
  unsealed app batch seals itself after 10 minutes without a card.
- A `duplicate` rejection marks the card done with an **Already scanned** chip linking to the earlier job (usually a
  resend after a lost response). The duplicate check covers every page, so a front sent again with its back isn't
  a duplicate (§2).
- The queue is a module-level singleton composable, so it survives going to a review page and back.
  `beforeunload` warns while photos are pending.
- **A privacy chip** says where photos go, from `GET /ingest/settings`'s `reader` (every member gets it): *Read by
  Claude Sonnet (cloud)*, *Read with OCR, then Claude Sonnet (cloud)*, or a lock and *Stays on this server*.
  Tapping it offers **Keep these cards on this server** for the batch (`localOnly=true`) when `localOnlyAvailable`.
  A batch can opt in to local-only when the group doesn't, never out.

### 1.2 `POST /api/ai/ingest`

A `BaseUserController` method whose only parameter is `request: Request`. Before any body byte is read:

1. Auth runs (the controller's dependency). Then **the `Authorization` header must be present**: a cookie alone gets
   `401`, which closes the cross-site POST an embedding page could make with the `SameSite=None` cookie (F18).
2. `503 paused_for_restore` with `Retry-After: 60` while the pause marker is set (§3.9); `503 ingest_disabled` when
   `AI_INGEST_ENABLED` is off.
3. `400 ai_not_enabled` when the group can't read cards (§1.1); `400 local_only_unavailable` when the group is
   local-only and lacks a local image provider (or OCR) plus a local default provider.
4. `429` with `Retry-After` when the group already has 200 `processing` jobs (counted in the database).
5. `413` when `Content-Length` exceeds `AI_INGEST_MAX_UPLOAD_MB` (100), or 45 MiB for `application/json`.
6. The body is read through a byte counter that also stops chunked bodies at the same caps.

The handler is `async` (it streams the body), so checks 3 and 4 run in **one `anyio.to_thread.run_sync` call**: the
quota count, the provider settings and `is_local_provider`'s `getaddrinfo` never block the event loop (Phase 3's
rule). Intake checks the pause again under the write lock (§3.9), so a restore that starts during a slow upload
still gets `503`. Error bodies are `{"detail": {"code": …, "message": …}}` with a translated `message`, which the
frontend toasts and a Shortcut can show.

| Content type | Shape | Callers |
|---|---|---|
| `multipart/form-data` | every file part is an image, any field name (`files` documented); text fields `batchId`, `position`, `split`, `localOnly`, `allowDuplicate` | PWA, `curl -F`, Shortcuts *Form* |
| `image/*`, `application/octet-stream` | the body is one image; options in the query string | Shortcuts *File* |
| `application/json` | `{"images": [{"data": "<base64 or data: URL>", "filename": "front.jpg"}], "split": false, "batchId": null, "localOnly": false}` | Shortcuts *JSON*, scripts |
| other | `415` | |

- Multipart is parsed by Starlette's `MultiPartParser` over the capped stream (`max_files=20`, `max_fields=20`). Its
  file parts spool to the system temp directory, never `DATA_DIR`, and are closed when the request ends.
- Base64 is decoded leniently (line breaks, a `data:` prefix), since Shortcuts wraps lines.
- `localOnly=true` per upload is checked after parsing, with the same `400` as the group setting.

```json
202 {"batchId": "…",
     "jobs": [{"id": "…", "status": "processing", "pageCount": 2, "reviewPath": "/g/home/recipes/cards/…"}],
     "rejected": [{"index": 2, "filename": "IMG_0007.HEIC", "reason": "duplicate", "duplicateOf": "…"}],
     "summary": "1 recipe card queued. You'll be notified when it's ready."}
```

- `summary` is in the request's `Accept-Language` (else en-US), for a Shortcut's *Show Notification*. Nothing is
  named `message`, which the frontend's axios interceptor would toast.
- `400` if nothing was accepted, with the same body in `detail`. Rejection reasons: `too_large`,
  `unsupported_format`, `pdf_not_supported`, `too_many_pixels`, `unreadable_image`, `too_many_pages`, `duplicate`.

**Home Assistant:**
- **Recommended, the inbox:** `camera.snapshot` to
  `/media/<share>/<group-slug>/<household-slug>/card_{{ now().strftime('%Y%m%d_%H%M%S') }}.jpg`, where `<share>` is
  the folder Mealie mounts at `/inbox`. On HA OS the share is added first under *Settings → System → Storage → Add
  network storage* (usage *Media*). No token is needed.
- **Without shared storage, `shell_command`** (60 s limit): `curl -sS -H "Authorization: Bearer …" -F
  files=@/media/snap.jpg http://mealie:9000/api/ai/ingest`, kept in `secrets.yaml`.
- `rest_command` can't send binary and templates can't base64 a camera image, so it isn't offered.

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

- **Off unless `AI_INGEST_INBOX_DIR` is set.** A directory inside `DATA_DIR` or `/app` is refused at startup (logged,
  inbox disabled). `docker-compose.ai.yml` and the Unraid template gain an optional share mounted at `/inbox`.
- **Ownership is by folder:** `<inbox>/<group-slug>/<household-slug>/`. Each scan creates the folder for every
  household (idempotent `mkdir`), and the cards page shows yours. Unknown folders are logged once and ignored. Inbox
  jobs have no uploader; they belong to the household. Anyone who can write to the share can queue cards for any
  household; the docs say so, and quotas and review-before-commit bound it.
- **A file is one card; a first-level subfolder is one multi-page card** (pages in name order), taken once all its
  files are stable.
- **Scan** every `AI_INGEST_INBOX_POLL_SECONDS` (30) from the dispatcher, in a thread, at most 20 files a tick.
  - Skipped: non-regular files by `lstat` (symlinks too); names starting with `.` or `~`; `.tmp .part .crdownload
    .partial .download .filepart`; `Thumbs.db`, `desktop.ini`; the reserved `processed/`, `failed/` and
    `.mealie-claimed/`.
  - A file is taken once its `(size, mtime_ns)` is unchanged since the last scan and it's 10 s old:
    `camera.snapshot`, SMB and scanners write in place under the final name.
- **Claim** by `os.rename` into `<household>/.mealie-claimed/<claim_ms>__<uuid>__<name>`, on the share's own
  filesystem (a rename into `DATA_DIR` would raise `EXDEV`). Another process's scan gets `FileNotFoundError`. The
  claim time is in the name because `rename`, Syncthing, rsync and `cp -p` all keep the file's original mtime.
- **Open once:** each claimed file is opened with `O_NOFOLLOW`, checked with `fstat` to be a regular file inside the
  inbox root, and that open file object goes to intake. Nothing reopens it by path.
- **Then** the same `IntakeService` (`source="inbox"`, batch auto-join per household folder, §1.4), then a rename to
  a unique name in `processed/YYYY-MM/`, or an unlink when `AI_INGEST_INBOX_KEEP_PROCESSED=false`. A rejected file
  goes to `failed/` with `<name>.error.txt`.
- **Crash safety:** a claimed file whose `claim_ms` is over 10 minutes old is re-claimed with a second atomic rename
  to a fresh `claim_ms`, so only one process retries it. Intake's duplicate check and insert share one transaction,
  and an inbox intake confirms its claimed file is still at its path before committing (a retry would have moved
  it). If the job exists, the content hash finds it and the file is just moved. Nothing is lost or ingested twice,
  Syncthing reverts included.
- **Skipped while paused** for a restore; the pause is checked again before each file (§3.9).

### 1.4 Batches

A batch is one capture session, Shortcut run or inbox burst. It groups the queue, orders the review and sends the
single notification (§8). Every job belongs to a batch.

- **App batches** are explicit (`POST /batches`), sealed by **Done** or after 10 idle minutes.
- **API and inbox uploads auto-join.** A request without `batchId` joins the newest unsealed batch with the same
  household, uploader (none for the inbox), `source` and source key (the inbox folder), if that batch saw an upload
  in the last 2 minutes. Otherwise it starts a new one. Auto batches seal after 2 idle minutes. `batchId=new`
  forces a new batch.
- A sealed batch is never reopened: the card goes into a new batch by the rules above, and the `202`'s `batchId`
  says which, so the PWA adopts it. An unknown or foreign `batchId` is `404`.
- **Sealing can't race an insert.** The job insert runs `UPDATE recipe_ingestion_batches SET last_upload_at=:now
  WHERE id=:b AND sealed_at IS NULL` in its own transaction and picks or creates another batch when that matches 0
  rows. Sealing is `UPDATE … SET sealed_at=:now WHERE id=:b AND sealed_at IS NULL`, plus `AND last_upload_at <
  :cutoff` for the idle seal. On SQLite the touch takes the write lock; on PostgreSQL it holds the row lock until
  commit, and a waiting seal re-checks its `WHERE`. So a job never joins a sealed or notified batch.
- A job's `source` is its batch's: `app` for batches the PWA creates, `api` for auto-joined API batches, `inbox`.
- Jobs carry `position`: the app's capture index, else arrival order. Ties sort by `created_at`.
- Two simultaneous first uploads can start two batches, and so two notifications. That's accepted.

### 1.5 Grouping and limits

One request is one card (front first); `split=true` makes each image its own card. Limits are code constants unless
named:

| Limit | Value |
|---|---|
| Per file | 30 MiB |
| Per request | `AI_INGEST_MAX_UPLOAD_MB` (100); JSON bodies 45 MiB |
| Images per request / pages per card | 20 / 4 |
| Pixels | 100 megapixels, checked before decoding |
| Processing jobs per group | 200 |

Formats are recognised by magic bytes only: JPEG/MPO, PNG, WebP, HEIF/HEIC, AVIF and TIFF (first frame). PDF gets
`pdf_not_supported`, since no rasterizer is installed.

## 2. Intake normalization and storage

Intake runs **inside the upload request**, so a job row exists only once its pages are on disk, and the uploaded
bytes, GPS included, never outlive the request. It costs about 0.3 s per phone JPEG and 1.2 s per 12 MP HEIC. At
most two run per process: they go through an intake `CapacityLimiter(2)` (`anyio.to_thread.run_sync(…,
limiter=…)`), so waiting uploads wait on the event loop rather than holding the default thread pool's tokens.

`images.normalize_page(raw: BinaryIO, page_dir, index, *, original_filename) -> PageMeta` takes an open binary file:
a multipart part's `SpooledTemporaryFile` (it has no path, F17), a `BytesIO` of decoded base64, or the inbox's
`O_NOFOLLOW` file object.
1. Sniff 16 bytes; reject anything not listed in §1.5. Hash the stream (`raw_sha256`, `raw_bytes`), then seek back.
2. `Image.open(raw, formats=[…])` (the default opener also tries EPS and PSD). Check `width × height` **before**
   `load()`, then `load()`, which is what catches truncated JPEGs. Importing `mealie.pkgs.img` registers HEIF.
3. Frame 0 (MPO and TIFF), `ImageOps.exif_transpose`, transparency flattened onto white, `convert("RGB")`.
4. Write, each to a temporary name in the same directory and then `os.replace`d:
   - `page.jpg`: long side at most 4096 (never upscaled), quality 90, ICC kept, **no EXIF, XMP or GPS**;
   - `view.jpg`: long side 2048, quality 90. Both the model and the review page see it, so crop fractions line up;
   - `thumb.webp`: `Image.thumbnail` to 480 px, aspect kept (not `PillowMinifier`'s 300 px centre crop).
5. Return `PageMeta {index, width, height, view_width, view_height, rotation, rotation_source (none | ocr | user),
   oriented, raw_sha256, page_sha256, original_filename (sanitized), format, raw_bytes, ocr: {text, confidence} |
   null}`.

**Layout:** `DATA_DIR/groups/<group_id>/ai-ingest/<job_id>/pages/<n>/{page.jpg,view.jpg,thumb.webp}`. `groups/` is
created at boot, backed up with the rows and restore-safe (F14). It's never served without auth and untouched by
admin maintenance. Never `.temp` (wiped), `recipes/` or `users/` (served without auth). Names are server-made; iOS
calls every capture `image.jpg`.

**Writes happen under the ingest write lock** (§3.9). Intake holds it from `create_job_dir` through the row insert;
tasks, rotate, commit, discard, eval-case saves and the purge hold it for their file work. Only intake creates job
directories; the others write into directories that exist, through a temporary name and `os.replace`. A restore
therefore never meets a directory or file created mid-copy.

**Duplicates:** `source_sha256` is the SHA-256 of the card's ordered page `raw_sha256`s, so the same front sent
later with its back isn't a duplicate. It's checked against the household's jobs (committed ones included) in the
insert's transaction, unless `allowDuplicate=true`. Each page keeps its own `raw_sha256`.

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
| `ready` | a draft exists | edit, reread, reextract, rotate, commit, discard, save as eval case |
| `failed` | first extraction failed | retry, rotate, discard |
| `committing` | commit in progress; `commit_started_at` is its lease | resumes automatically |
| `committed` | `recipe_id` set | view recipe, save as eval case (until the files are purged) |

- `task_state` is `NULL` (idle), `queued` or `running` (held by `lease_token` until `lease_expires_at`). `task_kind`
  is `extract` (first extraction, retry, re-extract) or `reread`.
- **The first extraction creates the draft.** A re-extract on a draft nobody edited (`draft_version =
  extracted_version`) replaces it; on an edited draft it becomes a whole-card **proposal**. A re-read always adds a
  proposal (§6.6).
- **Every new task starts clean:** `enqueue_task` (retry, re-extract, re-read) sets `attempts=0`,
  `rate_limit_retries=0`, `not_before=NULL` and `cancel_requested=false`, conditional on `task_state IS NULL`. The
  counters belong to one task, not the job's history.
- **Commit and discard cancel any pending task** on a `ready` job by clearing the task columns (§3.5): a re-read's
  result is advice, so it never blocks a commit.
- Discard is a hard delete of the row and its directory.

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
  (F12). Re-reads have priority 0 and extractions 10. Each process also keeps **one slot only re-reads use**, so a
  reviewer's re-read starts at once even when both extraction slots are busy with a batch (§3.4).
- **Run:** a **daemon thread** per task with its own event loop, registered as `(loop, asyncio_task, token,
  deadline)`. Not a `ThreadPoolExecutor`: its threads are joined at exit and would hold a container stop behind a
  provider call.
- **Heartbeat** every 20 s: `UPDATE … SET lease_expires_at=now+120 s WHERE lease_token IN (:held) AND
  task_state='running'`, then read `cancel_requested` and which tokens still exist. A token that's gone (commit,
  discard, sweep) cancels its task. Liveness comes from the dispatcher, not the task's loop, which can be busy in
  PIL, NLP or Tesseract for seconds.
- **Sweep** every tick: `running` rows past `lease_expires_at` go back to `queued` while `attempts < 3`. After three,
  the poison guard ends the task by status: a `processing` job becomes `failed` with `interrupted`; a `ready` job
  stays `ready` with its task cleared and `error_code=interrupted` (a banner). Each is its own transaction, fenced on
  the old token.
- **Housekeeping** every 60 s: seal idle batches and send due notifications (§8), resume stale commits (§7).
- **Inbox scan** every 30 s (§1.3). **Purge** once a day per process, first run 10 minutes after boot (§16).
- **Paused** while the restore marker is set: no claims, heartbeats, sweeps, inbox scans, housekeeping or purges
  (§3.9).
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

A backup taken while a task runs, restored within that same claim's lifetime, brings back the live token, and the
task's result can land on the restored row. It's the same claim's result for the same restored pages, so it's
harmless; this is stated, not prevented.

### 3.4 Concurrency

`AI_INGEST_CONCURRENCY` (2) task threads per process for any task, plus one that only re-reads use; the total is
that times `UVICORN_WORKERS` (documented). There is no cross-process per-group cap: it needs
`pg_advisory_xact_lock` (F12), and `UVICORN_WORKERS` defaults to 1. The 200-job quota bounds the queue. A task holds
a pooled connection only between awaits (§3.7).

### 3.5 Cancellation

- **`POST …/cancel`:** a queued task is cleared by a conditional update: a `processing` job becomes `failed` with
  `cancelled`, a `ready` job stays `ready`. A running task gets `cancel_requested=true`.
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
| `AIProviderLimitReachedError` | `limit_reached` |
| `OpenAINotEnabledException`, or no image provider and no OCR | `ai_not_enabled` |
| `AIProviderLocalOnlyError` | `local_only_unavailable` |
| `NoRecipeDataError`, `contains_recipe=false` | `no_recipe_found` |
| other provider errors | `provider_failed` with `{detail: describe_provider_error(cause)}`, never `str(e)` (it can hold the provider's response body) |
| the job's household is gone | `owner_missing` |
| 3 lease expiries / cancel / deadline / anything else | `interrupted` / `cancelled` / `timeout` / `internal_error` (logged with the job id) |

Commit adds `commit_invalid` (the draft no longer validates into a `Recipe`) and `commit_interrupted` (§7). Those
fourteen are `IngestErrorCode`. Codes and params are stored, and the frontend translates
`recipe-ingest.error.<code>`. Failed first extractions get **Retry** (also **Retry failed** per batch). A failed
re-read or re-extract leaves the job `ready`, with the code on `error_code` shown as a dismissible banner.

### 3.7 Inside a task

`runner/worker.py` `run_task(job_id, token)` in the task thread:
1. Read the job in a short session; stop if the fence fails.
2. `set_locale_context(get_locale_provider(job.locale), get_locale_config(job.locale))`. `locale` is the uploader's
   `Accept-Language`; inbox jobs use en-US.
3. `with ai_call_policy(AICallPolicy(local_only=job.local_only, job_id=job.id))` (§10).
4. A dedicated **AI session**: `session_context()` → `get_repositories(session, group_id=…, household_id=…)` →
   `JobOpenAIService(repos)`, used only for routing reads, the usage log and read-only lookups. **Job-state writes use
   their own short sessions** (F11). The cross-read shares this service and session (§4.1): each synchronous block
   runs to completion between awaits on the task's one thread, so the two reads never use the session at once.
5. **No transaction stays open across an await** (F11). `JobOpenAIService.runtime` is a `JobAIRuntime` whose
   `candidates()` and `record_attempt()` call the base method and then commit the dedicated session. The second
   matters because the usage write's `refresh` reopens a transaction and the runtime goes straight from a failed
   provider to awaiting the next. The pipeline's other awaits that follow a read on that session (OCR in a thread,
   the parser) call `end_transaction(session)` (in `pipeline/service.py`) first, which commits only if
   `session.in_transaction()`. The base `AIRuntime` keeps its request-path behaviour: only the job's own session is
   ended, and nothing is ever rolled back.
6. The handler (`tasks.handle_extract` or `tasks.handle_reread`) returns a result and writes nothing. Then the
   fenced finalize; then, after a first extraction, `events.maybe_notify_batch(batch_id)` (§8).

**Progress:** `CardWorkflowContext.report_progress` stores **keys**, at most one write a second:
`recipe-ingest.progress.orienting`, `reading-card`, `reading-card-ocr`, `cross-reading`, `structuring`,
`linking-ingredients`, `suggesting-organizers` (upstream step keys are mapped to these).

### 3.8 Shutdown, multiple workers, dev reload

- **Shutdown** (lifespan `finally`): stop claiming, cancel running tasks through their loops, wait up to 5 s (Docker
  allows 10), then release every lease still held (`queued`, lease cleared, `attempts - 1`, so deploys don't burn
  retries).
- **Several workers:** every process runs a dispatcher; claims, sweeps, seals, notifications and purges are all
  conditional updates, so they don't conflict.
- **Dev reload** kills threads on every save; lease expiry recovers their jobs.

### 3.9 Pausing for a backup restore

A restore replaces `groups/` and `recipes/` while background work continues (F14). A directory or file created
between `rmtree` and `copytree` aborts the restore after the database was already replaced. A check made when a
request starts isn't enough: an upload can take a minute to arrive, and only then does intake create its directory.
So the pause has two parts: a marker that stops new work, and a lock that waits for work already writing.

- **The marker:** `DATA_DIR/.ai-ingest-paused`, holding the time it was last refreshed. It's a root-level file, so
  `_copy_data` leaves it alone, and every worker process sees it. The restore refreshes it every 60 s; it's honoured
  while younger than 5 minutes, so a long copy never outlives its pause and a crash mid-restore pauses ingestion for
  at most 5 minutes.
- **The write lock:** `DATA_DIR/.ai-ingest-lock`, taken with `fcntl.flock` (per open file, so it works across
  threads and processes). Every fork section that writes under `groups/` or `recipes/` runs inside
  `storage.ingest_write()`: check the marker, take `LOCK_SH | LOCK_NB` (failing means a restore holds it), check the
  marker again, write, release. Any failure raises `IngestPaused`. Writers never block on the lock. The sections
  are intake (from `create_job_dir` through the row insert), each inbox file, a task's page writes (orientation),
  rotate, commit (claim through finish), discard, eval-case saves and each purged job.
- **`storage.pauses_ingest`**, a decorator on `BackupV2.restore` (one line in an already fork-modified file):
  write the marker and start its refresher, then take `LOCK_EX`, polling for up to 120 s while in-flight writers
  finish. If they don't, it raises before the restore has touched anything ("ingestion is busy, try again"). Then
  it runs the restore and, in `finally`, removes the marker and releases the lock. It covers every caller of
  `restore`.
- **While paused:**
  - the dispatcher doesn't claim, heartbeat, sweep, scan the inbox, do housekeeping or purge;
  - ingest routes that write files (upload, rotate, commit, discard, eval-case save) answer `503
    paused_for_restore` with `Retry-After: 60`, both at the start and when their write section can't start;
  - a running task raises `IngestPaused` at its next write section and writes nothing to the database: its
    finalize, or the release of its lease, waits (polling every 5 s, within its deadline) until the marker clears,
    then applies fenced on its token (§3.6). A restored row carries a different token, so the result is dropped,
    except in §3.3's harmless case.
- **Missing files** are transient only while paused. Outside a pause a missing job file fails the task with
  `files_missing` (§3.6), so a job can't stay `processing` forever and hold its batch's notification.
- Where `flock` isn't supported (some network filesystems), the marker alone applies and a warning is logged at
  startup.
- What isn't covered: upstream's own writers (a recipe image upload, a migration) race a restore as they always have.

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

0. **Orient** (`pipeline.orient_page(page) -> PageMeta`, worker and eval, once per page while `oriented` is false):
   with `ocr.is_available()`, `ocr.extract_text(page.jpg, min_ratio=ORIENT_MIN_RATIO)` returns rotation, text and
   mean word confidence in one run (2-4 s). It turns the page only when the best probe score is at least 1.5× the
   upright score (§4.4); the rewrite goes through `images.rotate_page_files` (`rotation_source="ocr"`) inside the
   write lock. Text and confidence go into `PageMeta.ocr`. Without Tesseract (upstream builds) nothing is rotated;
   the review page offers **Rotate** (§4.4).
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
     `-min-original.jpg` side file (F16).
   - `CardOCRCompiler(OCRImageCompiler)`: reuses `PageMeta.ocr` (running Tesseract only if it's missing), with the
     same rules and schema, on the **default** slot. Its `can_compile()` is false when the image compiler failed
     with `exceptions.RateLimitError`: a rate-limited card waits and is read properly, rather than read now with OCR.
     A monthly limit, a missing image provider or a local-only refusal still falls back, since the default slot may
     have other providers.
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
   `card-build-rules.txt`, then upstream's `to_recipe` and `cleaner.clean`.
4. **`ResolveOrganizersStep()`** (fast slot, optional), configured through the context's
   `WorkflowOptions(resolve_organizers=options.suggest_organizers, attach_organizers=False,
   create_new_organizers=False)` (F5). `options_for_group` sets `suggest_organizers=False` when the group has no
   tags, categories or tools (`OrganizerResolver(repos).existing_names()`), so card text isn't sent for nothing.
   Names matching an existing organizer go into the draft as suggestions; the rest are dropped.
5. **No `TranslateRecipeStep`:** it always runs when enabled, rebuilds every ingredient and sends text to the fast
   slot.
6. Await the cross-read (a failure becomes the info flag `cross_read_failed`), then `normalize_ingredients` (§5)
   with an `IngestMatcher`, which only reads, then `compute_flags` (§4.6).

Per card: one image call, one default call and one fast call (none without organizers), plus one image call with
cross-read on. `finalize_scraped_recipe` is never called, so no `recipes/<uuid>/` appears before commit.

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


class OpenAIRecipeCardTranscript(OpenAIBase):
    """Everything written on the card as plain text, one physical line of writing per line"""

    contains_recipe: bool
    text: str


class OpenAIRecipeCardRegion(OpenAIBase):
    readable: bool
    text: str
    alternatives: list[str] = Field(default_factory=list)
```

On Claude (measured with `anthropic_adapter.output_schema`): the transcription has 4 optional parameters and 0
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

`card-build-rules.txt`: keep `[illegible]` and `[blank]` where they are and never replace them; keep ingredient lines
exactly as transcribed; don't put the attribution in the description.

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
  (1.5) times the 0° score. Measured on the banana card (F7): sideways 27×, upside down 2.1×, upright and printed
  cards stay put. Text is read at the chosen rotation, so it matches the stored pages.
- **Model-reported rotation isn't used:** providers disagree on what "turned left" means, and a wrong turn makes a
  second read worse.
- Without Tesseract the review page offers **Rotate** (a synchronous threadpool call that rewrites `page.jpg`,
  `view.jpg` and the thumb in under a second; `409` while a task is active), then **Read again**. The docs say
  Tesseract (`INSTALL_OCR=true`, the fork image's default) is needed for flat-on-the-table batch capture. CI
  installs it, so the orientation tests run there.
- Region fractions always refer to the upright page, so cropper, server crop and views agree.

### 4.5 Cross-read (opt-in)

The most damaging error is a number the vision read invents in a blank: the transcription itself contains it, so no
check against that transcription can see it (F7). A second read is the only signal that can.

- **Setting:** `recipe_ingestion_settings.cross_read`, **off by default**. Re-extracts honour it; re-reads don't use
  it. The eval decides whether the default changes (§11.4).
- **Alignment** (`pipeline/crossread.py`, pure): the transcript is split into lines. Each draft ingredient takes its
  best transcript line by `token_set_ratio` on letters only (at least 60). Each step is compared with windows of up
  to 4 consecutive transcript lines (`partial_ratio` at least 70), since steps wrap. Lines with no match are left
  alone.
- **Salient tokens:** numbers (unicode fractions and mixed numbers as rationals, ranges kept), the case-sensitive
  unit tokens of the shorthand table (§5), temperatures, and the two markers.
- **Flags:** a number, case-sensitive unit or temperature in the draft line that the aligned window lacks raises
  `read_disagreement` (warning, the window's text offered as an alternative). A window holding `[blank]` where the
  draft has a number raises `blank` (error, `source: cross_read`). Agreement never lowers a flag; both reads can
  share a mistake.
- The review page's checks line says so: "Read by Claude Sonnet 5.5 · checked against a second reading" (§6.4).

### 4.6 Flags: how confidence is computed

`pipeline/flags.py` `compute_flags(draft, extraction, resolutions) -> list[CardFlag]` is pure. The server runs it
after extraction **and on every save**, so flags always describe the current draft. Flags are keyed to a field plus
the ingredient's `reference_id` or the step's `id`, never an index (F4), with a stable id `"<kind>:<field>:<ref>"`.

| Kind | Severity | Source | Raised when |
|---|---|---|---|
| `illegible`, `blank` | error | marker | a field contains `[illegible]` / `[blank]` (the real card's microwave time) |
| `blank` | error | cross_read | the second read has `[blank]` where the draft has a number (§4.5) |
| `missing_name` | error | validator | the name is empty |
| `unsure` | warning | model | an `unsure` entry matches the line (partial ratio ≥ 85); carries the alternatives |
| `not_on_card` | warning | validator | a number in the line (digits, fractions, ½-style glyphs, normalized) is nowhere in the transcription |
| `marker_dropped` | warning (card) | validator | the transcription has more markers than the draft: the structuring step filled a gap |
| `read_disagreement` | warning | cross_read | §4.5 |
| `check_parse` | warning | parser | NLP average confidence < 0.85 (`flag_rules.REVIEW_CONFIDENCE`, matching the parse dialog's `confidenceThreshold` in `use-parse-ingredients-dialog.ts`) |
| `unit_unclear` | warning | parser | a quantity, no unit, and a 1-3 letter token after the number (the lost "T." case) |
| `implausible_amount` | warning | validator | more than 20 tsp, tbsp or cups, or `11/2`-style fractions (offers "1 1/2") |
| `implausible_temperature` | warning | validator | °F outside 200-550 or °C outside 90-290 in a step |
| `empty_section` | warning (card) | validator | no ingredients or no steps (commit would add upstream's placeholder) |
| `read_by_ocr` | warning (card) | ocr | the OCR fallback read the card; params carry its confidence |
| `cross_read_failed` | info (card) | cross_read | the second read failed, so fewer checks ran |
| `shorthand_read` | info | parser | the pre-normalizer expanded "T.", "t." or "C." |
| `not_parsed` | info (card) | parser | the card isn't in English, so its lines stay as text (§5) |
| `new_food`, `new_unit` | info | parser | the name isn't linked; §5 says what commit does |

**Resolving flags:**
- **Errors block commit** (`422` listing them) until each is fixed or **kept**. **Keep as written** is one tap. For a
  marker, commit turns `[blank]` into `___` and `[illegible]` into "(unreadable)" in the job's locale; for a
  cross-read `blank` it keeps the number. `missing_name` can only be fixed.
- **Warnings are highlighted** and can be dismissed with **Looks right**; they don't block. Infos show quietly.
- Resolutions are stored by flag id. A marker flag whose marker is gone resolves itself; parse flags drop off a line
  once it's edited (each ingredient keeps an `extracted_hash` of its parsed fields).
- `HIGHLIGHTED_SEVERITIES = {error, warning}` is the one definition the page (through the DTO) and the eval use.
  A card with no unresolved error or warning is **clean**.

### 4.7 Region re-read

- `POST …/reread {page, x, y, width, height, target: {field, ref}}` takes fractions of the upright page
  (`x+width ≤ 1`, `y+height ≤ 1`, sides ≥ 0.02), queues a `reread` task and returns `202`. It runs in the re-read
  slot (§3.4), so it starts at once. A job with an active task answers `409 {detail: {code: "busy"}}`; the page
  queues further re-reads itself (§6.5).
- `pipeline.reread_region` crops `page.jpg` (4096 px: up to twice the detail of the whole-card read) with a 3%
  margin, upscales a crop under 1000 px with LANCZOS (at most 3×), and sends **only the crop** to the image slot with
  `card-reread.txt` and `OpenAIRecipeCardRegion`. With no image provider but Tesseract present, the crop is OCR'd and
  the proposal says so. The crop is built in memory; nothing is written to disk.
- A user's crop works with every provider, local ones included. Model bounding boxes differ in format and accuracy,
  so none are used.

## 5. Ingredient normalization and linking

`pipeline/ingredients.py` `normalize_ingredients(recipe, *, repos, translator, matcher, language)`, in the task
thread:

1. Strip lines and drop empty ones (one empty string makes NLP raise for the whole call). Keep section titles. Lines
   that still hold a marker aren't parsed; they stay as notes, flagged.
2. **Shorthand pre-normalizer** (`shorthand.py`), case-sensitive, only on the token right after the leading quantity
   ("don't" and "t-bone" are untouched), and only when the card's language is English or unknown:

   ```python
   QTY = r"(?:\d+\s+\d+/\d+|\d+/\d+|\d+(?:[.,]\d+)?|\d*\s*[½⅓⅔¼¾⅛⅜⅝⅞])"
   SHORTHAND = re.compile(
       rf"^(?P<lead>\s*[-•*]?\s*{QTY}(?:\s*(?:-|to)\s*{QTY})?\s*)"
       r"(?P<unit>TBSP|TBS|TB|Tbsp|Tbs|Tb|T|tsp|ts|t|C|c|pkg|Pkg)\.?(?=\s|$)"
   )
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
   flour". The eval and the cross-read import this table rather than keeping their own.
3. **NLP only:** `get_parser(RegisteredParser.nlp, …)`, `parser.data_matcher = matcher`, `await parser.parse(lines)`.
   Brute links "pkg." to kilogram, and the AI parser builds its own `OpenAIService` (escaping eval pinning), so
   neither is used. Non-English cards skip parsing: lines stay as text with the `not_parsed` info flag.
4. Restore `original_text` to the raw card line (NLP overwrites it with its input) and the titles; recompute
   `display` (it goes stale after matching).
5. Store `{reference_id, title, original_text, quantity, unit: {id|null, name}, food: {id|null, name}, note,
   display, parse_confidence, extracted_hash}`.

`IngestMatcher` (`matching.py`) extends Phase 1's alias-eager `FoodMatcher` (8 queries, not one per food) with
alias-eager units. One per task, and a fresh one per commit.

**At commit:** every food and unit id is checked against the group's matcher; unknown or foreign ids become names
(F20). Names are matched again exactly (name, plural or alias), so foods created since extraction aren't duplicated.
A missing **unit** is created. A missing **food** is created only if the committer can organize; otherwise the line
keeps quantity and unit and the food name leads its note. The review page labels these chips "New food" or "Kept as
text" from `permissions.canCreateFoods`.

## 6. Review page

### 6.1 Routes and order

- **Review:** `/g/<group>/recipes/cards/<jobId>`, with `definePageMeta({middleware: ["group-only"], key: route =>
  route.fullPath})` so each card remounts.
- **Start of a batch:** `/g/<group>/recipes/cards/review?batch=<id>` redirects to the batch's first `ready` card, in
  `position` order, that has an unresolved error or warning, else its first `ready` card, else the queue filtered to
  the batch. Notifications open this URL.
- Cards are reviewed in **capture order**, so the screen matches the stack in your hand. **Commit & next** goes to
  the batch's next `ready` card (wrapping round to skipped ones); after the last, to the queue with a batch summary.

### 6.2 Phone layout (xs/sm)

1. **Header:** "Card 3 of 10 · 2 to check", previous/next, and ⋯ (Rotate, Read whole card again, What the card says,
   Save as eval case (managers), Discard).
2. **Card strip:** sticky, about 32% of the height; tap for full screen (upstream's `RecipeImageLightbox` on
   `page.jpg`), swipe down to collapse it to a 56 px bar. Front/Back toggle.
3. **Checks line:** "Read by Claude Sonnet 5.5 · checked against a second reading", or "Read with OCR (confidence
   49%). Check every line."
4. **Needs a look (2):** one item per unresolved error or warning, in reading order. Each shows the line as read with
   the problem highlighted, then:
   - alternative chips (tap to apply);
   - a one-line input for a blank ("___ minutes": the typed value replaces the marker);
   - **Re-read** (the region dialog, targeted at the line);
   - **Keep as written** (errors) or **Looks right** (warnings);
   - **Edit** (jump to the row).

   Resolved items collapse with a check mark; a re-read's proposal appears inside its item with **Use** and
   **Dismiss**.
5. **The recipe:** collapsible sections. Name and details; ingredient rows that read like the card ("1 tbsp coconut
   oil (melted)") with link-status icons, tapping one expands quantity, unit and food autocompletes (from the group's
   unit and food stores, with no create button) and the note, plus the card's original line; steps; tags,
   categories and tools as upstream `RecipeOrganizerSelector`s (`:show-add="false"`, suggestions preselected);
   upstream `RecipeNotes`; the attribution; a **Use the card photo as the recipe image** switch (on by default).
6. **Bottom bar** (fixed, safe-area inset): **Skip** · **Commit & next**. While errors remain the primary reads **1 to
   fix** and scrolls to it. After commit, `router.replace` to the next card and a snackbar "Added *Banana Mug Cake*".

A clean card costs **one tap**; a flagged card about two per flag.

### 6.3 Desktop layout (md+)

The card viewer is on the left (5/12, sticky: `top: 48px; height: calc(100dvh - 48px)`) with Front/Back, **Rotate**,
**Re-read an area** and the transcription toggle. The editor (7/12) has the same parts as the phone, with "Needs a
look" first and the proposal banner under it.

**Keyboard** (`useMagicKeys`; ignored while typing, except the first): `Ctrl/⌘+Enter` commit and next;
`Alt+↓`/`Alt+↑` next or previous flagged field; `R` re-read an area; `[` `]` previous or next page; `Esc` closes the
selector.

### 6.4 What is highlighted, and why

- A field with an unresolved error (red, blocks commit) or warning (amber) gets a coloured edge **and** an icon, with
  its explanation in the "Needs a look" item ("The card leaves a gap here. Fill it in or keep it blank."). The
  reasons are exactly §4.6's kinds; nothing is highlighted without a stated cause.
- The checks line (§6.2) says which reader produced the draft and whether a second read checked it, from
  `extraction.read`.
- A group recipe whose slug matches the draft's name (checked on each GET) shows a "possible duplicate" banner with a
  link. Commit would make "Name (1)".
- When the household's new recipes default to public, a note says the card photo will show on the public recipe
  (asset and cover are served without auth, F19).

### 6.5 Re-read a region

`IngestRegionDialog` uses `vue-advanced-cropper`'s `Cropper` directly (`:canvas="false"`,
`:check-orientation="false"`, since the images are upright and EXIF-free) in a `BaseDialog` (full screen on phones),
`refresh()`ed after the transition. It sends fractions and the chosen field, preselected when opened from a field or
flag. The result arrives as a proposal. While a task is active, further re-reads wait in a client-side queue and
are sent one by one as the state poll shows the job idle.

### 6.6 Edits, autosave, versions

- A debounced `PUT` (1.5 s) carries `draftVersion`, the draft, flag resolutions and resolved proposal ids. The server
  validates the draft as a `CardDraft` (the whole whitelist: no id, slug, assets, settings, rating or extras exist on
  it), recomputes flags and writes with `WHERE id=:id AND draft_version=:v AND row_version=:rv` (§3.3), bumping both.
  A stale `draftVersion` is **409 `{detail: {code: "version_conflict", current}}`**, with no `message`, so the axios
  interceptor doesn't toast over the page's own **Reload this card** dialog (at most 1.5 s of typing lost). A
  changed `row_version` alone (a proposal landed) is retried on the server. The response carries the new version and
  flags, with a "Saved" indicator.
- Tasks never touch the draft or its version, except a re-extract on an unedited draft, which replaces it (§3.1).
  The editor is read-only while a re-extract runs, so that can't conflict. A re-read finishing mid-edit causes no
  conflict. Accepting a proposal is an ordinary edit.
- Commit waits for the pending save and sends the version it returned.
- While a task is active the page polls `GET …/state` every 2 s.

### 6.7 Queue page

Below capture (§1.1): batches newest first, each with its cards as rows (thumbnail, name, status chip: "Reading…",
"Ready · 2 to check", "Failed: no recipe found") and **Retry**, **Discard** or **Review**. Per batch: **Review**
(opens `review?batch=`) and **Retry failed**. Recently added cards (7 days) link to their recipes.

It polls `GET /api/ai/ingest/jobs?batchId=…` every 3 s only while something is processing or uploading and the page
is visible (the shopping list's pattern), and at once after uploads and commits. Items carry no top-level `message`.
The sidebar count is fetched once by `DefaultLayout`, then updated through a shared ref.

### 6.8 Components

All fork components in `components/Domain/Ingest/` edit the narrow `CardDraft`, never a `Recipe`:

| Component | Does |
|---|---|
| `IngestCardViewer` | pages, Front/Back, Rotate, zoom through `RecipeImageLightbox` |
| `IngestNeedsALook`, `IngestFlagItem` | the flag list and its one-tap fixes |
| `IngestProposalBanner` | Replace / Add to the end / Dismiss; Use the new reading / Keep mine |
| `IngestRecipeFields` | name, description, yield, servings, times, attribution |
| `IngestIngredientList`, `IngestIngredientRow` | compact card-like rows that expand into an editor without "create" |
| `IngestStepList` | plain title and text fields (upstream's step editor needs a saved slug for images) |
| `IngestRegionDialog` | the cropper |
| `IngestReviewBar`, `IngestTranscription`, `IngestEvalCaseDialog` | |

Reused upstream parts: `RecipeOrganizerSelector`, `RecipeNotes`, `RecipeImageLightbox`, `BaseDialog` (F23).

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
4. **Files** into `recipes/<id>/`: each `page.jpg` as `assets/recipe-card-<token>-<n>.jpg`. With the draft's cover
   switch on, `RecipeDataService(id).write_image(view.jpg of page 1, "jpg")`. The directory can't belong to another
   recipe. These are the only directories commit creates, and only under `recipes/<new id>/`.
5. **Build and create** (`draft_to_recipe`):
   - from the draft: name, description, yield and servings, times, ingredients (re-linked per §5), steps (title,
     text), notes with the attribution first (a note titled "From"), and markers of **kept** flags converted
     (§4.6);
   - organizers looked up **group-scoped by id**; unknown ones dropped with a warning, none created;
   - `assets` listing the card files ("Recipe card", "Recipe card (back)", `mdi-file-image`), named from the stored
     token, so a resumed commit names them identically;
   - `settings` built from the household preferences as `create_one` would, **but `show_assets=True`** (`public`
     untouched);
   - `id` from step 2, `slug=""`;
   - never from the draft: id, slug, assets, settings, rating, extras, owner ids.

   Then `RecipeService(repos, user, household, translator).create_one(recipe)`, with the committer's translator (the
   job's locale when housekeeping resumes it) and `get_repositories(session, group_id=…, household_id=…)`.
6. **Cover key:** `repos.recipes.update_image(slug)` when the cover was written (F21).
7. **Finish:** `UPDATE … SET status='committed', recipe_id=:id, committed_at=now WHERE id=:id AND
   status='committing' AND commit_recipe_id=:id`. Respond `201 {recipeId, slug, nextJobId, warnings}`. **Whichever
   call wins this update** (`rowcount == 1`) publishes upstream's `recipe_created`: a request through
   `BackgroundTasks`, as `_publish_recipe_created` does; housekeeping inline in its thread. So a resumed commit
   publishes it too, exactly once (at most once if the process dies right after the finish).

**Failure and recovery:**
- A validation error **before** `create_one` (the draft no longer validates into a `Recipe`) returns the job to
  `ready` with `commit_invalid` and the field errors, and removes `recipes/<id>` **only if no recipe row has that
  id**. A directory with a row is never deleted.
- **Once `create_one` has been called, the job never goes back to `ready`.** Any exception from there leaves it
  `committing` for recovery.
- `commit_started_at` is a lease. A repeat request, or the dispatcher's housekeeping, wins `UPDATE … SET
  commit_started_at=now WHERE id=:id AND status='committing' AND commit_started_at < now - 120 s` and resumes at
  step 3 as `committed_by`. If that user is gone the job returns to `ready` with `commit_interrupted` (only possible
  when no recipe row exists). Foods and units created before a crash are found by the exact re-match.
- A double tap gets `409` while `committing` (the page polls) or `200` with the same recipe once `committed`.
- A name collision isn't an error: `create_one` makes "Banana Mug Cake (1)", after the review page warned.

**Assets:** EXIF-free JPEGs named like `recipe-card-Zk3…Q-1.jpg`. The media route has no auth or privacy check
(F19), so the name is the capability. `show_assets` makes the card visible on the recipe; publicity still follows
the household. The cover has the same exposure as any recipe image, which the review page says for public
households (§6.4).

The row is kept after commit (provenance, the duplicate hash). Its files go after the retention period (§16).

## 8. Events and notifications

```python
class AIEventTypes(Enum):
    recipe_ingestion_ready = "recipe_ingestion_ready"


class AIEvent(Event):
    event_type: AIEventTypes  # type: ignore[assignment]


class EventIngestionReadyData(EventDocumentDataBase):
    document_type: EventDocumentType = EventDocumentType.generic
    operation: EventOperation = EventOperation.info
    batch_id: UUID4
    job_ids: list[UUID4]
    ready_count: int
    needs_attention_count: int  # ready cards with an unresolved error or warning
    failed_count: int
    review_url: str  # BASE_URL + /g/<group-slug>/recipes/cards/review?batch=<id>
```

- **Opt-in per notifier** in `ai_event_notifier_options` (a side table with a backref cascade on
  `GroupEventNotifierModel`). `AIEventAppriseListener(AppriseEventListener)` keeps the household's enabled notifiers
  whose row is on, applies `update_urls_with_event_data`, and `ApprisePublisher.publish` sends. The message is an
  `EventBusMessage(title, body)` built from the backend `recipe-ingest` namespace in the batch's locale: "Recipe cards
  ready" / "10 cards are ready to review (2 need a look, 1 failed)." It never goes through
  `EventBusService.dispatch` (F22).
- **Once per finished batch:** `maybe_notify_batch` (called after a first extraction, when a batch is sealed, and by
  housekeeping) runs `UPDATE recipe_ingestion_batches SET notified_at=now WHERE id=:b AND sealed_at IS NOT NULL AND
  notified_at IS NULL AND created_at > now - 24 h AND NOT EXISTS (SELECT 1 FROM recipe_ingestion_jobs WHERE
  batch_id=:b AND status='processing')` and publishes only on `rowcount == 1`. Two jobs finishing at once in two
  processes can't both win. Housekeeping catches batches sealed after their last job. Failed-only batches notify
  too. A job never joins a sealed batch (§1.4), so a notified batch never gains a card.
- **Batches created more than 24 h ago never notify**, so a restore doesn't replay old notifications.
- **At most once:** `notified_at` is set before publishing, so a crash loses a notification rather than doubling it.
  Apprise blocks, so it runs in the task thread or the dispatcher's thread limiter.
- **Counts and a link only**, no names or card text: notifications leave the server.
- Commit publishes upstream's `recipe_created`, so existing notifiers (and Phase 4's nutrition listener) see it.
- **UI:** `HouseholdNotifierAIEvents.vue` adds **Recipe cards ready to review** under each notifier on the
  (advanced-only) notifiers page, saved through `GET/PUT /api/ai/notifiers/{id}/events`, with **Send test
  notification** (`POST …/events/test`). The group's Recipe cards settings card links there.

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

`document_data` is a JSON string with camelCase keys. Match on the event name, never a number. A `rest` sensor on
`GET /api/ai/ingest/jobs/counts` (with the HA user's token) gives a "cards waiting" dashboard tile.

## 9. Permissions and household scoping

| Action | Who |
|---|---|
| Upload (app, API, Shortcut) | any household member, with the `Authorization` header |
| View, edit, re-read, rotate, re-extract, commit | any member of the job's household; the committer owns the recipe |
| Discard | the uploader; anyone for inbox jobs; otherwise `can_manage_household` |
| Create foods at commit | `can_organize`; units need nothing; organizers are never created (the selectors' own "add" is hidden) |
| Page images | the job's household, through the fork route only (`Cache-Control: private`, `nosniff`; `<img src>` authenticates with the cookie) |
| Card settings | everyone reads; group managers (`checks.can_manage()`) write |
| Notifier toggles and test | the same checks as upstream's notifier routes, with the notifier loaded through the household-scoped repo |
| Eval cases (save, list, delete) | group managers |

`IngestRepos` (fork, not in `AllRepositories`) filters every query by the controller's group and household: another
household's job, images included, is a `404`. Group managers in other households don't see your cards. The worker
loads the job's household; if it's gone the task fails `owner_missing`.

## 10. Privacy: local only

**A per-job policy that fails closed.**

- **Setting it:** `recipe_ingestion_settings.local_only` (group managers) makes every card job local-only. Otherwise
  an upload or app batch can ask for it (`localOnly=true`, the privacy chip). The job stores `local_only` at intake,
  so a later settings change or provider deletion never loosens a queued job.
- **"Local" means both:** a manager switched on `ai_providers.runs_locally` ("Runs on my network"), **and** the base
  URL is set and every address it resolves to is non-public (`safehttp.transport.is_blocked_ip`: loopback, RFC 1918,
  link-local, CGNAT/Tailscale), cached 60 s. An empty base URL is never local: the OpenAI SDK reads
  `OPENAI_BASE_URL`, and Claude's default is `api.anthropic.com`. The address check alone can't tell a LAN proxy to a
  cloud API from a local model; the flag alone could be a mistake.
- **Enforcement:** `mealie/services/ai/policy.py` keeps `AICallPolicy(local_only, job_id)` in a `ContextVar`. The
  base `AIRuntime.candidates()` ends with `apply_policy(slot, providers)`, which filters **every slot** through
  `is_local_provider` under the policy and raises `AIProviderLocalOnlyError` when nothing is left. Being in the
  runtime, it covers every routed call a card causes, including code that builds its own `OpenAIService`.
  `ContextVar`s follow awaits and `asyncio.to_thread`. `EvalAIRuntime.candidates` applies it too.
- **No leak through the fallback** (F3): with no local image provider the compile step tries `CardOCRCompiler`, whose
  default-slot call is filtered the same way. With no local default provider the job fails `local_only_unavailable`
  before any card text leaves.
- **Pipeline code never passes `provider=`** (that bypasses the runtime). A test runs the pipeline under the policy
  and asserts every call went through `candidates()`. The cross-read uses the same service, so it's filtered too.
- **The eval runs under the policy:** each fixture's run is wrapped in `ai_call_policy(local_only=fixture.local_only
  or --local-only)`, so a `local_only` fixture can't reach a cloud provider even through a mis-set config.
- `record_ai_usage` stores the policy's `job_id` on each usage row (a new nullable column): per-job cost and which
  provider read each card, with no content.

**UI:** the provider dialog's **Runs on my network (local model)** switch, disabled with an explanation for an empty
base URL. `GroupRecipeCardSettings` (Group Settings) has **Keep recipe card photos and text on this server**, with
a readiness list for managers: the local providers per image, default and fast slot, a warning when image and
default both lack one, and providers marked local whose address isn't private ("won't be used"). The cards page has
the privacy chip; the review page shows a "Local only" badge. Re-reads and re-extracts inherit the job's policy.

**Elsewhere:** metadata is stripped at intake; raw uploads never outlive the request; job images are household-only;
asset names are unguessable; notifications and the voice tool carry counts only (§8, §12); logs carry job ids and
error codes, never card text, transcriptions or provider bodies (compiler failures are recorded with
`describe_provider_error`, never logged with a traceback, §4.1).

## 11. Eval harness

### 11.1 One pipeline

`eval_recipe_cards.py` gains `--pipeline card|import` (default `card`; `import` keeps today's numbers reproducible).
Per fixture, on temporary copies, under `ai_call_policy` (§10): `images.normalize_page` → `pipeline.orient_page`
(with Tesseract, unless `--no-intake-ocr`) → `pipeline.extract_card` with `EvalOpenAIService` and
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
- `--check` validates fixtures without running and doesn't need `--group`.
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
  correct | clean), the number a future "commit clean cards" button would need;
- tokens per (provider, slot), the answering model, prompt SHA-256s, the git commit, per-card standard deviation.

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
- **Save as eval case** (review page ⋯, managers): `POST …/jobs/{id}/eval-case {slug, verified}` writes
  `DATA_DIR/groups/<group_id>/eval-cards/<slug>.json` and `<slug>-<n>.jpg`. The slug must match
  `^[a-z0-9][a-z0-9-]{0,63}$` and not exist yet (`409`). Images are the normalized pages **turned back by their
  recorded rotation**, so orientation is still exercised, and EXIF-free. Expected values come from the reviewed
  draft; a field that had a `[blank]` at extraction keeps it and is listed in `blanks`. `verified_by_owner` comes
  from the tick; `origin.drafted_by` is recorded, and the report marks runs scored against the same provider's own
  drafts. It works for `ready` and `committed` jobs until their files are purged.
- **Managing them:** `GET /api/ai/ingest/eval-cases` lists them and `DELETE /api/ai/ingest/eval-cases/{slug}` removes
  one; the group's Recipe cards settings card shows the list. They sit under `groups/`, so they're backed up and
  restore-safe, and the retention purge never touches them.
- **Cleaning a raw photo for the repo:** `python -m mealie.scripts.strip_card_photo IN OUT` removes metadata
  losslessly in pure Python: it keeps only APP2 segments starting `ICC_PROFILE\0` (so the Display P3 profile stays
  and the MPF index goes), drops APP1 (EXIF, XMP), APP13, COM and MPO trailing frames, and writes a minimal EXIF
  holding only Orientation (`--drop-orientation` to leave even that out), so the eval still exercises EXIF transpose.
  No `jpegtran` or `exiftool` is needed. With `jpegtran` (Ubuntu and WSL: `sudo apt install libjpeg-turbo-progs`),
  `jpegtran -copy none -rotate 90 -perfect` reproduces the sideways view the fixture exercises, and without
  `-rotate 90` it gives an upright copy (the raw pixels are upright, F7). `-copy none` also drops the Display P3
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

One read tool joins the Phase 1 registry (`tools/ingest.py`, one registry line), so MCP and `/api/ai/tools` both
get it: `recipe_card_queue()` → `{ready, needs_attention, processing, failed}` with speech like "Seven recipe cards
are ready to review. Two need a closer look."

- **Counts only:** card names are card text, and the MCP client or HA conversation agent may be cloud-hosted, so
  names never go out, local-only or not.
- Household-scoped `ToolContext`, database work through `run_blocking`, `writes=False`.
- No write tool: ingest needs images, which voice clients don't have, and HA's inbox and REST paths already cover
  cameras.

## 13. Data model

One migration on `970cf50b85f4`, in upstream's hex style. GUID primary keys; JSON stored as `Text` through a fork
`JsonText` TypeDecorator (generalizing `ai_mcp.StringList`); `NaiveDateTime` timestamps; backref cascades declared in
the fork model. **No string value the fork generates is one `uuid.UUID` accepts** (F15): tokens are GUID columns,
asset tokens are base64url, hashes are 64 hex characters, `source_name` carries a `/` prefix (§2), and
`integration_id` is display-only. `title` is card text and shares upstream's exposure for recipe names: a title that
is a bare UUID would come back reformatted after a restore, and the next save rewrites it from the draft.

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
| `task_payload` | JsonText, nullable | region and target |
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
| `created_at`, `update_at` | `BaseMixins` | |

Indexes: `(task_state, task_priority, created_at)`, `(household_id, status, created_at)`, `(household_id,
source_sha256)`, `(batch_id, position)`, `recipe_id`.

**`recipe_ingestion_batches`:** `id`; `group_id`, `household_id` (FKs, indexed, cascades); `created_by` (GUID,
nullable, no FK); `source` (String(16)); `source_key` (String(255), nullable: the inbox folder); `locale`;
`last_upload_at`, `sealed_at`, `notified_at`; `BaseMixins`. Index `(household_id, source, sealed_at)`.

**`recipe_ingestion_settings`:** `id`; `group_id` (FK, unique, backref `uselist=False`, cascade); `local_only` (false);
`cross_read` (false); `BaseMixins`. **No row means defaults**; PUT upserts. A fork table, not a column on upstream's
`ai_provider_settings`: a client PUTting those settings without the field would switch local-only off.

**`ai_event_notifier_options`:** `id`; `notifier_id` (FK `group_events_notifiers.id`, unique, backref
`uselist=False`, `cascade="all, delete-orphan"`); `recipe_ingestion_ready` (false); `BaseMixins`.

**New columns:** `ai_providers.runs_locally` (Boolean, not null, `server_default` false, so existing providers fail
closed); `ai_usage_log.job_id` (GUID, nullable, indexed, no FK).

**`look_for_datetime` gains:** `not_before`, `lease_expires_at`, `task_started_at`, `commit_started_at`,
`committed_at`, `last_upload_at`, `sealed_at`, `notified_at`.

**Schemas** (`mealie/schema/recipe_ingest/`, codegen → `frontend/app/lib/api/types/recipe-ingest.ts`):
- pages and drafts: `PageMeta`, `PageOut`; `CardDraft`, `CardDraftIngredient`, `CardDraftStep`, `CardDraftNote`,
  `CardDraftRef {id: UUID4 | None, name}`; `ExtractionMeta`;
- flags and proposals: `CardFlag {id, kind, severity, source, field, ref, params, alternatives, resolution}`,
  `FlagResolution` (`kept`, `dismissed`); `CardProposal {id, kind: region | full, target, text, readable,
  alternatives, draft, createdAt}`;
- jobs and batches: `RecipeIngestionJobSummary`, `RecipeIngestionJobOut`, `RecipeIngestionJobState`,
  `RecipeIngestionJobCounts`, `RecipeIngestionBatchOut`; `IngestResponse`, `IngestRejected`;
- requests: `CardDraftUpdate`, `RereadRequest`, `RotateRequest`, `CommitRequest`, `CommitOut`, `EvalCaseRequest`,
  `EvalCaseOut`;
- settings: `RecipeIngestionSettingsOut/Update`, `AINotifierEventsOut/Update`, `IngestAbout`;
- enums: `IngestStatus`, `IngestSource`, `IngestTaskKind`, `IngestTaskState`, `IngestErrorCode`,
  `IngestRejectReason`, `CardFlagKind`, `CardFlagSeverity`, `CardFlagSource`, `IngestReadPath`
  (`image`, `ocr`).
  - `IngestErrorCode` (14): `ai_not_enabled`, `local_only_unavailable`, `limit_reached`, `rate_limited`,
    `provider_failed`, `no_recipe_found`, `files_missing`, `owner_missing`, `interrupted`, `cancelled`, `timeout`,
    `internal_error`, `commit_invalid`, `commit_interrupted`.
  - `CardFlagKind` (18): `illegible`, `blank`, `missing_name`, `unsure`, `not_on_card`, `marker_dropped`,
    `read_disagreement`, `check_parse`, `unit_unclear`, `implausible_amount`, `implausible_temperature`,
    `empty_section`, `read_by_ocr`, `cross_read_failed`, `shorthand_read`, `not_parsed`, `new_food`, `new_unit`.
  - `CardFlagSource`: `marker`, `model`, `validator`, `parser`, `ocr`, `cross_read`.
  - `IngestRejectReason`: `too_large`, `unsupported_format`, `pdf_not_supported`, `too_many_pixels`,
    `unreadable_image`, `too_many_pages`, `duplicate`.

**Drafts survive upstream syncs:** `CardDraft` is the fork's own model, read leniently (`extra="ignore"`, migrations
keyed by `schema_version`) and turned into an upstream `Recipe` only at commit, where a validation error is shown to
the user.

```
CardDraft { schemaVersion: 1, name, description, recipeYield?, recipeYieldQuantity?, recipeServings?,
  prepTime?, performTime?, totalTime?, attribution?, useCardAsCover: true,
  ingredients: [{referenceId, title?, originalText, quantity?, unit?: {id?, name}, food?: {id?, name}, note,
                 display, parseConfidence?, extractedHash?}],
  steps: [{id, title?, text}], notes: [{title, text}],
  tags | categories | tools: [{id?, name}] }
```

## 14. API surface

Under `/api/ai`, household-scoped, camelCase. Routes that write files answer `503 paused_for_restore` during a restore.

| Method | Path | Body → response |
|---|---|---|
| `POST` | `/ingest` | §1.2 → `202 IngestResponse` |
| `POST` | `/ingest/batches` | → `201 RecipeIngestionBatchOut` |
| `POST` | `/ingest/batches/{id}/seal` | → `200` |
| `GET` | `/ingest/batches/{id}` | → batch with counts and its jobs `{id, position, status, errorCount, warningCount}` in order |
| `GET` | `/ingest/jobs?status=&batchId=&page=&perPage=` | → paginated `RecipeIngestionJobSummary {id, batchId, position, status, source, sourceName, title, pageCount, thumbUrl, errorCount, warningCount, task, error, recipe, localOnly, createdAt}` |
| `GET` | `/ingest/jobs/counts` | → `{processing, ready, needsAttention, failed}` |
| `GET` | `/ingest/jobs/{id}` | → `RecipeIngestionJobOut` (summary + `draftVersion`, `pages`, `transcription`, `read`, `draft`, `flags`, `proposals`, `permissions {canCreateFoods, canDiscard, canExportEval}`, `duplicateOf`, `householdRecipesPublic`) |
| `GET` | `/ingest/jobs/{id}/state` | → `{draftVersion, status, task, proposalIds, error}` |
| `PUT` | `/ingest/jobs/{id}` | `{draftVersion, draft, flagResolutions, resolvedProposalIds, clearError}` → `{draftVersion, flags, errorCount, warningCount}` · `409 {detail: {code: "version_conflict", current}}` |
| `POST` | `/ingest/jobs/{id}/reextract` · `/reread` · `/retry` | → `202 RecipeIngestionJobState` · `409 {detail: {code: "busy"}}` · `422` |
| `POST` | `/ingest/jobs/{id}/cancel` | → `200` |
| `POST` | `/ingest/jobs/{id}/pages/{n}/rotate` | `{degrees: 90 \| 180 \| 270}` → `200 PageOut` · `409` while a task is active |
| `GET` | `/ingest/jobs/{id}/pages/{n}/{view\|thumb\|page}` | → JPEG/WebP, ETag includes the rotation |
| `POST` | `/ingest/jobs/{id}/commit` | `{draftVersion, draft?}` → `201 {recipeId, slug, nextJobId, warnings}` · `200` if done · `409 {detail: {code}}` · `422 {detail: {code: "unresolved_flags", flags}}` |
| `DELETE` | `/ingest/jobs/{id}` | → `204` |
| `POST` | `/ingest/jobs/{id}/eval-case` | `{slug, verified}` → `201 {slug, files}` · `409` exists |
| `GET` | `/ingest/eval-cases` | → `[{slug, name, pageCount, verified, createdAt}]` (managers) |
| `DELETE` | `/ingest/eval-cases/{slug}` | → `204` (managers) |
| `GET`/`PUT` | `/ingest/settings` | `{localOnly, crossRead, canReadCards, ocrAvailable, reader {name, local, viaOcr} \| null, localOnlyAvailable, localReadiness (managers), limits, inbox {enabled, folder}}` · PUT `{localOnly, crossRead}` (managers) |
| `GET`/`PUT` | `/notifiers/{notifierId}/events` | `{recipeIngestionReady}` |
| `POST` | `/notifiers/{notifierId}/events/test` | → `204` |
| `GET` | `/about` | public: `{version, features: {ingest: {enabled, maxUploadBytes, maxImagesPerRequest, maxPagesPerCard, inbox}, mcp: true}}` (plan §10) |

Route order: `/ingest/jobs/counts` is declared before `/ingest/jobs/{id}`. Frontend: `RecipeIngestAPI`
(`lib/api/user/recipe-ingest.ts`) as `useUserApi().recipeIngest`.

**Error bodies:** errors the page handles itself (`version_conflict`, `busy`, `unresolved_flags`) carry a `code` and
no `message`, because the axios interceptor toasts any `detail.message` (F18). Errors it doesn't handle (`503`,
`429`, `413`, `415`, `401`) carry a translated `message`, which the interceptor toasts and Shortcuts can show.
`reader` is the first provider the card's read path would use under the group's policy (`viaOcr` when that's the
OCR fallback), so every member's privacy chip has data (§1.1).

## 15. Settings and environment

A fork `IngestSettings` (`pydantic_settings.BaseSettings`, `env_prefix="AI_INGEST_"`, in
`mealie/services/ai/ingest/settings.py`, cached by `get_ingest_settings()`). `mealie/core/config.py` already loads
`.env` into the environment, so upstream's `AppSettings` is untouched.

| Variable | Default | Meaning |
|---|---|---|
| `AI_INGEST_ENABLED` | `true` | off: ingest routes answer `503`, no dispatcher |
| `AI_INGEST_WORKER` | `true` (`false` under `TESTING`) | run the dispatcher in this process |
| `AI_INGEST_CONCURRENCY` | `2` | task threads per worker process (plus one re-read slot) |
| `AI_INGEST_MAX_UPLOAD_MB` | `100` | per request (JSON bodies are capped at 45 MiB regardless) |
| `AI_INGEST_RETENTION_DAYS` | `14` | §16 |
| `AI_INGEST_INBOX_DIR` | unset | outside `DATA_DIR` and `/app`, e.g. `/inbox` |
| `AI_INGEST_INBOX_POLL_SECONDS` | `30` | |
| `AI_INGEST_INBOX_KEEP_PROCESSED` | `true` | move to `processed/` rather than delete |

Code constants in `limits.py` (tests patch them): poll 5 s, lease 120 s, heartbeat 20 s, deadline 30 min, 3
attempts, 6 rate-limit retries, housekeeping 60 s, commit lease 120 s, app batch idle 10 min, auto batch idle 2 min,
notification cutoff 24 h, pause marker refreshed every 60 s and honoured for 5 min, restore lock wait 120 s, inbox
settle 10 s, claim retry 10 min and 20 files a tick, 2 intakes, 4 dispatcher DB threads and 1 re-read slot per
process, `ORIENT_MIN_RATIO` 1.5, and the §1.5 limits.

`docs/ai/DEPLOY.md` gains the `/inbox` mount (also in `docker-compose.ai.yml` and the Unraid template), nginx
`client_max_body_size 100m`, `OLLAMA_CONTEXT_LENGTH` ≥ 16384 (two 2048 px pages overflow Ollama's 4k default on
small GPUs), and the note that Tesseract (`INSTALL_OCR=true`) is needed for flat-shot orientation.

## 16. Retention and cleanup

The purge (`retention.purge_once`) runs from the dispatcher once a day per process, first 10 minutes after boot. It's
idempotent, so running in every worker is harmless, and it needs no `app.py` or scheduler hook.
- **Committed**, older than `AI_INGEST_RETENTION_DAYS`: delete the directory; clear `transcription`, `draft`,
  `flags`, `proposals`, `extraction` and `title`; keep the row (recipe link, duplicate hash).
- **Failed**, older than the retention: delete row and directory.
- **Ready** jobs are never purged (unreviewed cards); the queue shows their age.
- **Discard** deletes row and directory at once. **Committing** jobs are never touched. Each purged job's file work
  runs in the write lock (§3.9).
- **Empty batches** older than the retention; **orphan directories** under `groups/*/ai-ingest/` with no row, older
  than an hour (a crash before insert, discard racing a worker, a restore mismatch).
- **Eval cases** stay until a manager deletes them. The inbox, `processed/` included, is the user's.
- Nothing touches `recipes/` except a commit.

## 17. Backup, restore and upgrades

**Backup and restore:**
- The tables follow every exporter rule (F15). A round-trip test (like `test_backup_mcp_oauth.py`) runs on SQLite and
  PostgreSQL with jobs in every state, JSON holding dashed UUIDs and `created_at` keys, and a UUID-named upload in
  `source_name`.
- Job files and eval cases are under `groups/`: a backup holds rows and files from the same moment, and a restore
  never meets a missing top-level directory.
- The inbox is outside `DATA_DIR`: never backed up, wiped or re-ingested by a restore. The duplicate hash stops
  re-ingestion either way.
- During a restore, ingestion pauses (§3.9). Afterwards, `running` rows expire and requeue, `committing` rows resume
  against the restored database, `notified_at` and the 24-hour cutoff prevent repeat notifications, and a stale
  marker (a crash mid-restore) expires within 5 minutes.

**Upgrade and migration:**
- One revision, `down_revision = "970cf50b85f4"`: four tables and two columns (with `batch_alter_table`). Downgrade
  drops them. A merge revision at the next upstream sync (plan §10).
- `init_db` migrates in every worker without a lock; that's pre-existing, and this revision is plain DDL.
- **Work in flight:** shutdown releases leases and the new version continues. Drafts carry `schema_version` and are
  migrated on read; `extraction.pipeline_version` records which pipeline produced a draft.
- No backfill: `/create/ai` recipes are untouched; old `add-ocr-recipe` databases are repaired by `fork_compat.py`.

## 18. Testing

**Backend** (offline; `tests/unit_tests/services_tests/ai/ingest/`, `tests/integration_tests/ai_tests/ingest/`;
SQLite and PostgreSQL):
- **Intake:** a tiny generated HEIC with orientation 6 and GPS comes out upright with no EXIF; MPO frame 0; truncated
  JPEG and PDF rejected; pixel cap before `load()`; magic bytes over extension; thumbnail aspect kept; a
  `SpooledTemporaryFile` and a `BytesIO` as input; duplicates by the ordered page hashes (a front re-sent with its
  back isn't one); nothing left in `DATA_DIR` after a failed request; no file under `groups/` carries GPS after
  intake returns.
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
  ids dropped; whitelist; `show_assets` on and `public` from the household; cover key; asset files EXIF-free with the
  stored token; Keep-as-written conversions; a pending re-read cancelled; exactly one `recipe_created`, including
  after a crash between `create_one` and the finish.
- **Review:** `409`; a task's proposal doesn't conflict with a save; a re-extract replaces an unedited draft and
  proposes on an edited one; errors block commit until kept; `404` across households for every route and image;
  discard permissions.
- **Events:** only flagged notifiers; Apprise URL params; one notification when two jobs finish together; auto-seal;
  failed-only batches; batches over 24 h old never notify; the test notification; never via
  `EventBusService.dispatch`.
- **Inbox:** two scanners on one file; settle; symlinks, dotfiles and temp names; a symlink swapped in after the
  claim; subfolders; `processed/` and `failed/`; crash after insert; a claimed file whose mtime is a month old isn't
  retried by a second scanner; refusal inside `DATA_DIR`.
- **Restore:** a restore with jobs queued and the dispatcher running completes, the dispatcher survives with no
  traceback, and the marker is removed even when the restore raises; a restore waits for a writer holding the lock,
  and gives up cleanly after the wait limit; a write section refused while the lock is held exclusively.
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

**Outside CI:** a production-mode Mealie with a stub provider, driven in Chromium at 375 px and on desktop; a
restore while a batch is processing; two workers (`UVICORN_WORKERS=2`) for duplicate claims and notifications; the
10-card batch on a real iPhone; the HA automation on HA 2026.10; the eval against real providers.

## 19. Exit criteria walkthrough

**20 cards scored per provider:**
1. Review cards from the box on the phone, as below, and tap ⋯ → **Save as eval case** on each (at least 5 printed,
   5 faded or pencil, some two-sided, some sideways). Tick "verified" after checking the draft against the card.
2. Run `python -m mealie.scripts.eval_recipe_cards --group Home --cards /app/data/groups/<group-id>/eval-cards
   --provider "Claude Sonnet" --provider "Gemini Flash" --provider qwen3-vl --provider "qwen3-vl:Claude Sonnet"
   --ocr --ocr-provider qwen3-vl --chain "qwen3-vl>OCR+qwen3-vl" --repeat 3 --price …`, then again with
   `--cross-read` and with `--no-intake-ocr`.
3. Record the targets (§11.4), the cross-read default and D3 in `docs/ai/EVAL.md`.

**A 10-card batch from a phone:**
1. Open **Recipe cards**, turn on **Front & back**, and shoot 10 cards (about 3 taps a side in the app, or the Camera
   app plus one multi-select). Cards upload and are read while you shoot; with concurrency 2 and about 25 s a card,
   most are ready when you tap **Done**.
2. The HA notification "10 cards are ready to review (2 need a look)" → tap → the first card to review.
3. Clean cards: **Commit & next**, one tap. Flagged ones: about two taps a flag, such as typing "2" into the banana
   card's blank, or **Keep as written**.

## Out of scope

- **Inputs:** image URLs in the upload API (a user question); PDF and multi-page TIFF (no rasterizer); sharing photos
  through the PWA's share target, and Web Push; client-side downscaling except after a `413`.
- **Reading:** model-reported rotation; model bounding boxes and pre-positioned crops; OCR cross-checks on printed
  cards; logprobs; Claude's Message Batches API; translation; rebuilding from an edited transcription.
- **Parsing and linking:** the AI and brute ingredient parsers, non-English parsing, the upstream parse dialog in
  review.
- **Commit:** creating tags, categories or tools; a bulk "commit clean cards" button (the eval's clean-card
  precision decides it later); merging jobs after upload; undoing a commit; step images in review; `is_ocr_recipe`.
- **Runner:** a cross-process per-group cap (needs `pg_advisory_xact_lock`); per-user quotas; SSE progress.
- **Eval:** AUROC, cost per caught error and region-level scoring (noise at n=20).
- **Other:** an MCP upload tool; nutrition (Phase 4 listens to `recipe_created`); purging the inbox's `processed/`;
  fixing upstream's `create_from_ai` cover key and safehttp's redirect scheme gap (worth reporting upstream).

## Known limitations

- Notifications are at most once per batch, and two simultaneous first uploads can start two batches.
- After a process pause longer than the lease a provider call may run twice; its result is applied once.
- A result can land on a row restored from a backup taken during the same claim (§3.3; harmless).
- A restore reruns any card that was being read, and a crash mid-restore pauses ingestion for up to 5 minutes
  (§3.9). Upstream's own file writers still race a restore, as before.
- The address check runs before the SDK's own DNS lookup; the provider flag is the primary control.
- Anyone who can write to the inbox share can queue cards for any household.
- Without Tesseract, flat-on-the-table shots come out sideways until rotated by hand.
- A local model that takes one image per request fails two-sided cards (the eval shows which models do).
- Reloading the capture page drops photos not yet uploaded. Ingredient parsing is English-only.
- Total concurrency is `(AI_INGEST_CONCURRENCY + 1 re-read slot) × UVICORN_WORKERS`, with no per-group share.
