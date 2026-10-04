<template>
  <v-card
    v-if="items.length"
    class="ingest-needs-a-look"
    variant="outlined"
  >
    <v-card-title class="d-flex align-center text-subtitle-1 font-weight-medium">
      <v-icon
        class="mr-2"
        size="small"
        :color="openCount ? 'warning' : 'success'"
        :icon="openCount ? $globals.icons.alert : $globals.icons.check"
      />
      <span class="ingest-needs-a-look__title">
        {{ openCount ? $t("recipe-ingest.review.needs-a-look", { count: openCount }) : $t("recipe-ingest.review.nothing-to-check") }}
      </span>
    </v-card-title>
    <div class="ingest-needs-a-look__items">
      <template v-for="(item, index) in items" :key="item.flag.id">
        <v-divider v-if="index > 0" />
        <IngestFlagItem
          :item="item"
          :readonly="readonly"
          :can-reread="canReread"
          @alternative="(flag, alternative) => emit('alternative', flag, alternative)"
          @fill="(flag, value) => emit('fill', flag, value)"
          @reread="flag => emit('reread', flag)"
          @edit="flag => emit('edit', flag)"
          @resolve="(flag, resolution) => emit('resolve', flag, resolution)"
          @use-proposal="(proposal, mode) => emit('use-proposal', proposal, mode)"
          @dismiss-proposal="proposal => emit('dismiss-proposal', proposal)"
        />
      </template>
    </div>
  </v-card>
</template>

<script setup lang="ts">
import IngestFlagItem from "./IngestFlagItem.vue";
import type { NeedsALookItem } from "~/composables/use-recipe-ingest-review";
import type { CardFlag, CardProposal, FlagResolution } from "~/lib/api/types/recipe-ingest";

/**
 * "Needs a look (2)" (docs/ai/PHASE2.md §6.2): one item per error or warning on the card, in reading order. Items
 * still to check are open; resolved and fixed ones collapse with a check mark. A clean card shows nothing here.
 */
const props = withDefaults(defineProps<{
  items: NeedsALookItem[];
  readonly?: boolean;
  /** Whether Re-read works now */
  canReread?: boolean;
}>(), {
  readonly: false,
  canReread: true,
});

const emit = defineEmits<{
  /** an alternative reading to apply, or the value typed over a blank */
  (e: "alternative" | "fill", flag: CardFlag, text: string): void;
  (e: "reread" | "edit", flag: CardFlag): void;
  (e: "resolve", flag: CardFlag, resolution: FlagResolution | null): void;
  (e: "use-proposal", proposal: CardProposal, mode: "replace" | "append"): void;
  (e: "dismiss-proposal", proposal: CardProposal): void;
}>();

const openCount = computed(() => props.items.filter(item => item.state === "open").length);
</script>
