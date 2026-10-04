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
      <IngestPrivacyChip
        v-model:local-only="localOnly"
        class="mb-3 mb-sm-4"
        :settings="settings"
        :already-sent="sentBeforeLocalOnlyChange"
        :finished-batch="localOnlyFinishedBatch"
      />
      <IngestCapture v-if="!localOnlyBlocked" :max-pages-per-card="settings?.limits?.maxPagesPerCard" />
      <IngestUploadQueue class="mt-4" :group-slug="groupSlug" />
    </section>

    <p v-if="inboxFolder" class="inbox-hint text-body-2 text-medium-emphasis mb-6">
      {{ $t("recipe-ingest.settings.inbox-hint", { folder: inboxFolder }) }}
    </p>

    <!-- with scanning turned off on the server every list request is refused: no list at all -->
    <IngestBatchList v-if="showList" :group-slug="groupSlug" :batch-id="batchId" />
  </v-container>
</template>

<script setup lang="ts">
import { mdiCardTextOutline } from "@mdi/js";
import IngestBatchList from "~/components/Domain/Ingest/IngestBatchList.vue";
import IngestCapture from "~/components/Domain/Ingest/IngestCapture.vue";
import IngestPrivacyChip from "~/components/Domain/Ingest/IngestPrivacyChip.vue";
import IngestUploadQueue from "~/components/Domain/Ingest/IngestUploadQueue.vue";
import { nextLimitReset, useRecipeIngestSettings, useRecipeIngestText } from "~/composables/use-recipe-ingest";
import { useRecipeIngestUploads } from "~/composables/use-recipe-ingest-uploads";

/**
 * Recipe cards (docs/ai/PHASE2.md §1.1, §6.7): take or choose card photos, watch them upload and get read, and open
 * them for review. `?batch=<id>` shows one batch with its summary; `?unavailable=1` says the batch a notification
 * named can't be opened. Fork-owned.
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
const { localOnly, sentBeforeLocalOnlyChange, localOnlyFinishedBatch, openCardsPage } = useRecipeIngestUploads();
const { dateText } = useRecipeIngestText();

const canReadCards = computed(() => !!settings.value?.canReadCards);
/** The group keeps cards on this server, and no AI provider on the network can read them */
const localOnlyBlocked = computed(() => !!settings.value?.localOnly && !settings.value.localOnlyAvailable);
const inboxFolder = computed(() => (settings.value?.inbox?.enabled && settings.value.inbox.folder) || null);
/** The list waits for the settings, unless they failed to load (it then says itself whether it can load) */
const showList = computed(() => (settingsLoaded.value ? settings.value?.enabled !== false : settingsLoadFailed.value));
const batchUnavailable = computed(() => route.query.unavailable === "1");

function dismissBatchUnavailable() {
  const { unavailable: _unavailable, ...query } = route.query;
  void router.replace({ query });
}

// cards that fail to upload while this page is open show here, not as a toast and a sidebar badge
let closeCardsPage: (() => void) | null = null;
onMounted(() => {
  closeCardsPage = openCardsPage();
  void loadSettings();
});
onBeforeUnmount(() => closeCardsPage?.());
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
