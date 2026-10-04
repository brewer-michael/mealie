<template>
  <div class="ingest-privacy-chip">
    <v-chip
      class="privacy-chip"
      :class="`privacy-${kind}`"
      :color="kind === 'local' ? 'success' : kind === 'blocked' ? 'warning' : undefined"
      :prepend-icon="icon"
      variant="tonal"
      :aria-expanded="open"
      @click="open = !open"
    >
      {{ text }}
    </v-chip>
    <v-expand-transition>
      <v-card
        v-if="open"
        class="privacy-panel mt-2"
        variant="outlined"
        max-width="560"
      >
        <v-card-title class="text-subtitle-1">
          {{ $t("recipe-ingest.privacy.title") }}
        </v-card-title>
        <v-card-text>
          <p v-if="settings?.localOnly" class="group-local">
            {{ $t("recipe-ingest.privacy.group-local") }}
          </p>
          <v-switch
            v-else-if="settings?.localOnlyAvailable || localOnly"
            v-model="localOnly"
            class="keep-local"
            color="primary"
            :label="$t('recipe-ingest.privacy.keep-local')"
            :hint="$t('recipe-ingest.privacy.keep-local-hint')"
            persistent-hint
            inset
          />
          <p v-else class="local-unavailable">
            {{ $t("recipe-ingest.privacy.local-unavailable") }}
          </p>
          <!-- remembered on, but nothing on the network can read cards now -->
          <p v-if="kind === 'blocked' && !settings?.localOnly" class="keep-local-unavailable text-caption text-warning mt-4 mb-0">
            {{ $t("recipe-ingest.privacy.keep-local-unavailable") }}
          </p>
          <!-- spaced from the switch's hint: the cards that had already left keep the setting they went with -->
          <v-alert
            v-if="(alreadySent > 0 || finishedBatch) && !settings?.localOnly"
            class="already-sent mt-4"
            type="info"
            variant="tonal"
            density="compact"
          >
            <p v-if="alreadySent > 0" class="mb-0">
              {{ $t("recipe-ingest.privacy.already-sent", alreadySent) }}
            </p>
            <p v-if="finishedBatch" class="finished-batch mb-0">
              {{ $t("recipe-ingest.privacy.finished-batch") }}
            </p>
          </v-alert>
        </v-card-text>
      </v-card>
    </v-expand-transition>
  </div>
</template>

<script setup lang="ts">
import { mdiAlertCircleOutline, mdiCloudOutline, mdiLan, mdiLock } from "@mdi/js";
import type { RecipeIngestionSettingsOut } from "~/lib/api/types/recipe-ingest";

/**
 * Where the photos go (docs/ai/PHASE2.md §1.1, §10), from the card settings' `reader`. Tapping it offers keeping this
 * user's cards on this server when local providers can read them (remembered on this device). Kept on this server with
 * nothing local to read them, it's a warning. Fork-owned.
 */
const props = withDefaults(defineProps<{
  settings: RecipeIngestionSettingsOut | null;
  /** Cards that had already left when the switch last changed: the server keeps the setting they were sent with */
  alreadySent?: number;
  /** The switch's last change finished the open batch: the next photo starts a new one */
  finishedBatch?: boolean;
}>(), {
  alreadySent: 0,
  finishedBatch: false,
});

/** The batch asks to stay on this server (a batch can opt in when the group doesn't, never out) */
const localOnly = defineModel<boolean>("localOnly", { default: false });

const i18n = useI18n();
const open = ref(false);

/**
 * `local` only when a policy keeps every AI call of the card on this server (the group's setting or the batch's).
 * A local first reader alone doesn't: the card could fall back to a cloud provider, or be structured by one.
 * `blocked` when such a policy applies but no AI provider on the network can read cards: every card would fail.
 */
const kind = computed<"local" | "blocked" | "local-reader" | "ocr" | "cloud" | "none">(() => {
  const reader = props.settings?.reader;
  if (props.settings?.localOnly || localOnly.value) {
    return props.settings && !props.settings.localOnlyAvailable ? "blocked" : "local";
  }
  if (!reader) {
    return "none";
  }
  if (reader.local) {
    return "local-reader";
  }
  return reader.viaOcr ? "ocr" : "cloud";
});

const text = computed(() => {
  const name = props.settings?.reader?.name ?? "";
  switch (kind.value) {
    case "local":
      return i18n.t("recipe-ingest.privacy.local");
    case "blocked":
      return i18n.t("recipe-ingest.privacy.local-blocked");
    case "local-reader":
      return i18n.t("recipe-ingest.privacy.local-reader", { name });
    case "ocr":
      return i18n.t("recipe-ingest.privacy.ocr-then-cloud", { name });
    case "cloud":
      return i18n.t("recipe-ingest.privacy.cloud", { name });
    default:
      return i18n.t("recipe-ingest.privacy.no-reader");
  }
});

const icon = computed(() => {
  switch (kind.value) {
    case "local":
      return mdiLock;
    case "local-reader":
      return mdiLan;
    case "blocked":
    case "none":
      return mdiAlertCircleOutline;
    default:
      return mdiCloudOutline;
  }
});
</script>
