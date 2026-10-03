import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import GroupAIProviderUsage from "./GroupAIProviderUsage.vue";
import type { AIUsageProviderSummary, AIUsageSummary } from "~/lib/api/types/group";

const getUsage = vi.hoisted(() => vi.fn());

vi.mock("~/composables/api", () => ({
  useUserApi: () => ({ aiProviders: { getUsage } }),
}));

const wrappers: VueWrapper[] = [];
const slotStub = { template: "<div><slot /></div>" };

function summary(rows: Partial<AIUsageProviderSummary>[]): AIUsageSummary {
  return {
    start: "2026-10-01T00:00:00Z",
    end: "2026-11-01T00:00:00Z",
    byProvider: rows.map(row => ({
      providerId: "a",
      providerName: "A",
      model: "gpt-5",
      requests: 1,
      failures: 0,
      promptTokens: 0,
      completionTokens: 0,
      monthlyTokenLimit: null,
      lastUsedAt: null,
      ...row,
    })),
    byDay: [],
  };
}

async function mountUsage() {
  const wrapper = mount(GroupAIProviderUsage, {
    global: {
      mocks: { $globals: { icons: {} } },
      stubs: {
        BaseCardSectionTitle: slotStub,
        AppLoader: { template: "<div class=\"loader\" />" },
        VBtn: { template: "<button type=\"button\" />" },
        VAlert: { template: "<div class=\"alert\"><slot /></div>" },
        VCardText: { template: "<div class=\"empty\"><slot /></div>" },
        VTable: { template: "<table><slot /></table>" },
      },
    },
  });
  wrappers.push(wrapper);
  await flushPromises();
  return wrapper;
}

describe("GroupAIProviderUsage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    wrappers.forEach(wrapper => wrapper.unmount());
    wrappers.length = 0;
  });

  test("shows the limit used next to the limit", async () => {
    getUsage.mockResolvedValue({
      data: summary([{ promptTokens: 300, completionTokens: 100, monthlyTokenLimit: 1000 }, { providerId: "b" }]),
    });
    const wrapper = await mountUsage();

    const rows = wrapper.findAll("tbody tr");
    expect(rows).toHaveLength(2);
    expect(rows[0]?.text()).toContain("40% of 1,000");
    expect(rows[1]?.text()).toContain("No limit");
  });

  test("says when there's no usage", async () => {
    getUsage.mockResolvedValue({ data: summary([]) });
    const wrapper = await mountUsage();

    expect(wrapper.get(".empty").text()).toBe("No AI requests have been made this month.");
    expect(wrapper.find(".alert").exists()).toBe(false);
  });

  test("a failed load isn't shown as no usage", async () => {
    getUsage.mockResolvedValue({ data: null, error: new Error("nope") });
    const wrapper = await mountUsage();

    expect(wrapper.get(".alert").text()).toBe("Couldn't load usage");
    expect(wrapper.find(".empty").exists()).toBe(false);
  });

  test("a failed refresh keeps the loaded usage", async () => {
    getUsage.mockResolvedValueOnce({ data: summary([{}]) });
    getUsage.mockResolvedValueOnce({ data: null, error: new Error("nope") });
    const wrapper = await mountUsage();

    await (wrapper.vm as unknown as { load: () => Promise<void> }).load();
    await flushPromises();

    expect(getUsage).toHaveBeenCalledTimes(2);
    expect(wrapper.find(".alert").exists()).toBe(true);
    expect(wrapper.findAll("tbody tr")).toHaveLength(1);
  });
});
