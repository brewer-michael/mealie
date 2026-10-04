import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import ReviewPage from "./[jobId].vue";
import IngestRegionDialog from "~/components/Domain/Ingest/IngestRegionDialog.vue";
import {
  leaveRecipeIngestCommitNotice,
  resetRecipeIngestCounts,
  resetRecipeIngestReviewState,
  takeRecipeIngestCommitNotice,
} from "~/composables/use-recipe-ingest";
import type { CardFlag, RecipeIngestionJobOut } from "~/lib/api/types/recipe-ingest";

const api = vi.hoisted(() => ({
  getJob: vi.fn(),
  getBatch: vi.fn(),
  getJobState: vi.fn(),
  updateJob: vi.fn(),
  reread: vi.fn(),
  reextract: vi.fn(),
  rotatePage: vi.fn(),
  retry: vi.fn(),
  commit: vi.fn(),
  discard: vi.fn(),
  saveEvalCase: vi.fn(),
  getCounts: vi.fn(),
}));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() }));
const router = vi.hoisted(() => ({ replace: vi.fn() }));

vi.mock("~/composables/api", () => ({
  useUserApi: () => ({ recipeIngest: api }),
}));
vi.mock("~/composables/use-toast", () => ({
  alert: toast,
}));
vi.mock("~/composables/store/use-food-store", async () => {
  const { ref } = await import("vue");
  return { useFoodStore: () => ({ store: ref([{ id: "f-oil", name: "coconut oil" }]) }) };
});
vi.mock("~/composables/store/use-unit-store", async () => {
  const { ref } = await import("vue");
  return { useUnitStore: () => ({ store: ref([{ id: "u-tbsp", name: "tablespoon" }]) }) };
});
vi.mock("vue-advanced-cropper", () => ({
  Cropper: { name: "Cropper", props: ["src"], methods: { getResult: () => null, refresh: () => undefined }, template: "<div />" },
}));

const blank: CardFlag = { id: "blank:steps:s2", kind: "blank", severity: "error", source: "marker", field: "steps", ref: "s2", params: {}, alternatives: [] };
const unsure: CardFlag = {
  id: "unsure:ingredients:i1",
  kind: "unsure",
  severity: "warning",
  source: "model",
  field: "ingredients",
  ref: "i1",
  params: { text: "1/4" },
  alternatives: ["1/2"],
};

function job(overrides: Partial<RecipeIngestionJobOut> = {}): RecipeIngestionJobOut {
  const base = "/api/ai/ingest/jobs/j1/pages/0";
  return {
    id: "j1",
    batchId: "b1",
    position: 0,
    status: "ready",
    source: "app",
    pageCount: 1,
    draftVersion: 3,
    localOnly: true,
    pages: [{
      index: 0,
      width: 1536,
      height: 2048,
      viewWidth: 1536,
      viewHeight: 2048,
      rotation: 270,
      rotationSource: "ocr",
      oriented: true,
      pageUrl: `${base}/page?v=1`,
      viewUrl: `${base}/view?v=1`,
      thumbUrl: `${base}/thumb?v=1`,
    }],
    transcription: "Banana Mug Cake\n1/4 t. salt\nMicrowave on high for [blank] minutes.",
    read: { readPath: "image", provider: "Claude Sonnet", model: "claude-sonnet", crossRead: true },
    draft: {
      name: "Banana Mug Cake",
      ingredients: [{
        referenceId: "i1",
        originalText: "1/4 t. salt",
        quantity: 0.25,
        unit: { id: null, name: "teaspoon" },
        food: { id: null, name: "salt" },
        note: "",
        display: "1/4 teaspoon salt",
      }],
      steps: [{ id: "s1", text: "Mix in a mug." }, { id: "s2", text: "Microwave on high for [blank] minutes." }],
      notes: [],
      tags: [],
      categories: [],
      tools: [],
      useCardAsCover: true,
    },
    flags: [blank, unsure],
    proposals: [],
    permissions: { canCreateFoods: false, canDiscard: true, canExportEval: true },
    duplicateOf: { id: "r0", slug: "banana-mug-cake", name: "Banana Mug Cake" },
    householdRecipesPublic: true,
    ...overrides,
  };
}

const ok = (data: unknown) => ({ data, error: null, response: {} });

