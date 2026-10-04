<template>
  <v-container class="ingest-cards-page narrow-container">
    <BasePageTitle divider>
      <template #header>
        <v-icon
          :icon="mdiCardTextOutline"
          size="64"
          color="primary"
        />
      </template>
      <template #title>
        {{ $t("recipe-ingest.capture.title") }}
      </template>
      {{ $t("recipe-ingest.capture.description") }}
    </BasePageTitle>

    <v-progress-linear v-if="!settingsLoaded && settingsLoading" class="mb-4" indeterminate />

    <v-alert
      v-if="settingsLoaded && !canReadCards"
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
      <IngestPrivacyChip
        v-model:local-only="localOnly"
        class="mb-4"
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
