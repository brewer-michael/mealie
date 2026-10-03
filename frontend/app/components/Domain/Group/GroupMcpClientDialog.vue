<template>
  <BaseDialog
    v-model="dialog"
    :title="isEdit ? $t('mcp.edit-client') : $t('mcp.add-client')"
    :icon="$globals.icons.robot"
    :loading="saving"
    can-submit
    keep-open
    disable-submit-on-enter
    :submit-icon="isEdit ? $globals.icons.save : $globals.icons.createAlt"
    :submit-text="isEdit ? $t('general.update') : $t('general.create')"
    :submit-disabled="!valid || homeAssistantUrlInvalid"
    max-width="640px"
    width="100%"
    @submit="handleSubmit"
    @close="clearError"
  >
    <v-card-text style="max-height: 70vh; overflow-y: auto;">
      <template v-if="!isEdit">
        <v-radio-group
          :model-value="preset"
          :label="$t('mcp.preset')"
          inline
          hide-details
          class="mb-4"
          @update:model-value="selectPreset"
        >
          <v-radio :label="$t('mcp.preset-home-assistant')" value="home-assistant" />
          <v-radio :label="$t('mcp.preset-other')" value="other" />
        </v-radio-group>
        <v-alert
          v-if="presetFailed"
          type="warning"
          density="compact"
          variant="tonal"
          class="mb-4"
        >
          {{ $t("mcp.preset-load-failed") }}
        </v-alert>
        <v-text-field
          v-if="preset === 'home-assistant'"
          v-model="homeAssistantUrl"
          :label="$t('mcp.home-assistant-url')"
          :hint="$t('mcp.home-assistant-url-hint')"
          :rules="[homeAssistantUrlRule]"
          :error-messages="homeAssistantUrlRefused ?? undefined"
          :loading="presetLoading"
          persistent-hint
          density="compact"
          variant="outlined"
          class="mb-4"
          @change="applyHomeAssistantUrl"
          @keydown.enter.prevent="applyHomeAssistantUrl"
        />
      </template>

      <v-text-field
        v-model="form.name"
        :label="$t('general.name')"
        :rules="[nameRule]"
        density="compact"
        variant="outlined"
        class="mb-4"
      />

      <v-radio-group
        v-if="!isEdit && preset === 'other'"
        v-model="form.isConfidential"
        :label="$t('mcp.client-type')"
        hide-details
        class="mb-4"
      >
        <v-radio :label="$t('mcp.client-type-confidential')" :value="true" />
        <v-radio :label="$t('mcp.client-type-public')" :value="false" />
      </v-radio-group>
      <p v-else-if="client" class="text-body-2 mb-4">
        {{ client.isConfidential ? $t("mcp.confidential-client") : $t("mcp.public-client") }}.
        {{ $t("mcp.client-type-fixed") }}
      </p>

      <fieldset class="redirect-uris mb-4">
        <legend class="text-subtitle-2 mb-2">
          {{ $t("mcp.redirect-uris") }}
        </legend>
        <div
          v-for="(_, index) in form.redirectUris"
          :key="index"
          class="d-flex align-start"
        >
          <v-text-field
            v-model="form.redirectUris[index]"
            :label="$t('mcp.redirect-uri-n', { n: index + 1 })"
            :rules="[redirectUriRule]"
            density="compact"
            variant="outlined"
            class="mb-2"
          />
          <v-btn
            :icon="$globals.icons.delete"
            :aria-label="$t('mcp.remove-redirect-uri-n', { n: index + 1 })"
            :disabled="form.redirectUris.length === 1"
            variant="text"
            size="small"
            class="ms-1 mt-1"
            @click="removeRedirectUri(index)"
          />
        </div>
        <v-btn
          :prepend-icon="$globals.icons.createAlt"
          :disabled="form.redirectUris.length >= MAX_REDIRECT_URIS"
          variant="text"
          size="small"
          @click="addRedirectUri"
        >
          {{ $t("mcp.add-redirect-uri") }}
        </v-btn>
        <p class="text-caption text-medium-emphasis mt-1">
          {{ $t("mcp.redirect-uris-hint") }}
        </p>
      </fieldset>

      <v-checkbox
        v-if="clientIsConfidential"
        v-model="form.pkceOptional"
        :label="$t('mcp.pkce-optional')"
        :hint="$t('mcp.pkce-optional-hint')"
        persistent-hint
        density="compact"
        class="mb-2"
      />
      <v-checkbox
        v-model="form.allowWriteScope"
        :label="$t('mcp.allow-write-scope')"
        :hint="$t('mcp.allow-write-scope-hint')"
        persistent-hint
        density="compact"
      />

      <v-alert
        v-if="failed"
        ref="failureAlert"
        type="error"
        density="compact"
        variant="tonal"
        class="mt-4"
      >
        {{ failureReason || $t("mcp.save-failed") }}
      </v-alert>
    </v-card-text>
  </BaseDialog>
