<template>
  <BaseDialog
    v-model="dialog"
    :title="isEdit ? $t('group.ai-provider-settings.edit-provider') : $t('group.ai-provider-settings.create-provider')"
    :icon="$globals.icons.robot"
    :loading="loading"
    can-submit
    :submit-icon="isEdit ? $globals.icons.save : $globals.icons.createAlt"
    :submit-text="isEdit ? $t('general.update') : $t('general.create')"
    :submit-disabled="submitDisabled"
    @submit="handleSubmit"
    @close="resetForm"
  >
    <v-card-text v-if="init" style="max-height: 70vh; overflow-y: auto;">
      <v-form ref="form" v-no-autofill>
        <v-text-field
          v-model="formData.name"
          :label="$t('group.ai-provider-settings.provider-name')"
          :rules="[validators.required]"
          density="compact"
          variant="outlined"
          class="mb-4"
        />
        <v-select
          v-model="formData.protocol"
          :label="$t('group.ai-provider-settings.api-type')"
          :hint="$t('group.ai-provider-settings.api-type-description')"
          :items="protocolOptions"
          persistent-hint
          density="compact"
          variant="outlined"
          class="mb-4"
        />
        <GroupAIProviderModelField
          ref="modelField"
          v-model="formData.model"
          :protocol="formData.protocol"
          :base-url="formData.baseUrl"
          :timeout="formData.timeout"
          :request-headers="formData.requestHeaders"
          :request-params="formData.requestParams"
          :api-key="formData.apiKey"
          :provider-id="providerId"
        />
        <v-text-field
          v-model="formData.apiKey"
          :label="$t('group.ai-provider-settings.api-key')"
          :hint="$t(
            isEdit
              ? 'group.ai-provider-settings.api-key-description-edit'
              : 'group.ai-provider-settings.api-key-description-create',
          )"
          :persistent-hint="isEdit"
          :rules="isEdit ? [] : [validators.required]"
          density="compact"
          variant="outlined"
          type="password"
          class="mb-4"
        />
        <v-alert
          v-if="apiKeyNotice"
          :type="apiKeyNotice.type"
          density="compact"
          variant="tonal"
          class="mb-4"
        >
          {{ apiKeyNotice.text }}
        </v-alert>
        <v-text-field
          v-model="formData.baseUrl"
          :label="$t('group.ai-provider-settings.base-url')"
          :hint="$t(
            formData.protocol === 'anthropic'
              ? 'group.ai-provider-settings.base-url-description-anthropic'
              : 'group.ai-provider-settings.base-url-description',
          )"
          :rules="[baseUrlRule]"
          density="compact"
          variant="outlined"
          class="mb-4"
        />
        <v-number-input
          v-model.number="formData.timeout"
          :label="$t('group.ai-provider-settings.request-timeout-seconds')"
          type="number"
          :min="0"
          hide-details
          density="compact"
          variant="outlined"
          class="mb-4"
        />
        <v-number-input
          v-model="formData.monthlyTokenLimit"
          :label="$t('group.ai-provider-settings.monthly-token-limit')"
          :hint="$t('group.ai-provider-settings.monthly-token-limit-description')"
          :min="1"
          :max="maxMonthlyTokenLimit"
          control-variant="hidden"
          grouping
          clearable
          persistent-hint
          density="compact"
          variant="outlined"
          class="mb-4"
        />
        <v-expansion-panels v-model="advancedPanel" variant="accordion">
          <v-expansion-panel>
            <v-expansion-panel-title class="text-subtitle-2" expand-icon="$expand" collapse-icon="$expand">
              {{ $t('search.advanced') }}
            </v-expansion-panel-title>
            <v-expansion-panel-text class="px-0">
              <div class="mb-2 text-subtitle-2">
                {{ $t('group.ai-provider-settings.request-headers') }}
              </div>
              <BaseKeyValueEditor
                v-model="formData.requestHeaders"
                class="mb-4"
              />
              <v-divider class="mb-4" />
              <div class="mb-2 text-subtitle-2">
                {{ $t('group.ai-provider-settings.request-params') }}
              </div>
              <BaseKeyValueEditor
                v-model="formData.requestParams"
              />
            </v-expansion-panel-text>
          </v-expansion-panel>
        </v-expansion-panels>

        <v-alert
          v-if="testResult"
          :type="testResult.success ? 'success' : 'error'"
          density="compact"
          variant="tonal"
          class="mt-4"
        >
          {{ connectionMessage }}{{ imageSupportMessage }}
        </v-alert>
      </v-form>
    </v-card-text>
    <AppLoader v-else />

    <template #custom-card-action>
      <v-btn
        variant="text"
        :loading="testing"
        :disabled="submitDisabled || testing"
        @click="handleTest"
      >
        {{ $t('group.ai-provider-settings.test-connection') }}
      </v-btn>
    </template>
  </BaseDialog>
