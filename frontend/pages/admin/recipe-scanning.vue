<template>
  <v-container fluid class="narrow-container">
    <!-- Header -->
    <BasePageTitle divider>
      <template #header>
        <v-img
          width="100%"
          max-height="200"
          max-width="150"
          :src="require('~/static/svgs/manage-recipes.svg')"
        />
      </template>
      <template #title>
        {{ $t("settings.recipe-scanning-configuration") }}
      </template>
    </BasePageTitle>

    <!-- ELI5 Instructions -->
    <v-card class="mb-6">
      <v-card-title class="text-h6">
        <v-icon left class="mr-2">
          {{ $globals.icons.lightbulb }}
        </v-icon>
        {{ $t("settings.how-image-scanning-works") }}
      </v-card-title>
      <v-card-text>
        <p class="mb-3">{{ $t("settings.image-scanning-explanation") }}</p>
        <v-alert 
          type="info" 
          variant="tonal"
          class="mb-3"
        >
          <v-alert-title>{{ $t("settings.recommendation") }}</v-alert-title>
          {{ $t("settings.image-scanning-recommendation") }}
        </v-alert>
      </v-card-text>
    </v-card>

    <!-- Provider Configuration -->
    <v-card class="mb-6">
      <v-card-title class="text-h6">
        <v-icon left class="mr-2">
          {{ $globals.icons.cog }}
        </v-icon>
        {{ $t("settings.ai-providers") }}
      </v-card-title>
      <v-card-text>
        
        <!-- Primary Provider -->
        <div class="mb-6">
          <h3 class="text-h6 mb-3">
            {{ $t("settings.primary-provider") }}
            <v-tooltip activator="parent" location="top">
              {{ $t("settings.primary-provider-tooltip") }}
            </v-tooltip>
          </h3>
          
          <v-select
            v-model="primaryProvider"
            :items="providerOptions"
            item-title="name"
            item-value="key"
            :label="$t('settings.select-provider')"
            variant="outlined"
            class="mb-3"
            @update:model-value="onPrimaryProviderChange"
          >
            <template #item="{ props, item }">
              <v-list-item v-bind="props">
                <template #prepend>
                  <v-icon :color="item.raw.color">{{ item.raw.icon }}</v-icon>
                </template>
                <v-list-item-title>{{ item.raw.name }}</v-list-item-title>
                <v-list-item-subtitle>{{ item.raw.description }}</v-list-item-subtitle>
                <template #append>
                  <v-chip
                    v-if="item.raw.cost"
                    size="small"
                    :color="item.raw.freeCredits ? 'success' : 'warning'"
                  >
                    {{ item.raw.cost }}
                  </v-chip>
                </template>
              </v-list-item>
            </template>
          </v-select>

          <!-- Primary Provider Config Form -->
          <v-expand-transition>
            <div v-if="primaryProvider && primaryProvider !== 'none'">
              <ProviderConfigForm
                :provider="primaryProvider"
                :config="primaryConfig"
                @update:config="primaryConfig = $event"
                @test-connection="testConnection('primary')"
                :testing="testingConnection.primary"
                :test-result="testResults.primary"
              />
              
              <!-- Step 1: Static API Key Field for Primary Provider -->
              <v-text-field
                model-value="••••••••••••••••••••••••"
                label="API Key"
                type="password"
                variant="outlined"
                readonly
                class="mt-4"
              />
            </div>
          </v-expand-transition>
        </div>

        <v-divider class="my-6" />

        <!-- Secondary Provider -->
        <div class="mb-6">
          <div class="d-flex align-center mb-3">
            <h3 class="text-h6">
              {{ $t("settings.secondary-provider") }}
              <v-tooltip activator="parent" location="top">
                {{ $t("settings.secondary-provider-tooltip") }}
              </v-tooltip>
            </h3>
            <v-spacer />
            <v-btn
              v-if="!showSecondary"
              variant="outlined"
              color="primary"
              @click="showSecondary = true"
              prepend-icon="mdi-plus"
            >
              {{ $t("settings.add-secondary-provider") }}
            </v-btn>
          </div>

          <v-expand-transition>
            <div v-if="showSecondary">
              <v-select
                v-model="secondaryProvider"
                :items="availableSecondaryProviders"
                item-title="name"
                item-value="key"
                :label="$t('settings.select-secondary-provider')"
                variant="outlined"
                class="mb-3"
                @update:model-value="onSecondaryProviderChange"
              >
                <template #item="{ props, item }">
                  <v-list-item v-bind="props">
                    <template #prepend>
                      <v-icon :color="item.raw.color">{{ item.raw.icon }}</v-icon>
                    </template>
                    <v-list-item-title>{{ item.raw.name }}</v-list-item-title>
                    <v-list-item-subtitle>{{ item.raw.description }}</v-list-item-subtitle>
                    <template #append>
                      <v-chip
                        v-if="item.raw.cost"
                        size="small"
                        :color="item.raw.freeCredits ? 'success' : 'warning'"
                      >
                        {{ item.raw.cost }}
                      </v-chip>
                    </template>
                  </v-list-item>
                </template>
                <template #append-item>
                  <v-divider />
                  <v-list-item @click="removeSecondaryProvider">
                    <template #prepend>
                      <v-icon color="error">{{ $globals.icons.delete }}</v-icon>
                    </template>
                    <v-list-item-title>{{ $t("settings.remove-secondary-provider") }}</v-list-item-title>
                  </v-list-item>
                </template>
              </v-select>

              <!-- Secondary Provider Config Form -->
              <v-expand-transition>
                <div v-if="secondaryProvider && secondaryProvider !== 'none'">
                  <ProviderConfigForm
                    :provider="secondaryProvider"
                    :config="secondaryConfig"
                    @update:config="secondaryConfig = $event"
                    @test-connection="testConnection('secondary')"
                    :testing="testingConnection.secondary"
                    :test-result="testResults.secondary"
                  />
                  
                  <!-- Step 1: Static API Key Field for Secondary Provider -->
                  <v-text-field
                    model-value="••••••••••••••••••••••••"
                    label="API Key"
                    type="password"
                    variant="outlined"
                    readonly
                    class="mt-4"
                  />
                </div>
              </v-expand-transition>
            </div>
          </v-expand-transition>
        </div>
      </v-card-text>
    </v-card>

    <!-- OCR Fallback -->
    <v-card class="mb-6">
      <v-card-title class="text-h6">
        <v-icon left class="mr-2">
          {{ $globals.icons.shield }}
        </v-icon>
        {{ $t("settings.privacy-fallback") }}
      </v-card-title>
      <v-card-text>
        <v-radio-group v-model="enableOcrFallback" class="mt-2">
          <v-radio
            :value="true"
            color="primary"
          >
            <template #label>
              <div>
                <div class="font-weight-medium">
                  {{ $t("settings.enable-ocr-fallback") }}
                </div>
                <div class="text-caption text-medium-emphasis">
                  {{ $t("settings.ocr-fallback-description") }}
                </div>
              </div>
            </template>
          </v-radio>
          <v-radio
            :value="false"
            color="primary"
          >
            <template #label>
              <div>
                <div class="font-weight-medium">
                  {{ $t("settings.disable-ocr-fallback") }}
                </div>
                <div class="text-caption text-medium-emphasis">
                  {{ $t("settings.ocr-disabled-description") }}
                </div>
              </div>
            </template>
          </v-radio>
        </v-radio-group>

        <v-alert
          v-if="enableOcrFallback"
          type="info"
          variant="tonal"
          class="mt-4"
        >
          <v-alert-title>{{ $t("settings.ocr-expectations") }}</v-alert-title>
          {{ $t("settings.ocr-expectations-text") }}
        </v-alert>
      </v-card-text>
    </v-card>

    <!-- Save Button -->
    <div class="d-flex justify-end gap-2">
      <BaseButton
        color="success"
        @click="saveConfiguration"
        :loading="saving"
      >
        <template #icon>
          {{ $globals.icons.save }}
        </template>
        {{ $t("general.save") }}
      </BaseButton>
    </div>
  </v-container>
