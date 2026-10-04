import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import ReviewPage from "./[jobId].vue";
import IngestEvalCaseDialog from "~/components/Domain/Ingest/IngestEvalCaseDialog.vue";
import IngestRegionDialog from "~/components/Domain/Ingest/IngestRegionDialog.vue";
import {
  formatIngestDate,
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
  readWithCloud: vi.fn(),
  commit: vi.fn(),
  uncommit: vi.fn(),
  merge: vi.fn(),
  discard: vi.fn(),
  saveEvalCase: vi.fn(),
  getCounts: vi.fn(),
  getJobs: vi.fn(),
  cancel: vi.fn(),
  regionHint: vi.fn(),
  rebuild: vi.fn(),
  parseLines: vi.fn(),
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
  VMenu: {
    emits: ["update:modelValue"],
    template: "<div class=\"menu\"><slot name=\"activator\" :props=\"{ onClick: () => $emit('update:modelValue', true) }\" /><slot /></div>",
  },
  VListItem: {
    props: ["title", "subtitle", "disabled"],
    emits: ["click"],
    template: `
      <button type="button" class="menu-item" :class="$attrs.class" :disabled="disabled" @click="$emit('click')">{{ title }}<small v-if="subtitle" class="menu-item-reason">{{ subtitle }}</small></button>
    `,
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
    // no hint: the selection starts as a band
    api.regionHint.mockResolvedValue({ data: null, response: null, error: { response: { status: 404, data: { detail: { code: "not_found" } } } } });
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
    expect(takeCarriedReviewNotice("j2")).toEqual({ kind: "success", text: "Added Banana Mug Cake", detail: null, undoJobId: "j1" });
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

  test("Re-read on a flagged ingredient starts the selection on that line, where the server says it is", async () => {
    const hint = { page: 0, x: 0.05, y: 0.31, width: 0.9, height: 0.06, source: "ocr" };
    let answer: (value: unknown) => void = () => {};
    api.regionHint.mockImplementationOnce(() => new Promise((resolve) => {
      answer = resolve;
    }));
    const wrapper = await mountPage();

    await wrapper.get("[data-flag=\"unsure:ingredients:i1\"]").findAll("button").find(b => b.text() === "Re-read")!.trigger("click");
    const dialog = wrapper.getComponent(IngestRegionDialog);
    expect(api.regionHint).toHaveBeenCalledExactlyOnceWith("j1", { field: "ingredients", ref: "i1" });
    expect(dialog.props("modelValue")).toBe(true);
    expect(dialog.props("locating")).toBe(true);

    answer(ok(hint));
    await flushPromises();
    expect(dialog.props("locating")).toBe(false);
    expect(dialog.props("initialRegion")).toEqual(hint);
    expect(dialog.props("initialTarget")).toBe("ingredients:i1");
  });

  test("with no hint, or one too slow to wait for, the selection starts without it", async () => {
    const wrapper = await mountPage();
    await wrapper.get("[data-flag=\"blank:steps:s2\"]").findAll("button").find(b => b.text() === "Re-read")!.trigger("click");
    await flushPromises();
    const dialog = wrapper.getComponent(IngestRegionDialog);
    expect(dialog.props("locating")).toBe(false);
    expect(dialog.props("initialRegion")).toBeNull();

    // a server that doesn't answer: the dialog waits a moment, then starts without it
    api.regionHint.mockImplementationOnce(() => new Promise(() => {}));
    await wrapper.get(".ingest-ingredient__line").trigger("click");
    await wrapper.get(".ingest-ingredient__reread").trigger("click");
    expect(dialog.props("locating")).toBe(true);
    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();
    expect(dialog.props("locating")).toBe(false);
    expect(dialog.props("initialRegion")).toBeNull();
  });

  test("on a phone the ⋯ menu re-reads an area: nothing is preselected, a new ingredient or step can be chosen", async () => {
    const wrapper = await mountPage();

    await wrapper.findAll(".menu-item").find(item => item.text() === "Re-read an area")!.trigger("click");

    const dialog = wrapper.getComponent(IngestRegionDialog);
    expect(dialog.props("modelValue")).toBe(true);
    expect(dialog.props("initialTarget")).toBeNull();
    expect(dialog.props("targets")!.map((option: { value: string }) => option.value))
      .toEqual(expect.arrayContaining(["name", "ingredients:new", "steps:new"]));
    // no line to look for: the selection starts at once
    expect(dialog.props("locating")).toBe(false);
    expect(api.regionHint).not.toHaveBeenCalled();
  });

  test("a line or step re-reads its own area of the card", async () => {
    const wrapper = await mountPage();

    await wrapper.get(".ingest-ingredient__line").trigger("click");
    await wrapper.get(".ingest-ingredient__reread").trigger("click");
    expect(wrapper.getComponent(IngestRegionDialog).props("initialTarget")).toBe("ingredients:i1");

    await wrapper.findAll(".ingest-step__reread")[0]!.trigger("click");
    expect(wrapper.getComponent(IngestRegionDialog).props("initialTarget")).toBe("steps:s1");
  });

  test("a flagged note is marked like a step, and its flag finds the note by id wherever it is", async () => {
    const notes = [{ id: "n1", title: "From", text: "Grandma Jo" }, { id: "n2", title: "", text: "Doubles [illegible] in a 9x13 pan" }];
    const illegible: CardFlag = { id: "illegible:notes:n2", kind: "illegible", severity: "error", source: "marker", field: "notes", ref: "n2", params: {}, alternatives: ["well"] };
    api.getJob.mockResolvedValue(ok(job({ draft: { ...job().draft!, notes }, flags: [illegible] })));
    const wrapper = await mountPage();

    // upstream's RecipeNotes knows nothing of flags: the fork's note list marks the note the flag names
    expect(wrapper.find(".notes").exists()).toBe(false);
    expect(wrapper.get("#ingest-field-notes-n2").classes()).toContain("ingest-note--error");
    expect(wrapper.get("#ingest-field-notes-n1").classes()).not.toContain("ingest-note--error");
    const item = wrapper.get("[data-flag=\"illegible:notes:n2\"]");
    expect(item.text()).toContain("Doubles [illegible] in a 9x13 pan");
    // named as the note list numbers it
    expect(item.text()).toContain("· Note 2");

    // the first note goes: the flag still finds its note, and Edit scrolls to that note
    await wrapper.findAll(".ingest-note__delete")[0]!.trigger("click");
    expect(wrapper.get("#ingest-field-notes-n2").classes()).toContain("ingest-note--error");
    expect(item.text()).toContain("· Note 1");
    await item.findAll("button").find(b => b.text() === "Edit")!.trigger("click");
    await flushPromises();
    expect(scrolled.at(-1)).toBe("ingest-field-notes-n2");

    // what's typed over the unreadable spot goes into that note, which keeps its id
    await item.get("input").setValue("well");
    await item.findAll("button").find(b => b.text() === "Fill in")!.trigger("click");
    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();
    expect(api.updateJob.mock.calls.at(-1)![1].draft.notes).toEqual([{ id: "n2", title: "", text: "Doubles well in a 9x13 pan" }]);
    expect(wrapper.get("#ingest-field-notes-n2").classes()).not.toContain("ingest-note--error");
  });

  test("a note added in review gets an id that its saves keep, and a note's Re-read aims at that note", async () => {
    api.getJob.mockResolvedValue(ok(job({ draft: { ...job().draft!, notes: [{ id: "n1", title: "Tip", text: "Use a big mug" }] } })));
    const wrapper = await mountPage();

    await button(wrapper, "Add note").trigger("click");
    const added = wrapper.findAll(".ingest-note")[1]!;
    await added.findAll("textarea, input").at(-1)!.setValue("Freezes well");
    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();
    const sent = api.updateJob.mock.calls.at(-1)![1].draft.notes as { id: string; text: string }[];
    expect(sent.map(note => note.text)).toEqual(["Use a big mug", "Freezes well"]);
    expect(sent[0]!.id).toBe("n1");
    expect(sent[1]!.id).toMatch(/^[0-9a-f-]{36}$/);

    // an edit keeps it
    await added.findAll("textarea, input").at(-1)!.setValue("Freezes well for a month");
    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();
    expect(api.updateJob.mock.calls.at(-1)![1].draft.notes[1]).toEqual({ id: sent[1]!.id, title: "", text: "Freezes well for a month" });

    await wrapper.findAll(".ingest-note__reread")[0]!.trigger("click");
    const dialog = wrapper.getComponent(IngestRegionDialog);
    expect(dialog.props("initialTarget")).toBe("notes:n1");
    expect(dialog.props("targets")!.map((option: { value: string }) => option.value))
      .toEqual(expect.arrayContaining(["notes:n1", `notes:${sent[1]!.id}`, "notes:new"]));
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

  test("Save as eval case sends what the dialog says about the card: its tags and notes", async () => {
    api.saveEvalCase.mockResolvedValueOnce(ok({ slug: "banana-mug-cake", files: [] }));
    const wrapper = await mountPage();

    await wrapper.findAll(".menu-item").find(item => item.text() === "Save as eval case")!.trigger("click");
    const dialog = wrapper.getComponent(IngestEvalCaseDialog);
    expect(dialog.props("modelValue")).toBe(true);
    dialog.vm.$emit("save", { slug: "banana-mug-cake", verified: true, tags: ["handwritten", "faded"], notes: "Pencil" });
    await flushPromises();

    expect(api.saveEvalCase).toHaveBeenCalledExactlyOnceWith("j1", { slug: "banana-mug-cake", verified: true, tags: ["handwritten", "faded"], notes: "Pencil" });
    expect(dialog.props("modelValue")).toBe(false);
    expect(wrapper.get(".ingest-review-bar__notice").text()).toContain("Saved as banana-mug-cake");
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

  test("a failed card kept on this server can be read with cloud providers, once the reviewer agrees its photos leave", async () => {
    api.getJob.mockResolvedValue(ok(job({
      status: "failed",
      draft: null,
      flags: [],
      localOnly: true,
      error: { code: "local_only_unavailable", params: {} },
      permissions: { canDiscard: true, canReadWithCloud: true },
    })));
    api.readWithCloud.mockResolvedValueOnce(ok({ draftVersion: 0, status: "processing", task: { kind: "extract", state: "queued" }, proposalIds: [] }));
    const wrapper = await mountPage();

    expect(wrapper.get(".ingest-review__status").text()).toMatch(/^Failed: This card must stay on this server/);
    await button(wrapper, "Read with cloud providers").trigger("click");
    const dialog = wrapper.get(".dialog[data-title=\"Read with cloud providers\"]");
    expect(dialog.text()).toContain("This card was set to stay on this server. Reading it with a cloud provider sends its photos to that provider, outside your network.");
    expect(api.readWithCloud).not.toHaveBeenCalled();

    // Cancel changes nothing
    await dialog.get(".ingest-review__cloud-cancel").trigger("click");
    expect(wrapper.find(".dialog[data-title=\"Read with cloud providers\"]").exists()).toBe(false);
    expect(api.readWithCloud).not.toHaveBeenCalled();

    await button(wrapper, "Read with cloud providers").trigger("click");
    await wrapper.get(".ingest-review__cloud-confirm").trigger("click");
    await flushPromises();
    expect(api.readWithCloud).toHaveBeenCalledExactlyOnceWith("j1");
    expect(wrapper.find(".dialog[data-title=\"Read with cloud providers\"]").exists()).toBe(false);
    // being read now, and no longer kept on this server
    expect(wrapper.get(".ingest-review__status").text()).toBe("Waiting to be read Cancel");
    expect(wrapper.get(".ingest-review__position").text()).not.toContain("Local only");
  });

  test("reading with cloud providers isn't offered without the permission", async () => {
    api.getJob.mockResolvedValue(ok(job({
      status: "failed",
      draft: null,
      flags: [],
      localOnly: true,
      error: { code: "local_only_unavailable", params: {} },
      permissions: { canDiscard: true, canReadWithCloud: false },
    })));
    const wrapper = await mountPage();

    expect(wrapper.findAll("button").filter(b => b.text() === "Read with cloud providers")).toHaveLength(0);
    expect(button(wrapper, "Retry").exists()).toBe(true);
  });

  test("an added card can go back to review: the recipe is deleted once the reviewer agrees", async () => {
    const committed = job({
      status: "committed",
      flags: [],
      recipe: { id: "r1", slug: "banana-mug-cake", name: "Banana Mug Cake" },
      permissions: { canDiscard: false, canUncommit: true },
    });
    api.getJob.mockResolvedValue(ok(committed));
    const wrapper = await mountPage();

    expect(wrapper.get(".ingest-review__status").text()).toBe("Added");
    await button(wrapper, "Back to review").trigger("click");
    const dialog = () => wrapper.find(".dialog[data-title=\"Back to review\"]");
    expect(dialog().text()).toContain("This deletes the recipe \"Banana Mug Cake\" and brings the card back for review.");
    expect(api.uncommit).not.toHaveBeenCalled();

    api.uncommit.mockResolvedValueOnce(ok({ draftVersion: 4, status: "ready", task: null, proposalIds: [] }));
    api.getJob.mockResolvedValue(ok(job({ draftVersion: 4, recipe: null })));
    await dialog().get(".ingest-review__uncommit-confirm").trigger("click");
    await flushPromises();

    expect(api.uncommit).toHaveBeenCalledExactlyOnceWith("j1", {});
    expect(dialog().exists()).toBe(false);
    // the card is back, ready to review
    expect(wrapper.find(".ingest-review__status").exists()).toBe(false);
    expect(primary(wrapper).text()).toBe("1 to fix");
    expect(wrapper.get(".ingest-review-bar__notice").text()).toContain("The card is back for review.");
  });

  test("going back to review asks again when the recipe was edited since, and only then deletes it anyway", async () => {
    api.getJob.mockResolvedValue(ok(job({ status: "committed", flags: [], recipe: { id: "r1", slug: "banana-mug-cake", name: "Banana Mug Cake" }, permissions: { canUncommit: true } })));
    const wrapper = await mountPage();

    api.uncommit.mockResolvedValueOnce({ data: null, response: null, error: { response: { status: 409, data: { detail: { code: "recipe_edited" } } } } });
    await button(wrapper, "Back to review").trigger("click");
    await wrapper.get(".ingest-review__uncommit-confirm").trigger("click");
    await flushPromises();

    const edited = () => wrapper.find(".dialog[data-title=\"The recipe was changed\"]");
    expect(edited().text()).toContain("The recipe was changed after this card was added. Going back to review would delete those changes.");
    // keeping the recipe changes nothing
    await edited().get(".ingest-review__uncommit-keep").trigger("click");
    expect(edited().exists()).toBe(false);
    expect(api.uncommit).toHaveBeenCalledOnce();

    await button(wrapper, "Back to review").trigger("click");
    api.uncommit.mockResolvedValueOnce({ data: null, response: null, error: { response: { status: 409, data: { detail: { code: "recipe_edited" } } } } });
    await wrapper.get(".ingest-review__uncommit-confirm").trigger("click");
    await flushPromises();
    api.uncommit.mockResolvedValueOnce(ok({ draftVersion: 4, status: "ready", task: null, proposalIds: [] }));
    api.getJob.mockResolvedValue(ok(job({ draftVersion: 4, recipe: null })));
    await edited().get(".ingest-review__uncommit-force").trigger("click");
    await flushPromises();
    expect(api.uncommit).toHaveBeenLastCalledWith("j1", { force: true });
    expect(primary(wrapper).text()).toBe("1 to fix");
  });

  test("an added card isn't offered back to review without the permission", async () => {
    api.getJob.mockResolvedValue(ok(job({ status: "committed", flags: [], recipe: { id: "r1", slug: "banana-mug-cake" }, permissions: { canUncommit: false } })));
    const wrapper = await mountPage();

    expect(wrapper.findAll("button").filter(b => b.text() === "Back to review")).toHaveLength(0);
    expect(button(wrapper, "View recipe").attributes("data-to")).toBe("/g/home/r/banana-mug-cake");
  });

  test("the next card's review bar offers Undo for the card just added", async () => {
    carryReviewNotice("j1", { kind: "success", text: "Added Lemon Bars", undoJobId: "j0" });
    api.uncommit.mockResolvedValueOnce(ok({ draftVersion: 2, status: "ready", task: null, proposalIds: [] }));
    const wrapper = await mountPage();

    const notice = wrapper.get(".ingest-review-bar__notice");
    expect(notice.text()).toContain("Added Lemon Bars");
    await notice.findAll("button").find(b => b.text() === "Undo")!.trigger("click");
    await flushPromises();

    expect(api.uncommit).toHaveBeenCalledExactlyOnceWith("j0", {});
    expect(router.replace).toHaveBeenCalledExactlyOnceWith("/g/home/recipes/cards/j0");
  });

  test("a back sent as its own card can be added to the card before it, which opens being read", async () => {
    api.getBatch.mockResolvedValue(ok({ id: "b1", source: "app", jobs: [{ id: "j0", position: 0, status: "ready" }, { id: "j1", position: 1, status: "ready" }] }));
    const previous = job({ id: "j0", position: 0, pageCount: 1, permissions: { canMerge: true } });
    api.getJob.mockImplementation((id: string) => Promise.resolve(ok(id === "j0" ? previous : job({ position: 1, permissions: { canDiscard: true, canMerge: true } }))));
    api.merge.mockResolvedValueOnce(ok({ draftVersion: 3, status: "ready", task: { kind: "extract", state: "queued" }, proposalIds: [] }));
    const wrapper = await mountPage();
    const item = () => wrapper.get(".ingest-review__merge");

    // opening the ⋯ menu checks the card before this one
    await button(wrapper, "More").trigger("click");
    await flushPromises();
    expect(api.getJob).toHaveBeenLastCalledWith("j0");
    expect(item().text()).toBe("Add as back of previous card");
    expect(item().attributes("disabled")).toBeUndefined();

    await item().trigger("click");
    const dialog = () => wrapper.find(".dialog[data-title=\"Add as back of previous card\"]");
    expect(dialog().text()).toContain("This card's photos are added to card 1 as its back, and that card is read again. This card is then removed.");
    expect(api.merge).not.toHaveBeenCalled();
    await dialog().get(".ingest-review__merge-confirm").trigger("click");
    await flushPromises();

    expect(api.merge).toHaveBeenCalledExactlyOnceWith("j1", { intoJobId: "j0" });
    expect(router.replace).toHaveBeenCalledExactlyOnceWith("/g/home/recipes/cards/j0");
    expect(takeCarriedReviewNotice("j0")).toEqual({ kind: "info", text: "Photos added. The card is being read again.", detail: null });
  });

  test("Add as back of previous card is off, with the reason, when it can't be done", async () => {
    api.getBatch.mockResolvedValue(ok({ id: "b1", source: "app", jobs: [{ id: "j0", position: 0, status: "committed" }, { id: "j1", position: 1, status: "ready" }] }));
    let previous = job({ id: "j0", position: 0, status: "committed", pageCount: 1, permissions: { canMerge: false } });
    api.getJob.mockImplementation((id: string) => Promise.resolve(ok(id === "j0" ? previous : job({ position: 1, pageCount: 2, permissions: { canMerge: true } }))));
    const wrapper = await mountPage();
    const item = () => wrapper.get(".ingest-review__merge");

    // until the menu opens, the card before it isn't known
    expect(item().attributes("disabled")).toBeDefined();
    expect(item().get(".menu-item-reason").text()).toBe("Checking the previous card…");

    await button(wrapper, "More").trigger("click");
    await flushPromises();
    expect(item().attributes("disabled")).toBeDefined();
    expect(item().get(".menu-item-reason").text()).toBe("The previous card was already added as a recipe.");

    previous = job({ id: "j0", position: 0, pageCount: 3, permissions: { canMerge: true } });
    await button(wrapper, "More").trigger("click");
    await flushPromises();
    expect(item().get(".menu-item-reason").text()).toBe("Together they would have more than 4 photos.");
    expect(api.merge).not.toHaveBeenCalled();
  });

  test("the batch's first card, or a card the member can't change, isn't offered as a back", async () => {
    api.getJob.mockResolvedValue(ok(job({ permissions: { canDiscard: true, canMerge: true } })));
    const first = await mountPage();
    await button(first, "More").trigger("click");
    await flushPromises();
    expect(first.get(".ingest-review__merge").get(".menu-item-reason").text()).toBe("This is the first card in its batch.");
    expect(first.get(".ingest-review__merge").attributes("disabled")).toBeDefined();
    first.unmount();
    wrappers.length = 0;

    api.getJob.mockResolvedValue(ok(job({ permissions: { canDiscard: true, canMerge: false } })));
    const other = await mountPage();
    expect(other.find(".ingest-review__merge").exists()).toBe(false);
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

  // ==========================================
  // Wave 3: rebuild, Parse with AI, the duplicate banner, failed dates, link checks

  const extracting = { kind: "extract", state: "queued" };
  const idle = (overrides = {}) => ok({ draftVersion: 3, status: "ready", task: null, proposalIds: [], ...overrides });

  test("What the card says can be corrected, and the recipe rebuilt from it; the page waits, saying so", async () => {
    api.rebuild.mockResolvedValueOnce(idle({ task: extracting }));
    const wrapper = await mountPage();

    await button(wrapper, "What the card says").trigger("click");
    const dialog = () => wrapper.find(".dialog[data-title=\"What the card says\"]");
    await dialog().get(".ingest-transcription__edit").trigger("click");
    // (the stub box is one line; the component's own test keeps the lines)
    await dialog().get(".ingest-transcription .textarea input").setValue("Banana Mug Cake. 1/4 t. salt. Microwave on high for 2 minutes.");
    await dialog().get(".ingest-transcription__rebuild").trigger("click");
    await flushPromises();

    expect(api.rebuild).toHaveBeenCalledExactlyOnceWith("j1", { transcription: "Banana Mug Cake. 1/4 t. salt. Microwave on high for 2 minutes." });
    expect(dialog().exists()).toBe(false);
    expect(wrapper.get(".ingest-review__reading").text()).toContain("Rebuilding the recipe from your text. You can edit it when that's done.");

    // done: the draft nobody had edited is replaced, and the review bar says where it came from
    api.getJobState.mockResolvedValueOnce(idle({ draftVersion: 4 }));
    api.getJob.mockResolvedValueOnce(ok(job({ draftVersion: 4, transcription: "Banana Mug Cake\n1/4 t. salt\nMicrowave on high for 2 minutes.", flags: [unsure] })));
    await vi.advanceTimersByTimeAsync(2000);
    await flushPromises();
    expect(wrapper.find(".ingest-review__reading").exists()).toBe(false);
    expect(wrapper.get(".ingest-review-bar__notice").text()).toContain("Rebuilt from your text");
  });

  test("on desktop the card panel's text is corrected in place", async () => {
    api.rebuild.mockResolvedValueOnce(idle({ task: extracting }));
    const wrapper = await mountPage(true);

    await wrapper.findAll("button").find(b => b.text() === "What the card says" && !b.classes("menu-item"))!.trigger("click");
    const panel = wrapper.get(".ingest-card-panel");
    await panel.get(".ingest-transcription__edit").trigger("click");
    await panel.get(".ingest-transcription .textarea input").setValue("Banana Mug Cake");
    await panel.get(".ingest-transcription__rebuild").trigger("click");
    await flushPromises();

    expect(api.rebuild).toHaveBeenCalledOnce();
    expect(panel.find(".ingest-transcription .textarea").exists()).toBe(false);
    // nothing more can be rebuilt while it runs
    expect(panel.find(".ingest-transcription__edit").exists()).toBe(false);
  });

  test("Parse with AI on Check this ingredient parses that line; the line shows it, and takes the parse when done", async () => {
    const check: CardFlag = { id: "check_parse:ingredients:i1", kind: "check_parse", severity: "warning", source: "parser", field: "ingredients", ref: "i1", params: { confidence: 55 }, alternatives: [] };
    api.getJob.mockResolvedValue(ok(job({ flags: [check] })));
    api.parseLines.mockResolvedValueOnce(idle({ task: extracting }));
    const wrapper = await mountPage();

    await wrapper.get("[data-flag=\"check_parse:ingredients:i1\"]").get(".ingest-flag-item__parse").trigger("click");
    await flushPromises();
    expect(api.parseLines).toHaveBeenCalledExactlyOnceWith("j1", { refs: ["i1"] });
    expect(wrapper.get(".ingest-review__reading").text()).toContain("Parsing with AI. You can edit the card when that's done.");
    expect(wrapper.get(".ingest-ingredient__parsing").text()).toBe("Parsing with AI…");

    const parsed = { ...job().draft!.ingredients![0]!, quantity: 0.25, unit: { id: "u-tsp", name: "teaspoon" }, food: { id: "f-salt", name: "salt" }, display: "1/4 teaspoon salt", parseConfidence: 0.95 };
    api.getJobState.mockResolvedValueOnce(idle({ draftVersion: 4 }));
    api.getJob.mockResolvedValue(ok(job({ draftVersion: 4, draft: { ...job().draft!, ingredients: [parsed] }, flags: [] })));
    await vi.advanceTimersByTimeAsync(2000);
    await flushPromises();
    expect(wrapper.find(".ingest-ingredient__parsing").exists()).toBe(false);
    expect(wrapper.find(".ingest-review__reading").exists()).toBe(false);
    expect(wrapper.get(".ingest-review-bar__notice").text()).toContain("Parsed with AI");
  });

  test("the open line parses itself with AI; a card in another language parses its lines kept as text at once", async () => {
    const lines = [
      { referenceId: "i1", originalText: "2 tazas de harina", quantity: null, unit: null, food: null, note: "2 tazas de harina", display: "2 tazas de harina" },
      { referenceId: "i2", originalText: "1 [illegible] de sal", quantity: null, unit: null, food: null, note: "1 [illegible] de sal", display: "1 [illegible] de sal" },
      { referenceId: "i3", originalText: "3 huevos", quantity: null, unit: null, food: null, note: "3 huevos", display: "3 huevos" },
    ];
    const notParsed: CardFlag = { id: "not_parsed:card", kind: "not_parsed", severity: "info", source: "parser", field: "card", ref: null, params: {}, alternatives: [] };
    api.getJob.mockResolvedValue(ok(job({ draft: { ...job().draft!, ingredients: lines }, flags: [notParsed] })));
    api.parseLines.mockResolvedValue(idle({ task: extracting }));
    const wrapper = await mountPage();

    const info = wrapper.get(".ingest-review__info");
    expect(info.text()).toContain("Ingredients kept as text");
    await info.get(".ingest-review__parse-all").trigger("click");
    await flushPromises();
    // the line with a marker has a flag of its own
    expect(api.parseLines).toHaveBeenCalledExactlyOnceWith("j1", { refs: ["i1", "i3"] });
    expect(wrapper.findAll(".ingest-ingredient__parsing")).toHaveLength(2);

    api.getJobState.mockResolvedValueOnce(idle());
    await vi.advanceTimersByTimeAsync(2000);
    await flushPromises();
    await wrapper.findAll(".ingest-ingredient__line")[2]!.trigger("click");
    await wrapper.get(".ingest-ingredient__parse").trigger("click");
    await flushPromises();
    expect(api.parseLines).toHaveBeenLastCalledWith("j1", { refs: ["i3"] });
  });

  test("the possible-duplicate banner names what commit would call the recipe, a near name, or a card waiting", async () => {
    api.getJob.mockResolvedValue(ok(job({
      duplicateOf: { id: "r0", slug: "banana-mug-cake", name: "Banana Mug Cake" },
      duplicateName: "Banana Mug Cake (2)",
      duplicateJob: { id: "j7", title: "Banana Mug Cake" },
    })));
    const wrapper = await mountPage();
    const banner = () => wrapper.find(".ingest-review__duplicate");

    expect(banner().get(".ingest-review__duplicate-recipe").text())
      .toContain("A recipe called \"Banana Mug Cake\" already exists. Adding this card makes \"Banana Mug Cake (2)\".");
    expect(banner().get(".ingest-review__duplicate-card").text()).toContain("Another card waiting has the same name.");
    await banner().get(".ingest-review__duplicate-card button").trigger("click");
    expect(router.replace).toHaveBeenLastCalledWith("/g/home/recipes/cards/j7");

    // renamed: the save's answer says the name is like another recipe's, and no card waits with it
    api.updateJob.mockResolvedValueOnce(ok({
      draftVersion: 4,
      flags: [unsure],
      duplicateOf: { id: "r5", slug: "banana-mug-cakes", name: "Banana Mug Cakes" },
      duplicateJob: null,
      duplicateName: null,
    }));
    await wrapper.get("#ingest-field-name input").setValue("Banana Mug-Cake");
    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();
    expect(banner().text()).toContain("A recipe with a similar name already exists: \"Banana Mug Cakes\".");
    expect(banner().find(".ingest-review__duplicate-card").exists()).toBe(false);

    // and away from both: the banner goes
    api.updateJob.mockResolvedValueOnce(ok({ draftVersion: 5, flags: [unsure], duplicateOf: null, duplicateJob: null, duplicateName: null }));
    await wrapper.get("#ingest-field-name input").setValue("Banana Bread");
    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();
    expect(banner().exists()).toBe(false);
  });

  test("a failed card says when it's read again by itself and when it's removed", async () => {
    // dates the server sends without an offset are UTC; they show in the reader's time zone
    const date = (iso: string, withTime = false) => formatIngestDate(new Date(`${iso}Z`), "en-US", withTime);
    api.getJob.mockResolvedValue(ok(job({
      status: "failed",
      draft: null,
      flags: [],
      error: { code: "limit_reached", params: {} },
      autoRetryAt: "2099-11-01T00:00:00",
      expiresAt: "2099-11-15T00:00:00",
    })));
    const wrapper = await mountPage();

    expect(wrapper.findAll(".ingest-review__failed-when").map(line => line.text())).toEqual([
      `Tries again on ${date("2099-11-01T00:00:00", true)}`,
      `Removed on ${date("2099-11-15T00:00:00")} unless it's read again before then.`,
    ]);

    // a card that isn't waiting for the limits: only when it's removed; one due now is read shortly
    api.getJob.mockResolvedValue(ok(job({ status: "failed", draft: null, flags: [], error: { code: "no_recipe_found", params: {} }, expiresAt: "2099-10-18T09:00:00" })));
    const other = await mountPage();
    expect(other.findAll(".ingest-review__failed-when").map(line => line.text()))
      .toEqual([`Removed on ${date("2099-10-18T09:00:00")} unless it's read again before then.`]);

    api.getJob.mockResolvedValue(ok(job({ status: "failed", draft: null, flags: [], error: { code: "limit_reached", params: {} }, autoRetryAt: "2000-01-01T00:00:00" })));
    const due = await mountPage();
    expect(due.findAll(".ingest-review__failed-when").map(line => line.text())).toEqual(["Tries again shortly"]);
  });

  test("a near-miss link can be kept as a new food; skipped tag suggestions say why", async () => {
    const draft = job().draft!;
    const onion = { referenceId: "i2", originalText: "2 rd onions, diced", quantity: 2, unit: null, food: { id: "f-red", name: "red onion" }, note: "diced", display: "2 red onion diced" };
    const fuzzy: CardFlag = { id: "linked_fuzzy:ingredients:i2", kind: "linked_fuzzy", severity: "warning", source: "parser", field: "ingredients", ref: "i2", params: { name: "red onion", kind: "food", start: 2, end: 11 }, alternatives: [] };
    const skipped: CardFlag = { id: "organizers_skipped:card", kind: "organizers_skipped", severity: "info", source: "model", field: "card", ref: null, params: { reason: "local_only" }, alternatives: [] };
    api.getJob.mockResolvedValue(ok(job({
      draft: { ...draft, ingredients: [...draft.ingredients!, onion] },
      flags: [fuzzy, skipped],
      permissions: { canCreateFoods: true, canDiscard: true },
    })));
    const wrapper = await mountPage();

    expect(wrapper.get(".ingest-review__info").text())
      .toBe("No tags suggested: Tags, categories and tools weren't suggested: this card stays on this server, and no AI provider on your network can suggest them.");
    const item = wrapper.get("[data-flag=\"linked_fuzzy:ingredients:i2\"]");
    expect(item.text()).toContain("Linked to \"red onion\": check it's the same thing.");
    await item.get(".ingest-flag-item__keep-new").trigger("click");
    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();
    expect(api.updateJob.mock.calls[0]![1].draft.ingredients[1].food).toEqual({ id: null, name: "rd onions" });
  });
});
