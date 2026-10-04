<template>
  <div>
    <v-card variant="outlined" style="border-color: lightgray;">
      <v-card-text>
        <BaseCardSectionTitle :title="$t('recipe-ingest.settings.title')" />
        <p class="text-body-2 mb-4">
          {{ $t("recipe-ingest.settings.description") }}
        </p>

        <!-- A failed refresh keeps showing the settings loaded before it -->
        <v-alert
          v-if="loadFailed"
          type="error"
          density="compact"
          variant="tonal"
          class="mb-4 load-failed"
        >
          <div class="d-flex align-center flex-wrap">
            <span class="me-2">{{ $t("recipe-ingest.settings.load-failed") }}</span>
            <v-btn class="ms-auto" variant="text" size="small" :loading="loading" @click="load">
              {{ $t("recipe-ingest.queue.retry") }}
            </v-btn>
          </div>
        </v-alert>
        <AppLoader v-if="!settings && loading" />

        <template v-if="settings">
          <!-- AI_INGEST_ENABLED is off: no provider would help, and the switches can't be saved -->
          <v-alert
            v-if="disabled"
            type="info"
            density="compact"
            variant="tonal"
            class="mb-4 disabled"
          >
            {{ $t("recipe-ingest.settings.disabled") }}
          </v-alert>
          <v-alert
            v-else-if="!settings.canReadCards"
            type="info"
            density="compact"
            variant="tonal"
            class="mb-4 cannot-read"
          >
            {{ $t("recipe-ingest.settings.cannot-read") }}
          </v-alert>
          <v-alert
            v-else-if="settings.limitReached"
            type="warning"
            density="compact"
            variant="tonal"
            class="mb-4 limit-reached"
          >
            {{ $t("recipe-ingest.settings.limit-reached") }}
          </v-alert>
          <p v-if="settings.ocrAvailable" class="text-caption text-medium-emphasis mb-2 ocr-available">
            {{ $t("recipe-ingest.settings.ocr-available") }}
          </p>

          <v-switch
            :model-value="localOnly"
            :label="$t('recipe-ingest.settings.local-only')"
            :hint="$t('recipe-ingest.settings.local-only-hint')"
            :disabled="!canManage || saving || disabled"
            persistent-hint
            color="primary"
            class="mb-2 local-only"
            @update:model-value="value => save({ localOnly: !!value })"
          />
          <div v-if="readiness" class="ms-4 mb-4 readiness">
            <div class="text-subtitle-2 mt-2">
              {{ $t("recipe-ingest.settings.readiness-title") }}
            </div>
            <ul class="text-body-2 ms-6 readiness-slots">
              <li v-for="slot in readinessSlots" :key="slot.key" :class="`readiness-${slot.key}`">
                {{ $t(`recipe-ingest.settings.readiness-${slot.key}`, { names: slot.names }) }}
              </li>
            </ul>
            <v-alert
              v-if="!settings.localOnlyAvailable"
              type="warning"
              density="compact"
              variant="tonal"
              class="mt-2 readiness-warning"
            >
              {{ $t("recipe-ingest.settings.readiness-warning") }}
            </v-alert>
            <v-alert
              v-if="readiness.notPrivate?.length"
              type="warning"
              density="compact"
              variant="tonal"
              class="mt-2 not-private"
            >
              {{ $t("recipe-ingest.settings.not-private", { names: readiness.notPrivate.join(", ") }) }}
            </v-alert>
          </div>

          <v-switch
            :model-value="crossRead"
            :label="$t('recipe-ingest.settings.cross-read')"
            :hint="$t('recipe-ingest.settings.cross-read-hint')"
            :disabled="!canManage || saving || disabled"
            persistent-hint
            color="primary"
            class="mb-4 cross-read"
            @update:model-value="value => save({ crossRead: !!value })"
          />

          <div class="text-subtitle-2">
            {{ $t("recipe-ingest.settings.inbox-title") }}
          </div>
          <p class="text-body-2 mb-4 inbox">
            <template v-if="settings.inbox?.enabled && settings.inbox.folder">
              {{ $t("recipe-ingest.settings.inbox-hint", { folder: settings.inbox.folder }) }}
            </template>
            <template v-else>
              {{ $t("recipe-ingest.settings.inbox-off") }}
            </template>
          </p>

          <!-- The notifiers page is behind the profile's "Show advanced features" -->
          <p class="text-body-2 mb-4 notifications">
            <nuxt-link v-if="advanced" to="/household/notifiers">
              {{ $t("recipe-ingest.settings.notifications") }}
            </nuxt-link>
            <template v-else>
              {{ $t("recipe-ingest.settings.notifications-advanced") }}
            </template>
          </p>
        </template>

        <template v-if="canManage">
          <BaseCardSectionTitle :title="$t('recipe-ingest.eval.list-title')" size="medium" />
          <v-alert
            v-if="evalCasesLoadFailed"
            type="error"
            density="compact"
            variant="tonal"
            class="mb-4 eval-load-failed"
          >
            <div class="d-flex align-center flex-wrap">
              <span class="me-2">{{ $t("recipe-ingest.eval.load-failed") }}</span>
              <v-btn class="ms-auto" variant="text" size="small" :loading="evalCasesLoading" @click="loadEvalCases">
                {{ $t("recipe-ingest.queue.retry") }}
              </v-btn>
            </div>
          </v-alert>
          <p v-else-if="evalCases && !evalCases.length" class="text-body-2 text-medium-emphasis no-eval-cases">
            {{ $t("recipe-ingest.eval.empty") }}
          </p>
          <v-list v-if="evalCases?.length" density="compact" class="py-0 eval-cases">
            <v-list-item v-for="evalCase in evalCases" :key="evalCase.slug" class="px-0 eval-case">
              <v-list-item-title class="eval-case-name">
                {{ evalCase.name || evalCase.slug }}
              </v-list-item-title>
              <v-list-item-subtitle class="d-flex align-center flex-wrap ga-2 mt-1">
                <code>{{ evalCase.slug }}</code>
                <span>{{ $t("recipe-ingest.eval.pages", evalCase.pageCount ?? 0) }}</span>
                <v-chip v-if="evalCase.verified" size="x-small" color="success" variant="tonal" class="verified">
                  {{ $t("recipe-ingest.eval.verified-chip") }}
                </v-chip>
              </v-list-item-subtitle>
              <template #append>
                <v-btn
                  :icon="$globals.icons.delete"
                  :aria-label="$t('recipe-ingest.eval.delete')"
                  variant="text"
                  size="small"
                  color="error"
                  class="delete-eval-case"
                  @click="confirmDelete(evalCase.slug)"
                />
              </template>
            </v-list-item>
          </v-list>
        </template>
      </v-card-text>
    </v-card>

    <BaseDialog
      v-model="deleteDialogOpen"
      bottom-sheet
      :title="$t('recipe-ingest.eval.delete')"
      color="error"
      :icon="$globals.icons.alertCircle"
      can-confirm
      @confirm="deleteEvalCase"
    >
      <v-card-text>
        {{ $t("recipe-ingest.eval.delete-confirm", { slug: deleteTarget ?? "" }) }}
      </v-card-text>
    </BaseDialog>
  </div>
