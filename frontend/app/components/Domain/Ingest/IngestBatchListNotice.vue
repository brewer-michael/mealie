<template>
  <div
    class="commit-notice d-flex align-center flex-wrap ga-2"
    :class="`commit-notice--${notice.kind}`"
    :data-type="notice.kind"
    role="status"
    aria-live="polite"
  >
    <v-icon size="small" :color="notice.kind" :icon="$globals.icons[icon]" />
    <div class="commit-notice-body flex-grow-1 text-body-medium">
      <div class="commit-notice-text">
        {{ notice.text }}
      </div>
      <div v-if="notice.detail" class="commit-notice-detail text-body-small">
        {{ notice.detail }}
      </div>
      <ul v-if="notice.items.length" class="commit-notice-items text-body-small ps-4">
        <li v-for="(item, index) in notice.items" :key="index" class="commit-notice-item">
          {{ item }}
        </li>
      </ul>
    </div>
    <!-- on a phone, Undo or Open card go under the text rather than squeezing it -->
    <div class="commit-notice-actions d-flex align-center ga-1 ms-auto">
      <v-btn
        v-if="notice.undoJobId"
        class="commit-notice-undo"
        size="small"
        variant="text"
        :loading="busy"
        @click="emit('undo', notice.undoJobId)"
      >
        {{ $t("recipe-ingest.review.undo") }}
      </v-btn>
      <v-btn
        v-if="notice.cardPath"
        class="commit-notice-open"
        size="small"
        variant="text"
        :to="notice.cardPath"
      >
        {{ $t("recipe-ingest.review.open-card") }}
      </v-btn>
      <v-btn
        class="commit-notice-close"
        icon
        size="x-small"
        variant="text"
        :aria-label="$t('general.close')"
        @click="emit('dismiss')"
      >
        <v-icon :icon="$globals.icons.close" />
      </v-btn>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { RecipeIngestQueueNotice } from "~/composables/use-recipe-ingest";

/**
 * A dismissible line in the cards list (what the review said about a batch's last card, what "Add N clean cards"
 * did): it takes its own place in the page, so it never covers the page title the way a toast does. "Added …" offers
 * Undo; a line about one card can link to it. Fork-owned.
 */
const props = defineProps<{
  notice: RecipeIngestQueueNotice;
  /** Its Undo is being sent */
  busy?: boolean;
}>();

const emit = defineEmits<{
  (e: "dismiss"): void;
  (e: "undo", jobId: string): void;
}>();

/** The icon, by its name in `$globals.icons` */
const icon = computed(() => {
  switch (props.notice.kind) {
    case "error":
      return "alertCircle";
    case "warning":
      return "alert";
    case "info":
      return "informationOutline";
    default:
      return "check";
  }
});
</script>

<style scoped>
.commit-notice {
  min-height: 40px;
  padding: 4px 4px 4px 12px;
  border-radius: 4px;
  background: rgba(var(--v-theme-success), 0.12);
}

.commit-notice--warning {
  background: rgba(var(--v-theme-warning), 0.14);
}

.commit-notice--info {
  background: rgba(var(--v-theme-info), 0.12);
}

.commit-notice--error {
  background: rgba(var(--v-theme-error), 0.12);
}

.commit-notice-body {
  flex: 1 1 14rem;
  min-width: 0;
  overflow-wrap: anywhere;
}
</style>
