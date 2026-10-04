import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { ref } from "vue";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import GroupRecipeCardSettings from "./GroupRecipeCardSettings.vue";
import type { EvalCaseSummary, RecipeIngestionSettingsOut } from "~/lib/api/types/recipe-ingest";

const api = vi.hoisted(() => ({
  getSettings: vi.fn(),
  updateSettings: vi.fn(),
  getEvalCases: vi.fn(),
  deleteEvalCase: vi.fn(),
}));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
const state = vi.hoisted(() => ({
  user: null as unknown as { value: { canManage: boolean; advanced: boolean } },
  group: null as unknown as { value: { aiProviderSettings: object } | null },
}));

vi.mock("~/composables/api", () => ({
  useUserApi: () => ({ recipeIngest: api }),
}));
vi.mock("~/composables/use-toast", () => ({
  alert: toast,
}));
vi.mock("~/composables/use-mealie-auth", () => ({
  useMealieAuth: () => ({ user: state.user }),
}));
vi.mock("~/composables/use-groups", () => ({
  useGroupSelf: () => ({ group: state.group, actions: {} }),
}));

function settings(overrides: Partial<RecipeIngestionSettingsOut> = {}): RecipeIngestionSettingsOut {
  return {
    enabled: true,
    localOnly: false,
    crossRead: false,
    canReadCards: true,
    limitReached: false,
    ocrAvailable: false,
    reader: { name: "Qwen VL", local: true, viaOcr: false },
    localOnlyAvailable: true,
    localReadiness: { image: ["Qwen VL"], default: ["Qwen text"], fast: ["Qwen text"], notPrivate: [] },
    limits: {
      maxUploadBytes: 104857600,
      maxFileBytes: 31457280,
      maxImagesPerRequest: 20,
      maxPagesPerCard: 4,
      maxPixels: 100000000,
    },
    inbox: { enabled: true, folder: "home/family" },
    ...overrides,
  };
}

const banana: EvalCaseSummary = {
  slug: "banana-mug-cake",
  name: "Banana Mug Cake",
  pageCount: 1,
  verified: true,
  createdAt: "2026-10-03T12:00:00",
};
const fudge: EvalCaseSummary = { slug: "fudge", name: null, pageCount: 2, verified: false, createdAt: null };

const slot = (tag = "div", className = "") => ({ template: `<${tag} class="${className}"><slot /></${tag}>` });
const wrappers: VueWrapper[] = [];

async function mountSettings() {
  const wrapper = mount(GroupRecipeCardSettings, {
    global: {
      mocks: { $globals: { icons: { delete: "delete", alertCircle: "alert" } } },
      stubs: {
        BaseCardSectionTitle: {
          props: ["title"],
          template: "<h3>{{ title }}</h3>",
        },
        BaseDialog: {
          props: ["modelValue", "title"],
          emits: ["confirm", "update:modelValue"],
          template: `
            <div v-if="modelValue" class="confirm-dialog" :data-title="title">
              <slot />
              <button type="button" class="dialog-confirm" @click="$emit('confirm'); $emit('update:modelValue', false)">Confirm</button>
            </div>
          `,
        },
        AppLoader: { template: "<div class=\"loader\" />" },
        NuxtLink: {
          props: ["to"],
          template: "<a :href=\"to\"><slot /></a>",
        },
        VCard: slot(),
        VCardText: slot(),
        VAlert: {
          props: ["type"],
          template: "<div class=\"alert\" :data-type=\"type\"><slot /></div>",
        },
        VBtn: {
          props: ["disabled", "loading", "ariaLabel"],
          template: "<button type=\"button\" :disabled=\"disabled\" :aria-label=\"ariaLabel\"><slot /></button>",
        },
        VSwitch: {
          props: ["modelValue", "label", "hint", "disabled"],
          emits: ["update:modelValue"],
          template: `
            <label class="switch">
              <input
                type="checkbox"
                :checked="modelValue"
                :disabled="disabled"
                @change="$emit('update:modelValue', $event.target.checked)"
              >
              <span class="label">{{ label }}</span> <small class="hint">{{ hint }}</small>
            </label>
          `,
        },
        VList: slot("ul", "list"),
        VListItem: { template: "<li class=\"list-item\"><slot /><slot name=\"append\" /></li>" },
        VListItemTitle: slot("div", "item-title"),
        VListItemSubtitle: slot("div", "item-subtitle"),
        VChip: slot("span", "chip"),
      },
    },
  });
  wrappers.push(wrapper);
  await flushPromises();
  return wrapper;
}

function toggle(wrapper: VueWrapper, className: string) {
  return wrapper.get(`.${className} input`);
}

function checked(wrapper: VueWrapper, className: string) {
  return (toggle(wrapper, className).element as HTMLInputElement).checked;
}

function button(wrapper: VueWrapper, text: string, within?: string) {
  const root = within ? wrapper.get(within) : wrapper;
  const found = root.findAll("button").find(b => b.text() === text || b.attributes("aria-label") === text);
  if (!found) {
    throw new Error(`No ${text} button`);
  }
  return found;
}

