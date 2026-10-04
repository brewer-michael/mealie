<template>
  <div
    class="ingest-review-bar d-flex align-center ga-2 px-3 py-2"
    :class="{ 'ingest-review-bar--fixed': fixed }"
  >
    <v-btn
      variant="text"
      class="ingest-review-bar__skip"
      :disabled="committing"
      @click="emit('skip')"
    >
      {{ $t("recipe-ingest.review.skip") }}
    </v-btn>
    <span
      v-if="saveLabel"
      class="text-caption ingest-review-bar__saved"
      :class="{ 'text-error': saveState === 'error', 'text-medium-emphasis': saveState !== 'error' }"
      aria-live="polite"
    >
      {{ saveLabel }}
    </span>
    <v-spacer />
    <v-btn
      v-if="errorCount > 0"
      class="ingest-review-bar__primary"
      color="error"
      variant="flat"
      :prepend-icon="$globals.icons.alertCircle"
      @click="emit('fix')"
    >
      {{ $t("recipe-ingest.review.to-fix", { count: errorCount }) }}
    </v-btn>
    <v-btn
      v-else
      class="ingest-review-bar__primary"
      color="success"
      variant="flat"
      :loading="committing"
      :disabled="disabled || committing"
      :prepend-icon="$globals.icons.check"
      @click="emit('commit')"
    >
      {{ $t("recipe-ingest.review.commit-next") }}
    </v-btn>
  </div>
</template>

<script setup lang="ts">
import type { SaveState } from "~/composables/use-recipe-ingest-review";

/**
 * The review page's bottom bar (docs/ai/PHASE2.md §6.2): Skip, the "Saved" indicator and Commit & next. While errors
 * remain the primary button reads "1 to fix" and asks the page to scroll to it instead of committing.
 */
const props = withDefaults(defineProps<{
  /** Unresolved errors: commit is blocked until each is fixed or kept */
  errorCount?: number;
  disabled?: boolean;
  committing?: boolean;
  saveState?: SaveState;
  /** Pinned to the bottom of the screen (phones), with the safe-area inset */
  fixed?: boolean;
}>(), {
  errorCount: 0,
  disabled: false,
  committing: false,
  saveState: "idle",
  fixed: false,
});

const emit = defineEmits<{
  (e: "skip" | "commit" | "fix"): void;
}>();

const i18n = useI18n();

const saveLabel = computed(() => {
  switch (props.saveState) {
    case "saving":
      return i18n.t("recipe-ingest.review.saving");
    case "saved":
      return i18n.t("recipe-ingest.review.saved");
    case "error":
      return i18n.t("recipe-ingest.review.save-failed");
    default:
      return "";
  }
});
</script>

<style scoped>
.ingest-review-bar {
  position: sticky;
  bottom: 0;
  z-index: 3;
  background: rgb(var(--v-theme-surface));
  border-top: thin solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.ingest-review-bar--fixed {
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  padding-bottom: calc(8px + env(safe-area-inset-bottom)) !important;
}

.ingest-review-bar__saved {
  white-space: nowrap;
}
</style>
