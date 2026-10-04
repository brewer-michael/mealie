<template>
  <div class="ingest-capture">
    <v-btn-toggle
      v-model="mode"
      class="capture-mode mb-3 mb-sm-4"
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

    <!-- one row in every state from 375 px: the shutter, Choose and Done never move (narrower, Done wraps) -->
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
      <v-btn
        class="choose-photos"
        variant="tonal"
        :prepend-icon="mdiImageMultiple"
        @click="chooseInput?.click()"
      >
        <!-- phones: a short label, so the row fits 343 px -->
        <span class="d-none d-sm-inline">{{ $t("recipe-ingest.capture.choose-photos") }}</span>
        <span class="d-sm-none">{{ $t("recipe-ingest.capture.choose-photos-short") }}</span>
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
    <!-- below the buttons, so the Back side shutter stays where Take photo was (the top half of a phone screen) -->
    <div v-if="pendingFront" class="pending-front d-flex flex-wrap align-center ga-2 mt-3">
      <IngestCapturePhoto :photo="pendingFront" :label="$t('recipe-ingest.capture.front')" :show-name="false" />
      <span class="text-body-2 mr-2">{{ $t("recipe-ingest.capture.front") }}</span>
      <v-btn class="no-back" variant="tonal" @click="noBack">
        {{ $t("recipe-ingest.capture.no-back") }}
      </v-btn>
      <v-btn class="retake" variant="text" @click="openCamera('retake')">
        {{ $t("recipe-ingest.capture.retake") }}
      </v-btn>
    </div>
    <p v-if="openBatchCardCount" class="cards-queued text-caption mt-2 mb-0">
      {{ $t("recipe-ingest.capture.cards-queued", openBatchCardCount) }}
    </p>
    <!-- chosen or dropped files the server can't use; under the buttons, so they don't move -->
    <v-alert
      v-if="skipped"
      class="skipped-files mt-3"
      type="warning"
      variant="tonal"
      density="compact"
      closable
      @click:close="skipped = null"
    >
      <div v-if="skipped.unsupported.length" class="skipped-unsupported">
        {{ $t("recipe-ingest.capture.skipped-unsupported", { count: skipped.unsupported.length }, skipped.unsupported.length) }}
        <div class="skipped-names text-caption">
          {{ fileList(skipped.unsupported) }}
        </div>
      </div>
      <div
        v-if="skipped.tooManyPages.length"
        class="skipped-too-many-pages"
        :class="{ 'mt-2': skipped.unsupported.length }"
      >
        {{ $t("recipe-ingest.capture.skipped-too-many-pages", { count: skipped.tooManyPages.length, max: maxPagesPerCard }, skipped.tooManyPages.length) }}
        <div class="skipped-names text-caption">
          {{ fileList(skipped.tooManyPages) }}
        </div>
      </div>
    </v-alert>

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
      :accept="SCANNABLE_ACCEPT"
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
              <!-- a PDF or a multi-page TIFF: how many pages the card gets from it -->
              <span v-if="documentPages(card.photos)" class="draft-pages text-caption text-medium-emphasis">· {{ $t("recipe-ingest.capture.pages", documentPages(card.photos) ?? 0) }}</span>
            </v-card-title>
            <div class="d-flex ga-2 px-4">
              <figure
                v-for="(photo, side) in card.photos"
                :key="side"
                class="draft-photo ma-0"
              >
                <IngestCapturePhoto :photo="photo" :label="sideLabel(card.photos.length, side)" />
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
                v-if="canJoinDraft(index, maxPagesPerCard)"
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

    <!-- below everything, so it never moves the shutter; remembered in this browser -->
    <v-switch
      v-model="dataSaver"
      class="data-saver mt-2"
      color="primary"
      density="compact"
      :label="$t('recipe-ingest.capture.data-saver')"
      :hint="$t('recipe-ingest.capture.data-saver-hint')"
      persistent-hint
      inset
    />
    <v-alert
      v-if="storageFailed"
      class="storage-failed mt-3"
      type="warning"
      variant="tonal"
      density="compact"
      closable
      @click:close="storageFailed = false"
    >
      {{ $t("recipe-ingest.capture.storage-failed") }}
    </v-alert>
  </div>
</template>

<script setup lang="ts">
import { mdiCallMerge, mdiCallSplit, mdiCamera, mdiCheck, mdiImageMultiple, mdiImagePlus, mdiSwapHorizontal } from "@mdi/js";
import { useDropZone } from "@vueuse/core";
import IngestCapturePhoto from "./IngestCapturePhoto.vue";
import { SCANNABLE_ACCEPT } from "~/composables/use-recipe-ingest-files";
import { DEFAULT_MAX_PAGES_PER_CARD, useRecipeIngestUploads } from "~/composables/use-recipe-ingest-uploads";
import type { AddPhotosResult } from "~/composables/use-recipe-ingest-uploads";

