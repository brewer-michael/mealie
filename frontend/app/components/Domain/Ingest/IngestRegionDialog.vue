<template>
  <BaseDialog
    v-model="dialog"
    :title="$t('recipe-ingest.review.reread-title')"
    :icon="mdiCropFree"
    :submit-text="$t('recipe-ingest.review.reread-send')"
    :submit-icon="mdiTextRecognition"
    :submit-disabled="!current || !targetValue || locating"
    can-submit
    keep-open
    max-width="900"
    @submit="submit"
  >
    <v-card-text class="ingest-region-dialog">
      <p class="text-body-2 mb-4">
        {{ $t("recipe-ingest.review.reread-hint") }}
      </p>
      <div class="d-flex flex-wrap align-center ga-2 pt-1 mb-3">
        <v-btn-toggle
          v-if="pages.length > 1"
          v-model="selected"
          mandatory
          density="compact"
          variant="outlined"
          divided
          @update:model-value="pickPage"
        >
          <v-btn
            v-for="(page, index) in pages"
            :key="page.index"
            :value="index"
            size="small"
          >
            {{ pageLabel(index) }}
          </v-btn>
        </v-btn-toggle>
        <v-select
          v-model="targetValue"
          class="ingest-region-dialog__target"
          :items="targetItems"
          item-title="title"
          item-value="value"
          density="compact"
          variant="outlined"
          hide-details
          :label="$t('recipe-ingest.review.reread-for')"
        />
      </div>
      <!-- arrow keys on the focused selection move it, Shift and arrow keys resize it -->
      <div ref="frame" class="ingest-region-dialog__frame" @keydown="onKeydown">
        <!-- the server is saying where the line is on the card: the selection starts there once it has -->
        <div v-if="locating" class="ingest-region-dialog__locating d-flex align-center justify-center">
          <v-progress-circular indeterminate color="primary" />
        </div>
        <Cropper
          v-else-if="current"
          :key="`${current.viewUrl}#${opening}`"
          ref="cropper"
          class="ingest-region-dialog__cropper"
          :src="current.viewUrl"
          :canvas="false"
          :check-orientation="false"
          :default-size="defaultSize"
          :default-position="defaultPosition"
          :stencil-component="IngestRegionStencil"
          :stencil-props="stencilProps"
          @ready="tooSmall = false"
          @change="onChange"
        />
      </div>
      <p :id="keysHintId" class="d-sr-only">
        {{ $t("recipe-ingest.review.region-keys") }}
      </p>
      <p class="d-sr-only ingest-region-dialog__position" aria-live="polite">
        {{ positionText }}
      </p>
      <p
        v-if="tooSmall"
        class="text-error text-body-2 mt-2 mb-0 ingest-region-dialog__error"
        role="alert"
      >
        {{ $t("recipe-ingest.review.region-too-small") }}
      </p>
    </v-card-text>
  </BaseDialog>
</template>

<script setup lang="ts">
import { mdiCropFree, mdiTextRecognition } from "@mdi/js";
import { useResizeObserver } from "@vueuse/core";
import { useId } from "vue";
import { Cropper } from "vue-advanced-cropper";
import "vue-advanced-cropper/dist/style.css";
import IngestRegionStencil from "./IngestRegionStencil.vue";
import {
  nudgeRegion,
  regionFromCropResult,
  regionFromHint,
  type CropResultLike,
  type PageRegion,
  type RegionCoordinates,
  type RereadTargetOption,
} from "~/composables/use-recipe-ingest-review";
import type { PageOut, RegionHintOut, RereadRequest } from "~/lib/api/types/recipe-ingest";

/**
 * "Re-read an area" (docs/ai/PHASE2.md §4.7, §6.5): the reviewer drags over part of an upright page and picks what
 * it's for; the selection goes to `POST …/reread` as fractions of that page. The pages are already upright and
 * EXIF-free, so the cropper neither reads orientation nor draws a canvas. Full screen on phones (BaseDialog).
 * The selection (`IngestRegionStencil`) follows a finger from the first pixel, and takes the keyboard focus: arrow
 * keys move it, Shift and arrow keys resize it, and a screen reader hears where it is.
 *
 * Each opening starts the selection where the text probably is: `initialRegion` (the server's region hint for the
 * line it was opened from, on that line's page, with a margin either side: `regionFromHint`; `locating` while it's on
 * its way), else where the last area read on that page was while this card is open, else a band across the middle.
 */