describe("GroupRecipeCardSettings", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.user = ref({ canManage: true, advanced: true });
    state.group = ref({ aiProviderSettings: { defaultProviderId: "a" } });
    api.getSettings.mockResolvedValue({ data: settings(), error: null });
    api.getEvalCases.mockResolvedValue({ data: [banana, fudge], error: null });
  });

  afterEach(() => {
    wrappers.forEach(wrapper => wrapper.unmount());
    wrappers.length = 0;
  });

  test("shows the settings and which local providers would read the cards", async () => {
    api.getSettings.mockResolvedValue({
      data: settings({
        crossRead: true,
        localReadiness: { image: [], default: ["Qwen text", "Llama"], fast: ["Qwen text"], notPrivate: ["LAN proxy"] },
        localOnlyAvailable: false,
      }),
      error: null,
    });
    const wrapper = await mountSettings();

    expect(wrapper.get("h3").text()).toBe("Recipe cards");
    expect(checked(wrapper, "local-only")).toBe(false);
    expect(wrapper.get(".local-only .label").text()).toBe("Keep recipe card photos and text on this server");
    expect(checked(wrapper, "cross-read")).toBe(true);
    expect(wrapper.get(".cross-read .hint").text()).toContain("Each card costs a second image request.");

    expect(wrapper.findAll(".readiness-slots li").map(li => li.text())).toEqual([
      "Reading photos: none",
      "Building recipes: Qwen text, Llama",
      "Quick tasks: Qwen text",
    ]);
    expect(wrapper.get(".readiness-warning").text())
      .toBe("Cards can't be read locally yet: add a local image provider (or OCR) and a local text provider.");
    expect(wrapper.get(".not-private").text())
      .toBe("Marked as local, but not at a private address, so they won't be used: LAN proxy");
    expect(wrapper.find(".cannot-read").exists()).toBe(false);
  });

  test("no warnings when local providers can read the cards", async () => {
    const wrapper = await mountSettings();

    expect(wrapper.find(".readiness-warning").exists()).toBe(false);
    expect(wrapper.find(".not-private").exists()).toBe(false);
  });

  test("with scanning turned off on the server, says so instead of asking for a provider", async () => {
    api.getSettings.mockResolvedValue({
      data: settings({ enabled: false, canReadCards: false, reader: null, localReadiness: null }),
      error: null,
    });
    const wrapper = await mountSettings();

    expect(wrapper.get(".disabled").text()).toContain("Recipe card scanning is turned off on this server.");
    expect(wrapper.find(".cannot-read").exists()).toBe(false);
    expect(toggle(wrapper, "local-only").attributes("disabled")).toBeDefined();
    expect(toggle(wrapper, "cross-read").attributes("disabled")).toBeDefined();
  });

  test("warns a manager when this month's token limit stops cards being read", async () => {
    api.getSettings.mockResolvedValue({ data: settings({ limitReached: true }), error: null });
    const wrapper = await mountSettings();

    expect(wrapper.get(".limit-reached").text()).toContain("monthly token limit");
    expect(wrapper.find(".disabled").exists()).toBe(false);
  });

  test("says when the group can't read cards, and when OCR is available", async () => {
    api.getSettings.mockResolvedValue({ data: settings({ canReadCards: false, ocrAvailable: true }), error: null });
    const wrapper = await mountSettings();

    expect(wrapper.get(".cannot-read").text())
      .toBe("Cards can't be read yet: add a text provider, plus an image provider or OCR.");
    expect(wrapper.get(".ocr-available").text()).toBe("OCR is available on this server");
  });

  test("the local-only switch saves at once, keeping the other setting", async () => {
    api.updateSettings.mockResolvedValue({ data: settings({ localOnly: true }), error: null });
    const wrapper = await mountSettings();

    await toggle(wrapper, "local-only").setValue(true);
    await flushPromises();

    expect(api.updateSettings).toHaveBeenCalledExactlyOnceWith({ localOnly: true, crossRead: false });
    expect(checked(wrapper, "local-only")).toBe(true);
    expect(toast.success).toHaveBeenCalledExactlyOnceWith("Recipe card settings saved");
  });

  test("the second-reading switch saves at once", async () => {
    api.getSettings.mockResolvedValue({ data: settings({ localOnly: true }), error: null });
    api.updateSettings.mockResolvedValue({ data: settings({ localOnly: true, crossRead: true }), error: null });
    const wrapper = await mountSettings();

    await toggle(wrapper, "cross-read").setValue(true);
    await flushPromises();

    expect(api.updateSettings).toHaveBeenCalledExactlyOnceWith({ localOnly: true, crossRead: true });
    expect(checked(wrapper, "cross-read")).toBe(true);
  });

  test("a failed save switches it back and says so", async () => {
    api.updateSettings.mockResolvedValue({ data: null, error: new Error("503") });
    const wrapper = await mountSettings();

    await toggle(wrapper, "local-only").setValue(true);
    await flushPromises();

    expect(checked(wrapper, "local-only")).toBe(false);
    expect(toast.error).toHaveBeenCalledExactlyOnceWith("Couldn't save the recipe card settings");
  });

  test("the household's inbox folder", async () => {
    const wrapper = await mountSettings();
    expect(wrapper.get(".inbox").text()).toBe("Put photos in the home/family folder of the inbox share to scan them.");

    api.getSettings.mockResolvedValue({ data: settings({ inbox: { enabled: false, folder: null } }), error: null });
    const off = await mountSettings();
    expect(off.get(".inbox").text()).toBe("The inbox folder isn't set up on this server.");
  });

  test("links to the notifiers page, which needs advanced features", async () => {
    const wrapper = await mountSettings();
    const link = wrapper.get(".notifications a");
    expect(link.text()).toBe("Get notified when cards are ready");
    expect(link.attributes("href")).toBe("/household/notifiers");

    state.user = ref({ canManage: true, advanced: false });
    const notAdvanced = await mountSettings();
    expect(notAdvanced.find(".notifications a").exists()).toBe(false);
    expect(notAdvanced.get(".notifications").text()).not.toBe("");
  });

  test("lists the group's eval cases", async () => {
    const wrapper = await mountSettings();

    const items = wrapper.findAll(".eval-case");
    expect(items.map(item => item.get(".item-title").text())).toEqual(["Banana Mug Cake", "fudge"]);
    expect(items[0]!.get(".item-subtitle").text()).toContain("banana-mug-cake");
    expect(items[0]!.get(".item-subtitle").text()).toContain("1 page");
    expect(items[0]!.get(".verified").text()).toBe("Verified");
    expect(items[1]!.get(".item-subtitle").text()).toContain("2 pages");
    expect(items[1]!.find(".verified").exists()).toBe(false);
  });

  test("says when there are no eval cases", async () => {
    api.getEvalCases.mockResolvedValue({ data: [], error: null });
    const wrapper = await mountSettings();

    expect(wrapper.get(".no-eval-cases").text()).toBe("No eval cases yet");
  });

  test("deleting an eval case asks first", async () => {
    api.deleteEvalCase.mockResolvedValue({ data: null, error: null });
    const wrapper = await mountSettings();

    await button(wrapper, "Delete", ".eval-case").trigger("click");
    expect(api.deleteEvalCase).not.toHaveBeenCalled();
    expect(wrapper.get(".confirm-dialog").text()).toContain("Delete the eval case \"banana-mug-cake\"?");

    await wrapper.get(".dialog-confirm").trigger("click");
    await flushPromises();

    expect(api.deleteEvalCase).toHaveBeenCalledExactlyOnceWith("banana-mug-cake");
    expect(wrapper.findAll(".eval-case").map(item => item.get(".item-title").text())).toEqual(["fudge"]);
    expect(toast.success).toHaveBeenCalledExactlyOnceWith("Eval case deleted");
  });

  test("a failed delete shows the list as it is now", async () => {
    api.deleteEvalCase.mockResolvedValue({ data: null, error: new Error("404") });
    const wrapper = await mountSettings();
    api.getEvalCases.mockResolvedValue({ data: [fudge], error: null });

    await button(wrapper, "Delete", ".eval-case").trigger("click");
    await wrapper.get(".dialog-confirm").trigger("click");
    await flushPromises();

    expect(api.getEvalCases).toHaveBeenCalledTimes(2);
    expect(wrapper.findAll(".eval-case").map(item => item.get(".item-title").text())).toEqual(["fudge"]);
    expect(toast.success).not.toHaveBeenCalled();
  });

  test("a member who can't manage the group only reads the settings", async () => {
    state.user = ref({ canManage: false, advanced: true });
    api.getSettings.mockResolvedValue({ data: settings({ localReadiness: null }), error: null });
    const wrapper = await mountSettings();

    expect((toggle(wrapper, "local-only").element as HTMLInputElement).disabled).toBe(true);
    expect((toggle(wrapper, "cross-read").element as HTMLInputElement).disabled).toBe(true);
    expect(wrapper.find(".readiness").exists()).toBe(false);
    expect(wrapper.find(".eval-cases").exists()).toBe(false);
    expect(api.getEvalCases).not.toHaveBeenCalled();
  });

  test("a failed load can be retried", async () => {
    api.getSettings.mockResolvedValueOnce({ data: null, error: new Error("500") });
    const wrapper = await mountSettings();

    expect(wrapper.get(".load-failed").text()).toContain("Couldn't load the recipe card settings");
    expect(wrapper.find(".local-only").exists()).toBe(false);

    await button(wrapper, "Retry", ".load-failed").trigger("click");
    await flushPromises();
    expect(wrapper.find(".load-failed").exists()).toBe(false);
    expect(wrapper.find(".local-only").exists()).toBe(true);
  });

  test("reloads when the group's AI providers change", async () => {
    await mountSettings();
    expect(api.getSettings).toHaveBeenCalledOnce();

    // the page refreshes the group after a provider is created, changed ("Runs on my network") or deleted
    state.group.value = { aiProviderSettings: { defaultProviderId: "b" } };
    await flushPromises();

    expect(api.getSettings).toHaveBeenCalledTimes(2);
  });
});
