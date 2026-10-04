<template>
  <div class="mt-1">
    <v-switch
      :model-value="allowWrites ?? false"
      :label="$t('mcp.token-allow-writes')"
      :loading="loading || saving"
      :disabled="allowWrites === null || saving"
      color="primary"
      density="compact"
      inset
      hide-details
      @update:model-value="handleChange"
    />
    <p v-if="loadFailed" class="text-body-small text-error mb-0">
      {{ $t("mcp.token-grant-load-failed") }}
    </p>
  </div>
</template>

<script setup lang="ts">
import { useMcpApiTokenGrant } from "~/composables/use-mcp";
import { alert } from "~/composables/use-toast";

/** Profile → API Tokens: whether AI assistants using this token may make changes (docs/ai/PHASE3.md §3) */
const props = defineProps<{
  tokenId: number;
}>();

const i18n = useI18n();
const { allowWrites, loading, saving, loadFailed, load, set } = useMcpApiTokenGrant(() => props.tokenId);

// The token list is keyed by position, so a row can be reused for another token
watch(() => props.tokenId, load, { immediate: true });

async function handleChange(value: boolean | null) {
  if (!(await set(!!value))) {
    alert.error(i18n.t("mcp.token-grant-update-failed"));
  }
}
</script>
