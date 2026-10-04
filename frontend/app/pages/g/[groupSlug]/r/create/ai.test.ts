import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { ref } from "vue";
import AiImportPage from "./ai.vue";
import { resetRecipeIngestSettings } from "~/composables/use-recipe-ingest";

/** The AI import page's fork hook (docs/ai/PHASE2.md §1.1): the link to scan a stack of cards */
const api = vi.hoisted(() => ({ getSettings: vi.fn() }));

vi.mock("~/composables/api", () => ({
  useUserApi: () => ({ recipeIngest: api, recipes: {} }),
}));
vi.mock("~/composables/use-groups", () => ({
  useGroupSelf: () => ({ group: ref({ aiProviderSettings: { aiEnabled: true, imageProviderEnabled: true } }) }),
}));
vi.mock("~/composables/store/use-tag-store", () => ({ useTagStore: () => ({ store: ref([]) }) }));
vi.mock("~/composables/use-new-recipe-options", () => ({
  useNewRecipeOptions: () => ({
    stayInEditMode: ref(false),
    parseRecipe: ref(false),
    translateRecipe: ref(false),
    createNewOrganizers: ref(false),
    navigateToRecipe: vi.fn(),
  }),
}));
vi.mock("~/composables/use-validators", () => ({ validators: { urlOptional: () => true } }));

const wrappers: VueWrapper[] = [];

async function mountPage() {
  const wrapper = mount(AiImportPage, {
    shallow: true,
    global: {
      renderStubDefaultSlot: true,
      mocks: { $globals: { icons: new Proxy({}, { get: (_target, name) => String(name) }) } },
      stubs: { RouterLink: { props: ["to"], template: "<a class=\"router-link\" :href=\"to\"><slot /></a>" } },
    },
  });
  wrappers.push(wrapper);
  await flushPromises();
  return wrapper;
}

const scanLink = (wrapper: VueWrapper) => wrapper.find(".router-link.scan-cards");

describe("the AI import page's recipe card link", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetRecipeIngestSettings();
    vi.stubGlobal("definePageMeta", vi.fn());
    vi.stubGlobal("useMealieAuth", () => ({ user: ref({ groupSlug: "home" }) }));
    vi.stubGlobal("useRoute", () => ({ params: { groupSlug: "home" }, path: "/g/home/r/create/ai" }));
  });

  afterEach(() => {
    wrappers.forEach(wrapper => wrapper.unmount());
    wrappers.length = 0;
    vi.unstubAllGlobals();
  });

  test("shows when the group can read cards", async () => {
    api.getSettings.mockResolvedValue({ data: { enabled: true, canReadCards: true }, error: null });
    const link = scanLink(await mountPage());
    expect(link.attributes("href")).toBe("/g/home/recipes/cards");
    expect(link.text()).toBe("Have a stack of cards? Scan them in a batch");
  });

  test.each([
    ["scanning is turned off on the server", { enabled: false, canReadCards: false }],
    ["the group can't read cards", { enabled: true, canReadCards: false }],
  ])("doesn't show when %s", async (_name, settings) => {
    api.getSettings.mockResolvedValue({ data: settings, error: null });
    expect(scanLink(await mountPage()).exists()).toBe(false);
  });

  test("doesn't show while the card settings can't be loaded", async () => {
    api.getSettings.mockResolvedValue({ data: null, error: { message: "Network Error" } });
    expect(scanLink(await mountPage()).exists()).toBe(false);
  });
});
