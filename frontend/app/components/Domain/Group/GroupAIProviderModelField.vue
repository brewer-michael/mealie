<template>
  <div>
    <v-combobox
      v-model="model"
      v-model:menu="menu"
      :label="$t('group.ai-provider-settings.model')"
      :hint="$t(
        protocol === 'anthropic'
          ? 'group.ai-provider-settings.model-description-anthropic'
          : 'group.ai-provider-settings.model-description',
      )"
      :messages="needsApiKey ? $t('group.ai-provider-settings.load-models-needs-api-key') : undefined"
      :items="modelIds"
      :rules="[validators.required]"
      density="compact"
      variant="outlined"
      class="mb-4"
    >
      <template #item="{ props: itemProps, item }">
        <v-list-item v-bind="itemProps" :subtitle="modelSubtitle(item)">
          <template v-if="modelInfo.get(item)?.supportsImages" #append>
            <v-chip size="x-small" :prepend-icon="$globals.icons.fileImage">
              {{ $t('group.ai-provider-settings.reads-images') }}
            </v-chip>
          </template>
        </v-list-item>
      </template>
      <template v-if="modelInfo.get(model)?.supportsImages" #append-inner>
        <v-chip size="x-small" :prepend-icon="$globals.icons.fileImage">
          {{ $t('group.ai-provider-settings.reads-images') }}
        </v-chip>
      </template>
      <template #append>
        <v-btn
          variant="tonal"
          size="small"
          :loading="loading"
          :disabled="!canLoad"
          @click="handleLoad"
        >
          {{ $t('group.ai-provider-settings.load-models') }}
        </v-btn>
      </template>
    </v-combobox>
    <v-alert
      v-if="failed || empty"
      :type="failed ? 'error' : 'info'"
      density="compact"
      variant="tonal"
      closable
      class="mb-4"
      @click:close="reset"
    >
      {{ $t(
        failed
          ? 'group.ai-provider-settings.load-models-failed'
          : 'group.ai-provider-settings.no-models-found',
      ) }}
    </v-alert>
  </div>
</template>

<script setup lang="ts">
import { useAIProviderModels } from "~/composables/use-ai-provider-routing";
import { validators } from "~/composables/use-validators";
import type { AIProviderModelsQuery, AIProviderProtocol } from "~/lib/api/types/group";

/**
 * The provider dialog's model field: free text, plus the models the provider lists once loaded.
 * Takes the dialog's current (possibly unsaved) connection settings.
 */
const props = withDefaults(defineProps<{
  protocol: AIProviderProtocol;
  baseUrl?: string | null;
  timeout?: number;
  requestHeaders?: Record<string, string>;
  requestParams?: Record<string, string>;
  apiKey?: string | null;
  /** A saved provider, whose saved key is used while `apiKey` is blank */
  providerId?: string;
}>(), {
  baseUrl: null,
  timeout: 300,
  requestHeaders: () => ({}),
  requestParams: () => ({}),
  apiKey: null,
  providerId: undefined,
});

const model = defineModel<string>({ required: true });

const { models, loading, failed, empty, load, reset } = useAIProviderModels();

const menu = ref(false);
const modelIds = computed(() => models.value.map(info => info.id));
const modelInfo = computed(() => new Map(models.value.map(info => [info.id, info])));

// An unsaved provider needs a key to list models; a saved one falls back to its saved key
const needsApiKey = computed(() => !props.providerId && !props.apiKey?.trim());
const canLoad = computed(() => !loading.value && !needsApiKey.value);

// Another API type lists other models
watch(() => props.protocol, () => reset());

function modelSubtitle(id: string) {
  const displayName = modelInfo.value.get(id)?.displayName;
  return displayName && displayName !== id ? displayName : undefined;
}

async function handleLoad() {
  const query: AIProviderModelsQuery & { apiKey?: string } = {
    protocol: props.protocol,
    baseUrl: props.baseUrl || null,
    timeout: props.timeout,
    requestHeaders: Object.keys(props.requestHeaders).length ? props.requestHeaders : undefined,
    requestParams: Object.keys(props.requestParams).length ? props.requestParams : undefined,
  };
  if (props.apiKey) {
    query.apiKey = props.apiKey;
  }
  await load(query, props.providerId);
  // Show what was found right away
  menu.value = models.value.length > 0;
}

defineExpose({ reset });
</script>
