/**
 * Fallback routes, the usage summary and model lists for the group's AI providers (docs/ai/PHASE1.md).
 * Kept apart from upstream's `use-ai-providers.ts` to avoid sync conflicts.
 */
import { useUserApi } from "~/composables/api";
import type {
  AIProviderModelInfo,
  AIProviderModelsQuery,
  AIProviderRoutesUpdate,
  AIProviderSettingsOut,
  AIProviderSlot,
  AIUsageProviderSummary,
  AIUsageSummary,
} from "~/lib/api/types/group";

/** Every slot's ordered fallback provider ids */
export type AIProviderRoutes = Record<AIProviderSlot, string[]>;

/** The group's providers and upstream's per-slot primary providers */
export type AIProviderPrimaries = Pick<
  AIProviderSettingsOut,
  "providers" | "defaultProviderId" | "imageProviderId" | "audioProviderId"
>;

export const AI_PROVIDER_SLOTS: readonly AIProviderSlot[] = ["default", "image", "audio", "planner", "fast", "embedding"];

/** Slots without an upstream primary, edited in the settings' Advanced section */
export const ADVANCED_AI_PROVIDER_SLOTS: readonly AIProviderSlot[] = ["planner", "fast", "embedding"];

/** A monthly limit this far used up gets a warning color in the usage table */
const LIMIT_WARNING_FRACTION = 0.8;

export function emptyRoutes(): AIProviderRoutes {
  return { default: [], image: [], audio: [], planner: [], fast: [], embedding: [] };
}

/** Copies the API's routes, with every slot present */
export function normalizeRoutes(routes?: { [k: string]: string[] } | null): AIProviderRoutes {
  const result = emptyRoutes();
  for (const slot of AI_PROVIDER_SLOTS) {
    result[slot] = [...(routes?.[slot] ?? [])];
  }
  return result;
}

/** Whether a slot has an upstream primary provider (Default, Image and Audio) */
export function hasPrimarySlot(slot: AIProviderSlot): boolean {
  return !ADVANCED_AI_PROVIDER_SLOTS.includes(slot);
}

/** Upstream's primary provider for a slot; only Default, Image and Audio have one */
export function primaryProviderId(settings: AIProviderPrimaries, slot: AIProviderSlot): string | null {
  switch (slot) {
    case "default":
      return settings.defaultProviderId;
    case "image":
      return settings.imageProviderId;
    case "audio":
      return settings.audioProviderId;
    default:
      return null;
  }
}

/**
 * The fallbacks of a slot that can be used, in order: providers that still exist, without duplicates
 * and without the slot's primary (it's always tried first anyway).
 */
export function usableFallbacks(
  ids: readonly string[],
  providers: readonly { id: string }[],
  primaryId?: string | null,
): string[] {
  const known = new Set(providers.map(provider => provider.id));
  return [...new Set(ids)].filter(id => known.has(id) && id !== primaryId);
}

/**
 * What a saved provider's API key is tied to: testing it or listing its models with a blank key (the
 * saved one) is refused once the API type, base URL or request headers differ from the saved ones.
 */
export function apiKeyDestination(config: {
  protocol?: string | null;
  baseUrl?: string | null;
  requestHeaders?: Record<string, string> | null;
}): string {
  const headers = Object.entries(config.requestHeaders ?? {}).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return JSON.stringify([config.protocol || "openai", config.baseUrl || null, headers]);
}

