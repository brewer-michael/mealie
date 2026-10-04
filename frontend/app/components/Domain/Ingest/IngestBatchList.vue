<template>
  <div class="ingest-batch-list">
    <div class="d-flex align-center flex-wrap ga-2 mb-2">
      <h3 class="text-h6">
        {{ $t("recipe-ingest.queue.title") }}
      </h3>
      <v-spacer />
      <v-btn
        v-if="batchId"
        class="show-all"
        size="small"
        variant="text"
        :to="`/g/${groupSlug}/recipes/cards`"
      >
        {{ $t("general.show-all") }}
      </v-btn>
    </div>

    <div v-if="batchId && loaded && batchSummary" class="batch-summary text-body-2 mb-2">
      {{ batchSummary }}
    </div>
    <!--
      what the review said about the batch's last card, or what "Add N clean cards" did when its batch has gone: here,
      by the summary, where a toast would cover the page title
    -->
    <IngestBatchListNotice
      v-if="notice && !noticeInBatch"
      class="mb-3"
      :notice="notice"
      :busy="undoing"
      @dismiss="notice = null"
      @undo="undoCommit"
    />

    <v-alert
      v-if="loadFailed"
      class="load-failed mb-4"
      type="error"
      variant="tonal"
    >
      {{ $t("recipe-ingest.queue.load-failed") }}
      <template #append>
        <v-btn size="small" variant="text" @click="load">
          {{ $t("recipe-ingest.queue.retry") }}
        </v-btn>
      </template>
    </v-alert>

    <v-progress-linear v-if="!loaded && loading" class="list-loading" indeterminate />

    <p v-if="loaded && !batches.length && !recent.length" class="no-cards text-medium-emphasis">
      {{ $t("recipe-ingest.queue.empty") }}
    </p>

    <section
      v-for="batch in batches"
      :key="batch.id"
      class="ingest-batch mb-4"
      :data-batch="batch.id"
    >
      <div class="batch-header d-flex align-center flex-wrap ga-2">
        <div>
          <div class="batch-title text-subtitle-1">
            {{ $t("recipe-ingest.queue.batch", { date: formatDate(batch.createdAt) }) }}
          </div>
          <div class="batch-meta text-caption text-medium-emphasis">
            {{ $t("recipe-ingest.queue.batch-cards", batch.jobs.length) }} · {{ sourceText(batch.source) }}
          </div>
        </div>
        <v-spacer />
        <v-btn
          v-if="batch.failed"
          class="batch-retry-failed"
          size="small"
          variant="text"
          :loading="retryingBatch === batch.id"
          @click="retryFailed(batch)"
        >
          {{ $t("recipe-ingest.queue.retry-failed") }}
        </v-btn>
        <v-btn
          v-if="batch.clean.length >= MIN_CLEAN_CARDS"
          class="batch-add-clean"
          size="small"
          color="primary"
          variant="text"
          :loading="cleanBusy === batch.id"
          :disabled="cleanBusy !== null && cleanBusy !== batch.id"
          @click="askAddClean(batch)"
        >
          {{ $t("recipe-ingest.queue.add-clean", batch.clean.length) }}
        </v-btn>
        <v-btn
          v-if="batch.ready"
          class="batch-review"
          size="small"
          color="primary"
          variant="flat"
          :to="`/g/${groupSlug}/recipes/cards/review?batch=${batch.id}`"
        >
          {{ $t("recipe-ingest.queue.review-batch") }}
        </v-btn>
      </div>
      <!-- what "Add N clean cards" did, by the batch -->
      <IngestBatchListNotice
        v-if="notice && notice.batchId === batch.id"
        class="my-2"
        :notice="notice"
        :busy="undoing"
        @dismiss="notice = null"
        @undo="undoCommit"
      />
      <v-list class="py-0" density="comfortable">
        <IngestJobListItem
          v-for="job in batch.jobs"
          :key="job.id"
          :job="job"
          :group-slug="groupSlug"
          :busy="busyJobs.has(job.id)"
          @retry="retryJob"
          @cancel="cancelJob"
          @discard="askDiscard"
        />
      </v-list>
    </section>

    <!-- more open cards than one load fetches: the oldest wait for this, rather than being left out unsaid -->
    <div v-if="openMore" class="load-older-open d-flex align-center flex-wrap ga-2 mb-4">
      <span class="text-body-2 text-medium-emphasis">
        {{ $t("recipe-ingest.queue.newest-shown", jobs.length) }}
      </span>
      <v-btn
        class="load-older"
        size="small"
        variant="tonal"
        :loading="loadingOlder === 'open'"
        :disabled="loadingOlder !== null"
        @click="loadOlder('open')"
      >
        {{ $t("recipe-ingest.queue.load-older") }}
      </v-btn>
    </div>

    <section v-if="recent.length" class="recently-added mt-6">
      <h4 class="text-subtitle-1 mb-1">
        {{ $t("recipe-ingest.queue.recently-added") }}
      </h4>
      <v-list class="py-0" density="comfortable">
        <IngestJobListItem
          v-for="job in recent"
          :key="job.id"
          :job="job"
          :group-slug="groupSlug"
        />
      </v-list>
      <v-btn
        v-if="recentMore"
        class="load-older-recent mt-2"
        size="small"
        variant="tonal"
        :loading="loadingOlder === 'recent'"
        :disabled="loadingOlder !== null"
        @click="loadOlder('recent')"
      >
        {{ $t("recipe-ingest.queue.load-older") }}
      </v-btn>
    </section>

    <BaseDialog
      v-model="discardDialog"
      bottom-sheet
      :title="$t('recipe-ingest.queue.discard')"
      color="error"
      :icon="$globals.icons.alertCircle"
      can-confirm
      @confirm="confirmDiscard"
    >
      <v-card-text>
        {{ $t("recipe-ingest.queue.discard-confirm") }}
      </v-card-text>
    </BaseDialog>

    <BaseDialog
      v-model="cleanDialog"
      bottom-sheet
      :title="$t('recipe-ingest.queue.add-clean-title', cleanCards.length)"
      :icon="$globals.icons.check"
      can-confirm
      @confirm="addClean"
    >
      <v-card-text>
        <p class="mb-2">
          {{ $t("recipe-ingest.queue.add-clean-confirm") }}
        </p>
        <ul class="clean-cards ps-4">
          <li v-for="card in cleanCards" :key="card.id" class="clean-card">
            {{ cardTitle(card) }}
          </li>
        </ul>
      </v-card-text>
    </BaseDialog>
  </div>
