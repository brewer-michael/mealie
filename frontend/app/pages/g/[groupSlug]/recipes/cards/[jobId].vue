<template>
  <div
    class="ingest-review"
    :class="{ 'ingest-review--phone': !$vuetify.display.mdAndUp }"
    :style="barHeight ? { '--ingest-review-bar-height': `${barHeight}px` } : undefined"
  >
    <div v-if="review.loadState.value === 'loading'" class="d-flex justify-center pa-8">
      <AppLoader />
    </div>

    <v-container v-else-if="review.loadState.value !== 'ready' || !job" class="ingest-review__missing">
      <v-alert type="warning" variant="tonal">
        {{ review.loadState.value === "not-found" ? ingestErrorText("not_found") : $t("recipe-ingest.review.load-failed") }}
      </v-alert>
      <div class="d-flex flex-wrap ga-2 mt-4">
        <!-- a phone that lost its signal for a moment can load the card again where it is -->
        <v-btn
          v-if="review.loadState.value === 'failed'"
          class="ingest-review__try-again"
          color="primary"
          :prepend-icon="$globals.icons.refresh"
          @click="review.load()"
        >
          {{ $t("recipe-ingest.review.try-again") }}
        </v-btn>
        <v-btn variant="text" :to="queuePath">
          {{ $t("recipe-ingest.queue.title") }}
        </v-btn>
      </div>
    </v-container>

    <template v-else>
      <!-- header: where this card is in its batch, previous/next and the ⋯ menu -->
      <div class="ingest-review__header d-flex align-center ga-1 px-2 py-1">
        <v-btn
          icon
          variant="text"
          size="small"
          :disabled="!position?.previous"
          :aria-label="$t('recipe-ingest.review.previous')"
          @click="position?.previous && review.goTo(position.previous)"
        >
          <v-icon :icon="$globals.icons.chevronLeft" />
        </v-btn>
        <div class="flex-grow-1 text-center text-subtitle-2 ingest-review__position">
          {{ headerText }}
          <v-chip
            v-if="job.localOnly"
            class="ml-1"
            size="x-small"
            label
            :prepend-icon="$globals.icons.lock"
          >
            {{ $t("recipe-ingest.review.local-only") }}
          </v-chip>
        </div>
        <v-btn
          icon
          variant="text"
          size="small"
          :disabled="!position?.next"
          :aria-label="$t('recipe-ingest.review.next')"
          @click="position?.next && review.goTo(position.next)"
        >
          <v-icon :icon="$globals.icons.chevronRight" />
        </v-btn>
        <v-menu location="bottom end">
          <template #activator="{ props: menuProps }">
            <v-btn
              v-bind="menuProps"
              icon
              variant="text"
              size="small"
              :aria-label="$t('recipe-ingest.review.more')"
            >
              <v-icon :icon="$globals.icons.dotsVertical" />
            </v-btn>
          </template>
          <v-list density="compact" class="ingest-review__menu">
            <v-list-item
              :prepend-icon="$globals.icons.rotateRight"
              :title="$t('recipe-ingest.review.rotate')"
              :disabled="!canRotate"
              @click="rotateCurrent"
            />
            <v-list-item
              :prepend-icon="$globals.icons.refresh"
              :title="$t('recipe-ingest.review.read-again')"
              :disabled="!canReextract"
              @click="review.reextract()"
            />
            <!-- phones have no toolbar: a line the reading missed, or a field with no flag, is re-read from here -->
            <v-list-item
              :prepend-icon="mdiCropFree"
              :title="$t('recipe-ingest.review.reread-title')"
              :disabled="!canReread"
              @click="openReread()"
            />
            <v-list-item
              :prepend-icon="mdiTextRecognition"
              :title="$t('recipe-ingest.review.what-the-card-says')"
              @click="transcriptionDialog = true"
            />
            <v-list-item
              v-if="job.permissions?.canExportEval"
              :prepend-icon="$globals.icons.testTube"
              :title="$t('recipe-ingest.review.save-eval')"
              :disabled="!canExportEval"
              @click="evalDialog = true"
            />
            <v-list-item
              v-if="job.permissions?.canDiscard"
              :prepend-icon="$globals.icons.delete"
              :title="$t('recipe-ingest.review.discard')"
              :disabled="job.status === 'committing' || job.status === 'committed'"
              @click="discardDialog = true"
            />
          </v-list>
        </v-menu>
      </div>

      <v-row no-gutters>
        <v-col cols="12" md="5" class="ingest-review__viewer">
          <IngestCardViewer
            v-model:page="pageIndex"
            v-model:transcription-open="transcriptionPanel"
            :mode="$vuetify.display.mdAndUp ? 'panel' : 'strip'"
            :pages="job.pages ?? []"
            :transcription="job.transcription"
            :readonly="!canRotate"
            :can-reread="canReread"
            :rotating="review.pendingAction.value === 'rotate'"
            @rotate="pageNumber => review.rotate(pageNumber)"
            @reread="openReread()"
          />
        </v-col>

        <v-col cols="12" md="7" class="ingest-review__editor">
          <div class="pa-3">
            <!-- a card that isn't ready to review -->
            <template v-if="job.status === 'processing'">
              <v-alert type="info" variant="tonal" class="ingest-review__status">
                {{ progressLabel }}
                <v-progress-linear indeterminate class="mt-2" />
                <template v-if="canCancel" #append>
                  <v-btn
                    class="ingest-review__cancel"
                    size="small"
                    variant="text"
                    :loading="review.pendingAction.value === 'cancel'"
                    @click="review.cancelTask()"
                  >
                    {{ $t("general.cancel") }}
                  </v-btn>
                </template>
              </v-alert>
            </template>
            <template v-else-if="job.status === 'failed'">
              <v-alert type="error" variant="tonal" class="ingest-review__status">
                {{ $t("recipe-ingest.queue.failed", { reason: job.error ? ingestErrorText(job.error.code, job.error.params) : "" }) }}
              </v-alert>
              <div class="d-flex ga-2 mt-3">
                <v-btn
                  color="primary"
                  :loading="review.pendingAction.value === 'retry'"
                  @click="review.retry()"
                >
                  {{ $t("recipe-ingest.queue.retry") }}
                </v-btn>
                <v-btn
                  v-if="job.permissions?.canDiscard"
                  variant="text"
                  color="error"
                  @click="discardDialog = true"
                >
                  {{ $t("recipe-ingest.queue.discard") }}
                </v-btn>
              </div>
            </template>
            <template v-else-if="job.status === 'committing'">
              <v-alert type="info" variant="tonal" class="ingest-review__status">
                {{ $t("recipe-ingest.progress.committing") }}
                <v-progress-linear indeterminate class="mt-2" />
              </v-alert>
            </template>
            <template v-else-if="job.status === 'committed'">
              <v-alert type="success" variant="tonal" class="ingest-review__status">
                {{ $t("recipe-ingest.queue.committed") }}
              </v-alert>
              <div class="d-flex ga-2 mt-3">
                <v-btn
                  v-if="job.recipe?.slug"
                  color="primary"
                  :to="`/g/${groupSlug}/r/${job.recipe.slug}`"
                >
                  {{ $t("recipe-ingest.queue.view-recipe") }}
                </v-btn>
                <v-btn variant="text" @click="review.skip()">
                  {{ $t("recipe-ingest.review.next") }}
                </v-btn>
              </div>
            </template>

            <!-- the review itself -->
            <template v-else>
              <p v-if="checksLine" class="text-body-2 text-medium-emphasis mb-2 ingest-review__checks">
                <v-icon
                  size="small"
                  class="mr-1"
                  :icon="job.read?.readPath === 'ocr' ? $globals.icons.alert : mdiTextRecognition"
                />
                {{ checksLine }}
              </p>
              <p
                v-for="info in cardInfos"
                :key="info.id"
                class="text-caption text-medium-emphasis mb-2 ingest-review__info"
              >
                <v-icon size="x-small" :icon="$globals.icons.informationOutline" />
                {{ info.title }}: {{ info.explanation }}
              </p>

              <v-alert
                v-if="review.conflict.value"
                type="warning"
                variant="tonal"
                class="mb-3 ingest-review__conflict"
              >
                {{ $t("recipe-ingest.review.reload-text") }}
                <template #append>
                  <v-btn size="small" variant="flat" color="warning" @click="review.reload()">
                    {{ $t("recipe-ingest.review.reload") }}
                  </v-btn>
                </template>
              </v-alert>
              <v-alert
                v-if="job.error"
                type="warning"
                variant="tonal"
                closable
                class="mb-3 ingest-review__error"
                @click:close="review.dismissError()"
              >
                {{ ingestErrorText(job.error.code, job.error.params) }}
              </v-alert>
              <v-alert
                v-if="review.task.value?.kind === 'extract'"
                type="info"
                variant="tonal"
                class="mb-3 ingest-review__reading"
              >
                {{ $t("recipe-ingest.review.read-only-while-reading") }}
                <div class="text-caption">
                  {{ progressLabel }}
                </div>
                <v-progress-linear indeterminate class="mt-2" />
                <template v-if="canCancel" #append>
                  <v-btn
                    class="ingest-review__cancel"
                    size="small"
                    variant="text"
                    :loading="review.pendingAction.value === 'cancel'"
                    @click="review.cancelTask()"
                  >
                    {{ $t("general.cancel") }}
                  </v-btn>
                </template>
              </v-alert>
              <v-alert
                v-else-if="review.task.value?.kind === 'reread' || review.rereadQueue.value.length"
                type="info"
                variant="tonal"
                density="compact"
                class="mb-3 ingest-review__rereading"
              >
                {{ progressLabel }}
                <template v-if="canCancel" #append>
                  <v-btn
                    class="ingest-review__cancel"
                    size="small"
                    variant="text"
                    :loading="review.pendingAction.value === 'cancel'"
                    @click="review.cancelTask()"
                  >
                    {{ $t("general.cancel") }}
                  </v-btn>
                </template>
              </v-alert>
              <v-alert
                v-if="job.duplicateOf"
                type="warning"
                variant="tonal"
                density="compact"
                class="mb-3 ingest-review__duplicate"
              >
                {{ $t("recipe-ingest.review.possible-duplicate", { name: job.duplicateOf.name || review.draft.value.name }) }}
                <template v-if="job.duplicateOf.slug" #append>
                  <v-btn
                    size="small"
                    variant="text"
                    :to="`/g/${groupSlug}/r/${job.duplicateOf.slug}`"
                  >
                    {{ $t("recipe-ingest.review.view-duplicate") }}
                  </v-btn>
                </template>
              </v-alert>

              <IngestNeedsALook
                class="mb-3"
                :items="review.needsALook.value"
                :readonly="review.readOnly.value"
                :can-reread="canReread"
                @alternative="(flag, text) => review.applyFlagAlternative(flag, text)"
                @fill="(flag, value) => review.fillFlagBlank(flag, value)"
                @reread="flag => openReread(flag.field, flag.ref)"
                @edit="flag => revealField(flag.field, flag.ref, true)"
                @resolve="(flag, resolution) => review.resolveFlag(flag, resolution)"
                @use-proposal="(proposal, mode) => review.useProposal(proposal, mode)"
                @dismiss-proposal="proposal => review.dismissProposal(proposal)"
              />
              <IngestProposalBanner
                v-for="proposal in review.otherProposals.value"
                :key="proposal.id ?? proposal.createdAt"
                class="mb-3"
                :proposal="proposal"
                :label="proposal.target ? fieldLabel(i18nT, proposal.target.field) : null"
                :readonly="review.readOnly.value"
                @use="mode => review.useProposal(proposal, mode)"
                @dismiss="review.dismissProposal(proposal)"
              />

              <v-expansion-panels
                v-model="openSections"
                multiple
                variant="accordion"
                class="ingest-review__sections"
              >
                <v-expansion-panel value="recipe">
                  <v-expansion-panel-title>{{ $t("recipe-ingest.review.recipe") }}</v-expansion-panel-title>
                  <v-expansion-panel-text>
                    <IngestRecipeFields
                      v-model="review.draft.value"
                      :flags="review.openFlags.value"
                      :readonly="review.readOnly.value"
                    />
                  </v-expansion-panel-text>
                </v-expansion-panel>
                <v-expansion-panel value="ingredients">
                  <v-expansion-panel-title>
                    {{ $t("recipe-ingest.review.ingredients") }}
                  </v-expansion-panel-title>
                  <v-expansion-panel-text :id="fieldAnchorId('ingredients')">
                    <IngestIngredientList
                      v-model="review.draft.value"
                      v-model:expanded="expandedIngredient"
                      :flags="review.openFlags.value"
                      :info-flags="review.infoFlags.value"
                      :readonly="review.readOnly.value"
                      :can-create-foods="!!job.permissions?.canCreateFoods"
                      :can-reread="canReread"
                      :draggable="$vuetify.display.mdAndUp"
                      @reread="ref => openReread('ingredients', ref)"
                    />
                  </v-expansion-panel-text>
                </v-expansion-panel>
                <v-expansion-panel value="steps">
                  <v-expansion-panel-title>{{ $t("recipe-ingest.review.steps") }}</v-expansion-panel-title>
                  <v-expansion-panel-text :id="fieldAnchorId('steps')">
                    <IngestStepList
                      v-model="review.draft.value"
                      :flags="review.openFlags.value"
                      :readonly="review.readOnly.value"
                      :can-reread="canReread"
                      :draggable="$vuetify.display.mdAndUp"
                      @reread="ref => openReread('steps', ref)"
                    />
                  </v-expansion-panel-text>
                </v-expansion-panel>
                <v-expansion-panel value="organizers">
                  <v-expansion-panel-title>{{ $t("general.organizers") }}</v-expansion-panel-title>
                  <v-expansion-panel-text :id="fieldAnchorId('tags')">
                    <IngestOrganizerSelector
                      v-model="review.draft.value.tags"
                      selector-type="tags"
                      :readonly="review.readOnly.value"
                    />
                    <IngestOrganizerSelector
                      v-model="review.draft.value.categories"
                      selector-type="categories"
                      :readonly="review.readOnly.value"
                    />
                    <IngestOrganizerSelector
                      v-model="review.draft.value.tools"
                      selector-type="tools"
                      :readonly="review.readOnly.value"
                    />
                  </v-expansion-panel-text>
                </v-expansion-panel>
                <v-expansion-panel value="notes">
                  <v-expansion-panel-title>{{ $t("recipe-ingest.review.notes") }}</v-expansion-panel-title>
                  <v-expansion-panel-text :id="fieldAnchorId('notes')">
                    <RecipeNotes
                      v-model="review.draft.value.notes"
                      :edit="!review.readOnly.value"
                    />
                  </v-expansion-panel-text>
                </v-expansion-panel>
              </v-expansion-panels>

              <v-switch
                v-model="review.draft.value.useCardAsCover"
                class="mt-3 ingest-review__cover"
                color="primary"
                hide-details
                :disabled="review.readOnly.value"
                :label="$t('recipe-ingest.review.use-card-as-cover')"
              />
              <p v-if="job.householdRecipesPublic" class="text-caption text-medium-emphasis mt-1 ingest-review__public">
                <v-icon size="x-small" :icon="$globals.icons.informationOutline" />
                {{ $t("recipe-ingest.review.public-photo") }}
              </p>
            </template>
          </div>

          <IngestReviewBar
            v-if="job.status === 'ready' || review.notice.value"
            ref="reviewBar"
            :fixed="!$vuetify.display.mdAndUp"
            :actions="job.status === 'ready'"
            :notice="review.notice.value"
            :error-count="review.openErrors.value.length"
            :disabled="review.readOnly.value"
            :committing="review.committing.value"
            :save-state="review.saveState.value"
            @skip="review.skip()"
            @commit="commit"
            @fix="scrollToFirstError"
            @notice-action="review.runNoticeAction()"
            @notice-dismiss="review.dismissNotice()"
          />
        </v-col>
      </v-row>

      <IngestRegionDialog
        v-model="regionDialog"
        :pages="job.pages ?? []"
        :targets="regionTargets"
        :initial-page="pageIndex"
        :initial-target="regionTarget"
        @submit="request => review.requestReread(request)"
      />
      <IngestEvalCaseDialog
        v-model="evalDialog"
        v-model:exists="evalExists"
        :recipe-name="review.draft.value.name"
        :saving="review.pendingAction.value === 'eval'"
        @save="saveEvalCase"
      />
      <BaseDialog
        v-model="transcriptionDialog"
        :title="$t('recipe-ingest.review.what-the-card-says')"
        :icon="mdiTextRecognition"
        max-width="700"
      >
        <v-card-text>
          <IngestTranscription :text="job.transcription" />
        </v-card-text>
      </BaseDialog>
      <BaseDialog
        v-model="discardDialog"
        :title="$t('recipe-ingest.review.discard')"
        :icon="$globals.icons.delete"
        color="error"
        can-confirm
        @confirm="review.discard()"
      >
        <v-card-text>
          {{ $t("recipe-ingest.queue.discard-confirm") }}
        </v-card-text>
      </BaseDialog>
      <BaseDialog
        :model-value="review.conflict.value && conflictDialog"
        :title="$t('recipe-ingest.review.reload-title')"
        :icon="$globals.icons.alert"
        color="warning"
        @update:model-value="value => (conflictDialog = value)"
      >
        <v-card-text>
          {{ $t("recipe-ingest.review.reload-text") }}
        </v-card-text>
        <template #card-actions>
          <v-spacer />
          <v-btn
            color="warning"
            variant="flat"
            class="ingest-review__reload"
            @click="reload"
          >
            {{ $t("recipe-ingest.review.reload") }}
          </v-btn>
        </template>
      </BaseDialog>
    </template>

    <!-- leaving while the last changes couldn't be saved (offline) -->
    <BaseDialog
      :model-value="!!leaveTo"
      :title="$t('recipe-ingest.review.unsaved-title')"
      :icon="$globals.icons.alert"
      color="warning"
      @update:model-value="value => !value && (leaveTo = null)"
    >
      <v-card-text>
        {{ $t("recipe-ingest.review.unsaved-text") }}
      </v-card-text>
      <template #card-actions>
        <v-btn variant="text" class="ingest-review__stay" @click="leaveTo = null">
          {{ $t("recipe-ingest.review.stay") }}
        </v-btn>
        <v-spacer />
        <v-btn color="warning" variant="flat" class="ingest-review__leave" @click="leaveAnyway">
          {{ $t("recipe-ingest.review.leave") }}
        </v-btn>
      </template>
    </BaseDialog>
  </div>
