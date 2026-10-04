<template>
  <div
    :id="fieldAnchorId('ingredients', model.referenceId)"
    class="ingest-ingredient"
    :class="[severity ? `ingest-ingredient--${severity}` : undefined, { 'ingest-ingredient--expanded': expanded }]"
  >
    <div v-if="model.title && !expanded" class="ingest-ingredient__section text-subtitle-2 pt-2">
      {{ model.title }}
    </div>
    <div
      class="ingest-ingredient__line d-flex align-center ga-2"
      role="button"
      tabindex="0"
      :aria-expanded="expanded"
      @click="emit('toggle')"
      @keydown.enter.self.prevent="emit('toggle')"
      @keydown.space.self.prevent="emit('toggle')"
    >
      <v-icon
        v-if="severity"
        size="small"
        :color="severity"
        :icon="severity === 'error' ? $globals.icons.alertCircle : $globals.icons.alert"
      />
      <div class="flex-grow-1 ingest-ingredient__body">
        <span class="ingest-ingredient__text">{{ lineText }}</span>
        <v-icon
          v-if="foodLinked"
          size="small"
          color="success"
          class="ingest-ingredient__linked ml-1"
          :icon="$globals.icons.link"
          :title="$t('recipe-ingest.review.linked')"
          :aria-label="$t('recipe-ingest.review.linked')"
        />
        <div v-if="newFood || newUnit" class="d-flex flex-wrap ga-1 mt-1">
          <v-chip
            v-if="newFood"
            size="x-small"
            label
            class="ingest-ingredient__new-food"
            :color="canCreateFoods ? 'info' : undefined"
          >
            {{ canCreateFoods ? $t("recipe-ingest.review.new-food") : $t("recipe-ingest.review.kept-as-text") }}
          </v-chip>
          <v-chip
            v-if="newUnit"
            size="x-small"
            label
            color="info"
            class="ingest-ingredient__new-unit"
          >
            {{ $t("recipe-ingest.review.new-unit") }}
          </v-chip>
        </div>
      </div>
      <v-icon size="small" :icon="expanded ? $globals.icons.chevronDown : $globals.icons.chevronRight" />
    </div>

    <div v-if="expanded" class="ingest-ingredient__editor">
      <div class="d-flex flex-wrap ga-2">
        <v-text-field
          class="ingest-ingredient__quantity"
          :model-value="quantityText"
          inputmode="decimal"
          variant="underlined"
          density="compact"
          hide-details
          :label="$t('recipe-ingest.review.quantity')"
          :readonly="readonly"
          @update:model-value="setQuantity"
        />
        <v-combobox
          class="ingest-ingredient__unit"
          :model-value="asOption(model.unit)"
          :items="unitOptions"
          item-title="name"
          item-value="name"
          return-object
          clearable
          variant="underlined"
          density="compact"
          hide-details
          :label="$t('recipe-ingest.review.unit')"
          :readonly="readonly"
          @update:model-value="value => setRef('unit', value)"
        />
        <v-combobox
          class="ingest-ingredient__food"
          :model-value="asOption(model.food)"
          :items="foodOptions"
          item-title="name"
          item-value="name"
          return-object
          clearable
          variant="underlined"
          density="compact"
          hide-details
          :label="$t('recipe-ingest.review.food')"
          :readonly="readonly"
          @update:model-value="value => setRef('food', value)"
        />
      </div>
      <v-text-field
        :model-value="model.note ?? ''"
        variant="underlined"
        density="compact"
        hide-details
        :label="$t('recipe-ingest.review.note')"
        :readonly="readonly"
        @update:model-value="setNote"
      />
      <v-text-field
        :model-value="model.title ?? ''"
        variant="underlined"
        density="compact"
        hide-details
        :label="$t('recipe-ingest.review.section-title')"
        :readonly="readonly"
        @update:model-value="value => (model.title = value || null)"
      />
      <div
        v-for="info in infoTexts"
        :key="info.id"
        class="text-caption text-medium-emphasis mt-1 ingest-ingredient__info"
      >
        {{ info.title }}: {{ info.explanation }}
      </div>
      <div class="d-flex align-center mt-2">
        <span v-if="model.originalText" class="text-caption text-medium-emphasis ingest-ingredient__original">
          {{ $t("recipe-ingest.review.on-the-card", { text: model.originalText }) }}
        </span>
        <v-spacer />
        <v-btn
          v-if="!readonly"
          size="small"
          variant="text"
          color="error"
          :prepend-icon="$globals.icons.delete"
          @click="emit('remove')"
        >
          {{ $t("general.delete") }}
        </v-btn>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useRecipeIngestText } from "~/composables/use-recipe-ingest";
import {
  fieldAnchorId,
  fieldSeverity,
  formatQuantity,
  ingredientDisplay,
  parseQuantity,
  type IngestNamedOption,
} from "~/composables/use-recipe-ingest-review";
import type { CardDraftIngredient, CardDraftRef, CardFlag } from "~/lib/api/types/recipe-ingest";

