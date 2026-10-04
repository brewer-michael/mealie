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
          <!-- no card reader has run lately (AI_INGEST_WORKER off, or it stopped): cards and inbox photos wait -->
          <v-alert
            v-if="!disabled && settings.readerRunning === false"
            type="warning"
            density="compact"
            variant="tonal"
            class="mb-4 reader-not-running"
          >
            {{ $t("recipe-ingest.settings.reader-not-running") }}
          </v-alert>
          <!-- cards are read, but an optional part of the read is skipped until the monthly limit resets -->
          <v-alert
            v-if="!disabled && settings.limitedFeatures?.length"
            type="info"
            density="compact"
            variant="tonal"
            class="mb-4 limited-features"
          >
            <div v-for="feature in settings.limitedFeatures" :key="feature" class="limited-feature">
              {{ $t(`recipe-ingest.settings.limited.${feature}`, { date: dateText(nextLimitReset()) }) }}
            </div>
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
          <!-- photos waiting in the household's folder and why, and the ones it refused lately -->
          <IngestInboxStatus class="mb-4" :settings="settings" />

          <!-- The notifiers page is behind the profile's "Show advanced features" -->
          <p class="text-body-2 mb-4 notifications">
            <nuxt-link v-if="advanced" to="/household/notifiers">
              {{ $t("recipe-ingest.settings.notifications") }}
            </nuxt-link>
            <template v-else>
              {{ $t("recipe-ingest.settings.notifications-advanced") }}
            </template>
          </p>
          <!-- notification links are built from BASE_URL: at its default (or another local address) a phone can't open them -->
          <v-alert
            v-if="!disabled && settings.baseUrlSet === false"
            type="warning"
            density="compact"
            variant="tonal"
            class="mb-4 base-url-unset"
          >
            {{ $t("recipe-ingest.settings.base-url-unset") }}
          </v-alert>
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
                <!-- found from the card when it was saved: shown, not edited -->
                <v-chip
                  v-for="tag in foundTags(evalCase)"
                  :key="tag"
                  size="x-small"
                  variant="outlined"
                  class="found-tag"
                >
                  {{ tagText(tag) }}
                </v-chip>
              </v-list-item-subtitle>
              <!-- what the reviewer says about the card, and whether they checked the recipe: saved at once -->
              <div class="d-flex align-center flex-wrap column-gap-4 eval-case-edit">
                <v-chip-group
                  :model-value="chosenTags(evalCase)"
                  multiple
                  column
                  selected-class="text-primary"
                  class="flex-grow-0 eval-tags"
                  :disabled="updating.has(evalCase.slug)"
                  @update:model-value="value => setTags(evalCase, value)"
                >
                  <v-chip
                    v-for="tag in EVAL_TAGS"
                    :key="tag"
                    :value="tag"
                    filter
                    size="small"
                    variant="outlined"
                    :class="`eval-tag eval-tag-${tag}`"
                  >
                    {{ tagText(tag) }}
                  </v-chip>
                </v-chip-group>
                <v-checkbox
                  :model-value="!!evalCase.verified"
                  :label="$t('recipe-ingest.eval.verified-chip')"
                  :disabled="updating.has(evalCase.slug)"
                  density="compact"
                  color="success"
                  hide-details
                  class="flex-grow-0 eval-verified"
                  @update:model-value="value => updateEvalCase(evalCase, { verified: !!value })"
                />
              </div>
              <p v-if="evalCase.notes" class="text-caption text-medium-emphasis eval-case-notes">
                {{ evalCase.notes }}
              </p>
              <template #append>
                <v-btn
                  :icon="$globals.icons.download"
                  :aria-label="$t('recipe-ingest.eval.download')"
                  :loading="downloading === evalCase.slug"
                  variant="text"
                  size="small"
                  class="download-eval-case"
                  @click="downloadEvalCase(evalCase.slug)"
                />
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
import IngestInboxStatus from "~/components/Domain/Ingest/IngestInboxStatus.vue";
import { useUserApi } from "~/composables/api";
import { useGroupSelf } from "~/composables/use-groups";
import { useMealieAuth } from "~/composables/use-mealie-auth";
import {
  errorMessageOf,
  errorStatusOf,
  nextLimitReset,
  useRecipeIngestSettings,
  useRecipeIngestText,
} from "~/composables/use-recipe-ingest";
import { alert } from "~/composables/use-toast";
import type {
  EvalCaseSummary,
  EvalCaseTag,
  EvalCaseUpdate,
  RecipeIngestionSettingsUpdate,
} from "~/lib/api/types/recipe-ingest";

/**
 * Group Settings → Recipe cards (docs/ai/PHASE2.md §10, §11.6): keep cards on this server (with which local providers
 * would read them), the second reading, the household's inbox folder with the photos waiting there and the ones it
 * refused, a way to the notifiers page, and the group's eval cases, whose tags and "verified" save at once and which
 * download as a zip. It warns when nothing on the server reads cards, when notification links can't open on a phone
 * (BASE_URL), and notes optional parts of the read a monthly limit skips. The switches save at once; only group
 * managers change them. Fork-owned.
 */