</template>

<script setup lang="ts">
import { useAIProviders } from "~/composables/use-ai-providers";
import { apiKeyDestination, baseUrlHasQueryOrFragment } from "~/composables/use-ai-provider-routing";
import { validators } from "~/composables/use-validators";
import type { AIProviderCreate, AIProviderTestResult, AIProviderUpdate } from "~/lib/api/types/group";

const props = withDefaults(defineProps<{
  providerId?: string;
}>(), {
  providerId: undefined,
});

const emit = defineEmits<{
  (e: "create", data: AIProviderCreate): void;
  (e: "update", id: string, data: AIProviderUpdate): void;
}>();

const dialog = defineModel<boolean>({ default: false });

const { $globals } = useNuxtApp();
const i18n = useI18n();
const { loading, getOne, testOne, testSavedOne } = useAIProviders();
const init = ref(false);

const form = ref();
const modelField = ref<{ reset: () => void }>();
const advancedPanel = ref<number | undefined>(undefined);

const isEdit = computed(() => !!props.providerId);

const defaultForm = () => ({
  name: "",
  model: "",
  apiKey: "",
  baseUrl: "",
  timeout: 300,
  protocol: "openai" as NonNullable<AIProviderCreate["protocol"]>,
  monthlyTokenLimit: null as number | null,
  requestHeaders: {} as Record<string, string>,
  requestParams: {} as Record<string, string>,
});

const formData = reactive(defaultForm());

const protocolOptions = computed(() => [
  { title: i18n.t("group.ai-provider-settings.api-type-openai"), value: "openai" },
  { title: i18n.t("group.ai-provider-settings.api-type-anthropic"), value: "anthropic" },
]);

// The largest limit the backend stores (a 32-bit integer)
const maxMonthlyTokenLimit = 2_147_483_647;

const testing = ref(false);
const testResult = ref<AIProviderTestResult | null>(null);

// The saved key can't be decrypted (e.g. the server's secret changed), so it has to be entered again
const apiKeyUnreadable = ref(false);
// While the key is left blank, the saved one is only used where it was saved for
const savedApiKeyDestination = ref<string | null>(null);

const apiKeyNotice = computed(() => {
  if (formData.apiKey) {
    return null;
  }
  if (apiKeyUnreadable.value) {
    return { type: "warning" as const, text: i18n.t("group.ai-provider-settings.api-key-unreadable") };
  }
  if (savedApiKeyDestination.value && apiKeyDestination(formData) !== savedApiKeyDestination.value) {
    return { type: "info" as const, text: i18n.t("group.ai-provider-settings.api-key-needed-for-changes") };
  }
  return null;
});

const submitDisabled = computed(() => {
  return !formData.name?.trim() || !formData.model?.trim() || (!isEdit.value && !formData.apiKey?.trim())
    || baseUrlHasQueryOrFragment(formData.baseUrl);
});

function baseUrlRule(value: string | null) {
  return !baseUrlHasQueryOrFragment(value) || i18n.t("group.ai-provider-settings.base-url-no-query");
}

const connectionMessage = computed(() => {
  const result = testResult.value;
  if (!result) return "";
  if (result.success) return i18n.t("group.ai-provider-settings.test-connection-succeeded");
  return result.message || i18n.t("group.ai-provider-settings.test-connection-failed");
});

// Capability info rather than a second pass/fail check - a text-only provider is a valid setup,
// it just can't be used as the image provider. Appended to the connection message above.
const imageSupportMessage = computed(() => {
  const result = testResult.value;
  if (!result?.success) return "";
  return result.supportsImages
    ? ` — ${i18n.t("group.ai-provider-settings.supports-images")}`
    : ` — ${i18n.t("group.ai-provider-settings.text-only-provider")}`;
});