</template>

<script lang="ts">
import { onMounted } from 'vue';
import { useAppInfo } from "~/composables/api";
import { useRequests } from "~/composables/api/api-client";

// Provider configuration component
const ProviderConfigForm = defineComponent({
  name: "ProviderConfigForm",
  props: {
    provider: { type: String, required: true },
    config: { type: Object, required: true },
    testing: { type: Boolean, default: false },
    testResult: { type: Object, default: null }
  },
  emits: ["update:config", "test-connection"],
  setup(props, { emit }) {
    const { $globals } = useNuxtApp();
    
    const providerConfigs = computed(() => ({
      'gemini': {
        name: 'Google Gemini',
        fields: [
          {
            key: 'apiKey',
            label: 'API Key',
            type: 'password',
            placeholder: 'AIza...',
            helpText: 'Get your free API key from Google AI Studio',
            helpLink: 'https://makersuite.google.com/app/apikey'
          },
          {
            key: 'model',
            label: 'Model',
            type: 'select',
            options: [
              { value: 'gemini-1.5-flash', text: 'Gemini 1.5 Flash (Recommended - Free)' },
              { value: 'gemini-1.5-pro', text: 'Gemini 1.5 Pro (Higher quality)' }
            ],
            default: 'gemini-1.5-flash'
          }
        ]
      },
      'openai': {
        name: 'OpenAI GPT-4V',
        fields: [
          {
            key: 'apiKey',
            label: 'API Key',
            type: 'password',
            placeholder: 'sk-...',
            helpText: 'Requires $5 minimum payment to OpenAI',
            helpLink: 'https://platform.openai.com/api-keys'
          },
          {
            key: 'model',
            label: 'Model',
            type: 'select',
            options: [
              { value: 'gpt-4o-mini', text: 'GPT-4o Mini (Recommended - Lower cost)' },
              { value: 'gpt-4o', text: 'GPT-4o (Higher quality)' },
              { value: 'gpt-4-vision-preview', text: 'GPT-4 Vision (Legacy)' }
            ],
            default: 'gpt-4o-mini'
          }
        ]
      },
      'anthropic': {
        name: 'Anthropic Claude',
        fields: [
          {
            key: 'apiKey',
            label: 'API Key',
            type: 'password',
            placeholder: 'sk-ant-...',
            helpText: 'Get your API key from Anthropic Console',
            helpLink: 'https://console.anthropic.com/'
          },
          {
            key: 'model',
            label: 'Model',
            type: 'select',
            options: [
              { value: 'claude-3-haiku-20240307', text: 'Claude 3 Haiku (Recommended - Lower cost)' },
              { value: 'claude-3-sonnet-20240229', text: 'Claude 3 Sonnet (Higher quality)' },
              { value: 'claude-3-opus-20240229', text: 'Claude 3 Opus (Highest quality)' }
            ],
            default: 'claude-3-haiku-20240307'
          }
        ]
      },
      'ollama': {
        name: 'Ollama (Local)',
        fields: [
          {
            key: 'baseUrl',
            label: 'Base URL',
            type: 'text',
            placeholder: 'http://localhost:11434',
            helpText: 'URL of your Ollama server',
            default: 'http://localhost:11434'
          },
          {
            key: 'model',
            label: 'Model',
            type: 'select',
            options: [
              { value: 'llava', text: 'LLaVA (Recommended - General purpose)' },
              { value: 'llava:13b', text: 'LLaVA 13B (Higher quality)' },
              { value: 'bakllava', text: 'BakLLaVA (Alternative)' }
            ],
            default: 'llava'
          }
        ]
      }
    }));
    
    const currentConfig = computed(() => providerConfigs.value[props.provider]);
    const localConfig = ref({ ...props.config });
    
    // Set defaults for new providers
    watch(() => props.provider, (newProvider) => {
      const config = providerConfigs.value[newProvider];
      if (config) {
        config.fields.forEach(field => {
          if (field.default && !localConfig.value[field.key]) {
            localConfig.value[field.key] = field.default;
          }
        });
        emit('update:config', localConfig.value);
      }
    }, { immediate: true });
    
    const updateField = (key: string, value: any) => {
      localConfig.value[key] = value;
      emit('update:config', localConfig.value);
    };
    
    return {
      currentConfig,
      localConfig,
      updateField,
      $globals
    };
  },
  template: `
    <v-card variant="tonal" class="mb-4">
      <v-card-title class="text-subtitle-1">
        {{ currentConfig.name }} Configuration
      </v-card-title>
      <v-card-text>
        <div v-for="field in currentConfig.fields" :key="field.key" class="mb-4">
          <!-- Text/Password Fields -->
          <v-text-field
            v-if="field.type === 'text' || field.type === 'password'"
            :model-value="localConfig[field.key]"
            @update:model-value="updateField(field.key, $event)"
            :label="field.label"
            :placeholder="field.placeholder"
            :type="field.type"
            variant="outlined"
            :append-inner-icon="field.type === 'password' ? $globals.icons.eye : undefined"
          />
          
          <!-- Select Fields -->
          <v-select
            v-else-if="field.type === 'select'"
            :model-value="localConfig[field.key]"
            @update:model-value="updateField(field.key, $event)"
            :items="field.options"
            :label="field.label"
            item-title="text"
            item-value="value"
            variant="outlined"
          />
          
          <!-- Help Text -->
          <div v-if="field.helpText" class="text-caption text-medium-emphasis mt-1">
            {{ field.helpText }}
            <a 
              v-if="field.helpLink" 
              :href="field.helpLink" 
              target="_blank" 
              class="text-decoration-none"
            >
              Learn more →
            </a>
          </div>
        </div>
        
        <!-- Test Connection -->
        <div class="d-flex align-center gap-2 mt-4">
          <v-btn
            variant="outlined"
            color="primary"
            @click="$emit('test-connection')"
            :loading="testing"
            :disabled="!localConfig.apiKey"
          >
            <template #prepend>
              <v-icon>{{ $globals.icons.connectionTest }}</v-icon>
            </template>
            Test Connection
          </v-btn>
          
          <!-- Test Results -->
          <div v-if="testResult" class="ml-2">
            <v-chip
              :color="testResult.success ? 'success' : 'error'"
              size="small"
            >
              <v-icon start>
                {{ testResult.success ? $globals.icons.check : $globals.icons.close }}
              </v-icon>
              {{ testResult.success ? 'Connected' : 'Failed' }}
            </v-chip>
            <div v-if="!testResult.success" class="text-caption text-error mt-1">
              {{ testResult.error }}
            </div>
          </div>
        </div>
      </v-card-text>
    </v-card>
  `
});