</template>

<script setup lang="ts">
import { mdiCropFree, mdiTextRecognition } from "@mdi/js";
import { useActiveElement, useElementSize, useMagicKeys, whenever } from "@vueuse/core";
import IngestCardViewer from "~/components/Domain/Ingest/IngestCardViewer.vue";
import IngestEvalCaseDialog from "~/components/Domain/Ingest/IngestEvalCaseDialog.vue";
import IngestIngredientList from "~/components/Domain/Ingest/IngestIngredientList.vue";
import IngestNeedsALook from "~/components/Domain/Ingest/IngestNeedsALook.vue";
import IngestOrganizerSelector from "~/components/Domain/Ingest/IngestOrganizerSelector.vue";
import IngestProposalBanner from "~/components/Domain/Ingest/IngestProposalBanner.vue";
import IngestRecipeFields from "~/components/Domain/Ingest/IngestRecipeFields.vue";
import IngestRegionDialog from "~/components/Domain/Ingest/IngestRegionDialog.vue";
import IngestReviewBar from "~/components/Domain/Ingest/IngestReviewBar.vue";
import IngestStepList from "~/components/Domain/Ingest/IngestStepList.vue";
import IngestTranscription from "~/components/Domain/Ingest/IngestTranscription.vue";
import RecipeNotes from "~/components/Domain/Recipe/RecipeNotes.vue";
import { useRecipeIngestText, type TranslateFn } from "~/composables/use-recipe-ingest";
import {
  fieldAnchorId,
  fieldLabel,
  normalizeField,
  rereadTargets,
  rereadTargetValue,
  type RereadTargetOption,
} from "~/composables/use-recipe-ingest-review";
import { useRecipeIngestReview } from "~/composables/use-recipe-ingest-review";

