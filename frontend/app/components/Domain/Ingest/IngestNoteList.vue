<template>
  <div class="ingest-note-list">
    <!-- a note keeps its id through edits and deletes around it, so its flags (and their resolutions) stay with it -->
    <div
      v-for="(note, index) in model.notes"
      :id="fieldAnchorId('notes', note.id)"
      :key="note.id"
      class="ingest-note"
      :class="severityClass(note.id)"
    >
      <v-text-field
        v-model="note.title"
        variant="underlined"
        density="compact"
        hide-details
        class="ingest-note__title mb-3"
        :label="$t('recipe.title')"
        :readonly="readonly"
      />
      <v-textarea
        v-model="note.text"
        variant="outlined"
        auto-grow
        rows="2"
        hide-details="auto"
        :label="$t('recipe-ingest.review.note-number', { number: index + 1 })"
        :readonly="readonly"
        :append-inner-icon="severityIcon(note.id, $globals.icons)"
      />
      <div v-if="!readonly" class="d-flex flex-wrap align-center ga-1">
        <v-btn
          v-if="canReread"
          class="ingest-note__reread"
          size="small"
          variant="text"
          :prepend-icon="mdiCropFree"
          @click="emit('reread', note.id)"
        >
          {{ $t("recipe-ingest.review.re-read") }}
        </v-btn>
        <v-spacer />
        <v-btn
          class="ingest-note__delete"
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
    <v-btn
      v-if="!readonly"
      class="ingest-note-list__add"
      size="small"
      variant="text"
      color="primary"
      :prepend-icon="$globals.icons.create"
      @click="add"
    >
      {{ $t("recipe-ingest.review.add-note") }}
    </v-btn>
  </div>
</template>

<script setup lang="ts">
import { mdiCropFree } from "@mdi/js";
import { uuid4 } from "~/composables/use-utils";
import { fieldAnchorId, fieldSeverity, flagsForField, type ReviewDraft } from "~/composables/use-recipe-ingest-review";
import type { CardFlag } from "~/lib/api/types/recipe-ingest";

/**
 * The draft's notes as plain title and text fields, in place of upstream's `RecipeNotes` (docs/ai/PHASE2.md §6.4):
 * a note with an unresolved error or warning gets the coloured edge and icon steps get. Flags name a note by its id
 * (`ref`), which the note keeps when it's edited or others are deleted; a new note gets an id of its own. Each note
 * can be re-read from the card.
 */
const props = withDefaults(defineProps<{
  /** Unresolved errors and warnings (the page's open flags) */
  flags?: CardFlag[];
  readonly?: boolean;
  /** Offers Re-read on each note (a ready card) */
  canReread?: boolean;
}>(), {
  flags: () => [],
  readonly: false,
  canReread: false,
});

const emit = defineEmits<{
  /** re-read the area of the card this note (its id) is on */
  (e: "reread", noteId: string): void;
}>();

const model = defineModel<ReviewDraft>({ required: true });

function severity(noteId: string) {
  return fieldSeverity(flagsForField(props.flags, "notes", noteId));
}

function severityClass(noteId: string) {
  const level = severity(noteId);
  return level ? `ingest-note--${level}` : undefined;
}

function severityIcon(noteId: string, icons: Record<string, string>) {
  const level = severity(noteId);
  if (!level) {
    return undefined;
  }
  return level === "error" ? icons.alertCircle : icons.alert;
}

function add() {
  model.value.notes.push({ id: uuid4(), title: "", text: "" });
}

function remove(index: number) {
  model.value.notes.splice(index, 1);
}
</script>

<style scoped>
.ingest-note {
  border-left: 4px solid transparent;
  padding-left: 8px;
  margin-bottom: 8px;
}

.ingest-note--error {
  border-left-color: rgb(var(--v-theme-error));
}

.ingest-note--warning {
  border-left-color: rgb(var(--v-theme-warning));
}

.ingest-note--error :deep(.v-field__append-inner .v-icon) {
  color: rgb(var(--v-theme-error));
}

.ingest-note--warning :deep(.v-field__append-inner .v-icon) {
  color: rgb(var(--v-theme-warning));
}
</style>
