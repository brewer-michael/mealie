import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { defineComponent, h, nextTick } from "vue";
import {
  applyAlternative,
  applyProposal,
  buildNeedsALook,
  draftsEqual,
  editFlaggedText,
  fieldText,
  fillBlank,
  firstCardToReview,
  flagAlternatives,
  flagsForField,
  fixIngredient,
  formatQuantity,
  highlightSegments,
  ingredientDisplay,
  nextCardInBatch,
  normalizeDraft,
  parseQuantity,
  regionFromCropResult,
  rereadTargets,
  rereadTargetValue,
  sortFlags,
  suggestEvalSlug,
  useRecipeIngestReview,
  type RecipeIngestReview,
} from "../use-recipe-ingest-review";
import { resetRecipeIngestCounts } from "../use-recipe-ingest";
import type {
  CardDraft,
  CardFlag,
  CardProposal,
  RecipeIngestionBatchJob,
  RecipeIngestionJobOut,
  RecipeIngestionJobState,
  RereadRequest,
} from "~/lib/api/types/recipe-ingest";

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

vi.mock("~/composables/api", () => ({
  useUserApi: () => ({ recipeIngest: api }),
}));
vi.mock("~/composables/use-toast", () => ({
  alert: toast,
}));

// ==========================================
// Fixtures: the banana card (docs/ai/PHASE2.md F7) as the server returns it

function bananaDraft(overrides: Partial<CardDraft> = {}): CardDraft {
  return {
    schemaVersion: 1,
    name: "Banana Mug Cake",
    description: "",
    attribution: "From Grandma Jo",
    useCardAsCover: true,
    ingredients: [
      {
        referenceId: "i1",
        originalText: "1 T. coconut oil (melted)",
        quantity: 1,
        unit: { id: "u-tbsp", name: "tablespoon" },
        food: { id: "f-oil", name: "coconut oil" },
        note: "(melted)",
        display: "1 tablespoon coconut oil (melted)",
      },
      {
        referenceId: "i2",
        originalText: "1/4 t. salt",
        quantity: 0.25,
        unit: { id: "u-tsp", name: "teaspoon" },
        food: { id: null, name: "salt" },
        note: "",
        display: "1/4 teaspoon salt",
      },
      {
        referenceId: "i3",
        originalText: "[illegible] banana",
        quantity: null,
        unit: null,
        food: null,
        note: "[illegible] banana",
        display: "[illegible] banana",
      },
    ],
    steps: [
      { id: "s1", text: "Mix everything in a mug." },
      { id: "s2", text: "Microwave on high for [blank] minutes." },
    ],
    notes: [],
    tags: [],
    categories: [],
    tools: [],
    ...overrides,
  };
}

function flag(overrides: Partial<CardFlag> = {}): CardFlag {
  return {
    id: "blank:steps:s2",
    kind: "blank",
    severity: "error",
    source: "marker",
    field: "steps",
    ref: "s2",
    params: {},
    alternatives: [],
    resolution: null,
    ...overrides,
  };
}

const blankFlag = flag();
const unsureFlag = flag({
  id: "unsure:ingredients:i2",
  kind: "unsure",
  severity: "warning",
  source: "model",
  field: "ingredients",
  ref: "i2",
  params: { text: "1/4" },
  alternatives: ["1/2"],
});

function job(overrides: Partial<RecipeIngestionJobOut> = {}): RecipeIngestionJobOut {
  return {
    id: "j1",
    batchId: "b1",
    position: 0,
    status: "ready",
    source: "app",
    pageCount: 1,
    draftVersion: 3,
    task: null,
    error: null,
    pages: [],
    draft: bananaDraft(),
    flags: [blankFlag, unsureFlag],
    proposals: [],
    permissions: { canCreateFoods: true, canDiscard: true, canExportEval: false },
    ...overrides,
  };
}

function batchJob(id: string, position: number, status: RecipeIngestionBatchJob["status"], errorCount = 0, warningCount = 0): RecipeIngestionBatchJob {
  return { id, position, status, errorCount, warningCount };
}

function state(overrides: Partial<RecipeIngestionJobState> = {}): RecipeIngestionJobState {
  return { draftVersion: 3, status: "ready", task: null, proposalIds: [], error: null, ...overrides };
}

function apiError(status: number, detail: unknown) {
  return { data: null, response: null, error: { response: { status, data: { detail } } } };
}

const ok = (data: unknown) => ({ data, error: null, response: {} });

// ==========================================
// Pure helpers

describe("batches", () => {
  const jobs = [
    batchJob("c", 2, "ready", 0, 1),
    batchJob("a", 0, "committed"),
    batchJob("b", 1, "ready"),
    batchJob("d", 3, "processing"),
    batchJob("e", 4, "ready", 1),
  ];

  test("a batch's review starts at the first card, in capture order, that needs a look", () => {
    expect(firstCardToReview(jobs)).toBe("c");
  });

  test("else at its first ready card, else nowhere (the queue)", () => {
    expect(firstCardToReview([batchJob("b", 1, "ready"), batchJob("a", 0, "ready")])).toBe("a");
    expect(firstCardToReview([batchJob("a", 0, "committed"), batchJob("d", 3, "failed", 2)])).toBeNull();
    expect(firstCardToReview([])).toBeNull();
  });

  test("Commit & next goes to the next ready card, wrapping round to skipped ones", () => {
    expect(nextCardInBatch(jobs, "b")).toBe("c");
    expect(nextCardInBatch(jobs, "c")).toBe("e");
    // past the end: back to the card skipped earlier
    expect(nextCardInBatch(jobs, "e")).toBe("b");
    expect(nextCardInBatch([batchJob("b", 1, "ready")], "b")).toBeNull();
    // a card that isn't in the list (just committed elsewhere) starts from the top
    expect(nextCardInBatch(jobs, "zz")).toBe("b");
  });
});

