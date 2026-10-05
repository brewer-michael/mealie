import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { defineComponent, ref } from "vue";
import {
  NAV_REFRESH_INTERVAL_MS,
  resetRecipeIngestCounts,
  resetRecipeIngestSettings,
  useRecipeIngestNav,
  useRecipeIngestSettings,
} from "../use-recipe-ingest";
import type { RecipeIngestionSettingsOut } from "~/lib/api/types/recipe-ingest";

const api = vi.hoisted(() => ({ getSettings: vi.fn(), getCounts: vi.fn() }));
const toast = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn(), info: vi.fn() }));

vi.mock("~/composables/api", () => ({
  useUserApi: () => ({ recipeIngest: api }),
}));
vi.mock("~/composables/use-toast", () => ({
  alert: toast,
}));

function settings(overrides: Partial<RecipeIngestionSettingsOut> = {}): RecipeIngestionSettingsOut {
  return {
    enabled: true,
    localOnly: false,
    canReadCards: true,
    limits: { maxUploadBytes: 1, maxFileBytes: 1, maxImagesPerRequest: 20, maxPagesPerCard: 4, maxPixels: 1, maxJpegPixels: 1 },
    ...overrides,
  };
}

function counts(ready = 0, failed = 0, processing = 0, waiting = 0) {
  return { data: { ready, failed, processing, needsAttention: 0, waiting }, error: null };
}

const active = ref(true);
const routePath = ref("/g/home");
const failedWhileAway = ref(0);
const openCards = vi.fn();
const wrappers: VueWrapper[] = [];

async function mountNav() {
  let nav!: ReturnType<typeof useRecipeIngestNav>;
  const wrapper = mount(defineComponent({
    setup() {
      nav = useRecipeIngestNav({ active, routePath, failedWhileAway, openCards });
      return () => null;
    },
  }));
  wrappers.push(wrapper);
  await flushPromises();
  return nav;
}

function setVisibility(state: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", { value: state, configurable: true });
  document.dispatchEvent(new Event("visibilitychange"));
}