/**
 * Reviewing one recipe card (docs/ai/PHASE2.md §6): the card beside (desktop) or above (phone) its draft, what needs a
 * look, and Commit & next. Each card remounts the page (`key`), so its state never leaks into the next one.
 */
definePageMeta({
  middleware: ["group-only"],
  key: route => route.fullPath,
});

const i18n = useI18n();
const route = useRoute();
const router = useRouter();
const { flagText, ingestErrorText, progressText } = useRecipeIngestText();
const i18nT: TranslateFn = (key, named) => i18n.t(key, named ?? {});

const groupSlug = computed(() => String(route.params.groupSlug ?? ""));
const jobId = String(route.params.jobId ?? "");

/** Where the review itself last went (Previous, Next, Commit & next): it replaces the route, so Back goes to the queue */
let reviewNavigation: string | null = null;

const review = useRecipeIngestReview(jobId, {
  groupSlug,
  navigate: (path) => {
    reviewNavigation = path;
    return router.replace(path);
  },
});
const job = review.job;
const position = review.position;

useSeoMeta({
  title: i18n.t("recipe-ingest.nav.recipe-cards"),
});

onMounted(() => {
  void review.load();
});

const queuePath = computed(() => review.queuePath());

// ==========================================
// Leaving: the last changes are saved first; if that fails, the page asks

