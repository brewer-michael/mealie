import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { ref } from "vue";
import DefaultLayout from "./DefaultLayout.vue";
import { resetRecipeIngestCounts, resetRecipeIngestSettings } from "~/composables/use-recipe-ingest";
import { resetRecipeIngestUploads, useRecipeIngestUploads } from "~/composables/use-recipe-ingest-uploads";
import type { RecipeIngestionSettingsOut } from "~/lib/api/types/recipe-ingest";

/**
 * The layout's fork hook (docs/ai/PHASE2.md §1.1): the sidebar's "Recipe cards (N)" and the Create menu's "Scan recipe
 * cards", and the user's upload queue connected to them. Everything upstream around them is stubbed.
 */
const api = vi.hoisted(() => ({
  getSettings: vi.fn(),
  getCounts: vi.fn(),
  upload: vi.fn(),
  createBatch: vi.fn(),
  sealBatch: vi.fn(),
}));
const toast = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn(), info: vi.fn() }));
const loggedIn = await vi.hoisted(async () => {
  const { ref: vueRef } = await import("vue");
  return { isOwnGroup: vueRef(true) };
});

vi.mock("~/composables/api", () => ({
  useUserApi: () => ({ recipeIngest: api }),
}));
vi.mock("~/composables/use-toast", () => ({ alert: toast }));
vi.mock("~/composables/use-logged-in-state", () => ({
  useLoggedInState: () => ({ loggedIn: ref(true), isOwnGroup: loggedIn.isOwnGroup }),
}));
vi.mock("~/composables/use-groups", () => ({
  useGroupSelf: () => ({ group: ref({ aiProviderSettings: { aiEnabled: true } }) }),
}));
vi.mock("~/composables/use-users/preferences", () => ({
  useCookbookPreferences: () => ref({ hideOtherHouseholds: false }),
}));
vi.mock("~/composables/store/use-cookbook-store", () => ({
  useCookbookStore: () => ({ store: ref([]) }),
  usePublicCookbookStore: () => ({ store: ref([]) }),
}));

function settings(overrides: Partial<RecipeIngestionSettingsOut> = {}): RecipeIngestionSettingsOut {
  return {
    enabled: true,
    canReadCards: true,
    limits: { maxUploadBytes: 1, maxFileBytes: 1, maxImagesPerRequest: 20, maxPagesPerCard: 4, maxPixels: 1, maxJpegPixels: 1 },
    ...overrides,
  };
}

const router = { push: vi.fn() };
const wrappers: VueWrapper[] = [];

/** What the sidebar is given, and the Create menu's items as rendered */
async function mountLayout() {
  const wrapper = mount(DefaultLayout, {
    global: {
      mocks: { $globals: { icons: new Proxy({}, { get: (_target, name) => String(name) }) }, $vuetify: { theme: { current: { dark: false } } } },
      stubs: {
        TheSnackbar: true,
        AppHeader: true,
        NuxtPage: true,
        VApp: { template: "<div><slot /></div>" },
        VMain: { template: "<main><slot /></main>" },
        VScrollXTransition: { template: "<div><slot /></div>" },
        VBtn: true,
        VIcon: true,
        VDivider: true,
        VMenu: { template: "<div class=\"menu\"><slot name=\"activator\" :props=\"{}\" /><slot /></div>" },
        VList: { template: "<div><slot /></div>" },
        VListItem: { props: ["to"], template: "<a class=\"create-link\" :href=\"to\"><slot /></a>" },
        VListItemTitle: { template: "<span class=\"create-title\"><slot /></span>" },
        VListItemSubtitle: true,
        AppSidebar: {
          props: ["topLink"],
          template: `<nav><slot /><div v-for="link in topLink" :key="link.title" class="top-link" :data-to="link.to"
            :data-badge="link.badge ? link.badge.content : ''">{{ link.title }}</div></nav>`,
        },
      },
    },
  });
  wrappers.push(wrapper);
  await flushPromises();
  return wrapper;
}

function topLinks(wrapper: VueWrapper) {
  return wrapper.findAll(".top-link").map(link => ({ title: link.text(), badge: link.attributes("data-badge") }));
}

