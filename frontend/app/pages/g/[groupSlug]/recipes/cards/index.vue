<template>
  <v-container class="ingest-cards-page narrow-container">
    <!-- phones: no icon, a smaller description and tighter spacing, so the capture buttons sit in the top half -->
    <BasePageTitle divider class="ingest-cards-page__title">
      <template #header>
        <v-icon
          class="d-none d-sm-flex"
          :icon="mdiCardTextOutline"
          size="64"
          color="primary"
        />
      </template>
      <template #title>
        {{ $t("recipe-ingest.capture.title") }}
      </template>
      <span class="ingest-cards-page__description">{{ $t("recipe-ingest.capture.description") }}</span>
    </BasePageTitle>

    <v-progress-linear v-if="!settingsLoaded && settingsLoading" class="mb-4" indeterminate />

    <!-- a notification's batch that can't be opened any more (review.vue sends the user here) -->
    <v-alert
      v-if="batchUnavailable"
      class="batch-unavailable mb-4"
      type="info"
      variant="tonal"
      density="compact"
      closable
      @click:close="dismissBatchUnavailable"
    >
      {{ $t("recipe-ingest.queue.batch-unavailable") }}
    </v-alert>

    <v-alert
      v-if="settingsLoaded && settings?.enabled === false"
      class="ingest-disabled mb-6"
      type="info"
      variant="tonal"
    >
      {{ $t("recipe-ingest.error.ingest_disabled") }}
    </v-alert>

    <v-alert
      v-else-if="settingsLoaded && !canReadCards"
      class="cannot-read mb-6"
      type="info"
      variant="tonal"
    >
      {{ $t("recipe-ingest.capture.cannot-read") }}
    </v-alert>

    <v-alert
      v-else-if="settingsLoadFailed && !settingsLoaded"
      class="settings-load-failed mb-6"
      type="error"
      variant="tonal"
    >
      {{ $t("recipe-ingest.settings.load-failed") }}
      <template #append>
        <v-btn
          class="settings-retry"
          size="small"
          variant="text"
          :loading="settingsLoading"
          @click="loadSettings"
        >
          {{ $t("recipe-ingest.queue.retry") }}
        </v-btn>
      </template>
    </v-alert>

    <section v-else-if="canReadCards" class="capture mb-6">
      <!-- no card reader has run lately (AI_INGEST_WORKER off, or it stopped): uploads are taken, but nothing reads them -->
      <v-alert
        v-if="readerStopped"
        class="reader-not-running mb-4"
        type="warning"
        variant="tonal"
        density="compact"
      >
        {{ $t("recipe-ingest.settings.reader-not-running") }}
      </v-alert>
      <!-- the group keeps cards on this server, and nothing on the network can read them: every card would fail -->
      <v-alert
        v-if="localOnlyBlocked"
        class="local-only-blocked mb-4"
        type="warning"
        variant="tonal"
      >
        {{ $t("recipe-ingest.capture.local-only-blocked") }}
      </v-alert>
      <!-- uploads are still taken: a card read while the limit applies is read again by itself once it resets -->
      <v-alert
        v-else-if="settings?.limitReached"
        class="limit-reached mb-4"
        type="warning"
        variant="tonal"
        density="compact"
      >
        {{ $t("recipe-ingest.capture.limit-reached", { date: dateText(nextLimitReset(), true) }) }}
      </v-alert>
      <!-- cards are read, but an optional part of the read is skipped this month: a soft note, not a warning -->
      <p v-if="limitedFeatures.length" class="limited-features text-body-small text-medium-emphasis mb-3">
        <span v-for="feature in limitedFeatures" :key="feature" class="limited-feature d-block">
          {{ $t(`recipe-ingest.settings.limited.${feature}`, { date: dateText(nextLimitReset()) }) }}
        </span>
      </p>
      <!-- another tab of this browser keeps the user's queue (it uploads it): this one only says so, and can take it -->
      <v-alert
        v-if="queueElsewhere"
        class="queue-elsewhere mb-4"
        type="info"
        variant="tonal"
      >
        {{ $t("recipe-ingest.capture.queue-elsewhere") }}
        <template #append>
          <v-btn
            class="use-this-tab"
            size="small"
            variant="text"
            :loading="takingOver"
            @click="useThisTab"
          >
            {{ $t("recipe-ingest.capture.use-this-tab") }}
          </v-btn>
        </template>
      </v-alert>
      <template v-else>
        <IngestPrivacyChip
          v-model:local-only="localOnly"
          class="mb-3 mb-sm-4"
          :settings="settings"
          :already-sent="sentBeforeLocalOnlyChange"
          :finished-batch="localOnlyFinishedBatch"
        />
        <IngestCapture v-if="!localOnlyBlocked" :max-pages-per-card="settings?.limits?.maxPagesPerCard" />
      </template>
      <IngestUploadQueue class="mt-4" :group-slug="groupSlug" />
    </section>
    <!--
      without the capture section (scanning off, no reader, settings that didn't load), photos already queued still show
      here: their uploads fail until cards can be read, and Retry or Remove is how the user gets them out
    -->
    <IngestUploadQueue v-if="queueWithoutCapture" class="upload-queue-alone mb-6" :group-slug="groupSlug" />

    <p v-if="inboxFolder" class="inbox-hint text-body-medium text-medium-emphasis mb-6">
      {{ $t("recipe-ingest.settings.inbox-hint", { folder: inboxFolder }) }}
    </p>
    <!-- photos waiting in the household's inbox folder and why, and the ones it refused lately -->
    <IngestInboxStatus class="mb-6" :settings="settings" />

    <!-- with scanning turned off on the server every list request is refused: no list at all -->
    <IngestBatchList v-if="showList" :group-slug="groupSlug" :batch-id="batchId" />
  </v-container>
</template>

<script setup lang="ts">
import { mdiCardTextOutline } from "@mdi/js";
import { useDocumentVisibility, useIntervalFn } from "@vueuse/core";
import IngestBatchList from "~/components/Domain/Ingest/IngestBatchList.vue";
import IngestCapture from "~/components/Domain/Ingest/IngestCapture.vue";
import IngestInboxStatus from "~/components/Domain/Ingest/IngestInboxStatus.vue";
import IngestPrivacyChip from "~/components/Domain/Ingest/IngestPrivacyChip.vue";
import IngestUploadQueue from "~/components/Domain/Ingest/IngestUploadQueue.vue";
import { nextLimitReset, useRecipeIngestSettings, useRecipeIngestText } from "~/composables/use-recipe-ingest";
import { useRecipeIngestUploads } from "~/composables/use-recipe-ingest-uploads";

/**
 * Recipe cards (docs/ai/PHASE2.md §1.1, §6.7): take or choose card photos, watch them upload and get read, and open
 * them for review. `?batch=<id>` shows one batch with its summary; `?unavailable=1` says the batch a notification
 * named can't be opened. It warns when nothing on the server reads cards, notes optional parts of the read a monthly
 * limit skips, and shows the household's inbox: photos waiting there and why, and the ones it refused. Those change
 * by themselves, so while the page is visible the settings are asked again every minute, and on coming back to it.
 * When another tab of this browser keeps the user's upload queue, the page says so instead of capturing, with "Use this
 * tab". Fork-owned.
 */
definePageMeta({
  middleware: ["group-only"],
});

const i18n = useI18n();
const route = useRoute();
const router = useRouter();
const auth = useMealieAuth();

useSeoMeta({
  title: i18n.t("recipe-ingest.nav.recipe-cards"),
});

const groupSlug = computed(() => (route.params.groupSlug as string) || auth.user.value?.groupSlug || "");
const batchId = computed(() => (typeof route.query.batch === "string" && route.query.batch) || null);

const {
  settings,
  loaded: settingsLoaded,
  loading: settingsLoading,
  loadFailed: settingsLoadFailed,
  load: loadSettings,
} = useRecipeIngestSettings();
const {
  localOnly,
  sentBeforeLocalOnlyChange,
  localOnlyFinishedBatch,
  openCardsPage,
  queueElsewhere,
  takeOverQueue,
} = useRecipeIngestUploads();
const { dateText } = useRecipeIngestText();

/** While the page is visible, the settings (the reader, the inbox) are asked again this often */
const SETTINGS_REFRESH_MS = 60_000;

const canReadCards = computed(() => !!settings.value?.canReadCards);
/** No card reader (the ingest worker) has run in the last 3 minutes */
const readerStopped = computed(() => settings.value?.enabled !== false && settings.value?.readerRunning === false);
/** Optional parts of the read a monthly limit skips (tag suggestions, the second reading) */
const limitedFeatures = computed(() => settings.value?.limitedFeatures ?? []);
/** The group keeps cards on this server, and no AI provider on the network can read them */
const localOnlyBlocked = computed(() => !!settings.value?.localOnly && !settings.value.localOnlyAvailable);
const inboxFolder = computed(() => (settings.value?.inbox?.enabled && settings.value.inbox.folder) || null);
/** The list waits for the settings, unless they failed to load (it then says itself whether it can load) */
const showList = computed(() => (settingsLoaded.value ? settings.value?.enabled !== false : settingsLoadFailed.value));
/** The capture section, with its upload queue, is shown: the alerts above it say why it isn't */
const showCapture = computed(() => {
  if (settingsLoaded.value && (settings.value?.enabled === false || !canReadCards.value)) {
    return false;
  }
  return !(settingsLoadFailed.value && !settingsLoaded.value) && canReadCards.value;
});
/** Once the settings have answered (or failed to), the queue shows without the capture section */
const queueWithoutCapture = computed(() => !showCapture.value && (settingsLoaded.value || settingsLoadFailed.value));
const batchUnavailable = computed(() => route.query.unavailable === "1");

/** "Use this tab" is waiting for the other tab to hand the queue over */
const takingOver = ref(false);

async function useThisTab() {
  takingOver.value = true;
  try {
    await takeOverQueue();
  }
  finally {
    takingOver.value = false;
  }
}

function dismissBatchUnavailable() {
  const { unavailable: _unavailable, ...query } = route.query;
  void router.replace({ query });
}

// what the page warns about changes by itself (a reader starts or stops, the inbox takes or refuses photos): asked
// again every minute while the page is visible, and at once when it's back
const visibility = useDocumentVisibility();
const settingsRefresh = useIntervalFn(() => void loadSettings(), SETTINGS_REFRESH_MS, { immediate: false });
watch(visibility, (state) => {
  if (state === "visible") {
    void loadSettings();
    settingsRefresh.resume();
  }
  else {
    settingsRefresh.pause();
  }
});

// cards that fail to upload while this page is open show here, not as a toast and a sidebar badge
let closeCardsPage: (() => void) | null = null;
onMounted(() => {
  closeCardsPage = openCardsPage();
  void loadSettings();
  if (visibility.value === "visible") {
    settingsRefresh.resume();
  }
});
onBeforeUnmount(() => {
  settingsRefresh.pause();
  closeCardsPage?.();
});
</script>

<style scoped>
@media (max-width: 599.98px) {
  .ingest-cards-page__title :deep(h2) {
    margin: 0 0 4px;
  }

  .ingest-cards-page__title :deep(h3) {
    margin: 0;
  }

  .ingest-cards-page__description {
    display: block;
    font-size: 0.875rem;
    line-height: 1.25rem;
  }
}
</style>
