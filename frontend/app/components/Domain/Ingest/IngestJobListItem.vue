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
    <!-- text and actions wrap: on a phone the buttons go under the chips, so the name and the reason keep the width -->
    <div class="job-body">
      <div class="job-text">
        <v-list-item-title class="job-title">
          <NuxtLink v-if="link" :to="link" class="text-decoration-none">
            {{ title }}
          </NuxtLink>
          <template v-else>
            {{ title }}
          </template>
        </v-list-item-title>
        <div v-if="subtitle" class="job-source text-body-small text-medium-emphasis">
          {{ subtitle }}
        </div>
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
        <!-- why it failed, or what's being done: wraps, where a chip would cut it -->
        <div v-if="chip.caption" class="job-caption text-body-small mt-1" :class="chip.color === 'error' ? 'text-error' : 'text-medium-emphasis'">
          {{ chip.caption }}
        </div>
        <!-- a failed card: when it's read again by itself (over the monthly limit), else when it's removed -->
        <div v-if="failedWhen" class="job-when text-body-small text-medium-emphasis">
          {{ failedWhen }}
        </div>
      </div>
      <div class="job-actions d-flex align-center ga-1">
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
          v-if="job.status === 'processing'"
          class="job-cancel"
          size="small"
          variant="text"
          :loading="busy"
          @click="emit('cancel', job)"
        >
          {{ $t("recipe-ingest.queue.cancel") }}
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
    </div>
  </v-list-item>
</template>

<script setup lang="ts">
import { mdiCardTextOutline } from "@mdi/js";
import { serverDate, sourceFileName, useRecipeIngestText } from "~/composables/use-recipe-ingest";
import type { RecipeIngestionJobSummary } from "~/lib/api/types/recipe-ingest";

/**
 * One card in the queue (docs/ai/PHASE2.md §6.7): thumbnail, name (two lines at most), a short status chip with the
 * reason or progress under it, and Review, Retry, Cancel or Discard. A failed card says when it's read again by
 * itself (it failed over the monthly token limit, §3.6) or else when it's removed with its photos (§16). Fork-owned.
 */
const props = defineProps<{
  job: RecipeIngestionJobSummary;
  groupSlug: string;
  /** A Retry or Cancel for this card is in flight */
  busy?: boolean;
}>();

const emit = defineEmits<{
  (e: "retry" | "discard" | "cancel", job: RecipeIngestionJobSummary): void;
}>();

const i18n = useI18n();
const { cardTitle, ingestErrorText, progressText, dateText } = useRecipeIngestText();

/** The file a card came from: shown for inbox and API cards, whose capture order means nothing to the user */
const fileName = computed(() => sourceFileName(props.job.sourceName));

/**
 * The card's name; before it's read (or when it couldn't be), its file for inbox and API cards, else its place in the
 * batch
 */
const title = computed(() => cardTitle(props.job));
/** A failed card names its file, so it can be found and sent again */
const subtitle = computed(() =>
  props.job.status === "failed" && fileName.value && fileName.value !== title.value ? fileName.value : null,
);
const jobLink = computed(() => `/g/${props.groupSlug}/recipes/cards/${props.job.id}`);
const recipeLink = computed(() => {
  const slug = props.job.status === "committed" ? props.job.recipe?.slug : null;
  return slug ? `/g/${props.groupSlug}/r/${slug}` : null;
});
/** The card's review page, or its recipe once added */
const link = computed(() => (props.job.status === "committed" ? recipeLink.value : jobLink.value));
/** The server says who may (the uploader, anyone for an inbox card, household managers); never once it's added */
const canDiscard = computed(() =>
  props.job.canDiscard !== false && props.job.status !== "committing" && props.job.status !== "committed",
);

/** "Tries again on Nov 1, 2026, 1:00 AM" (`autoRetryAt`), else "Removed on Nov 15, 2026" (`expiresAt`) */
const failedWhen = computed(() => {
  if (props.job.status !== "failed") {
    return null;
  }
  const retryAt = serverDate(props.job.autoRetryAt);
  if (retryAt) {
    // the dispatcher reads a card that's due within a minute
    return retryAt.getTime() > Date.now()
      ? i18n.t("recipe-ingest.queue.retries-on", { date: dateText(retryAt, true) })
      : i18n.t("recipe-ingest.queue.retries-soon");
  }
  const expiresAt = serverDate(props.job.expiresAt);
  return expiresAt ? i18n.t("recipe-ingest.queue.removed-on", { date: dateText(expiresAt) }) : null;
});

interface Chip {
  text: string;
  color?: string;
  busy?: boolean;
  /** The reason or progress, under the chip */
  caption?: string | null;
}

const chip = computed<Chip>(() => {
  const job = props.job;
  switch (job.status) {
    case "processing": {
      if (!job.task || job.task.state === "queued") {
        return { text: progressText("queued") ?? i18n.t("recipe-ingest.queue.waiting"), color: "info", busy: true };
      }
      const reading = i18n.t("recipe-ingest.queue.reading");
      const progress = progressText(job.task.progressKey);
      return { text: reading, color: "info", busy: true, caption: progress !== reading ? progress : null };
    }
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
      return { text: i18n.t("recipe-ingest.queue.status-failed"), color: "error", caption: reason };
    }
    case "committing":
      return { text: i18n.t("recipe-ingest.queue.committing"), color: "info", busy: true };
    default:
      return { text: i18n.t("recipe-ingest.queue.committed"), color: "success" };
  }
});
</script>

<style scoped>
.job-body {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  column-gap: 8px;
  row-gap: 4px;
}

/* at least 200 px of text before the actions wrap under it */
.job-text {
  flex: 1 1 200px;
  min-width: 0;
}

.job-actions {
  flex: 0 0 auto;
  margin-left: auto;
}

.job-title {
  white-space: normal;
  overflow-wrap: anywhere;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.job-source,
.job-caption {
  overflow-wrap: anywhere;
}
</style>