</template>

<script setup lang="ts">
import { useDocumentVisibility } from "@vueuse/core";
import IngestBatchListNotice from "./IngestBatchListNotice.vue";
import IngestJobListItem from "./IngestJobListItem.vue";
import { useUserApi } from "~/composables/api";
import {
  errorCodeOf,
  errorMessageOf,
  errorStatusOf,
  serverDate,
  takeRecipeIngestCommitNotice,
  useRecipeIngestCounts,
  useRecipeIngestText,
} from "~/composables/use-recipe-ingest";
import type { RecipeIngestQueueNotice } from "~/composables/use-recipe-ingest";
import { carryReviewNotice } from "~/composables/use-recipe-ingest-review";
import { useRecipeIngestUploads } from "~/composables/use-recipe-ingest-uploads";
import { alert } from "~/composables/use-toast";
import type {
  IngestSource,
  IngestStatus,
  RecipeIngestionJobCounts,
  RecipeIngestionJobSummary,
} from "~/lib/api/types/recipe-ingest";
import type { RecipeIngestJobsQuery } from "~/lib/api/user/recipe-ingest";

/**
 * The household's cards by batch, newest first (docs/ai/PHASE2.md §6.7), and the ones added as recipes in the last 7
 * days, latest added first. A load fetches the newest 1000 open cards and 50 added ones; "Load older cards" fetches
 * more of either. It polls the batches that are being read or uploaded every 3 s while the page is visible, and at
 * once after an upload. Cards that arrive or change elsewhere (the inbox, a Shortcut, another device) show too: while
 * the page is visible the shared counts are checked every 20 s and a change reloads the list, as does coming back to
 * the page. With one batch it sums the batch up, and shows what the review said about the batch's last card, whose
 * Undo takes that card back to review. Fork-owned.
 */