export default defineNuxtComponent({
  components: { ProviderConfigForm },
  middleware: ["admin-only"],
  setup() {
    const { $globals } = useNuxtApp();
    const i18n = useI18n();
    const requests = useRequests();
    
    // State
    const primaryProvider = ref('none');
    const secondaryProvider = ref('none');
    const primaryConfig = ref({});
    const secondaryConfig = ref({});
    const enableOcrFallback = ref(true);
    const showSecondary = ref(false);
    const saving = ref(false);
    const testingConnection = ref({ primary: false, secondary: false });
    const testResults = ref({ primary: null, secondary: null });
    
    // Provider options
    const allProviders = [
      {
        key: 'none',
        name: 'Disabled',
        description: 'No AI provider configured',
        icon: $globals.icons.close,
        color: 'grey',
        cost: null
      },
      {
        key: 'gemini',
        name: 'Google Gemini',
        description: 'Free tier available - Best for getting started',
        icon: $globals.icons.google,
        color: 'blue',
        cost: 'Free*',
        freeCredits: true
      },
      {
        key: 'openai',
        name: 'OpenAI GPT-4V',
        description: 'High accuracy - Requires $5 minimum payment',
        icon: $globals.icons.openai,
        color: 'green',
        cost: '$0.01/scan',
        freeCredits: false
      },
      {
        key: 'anthropic',
        name: 'Anthropic Claude',
        description: 'High quality vision analysis',
        icon: $globals.icons.robot,
        color: 'orange',
        cost: '$0.01/scan',
        freeCredits: false
      },
      {
        key: 'ollama',
        name: 'Ollama (Local)',
        description: 'Run models locally - No API key required',
        icon: $globals.icons.server,
        color: 'purple',
        cost: 'Free',
        freeCredits: true
      }
    ];
    
    const providerOptions = computed(() => allProviders);
    
    const availableSecondaryProviders = computed(() => 
      allProviders.filter(p => p.key !== primaryProvider.value)
    );
    
    // Load existing settings on page load
    const loadSettings = async () => {
      try {
        console.log('Loading settings from API...');
        const { data, error } = await requests.get('/api/admin/recipe-scanning/settings');
        
        if (error) {
          console.error('API error:', error);
          return;
        }
        
        console.log('Loaded settings:', data);
        
        // Set provider selections
        primaryProvider.value = data.image_scanning_primary_provider || 'none';
        secondaryProvider.value = data.image_scanning_secondary_provider || 'none';
        enableOcrFallback.value = data.image_scanning_enable_ocr_fallback;
        
        console.log('Set primaryProvider to:', primaryProvider.value);
        
        // Show secondary provider if it's configured
        if (secondaryProvider.value !== 'none') {
          showSecondary.value = true;
        }
        
      } catch (error) {
        console.error('Failed to load settings:', error);
      }
    };

    // Load settings on component mount
    onMounted(() => {
      loadSettings();
    });

    // Methods
    const onPrimaryProviderChange = (value: string) => {
      primaryConfig.value = {};
      testResults.value.primary = null;
      if (value === secondaryProvider.value) {
        secondaryProvider.value = 'none';
        secondaryConfig.value = {};
        testResults.value.secondary = null;
      }
    };
    
    const onSecondaryProviderChange = (value: string) => {
      secondaryConfig.value = {};
      testResults.value.secondary = null;
    };
    
    const removeSecondaryProvider = () => {
      showSecondary.value = false;
      secondaryProvider.value = 'none';
      secondaryConfig.value = {};
      testResults.value.secondary = null;
    };
    
    const testConnection = async (type: 'primary' | 'secondary') => {
      testingConnection.value[type] = true;
      testResults.value[type] = null;
      
      try {
        // Simulate API test - replace with actual API call
        await new Promise(resolve => setTimeout(resolve, 2000));
        
        // Mock successful connection for demo
        testResults.value[type] = {
          success: true,
          message: 'Connection successful!'
        };
      } catch (error) {
        testResults.value[type] = {
          success: false,
          error: 'Connection failed. Please check your configuration.'
        };
      } finally {
        testingConnection.value[type] = false;
      }
    };
    
    const saveConfiguration = async () => {
      saving.value = true;
      try {
        // Prepare the settings data
        const settingsData = {
          image_scanning_primary_provider: primaryProvider.value,
          image_scanning_secondary_provider: secondaryProvider.value,
          image_scanning_enable_ocr_fallback: enableOcrFallback.value,
          // API keys - map to correct provider
          openai_api_key: primaryProvider.value === 'openai' ? primaryConfig.value.apiKey : 
                          (secondaryProvider.value === 'openai' ? secondaryConfig.value.apiKey : null),
          gemini_api_key: primaryProvider.value === 'gemini' ? primaryConfig.value.apiKey : 
                          (secondaryProvider.value === 'gemini' ? secondaryConfig.value.apiKey : null),
          anthropic_api_key: primaryProvider.value === 'anthropic' ? primaryConfig.value.apiKey : 
                             (secondaryProvider.value === 'anthropic' ? secondaryConfig.value.apiKey : null),
          ollama_base_url: primaryProvider.value === 'ollama' ? primaryConfig.value.baseUrl : 
                           (secondaryProvider.value === 'ollama' ? secondaryConfig.value.baseUrl : null),
          // Models - map to correct provider
          openai_model: primaryProvider.value === 'openai' ? primaryConfig.value.model : 
                        (secondaryProvider.value === 'openai' ? secondaryConfig.value.model : null),
          gemini_model: primaryProvider.value === 'gemini' ? primaryConfig.value.model : 
                        (secondaryProvider.value === 'gemini' ? secondaryConfig.value.model : null),
          anthropic_model: primaryProvider.value === 'anthropic' ? primaryConfig.value.model : 
                           (secondaryProvider.value === 'anthropic' ? secondaryConfig.value.model : null),
          ollama_model: primaryProvider.value === 'ollama' ? primaryConfig.value.model : 
                        (secondaryProvider.value === 'ollama' ? secondaryConfig.value.model : null),
        };

        // Make API call to save settings
        const { data } = await requests.put('/api/admin/recipe-scanning/settings', settingsData);
        const response = data;

        // Show success toast
        // TODO: Add proper toast notification system
        console.log('Settings saved successfully:', response);
        
      } catch (error) {
        console.error('Failed to save configuration:', error);
        // TODO: Show error toast
      } finally {
        saving.value = false;
      }
    };
    
    return {
      primaryProvider,
      secondaryProvider,
      primaryConfig,
      secondaryConfig,
      enableOcrFallback,
      showSecondary,
      saving,
      testingConnection,
      testResults,
      providerOptions,
      availableSecondaryProviders,
      onPrimaryProviderChange,
      onSecondaryProviderChange,
      removeSecondaryProvider,
      testConnection,
      saveConfiguration,
      $globals
    };
  }
});
</script>

<style scoped>
.narrow-container {
  max-width: 800px;
  margin: 0 auto;
}
</style>