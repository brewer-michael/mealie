<template>
  <div v-if="visibleCards.length || sealingCount" class="ingest-upload-queue">
    <p v-if="sealingCount" class="sealing d-flex align-center text-body-2 mb-2">
      <v-progress-circular
        indeterminate
        size="16"
        width="2"
        class="mr-2"
      />
      {{ $t("recipe-ingest.capture.sealing") }}
    </p>
    <v-list v-if="visibleCards.length" class="upload-cards py-0" density="compact">
      <v-list-item
        v-for="card in visibleCards"
        :key="card.key"
        class="upload-card px-0"
        :data-status="card.status"
      >
        <template #prepend>
          <img
            v-if="card.photos[0]"
            :src="previewUrl(card.photos[0])"
            class="upload-thumb mr-3"
            alt=""
            loading="lazy"
            decoding="async"
          >
        </template>
        <v-list-item-title class="upload-title">
          {{ $t("recipe-ingest.capture.card-number", { number: card.position + 1 }) }}
        </v-list-item-title>
        <div v-if="card.duplicateOf" class="already-scanned mt-1">
          <v-chip
            size="small"
            color="info"
            variant="tonal"
            :to="jobLink(card.duplicateOf)"
            :title="$t('recipe-ingest.capture.open-earlier')"
          >
            {{ $t("recipe-ingest.capture.already-scanned") }}
          </v-chip>
        </div>
        <div v-else class="upload-status text-body-2">
          {{ statusText(card) }}
        </div>
        <div v-if="detailText(card)" class="upload-detail text-caption">
          {{ detailText(card) }}
        </div>
        <ul v-if="rejections(card).length" class="upload-rejected text-caption pl-4 mb-0">
          <li v-for="rejected in rejections(card)" :key="rejected.index">
            {{ $t("recipe-ingest.capture.rejected", { reason: rejectReasonText(rejected.reason) }) }}
          </li>
        </ul>
        <v-progress-linear
          v-if="card.status === 'uploading' || card.status === 're-encoding'"
          class="mt-1"
          color="primary"
          :model-value="card.progress * 100"
          :indeterminate="card.status === 're-encoding' || card.progress === 0"
        />
        <template #append>
          <div class="d-flex align-center ga-1">
            <v-btn
              v-if="card.status === 'failed' && card.retryable"
              class="upload-retry"
              size="small"
              variant="tonal"
              @click="retry(card.key)"
            >
              {{ $t("recipe-ingest.capture.retry") }}
            </v-btn>
            <v-btn
              v-if="isRemovable(card)"
              class="upload-remove"
              icon
              size="small"
              variant="text"
              :aria-label="$t('recipe-ingest.capture.remove')"
              :title="$t('recipe-ingest.capture.remove')"
              @click="remove(card.key)"
            >
              <v-icon :icon="$globals.icons.close" />
            </v-btn>
          </div>
        </template>
      </v-list-item>
    </v-list>
  </div>
</template>

<script setup lang="ts">
import { INGEST_API_ERROR_CODES, INGEST_ERROR_CODES, useRecipeIngestText } from "~/composables/use-recipe-ingest";
import { hasNote, isBatchFinished, useRecipeIngestUploads } from "~/composables/use-recipe-ingest-uploads";
import type { UploadCard } from "~/composables/use-recipe-ingest-uploads";
import type { IngestRejected } from "~/lib/api/types/recipe-ingest";

/**
 * The cards on their way to the server (docs/ai/PHASE2.md §1.1): progress, retries, an "Already scanned" chip linking
 * to the earlier card, and photos the server didn't use. Uploaded cards move to the job list. Fork-owned.
 */
const props = defineProps<{
  groupSlug: string;
}>();

const i18n = useI18n();
const { ingestErrorText, rejectReasonText } = useRecipeIngestText();
const { cards, batches, previewUrl, retry, remove } = useRecipeIngestUploads();

const KNOWN_CODES = new Set<string>([...INGEST_ERROR_CODES, ...INGEST_API_ERROR_CODES]);

/** Everything but cards uploaded with nothing more to say */
const visibleCards = computed(() => cards.value.filter(card => card.status !== "done" || hasNote(card)));

/** Batches whose Done was tapped and that aren't sealed yet */
const sealingCount = computed(() => {
  const state = { batches: batches.value, cards: cards.value, openBatchKey: null };
  return batches.value.filter(batch => batch.sealing && !isBatchFinished(state, batch)).length;
});

function jobLink(jobId: string) {
  return `/g/${props.groupSlug}/recipes/cards/${jobId}`;
}

function statusText(card: UploadCard): string {
  switch (card.status) {
    case "waiting":
      return i18n.t("recipe-ingest.capture.waiting");
    case "uploading":
      return card.progress > 0
        ? i18n.t("recipe-ingest.capture.upload-progress", { percent: Math.round(card.progress * 100) })
        : i18n.t("recipe-ingest.capture.uploading");
    case "retrying":
      return i18n.t("recipe-ingest.capture.retrying");
    case "re-encoding":
      return i18n.t("recipe-ingest.capture.re-encoding");
    case "done":
      return i18n.t("recipe-ingest.capture.uploaded");
    default:
      return card.rejected.length && !card.retryable ? "" : i18n.t("recipe-ingest.capture.upload-failed");
  }
}

/** Why the last attempt failed, when the server said */
function detailText(card: UploadCard): string | null {
  if ((card.status !== "failed" && card.status !== "retrying") || !card.error || card.rejected.length) {
    return null;
  }
  return KNOWN_CODES.has(card.error) ? ingestErrorText(card.error) : null;
}

/** Photos the server didn't use, except the duplicate the chip already shows */
function rejections(card: UploadCard): IngestRejected[] {
  return card.rejected.filter(rejected => rejected.reason !== "duplicate");
}

function isRemovable(card: UploadCard): boolean {
  return card.status === "waiting" || card.status === "failed" || hasNote(card);
}
</script>

<style scoped>
.upload-thumb {
  width: 48px;
  height: 48px;
  object-fit: cover;
  border-radius: 6px;
  display: block;
}
</style>
