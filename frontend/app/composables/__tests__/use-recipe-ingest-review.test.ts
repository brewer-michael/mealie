import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { defineComponent, h, nextTick } from "vue";
import {
  applyAlternative,
  applyProposal,
  buildNeedsALook,
  cloneDraft,
  draftsEqual,
  editFlaggedText,
  fieldText,
  fillBlank,
  findFragment,
  firstCardToReview,
  flagAlternatives,
  flagsForField,
  fixIngredient,
  formatQuantity,
  highlightSegments,
  ingredientAsText,
  mergeBlockOf,
  ingredientDisplay,
  nextBatchCard,
  nextCardInBatch,
  normalizeDraft,
  nudgeRegion,
  parsedReading,
  parseLoss,
  parseQuantity,
  regionFromCropResult,
  regionFromHint,
  rereadTargets,
  rereadTargetValue,
  sortFlags,
  suggestEvalSlug,
  carryReviewNotice,
  resetCarriedReviewNotice,
  takeCarriedReviewNotice,
  useRecipeIngestReview,
  NOTICE_MS,
  type RecipeIngestReview,
} from "../use-recipe-ingest-review";
import {
  resetRecipeIngestCounts,
  resetRecipeIngestReviewState,
  runRecipeIngestLogoutTasks,
  setRecipeIngestSessionCheck,
  takeRecipeIngestCommitNotice,
} from "../use-recipe-ingest";
import { clearComposableCaches } from "../use-clear-composable-caches";
import type {
  CardDraft,
  CardFlag,
  CardProposal,
  RecipeIngestionBatchJob,
  RecipeIngestionJobError,
  RecipeIngestionJobOut,
  RecipeIngestionJobState,
  RecipeIngestionJobSummary,
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

  function summary(id: string, batchId: string, position: number, createdAt: string, errorCount = 0): RecipeIngestionJobSummary {
    return { id, batchId, position, status: "ready", source: "app", pageCount: 1, draftVersion: 1, createdAt, errorCount, warningCount: 0 };
  }

  test("after a batch's last card, the review goes on to the batch whose ready cards waited longest", () => {
    const ready = [
      summary("n1", "newer", 0, "2026-10-04T09:00:00Z"),
      summary("o2", "older", 1, "2026-10-03T08:01:00Z", 1),
      summary("o1", "older", 0, "2026-10-03T08:00:00Z"),
      summary("s1", "same", 1, "2026-10-01T08:00:00Z"),
    ];
    // where that batch's review starts: its first card with something to check
    expect(nextBatchCard(ready, "same", "s0")).toBe("o2");
    // never this batch, nor this card, nor a card that isn't ready
    expect(nextBatchCard([summary("s1", "same", 1, "2026-10-01T08:00:00Z")], "same", "s0")).toBeNull();
    expect(nextBatchCard([{ ...summary("x", "other", 0, "2026-10-01T08:00:00Z"), status: "processing" }], "same", "s0")).toBeNull();
    expect(nextBatchCard([], "same", "s0")).toBeNull();
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

  test("a flag on a note is about the note its id names, wherever that note is now", () => {
    // the server keys notes by their ids: two notes with a blank raise blank:notes:n1 and blank:notes:n2
    const draft = normalizeDraft(bananaDraft({
      notes: [{ id: "n1", title: "", text: "Bake [blank] min if doubled" }, { id: "n2", title: "", text: "Cool [blank] min" }],
    }));
    const second = flag({ id: "blank:notes:n2", field: "notes", ref: "n2" });
    const first = flag({ id: "blank:notes:n1", field: "notes", ref: "n1" });

    expect(fieldText(draft, "notes", "n2")).toBe("Cool [blank] min");
    const { items } = buildNeedsALook([second, first], [second, first], new Set(), draft, []);
    expect(items.map(item => [item.flag.id, item.line, item.text])).toEqual([
      ["blank:notes:n1", 0, "Bake [blank] min if doubled"],
      ["blank:notes:n2", 1, "Cool [blank] min"],
    ]);

    // moved up, the second note keeps its flag: the item follows it, in its new place
    draft.notes.reverse();
    expect(fieldText(draft, "notes", "n2")).toBe("Cool [blank] min");
    expect(buildNeedsALook([second, first], [second, first], new Set(), draft, []).items.map(item => [item.flag.id, item.line]))
      .toEqual([["blank:notes:n2", 0], ["blank:notes:n1", 1]]);

    expect(editFlaggedText(draft, second, "5", "fill")).toBe(true);
    expect(draft.notes.map(note => note.text)).toEqual(["Cool 5 min", "Bake [blank] min if doubled"]);
    expect(editFlaggedText(draft, first, "20", "fill")).toBe(true);
    expect(draft.notes.map(note => note.text)).toEqual(["Cool 5 min", "Bake 20 min if doubled"]);
    expect(draft.notes.map(note => note.id)).toEqual(["n2", "n1"]);
  });

  test("a marker in a note's title is filled there", () => {
    const draft = normalizeDraft(bananaDraft({
      notes: [{ id: "n1", title: "", text: "Grandma Jo's, 1962" }, { id: "n2", title: "From [blank]", text: "Can double for a 9x13 pan" }],
    }));
    const titleFlag = flag({ id: "blank:notes:n2", field: "notes", ref: "n2" });

    expect(fieldText(draft, "notes", "n2")).toBe("From [blank]\nCan double for a 9x13 pan");
    expect(editFlaggedText(draft, titleFlag, "Aunt May", "fill")).toBe(true);
    expect(draft.notes[1]).toMatchObject({ id: "n2", title: "From Aunt May", text: "Can double for a 9x13 pan" });
    expect(draft.notes[0]!.text).toBe("Grandma Jo's, 1962");
    // a note that's gone points at nothing, and a position is no note's id
    expect(editFlaggedText(draft, flag({ id: "blank:notes:gone", field: "notes", ref: "gone" }), "x", "fill")).toBe(false);
    expect(editFlaggedText(draft, flag({ id: "blank:notes:0", field: "notes", ref: "0" }), "x", "fill")).toBe(false);
    expect(fieldText(draft, "notes", "0")).toBe("");
  });

  test("notes keep their ids, and a note without one gets one", () => {
    const draft = normalizeDraft(bananaDraft({ notes: [{ id: "n1", title: "From", text: "Grandma Jo" }, { title: "", text: "Doubles well" }] }));
    expect(draft.notes[0]).toEqual({ id: "n1", title: "From", text: "Grandma Jo" });
    expect(draft.notes[1]!.id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    // the ids go out with the draft, so the server keys the note's flags (and their resolutions) to them
    expect(cloneDraft(draft).notes!.map(note => note.id)).toEqual(["n1", draft.notes[1]!.id]);
  });

  test("a re-read of a note goes into that note; one for a new note adds it, with an id", () => {
    const draft = normalizeDraft(bananaDraft({ notes: [{ id: "n1", title: "Tip", text: "Use a big mug" }, { id: "n2", title: "", text: "Doubles [illegible]" }] }));
    applyProposal(draft, { id: "p1", kind: "region", target: { field: "notes", ref: "n2" }, text: "Doubles well", readable: true }, "replace");
    expect(draft.notes.map(note => [note.id, note.title, note.text])).toEqual([["n1", "Tip", "Use a big mug"], ["n2", "", "Doubles well"]]);
    applyProposal(draft, { id: "p2", kind: "region", target: { field: "notes", ref: "n1" }, text: "or a ramekin", readable: true }, "append");
    expect(draft.notes[0]!.text).toBe("Use a big mug or a ramekin");
    applyProposal(draft, { id: "p3", kind: "region", target: { field: "notes", ref: null }, text: "Freezes well", readable: true }, "replace");
    expect(draft.notes).toHaveLength(3);
    expect(draft.notes[2]).toMatchObject({ title: "", text: "Freezes well" });
    expect(draft.notes[2]!.id).toBeTruthy();
    expect(new Set(draft.notes.map(note => note.id)).size).toBe(3);
  });
});

describe("flag positions", () => {
  // the server's params.start/end: where the flagged part is in the text the flag was computed on (PL-12)
  const microwave = "Add 1/2 c. milk. Microwave 2 minutes.";
  const invented = flag({
    id: "blank:steps:s2",
    source: "cross_read",
    params: { value: "2", start: 27, end: 28 },
  });

  test("a fill and the highlight hit the number the flag means, not the 2 of 1/2", () => {
    const draft = normalizeDraft(bananaDraft({ steps: [{ id: "s1", text: "Mix." }, { id: "s2", text: microwave }] }));
    const { items } = buildNeedsALook([invented], [invented], new Set(), draft, []);
    expect(items[0]!.span).toEqual({ start: 27, end: 28 });
    expect(highlightSegments(items[0]!.text, items[0]!.fragment, items[0]!.span)).toEqual([
      { text: "Add 1/2 c. milk. Microwave ", mark: false },
      { text: "2", mark: true },
      { text: " minutes.", mark: false },
    ]);

    expect(editFlaggedText(draft, invented, "3", "fill")).toBe(true);
    expect(draft.steps[1]!.text).toBe("Add 1/2 c. milk. Microwave 3 minutes.");
  });

  test("without a position, the first whole number matches: never a 2 inside 1/2 or 12", () => {
    const unplaced = flag({ ...invented, params: { value: "2" } });
    expect(fillBlank(microwave, "3", "2")).toBe("Add 1/2 c. milk. Microwave 3 minutes.");
    expect(applyAlternative(microwave, unplaced, "3")).toBe("Add 1/2 c. milk. Microwave 3 minutes.");
    expect(highlightSegments(microwave, "2").filter(segment => segment.mark)).toHaveLength(1);
    expect(findFragment("Beat 12 eggs", "2")).toBeNull();
    expect(findFragment("1/2 c. milk", "1")).toBeNull();
    expect(findFragment("1 can (10 3/4 oz.) soup", "1")).toEqual({ start: 0, end: 1 });
    expect(findFragment("Bake 1½ hours, then 1 more", "1")).toEqual({ start: 20, end: 21 });
    expect(findFragment("1.5 c. flour, 5 eggs", "5")).toEqual({ start: 14, end: 15 });
    // a word matches as a whole word; text that isn't a number or word (a marker) anywhere
    expect(findFragment("1 Tbsp. butter, 1 T. sugar", "T")).toEqual({ start: 18, end: 19 });
    expect(findFragment("a[blank]b", "[blank]")).toEqual({ start: 1, end: 8 });
    // shorthand: a number right before a unit's letters is still that number
    expect(findFragment("2T. butter", "2")).toEqual({ start: 0, end: 1 });
  });

  test("a position the text no longer holds falls back to the first whole match", () => {
    // edited since: the step is shorter, or something else is at that place now
    expect(findFragment("Microwave 2 minutes.", "2", { start: 27, end: 28 })).toEqual({ start: 10, end: 11 });
    expect(findFragment(microwave, "2", { start: 4, end: 5 })).toEqual({ start: 27, end: 28 });
    expect(findFragment("Add 1/2 c. milk.", "2", { start: 27, end: 28 })).toBeNull();
    // the server's position is trusted where the text holds the fragment
    expect(findFragment("1/2 c. milk", "1/2", { start: 0, end: 3 })).toEqual({ start: 0, end: 3 });
    // a longer reading whatever its case, as the server matched it; a unit letter only as written
    expect(findFragment("1/4 T. salt", "1/4 t.", { start: 0, end: 6 })).toEqual({ start: 0, end: 6 });
    expect(findFragment("1 t. salt, 1 T. sugar", "T", { start: 2, end: 3 })).toEqual({ start: 13, end: 14 });
  });

  test("a check on the second of two equal amounts highlights the second", () => {
    const draft = normalizeDraft(bananaDraft({
      ingredients: [{
        referenceId: "i1",
        originalText: "1 c. sugar, 1 c. flour",
        quantity: 1,
        unit: { id: "u-cup", name: "cup" },
        food: { id: null, name: "sugar flour" },
        note: "",
      }],
    }));
    const check = flag({
      id: "check_parse:ingredients:i1",
      kind: "check_parse",
      severity: "warning",
      source: "parser",
      field: "ingredients",
      ref: "i1",
      params: { value: "1", start: 12, end: 13 },
    });
    const { items } = buildNeedsALook([check], [check], new Set(), draft, []);
    expect(items[0]!.span).toEqual({ start: 12, end: 13 });
    expect(highlightSegments(items[0]!.text, items[0]!.fragment, items[0]!.span)).toEqual([
      { text: "1 c. sugar, ", mark: false },
      { text: "1", mark: true },
      { text: " c. flour", mark: false },
    ]);
  });

  test("a position in a note's text is shifted past its title in the item's text", () => {
    const draft = normalizeDraft(bananaDraft({ notes: [{ id: "n1", title: "Doubled 2x", text: "Bake 2 hours at 2 racks" }] }));
    const noteFlag = flag({
      id: "not_on_card:notes:n1",
      kind: "not_on_card",
      severity: "warning",
      source: "validator",
      field: "notes",
      ref: "n1",
      params: { value: "2", start: 16, end: 17 },
    });
    const { items } = buildNeedsALook([noteFlag], [noteFlag], new Set(), draft, []);
    expect(items[0]!.text).toBe("Doubled 2x\nBake 2 hours at 2 racks");
    expect(items[0]!.span).toEqual({ start: 27, end: 28 });

    const fill = flag({ ...noteFlag, kind: "blank", severity: "error", source: "cross_read" });
    expect(editFlaggedText(draft, fill, "3", "fill")).toBe(true);
    expect(draft.notes[0]).toMatchObject({ title: "Doubled 2x", text: "Bake 2 hours at 3 racks" });
  });

  test("a fix on a parsed line changes the part that holds the flagged spot", () => {
    const eggs = {
      referenceId: "i1",
      originalText: "2 eggs, beaten with 2 T. water",
      quantity: 2,
      unit: null,
      food: { id: "f-egg", name: "eggs" },
      note: "beaten with 2 T. water",
    };
    const first = flag({ id: "blank:ingredients:i1", source: "cross_read", field: "ingredients", ref: "i1", params: { value: "2", start: 0, end: 1 } });
    const second = flag({ ...first, params: { value: "2", start: 20, end: 21 } });

    expect(fixIngredient(eggs, first, "3", "fill")).toMatchObject({ quantity: 3, note: "beaten with 2 T. water" });
    expect(fixIngredient(eggs, second, "3", "fill")).toMatchObject({ quantity: 2, note: "beaten with 3 T. water" });

    // the 1 of the amount, not the 1 in the note's "10 3/4"
    const soup = {
      referenceId: "i2",
      originalText: "1 can (10 3/4 oz.) soup",
      quantity: 1,
      unit: { id: null, name: "can" },
      food: { id: null, name: "soup" },
      note: "(10 3/4 oz.)",
    };
    const amount = flag({ ...first, ref: "i2", params: { value: "1" } });
    expect(fixIngredient(soup, amount, "2", "fill")).toMatchObject({ quantity: 2, note: "(10 3/4 oz.)" });
    const inNote = flag({ ...first, ref: "i2", params: { value: "10 3/4", start: 7, end: 13 } });
    expect(fixIngredient(soup, inNote, "10 1/2", "fill")).toMatchObject({ quantity: 1, note: "(10 1/2 oz.)" });
  });
});

describe("check this ingredient", () => {
  const check = (params: Record<string, unknown>) => flag({
    id: "check_parse:ingredients:i1",
    kind: "check_parse",
    severity: "warning",
    source: "parser",
    field: "ingredients",
    ref: "i1",
    params,
  });
  const line = (originalText: string, quantity: number | null, unit: string | null, food: string | null, note: string) => ({
    referenceId: "i1",
    originalText,
    quantity,
    unit: unit ? { id: null, name: unit } : null,
    food: food ? { id: null, name: food } : null,
    note,
  });

  test("the parser's reading reads amount, unit and food, then the note", () => {
    expect(parsedReading(line("2-3 c. flour", 2, "cup", "flour", "to 3"))).toBe("2 cup flour, to 3");
    expect(parsedReading(line("1 can (10 3/4 oz.) soup", 1, "can", "soup", "(10 3/4 oz.)"))).toBe("1 can soup, (10 3/4 oz.)");
    expect(parsedReading(line("1 egg", 1, null, "egg", ""))).toBe("1 egg");
  });

  test("what the fields lost is named from the flag's value, and whether the note keeps it", () => {
    expect(parseLoss(check({ value: "2-3", start: 0, end: 3 }), line("2-3 c. flour", 2, "cup", "flour", "to 3")))
      .toEqual({ kind: "range", value: "2-3", end: "3", kept: true });
    expect(parseLoss(check({ value: "1 to 2" }), line("1 to 2 c. water", 1, "cup", "water", "to 2")))
      .toEqual({ kind: "range", value: "1 to 2", end: "2", kept: true });
    // a draft parsed before notes kept what the fields lose
    expect(parseLoss(check({ value: "2-3" }), line("2-3 c. flour", 2, "cup", "flour", "")))
      .toEqual({ kind: "range", value: "2-3", end: "3", kept: false });
    expect(parseLoss(check({ value: "10 3/4" }), line("1 can (10 3/4 oz.) soup", 1, "can", "soup", "(10 3/4 oz.)")))
      .toEqual({ kind: "amount", value: "10 3/4", kept: true });
    expect(parseLoss(check({ value: "3" }), line("2 or 3 eggs", 2, null, "eggs", "or 3 eggs")))
      .toEqual({ kind: "amount", value: "3", kept: true });
    expect(parseLoss(check({ value: "1" }), line("1 c. sugar, 1 c. flour", 1, "cup", "sugar flour", "")))
      .toEqual({ kind: "amount", value: "1", kept: false });
    expect(parseLoss(check({ value: "scant" }), line("1 scant cup sugar", 1, "cup scant", "sugar", "")))
      .toEqual({ kind: "in-name", value: "scant" });
    // a low-confidence parse says nothing more specific; other flags never do
    expect(parseLoss(check({ confidence: 60 }), line("1 sq chocolate", 1, null, "sq chocolate", ""))).toBeNull();
    expect(parseLoss(unsureFlag, line("1/4 t. salt", 0.25, "teaspoon", "salt", ""))).toBeNull();
  });

  test("Keep as text keeps the card's line as written, with no amount, unit or food", () => {
    expect(ingredientAsText({ ...line("2-3 c. flour", 2, "cup", "flour", "to 3"), title: "Crust" })).toMatchObject({
      referenceId: "i1",
      title: "Crust",
      quantity: null,
      unit: null,
      food: null,
      note: "2-3 c. flour",
      display: "2-3 c. flour",
    });
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
    expect(options.find(option => option.kind === "new-note")!.target).toEqual({ field: "notes", ref: null });
    expect(rereadTargetValue(options, "steps", "s2")).toBe("steps:s2");
    expect(rereadTargetValue(options, "prep_time", null)).toBe("prepTime");
    expect(rereadTargetValue(options, "ingredients", "gone")).toBe("ingredients:i1");
    // an empty section's flag has no line: its re-read adds one
    expect(rereadTargetValue(options, "steps", null)).toBe("steps:new");
    expect(rereadTargetValue(rereadTargets(normalizeDraft({ name: "Banana Mug Cake" })), "ingredients", "gone")).toBe("ingredients:new");
  });

  test("each note is a re-read target by its id, named by its title or its first words", () => {
    const options = rereadTargets(normalizeDraft(bananaDraft({
      notes: [{ id: "n1", title: "From", text: "Grandma Jo" }, { id: "n2", title: "", text: "Doubles well in a 9x13 pan, baked a little longer" }],
    })));
    expect(options.filter(option => option.kind === "note").map(option => [option.value, option.target, option.text])).toEqual([
      ["notes:n1", { field: "notes", ref: "n1" }, "From"],
      ["notes:n2", { field: "notes", ref: "n2" }, "Doubles well in a 9x13 pan, baked a…"],
    ]);
    // a note flag's Re-read is aimed at its note, not at a new one
    expect(rereadTargetValue(options, "notes", "n2")).toBe("notes:n2");
    expect(rereadTargetValue(options, "notes", null)).toBe("notes:new");
  });

  test("a card can become the back of the previous card only while both can be read again together", () => {
    const card = { pageCount: 1 };
    const previous = (overrides: Partial<RecipeIngestionJobOut> = {}) => ({ ...job({ id: "j0", pageCount: 1, permissions: { canMerge: true } }), ...overrides });

    expect(mergeBlockOf(card, previous(), 4)).toBeNull();
    // still finding out which card is before it
    expect(mergeBlockOf(card, undefined, 4)).toBe("checking");
    expect(mergeBlockOf(card, null, 4)).toBe("no-previous");
    expect(mergeBlockOf(card, previous({ status: "committed" }), 4)).toBe("previous-added");
    expect(mergeBlockOf(card, previous({ status: "committing" }), 4)).toBe("previous-added");
    expect(mergeBlockOf(card, previous({ status: "processing" }), 4)).toBe("previous-busy");
    expect(mergeBlockOf(card, previous({ task: { kind: "reread", state: "running" } }), 4)).toBe("previous-busy");
    expect(mergeBlockOf({ pageCount: 2 }, previous({ pageCount: 3 }), 4)).toBe("too-many-pages");
    expect(mergeBlockOf({ pageCount: 2 }, previous({ pageCount: 2 }), 4)).toBeNull();
    // a failed card can take a back too, but only one the user may change
    expect(mergeBlockOf(card, previous({ status: "failed" }), 4)).toBeNull();
    expect(mergeBlockOf(card, previous({ permissions: { canMerge: false } }), 4)).toBe("previous-not-allowed");
  });

  test("eval case names are made from the recipe's name", () => {
    expect(suggestEvalSlug("Grandma's Banana Mug Cake!")).toBe("grandma-s-banana-mug-cake");
    expect(suggestEvalSlug("  Crème brûlée  ")).toBe("creme-brulee");
    expect(suggestEvalSlug(null)).toBe("");
  });
});

describe("crop regions", () => {
  test("a region hint starts the selection at its height, reaching a little past it either side", () => {
    expect(regionFromHint({ x: 0.05, y: 0.2274, width: 0.9, height: 0.05 })).toEqual({ x: 0, y: 0.2274, width: 1, height: 0.05 });
    const column = regionFromHint({ x: 0.5, y: 0.1, width: 0.3, height: 0.05 });
    expect(column.x).toBeCloseTo(0.45);
    expect(column.width).toBeCloseTo(0.4);
    expect(column.y).toBe(0.1);
  });

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

  test("an arrow key moves the selection by 2% of the page, and Shift with it resizes it", () => {
    const image = { width: 1000, height: 2000 };
    const band = { left: 100, top: 1000, width: 800, height: 200 };
    expect(nudgeRegion(band, image, "ArrowRight", false)).toEqual({ ...band, left: 120 });
    expect(nudgeRegion(band, image, "ArrowUp", false)).toEqual({ ...band, top: 960 });
    expect(nudgeRegion(band, image, "ArrowRight", true)).toEqual({ ...band, width: 820 });
    expect(nudgeRegion(band, image, "ArrowLeft", true)).toEqual({ ...band, width: 780 });
    expect(nudgeRegion(band, image, "ArrowDown", true)).toEqual({ ...band, height: 240 });
    expect(nudgeRegion(band, image, "Enter", false)).toBeNull();
  });

  test("a selection moved or resized by key stays on the page and never shrinks to a sliver", () => {
    const image = { width: 1000, height: 2000 };
    expect(nudgeRegion({ left: 5, top: 0, width: 800, height: 200 }, image, "ArrowLeft", false)).toMatchObject({ left: 0 });
    expect(nudgeRegion({ left: 195, top: 0, width: 800, height: 200 }, image, "ArrowRight", false)).toMatchObject({ left: 200 });
    expect(nudgeRegion({ left: 0, top: 1850, width: 800, height: 200 }, image, "ArrowDown", false)).toMatchObject({ top: 1800 });
    expect(nudgeRegion({ left: 190, top: 0, width: 800, height: 200 }, image, "ArrowRight", true)).toMatchObject({ width: 810 });
    // twice the smallest region the server reads (4% of each side)
    expect(nudgeRegion({ left: 0, top: 0, width: 45, height: 200 }, image, "ArrowLeft", true)).toMatchObject({ width: 40 });
    expect(nudgeRegion({ left: 0, top: 0, width: 800, height: 90 }, image, "ArrowUp", true)).toMatchObject({ height: 80 });
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
    resetRecipeIngestReviewState();
    resetCarriedReviewNotice();
    api.getJob.mockResolvedValue(ok(job()));
    api.getJobs.mockResolvedValue(ok({ page: 1, per_page: -1, total: 0, total_pages: 0, items: [] }));
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

  test("the card photo is attached as the draft says, else as the household's default; public households are warned", async () => {
    api.getJob.mockResolvedValue(ok(job({ householdRecipesPublic: true, cardPhotoDefault: false, draft: bananaDraft({ useCardAsCover: false }) })));
    const { review } = await loaded();

    expect(review.draft.value.attachCardPhoto ?? null).toBeNull();
    expect(review.attachCardPhoto.value).toBe(false);
    expect(review.cardPhotoPublic.value).toBe(false);
    // reading the default doesn't change the draft
    expect(review.isDirty.value).toBe(false);

    review.attachCardPhoto.value = true;
    expect(review.draft.value.attachCardPhoto).toBe(true);
    expect(review.cardPhotoPublic.value).toBe(true);
    review.attachCardPhoto.value = false;
    review.draft.value.useCardAsCover = true;
    expect(review.cardPhotoPublic.value).toBe(true);

    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();
    expect(api.updateJob.mock.calls[0]![1].draft).toMatchObject({ attachCardPhoto: false, useCardAsCover: true });
  });

  test("a private household attaches the card photo by default and isn't warned", async () => {
    api.getJob.mockResolvedValue(ok(job({ householdRecipesPublic: false, cardPhotoDefault: true })));
    const { review } = await loaded();

    expect(review.attachCardPhoto.value).toBe(true);
    expect(review.cardPhotoPublic.value).toBe(false);
  });

  test("the cover follows the household's default until the reviewer sets it, and an edit doesn't store the default", async () => {
    api.getJob.mockResolvedValue(ok(job({
      householdRecipesPublic: true,
      cardPhotoDefault: false,
      cardCoverDefault: false,
      draft: bananaDraft({ useCardAsCover: null }),
    })));
    const { review } = await loaded();

    // a public household: no cover unless the reviewer turns it on, so nothing is public
    expect(review.draft.value.useCardAsCover ?? null).toBeNull();
    expect(review.useCardAsCover.value).toBe(false);
    expect(review.cardPhotoPublic.value).toBe(false);
    expect(review.isDirty.value).toBe(false);

    // an unrelated edit keeps "the household's default" (null) rather than turning the cover on
    review.draft.value.name = "Banana Mug Cake for Two";
    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();
    expect(api.updateJob.mock.calls[0]![1].draft.useCardAsCover ?? null).toBeNull();

    review.useCardAsCover.value = true;
    expect(review.draft.value.useCardAsCover).toBe(true);
    expect(review.cardPhotoPublic.value).toBe(true);
    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();
    expect(api.updateJob.mock.calls[1]![1].draft).toMatchObject({ useCardAsCover: true });
  });

  test("a private household (or a server that doesn't say) uses the card as the cover by default; a read-only card keeps it", async () => {
    api.getJob.mockResolvedValue(ok(job({ householdRecipesPublic: false, cardCoverDefault: true, draft: bananaDraft({ useCardAsCover: null }) })));
    const first = await loaded();
    expect(first.review.useCardAsCover.value).toBe(true);
    expect(first.review.cardPhotoPublic.value).toBe(false);

    const { useCardAsCover: _cover, ...withoutCover } = bananaDraft();
    api.getJob.mockResolvedValue(ok(job({ status: "committed", draft: withoutCover })));
    const second = await loaded();
    expect(second.review.useCardAsCover.value).toBe(true);
    second.review.useCardAsCover.value = false;
    expect(second.review.draft.value.useCardAsCover ?? null).toBeNull();
    expect(second.review.useCardAsCover.value).toBe(true);
  });

  test("Keep as text on Check this ingredient keeps the card's line, and the line's parser flags are done", async () => {
    const flour = { referenceId: "i4", originalText: "2-3 c. flour", quantity: 2, unit: { id: "u-cup", name: "cup" }, food: { id: "f-flour", name: "flour" }, note: "to 3" };
    const check = flag({ id: "check_parse:ingredients:i4", kind: "check_parse", severity: "warning", source: "parser", field: "ingredients", ref: "i4", params: { value: "2-3", start: 0, end: 3 } });
    const unit = flag({ id: "unit_unclear:ingredients:i4", kind: "unit_unclear", severity: "warning", source: "parser", field: "ingredients", ref: "i4", params: { token: "c." } });
    const draft = bananaDraft();
    api.getJob.mockResolvedValue(ok(job({ draft: { ...draft, ingredients: [...draft.ingredients!, flour] }, flags: [blankFlag, check, unit] })));
    const { review } = await loaded();
    expect(review.needsALook.value.find(item => item.flag.id === check.id)!.ingredient).toMatchObject({ referenceId: "i4", quantity: 2 });

    review.keepIngredientAsText(check);
    await nextTick();

    expect(review.draft.value.ingredients[3]).toMatchObject({ referenceId: "i4", quantity: null, unit: null, food: null, note: "2-3 c. flour", display: "2-3 c. flour" });
    expect(review.needsALook.value.map(item => [item.flag.id, item.state])).toEqual([
      ["check_parse:ingredients:i4", "fixed"],
      ["unit_unclear:ingredients:i4", "fixed"],
      ["blank:steps:s2", "open"],
    ]);

    // saved as an edit: the server no longer raises the parser's flags on the line
    api.updateJob.mockResolvedValueOnce(ok({ draftVersion: 4, flags: [blankFlag], errorCount: 1, warningCount: 0 }));
    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();
    expect(api.updateJob.mock.calls[0]![1].draft.ingredients[3]).toMatchObject({ quantity: null, note: "2-3 c. flour" });
    expect(review.needsALook.value.map(item => item.state)).toEqual(["fixed", "fixed", "open"]);

    // a flag that isn't on an ingredient has no line to keep
    review.keepIngredientAsText(blankFlag);
    expect(review.draft.value.steps[1]!.text).toBe("Microwave on high for [blank] minutes.");
  });

  test("a busy job queues the re-read, which is sent once the job is idle", async () => {
    api.reread.mockResolvedValueOnce(busyError);
    const { review } = await loaded();

    await review.requestReread(region);
    expect(api.reread).toHaveBeenCalledOnce();
    expect(review.rereadQueue.value).toEqual([region]);
    // said in the review bar, not by a toast over the header
    expect(review.notice.value).toMatchObject({ kind: "info", text: "This card is being read. Your re-read starts when it's done." });
    expect(toast.info).not.toHaveBeenCalled();
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
    expect(review.notice.value).toMatchObject({ kind: "info", text: "Re-read queued" });

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
    expect(api.getCounts).toHaveBeenCalled();
    // the next card's page says it in its review bar: upstream's toast would cover that page's header on phones
    expect(toast.success).not.toHaveBeenCalled();
    expect(takeRecipeIngestCommitNotice()).toBeNull();
    // with Undo for the card just added
    expect(takeCarriedReviewNotice("j2")).toEqual({ kind: "success", text: "Added Banana Mug Cake", detail: null, undoJobId: "j1" });
    expect(takeCarriedReviewNotice("j2")).toBeNull();
  });

  test("after the batch's last card, the queue opens on the batch, which sums it up and says what was added", async () => {
    api.getJob.mockResolvedValueOnce(ok(job({ flags: [] })));
    api.commit.mockResolvedValueOnce(ok({ recipeId: "r1", slug: "banana-mug-cake", nextJobId: null, warnings: [] }));
    api.getBatch.mockResolvedValue(ok({
      id: "b1",
      source: "app",
      jobs: [batchJob("j1", 0, "committed"), batchJob("j2", 1, "committed"), batchJob("j3", 2, "failed")],
    }));
    const { review, navigate } = await loaded();

    expect(await review.commit()).toBe("committed");

    // no other batch has a card ready
    expect(api.getJobs).toHaveBeenCalledExactlyOnceWith({ status: "ready", perPage: -1 });
    expect(navigate).toHaveBeenCalledExactlyOnceWith("/g/home/recipes/cards?batch=b1");
    // left for the queue, which says it beside its summary line: a toast would cover the page's title
    expect(toast.success).not.toHaveBeenCalled();
    // with the card just added, for the queue's Undo
    expect(takeRecipeIngestCommitNotice()).toEqual({ text: "Added Banana Mug Cake", warning: null, undoJobId: "j1" });
  });

  test("after the last ready card, while cards of the batch are still being read, the queue opens on the batch", async () => {
    api.getJob.mockResolvedValueOnce(ok(job({ flags: [] })));
    api.commit.mockResolvedValueOnce(ok({ recipeId: "r1", slug: "banana-mug-cake", nextJobId: null, warnings: [] }));
    api.getBatch.mockResolvedValue(ok({
      id: "b1",
      source: "app",
      jobs: [batchJob("j1", 0, "committed"), batchJob("j2", 1, "processing"), batchJob("j3", 2, "processing")],
    }));
    // another batch has a card ready, but this one isn't done
    api.getJobs.mockResolvedValue(ok({ items: [{ id: "x1", batchId: "b2", position: 0, status: "ready", source: "api", pageCount: 1 }] }));
    const { review, navigate } = await loaded();

    expect(await review.commit()).toBe("committed");

    expect(navigate).toHaveBeenCalledExactlyOnceWith("/g/home/recipes/cards?batch=b1");
    expect(api.getJobs).not.toHaveBeenCalled();
    expect(takeRecipeIngestCommitNotice()).toEqual({ text: "Added Banana Mug Cake · 2 cards are still being read", warning: null, undoJobId: "j1" });
  });

  test("after a batch's last card, Commit & next goes on to another batch's ready card", async () => {
    api.getJob.mockResolvedValueOnce(ok(job({ flags: [] })));
    api.commit.mockResolvedValueOnce(ok({ recipeId: "r1", slug: "banana-mug-cake", nextJobId: null, warnings: ["tag_dropped:Desserts"] }));
    api.getBatch.mockResolvedValue(ok({ id: "b1", source: "app", jobs: [batchJob("j1", 0, "committed"), batchJob("j2", 1, "failed")] }));
    api.getJobs.mockResolvedValue(ok({
      items: [
        { id: "x2", batchId: "b2", position: 1, status: "ready", source: "inbox", pageCount: 1, createdAt: "2026-10-04T08:00:00Z" },
        { id: "x1", batchId: "b2", position: 0, status: "ready", source: "inbox", pageCount: 1, createdAt: "2026-10-04T08:00:00Z" },
      ],
    }));
    const { review, navigate } = await loaded();

    expect(await review.commit()).toBe("committed");

    expect(navigate).toHaveBeenCalledExactlyOnceWith("/g/home/recipes/cards/x1");
    expect(takeRecipeIngestCommitNotice()).toBeNull();
    expect(takeCarriedReviewNotice("x1")).toEqual({
      kind: "warning",
      text: "Added Banana Mug Cake · Next batch",
      detail: "The tag \"Desserts\" no longer exists, so it wasn't added.",
      undoJobId: "j1",
    });
  });

  test("Skip on the batch's last card says so only when nothing in the batch is still being read", async () => {
    api.getBatch.mockResolvedValue(ok({ id: "b1", source: "app", jobs: [batchJob("j1", 0, "ready"), batchJob("j2", 1, "processing")] }));
    const { review, navigate } = await loaded();

    await review.skip();
    expect(navigate).toHaveBeenLastCalledWith("/g/home/recipes/cards?batch=b1");
    expect(takeRecipeIngestCommitNotice()).toEqual({ text: "1 card is still being read", warning: null });

    api.getBatch.mockResolvedValue(ok({ id: "b1", source: "app", jobs: [batchJob("j1", 0, "ready"), batchJob("j2", 1, "failed")] }));
    await review.skip();
    expect(navigate).toHaveBeenLastCalledWith("/g/home/recipes/cards?batch=b1");
    expect(takeRecipeIngestCommitNotice()).toEqual({ text: "That was the last card to review in this batch.", warning: null });
    expect(toast.info).not.toHaveBeenCalled();
  });

  test("Skip on the batch's last card goes on to another batch's ready card", async () => {
    api.getBatch.mockResolvedValue(ok({ id: "b1", source: "app", jobs: [batchJob("j1", 0, "ready")] }));
    api.getJobs.mockResolvedValue(ok({ items: [{ id: "x1", batchId: "b2", position: 0, status: "ready", source: "app", pageCount: 1 }] }));
    const { review, navigate } = await loaded();

    await review.skip();

    expect(navigate).toHaveBeenCalledExactlyOnceWith("/g/home/recipes/cards/x1");
    expect(takeCarriedReviewNotice("x1")).toEqual({ kind: "info", text: "Next batch", detail: null });
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

    expect(review.notice.value).toMatchObject({ kind: "error", text: "This recipe couldn't be added. Check the card's fields and try again." });
    expect(toast.error).not.toHaveBeenCalled();
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
    expect(review.notice.value).toBeNull();

    await review.reextract();
    expect(review.notice.value).toMatchObject({ kind: "info", text: "This card is being read. Try again when it's done." });
    expect(toast.info).not.toHaveBeenCalled();
    expect(toast.error).not.toHaveBeenCalled();
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

  /**
   * The poll's GET goes out while a re-read runs, then a save that keeps the draft version (it only resolves a flag or
   * dismisses the banner) goes out too. The GET answers first, with the row from before the save.
   */
  async function readBeforeQuietSave(act: (review: RecipeIngestReview) => void, loadedJob: RecipeIngestionJobOut, read: RecipeIngestionJobOut, saved: unknown) {
    api.getJob.mockResolvedValueOnce(ok(loadedJob));
    const { review } = await loaded();
    let answerGet: (value: unknown) => void = () => {};
    api.getJobState.mockResolvedValue(ok(state({ proposalIds: ["p1"], error: read.error })));
    api.getJob.mockImplementation(() => new Promise((resolve) => {
      answerGet = resolve;
    }));
    const polled = review.pollState();
    await flushPromises();

    let answerSave: (value: unknown) => void = () => {};
    api.updateJob.mockImplementation(() => new Promise((resolve) => {
      answerSave = resolve;
    }));
    act(review);
    await vi.advanceTimersByTimeAsync(1500);
    expect(api.updateJob).toHaveBeenCalledOnce();

    answerGet(ok(read));
    await flushPromises();
    answerSave(ok(saved));
    await polled;
    await flushPromises();
    return review;
  }

  const rereadProposal: CardProposal = { id: "p1", kind: "region", target: { field: "name", ref: null }, text: "Banana Cake", readable: true };

  test("a read from before a save that only kept an error as written doesn't open it again", async () => {
    const running = job({ flags: [blankFlag], task: { kind: "reread", state: "running" } });
    const review = await readBeforeQuietSave(
      r => r.resolveFlag(blankFlag, "kept"),
      running,
      job({ flags: [blankFlag], proposals: [rereadProposal] }),
      { draftVersion: 3, flags: [{ ...blankFlag, resolution: "kept" }], errorCount: 0, warningCount: 0 },
    );
    expect(api.updateJob.mock.calls[0]![1].flagResolutions).toEqual({ "blank:steps:s2": "kept" });

    expect(review.openErrors.value).toEqual([]);
    expect(review.flags.value.map(item => [item.id, item.resolution])).toEqual([["blank:steps:s2", "kept"]]);
    // the read's news still shows
    expect(review.proposals.value.map(item => item.id)).toEqual(["p1"]);
    expect(review.task.value).toBeNull();

    api.commit.mockResolvedValueOnce(ok({ recipeId: "r1", slug: "banana-mug-cake", nextJobId: "j2", warnings: [] }));
    expect(await review.commit()).toBe("committed");
    expect(api.commit).toHaveBeenCalledExactlyOnceWith("j1", { draftVersion: 3 });
  });

  test("a read from before the save that dismissed a failed re-read's banner doesn't bring it back", async () => {
    const failedReread: RecipeIngestionJobError = { code: "provider_failed", params: {} };
    const review = await readBeforeQuietSave(
      r => r.dismissError(),
      job({ flags: [], error: failedReread, task: { kind: "reread", state: "running" } }),
      job({ flags: [], error: failedReread, proposals: [rereadProposal] }),
      { draftVersion: 3, flags: [], errorCount: 0, warningCount: 0 },
    );
    expect(api.updateJob.mock.calls[0]![1].clearError).toBe(true);

    expect(review.job.value?.error).toBeNull();
    expect(review.proposals.value.map(item => item.id)).toEqual(["p1"]);
  });

  test("a dismissed banner stays dismissed while its save waits", async () => {
    const failedReread: RecipeIngestionJobError = { code: "provider_failed", params: {} };
    api.getJob.mockResolvedValueOnce(ok(job({ flags: [], error: failedReread, task: { kind: "reread", state: "running" } })));
    const { review } = await loaded();
    review.dismissError();

    api.getJob.mockResolvedValueOnce(ok(job({ flags: [], error: failedReread, proposals: [rereadProposal] })));
    await review.refresh();
    expect(review.job.value?.error).toBeNull();
    expect(review.proposals.value.map(item => item.id)).toEqual(["p1"]);
  });

  test("Save as eval case sends the name, the tick, the tags and the notes", async () => {
    const { review } = await loaded();
    api.saveEvalCase.mockResolvedValueOnce(ok({ slug: "banana-mug-cake", files: [] }));

    const request = { slug: "banana-mug-cake", verified: true, tags: ["handwritten" as const, "faded" as const], notes: "Pencil" };
    expect(await review.saveEvalCase(request)).toBe("saved");
    expect(api.saveEvalCase).toHaveBeenCalledExactlyOnceWith("j1", request);
    expect(review.notice.value).toMatchObject({ kind: "success", text: "Saved as banana-mug-cake" });
  });

  test("Save as eval case says a name is taken only when it is; other refusals say why", async () => {
    const { review } = await loaded();

    api.saveEvalCase.mockResolvedValueOnce(apiError(409, { code: "eval_case_exists" }));
    expect(await review.saveEvalCase({ slug: "banana-mug-cake", verified: true })).toBe("exists");
    expect(review.notice.value).toBeNull();

    api.saveEvalCase.mockResolvedValueOnce(apiError(409, { code: "not_exportable" }));
    expect(await review.saveEvalCase({ slug: "banana-mug-cake-2", verified: true })).toBe("failed");
    expect(review.notice.value).toMatchObject({
      kind: "error",
      text: "Only a card that is ready to review or added, with its photos, can be saved as an eval case.",
    });

    api.saveEvalCase.mockResolvedValueOnce(apiError(409, { code: "files_missing" }));
    expect(await review.saveEvalCase({ slug: "banana-mug-cake-3", verified: true })).toBe("failed");
    expect(review.notice.value).toMatchObject({ kind: "error", text: "The card's photos are missing. Scan it again." });
    expect(toast.error).not.toHaveBeenCalled();
  });

  test("what commit left out (an organizer deleted since) is said with the Added notice", async () => {
    api.getJob.mockResolvedValueOnce(ok(job({ flags: [], draft: bananaDraft({ tags: [{ id: "t1", name: "Desserts" }] }) })));
    api.commit.mockResolvedValueOnce(ok({
      recipeId: "r1",
      slug: "banana-mug-cake",
      nextJobId: "j2",
      warnings: ["tag_dropped:Desserts", "something_new:x"],
    }));
    const { review, navigate } = await loaded();

    expect(await review.commit()).toBe("committed");
    expect(takeCarriedReviewNotice("j2")).toEqual({
      kind: "warning",
      text: "Added Banana Mug Cake",
      detail: "The tag \"Desserts\" no longer exists, so it wasn't added.",
      undoJobId: "j1",
    });
    expect(toast.success).not.toHaveBeenCalled();
    expect(toast.warning).not.toHaveBeenCalled();
    expect(navigate).toHaveBeenCalledExactlyOnceWith("/g/home/recipes/cards/j2");
  });

  test("after the last card, what commit left out is said with Added beside the queue's summary", async () => {
    api.getJob.mockResolvedValueOnce(ok(job({ flags: [] })));
    api.commit.mockResolvedValueOnce(ok({ recipeId: "r1", slug: "banana-mug-cake", nextJobId: null, warnings: ["tag_dropped:Desserts"] }));
    api.getBatch.mockResolvedValue(ok({ id: "b1", source: "app", jobs: [batchJob("j1", 0, "committed")] }));
    const { review, navigate } = await loaded();

    expect(await review.commit()).toBe("committed");
    expect(toast.warning).not.toHaveBeenCalled();
    expect(navigate).toHaveBeenCalledExactlyOnceWith("/g/home/recipes/cards?batch=b1");
    expect(takeRecipeIngestCommitNotice()).toEqual({
      text: "Added Banana Mug Cake",
      warning: "The tag \"Desserts\" no longer exists, so it wasn't added.",
      undoJobId: "j1",
    });
  });

  test("the next card shows its place in the batch at once, before its own fetch of the batch answers", async () => {
    api.getJob.mockResolvedValueOnce(ok(job({ flags: [] })));
    api.commit.mockResolvedValueOnce(ok({ recipeId: "r1", slug: "banana-mug-cake", nextJobId: "j2", warnings: [] }));
    const { review: first } = await loaded();
    expect(await first.commit()).toBe("committed");

    // the next card's page: its batch fetch is slow
    let answerBatch: (value: unknown) => void = () => {};
    api.getBatch.mockImplementation(() => new Promise((resolve) => {
      answerBatch = resolve;
    }));
    api.getJob.mockResolvedValueOnce(ok(job({ id: "j2", position: 1, flags: [] })));
    let next: RecipeIngestReview | undefined;
    const Host = defineComponent({
      setup() {
        next = useRecipeIngestReview("j2", { groupSlug: "home", navigate: vi.fn() });
        return () => h("div");
      },
    });
    wrappers.push(mount(Host));
    await next!.load();
    expect(next!.position.value).toEqual({ number: 2, total: 2, previous: "j1", next: null });

    // the fresh batch replaces it
    answerBatch(ok({ id: "b1", source: "app", jobs: [batchJob("j1", 0, "committed"), batchJob("j2", 1, "ready"), batchJob("j3", 2, "ready")] }));
    await flushPromises();
    expect(next!.position.value).toEqual({ number: 2, total: 3, previous: "j1", next: "j3" });
  });

  test("signing out forgets the batches and the notice review pages carry over", async () => {
    api.getJob.mockResolvedValueOnce(ok(job({ flags: [] })));
    api.commit.mockResolvedValueOnce(ok({ recipeId: "r1", slug: "banana-mug-cake", nextJobId: "j2", warnings: [] }));
    const { review } = await loaded();
    expect(await review.commit()).toBe("committed");

    clearComposableCaches();
    expect(takeRecipeIngestCommitNotice()).toBeNull();
    api.getBatch.mockImplementation(() => new Promise(() => {}));
    const { review: next } = await loaded();
    expect(next.position.value).toBeNull();
  });

  test("using a re-read in place of the flagged line shows the flag as fixed at once; the save's answer settles it", async () => {
    const proposal: CardProposal = { id: "p1", kind: "region", target: { field: "steps", ref: "s2" }, text: "Microwave on high for 2 minutes.", readable: true };
    api.getJob.mockResolvedValueOnce(ok(job({ flags: [blankFlag], proposals: [proposal] })));
    let answerSave: (value: unknown) => void = () => {};
    api.updateJob.mockImplementation(() => new Promise((resolve) => {
      answerSave = resolve;
    }));
    const { review } = await loaded();
    expect(review.needsALook.value.map(item => [item.state, item.proposals.map(p => p.id)])).toEqual([["open", ["p1"]]]);

    review.useProposal(proposal, "replace");
    await nextTick();
    expect(review.draft.value.steps[1]!.text).toBe("Microwave on high for 2 minutes.");
    expect(review.needsALook.value.map(item => item.state)).toEqual(["fixed"]);
    expect(review.openErrors.value).toEqual([]);

    // the server no longer raises it
    await vi.advanceTimersByTimeAsync(1500);
    answerSave(ok({ draftVersion: 4, flags: [], errorCount: 0, warningCount: 0 }));
    await flushPromises();
    expect(review.needsALook.value.map(item => item.state)).toEqual(["fixed"]);
  });

  test("a re-read the server still finds a problem in opens its flag again when the save answers", async () => {
    const proposal: CardProposal = { id: "p1", kind: "region", target: { field: "steps", ref: "s2" }, text: "Microwave on high for [blank] min.", readable: true };
    api.getJob.mockResolvedValueOnce(ok(job({ flags: [blankFlag], proposals: [proposal] })));
    api.updateJob.mockResolvedValueOnce(ok({ draftVersion: 4, flags: [blankFlag], errorCount: 1, warningCount: 0 }));
    const { review } = await loaded();

    review.useProposal(proposal, "replace");
    await nextTick();
    expect(review.needsALook.value.map(item => item.state)).toEqual(["fixed"]);

    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();
    expect(review.needsALook.value.map(item => item.state)).toEqual(["open"]);
  });

  test("a re-read added to the end of a flagged line leaves its flag for the save to settle", async () => {
    const proposal: CardProposal = { id: "p1", kind: "region", target: { field: "steps", ref: "s2" }, text: "Let it cool.", readable: true };
    api.getJob.mockResolvedValueOnce(ok(job({ flags: [blankFlag], proposals: [proposal] })));
    const { review } = await loaded();

    review.useProposal(proposal, "append");
    await nextTick();
    expect(review.needsALook.value.map(item => item.state)).toEqual(["open"]);
  });

  test("a missing card says so", async () => {
    api.getJob.mockResolvedValueOnce(apiError(404, "Not found"));
    const { review } = mountReview();
    await review.load();
    expect(review.loadState.value).toBe("not-found");
  });

  test("a card that failed to load (no signal) loads again on Try again", async () => {
    api.getJob.mockResolvedValueOnce({ data: null, response: null, error: { message: "Network Error" } });
    const { review } = mountReview();
    await review.load();
    expect(review.loadState.value).toBe("failed");

    await review.load();
    expect(review.loadState.value).toBe("ready");
    expect(review.draft.value.name).toBe("Banana Mug Cake");
  });

  // ==========================================
  // Notices (docs/ai/PHASE2.md §6.2: in the review bar, never a toast over the page)

  test("the card Commit & next opened says what was added, once, in its review bar", async () => {
    carryReviewNotice("j1", { kind: "success", text: "Added Lemon Bars" });
    const { review } = await loaded();
    expect(review.notice.value).toMatchObject({ kind: "success", text: "Added Lemon Bars", detail: null, action: null });

    // it goes by itself
    await vi.advanceTimersByTimeAsync(NOTICE_MS);
    expect(review.notice.value).toBeNull();

    // once only, and only on the card it was left for
    carryReviewNotice("j9", { kind: "success", text: "Added Lemon Bars" });
    const { review: other } = await loaded();
    expect(other.notice.value).toBeNull();
  });

  test("a warning stays until it's dismissed", async () => {
    carryReviewNotice("j1", { kind: "warning", text: "Added Lemon Bars", detail: "The tag \"Desserts\" no longer exists, so it wasn't added." });
    const { review } = await loaded();

    await vi.advanceTimersByTimeAsync(NOTICE_MS * 3);
    expect(review.notice.value).toMatchObject({ kind: "warning", detail: "The tag \"Desserts\" no longer exists, so it wasn't added." });

    review.dismissNotice();
    expect(review.notice.value).toBeNull();
  });

  test("a notice left a minute ago is about another visit", async () => {
    carryReviewNotice("j1", { kind: "success", text: "Added Lemon Bars" });
    const later = Date.now() + 60_000;
    vi.spyOn(Date, "now").mockReturnValue(later);
    try {
      const { review } = await loaded();
      expect(review.notice.value).toBeNull();
    }
    finally {
      vi.restoreAllMocks();
    }
  });

  test("after turning a page the review bar offers to read the card again, and stays until used", async () => {
    api.rotatePage.mockResolvedValueOnce(ok({ index: 0, width: 2048, height: 1536, rotation: 90, rotationSource: "user", oriented: true }));
    api.reextract.mockResolvedValueOnce(ok(state({ task: { kind: "extract", state: "queued" } })));
    const { review } = await loaded();

    await review.rotate(0);
    expect(review.notice.value).toMatchObject({ kind: "info", text: "Turn the card upright, then read it again." });
    // short, so the hint and its button fit in the bar on a phone
    expect(review.notice.value!.action!.label).toBe("Read again");
    await vi.advanceTimersByTimeAsync(NOTICE_MS * 2);
    expect(review.notice.value).not.toBeNull();

    await review.runNoticeAction();
    expect(api.reextract).toHaveBeenCalledExactlyOnceWith("j1");
    expect(review.notice.value).toBeNull();
    expect(toast.info).not.toHaveBeenCalled();
  });

  test("a failed card, once turned, is offered Retry (it has nothing to read again)", async () => {
    api.getJob.mockResolvedValueOnce(ok(job({ status: "failed", draft: null, flags: [], error: { code: "no_recipe_found", params: {} } })));
    api.rotatePage.mockResolvedValueOnce(ok({ index: 0, width: 2048, height: 1536, rotation: 90, rotationSource: "user", oriented: true }));
    api.retry.mockResolvedValueOnce(ok(state({ status: "processing", task: { kind: "extract", state: "queued" } })));
    const { review } = await loaded();

    await review.rotate(0);
    expect(review.notice.value!.action!.label).toBe("Retry");
    await review.runNoticeAction();
    expect(api.retry).toHaveBeenCalledExactlyOnceWith("j1");
    expect(api.reextract).not.toHaveBeenCalled();
  });

  test("a failed card kept on this server can be read with the group's cloud providers: it's no longer kept local", async () => {
    const localOnlyFailed = { code: "local_only_unavailable" as const, params: {} };
    api.getJob.mockResolvedValueOnce(ok(job({
      status: "failed",
      draft: null,
      flags: [],
      localOnly: true,
      error: localOnlyFailed,
      permissions: { canDiscard: true, canReadWithCloud: true },
    })));
    api.readWithCloud.mockResolvedValueOnce(ok(state({ status: "processing", task: { kind: "extract", state: "queued" } })));
    const { review } = await loaded();

    expect(await review.readWithCloud()).toBe(true);
    expect(api.readWithCloud).toHaveBeenCalledExactlyOnceWith("j1");
    expect(review.job.value).toMatchObject({ status: "processing", localOnly: false, error: null, task: { kind: "extract", state: "queued" } });
    expect(review.job.value!.permissions).toEqual({ canDiscard: true, canReadWithCloud: false });
    expect(review.notice.value).toBeNull();
  });

  test("reading with the cloud refused (the group now keeps every card local) says why and shows the card as it is", async () => {
    const failed = job({
      status: "failed",
      draft: null,
      flags: [],
      localOnly: true,
      error: { code: "local_only_unavailable", params: {} },
      permissions: { canReadWithCloud: true },
    });
    api.getJob.mockResolvedValueOnce(ok(failed));
    const { review } = await loaded();
    api.readWithCloud.mockResolvedValueOnce(apiError(409, { code: "group_local_only" }));
    api.getJob.mockResolvedValueOnce(ok({ ...failed, permissions: { canReadWithCloud: false } }));

    expect(await review.readWithCloud()).toBe(false);
    expect(review.notice.value).toMatchObject({
      kind: "error",
      text: "This group keeps every card on this server, so cards can't be read with a cloud provider.",
    });
    expect(api.getJob).toHaveBeenCalledTimes(2);
    expect(review.job.value).toMatchObject({ status: "failed", localOnly: true, permissions: { canReadWithCloud: false } });
  });

  test("Back to review deletes the recipe and brings the card back for review with its draft", async () => {
    const committed = job({ status: "committed", flags: [], recipe: { id: "r1", slug: "banana-mug-cake", name: "Banana Mug Cake" }, permissions: { canUncommit: true } });
    api.getJob.mockResolvedValueOnce(ok(committed));
    const { review } = await loaded();
    expect(review.readOnly.value).toBe(true);

    api.uncommit.mockResolvedValueOnce(ok(state({ status: "ready", draftVersion: 4 })));
    api.getJob.mockResolvedValueOnce(ok(job({ draftVersion: 4, flags: [], recipe: null, permissions: { canUncommit: false } })));
    expect(await review.uncommit()).toBe("done");

    expect(api.uncommit).toHaveBeenCalledExactlyOnceWith("j1", {});
    expect(review.job.value).toMatchObject({ status: "ready", draftVersion: 4, recipe: null });
    expect(review.draft.value.name).toBe("Banana Mug Cake");
    expect(review.readOnly.value).toBe(false);
    expect(review.notice.value).toMatchObject({ kind: "success", text: "The card is back for review." });
    expect(api.getCounts).toHaveBeenCalled();
  });

  test("Back to review asks again when the recipe was edited since; with force it goes back anyway", async () => {
    api.getJob.mockResolvedValueOnce(ok(job({ status: "committed", flags: [], permissions: { canUncommit: true } })));
    const { review } = await loaded();

    api.uncommit.mockResolvedValueOnce(apiError(409, { code: "recipe_edited" }));
    expect(await review.uncommit()).toBe("edited");
    // the page asks; nothing to say yet
    expect(review.notice.value).toBeNull();
    expect(review.job.value!.status).toBe("committed");

    api.uncommit.mockResolvedValueOnce(ok(state({ status: "ready", draftVersion: 4 })));
    api.getJob.mockResolvedValueOnce(ok(job({ draftVersion: 4, flags: [] })));
    expect(await review.uncommit(true)).toBe("done");
    expect(api.uncommit).toHaveBeenLastCalledWith("j1", { force: true });
    expect(review.job.value!.status).toBe("ready");
  });

  test("Back to review refused (the card's photos were removed) says why and shows the card as it is", async () => {
    const committed = job({ status: "committed", flags: [], permissions: { canUncommit: true } });
    api.getJob.mockResolvedValueOnce(ok(committed));
    const { review } = await loaded();

    api.uncommit.mockResolvedValueOnce(apiError(409, { code: "purged" }));
    api.getJob.mockResolvedValueOnce(ok({ ...committed, permissions: { canUncommit: false } }));
    expect(await review.uncommit()).toBe("failed");
    expect(review.notice.value).toMatchObject({ kind: "error", text: "This card's photos and draft were removed after a while, so it can't go back to review." });
    expect(review.job.value!.permissions!.canUncommit).toBe(false);
  });

  test("the next card's Added notice has Undo, which takes the card just added back to review", async () => {
    carryReviewNotice("j1", { kind: "success", text: "Added Lemon Bars", detail: null, undoJobId: "j0" });
    api.uncommit.mockResolvedValueOnce(ok(state({ status: "ready", draftVersion: 2 })));
    const { review, navigate } = await loaded();

    expect(review.notice.value).toMatchObject({ kind: "success", text: "Added Lemon Bars", action: { label: "Undo" } });
    // with its action it stays until it's used or dismissed
    await vi.advanceTimersByTimeAsync(NOTICE_MS * 2);
    expect(review.notice.value).not.toBeNull();

    await review.runNoticeAction();
    expect(api.uncommit).toHaveBeenCalledExactlyOnceWith("j0", {});
    expect(navigate).toHaveBeenCalledExactlyOnceWith("/g/home/recipes/cards/j0");
    expect(takeCarriedReviewNotice("j0")).toEqual({ kind: "success", text: "The card is back for review.", detail: null });
    expect(api.getCounts).toHaveBeenCalled();
  });

  test("Undo on a recipe edited since doesn't delete it: the notice says so and opens the card to decide there", async () => {
    carryReviewNotice("j1", { kind: "success", text: "Added Lemon Bars", detail: null, undoJobId: "j0" });
    api.uncommit.mockResolvedValueOnce(apiError(409, { code: "recipe_edited" }));
    const { review, navigate } = await loaded();

    await review.runNoticeAction();
    expect(api.uncommit).toHaveBeenCalledExactlyOnceWith("j0", {});
    expect(navigate).not.toHaveBeenCalled();
    expect(review.notice.value).toMatchObject({
      kind: "warning",
      text: "The recipe was changed after this card was added. Going back to review would delete those changes.",
      action: { label: "Open card" },
    });
    await review.runNoticeAction();
    expect(navigate).toHaveBeenCalledExactlyOnceWith("/g/home/recipes/cards/j0");
  });

  test("Add as back of previous card: the card before it in the batch gets its photos and is opened, being read", async () => {
    api.getBatch.mockResolvedValue(ok({ id: "b1", source: "app", jobs: [batchJob("j0", 0, "ready"), batchJob("j1", 1, "ready", 1, 1)] }));
    const previous = job({ id: "j0", position: 0, pageCount: 1, permissions: { canMerge: true } });
    api.getJob.mockImplementation((id: string) => Promise.resolve(ok(id === "j0" ? previous : job({ position: 1, permissions: { canMerge: true } }))));
    api.merge.mockResolvedValueOnce(ok(state({ status: "ready", draftVersion: 3, task: { kind: "extract", state: "queued" } })));
    const { review, navigate } = await loaded();

    expect(review.mergeBlock.value).toBe("checking");
    await review.checkPreviousCard();
    expect(api.getJob).toHaveBeenLastCalledWith("j0");
    expect(review.mergeBlock.value).toBeNull();
    // an edit waiting to be saved goes with the card
    review.draft.value.name = "Banana Mug Cake, back";
    await nextTick();

    expect(await review.mergeIntoPrevious()).toBe(true);
    expect(api.merge).toHaveBeenCalledExactlyOnceWith("j1", { intoJobId: "j0" });
    expect(navigate).toHaveBeenCalledExactlyOnceWith("/g/home/recipes/cards/j0");
    expect(takeCarriedReviewNotice("j0")).toEqual({ kind: "info", text: "Photos added. The card is being read again.", detail: null });
    expect(await review.saveBeforeLeaving()).toBe(true);
    await vi.advanceTimersByTimeAsync(5000);
    expect(api.updateJob).not.toHaveBeenCalled();
    expect(api.getCounts).toHaveBeenCalled();
  });

  test("Add as back of previous card says why it can't: the first card, an added one, or too many photos", async () => {
    const { review } = await loaded();
    // without the permission there's nothing to offer
    expect(review.mergeBlock.value).toBeNull();
    expect(review.canMerge.value).toBe(false);
    wrappers.forEach(wrapper => wrapper.unmount());
    wrappers.length = 0;

    api.getJob.mockResolvedValue(ok(job({ permissions: { canMerge: true } })));
    const first = await loaded();
    await first.review.checkPreviousCard();
    expect(first.review.canMerge.value).toBe(true);
    expect(first.review.mergeBlock.value).toBe("no-previous");
    expect(await first.review.mergeIntoPrevious()).toBe(false);
    expect(api.merge).not.toHaveBeenCalled();

    api.getBatch.mockResolvedValue(ok({ id: "b1", source: "app", jobs: [batchJob("j0", 0, "committed"), batchJob("j1", 1, "ready")] }));
    api.getJob.mockImplementation((id: string) => Promise.resolve(ok(id === "j0"
      ? job({ id: "j0", status: "committed", pageCount: 1, permissions: { canMerge: false } })
      : job({ position: 1, pageCount: 2, permissions: { canMerge: true } }))));
    const second = await loaded();
    await second.review.checkPreviousCard();
    expect(second.review.mergeBlock.value).toBe("previous-added");

    api.getJob.mockImplementation((id: string) => Promise.resolve(ok(id === "j0"
      ? job({ id: "j0", pageCount: 3, permissions: { canMerge: true } })
      : job({ position: 1, pageCount: 2, permissions: { canMerge: true } }))));
    await second.review.checkPreviousCard();
    expect(second.review.mergeBlock.value).toBe("too-many-pages");

    // a card being read can't move until that's done
    second.review.job.value!.task = { kind: "reread", state: "running" };
    expect(second.review.mergeBlock.value).toBe("busy");
  });

  test("a merge the server refuses says why with its numbers, and checks the previous card again", async () => {
    api.getBatch.mockResolvedValue(ok({ id: "b1", source: "app", jobs: [batchJob("j0", 0, "ready"), batchJob("j1", 1, "ready")] }));
    api.getJob.mockImplementation((id: string) => Promise.resolve(ok(id === "j0"
      ? job({ id: "j0", pageCount: 1, permissions: { canMerge: true } })
      : job({ position: 1, permissions: { canMerge: true } }))));
    const { review, navigate } = await loaded();
    await review.checkPreviousCard();

    api.merge.mockResolvedValueOnce(apiError(409, { code: "too_many_pages", max: 4 }));
    expect(await review.mergeIntoPrevious()).toBe(false);
    expect(review.notice.value).toMatchObject({ kind: "error", text: "A card can have at most 4 photos." });
    expect(navigate).not.toHaveBeenCalled();
    expect(api.getJob.mock.calls.filter(([id]) => id === "j0")).toHaveLength(2);
  });

  test("Discard goes on to the next card, which says the card was discarded", async () => {
    api.discard.mockResolvedValueOnce(ok(null));
    const { review, navigate } = await loaded();

    await review.discard();

    expect(navigate).toHaveBeenCalledExactlyOnceWith("/g/home/recipes/cards/j2");
    expect(takeCarriedReviewNotice("j2")).toEqual({ kind: "success", text: "Card discarded", detail: null });
    expect(toast.success).not.toHaveBeenCalled();
  });

  // ==========================================
  // Autosave that fails (docs/ai/PHASE2.md §6.6)

  const offline = { data: null, response: null, error: { message: "Network Error" } };

  test("a save with no answer is tried again: after 2 s, 4 s, 8 s ... and the edits go once it works", async () => {
    const { review } = await loaded();
    api.updateJob.mockResolvedValueOnce(offline).mockResolvedValueOnce(offline).mockResolvedValueOnce(apiError(502, "Bad gateway"));

    review.draft.value.name = "Banana Mug Cake for One";
    await nextTick();
    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();
    expect(api.updateJob).toHaveBeenCalledTimes(1);
    expect(review.saveState.value).toBe("error");

    await vi.advanceTimersByTimeAsync(1999);
    expect(api.updateJob).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    await flushPromises();
    expect(api.updateJob).toHaveBeenCalledTimes(2);

    await vi.advanceTimersByTimeAsync(4000);
    await flushPromises();
    expect(api.updateJob).toHaveBeenCalledTimes(3);

    // a server error is tried again too; this time it works
    await vi.advanceTimersByTimeAsync(8000);
    await flushPromises();
    expect(api.updateJob).toHaveBeenCalledTimes(4);
    expect(api.updateJob.mock.calls[3]![1]).toMatchObject({ draftVersion: 3, draft: { name: "Banana Mug Cake for One" } });
    expect(review.saveState.value).toBe("saved");
    expect(review.isDirty.value).toBe(false);

    // nothing more is sent, and the next failure starts again at 2 s
    await vi.advanceTimersByTimeAsync(120_000);
    expect(api.updateJob).toHaveBeenCalledTimes(4);
    api.updateJob.mockResolvedValueOnce(offline);
    review.draft.value.name = "Banana Mug Cake";
    await nextTick();
    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();
    await vi.advanceTimersByTimeAsync(2000);
    await flushPromises();
    expect(api.updateJob).toHaveBeenCalledTimes(6);
  });

  test("retries wait at most a minute apart, and stop when the page closes", async () => {
    const { review } = await loaded();
    api.updateJob.mockResolvedValue(offline);
    review.draft.value.name = "Offline";
    await nextTick();
    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();
    // 2 + 4 + 8 + 16 + 32 s, then every 60 s
    await vi.advanceTimersByTimeAsync(62_000);
    await flushPromises();
    expect(api.updateJob).toHaveBeenCalledTimes(6);
    await vi.advanceTimersByTimeAsync(59_999);
    expect(api.updateJob).toHaveBeenCalledTimes(6);
    await vi.advanceTimersByTimeAsync(1);
    await flushPromises();
    expect(api.updateJob).toHaveBeenCalledTimes(7);

    wrappers.forEach(wrapper => wrapper.unmount());
    wrappers.length = 0;
    await flushPromises();
    const sent = api.updateJob.mock.calls.length;
    await vi.advanceTimersByTimeAsync(300_000);
    expect(api.updateJob).toHaveBeenCalledTimes(sent);
  });

  test("a save refused for what it holds isn't sent again, and says so", async () => {
    const { review } = await loaded();
    api.updateJob.mockResolvedValueOnce(apiError(422, [{ loc: ["body", "draft"], msg: "bad" }]));

    review.draft.value.name = "Bad";
    await nextTick();
    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();
    await vi.advanceTimersByTimeAsync(120_000);

    expect(api.updateJob).toHaveBeenCalledOnce();
    expect(review.saveState.value).toBe("error");
    expect(review.notice.value).toMatchObject({ kind: "error", text: "Couldn't save your last change. Check what you just typed." });
  });

  test("a save refused because the card was added on another device shows the card as it is now", async () => {
    const { review } = await loaded();
    api.updateJob.mockResolvedValueOnce(apiError(409, { code: "invalid_status", status: "committed" }));
    api.getJob.mockResolvedValueOnce(ok(job({ status: "committed", recipe: { id: "r1", slug: "banana-mug-cake" } })));

    review.draft.value.name = "Too late";
    await nextTick();
    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();

    expect(review.job.value?.status).toBe("committed");
    expect(review.notice.value).toMatchObject({ kind: "warning", text: "This card was already added. Your last changes weren't saved." });
    expect(review.saveState.value).toBe("idle");
    // nothing is left to save, try again or ask about when leaving
    await vi.advanceTimersByTimeAsync(120_000);
    expect(api.updateJob).toHaveBeenCalledOnce();
    expect(await review.saveBeforeLeaving()).toBe(true);
  });

  test("a save refused because the card was discarded elsewhere shows that it's gone", async () => {
    const { review } = await loaded();
    api.updateJob.mockResolvedValueOnce(apiError(404, { code: "not_found" }));
    api.getJob.mockResolvedValueOnce(apiError(404, { code: "not_found" }));

    review.draft.value.name = "Too late";
    await nextTick();
    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();

    expect(review.loadState.value).toBe("not-found");
    expect(await review.saveBeforeLeaving()).toBe(true);
  });

  test("leaving saves what's pending first; only a save that still fails keeps the page", async () => {
    const { review } = await loaded();
    review.draft.value.name = "Typed just now";
    await nextTick();

    // the debounce hasn't fired: leaving sends it now
    expect(await review.saveBeforeLeaving()).toBe(true);
    expect(api.updateJob).toHaveBeenCalledOnce();

    api.updateJob.mockResolvedValueOnce(offline);
    review.draft.value.name = "Typed offline";
    await nextTick();
    expect(await review.saveBeforeLeaving()).toBe(false);
    expect(review.isDirty.value).toBe(true);
  });

  test("a logout the user chose saves the edit still waiting for its autosave, while the session is there", async () => {
    const { review } = await loaded();
    review.draft.value.name = "Typed just before logging out";
    await nextTick();
    expect(api.updateJob).not.toHaveBeenCalled();

    await Promise.all(runRecipeIngestLogoutTasks());
    expect(api.updateJob).toHaveBeenCalledOnce();
    expect(api.updateJob.mock.calls[0]![1].draft).toMatchObject({ name: "Typed just before logging out" });
    expect(review.isDirty.value).toBe(false);

    // the page closed: a later logout has nothing of it to wait for
    wrappers.forEach(wrapper => wrapper.unmount());
    wrappers.length = 0;
    expect(runRecipeIngestLogoutTasks()).toHaveLength(0);
  });

  test("once signed out, the page sends nothing and lets the browser leave without asking", async () => {
    let signedIn = true;
    setRecipeIngestSessionCheck(() => signedIn);
    try {
      const { review } = await loaded();
      review.draft.value.name = "Typed as the session ran out";
      await nextTick();
      const unload = () => {
        const event = new Event("beforeunload", { cancelable: true });
        window.dispatchEvent(event);
        return event.defaultPrevented;
      };
      // signed in, an unsaved edit asks before the page is closed
      expect(unload()).toBe(true);

      // an expired session's redirect clears the token first
      signedIn = false;
      expect(unload()).toBe(false);
      await vi.advanceTimersByTimeAsync(1500);
      await flushPromises();
      await review.save();
      await vi.advanceTimersByTimeAsync(120_000);
      expect(api.updateJob).not.toHaveBeenCalled();
    }
    finally {
      setRecipeIngestSessionCheck(null);
    }
  });

  // ==========================================
  // Cancel (docs/ai/PHASE2.md §3.5)

  test("Cancel asks a running re-read to stop, and drops the re-reads waiting", async () => {
    api.getJob.mockResolvedValueOnce(ok(job({ task: { kind: "reread", state: "running" } })));
    const { review } = await loaded();
    await review.requestReread(region);
    expect(review.rereadQueue.value).toHaveLength(1);

    api.cancel.mockResolvedValueOnce(ok(state({ task: { kind: "reread", state: "running", cancelRequested: true } })));
    expect(await review.cancelTask()).toBe(true);

    expect(api.cancel).toHaveBeenCalledExactlyOnceWith("j1");
    expect(review.rereadQueue.value).toEqual([]);
    expect(review.task.value).toMatchObject({ cancelRequested: true });

    // it stops within a heartbeat; the waiting re-read never goes
    api.getJobState.mockResolvedValueOnce(ok(state()));
    await vi.advanceTimersByTimeAsync(2000);
    await flushPromises();
    expect(review.task.value).toBeNull();
    expect(api.reread).not.toHaveBeenCalled();
  });

  test("Cancel clears a queued re-extract at once and says so", async () => {
    api.getJob.mockResolvedValueOnce(ok(job({ task: { kind: "extract", state: "queued" } })));
    const { review } = await loaded();
    expect(review.readOnly.value).toBe(true);

    api.cancel.mockResolvedValueOnce(ok(state()));
    api.getJob.mockResolvedValueOnce(ok(job()));
    await review.cancelTask();

    expect(review.task.value).toBeNull();
    expect(review.readOnly.value).toBe(false);
    expect(review.notice.value).toMatchObject({ kind: "info", text: "Reading stopped" });
  });

  test("a refused cancel says why", async () => {
    api.getJob.mockResolvedValueOnce(ok(job({ task: { kind: "reread", state: "running" } })));
    const { review } = await loaded();
    api.cancel.mockResolvedValueOnce(apiError(404, { code: "not_found" }));

    expect(await review.cancelTask()).toBe(false);
    expect(review.notice.value).toMatchObject({ kind: "error", text: "This card no longer exists." });
  });

  // ==========================================
  // What a save parsed (FR-01)

  /** "1 C. brown sugar" typed on a new line, as the server's save parses and stores it */
  const typedSugar = { referenceId: "i4", title: null, originalText: "", quantity: null, unit: null, food: null, note: "1 C. brown sugar", display: "1 C. brown sugar" };
  const parsedSugar = {
    referenceId: "i4",
    title: null,
    originalText: "1 C. brown sugar",
    quantity: 1,
    unit: { id: "u-cup", name: "cup" },
    food: { id: "f-sugar", name: "brown sugar" },
    note: "",
    display: "1 cup brown sugar",
    parseConfidence: 0.92,
    extractedHash: "hash-i4",
  };
  const sugarShorthand = flag({ id: "shorthand_read:ingredients:i4", kind: "shorthand_read", severity: "info", source: "parser", field: "ingredients", ref: "i4", params: { from: "C.", to: "cup" } });

  test("a line typed as text shows what the save parsed: its amount, unit and food", async () => {
    const { review } = await loaded();
    review.draft.value.ingredients.push({ ...typedSugar });
    await nextTick();

    api.updateJob.mockResolvedValueOnce(ok({
      draftVersion: 4,
      flags: [blankFlag, unsureFlag, sugarShorthand],
      errorCount: 1,
      warningCount: 1,
      ingredients: [parsedSugar],
    }));
    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();

    expect(api.updateJob.mock.calls[0]![1].draft.ingredients[3]).toMatchObject({ note: "1 C. brown sugar", quantity: null });
    expect(review.draft.value.ingredients[3]).toMatchObject({
      referenceId: "i4",
      originalText: "1 C. brown sugar",
      quantity: 1,
      unit: { id: "u-cup", name: "cup" },
      food: { id: "f-sugar", name: "brown sugar" },
      note: "",
    });
    // the flags the server raised describe the line the page now shows
    expect(review.infoFlags.value.map(item => item.id)).toEqual(["shorthand_read:ingredients:i4"]);
    // the parsed line is what the server stores: nothing is saved again
    expect(review.isDirty.value).toBe(false);
    await vi.advanceTimersByTimeAsync(5000);
    await flushPromises();
    expect(api.updateJob).toHaveBeenCalledOnce();
  });

  test("a line edited again while its save was out keeps the edit, which is saved next", async () => {
    const { review } = await loaded();
    review.draft.value.ingredients.push({ ...typedSugar });
    await nextTick();

    let answerSave: (value: unknown) => void = () => {};
    api.updateJob.mockImplementationOnce(() => new Promise((resolve) => {
      answerSave = resolve;
    }));
    await vi.advanceTimersByTimeAsync(1500);
    expect(api.updateJob).toHaveBeenCalledOnce();

    // typed over while the save was out; another line, untouched since, takes its parse
    review.draft.value.ingredients[3]!.note = "1 C. packed brown sugar";
    await nextTick();
    const parsedSalt = { ...bananaDraft().ingredients![1]!, note: "fine" };
    answerSave(ok({ draftVersion: 4, flags: [blankFlag], errorCount: 1, warningCount: 0, ingredients: [parsedSugar, parsedSalt] }));
    await flushPromises();

    expect(review.draft.value.ingredients[3]).toMatchObject({ note: "1 C. packed brown sugar", quantity: null, unit: null, food: null });
    expect(review.draft.value.ingredients[1]).toMatchObject({ referenceId: "i2", note: "fine" });
    expect(review.isDirty.value).toBe(true);

    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();
    expect(api.updateJob).toHaveBeenCalledTimes(2);
    const second = api.updateJob.mock.calls[1]![1];
    expect(second.draftVersion).toBe(4);
    expect(second.draft.ingredients[3]).toMatchObject({ note: "1 C. packed brown sugar", quantity: null });
    expect(second.draft.ingredients[1]).toMatchObject({ note: "fine" });
  });

  // ==========================================
  // Where a line is on the card (FR-03)

  test("where a line is on the card comes from the server; no hint, a failure or a slow answer is none", async () => {
    const { review } = await loaded();
    const hint = { page: 0, x: 0.05, y: 0.31, width: 0.9, height: 0.06, source: "ocr" };

    api.regionHint.mockResolvedValueOnce(ok(hint));
    expect(await review.regionHint({ field: "ingredients", ref: "i2" })).toEqual(hint);
    expect(api.regionHint).toHaveBeenCalledWith("j1", { field: "ingredients", ref: "i2" });

    api.regionHint.mockResolvedValueOnce(apiError(404, { code: "not_found" }));
    expect(await review.regionHint({ field: "name", ref: null })).toBeNull();
    api.regionHint.mockRejectedValueOnce(new Error("offline"));
    expect(await review.regionHint({ field: "name", ref: null })).toBeNull();

    api.regionHint.mockImplementationOnce(() => new Promise(() => {}));
    let answer: unknown = "waiting";
    void review.regionHint({ field: "steps", ref: "s2" }).then((value) => {
      answer = value;
    });
    await vi.advanceTimersByTimeAsync(1499);
    expect(answer).toBe("waiting");
    await vi.advanceTimersByTimeAsync(1);
    expect(answer).toBeNull();
    // nothing about it is said on the page
    expect(review.notice.value).toBeNull();
  });

  // ==========================================
  // Rebuild from this text (FR-22)

  const extracting = { kind: "extract" as const, state: "queued" as const };

  test("Rebuild from this text saves what's pending, sends the text, and the replaced draft says it was rebuilt", async () => {
    const { review } = await loaded();
    review.draft.value.attribution = "Grandma Jo";
    await nextTick();

    api.rebuild.mockResolvedValueOnce(ok(state({ draftVersion: 4, task: extracting })));
    expect(await review.rebuild("Banana Mug Cake\n1 ripe banana")).toBe(true);
    // the edit went first, so the server knows the draft was edited
    expect(api.updateJob).toHaveBeenCalledOnce();
    expect(api.rebuild).toHaveBeenCalledWith("j1", { transcription: "Banana Mug Cake\n1 ripe banana" });
    expect(api.updateJob.mock.invocationCallOrder[0]!).toBeLessThan(api.rebuild.mock.invocationCallOrder[0]!);
    expect(review.taskMode.value).toBe("rebuild");
    expect(review.readOnly.value).toBe(true);

    // done: a draft nobody edited is replaced
    api.getJobState.mockResolvedValueOnce(ok(state({ draftVersion: 5 })));
    api.getJob.mockResolvedValueOnce(ok(job({ draftVersion: 5, transcription: "Banana Mug Cake\n1 ripe banana", draft: bananaDraft({ name: "Banana Mug Cake" }), flags: [] })));
    await vi.advanceTimersByTimeAsync(2000);
    await flushPromises();

    expect(review.taskMode.value).toBeNull();
    expect(review.readOnly.value).toBe(false);
    expect(review.job.value!.transcription).toBe("Banana Mug Cake\n1 ripe banana");
    expect(review.notice.value).toMatchObject({ kind: "success", text: "Rebuilt from your text" });
  });

  test("an edited card gets the rebuilt recipe as a proposal, whose banner says so", async () => {
    const { review } = await loaded();
    api.rebuild.mockResolvedValueOnce(ok(state({ task: extracting })));
    await review.rebuild("Banana Mug Cake");

    const rebuilt: CardProposal = { id: "p9", kind: "full", origin: "rebuild", draft: bananaDraft({ name: "Banana Cake" }) };
    api.getJobState.mockResolvedValueOnce(ok(state({ proposalIds: ["p9"] })));
    api.getJob.mockResolvedValueOnce(ok(job({ proposals: [rebuilt] })));
    await vi.advanceTimersByTimeAsync(2000);
    await flushPromises();

    expect(review.otherProposals.value.map(proposal => proposal.id)).toEqual(["p9"]);
    expect(review.draft.value.name).toBe("Banana Mug Cake");
    expect(review.notice.value).toBeNull();
  });

  test("a rebuild that fails shows its banner; a stopped one says only that it stopped", async () => {
    const { review } = await loaded();
    api.rebuild.mockResolvedValueOnce(ok(state({ task: extracting })));
    await review.rebuild("Banana Mug Cake");
    api.getJobState.mockResolvedValueOnce(ok(state({ error: { code: "provider_failed", params: {} } })));
    api.getJob.mockResolvedValueOnce(ok(job({ error: { code: "provider_failed", params: {} } })));
    await vi.advanceTimersByTimeAsync(2000);
    await flushPromises();
    expect(review.job.value!.error).toMatchObject({ code: "provider_failed" });
    expect(review.notice.value).toBeNull();

    // asked to stop: it stops within a heartbeat, and nothing says it was rebuilt
    api.rebuild.mockResolvedValueOnce(ok(state({ task: { ...extracting, state: "running" } })));
    await review.rebuild("Banana Mug Cake");
    api.cancel.mockResolvedValueOnce(ok(state({ task: { ...extracting, state: "running", cancelRequested: true } })));
    await review.cancelTask();
    api.getJobState.mockResolvedValueOnce(ok(state()));
    api.getJob.mockResolvedValueOnce(ok(job()));
    await vi.advanceTimersByTimeAsync(2000);
    await flushPromises();
    expect(review.taskMode.value).toBeNull();
    expect(review.notice.value).toBeNull();
  });

  test("a rebuild isn't sent while another task runs, nor with edits that couldn't be saved, nor for empty text", async () => {
    const { review } = await loaded();
    api.rebuild.mockResolvedValueOnce(apiError(409, { code: "busy" }));
    expect(await review.rebuild("Banana Mug Cake")).toBe(false);
    expect(review.notice.value).toMatchObject({ kind: "info", text: "This card is being read. Try again when it's done." });
    expect(review.taskMode.value).toBeNull();

    expect(await review.rebuild("   ")).toBe(false);
    expect(api.rebuild).toHaveBeenCalledOnce();

    api.updateJob.mockResolvedValueOnce(apiError(500, "Server error"));
    review.draft.value.name = "Offline";
    await nextTick();
    expect(await review.rebuild("Banana Mug Cake")).toBe(false);
    expect(api.rebuild).toHaveBeenCalledOnce();
    expect(review.notice.value).toMatchObject({ kind: "error", text: "Your last changes aren't saved yet. Try again when they are." });
  });

  // ==========================================
  // Parse with AI (FR-24) and Keep as new food (FR-25)

  /** A line of a card in Spanish, kept as written */
  const harina = { referenceId: "i4", originalText: "2 tazas de harina", quantity: null, unit: null, food: null, note: "2 tazas de harina", display: "2 tazas de harina" };
  const harinaParsed = { ...harina, quantity: 2, unit: { id: "u-cup", name: "cup" }, food: { id: "f-flour", name: "harina" }, note: "", display: "2 cup harina", parseConfidence: 0.9 };

  test("Parse with AI saves first, sends the line, shows it parsing, and the parsed line lands when it's done", async () => {
    const draft = bananaDraft();
    api.getJob.mockResolvedValueOnce(ok(job({ draft: { ...draft, ingredients: [...draft.ingredients!, harina] } })));
    const { review } = await loaded();
    review.draft.value.name = "Pastel de plátano";
    await nextTick();

    api.parseLines.mockResolvedValueOnce(ok(state({ draftVersion: 4, task: extracting })));
    expect(await review.parseWithAi(["i4", "gone"])).toBe(true);
    expect(api.updateJob).toHaveBeenCalledOnce();
    expect(api.parseLines).toHaveBeenCalledWith("j1", { refs: ["i4"] });
    expect(review.taskMode.value).toBe("parse_lines");
    expect(review.parsingRefs.value).toEqual(["i4"]);
    // the editor waits, as it does while the card is read again
    expect(review.readOnly.value).toBe(true);

    api.getJobState.mockResolvedValueOnce(ok(state({ draftVersion: 5 })));
    api.getJob.mockResolvedValueOnce(ok(job({ draftVersion: 5, draft: { ...draft, name: "Pastel de plátano", ingredients: [...draft.ingredients!, harinaParsed] } })));
    await vi.advanceTimersByTimeAsync(2000);
    await flushPromises();

    expect(review.parsingRefs.value).toEqual([]);
    expect(review.readOnly.value).toBe(false);
    expect(review.draft.value.ingredients[3]).toMatchObject({ quantity: 2, unit: { name: "cup" }, food: { name: "harina" } });
    expect(review.notice.value).toMatchObject({ kind: "success", text: "Parsed with AI" });
  });

  test("a parse that changed nothing says so; one that failed shows its banner", async () => {
    const draft = bananaDraft();
    api.getJob.mockResolvedValue(ok(job({ draft: { ...draft, ingredients: [...draft.ingredients!, harina] } })));
    const { review } = await loaded();

    api.parseLines.mockResolvedValueOnce(ok(state({ task: extracting })));
    await review.parseWithAi(["i4"]);
    api.getJobState.mockResolvedValueOnce(ok(state()));
    await vi.advanceTimersByTimeAsync(2000);
    await flushPromises();
    expect(review.notice.value).toMatchObject({ kind: "info", text: "Parse with AI didn't change the line." });

    api.parseLines.mockResolvedValueOnce(ok(state({ task: extracting })));
    await review.parseWithAi(["i4", "i3"]);
    api.getJobState.mockResolvedValueOnce(ok(state({ error: { code: "ai_not_enabled", params: {} } })));
    api.getJob.mockResolvedValueOnce(ok(job({ draft: { ...draft, ingredients: [...draft.ingredients!, harina] }, error: { code: "ai_not_enabled", params: {} } })));
    review.dismissNotice();
    await vi.advanceTimersByTimeAsync(2000);
    await flushPromises();
    expect(review.job.value!.error).toMatchObject({ code: "ai_not_enabled" });
    expect(review.notice.value).toBeNull();
  });

  test("Parse with AI isn't sent for lines the card hasn't, or while another task runs", async () => {
    const { review } = await loaded();
    expect(await review.parseWithAi(["gone"])).toBe(false);
    expect(api.parseLines).not.toHaveBeenCalled();

    api.parseLines.mockResolvedValueOnce(apiError(409, { code: "busy" }));
    expect(await review.parseWithAi(["i1"])).toBe(false);
    expect(review.notice.value).toMatchObject({ kind: "info", text: "This card is being read. Try again when it's done." });
    expect(review.parsingRefs.value).toEqual([]);
  });

  test("Keep as new food unlinks the near miss and names the food as the card writes it", async () => {
    const onion = { referenceId: "i4", originalText: "2 rd onions, diced", quantity: 2, unit: null, food: { id: "f-red", name: "red onion" }, note: "diced", display: "2 red onion diced" };
    const fuzzy = flag({ id: "linked_fuzzy:ingredients:i4", kind: "linked_fuzzy", severity: "warning", source: "parser", field: "ingredients", ref: "i4", params: { name: "red onion", kind: "food", start: 2, end: 11 } });
    const draft = bananaDraft();
    api.getJob.mockResolvedValueOnce(ok(job({ draft: { ...draft, ingredients: [...draft.ingredients!, onion] }, flags: [blankFlag, fuzzy] })));
    const { review } = await loaded();

    review.keepAsNew(fuzzy);
    await nextTick();
    expect(review.draft.value.ingredients[3]).toMatchObject({ food: { id: null, name: "rd onions" }, quantity: 2, note: "diced", display: "2 rd onions diced" });
    expect(review.needsALook.value.find(item => item.flag.id === fuzzy.id)!.state).toBe("fixed");

    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();
    expect(api.updateJob.mock.calls[0]![1].draft.ingredients[3].food).toEqual({ id: null, name: "rd onions" });
  });

  // ==========================================
  // The possible-duplicate banner follows the saved name (FR-25)

  test("a save's answer says which recipe or waiting card the saved name matches, and what commit would name it", async () => {
    api.getJob.mockResolvedValueOnce(ok(job({ duplicateOf: { id: "r0", slug: "banana-mug-cake", name: "Banana Mug Cake" }, duplicateName: "Banana Mug Cake (1)" })));
    const { review } = await loaded();
    expect(review.job.value!.duplicateName).toBe("Banana Mug Cake (1)");

    // renamed away from the clash: the banner goes
    api.updateJob.mockResolvedValueOnce(ok({ draftVersion: 4, flags: [blankFlag], duplicateOf: null, duplicateJob: null, duplicateName: null }));
    review.draft.value.name = "Banana Bread";
    await nextTick();
    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();
    expect(review.job.value).toMatchObject({ duplicateOf: null, duplicateJob: null, duplicateName: null });

    // to a name another card waiting has, and a recipe holds twice already
    api.updateJob.mockResolvedValueOnce(ok({
      draftVersion: 5,
      flags: [blankFlag],
      duplicateOf: { id: "r1", slug: "scones", name: "Scones" },
      duplicateJob: { id: "j7", title: "Scones" },
      duplicateName: "Scones (2)",
    }));
    review.draft.value.name = "Scones";
    await nextTick();
    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();
    expect(review.job.value).toMatchObject({
      duplicateOf: { slug: "scones" },
      duplicateJob: { id: "j7" },
      duplicateName: "Scones (2)",
    });
  });
});
