<template>
  <div class="ingest-review-bar px-3 py-2" :class="{ 'ingest-review-bar--fixed': fixed }">
    <!-- the page's notices, inside the bar so they never cover the card or its header (it grows to hold them) -->
    <div class="ingest-review-bar__notices" role="status" aria-live="polite">
      <div
        v-if="notice"
        :key="notice.id"
        class="ingest-review-bar__notice d-flex align-center ga-2 mb-2"
        :class="`ingest-review-bar__notice--${notice.kind}`"
      >
        <v-icon size="small" :color="notice.kind" :icon="$globals.icons[noticeIcon]" />
        <div class="flex-grow-1 text-body-medium ingest-review-bar__notice-body">
          <div class="ingest-review-bar__notice-text">
            {{ notice.text }}
          </div>
          <div v-if="notice.detail" class="text-body-small ingest-review-bar__notice-detail">
            {{ notice.detail }}
          </div>
        </div>
        <v-btn
          v-if="notice.action"
          class="ingest-review-bar__notice-action"
          size="small"
          variant="text"
          :color="notice.kind === 'error' ? 'error' : 'primary'"
          @click="emit('notice-action')"
        >
          {{ notice.action.label }}
        </v-btn>
        <v-btn
          class="ingest-review-bar__notice-close"
          icon
          size="x-small"
          variant="text"
          :aria-label="$t('general.close')"
          @click="emit('notice-dismiss')"
        >
          <v-icon :icon="$globals.icons.close" />
        </v-btn>
      </div>
    </div>

    <div v-if="actions" class="d-flex align-center ga-2">
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
        class="text-body-small ingest-review-bar__saved"
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
  </div>
</template>

<script setup lang="ts">
import type { ReviewNotice, SaveState } from "~/composables/use-recipe-ingest-review";

/**
 * The review page's bottom bar (docs/ai/PHASE2.md §6.2): Skip, the "Saved" indicator and Commit & next. While errors
 * remain the primary button reads "1 to fix" and asks the page to scroll to it instead of committing. Above them, the
 * page's notices ("Added Banana Mug Cake", "Re-read queued") as one dismissible strip with an optional button: the bar
 * grows to hold it, so it covers nothing (a toast covered the header or the line just above the bar). Without
 * `actions` (a card that isn't ready to review) the bar holds only the notice.
 */
const props = withDefaults(defineProps<{
  /** Unresolved errors: commit is blocked until each is fixed or kept */
  errorCount?: number;
  disabled?: boolean;
  committing?: boolean;
  saveState?: SaveState;
  /** Pinned to the bottom of the screen (phones), with the safe-area inset */
  fixed?: boolean;
  notice?: ReviewNotice | null;
  /** Skip, the save indicator and Commit & next */
  actions?: boolean;
}>(), {
  errorCount: 0,
  disabled: false,
  committing: false,
  saveState: "idle",
  fixed: false,
  notice: null,
  actions: true,
});

const emit = defineEmits<{
  (e: "skip" | "commit" | "fix" | "notice-action" | "notice-dismiss"): void;
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

/** The notice's icon, by its name in `$globals.icons` */
const noticeIcon = computed(() => {
  switch (props.notice?.kind) {
    case "success":
      return "check";
    case "warning":
      return "alert";
    case "error":
      return "alertCircle";
    default:
      return "informationOutline";
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

.ingest-review-bar__notice {
  min-height: 40px;
  padding: 4px 4px 4px 12px;
  border-radius: 4px;
  background: rgba(var(--v-theme-info), 0.12);
}

.ingest-review-bar__notice--success {
  background: rgba(var(--v-theme-success), 0.12);
}

.ingest-review-bar__notice--warning {
  background: rgba(var(--v-theme-warning), 0.14);
}

.ingest-review-bar__notice--error {
  background: rgba(var(--v-theme-error), 0.12);
}

.ingest-review-bar__notice-body {
  min-width: 0;
  overflow-wrap: anywhere;
}
</style>