</template>

<script setup lang="ts">
import {
  MAX_CLIENT_NAME_LENGTH,
  MAX_REDIRECT_URIS,
  clientFormFrom,
  emptyClientForm,
  isClientFormValid,
  redirectUriProblem,
  useMcpClientEditor,
  type McpClientForm,
} from "~/composables/use-mcp";
import type { ComponentPublicInstance } from "vue";
import type { McpClientCreate, McpClientCreated, McpClientOut } from "~/lib/api/types/mcp";

type Preset = "home-assistant" | "other";

/** Adds an OAuth client (from a preset), or edits `client` */
const props = withDefaults(defineProps<{
  client?: McpClientOut | null;
}>(), {
  client: null,
});

const emit = defineEmits<{
  (e: "created", client: McpClientCreated): void;
  (e: "updated", client: McpClientOut): void;
}>();

const dialog = defineModel<boolean>({ default: false });

const i18n = useI18n();
const { saving, failed, failureReason, create, update, homeAssistantPreset, clearError } = useMcpClientEditor();

const form = reactive<McpClientForm>(emptyClientForm());
const preset = ref<Preset>("home-assistant");
const homeAssistantUrl = ref("");
/** The Home Assistant address the API refused, and why */
const homeAssistantUrlRefusal = ref<{ url: string; message: string } | null>(null);
const presetLoading = ref(false);
const presetFailed = ref(false);
/** Counts preset requests, so only the latest one's preset is applied */
let presetRequest = 0;
/** The latest preset being loaded and applied, which saving waits for */
let presetApplied: Promise<void> = Promise.resolve();
let submitting = false;
const failureAlert = ref<ComponentPublicInstance | null>(null);

const isEdit = computed(() => !!props.client);
const clientIsConfidential = computed(() => props.client?.isConfidential ?? form.isConfidential);
const valid = computed(() => isClientFormValid(form));

/** Why the API refused the address entered, while it's still the one entered */
const homeAssistantUrlRefused = computed(() => {
  const refusal = homeAssistantUrlRefusal.value;
  return refusal && refusal.url === homeAssistantUrl.value.trim() ? refusal.message : null;
});

/** A Home Assistant address that can't be used keeps the client from being added with the wrong redirect URI */
const homeAssistantUrlInvalid = computed(() => !isEdit.value
  && preset.value === "home-assistant"
  && (homeAssistantUrlRule(homeAssistantUrl.value) !== true || !!homeAssistantUrlRefused.value));

function setForm(value: McpClientForm) {
  Object.assign(form, value);
}

watch(dialog, (open) => {
  if (!open) {
    return;
  }

  clearError();
  presetFailed.value = false;
  homeAssistantUrl.value = "";
  homeAssistantUrlRefusal.value = null;
  if (props.client) {
    setForm(clientFormFrom(props.client));
  }
  else {
    preset.value = "home-assistant";
    applyPreset();
  }
}, { immediate: true });

