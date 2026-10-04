<template>
  <div class="ingest-transcription">
    <!-- "Rebuild from this text": the reviewer corrects what was read, and the recipe is built again from it -->
    <template v-if="editing">
      <p class="text-body-medium text-medium-emphasis mb-2 ingest-transcription__hint">
        {{ $t("recipe-ingest.review.rebuild-hint") }}
      </p>
      <v-textarea
        v-model="edited"
        class="ingest-transcription__editor"
        variant="outlined"
        auto-grow
        rows="8"
        :max-rows="24"
        :error-messages="tooLong ? $t('recipe-ingest.review.text-too-long', { max: MAX_TRANSCRIPTION }) : undefined"
        :hide-details="!tooLong"
        :aria-label="$t('recipe-ingest.review.what-the-card-says')"
        :disabled="rebuilding"
      />
      <div class="d-flex flex-wrap ga-2 mt-2">
        <v-btn
          class="ingest-transcription__rebuild"
          color="primary"
          variant="flat"
          :prepend-icon="mdiCogRefreshOutline"
          :loading="rebuilding"
          :disabled="!canRebuild || !edited.trim() || tooLong"
          @click="emit('rebuild', edited)"
        >
          {{ $t("recipe-ingest.review.rebuild") }}
        </v-btn>
        <v-btn
          class="ingest-transcription__cancel"
          variant="text"
          :disabled="rebuilding"
          @click="editing = false"
        >
          {{ $t("general.cancel") }}
        </v-btn>
      </div>
    </template>
    <template v-else>
      <div v-if="canRebuild" class="d-flex justify-end mb-1">
        <v-btn
          class="ingest-transcription__edit"
          size="small"
          variant="text"
          :prepend-icon="$globals.icons.edit"
          @click="editing = true"
        >
          {{ $t("recipe-ingest.review.edit") }}
        </v-btn>
      </div>
      <pre
        v-if="text && text.trim()"
        class="ingest-transcription__text"
      ><span
        v-for="(segment, index) in segments"
        :key="index"
        :class="{ 'ingest-transcription__marker': segment.mark }"
      >{{ segment.text }}</span></pre>
      <p v-else class="text-medium-emphasis ingest-transcription__empty">
        {{ $t("recipe-ingest.review.no-transcription") }}
      </p>
    </template>
  </div>
</template>

<script setup lang="ts">
import { mdiCogRefreshOutline } from "@mdi/js";
import { MARKERS, MAX_TRANSCRIPTION, type TextSegment } from "~/composables/use-recipe-ingest-review";

/**
 * "What the card says": the card's text as read, with `[illegible]` and `[blank]` picked out. On a card being
 * reviewed (`canRebuild`) Edit turns it into a text box, and "Rebuild from this text" builds the recipe again from
 * the corrected text (docs/ai/PHASE2.md §3.1): the page leaves editing (`editing`) once the rebuild is on its way.
 */
const props = withDefaults(defineProps<{
  text?: string | null;
  /** Whether the text can be corrected and the recipe rebuilt from it now: a ready card nothing is reading */
  canRebuild?: boolean;
  /** The rebuild is being sent */
  rebuilding?: boolean;
}>(), {
  text: null,
  canRebuild: false,
  rebuilding: false,
});

const emit = defineEmits<{
  (e: "rebuild", text: string): void;
}>();

const editing = defineModel<boolean>("editing", { default: false });

/** The text being corrected, from the card's text as it was when Edit was pressed */
const edited = ref("");

watch(editing, (open) => {
  if (open) {
    edited.value = props.text ?? "";
  }
}, { immediate: true });

const tooLong = computed(() => edited.value.length > MAX_TRANSCRIPTION);

const MARKER_PATTERN = new RegExp(`(${Object.values(MARKERS).map(marker => marker.replace(/[[\]]/g, "\\$&")).join("|")})`);

const segments = computed<TextSegment[]>(() =>
  (props.text ?? "")
    .split(MARKER_PATTERN)
    .filter(Boolean)
    .map(part => ({ text: part, mark: (Object.values(MARKERS) as string[]).includes(part) })),
);
</script>

<style scoped>
.ingest-transcription__text {
  font-family: monospace;
  font-size: 0.9rem;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  margin: 0;
}

.ingest-transcription__editor :deep(textarea) {
  font-family: monospace;
  font-size: 0.9rem;
}

.ingest-transcription__marker {
  background-color: rgba(var(--v-theme-warning), 0.35);
  border-radius: 2px;
  font-weight: 600;
}
</style>
