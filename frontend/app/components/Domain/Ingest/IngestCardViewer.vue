<template>
  <div
    class="ingest-card-viewer"
    :class="mode === 'strip' ? ['ingest-card-strip', { 'ingest-card-strip--collapsed': collapsed }] : 'ingest-card-panel'"
    @touchstart.passive="onTouchStart"
    @touchend.passive="onTouchEnd"
  >
    <!-- phones: a sticky strip; swipe down to fold it to a bar, tap for full screen -->
    <template v-if="mode === 'strip'">
      <button
        v-if="collapsed"
        type="button"
        class="ingest-card-strip__bar d-flex align-center ga-2 px-3"
        :aria-label="$t('recipe-ingest.review.show-card')"
        @click="collapsed = false"
      >
        <img
          v-if="current"
          :src="current.thumbUrl"
          alt=""
          class="ingest-card-strip__thumb"
        >
        <span class="text-body-medium">{{ pageLabel(selected) }}</span>
        <v-spacer />
        <v-icon :icon="$globals.icons.chevronDown" />
      </button>
      <template v-else>
        <button
          v-if="current"
          type="button"
          class="ingest-card-strip__image-button"
          :aria-label="$t('recipe-ingest.review.full-screen')"
          @click="lightbox = true"
        >
          <img
            :src="current.viewUrl"
            :alt="pageLabel(selected)"
            class="ingest-card-viewer__image"
            draggable="false"
          >
        </button>
        <div class="ingest-card-strip__controls d-flex align-center ga-1">
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
          <v-spacer />
          <v-btn
            icon
            size="small"
            variant="tonal"
            :aria-label="$t('recipe-ingest.review.hide-card')"
            @click="collapsed = true"
          >
            <v-icon :icon="mdiChevronUp" />
          </v-btn>
        </div>
      </template>
    </template>

    <!-- desktop: a sticky panel with the page tools -->
    <template v-else>
      <div class="ingest-card-panel__toolbar d-flex align-center flex-wrap ga-1 pa-2">
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
        <v-spacer />
        <v-btn
          size="small"
          variant="text"
          :prepend-icon="$globals.icons.rotateRight"
          :disabled="readonly || !current"
          :loading="rotating"
          @click="current && emit('rotate', current.index)"
        >
          {{ $t("recipe-ingest.review.rotate") }}
        </v-btn>
        <v-btn
          size="small"
          variant="text"
          :prepend-icon="mdiCropFree"
          :disabled="!canReread || !current"
          @click="emit('reread')"
        >
          {{ $t("recipe-ingest.review.reread-title") }}
        </v-btn>
        <v-btn
          size="small"
          :variant="showTranscription ? 'tonal' : 'text'"
          :prepend-icon="mdiTextRecognition"
          :aria-pressed="showTranscription"
          @click="showTranscription = !showTranscription"
        >
          {{ $t("recipe-ingest.review.what-the-card-says") }}
        </v-btn>
        <v-btn
          icon
          size="small"
          variant="text"
          :aria-label="$t('recipe-ingest.review.full-screen')"
          :disabled="!current"
          @click="lightbox = true"
        >
          <v-icon :icon="mdiFullscreen" />
        </v-btn>
      </div>
      <div class="ingest-card-panel__content">
        <!-- the page's own "What the card says" (correctable there), else the text as read -->
        <div v-if="showTranscription" class="pa-3">
          <slot name="transcription">
            <IngestTranscription :text="transcription" />
          </slot>
        </div>
        <button
          v-else-if="current"
          type="button"
          class="ingest-card-panel__image-button"
          :aria-label="$t('recipe-ingest.review.full-screen')"
          @click="lightbox = true"
        >
          <img
            :src="current.viewUrl"
            :alt="pageLabel(selected)"
            class="ingest-card-viewer__image"
            draggable="false"
          >
        </button>
      </div>
    </template>

    <RecipeImageLightbox
      v-model="lightbox"
      :image-url="current?.pageUrl"
      :image-alt="pageLabel(selected)"
    />
  </div>
</template>

<script setup lang="ts">
import { mdiChevronUp, mdiCropFree, mdiFullscreen, mdiTextRecognition } from "@mdi/js";
import IngestTranscription from "./IngestTranscription.vue";
import RecipeImageLightbox from "~/components/Domain/Recipe/RecipeImageLightbox.vue";
import type { PageOut } from "~/lib/api/types/recipe-ingest";

