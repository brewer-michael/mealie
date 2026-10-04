<template>
  <v-alert
    class="ingest-proposal"
    :class="{ 'ingest-proposal--compact': compact }"
    type="info"
    variant="tonal"
    density="compact"
    :icon="false"
  >
    <div v-if="label" class="text-caption text-medium-emphasis">
      {{ label }}
    </div>
    <div class="ingest-proposal__text">
      <template v-if="proposal.kind === 'full'">
        {{ proposal.origin === "rebuild" ? $t("recipe-ingest.review.proposal-rebuild") : $t("recipe-ingest.review.proposal-full") }}
      </template>
      <template v-else-if="readable">
        {{ $t("recipe-ingest.review.proposal-region", { text: proposal.text ?? "" }) }}
      </template>
      <template v-else>
        {{ $t("recipe-ingest.review.proposal-unreadable") }}
      </template>
    </div>
    <div v-if="proposal.viaOcr" class="text-caption text-medium-emphasis">
      {{ $t("recipe-ingest.review.proposal-via-ocr") }}
    </div>
    <div class="ingest-proposal__actions d-flex flex-wrap ga-1 mt-1">
      <template v-if="proposal.kind === 'full'">
        <v-btn
          size="small"
          color="primary"
          variant="flat"
          :disabled="readonly || !proposal.draft"
          @click="emit('use', 'replace')"
        >
          {{ $t("recipe-ingest.review.use-new-reading") }}
        </v-btn>
        <v-btn
          size="small"
          variant="text"
          :disabled="readonly"
          @click="emit('dismiss')"
        >
          {{ $t("recipe-ingest.review.keep-mine") }}
        </v-btn>
      </template>
      <template v-else>
        <template v-if="readable">
          <v-btn
            v-if="compact"
            size="small"
            color="primary"
            variant="flat"
            :disabled="readonly"
            @click="emit('use', canReplace ? 'replace' : 'append')"
          >
            {{ $t("recipe-ingest.review.use") }}
          </v-btn>
          <template v-else>
            <v-btn
              v-if="canReplace"
              size="small"
              color="primary"
              variant="flat"
              :disabled="readonly"
              @click="emit('use', 'replace')"
            >
              {{ $t("recipe-ingest.review.replace") }}
            </v-btn>
            <v-btn
              size="small"
              color="primary"
              :variant="canReplace ? 'text' : 'flat'"
              :disabled="readonly"
              @click="emit('use', 'append')"
            >
              {{ $t("recipe-ingest.review.add-to-end") }}
            </v-btn>
          </template>
        </template>
        <v-btn
          size="small"
          variant="text"
          :disabled="readonly"
          @click="emit('dismiss')"
        >
          {{ $t("recipe-ingest.review.dismiss") }}
        </v-btn>
      </template>
    </div>
  </v-alert>
</template>

<script setup lang="ts">
import { normalizeField } from "~/composables/use-recipe-ingest-review";
import type { CardProposal } from "~/lib/api/types/recipe-ingest";

/**
 * A re-read's result (docs/ai/PHASE2.md §6.6): a region re-read offers Replace / Add to the end / Dismiss (or, inside
 * its "Needs a look" item, Use / Dismiss); a whole-card re-read of an edited card offers Use the new reading / Keep mine,
 * as does a recipe rebuilt from the reviewer's text ("Rebuilt from your text").
 */
const props = withDefaults(defineProps<{
  proposal: CardProposal;
  /** Inside a "Needs a look" item: one Use button */
  compact?: boolean;
  /** What the reading is for ("Step: 2"), shown above it */
  label?: string | null;
  readonly?: boolean;
}>(), {
  compact: false,
  label: null,
  readonly: false,
});

const emit = defineEmits<{
  (e: "use", mode: "replace" | "append"): void;
  (e: "dismiss"): void;
}>();

const readable = computed(() => props.proposal.readable !== false && !!props.proposal.text?.trim());

/** A reading for a new ingredient, step or note can only be added */
const canReplace = computed(() => {
  const target = props.proposal.target;
  if (!target) {
    return false;
  }
  const field = normalizeField(target.field);
  return !(["ingredients", "steps", "notes"].includes(field) && !target.ref);
});
</script>

<style scoped>
.ingest-proposal__text {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
