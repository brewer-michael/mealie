<template>
  <v-autocomplete
    v-model="selected"
    v-model:search="search"
    class="pa-0 ma-0 ingest-organizer-selector"
    :items="options"
    :custom-filter="filter"
    :label="label"
    :prepend-inner-icon="icon"
    :disabled="readonly"
    :auto-select-first="hasMatch"
    item-title="name"
    item-value="value"
    variant="outlined"
    chips
    closable-chips
    multiple
    return-object
    @update:model-value="search = ''"
  >
    <template #chip="{ item, index }">
      <v-chip
        :key="index"
        class="ma-1"
        color="accent"
        variant="flat"
        label
        :text="item.id ? item.name : $t('recipe-ingest.review.new-organizer', { name: item.name })"
        closable
        @click:close="removeAt(index)"
      />
    </template>
    <template #item="{ props: itemProps, item }">
      <v-list-item
        v-if="item.create"
        v-bind="itemProps"
        class="ingest-organizer-selector__create"
        :prepend-icon="$globals.icons.create"
        :title="$t('recipe-ingest.review.create-organizer', { name: item.name })"
      />
      <v-list-item v-else v-bind="itemProps" />
    </template>
  </v-autocomplete>
</template>

<script setup lang="ts">
import { useCategoryStore, useTagStore, useToolStore } from "~/composables/store";
import { normalizeFilter } from "~/composables/use-utils";
import type { CardDraftRef } from "~/lib/api/types/recipe-ingest";

/**
 * The review page's tag, category or tool selector (docs/ai/PHASE2.md §6.2), fork-owned: it chooses among the group's
 * organizers like upstream's `RecipeOrganizerSelector`, but never creates one on the spot. Someone who may create
 * organizers (`canCreate`) gets a `Create "Grandma's"` item, chosen with a click or the arrow keys and Enter, which
 * adds the name alone: commit creates it. Enter on the typed text only picks the first match, so an unmatched tag
 * and Enter create nothing.
 */
const props = withDefaults(defineProps<{
  selectorType: "tags" | "categories" | "tools";
  readonly?: boolean;
  /** Whether the reviewer may create organizers (`permissions.canCreateOrganizers`) */
  canCreate?: boolean;
}>(), {
  readonly: false,
  canCreate: false,
});

const model = defineModel<CardDraftRef[]>({ required: true });

const i18n = useI18n();
const { $globals } = useNuxtApp();

/** An entry of the list: a group organizer, a chosen one, or the item that adds a new one */
interface Option {
  /** What tells entries apart: an organizer's name (names are unique in a group, ignoring case) */
  value: string;
  id: string | null;
  name: string;
  create?: boolean;
}

const STORES = { tags: useTagStore, categories: useCategoryStore, tools: useToolStore } as const;
const { store } = STORES[props.selectorType]();

const LABELS = { tags: "tag.tags", categories: "category.categories", tools: "tool.tools" } as const;
const label = computed(() => i18n.t(LABELS[props.selectorType]));
const icon = computed(() => $globals.icons[props.selectorType]);

const search = ref("");
const typed = computed(() => (search.value ?? "").trim());
const sameName = (a: string, b: string) => a.toLocaleLowerCase() === b.toLocaleLowerCase();

const toOption = (organizer: CardDraftRef): Option => ({ value: organizer.name ?? "", id: organizer.id ?? null, name: organizer.name ?? "" });

const groupOptions = computed<Option[]>(() =>
  (store.value ?? []).map(organizer => toOption({ id: organizer.id, name: organizer.name })),
);

/** Whether some organizer matches the typed text: only then does Enter pick (the first match) */
const hasMatch = computed(() => groupOptions.value.some(option => normalizeFilter(option.name, typed.value)));

/** The text as a new organizer, when it names none the group has or the draft holds */
const createOption = computed<Option | null>(() => {
  const name = typed.value;
  if (!props.canCreate || !name) {
    return null;
  }
  const known = [...groupOptions.value, ...model.value.map(toOption)].some(option => sameName(option.name, name));
  return known ? null : { value: `create:${name}`, id: null, name, create: true };
});

const options = computed<Option[]>(() => [...groupOptions.value, ...(createOption.value ? [createOption.value] : [])]);

/** Upstream's matching for the group's organizers; the create item always shows */
function filter(value: string, query: string, item?: { raw?: Option }) {
  return item?.raw?.create ? true : normalizeFilter(value, query);
}

const selected = computed<Option[]>({
  get: () => model.value.map(toOption),
  set: (value) => {
    model.value = (value ?? []).map(option => ({ id: option.id ?? null, name: option.name }));
  },
});

function removeAt(index: number) {
  model.value = model.value.filter((_, position) => position !== index);
}
</script>

<style scoped>
.ingest-organizer-selector {
  /* aligns the field with the other inputs, as upstream's selector does */
  margin-top: 6px;
}
</style>
