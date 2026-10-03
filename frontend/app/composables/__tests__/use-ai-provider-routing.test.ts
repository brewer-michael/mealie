import { beforeEach, describe, expect, test, vi } from "vitest";
import {
  apiKeyDestination,
  baseUrlHasQueryOrFragment,
  buildRoutesPayload,
  emptyRoutes,
  formatPercent,
  hasPrimarySlot,
  limitUsage,
  limitUsageColor,
  normalizeRoutes,
  primaryProviderId,
  usableFallbacks,
  usageRows,
  useAIProviderKeyStatus,
  useAIProviderModels,
  useAIProviderRoutes,
  useAIProviderUsage,
  usedTokens,
} from "../use-ai-provider-routing";
import type { AIProviderPrimaries } from "../use-ai-provider-routing";
import type { AIUsageProviderSummary, AIUsageSummary } from "~/lib/api/types/group";

const getRoutes = vi.fn();
const updateRoutes = vi.fn();
const listModels = vi.fn();
const listSavedModels = vi.fn();
const getUsage = vi.fn();
const getAll = vi.fn();
vi.mock("~/composables/api", () => ({
  useUserApi: () => ({
    aiProviders: { getRoutes, updateRoutes, listModels, listSavedModels, getUsage, getAll },
  }),
}));

const providers = [
  { id: "a", name: "A" },
  { id: "b", name: "B" },
  { id: "c", name: "C" },
];

function settings(overrides: Partial<AIProviderPrimaries> = {}): AIProviderPrimaries {
  return {
    providers,
    defaultProviderId: null,
    imageProviderId: null,
    audioProviderId: null,
    ...overrides,
  };
}