describe("flags", () => {
  test("flags for a field match its line, whatever case the server names the field in", () => {
    const flags = [
      blankFlag,
      unsureFlag,
      flag({ id: "not_on_card:prep_time:", kind: "not_on_card", severity: "warning", field: "prep_time", ref: null }),
    ];

    expect(flagsForField(flags, "steps").map(f => f.id)).toEqual(["blank:steps:s2"]);
    expect(flagsForField(flags, "steps", "s1")).toEqual([]);
    expect(flagsForField(flags, "ingredients", "i2")).toEqual([unsureFlag]);
    expect(flagsForField(flags, "prepTime").map(f => f.id)).toEqual(["not_on_card:prep_time:"]);
    expect(flagsForField(flags, "prep_time", null)).toHaveLength(1);
  });

  test("an alternative replaces the flagged part, or the whole line when it isn't there", () => {
    expect(applyAlternative("1/4 t. salt", unsureFlag, "1/2")).toBe("1/2 t. salt");
    const disagreement = flag({ kind: "read_disagreement", severity: "warning", params: { text: "Microwave [blank] minutes" } });
    expect(applyAlternative("Microwave 2 minutes", disagreement, "Microwave [blank] minutes")).toBe("Microwave [blank] minutes");
    const amount = flag({ kind: "implausible_amount", severity: "warning", params: { value: "11/2", suggestion: "1 1/2" } });
    expect(flagAlternatives(amount)).toEqual(["1 1/2"]);
    expect(applyAlternative("11/2 cups flour", amount, "1 1/2")).toBe("1 1/2 cups flour");
  });

  test("a typed value replaces the blank, and only the blank", () => {
    expect(fillBlank("Microwave on high for [blank] minutes.", " 2 ")).toBe("Microwave on high for 2 minutes.");
    expect(fillBlank("[blank] to [blank] minutes", "2")).toBe("2 to [blank] minutes");
    expect(fillBlank("Microwave on high for [blank] minutes.", "   ")).toBe("Microwave on high for [blank] minutes.");
    expect(fillBlank("No gap here", "2")).toBe("No gap here");
    // a second reading saw a blank where this one has a number: the number is what's replaced
    expect(fillBlank("Microwave 2 minutes", "1", "2")).toBe("Microwave 1 minutes");
  });

  test("the flagged part is highlighted, and a blank shows as a gap", () => {
    expect(highlightSegments("Microwave for [blank] minutes.", "[blank]")).toEqual([
      { text: "Microwave for ", mark: false },
      { text: "___", mark: true },
      { text: " minutes.", mark: false },
    ]);
    expect(highlightSegments("1/4 t. salt", "3")).toEqual([{ text: "1/4 t. salt", mark: false }]);
  });

  test("flags read top to bottom: the card, the name, then ingredients and steps in order", () => {
    const draft = bananaDraft();
    const flags = [
      blankFlag,
      flag({ id: "check_parse:ingredients:i3", kind: "check_parse", severity: "warning", field: "ingredients", ref: "i3" }),
      unsureFlag,
      flag({ id: "read_by_ocr:card:", kind: "read_by_ocr", severity: "warning", field: "card", ref: null }),
      flag({ id: "missing_name:name:", kind: "missing_name", field: "name", ref: null }),
    ];
    expect(sortFlags(flags, draft).map(f => f.id)).toEqual([
      "read_by_ocr:card:",
      "missing_name:name:",
      "unsure:ingredients:i2",
      "check_parse:ingredients:i3",
      "blank:steps:s2",
    ]);
  });

  test("the list keeps what was fixed or resolved, and puts re-reads inside their line's item", () => {
    const draft = bananaDraft();
    const kept = { ...unsureFlag, resolution: "dismissed" as const };
    const gone = flag({ id: "illegible:ingredients:i3", kind: "illegible", field: "ingredients", ref: "i3" });
    const proposal: CardProposal = { id: "p1", kind: "region", target: { field: "steps", ref: "s2" }, text: "2", readable: true };
    const other: CardProposal = { id: "p2", kind: "region", target: { field: "name", ref: null }, text: "Banana Cake", readable: true };

    const { items, otherProposals } = buildNeedsALook([blankFlag, unsureFlag, gone], [blankFlag, kept], new Set(), draft, [proposal, other]);

    expect(items.map(item => [item.flag.id, item.state])).toEqual([
      ["unsure:ingredients:i2", "resolved"],
      ["illegible:ingredients:i3", "fixed"],
      ["blank:steps:s2", "open"],
    ]);
    const blank = items[2]!;
    expect(blank.text).toBe("Microwave on high for [blank] minutes.");
    expect(blank.fragment).toBe("[blank]");
    expect(blank.line).toBe(1);
    expect(blank.proposals).toEqual([proposal]);
    expect(otherProposals).toEqual([other]);

    // a one-tap fix shows at once, before the save comes back
    const fixed = buildNeedsALook([blankFlag], [blankFlag], new Set([blankFlag.id]), draft, []);
    expect(fixed.items[0]!.state).toBe("fixed");
  });

  test("an ingredient's line is the card's line once parsed, else its text", () => {
    const draft = bananaDraft();
    expect(fieldText(draft, "ingredients", "i1")).toBe("1 T. coconut oil (melted)");
    expect(fieldText(draft, "ingredients", "i3")).toBe("[illegible] banana");
    expect(fieldText(draft, "steps", "s2")).toBe("Microwave on high for [blank] minutes.");
    expect(fieldText({ ...draft, prepTime: "5 minutes" }, "prep_time")).toBe("5 minutes");
  });

  test("a flag on a note is about the note its ref names, not the first one holding the marker", () => {
    // the server keys notes by index: two notes with a blank raise blank:notes:0 and blank:notes:1
    const draft = normalizeDraft(bananaDraft({
      notes: [{ title: "", text: "Bake [blank] min if doubled" }, { title: "", text: "Cool [blank] min" }],
    }));
    const second = flag({ id: "blank:notes:1", field: "notes", ref: "1" });
    const first = flag({ id: "blank:notes:0", field: "notes", ref: "0" });

    expect(fieldText(draft, "notes", "1")).toBe("Cool [blank] min");
    const { items } = buildNeedsALook([second, first], [second, first], new Set(), draft, []);
    expect(items.map(item => [item.flag.id, item.line, item.text])).toEqual([
      ["blank:notes:0", 0, "Bake [blank] min if doubled"],
      ["blank:notes:1", 1, "Cool [blank] min"],
    ]);

    expect(editFlaggedText(draft, second, "5", "fill")).toBe(true);
    expect(draft.notes.map(note => note.text)).toEqual(["Bake [blank] min if doubled", "Cool 5 min"]);
    expect(editFlaggedText(draft, first, "20", "fill")).toBe(true);
    expect(draft.notes.map(note => note.text)).toEqual(["Bake 20 min if doubled", "Cool 5 min"]);
  });

  test("a marker in a note's title is filled there", () => {
    const draft = normalizeDraft(bananaDraft({
      notes: [{ title: "", text: "Grandma Jo's, 1962" }, { title: "From [blank]", text: "Can double for a 9x13 pan" }],
    }));
    const titleFlag = flag({ id: "blank:notes:1", field: "notes", ref: "1" });

    expect(fieldText(draft, "notes", "1")).toBe("From [blank]\nCan double for a 9x13 pan");
    expect(editFlaggedText(draft, titleFlag, "Aunt May", "fill")).toBe(true);
    expect(draft.notes[1]).toMatchObject({ title: "From Aunt May", text: "Can double for a 9x13 pan" });
    expect(draft.notes[0]!.text).toBe("Grandma Jo's, 1962");
    // a note that's gone points at nothing
    expect(editFlaggedText(draft, flag({ id: "blank:notes:5", field: "notes", ref: "5" }), "x", "fill")).toBe(false);
  });
});