const leaveTo = ref<string | null>(null);
let leaving = false;

async function beforeLeaving(to: { fullPath: string }) {
  if (leaving || (await review.saveBeforeLeaving())) {
    return true;
  }
  leaveTo.value = to.fullPath;
  return false;
}

// another page, or another card: the router counts a card's previous and next as the same page with a new id
onBeforeRouteLeave(beforeLeaving);
onBeforeRouteUpdate(beforeLeaving);

async function leaveAnyway() {
  const to = leaveTo.value;
  leaveTo.value = null;
  if (!to) {
    return;
  }
  leaving = true;
  await (to === reviewNavigation ? router.replace(to) : router.push(to));
}

// ==========================================
// The review bar: on phones it's pinned, so the page leaves room for it as it grows with a notice

const reviewBar = ref<{ $el: HTMLElement } | null>(null);
const { height: barHeight } = useElementSize(() => reviewBar.value?.$el ?? null, undefined, { box: "border-box" });

// ==========================================
// Header and checks line

const headerText = computed(() => {
  const parts: string[] = [];
  if (position.value) {
    parts.push(i18n.t("recipe-ingest.review.card-of", { number: position.value.number, total: position.value.total }));
  }
  if (review.toCheck.value > 0) {
    parts.push(i18n.t("recipe-ingest.review.to-check", { count: review.toCheck.value }));
  }
  return parts.join(" · ") || job.value?.title || "";
});

