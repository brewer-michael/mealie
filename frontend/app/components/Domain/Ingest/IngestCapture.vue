<template>
  <div class="ingest-capture">
    <v-btn-toggle
      v-model="mode"
      class="capture-mode mb-4"
      color="primary"
      density="comfortable"
      variant="outlined"
      divided
      mandatory
    >
      <v-btn value="one-side" class="mode-one-side">
        {{ $t("recipe-ingest.capture.one-side") }}
      </v-btn>
      <v-btn value="front-and-back" class="mode-front-and-back">
        {{ $t("recipe-ingest.capture.front-and-back") }}
      </v-btn>
    </v-btn-toggle>

    <div v-if="pendingFront" class="pending-front d-flex align-center mb-3">
      <img
        :src="previewUrl(pendingFront)"
        class="capture-thumb"
        :alt="$t('recipe-ingest.capture.front')"
        decoding="async"
      >
      <span class="ml-3 text-body-2">{{ $t("recipe-ingest.capture.front") }}</span>
    </div>

    <div class="capture-actions d-flex flex-wrap align-center ga-2">
      <v-btn
        class="take-photo"
        color="primary"
        size="large"
        :prepend-icon="mdiCamera"
        @click="openCamera('shot')"
      >
        {{ cameraLabel }}
      </v-btn>
      <template v-if="pendingFront">
        <v-btn class="no-back" variant="tonal" @click="noBack">
          {{ $t("recipe-ingest.capture.no-back") }}
        </v-btn>
        <v-btn class="retake" variant="text" @click="openCamera('retake')">
          {{ $t("recipe-ingest.capture.retake") }}
        </v-btn>
      </template>
      <v-btn
        class="choose-photos"
        variant="tonal"
        :prepend-icon="mdiImageMultiple"
        @click="chooseInput?.click()"
      >
        {{ $t("recipe-ingest.capture.choose-photos") }}
      </v-btn>
      <v-spacer />
      <v-btn
        v-if="canFinish"
        class="done"
        color="success"
        variant="flat"
        :prepend-icon="mdiCheck"
        @click="done"
      >
        {{ $t("recipe-ingest.capture.done") }}
      </v-btn>
    </div>
    <p v-if="openBatchCardCount" class="cards-queued text-caption mt-2 mb-0">
      {{ $t("recipe-ingest.capture.cards-queued", openBatchCardCount) }}
    </p>

    <!-- One shot per tap: iOS ignores `multiple` with `capture` -->
    <input
      ref="cameraInput"
      class="camera-input d-none"
      type="file"
      accept="image/*"
      capture="environment"
      @change="onCamera"
    >
    <input
      ref="chooseInput"
      class="choose-input d-none"
      type="file"
      accept="image/*"
      multiple
      @change="onChoose"
    >

    <div v-if="drafts.length" class="drafts mt-4">
      <v-row dense>
        <v-col
          v-for="(card, index) in drafts"
          :key="card.key"
          cols="12"
          sm="6"
          md="4"
        >
          <v-card class="draft-card" variant="outlined">
            <v-card-title class="text-subtitle-2">
              {{ $t("recipe-ingest.capture.card-number", { number: index + 1 }) }}
            </v-card-title>
            <div class="d-flex ga-2 px-4">
              <figure
                v-for="(photo, side) in card.photos"
                :key="side"
                class="draft-photo ma-0"
              >
                <img
                  :src="previewUrl(photo)"
                  class="capture-thumb"
                  :alt="sideLabel(card.photos.length, side)"
                  loading="lazy"
                  decoding="async"
                >
                <figcaption v-if="card.photos.length > 1" class="text-caption text-center">
                  {{ sideLabel(card.photos.length, side) }}
                </figcaption>
              </figure>
            </div>
            <v-card-actions class="flex-wrap">
              <v-btn
                v-if="card.photos.length > 1"
                class="draft-swap"
                size="small"
                :prepend-icon="mdiSwapHorizontal"
                @click="swapDraft(index)"
              >
                {{ $t("recipe-ingest.capture.swap") }}
              </v-btn>
              <v-btn
                v-if="card.photos.length > 1"
                class="draft-split"
                size="small"
                :prepend-icon="mdiCallSplit"
                @click="splitDraft(index)"
              >
                {{ $t("recipe-ingest.capture.split") }}
              </v-btn>
              <v-btn
                v-if="canJoin(drafts, index, maxPagesPerCard)"
                class="draft-join"
                size="small"
                :prepend-icon="mdiCallMerge"
                @click="joinDraft(index, maxPagesPerCard)"
              >
                {{ $t("recipe-ingest.capture.join") }}
              </v-btn>
              <v-spacer />
              <v-btn
                class="draft-remove"
                size="small"
                color="error"
                variant="text"
                @click="removeDraft(index)"
              >
                {{ $t("recipe-ingest.capture.remove") }}
              </v-btn>
            </v-card-actions>
          </v-card>
        </v-col>
      </v-row>
      <v-btn
        class="upload-drafts mt-2"
        color="primary"
        :prepend-icon="$globals.icons.upload"
        @click="uploadDrafts"
      >
        {{ $t("general.upload") }}
      </v-btn>
    </div>

    <!-- Desktop: drop photos here -->
    <div
      ref="dropZone"
      class="drop-zone d-none d-md-flex flex-column align-center justify-center mt-4 pa-6"
      :class="{ 'drop-zone-over': isOverDropZone }"
      role="button"
      tabindex="0"
      @click="chooseInput?.click()"
      @keydown.enter="chooseInput?.click()"
    >
      <v-icon :icon="mdiImagePlus" size="large" class="mb-2" />
      <div>{{ $t("recipe-ingest.capture.drop-zone") }}</div>
      <div class="text-caption">
        {{ $t("recipe-ingest.capture.drop-zone-hint") }}
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { mdiCallMerge, mdiCallSplit, mdiCamera, mdiCheck, mdiImageMultiple, mdiImagePlus, mdiSwapHorizontal } from "@mdi/js";
import { useDropZone } from "@vueuse/core";
import { canJoin, DEFAULT_MAX_PAGES_PER_CARD, useRecipeIngestUploads } from "~/composables/use-recipe-ingest-uploads";