describe("ingredients", () => {
  test("amounts are read and written the way cards write them", () => {
    expect(parseQuantity("1 1/2")).toBe(1.5);
    expect(parseQuantity("1/4")).toBe(0.25);
    expect(parseQuantity("½")).toBe(0.5);
    expect(parseQuantity("1½")).toBe(1.5);
    expect(parseQuantity("1,5")).toBe(1.5);
    expect(parseQuantity("a pinch")).toBeNull();
    expect(parseQuantity("")).toBeNull();
    expect(formatQuantity(1.5)).toBe("1 1/2");
    expect(formatQuantity(0.25)).toBe("1/4");
    expect(formatQuantity(1 / 3)).toBe("1/3");
    expect(formatQuantity(2)).toBe("2");
    expect(formatQuantity(null)).toBe("");
  });

  test("a line reads like the card", () => {
    const [oil] = bananaDraft().ingredients!;
    expect(ingredientDisplay({ ...oil!, quantity: 1.5 })).toBe("1 1/2 tablespoon coconut oil (melted)");
    expect(ingredientDisplay({ referenceId: "x", note: "a pinch of salt" })).toBe("a pinch of salt");
  });

  test("a fix changes the part of a parsed line that holds the flagged text", () => {
    const salt = bananaDraft().ingredients![1]!;
    const fixed = fixIngredient(salt, unsureFlag, "1/2", "alternative");
    expect(fixed.quantity).toBe(0.5);
    expect(fixed.unit).toEqual(salt.unit);
    expect(fixed.display).toBe("1/2 teaspoon salt");
  });

  test("a fix on a line kept as text changes its text", () => {
    const banana = bananaDraft().ingredients![2]!;
    const illegible = flag({ id: "illegible:ingredients:i3", kind: "illegible", field: "ingredients", ref: "i3" });
    const fixed = fixIngredient(banana, illegible, "1 ripe", "fill");
    expect(fixed.note).toBe("1 ripe banana");
    expect(fixed.display).toBe("1 ripe banana");
  });
});