const slot = (tag = "div", className = "") => ({ inheritAttrs: true, template: `<${tag} class="${className}"><slot /></${tag}>` });
const input = (className: string) => ({
  props: ["modelValue", "label", "readonly", "disabled"],
  emits: ["update:modelValue"],
  template: `<label class="${className}">{{ label }}<input :value="typeof modelValue === 'object' && modelValue ? modelValue.name : modelValue" :disabled="disabled" @input="$emit('update:modelValue', $event.target.value)"></label>`,
});

const stubs = {
  AppLoader: { template: "<div class=\"loader\" />" },
  BaseDialog: {
    props: ["modelValue", "title"],
    emits: ["update:modelValue", "confirm", "submit"],
    template: `
      <div v-if="modelValue" class="dialog" :data-title="title">
        <slot />
        <slot name="card-actions"><button type="button" class="dialog-confirm" @click="$emit('confirm')">Confirm</button></slot>
      </div>
    `,
  },
  RecipeOrganizerSelector: { props: ["modelValue", "selectorType", "showAdd"], template: "<div class=\"organizers\" :data-type=\"selectorType\" :data-show-add=\"showAdd\" />" },
  RecipeNotes: { props: ["modelValue", "edit"], template: "<div class=\"notes\" />" },
  RecipeImageLightbox: { props: ["modelValue", "imageUrl"], template: "<div />" },
  VContainer: slot(),
  VRow: slot(),
  VCol: slot(),
  VCard: slot("section"),
  VCardTitle: slot("h3"),
  VCardText: slot(),
  VDivider: { template: "<hr>" },
  VSpacer: { template: "<span />" },
  VList: slot(),
  VMenu: { template: "<div class=\"menu\"><slot name=\"activator\" :props=\"{}\" /><slot /></div>" },
  VListItem: {
    props: ["title", "disabled"],
    emits: ["click"],
    template: "<button type=\"button\" class=\"menu-item\" :disabled=\"disabled\" @click=\"$emit('click')\">{{ title }}</button>",
  },
  VExpansionPanels: slot(),
  VExpansionPanel: slot(),
  VExpansionPanelTitle: slot("h4"),
  VExpansionPanelText: slot(),
  VProgressLinear: { template: "<div class=\"progress\" />" },
  VIcon: { template: "<i />" },
  VChip: { template: "<span class=\"chip\"><slot /></span>" },
  VBtnToggle: slot(),
  VAlert: {
    props: ["type", "closable"],
    emits: ["click:close"],
    template: `
      <div class="alert" :data-type="type">
        <slot /><slot name="append" />
        <button v-if="closable" type="button" class="alert-close" @click="$emit('click:close')">Close</button>
      </div>
    `,
  },
  VBtn: {
    props: ["disabled", "to", "loading"],
    emits: ["click"],
    template: "<button type=\"button\" :class=\"$attrs.class\" :data-to=\"to\" :disabled=\"disabled\" @click=\"$emit('click')\"><slot /></button>",
  },
  VTextField: input("text-field"),
  VTextarea: input("textarea"),
  VCombobox: input("combobox"),
  VSwitch: {
    props: ["modelValue", "label"],
    template: "<label class=\"switch\"><input type=\"checkbox\" :checked=\"modelValue\">{{ label }}</label>",
  },
  VSelect: slot(),
  VCheckbox: slot(),
  VSnackbar: {
    props: ["modelValue", "location", "color", "timeout"],
    template: `
      <div v-if="modelValue" class="snackbar" :class="$attrs.class" :data-location="location" :data-color="color">
        <slot /><slot name="actions" />
      </div>
    `,
  },
};

const route = { params: { groupSlug: "home", jobId: "j1" }, query: {}, fullPath: "/g/home/recipes/cards/j1" };
const wrappers: VueWrapper[] = [];
const scrolled: string[] = [];

async function mountPage(desktop = false) {
  const wrapper = mount(ReviewPage, {
    attachTo: document.body,
    global: {
      mocks: { $globals: { icons: {} }, $vuetify: { display: { mdAndUp: desktop } } },
      stubs,
    },
  });
  wrappers.push(wrapper);
  await flushPromises();
  return wrapper;
}