/**
 * Taking and choosing card photos (docs/ai/PHASE2.md §1.1). A card taken with the camera uploads as soon as it's
 * complete; chosen or dropped photos are paired into cards first, with Swap and Split/Join. Fork-owned.
 */
withDefaults(defineProps<{
  /** The server's limit, from the card settings */
  maxPagesPerCard?: number;
}>(), {
  maxPagesPerCard: DEFAULT_MAX_PAGES_PER_CARD,
});

const i18n = useI18n();
const {
  mode,
  drafts,
  pendingFront,
  openBatch,
  openBatchCardCount,
  previewUrl,
  takePhoto,
  retake,
  noBack,
  addPhotos,
  swapDraft,
  splitDraft,
  joinDraft,
  removeDraft,
  uploadDrafts,
  done,
} = useRecipeIngestUploads();

const cameraInput = ref<HTMLInputElement | null>(null);
const chooseInput = ref<HTMLInputElement | null>(null);
const dropZone = ref<HTMLElement | null>(null);
/** What the next camera photo is for */
const cameraPurpose = ref<"shot" | "retake">("shot");

const cameraLabel = computed(() => {
  if (pendingFront.value) {
    return i18n.t("recipe-ingest.capture.back-side");
  }
  return openBatchCardCount.value ? i18n.t("recipe-ingest.capture.next-card") : i18n.t("recipe-ingest.capture.take-photo");
});

const canFinish = computed(() => !!openBatch.value || !!pendingFront.value || drafts.value.length > 0);

function sideLabel(count: number, side: number): string {
  if (side === 0) {
    return i18n.t("recipe-ingest.capture.front");
  }
  return side === 1 && count === 2 ? i18n.t("recipe-ingest.capture.back") : String(side + 1);
}

function openCamera(purpose: "shot" | "retake") {
  cameraPurpose.value = purpose;
  cameraInput.value?.click();
}

/** The input's files; its value is reset so the same photo can be picked again */
function readFiles(event: Event): File[] {
  const input = event.target as HTMLInputElement;
  const files = Array.from(input.files ?? []);
  input.value = "";
  return files;
}

function onCamera(event: Event) {
  const [photo] = readFiles(event);
  if (!photo) {
    return;
  }
  if (cameraPurpose.value === "retake") {
    retake(photo);
  }
  else {
    takePhoto(photo);
  }
  cameraPurpose.value = "shot";
}

function onChoose(event: Event) {
  addPhotos(readFiles(event));
}

const { isOverDropZone } = useDropZone(dropZone, (files) => {
  if (files?.length) {
    addPhotos(files);
  }
});
</script>

<style scoped>
.capture-thumb {
  width: 72px;
  height: 72px;
  object-fit: cover;
  border-radius: 6px;
  display: block;
}

.drop-zone {
  border: 2px dashed rgba(var(--v-border-color), var(--v-border-opacity));
  border-radius: 12px;
  cursor: pointer;
  min-height: 140px;
}

.drop-zone-over {
  border-color: rgb(var(--v-theme-primary));
  background-color: rgba(var(--v-theme-primary), 0.08);
}
</style>