/** Which reader produced the draft, and whether a second reading checked it */
const checksLine = computed(() => {
  const read = job.value?.read;
  if (!read) {
    return null;
  }
  if (read.readPath === "ocr") {
    return i18n.t("recipe-ingest.review.read-with-ocr", { confidence: Math.round(read.ocrConfidence ?? 0) });
  }
  const name = read.provider || read.model;
  if (!name) {
    return null;
  }
  if (read.crossReadFailed) {
    return i18n.t("recipe-ingest.review.read-by-cross-read-failed", { name });
  }
  return read.crossRead
    ? i18n.t("recipe-ingest.review.read-by-cross-read", { name })
    : i18n.t("recipe-ingest.review.read-by", { name });
});

/** Infos about the card as a whole ("Ingredients kept as text"); a failed second reading is in the checks line */
const cardInfos = computed(() =>
  review.infoFlags.value
    .filter(flag => normalizeField(flag.field) === "card" && flag.kind !== "cross_read_failed")
    .map(flag => ({ id: flag.id, ...flagText(flag) })),
);

const progressLabel = computed(() => {
  const current = review.task.value;
  if (!current) {
    return review.rereadQueue.value.length ? i18n.t("recipe-ingest.review.reread-queued") : i18n.t("recipe-ingest.progress.queued");
  }
  if (current.cancelRequested) {
    return i18n.t("recipe-ingest.review.stopping");
  }
  if (current.state === "queued") {
    return i18n.t("recipe-ingest.progress.queued");
  }
  return progressText(current.progressKey)
    ?? (current.kind === "extract" ? i18n.t("recipe-ingest.review.reading-again") : i18n.t("recipe-ingest.progress.reading-card"));
});

