<template>
  <div class="d-flex justify-center pa-8">
    <AppLoader />
  </div>
</template>

<script setup lang="ts">
import { useUserApi } from "~/composables/api";
import { rememberRecipeIngestBatch } from "~/composables/use-recipe-ingest";
import { firstCardToReview } from "~/composables/use-recipe-ingest-review";

/**
 * The start of a batch's review (docs/ai/PHASE2.md §6.1), which notifications open: the batch's first ready card, in
 * capture order, with an unresolved error or warning, else its first ready card, else the queue filtered to the batch.
 * A batch that can't be opened sends the user to the queue, which says so. Fork-owned.
 */
definePageMeta({
  middleware: ["group-only"],
});

const i18n = useI18n();
const route = useRoute();
const router = useRouter();
const api = useUserApi();

useSeoMeta({
  title: i18n.t("recipe-ingest.nav.recipe-cards"),
});

onMounted(async () => {
  const queue = `/g/${String(route.params.groupSlug ?? "")}/recipes/cards`;
  const batchId = typeof route.query.batch === "string" ? route.query.batch : null;
  if (!batchId) {
    await router.replace(queue);
    return;
  }

  const { data } = await api.recipeIngest.getBatch(batchId);
  if (!data) {
    // purged, discarded, another household's, or the request failed: the queue says the batch can't be opened
    await router.replace(`${queue}?unavailable=1`);
    return;
  }
  // the card opens showing its place in the batch at once
  rememberRecipeIngestBatch(data);
  const first = firstCardToReview(data.jobs ?? []);
  await router.replace(first ? `${queue}/${first}` : `${queue}?batch=${encodeURIComponent(batchId)}`);
});
</script>
