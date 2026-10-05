# Deploying the AI build

This fork is upstream Mealie `v3.28.0` plus the AI additions described in
[`AI_INTEGRATION_PLAN.md`](../AI_INTEGRATION_PLAN.md). It's published as **`brewermichael/mealie-dev:ai`**, an image
built from the stock `docker/Dockerfile` with Tesseract added for the OCR fallback.

- [1. Build the image](#1-build-the-image)
- [2. Run it](#2-run-it)
- [3. Upgrade from the old `mealie-dev` image](#3-upgrade-from-the-old-mealie-dev-image)
- [4. Configure AI providers](#4-configure-ai-providers)
- [5. OCR fallback](#5-ocr-fallback)
- [6. Recipe cards](#6-recipe-cards)
- [7. Upstream behaviour this build changes](#7-upstream-behaviour-this-build-changes)

For upstream's own reference, see [Backend configuration](../docs/documentation/getting-started/installation/backend-config.md)
and [AI integration](../docs/documentation/getting-started/installation/ai-providers.md).

---

## 1. Build the image

Build from the repository root on any machine with Docker. The frontend and backend are both built inside Docker, so
nothing else is needed, but the frontend build is slow and memory-hungry.

```sh
git clone https://github.com/brewer-michael/mealie.git
cd mealie
git checkout ai-integration

docker build -f docker/Dockerfile \
  --build-arg INSTALL_OCR=true \
  --build-arg COMMIT="$(git rev-parse HEAD)" \
  -t brewermichael/mealie-dev:ai .

docker login
docker push brewermichael/mealie-dev:ai
```

| Build arg | Default | Effect |
|---|---|---|
| `INSTALL_OCR` | `false` | `true` installs `tesseract-ocr` in the runtime image. With `false` the image is the same as upstream's and OCR is simply unavailable. |
| `OCR_LANGUAGE_PACKS` | `eng` | Space-separated language packs to add, each installed as the Debian package `tesseract-ocr-<pack>`, e.g. `"eng deu fra"`. Debian writes the `_` in Tesseract codes as `-`, so `chi_sim` is `chi-sim`. |
| `COMMIT` | (none) | Upstream's arg; shown as the build's commit on the About page. |

Unraid runs `linux/amd64`. If you build on an ARM machine (such as an Apple Silicon Mac), build and push for that
platform instead:

```sh
docker buildx build --platform linux/amd64 -f docker/Dockerfile \
  --build-arg INSTALL_OCR=true -t brewermichael/mealie-dev:ai --push .
```

The `:ai` tag is new, so pushing it doesn't change the existing `latest` and `dev` tags or the containers that use
them.

## 2. Run it

### Docker Compose

[`docker/docker-compose.ai.yml`](../../docker/docker-compose.ai.yml) builds the image with OCR enabled and runs it on
port `9925`, keeping its data in a named volume.

```sh
docker compose -f docker/docker-compose.ai.yml up -d --build   # build locally, then start
docker compose -f docker/docker-compose.ai.yml pull            # or fetch the pushed image instead of building
docker compose -f docker/docker-compose.ai.yml up -d
```

Set `BASE_URL` and `TZ` in the file first. The file also has a commented-out `ollama` service, with notes for
NVIDIA and AMD GPUs, if you want local models next to Mealie, and commented-out lines for the recipe card inbox and
the other recipe card settings ([section 6](#6-recipe-cards)).

### Unraid

[`docker/unraid/mealie-ai.xml`](../../docker/unraid/mealie-ai.xml) is a user template. To install it, run this in the
Unraid terminal:

```sh
wget -O /boot/config/plugins/dockerMan/templates-user/my-mealie-ai.xml \
  https://raw.githubusercontent.com/brewer-michael/mealie/ai-integration/docker/unraid/mealie-ai.xml
```

Then go to **Docker > Add Container** and pick **mealie-ai** from the template list. Two fields must be filled in:

- **`BASE_URL`** is the address you open Mealie at, such as `http://192.168.1.10:9925` or
  `https://mealie.example.com`. It's used for links in emails and shared pages.
- **`TZ`** is your time zone, such as `America/New_York`. Unraid passes in the server's time zone on its own, but a
  template variable replaces it, so the template makes this field required rather than leaving it blank.

**App Data** defaults to `/mnt/user/appdata/mealie-ai`. AI providers are not template variables; you set them up in
the web UI ([section 4](#4-configure-ai-providers)). **Recipe card inbox** is optional and empty by default: mapping
a share there turns the inbox on ([section 6](#the-inbox-folder)). The other recipe card settings are under **Show
more settings**, empty by default.

## 3. Upgrade from the old `mealie-dev` image

The old `brewermichael/mealie-dev` image was built from the `add-ocr-recipe` branch on upstream `v3.1.2` (August 2025).
Moving to this build jumps more than a year of upstream releases. Home Assistant 2026.10 also needs Mealie `v3.2.0` or
newer for its Mealie integration, so the old image stops working with Home Assistant after that update.

### Before you start

1. **Back up your data first.** Stop the old container, then copy its whole App Data folder, which holds the database,
   images and the secret key:

   ```sh
   cp -a /mnt/user/appdata/mealie-dev /mnt/user/appdata/mealie-ai
   ```

   The new container runs on the copy and the original stays untouched, so the original is your backup and your way
   back. If you use PostgreSQL rather than the default SQLite, also take a `pg_dump` of the database.
2. **Write down your AI API keys.** Keys saved on the old `/admin/recipe-scanning` page are not carried over (see
   below). If you no longer have them, they're in the `admin_settings` table of the old database.

### Steps

1. Install the new container from the template ([section 2](#unraid)), with App Data set to the copied folder.
2. Start it and watch the log (`docker logs -f mealie-ai`). The first start runs every database migration since
   `v3.1.2`, which can take a few minutes on a large library. Let it finish without restarting the container.
3. Sign in. Your recipes, users and meal plans are kept.
4. Add your AI providers again ([section 4](#4-configure-ai-providers)).
5. If Home Assistant uses the Mealie integration and the port changed (the old template used `9000`, the new one
   `9925`), update the integration's address.
6. Once everything works, remove the old container. Keep the old App Data folder for a while.

To roll back, stop `mealie-ai` and start the old container again; it still points at the untouched folder.

### What changed for the old database

A database that ever ran the old `add-ocr-recipe` build is stamped with the migration revision `add_admin_settings`
and has an extra `admin_settings` table. Upstream doesn't know that revision, so stock Mealie refuses to start on it
and logs `Can't locate revision identified by 'add_admin_settings'`.

This build repairs that automatically on startup, before migrations run. It sets the stored revision back to
`e6bb583aac2d`, the upstream revision the old migration was built on, and drops the leftover `admin_settings` table.
Upstream's migrations then continue from there as normal. Nothing else in the old database came from that branch.

If startup still fails with that error, apply the same fix by hand while the container is stopped:

```sql
UPDATE alembic_version SET version_num = 'e6bb583aac2d' WHERE version_num = 'add_admin_settings';
DROP TABLE IF EXISTS admin_settings;
```

For SQLite, the image doesn't include the `sqlite3` command, but its Python can run the fix against the App Data
folder:

```sh
docker run --rm -i -v /mnt/user/appdata/mealie-ai:/app/data --entrypoint python brewermichael/mealie-dev:ai - <<'EOF'
import sqlite3
db = sqlite3.connect("/app/data/mealie.db")
db.execute("UPDATE alembic_version SET version_num = 'e6bb583aac2d' WHERE version_num = 'add_admin_settings'")
db.execute("DROP TABLE IF EXISTS admin_settings")
db.commit()
print(db.execute("SELECT version_num FROM alembic_version").fetchall())
EOF
```

For PostgreSQL, run the two statements with `psql` against the Mealie database. Then start the container again.

### What no longer exists

- **Environment variables** `IMAGE_SCANNING_PRIMARY_PROVIDER`, `IMAGE_SCANNING_SECONDARY_PROVIDER`,
  `IMAGE_SCANNING_ENABLE_OCR_FALLBACK`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `OLLAMA_BASE_URL` and the per-provider
  `*_MODEL` variables are ignored. Remove them. OCR is now controlled by `OCR_ENABLED`, `OCR_LANGUAGES` and
  `OCR_TIMEOUT` ([section 5](#5-ocr-fallback)).
- **The `/admin/recipe-scanning` page** is gone. Providers are configured per group instead, and photo imports,
  including the OCR fallback, go through **Create > Import with AI**.
- **Saved API keys** in the old `admin_settings` table are dropped with it. Enter them again as providers.
- **`OPENAI_API_KEY`** is a special case. If it's still set on the container the first time the new image starts,
  upstream's own migration creates providers from it in every group. The first is named after `OPENAI_MODEL`
  (`gpt-4o` if unset) and fills the default and image slots. Unless `OPENAI_ENABLE_TRANSCRIPTION_SERVICES=false`, a
  second one named after `OPENAI_AUDIO_MODEL` (`whisper-1` if unset) fills the audio slot. Both use the key and
  `OPENAI_BASE_URL`. Check them in Group Settings, then remove the variable. A container created from the new
  template doesn't set it.

## 4. Configure AI providers

### Where

In the sidebar, open the **Settings** (cog) menu, choose **User Settings**, then the **Group Settings** card. Its
address is `/group`. The **AI Provider Settings** section is below the group preferences. You need a user that can
manage the group. A site admin can also set providers for any group under **Admin Settings > Groups > (group)**.

Add providers with **Create Provider**, assign them to slots, then press **Update** under the section to save the
slot choices.

| Field | Notes |
|---|---|
| Provider Name | Any label |
| Base URL | Leave empty for OpenAI. Otherwise an OpenAI-compatible endpoint (see the examples below). |
| API Key | **Required.** Mealie rejects an empty key, so use any placeholder (such as `ollama`) for services without keys. |
| Model | The model ID as the provider spells it |
| Request Timeout | Seconds, default 300. Raise it for slow local models. |
| Request Headers / Parameters | Extra HTTP headers or query parameters sent with every request |

### Slots

| Slot | Used for | Needs |
|---|---|---|
| **Default Provider** | Required: with no default provider, all AI features are off and **Import with AI** is hidden. Used for AI ingredient parsing, reading a page when the URL scraper fails, and turning every source in Import with AI (text, the image reader's output, transcripts, OCR text) into a recipe and its organizers. | A chat model that returns JSON reliably |
| **Image Provider** | Reading photos in Import with AI. Only that reading step uses it; the default provider still builds the recipe. | A vision-capable model |
| **Audio Provider** | Transcribing videos when you import a video link (YouTube, TikTok and so on) | A model that serves OpenAI's `/audio/transcriptions` endpoint. If that call fails, Mealie sends the audio to the same model as a chat message, which works for chat models that accept audio input. |

One provider can fill several slots. The image and audio slots only work while a default provider is set.

### Test before you save

The provider dialog has a **Test Connection** button. It sends a real request in the same structured format Mealie
uses, so a pass means the base URL, key and model all work. It then sends a sample recipe image and reports either
**Supports images** or **Text-only, can't be your image provider**. Use it to pick the image provider. It doesn't
test audio; import a short video to check that. **Admin Settings > Debug > OpenAI** can also send a one-off message
or image to any group's provider.

### Examples

These guides don't recommend specific models, because model names change often. For the image slot, choose a current
vision-capable model from the provider's list and confirm it with Test Connection.

**OpenAI**

- Base URL: *(empty)*
- API Key: from the OpenAI platform dashboard
- Covers all three slots: a chat model for default, a vision-capable one for image, and a transcription model for audio.

**Google Gemini** (OpenAI-compatible endpoint)

- Base URL: `https://generativelanguage.googleapis.com/v1beta/openai/`
- API Key: a Gemini API key from Google AI Studio
- Model: a Gemini model ID as listed by Google
- Gemini models accept images, so one provider can be both default and image. Audio through this endpoint is
  untested here.

**Ollama** (local)

- Base URL: `http://<ollama-host>:11434/v1`. On Unraid that's usually the server's LAN IP. In
  `docker-compose.ai.yml` with the bundled service enabled, it's `http://ollama:11434/v1`.
- API Key: any non-empty text, such as `ollama`. Ollama ignores it, but Mealie requires one.
- Model: a model you've already pulled (`docker exec ollama ollama pull <model>`), named as `ollama list` shows it.
- Use a vision model for the image slot. Raise Request Timeout on CPU-only hosts.

**OpenRouter**

- Base URL: `https://openrouter.ai/api/v1`
- API Key: from your OpenRouter account
- Model: OpenRouter's `vendor/model` IDs. Filter its model list for image input when choosing the image provider.
- Optional Request Headers `HTTP-Referer` and `X-Title` identify your app in OpenRouter's usage stats.

**Anthropic (Claude)** through its OpenAI SDK compatibility endpoint

- Base URL: `https://api.anthropic.com/v1/`
- API Key: from the Claude Console
- Model: a Claude model ID
- Limitations, from Anthropic's compatibility docs:
  - Anthropic describes this layer as meant for testing and comparison, not as a long-term production integration.
  - `response_format` is ignored, so Mealie's JSON schema isn't enforced. Mealie still parses JSON out of the reply,
    including fenced code blocks, so it usually works, but expect occasional failed imports.
  - Audio input isn't supported, so don't use it as the audio provider.
- A native Anthropic adapter is planned ([plan §4.1](../AI_INTEGRATION_PLAN.md#41-ai-platform-additions-on-top-of-upstream)).

Base URLs were checked on 2026-10-02 against each vendor's own material: Google's `google-gemini/cookbook` OpenAI
compatibility notebook plus a live request to the endpoint, Ollama's `docs/api/openai-compatibility.mdx`, OpenRouter's
`openrouter-examples-python`, and Anthropic's OpenAI SDK compatibility page.

## 5. OCR fallback

Images built with `INSTALL_OCR=true` (including `:ai`) include Tesseract. When you import photos with **Import with
AI**, Mealie uses OCR in two cases:

- the group has **no image provider**, or
- the image provider **fails** on the photos.

Tesseract reads the text from each photo on the server, and the **default provider** turns that text into a recipe. So
OCR still needs a default provider. Without one, AI imports are off entirely. OCR loses the page layout and struggles
with handwriting, so a vision model gives better results and OCR is never preferred over one that works.

Recipe cards ([section 6](#6-recipe-cards)) use Tesseract twice: as the same fallback reader, and to turn a card shot
flat on the table, which phones often save sideways, the right way up before it's read. The two have their own
switches: `OCR_ENABLED=false` turns off only the fallback reader, and `AI_INGEST_ORIENT=false` only the turning.

| Variable | Default | Meaning |
|---|---|---|
| `OCR_ENABLED` | `true` | Turns the fallback reader off when `false` (recipe cards are still turned upright). It's also skipped when the `tesseract` command isn't installed, as in images built without `INSTALL_OCR`. |
| `OCR_LANGUAGES` | `eng` | Tesseract language codes joined by `+`, such as `eng+deu`. Each one needs its pack built in through `OCR_LANGUAGE_PACKS`, where `_` becomes `-` (`chi_sim` needs `chi-sim`). |
| `OCR_TIMEOUT` | `60` | Maximum seconds Tesseract may spend on one image |

Whether to keep Tesseract long term is open decision D3 in the [plan](../AI_INTEGRATION_PLAN.md#11-open-decisions).

## 6. Recipe cards

The **Recipe cards** page scans stacks of recipe cards from a phone, an iOS Shortcut, Home Assistant or a watched
folder; the user guide is [`CARDS.md`](CARDS.md). It runs inside the Mealie container and needs only the AI providers
of [section 4](#4-configure-ai-providers): a default provider, plus an image provider or OCR. The settings below are
all optional.

### The inbox folder

Photos and PDFs put in a watched folder are read like uploads. The inbox needs Linux or macOS (the Docker image is
Linux); it's off when Mealie runs directly on Windows.

1. Mount a folder at `/inbox`, **outside App Data**. A folder inside `/app/data` or `/app` is refused at startup (the
   log says so): backups would zip it, and a restore would wipe it.
   - **Compose:** uncomment the `/inbox` volume in `docker-compose.ai.yml`, with your host folder.
   - **Unraid:** set **Recipe card inbox** to a share such as `/mnt/user/mealie-inbox`. To let Home Assistant drop
     camera snapshots there, export the share over SMB or NFS.
2. Restart. A folder mounted at `/inbox` turns the inbox on by itself (the log says `Recipe card inbox on at /inbox
   (a mounted folder)`). Set `AI_INGEST_INBOX_DIR` only for another path, or when `/inbox` isn't itself a mount (only
   household folders mounted, or a bind mount from the same disk outside Docker). `AI_INGEST_INBOX_DIR=/inbox` with
   nothing mapped there keeps the inbox off, with a line in the log.
3. On its next scan Mealie creates a folder per household, `/inbox/<group-slug>/<household-slug>/`. **Group Settings
   > Recipe cards** shows yours. `GET /api/ai/about` reports `"inbox": true` once it's on.

- **Permissions.** Mealie, running as `PUID`/`PGID`, must be able to create folders and move files in the share.
  Folders it creates get `AI_INGEST_INBOX_DIR_MODE` (`2775`: group-writable and setgid, so new files belong to
  `PGID`); folders that already exist keep their mode. Whatever writes the photos must write as a member of `PGID`,
  and must create card subfolders group-writable (umask `002`; Samba: `force group`, `create mask = 0664`,
  `directory mask = 2775`). A subfolder Mealie can't write to stays in the inbox, with a line in the log (*Mealie's
  group needs write access to the household folder, and to a card folder itself: umask 002*), and the Recipe cards
  page lists it; a photo it can't read moves to `failed/`.
- **One household per device.** Anyone who can write to the share can queue cards for any household in it. Cards are
  still reviewed before they become recipes. To give a device only its own household, mount that household's folder
  on its own, such as `-v /mnt/user/inbox-family:/inbox/home/family`, and set `AI_INGEST_INBOX_DIR=/inbox`, because
  `/inbox` itself then isn't a mount. Docker creates `/inbox` and `/inbox/home` for the mount, owned by root, so
  Mealie can't create the other households' folders there (the log says so once); mount each household that should
  have an inbox. This layout hasn't been tried on a real Docker host yet.
- **`processed/`.** Read photos move to `processed/YYYY-MM/` and are kept, unless `AI_INGEST_INBOX_PROCESSED_DAYS` is
  set: then a daily purge (first 10 minutes after the first scan) deletes the photos and card folders read longer ago
  than that, and removes month folders it empties. `AI_INGEST_INBOX_KEEP_PROCESSED=false` deletes photos as soon as
  they're read.
- **Backups.** The inbox isn't backed up.

How files are picked up is in [`CARDS.md` section 6](CARDS.md#6-the-inbox-folder).

### Settings

| Variable | Default | Meaning |
|---|---|---|
| `AI_INGEST_ENABLED` | `true` | `false` turns recipe card scanning off: its routes answer `503` and nothing runs in the background |
| `AI_INGEST_WORKER` | `true` | Read cards in this process. Leave it on. With it off, uploads are still taken but nothing reads them: the log warns at startup, `GET /api/ai/about` reports `"worker": false`, and the Recipe cards page says so. |
| `AI_INGEST_CONCURRENCY` | `2` | Cards read at once per worker process, plus one slot kept for re-reads (1 to 32) |
| `AI_INGEST_GROUP_CONCURRENCY` | `0` | At most this many cards of one group read at once, across worker processes; `0` is no cap. Re-reads and Parse with AI don't count. Groups take turns either way. |
| `AI_INGEST_MAX_PROCESSING_PER_USER` | `0` | Uploads from a user who already has this many cards waiting to be read get `429 user_quota`; `0` is no cap. Inbox cards don't count. |
| `AI_INGEST_MAX_UPLOAD_MB` | `100` | Largest upload request. JSON bodies are capped at 45 MiB whatever this says. |
| `AI_INGEST_RETENTION_DAYS` | `14` | Days before the photos of committed cards, and failed cards entirely, are deleted. Cards waiting for review are never deleted; a card waiting for a monthly limit to reset is kept until this long after its retry date. |
| `AI_INGEST_ORIENT` | `true` | Turn sideways cards upright with Tesseract when it's installed, whatever `OCR_ENABLED` says |
| `AI_INGEST_INBOX_DIR` | unset (`/inbox` when a folder is mounted there) | The inbox folder inside the container |
| `AI_INGEST_INBOX_POLL_SECONDS` | `30` | How often the inbox is scanned |
| `AI_INGEST_INBOX_KEEP_PROCESSED` | `true` | `false` deletes read photos instead of moving them to `processed/` |
| `AI_INGEST_INBOX_PROCESSED_DAYS` | unset | Delete photos from `processed/` this many days after they were read (checked once a day, first 10 minutes after the first scan); unset keeps them |
| `AI_INGEST_INBOX_DIR_MODE` | `2775` | Octal mode of the inbox folders Mealie creates; existing folders aren't changed. An invalid value is logged and `2775` is used. |
| `AI_INGEST_LOCK_DIR` | unset | A local folder for the card write lock, when App Data is on a filesystem without file locks ([below](#backups-and-restores)) |
| `AI_INGEST_URL_FETCH` | `false` | Accept image URLs in the upload API ([Image URLs](#image-urls)) |
| `AI_INGEST_URL_ALLOW_HOSTS` | empty | Private hosts, addresses or CIDR ranges, comma-separated, that image URLs may reach, such as Home Assistant's address |
| `AI_INGEST_URL_TIMEOUT` | `20` | Seconds one image URL's download may take in all, redirects included (1 to 300) |
| `AI_INGEST_PDF_CPU_SECONDS` | `20` | CPU seconds one PDF may take to render (it may take 1.5 times this in all). A PDF that runs out is refused `pdf_not_supported`, and so are the later PDFs of its upload; raise it on a slow NAS or ARM board (1 to 600) |
| `AI_INGEST_PDF_UNCONFINED` | `false` | Render PDFs where the renderer's seccomp filter can't apply (another architecture, a kernel or container without seccomp); off, such PDFs are refused and the log says why |

An empty variable counts as unset, so the Unraid template's empty fields keep the defaults.

### Image URLs

With `AI_INGEST_URL_FETCH=true`, the upload API's JSON body may name an image by URL
(`{"images": [{"url": "http://..."}]}`), and Mealie downloads it. It's for Home Assistant's `rest_command`, which
can't send a file ([`CARDS.md`](CARDS.md#with-an-image-url-rest_command)). Off, such an image is refused
`url_not_allowed` and nothing is fetched.

The trade-off: anyone with an API token can then make the server send GET requests on their behalf. Mealie limits
what those can reach:

- only `http` and `https`, with no `user:password@`, no cookies and no proxy from the environment;
- only public addresses, plus what `AI_INGEST_URL_ALLOW_HOSTS` and upstream's `HTTP_ALLOW_LIST` name;
  `HTTP_DISALLOW_LIST` refuses what it names. The connection goes to the address that was checked; an allowed address or
  range vouches only for itself, so a host's other private addresses are left out;
- at most 3 redirects, each checked again, never to another scheme and never from `https` to `http`;
- the body uncompressed only, at most 30 MB, within `AI_INGEST_URL_TIMEOUT`, which counts the address lookup too (a host
  that doesn't resolve is `url_fetch_failed`); what comes back must be an image or a PDF;
- logs name the host only, since a camera URL can carry a token.

Keep `AI_INGEST_URL_ALLOW_HOSTS` to the one address you need, such as `192.168.1.20` for Home Assistant.

### PDFs

PDFs are rendered with PDFium (`pypdfium2`, in the image), one page per card page, in a separate process with a time
limit (`AI_INGEST_PDF_CPU_SECONDS` of CPU, 20 by default, and 1.5 times that in all), one PDF at a time per worker
process, with at most one card per group waiting. Once one of an upload's PDFs runs out of time, its other PDFs aren't
tried; the inbox leaves the group's other PDFs for its next scan. That process is confined: no new privileges, no files
but fonts (Landlock, where the kernel has it), and a seccomp filter (x86-64 and arm64) refusing network sockets, new
processes, anything that reaches the server's process and any change to a file; no file writes and capped memory.
Where the seccomp filter can't apply (another architecture, a kernel or container without seccomp), PDFs are refused
unless `AI_INGEST_PDF_UNCONFINED=true`. The log says once which protections apply.

### Reverse proxy

Phone photos are several megabytes. nginx accepts 1 MB request bodies by default, so raise it to match
`AI_INGEST_MAX_UPLOAD_MB`:

```nginx
client_max_body_size 100m;
```

nginx's default 60 s read timeout is enough: a backup restore waits at most 45 seconds for card scanning and other
writes to finish ([Backups and restores](#backups-and-restores)).

Cloudflare's proxy caps uploads at 100 MB. After a `413` the app makes that card's photos smaller once in the browser
and sends them again, but the iOS Shortcuts and Home Assistant don't.

### How many cards are read at once

Each worker process reads `AI_INGEST_CONCURRENCY` cards at once, plus one re-read, so at most `WORKER_PER_CORE ×
UVICORN_WORKERS × (AI_INGEST_CONCURRENCY + 1)` cards are with your AI providers at a time. Keep it low for a single
local GPU or a provider with tight rate limits; `AI_INGEST_GROUP_CONCURRENCY` caps one group across all processes.
A group can have at most 200 cards waiting to be read; more uploads get `429`.

Several workers can start a new install together: one migrates the database while the others wait (a lock file in
App Data on SQLite, an advisory lock on PostgreSQL), logging *Still waiting for another Mealie process to finish
migrating the database* every minute. The PostgreSQL lock works behind PgBouncer in session or transaction mode; keep
PgBouncer's `idle_transaction_timeout` off (its default) or longer than a migration.

### Ollama

Set `OLLAMA_CONTEXT_LENGTH` to at least `16384` on the Ollama server (the commented-out service in
`docker-compose.ai.yml` has it). A two-sided card sends two 2048-pixel images, which overflow Ollama's default
context on small GPUs. Some local vision models take only one image per request; Mealie then reads a two-sided card's
pages one at a time.

### Backups and restores

- Card photos, drafts and eval cases are stored under App Data (`groups/<group id>/ai-ingest/` and
  `groups/<group id>/eval-cards/`), so backups include them. The inbox isn't included, and neither are runtime files
  (the locks, the pause marker, `.ai-ingest-results/`, `.ai-ingest-inbox/`).
- **A restore pauses Mealie.** Every request to the API gets `503` with `Retry-After: 60` while it runs (the app waits
  and retries, and doesn't sign you out); a change shows *A backup is being restored. Try again in a minute.* A page
  opened meanwhile says *A backup is being restored. This page will open when it's done.* and opens once it's over. The
  inbox and the background reader stop. Before it starts, the restore waits up to 45 seconds for writes in progress; if
  they're still busy, it stops without changing anything, and you try again. If another Mealie process is updating its
  database, the restore says *Mealie is updating its database. Try the restore again in a minute.* A Mealie process that
  starts during a restore (a restarted worker) waits for it to finish.
- **Health checks.** While a restore runs, every API request answers `503 paused_for_restore`, reads included. The
  image's `healthcheck.sh` counts that as healthy. A Kubernetes liveness probe should run that script (an exec probe) or
  a TCP check, not an HTTP check on `/api/app/about`, or it restarts Mealie in the middle of the restore.
- Cards being read during a restore go back in the queue. A reading the provider already returned is kept (in
  `.ai-ingest-results/`, for up to 24 hours) and used, so it isn't paid for twice. If Mealie crashes in the middle of
  a restore, the pause ends as soon as Mealie starts again (within 5 minutes where file locks don't work).
- **Locks.** Card writes and restores share a file lock, `.ai-ingest-lock` in App Data. If App Data's filesystem has no
  file locks (some network filesystems), the lock goes in `/tmp/mealie-ai-ingest-<uid>/` and the log says so; set
  `AI_INGEST_LOCK_DIR` to a local folder only Mealie's user can write to on hosts shared with other users.

## 7. Upstream behaviour this build changes

Small fixes to upstream Mealie that come with this build. Each is a commented fork hook in upstream's code.

- **Several workers start together:** database migrations run under a lock ([above](#how-many-cards-are-read-at-once)).
- **During a backup restore** every API request gets `503`, the restore waits for writes in progress, and a worker
  starting meanwhile waits for the restore ([above](#backups-and-restores)). Backups leave out the fork's runtime files.
- **Redirects:** recipe imports, image downloads and webhooks refuse a redirect from `https` to `http` or to another
  scheme. A recipe image import says *Url redirected to an insecure http:// address*; one refused webhook no longer
  stops the household's other webhooks.
- **Downloads by the server** (recipe pages, recipe images) stop at 50 MB, counted after decoding: Mealie decodes a
  compressed download itself and stops at the limit, so a small compressed file can't fill memory. A recipe image over
  that is refused with `400`. A gzip page missing only its checksum trailer is still read.
- **Webhooks and recipe actions** read at most 1 MB of the answer (decoded the same way; a larger answer is refused and
  logged, and doesn't stop the household's other webhooks), and give up on a request after 30 seconds, however slowly
  the answer comes.
- **`HTTP_ALLOW_LIST` addresses and ranges** allow only the addresses they cover: when a name resolves to an allowed
  address and also to other private ones (a router's IPv6 ULA or link-local record), Mealie connects only to the allowed
  ones. List the host name itself to allow all of its addresses.
- **Docker's health check** counts a backup restore's `503` as healthy ([above](#backups-and-restores)).
- **Apprise notifications** keep `+`, `&` and `%` in event fields such as a recipe's name, so Home Assistant's
  `from_json` reads `document_data`.
- **Import with AI** records the picture it saved for a new recipe, so the recipe shows it.

## Related

- [`AI_INTEGRATION_PLAN.md`](../AI_INTEGRATION_PLAN.md): what this fork adds and why
- [`CARDS.md`](CARDS.md): scanning recipe cards
- [`home-assistant/README.md`](../home-assistant/README.md): connecting Home Assistant and voice