describe("drafts and proposals", () => {
  test("drafts compare by content, not key order", () => {
    const draft = bananaDraft();
    const reordered = JSON.parse(JSON.stringify({ steps: draft.steps, ...draft })) as CardDraft;
    expect(draftsEqual(draft, reordered)).toBe(true);
    expect(draftsEqual({ name: "a", prepTime: undefined }, { name: "a" })).toBe(true);
    expect(draftsEqual(draft, { ...draft, name: "Banana Bread" })).toBe(false);
    expect(draftsEqual(null, undefined)).toBe(true);
  });

  test("a region re-read replaces the step's text, or is added to its end", () => {
    const proposal: CardProposal = { id: "p1", kind: "region", target: { field: "steps", ref: "s2" }, text: "Microwave on high for 2 minutes.", readable: true };
    expect(applyProposal(normalizeDraft(bananaDraft()), proposal, "replace").steps[1]!.text).toBe("Microwave on high for 2 minutes.");

    const tail: CardProposal = { ...proposal, text: "Let it cool." };
    expect(applyProposal(normalizeDraft(bananaDraft()), tail, "append").steps[1]!.text)
      .toBe("Microwave on high for [blank] minutes. Let it cool.");
  });

  test("a re-read ingredient takes the parsed line the server sent, keeping its place", () => {
    const proposal: CardProposal = {
      id: "p1",
      kind: "region",
      target: { field: "ingredients", ref: "i3" },
      text: "1 ripe banana",
      readable: true,
      draft: { ingredients: [{ referenceId: "tmp", originalText: "1 ripe banana", quantity: 1, food: { id: "f-banana", name: "banana" }, note: "ripe" }] },
    };
    const draft = applyProposal(normalizeDraft(bananaDraft()), proposal, "replace");
    expect(draft.ingredients).toHaveLength(3);
    expect(draft.ingredients[2]).toMatchObject({ referenceId: "i3", quantity: 1, food: { id: "f-banana" }, display: "1 banana ripe" });
  });

  test("a reading for a new step is added", () => {
    const proposal: CardProposal = { id: "p1", kind: "region", target: { field: "steps", ref: null }, text: "Serve warm.", readable: true };
    const draft = applyProposal(normalizeDraft(bananaDraft()), proposal, "replace");
    expect(draft.steps.map(step => step.text)).toEqual(["Mix everything in a mug.", "Microwave on high for [blank] minutes.", "Serve warm."]);
  });

  test("re-read targets name each field by its JSON name, the lines that exist, and a new line", () => {
    const options = rereadTargets(normalizeDraft(bananaDraft()));
    expect(options.find(option => option.kind === "prepTime")!.target).toEqual({ field: "prepTime", ref: null });
    // an ingredient or step target names its line, except the new one (the server takes a target without a ref)
    const lines = options.filter(option => ["ingredient", "step"].includes(option.kind));
    expect(lines.length).toBeGreaterThan(0);
    expect(lines.every(option => option.target.ref)).toBe(true);
    expect(options.find(option => option.kind === "new-ingredient")!.target).toEqual({ field: "ingredients", ref: null });
    expect(options.find(option => option.kind === "new-step")!.target).toEqual({ field: "steps", ref: null });
    expect(options.find(option => option.kind === "note")!.target).toEqual({ field: "notes", ref: null });
    expect(rereadTargetValue(options, "steps", "s2")).toBe("steps:s2");
    expect(rereadTargetValue(options, "prep_time", null)).toBe("prepTime");
    expect(rereadTargetValue(options, "ingredients", "gone")).toBe("ingredients:i1");
    // an empty section's flag has no line: its re-read adds one
    expect(rereadTargetValue(options, "steps", null)).toBe("steps:new");
    expect(rereadTargetValue(rereadTargets(normalizeDraft({ name: "Banana Mug Cake" })), "ingredients", "gone")).toBe("ingredients:new");
  });

  test("eval case names are made from the recipe's name", () => {
    expect(suggestEvalSlug("Grandma's Banana Mug Cake!")).toBe("grandma-s-banana-mug-cake");
    expect(suggestEvalSlug("  Crème brûlée  ")).toBe("creme-brulee");
    expect(suggestEvalSlug(null)).toBe("");
  });
});

describe("crop regions", () => {
  test("a selection becomes fractions of the upright page", () => {
    const region = regionFromCropResult({
      coordinates: { left: 512, top: 1024, width: 1024, height: 256 },
      image: { width: 2048, height: 1536 },
    });
    expect(region).toEqual({ x: 0.25, y: 0.6666, width: 0.5, height: 0.1666 });
  });

  test("a selection never reaches past the page's edge", () => {
    const region = regionFromCropResult({
      coordinates: { left: 1900, top: -10, width: 400, height: 2000 },
      image: { width: 2000, height: 1000 },
    })!;
    expect(region.x + region.width).toBeLessThanOrEqual(1);
    expect(region.y).toBe(0);
    expect(region.height).toBe(1);
  });

  test("a sliver or a missing image is no region", () => {
    expect(regionFromCropResult({ coordinates: { left: 0, top: 0, width: 20, height: 400 }, image: { width: 2000, height: 1000 } })).toBeNull();
    expect(regionFromCropResult({ coordinates: { left: 0, top: 0, width: 20, height: 20 }, image: { width: 0, height: 0 } })).toBeNull();
    expect(regionFromCropResult(null)).toBeNull();
  });
});

// ==========================================
// The page's state

const wrappers: VueWrapper[] = [];

function mountReview() {
  let review: RecipeIngestReview | undefined;
  const navigate = vi.fn();
  const Host = defineComponent({
    setup() {
      review = useRecipeIngestReview("j1", { groupSlug: "home", navigate });
      return () => h("div");
    },
  });
  wrappers.push(mount(Host));
  return { review: review!, navigate };
}

async function loaded() {
  const mounted = mountReview();
  await mounted.review.load();
  await flushPromises();
  return mounted;
}