function usageRow(overrides: Partial<AIUsageProviderSummary> = {}): AIUsageProviderSummary {
  return {
    providerId: "a",
    providerName: "A",
    model: "gpt-5",
    requests: 0,
    failures: 0,
    promptTokens: 0,
    completionTokens: 0,
    monthlyTokenLimit: null,
    lastUsedAt: null,
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("normalizeRoutes", () => {
  test("fills in every slot", () => {
    expect(normalizeRoutes({ default: ["a"] })).toEqual({ ...emptyRoutes(), default: ["a"] });
    expect(normalizeRoutes(null)).toEqual(emptyRoutes());
  });

  test("copies the lists", () => {
    const routes = { fast: ["a", "b"] };
    const normalized = normalizeRoutes(routes);
    normalized.fast.push("c");
    expect(routes.fast).toEqual(["a", "b"]);
  });
});

describe("hasPrimarySlot", () => {
  test.each(["default", "image", "audio"] as const)("%s has an upstream primary", (slot) => {
    expect(hasPrimarySlot(slot)).toBe(true);
  });

  test.each(["planner", "fast", "embedding"] as const)("%s has none", (slot) => {
    expect(hasPrimarySlot(slot)).toBe(false);
  });
});

describe("primaryProviderId", () => {
  test("maps the upstream slots to their primary", () => {
    const primaries = settings({ defaultProviderId: "a", imageProviderId: "b", audioProviderId: "c" });
    expect(primaryProviderId(primaries, "default")).toBe("a");
    expect(primaryProviderId(primaries, "image")).toBe("b");
    expect(primaryProviderId(primaries, "audio")).toBe("c");
  });

  test.each(["planner", "fast", "embedding"] as const)("%s has no primary", (slot) => {
    expect(primaryProviderId(settings({ defaultProviderId: "a" }), slot)).toBeNull();
  });
});

describe("usableFallbacks", () => {
  test("keeps the order", () => {
    expect(usableFallbacks(["c", "a", "b"], providers)).toEqual(["c", "a", "b"]);
  });

  test("excludes the primary", () => {
    expect(usableFallbacks(["c", "a", "b"], providers, "a")).toEqual(["c", "b"]);
  });

  test("drops unknown providers and duplicates", () => {
    expect(usableFallbacks(["b", "deleted", "b", "a"], providers)).toEqual(["b", "a"]);
  });
});

describe("apiKeyDestination", () => {
  const saved = { protocol: "openai", baseUrl: "http://localhost:11434/v1", requestHeaders: { "X-A": "1", "X-B": "2" } };

  test("ignores the header order and a blank base URL's form", () => {
    expect(apiKeyDestination({ ...saved, requestHeaders: { "X-B": "2", "X-A": "1" } })).toBe(apiKeyDestination(saved));
    expect(apiKeyDestination({ baseUrl: "", requestHeaders: {} })).toBe(apiKeyDestination({ protocol: "openai", baseUrl: null }));
  });

  test.each([
    { protocol: "anthropic" },
    { baseUrl: "http://other-host:11434/v1" },
    { requestHeaders: { "X-A": "1" } },
  ])("changes with %j", (change) => {
    expect(apiKeyDestination({ ...saved, ...change })).not.toBe(apiKeyDestination(saved));
  });
});

describe("baseUrlHasQueryOrFragment", () => {
  test.each([
    ["https://api.example.com/v1", false],
    ["", false],
    [null, false],
    ["https://api.example.com/v1?key=abc", true],
    ["https://api.example.com/v1#models", true],
  ])("%s -> %s", (baseUrl, expected) => {
    expect(baseUrlHasQueryOrFragment(baseUrl)).toBe(expected);
  });
});

describe("buildRoutesPayload", () => {
  test("replaces every slot, without each slot's primary", () => {
    const routes = {
      ...emptyRoutes(),
      default: ["a", "b"],
      image: ["c", "deleted"],
      fast: ["a", "c"],
    };

    // "fast" has no primary, so the default primary "a" stays in its list
    expect(buildRoutesPayload(routes, settings({ defaultProviderId: "a" }))).toEqual({
      routes: {
        default: ["b"],
        image: ["c"],
        audio: [],
        planner: [],
        fast: ["a", "c"],
        embedding: [],
      },
    });
  });
});

describe("usage helpers", () => {
  test("tokens are prompt plus completion tokens", () => {
    expect(usedTokens(usageRow({ promptTokens: 120, completionTokens: 30 }))).toBe(150);
  });

  test("limit usage is null without a limit", () => {
    expect(limitUsage(usageRow({ promptTokens: 10 }))).toBeNull();
  });

  test("limit usage is the share of the limit used", () => {
    expect(limitUsage(usageRow({ promptTokens: 300, completionTokens: 100, monthlyTokenLimit: 1000 }))).toBe(0.4);
    expect(limitUsage(usageRow({ promptTokens: 1500, monthlyTokenLimit: 1000 }))).toBe(1.5);
  });

  test.each([
    [null, undefined],
    [0.5, undefined],
    [0.8, "warning"],
    [0.99, "warning"],
    [1, "error"],
    [1.5, "error"],
  ])("limit usage %s is colored %s", (fraction, color) => {
    expect(limitUsageColor(fraction)).toBe(color);
  });

  test.each([
    [0, "0%"],
    [0.4, "40%"],
    [0.4237, "42.4%"],
    [1.5, "150%"],
  ])("formats %s as %s", (fraction, expected) => {
    expect(formatPercent(fraction, "en-US")).toBe(expected);
  });

  test("only providers that were called get a row", () => {
    const used = usageRow({ providerId: "b", requests: 3 });
    const deleted = usageRow({ providerId: null, providerName: "Old", requests: 1 });
    const summary: AIUsageSummary = {
      start: "2026-10-01T00:00:00Z",
      end: "2026-11-01T00:00:00Z",
      byProvider: [usageRow(), used, deleted],
      byDay: [],
    };

    expect(usageRows(summary)).toEqual([used, deleted]);
    expect(usageRows({ ...summary, byProvider: [usageRow()] })).toEqual([]);
    expect(usageRows(null)).toEqual([]);
  });
});

describe("useAIProviderRoutes", () => {
  test("loads the routes with every slot present", async () => {
    getRoutes.mockResolvedValue({ data: { routes: { default: ["b"] } } });
    const { routes, loadFailed, load } = useAIProviderRoutes();

    await load();

    expect(routes.value).toEqual({ ...emptyRoutes(), default: ["b"] });
    expect(loadFailed.value).toBe(false);
  });

  test("saves the cleaned-up routes", async () => {
    getRoutes.mockResolvedValue({ data: { routes: { default: ["a", "b"] } } });
    updateRoutes.mockResolvedValue({ data: { routes: { default: ["b"] } } });
    const { routes, load, save } = useAIProviderRoutes();
    await load();

    expect(await save(settings({ defaultProviderId: "a" }))).toBe(true);
    expect(updateRoutes).toHaveBeenCalledWith({ routes: { ...emptyRoutes(), default: ["b"] } });
    expect(routes.value).toEqual({ ...emptyRoutes(), default: ["b"] });
  });

  test("reports a failed save", async () => {
    getRoutes.mockResolvedValue({ data: { routes: {} } });
    updateRoutes.mockResolvedValue({ data: null, error: new Error("nope") });
    const { load, save } = useAIProviderRoutes();
    await load();

    expect(await save(settings())).toBe(false);
  });

  test("doesn't save routes that never loaded", async () => {
    getRoutes.mockResolvedValue({ data: null, error: new Error("nope") });
    const { routes, loadFailed, load, save } = useAIProviderRoutes();
    await load();

    expect(routes.value).toBeNull();
    expect(loadFailed.value).toBe(true);
    expect(await save(settings())).toBe(true);
    expect(updateRoutes).not.toHaveBeenCalled();
  });
});

describe("useAIProviderKeyStatus", () => {
  test("lists the providers whose key can't be read", async () => {
    getAll.mockResolvedValue({
      data: [
        { id: "a", name: "A", model: "gpt-5", apiKeySet: true },
        { id: "b", name: "B", model: "gpt-5", apiKeySet: false },
      ],
    });
    const { unreadableIds, load } = useAIProviderKeyStatus();

    await load();

    expect(unreadableIds.value).toEqual(["b"]);
  });

  test("keeps the last list when loading fails", async () => {
    getAll.mockResolvedValueOnce({ data: [{ id: "b", name: "B", model: "gpt-5", apiKeySet: false }] });
    getAll.mockResolvedValueOnce({ data: null, error: new Error("nope") });
    const { unreadableIds, load } = useAIProviderKeyStatus();

    await load();
    await load();

    expect(unreadableIds.value).toEqual(["b"]);
  });
});

describe("useAIProviderUsage", () => {
  const summary: AIUsageSummary = {
    start: "2026-10-01T00:00:00Z",
    end: "2026-11-01T00:00:00Z",
    byProvider: [usageRow({ requests: 2 })],
    byDay: [],
  };

  test("loads this month's usage", async () => {
    getUsage.mockResolvedValue({ data: summary });
    const { usage, failed, load } = useAIProviderUsage();

    await load();

    expect(getUsage).toHaveBeenCalledWith();
    expect(usage.value).toEqual(summary);
    expect(failed.value).toBe(false);
  });

  test("a failed load is flagged, not shown as no usage", async () => {
    getUsage.mockResolvedValue({ data: null, error: new Error("nope") });
    const { usage, failed, loading, load } = useAIProviderUsage();

    await load();

    expect(usage.value).toBeNull();
    expect(failed.value).toBe(true);
    expect(loading.value).toBe(false);
  });

  test("a failed refresh keeps the usage loaded before it", async () => {
    getUsage.mockResolvedValueOnce({ data: summary });
    getUsage.mockResolvedValueOnce({ data: null, error: new Error("nope") });
    getUsage.mockResolvedValueOnce({ data: summary });
    const { usage, failed, load } = useAIProviderUsage();

    await load();
    await load();
    expect(usage.value).toEqual(summary);
    expect(failed.value).toBe(true);

    await load();
    expect(failed.value).toBe(false);
  });
});

describe("useAIProviderModels", () => {
  const query = { protocol: "anthropic" as const, baseUrl: null, timeout: 300 };
  const models = [{ id: "claude-sonnet-5-5", displayName: "Claude Sonnet 5.5", supportsImages: true }];

  test("lists an unsaved provider's models with the given key", async () => {
    listModels.mockResolvedValue({ data: models });
    const { models: loaded, failed, empty, load } = useAIProviderModels();

    await load({ ...query, apiKey: "sk-test" });

    expect(listModels).toHaveBeenCalledWith({ ...query, apiKey: "sk-test" });
    expect(listSavedModels).not.toHaveBeenCalled();
    expect(loaded.value).toEqual(models);
    expect(failed.value).toBe(false);
    expect(empty.value).toBe(false);
  });

  test("lists a saved provider's models", async () => {
    listSavedModels.mockResolvedValue({ data: models });
    const { models: loaded, load } = useAIProviderModels();

    await load(query, "provider-id");

    expect(listSavedModels).toHaveBeenCalledWith("provider-id", query);
    expect(listModels).not.toHaveBeenCalled();
    expect(loaded.value).toEqual(models);
  });

  test("flags a failed load", async () => {
    listModels.mockResolvedValue({
      data: null,
      error: { response: { data: { detail: { message: "AuthenticationError (HTTP 401)" } } } },
    });
    const { models: loaded, failed, empty, load } = useAIProviderModels();

    await load({ ...query, apiKey: "wrong" });

    expect(loaded.value).toEqual([]);
    expect(failed.value).toBe(true);
    expect(empty.value).toBe(false);
  });

  test("flags an empty model list and clears the last result on reload", async () => {
    listModels.mockResolvedValueOnce({ data: null, error: new Error("Network Error") });
    listModels.mockResolvedValueOnce({ data: [] });
    const { failed, empty, load, reset } = useAIProviderModels();

    await load({ ...query, apiKey: "sk-test" });
    await load({ ...query, apiKey: "sk-test" });

    expect(failed.value).toBe(false);
    expect(empty.value).toBe(true);

    reset();
    expect(empty.value).toBe(false);
  });
});
