<template>
  <div
    :id="item.anchor"
    class="ingest-flag-item"
    :class="[`ingest-flag-item--${item.flag.severity}`, `ingest-flag-item--${item.state}`]"
    :data-flag="item.flag.id"
  >
    <div class="d-flex align-start ga-2">
      <v-icon
        class="mt-1 flex-shrink-0"
        size="small"
        :color="item.state === 'open' ? severityColor : 'success'"
        :icon="item.state !== 'open' ? $globals.icons.check : item.flag.severity === 'error' ? $globals.icons.alertCircle : $globals.icons.alert"
      />
      <div class="flex-grow-1 ingest-flag-item__body">
        <div class="d-flex align-center flex-wrap ga-1">
          <span class="text-subtitle-2 ingest-flag-item__title">{{ texts.title }}</span>
          <span v-if="label" class="text-caption text-medium-emphasis">· {{ label }}</span>
          <v-spacer />
          <v-btn
            v-if="item.state === 'resolved'"
            size="small"
            variant="text"
            :disabled="readonly"
            @click="emit('resolve', item.flag, null)"
          >
            {{ $t("recipe-ingest.review.undo") }}
          </v-btn>
        </div>

        <template v-if="item.state === 'open'">
          <div v-if="segments.length" class="ingest-flag-item__line">
            <span
              v-for="(segment, index) in segments"
              :key="index"
              :class="{ 'ingest-flag-item__mark': segment.mark }"
            >{{ segment.text }}</span>
          </div>
          <!-- "Check this ingredient": what the parser made of the line, and what it lost -->
          <div v-if="reading" class="text-body-2 mt-1 ingest-flag-item__reading">
            {{ $t("recipe-ingest.flag.check_parse.read-as", { reading }) }}
          </div>
          <div v-if="explanation" class="text-body-2 text-medium-emphasis mt-1 ingest-flag-item__explanation">
            {{ explanation }}
          </div>

          <IngestProposalBanner
            v-for="proposal in item.proposals"
            :key="proposal.id ?? proposal.createdAt"
            class="mt-2"
            compact
            :proposal="proposal"
            :readonly="readonly"
            @use="mode => emit('use-proposal', proposal, mode)"
            @dismiss="emit('dismiss-proposal', proposal)"
          />

          <div v-if="alternatives.length" class="d-flex flex-wrap ga-1 mt-2 ingest-flag-item__alternatives">
            <v-chip
              v-for="alternative in alternatives"
              :key="alternative"
              size="small"
              color="primary"
              variant="outlined"
              :disabled="readonly"
              @click="emit('alternative', item.flag, alternative)"
            >
              {{ $t("recipe-ingest.flag.actions.use-alternative", { text: alternative }) }}
            </v-chip>
          </div>

          <div v-if="fillable" class="d-flex align-center ga-2 mt-2 ingest-flag-item__fill">
            <v-text-field
              v-model="typed"
              density="compact"
              variant="outlined"
              hide-details="auto"
              :label="$t('recipe-ingest.review.fill-blank')"
              :hint="$t('recipe-ingest.review.fill-blank-hint')"
              :disabled="readonly"
              @keydown.enter.prevent="fill"
            />
            <v-btn
              size="small"
              color="primary"
              variant="flat"
              :disabled="readonly || !typed.trim()"
              @click="fill"
            >
              {{ $t("recipe-ingest.flag.actions.fill-in") }}
            </v-btn>
          </div>

          <div class="d-flex flex-wrap ga-1 mt-2 ingest-flag-item__actions">
            <v-btn
              v-if="onField"
              size="small"
              variant="tonal"
              :prepend-icon="mdiCropFree"
              :disabled="readonly || !canReread"
              @click="emit('reread', item.flag)"
            >
              {{ $t("recipe-ingest.review.re-read") }}
            </v-btn>
            <v-btn
              v-if="resolution && texts.action"
              size="small"
              variant="tonal"
              :color="item.flag.severity === 'error' ? 'error' : undefined"
              :disabled="readonly"
              @click="emit('resolve', item.flag, resolution)"
            >
              {{ texts.action }}
            </v-btn>
            <v-btn
              v-if="reading"
              size="small"
              variant="tonal"
              :disabled="readonly"
              @click="emit('keep-as-text', item.flag)"
            >
              {{ $t("recipe-ingest.flag.check_parse.keep-as-text") }}
            </v-btn>
            <v-btn
              v-if="onField"
              size="small"
              variant="text"
              :prepend-icon="$globals.icons.edit"
              @click="emit('edit', item.flag)"
            >
              {{ $t("recipe-ingest.review.edit") }}
            </v-btn>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { mdiCropFree } from "@mdi/js";