</template>

<script setup lang="ts">
import { useUserApi } from "~/composables/api";
import { useGroupSelf } from "~/composables/use-groups";
import { useMealieAuth } from "~/composables/use-mealie-auth";
import { useRecipeIngestSettings } from "~/composables/use-recipe-ingest";
import { alert } from "~/composables/use-toast";
import type { EvalCaseSummary, RecipeIngestionSettingsUpdate } from "~/lib/api/types/recipe-ingest";

/**
 * Group Settings → Recipe cards (docs/ai/PHASE2.md §10, §11.6): keep cards on this server (with which local providers
 * would read them), the second reading, the household's inbox folder, a way to the notifiers page, and the group's
 * eval cases. The switches save at once; only group managers change them. Fork-owned.
 */
const api = useUserApi();
const i18n = useI18n();
const auth = useMealieAuth();
const { group } = useGroupSelf();
const { settings, loading, loadFailed, saving, load, save: saveSettings } = useRecipeIngestSettings();

const canManage = computed(() => !!auth.user.value?.canManage);
/** Recipe card scanning is turned off on this server (`AI_INGEST_ENABLED`) */
const disabled = computed(() => settings.value?.enabled === false);
const advanced = computed(() => !!auth.user.value?.advanced);

// What the switches show: the requested value while it's saved, the saved one after
const localOnly = ref(false);
const crossRead = ref(false);
watch(settings, (value) => {
  localOnly.value = value?.localOnly ?? false;
  crossRead.value = value?.crossRead ?? false;
}, { immediate: true });

const readiness = computed(() => settings.value?.localReadiness ?? null);
const readinessSlots = computed(() => {
  const none = i18n.t("recipe-ingest.settings.readiness-none");
  const names = (list: string[] | undefined) => (list?.length ? list.join(", ") : none);
  return [
    { key: "image", names: names(readiness.value?.image) },
    { key: "default", names: names(readiness.value?.default) },
    { key: "fast", names: names(readiness.value?.fast) },
  ];
});

async function save(change: Partial<RecipeIngestionSettingsUpdate>) {
  const update: RecipeIngestionSettingsUpdate = { localOnly: localOnly.value, crossRead: crossRead.value, ...change };
  localOnly.value = !!update.localOnly;
  crossRead.value = !!update.crossRead;

  if (await saveSettings(update)) {
    alert.success(i18n.t("recipe-ingest.settings.saved"));
  }
  else {
    localOnly.value = settings.value?.localOnly ?? false;
    crossRead.value = settings.value?.crossRead ?? false;
    alert.error(i18n.t("recipe-ingest.settings.save-failed"));
  }
}

// ==========================================
// Eval cases (group managers)

const evalCases = ref<EvalCaseSummary[] | null>(null);
const evalCasesLoading = ref(false);
const evalCasesLoadFailed = ref(false);
const deleteDialogOpen = ref(false);
const deleteTarget = ref<string | null>(null);

async function loadEvalCases() {
  evalCasesLoading.value = true;
  try {
    const { data } = await api.recipeIngest.getEvalCases();
    evalCasesLoadFailed.value = !data;
    if (data) {
      evalCases.value = data;
    }
  }
  finally {
    evalCasesLoading.value = false;
  }
}

function confirmDelete(slug: string) {
  deleteTarget.value = slug;
  deleteDialogOpen.value = true;
}

async function deleteEvalCase() {
  const slug = deleteTarget.value;
  if (!slug) {
    return;
  }

  const { error } = await api.recipeIngest.deleteEvalCase(slug);
  if (error) {
    // already gone (404), or a restore is running (503, whose message the API's toast shows): show what's there
    await loadEvalCases();
    return;
  }
  evalCases.value = evalCases.value?.filter(evalCase => evalCase.slug !== slug) ?? null;
  alert.success(i18n.t("recipe-ingest.eval.deleted"));
}

onMounted(() => {
  load();
  if (canManage.value) {
    loadEvalCases();
  }
});

// The group page refreshes its AI settings whenever a provider changes ("Runs on my network" included): what can
// read cards, and locally, changes with them
watch(() => group.value?.aiProviderSettings, (now, before) => {
  if (before !== undefined) {
    load();
  }
});
</script>
