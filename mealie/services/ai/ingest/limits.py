"""
Recipe card ingestion's fixed limits and timings (docs/ai/PHASE2.md §1.5, §15). Code constants rather than settings:
tests patch them. The few that are settings are in `mealie.services.ai.ingest.settings`.

Durations are in seconds.
"""

MIB = 1024 * 1024

# ==========================================
# Uploads (§1.2, §1.5)

MAX_FILE_BYTES = 30 * MIB
"""Per image"""
MAX_JSON_BODY_BYTES = 45 * MIB
"""Per `application/json` request, whatever `AI_INGEST_MAX_UPLOAD_MB` says"""
MAX_IMAGES_PER_REQUEST = 20
MAX_PAGES_PER_CARD = 4
MAX_MULTIPART_FIELDS = 20
MAX_PIXELS = 100_000_000
"""Checked before an image is decoded"""
MAX_PROCESSING_JOBS_PER_GROUP = 200
"""More are refused with 429"""
QUOTA_RETRY_AFTER = 60
PAUSED_RETRY_AFTER = 60
"""`Retry-After` while a backup restore pauses ingestion"""
INTAKE_CONCURRENCY = 2
"""Uploads normalized at once per process; the rest wait on the event loop"""

# ==========================================
# Pages (§2)

PAGE_MAX_SIDE = 4096
"""`page.jpg`'s long side, never upscaled"""
VIEW_MAX_SIDE = 2048
"""`view.jpg`'s long side, never upscaled: what the model and the review page see"""
THUMB_MAX_SIDE = 480
JPEG_QUALITY = 90
THUMB_WEBP_QUALITY = 80

# ==========================================
# Batches (§1.4)

APP_BATCH_IDLE = 10 * 60
"""An app batch nobody marked Done seals itself after this long without a card"""
AUTO_BATCH_IDLE = 2 * 60
"""API and inbox uploads join a batch that saw an upload this recently; it seals after this long without one"""
NOTIFY_CUTOFF = 24 * 60 * 60
"""Batches whose cards were last written longer ago never notify, so a restore doesn't replay old notifications"""
NOTIFY_LEASE = 5 * 60
"""Seconds a process has to send a batch's notification before housekeeping may try again"""
NOTIFY_ATTEMPTS = 5
"""Attempts at a batch's notification before it's given up on"""

# ==========================================
# The runner (§3)

POLL_INTERVAL = 5
"""How often the dispatcher looks for work when nothing wakes it"""
LEASE = 120
"""How long a claim lasts without a heartbeat"""
HEARTBEAT_INTERVAL = 20
TASK_DEADLINE = 30 * 60
MAX_ATTEMPTS = 3
"""Lease expiries before a task is given up as interrupted"""
MAX_RATE_LIMIT_RETRIES = 6
RATE_LIMIT_BACKOFF = 60
"""The first retry's delay after every provider answered 429; doubled each time"""
RATE_LIMIT_BACKOFF_MAX = 15 * 60
PRIORITY_REREAD = 0
PRIORITY_EXTRACT = 10
"""Lower runs first"""
REREAD_SLOTS = 1
"""Task threads per process that only re-reads use, on top of `AI_INGEST_CONCURRENCY`"""
DISPATCHER_DB_THREADS = 4
"""The dispatcher's own thread limiter for database calls"""
PHASE_BACKOFF_MAX = 60
"""A failing dispatcher phase backs off, doubling up to this"""
SHUTDOWN_GRACE = 5
"""How long shutdown waits for cancelled tasks before releasing their leases"""
PROGRESS_INTERVAL = 1
"""At most one progress write a second"""
LOCAL_ONLY_RECHECK = 5
"""How often a running task reads its group's local-only setting again: switching it on covers the task's next calls"""
HOUSEKEEPING_INTERVAL = 60
"""Sealing idle batches, sending due notifications, resuming stale commits, retrying cards after a monthly limit"""
LIMIT_RECHECK_INTERVAL = 10 * 60
"""How often a group's monthly limits are checked again for its cards waiting for them to reset"""
COMMIT_LEASE = 120
"""A commit not finished this long after it started is resumed"""
PURGE_INTERVAL = 24 * 60 * 60
PURGE_FIRST_DELAY = 10 * 60
"""The daily purge's first run after boot"""
ORPHAN_DIR_AGE = 60 * 60
"""Job directories with no row are removed once they're this old"""
EMPTY_BATCH_AGE = 24 * 60 * 60
"""Batches with no cards (a duplicate-only or abandoned upload) are removed once their last upload is this old"""
DISPATCHER_SEEN_INTERVAL = 60
"""How often a running dispatcher writes its presence file (`storage.dispatcher_seen_at`)"""
ORIENT_MIN_RATIO = 1.5
"""A page is turned only when the best orientation scores at least this many times the upright one (§4.4)"""

# ==========================================
# Pausing for a backup restore (§3.9)

PAUSE_REFRESH = 60
"""How often a restore rewrites the pause marker's time"""
PAUSE_TTL = 5 * 60
"""
How long a marker is honoured after its last refresh. A marker whose restore is gone is removed at once
(`storage.is_paused`); this bounds one whose restore can't be told (an older version's, or another host's where file
locks don't work).
"""
RESTORE_LOCK_WAIT = 45
"""
How long a restore waits for in-flight writers before giving up, having changed nothing ("try again"). Write sections
take seconds (an upstream write's starts once its body is in); this stays under the 60 s that reverse proxies commonly
allow a request, so the browser sees the answer.
"""
RESTORE_LOCK_POLL = 0.25
GUARD_THREADS = 4
"""Threads per process on which requests check for a pause, and writes enter their section (`restore_guard`)"""
PAUSED_TASK_POLL = 5
"""How often a task that hit the pause checks whether it's over"""
PAUSED_RELEASE_DELAY = 60
"""A task released because of a pause isn't claimed again for this long"""
KEPT_RESULT_TTL = 24 * 60 * 60
"""A result a restore cut off is kept this long for the card's next task; the daily purge removes older ones"""
KEPT_RESULT_POLL = 0.5
"""How often a task waiting for the result of one a restore cut off (still running) looks for it"""
KEPT_RESULT_WAIT = 5 * 60
"""How long a task waits for that result at most, then reads the card itself (a provider call takes a minute or two)"""

# ==========================================
# The inbox (§1.3)

INBOX_SETTLE = 10
"""A file is taken once its size and mtime are unchanged across two scans and it's this old"""
INBOX_CLAIM_RETRY = 10 * 60
"""A claimed file still in the claim folder after this long is claimed again and retried"""
INBOX_FILES_PER_TICK = 20

# ==========================================
# Region re-reads (§4.7)

REGION_MIN_SIDE = 0.02
REREAD_MARGIN = 0.03
"""Added around a re-read region on each side, as a fraction of the page"""
REREAD_MIN_SIDE = 1000
"""A crop with a shorter long side is upscaled to this"""
REREAD_MAX_UPSCALE = 3
