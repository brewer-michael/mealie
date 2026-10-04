<template>
  <div class="ingest-recipe-fields">
    <div
      v-for="field in lineFields"
      :id="fieldAnchorId(field.key)"
      :key="field.key"
      class="ingest-field"
      :class="severityClass(field.key)"
    >
      <v-textarea
        v-if="field.key === 'description'"
        v-model="model.description"
        variant="underlined"
        auto-grow
        rows="1"
        :label="$t(field.label)"
        :readonly="readonly"
        :append-inner-icon="severityIcon(field.key, $globals.icons)"
        hide-details="auto"
      />
      <v-text-field
        v-else
        :model-value="model[field.key] ?? ''"
        variant="underlined"
        :label="$t(field.label)"
        :readonly="readonly"
        :append-inner-icon="severityIcon(field.key, $globals.icons)"
        hide-details="auto"
        @update:model-value="value => setText(field.key, value)"
      />
    </div>

    <div class="d-flex flex-wrap gc-4">
      <div
        v-for="field in detailFields"
        :id="fieldAnchorId(field.key)"
        :key="field.key"
        class="ingest-field ingest-field--detail"
        :class="severityClass(field.key)"
      >
        <v-text-field
          v-if="field.key === 'recipeServings'"
          :model-value="servingsText"
          variant="underlined"
          inputmode="decimal"
          :label="$t(field.label)"
          :readonly="readonly"
          :append-inner-icon="severityIcon(field.key, $globals.icons)"
          :error-messages="servingsError"
          hide-details="auto"
          @update:model-value="setServings"
        />
        <v-text-field
          v-else
          :model-value="model[field.key] ?? ''"
          variant="underlined"
          :label="$t(field.label)"
          :readonly="readonly"
          :append-inner-icon="severityIcon(field.key, $globals.icons)"
          hide-details="auto"
          @update:model-value="value => setText(field.key, value)"
        />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import {
  fieldAnchorId,
  fieldSeverity,
  flagsForField,
  parseQuantity,
  type ReviewDraft,
} from "~/composables/use-recipe-ingest-review";
import type { CardFlag } from "~/lib/api/types/recipe-ingest";

type LineField = "name" | "attribution" | "description";
type DetailField = "recipeYield" | "recipeServings" | "prepTime" | "performTime" | "totalTime";

/**
 * The recipe's name, attribution, description, yield, servings and times (docs/ai/PHASE2.md §6.8). A field with an
 * unresolved error or warning gets a coloured edge and an icon; the reason is in its "Needs a look" item.
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

const i18n = useI18n();

const lineFields: { key: LineField; label: string }[] = [
  { key: "name", label: "recipe-ingest.review.name" },
  { key: "attribution", label: "recipe-ingest.review.attribution" },
  { key: "description", label: "recipe-ingest.review.description" },
];

const detailFields: { key: DetailField; label: string }[] = [
  { key: "recipeYield", label: "recipe-ingest.review.yield" },
  { key: "recipeServings", label: "recipe-ingest.review.servings" },
  { key: "prepTime", label: "recipe-ingest.review.prep-time" },
  { key: "performTime", label: "recipe-ingest.review.cook-time" },
  { key: "totalTime", label: "recipe-ingest.review.total-time" },
];

function severity(field: string) {
  return fieldSeverity(flagsForField(props.flags, field));
}

function severityClass(field: string) {
  const level = severity(field);
  return level ? `ingest-field--${level}` : undefined;
}

function severityIcon(field: string, icons: Record<string, string>) {
  const level = severity(field);
  if (!level) {
    return undefined;
  }
  return level === "error" ? icons.alertCircle : icons.alert;
}

function setText(field: LineField | DetailField, value: string | null) {
  if (field === "description" || field === "recipeServings") {
    // their own inputs set them
    return;
  }
  if (field === "name") {
    model.value.name = value ?? "";
    return;
  }
  // optional fields go back to null when emptied, so an untouched field and a cleared one save alike
  model.value[field] = value || null;
}

// typed as text ("4", "1 1/2") and stored as a number; text that isn't one yet ("4-") stays in the box
const servingsText = ref(model.value.recipeServings === null || model.value.recipeServings === undefined ? "" : String(model.value.recipeServings));

watch(() => model.value.recipeServings, (value) => {
  if ((value ?? null) !== parseQuantity(servingsText.value)) {
    servingsText.value = value === null || value === undefined ? "" : String(value);
  }
});

function setServings(value: string | null) {
  servingsText.value = value ?? "";
  model.value.recipeServings = parseQuantity(value);
}

/** Servings are one number: text that isn't one ("4-6") is saved as no servings, so the field says so */
const servingsError = computed(() =>
  servingsText.value.trim() && parseQuantity(servingsText.value) === null
    ? i18n.t("recipe-ingest.review.servings-not-a-number")
    : undefined,
);
</script>

<style scoped>
.ingest-field {
  border-left: 4px solid transparent;
  padding-left: 8px;
}

.ingest-field--detail {
  flex: 1 1 140px;
  min-width: 120px;
}

.ingest-field--error {
  border-left-color: rgb(var(--v-theme-error));
}

.ingest-field--warning {
  border-left-color: rgb(var(--v-theme-warning));
}

.ingest-field--error :deep(.v-field__append-inner .v-icon) {
  color: rgb(var(--v-theme-error));
}

.ingest-field--warning :deep(.v-field__append-inner .v-icon) {
  color: rgb(var(--v-theme-warning));
}
</style>
