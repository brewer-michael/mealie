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

Server settings are in [`DEPLOY.md`](DEPLOY.md#6-recipe-cards). The design is [`PHASE2.md`](PHASE2.md), and scoring
providers on your own cards is [`EVAL.md`](EVAL.md).

---

## 1. Before you start

- **AI providers.** A group manager sets them up under **Group Settings > AI Provider Settings**
  ([`DEPLOY.md` section 4](DEPLOY.md#4-configure-ai-providers)). Cards need a **default provider**, plus an **image
  provider** (a vision model) or the OCR fallback. Until then the Recipe cards entries are hidden.
- **Tesseract** (in images built with `INSTALL_OCR=true`, including `:ai`). Phones often save a card shot flat on
  the table sideways. Tesseract finds the right way up before the card is read. Without it those cards come out
  sideways, and you turn them by hand (**⋯ > Rotate** on the review page).
- **Group Settings > Recipe cards** holds the group's options: keep cards on this server ([section
  7](#7-keeping-cards-on-your-network)), **Check each card with a second reading** (each card costs a second image
  request), the inbox folder, and the eval cases.
- **Who can do what.** Anyone in a household can scan, review and commit; cards belong to the household that scanned
  them. Whoever commits a card owns the recipe. Discard is for the person who uploaded the card (anyone, for inbox
  cards) and household managers.

---

## 2. Scan from your phone

Open **Recipe cards** in the sidebar (it shows how many are ready, such as *Recipe cards (3)*), or **Create > Scan
recipe cards**, or the link on the **Import with AI** page. The address is `/g/<group-slug>/recipes/cards`.

1. Choose **One side** or **Front & back**. The choice is remembered on this device.
2. Tap **Take photo**. In Front & back mode the button then reads **Back side**, then **Next card**. **No back** ends a
   card that has nothing on the back; **Retake** replaces the last photo.
3. For a big stack it's quicker to shoot every card with the Camera app first, then tap **Choose photos** and pick
   them all. They wait in a tray. In Front & back mode they pair in the order you picked them; fix a pair with
   **Swap**, **Split**, **Join** or **Remove**, then tap **Upload**. On a computer you can drop photos on the page.
4. Each card uploads as soon as it's complete, two at a time, and is read while you keep shooting. A failed upload is
   retried three times, then shows **Retry**. A photo too large for the server or a proxy is made smaller once in
   the browser and sent again (*Making the photo smaller*).
5. Tap **Done** after the last card. Done never cuts off a card that's still uploading: the batch is closed only once
   every card has uploaded or failed (*Finishing the batch when the last cards are uploaded*). A batch you never mark
   Done closes itself 10 minutes after its last card.

- **Already scanned:** a card you've sent before (the same photos, usually a resend after a lost connection) is
  marked *Already scanned*, with **Open the earlier card**. The same front sent again with its back is a new card.
- **Keep the page open** until the uploads finish. The browser warns you if you try to leave, and reloading the page
  drops photos that haven't uploaded yet.
- **Where photos go:** a chip under the buttons says who reads the cards: *Read by Claude Sonnet (cloud)*, *Read with
  OCR, then Claude Sonnet (cloud)*, *Read by qwen3-vl on your network*, or a lock and *Stays on this server*. Tap it
  for **Keep these cards on this server** ([section 7](#7-keeping-cards-on-your-network)).
- **Formats:** JPEG, PNG, WebP, HEIC/HEIF, AVIF and TIFF (first page). PDFs aren't accepted. At most 30 MiB and 100
  megapixels a photo, and 4 photos a card.
- **Metadata:** each photo is turned upright and stripped of its metadata (GPS, camera, time) during the upload. The
  file as uploaded isn't kept.

---

## 3. Review and commit

The list under the capture buttons shows each batch's cards: *Reading*, *Ready*, *Ready · 2 to check* or *Failed:
…*, with **Review**, **Retry** and **Discard**. **Review batch** opens the batch's first card that needs a look.
Cards are reviewed in the order you shot them, so the screen matches the stack in your hand.

The review page shows the card and the recipe as read. **Needs a look (N)** lists each thing to check, with its
reason:

| You see | What to do |
|---|---|
| A gap the card leaves (`[blank]`, such as a missing time) | Type the value in, or **Keep blank** |
| A word the reader wasn't sure of, or couldn't read | Tap a suggested reading, **Re-read**, **Keep as written** or **Edit** |
| A number that isn't on the card, an amount or temperature that looks wrong | Fix it, or **Looks right** |
| An ingredient Mealie couldn't parse or link to your foods and units | Open the row and pick the unit and food |

Errors (red) block the commit until you fix them or keep them; warnings (amber) don't.

- **Re-read** an area: drag over part of the card. The new reading appears as a suggestion with **Use** and
  **Dismiss**. Re-reads go ahead of cards still waiting to be read.
- **⋯ (More):** **Rotate**, **Read whole card again**, **What the card says** (the transcription), **Save as eval
  case** (group managers; see [`EVAL.md`](EVAL.md#saving-eval-cases-from-the-phone)) and **Discard**.
- Edits save on their own (*Saved*). If the card was changed somewhere else, **Reload this card**.
- **Commit & next** creates the recipe and opens the next card; a clean card takes this one tap. **Skip** leaves a
  card for later. After the last card you're back at the list.
- On a computer: `Ctrl`/`⌘`+`Enter` commits, `Alt`+`↓`/`↑` moves between things to check, `R` re-reads an
  area, and `[` `]` switch between front and back.

**What commit does:**
- Ingredients are linked to your foods and units. New units are created. New foods are created only for users who can
  organize; for anyone else the food's name stays in the line as text.
- Tags, categories and tools are only suggested from the group's existing ones, never created.
- The card photo is attached to the recipe as an asset (*Recipe card*, *Recipe card (back)*) and, unless you turn off
  **Use the card photo as the recipe image**, used as its picture. If the household's new recipes are public, the
  photo shows on the public recipe too; the review page says so.

**How long cards stay:** ready cards wait until you review them. The photos of committed and failed cards are deleted
after 14 days (`AI_INGEST_RETENTION_DAYS`); a committed recipe keeps its own copy.

---

## 4. iOS Shortcuts

Shortcuts send cards to `POST /api/ai/ingest` ([section 5](#5-the-upload-api)). Each needs:

- **A Mealie API token.** Sign in as the user the cards should belong to, turn on **Show advanced features** in your
  profile settings, and create a token under **Profile > API Tokens**. Keep it as safe as a password. A dedicated
  user, such as the `Kitchen Voice` user from the [HA guide](../home-assistant/README.md#use-a-dedicated-kitchen-voice-user-for-the-token),
  works well.
- **The URL** `https://mealie.example.com/api/ai/ingest`, or `http://192.168.1.10:9925/api/ai/ingest` at home.
- **The header** `Authorization` with the value `Bearer <token>`. Mealie refuses uploads without it.

The answer holds `summary`, such as "1 recipe card queued. You'll be notified when it's ready.", in the phone's
language. When nothing was queued, the reason is in `detail.summary` (cards already scanned or that couldn't be used) or
`detail.message` (anything else).

**Scan a card** (one side):

| # | Action | Settings |
|---|---|---|
| 1 | Take Photo | |
| 2 | Get Contents of URL | the URL; Method **POST**; Headers: `Authorization` = `Bearer <token>`; Request Body **File**, File = *Photo* |
| 3 | Get Dictionary Value | `summary` in *Contents of URL* |
| 4 | Show Notification | *Dictionary Value* |

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
| 3 | Get Contents of URL | as in *Scan a card*, File = *Repeat Item* |
| 4 | Get Dictionary Value | `summary` in *Contents of URL* |
| 5 | End Repeat | |
| 6 | Show Notification | *Repeat Results* |

Uploads that follow each other within 2 minutes join one batch, so a shared stack sends one "ready" notification.
HEIC photos are fine. A JSON body is limited to 45 MiB, which is plenty for two photos.

---

## 5. The upload API

`POST /api/ai/ingest` takes **one card per request** (front first). It needs the `Authorization: Bearer <token>`
header; a browser cookie alone is refused.

| Content type | Body | Use |
|---|---|---|
| `multipart/form-data` | every file part is a photo (call the field `files`); optional text fields below | `curl -F`, Shortcuts *Form* |
| `image/*` or `application/octet-stream` | one photo; options in the query string | Shortcuts *File* |
| `application/json` | `{"images": [{"data": "<base64>", "filename": "front.jpg"}]}` plus options | Shortcuts *JSON*, scripts |

| Option | Meaning |
|---|---|
| `split` | `true`: each photo is its own card |
| `batchId` | join this app batch; `new` starts a new batch. Without it, uploads within 2 minutes of each other share a batch. |
| `position` | the card's place in the batch's review order |
| `localOnly` | `true`: keep this card on your network ([section 7](#7-keeping-cards-on-your-network)) |
| `allowDuplicate` | `true`: accept photos already scanned |

```sh
curl -sS -H "Authorization: Bearer $MEALIE_TOKEN" \
  -F files=@front.jpg -F files=@back.jpg \
  https://mealie.example.com/api/ai/ingest
```

```json
{"batchId": "…", "jobs": [{"id": "…", "status": "processing", "pageCount": 2, "reviewPath": "/g/home/recipes/cards/…"}],
 "rejected": [], "summary": "1 recipe card queued. You'll be notified when it's ready."}
```

| Status | Meaning |
|---|---|
| `202` | queued; `rejected` lists photos that weren't used (`duplicate`, `too_large`, `unsupported_format`, `pdf_not_supported`, `too_many_pixels`, `unreadable_image`, `too_many_pages`) |
| `400` | nothing was accepted (the same body inside `detail`), the group can't read cards (`ai_not_enabled`), or the card must stay local and can't (`local_only_unavailable`) |
| `401` | no `Authorization` header |
| `413` | over `AI_INGEST_MAX_UPLOAD_MB` (100 MB), or 45 MiB for JSON |
| `415` | another content type |
| `429` | the group already has 200 cards waiting to be read; see `Retry-After` |
| `503` | a backup restore is running (`Retry-After: 60`), or card scanning is turned off on the server |

`GET /api/ai/about` (no token needed) reports whether card scanning and the inbox are on, and the upload limits.

---

## 6. The inbox folder

A folder Mealie watches, for scanners, Syncthing, a network share or HA's camera snapshots. The server side (the
`/inbox` mount and `AI_INGEST_INBOX_DIR`) is in [`DEPLOY.md`](DEPLOY.md#the-inbox-folder).

- **One folder per household:** `<inbox>/<group-slug>/<household-slug>/`, such as `home/family/`. Mealie creates them
  on each scan. **Group Settings > Recipe cards** and the Recipe cards page show yours.
- **A photo is one card. A subfolder is one card with several pages**, in name order (`1-front.jpg`, `2-back.jpg`).
- **A file is taken once it stops changing:** Mealie looks every 30 seconds and takes a file whose size and time are
  unchanged since the last look and that is at least 10 seconds old. A file still being written is left alone.
- **Ignored:** names starting with `.` or `~`; partial downloads (`.tmp`, `.part`, `.crdownload`, `.partial`,
  `.download`, `.filepart`); `Thumbs.db` and `desktop.ini`; links.
- **Afterwards** the photo moves to `processed/YYYY-MM/` in the household's folder (or is deleted, with
  `AI_INGEST_INBOX_KEEP_PROCESSED=false`). A photo that can't be used moves to `failed/`, next to a
  `<name>.error.txt` that says why. Mealie never empties `processed/`.
- **Photos wait in place** while the group can't read cards, must stay local without a local reader, or already has
  200 cards waiting.
- **Inbox cards have no uploader:** they belong to the household, anyone in it can discard them, and their
  notification is in English.
- **Anyone who can write to the share can queue cards for any household in it.** Cards are still reviewed before they
  become recipes, but share the folder only with devices you trust.

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
     doesn't, never out.

Every AI call a local-only card causes is checked: the read, the OCR fallback, building the recipe, suggestions,
re-reads and re-extracts. If no local provider can do a step, the card fails with *local only unavailable* rather
than going to the cloud. A card queued as local-only stays local-only even if the setting is turned off later. The
review page shows a *Local only* badge.

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
3. Under the notifier, tick **Recipe cards ready to review**, then press **Send test notification**.

- The link uses `BASE_URL`, so set that to the address your phone opens Mealie at.
- Uploads from Shortcuts, scripts, HA and the inbox share a batch until 2 minutes pass without a new card, so their
  notification comes at least 2 minutes after the last card. An app batch notifies once you tap **Done** and every
  card is read.
- A notifier hears about its own household's cards only.
- A batch where every card failed notifies too. A batch created more than 24 hours ago never notifies, so restoring
  a backup doesn't repeat old notifications. If Mealie crashes at the wrong moment, a notification can be lost, never
  sent twice.
- Notifications hold counts and a link only.

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

HA can upload the snapshot itself with `curl`. Keep the whole command, token included, in `secrets.yaml`:

```yaml
# secrets.yaml
mealie_upload_card: >-
  curl -sS --max-time 55 -H "Authorization: Bearer YOUR_API_TOKEN"
  -F files=@/media/mealie_card.jpg http://192.168.1.10:9925/api/ai/ingest
```

```yaml
# configuration.yaml
shell_command:
  mealie_upload_card: !secret mealie_upload_card

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

HA stops a shell command after 60 seconds. `rest_command` can't send a camera image, so it isn't an option.

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
   notifier test doesn't fire this automation, because it sends a different event.

`document_data` is a JSON string with `batchId`, `jobIds`, `readyCount`, `needsAttentionCount`, `failedCount` and
`reviewUrl`. Match on the event name, never on a number. `local_only: true` accepts the webhook only from your local
network, so Mealie must reach HA there.

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
| A Shortcut or script gets `401` | It must send the `Authorization: Bearer <token>` header ([section 4](#4-ios-shortcuts)). |
| Uploads fail with `413` | A reverse proxy's body limit. For nginx, set `client_max_body_size 100m` ([`DEPLOY.md`](DEPLOY.md#reverse-proxy)). |
| "Recipe card uploads are paused while a backup is restored" | Wait a minute; the app retries on its own. |
| Cards come out sideways | Tesseract is missing or `OCR_ENABLED=false`. Use **⋯ > Rotate**. |
| Inbox photos aren't picked up | `GET /api/ai/about` should show `"inbox": true`. Check the folder (group and household slugs), that the file has stopped changing, and that the group can read cards. |
| Inbox photos land in `failed/` | Read the `.error.txt` next to the photo. |
| No notification | The notifier is enabled, **Recipe cards ready to review** is ticked, and it's in the cards' household. Try **Send test notification**. |
| HA's automation doesn't fire | The webhook ID matches, `json://` versus `jsons://` matches how HA is served, and Mealie reaches HA on the local network. |
| A card fails with *local only unavailable* | No local provider can do one of the steps ([section 7](#7-keeping-cards-on-your-network)). |
| Two-sided cards fail with a local model | Some local models take one image per request. The eval shows which ones work. |
