<template>
  <div v-if="providerSettings">
    <BaseCardSectionTitle v-if="!hideHeader" :title="$t('group.ai-provider-settings.ai-provider-settings')">
      <template v-if="noDefaultProviderWarning" #append-title>
        <v-tooltip location="bottom" color="warning">
          <template #activator="{ props: tooltipProps }">
            <v-icon v-bind="tooltipProps" size="small" color="warning" class="ms-2">
              {{ $globals.icons.alert }}
            </v-icon>
          </template>
          <span>{{ $t('group.ai-provider-settings.no-default-provider-warning') }}</span>
        </v-tooltip>
      </template>
    </BaseCardSectionTitle>
    <v-card-text v-if="!hideHeader" class="pt-0 pb-10 px-0">
      {{ $t("group.ai-provider-settings.ai-provider-settings-description") }}
    </v-card-text>

    <v-row class="mb-4">
      <v-col cols="12">
        <v-autocomplete
          v-model="local.defaultProviderId"
          :label="$t('group.ai-provider-settings.default-provider')"
          :items="local.providers"
          item-title="name"
          item-value="id"
          clearable
          hide-details
          density="compact"
          variant="outlined"
        />
        <v-card-subtitle class="mt-1">
          {{ $t("group.ai-provider-settings.default-provider-description") }}
        </v-card-subtitle>
        <GroupAIProviderRouteSelect
          v-if="routes"
          :model-value="routes.default"
          route-slot="default"
          :providers="local.providers"
          :primary-id="local.defaultProviderId"
          class="mt-3"
          @update:model-value="(ids) => setRoute('default', ids)"
        />
      </v-col>
      <v-col cols="12">
        <v-autocomplete
          v-model="local.audioProviderId"
          :label="$t('group.ai-provider-settings.audio-provider')"
          :items="local.providers"
          item-title="name"
          item-value="id"
          clearable
          hide-details
          density="compact"
          variant="outlined"
        />
        <v-card-subtitle class="mt-1">
          {{ $t("group.ai-provider-settings.audio-provider-description") }}
        </v-card-subtitle>
        <GroupAIProviderRouteSelect
          v-if="routes"
          :model-value="routes.audio"
          route-slot="audio"
          :providers="local.providers"
          :primary-id="local.audioProviderId"
          class="mt-3"
          @update:model-value="(ids) => setRoute('audio', ids)"
        />
      </v-col>
      <v-col cols="12">
        <v-autocomplete
          v-model="local.imageProviderId"
          :label="$t('group.ai-provider-settings.image-provider')"
          :items="local.providers"
          item-title="name"
          item-value="id"
          clearable
          hide-details
          density="compact"
          variant="outlined"
        />
        <v-card-subtitle class="mt-1">
          {{ $t("group.ai-provider-settings.image-provider-description") }}
        </v-card-subtitle>
        <GroupAIProviderRouteSelect
          v-if="routes"
          :model-value="routes.image"
          route-slot="image"
          :providers="local.providers"
          :primary-id="local.imageProviderId"
          class="mt-3"
          @update:model-value="(ids) => setRoute('image', ids)"
        />
      </v-col>
    </v-row>

    <v-alert
      v-if="routesLoadFailed"
      type="warning"
      density="compact"
      variant="tonal"
      class="mb-6"
    >
      {{ $t("group.ai-provider-settings.fallbacks-load-failed") }}
    </v-alert>
    <GroupAIProviderAdvancedRoutes v-if="routes" v-model="routes" :providers="local.providers" class="mb-6" />

    <GroupAIProviderDialog
      v-model="dialogOpen"
      :provider-id="editingProviderId ?? undefined"
      @create="(data) => $emit('create', data)"
      @update="(id, data) => $emit('update', id, data)"
    />

    <BaseCardSectionTitle
      :title="$t('group.ai-provider-settings.providers')"
      size="medium"
      class="pt-2"
    >
      <template #append-title>
        <BaseButton
          :text="$t('group.ai-provider-settings.create-provider')"
          class="ms-auto my-2"
          create
          small
          @click="openCreate"
        />
      </template>
    </BaseCardSectionTitle>

    <v-card
      v-for="provider in local.providers"
      :key="provider.id"
      variant="tonal"
      class="pa-0 mb-4"
    >
      <v-row no-gutters>
        <v-col :cols="10">
          <v-card-text>
            {{ provider.name }}
            <div v-if="unreadableKeyProviderIds.includes(provider.id)" class="text-body-small text-warning mt-1">
              {{ $t("group.ai-provider-settings.api-key-unreadable") }}
            </div>
          </v-card-text>
        </v-col>

        <v-col :cols="2">
          <BaseButtonGroup
            :buttons="[
              {
                icon: $globals.icons.edit,
                text: $t('general.edit'),
                event: 'edit',
              },
              {
                icon: $globals.icons.delete,
                text: $t('general.delete'),
                event: 'delete',
              },
            ]"
            @edit="openEdit(provider.id)"
            @delete="$emit('delete', provider.id)"
          />
        </v-col>
      </v-row>
    </v-card>
  </div>
</template>

<script setup lang="ts">
import type { AIProviderRoutes } from "~/composables/use-ai-provider-routing";
import type { AIProviderCreate, AIProviderUpdate } from "~/lib/api/types/group";
import type { AIProviderSettingsOut } from "~/lib/api/types/user";

const providerSettings = defineModel<AIProviderSettingsOut>({ required: true });
// Per-slot fallback providers; the fallback lists and the Advanced section only show when bound
const routes = defineModel<AIProviderRoutes | null>("routes", { default: null });

const props = withDefaults(defineProps<{
  hideHeader?: boolean;
  /** Shows that the fallback lists couldn't be loaded (they're hidden then) */
  routesLoadFailed?: boolean;
  /** Providers whose saved API key can't be read; only known to the group's own settings page */
  unreadableKeyProviderIds?: readonly string[];
}>(), {
  hideHeader: false,
  routesLoadFailed: false,
  unreadableKeyProviderIds: () => [],
});

const { hideHeader } = toRefs(props);

const local = reactive({ ...providerSettings.value });
watch(local, (newVal) => { providerSettings.value = { ...newVal }; });
// Sync back when the parent refreshes after create/update/delete
watch(providerSettings, (newVal) => { if (newVal) Object.assign(local, newVal); });

const noDefaultProviderWarning = computed(
  () => local.providers.length > 0 && !local.defaultProviderId,
);

defineEmits<{
  (e: "create", data: AIProviderCreate): void;
  (e: "update", id: string, data: AIProviderUpdate): void;
  (e: "delete", id: string): void;
}>();

function setRoute(slot: keyof AIProviderRoutes, ids: string[]) {
  if (routes.value) {
    routes.value = { ...routes.value, [slot]: ids };
  }
}

const dialogOpen = ref(false);
const editingProviderId = ref<string | null>(null);

function openCreate() {
  editingProviderId.value = null;
  dialogOpen.value = true;
}

function openEdit(id: string) {
  editingProviderId.value = id;
  dialogOpen.value = true;
}
</script>