/** A base URL can't have a query string or fragment (the API's paths are appended to it) */
export function baseUrlHasQueryOrFragment(baseUrl?: string | null): boolean {
  return /[?#]/.test(baseUrl ?? "");
}

/** The PUT body for the routes, replacing every slot */
export function buildRoutesPayload(routes: AIProviderRoutes, settings: AIProviderPrimaries): AIProviderRoutesUpdate {
  return {
    routes: Object.fromEntries(
      AI_PROVIDER_SLOTS.map(slot => [
        slot,
        usableFallbacks(routes[slot], settings.providers, primaryProviderId(settings, slot)),
      ]),
    ),
  };
}

// ==========================================
// Usage

export function usedTokens(row: AIUsageProviderSummary): number {
  return row.promptTokens + row.completionTokens;
}

/** The share of the monthly token limit used (1 = all of it), or null without a limit */
export function limitUsage(row: AIUsageProviderSummary): number | null {
  if (!row.monthlyTokenLimit || row.monthlyTokenLimit < 1) {
    return null;
  }
  return usedTokens(row) / row.monthlyTokenLimit;
}

/** A Vuetify color for how much of its limit a provider has used */
export function limitUsageColor(fraction: number | null): "error" | "warning" | undefined {
  if (fraction === null) {
    return undefined;
  }
  if (fraction >= 1) {
    return "error";
  }
  return fraction >= LIMIT_WARNING_FRACTION ? "warning" : undefined;
}

export function formatPercent(fraction: number, locale?: string): string {
  return new Intl.NumberFormat(locale, { style: "percent", maximumFractionDigits: 1 }).format(fraction);
}

/** The usage table's rows: providers that were called in the summary's range */
export function usageRows(summary: AIUsageSummary | null): AIUsageProviderSummary[] {
  return summary?.byProvider.filter(row => row.requests > 0) ?? [];
}

// ==========================================
// Composables

export function useAIProviderRoutes() {
  const api = useUserApi();

  /** null until loaded, and if loading failed (saving then does nothing, so stored routes are kept) */
  const routes = ref<AIProviderRoutes | null>(null);
  const loadFailed = ref(false);

  async function load() {
    const { data } = await api.aiProviders.getRoutes();
    routes.value = data ? normalizeRoutes(data.routes) : null;
    loadFailed.value = !data;
    return routes.value;
  }

  /** Saves every slot's routes; returns whether that worked (true when there was nothing to save) */
  async function save(settings: AIProviderPrimaries) {
    if (!routes.value) {
      return true;
    }

    const { data } = await api.aiProviders.updateRoutes(buildRoutesPayload(routes.value, settings));
    if (!data) {
      return false;
    }

    routes.value = normalizeRoutes(data.routes);
    return true;
  }

  return { routes, loadFailed: readonly(loadFailed), load, save };
}

/** The group's providers whose saved API key can't be read (e.g. after the server's secret changed) */
export function useAIProviderKeyStatus() {
  const api = useUserApi();
  const unreadableIds = ref<string[]>([]);

  /** Keeps the last list if loading fails */
  async function load() {
    const { data } = await api.aiProviders.getAll();
    if (data) {
      unreadableIds.value = data.filter(provider => !provider.apiKeySet).map(provider => provider.id);
    }
  }

  return { unreadableIds: readonly(unreadableIds), load };
}

export function useAIProviderUsage() {
  const api = useUserApi();
  const usage = ref<AIUsageSummary | null>(null);
  const loading = ref(false);
  /** Whether the last load failed; the usage loaded before it is kept */
  const failed = ref(false);

  /** Loads the current UTC month's usage */
  async function load() {
    loading.value = true;
    try {
      const { data } = await api.aiProviders.getUsage();
      if (data) {
        usage.value = data;
      }
      failed.value = !data;
    }
    finally {
      loading.value = false;
    }
  }

  return { usage, loading: readonly(loading), failed: readonly(failed), load };
}

export function useAIProviderModels() {
  const api = useUserApi();
  const models = ref<AIProviderModelInfo[]>([]);
  const loading = ref(false);
  /** Whether the last load failed (the API's reason, if any, is already shown as a toast) */
  const failed = ref(false);
  /** Whether the last load succeeded but listed nothing */
  const empty = ref(false);

  function reset() {
    models.value = [];
    failed.value = false;
    empty.value = false;
  }

  /**
   * Lists the models a provider configuration offers. With `providerId`, a blank `apiKey` uses that
   * provider's saved key; without it, the configuration is an unsaved one and needs a key.
   */
  async function load(query: AIProviderModelsQuery & { apiKey?: string }, providerId?: string) {
    loading.value = true;
    reset();
    try {
      const { data } = providerId
        ? await api.aiProviders.listSavedModels(providerId, query)
        : await api.aiProviders.listModels({ ...query, apiKey: query.apiKey ?? "" });

      if (data) {
        models.value = data;
        empty.value = data.length === 0;
      }
      else {
        failed.value = true;
      }
    }
    finally {
      loading.value = false;
    }
  }

  return { models, loading: readonly(loading), failed: readonly(failed), empty: readonly(empty), load, reset };
}
