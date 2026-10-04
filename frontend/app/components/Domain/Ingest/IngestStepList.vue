<template>
  <div class="ingest-step-list">
    <div
      v-for="(step, index) in model.steps"
      :id="fieldAnchorId('steps', step.id)"
      :key="step.id"
      class="ingest-step"
      :class="severityClass(step.id)"
    >
      <v-text-field
        :model-value="step.title ?? ''"
        variant="underlined"
        density="compact"
        hide-details
        class="ingest-step__title"
        :label="$t('recipe-ingest.review.section-title')"
        :readonly="readonly"
        @update:model-value="value => (step.title = value || null)"
      />
      <v-textarea
        v-model="step.text"
        variant="outlined"
        auto-grow
        rows="2"
        hide-details="auto"
        :label="$t('recipe.step-index', { step: index + 1 })"
        :readonly="readonly"
        :append-inner-icon="severityIcon(step.id, $globals.icons)"
      />
      <div v-if="!readonly" class="d-flex justify-end">
        <v-btn
          size="small"
          variant="text"
          color="error"
          :prepend-icon="$globals.icons.delete"
          @click="remove(index)"
        >
          {{ $t("general.delete") }}
        </v-btn>
      </div>
    </div>
    <v-btn
      v-if="!readonly"
      size="small"
      variant="text"
      color="primary"
      :prepend-icon="$globals.icons.create"
      @click="add"
    >
      {{ $t("recipe-ingest.review.add-step") }}
    </v-btn>
  </div>
</template>

<script setup lang="ts">
import { uuid4 } from "~/composables/use-utils";
import { fieldAnchorId, fieldSeverity, flagsForField, type ReviewDraft } from "~/composables/use-recipe-ingest-review";
import type { CardFlag } from "~/lib/api/types/recipe-ingest";

/**
 * The draft's steps as plain title and text fields (docs/ai/PHASE2.md §6.8): upstream's step editor needs a saved
 * recipe for its images. A step with an unresolved error or warning gets a coloured edge and an icon.
 */
const props = withDefaults(defineProps<{
  /** Unresolved errors and warnings (the page's open flags) */
  flags?: CardFlag[];
  readonly?: boolean;
}>(), {
  flags: () => [],
  readonly: false,
});

const model = defineModel<ReviewDraft>({ required: true });

function severity(stepId: string | undefined) {
  return fieldSeverity(flagsForField(props.flags, "steps", stepId ?? null));
}

function severityClass(stepId: string | undefined) {
  const level = severity(stepId);
  return level ? `ingest-step--${level}` : undefined;
}

function severityIcon(stepId: string | undefined, icons: Record<string, string>) {
  const level = severity(stepId);
  if (!level) {
    return undefined;
  }
  return level === "error" ? icons.alertCircle : icons.alert;
}

function add() {
  model.value.steps.push({ id: uuid4(), title: null, text: "" });
}

function remove(index: number) {
  model.value.steps.splice(index, 1);
}
</script>

<style scoped>
.ingest-step {
  border-left: 4px solid transparent;
  padding-left: 8px;
  margin-bottom: 8px;
}

.ingest-step--error {
  border-left-color: rgb(var(--v-theme-error));
}

.ingest-step--warning {
  border-left-color: rgb(var(--v-theme-warning));
}

.ingest-step--error :deep(.v-field__append-inner .v-icon) {
  color: rgb(var(--v-theme-error));
}

.ingest-step--warning :deep(.v-field__append-inner .v-icon) {
  color: rgb(var(--v-theme-warning));
}
</style>