const api = useUserApi();
const i18n = useI18n();
const { ingestErrorText, dateText } = useRecipeIngestText();
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

/** The tags a reviewer gives a case; the others (`sideways`, `two-sided`, `blank`) were found from the card */
const EVAL_TAGS: EvalCaseTag[] = ["handwritten", "printed", "faded"];
/** How long a downloaded zip's object URL is kept, so the browser has read it */
const DOWNLOAD_URL_LIFETIME_MS = 60_000;

const evalCases = ref<EvalCaseSummary[] | null>(null);
const evalCasesLoading = ref(false);
const evalCasesLoadFailed = ref(false);
const deleteDialogOpen = ref(false);
const deleteTarget = ref<string | null>(null);
/** Cases with a change being saved */
const updating = ref(new Set<string>());
/** The case whose zip is being fetched */
const downloading = ref<string | null>(null);

function isEvalTag(tag: string): tag is EvalCaseTag {
  return (EVAL_TAGS as string[]).includes(tag);
}

function chosenTags(evalCase: EvalCaseSummary): EvalCaseTag[] {
  return (evalCase.tags ?? []).filter(isEvalTag);
}

function foundTags(evalCase: EvalCaseSummary): string[] {
  return (evalCase.tags ?? []).filter(tag => !isEvalTag(tag));
}

/** A tag's name; one this page doesn't know shows as it is */
function tagText(tag: string): string {
  const key = `recipe-ingest.eval.tag-${tag}`;
  return i18n.te(key) ? i18n.t(key) : tag;
}

function replaceEvalCase(evalCase: EvalCaseSummary) {
  evalCases.value = evalCases.value?.map(item => (item.slug === evalCase.slug ? evalCase : item)) ?? null;
}

function markUpdating(slug: string, on: boolean) {
  const next = new Set(updating.value);
  if (on) {
    next.add(slug);
  }
  else {
    next.delete(slug);
  }
  updating.value = next;
}

/**
 * Saves a change to a case. It shows at once, and is undone if the save fails: a case deleted meanwhile leaves the
 * list, and a refusal the API client didn't already show (a restore running) says it couldn't be saved.
 */
async function updateEvalCase(evalCase: EvalCaseSummary, update: EvalCaseUpdate) {
  const before = evalCase;
  replaceEvalCase({
    ...evalCase,
    ...(update.verified !== undefined && update.verified !== null ? { verified: update.verified } : {}),
    ...(update.tags ? { tags: [...update.tags, ...foundTags(evalCase)] } : {}),
  });
  markUpdating(evalCase.slug, true);
  try {
    const { data, error } = await api.recipeIngest.updateEvalCase(evalCase.slug, update);
    if (data) {
      replaceEvalCase(data);
      return;
    }
    replaceEvalCase(before);
    if (errorStatusOf(error) === 404) {
      alert.error(i18n.t("recipe-ingest.eval.gone"));
      await loadEvalCases();
    }
    else if (!errorMessageOf(error)) {
      alert.error(i18n.t("recipe-ingest.eval.save-failed"));
    }
  }
  finally {
    markUpdating(evalCase.slug, false);
  }
}

function setTags(evalCase: EvalCaseSummary, value: unknown) {
  const tags = Array.isArray(value) ? value.filter((tag): tag is EvalCaseTag => typeof tag === "string" && isEvalTag(tag)) : [];
  // in the order the chips show
  void updateEvalCase(evalCase, { tags: EVAL_TAGS.filter(tag => tags.includes(tag)) });
}

/** Hands the browser a file to save */
function saveFile(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), DOWNLOAD_URL_LIFETIME_MS);
}

/** Saves a case's zip (its JSON and photos), to add to tests/data/cards or run the eval elsewhere */
async function downloadEvalCase(slug: string) {
  downloading.value = slug;
  try {
    const { data, error } = await api.recipeIngest.downloadEvalCase(slug);
    if (data) {
      saveFile(data, `${slug}.zip`);
      return;
    }
    const status = errorStatusOf(error);
    if (status === 404) {
      alert.error(i18n.t("recipe-ingest.eval.gone"));
      await loadEvalCases();
    }
    else {
      alert.error(status === 503 ? ingestErrorText("paused_for_restore") : i18n.t("recipe-ingest.eval.download-failed"));
    }
  }
  finally {
    downloading.value = null;
  }
}

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
    if (errorStatusOf(error) === 404 || errorMessageOf(error)) {
      // already gone, or a restore is running (503, whose message the API's toast shows): show what's there
      await loadEvalCases();
    }
    else {
      alert.error(i18n.t("recipe-ingest.eval.delete-failed"));
    }
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

<style scoped>
.eval-case + .eval-case {
  border-top: thin solid rgba(var(--v-border-color), var(--v-border-opacity));
}
</style>