const props = defineProps<{
  groupSlug: string;
  /** Show only this batch, with its summary (the review page's "back to the queue") */
  batchId?: string | null;
}>();

type Job = RecipeIngestionJobSummary;

interface BatchView {
  id: string;
  /** In capture order */
  jobs: Job[];
  /** The first card's arrival */
  createdAt: string | null;
  source: IngestSource;
  ready: number;
  failed: number;
  /** Ready cards with nothing to check and nothing reading them: "Add N clean cards" adds them */
  clean: Job[];
}

const POLL_INTERVAL_MS = 3000;
/** How often, while the page is visible, the counts are checked for changes made elsewhere */
const COUNTS_INTERVAL_MS = 20_000;
const PER_PAGE = 100;
/**
 * The open cards' pages a load fetches at first, and how many more each "Load older cards" adds: ready and failed
 * cards stay until they're reviewed or removed, so a household can have more open cards than one load should fetch
 */
const OPEN_PAGES = 10;
const RECENT_DAYS = 7;
const DAY_MS = 24 * 60 * 60 * 1000;
/** Added cards per page; a load fetches one page at first, and each "Load older cards" one more */
const RECENT_PER_PAGE = 50;
const OPEN_STATUSES: IngestStatus[] = ["processing", "ready", "failed", "committing"];
/** At least this many clean cards make "Add N clean cards" worth offering */
const MIN_CLEAN_CARDS = 2;

const i18n = useI18n();
const api = useUserApi();
const router = useRouter();
const { cardTitle, ingestErrorText } = useRecipeIngestText();
const counts = useRecipeIngestCounts();
const uploads = useRecipeIngestUploads();
const visibility = useDocumentVisibility();

/** Cards not added yet (all of the batch's when filtered) */
const jobs = ref<Job[]>([]);
/** Cards added in the last 7 days (all of the batch's when filtered), latest added first */
const recent = ref<Job[]>([]);
/** How many pages of open and of added cards a load fetches ("Load older cards" raises them) */
const openPages = ref(OPEN_PAGES);
const recentPages = ref(1);
/** The server has more open, or more added, cards than the list fetched */
const openMore = ref(false);
const recentMore = ref(false);
/** The list whose older cards are being loaded */
const loadingOlder = ref<"open" | "recent" | null>(null);
const loading = ref(false);
const loaded = ref(false);
const loadFailed = ref(false);
/** Cards with a Retry or Cancel in flight */
const busyJobs = ref(new Set<string>());
const retryingBatch = ref<string | null>(null);
const discardDialog = ref(false);
const discardTarget = ref<Job | null>(null);

/**
 * "Added Banana Mug Cake · 2 cards are still being read", left by the review page after the batch's last card, with
 * Undo for the card just added
 */
const left = takeRecipeIngestCommitNotice();
const notice = ref<RecipeIngestQueueNotice | null>(left
  ? {
      kind: left.warning ? "warning" : "success",
      text: left.text,
      detail: left.warning,
      items: [],
      undoJobId: left.undoJobId ?? null,
    }
  : null);

function isActive(job: Job): boolean {
  return job.status === "processing" || job.status === "committing" || !!job.task;
}

/** Ready, with nothing highlighted to check and nothing reading it again */
function isClean(job: Job): boolean {
  return job.status === "ready" && !job.task && !job.errorCount && !job.warningCount;
}

/** The time a card was added as a recipe, or uploaded; 0 for none (a server time without an offset is UTC) */
function time(value: string | null | undefined): number {
  return serverDate(value)?.getTime() ?? 0;
}

/** Added as a recipe in the last 7 days (by when it was added, not when it was uploaded) */
function isRecent(job: Job, now = Date.now()): boolean {
  const committed = time(job.committedAt);
  return committed > 0 && now - committed <= RECENT_DAYS * DAY_MS;
}