// Fetch existing provider when editing; reset form for create mode
watch(
  () => [dialog.value, props.providerId] as const,
  async ([open, id]) => {
    if (!open) return;
    testResult.value = null;
    if (!id) {
      // Create mode — just show the empty form
      resetForm();
      init.value = true;
      return;
    }
    init.value = false;
    const { data } = await getOne(id);
    init.value = true;
    if (data) {
      formData.name = data.name;
      formData.model = data.model;
      formData.apiKey = "";
      formData.baseUrl = data.baseUrl ?? "";
      formData.timeout = data.timeout ?? 300;
      formData.protocol = data.protocol ?? "openai";
      formData.monthlyTokenLimit = data.monthlyTokenLimit ?? null;
      apiKeyUnreadable.value = data.apiKeySet === false;
      savedApiKeyDestination.value = apiKeyDestination(data);
      formData.requestHeaders = { ...(data.requestHeaders ?? {}) };
      formData.requestParams = { ...(data.requestParams ?? {}) };
    }
  },
  { immediate: true },
);

function handleSubmit() {
  // Required field guard (button is also disabled, but keep as a safeguard)
  if (!formData.name?.trim() || !formData.model?.trim()) return;
  if (!isEdit.value && !formData.apiKey?.trim()) return;

  if (isEdit.value && props.providerId) {
    const payload: AIProviderUpdate & { apiKey?: string } = {
      name: formData.name,
      model: formData.model,
      baseUrl: formData.baseUrl || null,
      timeout: formData.timeout,
      protocol: formData.protocol,
      monthlyTokenLimit: formData.monthlyTokenLimit || null,
      requestHeaders: Object.keys(formData.requestHeaders).length ? formData.requestHeaders : undefined,
      requestParams: Object.keys(formData.requestParams).length ? formData.requestParams : undefined,
    };
    if (formData.apiKey) {
      payload.apiKey = formData.apiKey;
    }
    emit("update", props.providerId, payload);
  }
  else {
    const createPayload = {
      name: formData.name,
      model: formData.model,
      apiKey: formData.apiKey,
      baseUrl: formData.baseUrl || null,
      timeout: formData.timeout,
      protocol: formData.protocol,
      monthlyTokenLimit: formData.monthlyTokenLimit || null,
      requestHeaders: Object.keys(formData.requestHeaders).length ? formData.requestHeaders : undefined,
      requestParams: Object.keys(formData.requestParams).length ? formData.requestParams : undefined,
    };
    emit("create", createPayload as AIProviderCreate);
  }
}

function resetForm() {
  Object.assign(formData, defaultForm());
  form.value?.reset();
  advancedPanel.value = undefined;
  testResult.value = null;
  apiKeyUnreadable.value = false;
  savedApiKeyDestination.value = null;
  modelField.value?.reset();
}

async function handleTest() {
  testing.value = true;
  testResult.value = null;
  try {
    let data: AIProviderTestResult | null;
    if (isEdit.value && props.providerId) {
      // Test the form's CURRENT values, not what's saved in the DB — the user may have just
      // changed the model/base_url. If they left the API key blank (meaning "keep the existing
      // one"), the backend falls back to the saved key since we don't have that value here.
      const overrides: AIProviderUpdate & { apiKey?: string } = {
        name: formData.name,
        model: formData.model,
        baseUrl: formData.baseUrl || null,
        timeout: formData.timeout,
        protocol: formData.protocol,
        requestHeaders: Object.keys(formData.requestHeaders).length ? formData.requestHeaders : undefined,
        requestParams: Object.keys(formData.requestParams).length ? formData.requestParams : undefined,
      };
      if (formData.apiKey) {
        overrides.apiKey = formData.apiKey;
      }
      ({ data } = await testSavedOne(props.providerId, overrides));
    }
    else {
      ({ data } = await testOne({
        name: formData.name,
        model: formData.model,
        apiKey: formData.apiKey,
        baseUrl: formData.baseUrl || null,
        timeout: formData.timeout,
        protocol: formData.protocol,
        requestHeaders: Object.keys(formData.requestHeaders).length ? formData.requestHeaders : undefined,
        requestParams: Object.keys(formData.requestParams).length ? formData.requestParams : undefined,
      } as AIProviderCreate));
    }

    testResult.value = data;
  }
  finally {
    testing.value = false;
  }
}
</script>