/**
 * Taking and choosing card photos (docs/ai/PHASE2.md §1.1). A card taken with the camera uploads as soon as it's
 * complete; chosen or dropped photos and PDFs are paired into cards first, with Swap and Split/Join (a PDF is a card
 * of its own). Files the server can't read are left out, and the panel says which. The tray shows small thumbnails (a
 * placeholder where the browser can't show a photo), and Data saver sends smaller photos. Fork-owned.
 */
const props = withDefaults(defineProps<{
  /** The server's limit, from the card settings */
  maxPagesPerCard?: number;
}>(), {
  maxPagesPerCard: DEFAULT_MAX_PAGES_PER_CARD,
});

const i18n = useI18n();
const {
  mode,
  dataSaver,
  storageFailed,
  drafts,
  pendingFront,
  openBatch,
  openBatchCardCount,
  takePhoto,
  retake,
  noBack,
  addPhotos,
  pagesOf,
  canJoinDraft,
  swapDraft,
  splitDraft,
  joinDraft,
  removeDraft,
  uploadDrafts,
  done,
  keepBatchOpen,
} = useRecipeIngestUploads();

// a pause in the stack (a phone call, sorting cards) doesn't end the batch while this page is shown
let releaseBatch: (() => void) | null = null;
onMounted(() => {
  releaseBatch = keepBatchOpen();
});
onBeforeUnmount(() => releaseBatch?.());

const cameraInput = ref<HTMLInputElement | null>(null);
const chooseInput = ref<HTMLInputElement | null>(null);
const dropZone = ref<HTMLElement | null>(null);
/** What the next camera photo is for */
const cameraPurpose = ref<"shot" | "retake">("shot");
/** What the last chosen or dropped files left out; until closed or the next files */
const skipped = ref<AddPhotosResult | null>(null);
/** Names listed under the skipped notice */
const LISTED_NAMES = 5;

const cameraLabel = computed(() => {
  if (pendingFront.value) {
    return i18n.t("recipe-ingest.capture.back-side");
  }
  return openBatch.value ? i18n.t("recipe-ingest.capture.next-card") : i18n.t("recipe-ingest.capture.take-photo");
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

/** Chosen or dropped files go to the tray, once the server's formats and page limit have been checked */
async function addFiles(files: File[]) {
  if (!files.length) {
    return;
  }
  const result = await addPhotos(files, props.maxPagesPerCard);
  skipped.value = result.unsupported.length || result.tooManyPages.length ? result : null;
}

function fileList(names: string[]): string {
  const listed = names.filter(Boolean).slice(0, LISTED_NAMES).join(", ");
  const more = names.length - Math.min(names.length, LISTED_NAMES);
  return more ? `${listed} ${i18n.t("recipe-ingest.capture.skipped-more", { count: more })}` : listed;
}

/** The pages a draft card gets, when one of its files is a document of several pages */
function documentPages(photos: Blob[]): number | null {
  const counts = photos.map(photo => pagesOf(photo));
  if (!counts.some(count => count !== null && count > 1) || counts.includes(null)) {
    return null;
  }
  return counts.reduce<number>((total, count) => total + (count ?? 0), 0);
}

function onCamera(event: Event) {
  const [photo] = readFiles(event);
  if (!photo) {
    return;
  }
  if (!photo.type.startsWith("image/") || photo.type === "image/tiff") {
    // a computer's file picker can give anything: a PDF (a card of its own), or a file to leave out
    cameraPurpose.value = "shot";
    void addFiles([photo]);
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
  void addFiles(readFiles(event));
}

// No `dataTypes`: vueuse refuses a whole drop when one file doesn't match (ten photos and a Thumbs.db), can't tell a
// .txt file from dragged text, and says nothing. Every dropped file goes to `addFiles`, which reads what each is and
// says what it left out; the browser never opens a dropped file itself.
const { isOverDropZone } = useDropZone(dropZone, {
  onDrop: (files) => {
    void addFiles(files ?? []);
  },
  preventDefaultForUnhandled: true,
});
</script>

<style scoped>
/* phones: the shutter, Choose and Done fit one row of 343 px in every state, one height, one top */
@media (max-width: 599.98px) {
  .capture-actions .v-btn {
    height: 44px;
    padding-inline: 12px;
  }
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