const batches = computed<BatchView[]>(() => {
  const byBatch = new Map<string, Job[]>();
  for (const job of jobs.value) {
    if (job.status !== "committed") {
      byBatch.set(job.batchId, [...(byBatch.get(job.batchId) ?? []), job]);
    }
  }
  const views = [...byBatch.entries()].map(([id, batchJobs]): BatchView => {
    const sorted = [...batchJobs].sort((a, b) => a.position - b.position || time(a.createdAt) - time(b.createdAt));
    const first = [...sorted].sort((a, b) => time(a.createdAt) - time(b.createdAt))[0];
    return {
      id,
      jobs: sorted,
      createdAt: first?.createdAt ?? null,
      source: first?.source ?? "app",
      ready: sorted.filter(job => job.status === "ready").length,
      failed: sorted.filter(job => job.status === "failed").length,
      clean: sorted.filter(isClean),
    };
  });
  // Newest batch first: the one whose latest card arrived last
  const latest = (view: BatchView) => Math.max(...view.jobs.map(job => time(job.createdAt)));
  return views.sort((a, b) => latest(b) - latest(a));
});

/** The notice is about a batch the list shows: it goes in that batch's section */
const noticeInBatch = computed(() => !!notice.value?.batchId && batches.value.some(b => b.id === notice.value?.batchId));

/**
 * The batch so far: "3 added, 1 left to review, 2 still being read, 1 failed". "Batch done" only once none of its
 * cards is being read; a card being added counts as added.
 */
const batchSummary = computed(() => {
  if (!props.batchId) {
    return null;
  }
  const open = jobs.value.filter(job => job.batchId === props.batchId);
  const count = (status: IngestStatus) => open.filter(job => job.status === status).length;
  const added = recent.value.filter(job => job.batchId === props.batchId).length + count("committing");
  const reading = count("processing");
  const parts = [
    { key: "recipe-ingest.queue.summary-added", count: added },
    { key: "recipe-ingest.queue.summary-left", count: count("ready") },
    { key: "recipe-ingest.queue.summary-reading", count: reading },
    { key: "recipe-ingest.queue.summary-failed", count: count("failed") },
  ]
    .filter(part => part.count > 0)
    .map(part => i18n.t(part.key, { count: part.count }));
  if (!parts.length) {
    return null;
  }
  const summary = parts.join(", ");
  return reading ? summary : i18n.t("recipe-ingest.queue.batch-done", { summary });
});

