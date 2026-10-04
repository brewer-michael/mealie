<template>
  <section class="notifier-ai-events">
    <h4>{{ $t("recipe-ingest.settings.title") }}</h4>
    <v-alert
      v-if="loadFailed"
      type="error"
      density="compact"
      variant="tonal"
      class="my-2 load-failed"
    >
      <div class="d-flex align-center flex-wrap">
        <span class="me-2">{{ $t("recipe-ingest.notifier.load-failed") }}</span>
        <v-btn class="ms-auto" variant="text" size="small" :loading="loading" @click="load">
          {{ $t("recipe-ingest.queue.retry") }}
        </v-btn>
      </div>
    </v-alert>
    <!-- the switch is the fork's own option, saved on its own (the options above wait for Save): its hint says so -->
    <v-switch
      v-else
      :model-value="recipeCardsReady"
      :label="$t('recipe-ingest.notifier.recipe-cards-ready')"
      :messages="[$t('recipe-ingest.notifier.recipe-cards-ready-hint'), $t('recipe-ingest.notifier.saved-at-once')]"
      :disabled="!loaded || saving"
      :loading="saving"
      color="primary"
      density="compact"
      class="recipe-cards-ready"
      @update:model-value="save"
    />
    <!-- the links in these notifications are built from BASE_URL: at its default they open nothing on a phone -->
    <v-alert
      v-if="showBaseUrlWarning"
      type="warning"
      density="compact"
      variant="tonal"
      class="mt-2 base-url-unset"
    >
      {{ $t("recipe-ingest.settings.base-url-unset") }}
    </v-alert>
    <v-btn
      variant="text"
      size="small"
      class="mt-2 send-test"
      :prepend-icon="$globals.icons.testTube"
      :loading="testing"
      @click="sendTest"
    >
      {{ $t("recipe-ingest.notifier.send-test") }}
    </v-btn>
    <!-- what the test did, under its button: on a page of notifiers, a toast wouldn't say which one -->
    <v-alert
      v-if="testResult"
      :type="testResult.ok ? 'success' : 'error'"
      density="compact"
      variant="tonal"
      closable
      class="mt-1 test-result"
      @click:close="testResult = null"
    >
      {{ testResult.text }}
    </v-alert>
  </section>
</template>

<script setup lang="ts">
import { useUserApi } from "~/composables/api";
import { errorCodeOf, errorStatusOf, useRecipeIngestSettings, useRecipeIngestText } from "~/composables/use-recipe-ingest";
import { alert } from "~/composables/use-toast";

/**
 * A household notifier's "Recipe cards ready to review" switch and its test notification (docs/ai/PHASE2.md §8), on
 * the notifiers page under each notifier's options. The switch is the fork's own option, saved on its own, and says
 * so. The test's outcome shows under its button: sent, or why it failed (the notifier didn't get it: 502
 * `notification_failed`). While BASE_URL is left at a local address, it warns that the links won't open on a phone.
 * Fork-owned.
 */
const props = defineProps<{
  notifierId: string;
}>();

const api = useUserApi();
const i18n = useI18n();
const { ingestErrorText } = useRecipeIngestText();
const ingestSettings = useRecipeIngestSettings();

const recipeCardsReady = ref(false);
const loaded = ref(false);
const loading = ref(false);
const loadFailed = ref(false);
const saving = ref(false);
const testing = ref(false);
/** What the last test did */
const testResult = ref<{ ok: boolean; text: string } | null>(null);

/**
 * BASE_URL is the server's default or a local address, so the notification's link won't open on a phone: said while
 * the switch is on, or once a test was sent (its link is built the same way)
 */
const showBaseUrlWarning = computed(() => {
  const settings = ingestSettings.settings.value;
  return !!settings && settings.enabled !== false && settings.baseUrlSet === false
    && (recipeCardsReady.value || testResult.value !== null);
});

async function load() {
  loading.value = true;
  try {
    const { data } = await api.recipeIngest.getNotifierEvents(props.notifierId);
    loadFailed.value = !data;
    if (data) {
      recipeCardsReady.value = data.recipeIngestionReady ?? false;
      loaded.value = true;
    }
  }
  finally {
    loading.value = false;
  }
}

async function save(value: boolean | null) {
  const wanted = !!value;
  const previous = recipeCardsReady.value;
  recipeCardsReady.value = wanted;
  saving.value = true;
  try {
    const { data } = await api.recipeIngest.updateNotifierEvents(props.notifierId, { recipeIngestionReady: wanted });
    if (data) {
      recipeCardsReady.value = data.recipeIngestionReady ?? wanted;
    }
    else {
      recipeCardsReady.value = previous;
      alert.error(i18n.t("recipe-ingest.notifier.save-failed"));
    }
  }
  finally {
    saving.value = false;
  }
}

/**
 * Why a test failed: the code's text (`notification_failed`: the notifier didn't get it; `not_found`: the notifier was
 * deleted meanwhile), else the HTTP status
 */
function testFailure(error: unknown): string {
  const code = errorCodeOf(error);
  if (code === "not_found") {
    return i18n.t("recipe-ingest.notifier.gone");
  }
  if (code) {
    return ingestErrorText(code);
  }
  const status = errorStatusOf(error);
  // no answer at all: the server couldn't be reached
  return status === null ? i18n.t("recipe-ingest.error.network") : i18n.t("recipe-ingest.error.unknown", { code: status });
}

/** Sends a test; quietly, so its outcome shows once, under the button */
async function sendTest() {
  testing.value = true;
  testResult.value = null;
  try {
    const { error } = await api.recipeIngest.testNotifierEvents(props.notifierId, { suppressAlert: true });
    testResult.value = error
      ? { ok: false, text: i18n.t("recipe-ingest.notifier.test-failed", { reason: testFailure(error) }) }
      : { ok: true, text: i18n.t("recipe-ingest.notifier.test-sent") };
  }
  finally {
    testing.value = false;
  }
}

watch(() => props.notifierId, () => {
  loaded.value = false;
  testResult.value = null;
  load();
});

onMounted(() => {
  load();
  // whether BASE_URL is set comes with the group's card settings (the layout loads them; they may not have yet)
  if (!ingestSettings.loaded.value) {
    void ingestSettings.load();
  }
});
</script>