/**
 * One ingredient line (docs/ai/PHASE2.md §6.2): it reads like the card ("1 tbsp coconut oil (melted)") with its link
 * status; tapped, it opens amount, unit and food autocompletes from the group's stores (with no "create": a name
 * that isn't linked becomes a New food, or Kept as text for members who can't add foods) and the card's line.
 */
const props = withDefaults(defineProps<{
  /** This line's unresolved errors and warnings */
  flags?: CardFlag[];
  /** This line's info flags ("Abbreviation written out"), shown quietly when it's open */
  infos?: CardFlag[];
  expanded?: boolean;
  readonly?: boolean;
  /** Commit creates foods that don't exist yet; otherwise their names stay in the note */
  canCreateFoods?: boolean;
  foodOptions?: IngestNamedOption[];
  unitOptions?: IngestNamedOption[];
}>(), {
  flags: () => [],
  infos: () => [],
  expanded: false,
  readonly: false,
  canCreateFoods: false,
  foodOptions: () => [],
  unitOptions: () => [],
});

const emit = defineEmits<{
  (e: "toggle" | "remove"): void;
}>();

const model = defineModel<CardDraftIngredient>({ required: true });

const { flagText } = useRecipeIngestText();

const severity = computed(() => fieldSeverity(props.flags));
// a new food is kept as text at commit when the reviewer can't add foods, and its explanation says so
const infoTexts = computed(() =>
  props.infos.map(flag => ({ id: flag.id, ...flagText(flag, { canCreateFoods: props.canCreateFoods }) })),
);
const lineText = computed(() => model.value.display || ingredientDisplay(model.value) || model.value.originalText || "");
const foodLinked = computed(() => !!model.value.food?.id);
const newFood = computed(() => !!model.value.food?.name && !model.value.food.id);
const newUnit = computed(() => !!model.value.unit?.name && !model.value.unit.id);

function refresh() {
  model.value.display = ingredientDisplay(model.value);
}

// typed as text ("1 1/2", "½") and stored as a number; text that isn't one yet ("1 1/") stays in the box
const quantityText = ref(formatQuantity(model.value.quantity));

watch(() => model.value.quantity, (value) => {
  if ((value ?? null) !== parseQuantity(quantityText.value)) {
    quantityText.value = formatQuantity(value);
  }
});

function setQuantity(value: string | null) {
  quantityText.value = value ?? "";
  model.value.quantity = parseQuantity(value);
  refresh();
}

/** A store item or a typed name, linked when the name matches an item exactly (name, plural or abbreviation) */
function toDraftRef(value: unknown, options: IngestNamedOption[]): CardDraftRef | null {
  if (!value) {
    return null;
  }
  if (typeof value === "string") {
    const name = value.trim();
    if (!name) {
      return null;
    }
    const lower = name.toLowerCase();
    const match = options.find(option =>
      [option.name, option.pluralName, option.abbreviation].some(text => text && text.toLowerCase() === lower),
    );
    return match ? { id: match.id, name: match.name } : { id: null, name };
  }
  const item = value as { id?: string | null; name?: string };
  return item.name ? { id: item.id ?? null, name: item.name } : null;
}

/** The draft's unit or food as the combobox's items are typed (it may not be linked to one of them) */
function asOption(value: CardDraftRef | null | undefined): IngestNamedOption | null {
  return (value ?? null) as IngestNamedOption | null;
}

function setRef(kind: "unit" | "food", value: unknown) {
  model.value[kind] = toDraftRef(value, kind === "unit" ? props.unitOptions : props.foodOptions);
  refresh();
}

function setNote(value: string | null) {
  model.value.note = value ?? "";
  refresh();
}
</script>

<style scoped>
.ingest-ingredient {
  border-left: 4px solid transparent;
  padding-left: 8px;
}

.ingest-ingredient--error {
  border-left-color: rgb(var(--v-theme-error));
}

.ingest-ingredient--warning {
  border-left-color: rgb(var(--v-theme-warning));
}

.ingest-ingredient__line {
  min-height: 40px;
  padding: 4px 0;
  cursor: pointer;
}

.ingest-ingredient__body,
.ingest-ingredient__text {
  min-width: 0;
  overflow-wrap: anywhere;
}

.ingest-ingredient__editor {
  padding: 4px 8px 8px;
  margin-bottom: 8px;
  border-radius: 4px;
  background: rgba(var(--v-theme-on-surface), 0.04);
}

.ingest-ingredient__quantity {
  flex: 0 1 96px;
}

.ingest-ingredient__unit {
  flex: 1 1 120px;
}

.ingest-ingredient__food {
  flex: 2 1 160px;
}

.ingest-ingredient__original {
  overflow-wrap: anywhere;
}
</style>
