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
    </v-card-text>
  </BaseDialog>
</template>

<script setup lang="ts">
import { EVAL_CASE_SLUG, suggestEvalSlug } from "~/composables/use-recipe-ingest-review";

/**
 * "Save as eval case" (docs/ai/PHASE2.md §11.6, group managers): names the case and records whether the reviewer
 * checked the recipe against the card. The page saves it; a name already taken is shown here.
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
  (e: "save", value: { slug: string; verified: boolean }): void;
  (e: "update:exists", value: boolean): void;
}>();

const dialog = defineModel<boolean>({ required: true });

const i18n = useI18n();
const slug = ref("");
const verified = ref(false);

watch(dialog, (open) => {
  if (open) {
    slug.value = suggestEvalSlug(props.recipeName);
    verified.value = false;
  }
}, { immediate: true });

watch(slug, () => emit("update:exists", false));

const valid = computed(() => EVAL_CASE_SLUG.test(slug.value));

const slugError = computed(() => {
  if (props.exists) {
    return i18n.t("recipe-ingest.eval.exists");
  }
  return slug.value && !valid.value ? i18n.t("recipe-ingest.eval.slug-hint") : "";
});

function save() {
  if (valid.value && !props.saving) {
    emit("save", { slug: slug.value, verified: verified.value });
  }
}
</script>
