<template>
  <div class="ingest-step-list">
    <!-- steps keep their ids when moved, so their flags and re-reads follow them -->
    <VueDraggable
      v-model="model.steps"
      handle=".ingest-step__handle"
      :disabled="readonly || !draggable"
      :animation="200"
    >
      <div
        v-for="(step, index) in model.steps"
        :id="fieldAnchorId('steps', step.id)"
        :key="step.id"
        class="ingest-step"
        :class="severityClass(step.id)"
      >
        <v-text-field
          :model-value="step.title ?? ''"
          variant="underlined"
          density="compact"
          hide-details
          class="ingest-step__title mb-3"
          :label="$t('recipe-ingest.review.section-title')"
          :readonly="readonly"
          @update:model-value="value => (step.title = value || null)"
        />
        <v-textarea
          v-model="step.text"
          variant="outlined"
          auto-grow
          rows="2"
          hide-details="auto"
          :label="$t('recipe.step-index', { step: index + 1 })"
          :readonly="readonly"
          :append-inner-icon="severityIcon(step.id, $globals.icons)"
        />
        <div v-if="!readonly" class="d-flex flex-wrap align-center ga-1">
          <v-icon
            v-if="draggable"
            class="ingest-step__handle mx-2"
            :icon="$globals.icons.arrowUpDown"
            :title="$t('recipe-ingest.review.drag-to-move')"
          />
          <v-btn
            :ref="element => setMoveButton(step.id, 'up', element)"
            class="ingest-step__move-up"
            icon
            size="small"
            variant="text"
            :disabled="index === 0"
            :aria-label="$t('recipe-ingest.review.move-up')"
            :title="$t('recipe-ingest.review.move-up')"
            @click="move(index, -1)"
          >
            <v-icon :icon="mdiArrowUp" />
          </v-btn>
          <v-btn
            :ref="element => setMoveButton(step.id, 'down', element)"
            class="ingest-step__move-down"
            icon
            size="small"
            variant="text"
            :disabled="index === model.steps.length - 1"
            :aria-label="$t('recipe-ingest.review.move-down')"
            :title="$t('recipe-ingest.review.move-down')"
            @click="move(index, 1)"
          >
            <v-icon :icon="mdiArrowDown" />
          </v-btn>
          <v-btn
            v-if="canReread"
            class="ingest-step__reread"
            size="small"
            variant="text"
            :prepend-icon="mdiCropFree"
            @click="emit('reread', step.id ?? null)"
          >
            {{ $t("recipe-ingest.review.re-read") }}
          </v-btn>
          <v-spacer />
          <v-btn
            size="small"
            variant="text"
            color="error"
            :prepend-icon="$globals.icons.delete"
            @click="remove(index)"
          >
            {{ $t("general.delete") }}
          </v-btn>
        </div>
      </div>
    </VueDraggable>
    <v-btn
      v-if="!readonly"
      size="small"
      variant="text"
      color="primary"
      :prepend-icon="$globals.icons.create"
      @click="add"
    >
      {{ $t("recipe-ingest.review.add-step") }}
    </v-btn>
  </div>
</template>

<script setup lang="ts">
import { mdiArrowDown, mdiArrowUp, mdiCropFree } from "@mdi/js";
import type { ComponentPublicInstance } from "vue";
import { VueDraggable } from "vue-draggable-plus";
import { uuid4 } from "~/composables/use-utils";
import { fieldAnchorId, fieldSeverity, flagsForField, type ReviewDraft } from "~/composables/use-recipe-ingest-review";
import type { CardFlag } from "~/lib/api/types/recipe-ingest";

/**
 * The draft's steps as plain title and text fields (docs/ai/PHASE2.md §6.8): upstream's step editor needs a saved
 * recipe for its images. A step with an unresolved error or warning gets a coloured edge and an icon. Steps move with
 * their Move up and Move down buttons, or by their drag handles on desktop (upstream's `vue-draggable-plus`), and
 * each can be re-read from the card.
 */
const props = withDefaults(defineProps<{
  /** Unresolved errors and warnings (the page's open flags) */
  flags?: CardFlag[];
  readonly?: boolean;
  /** Drag handles on the steps (desktop) */
  draggable?: boolean;
  /** Offers Re-read on each step (a ready card) */
  canReread?: boolean;
}>(), {
  flags: () => [],
  readonly: false,
  draggable: false,
  canReread: false,
});

const emit = defineEmits<{
  /** re-read the area of the card this step (its id) is on */
  (e: "reread", stepId: string | null): void;
}>();

const model = defineModel<ReviewDraft>({ required: true });

function severity(stepId: string | undefined) {
  return fieldSeverity(flagsForField(props.flags, "steps", stepId ?? null));
}

function severityClass(stepId: string | undefined) {
  const level = severity(stepId);
  return level ? `ingest-step--${level}` : undefined;
}

function severityIcon(stepId: string | undefined, icons: Record<string, string>) {
  const level = severity(stepId);
  if (!level) {
    return undefined;
  }
  return level === "error" ? icons.alertCircle : icons.alert;
}

function add() {
  model.value.steps.push({ id: uuid4(), title: null, text: "" });
}

function remove(index: number) {
  model.value.steps.splice(index, 1);
}

/** Each step's move buttons, so the focus can follow a step the keyboard moved */
const moveButtons = new Map<string, HTMLElement>();

function setMoveButton(stepId: string | undefined, direction: "up" | "down", element: Element | ComponentPublicInstance | null) {
  const key = `${stepId}:${direction}`;
  const button = (element as ComponentPublicInstance | null)?.$el as HTMLElement | undefined;
  if (button) {
    moveButtons.set(key, button);
  }
  else {
    moveButtons.delete(key);
  }
}

/** Moves a step one place up (-1) or down (1); the focus stays on its move button, which reordering would drop */
async function move(index: number, offset: -1 | 1) {
  const target = index + offset;
  if (target < 0 || target >= model.value.steps.length) {
    return;
  }
  const [step] = model.value.steps.splice(index, 1);
  model.value.steps.splice(target, 0, step!);
  await nextTick();
  // at the top or bottom that way is off: the other button takes the focus
  const last = model.value.steps.length - 1;
  const direction = offset < 0 ? (target === 0 ? "down" : "up") : (target === last ? "up" : "down");
  moveButtons.get(`${step!.id}:${direction}`)?.focus?.();
}
</script>

<style scoped>
.ingest-step {
  border-left: 4px solid transparent;
  padding-left: 8px;
  margin-bottom: 8px;
}

.ingest-step--error {
  border-left-color: rgb(var(--v-theme-error));
}

.ingest-step--warning {
  border-left-color: rgb(var(--v-theme-warning));
}

.ingest-step--error :deep(.v-field__append-inner .v-icon) {
  color: rgb(var(--v-theme-error));
}

.ingest-step--warning :deep(.v-field__append-inner .v-icon) {
  color: rgb(var(--v-theme-warning));
}
</style>