/** Loads Home Assistant's preset for `url` and applies it, unless a newer request replaced it */
function loadHomeAssistantPreset(url: string, apply: (data: McpClientCreate) => void) {
  const request = ++presetRequest;
  presetLoading.value = true;
  presetApplied = (async () => {
    try {
      const { preset: data, refusal } = await homeAssistantPreset(url);
      if (request !== presetRequest) {
        return;
      }
      presetFailed.value = !data && !refusal;
      homeAssistantUrlRefusal.value = refusal ? { url: url.trim(), message: refusal } : null;
      if (data && preset.value === "home-assistant") {
        apply(data);
      }
    }
    finally {
      if (request === presetRequest) {
        presetLoading.value = false;
      }
    }
  })();
}

function selectPreset(value: unknown) {
  if (value === "home-assistant" || value === "other") {
    preset.value = value;
    // Each preset starts afresh; the address only goes with Home Assistant's
    homeAssistantUrl.value = "";
    homeAssistantUrlRefusal.value = null;
    applyPreset();
  }
}

function applyPreset() {
  presetFailed.value = false;
  setForm(emptyClientForm());
  if (preset.value === "other") {
    presetRequest++;
    presetLoading.value = false;
    presetApplied = Promise.resolve();
    return;
  }

  loadHomeAssistantPreset(homeAssistantUrl.value, data => setForm(clientFormFrom(data)));
}

/** Points the second redirect URI at the Home Assistant address entered */
function applyHomeAssistantUrl() {
  if (homeAssistantUrlRule(homeAssistantUrl.value) !== true) {
    return;
  }

  loadHomeAssistantPreset(homeAssistantUrl.value, (data) => {
    form.redirectUris = [...data.redirectUris];
  });
}

function nameRule(value: string) {
  if (!value?.trim()) {
    return i18n.t("validators.required");
  }
  return value.trim().length <= MAX_CLIENT_NAME_LENGTH
    || i18n.t("mcp.name-too-long", { max: MAX_CLIENT_NAME_LENGTH });
}

function redirectUriRule(value: string) {
  // Blank fields are left out; at least one redirect URI is needed to save
  const problem = value?.trim() ? redirectUriProblem(value) : null;
  return problem ? i18n.t(`mcp.redirect-uri-${problem}`) : true;
}

function homeAssistantUrlRule(value: string) {
  const trimmed = value?.trim();
  return !trimmed || /^https?:\/\//.test(trimmed) || i18n.t("mcp.redirect-uri-scheme");
}

function addRedirectUri() {
  if (form.redirectUris.length < MAX_REDIRECT_URIS) {
    form.redirectUris.push("");
  }
}

function removeRedirectUri(index: number) {
  if (form.redirectUris.length > 1) {
    form.redirectUris.splice(index, 1);
  }
}

async function handleSubmit() {
  if (submitting) {
    return;
  }

  submitting = true;
  try {
    // Leaving the Home Assistant address field by clicking Create loads its preset first: save what it gives
    await presetApplied;
    if (!valid.value || saving.value || presetLoading.value || homeAssistantUrlInvalid.value) {
      return;
    }

    if (props.client) {
      const updated = await update(props.client, form);
      if (updated) {
        emit("updated", updated);
        dialog.value = false;
      }
    }
    else {
      const created = await create(form);
      if (created) {
        emit("created", created);
        dialog.value = false;
      }
    }

    if (failed.value) {
      await showFailure();
    }
  }
  finally {
    submitting = false;
  }
}

/** The reason is below the form, which may be scrolled up: bring it into view */
async function showFailure() {
  await nextTick();
  (failureAlert.value?.$el as HTMLElement | undefined)?.scrollIntoView?.({ block: "nearest" });
}
</script>

<style scoped>
.redirect-uris {
  border: none;
  padding: 0;
  margin: 0;
}
</style>
