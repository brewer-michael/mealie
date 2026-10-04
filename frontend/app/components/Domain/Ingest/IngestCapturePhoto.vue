<template>
  <div
    class="ingest-capture-photo"
    :style="{ width: `${size}px`, height: `${size}px` }"
    :data-preview="state"
  >
    <img
      v-if="url"
      :src="url"
      class="photo-image"
      :alt="altText"
      decoding="async"
      @error="markPreviewBroken(photo)"
    >
    <div
      v-else
      class="photo-placeholder d-flex flex-column align-center justify-center"
      role="img"
      :aria-label="altText"
      :title="name || undefined"
    >
      <v-icon :icon="icon" :size="size >= 64 ? 28 : 22" />
      <span v-if="showName && state === 'unavailable' && name" class="photo-name">{{ name }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { mdiCardTextOutline, mdiFilePdfBox, mdiImageOutline } from "@mdi/js";
import { photoName, useRecipeIngestUploads } from "~/composables/use-recipe-ingest-uploads";

/**
 * A photo in the capture tray or the upload queue (docs/ai/PHASE2.md §1.1): its small thumbnail, made once, or a card
 * placeholder with the file name where the browser can't show the photo (HEIC outside Safari; a PDF, with a PDF
 * icon). Fork-owned.
 */
const props = withDefaults(defineProps<{
  photo: Blob;
  /** Its side ("Front"), or what it shows */
  label?: string;
  /** Square, in pixels */
  size?: number;
  /** The file name under the placeholder icon (there's room from 64 px) */
  showName?: boolean;
}>(), {
  label: "",
  size: 72,
  showName: true,
});

const { previewUrl, previewState, markPreviewBroken, isPdf } = useRecipeIngestUploads();

const url = computed(() => previewUrl(props.photo));
const state = computed(() => previewState(props.photo));
const name = computed(() => photoName(props.photo));
const altText = computed(() => [props.label, name.value].filter(Boolean).join(": "));
const icon = computed(() => {
  if (state.value === "pending") {
    return mdiImageOutline;
  }
  return isPdf(props.photo) ? mdiFilePdfBox : mdiCardTextOutline;
});
</script>

<style scoped>
.ingest-capture-photo {
  border-radius: 6px;
  overflow: hidden;
  flex: 0 0 auto;
  background-color: rgba(var(--v-theme-on-surface), 0.08);
}

.photo-image {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.photo-placeholder {
  width: 100%;
  height: 100%;
  padding: 2px;
  color: rgba(var(--v-theme-on-surface), 0.6);
}

.photo-name {
  font-size: 10px;
  line-height: 12px;
  max-width: 100%;
  margin-top: 2px;
  text-align: center;
  overflow-wrap: anywhere;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>
