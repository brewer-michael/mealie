<template>
  <div v-if="waiting > 0 || rejections.length" class="ingest-inbox-status">
    <!-- photos left in the folder: why they wait, or that they're being added -->
    <v-alert
      v-if="waiting > 0"
      class="inbox-waiting mb-2"
      :type="waitingReason ? 'warning' : 'info'"
      density="compact"
      variant="tonal"
    >
      {{ waitingText }}
    </v-alert>
    <div v-if="rejections.length" class="inbox-rejections mb-2">
      <div class="text-subtitle-2">
        {{ $t("recipe-ingest.inbox.rejections-title") }}
      </div>
      <ul class="inbox-rejection-list text-body-2 ps-4">
        <li v-for="(item, index) in rejections" :key="`${index}-${item.name}`" class="inbox-rejection">
          <span class="inbox-rejection-name font-weight-medium">{{ item.name }}</span>:
          <span class="inbox-rejection-reason">{{ reasonText(item) }}</span>
          <span v-if="whenText(item)" class="inbox-rejection-time text-caption text-medium-emphasis">
            · {{ whenText(item) }}
          </span>
        </li>
      </ul>
      <p v-if="folder && movedToFailed" class="inbox-failed-folder text-caption text-medium-emphasis mb-0">
        {{ $t("recipe-ingest.inbox.failed-folder", { folder }) }}
      </p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { serverDate, useRecipeIngestText } from "~/composables/use-recipe-ingest";
import type { IngestInboxRejection, RecipeIngestionSettingsOut } from "~/lib/api/types/recipe-ingest";

/**
 * The household's inbox folder as the server last saw it (docs/ai/PHASE2.md §1.3), on the cards page and the settings
 * card: how many photos wait in it and why (the group can't read cards, keeps them local with nothing local to read
 * them, is at its quota, or nothing on the server reads cards), and the photos it refused lately (moved to `failed/`
 * with a note) or may not move (`no_permission`), each with the reason and when. Nothing when there's nothing to say.
 * Fork-owned.
 */
const props = defineProps<{
  settings: RecipeIngestionSettingsOut | null | undefined;
}>();

/** The server stops counting waiting photos here (`inbox.STATUS_MAX_WAITING`): the count is "at least" */
const MAX_WAITING_COUNTED = 1000;

const i18n = useI18n();
const { dateText, rejectReasonText } = useRecipeIngestText();

const inbox = computed(() => (props.settings?.enabled !== false && props.settings?.inbox?.enabled ? props.settings.inbox : null));
const folder = computed(() => inbox.value?.folder ?? null);
const waiting = computed(() => inbox.value?.waiting ?? 0);
const rejections = computed<IngestInboxRejection[]>(() => inbox.value?.rejections ?? []);
/** Some were refused, so they're in `failed/` (a `no_permission` one stays where it is) */
const movedToFailed = computed(() => rejections.value.some(item => item.reason !== "no_permission"));

/**
 * Why the photos wait: the server's reason, else that nothing reads cards (no reader means no inbox scan either);
 * none while they're simply being added
 */
const waitingReason = computed(() => {
  if (!waiting.value) {
    return null;
  }
  return inbox.value?.waitingReason ?? (props.settings?.readerRunning === false ? "no-reader" : null);
});

const waitingText = computed(() => {
  const count = waiting.value >= MAX_WAITING_COUNTED ? `${MAX_WAITING_COUNTED}+` : String(waiting.value);
  if (!waitingReason.value) {
    return i18n.t("recipe-ingest.inbox.adding", { count }, waiting.value);
  }
  const reason = i18n.t(`recipe-ingest.inbox.waiting-reason.${waitingReason.value}`);
  return i18n.t("recipe-ingest.inbox.waiting", { count, reason }, waiting.value);
});

/** The refusal's reason, with the limit it went over (a JPEG may have more pixels); a note without a code says so */
function reasonText(item: IngestInboxRejection): string {
  if (!item.reason) {
    return i18n.t("recipe-ingest.inbox.unknown-reason");
  }
  return rejectReasonText(item.reason, { limits: props.settings?.limits, jpeg: /\.jpe?g$/i.test(item.name) });
}

function whenText(item: IngestInboxRejection): string | null {
  const at = serverDate(item.at);
  return at ? dateText(at, true) : null;
}
</script>

<style scoped>
.inbox-rejection {
  overflow-wrap: anywhere;
}
</style>
