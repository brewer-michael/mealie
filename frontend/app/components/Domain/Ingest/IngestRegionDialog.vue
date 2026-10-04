<template>
  <BaseDialog
    v-model="dialog"
    :title="$t('recipe-ingest.review.reread-title')"
    :icon="mdiCropFree"
    :submit-text="$t('recipe-ingest.review.reread-send')"
    :submit-icon="mdiTextRecognition"
    :submit-disabled="!current || !targetValue"
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
      <div ref="frame" class="ingest-region-dialog__frame">
        <Cropper
          v-if="current"
          :key="current.viewUrl"
          ref="cropper"
          class="ingest-region-dialog__cropper"
          :src="current.viewUrl"
          :canvas="false"
          :check-orientation="false"
          :default-size="defaultSize"
          @ready="tooSmall = false"
          @change="tooSmall = false"
        />
      </div>
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
import { Cropper } from "vue-advanced-cropper";
import "vue-advanced-cropper/dist/style.css";
import {
  regionFromCropResult,
  type CropResultLike,
  type RereadTargetOption,
} from "~/composables/use-recipe-ingest-review";
import type { PageOut, RereadRequest } from "~/lib/api/types/recipe-ingest";

/**
 * "Re-read an area" (docs/ai/PHASE2.md §4.7, §6.5): the reviewer drags over part of an upright page and picks what
 * it's for; the selection goes to `POST …/reread` as fractions of that page. The pages are already upright and
 * EXIF-free, so the cropper neither reads orientation nor draws a canvas. Full screen on phones (BaseDialog).
 */
const props = withDefaults(defineProps<{
  pages?: PageOut[];
  /** What the re-read can be for, in reading order */
  targets?: RereadTargetOption[];
  /** The page shown first (its position in `pages`) */
  initialPage?: number;
  /** The target chosen first: the flag's or field's line the dialog was opened from */
  initialTarget?: string | null;
}>(), {
  pages: () => [],
  targets: () => [],
  initialPage: 0,
  initialTarget: null,
});

const emit = defineEmits<{
  (e: "submit", request: RereadRequest): void;
}>();

const dialog = defineModel<boolean>({ required: true });

const i18n = useI18n();

type CropperInstance = { getResult: () => CropResultLike; refresh: () => void };
const cropper = ref<CropperInstance | null>(null);
const frame = ref<HTMLElement | null>(null);
const selected = ref(props.initialPage);
const targetValue = ref<string | null>(props.initialTarget);
const tooSmall = ref(false);

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
      return i18n.t("recipe-ingest.review.add-note");
    default:
      return option.value;
  }
}

const targetItems = computed(() => props.targets.map(option => ({ value: option.value, title: targetTitle(option) })));

/** A wide band across the middle: most fields are one line of writing */
function defaultSize({ imageSize }: { imageSize: { width: number; height: number } }) {
  return { width: imageSize.width * 0.9, height: imageSize.height * 0.2 };
}

// The cropper measures its box when it mounts, which is mid-transition inside a dialog: measure again once the
// dialog has opened, and whenever the box changes size (rotating a phone, the dialog going full screen)
const DIALOG_TRANSITION_MS = 300;
let refreshTimer: ReturnType<typeof setTimeout> | null = null;

function refreshCropper() {
  cropper.value?.refresh();
}

watch(dialog, async (open) => {
  if (open) {
    selected.value = Math.min(props.initialPage, Math.max(0, props.pages.length - 1));
    targetValue.value = props.initialTarget ?? props.targets[0]?.value ?? null;
    tooSmall.value = false;
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
</style>