describe("the layout's recipe card entries", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetRecipeIngestSettings();
    resetRecipeIngestCounts();
    active.value = true;
    routePath.value = "/g/home";
    failedWhileAway.value = 0;
    setVisibility("visible");
    api.getSettings.mockResolvedValue({ data: settings(), error: null });
    api.getCounts.mockResolvedValue(counts(3));
  });

  afterEach(() => {
    wrappers.forEach(wrapper => wrapper.unmount());
    wrappers.length = 0;
    vi.useRealTimers();
  });

  test("a group that can read cards: both entries, and the ready count", async () => {
    const nav = await mountNav();
    expect(nav.showCardsLink.value).toBe(true);
    expect(nav.showScanLink.value).toBe(true);
    expect(nav.cardsTitle.value).toBe("Recipe cards (3)");
    expect(nav.cardsBadge.value).toBeNull();
  });

  test("not in their own group: nothing, and nothing asked", async () => {
    active.value = false;
    const nav = await mountNav();
    expect(nav.showCardsLink.value).toBe(false);
    expect(api.getSettings).not.toHaveBeenCalled();
    expect(api.getCounts).not.toHaveBeenCalled();
  });

  test("scanning turned off on the server: no entries, and no count requests (they'd be refused)", async () => {
    api.getSettings.mockResolvedValue({ data: settings({ enabled: false, canReadCards: false }), error: null });
    const nav = await mountNav();
    routePath.value = "/g/home/recipes/finder";
    window.dispatchEvent(new Event("focus"));
    await flushPromises();

    expect(nav.showCardsLink.value).toBe(false);
    expect(nav.showScanLink.value).toBe(false);
    expect(api.getCounts).not.toHaveBeenCalled();
  });

  test("cards can't be read any more, but some wait: the sidebar entry stays, the Create item goes", async () => {
    api.getSettings.mockResolvedValue({ data: settings({ canReadCards: false }), error: null });
    api.getCounts.mockResolvedValue(counts(0, 1));
    const nav = await mountNav();
    expect(nav.showCardsLink.value).toBe(true);
    expect(nav.showScanLink.value).toBe(false);

    // cards waiting for a monthly limit are open too (they aren't counted as failed)
    api.getCounts.mockResolvedValue(counts(0, 0, 0, 2));
    vi.setSystemTime(Date.now() + NAV_REFRESH_INTERVAL_MS);
    routePath.value = "/g/home/recipes";
    await flushPromises();
    expect(nav.showCardsLink.value).toBe(true);

    api.getCounts.mockResolvedValue(counts(0, 0));
    vi.setSystemTime(Date.now() + 2 * NAV_REFRESH_INTERVAL_MS);
    routePath.value = "/g/home/recipes/cards";
    await flushPromises();
    expect(nav.showCardsLink.value).toBe(false);
    vi.useRealTimers();
  });

  test("a group manager's change (the settings card reloads the settings) reaches the entries", async () => {
    api.getSettings.mockResolvedValueOnce({ data: settings({ canReadCards: false }), error: null });
    api.getCounts.mockResolvedValue(counts(0));
    const nav = await mountNav();
    expect(nav.showScanLink.value).toBe(false);

    // the group settings page, after an image provider was added
    api.getSettings.mockResolvedValueOnce({ data: settings({ canReadCards: true }), error: null });
    await useRecipeIngestSettings().load();
    expect(nav.showScanLink.value).toBe(true);
    expect(nav.showCardsLink.value).toBe(true);
  });

  test("settings that failed to load are tried again on the next page", async () => {
    api.getSettings.mockResolvedValueOnce({ data: null, error: { message: "Network Error" } });
    const nav = await mountNav();
    expect(nav.showCardsLink.value).toBe(false);
    expect(api.getCounts).not.toHaveBeenCalled();

    routePath.value = "/g/home/recipes/finder";
    await flushPromises();
    expect(api.getSettings).toHaveBeenCalledTimes(2);
    expect(nav.showCardsLink.value).toBe(true);
    expect(nav.cardsTitle.value).toBe("Recipe cards (3)");
  });

  test("the count is refreshed on focus, on coming back and on page changes, at most every 30 s", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    const nav = await mountNav();
    expect(api.getCounts).toHaveBeenCalledOnce();

    // cards were read meanwhile (a Shortcut, the inbox, another phone)
    api.getCounts.mockResolvedValue(counts(5));
    window.dispatchEvent(new Event("focus"));
    routePath.value = "/g/home/x";
    await flushPromises();
    expect(api.getCounts).toHaveBeenCalledOnce();

    vi.setSystemTime(Date.now() + NAV_REFRESH_INTERVAL_MS);
    window.dispatchEvent(new Event("focus"));
    await flushPromises();
    expect(api.getCounts).toHaveBeenCalledTimes(2);
    expect(nav.cardsTitle.value).toBe("Recipe cards (5)");

    vi.setSystemTime(Date.now() + NAV_REFRESH_INTERVAL_MS);
    setVisibility("visible");
    await flushPromises();
    expect(api.getCounts).toHaveBeenCalledTimes(3);

    vi.setSystemTime(Date.now() + NAV_REFRESH_INTERVAL_MS);
    routePath.value = "/g/home/y";
    await flushPromises();
    expect(api.getCounts).toHaveBeenCalledTimes(4);
  });

  test("an upload that failed for good while no cards page was open: one toast to open them, and a red badge", async () => {
    api.getSettings.mockResolvedValue({ data: settings({ canReadCards: false }), error: null });
    api.getCounts.mockResolvedValue(counts(0));
    const nav = await mountNav();
    expect(nav.showCardsLink.value).toBe(false);

    failedWhileAway.value = 2;
    await flushPromises();
    expect(toast.error).toHaveBeenCalledOnce();
    const [text, , options] = toast.error.mock.calls[0]!;
    expect(text).toBe("2 cards couldn't be uploaded.");
    expect(options.action.message).toBe("Open recipe cards");
    options.action.onClick();
    expect(openCards).toHaveBeenCalledOnce();
    // the entry shows, with the badge, until the cards page opens
    expect(nav.showCardsLink.value).toBe(true);
    expect(nav.cardsBadge.value).toEqual({ content: 2, color: "error", label: "2 cards couldn't be uploaded." });

    failedWhileAway.value = 0;
    await flushPromises();
    expect(nav.cardsBadge.value).toBeNull();
    expect(toast.error).toHaveBeenCalledOnce();
  });
});
