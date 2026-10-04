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
      v-else-if="settingsLoadFailed"
      class="settings-load-failed mb-6"
      type="error"
      variant="tonal"
    >
      {{ $t("recipe-ingest.settings.load-failed") }}
    </v-alert>

    <section v-else-if="canReadCards" class="capture mb-6">
      <!-- uploads are still taken (the limit may reset first); a card read while it applies fails and can be retried -->
      <v-alert
        v-if="settings?.limitReached"
        class="limit-reached mb-4"
        type="warning"
        variant="tonal"
        density="compact"
      >
        {{ $t("recipe-ingest.capture.limit-reached") }}
      </v-alert>
      <IngestPrivacyChip
        v-model:local-only="localOnly"
        class="mb-3 mb-sm-4"
        :settings="settings"
        :already-sent="sentBeforeLocalOnlyChange"
      />
      <IngestCapture :max-pages-per-card="settings?.limits?.maxPagesPerCard" />
      <IngestUploadQueue class="mt-4" :group-slug="groupSlug" />
    </section>

    <p v-if="inboxFolder" class="inbox-hint text-body-2 text-medium-emphasis mb-6">
      {{ $t("recipe-ingest.settings.inbox-hint", { folder: inboxFolder }) }}
    </p>

    <IngestBatchList :group-slug="groupSlug" :batch-id="batchId" />
  </v-container>
</template>

<script setup lang="ts">
import { mdiCardTextOutline } from "@mdi/js";
import IngestBatchList from "~/components/Domain/Ingest/IngestBatchList.vue";
import IngestCapture from "~/components/Domain/Ingest/IngestCapture.vue";
import IngestPrivacyChip from "~/components/Domain/Ingest/IngestPrivacyChip.vue";
import IngestUploadQueue from "~/components/Domain/Ingest/IngestUploadQueue.vue";
import { useRecipeIngestSettings } from "~/composables/use-recipe-ingest";
import { useRecipeIngestUploads } from "~/composables/use-recipe-ingest-uploads";

/**
 * Recipe cards (docs/ai/PHASE2.md §1.1, §6.7): take or choose card photos, watch them upload and get read, and open
 * them for review. `?batch=<id>` shows one batch with its summary. Fork-owned.
 */
definePageMeta({
  middleware: ["group-only"],
});

const i18n = useI18n();
const route = useRoute();
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
const { localOnly, sentBeforeLocalOnlyChange } = useRecipeIngestUploads();

const canReadCards = computed(() => !!settings.value?.canReadCards);
const inboxFolder = computed(() => (settings.value?.inbox?.enabled && settings.value.inbox.folder) || null);

onMounted(loadSettings);
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