// ==========================================
// The card's pages and the ⋯ menu

const pageIndex = ref(0);
const transcriptionPanel = ref(false);
const transcriptionDialog = ref(false);
const discardDialog = ref(false);
const evalDialog = ref(false);
const evalExists = ref(false);
const conflictDialog = ref(true);

const canRotate = computed(() =>
  (job.value?.status === "ready" || job.value?.status === "failed")
  && !review.task.value
  && !review.pendingAction.value,
);
const canReextract = computed(() => job.value?.status === "ready" && !review.task.value && !review.pendingAction.value);
/** An area can be re-read on a ready card the editor isn't locked on; while a re-read runs, more wait their turn */
const canReread = computed(() =>
  job.value?.status === "ready"
  && !review.readOnly.value
  && !review.pendingAction.value
  && !!job.value.pages?.length,
);
/** What reads the card can be stopped: a card being read, a re-read or re-extract, or re-reads waiting their turn */
const canCancel = computed(() =>
  (!!review.task.value || review.rereadQueue.value.length > 0)
  && !review.task.value?.cancelRequested
  && (job.value?.status === "processing" || job.value?.status === "ready"),
);
const canExportEval = computed(() => job.value?.status === "ready" || job.value?.status === "committed");

function rotateCurrent() {
  const page = job.value?.pages?.[pageIndex.value];
  if (page) {
    void review.rotate(page.index);
  }
}

