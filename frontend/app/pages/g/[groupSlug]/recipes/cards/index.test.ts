import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { ref } from "vue";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import CardsPage from "./index.vue";
import type { RecipeIngestionSettingsOut } from "~/lib/api/types/recipe-ingest";

const api = vi.hoisted(() => ({ getSettings: vi.fn() }));

vi.mock("~/composables/api", () => ({
  useUserApi: () => ({ recipeIngest: api }),
}));
vi.mock("~/composables/use-recipe-ingest-uploads", async importOriginal => ({
  ...await importOriginal<typeof import("~/composables/use-recipe-ingest-uploads")>(),
  useRecipeIngestUploads: () => ({ localOnly: ref(false), sentBeforeLocalOnlyChange: ref(0) }),
}));

function settings(overrides: Partial<RecipeIngestionSettingsOut> = {}): RecipeIngestionSettingsOut {
  return {
    enabled: true,
    localOnly: false,
    crossRead: false,
    canReadCards: true,
    limitReached: false,
    ocrAvailable: false,
    reader: { name: "Claude", local: false, viaOcr: false },
    localOnlyAvailable: false,
    localReadiness: null,
    limits: {
      maxUploadBytes: 104857600,
      maxFileBytes: 31457280,
      maxImagesPerRequest: 20,
      maxPagesPerCard: 4,
      maxPixels: 100000000,
    },
    inbox: { enabled: false, folder: null },
    ...overrides,
  };
}

const slot = (className = "") => ({ template: `<div class="${className}"><slot /></div>` });
const wrappers: VueWrapper[] = [];

async function mountPage() {
  const wrapper = mount(CardsPage, {
    global: {
      stubs: {
        BasePageTitle: slot("title"),
        VContainer: slot(),
        VIcon: { template: "<i />" },
        VProgressLinear: { template: "<div class=\"progress\" />" },
        VAlert: { props: ["type"], template: "<div class=\"alert\" :data-type=\"type\"><slot /></div>" },
        IngestPrivacyChip: { template: "<div class=\"privacy-chip\" />" },
        IngestCapture: { template: "<div class=\"capture-buttons\" />" },
        IngestUploadQueue: { template: "<div class=\"upload-queue\" />" },
        IngestBatchList: { template: "<div class=\"batch-list\" />" },
      },
    },
  });
  wrappers.push(wrapper);
  await flushPromises();
  return wrapper;
}

describe("the recipe cards page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal("definePageMeta", vi.fn());
    vi.stubGlobal("useSeoMeta", vi.fn());
    vi.stubGlobal("useRoute", () => ({ params: { groupSlug: "home" }, query: {} }));
    vi.stubGlobal("useMealieAuth", () => ({ user: ref({ groupSlug: "home" }) }));
  });

  afterEach(() => {
    wrappers.forEach(wrapper => wrapper.unmount());
    wrappers.length = 0;
    vi.unstubAllGlobals();
  });

  test("a group that can read cards gets the capture buttons", async () => {
    api.getSettings.mockResolvedValue({ data: settings(), error: null });
    const wrapper = await mountPage();

    expect(wrapper.find(".capture-buttons").exists()).toBe(true);
    expect(wrapper.find(".alert").exists()).toBe(false);
  });

  test("warns before anything is uploaded when this month's token limit stops cards being read", async () => {
    api.getSettings.mockResolvedValue({ data: settings({ limitReached: true }), error: null });
    const wrapper = await mountPage();

    expect(wrapper.get(".limit-reached").attributes("data-type")).toBe("warning");
    expect(wrapper.get(".limit-reached").text()).toContain("monthly token limit");
    expect(wrapper.find(".capture-buttons").exists()).toBe(true); // uploads are still taken
  });

  test("with scanning turned off on the server, says so rather than asking for a provider", async () => {
    api.getSettings.mockResolvedValue({ data: settings({ enabled: false, canReadCards: false }), error: null });
    const wrapper = await mountPage();

    expect(wrapper.get(".ingest-disabled").text()).toBe("Recipe card scanning is turned off on this server.");
    expect(wrapper.find(".cannot-read").exists()).toBe(false);
    expect(wrapper.find(".capture-buttons").exists()).toBe(false);
  });

  test("a group without AI is told to set it up", async () => {
    api.getSettings.mockResolvedValue({ data: settings({ canReadCards: false }), error: null });
    const wrapper = await mountPage();

    expect(wrapper.get(".cannot-read").text()).toContain("A group manager can set it up in the group settings.");
    expect(wrapper.find(".capture-buttons").exists()).toBe(false);
  });
});