const conflictError = apiError(409, { code: "version_conflict", current: 7 });
const busyError = apiError(409, { code: "busy" });
const region: RereadRequest = { page: 0, x: 0.1, y: 0.5, width: 0.8, height: 0.1, target: { field: "steps", ref: "s2" } };
const otherRegion: RereadRequest = { ...region, y: 0.2, target: { field: "name", ref: null } };

describe("useRecipeIngestReview", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
    resetRecipeIngestCounts();
    api.getJob.mockResolvedValue(ok(job()));
    api.getBatch.mockResolvedValue(ok({ id: "b1", source: "app", jobs: [batchJob("j1", 0, "ready", 1, 1), batchJob("j2", 1, "ready")] }));
    api.getJobState.mockResolvedValue(ok(state()));
    api.updateJob.mockImplementation((_id: string, payload: { draftVersion: number }) =>
      Promise.resolve(ok({ draftVersion: payload.draftVersion + 1, flags: [blankFlag, unsureFlag], errorCount: 1, warningCount: 1 })),
    );
    api.getCounts.mockResolvedValue(ok({ ready: 1 }));
  });

  afterEach(() => {
    wrappers.forEach(wrapper => wrapper.unmount());
    wrappers.length = 0;
    vi.useRealTimers();
  });

  test("loads the card, its flags and its place in the batch", async () => {
    const { review } = await loaded();

    expect(review.draft.value.name).toBe("Banana Mug Cake");
    expect(review.toCheck.value).toBe(2);
    expect(review.openErrors.value.map(item => item.flag.id)).toEqual(["blank:steps:s2"]);
    expect(review.firstErrorAnchor.value).toBe("ingest-flag-blank-steps-s2");
    expect(review.position.value).toEqual({ number: 1, total: 2, previous: null, next: "j2" });
    expect(review.readOnly.value).toBe(false);
  });

  test("edits save 1.5 s after the last change, carrying the draft version", async () => {
    const { review } = await loaded();

    review.draft.value.name = "Banana Mug Cake for One";
    await nextTick();
    await vi.advanceTimersByTimeAsync(1000);
    review.draft.value.description = "Ready in minutes";
    await nextTick();
    await vi.advanceTimersByTimeAsync(1499);
    expect(api.updateJob).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(1);
    await flushPromises();
    expect(api.updateJob).toHaveBeenCalledOnce();
    const [id, payload] = api.updateJob.mock.calls[0]!;
    expect(id).toBe("j1");
    expect(payload).toMatchObject({
      draftVersion: 3,
      draft: { name: "Banana Mug Cake for One", description: "Ready in minutes" },
      flagResolutions: {},
      resolvedProposalIds: [],
      clearError: false,
    });
    expect(review.saveState.value).toBe("saved");
    expect(review.draftVersion.value).toBe(4);
    expect(review.isDirty.value).toBe(false);

    // the next save carries the version the last one returned
    review.draft.value.name = "Banana Mug Cake";
    await nextTick();
    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();
    expect(api.updateJob.mock.calls[1]![1].draftVersion).toBe(4);
  });

  test("a stale version opens the reload dialog without a toast, and Reload this card loads the stored card", async () => {
    api.updateJob.mockResolvedValueOnce(conflictError);
    const { review } = await loaded();

    review.draft.value.name = "Mine";
    await nextTick();
    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();

    expect(review.conflict.value).toBe(true);
    expect(review.readOnly.value).toBe(true);
    expect(toast.error).not.toHaveBeenCalled();
    expect(toast.info).not.toHaveBeenCalled();

    // nothing more is sent while in conflict
    review.draft.value.name = "Mine, again";
    await nextTick();
    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();
    expect(api.updateJob).toHaveBeenCalledOnce();

    api.getJob.mockResolvedValueOnce(ok(job({ draftVersion: 7, draft: bananaDraft({ name: "Theirs" }) })));
    await review.reload();
    await flushPromises();

    expect(review.conflict.value).toBe(false);
    expect(review.draft.value.name).toBe("Theirs");
    expect(review.draftVersion.value).toBe(7);
    expect(review.isDirty.value).toBe(false);
  });

  test("one-tap fixes show at once and save with the resolutions", async () => {
    const { review } = await loaded();

    review.fillFlagBlank(blankFlag, "2");
    review.resolveFlag(unsureFlag, "dismissed");
    await nextTick();

    expect(review.draft.value.steps[1]!.text).toBe("Microwave on high for 2 minutes.");
    expect(review.toCheck.value).toBe(0);
    expect(review.openErrors.value).toEqual([]);
    expect(review.needsALook.value.map(item => item.state)).toEqual(["resolved", "fixed"]);

    api.updateJob.mockResolvedValueOnce(ok({ draftVersion: 4, flags: [{ ...unsureFlag, resolution: "dismissed" }], errorCount: 0, warningCount: 0 }));
    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();

    expect(api.updateJob.mock.calls[0]![1]).toMatchObject({
      draftVersion: 3,
      flagResolutions: { "unsure:ingredients:i2": "dismissed" },
    });
    // the server no longer raises the blank: it stays in the list as fixed
    expect(review.needsALook.value.map(item => [item.flag.id, item.state])).toEqual([
      ["unsure:ingredients:i2", "resolved"],
      ["blank:steps:s2", "fixed"],
    ]);
  });

  test("an alternative chip applies the other reading", async () => {
    const { review } = await loaded();

    review.applyFlagAlternative(unsureFlag, "1/2");
    await nextTick();

    expect(review.draft.value.ingredients[1]).toMatchObject({ quantity: 0.5, display: "1/2 teaspoon salt" });
    expect(review.needsALook.value.find(item => item.flag.id === unsureFlag.id)!.state).toBe("fixed");
  });

  test("a busy job queues the re-read, which is sent once the job is idle", async () => {
    api.reread.mockResolvedValueOnce(busyError);
    const { review } = await loaded();

    await review.requestReread(region);
    expect(api.reread).toHaveBeenCalledOnce();
    expect(review.rereadQueue.value).toEqual([region]);
    expect(toast.info).toHaveBeenCalledWith("This card is being read. Your re-read starts when it's done.");
    expect(toast.error).not.toHaveBeenCalled();

    // still busy: the queue waits
    api.getJobState.mockResolvedValueOnce(ok(state({ task: { kind: "reread", state: "running" } })));
    await vi.advanceTimersByTimeAsync(2000);
    await flushPromises();
    expect(api.reread).toHaveBeenCalledOnce();

    // idle: the queued re-read goes
    api.reread.mockResolvedValueOnce(ok(state({ task: { kind: "reread", state: "queued" } })));
    api.getJobState.mockResolvedValueOnce(ok(state()));
    await vi.advanceTimersByTimeAsync(2000);
    await flushPromises();
    expect(api.reread).toHaveBeenCalledTimes(2);
    expect(api.reread.mock.calls[1]).toEqual(["j1", region]);
    expect(review.rereadQueue.value).toEqual([]);
    expect(review.task.value).toEqual({ kind: "reread", state: "queued" });
  });

  test("re-reads asked for while a task runs wait in line and go one at a time", async () => {
    api.getJob.mockResolvedValue(ok(job({ task: { kind: "reread", state: "running" } })));
    const { review } = await loaded();

    await review.requestReread(region);
    await review.requestReread(otherRegion);
    expect(api.reread).not.toHaveBeenCalled();
    expect(review.rereadQueue.value).toEqual([region, otherRegion]);
    expect(toast.info).toHaveBeenCalledWith("Re-read queued");

    api.reread.mockResolvedValue(ok(state({ task: { kind: "reread", state: "queued" } })));
    api.getJobState.mockResolvedValueOnce(ok(state()));
    await vi.advanceTimersByTimeAsync(2000);
    await flushPromises();
    expect(api.reread.mock.calls.map(call => call[1])).toEqual([region]);

    api.getJobState.mockResolvedValueOnce(ok(state({ task: { kind: "reread", state: "running" } })));
    await vi.advanceTimersByTimeAsync(2000);
    await flushPromises();
    expect(api.reread).toHaveBeenCalledOnce();

    api.getJobState.mockResolvedValueOnce(ok(state({ proposalIds: ["p1"] })));
    api.getJob.mockResolvedValueOnce(ok(job({
      proposals: [{ id: "p1", kind: "region", target: { field: "steps", ref: "s2" }, text: "Microwave on high for 2 minutes.", readable: true }],
    })));
    await vi.advanceTimersByTimeAsync(2000);
    await flushPromises();
    expect(api.reread.mock.calls.map(call => call[1])).toEqual([region, otherRegion]);
    expect(review.rereadQueue.value).toEqual([]);
    // the first re-read's result landed inside its line's item
    expect(review.needsALook.value.find(item => item.flag.id === blankFlag.id)!.proposals.map(p => p.id)).toEqual(["p1"]);
  });

  test("the editor is read-only while the card is read again, and takes the new draft when it's done", async () => {
    api.getJob.mockResolvedValueOnce(ok(job({ task: { kind: "extract", state: "running", progressKey: "recipe-ingest.progress.reading-card" } })));
    const { review } = await loaded();
    expect(review.readOnly.value).toBe(true);

    review.fillFlagBlank(blankFlag, "2");
    expect(review.draft.value.steps[1]!.text).toContain("[blank]");

    api.getJobState.mockResolvedValueOnce(ok(state({ draftVersion: 4 })));
    api.getJob.mockResolvedValueOnce(ok(job({ draftVersion: 4, draft: bananaDraft({ name: "Banana Mug Cake (read again)" }), flags: [] })));
    await vi.advanceTimersByTimeAsync(2000);
    await flushPromises();

    expect(review.readOnly.value).toBe(false);
    expect(review.draft.value.name).toBe("Banana Mug Cake (read again)");
    expect(review.toCheck.value).toBe(0);
    expect(api.updateJob).not.toHaveBeenCalled();
  });

  test("polling stops once the job is idle", async () => {
    api.getJob.mockResolvedValueOnce(ok(job({ task: { kind: "reread", state: "running" } })));
    await loaded();

    await vi.advanceTimersByTimeAsync(2000);
    await flushPromises();
    expect(api.getJobState).toHaveBeenCalledOnce();

    await vi.advanceTimersByTimeAsync(6000);
    await flushPromises();
    expect(api.getJobState).toHaveBeenCalledOnce();
  });

  test("Commit & next waits for the pending save, sends its version and opens the next card", async () => {
    api.getJob.mockResolvedValueOnce(ok(job({ flags: [] })));
    api.commit.mockResolvedValueOnce(ok({ recipeId: "r1", slug: "banana-mug-cake", nextJobId: "j2", warnings: [] }));
    const { review, navigate } = await loaded();

    review.draft.value.name = "Banana Mug Cake";
    review.draft.value.description = "Edited a moment ago";
    await nextTick();
    expect(await review.commit()).toBe("committed");

    expect(api.updateJob).toHaveBeenCalledOnce();
    expect(api.commit).toHaveBeenCalledExactlyOnceWith("j1", { draftVersion: 4 });
    expect(api.updateJob.mock.invocationCallOrder[0]).toBeLessThan(api.commit.mock.invocationCallOrder[0]!);
    expect(navigate).toHaveBeenCalledExactlyOnceWith("/g/home/recipes/cards/j2");
    expect(toast.success).toHaveBeenCalledWith("Added Banana Mug Cake");
    expect(api.getCounts).toHaveBeenCalled();
  });

  test("after the batch's last card, the queue opens on the batch, which sums it up: no summary toast", async () => {
    api.getJob.mockResolvedValueOnce(ok(job({ flags: [] })));
    api.commit.mockResolvedValueOnce(ok({ recipeId: "r1", slug: "banana-mug-cake", nextJobId: null, warnings: [] }));
    api.getBatch.mockResolvedValue(ok({
      id: "b1",
      source: "app",
      jobs: [batchJob("j1", 0, "committed"), batchJob("j2", 1, "committed"), batchJob("j3", 2, "failed")],
    }));
    const { review, navigate } = await loaded();

    expect(await review.commit()).toBe("committed");

    expect(navigate).toHaveBeenCalledExactlyOnceWith("/g/home/recipes/cards?batch=b1");
    expect(toast.success).toHaveBeenCalledExactlyOnceWith("Added Banana Mug Cake");
  });

  test("errors block commit: nothing is sent, and the page is told to show them", async () => {
    const { review } = await loaded();

    expect(await review.commit()).toBe("fix");
    expect(api.commit).not.toHaveBeenCalled();

    review.resolveFlag(blankFlag, "kept");
    api.commit.mockResolvedValueOnce(ok({ recipeId: "r1", slug: "banana-mug-cake", nextJobId: "j2" }));
    expect(await review.commit()).toBe("committed");
    expect(api.updateJob.mock.calls[0]![1].flagResolutions).toEqual({ "blank:steps:s2": "kept" });
  });

  test("errors the server still holds against the commit open again", async () => {
    api.getJob.mockResolvedValueOnce(ok(job({ flags: [blankFlag] })));
    const { review, navigate } = await loaded();
    review.fillFlagBlank(blankFlag, "2");
    api.updateJob.mockResolvedValueOnce(ok({ draftVersion: 4, flags: [], errorCount: 0 }));
    api.commit.mockResolvedValueOnce(apiError(422, { code: "unresolved_flags", flags: [blankFlag] }));

    expect(await review.commit()).toBe("fix");

    expect(review.openErrors.value.map(item => item.flag.id)).toEqual(["blank:steps:s2"]);
    expect(navigate).not.toHaveBeenCalled();
    expect(toast.error).not.toHaveBeenCalled();
  });

  test("a second tap while the first commit runs just shows the card as it is now", async () => {
    api.getJob.mockResolvedValueOnce(ok(job({ flags: [] })));
    api.commit.mockResolvedValueOnce(apiError(409, { code: "committing" }));
    api.getJob.mockResolvedValueOnce(ok(job({ flags: [], status: "committing" })));
    const { review, navigate } = await loaded();

    expect(await review.commit()).toBe("failed");

    expect(review.job.value?.status).toBe("committing");
    expect(toast.error).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
  });

  test("a draft that doesn't make a recipe says why", async () => {
    api.getJob.mockResolvedValueOnce(ok(job({ flags: [] })));
    api.commit.mockResolvedValueOnce(apiError(422, { code: "commit_invalid" }));
    api.getJob.mockResolvedValueOnce(ok(job({ flags: [], error: { code: "commit_invalid", params: {} } })));
    const { review } = await loaded();

    expect(await review.commit()).toBe("failed");

    expect(toast.error).toHaveBeenCalledWith("This recipe couldn't be added. Check the card's fields and try again.");
    expect(review.job.value?.error?.code).toBe("commit_invalid");
  });

  test("Skip opens the next ready card, keeping this one for later", async () => {
    const { review, navigate } = await loaded();
    await review.skip();
    expect(navigate).toHaveBeenCalledExactlyOnceWith("/g/home/recipes/cards/j2");
  });

  test("an error with a message was already toasted; one without gets its translated text", async () => {
    api.rotatePage.mockResolvedValueOnce(apiError(503, { code: "paused_for_restore", message: "Paused" }));
    api.reextract.mockResolvedValueOnce(apiError(409, { code: "busy" }));
    const { review } = await loaded();

    await review.rotate(0);
    expect(toast.error).not.toHaveBeenCalled();
    expect(toast.info).not.toHaveBeenCalled();

    await review.reextract();
    expect(toast.info).toHaveBeenCalledWith("This card is being read. Try again when it's done.");
  });

  test("a refresh that already sees this page's save in flight raises no conflict (§6.6)", async () => {
    api.getJob.mockResolvedValueOnce(ok(job({ flags: [], task: { kind: "reread", state: "running" } })));
    const { review } = await loaded();
    review.draft.value.steps[0]!.text = "Mix well.";
    await nextTick();

    // the autosave goes out and is slow to answer
    let answerSave: (value: unknown) => void = () => {};
    api.updateJob.mockImplementation(() => new Promise((resolve) => {
      answerSave = resolve;
    }));
    await vi.advanceTimersByTimeAsync(1500);
    expect(api.updateJob).toHaveBeenCalledOnce();

    // the re-read lands: the poll sees its proposal, and the GET already sees the saved version
    const proposal: CardProposal = { id: "p1", kind: "region", target: { field: "name", ref: null }, text: "Banana Cake", readable: true };
    api.getJobState.mockResolvedValue(ok(state({ draftVersion: 4, proposalIds: ["p1"] })));
    api.getJob.mockResolvedValue(ok(job({
      draftVersion: 4,
      flags: [],
      draft: bananaDraft({ steps: [{ id: "s1", text: "Mix well." }, { id: "s2", text: "Microwave on high for [blank] minutes." }] }),
      proposals: [proposal],
    })));
    const polled = review.pollState();
    await flushPromises();
    answerSave(ok({ draftVersion: 4, flags: [], errorCount: 0, warningCount: 0 }));
    await polled;
    await flushPromises();

    expect(review.conflict.value).toBe(false);
    expect(review.readOnly.value).toBe(false);
    expect(review.draftVersion.value).toBe(4);
    expect(review.draft.value.steps[0]!.text).toBe("Mix well.");
    expect(review.proposals.value.map(item => item.id)).toEqual(["p1"]);
  });

  test("a read from before this page's save, answered after it, rolls nothing back", async () => {
    api.getJob.mockResolvedValueOnce(ok(job({ flags: [], task: { kind: "reread", state: "running" } })));
    api.updateJob.mockImplementation((_id: string, payload: { draftVersion: number }) =>
      Promise.resolve(ok({ draftVersion: payload.draftVersion + 1, flags: [], errorCount: 0, warningCount: 0 })),
    );
    const { review } = await loaded();
    review.draft.value.steps[0]!.text = "Mix well.";
    await nextTick();

    // the poll's GET goes out and reads the row before the save
    let answerGet: (value: unknown) => void = () => {};
    const proposal: CardProposal = { id: "p1", kind: "region", target: { field: "name", ref: null }, text: "Banana Cake", readable: true };
    api.getJobState.mockResolvedValue(ok(state({ proposalIds: ["p1"] })));
    api.getJob.mockImplementation(() => new Promise((resolve) => {
      answerGet = resolve;
    }));
    const polled = review.pollState();
    await flushPromises();

    // meanwhile the autosave fires and is answered first; the reviewer keeps typing
    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();
    expect(review.draftVersion.value).toBe(4);
    review.draft.value.steps[0]!.text = "Mix well, then rest.";
    await nextTick();

    answerGet(ok(job({ draftVersion: 3, flags: [], proposals: [proposal] })));
    await polled;
    await flushPromises();

    expect(review.conflict.value).toBe(false);
    expect(review.draftVersion.value).toBe(4);
    expect(review.job.value?.draftVersion).toBe(4);
    expect(review.draft.value.steps[0]!.text).toBe("Mix well, then rest.");
    // the read's news still shows
    expect(review.proposals.value.map(item => item.id)).toEqual(["p1"]);
    expect(review.task.value).toBeNull();

    // and the next save and the commit carry the version the save returned
    api.commit.mockResolvedValueOnce(ok({ recipeId: "r1", slug: "banana-mug-cake", nextJobId: "j2", warnings: [] }));
    expect(await review.commit()).toBe("committed");
    expect(api.updateJob.mock.calls.at(-1)![1].draftVersion).toBe(4);
    expect(api.commit).toHaveBeenCalledExactlyOnceWith("j1", { draftVersion: 5 });
  });

  test("Save as eval case says a name is taken only when it is; other refusals say why", async () => {
    const { review } = await loaded();

    api.saveEvalCase.mockResolvedValueOnce(apiError(409, { code: "eval_case_exists" }));
    expect(await review.saveEvalCase("banana-mug-cake", true)).toBe("exists");
    expect(toast.error).not.toHaveBeenCalled();

    api.saveEvalCase.mockResolvedValueOnce(apiError(409, { code: "not_exportable" }));
    expect(await review.saveEvalCase("banana-mug-cake-2", true)).toBe("failed");
    expect(toast.error).toHaveBeenCalledWith(
      "Only a card that is ready to review or added, with its photos, can be saved as an eval case.",
    );

    api.saveEvalCase.mockResolvedValueOnce(apiError(409, { code: "files_missing" }));
    expect(await review.saveEvalCase("banana-mug-cake-3", true)).toBe("failed");
    expect(toast.error).toHaveBeenLastCalledWith("The card's photos are missing. Scan it again.");
  });

  test("what commit left out (an organizer deleted since) is said with the Added toast", async () => {
    api.getJob.mockResolvedValueOnce(ok(job({ flags: [], draft: bananaDraft({ tags: [{ id: "t1", name: "Desserts" }] }) })));
    api.commit.mockResolvedValueOnce(ok({
      recipeId: "r1",
      slug: "banana-mug-cake",
      nextJobId: "j2",
      warnings: ["tag_dropped:Desserts", "something_new:x"],
    }));
    const { review, navigate } = await loaded();

    expect(await review.commit()).toBe("committed");
    expect(toast.warning).toHaveBeenCalledExactlyOnceWith(
      "The tag \"Desserts\" no longer exists, so it wasn't added.",
      "Added Banana Mug Cake",
    );
    expect(toast.success).not.toHaveBeenCalled();
    expect(navigate).toHaveBeenCalledExactlyOnceWith("/g/home/recipes/cards/j2");
  });

  test("a missing card says so", async () => {
    api.getJob.mockResolvedValueOnce(apiError(404, "Not found"));
    const { review } = mountReview();
    await review.load();
    expect(review.loadState.value).toBe("not-found");
  });
});
