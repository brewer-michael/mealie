import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { ref } from "vue";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import CardsPage from "./index.vue";
import { resetRecipeIngestSettings } from "~/composables/use-recipe-ingest";
import type { RecipeIngestionSettingsOut } from "~/lib/api/types/recipe-ingest";

const api = vi.hoisted(() => ({ getSettings: vi.fn() }));
const uploads = vi.hoisted(() => ({ closeCardsPage: vi.fn(), openCardsPage: vi.fn(), takeOverQueue: vi.fn() }));
const queueElsewhere = ref(false);
const queueKeptInMemoryElsewhere = ref(false);
const uploadingLeftovers = ref(false);

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
    queueElsewhere,
    queueKeptInMemoryElsewhere,
    uploadingLeftovers,
    takeOverQueue: uploads.takeOverQueue,
  }),
}));

function settings(overrides: Partial<RecipeIngestionSettingsOut> = {}): RecipeIngestionSettingsOut {
  return {
    enabled: true,
    localOnly: false,
    crossRead: false,
    canReadCards: true,
    limitReached: false,
    limitedFeatures: [],
    baseUrlSet: true,
    readerRunning: true,
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

function setVisibility(state: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", { value: state, configurable: true });
  document.dispatchEvent(new Event("visibilitychange"));
}

const route = { params: { groupSlug: "home" }, query: {} as Record<string, string> };
const router = { replace: vi.fn() };

describe("the recipe cards page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetRecipeIngestSettings();
    route.query = {};
    uploads.openCardsPage.mockReturnValue(uploads.closeCardsPage);
    uploads.takeOverQueue.mockResolvedValue(undefined);
    queueElsewhere.value = false;
    queueKeptInMemoryElsewhere.value = false;
    uploadingLeftovers.value = false;
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
    vi.useRealTimers();
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

  test("the limit warning says the cards are read again by themselves, and when", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-04T15:00:00Z"));
    api.getSettings.mockResolvedValue({ data: settings({ limitReached: true }), error: null });
    const wrapper = await mountPage();

    const reset = new Intl.DateTimeFormat("en-US", { dateStyle: "medium", timeStyle: "short" })
      .format(new Date("2026-11-01T00:00:00Z"));
    expect(wrapper.get(".limit-reached").text()).toBe(
      "Every AI provider that reads cards has used its monthly token limit, so cards you add now can't be read yet. "
      + `They're read automatically when the limit resets on ${reset}, or within about 10 minutes after a group manager `
      + "raises it.",
    );
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

  test("warns when nothing on the server reads cards; uploads are still taken", async () => {
    api.getSettings.mockResolvedValue({ data: settings({ readerRunning: false }), error: null });
    const wrapper = await mountPage();

    expect(wrapper.get(".reader-not-running").attributes("data-type")).toBe("warning");
    expect(wrapper.get(".reader-not-running").text()).toBe(
      "Cards are accepted, but nothing on the server is reading them, so they wait. "
      + "A server administrator can check that AI_INGEST_WORKER is on and look at the server's log.",
    );
    expect(wrapper.find(".capture-buttons").exists()).toBe(true);
  });

  test("notes, softly, which optional parts of the read a monthly limit skips, and until when", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-04T15:00:00Z"));
    api.getSettings.mockResolvedValue({ data: settings({ limitedFeatures: ["suggestions", "cross_read"] }), error: null });
    const wrapper = await mountPage();

    const reset = new Intl.DateTimeFormat("en-US", { dateStyle: "medium" }).format(new Date("2026-11-01T00:00:00Z"));
    const note = wrapper.get(".limited-features");
    expect(note.findAll(".limited-feature").map(line => line.text())).toEqual([
      `Tag, category and tool suggestions are off until the monthly token limit resets on ${reset}.`,
      `The second reading is off until the monthly token limit resets on ${reset}.`,
    ]);
    // a note, not a warning
    expect(wrapper.find(".alert").exists()).toBe(false);
  });

  test("shows the household's inbox: photos waiting there and why, and the ones it refused", async () => {
    api.getSettings.mockResolvedValue({
      data: settings({
        inbox: {
          enabled: true,
          folder: "home/family",
          waiting: 4,
          waitingReason: "quota",
          rejections: [{ name: "IMG_0001.jpg", reason: "duplicate", at: "2026-10-04T13:00:00Z" }],
        },
      }),
      error: null,
    });
    const wrapper = await mountPage();

    expect(wrapper.get(".inbox-hint").text()).toBe("Put photos in the home/family folder of the inbox share to scan them.");
    expect(wrapper.get(".inbox-waiting").text())
      .toBe("4 photos are waiting in the inbox: too many of your group's cards are being read. They're added as those finish.");
    expect(wrapper.get(".inbox-rejection").text()).toContain("IMG_0001.jpg: Already scanned");
  });

  test("inbox photos that wait because the group can't read cards show too", async () => {
    api.getSettings.mockResolvedValue({
      data: settings({ canReadCards: false, inbox: { enabled: true, folder: "home/family", waiting: 2, waitingReason: "cannot_read", rejections: [] } }),
      error: null,
    });
    const wrapper = await mountPage();
    expect(wrapper.find(".cannot-read").exists()).toBe(true);
    expect(wrapper.get(".inbox-waiting").text()).toBe("2 photos are waiting in the inbox: AI isn't set up to read recipe cards.");
  });

  test("while it's visible, the page asks for the settings again every minute and when it's back, so its warnings follow", async () => {
    vi.useFakeTimers();
    setVisibility("visible");
    api.getSettings.mockResolvedValue({ data: settings({ readerRunning: false }), error: null });
    const wrapper = await mountPage();
    expect(wrapper.find(".reader-not-running").exists()).toBe(true);
    expect(api.getSettings).toHaveBeenCalledOnce();

    // the reader started
    api.getSettings.mockResolvedValue({ data: settings(), error: null });
    await vi.advanceTimersByTimeAsync(60_000);
    expect(api.getSettings).toHaveBeenCalledTimes(2);
    expect(wrapper.find(".reader-not-running").exists()).toBe(false);

    // hidden: nothing is asked
    setVisibility("hidden");
    await vi.advanceTimersByTimeAsync(180_000);
    expect(api.getSettings).toHaveBeenCalledTimes(2);

    // back: asked at once, then every minute again
    setVisibility("visible");
    await vi.advanceTimersByTimeAsync(0);
    expect(api.getSettings).toHaveBeenCalledTimes(3);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(api.getSettings).toHaveBeenCalledTimes(4);

    // gone from the page: nothing more
    wrapper.unmount();
    wrappers.length = 0;
    await vi.advanceTimersByTimeAsync(180_000);
    expect(api.getSettings).toHaveBeenCalledTimes(4);
  });

  test("a group without AI is told to set it up", async () => {
    api.getSettings.mockResolvedValue({ data: settings({ canReadCards: false }), error: null });
    const wrapper = await mountPage();

    expect(wrapper.get(".cannot-read").text()).toContain("A group manager can set it up in the group settings.");
    expect(wrapper.find(".capture-buttons").exists()).toBe(false);
  });

  test.each([
    ["the group can't read cards", () => ({ data: settings({ canReadCards: false }), error: null })],
    ["scanning is turned off on the server", () => ({ data: settings({ enabled: false, canReadCards: false }), error: null })],
    ["the settings didn't load", () => ({ data: null, error: { message: "Network Error" } })],
  ])("photos already queued still show when %s, so their Retry and Remove can be reached", async (_name, answer) => {
    api.getSettings.mockResolvedValue(answer());
    const wrapper = await mountPage();

    expect(wrapper.find(".capture-buttons").exists()).toBe(false);
    expect(wrapper.findAll(".upload-queue")).toHaveLength(1);
  });

  test("while another tab keeps the queue, the page says so instead of capturing, and Use this tab takes it", async () => {
    api.getSettings.mockResolvedValue({ data: settings(), error: null });
    queueElsewhere.value = true;
    const wrapper = await mountPage();

    expect(wrapper.get(".queue-elsewhere").text()).toContain(
      "Your recipe cards are being added in another tab of this browser. Carry on there, or use this tab instead.",
    );
    expect(wrapper.find(".capture-buttons").exists()).toBe(false);
    expect(wrapper.find(".privacy-chip").exists()).toBe(false);

    await wrapper.get(".queue-elsewhere .btn").trigger("click");
    expect(uploads.takeOverQueue).toHaveBeenCalledOnce();
    // the other tab hands it over
    queueElsewhere.value = false;
    await flushPromises();
    expect(wrapper.find(".queue-elsewhere").exists()).toBe(false);
    expect(wrapper.find(".capture-buttons").exists()).toBe(true);
  });

  test("Use this tab refused by a tab that couldn't save its photos: the page says why they stay there", async () => {
    api.getSettings.mockResolvedValue({ data: settings(), error: null });
    queueElsewhere.value = true;
    const wrapper = await mountPage();
    expect(wrapper.find(".queue-kept-in-memory").exists()).toBe(false);

    queueKeptInMemoryElsewhere.value = true;
    await flushPromises();
    expect(wrapper.get(".queue-kept-in-memory").text()).toBe(
      "That tab couldn't save its photos on this device, so they can't move here. Let them upload there first.",
    );
  });

  test("a tab still uploading photos it couldn't save, after the queue left it, says to keep it open", async () => {
    api.getSettings.mockResolvedValue({ data: settings(), error: null });
    queueElsewhere.value = true;
    uploadingLeftovers.value = true;
    const wrapper = await mountPage();
    expect(wrapper.get(".uploading-leftovers").text()).toBe(
      "This tab is still uploading photos it couldn't save on this device. Keep it open until they're uploaded.",
    );
    expect(wrapper.find(".upload-queue").exists()).toBe(true);
  });

  test("the queue shows once: under the capture buttons when they're there, not before the settings answer", async () => {
    let answer: (value: unknown) => void = () => {};
    api.getSettings.mockReturnValue(new Promise((resolve) => {
      answer = resolve;
    }));
    const wrapper = await mountPage();
    expect(wrapper.find(".upload-queue").exists()).toBe(false);

    answer({ data: settings(), error: null });
    await flushPromises();
    expect(wrapper.findAll(".upload-queue")).toHaveLength(1);
    expect(wrapper.find(".capture .upload-queue").exists()).toBe(true);
  });
});
