<template>
  <div class="ingest-transcription">
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
  </div>
</template>

<script setup lang="ts">
import { MARKERS, type TextSegment } from "~/composables/use-recipe-ingest-review";

/** "What the card says": the card's text as read, with `[illegible]` and `[blank]` picked out */
const props = defineProps<{
  text?: string | null;
}>();

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

.ingest-transcription__marker {
  background-color: rgba(var(--v-theme-warning), 0.35);
  border-radius: 2px;
  font-weight: 600;
}
</style>
