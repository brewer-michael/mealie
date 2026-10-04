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

    <p v-if="batchId && loaded && batchSummary" class="batch-summary text-body-2">
      {{ batchSummary }}
    </p>

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
      <v-list class="py-0" density="comfortable">
        <IngestJobListItem
          v-for="job in batch.jobs"
          :key="job.id"
          :job="job"
          :group-slug="groupSlug"
          :busy="retrying.has(job.id)"
          @retry="retryJob"
          @discard="askDiscard"
        />
      </v-list>
    </section>

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
  </div>
</template>

<script setup lang="ts">
import { useDocumentVisibility } from "@vueuse/core";
import IngestJobListItem from "./IngestJobListItem.vue";
import { useUserApi } from "~/composables/api";
import { errorStatusOf, useRecipeIngestCounts } from "~/composables/use-recipe-ingest";
import { useRecipeIngestUploads } from "~/composables/use-recipe-ingest-uploads";
import { alert } from "~/composables/use-toast";
import type { PaginationData } from "~/lib/api/types/non-generated";
import type { IngestSource, IngestStatus, RecipeIngestionJobSummary } from "~/lib/api/types/recipe-ingest";
import type { RecipeIngestJobsQuery } from "~/lib/api/user/recipe-ingest";

/**
 * The household's cards by batch, newest first (docs/ai/PHASE2.md §6.7), and the ones added in the last 7 days. It
 * polls the batches that are being read or uploaded every 3 s while the page is visible, and at once after an upload.
 * Fork-owned.
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
}

const POLL_INTERVAL_MS = 3000;
const PER_PAGE = 100;
/** At most this many pages of a list (a household has at most 200 cards being read) */
const MAX_PAGES = 10;
const RECENT_DAYS = 7;
const RECENT_LIMIT = 50;
const OPEN_STATUSES: IngestStatus[] = ["processing", "ready", "failed", "committing"];

const i18n = useI18n();
const api = useUserApi();
const counts = useRecipeIngestCounts();
const uploads = useRecipeIngestUploads();
const visibility = useDocumentVisibility();

/** Cards not added yet (all of the batch's when filtered) */
const jobs = ref<Job[]>([]);
/** Cards added in the last 7 days, newest first */
const recent = ref<Job[]>([]);
const loading = ref(false);
const loaded = ref(false);
const loadFailed = ref(false);
const retrying = ref(new Set<string>());
const retryingBatch = ref<string | null>(null);
const discardDialog = ref(false);
const discardTarget = ref<Job | null>(null);

function isActive(job: Job): boolean {
  return job.status === "processing" || job.status === "committing" || !!job.task;
}

function isRecent(job: Job, now = Date.now()): boolean {
  const created = job.createdAt ? new Date(job.createdAt).getTime() : Number.NaN;
  return Number.isFinite(created) && now - created <= RECENT_DAYS * 24 * 60 * 60 * 1000;
}

function time(value: string | null | undefined): number {
  const parsed = value ? new Date(value).getTime() : Number.NaN;
  return Number.isFinite(parsed) ? parsed : 0;
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
    };
  });
  // Newest batch first: the one whose latest card arrived last
  const latest = (view: BatchView) => Math.max(...view.jobs.map(job => time(job.createdAt)));
  return views.sort((a, b) => latest(b) - latest(a));
});

const batchSummary = computed(() => {
  if (!props.batchId) {
    return null;
  }
  const added = recent.value.filter(job => job.batchId === props.batchId).length;
  const left = jobs.value.filter(job => job.batchId === props.batchId && job.status === "ready").length;
  return added ? i18n.t("recipe-ingest.queue.batch-summary", { added, left }) : null;
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

/** Every page of a job list; null when a request failed */
async function fetchAll(query: RecipeIngestJobsQuery): Promise<Job[] | null> {
  const items: Job[] = [];
  for (let page = 1; page <= MAX_PAGES; page++) {
    const { data } = await api.recipeIngest.getJobs({ ...query, page, perPage: PER_PAGE });
    const result = data as PaginationData<Job> | null;
    if (!result) {
      return null;
    }
    items.push(...result.items);
    if (page >= (result.total_pages ?? 1)) {
      break;
    }
  }
  return items;
}

function sortRecent(list: Job[]): Job[] {
  return [...list].sort((a, b) => time(b.createdAt) - time(a.createdAt));
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

const shouldPoll = computed(
  () => visibility.value === "visible" && (jobs.value.some(isActive) || uploads.isUploading.value),
);

function clearPollTimer() {
  if (pollTimer !== null) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}

function schedulePoll() {
  clearPollTimer();
  if (!disposed && shouldPoll.value) {
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
  const results = await Promise.all(ids.map(batchId => fetchAll({ batchId })));
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
    void counts.refresh();
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
      const all = await fetchAll({ batchId: props.batchId });
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
      const [open, committed] = await Promise.all([
        fetchAll({ status: OPEN_STATUSES }),
        api.recipeIngest.getJobs({ status: "committed", perPage: RECENT_LIMIT }),
      ]);
      if (!current()) {
        return;
      }
      if (open) {
        jobs.value = open;
      }
      const added = (committed.data as PaginationData<Job> | null)?.items;
      if (added) {
        recent.value = sortRecent(added.filter(job => isRecent(job)));
      }
      loadFailed.value = !open;
      loaded.value ||= !!open;
    }
    // The sidebar's count may be stale: cards were read while the user was elsewhere
    void counts.refresh();
  }
  finally {
    if (seq === loadSeq) {
      loading.value = false;
    }
  }
  schedulePoll();
}

watch(shouldPoll, (should) => {
  if (!should) {
    clearPollTimer();
  }
  else if (loaded.value && pollTimer === null && !polling) {
    // The page came back into view, or uploads started: catch up at once
    void pollNow();
  }
});

watch(uploads.uploadedCount, () => {
  const batchId = uploads.lastUploadBatchId.value;
  if (batchId) {
    extraBatchIds.add(batchId);
  }
  void pollNow();
});

watch(() => props.batchId, () => {
  loaded.value = false;
  jobs.value = [];
  recent.value = [];
  void load();
});

onMounted(load);

onBeforeUnmount(() => {
  disposed = true;
  clearPollTimer();
});

// ==========================================
// Actions

function markRetrying(jobId: string, on: boolean) {
  const next = new Set(retrying.value);
  if (on) {
    next.add(jobId);
  }
  else {
    next.delete(jobId);
  }
  retrying.value = next;
}

/** Retries a failed card; the server answers with its new state, so the list shows it as queued at once */
async function retryOne(job: Job): Promise<boolean> {
  markRetrying(job.id, true);
  try {
    const { data } = await api.recipeIngest.retry(job.id);
    if (data) {
      jobs.value = jobs.value.map(j => (j.id === job.id
        ? { ...j, status: data.status, task: data.task ?? null, error: data.error ?? null }
        : j));
      extraBatchIds.add(job.batchId);
    }
    return !!data;
  }
  finally {
    markRetrying(job.id, false);
  }
}

async function retryJob(job: Job) {
  await retryOne(job);
  void counts.refresh();
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
  void counts.refresh();
  void pollNow();
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
    void counts.refresh();
  }
  else if (!(error as { response?: { data?: { detail?: { message?: unknown } } } }).response?.data?.detail?.message) {
    // Errors with a message were already shown by the API client
    alert.error(i18n.t("recipe-ingest.error.unknown", { code: errorStatusOf(error) ?? "network" }));
  }
}
</script>