import IngestProposalBanner from "./IngestProposalBanner.vue";
import { useRecipeIngestText } from "~/composables/use-recipe-ingest";
import {
  canFillFlag,
  canKeepFlag,
  fieldLabel,
  flagAlternatives,
  highlightSegments,
  isParsedIngredient,
  parsedReading,
  parseLoss,
  type NeedsALookItem,
} from "~/composables/use-recipe-ingest-review";
import type { CardFlag, CardProposal, FlagResolution } from "~/lib/api/types/recipe-ingest";

/**
 * One "Needs a look" item (docs/ai/PHASE2.md §6.2): the line as read with the problem highlighted, then its one-tap
 * fixes: alternative readings, a fill-in box for a blank, Re-read, Keep as written (errors) or Looks right
 * (warnings), and Edit. "Check this ingredient" also shows what the parser read ("Read as: 2 cup flour, to 3"), what
 * it lost, and Keep as text. A resolved item collapses with a check mark and can be undone; a fixed one just collapses.
 */
const props = withDefaults(defineProps<{
  item: NeedsALookItem;
  readonly?: boolean;
  /** Whether Re-read works now (the page is busy turning a page, say) */
  canReread?: boolean;
}>(), {
  readonly: false,
  canReread: true,
});

const emit = defineEmits<{
  /** an alternative reading to apply, or the value typed over a blank */
  (e: "alternative" | "fill", flag: CardFlag, text: string): void;
  /** "Keep as text": the flagged ingredient line kept as written, with no amount, unit or food */
  (e: "reread" | "edit" | "keep-as-text", flag: CardFlag): void;
  (e: "resolve", flag: CardFlag, resolution: FlagResolution | null): void;
  (e: "use-proposal", proposal: CardProposal, mode: "replace" | "append"): void;
  (e: "dismiss-proposal", proposal: CardProposal): void;
}>();

const i18n = useI18n();
const { flagText } = useRecipeIngestText();

const texts = computed(() => flagText(props.item.flag));
const label = computed(() => fieldLabel((key, named) => i18n.t(key, named ?? {}), props.item.field, props.item.line));
const segments = computed(() => highlightSegments(props.item.text, props.item.fragment, props.item.span));

/** For "Check this ingredient" on a parsed line: what the parser read, which Keep as text replaces with the line */
const reading = computed(() => {
  const ingredient = props.item.ingredient;
  return props.item.flag.kind === "check_parse" && ingredient && isParsedIngredient(ingredient) ? parsedReading(ingredient) : null;
});

/** What the parser lost, said plainly ("The 3 of 2-3 is kept in the note."), in place of the general explanation */
const explanation = computed(() => {
  const loss = reading.value ? parseLoss(props.item.flag, props.item.ingredient) : null;
  if (!loss) {
    return texts.value.explanation;
  }
  const key = loss.kind === "in-name" ? "in-name" : `${loss.kind}-${loss.kept ? "kept" : "lost"}`;
  return i18n.t(`recipe-ingest.flag.check_parse.${key}`, { ...loss });
});
const alternatives = computed(() => flagAlternatives(props.item.flag));
const fillable = computed(() => canFillFlag(props.item.flag));
/** Edit and Re-read need a place in the editor; a re-read for an ingredient or step flag without a line adds one */
const onField = computed(() => props.item.field !== "card");

/** What the one-tap resolution stores: errors are kept as written, warnings dismissed as right */
const resolution = computed<FlagResolution | null>(() => {
  if (props.item.flag.severity === "warning") {
    return "dismissed";
  }
  return canKeepFlag(props.item.flag) ? "kept" : null;
});

const severityColor = computed(() => (props.item.flag.severity === "error" ? "error" : "warning"));

const typed = ref("");

function fill() {
  const value = typed.value.trim();
  if (value && !props.readonly) {
    emit("fill", props.item.flag, value);
    typed.value = "";
  }
}
</script>

<style scoped>
.ingest-flag-item {
  border-left: 4px solid transparent;
  padding: 8px 8px 8px 12px;
}

.ingest-flag-item--open.ingest-flag-item--error {
  border-left-color: rgb(var(--v-theme-error));
}

.ingest-flag-item--open.ingest-flag-item--warning {
  border-left-color: rgb(var(--v-theme-warning));
}

.ingest-flag-item--resolved,
.ingest-flag-item--fixed {
  opacity: 0.75;
}

.ingest-flag-item__body {
  min-width: 0;
}

.ingest-flag-item__line {
  margin-top: 4px;
  font-family: monospace;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.ingest-flag-item__mark {
  background-color: rgba(var(--v-theme-warning), 0.35);
  border-radius: 2px;
  padding: 0 2px;
  font-weight: 600;
}

.ingest-flag-item--error .ingest-flag-item__mark {
  background-color: rgba(var(--v-theme-error), 0.25);
}
</style>
