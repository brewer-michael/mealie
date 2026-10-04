# Scanning recipe cards

The **Recipe cards** page turns a stack of handwritten or printed recipe cards into Mealie recipes. You photograph the
cards, Mealie reads each one with your AI providers while you keep shooting, and you check every card before it
becomes a recipe. Cards can also come from an iOS Shortcut, Home Assistant (HA) or a watched folder.

- [1. Before you start](#1-before-you-start)
- [2. Scan from your phone](#2-scan-from-your-phone)
- [3. Review and commit](#3-review-and-commit)
- [4. iOS Shortcuts](#4-ios-shortcuts)
- [5. The upload API](#5-the-upload-api)
- [6. The inbox folder](#6-the-inbox-folder)
- [7. Keeping cards on your network](#7-keeping-cards-on-your-network)
- [8. "Recipe cards ready" notifications](#8-recipe-cards-ready-notifications)
- [9. Home Assistant](#9-home-assistant)
- [10. Cleaning a photo for the eval set](#10-cleaning-a-photo-for-the-eval-set)
- [11. Troubleshooting](#11-troubleshooting)
- [12. Try it](#12-try-it)

Server settings are in [`DEPLOY.md`](DEPLOY.md#6-recipe-cards). The design is [`PHASE2.md`](PHASE2.md), and scoring
providers on your own cards is [`EVAL.md`](EVAL.md).

---

## 1. Before you start

- **AI providers.** A group manager sets them up under **Group Settings > AI Provider Settings**
  ([`DEPLOY.md` section 4](DEPLOY.md#4-configure-ai-providers)). Cards need a **default provider**, plus an **image
  provider** (a vision model) or the OCR fallback. Until then the Recipe cards entries are hidden.
- **Tesseract** (in images built with `INSTALL_OCR=true`, including `:ai`). Phones often save a card shot flat on
  the table sideways. Tesseract finds the right way up before the card is read. Without it, a page the image provider
  reports as sideways is still turned; anything else you turn by hand (**⋯ > Rotate** on the review page).
- **Group Settings > Recipe cards** holds the group's options: keep cards on this server ([section
  7](#7-keeping-cards-on-your-network)), **Check each card with a second reading** (each card costs a second image
  request), the inbox folder and what is waiting in it, and the eval cases. It also warns when nothing on the server
  is reading cards, when `BASE_URL` still points at localhost, and when suggestions or the second reading are off
  until the monthly token limit resets.
- **Who can do what.** Anyone in a household can scan, review and commit; cards belong to the household that scanned
  them. Whoever commits a card owns the recipe.

  | Action | Who |
  |---|---|
  | **Discard** | The person who uploaded the card. Anyone in the household for cards from the inbox or sent with an API token (Shortcuts, HA). Household managers for the rest. |
  | **Add as back of previous card**, **Read with cloud providers** | The person who uploaded the card, or a household manager |
  | **Back to review** (undo a commit) | Whoever added the card, or a household manager, when they may delete the recipe (its owner or an admin) or it's already gone |
  | **Save as eval case** | Group managers |

- **Language.** A card takes the language it was sent in: Mealie's language on the device, or the phone's language
  for a Shortcut. The ingredient lines of a card in another language than English are parsed by AI. Mealie's own
  texts for recipe cards (upload answers, notifications, the card pages) exist only in English so far.

---

## 2. Scan from your phone

Open **Recipe cards** in the sidebar (it shows how many are ready, such as *Recipe cards (3)*), or **Create > Scan
recipe cards**, or the link on the **Import with AI** page. The address is `/g/<group-slug>/recipes/cards`.

1. Choose **One side** or **Front & back**. The choice is remembered on this device.
2. Tap **Take photo**. In Front & back mode the button then reads **Back side**, then **Next card**. **No back** ends a
   card that has nothing on the back; **Retake** replaces the last photo.
3. For a big stack it's quicker to shoot every card with the Camera app first, then tap **Choose photos** (**Choose**
   on a phone) and pick them all. They wait in a tray. In Front & back mode they pair in the order you picked them;
   fix a pair with **Swap**, **Split**, **Join** or **Remove**, then tap **Upload**. On a computer you can drop
   photos and PDFs on the page.
4. Each card uploads as soon as it's complete, two at a time, and is read while you keep shooting. A failed upload is
   retried three times, then shows **Retry**. A photo too large for the server or a proxy is made smaller once in
   the browser and sent again (*Making the photo smaller*).
5. Tap **Done** after the last card. Done never cuts off a card that's still uploading: the batch is closed only once
   every card has uploaded or failed (*Finishing the batch when the last cards are uploaded*). A batch you never mark
   Done stays open while the page is open, and closes itself 10 minutes after you leave the page (or after its last
   card, if that's later).

- **PDFs and multi-page TIFFs:** each one is a card of its own, with one page per page of the file (at most 4); a
  one-page file pairs like a photo. The tray shows *· 3 pages*. Files that aren't photos or PDFs are skipped with a
  notice (*Skipped 2 files: only photos (JPEG, PNG, WebP, HEIC, AVIF, TIFF) and PDFs can be scanned.*), and so is a
  file with more than 4 pages.
- **Formats and limits:** JPEG, PNG, WebP, HEIC/HEIF, AVIF, TIFF and PDF. At most 30 MB a file, 100 megapixels a photo
  (260 for a JPEG, fewer for a progressive JPEG), and 4 photos or pages a card.
- **Data saver:** a switch under the buttons, off by default and remembered on this device. It sends each photo at most
  4096 pixels on its long side, the size the server keeps anyway. PDFs and multi-page TIFFs go as they are.
- **Already scanned:** a card you've sent before (the same photos, usually a resend after a lost connection) is
  marked *Already scanned*, with **Open the earlier card** and **Scan again**. The same front sent again with its back
  is a new card. A card whose recipe was deleted can be scanned again.
- **Reloads and closed tabs:** photos waiting to upload are kept in the browser and go on uploading when you open
  Mealie again in that browser (a card whose tries ran out keeps its **Retry**). The browser still asks before you
  leave while uploads are running. If this browser can't keep them (private browsing, a full disk), the page says
  *Photos can't be kept on this device, so keep this page open until they're uploaded.*
- **Two tabs:** one tab of the browser uploads at a time. Another tab's cards page says *Your recipe cards are being
  added in another tab of this browser*, with **Use this tab**.
- **Log out:** with photos still waiting, Log out asks first (*2 photos haven't been uploaded. Log out anyway?*).
- **A card that fails while you're elsewhere** in Mealie shows a message with **Open recipe cards**, and a red badge on
  the sidebar entry.
- **Where photos go:** a chip under the buttons says who reads the cards: *Read by Claude Sonnet (cloud)*, *Read with
  OCR, then Claude Sonnet (cloud)*, *Read by qwen3-vl on your network*, or a lock and *Stays on this server*. Tap it
  for **Keep these cards on this server** ([section 7](#7-keeping-cards-on-your-network)).
- **Metadata:** each photo is turned upright and stripped of its metadata (GPS, camera, time) during the upload. The
  file as uploaded isn't kept.

The page also tells you when cards can't be read now: AI isn't set up, the group keeps cards on this server and nothing
local can read them, every provider has used its monthly token limit (cards are then read again automatically when
the limit resets, or sooner if a manager raises it), or nothing on the server is reading cards (*Cards are accepted,
but nothing on the server is reading them, so they wait.*).

---

## 3. Review and commit

The list under the capture buttons shows each batch's cards: *Reading*, *Ready*, *Ready · 2 to check* or *Failed*
with the reason, and **Review**, **Retry**, **Cancel** (while a card is being read) and **Discard**. A batch line
counts its cards (*2 added, 2 still being read, 1 failed*) and starts with *Batch done:* once nothing is being read.
**Review batch** opens the batch's first card that needs a look. Cards are reviewed in the order you shot them, so
the screen matches the stack in your hand. **Recently added** lists the cards added in the last 7 days, newest first;
**Load older cards** shows more.

A failed card says when it tries again or goes: *Tries again on …* for a card that hit the monthly token limit, else
*Removed on …*.

The review page shows the card and the recipe as read. **Needs a look (N)** lists each thing to check, with its
reason:

| You see | What to do |
|---|---|
| A gap the card leaves (`[blank]`, such as a missing time) | Type the value in, or **Keep blank** |
| A word the reader wasn't sure of, or couldn't read | Tap a suggested reading, **Re-read**, **Keep as written** or **Edit** |
| A number that isn't on the card, an amount or temperature that looks wrong | Fix it, or **Looks right** |
| *Check this ingredient*: the line may be split wrongly (it shows *Read as: …* and what was lost) | Fix it, **Parse with AI**, **Keep as text** or **Looks right** |
| *Check the link*: the line was linked to a food or unit whose name isn't on the card | Pick another, **Keep as new food** (or unit), or **Looks right** |
| An ingredient Mealie couldn't parse or link to your foods and units | Open the row and pick the unit and food |

Errors (red) block the commit until you fix them or keep them; warnings (amber) don't.

- **Re-read** an area: the selection opens on the line you're checking (found from Tesseract's line positions or the
  line's place in the text; otherwise where you last re-read on that page). Drag it, or move it with the arrow keys
  (Shift and the arrows resize it), then **Read this area** or Enter. The new reading appears as a suggestion with
  **Use** and **Dismiss**. Re-reads go ahead of cards still waiting to be read.
- **Parse with AI** on an open ingredient line, or on *Check this ingredient*, has the AI split the line into amount,
  unit and food. The lines of a card in another language than English are parsed by AI as the card is read; if that
  fails they're kept as text (*Ingredients kept as text*), with a **Parse with AI** button for all of them.
  The card can't be edited while it runs; then the bar says *Parsed with AI*.
- **⋯ (More):** **Rotate**, **Read whole card again**, **Re-read an area**, **What the card says** (the
  transcription), **Save as eval case** (see [`EVAL.md`](EVAL.md#saving-eval-cases-from-the-phone)), **Add as back of
  previous card** and **Discard**.
- **Rebuild from the text:** in **What the card says**, tap **Edit**, correct the text, then **Rebuild from this
  text**. The recipe is built again from your text; the photos aren't read again. If you edited the card meanwhile,
  a banner offers **Use the new reading** or **Keep mine**.
- **Add as back of previous card:** for a back that went up as a card of its own. Its photos join the card before it in
  the batch, that card is read again, and this one is removed. It's off, with the reason, when it can't be done:
  no card before it, either card being read, the previous card already added, or more than 4 photos together. If
  either card had to stay on this server, the merged card does too.
- **Read with cloud providers:** on a card that failed because it had to stay on this server and nothing local could
  read it (when the group doesn't keep every card local). A confirmation says the photos leave your network.
- **Duplicates:** a banner says *A recipe called "X" already exists. Adding this card makes "X (1)".*, *A recipe with
  a similar name already exists*, or *Another card waiting has the same name.* with **Open card**.
- Edits save on their own (*Saved*), and a save that fails is tried again. Leaving with unsaved changes asks first. If
  the card was changed somewhere else, **Reload this card**.
- **Commit & next** creates the recipe and opens the next card, going on into the next batch; a clean card takes this
  one tap. **Skip** leaves a card for later.
- **Undo:** the *Added …* line after a commit has **Undo**, and an added card's page has **Back to review**. Both
  delete the recipe and bring the card back for review. If the recipe was changed since, Mealie asks: **Keep the
  recipe** or **Delete it anyway**. This works until the card's photos are deleted (14 days).
- **Add N clean cards:** a batch with at least two clean cards (nothing highlighted, nothing being read) has this
  button on the cards page. It lists the cards, then adds them as they were read, 5 at a time (*Adding 10 of 40
  cards…*). Each card keeps its own card photo settings (below).
- On a computer: `Ctrl`/`⌘`+`Enter` commits, `Alt`+`↓`/`↑` moves between things to check, `R` re-reads an
  area, and `[` `]` switch between front and back.

**What commit does:**
- Ingredients are linked to your foods and units. New units are created. New foods are created only for users who can
  organize; for anyone else the food's name stays in the line as text. A line kept with a blank amount, such as
  `[blank] C. sugar`, reads *sugar ___ cup* on the recipe.
- Tags, categories and tools are suggested from the group's existing ones only. To add a new one, type its name and
  choose **Create "X"** (users who can organize); it shows as *X (new)* and is created when you commit. *No tags
  suggested* says why when suggestions were skipped.
- If the name is taken, the recipe gets the first free *Name (1)*, *Name (2)* and so on.
- **The card photo:** **Use the card photo as the recipe image** and **Attach the card photo to the recipe** (as
  assets named *Recipe card*, *Recipe card (back)*). Both are on by default, except in a household whose new recipes
  can be seen without logging in (not a private household, and recipes public by default): there they start off,
  because anyone could see the photo. If you turn one on there, the page warns *Recipes in this household are public:
  the card photo will be visible to anyone.* A portrait card is shown whole on the recipe's 4:3 picture.

**How long cards stay:** ready cards wait until you review them. The photos of committed cards are deleted after 14
days (`AI_INGEST_RETENTION_DAYS`); a committed recipe keeps its own copy. Failed cards are removed 14 days after their
last change. A card that failed on the monthly token limit is read again on the 1st of next month (UTC), or sooner once
a manager raises the limit, and is kept until 14 days after that.

---

## 4. iOS Shortcuts

Shortcuts send cards to `POST /api/ai/ingest` ([section 5](#5-the-upload-api)). Each needs:

- **A Mealie API token.** Sign in as the user the cards should belong to, turn on **Show advanced features** in your
  profile settings, and create a token under **Profile > API Tokens**. Keep it as safe as a password. A dedicated
  user, such as the `Kitchen Voice` user from the [HA guide](../home-assistant/README.md#use-a-dedicated-kitchen-voice-user-for-the-token),
  works well.
- **The URL** `https://mealie.example.com/api/ai/ingest`, or `http://192.168.1.10:9925/api/ai/ingest` at home.
- **The header** `Authorization` with the value `Bearer <token>`. Mealie refuses uploads without it.

Every answer, accepted or refused, has a top-level `summary` to show:

- *1 recipe card queued. You'll be notified when it's ready.* when a household notifier sends "Recipe cards ready"
  ([section 8](#8-recipe-cards-ready-notifications)), else *1 recipe card queued for review in Mealie.*
- *No recipe cards were queued. 1 card was already scanned.* when nothing was used.
- The reason for a refusal, such as *Mealie didn't accept the API token. Check it and try again.* or *Too many recipe
  cards are waiting to be read. Try again when some have finished.*

**Scan a card** (one side):

| # | Action | Settings |
|---|---|---|
| 1 | Take Photo | |
| 2 | Get Contents of URL | the URL with `?done=true` added; Method **POST**; Headers: `Authorization` = `Bearer <token>`; Request Body **File**, File = *Photo* |
| 3 | Get Dictionary Value | `summary` in *Contents of URL* |
| 4 | Show Notification | *Dictionary Value* |

`done=true` ends the batch with this card, so the "ready" notification comes as soon as the card is read, not 2 minutes
later.

**Scan a two-sided card:**

| # | Action | Settings |
|---|---|---|
| 1 | Take Photo | the front |
| 2 | Base64 Encode | *Photo*, Line Breaks **None**; then Set Variable `Front` |
| 3 | Take Photo | the back |
| 4 | Base64 Encode | *Photo*, Line Breaks **None**; then Set Variable `Back` |
| 5 | Get Contents of URL | as above, but Request Body **JSON**: a key `images` of type Array holding two Dictionary items, each with a key `data` set to `Front` and `Back` |
| 6 | Get Dictionary Value, Show Notification | as above |

**Share photos to Mealie** (from the Photos share sheet; each photo is one card):

| # | Action | Settings |
|---|---|---|
| 1 | Receive Images from Share Sheet | in the Shortcut's details, turn on **Show in Share Sheet** |
| 2 | Repeat with Each | item in *Shortcut Input* |
| 3 | Get Contents of URL | as in *Scan a card*, without `?done=true`, File = *Repeat Item* |
| 4 | Get Dictionary Value | `batchId` in *Contents of URL*; then Set Variable `Batch` |
| 5 | Get Dictionary Value | `summary` in *Contents of URL* |
| 6 | End Repeat | |
| 7 | Get Contents of URL | `https://mealie.example.com/api/ai/ingest/batches/`, then `Batch`, then `/seal`; Method **POST**; the same header |
| 8 | Show Notification | *Repeat Results* |

Uploads from one user that follow each other within 2 minutes join one batch, so a shared stack sends one "ready"
notification. Step 7 ends the batch, so it comes as soon as the cards are read; without it, 2 minutes after the last
card.

HEIC photos are fine. A file body must be `image/*` or `application/octet-stream`, so send a PDF as a form (Request
Body **Form**, a File field `files`). A JSON body is limited to 45 MB, which is plenty for two photos.

---

## 5. The upload API

`POST /api/ai/ingest` takes **one card per request** (front first). It needs the `Authorization: Bearer <token>`
header; a browser cookie alone is refused.

| Content type | Body | Use |
|---|---|---|
| `multipart/form-data` | every file part is a photo or PDF (call the field `files`); optional text fields below | `curl -F`, Shortcuts *Form* |
| `image/*` or `application/octet-stream` | one file (a PDF as `application/octet-stream`); options in the query string | Shortcuts *File* |
| `application/json` | `{"images": [{"data": "<base64>", "filename": "front.jpg"}]}` plus options | Shortcuts *JSON*, scripts |

A PDF or a multi-page TIFF gives the card all its pages (at most 4 in all).

| Option | Meaning |
|---|---|
| `split` | `true`: each file is its own card |
| `batchId` | join this batch; `new` starts a new batch. Without it, the same user's uploads within 2 minutes of each other share a batch. |
| `position` | the card's place in the batch's review order |
| `done` | `true`: end the batch with this card (as the app's **Done**): it notifies once its cards are read |
| `localOnly` | `true`: keep this card on your network ([section 7](#7-keeping-cards-on-your-network)) |
| `allowDuplicate` | `true`: accept photos already scanned |

```sh
curl -sS -H "Authorization: Bearer $MEALIE_TOKEN" \
  -F files=@front.jpg -F files=@back.jpg -F done=true \
  https://mealie.example.com/api/ai/ingest
```

```json
{"batchId": "…", "jobs": [{"id": "…", "status": "processing", "pageCount": 2, "reviewPath": "/g/home/recipes/cards/…"}],
 "rejected": [], "summary": "1 recipe card queued. You'll be notified when it's ready."}
```

| Status | Meaning |
|---|---|
| `202` | queued; `rejected` lists files that weren't used: `duplicate`, `too_large`, `unsupported_format`, `pdf_not_supported` (a PDF that can't be opened), `too_many_pixels`, `unreadable_image`, `too_many_pages`, `url_not_allowed`, `url_fetch_failed`, or `quota` (a later card of the request would pass a cap below) |
| `400` | nothing was accepted (`nothing_accepted`, the same body inside `detail`), the group can't read cards (`ai_not_enabled`), the card must stay local and can't (`local_only_unavailable`), or the body can't be read (`invalid_body`) |
| `401` | no `Authorization` header, or a bad token |
| `404` | a `batchId` that isn't one of the household's batches |
| `413` | over `AI_INGEST_MAX_UPLOAD_MB` (100 MB), or 45 MB for JSON |
| `415` | another content type |
| `429` | the group already has 200 cards waiting to be read (`too_many_jobs`), or you have as many as the server allows per user (`user_quota`); see `Retry-After` |
| `503` | a backup restore is running (`Retry-After: 60`), or card scanning is turned off on the server |

Every refusal is `{"detail": …, "summary": "…"}`, so a Shortcut can always show `summary`.

**Image URLs** (off unless the server allows them): a JSON image may be `{"url": "http://..."}` instead of `data`, and
Mealie downloads it. With `AI_INGEST_URL_FETCH` off it's refused `url_not_allowed`. Mealie only fetches public
addresses, plus the ones the server lists in `AI_INGEST_URL_ALLOW_HOSTS` (such as HA's); see
[`DEPLOY.md`](DEPLOY.md#image-urls). HA's use for it is in [section 9](#with-an-image-url-rest_command).

**Batches:**

| Route | What it does |
|---|---|
| `GET /api/ai/ingest/batches/{id}` | the batch, with its counts and its cards in review order |
| `POST /api/ai/ingest/batches/{id}/seal` | ends the batch: no more cards join it, and it notifies once its cards are read |
| `POST /api/ai/ingest/batches/{id}/touch` | the capture page's heartbeat: keeps an app batch you started open for another 10 minutes (`409` once it's ended) |
| `GET /api/ai/ingest/jobs/counts` | `ready`, `needsAttention`, `processing` and `failed` for the token user's household |

`GET /api/ai/about` (no token needed) reports whether card scanning and the inbox are on, whether a card reader is
running (`"worker": true`), and the upload limits.

---

## 6. The inbox folder

A folder Mealie watches, for scanners, Syncthing, a network share or HA's camera snapshots. The server side (the
`/inbox` mount, folder permissions and settings) is in [`DEPLOY.md`](DEPLOY.md#the-inbox-folder). The inbox needs
Linux or macOS; the Docker image is Linux. It's off when Mealie runs directly on Windows.

- **One folder per household:** `<inbox>/<group-slug>/<household-slug>/`, such as `home/family/`. Mealie creates them
  on each scan. **Group Settings > Recipe cards** and the Recipe cards page show yours.
- **A photo or a PDF is one card. A subfolder is one card with several pages**, in name order (`1-front.jpg`,
  `2-back.jpg`). A PDF or a multi-page TIFF gives the card all its pages (at most 4).
- **A file is taken once it stops changing:** Mealie looks every 30 seconds and takes a file whose size and time are
  unchanged since the last look and that is at least 10 seconds old. A file still being written is left alone.
- **Ignored:** names starting with `.` or `~`; partial downloads (`.tmp`, `.part`, `.crdownload`, `.partial`,
  `.download`, `.filepart`); `Thumbs.db` and `desktop.ini`; links.
- **Afterwards** the photo moves to `processed/YYYY-MM/` in the household's folder (or is deleted, with
  `AI_INGEST_INBOX_KEEP_PROCESSED=false`). `processed/` is kept unless the server sets
  `AI_INGEST_INBOX_PROCESSED_DAYS`. A photo that can't be used moves to `failed/`, next to a `<name>.error.txt` that
  says why.
- **Refusals are told:** the household's notifiers get one "Recipe cards not added" notification per burst, with counts
  by reason and no file names ([section 8](#recipe-cards-not-added)). The Recipe cards page and **Group Settings >
  Recipe cards** list the newest under *Not added from the inbox*, with each photo's name, why and when.
- **Photos wait in place** while the group can't read cards, must stay local without a local reader, or already has
  200 cards waiting. The same two places say so: *3 photos are waiting in the inbox: …*.
- **Writers need Mealie's group:** the folders Mealie creates are group-writable (`2775`). Whatever writes into them
  must be in Mealie's group, and a multi-page card's subfolder must be group-writable too (umask `002`), or it stays
  in the folder. The cards page lists such a folder with *Mealie may not move this out of the inbox folder.*
- **Inbox cards have no uploader:** they belong to the household, and anyone in it can discard them. They take the
  language of the household's latest upload from the app or the API (else English); a card that isn't in English has
  its ingredient lines parsed by AI.
- **Anyone who can write to the share can queue cards for any household in it.** Cards are still reviewed before they
  become recipes, but share the folder only with devices you trust, or give each device only its household's folder
  ([`DEPLOY.md`](DEPLOY.md#the-inbox-folder)).

---

## 7. Keeping cards on your network

Cards carry family names and handwriting. Mealie can keep a card's photos and text away from cloud AI services
entirely.

1. **Mark your local providers.** In **Group Settings > AI Provider Settings**, edit each provider on your own network
   (such as Ollama) and turn on **Runs on my network (local model)**. It needs a base URL, and Mealie also checks that
   the address is private (your LAN, the machine itself, or Tailscale). A provider marked local at a public address
   isn't used.
2. **Cover the slots a card uses:** a local **image provider** (or OCR) and a local **default provider**. If the
   **fast** slot has providers of its own, give it a local one too, or tag suggestions are skipped for these cards.
3. **Turn it on**, for the whole group or for the cards you're sending:
   - **Group Settings > Recipe cards > Keep recipe card photos and text on this server** (group managers). The card
     lists the local providers for *Reading photos*, *Building recipes* and *Quick tasks*, and warns when cards can't
     be read locally.
   - Or tap the privacy chip on the Recipe cards page and choose **Keep these cards on this server**. It applies to
     every card not sent yet; cards already sent keep the setting they went with. Cards can opt in when the group
     doesn't. The choice is remembered on this device.

Every AI call a local-only card causes is checked: the read, the OCR fallback, building the recipe, suggestions,
re-reads, re-extracts, rebuilds and Parse with AI. Turning the group setting on also covers cards being read at that
moment, from their next call. If no local provider can do a step, the card fails with *local only unavailable* rather
than going to the cloud; if the local providers have used their monthly token limit, it fails on the limit and is read
again when it resets. Local-only calls connect only to the private address Mealie checked, and ignore proxy settings.

A card queued as local-only stays local-only even if the setting is turned off later. The one way out is **Read with
cloud providers** on a card that failed for it, when the group doesn't keep every card local ([section
3](#3-review-and-commit)). A card merged with a local-only card is local-only too. The review page shows a *Local only*
badge.

Notifications and the `recipe_card_queue` voice tool carry counts and a link only, never card text, whatever the
setting. For Ollama, see the context length note in [`DEPLOY.md`](DEPLOY.md#ollama).

---

## 8. "Recipe cards ready" notifications

Mealie sends one notification when a batch has been read: "Recipe cards ready" / "10 cards are ready to review (2
need a look, 1 failed).", with a link to the batch's first card to review. It goes through the household's Apprise
notifiers, so any Apprise service works (ntfy, Pushover, Telegram, HA and others).

1. Turn on **Show advanced features** in your profile settings. The notifiers page needs it.
2. Open **Profile > Notifiers** (`/household/notifiers`) and create a notifier with your Apprise URL. Keep it
   enabled. Upstream's own event boxes can stay off.
3. Under the notifier, tick **Recipe cards ready to review** (it's saved at once), then press **Send test
   notification**. The result shows under the button.

- The link uses `BASE_URL`, so set that to the address your phone opens Mealie at. While it points at localhost, the
  notifier card and **Group Settings > Recipe cards** warn you.
- Uploads through the API (Shortcuts, scripts, HA's `shell_command`) join the same user's batch until 2 minutes pass
  without a new card, and the inbox keeps batches of its own, so a notification comes at least 2 minutes after the
  last card. A camera snapshot into the inbox and an upload through the API at the same moment send two
  notifications. An app batch notifies once you tap **Done** and every card is read; an upload with `done=true`, once
  its card is read.
- A notifier hears about its own household's cards only.
- A batch where every card failed notifies too. A batch whose cards were last changed more than 24 hours ago never
  notifies, so restoring a backup doesn't repeat old notifications; a batch read over several days still does.
- A notifier that fails is tried again every few minutes, up to 5 times, and the server log names it. Each notifier
  gets the notification once; only if Mealie stops between sending and recording it can one get it twice.
- A failed test says *Test failed: The notifier didn't get it. Check its URL, and that the service it sends to is
  running.* Only household and group managers are told; anyone else sees *Test notification sent. Only household
  managers are told when one isn't delivered.*
- Notifications hold counts and a link only.

### "Recipe cards not added"

Photos the inbox couldn't use send "Recipe cards not added" to the same notifiers (those with **Recipe cards ready to
review** ticked): "2 recipe cards from the inbox weren't added (1 already scanned, 1 not a supported image). They're in
the inbox's failed folder." One goes out per burst: once a scan has taken everything it found in the folder, or two
minutes after the first refusal. It names no files, and its link opens the Recipe cards page.

---

## 9. Home Assistant

### Camera snapshots into the inbox

The simplest path from a camera: HA saves a snapshot straight into your household's inbox folder. No token is
needed.

1. Set up the inbox on the Mealie server as a network share ([`DEPLOY.md`](DEPLOY.md#the-inbox-folder)), for
   example an Unraid share `mealie-inbox` mounted in Mealie at `/inbox`.
2. In HA OS, go to **Settings > System > Storage > Add network storage**. Name it `mealie_inbox`, choose usage
   **Media**, and enter the server, the protocol (Samba or NFS), the share and its user. It appears in HA as
   `/media/mealie_inbox`.
3. Look up your folder in **Group Settings > Recipe cards** (`home/family` below).
4. Add a script:

   ```yaml
   script:
     mealie_scan_card:
       alias: "Mealie: scan a recipe card"
       sequence:
         - action: camera.snapshot
           target:
             entity_id: camera.kitchen_counter
           data:
             filename: "/media/mealie_inbox/home/family/card_{{ now().strftime('%Y%m%d_%H%M%S') }}.jpg"
   ```

Mealie takes the photo within a minute. Outside HA OS, the target folder has to be listed in
`homeassistant: allowlist_external_dirs`.

### Without shared storage: `shell_command`

HA can upload the snapshot itself with `curl`. Put the token in a file next to `configuration.yaml`, not in the
command: HA writes the whole command to its log when it fails. Create `/config/mealie_auth_header` with one line:

```text
Authorization: Bearer YOUR_API_TOKEN
```

```yaml
# configuration.yaml
shell_command:
  mealie_upload_card: >-
    curl -sS --fail-with-body --max-time 55 -H @/config/mealie_auth_header
    -F files=@/media/mealie_card.jpg http://192.168.1.10:9925/api/ai/ingest

script:
  mealie_send_card:
    alias: "Mealie: send a recipe card"
    sequence:
      - action: camera.snapshot
        target:
          entity_id: camera.kitchen_counter
        data:
          filename: /media/mealie_card.jpg
      - action: shell_command.mealie_upload_card
```

`--fail-with-body` makes a refused upload (a duplicate, a bad token, a full queue) show in HA's log with Mealie's
answer; without it HA reports success. HA stops a shell command after 60 seconds.

Using both scripts? Put them under one `script:` key; a second `script:` key replaces the first. The same goes for
`automation:`.

### With an image URL: `rest_command`

`rest_command` can't send a file, but it can send the address of one. This needs the Mealie server to allow image URLs
from HA's address (`AI_INGEST_URL_FETCH=true` and `AI_INGEST_URL_ALLOW_HOSTS=192.168.1.20`, see
[`DEPLOY.md`](DEPLOY.md#image-urls)); it's off by default. HA serves files in `/config/www/` at `/local/`, without
signing in, so anyone who can reach HA can read the latest snapshot there.

```yaml
# configuration.yaml (with mealie_api_bearer: "Bearer YOUR_API_TOKEN" in secrets.yaml)
rest_command:
  mealie_upload_card_url:
    url: http://192.168.1.10:9925/api/ai/ingest
    method: POST
    headers:
      Authorization: !secret mealie_api_bearer
    content_type: application/json
    payload: '{"images": [{"url": "http://192.168.1.20:8123/local/mealie_card.jpg"}]}'
    timeout: 60

script:
  mealie_send_card_url:
    alias: "Mealie: send a recipe card by URL"
    sequence:
      - action: camera.snapshot
        target:
          entity_id: camera.kitchen_counter
        data:
          filename: /config/www/mealie_card.jpg
      - action: rest_command.mealie_upload_card_url
```

### The "ready" notification on your phone

1. In Mealie, create a notifier ([section 8](#8-recipe-cards-ready-notifications)) with the URL
   `json://homeassistant.local:8123/api/webhook/mealie_cards` (or `jsons://` if HA uses HTTPS), and tick **Recipe
   cards ready to review**. Pick your own hard-to-guess webhook ID in place of `mealie_cards`.
2. Add the automation, with your phone's `notify` action:

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

3. Press **Send test notification** in Mealie. Your phone should show "Recipe cards ready (test)". Upstream's own
   notifier test doesn't fire this automation: it carries no event name. Mealie reports the test as sent whenever HA
   answers, and HA answers every webhook ID, even a mistyped one; if nothing shows, compare the webhook IDs (HA logs
   `Received message for unregistered webhook <id>`).

`document_data` is a JSON string with `batchId`, `jobIds`, `readyCount`, `needsAttentionCount`, `failedCount` and
`reviewUrl`. Match on the event name, never on a number. `local_only: true` accepts the webhook only from your local
network, so Mealie must reach HA there.

### Photos the inbox didn't add

Photos the inbox couldn't use send "Recipe cards not added" (`recipe_ingestion_rejected`) to the same notifiers. Add a
second automation on the same webhook ID, with the same `allowed_methods` and `local_only` (HA keeps the first
automation's settings for a shared webhook):

```yaml
automation:
  - alias: "Mealie: recipe cards not added"
    triggers:
      - trigger: webhook
        webhook_id: mealie_cards
        allowed_methods: [POST]
        local_only: true
    conditions:
      - condition: template
        value_template: "{{ trigger.json.event_type == 'recipe_ingestion_rejected' }}"
    actions:
      - variables:
          rejected: "{{ trigger.json.document_data | from_json }}"
      - action: notify.mobile_app_kitchen_phone
        data:
          title: "{{ trigger.json.title }}"
          message: "{{ trigger.json.message }}"
          data:
            url: "{{ rejected.reviewUrl }}"
            clickAction: "{{ rejected.reviewUrl }}"
```

Its `document_data` holds `count`, `reasons` (how many for each reason, such as `{"duplicate": 1,
"unsupported_format": 1}`) and `reviewUrl` (the Recipe cards page). Keep both automations under one `automation:` key.

### A "cards waiting" sensor

```yaml
rest:
  - resource: http://192.168.1.10:9925/api/ai/ingest/jobs/counts
    headers:
      Authorization: !secret mealie_api_bearer   # in secrets.yaml: mealie_api_bearer: "Bearer YOUR_API_TOKEN"
    scan_interval: 300
    sensor:
      - name: "Mealie recipe cards ready"
        unique_id: mealie_recipe_cards_ready
        value_template: "{{ value_json.ready }}"
        json_attributes:
          - needsAttention
          - processing
          - failed
```

The counts are for the token user's household: `ready`, `needsAttention` (ready cards with something to check),
`processing` and `failed`.

### Asking by voice

With [Layer B](../home-assistant/README.md#8-layer-b-mealies-mcp-server) set up, the `recipe_card_queue` tool answers
"Any recipe cards to review?" with counts only, such as "7 recipe cards are ready to review. 2 need a closer look."

---

## 10. Cleaning a photo for the eval set

Photos you add to the repository as eval fixtures must lose their metadata first: phone photos carry GPS, and an
iPhone saves an MPO (the photo plus a second, smaller frame). Cards saved with **Save as eval case** are already
clean. [`EVAL.md`](EVAL.md#cleaning-a-photo) has the details.

- **`strip_card_photo`**, lossless, with nothing to install (from a checkout of this repository):

  ```sh
  uv run python -m mealie.scripts.strip_card_photo IMG_2503.jpg card.jpg
  ```

  It keeps the first frame and the colour profile, drops EXIF, XMP, GPS and the second frame, and keeps only the EXIF
  orientation (`--drop-orientation` drops that too).
- **`jpegtran`**, lossless. Ubuntu and WSL don't include it: run `sudo apt install libjpeg-turbo-progs` first. It
  takes one input file and writes to `-outfile`:

  ```sh
  jpegtran -copy none -rotate 90 -perfect -outfile card.jpg IMG_2503.jpg   # the sideways view
  jpegtran -copy none -perfect -outfile card.jpg IMG_2503.jpg              # upright
  ```

  `-rotate 90` gives the sideways view a phone held flat produces; leave it out for an upright card. `-copy none`
  also drops the Display P3 colour profile (a slight colour shift). `-copy icc` keeps the profile, but also a stale
  index to the second frame, so the file still looks like a two-frame MPO.
- **Pillow**, which re-encodes the photo, keeps the profile, and leaves nothing behind in the folder (`uv` fetches
  Pillow into its cache):

  ```sh
  uv run --no-project --with pillow python -c "from PIL import Image; im = Image.open('IMG_2503.jpg'); im.save('card.jpg', quality=95, icc_profile=im.info.get('icc_profile'))"
  ```

---

## 11. Troubleshooting

| Symptom | Check |
|---|---|
| No **Recipe cards** entry | The group needs a default provider, plus an image provider or OCR ([section 1](#1-before-you-start)). |
| *Cards are accepted, but nothing on the server is reading them* | No process reads cards: check `AI_INGEST_WORKER` and the server log ([`DEPLOY.md`](DEPLOY.md#settings)). |
| A Shortcut or script gets `401` | It must send the `Authorization: Bearer <token>` header ([section 4](#4-ios-shortcuts)). |
| A Shortcut gets `429` | The group has 200 cards waiting, or you have as many being read as the server allows per user. Try again when some are read. |
| Uploads fail with `413` | A reverse proxy's body limit. For nginx, set `client_max_body_size 100m` ([`DEPLOY.md`](DEPLOY.md#reverse-proxy)). |
| "Recipe card uploads are paused while a backup is restored" | Wait a minute; the app retries on its own. |
| Cards come out sideways | Tesseract is missing or `AI_INGEST_ORIENT=false`. Use **⋯ > Rotate**. |
| A card *Tries again on* the 1st of the month | Every provider for a step used its monthly token limit. A group manager can raise the limit to read it sooner. |
| Inbox photos aren't picked up | `GET /api/ai/about` should show `"inbox": true`. Check the folder (group and household slugs), that the file has stopped changing, and *waiting in the inbox* on the Recipe cards page. |
| An inbox subfolder stays where it is | Mealie's group can't write to it: create card folders group-writable (umask `002`). The cards page lists it under *Not added from the inbox*. |
| Inbox photos land in `failed/` | Read the `.error.txt` next to the photo, or *Not added from the inbox* on the cards page. |
| No notification | The notifier is enabled, **Recipe cards ready to review** is ticked, and it's in the cards' household. Try **Send test notification**; a failed test names the problem, and the server log names the notifier. |
| The notification's link opens localhost | Set `BASE_URL` to the address your phone uses. |
| HA's automation doesn't fire | The webhook ID matches, `json://` versus `jsons://` matches how HA is served, and Mealie reaches HA on the local network. A sent test proves only that HA answered: HA accepts any webhook ID. |
| A card fails with *local only unavailable* | No local provider can do one of the steps ([section 7](#7-keeping-cards-on-your-network)). |
| Two-sided cards fail with a local model | Some local models take one image per request. Mealie then reads the pages one at a time; if that fails too, the eval shows which models work. |

---

## 12. Try it

A short check of the whole flow on your own phone and HA. Note what you see in the table at the end.

1. **iPhone, a 10-card stack.** Open **Recipe cards** in Safari, choose **Front & back**, and shoot 10 cards, some with
   a back and some with **No back**. Tap **Done**. Each card should read within a minute or two.
2. **Review.** Open the batch, fix what *Needs a look* lists, and **Commit & next** through it. Tap **Undo** on one
   *Added …* line and add it again. If the batch shows **Add N clean cards**, try it.
3. **A reload.** Turn on Airplane Mode and shoot a card. Turn Airplane Mode off and reload the page at once: the card
   should still be there and upload (tap **Retry** if it shows).
4. **Shortcuts.** Run *Scan a card*, *Scan a two-sided card* and *Share photos to Mealie*
   ([section 4](#4-ios-shortcuts)). Then send the same photo again: the notification should say *1 card was already
   scanned.* Change one letter of the token: it should say *Mealie didn't accept the API token.*
5. **HA.** Run the camera snapshot script ([section 9](#9-home-assistant)). The "ready" notification should reach your
   phone, and a tap should open the card to review. Drop a `.txt` file into your inbox folder: "Recipe cards not
   added" should follow within a few minutes. Check the sensor, and ask "Any recipe cards to review?".
6. **Each provider.** Read one card with each of your providers, and one with **Keep these cards on this server**
   on: its chip should say it stays on this server.
7. **The eval.** Save 20 cards as eval cases and score your providers
   ([`EVAL.md`](EVAL.md#runbook-20-cards-scored-per-provider)).

| Step | Date | Worked? | Notes |
|---|---|---|---|
| 1. iPhone stack | | | |
| 2. Review | | | |
| 3. Reload | | | |
| 4. Shortcuts | | | |
| 5. HA | | | |
| 6. Each provider | | | |
| 7. Eval | | | |
