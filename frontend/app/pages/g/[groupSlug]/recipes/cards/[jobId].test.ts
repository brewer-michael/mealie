import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import ReviewPage from "./[jobId].vue";
import IngestRegionDialog from "~/components/Domain/Ingest/IngestRegionDialog.vue";
import {
  resetRecipeIngestCounts,
  resetRecipeIngestReviewState,
  takeRecipeIngestCommitNotice,
} from "~/composables/use-recipe-ingest";
import { carryReviewNotice, resetCarriedReviewNotice, takeCarriedReviewNotice } from "~/composables/use-recipe-ingest-review";
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
  getJobs: vi.fn(),
  cancel: vi.fn(),
}));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() }));
const router = vi.hoisted(() => ({ replace: vi.fn(), push: vi.fn() }));
type Guard = (to: { fullPath: string }) => Promise<boolean>;
/** The page's route guards: leaving for another page, and for another card (the same route with a new id) */
const leaveGuard = vi.hoisted(() => ({ current: null as null | Guard, update: null as null | Guard }));

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
  BoundingBox: { template: "<div><slot /></div>" },
  DraggableArea: { template: "<div><slot /></div>" },
  StencilPreview: { template: "<div />" },
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
  IngestOrganizerSelector: { props: ["modelValue", "selectorType", "readonly", "canCreate"], template: "<div class=\"organizers\" :data-type=\"selectorType\" :data-can-create=\"canCreate\" />" },
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
  VueDraggable: { props: ["modelValue", "disabled", "handle"], template: "<div class=\"draggable\" :data-disabled=\"disabled\"><slot /></div>" },
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
    template: "<button type=\"button\" :class=\"$attrs.class\" :aria-label=\"$attrs['aria-label']\" :data-to=\"to\" :disabled=\"disabled\" @click=\"$emit('click')\"><slot /></button>",
  },
  VTextField: input("text-field"),
  VTextarea: input("textarea"),
  VCombobox: input("combobox"),
  VSwitch: {
    props: ["modelValue", "label", "disabled"],
    emits: ["update:modelValue"],
    template: "<label class=\"switch\"><input type=\"checkbox\" :checked=\"modelValue\" :disabled=\"disabled\" @change=\"$emit('update:modelValue', $event.target.checked)\">{{ label }}</label>",
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
    leaveGuard.current = null;
    leaveGuard.update = null;
    vi.stubGlobal("onBeforeRouteLeave", (guard: Guard) => {
      leaveGuard.current = guard;
    });
    vi.stubGlobal("onBeforeRouteUpdate", (guard: Guard) => {
      leaveGuard.update = guard;
    });
    resetCarriedReviewNotice();
    api.getJobs.mockResolvedValue(ok({ items: [] }));
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
    expect(wrapper.get(".ingest-review__public").text()).toBe("Recipes in this household are public: the card photo will be visible to anyone.");
    // a member who can't add foods sees the food kept as text
    expect(wrapper.get(".ingest-ingredient__new-food").text()).toBe("Kept as text");
    // a member who can't organize can't add new tags either
    expect(wrapper.findAll(".organizers").map(o => [o.attributes("data-type"), o.attributes("data-can-create")]))
      .toEqual([["tags", "false"], ["categories", "false"], ["tools", "false"]]);
    expect(primary(wrapper).text()).toBe("1 to fix");
    // phones get the strip and the pinned bar
    expect(wrapper.find(".ingest-card-strip").exists()).toBe(true);
    expect(wrapper.find(".ingest-review-bar--fixed").exists()).toBe(true);
  });

  test("the card photo switch starts at the household's default; the warning shows while the photo would be public", async () => {
    const draft = { ...job().draft!, useCardAsCover: false };
    api.getJob.mockResolvedValue(ok(job({ draft, householdRecipesPublic: true, cardPhotoDefault: false })));
    const wrapper = await mountPage();
    const attach = () => wrapper.findAll(".switch").find(s => s.text() === "Attach the card photo to the recipe")!;
    const cover = () => wrapper.findAll(".switch").find(s => s.text() === "Use the card photo as the recipe image")!;

    // a public household: off unless the reviewer turns it on, and nothing is public while both are off
    expect((attach().get("input").element as HTMLInputElement).checked).toBe(false);
    expect((cover().get("input").element as HTMLInputElement).checked).toBe(false);
    expect(wrapper.find(".ingest-review__public").exists()).toBe(false);

    await attach().get("input").setValue(true);
    expect(wrapper.get(".ingest-review__public").text()).toBe("Recipes in this household are public: the card photo will be visible to anyone.");
    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();
    expect(api.updateJob.mock.calls[0]![1].draft).toMatchObject({ attachCardPhoto: true, useCardAsCover: false });

    await attach().get("input").setValue(false);
    expect(wrapper.find(".ingest-review__public").exists()).toBe(false);
    // the cover is the recipe's image: public too
    await cover().get("input").setValue(true);
    expect(wrapper.find(".ingest-review__public").exists()).toBe(true);
  });

  test("a choice the draft holds wins over the default, and a private household gets no warning", async () => {
    api.getJob.mockResolvedValue(ok(job({ draft: { ...job().draft!, attachCardPhoto: true }, householdRecipesPublic: false, cardPhotoDefault: true })));
    const wrapper = await mountPage();
    const attach = wrapper.findAll(".switch").find(s => s.text() === "Attach the card photo to the recipe")!;

    expect((attach.get("input").element as HTMLInputElement).checked).toBe(true);
    expect(wrapper.find(".ingest-review__public").exists()).toBe(false);

    api.getJob.mockResolvedValue(ok(job({ draft: { ...job().draft!, attachCardPhoto: false }, householdRecipesPublic: false, cardPhotoDefault: true })));
    const other = await mountPage();
    const off = other.findAll(".switch").find(s => s.text() === "Attach the card photo to the recipe")!;
    expect((off.get("input").element as HTMLInputElement).checked).toBe(false);
  });

  test("someone who may organize can add new tags, categories and tools", async () => {
    api.getJob.mockResolvedValue(ok(job({ permissions: { canCreateFoods: true, canDiscard: true, canExportEval: true, canCreateOrganizers: true } })));
    const wrapper = await mountPage();

    expect(wrapper.findAll(".organizers").map(o => o.attributes("data-can-create"))).toEqual(["true", "true", "true"]);
  });

  test("infos about the whole card show quietly, and aren't counted", async () => {
    const notParsed: CardFlag = { id: "not_parsed:card:", kind: "not_parsed", severity: "info", source: "parser", field: "card", ref: null };
    const crossReadFailed: CardFlag = { id: "cross_read_failed:card:", kind: "cross_read_failed", severity: "info", source: "cross_read", field: "card", ref: null };
    api.getJob.mockResolvedValue(ok(job({ flags: [blank, notParsed, crossReadFailed] })));
    const wrapper = await mountPage();

    expect(wrapper.findAll(".ingest-review__info").map(p => p.text())).toEqual([
      "Ingredients kept as text: This card isn't in English, and its ingredient lines couldn't be split into amount, unit and food, so they're kept as written.",
    ]);
    expect(wrapper.get(".ingest-needs-a-look h3").text()).toBe("Needs a look (1)");
  });

  test("Check this ingredient shows the parser's reading, and Keep as text saves the line as written", async () => {
    const check: CardFlag = { id: "check_parse:ingredients:i1", kind: "check_parse", severity: "warning", source: "parser", field: "ingredients", ref: "i1", params: { confidence: 60 } };
    api.getJob.mockResolvedValue(ok(job({ flags: [blank, check] })));
    const wrapper = await mountPage();

    const item = wrapper.get("[data-flag=\"check_parse:ingredients:i1\"]");
    expect(item.get(".ingest-flag-item__reading").text()).toBe("Read as: 1/4 teaspoon salt");
    await button(wrapper, "Keep as text").trigger("click");
    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();

    expect(api.updateJob.mock.calls[0]![1].draft.ingredients[0]).toMatchObject({ quantity: null, unit: null, food: null, note: "1/4 t. salt" });
    expect(wrapper.get("[data-flag=\"check_parse:ingredients:i1\"]").classes()).toContain("ingest-flag-item--fixed");
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
    // said by the next card's page in its review bar, not by a toast over its header
    expect(toast.success).not.toHaveBeenCalled();
    expect(takeRecipeIngestCommitNotice()).toBeNull();
    expect(takeCarriedReviewNotice("j2")).toEqual({ kind: "success", text: "Added Banana Mug Cake", detail: null });
  });

  test("the card opened by Commit & next says what was added inside its review bar, not over the page", async () => {
    carryReviewNotice("j1", { kind: "success", text: "Added Lemon Bars" });
    const wrapper = await mountPage();

    const bar = wrapper.get(".ingest-review-bar");
    const notice = bar.get(".ingest-review-bar__notice");
    expect(notice.classes()).toContain("ingest-review-bar__notice--success");
    expect(notice.text()).toContain("Added Lemon Bars");
    // in a live region, above Skip and Commit & next
    expect(bar.get("[role=status]").element.contains(notice.element)).toBe(true);
    expect(wrapper.find(".v-snackbar, .snackbar").exists()).toBe(false);
    expect(toast.success).not.toHaveBeenCalled();

    // dismissed with its close button
    await bar.get(".ingest-review-bar__notice-close").trigger("click");
    expect(wrapper.find(".ingest-review-bar__notice").exists()).toBe(false);

    // once only
    wrappers.forEach(w => w.unmount());
    wrappers.length = 0;
    expect((await mountPage()).find(".ingest-review-bar__notice").exists()).toBe(false);
  });

  test("what the commit left out is said with it", async () => {
    carryReviewNotice("j1", { kind: "warning", text: "Added Lemon Bars", detail: "The tag \"Desserts\" no longer exists, so it wasn't added." });
    const wrapper = await mountPage();

    const notice = wrapper.get(".ingest-review-bar__notice");
    expect(notice.classes()).toContain("ingest-review-bar__notice--warning");
    expect(notice.get(".ingest-review-bar__notice-detail").text()).toBe("The tag \"Desserts\" no longer exists, so it wasn't added.");
  });

  test("turning a page offers Read whole card again in the review bar, not over the header", async () => {
    api.rotatePage.mockResolvedValueOnce(ok({ ...job().pages![0], rotation: 0, rotationSource: "user" }));
    api.reextract.mockResolvedValueOnce(ok({ draftVersion: 3, status: "ready", task: { kind: "extract", state: "queued" }, proposalIds: [] }));
    const wrapper = await mountPage();

    await button(wrapper, "Rotate").trigger("click");
    await flushPromises();
    const notice = wrapper.get(".ingest-review-bar__notice");
    expect(notice.text()).toContain("Turn the card upright, then read it again.");
    expect(toast.info).not.toHaveBeenCalled();

    await notice.get(".ingest-review-bar__notice-action").trigger("click");
    await flushPromises();
    expect(api.reextract).toHaveBeenCalledExactlyOnceWith("j1");
    expect(wrapper.find(".ingest-review-bar__notice").exists()).toBe(false);
  });

  test("a failed card has no Skip or Commit, but its review bar still says what happened", async () => {
    api.getJob.mockResolvedValue(ok(job({ status: "failed", draft: null, flags: [], error: { code: "no_recipe_found", params: {} } })));
    api.rotatePage.mockResolvedValueOnce(ok({ ...job().pages![0], rotation: 0, rotationSource: "user" }));
    const wrapper = await mountPage();
    expect(wrapper.find(".ingest-review-bar").exists()).toBe(false);

    await button(wrapper, "Rotate").trigger("click");
    await flushPromises();

    const bar = wrapper.get(".ingest-review-bar");
    expect(bar.find(".ingest-review-bar__primary").exists()).toBe(false);
    expect(bar.get(".ingest-review-bar__notice-action").text()).toBe("Retry");
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

  test("on a phone the ⋯ menu re-reads an area: nothing is preselected, a new ingredient or step can be chosen", async () => {
    const wrapper = await mountPage();

    await wrapper.findAll(".menu-item").find(item => item.text() === "Re-read an area")!.trigger("click");

    const dialog = wrapper.getComponent(IngestRegionDialog);
    expect(dialog.props("modelValue")).toBe(true);
    expect(dialog.props("initialTarget")).toBeNull();
    expect(dialog.props("targets")!.map((option: { value: string }) => option.value))
      .toEqual(expect.arrayContaining(["name", "ingredients:new", "steps:new"]));
  });

  test("a line or step re-reads its own area of the card", async () => {
    const wrapper = await mountPage();

    await wrapper.get(".ingest-ingredient__line").trigger("click");
    await wrapper.get(".ingest-ingredient__reread").trigger("click");
    expect(wrapper.getComponent(IngestRegionDialog).props("initialTarget")).toBe("ingredients:i1");

    await wrapper.findAll(".ingest-step__reread")[0]!.trigger("click");
    expect(wrapper.getComponent(IngestRegionDialog).props("initialTarget")).toBe("steps:s1");
  });

  test("on a failed card nothing offers a re-read that couldn't happen", async () => {
    api.getJob.mockResolvedValue(ok(job({ status: "failed", draft: null, flags: [], error: { code: "no_recipe_found", params: {} } })));
    const desktop = await mountPage(true);

    // the panel can still turn the page, for a Retry
    expect(button(desktop, "Rotate").attributes("disabled")).toBeUndefined();
    expect(button(desktop, "Re-read an area").attributes("disabled")).toBeDefined();
    expect(desktop.findAll(".menu-item").find(item => item.text() === "Re-read an area")!.attributes("disabled")).toBeDefined();

    // nor does the R shortcut
    press("r");
    release("r");
    await flushPromises();
    expect(desktop.getComponent(IngestRegionDialog).props("modelValue")).toBe(false);
  });

  test("a re-read or re-extract in progress can be cancelled from its progress line", async () => {
    api.getJob.mockResolvedValue(ok(job({ task: { kind: "reread", state: "running", progressKey: null } })));
    api.cancel.mockResolvedValueOnce(ok({ draftVersion: 3, status: "ready", task: { kind: "reread", state: "running", cancelRequested: true }, proposalIds: [] }));
    const wrapper = await mountPage();

    const line = wrapper.get(".ingest-review__rereading");
    await line.get(".ingest-review__cancel").trigger("click");
    await flushPromises();

    expect(api.cancel).toHaveBeenCalledExactlyOnceWith("j1");
    // stopping within a heartbeat: no second Cancel meanwhile
    expect(wrapper.get(".ingest-review__rereading").text()).toContain("Stopping…");
    expect(wrapper.find(".ingest-review__cancel").exists()).toBe(false);

    api.getJobState.mockResolvedValueOnce(ok({ draftVersion: 3, status: "ready", task: null, proposalIds: [] }));
    await vi.advanceTimersByTimeAsync(2000);
    await flushPromises();
    expect(wrapper.find(".ingest-review__rereading").exists()).toBe(false);
  });

  test("Read whole card again can be cancelled while it runs", async () => {
    api.getJob.mockResolvedValue(ok(job({ task: { kind: "extract", state: "queued" } })));
    api.cancel.mockResolvedValueOnce(ok({ draftVersion: 3, status: "ready", task: null, proposalIds: [] }));
    const wrapper = await mountPage();

    api.getJob.mockResolvedValue(ok(job()));
    await wrapper.get(".ingest-review__reading .ingest-review__cancel").trigger("click");
    await flushPromises();

    expect(api.cancel).toHaveBeenCalledOnce();
    expect(wrapper.find(".ingest-review__reading").exists()).toBe(false);
    expect(wrapper.get(".ingest-review-bar__notice").text()).toContain("Reading stopped");
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

    expect(wrapper.get(".ingest-review__status").text()).toBe("Reading the card Cancel");
    expect(wrapper.find(".ingest-review-bar").exists()).toBe(false);

    // a mistaken scan, or a slow local model, needn't be waited out: it fails as cancelled, for Retry or Discard
    api.cancel.mockResolvedValueOnce(ok({ draftVersion: 0, status: "processing", task: { kind: "extract", state: "running", cancelRequested: true }, proposalIds: [] }));
    await wrapper.get(".ingest-review__status .ingest-review__cancel").trigger("click");
    await flushPromises();
    expect(api.cancel).toHaveBeenCalledExactlyOnceWith("j1");
    expect(wrapper.get(".ingest-review__status").text()).toBe("Stopping…");
  });

  test("a failed card can be read again", async () => {
    api.getJob.mockResolvedValue(ok(job({ status: "failed", draft: null, flags: [], error: { code: "no_recipe_found", params: {} } })));
    api.retry.mockResolvedValueOnce(ok({ draftVersion: 0, status: "processing", task: { kind: "extract", state: "queued" }, proposalIds: [] }));
    const wrapper = await mountPage();

    expect(wrapper.get(".ingest-review__status").text()).toBe("Failed: No recipe was found on this card.");
    await button(wrapper, "Retry").trigger("click");
    await flushPromises();
    expect(api.retry).toHaveBeenCalledExactlyOnceWith("j1");
    expect(wrapper.get(".ingest-review__status").text()).toBe("Waiting to be read Cancel");
  });

  test("a card that's gone says so, with the way back", async () => {
    api.getJob.mockResolvedValue({ data: null, response: null, error: { response: { status: 404, data: { detail: "Not found" } } } });
    const wrapper = await mountPage();

    expect(wrapper.get(".ingest-review__missing").text()).toContain("This card no longer exists.");
    expect(button(wrapper, "Cards").attributes("data-to")).toBe("/g/home/recipes/cards");
    // trying again wouldn't bring it back
    expect(wrapper.find(".ingest-review__try-again").exists()).toBe(false);
  });

  test("a card that failed to load (a phone that lost its signal) can be loaded again where it is", async () => {
    api.getJob.mockResolvedValueOnce({ data: null, response: null, error: { message: "Network Error" } });
    const wrapper = await mountPage();

    expect(wrapper.get(".ingest-review__missing").text()).toContain("Couldn't load this card.");
    await wrapper.get(".ingest-review__try-again").trigger("click");
    await flushPromises();

    expect(api.getJob).toHaveBeenCalledTimes(2);
    expect(wrapper.find(".ingest-review__missing").exists()).toBe(false);
    expect(wrapper.get(".ingest-review__position").text()).toContain("Card 1 of 2");
  });

  test("leaving saves the last edit first; when that fails, the page asks before leaving", async () => {
    const wrapper = await mountPage();

    // typed a moment ago: leaving sends it and goes
    await wrapper.findAll(".text-field").find(field => field.text().startsWith("Name"))!.get("input").setValue("Banana Mug Cake for One");
    expect(await leaveGuard.current!({ fullPath: "/g/home/recipes" })).toBe(true);
    expect(api.updateJob).toHaveBeenCalledOnce();

    // offline: the page stays and asks
    api.updateJob.mockResolvedValue({ data: null, response: null, error: { message: "Network Error" } });
    await wrapper.findAll(".text-field").find(field => field.text().startsWith("Name"))!.get("input").setValue("Banana Mug Cake for Two");
    expect(await leaveGuard.current!({ fullPath: "/g/home/recipes" })).toBe(false);
    await flushPromises();
    const dialog = wrapper.get(".dialog[data-title=\"Changes not saved\"]");
    expect(dialog.text()).toContain("Your last changes aren't saved. Leave anyway?");

    // Stay keeps the page
    await dialog.get(".ingest-review__stay").trigger("click");
    expect(wrapper.find(".dialog[data-title=\"Changes not saved\"]").exists()).toBe(false);
    expect(router.push).not.toHaveBeenCalled();

    // Leave goes, without asking again
    expect(await leaveGuard.current!({ fullPath: "/g/home/recipes" })).toBe(false);
    await flushPromises();
    await wrapper.get(".ingest-review__leave").trigger("click");
    await flushPromises();
    expect(router.push).toHaveBeenCalledExactlyOnceWith("/g/home/recipes");
    expect(await leaveGuard.current!({ fullPath: "/g/home/recipes" })).toBe(true);
  });

  test("Next with the last edit unsaved asks, then goes on the way the review does (replacing the page)", async () => {
    api.updateJob.mockResolvedValue({ data: null, response: null, error: { message: "Network Error" } });
    const wrapper = await mountPage();
    await wrapper.findAll(".text-field").find(field => field.text().startsWith("Name"))!.get("input").setValue("Offline edit");

    await button(wrapper, "Next card").trigger("click");
    expect(router.replace).toHaveBeenCalledExactlyOnceWith("/g/home/recipes/cards/j2");
    // the router runs the update guard for that navigation (the same route with another id)
    expect(await leaveGuard.update!({ fullPath: "/g/home/recipes/cards/j2" })).toBe(false);
    await flushPromises();

    await wrapper.get(".ingest-review__leave").trigger("click");
    await flushPromises();
    expect(router.replace).toHaveBeenLastCalledWith("/g/home/recipes/cards/j2");
    expect(router.push).not.toHaveBeenCalled();
  });
});
