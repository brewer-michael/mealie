<template>
  <div class="ingest-organizer-selector" @keyup.enter.capture="keepEnterFromCreating">
    <RecipeOrganizerSelector
      v-model="model"
      :selector-type="selectorType"
      :show-add="false"
      :input-attrs="{ disabled: readonly }"
    />
  </div>
</template>

<script setup lang="ts">
import RecipeOrganizerSelector from "~/components/Domain/Recipe/RecipeOrganizerSelector.vue";
import type { CardDraftRef } from "~/lib/api/types/recipe-ingest";

/**
 * The review page's tag, category or tool selector (docs/ai/PHASE2.md §6.2): upstream's `RecipeOrganizerSelector`
 * choosing among the group's organizers, which review never creates (§9). Fork-owned.
 */
withDefaults(defineProps<{
  selectorType: "tags" | "categories" | "tools";
  readonly?: boolean;
}>(), {
  readonly: false,
});

const model = defineModel<CardDraftRef[]>({ required: true });

/**
 * Upstream's selector creates an organizer when Enter goes up on text that matches none, whatever `showAdd` says, so
 * the key never reaches it (Enter going down still picks the first match). The page's shortcuts listen on the window
 * (`useMagicKeys`) and must see the key go up, or Enter would stay held: they get a copy.
 */
function keepEnterFromCreating(event: KeyboardEvent) {
  event.stopPropagation();
  document.dispatchEvent(new KeyboardEvent(event.type, event));
}
</script>
