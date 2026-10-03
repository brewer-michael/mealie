<template>
  <div>
    <BaseCardSectionTitle :title="$t('group.ai-provider-settings.usage-this-month')" size="medium">
      <template #append-title>
        <v-btn
          class="ms-auto"
          variant="text"
          size="small"
          :icon="$globals.icons.refresh"
          :loading="loading"
          :aria-label="$t('group.ai-provider-settings.refresh-usage')"
          @click="load"
        />
      </template>
    </BaseCardSectionTitle>

    <!-- A failed refresh keeps showing the usage loaded before it -->
    <v-alert
      v-if="failed"
      type="error"
      density="compact"
      variant="tonal"
      class="mb-4"
    >
      {{ $t("group.ai-provider-settings.usage-load-failed") }}
    </v-alert>
    <AppLoader v-if="!usage && loading" />
    <template v-else-if="usage">
      <v-card-text v-if="!rows.length" class="px-0 pt-0">
        {{ $t("group.ai-provider-settings.no-usage-this-month") }}
      </v-card-text>
      <v-table v-else density="compact">
        <thead>
          <tr>
            <th>{{ $t("group.ai-provider-settings.ai-provider") }}</th>
            <th>{{ $t("group.ai-provider-settings.model") }}</th>
            <th class="text-end">
              {{ $t("group.ai-provider-settings.requests") }}
            </th>
            <th class="text-end">
              {{ $t("group.ai-provider-settings.failures") }}
            </th>
            <th class="text-end">
              {{ $t("group.ai-provider-settings.tokens") }}
            </th>
            <th class="text-end">
              {{ $t("group.ai-provider-settings.monthly-token-limit") }}
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="`${row.providerId ?? 'deleted'}-${row.providerName}-${row.model}`">
            <td>
              {{ row.providerName }}
              <span v-if="!row.providerId" class="text-caption text-medium-emphasis">
                {{ $t("group.ai-provider-settings.deleted-provider") }}
              </span>
            </td>
            <td>{{ row.model }}</td>
            <td class="text-end">
              {{ formatNumber(row.requests) }}
            </td>
            <td class="text-end" :class="{ 'text-error': row.failures > 0 }">
              {{ formatNumber(row.failures) }}
            </td>
            <td class="text-end">
              {{ formatNumber(usedTokens(row)) }}
            </td>
            <td class="text-end" :class="limitColor(row) ? `text-${limitColor(row)}` : undefined">
              {{ limitText(row) }}
            </td>
          </tr>
        </tbody>
      </v-table>
    </template>
  </div>
</template>

<script setup lang="ts">
import {
  formatPercent,
  limitUsage,
  limitUsageColor,
  usageRows,
  usedTokens,
  useAIProviderUsage,
} from "~/composables/use-ai-provider-routing";
import type { AIUsageProviderSummary } from "~/lib/api/types/group";

/** The group's AI usage this (UTC) month, per provider */
const i18n = useI18n();
const { usage, loading, failed, load } = useAIProviderUsage();

const locale = computed(() => i18n.locale.value);
const rows = computed(() => usageRows(usage.value));

function formatNumber(value: number) {
  return value.toLocaleString(locale.value);
}

function limitText(row: AIUsageProviderSummary) {
  const fraction = limitUsage(row);
  return fraction === null
    ? i18n.t("group.ai-provider-settings.no-limit")
    : i18n.t("group.ai-provider-settings.percent-of-limit", {
        percent: formatPercent(fraction, locale.value),
        limit: formatNumber(row.monthlyTokenLimit ?? 0),
      });
}

function limitColor(row: AIUsageProviderSummary) {
  return limitUsageColor(limitUsage(row));
}

onMounted(load);

// Lets the page reload the usage after a provider changes
defineExpose({ load });
</script>