const props = withDefaults(defineProps<{
  pages?: PageOut[];
  /** What the re-read can be for, in reading order */
  targets?: RereadTargetOption[];
  /** The page shown first (its position in `pages`) */
  initialPage?: number;
  /** The target chosen first: the flag's or field's line the dialog was opened from */
  initialTarget?: string | null;
  /** Where that line probably is on the card (`GET …/region-hint`): its page and the selection to start with */
  initialRegion?: RegionHintOut | null;
  /** The region hint is still on its way: the selection waits for it */
  locating?: boolean;
}>(), {
  pages: () => [],
  targets: () => [],
  initialPage: 0,
  initialTarget: null,
  initialRegion: null,
  locating: false,
});

const emit = defineEmits<{
  (e: "submit", request: RereadRequest): void;
}>();

const dialog = defineModel<boolean>({ required: true });

const i18n = useI18n();

type CropperTransform = (params: { coordinates: RegionCoordinates; imageSize: { width: number; height: number } }) => RegionCoordinates;
type CropperInstance = {
  getResult: () => CropResultLike;
  refresh: () => void;
  setCoordinates: (transform: CropperTransform, options?: { transitions?: boolean }) => void;
};
const cropper = ref<CropperInstance | null>(null);
const frame = ref<HTMLElement | null>(null);
const selected = ref(props.initialPage);
const targetValue = ref<string | null>(props.initialTarget);
const tooSmall = ref(false);
/** Where the selection is, said after each arrow key (an `aria-live` line) */
const positionText = ref("");
const keysHintId = `ingest-region-keys-${useId()}`;
const stencilProps = computed(() => ({ label: i18n.t("recipe-ingest.review.region-selection"), describedBy: keysHintId }));

const current = computed(() => props.pages[Math.min(selected.value, props.pages.length - 1)] ?? null);

function pageLabel(index: number) {
  if (index === 0) {
    return i18n.t("recipe-ingest.review.front");
  }
  return index === 1 ? i18n.t("recipe-ingest.review.back") : String(index + 1);
}

function targetTitle(option: RereadTargetOption): string {
  switch (option.kind) {
    case "name":
      return i18n.t("recipe-ingest.review.name");
    case "attribution":
      return i18n.t("recipe-ingest.review.attribution");
    case "description":
      return i18n.t("recipe-ingest.review.description");
    case "recipeYield":
      return i18n.t("recipe-ingest.review.yield");
    case "recipeServings":
      return i18n.t("recipe-ingest.review.servings");
    case "prepTime":
      return i18n.t("recipe-ingest.review.prep-time");
    case "performTime":
      return i18n.t("recipe-ingest.review.cook-time");
    case "totalTime":
      return i18n.t("recipe-ingest.review.total-time");
    case "ingredient":
      return option.text || i18n.t("recipe-ingest.review.ingredients");
    case "step":
      return i18n.t("recipe.step-index", { step: option.text });
    case "new-ingredient":
      return i18n.t("recipe-ingest.review.add-ingredient");
    case "new-step":
      return i18n.t("recipe-ingest.review.add-step");
    case "note":
      return option.text || i18n.t("recipe-ingest.review.notes");
    case "new-note":
      return i18n.t("recipe-ingest.review.add-note");
    default:
      return option.value;
  }
}

const targetItems = computed(() => props.targets.map(option => ({ value: option.value, title: targetTitle(option) })));

type ImageSize = { width: number; height: number };

/** The last area read on each page while this card is open, by the page's image (a turned page has a new one) */
const lastRegions = new Map<string, PageRegion>();
/** Counts the openings: each one mounts the cropper afresh, so its selection starts where `startRegion` says */
const opening = ref(0);

/**
 * Where the selection starts on the shown page, as fractions of it: the hint for its line, else the last area read
 * there. Read as the cropper mounts.
 */
function startRegion(): PageRegion | null {
  const page = current.value;
  if (!page) {
    return null;
  }
  const hint = props.initialRegion;
  if (hint && hint.page === page.index) {
    return regionFromHint(hint);
  }
  return lastRegions.get(page.viewUrl) ?? null;
}

/** The start region's size; else a wide band across the middle: most fields are one line of writing */
function defaultSize({ imageSize }: { imageSize: ImageSize }) {
  const start = startRegion();
  if (start) {
    return { width: start.width * imageSize.width, height: start.height * imageSize.height };
  }
  return { width: imageSize.width * 0.9, height: imageSize.height * 0.2 };
}