async function saveEvalCase(value: { slug: string; verified: boolean }) {
  const result = await review.saveEvalCase(value.slug, value.verified);
  if (result === "saved") {
    evalDialog.value = false;
  }
  evalExists.value = result === "exists";
}

watch(review.conflict, (conflict) => {
  if (conflict) {
    conflictDialog.value = true;
  }
});

async function reload() {
  conflictDialog.value = false;
  await review.reload();
}

// ==========================================
// Re-read an area

const regionDialog = ref(false);
const regionTargets = ref<RereadTargetOption[]>([]);
const regionTarget = ref<string | null>(null);

/** Opens the region dialog, aimed at a field (and line) when opened from a flag or a line; else for the reviewer to say */
function openReread(field?: string, ref?: string | null) {
  if (!canReread.value) {
    return;
  }
  regionTargets.value = rereadTargets(review.draft.value);
  regionTarget.value = field ? rereadTargetValue(regionTargets.value, field, ref ?? null) : null;
  regionDialog.value = true;
}

// ==========================================
// Scrolling to fields

const SECTION_OF_FIELD: Record<string, string> = {
  ingredients: "ingredients",
  steps: "steps",
  notes: "notes",
  tags: "organizers",
  categories: "organizers",
  tools: "organizers",
};

const openSections = ref<string[]>(["recipe", "ingredients", "steps", "organizers", "notes"]);
const expandedIngredient = ref<string | null>(null);

