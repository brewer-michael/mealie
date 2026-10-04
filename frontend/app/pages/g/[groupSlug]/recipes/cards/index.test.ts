import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { ref } from "vue";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import CardsPage from "./index.vue";
import { resetRecipeIngestSettings } from "~/composables/use-recipe-ingest";
import type { RecipeIngestionSettingsOut } from "~/lib/api/types/recipe-ingest";

const api = vi.hoisted(() => ({ getSettings: vi.fn() }));
const uploads = vi.hoisted(() => ({ closeCardsPage: vi.fn(), openCardsPage: vi.fn() }));

vi.mock("~/composables/api", () => ({
  useUserApi: () => ({ recipeIngest: api }),
}));
vi.mock("~/composables/use-recipe-ingest-uploads", async importOriginal => ({
  ...await importOriginal<typeof import("~/composables/use-recipe-ingest-uploads")>(),
  useRecipeIngestUploads: () => ({
    localOnly: ref(false),
    sentBeforeLocalOnlyChange: ref(0),
    localOnlyFinishedBatch: ref(false),
    openCardsPage: uploads.openCardsPage,
  }),
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
      maxJpegPixels: 256000000,
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
        VAlert: {
          props: ["type"],
          emits: ["click:close"],
          template: `<div class="alert" :data-type="type"><slot /><slot name="append" />
            <button class="alert-close" @click="$emit('click:close')" /></div>`,
        },
        VBtn: { template: "<button type=\"button\" class=\"btn\"><slot /></button>" },
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

const route = { params: { groupSlug: "home" }, query: {} as Record<string, string> };
const router = { replace: vi.fn() };

describe("the recipe cards page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetRecipeIngestSettings();
    route.query = {};
    uploads.openCardsPage.mockReturnValue(uploads.closeCardsPage);
    vi.stubGlobal("definePageMeta", vi.fn());
    vi.stubGlobal("useSeoMeta", vi.fn());
    vi.stubGlobal("useRoute", () => route);
    vi.stubGlobal("useRouter", () => router);
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
    // every list and count request would be refused with a toast: the list isn't there to make them
    expect(wrapper.find(".batch-list").exists()).toBe(false);
  });

  test("the list waits for the settings, and shows when they say scanning is on", async () => {
    let answer: (value: unknown) => void = () => {};
    api.getSettings.mockReturnValue(new Promise((resolve) => {
      answer = resolve;
    }));
    const wrapper = await mountPage();
    expect(wrapper.find(".batch-list").exists()).toBe(false);

    answer({ data: settings(), error: null });
    await flushPromises();
    expect(wrapper.find(".batch-list").exists()).toBe(true);
  });

  test("settings that fail to load can be tried again", async () => {
    api.getSettings.mockResolvedValueOnce({ data: null, error: { message: "Network Error" } });
    const wrapper = await mountPage();
    expect(wrapper.get(".settings-load-failed").text()).toContain("Couldn't load the recipe card settings");
    expect(wrapper.find(".capture-buttons").exists()).toBe(false);
    // the list says itself whether it can load
    expect(wrapper.find(".batch-list").exists()).toBe(true);

    api.getSettings.mockResolvedValueOnce({ data: settings(), error: null });
    await wrapper.get(".settings-load-failed .btn").trigger("click");
    await flushPromises();
    expect(api.getSettings).toHaveBeenCalledTimes(2);
    expect(wrapper.find(".settings-load-failed").exists()).toBe(false);
    expect(wrapper.find(".capture-buttons").exists()).toBe(true);
  });

  test("a group that keeps cards on this server with nothing local to read them can't capture, and is told why", async () => {
    api.getSettings.mockResolvedValue({ data: settings({ localOnly: true, localOnlyAvailable: false }), error: null });
    const wrapper = await mountPage();

    expect(wrapper.get(".local-only-blocked").attributes("data-type")).toBe("warning");
    expect(wrapper.get(".local-only-blocked").text()).toBe(
      "Your group keeps cards on this server, but no AI provider on your network can read them. Ask a group manager to add one.",
    );
    expect(wrapper.find(".capture-buttons").exists()).toBe(false);
    expect(wrapper.find(".privacy-chip").exists()).toBe(true);
    expect(wrapper.find(".upload-queue").exists()).toBe(true);
  });

  test("a notification's batch that can't be opened is said to be gone, until dismissed", async () => {
    api.getSettings.mockResolvedValue({ data: settings(), error: null });
    route.query = { unavailable: "1", other: "x" };
    const wrapper = await mountPage();

    expect(wrapper.get(".batch-unavailable").text()).toContain("That batch isn't available any more.");
    await wrapper.get(".batch-unavailable .alert-close").trigger("click");
    expect(router.replace).toHaveBeenCalledWith({ query: { other: "x" } });
  });

  test("while it's open, failed uploads show here instead of in a toast", async () => {
    api.getSettings.mockResolvedValue({ data: settings(), error: null });
    const wrapper = await mountPage();
    expect(uploads.openCardsPage).toHaveBeenCalledOnce();
    expect(uploads.closeCardsPage).not.toHaveBeenCalled();
    wrapper.unmount();
    wrappers.length = 0;
    expect(uploads.closeCardsPage).toHaveBeenCalledOnce();
  });

  test("a group without AI is told to set it up", async () => {
    api.getSettings.mockResolvedValue({ data: settings({ canReadCards: false }), error: null });
    const wrapper = await mountPage();

    expect(wrapper.get(".cannot-read").text()).toContain("A group manager can set it up in the group settings.");
    expect(wrapper.find(".capture-buttons").exists()).toBe(false);
  });
});
