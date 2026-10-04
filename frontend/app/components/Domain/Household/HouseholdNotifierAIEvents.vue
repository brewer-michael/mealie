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
    <!-- Saved as soon as it's switched, unlike the options above, which wait for Save -->
    <v-switch
      v-else
      :model-value="recipeCardsReady"
      :label="$t('recipe-ingest.notifier.recipe-cards-ready')"
      :hint="$t('recipe-ingest.notifier.recipe-cards-ready-hint')"
      :disabled="!loaded || saving"
      :loading="saving"
      persistent-hint
      color="primary"
      density="compact"
      class="recipe-cards-ready"
      @update:model-value="save"
    />
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
  </section>
</template>

<script setup lang="ts">
import { useUserApi } from "~/composables/api";
import { errorMessageOf } from "~/composables/use-recipe-ingest";
import { alert } from "~/composables/use-toast";

/**
 * A household notifier's "Recipe cards ready to review" switch and its test notification (docs/ai/PHASE2.md §8), on
 * the notifiers page under each notifier's options. The switch is the fork's own option, saved on its own. Fork-owned.
 */
const props = defineProps<{
  notifierId: string;
}>();

const api = useUserApi();
const i18n = useI18n();

const recipeCardsReady = ref(false);
const loaded = ref(false);
const loading = ref(false);
const loadFailed = ref(false);
const saving = ref(false);
const testing = ref(false);

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

async function sendTest() {
  testing.value = true;
  try {
    const { error } = await api.recipeIngest.testNotifierEvents(props.notifierId);
    if (error) {
      // a notifier that didn't get it (502 `notification_failed`): the API client already showed the server's message
      if (!errorMessageOf(error)) {
        alert.error(i18n.t("recipe-ingest.notifier.test-failed"));
      }
    }
    else {
      alert.success(i18n.t("recipe-ingest.notifier.test-sent"));
    }
  }
  finally {
    testing.value = false;
  }
}

watch(() => props.notifierId, () => {
  loaded.value = false;
  load();
});

onMounted(load);
</script>
