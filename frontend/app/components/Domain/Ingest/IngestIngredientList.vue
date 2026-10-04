<template>
  <div class="ingest-ingredient-list">
    <template v-for="(ingredient, index) in model.ingredients" :key="ingredient.referenceId">
      <IngestIngredientRow
        :model-value="ingredient"
        :flags="flagsForField(flags, 'ingredients', ingredient.referenceId ?? null)"
        :infos="flagsForField(infoFlags, 'ingredients', ingredient.referenceId ?? null)"
        :expanded="expanded === ingredient.referenceId"
        :readonly="readonly"
        :can-create-foods="canCreateFoods"
        :food-options="foodOptions"
        :unit-options="unitOptions"
        @update:model-value="value => (model.ingredients[index] = value)"
        @toggle="toggle(ingredient.referenceId)"
        @remove="remove(index)"
      />
    </template>
    <v-btn
      v-if="!readonly"
      class="mt-1"
      size="small"
      variant="text"
      color="primary"
      :prepend-icon="$globals.icons.create"
      @click="add"
    >
      {{ $t("recipe-ingest.review.add-ingredient") }}
    </v-btn>
  </div>
</template>

<script setup lang="ts">
import IngestIngredientRow from "./IngestIngredientRow.vue";
import { useFoodStore } from "~/composables/store/use-food-store";
import { useUnitStore } from "~/composables/store/use-unit-store";
import { uuid4 } from "~/composables/use-utils";
import { flagsForField, type IngestNamedOption, type ReviewDraft } from "~/composables/use-recipe-ingest-review";
import type { CardFlag } from "~/lib/api/types/recipe-ingest";

/**
 * The draft's ingredient lines (docs/ai/PHASE2.md §6.2): compact card-like rows, one open at a time, with the group's
 * foods and units to link them to. Nothing here creates a food or unit: commit does, per §5.
 */
withDefaults(defineProps<{
  /** Unresolved errors and warnings (the page's open flags) */
  flags?: CardFlag[];
  /** Info flags, shown quietly on their lines */
  infoFlags?: CardFlag[];
  readonly?: boolean;
  canCreateFoods?: boolean;
}>(), {
  flags: () => [],
  infoFlags: () => [],
  readonly: false,
  canCreateFoods: false,
});

const model = defineModel<ReviewDraft>({ required: true });
/** The `referenceId` of the open row */
const expanded = defineModel<string | null>("expanded", { default: null });

const { store: foods } = useFoodStore();
const { store: units } = useUnitStore();

const foodOptions = computed<IngestNamedOption[]>(() =>
  foods.value.map(food => ({ id: food.id, name: food.name, pluralName: food.pluralName })),
);
const unitOptions = computed<IngestNamedOption[]>(() =>
  units.value.map(unit => ({ id: unit.id, name: unit.name, pluralName: unit.pluralName, abbreviation: unit.abbreviation })),
);

function toggle(referenceId: string | undefined) {
  expanded.value = expanded.value === referenceId ? null : referenceId ?? null;
}

function add() {
  const referenceId = uuid4();
  model.value.ingredients.push({
    referenceId,
    title: null,
    originalText: "",
    quantity: null,
    unit: null,
    food: null,
    note: "",
    display: "",
  });
  expanded.value = referenceId;
}

function remove(index: number) {
  const [removed] = model.value.ingredients.splice(index, 1);
  if (removed?.referenceId === expanded.value) {
    expanded.value = null;
  }
}
</script>