/** The start region's place; else the band is centred */
function defaultPosition({ coordinates, imageSize }: { coordinates: RegionCoordinates; imageSize: ImageSize }) {
  const start = startRegion();
  if (start) {
    return { left: start.x * imageSize.width, top: start.y * imageSize.height };
  }
  return { left: (imageSize.width - coordinates.width) / 2, top: (imageSize.height - coordinates.height) / 2 };
}

/** The position in `pages` of the page with this `PageOut.index`, if the card has it */
function pagePosition(index: number | null | undefined): number | null {
  const position = props.pages.findIndex(page => page.index === index);
  return position < 0 ? null : position;
}

// The cropper measures its box when it mounts, which is mid-transition inside a dialog: measure again once the
// dialog has opened, and whenever the box changes size (rotating a phone, the dialog going full screen)
const DIALOG_TRANSITION_MS = 300;
let refreshTimer: ReturnType<typeof setTimeout> | null = null;

function refreshCropper() {
  cropper.value?.refresh();
}

/** Whether the reviewer picked a page since the dialog opened: a hint arriving later doesn't turn it */
let pagePicked = false;

function pickPage() {
  pagePicked = true;
}

// the hint names the page its line is on, which the dialog shows (unless the reviewer already picked one)
watch(() => [props.initialRegion, props.locating] as const, ([hint, locating]) => {
  const position = pagePosition(hint?.page);
  if (dialog.value && !locating && !pagePicked && position !== null) {
    selected.value = position;
  }
});

watch(dialog, async (open) => {
  if (open) {
    opening.value += 1;
    pagePicked = false;
    selected.value = pagePosition(props.locating ? null : props.initialRegion?.page)
      ?? Math.min(props.initialPage, Math.max(0, props.pages.length - 1));
    // opened from a flag or a line, it's for that line; otherwise the reviewer picks what it's for
    targetValue.value = props.initialTarget ?? null;
    tooSmall.value = false;
    positionText.value = "";
    await nextTick();
    if (refreshTimer) {
      clearTimeout(refreshTimer);
    }
    refreshTimer = setTimeout(refreshCropper, DIALOG_TRANSITION_MS);
  }
}, { immediate: true });

useResizeObserver(frame, refreshCropper);

onBeforeUnmount(() => {
  if (refreshTimer) {
    clearTimeout(refreshTimer);
  }
});

/** Whether the next change comes from an arrow key, and so is said aloud */
let keyboardChange = false;
const ARROW_KEYS = ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"];

function onKeydown(event: KeyboardEvent) {
  if (!ARROW_KEYS.includes(event.key) || event.altKey || event.ctrlKey || event.metaKey) {
    return;
  }
  if (!(event.target as HTMLElement | null)?.closest?.(".ingest-region-stencil")) {
    return;
  }
  // the dialog doesn't scroll, whether or not the selection can go further
  event.preventDefault();
  keyboardChange = true;
  const resize = event.shiftKey;
  // without transitions: while one runs the cropper ignores new coordinates, which would drop held-down keys
  cropper.value?.setCoordinates(
    ({ coordinates, imageSize }) => nudgeRegion(coordinates, imageSize, event.key, resize) ?? coordinates,
    { transitions: false },
  );
}

function onChange(result: CropResultLike) {
  tooSmall.value = false;
  if (!keyboardChange) {
    return;
  }
  keyboardChange = false;
  const region = regionFromCropResult(result, 0);
  if (region) {
    const percent = (value: number) => Math.round(value * 100);
    positionText.value = i18n.t("recipe-ingest.review.region-position", {
      left: percent(region.x),
      top: percent(region.y),
      width: percent(region.width),
      height: percent(region.height),
    });
  }
}

function submit() {
  const page = current.value;
  const option = props.targets.find(item => item.value === targetValue.value);
  if (!page || !option) {
    return;
  }
  const region = regionFromCropResult(cropper.value?.getResult());
  if (!region) {
    tooSmall.value = true;
    return;
  }
  lastRegions.set(page.viewUrl, region);
  emit("submit", { page: page.index, ...region, target: { ...option.target } });
  dialog.value = false;
}
</script>

<style scoped>
.ingest-region-dialog__target {
  flex: 1 1 220px;
  min-width: 200px;
}

.ingest-region-dialog__frame {
  width: 100%;
}

.ingest-region-dialog__cropper {
  max-height: 65dvh;
  background: #ddd;
}

.ingest-region-dialog__locating {
  height: 40dvh;
  min-height: 160px;
}
</style>