function scrollToElement(id: string, focus: boolean) {
  const element = document.getElementById(id);
  if (!element) {
    return;
  }
  element.scrollIntoView({ behavior: "smooth", block: "center" });
  if (focus) {
    element.querySelector<HTMLElement>("input:not([type=hidden]), textarea")?.focus({ preventScroll: true });
  }
}

/** Opens the field's section (and ingredient row), then scrolls to it */
async function revealField(field: string, ref?: string | null, focus = false) {
  const key = normalizeField(field);
  if (key === "card") {
    return;
  }
  const section = SECTION_OF_FIELD[key] ?? "recipe";
  if (!openSections.value.includes(section)) {
    openSections.value = [...openSections.value, section];
  }
  if (key === "ingredients" && ref) {
    expandedIngredient.value = ref;
  }
  await nextTick();
  const exact = fieldAnchorId(key, ref);
  scrollToElement(document.getElementById(exact) ? exact : fieldAnchorId(key), focus);
}

async function scrollToFirstError() {
  const anchor = review.firstErrorAnchor.value;
  if (anchor) {
    await nextTick();
    scrollToElement(anchor, false);
  }
}

async function commit() {
  const result = await review.commit();
  if (result === "fix") {
    await scrollToFirstError();
  }
}

// ==========================================
// Keyboard (desktop): ignored while typing, except Ctrl/⌘+Enter

const keys = useMagicKeys();
const activeElement = useActiveElement();
const typing = computed(() => {
  const element = activeElement.value;
  return !!element && (["INPUT", "TEXTAREA", "SELECT"].includes(element.tagName) || element.isContentEditable);
});
const dialogOpen = computed(() =>
  regionDialog.value
  || evalDialog.value
  || discardDialog.value
  || transcriptionDialog.value
  || review.conflict.value
  || !!leaveTo.value,
);
const plainKeys = computed(() => !typing.value && !dialogOpen.value && job.value?.status === "ready");
const noModifier = () => !keys.ctrl!.value && !keys.meta!.value && !keys.alt!.value;

whenever(() => (keys["Ctrl+Enter"]!.value || keys["Meta+Enter"]!.value) && !dialogOpen.value, () => {
  void commit();
});

/** Alt+↓ / Alt+↑: the next or previous field still to check */
let flagCursor = -1;
function stepFlag(direction: 1 | -1) {
  const items = review.needsALook.value.filter(item => item.state === "open");
  if (!items.length) {
    return;
  }
  flagCursor = (flagCursor + direction + items.length) % items.length;
  const flag = items[flagCursor]!.flag;
  if (flag.field === "card") {
    scrollToElement(items[flagCursor]!.anchor, false);
  }
  else {
    void revealField(flag.field, flag.ref, false);
  }
}

whenever(() => keys["Alt+ArrowDown"]!.value && plainKeys.value, () => stepFlag(1));
whenever(() => keys["Alt+ArrowUp"]!.value && plainKeys.value, () => stepFlag(-1));
whenever(() => keys.r!.value && noModifier() && plainKeys.value, () => openReread());
whenever(() => keys.BracketLeft!.value && noModifier() && plainKeys.value, () => {
  pageIndex.value = Math.max(0, pageIndex.value - 1);
});
whenever(() => keys.BracketRight!.value && noModifier() && plainKeys.value, () => {
  pageIndex.value = Math.min((job.value?.pages?.length ?? 1) - 1, pageIndex.value + 1);
});
whenever(() => keys.Escape!.value && regionDialog.value, () => {
  regionDialog.value = false;
});
</script>

<style scoped>
.ingest-review--phone {
  /* room for the fixed bottom bar, as tall as it is with its notice (measured), else its usual height */
  padding-bottom: var(--ingest-review-bar-height, calc(72px + env(safe-area-inset-bottom)));
}

/* On phones the columns stack, so the strip's own column is only as tall as the strip: the column sticks instead */
.ingest-review--phone .ingest-review__viewer {
  position: sticky;
  top: 48px;
  z-index: 4;
}

.ingest-review__header {
  border-bottom: thin solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.ingest-review__position {
  min-width: 0;
}

.ingest-review__editor {
  min-width: 0;
}
</style>