function formatDate(value: string | null): string {
  const date = value ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) {
    return "";
  }
  return new Intl.DateTimeFormat(i18n.locale.value, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function sourceText(source: IngestSource): string {
  return i18n.t(`recipe-ingest.queue.source-${source}`);
}

// ==========================================
// Loading and polling

interface FetchedPages {
  items: Job[];
  /** The server has more pages than were fetched */
  more: boolean;
}

/** The first `pages` pages of a job list (fewer when it ends sooner); null when a request failed */
async function fetchPages(query: RecipeIngestJobsQuery, pages: number, perPage: number): Promise<FetchedPages | null> {
  const items = new Map<string, Job>();
  for (let page = 1; page <= pages; page++) {
    const { data: result } = await api.recipeIngest.getJobs({ ...query, page, perPage });
    if (!result) {
      return null;
    }
    // a card that moved to the next page while the pages were read shows once
    result.items.forEach(job => items.set(job.id, job));
    if (page >= (result.total_pages ?? 1)) {
      return { items: [...items.values()], more: false };
    }
  }
  return { items: [...items.values()], more: true };
}

/** All of one batch's cards, in one request (a batch shows whole: its counts, Retry failed, Add clean cards) */
async function fetchBatch(batchId: string): Promise<Job[] | null> {
  const { data: result } = await api.recipeIngest.getJobs({ batchId, perPage: -1 });
  return result?.items ?? null;
}

/** Latest added first */
function sortRecent(list: Job[]): Job[] {
  return [...list].sort((a, b) => time(b.committedAt) - time(a.committedAt) || time(b.createdAt) - time(a.createdAt));
}

/** Replaces one batch's cards with what the server says now; whether a card arrived, changed status or left */
function mergeBatch(batchId: string, items: Job[]): boolean {
  const before = jobs.value.filter(job => job.batchId === batchId);
  const known = new Set(before.map(job => job.id));
  const statuses = new Map(items.map(job => [job.id, job.status]));
  const open = items.filter(job => job.status !== "committed");
  const changed = before.some(job => statuses.get(job.id) !== job.status) || open.some(job => !known.has(job.id));

  jobs.value = [...jobs.value.filter(job => job.batchId !== batchId), ...open];
  const added = items.filter(job => job.status === "committed" && (props.batchId || isRecent(job)));
  if (added.length) {
    const ids = new Set(added.map(job => job.id));
    recent.value = sortRecent([...recent.value.filter(job => !ids.has(job.id)), ...added]);
  }
  return changed;
}

let disposed = false;
let pollTimer: ReturnType<typeof setTimeout> | null = null;
let polling = false;
let pollAgain = false;
/** Batches to poll once more whatever their state (the batch of an upload that just finished) */
const extraBatchIds = new Set<string>();
let countsTimer: ReturnType<typeof setTimeout> | null = null;
/** The counts as of the list's last load or change: other counts mean something changed elsewhere */
let seenCounts: string | null = null;

const isVisible = () => visibility.value === "visible";
const shouldPoll = computed(() => jobs.value.some(isActive) || uploads.isUploading.value);

function clearPollTimer() {
  if (pollTimer !== null) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}

function countsKey(value: RecipeIngestionJobCounts | null | undefined): string | null {
  return value ? [value.processing ?? 0, value.ready ?? 0, value.needsAttention ?? 0, value.failed ?? 0].join("/") : null;
}

/** Refreshes the shared counts (the sidebar's) after a change this list made or saw, and remembers them */
async function refreshCounts() {
  const fresh = await counts.refresh();
  seenCounts = countsKey(fresh) ?? seenCounts;
}

function clearCountsTimer() {
  if (countsTimer !== null) {
    clearTimeout(countsTimer);
    countsTimer = null;
  }
}

function scheduleCountsCheck() {
  clearCountsTimer();
  if (!disposed && isVisible()) {
    countsTimer = setTimeout(() => {
      countsTimer = null;
      void checkCounts();
    }, COUNTS_INTERVAL_MS);
  }
}

/** Counts that changed although this list did nothing: cards arrived, were read or added elsewhere. Reload. */
async function checkCounts() {
  const fresh = countsKey(await counts.refresh());
  if (disposed) {
    return;
  }
  if (fresh !== null && seenCounts !== null && fresh !== seenCounts) {
    await load();
    return;
  }
  seenCounts = fresh ?? seenCounts;
  scheduleCountsCheck();
}

function schedulePoll() {
  clearPollTimer();
  if (!disposed && shouldPoll.value && isVisible()) {
    pollTimer = setTimeout(() => {
      pollTimer = null;
      void pollNow();
    }, POLL_INTERVAL_MS);
  }
}

/** The batches being read or uploaded, plus the ones asked for */
function batchesToPoll(): string[] {
  const ids = new Set<string>([
    ...jobs.value.filter(isActive).map(job => job.batchId),
    ...uploads.uploadingBatchIds.value,
    ...extraBatchIds,
  ]);
  extraBatchIds.clear();
  return [...ids].filter(id => !props.batchId || id === props.batchId);
}

async function poll() {
  const ids = batchesToPoll();
  if (!ids.length) {
    return;
  }
  const results = await Promise.all(ids.map(batchId => fetchBatch(batchId)));
  if (disposed) {
    return;
  }
  let changed = false;
  ids.forEach((batchId, index) => {
    const items = results[index];
    if (items) {
      changed = mergeBatch(batchId, items) || changed;
    }
  });
  if (changed) {
    void refreshCounts();
  }
}

/** Polls now (or right after the poll in flight), then every 3 s while there's something to watch */
async function pollNow() {
  if (polling) {
    pollAgain = true;
    return;
  }
  clearPollTimer();
  polling = true;
  try {
    await poll();
  }
  finally {
    polling = false;
  }
  if (pollAgain && !disposed) {
    pollAgain = false;
    await pollNow();
    return;
  }
  schedulePoll();
}

let loadSeq = 0;

/** Loads the list (a newer load wins over one still in flight) */
async function load() {
  const seq = ++loadSeq;
  const current = () => seq === loadSeq && !disposed;
  loading.value = true;
  try {
    if (props.batchId) {
      const all = await fetchBatch(props.batchId);
      if (!current()) {
        return;
      }
      if (all) {
        jobs.value = all.filter(job => job.status !== "committed");
        recent.value = sortRecent(all.filter(job => job.status === "committed"));
      }
      loadFailed.value = !all;
      loaded.value ||= !!all;
    }
    else {
      // added in the last 7 days by when they were added: a card uploaded earlier and added today is among them
      const since = new Date(Date.now() - RECENT_DAYS * DAY_MS);
      const [open, committed] = await Promise.all([
        fetchPages({ status: OPEN_STATUSES }, openPages.value, PER_PAGE),
        fetchPages({ status: "committed", committedSince: since, orderBy: "committedAt" }, recentPages.value, RECENT_PER_PAGE),
      ]);
      if (!current()) {
        return;
      }
      if (open) {
        jobs.value = open.items;
        openMore.value = open.more;
      }
      if (committed) {
        recent.value = sortRecent(committed.items);
        recentMore.value = committed.more;
      }
      loadFailed.value = !open;
      loaded.value ||= !!open;
    }
    // The sidebar's count may be stale: cards were read while the user was elsewhere
    await refreshCounts();
  }
  finally {
    if (seq === loadSeq) {
      loading.value = false;
    }
  }
  if (seq === loadSeq) {
    schedulePoll();
    scheduleCountsCheck();
  }
}

watch(shouldPoll, (should) => {
  if (!should) {
    clearPollTimer();
  }
  else if (loaded.value && pollTimer === null && !polling && !loading.value && isVisible()) {
    // Uploads started: catch up at once (a load in flight polls after it)
    void pollNow();
  }
});

watch(visibility, (state) => {
  if (state !== "visible") {
    clearPollTimer();
    clearCountsTimer();
  }
  else if (loaded.value) {
    // Back on the page: whatever happened meanwhile, here or elsewhere, shows at once
    void load();
  }
});

watch(uploads.uploadedCount, () => {
  const batchId = uploads.lastUploadBatchId.value;
  if (batchId) {
    extraBatchIds.add(batchId);
  }
  void pollNow();
});

/** "Load older cards": the next open cards, or the next added ones; the list keeps them through its reloads */
async function loadOlder(list: "open" | "recent") {
  if (loadingOlder.value) {
    return;
  }
  loadingOlder.value = list;
  if (list === "open") {
    openPages.value += OPEN_PAGES;
  }
  else {
    recentPages.value += 1;
  }
  try {
    await load();
  }
  finally {
    loadingOlder.value = null;
  }
}

watch(() => props.batchId, () => {
  loaded.value = false;
  jobs.value = [];
  recent.value = [];
  openPages.value = OPEN_PAGES;
  recentPages.value = 1;
  openMore.value = false;
  recentMore.value = false;
  notice.value = null;
  void load();
});

onMounted(load);

onBeforeUnmount(() => {
  disposed = true;
  clearPollTimer();
  clearCountsTimer();
});

// ==========================================
// Actions

function markBusy(jobId: string, on: boolean) {
  const next = new Set(busyJobs.value);
  if (on) {
    next.add(jobId);
  }
  else {
    next.delete(jobId);
  }
  busyJobs.value = next;
}

/**
 * Says why the server refused a card action. Errors with a message were already shown by the API client; the others
 * say why by their code (`invalid_status`: retried or discarded elsewhere; `forbidden`: only the uploader, or a
 * household manager, discards someone else's card).
 */
function notifyRefusal(error: unknown) {
  if (errorMessageOf(error)) {
    return;
  }
  const code = errorCodeOf(error);
  const status = errorStatusOf(error);
  if (code) {
    alert.error(ingestErrorText(code));
  }
  else {
    // no answer at all: the server couldn't be reached
    alert.error(status === null ? i18n.t("recipe-ingest.error.network") : i18n.t("recipe-ingest.error.unknown", { code: status }));
  }
}

/** Shows the card's state as the server answered it */
function applyState(jobId: string, state: { status: IngestStatus; task?: Job["task"]; error?: Job["error"] }) {
  jobs.value = jobs.value.map(j => (j.id === jobId
    ? { ...j, status: state.status, task: state.task ?? null, error: state.error ?? null }
    : j));
}

/**
 * Retries a failed card; the server answers with its new state, so the list shows it as queued at once. A refusal
 * says why, and the card's batch is polled so the row shows what the card is now.
 */
async function retryOne(job: Job): Promise<boolean> {
  markBusy(job.id, true);
  try {
    const { data, error } = await api.recipeIngest.retry(job.id);
    if (data) {
      applyState(job.id, data);
    }
    else {
      notifyRefusal(error);
    }
    extraBatchIds.add(job.batchId);
    return !!data;
  }
  finally {
    markBusy(job.id, false);
  }
}

async function retryJob(job: Job) {
  await retryOne(job);
  void refreshCounts();
  void pollNow();
}

/**
 * Stops reading a card: a card still waiting fails as cancelled at once, one being read stops within moments (the
 * poll shows it). It can be retried.
 */
async function cancelJob(job: Job) {
  markBusy(job.id, true);
  try {
    const { data, error } = await api.recipeIngest.cancel(job.id);
    if (data) {
      applyState(job.id, data);
    }
    else {
      notifyRefusal(error);
    }
    extraBatchIds.add(job.batchId);
  }
  finally {
    markBusy(job.id, false);
  }
  void refreshCounts();
  void pollNow();
}

async function retryFailed(batch: BatchView) {
  retryingBatch.value = batch.id;
  try {
    await Promise.all(batch.jobs.filter(job => job.status === "failed").map(job => retryOne(job)));
  }
  finally {
    retryingBatch.value = null;
  }
  void refreshCounts();
  void pollNow();
}

// ==========================================
// Adding a batch's clean cards

/** The batch whose clean cards are being checked or added */
const cleanBusy = ref<string | null>(null);
const cleanDialog = ref(false);
const cleanBatchId = ref<string | null>(null);
/** The cards the question lists, in capture order, with the draft version each was checked at */
const cleanCards = ref<Job[]>([]);
let cleanVersions: Record<string, number> = {};

function showNotice(
  batchId: string,
  kind: RecipeIngestQueueNotice["kind"],
  text: string,
  detail: string | null = null,
  items: string[] = [],
) {
  notice.value = { kind, text, detail, items, batchId };
}

/**
 * "Add N clean cards": the batch is read again, so the question lists the cards as they are now, each with the draft
 * version it was read at, and only those are added (a card changed after this is left for review)
 */
async function askAddClean(batch: BatchView) {
  cleanBusy.value = batch.id;
  try {
    const items = await fetchBatch(batch.id);
    if (disposed) {
      return;
    }
    if (!items) {
      showNotice(batch.id, "error", i18n.t("recipe-ingest.queue.clean-check-failed"));
      return;
    }
    mergeBatch(batch.id, items);
    const listed = items.filter(isClean).sort((a, b) => a.position - b.position);
    const versions = Object.fromEntries(listed.map(job => [job.id, job.draftVersion]));
    if (!listed.length) {
      showNotice(batch.id, "info", i18n.t("recipe-ingest.queue.clean-none"));
      return;
    }
    cleanBatchId.value = batch.id;
    cleanCards.value = listed;
    cleanVersions = versions;
    cleanDialog.value = true;
  }
  finally {
    cleanBusy.value = null;
  }
}

/** Why a card was left for review when the clean cards were added */
function cleanSkipReason(code: string): string {
  const key = `recipe-ingest.queue.clean-skip.${code}`;
  return i18n.te(key) ? i18n.t(key) : ingestErrorText(code);
}

/** Adds the cards the question listed, one by one on the server; then says what was added and what was left */
async function addClean() {
  const batchId = cleanBatchId.value;
  const cards = cleanCards.value;
  if (!batchId || !cards.length) {
    return;
  }
  cleanBusy.value = batchId;
  try {
    const { data, error } = await api.recipeIngest.commitClean(batchId, {
      jobIds: cards.map(card => card.id),
      draftVersions: cleanVersions,
    });
    if (disposed) {
      return;
    }
    if (!data) {
      // a message the API client showed (a restore running) isn't said again
      if (!errorMessageOf(error)) {
        showNotice(batchId, "error", i18n.t("recipe-ingest.queue.add-clean-failed"));
      }
    }
    else {
      const added = data.committed?.length ?? 0;
      const skipped = data.skipped ?? [];
      const names = new Map(cards.map(card => [card.id, cardTitle(card)]));
      const text = added
        ? i18n.t("recipe-ingest.queue.added-clean", added)
        : i18n.t("recipe-ingest.queue.added-clean-none");
      if (!skipped.length) {
        showNotice(batchId, "success", text);
      }
      else {
        showNotice(
          batchId,
          added ? "warning" : "error",
          text,
          i18n.t("recipe-ingest.queue.clean-left", skipped.length),
          skipped.map(item => i18n.t("recipe-ingest.queue.clean-skipped", {
            title: names.get(item.jobId) ?? i18n.t("recipe-ingest.queue.untitled"),
            reason: cleanSkipReason(item.code),
          })),
        );
      }
    }
  }
  finally {
    cleanBusy.value = null;
    cleanCards.value = [];
    cleanBatchId.value = null;
  }
  // the list shows what happened: added cards move to Recently added, the others stay
  const items = await fetchBatch(batchId);
  if (items && !disposed) {
    mergeBatch(batchId, items);
  }
  void refreshCounts();
}

// ==========================================
// Undo on "Added …"

/** Undo is being sent */
const undoing = ref(false);

function cardPath(jobId: string): string {
  return `/g/${props.groupSlug}/recipes/cards/${jobId}`;
}

/**
 * Undo on the review's "Added …": takes the card just added back to review (its recipe is deleted) and opens it. A
 * recipe edited since isn't deleted from here: the line says so and links to the card, whose Back to review asks
 * first. Other refusals say why.
 */
async function undoCommit(jobId: string) {
  if (undoing.value) {
    return;
  }
  undoing.value = true;
  try {
    const { data, error } = await api.recipeIngest.uncommit(jobId, {});
    if (disposed) {
      return;
    }
    if (data) {
      void refreshCounts();
      carryReviewNotice(jobId, { kind: "success", text: i18n.t("recipe-ingest.review.back-to-review-done"), detail: null });
      await router.push(cardPath(jobId));
      return;
    }
    if (errorCodeOf(error) === "recipe_edited") {
      notice.value = {
        kind: "warning",
        text: ingestErrorText("recipe_edited"),
        detail: null,
        items: [],
        batchId: notice.value?.batchId ?? null,
        cardPath: cardPath(jobId),
      };
    }
    else {
      notifyRefusal(error);
      // gone, or not added any more (taken back elsewhere): Undo can't do anything now
      const status = errorStatusOf(error);
      if ((status === 404 || status === 409) && notice.value?.undoJobId === jobId) {
        notice.value = { ...notice.value, undoJobId: null };
      }
    }
  }
  finally {
    undoing.value = false;
  }
}

function askDiscard(job: Job) {
  discardTarget.value = job;
  discardDialog.value = true;
}

async function confirmDiscard() {
  const job = discardTarget.value;
  discardTarget.value = null;
  if (!job) {
    return;
  }
  const { error } = await api.recipeIngest.discard(job.id);
  // Already gone is as good as discarded
  if (!error || errorStatusOf(error) === 404) {
    jobs.value = jobs.value.filter(j => j.id !== job.id);
    alert.success(i18n.t("recipe-ingest.queue.discarded"));
    void refreshCounts();
  }
  else {
    notifyRefusal(error);
  }
}
</script>

<style scoped>
/* a long batch's clean cards scroll inside the question, so its buttons stay on screen */
.clean-cards {
  max-height: 40vh;
  overflow-y: auto;
}
</style>
