<template>
  <v-autocomplete
    v-model="selected"
    :label="$t(text.label)"
    :hint="$t(hintKey)"
    :items="options"
    :disabled="needsPrimary"
    item-title="name"
    item-value="id"
    multiple
    chips
    closable-chips
    persistent-hint
    density="compact"
    variant="outlined"
  >
    <!-- Numbered chips, since the selection order is the order providers are tried in -->
    <template #chip="{ props: chipProps, item, index }">
      <v-chip v-bind="chipProps" :text="`${index + 1}. ${item.name}`" />
    </template>
  </v-autocomplete>
</template>

<script setup lang="ts">
import { hasPrimarySlot, usableFallbacks } from "~/composables/use-ai-provider-routing";
import type { AIProviderSlot, AIProviderSummary } from "~/lib/api/types/group";

/** An ordered list of fallback providers for one slot */
const props = withDefaults(defineProps<{
  routeSlot: AIProviderSlot;
  providers: AIProviderSummary[];
  /** The slot's primary provider (Default, Image and Audio only), which isn't offered as its own fallback */
  primaryId?: string | null;
}>(), {
  primaryId: null,
});

const providerIds = defineModel<string[]>({ required: true });

// i18n keys; `needsPrimary` is the hint while a slot with a primary has none
const SLOT_TEXT: Record<AIProviderSlot, { label: string; hint: string; needsPrimary?: string }> = {
  default: {
    label: "group.ai-provider-settings.default-fallbacks",
    hint: "group.ai-provider-settings.default-fallbacks-description",
    needsPrimary: "group.ai-provider-settings.default-fallbacks-need-primary",
  },
  image: {
    label: "group.ai-provider-settings.image-fallbacks",
    hint: "group.ai-provider-settings.image-fallbacks-description",
    needsPrimary: "group.ai-provider-settings.image-fallbacks-need-primary",
  },
  audio: {
    label: "group.ai-provider-settings.audio-fallbacks",
    hint: "group.ai-provider-settings.audio-fallbacks-description",
    needsPrimary: "group.ai-provider-settings.audio-fallbacks-need-primary",
  },
  planner: {
    label: "group.ai-provider-settings.planner-providers",
    hint: "group.ai-provider-settings.planner-providers-description",
  },
  fast: {
    label: "group.ai-provider-settings.fast-providers",
    hint: "group.ai-provider-settings.fast-providers-description",
  },
  embedding: {
    label: "group.ai-provider-settings.embedding-providers",
    hint: "group.ai-provider-settings.embedding-providers-description",
  },
};

const text = computed(() => SLOT_TEXT[props.routeSlot]);

// A slot's fallbacks are only used after its primary, so they can't be edited without one. They're
// kept (and saved) as they are, and apply again once a primary is chosen.
const needsPrimary = computed(() => hasPrimarySlot(props.routeSlot) && !props.primaryId);
const hintKey = computed(() => (needsPrimary.value && text.value.needsPrimary) || text.value.hint);

const options = computed(() => props.providers.filter(provider => provider.id !== props.primaryId));

// Ids that can't be used here (a deleted provider, or the slot's primary) aren't shown. They're dropped
// once the list is edited, and are never saved.
const selected = computed({
  get: () => usableFallbacks(providerIds.value, props.providers, props.primaryId),
  set: (ids: string[]) => {
    providerIds.value = ids;
  },
});
</script>