function button(wrapper: VueWrapper, text: string) {
  const found = wrapper.findAll("button").find(b => b.text() === text || b.attributes("aria-label") === text);
  if (!found) {
    throw new Error(`No ${text} button`);
  }
  return found;
}

function primary(wrapper: VueWrapper) {
  return wrapper.get(".ingest-review-bar__primary");
}

function press(key: string, options: KeyboardEventInit = {}) {
  window.dispatchEvent(new KeyboardEvent("keydown", { key, ...options }));
}

function release(key: string, options: KeyboardEventInit = {}) {
  window.dispatchEvent(new KeyboardEvent("keyup", { key, ...options }));
}

describe("the recipe card review page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
    resetRecipeIngestCounts();
    resetRecipeIngestReviewState();
    scrolled.length = 0;
    Element.prototype.scrollIntoView = function (this: Element) {
      scrolled.push(this.id);
    };
    vi.stubGlobal("definePageMeta", vi.fn());
    vi.stubGlobal("useSeoMeta", vi.fn());
    vi.stubGlobal("useRoute", () => route);
    vi.stubGlobal("useRouter", () => router);
    api.getJob.mockResolvedValue(ok(job()));
    api.getBatch.mockResolvedValue(ok({ id: "b1", source: "app", jobs: [{ id: "j1", position: 0, status: "ready" }, { id: "j2", position: 1, status: "ready" }] }));
    api.getJobState.mockResolvedValue(ok({ draftVersion: 3, status: "ready", task: null, proposalIds: [] }));
    api.updateJob.mockImplementation((_id: string, payload: { draftVersion: number }) =>
      Promise.resolve(ok({ draftVersion: payload.draftVersion + 1, flags: [unsure], errorCount: 0, warningCount: 1 })),
    );
    api.getCounts.mockResolvedValue(ok({ ready: 1 }));
  });

  afterEach(() => {
    wrappers.forEach(wrapper => wrapper.unmount());
    wrappers.length = 0;
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  test("shows where the card is, how it was read and what needs a look", async () => {
    const wrapper = await mountPage();

    expect(api.getJob).toHaveBeenCalledExactlyOnceWith("j1");
    expect(wrapper.get(".ingest-review__position").text()).toBe("Card 1 of 2 · 2 to check Local only");
    expect(wrapper.get(".ingest-review__checks").text()).toBe("Read by Claude Sonnet · checked against a second reading");
    expect(wrapper.get(".ingest-needs-a-look h3").text()).toBe("Needs a look (2)");
    expect(wrapper.findAll("[data-flag]").map(item => item.attributes("data-flag"))).toEqual(["unsure:ingredients:i1", "blank:steps:s2"]);
    expect(wrapper.get(".ingest-review__duplicate").text()).toContain("A recipe called \"Banana Mug Cake\" already exists.");
    expect(wrapper.get(".ingest-review__public").text()).toContain("New recipes in this household are public");
    // a member who can't add foods sees the food kept as text
    expect(wrapper.get(".ingest-ingredient__new-food").text()).toBe("Kept as text");
    expect(wrapper.findAll(".organizers").map(o => [o.attributes("data-type"), o.attributes("data-show-add")]))
      .toEqual([["tags", "false"], ["categories", "false"], ["tools", "false"]]);
    expect(primary(wrapper).text()).toBe("1 to fix");
    // phones get the strip and the pinned bar
    expect(wrapper.find(".ingest-card-strip").exists()).toBe(true);
    expect(wrapper.find(".ingest-review-bar--fixed").exists()).toBe(true);
  });

  test("infos about the whole card show quietly, and aren't counted", async () => {
    const notParsed: CardFlag = { id: "not_parsed:card:", kind: "not_parsed", severity: "info", source: "parser", field: "card", ref: null };
    const crossReadFailed: CardFlag = { id: "cross_read_failed:card:", kind: "cross_read_failed", severity: "info", source: "cross_read", field: "card", ref: null };
    api.getJob.mockResolvedValue(ok(job({ flags: [blank, notParsed, crossReadFailed] })));
    const wrapper = await mountPage();

    expect(wrapper.findAll(".ingest-review__info").map(p => p.text())).toEqual([
      "Ingredients kept as text: This card isn't in English, so its ingredient lines are kept as written.",
    ]);
    expect(wrapper.get(".ingest-needs-a-look h3").text()).toBe("Needs a look (1)");
  });

  test("1 to fix scrolls to the error instead of committing", async () => {
    const wrapper = await mountPage();

    await primary(wrapper).trigger("click");
    await flushPromises();

    expect(api.commit).not.toHaveBeenCalled();
    expect(scrolled).toEqual(["ingest-flag-blank-steps-s2"]);
  });

  test("typing into the blank, then Commit & next adds the recipe and opens the next card", async () => {
    api.commit.mockResolvedValueOnce(ok({ recipeId: "r1", slug: "banana-mug-cake-1", nextJobId: "j2", warnings: [] }));
    const wrapper = await mountPage();

    const item = wrapper.get("[data-flag=\"blank:steps:s2\"]");
    await item.get("input").setValue("2");
    await button(wrapper, "Fill in").trigger("click");

    // the warning that's left doesn't block
    expect(primary(wrapper).text()).toBe("Commit & next");
    expect(wrapper.get(".ingest-review__position").text()).toBe("Card 1 of 2 · 1 to check Local only");

    await primary(wrapper).trigger("click");
    await flushPromises();

    expect(api.updateJob.mock.calls[0]![1].draft.steps[1].text).toBe("Microwave on high for 2 minutes.");
    expect(api.commit).toHaveBeenCalledExactlyOnceWith("j1", { draftVersion: 4 });
    expect(router.replace).toHaveBeenCalledExactlyOnceWith("/g/home/recipes/cards/j2");
    // said by the next card's page, not by a toast over its header
    expect(toast.success).not.toHaveBeenCalled();
    expect(takeRecipeIngestCommitNotice()).toEqual({ text: "Added Banana Mug Cake", warning: null });
  });

  test("the card opened by Commit & next says what was added, at the bottom above the review bar", async () => {
    leaveRecipeIngestCommitNotice({ text: "Added Lemon Bars", warning: null });
    const wrapper = await mountPage();

    const notice = wrapper.get(".ingest-review__notice");
    expect(notice.attributes("data-location")).toBe("bottom");
    expect(notice.attributes("data-color")).toBe("success");
    expect(notice.text()).toContain("Added Lemon Bars");
    expect(toast.success).not.toHaveBeenCalled();

    // once only
    wrappers.forEach(w => w.unmount());
    wrappers.length = 0;
    expect((await mountPage()).find(".ingest-review__notice").exists()).toBe(false);
  });

  test("a notice the next card never showed isn't shown on a card opened later", async () => {
    leaveRecipeIngestCommitNotice({ text: "Added Lemon Bars", warning: null });
    const later = Date.now() + 60_000;
    vi.spyOn(Date, "now").mockReturnValue(later);
    try {
      const wrapper = await mountPage();
      expect(wrapper.find(".ingest-review__notice").exists()).toBe(false);
    }
    finally {
      vi.restoreAllMocks();
    }
  });

  test("what the commit left out is said with it", async () => {
    leaveRecipeIngestCommitNotice({ text: "Added Lemon Bars", warning: "The tag \"Desserts\" no longer exists, so it wasn't added." });
    const wrapper = await mountPage();

    const notice = wrapper.get(".ingest-review__notice");
    expect(notice.attributes("data-color")).toBe("warning");
    expect(notice.get(".ingest-review__notice-warning").text()).toBe("The tag \"Desserts\" no longer exists, so it wasn't added.");
  });

  test("a stale version shows the Reload this card dialog, which loads the stored card", async () => {
    api.updateJob.mockResolvedValueOnce({ data: null, response: null, error: { response: { status: 409, data: { detail: { code: "version_conflict", current: 5 } } } } });
    const wrapper = await mountPage();

    await button(wrapper, "Looks right").trigger("click");
    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();

    const dialog = wrapper.get(".dialog[data-title=\"This card changed\"]");
    expect(dialog.text()).toContain("This card was changed somewhere else.");
    expect(toast.error).not.toHaveBeenCalled();
    expect(wrapper.find(".ingest-review__conflict").exists()).toBe(true);

    api.getJob.mockResolvedValueOnce(ok(job({ draftVersion: 5, flags: [] })));
    await dialog.get(".ingest-review__reload").trigger("click");
    await flushPromises();

    expect(api.getJob).toHaveBeenCalledTimes(2);
    expect(wrapper.find(".dialog[data-title=\"This card changed\"]").exists()).toBe(false);
    expect(primary(wrapper).text()).toBe("Commit & next");
  });

  test("Re-read on a flag opens the region dialog aimed at its line", async () => {
    const wrapper = await mountPage();

    await wrapper.get("[data-flag=\"blank:steps:s2\"]").findAll("button").find(b => b.text() === "Re-read")!.trigger("click");

    const dialog = wrapper.getComponent(IngestRegionDialog);
    expect(dialog.props("modelValue")).toBe(true);
    expect(dialog.props("initialTarget")).toBe("steps:s2");
    expect(dialog.props("targets")!.map((option: { value: string }) => option.value)).toContain("ingredients:i1");
  });

  test("the ⋯ menu reads the card again and shows what it says", async () => {
    api.reextract.mockResolvedValueOnce(ok({ draftVersion: 3, status: "ready", task: { kind: "extract", state: "queued" }, proposalIds: [] }));
    const wrapper = await mountPage();

    await button(wrapper, "Read whole card again").trigger("click");
    await flushPromises();
    expect(api.reextract).toHaveBeenCalledExactlyOnceWith("j1");
    expect(wrapper.get(".ingest-review__reading").text()).toContain("The card is being read again");

    await button(wrapper, "What the card says").trigger("click");
    expect(wrapper.get(".dialog[data-title=\"What the card says\"]").text()).toContain("Microwave on high for [blank] minutes.");
  });

  test("Ctrl+Enter commits from the keyboard, even while typing", async () => {
    api.getJob.mockResolvedValue(ok(job({ flags: [unsure] })));
    api.commit.mockResolvedValueOnce(ok({ recipeId: "r1", slug: "banana-mug-cake", nextJobId: "j2" }));
    await mountPage(true);

    press("Control", { ctrlKey: true });
    press("Enter", { ctrlKey: true });
    await flushPromises();
    release("Enter", { ctrlKey: true });
    release("Control");

    expect(api.commit).toHaveBeenCalledOnce();
    expect(router.replace).toHaveBeenCalledWith("/g/home/recipes/cards/j2");
  });

  test("on desktop the card sits in a panel beside the editor", async () => {
    const wrapper = await mountPage(true);
    expect(wrapper.find(".ingest-card-panel").exists()).toBe(true);
    expect(wrapper.find(".ingest-review-bar--fixed").exists()).toBe(false);
  });

  test("a card still being read shows its progress", async () => {
    api.getJob.mockResolvedValue(ok(job({ status: "processing", draft: null, flags: [], task: { kind: "extract", state: "running", progressKey: "recipe-ingest.progress.reading-card" } })));
    const wrapper = await mountPage();

    expect(wrapper.get(".ingest-review__status").text()).toBe("Reading the card");
    expect(wrapper.find(".ingest-review-bar").exists()).toBe(false);
  });

  test("a failed card can be read again", async () => {
    api.getJob.mockResolvedValue(ok(job({ status: "failed", draft: null, flags: [], error: { code: "no_recipe_found", params: {} } })));
    api.retry.mockResolvedValueOnce(ok({ draftVersion: 0, status: "processing", task: { kind: "extract", state: "queued" }, proposalIds: [] }));
    const wrapper = await mountPage();

    expect(wrapper.get(".ingest-review__status").text()).toBe("Failed: No recipe was found on this card.");
    await button(wrapper, "Retry").trigger("click");
    await flushPromises();
    expect(api.retry).toHaveBeenCalledExactlyOnceWith("j1");
    expect(wrapper.get(".ingest-review__status").text()).toBe("Waiting to be read");
  });

  test("a card that's gone says so, with the way back", async () => {
    api.getJob.mockResolvedValue({ data: null, response: null, error: { response: { status: 404, data: { detail: "Not found" } } } });
    const wrapper = await mountPage();

    expect(wrapper.get(".ingest-review__missing").text()).toContain("This card no longer exists.");
    expect(button(wrapper, "Cards").attributes("data-to")).toBe("/g/home/recipes/cards");
  });
});
