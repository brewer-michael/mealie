<template>
  <v-expansion-panels variant="accordion">
    <v-expansion-panel>
      <v-expansion-panel-title class="text-title-small" expand-icon="$expand" collapse-icon="$expand">
        {{ $t('search.advanced') }}
      </v-expansion-panel-title>
      <v-expansion-panel-text>
        <div class="text-body-medium mb-4">
          {{ $t('group.ai-provider-settings.advanced-routes-description') }}
        </div>
        <GroupAIProviderRouteSelect
          v-for="routeSlot in ADVANCED_AI_PROVIDER_SLOTS"
          :key="routeSlot"
          :model-value="routes[routeSlot]"
          :route-slot="routeSlot"
          :providers="providers"
          class="mb-4"
          @update:model-value="(ids) => setRoute(routeSlot, ids)"
        />
      </v-expansion-panel-text>
    </v-expansion-panel>
  </v-expansion-panels>
</template>

<script setup lang="ts">
import { ADVANCED_AI_PROVIDER_SLOTS, type AIProviderRoutes } from "~/composables/use-ai-provider-routing";
import type { AIProviderSlot, AIProviderSummary } from "~/lib/api/types/group";

/** Provider lists for the slots without an upstream primary (Planner, Fast tasks, Embeddings) */
defineProps<{
  providers: AIProviderSummary[];
}>();

const routes = defineModel<AIProviderRoutes>({ required: true });

function setRoute(slot: AIProviderSlot, ids: string[]) {
  routes.value = { ...routes.value, [slot]: ids };
}
</script>
