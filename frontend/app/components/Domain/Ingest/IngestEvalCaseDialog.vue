<template>
  <BaseDialog
    v-model="dialog"
    :title="$t('recipe-ingest.eval.title')"
    :icon="$globals.icons.testTube"
    :submit-text="$t('recipe-ingest.eval.save')"
    :submit-icon="$globals.icons.save"
    :submit-disabled="!valid || saving"
    :loading="saving"
    can-submit
    keep-open
    @submit="save"
  >
    <v-card-text class="ingest-eval-dialog">
      <p class="text-body-2 mb-4">
        {{ $t("recipe-ingest.eval.description") }}
      </p>
      <v-text-field
        v-model="slug"
        variant="outlined"
        :label="$t('recipe-ingest.eval.slug')"
        :hint="$t('recipe-ingest.eval.slug-hint')"
        persistent-hint
        :error-messages="slugError"
        autocapitalize="off"
        spellcheck="false"
      />
      <v-checkbox
        v-model="verified"
        hide-details
        :label="$t('recipe-ingest.eval.verified')"
      />
      <!-- what the card is like, which the eval report groups scores by -->
      <p id="ingest-eval-tags-label" class="text-body-2 mt-2 mb-1">
        {{ $t("recipe-ingest.review.eval-tags") }}
      </p>
      <v-chip-group
        v-model="tags"
        multiple
        column
        selected-class="text-primary"
        class="ingest-eval-dialog__tags"
        aria-labelledby="ingest-eval-tags-label"
      >
        <v-chip
          v-for="tag in EVAL_CASE_TAGS"
          :key="tag"
          :value="tag"
          filter
          size="small"
          variant="outlined"
        >
          {{ $t(`recipe-ingest.eval.tag-${tag}`) }}
        </v-chip>
      </v-chip-group>
      <v-textarea
        v-model="notes"
        class="mt-3 ingest-eval-dialog__notes"
        variant="outlined"
        rows="2"
        auto-grow
        :label="$t('recipe-ingest.review.eval-notes')"
        :counter="MAX_EVAL_NOTES"
        :error-messages="notesError"
      />
    </v-card-text>
  </BaseDialog>
</template>

<script setup lang="ts">
import { EVAL_CASE_SLUG, EVAL_CASE_TAGS, MAX_EVAL_NOTES, suggestEvalSlug } from "~/composables/use-recipe-ingest-review";
import type { EvalCaseRequest, EvalCaseTag } from "~/lib/api/types/recipe-ingest";

/**
 * "Save as eval case" (docs/ai/PHASE2.md §11.6, group managers): names the case, records whether the reviewer
 * checked the recipe against the card, and what the card is like (handwritten, printed, faded) with any notes, which
 * go into the case's JSON. The page saves it; a name already taken is shown here.
 */
const props = withDefaults(defineProps<{
  /** The recipe's name, from which the case's name is suggested */
  recipeName?: string | null;
  saving?: boolean;
  /** The last save failed because a case with this name exists */
  exists?: boolean;
}>(), {
  recipeName: null,
  saving: false,
  exists: false,
});

const emit = defineEmits<{
  (e: "save", value: Required<EvalCaseRequest>): void;
  (e: "update:exists", value: boolean): void;
}>();

const dialog = defineModel<boolean>({ required: true });

const i18n = useI18n();
const slug = ref("");
const verified = ref(false);
const tags = ref<EvalCaseTag[]>([]);
const notes = ref("");

watch(dialog, (open) => {
  if (open) {
    slug.value = suggestEvalSlug(props.recipeName);
    verified.value = false;
    tags.value = [];
    notes.value = "";
  }
}, { immediate: true });

watch(slug, () => emit("update:exists", false));

const notesTooLong = computed(() => notes.value.trim().length > MAX_EVAL_NOTES);
const valid = computed(() => EVAL_CASE_SLUG.test(slug.value) && !notesTooLong.value);

const notesError = computed(() => (notesTooLong.value ? i18n.t("recipe-ingest.review.eval-notes-too-long", { max: MAX_EVAL_NOTES }) : ""));

const slugError = computed(() => {
  if (props.exists) {
    return i18n.t("recipe-ingest.eval.exists");
  }
  return slug.value && !EVAL_CASE_SLUG.test(slug.value) ? i18n.t("recipe-ingest.eval.slug-hint") : "";
});

function save() {
  if (valid.value && !props.saving) {
    emit("save", {
      slug: slug.value,
      verified: verified.value,
      // in the order the chips show
      tags: EVAL_CASE_TAGS.filter(tag => tags.value.includes(tag)),
      notes: notes.value.trim(),
    });
  }
}
</script>
