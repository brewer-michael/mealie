<template>
  <v-list-item class="ingest-job px-0" :data-status="job.status">
    <template #prepend>
      <v-avatar
        class="job-thumb mr-3"
        rounded="lg"
        size="56"
        color="surface-variant"
      >
        <v-img
          v-if="job.thumbUrl"
          :src="job.thumbUrl"
          :alt="title"
          cover
        />
        <v-icon v-else :icon="mdiCardTextOutline" />
      </v-avatar>
    </template>
    <v-list-item-title class="job-title">
      <NuxtLink v-if="link" :to="link" class="text-decoration-none">
        {{ title }}
      </NuxtLink>
      <template v-else>
        {{ title }}
      </template>
    </v-list-item-title>
    <div class="d-flex flex-wrap align-center ga-1 mt-1">
      <v-chip
        class="job-status"
        size="small"
        variant="tonal"
        :color="chip.color"
      >
        <v-progress-circular
          v-if="chip.busy"
          indeterminate
          size="12"
          width="2"
          class="mr-1"
        />
        {{ chip.text }}
      </v-chip>
      <v-chip
        v-if="job.localOnly"
        class="job-local-only"
        size="small"
        variant="text"
        :prepend-icon="$globals.icons.lock"
      >
        {{ $t("recipe-ingest.queue.local-only") }}
      </v-chip>
    </div>
    <template #append>
      <div class="d-flex align-center ga-1">
        <v-btn
          v-if="job.status === 'ready'"
          class="job-review"
          size="small"
          color="primary"
          variant="tonal"
          :to="jobLink"
        >
          {{ $t("recipe-ingest.queue.review") }}
        </v-btn>
        <v-btn
          v-if="job.status === 'failed'"
          class="job-retry"
          size="small"
          variant="tonal"
          :loading="busy"
          @click="emit('retry', job)"
        >
          {{ $t("recipe-ingest.queue.retry") }}
        </v-btn>
        <v-btn
          v-if="recipeLink"
          class="job-view-recipe"
          size="small"
          variant="text"
          :to="recipeLink"
        >
          {{ $t("recipe-ingest.queue.view-recipe") }}
        </v-btn>
        <v-btn
          v-if="canDiscard"
          class="job-discard"
          icon
          size="small"
          variant="text"
          :aria-label="$t('recipe-ingest.queue.discard')"
          :title="$t('recipe-ingest.queue.discard')"
          @click="emit('discard', job)"
        >
          <v-icon :icon="$globals.icons.delete" />
        </v-btn>
      </div>
    </template>
  </v-list-item>
</template>

<script setup lang="ts">
import { mdiCardTextOutline } from "@mdi/js";
import { useRecipeIngestText } from "~/composables/use-recipe-ingest";
import type { RecipeIngestionJobSummary } from "~/lib/api/types/recipe-ingest";

/** One card in the queue (docs/ai/PHASE2.md §6.7): thumbnail, name, a status chip, Retry, Discard or Review. Fork-owned. */
const props = defineProps<{
  job: RecipeIngestionJobSummary;
  groupSlug: string;
  /** A Retry for this card is in flight */
  busy?: boolean;
}>();

const emit = defineEmits<{
  (e: "retry" | "discard", job: RecipeIngestionJobSummary): void;
}>();

const i18n = useI18n();
const { ingestErrorText, progressText } = useRecipeIngestText();

const title = computed(() => props.job.title || i18n.t("recipe-ingest.queue.untitled"));
const jobLink = computed(() => `/g/${props.groupSlug}/recipes/cards/${props.job.id}`);
const recipeLink = computed(() => {
  const slug = props.job.status === "committed" ? props.job.recipe?.slug : null;
  return slug ? `/g/${props.groupSlug}/r/${slug}` : null;
});
/** The card's review page, or its recipe once added */
const link = computed(() => (props.job.status === "committed" ? recipeLink.value : jobLink.value));
const canDiscard = computed(() => props.job.status !== "committing" && props.job.status !== "committed");

const chip = computed<{ text: string; color?: string; busy?: boolean }>(() => {
  const job = props.job;
  switch (job.status) {
    case "processing":
      if (!job.task || job.task.state === "queued") {
        return { text: progressText("queued") ?? i18n.t("recipe-ingest.queue.waiting"), color: "info", busy: true };
      }
      return { text: progressText(job.task.progressKey) ?? i18n.t("recipe-ingest.queue.reading"), color: "info", busy: true };
    case "ready": {
      const toCheck = (job.errorCount ?? 0) + (job.warningCount ?? 0);
      return toCheck
        ? { text: i18n.t("recipe-ingest.queue.ready-to-check", { count: toCheck }), color: "warning" }
        : { text: i18n.t("recipe-ingest.queue.ready"), color: "success" };
    }
    case "failed": {
      const reason = job.error
        ? ingestErrorText(job.error.code, job.error.params)
        : ingestErrorText("internal_error");
      return { text: i18n.t("recipe-ingest.queue.failed", { reason }), color: "error" };
    }
    case "committing":
      return { text: i18n.t("recipe-ingest.queue.committing"), color: "info", busy: true };
    default:
      return { text: i18n.t("recipe-ingest.queue.committed"), color: "success" };
  }
});
</script>