function createTitles(wrapper: VueWrapper) {
  return wrapper.findAll(".create-title").map(title => title.text());
}

describe("DefaultLayout's recipe card entries", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetRecipeIngestSettings();
    resetRecipeIngestCounts();
    resetRecipeIngestUploads();
    loggedIn.isOwnGroup.value = true;
    vi.stubGlobal("useNuxtApp", () => ({ $globals: { icons: new Proxy({}, { get: (_target, name) => String(name) }) } }));
    vi.stubGlobal("useDisplay", () => ({ lgAndUp: ref(true) }));
    vi.stubGlobal("useMealieAuth", () => ({ user: ref({ id: "u1", groupSlug: "home", householdId: "h1" }) }));
    vi.stubGlobal("useRoute", () => ({ params: { groupSlug: "home" }, path: "/g/home" }));
    vi.stubGlobal("useRouter", () => router);
    api.getSettings.mockResolvedValue({ data: settings(), error: null });
    api.getCounts.mockResolvedValue({ data: { ready: 2, failed: 0, processing: 0, needsAttention: 0 }, error: null });
  });

  afterEach(() => {
    wrappers.forEach(wrapper => wrapper.unmount());
    wrappers.length = 0;
    resetRecipeIngestUploads();
    vi.unstubAllGlobals();
  });

  test("a group that can read cards: the sidebar entry with the ready count, and the Create item", async () => {
    const wrapper = await mountLayout();
    expect(topLinks(wrapper)).toContainEqual({ title: "Recipe cards (2)", badge: "" });
    expect(wrapper.find(".top-link[data-to='/g/home/recipes/cards']").exists()).toBe(true);
    expect(createTitles(wrapper)).toContain("Scan recipe cards");
  });

  test("a group that can't read cards, with none waiting: neither", async () => {
    api.getSettings.mockResolvedValue({ data: settings({ canReadCards: false }), error: null });
    api.getCounts.mockResolvedValue({ data: { ready: 0, failed: 0, processing: 0 }, error: null });
    const wrapper = await mountLayout();
    expect(topLinks(wrapper).map(link => link.title)).not.toContain("Recipe cards");
    expect(createTitles(wrapper)).not.toContain("Scan recipe cards");
  });

  test("cards waiting although new ones can't be read: the sidebar entry only", async () => {
    api.getSettings.mockResolvedValue({ data: settings({ canReadCards: false }), error: null });
    api.getCounts.mockResolvedValue({ data: { ready: 1, failed: 1, processing: 0 }, error: null });
    const wrapper = await mountLayout();
    expect(topLinks(wrapper)).toContainEqual({ title: "Recipe cards (1)", badge: "" });
    expect(createTitles(wrapper)).not.toContain("Scan recipe cards");
  });

  test("scanning turned off on the server: no entry, and no count request", async () => {
    api.getSettings.mockResolvedValue({ data: settings({ enabled: false, canReadCards: false }), error: null });
    const wrapper = await mountLayout();
    expect(topLinks(wrapper).map(link => link.title)).not.toContain("Recipe cards");
    expect(api.getCounts).not.toHaveBeenCalled();
  });

  test("an upload that fails for good on another page: a red badge on the entry, and a toast that opens the cards", async () => {
    api.upload.mockResolvedValue({ data: null, error: { response: { status: 400, data: { detail: { code: "ai_not_enabled" } } } } });
    api.createBatch.mockResolvedValue({ data: { id: "b1", source: "app" }, error: null });
    const wrapper = await mountLayout();

    useRecipeIngestUploads().takePhoto(new File(["x"], "IMG_1.jpg", { type: "image/jpeg" }));
    await flushPromises();
    expect(topLinks(wrapper)).toContainEqual({ title: "Recipe cards (2)", badge: "1" });
    expect(toast.error).toHaveBeenCalledOnce();
    toast.error.mock.calls[0]![2].action.onClick();
    expect(router.push).toHaveBeenCalledWith("/g/home/recipes/cards");

    // the cards page opened: it shows the card itself
    const close = useRecipeIngestUploads().openCardsPage();
    await flushPromises();
    expect(topLinks(wrapper)).toContainEqual({ title: "Recipe cards (2)", badge: "" });
    close();
  });
});
