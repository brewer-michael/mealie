<template>
  <div class="ingest-ingredient-list">
    <!-- lines keep their ids when moved, so their flags and re-reads follow them -->
    <VueDraggable
      v-model="model.ingredients"
      handle=".ingest-ingredient__handle"
      :disabled="readonly || !draggable"
      :animation="200"
    >
      <IngestIngredientRow
        v-for="(ingredient, index) in model.ingredients"
        :key="ingredient.referenceId"
        :model-value="ingredient"
        :flags="flagsForField(flags, 'ingredients', ingredient.referenceId ?? null)"
        :infos="flagsForField(infoFlags, 'ingredients', ingredient.referenceId ?? null)"
        :expanded="expanded === ingredient.referenceId"
        :readonly="readonly"
        :can-create-foods="canCreateFoods"
        :food-options="foodOptions"
        :unit-options="unitOptions"
        :can-move-up="index > 0"
        :can-move-down="index < model.ingredients.length - 1"
        :draggable="draggable"
        :can-reread="canReread"
        :can-parse="canParse"
        :parsing="parsingRefs.includes(ingredient.referenceId ?? '')"
        @update:model-value="value => (model.ingredients[index] = value)"
        @toggle="toggle(ingredient.referenceId)"
        @remove="remove(index)"
        @move-up="move(index, -1)"
        @move-down="move(index, 1)"
        @reread="emit('reread', ingredient.referenceId ?? null)"
        @parse="ingredient.referenceId && emit('parse', ingredient.referenceId)"
      />
    </VueDraggable>
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
import { VueDraggable } from "vue-draggable-plus";
import IngestIngredientRow from "./IngestIngredientRow.vue";
import { useFoodStore } from "~/composables/store/use-food-store";
import { useUnitStore } from "~/composables/store/use-unit-store";
import { uuid4 } from "~/composables/use-utils";
import { flagsForField, type IngestNamedOption, type ReviewDraft } from "~/composables/use-recipe-ingest-review";
import type { CardFlag } from "~/lib/api/types/recipe-ingest";

/**
 * The draft's ingredient lines (docs/ai/PHASE2.md §6.2): compact card-like rows, one open at a time, with the group's
 * foods and units to link them to. Nothing here creates a food or unit: commit does, per §5. Lines move with the open
 * line's Move up and Move down, or by their drag handles on desktop (upstream's `vue-draggable-plus`).
 */
withDefaults(defineProps<{
  /** Unresolved errors and warnings (the page's open flags) */
  flags?: CardFlag[];
  /** Info flags, shown quietly on their lines */
  infoFlags?: CardFlag[];
  readonly?: boolean;
  canCreateFoods?: boolean;
  /** Drag handles on the lines (desktop) */
  draggable?: boolean;
  /** Offers Re-read on the open line (a ready card) */
  canReread?: boolean;
  /** Offers "Parse with AI" on the open line */
  canParse?: boolean;
  /** The lines (`referenceId`s) "Parse with AI" is reading now */
  parsingRefs?: string[];
}>(), {
  flags: () => [],
  infoFlags: () => [],
  readonly: false,
  canCreateFoods: false,
  draggable: false,
  canReread: false,
  canParse: false,
  parsingRefs: () => [],
});

const emit = defineEmits<{
  /** re-read the area of the card this line (its `referenceId`) is on */
  (e: "reread", referenceId: string | null): void;
  /** parse this line (its `referenceId`) with the AI ingredient parser */
  (e: "parse", referenceId: string): void;
}>();

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

/** Moves a line one place up (-1) or down (1) */
function move(index: number, offset: -1 | 1) {
  const target = index + offset;
  if (target < 0 || target >= model.value.ingredients.length) {
    return;
  }
  const [line] = model.value.ingredients.splice(index, 1);
  model.value.ingredients.splice(target, 0, line!);
}

function remove(index: number) {
  const [removed] = model.value.ingredients.splice(index, 1);
  if (removed?.referenceId === expanded.value) {
    expanded.value = null;
  }
}
</script>