/**
 * The card's pages (docs/ai/PHASE2.md §6.2, §6.3). On phones (`strip`) a sticky strip about a third of the screen
 * high: tap for full screen, swipe down to fold it to a 56 px bar, Front/Back. On desktop (`panel`) a sticky column
 * with Front/Back, Rotate, Re-read an area and the transcription toggle. Images load through the authenticated page
 * routes (`<img>` sends the session cookie); their URLs change when a page is rotated. Rotate follows `readonly`
 * (a failed card can be turned before it's read again); Re-read an area follows `canReread` (only a ready card).
 * The `transcription` slot replaces the panel's text (the review page's, with Rebuild from this text).
 */
const props = withDefaults(defineProps<{
  pages?: PageOut[];
  mode?: "strip" | "panel";
  transcription?: string | null;
  readonly?: boolean;
  /** Whether Re-read an area works now: the card is ready and its editor isn't locked */
  canReread?: boolean;
  rotating?: boolean;
}>(), {
  pages: () => [],
  mode: "panel",
  transcription: null,
  readonly: false,
  canReread: false,
  rotating: false,
});

const emit = defineEmits<{
  /** rotate the page with this `PageOut.index` a quarter turn clockwise */
  (e: "rotate", pageIndex: number): void;
  (e: "reread"): void;
}>();

/** The shown page's position in `pages` (0 is the front) */
const selected = defineModel<number>("page", { default: 0 });
const showTranscription = defineModel<boolean>("transcriptionOpen", { default: false });

const i18n = useI18n();
const lightbox = ref(false);
const collapsed = ref(false);

const current = computed(() => props.pages[Math.min(selected.value, props.pages.length - 1)] ?? null);

watch(() => props.pages.length, (length) => {
  if (selected.value >= length) {
    selected.value = Math.max(0, length - 1);
  }
});

function pageLabel(index: number) {
  if (index === 0) {
    return i18n.t("recipe-ingest.review.front");
  }
  return index === 1 ? i18n.t("recipe-ingest.review.back") : String(index + 1);
}

// a downward swipe folds the strip, an upward one opens it again
const SWIPE_DISTANCE = 40;
let touchStartY: number | null = null;

function onTouchStart(event: TouchEvent) {
  touchStartY = props.mode === "strip" ? event.touches[0]?.clientY ?? null : null;
}

function onTouchEnd(event: TouchEvent) {
  const endY = event.changedTouches[0]?.clientY;
  if (touchStartY === null || endY === undefined) {
    return;
  }
  const distance = endY - touchStartY;
  touchStartY = null;
  if (distance > SWIPE_DISTANCE) {
    collapsed.value = true;
  }
  else if (distance < -SWIPE_DISTANCE) {
    collapsed.value = false;
  }
}
</script>

<style scoped>
.ingest-card-viewer__image {
  display: block;
  max-width: 100%;
  max-height: 100%;
  margin: 0 auto;
  object-fit: contain;
  user-select: none;
}

.ingest-card-strip {
  position: sticky;
  top: 48px;
  z-index: 4;
  height: 32dvh;
  min-height: 160px;
  display: flex;
  flex-direction: column;
  background: rgb(var(--v-theme-background));
  border-bottom: thin solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.ingest-card-strip--collapsed {
  height: 56px;
  min-height: 56px;
}

.ingest-card-strip__image-button {
  flex: 1 1 auto;
  min-height: 0;
  width: 100%;
  cursor: zoom-in;
}

.ingest-card-strip__controls {
  padding: 4px 8px;
}

.ingest-card-strip__bar {
  height: 56px;
  width: 100%;
}

.ingest-card-strip__thumb {
  height: 40px;
  width: 40px;
  object-fit: cover;
  border-radius: 4px;
}

.ingest-card-panel {
  position: sticky;
  top: 48px;
  height: calc(100dvh - 48px);
  display: flex;
  flex-direction: column;
}

.ingest-card-panel__content {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
}

.ingest-card-panel__image-button {
  display: block;
  width: 100%;
  height: 100%;
  cursor: zoom-in;
}
</style>
