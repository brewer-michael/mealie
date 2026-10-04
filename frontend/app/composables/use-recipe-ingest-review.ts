/**
 * The recipe card review page (docs/ai/PHASE2.md §6): pure helpers for batches, flags, drafts and crop regions, and
 * `useRecipeIngestReview`, the page's state: autosave carrying the draft version (retried while the page is open),
 * the version conflict, state polling while a task runs, the client-side re-read queue, commit, where the review goes
 * next, the ⋯ menu's actions and the notices the review bar shows. Fork-owned.
 */
import { useDebounceFn, useEventListener, useIntervalFn } from "@vueuse/core";
import { computed, onBeforeUnmount, readonly, ref, toValue, watch, type MaybeRefOrGetter } from "vue";
import { useUserApi } from "~/composables/api";
import { useFraction } from "~/composables/recipes/use-fraction";
import {
  errorCodeOf,
  errorStatusOf,
  leaveRecipeIngestCommitNotice,
  rememberedRecipeIngestBatch,
  rememberRecipeIngestBatch,
  useRecipeIngestCounts,
  useRecipeIngestSettings,
  useRecipeIngestText,
} from "~/composables/use-recipe-ingest";
import type { RecipeIngestCommitNotice, TranslateFn } from "~/composables/use-recipe-ingest";
import { uuid4 } from "~/composables/use-utils";
import type {
  CardDraft,
  CardDraftIngredient,
  CardDraftNote,
  CardDraftRef,
  CardDraftStep,
  CardFlag,
  CardFlagKind,
  CardFlagSeverity,
  CardProposal,
  EvalCaseRequest,
  EvalCaseTag,
  FlagResolution,
  IngestStatus,
  ProposalTarget,
  RecipeIngestionBatchJob,
  RecipeIngestionBatchOut,
  RecipeIngestionJobOut,
  RecipeIngestionJobState,
  RecipeIngestionJobSummary,
  RereadRequest,
  RotateRequest,
} from "~/lib/api/types/recipe-ingest";

/** Debounce of the draft's autosave */
export const AUTOSAVE_DELAY_MS = 1500;
/** How often the page asks for the job's state while a task runs */
export const STATE_POLL_MS = 2000;
/** How long a failed save waits before it's tried again: 2 s, doubling up to a minute, while the page is open */
export const SAVE_RETRY_MS: readonly number[] = [2000, 4000, 8000, 16000, 32000, 60000];
/** How long a notice without an action stays; warnings, errors and notices with an action stay until dismissed */
export const NOTICE_MS = 6000;
/** A re-read region's smallest side, as a fraction of the page (the server's `MIN_REGION_SIDE`) */
export const MIN_REGION_SIDE = 0.02;
/** What the page highlights (the server's `flag_rules.HIGHLIGHTED_SEVERITIES`) */
export const HIGHLIGHTED_SEVERITIES: readonly CardFlagSeverity[] = ["error", "warning"];
/** Errors that "Keep as written" resolves (the server's `flag_rules.KEEPABLE_KINDS`); every other error is fixed */
export const KEEPABLE_KINDS: readonly CardFlagKind[] = ["illegible", "blank"];
/** Flags about the parser's reading of a line, which the server drops once the line is edited (`flags.PARSE_KINDS`) */
export const PARSE_KINDS: readonly CardFlagKind[] = ["check_parse", "unit_unclear", "shorthand_read"];
/** The two markers a card's reading holds (docs/ai/PHASE2.md §4.3) */
export const MARKERS = { illegible: "[illegible]", blank: "[blank]" } as const;

// ==========================================
// Drafts

/** A draft with every list and the cover switch present, so the editor never meets a missing field */
export type ReviewDraft = CardDraft & {
  name: string;
  description: string;
  useCardAsCover: boolean;
  ingredients: CardDraftIngredient[];
  steps: CardDraftStep[];
  notes: (CardDraftNote & { id: string; title: string; text: string })[];
  tags: CardDraftRef[];
  categories: CardDraftRef[];
  tools: CardDraftRef[];
};

/** A plain copy of a draft (drafts are JSON) */
export function cloneDraft<T>(draft: T): T {
  return JSON.parse(JSON.stringify(draft)) as T;
}

/** A copy of the draft with its defaults filled in; ingredients, steps and notes without an id get one */
export function normalizeDraft(draft: CardDraft | null | undefined): ReviewDraft {
  const source = cloneDraft(draft ?? {});
  return {
    ...source,
    name: source.name ?? "",
    description: source.description ?? "",
    useCardAsCover: source.useCardAsCover ?? true,
    ingredients: (source.ingredients ?? []).map(ingredient => ({
      ...ingredient,
      referenceId: ingredient.referenceId || uuid4(),
      originalText: ingredient.originalText ?? "",
      note: ingredient.note ?? "",
      display: ingredient.display ?? "",
    })),
    steps: (source.steps ?? []).map(step => ({ ...step, id: step.id || uuid4(), text: step.text ?? "" })),
    // the server keys a note's flags (and their resolutions) to its id, so the id goes back with every save
    notes: (source.notes ?? []).map(note => ({ ...note, id: note.id || uuid4(), title: note.title ?? "", text: note.text ?? "" })),
    tags: source.tags ?? [],
    categories: source.categories ?? [],
    tools: source.tools ?? [],
  };
}

/** JSON with object keys sorted and undefined values left out, so equal drafts compare equal */
export function stableStringify(value: unknown): string {
  return JSON.stringify(value, (_key, item: unknown) => {
    if (item && typeof item === "object" && !Array.isArray(item)) {
      return Object.fromEntries(
        Object.entries(item as Record<string, unknown>)
          .filter(([, entry]) => entry !== undefined)
          .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)),
      );
    }
    return item;
  }) ?? "null";
}

/** Whether two drafts hold the same content, whatever their key order */
export function draftsEqual(a: CardDraft | null | undefined, b: CardDraft | null | undefined): boolean {
  return stableStringify(a ?? null) === stableStringify(b ?? null);
}

const UNICODE_FRACTIONS: Record<string, number> = {
  "½": 1 / 2,
  "⅓": 1 / 3,
  "⅔": 2 / 3,
  "¼": 1 / 4,
  "¾": 3 / 4,
  "⅕": 1 / 5,
  "⅖": 2 / 5,
  "⅗": 3 / 5,
  "⅘": 4 / 5,
  "⅙": 1 / 6,
  "⅚": 5 / 6,
  "⅛": 1 / 8,
  "⅜": 3 / 8,
  "⅝": 5 / 8,
  "⅞": 7 / 8,
};

/** A typed amount as a number: "2", "1.5", "1,5", "1/2", "1 1/2", "½", "1½"; null when it isn't one */
export function parseQuantity(text: string | number | null | undefined): number | null {
  if (typeof text === "number") {
    return Number.isFinite(text) ? text : null;
  }
  const value = (text ?? "").trim().replace(",", ".");
  if (!value) {
    return null;
  }
  const glyph = /^(\d+)?\s*([½⅓⅔¼¾⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞])$/.exec(value);
  if (glyph) {
    return Number(glyph[1] ?? 0) + (UNICODE_FRACTIONS[glyph[2]!] ?? 0);
  }
  const mixed = /^(\d+)\s+(\d+)\/(\d+)$/.exec(value);
  if (mixed && Number(mixed[3]) > 0) {
    return Number(mixed[1]) + Number(mixed[2]) / Number(mixed[3]);
  }
  const fraction = /^(\d+)\/(\d+)$/.exec(value);
  if (fraction && Number(fraction[2]) > 0) {
    return Number(fraction[1]) / Number(fraction[2]);
  }
  return /^\d+(\.\d+)?$/.test(value) ? Number(value) : null;
}

/** An amount as a card writes it: 1.5 → "1 1/2", 0.25 → "1/4"; small or odd amounts as decimals */
export function formatQuantity(quantity: number | null | undefined): string {
  if (!quantity) {
    return "";
  }
  if (quantity < 0.1) {
    return String(Number(quantity.toPrecision(3)));
  }
  const [whole, numerator, denominator] = useFraction().frac(quantity, 10, true) as [number, number, number];
  if (Math.abs(whole + numerator / denominator - quantity) > 0.01) {
    return String(Number(quantity.toPrecision(3)));
  }
  const parts = [whole > 0 ? String(whole) : "", numerator > 0 ? `${numerator}/${denominator}` : ""].filter(Boolean);
  return parts.join(" ") || "0";
}

/** The ingredient as one line, the way the card reads: "1 tbsp coconut oil (melted)" */
export function ingredientDisplay(ingredient: CardDraftIngredient): string {
  return [formatQuantity(ingredient.quantity), ingredient.unit?.name, ingredient.food?.name, ingredient.note]
    .map(part => (part ?? "").trim())
    .filter(Boolean)
    .join(" ");
}

/** The ingredient with its `display` recomputed (it goes stale with every edit) */
export function withDisplay(ingredient: CardDraftIngredient): CardDraftIngredient {
  return { ...ingredient, display: ingredientDisplay(ingredient) };
}

/** A food or unit from the group's store, as the ingredient autocompletes list them */
export interface IngestNamedOption {
  id: string;
  name: string;
  pluralName?: string | null;
  abbreviation?: string | null;
}

/** Whether a line was split into amount, unit and food, rather than kept as text in its note */
export function isParsedIngredient(ingredient: CardDraftIngredient): boolean {
  return (ingredient.quantity ?? null) !== null || !!ingredient.unit?.name || !!ingredient.food?.name;
}

// ==========================================
// Fields

/**
 * A draft field by its JSON name (`prepTime`), as flags, proposals and re-read targets name it. The attribute name
 * (`prep_time`) is read the same way, which the server also accepts.
 */
export function normalizeField(field: string): string {
  return field.replace(/_([a-z])/g, (_match, letter: string) => letter.toUpperCase());
}

/** Single-value fields a flag or a re-read can point at, in reading order */
export const TEXT_FIELDS = [
  "name",
  "attribution",
  "description",
  "recipeYield",
  "recipeServings",
  "prepTime",
  "performTime",
  "totalTime",
] as const;
export type TextField = (typeof TEXT_FIELDS)[number];

/** The page's reading order: the card as a whole first, then top to bottom */
const FIELD_ORDER = ["card", ...TEXT_FIELDS, "ingredients", "steps", "notes", "tags", "categories", "tools"];

function isTextField(field: string): field is TextField {
  return (TEXT_FIELDS as readonly string[]).includes(field);
}

/** Sets a single-value field from text: servings, the one number among them, as a number (null when it isn't one) */
function setTextField(draft: ReviewDraft, field: TextField, value: string) {
  if (field === "recipeServings") {
    draft.recipeServings = parseQuantity(value);
  }
  else {
    draft[field] = value;
  }
}

/** The id of the element that edits a field (or one ingredient or step), for scrolling to it */
export function fieldAnchorId(field: string, ref?: string | null): string {
  const key = normalizeField(field);
  return ref ? `ingest-field-${key}-${ref}` : `ingest-field-${key}`;
}

/** The id of a flag's "Needs a look" item */
export function flagAnchorId(flagId: string): string {
  return `ingest-flag-${flagId.replace(/[^\w-]/g, "-")}`;
}

const FIELD_LABELS: Record<string, string> = {
  name: "recipe-ingest.review.name",
  attribution: "recipe-ingest.review.attribution",
  description: "recipe-ingest.review.description",
  recipeYield: "recipe-ingest.review.yield",
  recipeServings: "recipe-ingest.review.servings",
  prepTime: "recipe-ingest.review.prep-time",
  performTime: "recipe-ingest.review.cook-time",
  totalTime: "recipe-ingest.review.total-time",
  ingredients: "recipe-ingest.review.ingredients",
  steps: "recipe-ingest.review.steps",
  notes: "recipe-ingest.review.notes",
  tags: "tag.tags",
  categories: "category.categories",
  tools: "tool.tools",
};

/** A field's label ("Name", "Step: 2", "Note 1", as the editor numbers them); none for the card as a whole */
export function fieldLabel(t: TranslateFn, field: string, line?: number | null): string | null {
  const key = normalizeField(field);
  if (key === "steps" && line !== null && line !== undefined) {
    return t("recipe.step-index", { step: line + 1 });
  }
  if (key === "notes" && line !== null && line !== undefined) {
    return t("recipe-ingest.review.note-number", { number: line + 1 });
  }
  return FIELD_LABELS[key] ? t(FIELD_LABELS[key]) : null;
}

/** A note's position in the draft by its id (a flag's or proposal's `ref`: the server keys notes by id); -1 when gone */
export function notePosition(draft: CardDraft, ref: string | null | undefined): number {
  return ref ? (draft.notes ?? []).findIndex(note => note.id === ref) : -1;
}

/** A note's text as one line of the "Needs a look" list and the re-read targets show it: its title above its text */
function noteText(note: CardDraftNote): string {
  return [note.title, note.text].filter(Boolean).join("\n");
}

/** The text a field (or one ingredient, step or note) holds now, as the "Needs a look" item shows it */
export function fieldText(draft: CardDraft, field: string, ref?: string | null): string {
  const key = normalizeField(field);
  if (key === "ingredients") {
    const ingredient = draft.ingredients?.find(item => item.referenceId === ref);
    if (!ingredient) {
      return "";
    }
    return isParsedIngredient(ingredient)
      ? ingredient.originalText || ingredient.display || ingredientDisplay(ingredient)
      : ingredient.note || ingredient.originalText || "";
  }
  if (key === "steps") {
    return draft.steps?.find(step => step.id === ref)?.text ?? "";
  }
  if (key === "notes") {
    // a note by its id; without one, every note (a flag on the notes as a whole)
    const notes = ref ? (draft.notes ?? []).filter(note => note.id === ref) : draft.notes ?? [];
    return notes.map(noteText).join("\n");
  }
  if (isTextField(key)) {
    const value = draft[key];
    return value === null || value === undefined ? "" : String(value);
  }
  return "";
}

// ==========================================
// Flags

export function isHighlighted(flag: CardFlag): boolean {
  return HIGHLIGHTED_SEVERITIES.includes(flag.severity);
}

/** An unresolved error or warning */
export function isOpenFlag(flag: CardFlag): boolean {
  return isHighlighted(flag) && !flag.resolution;
}

/** Whether "Keep as written" resolves the flag; warnings are resolved with "Looks right" */
export function canKeepFlag(flag: CardFlag): boolean {
  return flag.severity === "error" && KEEPABLE_KINDS.includes(flag.kind);
}

/**
 * The flags on a field: every flag on it when `ref` is left out, else only those on that ingredient or step.
 * Field names match whether the server writes them `prep_time` or `prepTime`.
 */
export function flagsForField(flags: readonly CardFlag[], field: string, ref?: string | null): CardFlag[] {
  const key = normalizeField(field);
  return flags.filter(flag => normalizeField(flag.field) === key && (ref === undefined || (flag.ref ?? null) === (ref ?? null)));
}

/** The worst unresolved severity among flags: what colour and icon a field gets */
export function fieldSeverity(flags: readonly CardFlag[]): "error" | "warning" | null {
  const open = flags.filter(isOpenFlag);
  if (open.some(flag => flag.severity === "error")) {
    return "error";
  }
  return open.length ? "warning" : null;
}

function stringParam(flag: CardFlag, name: string): string | null {
  const value = flag.params?.[name];
  if (typeof value === "string" && value.trim()) {
    return value;
  }
  return typeof value === "number" ? String(value) : null;
}

/**
 * The part of the line a flag is about, which an alternative or a typed value replaces: the marker, the unsure
 * reading, the number or the token. None for a second reading's disagreement, whose alternative is the whole line.
 */
export function flagFragment(flag: CardFlag): string | null {
  switch (flag.kind) {
    case "illegible":
      return MARKERS.illegible;
    case "blank":
      return flag.source === "cross_read" ? stringParam(flag, "value") : MARKERS.blank;
    case "unsure":
      return stringParam(flag, "text");
    case "read_disagreement":
      return null;
    case "shorthand_read":
      return stringParam(flag, "to");
    default:
      return stringParam(flag, "value") ?? stringParam(flag, "token");
  }
}

/** The readings offered as one-tap chips: the flag's alternatives, plus a suggested amount ("1 1/2" for "11/2") */
export function flagAlternatives(flag: CardFlag): string[] {
  const fragment = flagFragment(flag);
  const suggestion = stringParam(flag, "suggestion");
  const all = [...(flag.alternatives ?? []), ...(suggestion ? [suggestion] : [])];
  return [...new Set(all.map(item => item.trim()))].filter(item => item && item !== fragment);
}

/** Whether the flag gets a fill-in box: a marker (or a number a second reading saw as blank) the reviewer types over */
export function canFillFlag(flag: CardFlag): boolean {
  return (flag.kind === "blank" || flag.kind === "illegible") && flag.field !== "card" && !!flagFragment(flag);
}

/** A part of a text, as character offsets: `text.slice(start, end)` */
export interface TextSpan {
  start: number;
  end: number;
}

/**
 * Where the flagged part is, as the server found it (`params.start` and `params.end`): offsets into the text the flag
 * was computed on (a single field's, step's or note's text, an ingredient's card line). None when the flag has none.
 */
export function flagSpan(flag: CardFlag): TextSpan | null {
  const start = flag.params?.start;
  const end = flag.params?.end;
  if (typeof start !== "number" || typeof end !== "number" || !Number.isInteger(start) || !Number.isInteger(end)) {
    return null;
  }
  return start >= 0 && end > start ? { start, end } : null;
}

const FRACTION_GLYPHS = Object.keys(UNICODE_FRACTIONS).join("");
const DIGIT = new RegExp(`[\\d${FRACTION_GLYPHS}]`);
const LETTER_OR_DIGIT = /[\p{L}\p{N}]/u;

/**
 * Whether a fragment edge joins the character beside it (`outer`, with `beyond` past that) into a bigger token: a
 * digit edge joins digits ("2" in "12", "1" in "1½") and a separator before another digit ("2" in "1/2" or "1.5");
 * a letter edge joins letters and digits ("T" in "Tbsp"). A digit before letters still stands alone ("2" in "2T.").
 */
function joinsNeighbour(edge: string, outer: string | undefined, beyond: string | undefined): boolean {
  if (!outer) {
    return false;
  }
  if (DIGIT.test(edge)) {
    return DIGIT.test(outer) || (/[/.,]/.test(outer) && !!beyond && DIGIT.test(beyond));
  }
  return LETTER_OR_DIGIT.test(edge) && LETTER_OR_DIGIT.test(outer);
}

/** Whether `fragment` at `start` stands on its own in `text`: no number or word runs on from either end */
function isWholeToken(text: string, fragment: string, start: number): boolean {
  const end = start + fragment.length;
  return !joinsNeighbour(fragment[0]!, text[start - 1], text[start - 2])
    && !joinsNeighbour(fragment[fragment.length - 1]!, text[end], text[end + 1]);
}

/**
 * Whether the text at `span` is the fragment; a reading longer than three characters whatever its case, as the server
 * matches an `unsure` reading ("1/4 t." in "1/4 T. salt"), while "T" and "t" stay different units
 */
function fragmentAt(text: string, fragment: string, span: TextSpan): boolean {
  if (span.end > text.length || span.end - span.start !== fragment.length) {
    return false;
  }
  const there = text.slice(span.start, span.end);
  return there === fragment || (fragment.length > 3 && there.toLowerCase() === fragment.toLowerCase());
}

/**
 * Where the flagged part is in `text`: the server's position (`span`) when the text there still is the fragment
 * (`fragmentAt`), else the first occurrence that stands on its own, so "2" never matches inside "1/2" or "12".
 * None when the text doesn't hold it.
 */
export function findFragment(text: string, fragment: string | null | undefined, span?: TextSpan | null): TextSpan | null {
  if (!fragment) {
    return null;
  }
  if (span && fragmentAt(text, fragment, span)) {
    return { start: span.start, end: span.end };
  }
  for (let index = text.indexOf(fragment); index >= 0; index = text.indexOf(fragment, index + 1)) {
    if (isWholeToken(text, fragment, index)) {
      return { start: index, end: index + fragment.length };
    }
  }
  return null;
}

function replaceFragment(text: string, fragment: string, replacement: string, span?: TextSpan | null): string | null {
  const found = findFragment(text, fragment, span);
  return found ? text.slice(0, found.start) + replacement + text.slice(found.end) : null;
}

/**
 * The text with one of a flag's alternatives applied: it replaces the flagged part when the text holds it (at the
 * flag's position when that still holds it), else the alternative is the whole new text (a second reading's line, or
 * a reading that has changed since). `span` is where to look, by default the flag's own position; pass null for text
 * other than the one the flag was computed on (a parsed line's note).
 */
export function applyAlternative(text: string, flag: CardFlag, alternative: string, span: TextSpan | null = flagSpan(flag)): string {
  const fragment = flagFragment(flag);
  return (fragment ? replaceFragment(text, fragment, alternative, span) : null) ?? alternative;
}

/**
 * The text with a typed value in place of the marker (or flagged part, at `span` when the text still holds it there);
 * unchanged when the value is empty or the marker is already gone
 */
export function fillBlank(text: string, value: string, fragment: string = MARKERS.blank, span: TextSpan | null = null): string {
  const typed = value.trim();
  if (!typed) {
    return text;
  }
  return replaceFragment(text, fragment, typed, span) ?? text;
}

export interface TextSegment {
  text: string;
  mark: boolean;
}

/**
 * The text cut around the flagged part, for highlighting it: the occurrence at `span` (where the flag says it is) when
 * the text still holds it there, else the first that stands on its own (`findFragment`). A blank shows as "___".
 */
export function highlightSegments(text: string, fragment: string | null, span: TextSpan | null = null): TextSegment[] {
  const show = (part: string) => part.split(MARKERS.blank).join("___");
  const found = findFragment(text, fragment, span);
  if (!found) {
    return text ? [{ text: show(text), mark: false }] : [];
  }
  return [
    { text: show(text.slice(0, found.start)), mark: false },
    { text: show(text.slice(found.start, found.end)), mark: true },
    { text: show(text.slice(found.end)), mark: false },
  ].filter(segment => segment.text);
}

/**
 * Where a part of a parsed line (its note, unit or food name) sits on the card's line, when it covers `span`: the
 * span within that part. None when the part isn't written on the line around it.
 */
function spanInPart(line: string, part: string | null | undefined, span: TextSpan): TextSpan | null {
  if (!part) {
    return null;
  }
  for (let index = line.indexOf(part); index >= 0; index = line.indexOf(part, index + 1)) {
    if (index <= span.start && span.end <= index + part.length) {
      return { start: span.start - index, end: span.end - index };
    }
  }
  return null;
}

/**
 * An ingredient with a flag's fix applied. A line kept as text changes in its note. A parsed line changes in the
 * part that holds the flagged text (note, amount, unit or food: an edited unit or food is linked again by name at
 * commit): the part written where the flag points on the card's line, else the first holding it as a whole token.
 * When no part holds it, the fixed line is kept as text.
 */
export function fixIngredient(
  ingredient: CardDraftIngredient,
  flag: CardFlag,
  replacement: string,
  mode: "alternative" | "fill",
): CardDraftIngredient {
  const fragment = flagFragment(flag) ?? (mode === "fill" ? MARKERS.blank : null);
  const fix = (text: string, span: TextSpan | null) => (mode === "fill"
    ? fillBlank(text, replacement, fragment ?? MARKERS.blank, span)
    : applyAlternative(text, flag, replacement, span));
  const fixName = (named: CardDraftRef, span: TextSpan | null) => ({ id: null, name: fix(named.name ?? "", span).trim() });

  if (!isParsedIngredient(ingredient)) {
    return withDisplay({ ...ingredient, note: fix(ingredient.note || ingredient.originalText || "", flagSpan(flag)) });
  }
  if (fragment) {
    const amount = parseQuantity(fragment);
    const newAmount = parseQuantity(replacement);
    const isAmount = amount !== null && newAmount !== null && amount === ingredient.quantity;

    // where the flag points on the card's line: the part written there, else the amount
    const line = ingredient.originalText ?? "";
    const onLine = flagSpan(flag);
    if (onLine && fragmentAt(line, fragment, onLine)) {
      const inNote = spanInPart(line, ingredient.note, onLine);
      if (inNote) {
        return withDisplay({ ...ingredient, note: fix(ingredient.note ?? "", inNote) });
      }
      const inUnit = ingredient.unit ? spanInPart(line, ingredient.unit.name, onLine) : null;
      if (ingredient.unit && inUnit) {
        return withDisplay({ ...ingredient, unit: fixName(ingredient.unit, inUnit) });
      }
      const inFood = ingredient.food ? spanInPart(line, ingredient.food.name, onLine) : null;
      if (ingredient.food && inFood) {
        return withDisplay({ ...ingredient, food: fixName(ingredient.food, inFood) });
      }
      if (isAmount) {
        return withDisplay({ ...ingredient, quantity: newAmount });
      }
    }

    // no position to go by: the first part holding it
    if (ingredient.note && findFragment(ingredient.note, fragment)) {
      return withDisplay({ ...ingredient, note: fix(ingredient.note, null) });
    }
    if (isAmount) {
      return withDisplay({ ...ingredient, quantity: newAmount });
    }
    if (ingredient.unit && findFragment(ingredient.unit.name ?? "", fragment)) {
      return withDisplay({ ...ingredient, unit: fixName(ingredient.unit, null) });
    }
    if (ingredient.food && findFragment(ingredient.food.name ?? "", fragment)) {
      return withDisplay({ ...ingredient, food: fixName(ingredient.food, null) });
    }
  }
  // the card's line holds the flag's position; a line built from the fields doesn't
  const line = ingredient.originalText
    ? fix(ingredient.originalText, flagSpan(flag))
    : fix(ingredientDisplay(ingredient), null);
  return withDisplay({ ...ingredient, quantity: null, unit: null, food: null, note: line });
}

/** The line kept as written on the card, with no amount, unit or food: what "Keep as text" makes of it */
export function ingredientAsText(ingredient: CardDraftIngredient): CardDraftIngredient {
  const text = ingredient.originalText || ingredientDisplay(ingredient);
  return withDisplay({ ...ingredient, quantity: null, unit: null, food: null, note: text });
}

/** What the parser made of a line, as "Check this ingredient" shows it: "2 cup flour, to 3" */
export function parsedReading(ingredient: CardDraftIngredient): string {
  const fields = [formatQuantity(ingredient.quantity), ingredient.unit?.name, ingredient.food?.name]
    .map(part => (part ?? "").trim())
    .filter(Boolean)
    .join(" ");
  return [fields, (ingredient.note ?? "").trim()].filter(Boolean).join(", ");
}

/**
 * What a `check_parse` flag's `params.value` says the amount, unit and food lost, and whether the note keeps it:
 * a range's end ("2-3" read as 2: `range`, with `end` "3"), a second amount ("+ 2 T.", the "10 3/4" of "1 can
 * (10 3/4 oz.) soup", a second ingredient run into the line: `amount`), or a size word read into the unit or food
 * ("cup scant": `in-name`). `kept`: the note holds it, as parsing keeps it there (a line parsed before that doesn't).
 */
export type ParseLoss
  = | { kind: "range"; value: string; end: string; kept: boolean }
    | { kind: "amount"; value: string; kept: boolean }
    | { kind: "in-name"; value: string };

const RANGE_SEPARATOR = /\s*(?:-|–|—|\bto\b)\s*/g;

/** The `ParseLoss` of a `check_parse` flag on this ingredient; none for other flags, or one without a value */
export function parseLoss(flag: CardFlag, ingredient: CardDraftIngredient | null | undefined): ParseLoss | null {
  const value = stringParam(flag, "value")?.trim();
  if (flag.kind !== "check_parse" || !value || !ingredient) {
    return null;
  }
  if (!/\d/.test(value)) {
    return { kind: "in-name", value };
  }
  const note = ingredient.note ?? "";
  for (const separator of value.matchAll(RANGE_SEPARATOR)) {
    const start = value.slice(0, separator.index);
    const end = value.slice(separator.index + separator[0].length);
    const first = parseQuantity(start);
    if (first !== null && parseQuantity(end) !== null && first === ingredient.quantity) {
      return { kind: "range", value, end, kept: !!findFragment(note, end) };
    }
  }
  return { kind: "amount", value, kept: !!findFragment(note, value) };
}

/**
 * Applies a change to the text a flag points at, in place. Returns whether the draft changed (a flag on the card as
 * a whole, or on an ingredient or step that's gone, points at nothing).
 */
export function editFlaggedText(
  draft: ReviewDraft,
  flag: CardFlag,
  replacement: string,
  mode: "alternative" | "fill",
): boolean {
  const field = normalizeField(flag.field);
  const fragment = flagFragment(flag) ?? MARKERS.blank;
  // the flag's position is in the text it was computed on: a field's, step's or note's text, not a note's title
  const span = flagSpan(flag);
  const fix = (text: string, at: TextSpan | null = span) => (mode === "fill"
    ? fillBlank(text, replacement, fragment, at)
    : applyAlternative(text, flag, replacement, at));

  if (field === "ingredients") {
    const index = draft.ingredients.findIndex(item => item.referenceId === flag.ref);
    if (index < 0) {
      return false;
    }
    const fixed = fixIngredient(draft.ingredients[index]!, flag, replacement, mode);
    const changed = stableStringify(fixed) !== stableStringify(draft.ingredients[index]);
    draft.ingredients.splice(index, 1, fixed);
    return changed;
  }
  if (field === "steps") {
    const step = draft.steps.find(item => item.id === flag.ref);
    if (!step) {
      return false;
    }
    const text = fix(step.text ?? "");
    const changed = text !== step.text;
    step.text = text;
    return changed;
  }
  if (field === "notes") {
    // the flag's note by its id, wherever it is now; without one, the first note holding the flagged part
    const note = flag.ref
      ? draft.notes.find(item => item.id === flag.ref)
      : draft.notes.find(item => findFragment(item.text ?? "", fragment) || findFragment(item.title ?? "", fragment));
    if (!note) {
      return false;
    }
    // the marker can be in the note's title (the server checks both); otherwise the text changes
    const part = !findFragment(note.text ?? "", fragment) && findFragment(note.title ?? "", fragment) ? "title" : "text";
    const text = fix(note[part] ?? "", part === "text" ? span : null);
    const changed = text !== (note[part] ?? "");
    note[part] = text;
    return changed;
  }
  if (field === "recipeServings") {
    const value = parseQuantity(fix(draft.recipeServings === null || draft.recipeServings === undefined ? "" : String(draft.recipeServings)));
    const changed = value !== (draft.recipeServings ?? null);
    draft.recipeServings = value;
    return changed;
  }
  if (isTextField(field)) {
    const current = draft[field];
    const text = fix(current === null || current === undefined ? "" : String(current));
    const changed = text !== (current ?? "");
    setTextField(draft, field, text);
    return changed;
  }
  return false;
}

/** Where a flag sits in the card's reading order: field, then line, then errors before warnings */
function readingOrderKey(flag: CardFlag, draft: CardDraft): [number, number, number] {
  const field = normalizeField(flag.field);
  const fieldIndex = FIELD_ORDER.indexOf(field);
  let line = 0;
  if (field === "ingredients") {
    line = draft.ingredients?.findIndex(item => item.referenceId === flag.ref) ?? -1;
  }
  else if (field === "steps") {
    line = draft.steps?.findIndex(item => item.id === flag.ref) ?? -1;
  }
  else if (field === "notes") {
    line = flag.ref ? notePosition(draft, flag.ref) : 0;
  }
  const severity = flag.severity === "error" ? 0 : flag.severity === "warning" ? 1 : 2;
  return [fieldIndex < 0 ? FIELD_ORDER.length : fieldIndex, line < 0 ? Number.MAX_SAFE_INTEGER : line, severity];
}

/** Flags in the card's reading order (a stable sort) */
export function sortFlags<T extends CardFlag>(flags: readonly T[], draft: CardDraft): T[] {
  return flags
    .map((flag, position) => ({ flag, key: [...readingOrderKey(flag, draft), position] }))
    .sort((a, b) => {
      for (let i = 0; i < a.key.length; i++) {
        if (a.key[i] !== b.key[i]) {
          return a.key[i]! - b.key[i]!;
        }
      }
      return 0;
    })
    .map(entry => entry.flag);
}

export type NeedsALookState = "open" | "resolved" | "fixed";

export interface NeedsALookItem {
  flag: CardFlag;
  /** `open`: still to check; `resolved`: kept or dismissed (can be undone); `fixed`: the problem is gone */
  state: NeedsALookState;
  /** The field in camelCase */
  field: string;
  /** The ingredient's or step's position (from 0), for list fields */
  line: number | null;
  /** The line as it reads now, the part the flag is about, and where in the line that part is (none when it isn't) */
  text: string;
  fragment: string | null;
  span: TextSpan | null;
  /** The ingredient, for a flag on one: what the parser made of the line */
  ingredient: CardDraftIngredient | null;
  /** Re-read results for this line, shown inside the item */
  proposals: CardProposal[];
  anchor: string;
}

/**
 * A flag's position in the text its item shows (`fieldText`): a note's item shows its title above its text, and the
 * flag's position is in the text
 */
function spanInFieldText(draft: CardDraft, field: string, flag: CardFlag): TextSpan | null {
  const span = flagSpan(flag);
  if (!span || field !== "notes") {
    return span;
  }
  const note = flag.ref ? draft.notes?.find(item => item.id === flag.ref) : undefined;
  if (!note?.text) {
    return null;
  }
  const offset = note.title ? note.title.length + 1 : 0;
  return { start: span.start + offset, end: span.end + offset };
}

function sameTarget(target: ProposalTarget | null | undefined, field: string, ref: string | null | undefined) {
  return !!target && normalizeField(target.field) === normalizeField(field) && (target.ref ?? null) === (ref ?? null);
}

/**
 * The "Needs a look" list: one item per error or warning seen on this card, in reading order. Flags the server no
 * longer raises, or that the reviewer fixed with one tap since the last save, are `fixed`. Region re-reads go inside
 * the item for their line; the rest (and whole-card re-reads) are returned for the proposal banner.
 */
export function buildNeedsALook(
  seen: readonly CardFlag[],
  current: readonly CardFlag[],
  fixed: ReadonlySet<string>,
  draft: CardDraft,
  proposals: readonly CardProposal[],
): { items: NeedsALookItem[]; otherProposals: CardProposal[] } {
  const byId = new Map(current.map(flag => [flag.id, flag]));
  const items: NeedsALookItem[] = sortFlags(seen.filter(isHighlighted), draft).map((seenFlag) => {
    const flag = byId.get(seenFlag.id) ?? seenFlag;
    const state: NeedsALookState = !byId.has(seenFlag.id) || fixed.has(seenFlag.id)
      ? "fixed"
      : flag.resolution ? "resolved" : "open";
    const field = normalizeField(flag.field);
    let line: number | null = null;
    if (field === "ingredients") {
      line = draft.ingredients?.findIndex(item => item.referenceId === flag.ref) ?? -1;
    }
    else if (field === "steps") {
      line = draft.steps?.findIndex(item => item.id === flag.ref) ?? -1;
    }
    else if (field === "notes") {
      line = flag.ref ? notePosition(draft, flag.ref) : null;
    }
    const ingredient = field === "ingredients" && line !== null && line >= 0 ? draft.ingredients![line]! : null;
    const text = fieldText(draft, field, flag.ref);
    const fragment = flagFragment(flag);
    return {
      flag,
      state,
      field,
      line: line !== null && line < 0 ? null : line,
      text,
      fragment,
      span: findFragment(text, fragment, spanInFieldText(draft, field, flag)),
      ingredient,
      proposals: [],
      anchor: flagAnchorId(flag.id),
    };
  });

  const otherProposals: CardProposal[] = [];
  for (const proposal of proposals) {
    const item = proposal.kind === "region"
      ? items.find(entry => entry.state !== "fixed" && sameTarget(proposal.target, entry.field, entry.flag.ref))
      : undefined;
    if (item) {
      item.proposals.push(proposal);
    }
    else {
      otherProposals.push(proposal);
    }
  }
  return { items, otherProposals };
}

// ==========================================
// Proposals

/** A new ingredient line from a re-read: the parsed line the server sent, else the text kept as written */
function proposedIngredient(proposal: CardProposal, referenceId: string): CardDraftIngredient {
  const parsed = proposal.draft?.ingredients?.[0];
  if (parsed) {
    return withDisplay({ ...normalizeDraft({ ingredients: [parsed] }).ingredients[0]!, referenceId });
  }
  const text = proposal.text ?? "";
  return withDisplay({ referenceId, originalText: text, quantity: null, unit: null, food: null, note: text, display: "" });
}

/**
 * Applies a re-read to the draft, in place: `replace` puts the reading in place of the target's text (or line);
 * `append` adds it to the end of that text, or as a new line after the target ingredient. A target without a line
 * (`ingredients`, `steps` or `notes` alone) adds a new one. A whole-card re-read replaces the draft.
 */
export function applyProposal(draft: ReviewDraft, proposal: CardProposal, mode: "replace" | "append"): ReviewDraft {
  if (proposal.kind === "full") {
    return proposal.draft ? normalizeDraft(proposal.draft) : draft;
  }
  const target = proposal.target;
  const text = (proposal.text ?? "").trim();
  if (!target || (!text && !proposal.draft?.ingredients?.length)) {
    return draft;
  }
  const field = normalizeField(target.field);
  const join = (current: string | null | undefined, separator = " ") => (mode === "append" && current ? `${current}${separator}${text}` : text);

  if (field === "ingredients") {
    const index = draft.ingredients.findIndex(item => item.referenceId === target.ref);
    if (index < 0) {
      draft.ingredients.push(proposedIngredient(proposal, uuid4()));
    }
    else if (mode === "append") {
      draft.ingredients.splice(index + 1, 0, proposedIngredient(proposal, uuid4()));
    }
    else {
      const current = draft.ingredients[index]!;
      draft.ingredients.splice(index, 1, { ...proposedIngredient(proposal, current.referenceId!), title: current.title });
    }
  }
  else if (field === "steps") {
    const step = draft.steps.find(item => item.id === target.ref);
    if (step) {
      step.text = join(step.text);
    }
    else {
      draft.steps.push({ id: uuid4(), title: null, text });
    }
  }
  else if (field === "notes") {
    const note = target.ref ? draft.notes.find(item => item.id === target.ref) : undefined;
    if (note) {
      note.text = join(note.text);
    }
    else {
      draft.notes.push({ id: uuid4(), title: "", text });
    }
  }
  else if (field === "recipeServings") {
    draft.recipeServings = parseQuantity(text) ?? draft.recipeServings ?? null;
  }
  else if (isTextField(field)) {
    setTextField(draft, field, join(fieldText(draft, field), field === "description" ? "\n" : " "));
  }
  return draft;
}

/** The fields a re-read can be for, in reading order: what the region dialog offers */
export interface RereadTargetOption {
  /** A key unique among the options */
  value: string;
  target: ProposalTarget;
  /** "name", "ingredient", "step", "note", "new-ingredient", ...: which label the dialog shows */
  kind: TextField | "ingredient" | "step" | "note" | "new-ingredient" | "new-step" | "new-note";
  /** The line's text, the step's number (from 1), or the note's title (else its first words) */
  text: string;
}

/** How much of a note's text names it among the re-read targets, when it has no title */
const NOTE_LABEL_LENGTH = 36;

/** A note as the region dialog names it: its title, else the start of its text */
function noteLabel(note: CardDraftNote): string {
  const title = (note.title ?? "").trim();
  if (title) {
    return title;
  }
  const text = (note.text ?? "").trim().replace(/\s+/g, " ");
  return text.length > NOTE_LABEL_LENGTH ? `${text.slice(0, NOTE_LABEL_LENGTH).trimEnd()}…` : text;
}

/**
 * Every line a re-read can be for: each field, each ingredient, step and note by its `ref`, and a new ingredient,
 * step or note (a target without a `ref`, for a line the reading missed), which `applyProposal` adds.
 */
export function rereadTargets(draft: ReviewDraft): RereadTargetOption[] {
  const options: RereadTargetOption[] = TEXT_FIELDS.filter(field => field !== "recipeServings").map(field => ({
    value: field,
    target: { field, ref: null },
    kind: field,
    text: fieldText(draft, field),
  }));
  draft.ingredients.forEach((ingredient) => {
    options.push({
      value: `ingredients:${ingredient.referenceId}`,
      target: { field: "ingredients", ref: ingredient.referenceId ?? null },
      kind: "ingredient",
      text: ingredient.display || ingredient.originalText || ingredientDisplay(ingredient),
    });
  });
  options.push({ value: "ingredients:new", target: { field: "ingredients", ref: null }, kind: "new-ingredient", text: "" });
  draft.steps.forEach((step, index) => {
    options.push({
      value: `steps:${step.id}`,
      target: { field: "steps", ref: step.id ?? null },
      kind: "step",
      text: String(index + 1),
    });
  });
  options.push({ value: "steps:new", target: { field: "steps", ref: null }, kind: "new-step", text: "" });
  draft.notes.forEach((note) => {
    options.push({
      value: `notes:${note.id}`,
      target: { field: "notes", ref: note.id ?? null },
      kind: "note",
      text: noteLabel(note),
    });
  });
  options.push({ value: "notes:new", target: { field: "notes", ref: null }, kind: "new-note", text: "" });
  return options;
}

/** The option for a target (a flag's field and line), falling back to the field as a whole */
export function rereadTargetValue(options: readonly RereadTargetOption[], field: string, ref?: string | null): string | null {
  const key = normalizeField(field);
  const exact = options.find(option => normalizeField(option.target.field) === key && (option.target.ref ?? null) === (ref ?? null));
  return exact?.value ?? options.find(option => normalizeField(option.target.field) === key)?.value ?? null;
}

// ==========================================
// Crop regions

export interface CropResultLike {
  coordinates: { left: number; top: number; width: number; height: number };
  image: { width: number; height: number };
}

export interface PageRegion {
  x: number;
  y: number;
  width: number;
  height: number;
}

const clamp01 = (value: number) => Math.min(1, Math.max(0, value));
// four decimals, rounded down (so the region stays on the page), ignoring floating-point noise (153.6 / 1536)
const floor4 = (value: number) => Math.floor(value * 10000 + 1e-6) / 10000;

/**
 * A cropper selection as fractions of the upright page, as `POST …/reread` takes it (`x + width ≤ 1`,
 * `y + height ≤ 1`). None when there's no image or a side is under `minSide`.
 */
export function regionFromCropResult(result: CropResultLike | null | undefined, minSide = MIN_REGION_SIDE): PageRegion | null {
  const imageWidth = result?.image?.width ?? 0;
  const imageHeight = result?.image?.height ?? 0;
  if (!result || imageWidth <= 0 || imageHeight <= 0) {
    return null;
  }
  const { left, top, width, height } = result.coordinates;
  const x = floor4(clamp01(left / imageWidth));
  const y = floor4(clamp01(top / imageHeight));
  const region = {
    x,
    y,
    width: floor4(Math.min(clamp01(width / imageWidth), 1 - x)),
    height: floor4(Math.min(clamp01(height / imageHeight), 1 - y)),
  };
  return region.width >= minSide && region.height >= minSide ? region : null;
}

/** How far an arrow key moves or resizes the re-read selection, as a fraction of the page */
export const REGION_KEY_STEP = 0.02;

/** A cropper selection in the page's pixels */
export type RegionCoordinates = CropResultLike["coordinates"];

/**
 * The selection after an arrow key (docs/ai/PHASE2.md §6.5), in the page's pixels: the arrows move it by
 * `REGION_KEY_STEP` of the page; with `resize`, Right and Down make it wider and taller, Left and Up narrower and
 * shorter, never under twice the smallest region. It stays on the page. None for any other key.
 */
export function nudgeRegion(
  coordinates: RegionCoordinates,
  image: { width: number; height: number },
  key: string,
  resize: boolean,
): RegionCoordinates | null {
  const dx = key === "ArrowLeft" ? -1 : key === "ArrowRight" ? 1 : 0;
  const dy = key === "ArrowUp" ? -1 : key === "ArrowDown" ? 1 : 0;
  if ((!dx && !dy) || image.width <= 0 || image.height <= 0) {
    return null;
  }
  const between = (value: number, low: number, high: number) => Math.min(Math.max(value, low), Math.max(low, high));
  const { left, top, width, height } = coordinates;
  if (resize) {
    const minWidth = Math.min(image.width, image.width * MIN_REGION_SIDE * 2);
    const minHeight = Math.min(image.height, image.height * MIN_REGION_SIDE * 2);
    return {
      left,
      top,
      width: between(width + dx * image.width * REGION_KEY_STEP, minWidth, image.width - left),
      height: between(height + dy * image.height * REGION_KEY_STEP, minHeight, image.height - top),
    };
  }
  return {
    left: between(left + dx * image.width * REGION_KEY_STEP, 0, image.width - width),
    top: between(top + dy * image.height * REGION_KEY_STEP, 0, image.height - height),
    width,
    height,
  };
}

// ==========================================
// Batches

function inReviewOrder(jobs: readonly RecipeIngestionBatchJob[]): RecipeIngestionBatchJob[] {
  // a stable sort: the server already orders ties by arrival
  return [...jobs].sort((a, b) => a.position - b.position);
}

/**
 * Where a batch's review starts (docs/ai/PHASE2.md §6.1): the first ready card, in capture order, with an unresolved
 * error or warning, else the first ready card. None when no card is ready (the page then shows the queue).
 */
export function firstCardToReview(jobs: readonly RecipeIngestionBatchJob[]): string | null {
  const ready = inReviewOrder(jobs).filter(job => job.status === "ready");
  return (ready.find(job => (job.errorCount ?? 0) + (job.warningCount ?? 0) > 0) ?? ready[0])?.id ?? null;
}

/** The batch's next ready card after this one in capture order, wrapping round to cards skipped earlier */
export function nextCardInBatch(jobs: readonly RecipeIngestionBatchJob[], currentId: string): string | null {
  const ordered = inReviewOrder(jobs);
  const index = ordered.findIndex(job => job.id === currentId);
  const rotated = index < 0 ? ordered : [...ordered.slice(index + 1), ...ordered.slice(0, index)];
  return rotated.find(job => job.status === "ready" && job.id !== currentId)?.id ?? null;
}

/**
 * Where the review goes once a batch has no ready card left: the batch, among the others with ready cards, whose
 * oldest ready card came first, starting at the card its review would start at. None when no other batch has one.
 */
export function nextBatchCard(
  jobs: readonly RecipeIngestionJobSummary[],
  batchId: string | null | undefined,
  currentId: string,
): string | null {
  const batches = new Map<string, RecipeIngestionJobSummary[]>();
  for (const job of jobs) {
    if (job.status === "ready" && job.batchId !== batchId && job.id !== currentId) {
      batches.set(job.batchId, [...(batches.get(job.batchId) ?? []), job]);
    }
  }
  const arrived = (job: RecipeIngestionJobSummary) => {
    const time = Date.parse(job.createdAt ?? "");
    return Number.isNaN(time) ? Number.POSITIVE_INFINITY : time;
  };
  const oldest = (list: RecipeIngestionJobSummary[]) => Math.min(...list.map(arrived));
  const [first] = [...batches.values()].sort((a, b) => {
    const [x, y] = [oldest(a), oldest(b)];
    return x < y ? -1 : x > y ? 1 : 0;
  });
  return first ? firstCardToReview(first) : null;
}

// ==========================================
// Merging a card into the previous one

/** The server's `MAX_PAGES_PER_CARD`, until the group's card settings (which carry it) have loaded */
const DEFAULT_MAX_PAGES_PER_CARD = 4;

/**
 * Why a card can't become the back of the previous card of its batch ("Add as back of previous card"): `busy` while
 * the card itself is being read, `checking` while the previous card isn't known yet, `no-previous` for a batch's
 * first card, `previous-added` once it was added as a recipe, `previous-busy` while it's being read,
 * `previous-not-allowed` when the user may not change it (another member's card), `too-many-pages` when the two
 * together have more photos than a card takes.
 */
export type MergeBlock
  = | "busy"
    | "checking"
    | "no-previous"
    | "previous-added"
    | "previous-busy"
    | "previous-not-allowed"
    | "too-many-pages";

/**
 * Whether this card can be added to the previous one as its back, as the server's merge checks it: the previous card
 * (`undefined` while unknown, `null` when there is none) ready or failed with nothing reading it, one the user may
 * change (its `canMerge`), and the photos of both fitting one card. Null when it can.
 */
export function mergeBlockOf(
  card: Pick<RecipeIngestionJobOut, "pageCount">,
  previous: Pick<RecipeIngestionJobOut, "status" | "task" | "pageCount" | "permissions"> | null | undefined,
  maxPages: number,
): MergeBlock | null {
  if (previous === undefined) {
    return "checking";
  }
  if (previous === null) {
    return "no-previous";
  }
  if (previous.status === "committed" || previous.status === "committing") {
    return "previous-added";
  }
  if (previous.status === "processing" || previous.task) {
    return "previous-busy";
  }
  if (previous.pageCount + card.pageCount > maxPages) {
    return "too-many-pages";
  }
  return previous.permissions?.canMerge ? null : "previous-not-allowed";
}

/** A card the review opens next: the batch's next ready card, or another batch's ("next-batch") */
interface CardStop {
  kind: "card" | "next-batch";
  id: string;
}

/** The queue filtered to the batch, with how many of its cards are still being read */
interface QueueStop {
  kind: "queue";
  processing: number;
}

/**
 * Where the review goes after a card (docs/ai/PHASE2.md §6.1): the batch's next ready card; else, while cards of
 * the batch are still being read, the queue filtered to the batch; else another batch's ready card; else the queue.
 */
export type NextStop = CardStop | QueueStop;

// ==========================================
// Notices

export type ReviewNoticeKind = "success" | "info" | "warning" | "error";

/** What a notice says: all of it but its action, which is what can be carried to the next card's page */
export interface ReviewNoticeText {
  kind: ReviewNoticeKind;
  text: string;
  /** A second line: what commit left out */
  detail?: string | null;
  /** The card this notice says was added: the page that shows it offers Undo, which takes it back to review */
  undoJobId?: string | null;
}

/** A notice in the review bar (docs/ai/PHASE2.md §6.2), in place of a toast that would cover the page */
export interface ReviewNotice extends ReviewNoticeText {
  /** New for every notice, so the same words said twice start their time again */
  id: number;
  /** One button beside the notice ("Read whole card again") */
  action?: { label: string; run: () => unknown } | null;
}

/** A notice carried to the next card's page is about that visit only */
const CARRIED_NOTICE_MAX_AGE_MS = 10_000;
let carriedNotice: { jobId: string; notice: ReviewNoticeText; at: number } | null = null;

/** Leaves a notice for the card the review opens next ("Added Banana Mug Cake"), which shows it in its review bar */
export function carryReviewNotice(jobId: string, notice: ReviewNoticeText) {
  carriedNotice = { jobId, notice: { ...notice }, at: Date.now() };
}

/** The notice left for this card, once, while it's fresh */
export function takeCarriedReviewNotice(jobId: string): ReviewNoticeText | null {
  const left = carriedNotice;
  if (!left || left.jobId !== jobId) {
    return null;
  }
  carriedNotice = null;
  return Date.now() - left.at <= CARRIED_NOTICE_MAX_AGE_MS ? left.notice : null;
}

/** Forgets a carried notice (on logout, through `resetRecipeIngestState`, and between tests) */
export function resetCarriedReviewNotice() {
  carriedNotice = null;
}

// ==========================================
// Eval cases

/** An eval case's name (the server's `EVAL_CASE_SLUG_PATTERN`) */
export const EVAL_CASE_SLUG = /^[a-z0-9][a-z0-9-]{0,63}$/;
/** What a reviewer can say the card is like, in the order the dialog offers them (the server's `EvalCaseTag`) */
export const EVAL_CASE_TAGS: readonly EvalCaseTag[] = ["handwritten", "printed", "faded"];
/** The longest eval case notes (the server's `MAX_EVAL_NOTES`) */
export const MAX_EVAL_NOTES = 2000;

/** An eval case name made from the recipe's name: "Banana Mug Cake" → "banana-mug-cake" */
export function suggestEvalSlug(name: string | null | undefined): string {
  return (name ?? "")
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+/, "")
    .slice(0, 64)
    .replace(/-+$/, "");
}

// ==========================================
// The page's state

export type SaveState = "idle" | "saving" | "saved" | "error";
export type LoadState = "loading" | "ready" | "not-found" | "failed";

export interface RecipeIngestReviewOptions {
  groupSlug: MaybeRefOrGetter<string>;
  /** Leaves the page (the review page replaces its route, so Back returns to the queue) */
  navigate: (path: string) => unknown;
}

const ACTIVE_STATUSES: readonly IngestStatus[] = ["processing", "committing"];

/** Whether the axios interceptor already toasted the error's translated `detail.message` */
function alreadyToasted(error: unknown): boolean {
  const detail = (error as { response?: { data?: { detail?: unknown } } } | null)?.response?.data?.detail;
  return typeof (detail as { message?: unknown } | null | undefined)?.message === "string";
}

/** How a Back to review ended: `edited` asks first, as the recipe was edited after the commit */
export type UncommitOutcome = "done" | "edited" | "failed";

/** How a save ended: `retry` (no answer, or the server failed) is tried again later; `refused` means the card was
 * committed, discarded or taken back somewhere else */
type SaveOutcome = "saved" | "conflict" | "refused" | "retry" | "failed";

/**
 * The review page's state for one job. Edits to `draft` autosave after `AUTOSAVE_DELAY_MS` with the draft version;
 * a save that gets no answer (or a server error) is tried again after `SAVE_RETRY_MS` while the page is open; a
 * stale version sets `conflict` (the page's "Reload this card" dialog). While a task runs the page polls the job's
 * state every `STATE_POLL_MS`, picks up proposals and a replaced draft, and sends queued re-reads one at a time
 * once the job is idle. What the page has to say goes to `notice`, which the review bar shows.
 */
export function useRecipeIngestReview(jobId: string, options: RecipeIngestReviewOptions) {
  const api = useUserApi();
  const i18n = useI18n();
  const text = useRecipeIngestText();
  const counts = useRecipeIngestCounts();
  const ingestSettings = useRecipeIngestSettings();

  const job = ref<RecipeIngestionJobOut | null>(null);
  const batch = ref<RecipeIngestionBatchOut | null>(null);
  const loadState = ref<LoadState>("loading");
  const draft = ref<ReviewDraft>(normalizeDraft(null));
  const draftVersion = ref(0);
  const flags = ref<CardFlag[]>([]);
  const proposals = ref<CardProposal[]>([]);
  const saveState = ref<SaveState>("idle");
  const conflict = ref(false);
  const committing = ref(false);
  /** The action in flight (`rotate`, `reextract`, `retry`, `cloud`, `discard`, `eval`, ...), so its button can spin */
  const pendingAction = ref<string | null>(null);
  /** Re-reads waiting for the job to be idle, sent one at a time */
  const rereadQueue = ref<RereadRequest[]>([]);
  /** What the review bar says ("Re-read queued", "Added Banana Mug Cake" from the last card) */
  const notice = ref<ReviewNotice | null>(null);
  let unmounted = false;

  /** What the server last stored, so only real changes are saved */
  const savedDraft = ref(stableStringify(draft.value));
  const pendingResolutions = new Map<string, FlagResolution | null>();
  const pendingProposalIds = new Set<string>();
  /** Proposals used or dismissed here, which a refresh mustn't bring back before the save that removes them */
  const handledProposalIds = new Set<string>();
  let pendingClearError = false;
  let saving: Promise<void> | null = null;
  /** Bumped by every save as it goes out, so a read can tell whether a save overlapped it */
  let saveSeq = 0;
  /** The last save that dismissed the error banner */
  let clearedErrorSeq = 0;
  let fixCounter = 0;
  /** Flags fixed with one tap since the last save, by the fix's sequence number */
  const fixedFlags = ref(new Map<string, number>());
  /** Every error and warning seen on this card, in the order first seen: the "Needs a look" list */
  const seenFlags = ref<CardFlag[]>([]);
  let resetSeen = false;

  const isDirty = computed(() => stableStringify(draft.value) !== savedDraft.value);
  const task = computed(() => job.value?.task ?? null);
  const status = computed(() => job.value?.status ?? null);
  const readOnly = computed(() =>
    status.value !== "ready" || task.value?.kind === "extract" || conflict.value || committing.value,
  );

  /**
   * Whether commit attaches the card's photos to the recipe: the draft's switch, else the household's default
   * (`cardPhotoDefault`, off where new recipes are public). Turning the switch stores the choice in the draft.
   */
  const attachCardPhoto = computed<boolean>({
    get: () => draft.value.attachCardPhoto ?? job.value?.cardPhotoDefault ?? true,
    set: (value) => {
      if (!readOnly.value) {
        draft.value.attachCardPhoto = value;
      }
    },
  });
  /** Whether anyone could see the card photo: new recipes here are public, and it's the cover or attached */
  const cardPhotoPublic = computed(() =>
    !!job.value?.householdRecipesPublic && (draft.value.useCardAsCover || attachCardPhoto.value),
  );

  function hasPendingChanges() {
    return isDirty.value || pendingResolutions.size > 0 || pendingProposalIds.size > 0 || pendingClearError;
  }

  // ==========================================
  // Flags

  function trackSeen(list: readonly CardFlag[]) {
    if (resetSeen) {
      seenFlags.value = [];
      resetSeen = false;
    }
    const seen = new Map(seenFlags.value.map(flag => [flag.id, flag]));
    for (const flag of list.filter(isHighlighted)) {
      seen.set(flag.id, flag);
    }
    seenFlags.value = [...seen.values()];
  }

  /** Takes the server's flags, keeping resolutions made since the request left; one-tap fixes it has seen are done */
  function setFlags(list: readonly CardFlag[], seenFixes = Number.POSITIVE_INFINITY) {
    const merged = list.map(flag => (pendingResolutions.has(flag.id) ? { ...flag, resolution: pendingResolutions.get(flag.id) ?? null } : { ...flag }));
    const fixed = new Map([...fixedFlags.value].filter(([, sequence]) => sequence > seenFixes));
    fixedFlags.value = fixed;
    flags.value = merged;
    trackSeen(merged);
  }

  const review = computed(() =>
    buildNeedsALook(seenFlags.value, flags.value, new Set(fixedFlags.value.keys()), draft.value, proposals.value),
  );
  const needsALook = computed(() => review.value.items);
  const otherProposals = computed(() => review.value.otherProposals);
  const openItems = computed(() => needsALook.value.filter(item => item.state === "open"));
  const openErrors = computed(() => openItems.value.filter(item => item.flag.severity === "error"));
  /** Unresolved errors and warnings, as "2 to check" */
  const toCheck = computed(() => openItems.value.length);
  /** The flags the fields highlight: unresolved and not fixed since the last save */
  const openFlags = computed(() => openItems.value.map(item => item.flag));
  /** Flags that only inform ("Abbreviation written out", "New food"): shown quietly, never counted */
  const infoFlags = computed(() => flags.value.filter(flag => flag.severity === "info"));

  // ==========================================
  // Notices

  let noticeCount = 0;
  let noticeTimer: ReturnType<typeof setTimeout> | null = null;

  function clearNoticeTimer() {
    if (noticeTimer) {
      clearTimeout(noticeTimer);
      noticeTimer = null;
    }
  }

  /** Shows a notice in the review bar in place of the last one; a success or info without an action goes by itself */
  function notify(kind: ReviewNoticeKind, text: string, extra: { detail?: string | null; action?: ReviewNotice["action"] } = {}) {
    noticeCount += 1;
    const id = noticeCount;
    notice.value = { id, kind, text, detail: extra.detail ?? null, action: extra.action ?? null };
    clearNoticeTimer();
    if (!extra.action && (kind === "success" || kind === "info")) {
      noticeTimer = setTimeout(() => {
        noticeTimer = null;
        if (notice.value?.id === id) {
          notice.value = null;
        }
      }, NOTICE_MS);
    }
  }

  function dismissNotice() {
    clearNoticeTimer();
    notice.value = null;
  }

  /** The notice's button: it does its thing, and the notice goes */
  function runNoticeAction() {
    const action = notice.value?.action;
    dismissNotice();
    return action?.run();
  }

  // what the last card's Commit & next (or Skip, or Discard) said about it and where the review went; "Added …"
  // offers Undo for the card just added (`undoCommit`, defined below with the other card actions)
  const carried = takeCarriedReviewNotice(jobId);
  if (carried) {
    const undoJobId = carried.undoJobId;
    notify(carried.kind, carried.text, {
      detail: carried.detail,
      action: undoJobId ? { label: i18n.t("recipe-ingest.review.undo"), run: () => undoCommit(undoJobId) } : null,
    });
  }

  // ==========================================
  // Loading

  /**
   * Takes a read of the job. `overlap` says which of this page's saves were in flight while it was read: such a read
   * may come from before them.
   */
  function applyJob(data: RecipeIngestionJobOut, initial: boolean, overlap = { save: false, clearedError: false }) {
    // a read from before a save this page made: its draft and flags are out of date, the rest (proposals, status,
    // task) isn't. Draft versions only grow, but a save that only resolves flags or proposals, or dismisses the
    // banner, keeps the version, so a read at the same version that overlapped a save may predate it too.
    const stale = !initial
      && (data.draftVersion < draftVersion.value || (overlap.save && data.draftVersion === draftVersion.value));
    if (stale) {
      proposals.value = (data.proposals ?? []).filter(proposal => !proposal.id || !handledProposalIds.has(proposal.id));
      const current = job.value;
      job.value = current
        ? {
            ...data,
            draftVersion: current.draftVersion,
            draft: current.draft,
            flags: current.flags,
            errorCount: current.errorCount,
            warningCount: current.warningCount,
            title: current.title,
            // the banner a save dismissed meanwhile may still be in the read
            error: overlap.clearedError ? current.error : data.error,
          }
        : data;
      keepDismissedError();
      return;
    }
    const versionChanged = data.draftVersion !== draftVersion.value;
    const becameReady = job.value?.status !== "ready" && data.status === "ready";
    if (initial || becameReady || versionChanged) {
      if (!initial && isDirty.value && versionChanged && job.value?.status === "ready") {
        // edited here while another tab or a re-extract changed the stored draft: only a reload can sort it out
        conflict.value = true;
        job.value = { ...data, draftVersion: draftVersion.value };
        return;
      }
      draft.value = normalizeDraft(data.draft);
      savedDraft.value = stableStringify(draft.value);
      draftVersion.value = data.draftVersion;
      pendingResolutions.clear();
      fixedFlags.value = new Map();
      seenFlags.value = [];
      setFlags(data.flags ?? []);
    }
    else if (!isDirty.value && !saving) {
      setFlags(data.flags ?? []);
    }
    proposals.value = (data.proposals ?? []).filter(proposal => !proposal.id || !handledProposalIds.has(proposal.id));
    job.value = data;
    keepDismissedError();
  }

  /** A banner dismissed here stays dismissed while that waits to be saved, as resolutions do */
  function keepDismissedError() {
    if (pendingClearError && job.value) {
      job.value.error = null;
    }
  }

  async function loadBatch() {
    const batchId = job.value?.batchId;
    if (!batchId) {
      return null;
    }
    const { data } = await api.recipeIngest.getBatch(batchId);
    if (data) {
      batch.value = data;
      rememberRecipeIngestBatch(data);
    }
    return data;
  }

  async function load() {
    loadState.value = "loading";
    const { data, error } = await api.recipeIngest.getJob(jobId);
    if (!data) {
      loadState.value = errorStatusOf(error) === 404 ? "not-found" : "failed";
      return;
    }
    applyJob(data, true);
    // the batch as the last card saw it shows this card's place at once; the fetch brings it up to date
    batch.value ??= rememberedRecipeIngestBatch(data.batchId);
    loadState.value = "ready";
    void loadBatch();
  }

  /** Fetches the job again without dropping unsaved edits */
  async function refresh() {
    // the first save that may overlap the read: the one in flight now, else the next
    const firstOverlapping = saving ? saveSeq : saveSeq + 1;
    const { data, error } = await api.recipeIngest.getJob(jobId);
    if (!data) {
      if (errorStatusOf(error) === 404) {
        loadState.value = "not-found";
      }
      return;
    }
    // a save that overlapped the read lands first, so the read's version is compared with the one it returned:
    // the read may have seen that save, which isn't a change from somewhere else (docs/ai/PHASE2.md §6.6)
    while (saving) {
      await saving;
    }
    applyJob(data, false, { save: saveSeq >= firstOverlapping, clearedError: clearedErrorSeq >= firstOverlapping });
  }

  /** "Reload this card": drops the unsaved edits and loads the stored card */
  async function reload() {
    conflict.value = false;
    pendingResolutions.clear();
    pendingProposalIds.clear();
    pendingClearError = false;
    saveState.value = "idle";
    await load();
  }

  // ==========================================
  // Saving

  async function sendSave(): Promise<SaveOutcome> {
    saveSeq += 1;
    const seq = saveSeq;
    const sentDraft = cloneDraft(draft.value);
    const sentJson = stableStringify(sentDraft);
    const sentFixes = fixCounter;
    const resolutions = Object.fromEntries(pendingResolutions);
    const proposalIds = [...pendingProposalIds];
    const clearError = pendingClearError;
    pendingResolutions.clear();
    pendingProposalIds.clear();
    pendingClearError = false;
    saveState.value = "saving";

    const { data, error } = await api.recipeIngest.updateJob(jobId, {
      draftVersion: draftVersion.value,
      draft: sentDraft,
      flagResolutions: resolutions,
      resolvedProposalIds: proposalIds,
      clearError,
    });

    if (data) {
      draftVersion.value = data.draftVersion;
      savedDraft.value = sentJson;
      if (job.value) {
        job.value.draftVersion = data.draftVersion;
        job.value.errorCount = data.errorCount ?? 0;
        job.value.warningCount = data.warningCount ?? 0;
        job.value.title = sentDraft.name;
        if (clearError) {
          job.value.error = null;
        }
      }
      if (clearError) {
        clearedErrorSeq = seq;
      }
      setFlags(data.flags ?? [], sentFixes);
      saveState.value = "saved";
      return "saved";
    }

    // keep what wasn't saved for the next try, unless it changed since
    for (const [id, resolution] of Object.entries(resolutions)) {
      if (!pendingResolutions.has(id)) {
        pendingResolutions.set(id, resolution);
      }
    }
    proposalIds.forEach(id => pendingProposalIds.add(id));
    pendingClearError ||= clearError;

    const code = errorCodeOf(error);
    const status = errorStatusOf(error);
    if (code === "version_conflict") {
      // the 409 has no message, so nothing was toasted: the page shows its "Reload this card" dialog
      conflict.value = true;
      saveState.value = "idle";
      return "conflict";
    }
    saveState.value = "error";
    if (code === "invalid_status" || status === 404) {
      return "refused";
    }
    // no answer (offline), the server failing or busy: the same save can work later
    return status === null || status >= 500 || status === 429 ? "retry" : "failed";
  }

  // a save that got no answer is tried again: 2 s, 4 s, 8 s ... up to a minute apart, while the page is open
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let retries = 0;

  function cancelRetry() {
    if (retryTimer) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }
  }

  function scheduleRetry() {
    if (unmounted || retryTimer) {
      return;
    }
    const delay = SAVE_RETRY_MS[Math.min(retries, SAVE_RETRY_MS.length - 1)]!;
    retries += 1;
    retryTimer = setTimeout(() => {
      retryTimer = null;
      void save();
    }, delay);
  }

  /** Forgets the edits waiting to be saved: they can't be (the card is gone or moved on) */
  function dropPendingChanges() {
    savedDraft.value = stableStringify(draft.value);
    pendingResolutions.clear();
    pendingProposalIds.clear();
    pendingClearError = false;
    cancelRetry();
  }

  /** A save refused because the card was committed, discarded or read again elsewhere: show the card as it is now */
  async function settleRefusedSave() {
    dropPendingChanges();
    saveState.value = "idle";
    await refresh();
    if (loadState.value === "not-found") {
      // the page says the card no longer exists
      return;
    }
    const added = job.value?.status === "committed" || job.value?.status === "committing";
    notify("warning", i18n.t(added ? "recipe-ingest.review.save-refused-added" : "recipe-ingest.review.save-refused-changed"));
  }

  /** Saves pending changes now; waits for a save in flight first. Does nothing in a conflict or when nothing changed. */
  async function save(): Promise<void> {
    while (saving) {
      await saving;
    }
    if (conflict.value || job.value?.status !== "ready" || !hasPendingChanges()) {
      return;
    }
    const sending = sendSave();
    saving = sending.then(() => undefined).finally(() => {
      saving = null;
    });
    await saving;
    const outcome = await sending;
    if (outcome === "saved") {
      cancelRetry();
      retries = 0;
    }
    else if (outcome === "retry") {
      scheduleRetry();
    }
    else if (outcome === "refused") {
      await settleRefusedSave();
    }
    else if (outcome === "failed") {
      // refused for what it holds (a 422): sending it again won't help, the next edit will
      notify("error", i18n.t("recipe-ingest.review.save-rejected"));
    }
  }

  const scheduleSave = useDebounceFn(() => save(), AUTOSAVE_DELAY_MS);

  watch(draft, () => {
    if (isDirty.value) {
      void scheduleSave();
    }
  }, { deep: true });

  // ==========================================
  // Flags and proposals

  function resolveFlag(flag: CardFlag, resolution: FlagResolution | null) {
    if (readOnly.value) {
      return;
    }
    flags.value = flags.value.map(item => (item.id === flag.id ? { ...item, resolution } : item));
    pendingResolutions.set(flag.id, resolution);
    void scheduleSave();
  }

  function markFixed(flag: CardFlag) {
    fixCounter += 1;
    fixedFlags.value = new Map(fixedFlags.value).set(flag.id, fixCounter);
  }

  /** One tap: puts an alternative reading in place of the flagged text */
  function applyFlagAlternative(flag: CardFlag, alternative: string) {
    if (!readOnly.value && editFlaggedText(draft.value, flag, alternative, "alternative")) {
      markFixed(flag);
    }
  }

  /** Types a value over a blank (or an unreadable spot) */
  function fillFlagBlank(flag: CardFlag, value: string) {
    if (!readOnly.value && value.trim() && editFlaggedText(draft.value, flag, value, "fill")) {
      markFixed(flag);
    }
  }

  /**
   * "Keep as text" on "Check this ingredient": the flagged line is kept as written, with no amount, unit or food.
   * The parser's flags on the line are done with (the server drops them once the line is edited).
   */
  function keepIngredientAsText(flag: CardFlag) {
    const index = draft.value.ingredients.findIndex(item => item.referenceId === flag.ref);
    if (readOnly.value || normalizeField(flag.field) !== "ingredients" || index < 0) {
      return;
    }
    draft.value.ingredients.splice(index, 1, ingredientAsText(draft.value.ingredients[index]!));
    flagsForField(flags.value, "ingredients", flag.ref ?? null)
      .filter(item => isHighlighted(item) && PARSE_KINDS.includes(item.kind))
      .forEach(item => markFixed(item));
  }

  function settleProposal(proposal: CardProposal) {
    proposals.value = proposals.value.filter(item => item !== proposal && (!proposal.id || item.id !== proposal.id));
    if (proposal.id) {
      handledProposalIds.add(proposal.id);
      pendingProposalIds.add(proposal.id);
    }
    void scheduleSave();
  }

  /** Uses a re-read: `replace` the target's text, or `append` to it; a whole-card re-read replaces the draft */
  function useProposal(proposal: CardProposal, mode: "replace" | "append" = "replace") {
    if (readOnly.value) {
      return;
    }
    if (proposal.kind === "full") {
      if (proposal.draft) {
        draft.value = normalizeDraft(proposal.draft);
        // the flags of the old draft don't describe the new one
        resetSeen = true;
      }
    }
    else {
      const before = stableStringify(draft.value);
      applyProposal(draft.value, proposal, mode);
      if (mode === "replace" && proposal.target && stableStringify(draft.value) !== before) {
        // the new reading took the flagged text's place: its flags show as fixed at once, and the save's answer
        // confirms that (or opens them again when the server still raises them)
        flagsForField(flags.value, proposal.target.field, proposal.target.ref ?? null)
          .filter(isHighlighted)
          .forEach(flag => markFixed(flag));
      }
    }
    settleProposal(proposal);
  }

  function dismissProposal(proposal: CardProposal) {
    if (!readOnly.value) {
      settleProposal(proposal);
    }
  }

  /** Dismisses the banner of a failed re-read or re-extract */
  function dismissError() {
    if (job.value) {
      job.value.error = null;
    }
    pendingClearError = true;
    void scheduleSave();
  }

  // ==========================================
  // Errors

  function notifyError(error: unknown) {
    if (alreadyToasted(error)) {
      return;
    }
    const code = errorCodeOf(error);
    // the refusal's own values fill its text ("A card can have at most {max} photos.")
    const detail = (error as { response?: { data?: { detail?: unknown } } } | null)?.response?.data?.detail;
    const params = detail && typeof detail === "object" ? detail as Record<string, unknown> : null;
    notify("error", code ? text.ingestErrorText(code, params) : i18n.t("events.something-went-wrong"));
  }

  function applyState(state: RecipeIngestionJobState) {
    if (job.value) {
      job.value.task = state.task ?? null;
      job.value.status = state.status;
    }
  }

  // ==========================================
  // Re-reads and polling

  async function sendReread(request: RereadRequest): Promise<boolean> {
    const { data, error } = await api.recipeIngest.reread(jobId, request);
    if (data) {
      applyState(data);
      return true;
    }
    if (errorCodeOf(error) === "busy") {
      // another task is running: wait for it, then send this one (the poll keeps going while the queue isn't empty)
      rereadQueue.value = [request, ...rereadQueue.value];
      notify("info", i18n.t("recipe-ingest.review.busy"));
      return false;
    }
    notifyError(error);
    return false;
  }

  /** Re-reads a region; while a task runs (or other re-reads wait) it joins the queue */
  async function requestReread(request: RereadRequest) {
    if (task.value || rereadQueue.value.length > 0) {
      rereadQueue.value = [...rereadQueue.value, request];
      notify("info", i18n.t("recipe-ingest.review.reread-queued"));
      return;
    }
    await sendReread(request);
  }

  async function sendNextReread() {
    const [next, ...rest] = rereadQueue.value;
    if (!next) {
      return;
    }
    rereadQueue.value = rest;
    await sendReread(next);
  }

  let polling = false;
  async function pollState() {
    if (polling || !job.value) {
      return;
    }
    polling = true;
    try {
      const { data, error } = await api.recipeIngest.getJobState(jobId);
      if (!data) {
        if (errorStatusOf(error) === 404) {
          loadState.value = "not-found";
        }
        return;
      }
      const current = job.value;
      const known = new Set([...proposals.value.map(proposal => proposal.id), ...handledProposalIds]);
      const finishedExtract = current.task?.kind === "extract" && !data.task;
      const changed = data.status !== current.status
        || (!saving && data.draftVersion !== draftVersion.value)
        || (data.proposalIds ?? []).some(id => !known.has(id))
        || (data.error?.code ?? null) !== (current.error?.code ?? null)
        || finishedExtract;
      current.task = data.task ?? null;
      if (changed) {
        await refresh();
      }
      if (!job.value?.task && job.value?.status === "ready" && rereadQueue.value.length > 0) {
        await sendNextReread();
      }
    }
    finally {
      polling = false;
    }
  }

  const pollNeeded = computed(() =>
    loadState.value === "ready"
    && !!job.value
    && (ACTIVE_STATUSES.includes(job.value.status) || !!job.value.task || rereadQueue.value.length > 0),
  );
  const poller = useIntervalFn(pollState, STATE_POLL_MS, { immediate: false });
  watch(pollNeeded, need => (need ? poller.resume() : poller.pause()), { immediate: true });

  // ==========================================
  // Card actions

  async function runAction<T>(name: string, action: () => Promise<T>): Promise<T | undefined> {
    if (pendingAction.value) {
      return undefined;
    }
    pendingAction.value = name;
    try {
      return await action();
    }
    finally {
      pendingAction.value = null;
    }
  }

  /** "Read whole card again": an unedited draft is replaced; an edited one gets a whole-card proposal */
  async function reextract() {
    return await runAction("reextract", async () => {
      await save();
      const { data, error } = await api.recipeIngest.reextract(jobId);
      if (data) {
        applyState(data);
        return true;
      }
      if (!alreadyToasted(error) && errorCodeOf(error) === "busy") {
        notify("info", text.ingestErrorText("busy"));
      }
      else {
        notifyError(error);
      }
      return false;
    });
  }

  /** Turns a page clockwise, then offers to read the card again (a failed card from the start) */
  async function rotate(pageIndex: number, degrees: RotateRequest["degrees"] = 90) {
    return await runAction("rotate", async () => {
      const { data, error } = await api.recipeIngest.rotatePage(jobId, pageIndex, { degrees });
      if (data && job.value) {
        const pages = [...(job.value.pages ?? [])];
        const position = pages.findIndex(page => page.index === pageIndex);
        if (position >= 0) {
          pages.splice(position, 1, data);
        }
        job.value.pages = pages;
        const failed = job.value.status === "failed";
        notify("info", i18n.t("recipe-ingest.review.rotate-hint"), {
          action: failed
            ? { label: i18n.t("recipe-ingest.queue.retry"), run: () => retry() }
            : { label: i18n.t("recipe-ingest.review.read-again-short"), run: () => reextract() },
        });
        return true;
      }
      if (!alreadyToasted(error) && errorStatusOf(error) === 409) {
        // a task is running: the page can't turn it under the reader
        notify("info", text.ingestErrorText("busy"));
      }
      else {
        notifyError(error);
      }
      return false;
    });
  }

  /** `POST …/uncommit` for a card, telling a recipe edited since (`recipe_edited`) from the other refusals */
  async function sendUncommit(id: string, force: boolean): Promise<{ outcome: UncommitOutcome; error: unknown }> {
    const { data, error } = await api.recipeIngest.uncommit(id, force ? { force: true } : {});
    if (data) {
      void counts.refresh();
      return { outcome: "done", error: null };
    }
    return { outcome: errorCodeOf(error) === "recipe_edited" ? "edited" : "failed", error };
  }

  /**
   * "Back to review" on an added card: deletes the recipe it became and makes the card ready again with its draft.
   * `edited`: the recipe was changed after the commit, so the page asks before sending it again with `force`.
   * Otherwise refused, it says why and shows the card as it is now.
   */
  async function uncommit(force = false): Promise<UncommitOutcome> {
    return (await runAction("uncommit", async () => {
      const { outcome, error } = await sendUncommit(jobId, force);
      if (outcome === "done") {
        await refresh();
        notify("success", i18n.t("recipe-ingest.review.back-to-review-done"));
      }
      else if (outcome === "failed") {
        notifyError(error);
        await refresh();
      }
      return outcome;
    })) ?? "failed";
  }

  /**
   * Undo on "Added …": takes the card just added (another card) back to review and opens it there. A recipe edited
   * since isn't deleted from here: the notice says so and opens that card, whose Back to review asks first.
   */
  async function undoCommit(id: string) {
    return await runAction("undo", async () => {
      const { outcome, error } = await sendUncommit(id, false);
      if (outcome === "done") {
        carryReviewNotice(id, { kind: "success", text: i18n.t("recipe-ingest.review.back-to-review-done"), detail: null });
        await options.navigate(cardPath(id));
        return true;
      }
      if (outcome === "edited") {
        notify("warning", text.ingestErrorText("recipe_edited"), {
          action: { label: i18n.t("recipe-ingest.review.open-card"), run: () => goTo(id) },
        });
      }
      else {
        notifyError(error);
      }
      return false;
    });
  }

  /** Whether "Add as back of previous card" is offered: a ready or failed card the user uploaded or manages */
  const canMerge = computed(() => !!job.value?.permissions?.canMerge);
  /** The card before this one in its batch, as last fetched: `undefined` until then, `null` when there's none */
  const previousCard = ref<RecipeIngestionJobOut | null | undefined>(undefined);
  /** The most photos a card takes (the group's card settings, loaded by the layout) */
  const maxPagesPerCard = computed(() => ingestSettings.settings.value?.limits?.maxPagesPerCard || DEFAULT_MAX_PAGES_PER_CARD);
  /** Why this card can't be added to the previous one now (`MergeBlock`); null when it can, or isn't offered */
  const mergeBlock = computed<MergeBlock | null>(() => {
    if (!canMerge.value || !job.value) {
      return null;
    }
    if (task.value) {
      return "busy";
    }
    return mergeBlockOf(job.value, previousCard.value, maxPagesPerCard.value);
  });

  /**
   * Fetches the card before this one in its batch (by capture position), whose state, pages and permissions say
   * whether this card can become its back. The ⋯ menu calls it as it opens.
   */
  async function checkPreviousCard() {
    if (!batch.value) {
      await loadBatch();
    }
    const previousId = position.value?.previous ?? null;
    if (!batch.value) {
      previousCard.value = undefined;
      return;
    }
    if (!previousId) {
      previousCard.value = null;
      return;
    }
    const { data, error } = await api.recipeIngest.getJob(previousId);
    if (data) {
      previousCard.value = data;
    }
    else {
      // gone since the batch was read: none to add to; otherwise not known (the menu says it's checking)
      previousCard.value = errorStatusOf(error) === 404 ? null : undefined;
    }
  }

  /**
   * "Add as back of previous card": this card's photos become the previous card's next pages, this card is deleted,
   * and the previous card is read again; the review opens it. Refused, it says why and checks that card again.
   */
  async function mergeIntoPrevious() {
    const into = previousCard.value;
    if (!into || mergeBlock.value) {
      return false;
    }
    return (await runAction("merge", async () => {
      const { data, error } = await api.recipeIngest.merge(jobId, { intoJobId: into.id });
      if (data) {
        // this card is gone: its edits can't be saved any more
        dropPendingChanges();
        void counts.refresh();
        carryReviewNotice(into.id, { kind: "info", text: i18n.t("recipe-ingest.review.merged"), detail: null });
        await options.navigate(cardPath(into.id));
        return true;
      }
      notifyError(error);
      await checkPreviousCard();
      return false;
    })) ?? false;
  }

  /** Reads a failed card again */
  async function retry() {
    return await runAction("retry", async () => {
      const { data, error } = await api.recipeIngest.retry(jobId);
      if (data) {
        applyState(data);
        if (job.value) {
          job.value.error = null;
        }
        return true;
      }
      notifyError(error);
      return false;
    });
  }

  /**
   * Reads a card that failed because it had to stay on this server (`local_only_unavailable`) again, with any of the
   * group's providers, cloud ones included: the card is no longer kept local. Refused (the group now keeps every card
   * local, or the card changed), it says why and shows the card as it is now.
   */
  async function readWithCloud() {
    return await runAction("cloud", async () => {
      const { data, error } = await api.recipeIngest.readWithCloud(jobId);
      if (data) {
        applyState(data);
        if (job.value) {
          job.value.error = null;
          job.value.localOnly = false;
          job.value.permissions = { ...job.value.permissions, canReadWithCloud: false };
        }
        return true;
      }
      notifyError(error);
      if (errorStatusOf(error) === 409 || errorStatusOf(error) === 403) {
        await refresh();
      }
      return false;
    });
  }

  /**
   * Stops what's reading the card (a re-read, a re-extract, or a card still being read) and drops the re-reads
   * waiting. A running task stops within a heartbeat (`task.cancelRequested` until then); a queued one at once.
   */
  async function cancelTask() {
    rereadQueue.value = [];
    if (!task.value) {
      return true;
    }
    return await runAction("cancel", async () => {
      const { data, error } = await api.recipeIngest.cancel(jobId);
      if (!data) {
        notifyError(error);
        return false;
      }
      applyState(data);
      if (!data.task) {
        await refresh();
        notify("info", i18n.t("recipe-ingest.review.read-cancelled"));
      }
      return true;
    });
  }

  function groupPath(path = "") {
    return `/g/${toValue(options.groupSlug)}/recipes/cards${path}`;
  }

  /** The queue, filtered to this card's batch */
  function queuePath() {
    const batchId = job.value?.batchId;
    return groupPath(batchId ? `?batch=${encodeURIComponent(batchId)}` : "");
  }

  function cardPath(id: string) {
    return groupPath(`/${id}`);
  }

  /**
   * Where the review goes after this card (`NextStop`), from the batch fresh from the server: `preferred` is the
   * commit's own answer for the batch's next ready card
   */
  async function nextStop(preferred?: string | null): Promise<NextStop> {
    const fresh = await loadBatch();
    const jobs = fresh?.jobs ?? batch.value?.jobs ?? [];
    const next = preferred && preferred !== jobId ? preferred : nextCardInBatch(jobs, jobId);
    if (next) {
      return { kind: "card", id: next };
    }
    const processing = jobs.filter(item => item.id !== jobId && item.status === "processing").length;
    if (processing > 0) {
      return { kind: "queue", processing };
    }
    const { data } = await api.recipeIngest.getJobs({ status: "ready", perPage: -1 });
    const other = nextBatchCard(data?.items ?? [], job.value?.batchId, jobId);
    return other ? { kind: "next-batch", id: other } : { kind: "queue", processing: 0 };
  }

  /**
   * Leaves for the next stop. What there is to say about this card (`said`: "Added …") goes with it: to the next
   * card's review bar, with "Next batch" when that card is in another batch; or to the queue, with how many cards
   * are still being read, else `lastWords` ("That was the last card …").
   */
  async function goOn(stop: NextStop, said: ReviewNoticeText | null, lastWords: string | null = null) {
    if (stop.kind === "queue") {
      const still = stop.processing > 0 ? i18n.t("recipe-ingest.review.still-reading", stop.processing) : lastWords;
      const words = [said?.text, still].filter((part): part is string => !!part);
      if (words.length) {
        // the card just added goes with it, for the queue's Undo (`RecipeIngestCommitNotice` gains `undoJobId`)
        const left: RecipeIngestCommitNotice & { undoJobId?: string | null } = {
          text: words.join(" · "),
          warning: said?.detail ?? null,
          ...(said?.undoJobId ? { undoJobId: said.undoJobId } : {}),
        };
        leaveRecipeIngestCommitNotice(left);
      }
      return await options.navigate(queuePath());
    }
    const words = [said?.text, stop.kind === "next-batch" ? i18n.t("recipe-ingest.review.next-batch") : null]
      .filter((part): part is string => !!part);
    if (words.length) {
      carryReviewNotice(stop.id, {
        kind: said?.kind ?? "info",
        text: words.join(" · "),
        detail: said?.detail ?? null,
        ...(said?.undoJobId ? { undoJobId: said.undoJobId } : {}),
      });
    }
    return await options.navigate(cardPath(stop.id));
  }

  /** "Skip": the next card to review, keeping this one for later */
  async function skip() {
    await save();
    await goOn(await nextStop(), null, i18n.t("recipe-ingest.review.last-card"));
  }

  function goTo(id: string) {
    return options.navigate(cardPath(id));
  }

  async function discard() {
    return await runAction("discard", async () => {
      const { error } = await api.recipeIngest.discard(jobId);
      if (error) {
        notifyError(error);
        return false;
      }
      // nothing is left to save
      dropPendingChanges();
      void counts.refresh();
      await goOn(await nextStop(), { kind: "success", text: i18n.t("recipe-ingest.queue.discarded") });
      return true;
    });
  }

  /** Saves the reviewed card to the group's eval set (managers), with what the reviewer says the card is like */
  async function saveEvalCase(request: EvalCaseRequest): Promise<"saved" | "exists" | "failed"> {
    return (await runAction("eval", async () => {
      await save();
      const { data, error } = await api.recipeIngest.saveEvalCase(jobId, request);
      if (data) {
        notify("success", i18n.t("recipe-ingest.eval.saved", { slug: data.slug }));
        return "saved" as const;
      }
      // the dialog says so for a name that's taken; the other refusals (not_exportable, files_missing) are toasted
      if (errorCodeOf(error) === "eval_case_exists") {
        return "exists" as const;
      }
      notifyError(error);
      return "failed" as const;
    })) ?? "failed";
  }

  /** The id of the first unresolved error's "Needs a look" item, which "1 to fix" scrolls to */
  const firstErrorAnchor = computed(() => openErrors.value[0]?.anchor ?? null);

  /**
   * "Commit & next": waits for the pending save, commits with the version it returned, then goes on (`nextStop`):
   * to the batch's next ready card, which says "Added …"; while cards of the batch are still being read, to the
   * queue filtered to the batch; else to another batch's ready card; else to the queue, which sums the batch up.
   * Unresolved errors don't commit: the result is `"fix"` and the page scrolls to the first.
   */
  async function commit(): Promise<"committed" | "fix" | "conflict" | "failed"> {
    if (committing.value || !job.value || job.value.status !== "ready") {
      return "failed";
    }
    if (openErrors.value.length > 0) {
      return "fix";
    }
    committing.value = true;
    try {
      await save();
      if (conflict.value) {
        return "conflict";
      }
      if (job.value?.status !== "ready") {
        // the save found the card committed, discarded or read again elsewhere: the page shows it as it is now
        return "failed";
      }
      if (hasPendingChanges() && saveState.value === "error") {
        notify("error", i18n.t("recipe-ingest.review.commit-unsaved"));
        return "failed";
      }
      const name = draft.value.name;
      const { data, error } = await api.recipeIngest.commit(jobId, { draftVersion: draftVersion.value });
      if (!data) {
        const code = errorCodeOf(error);
        if (code === "unresolved_flags") {
          const detail = (error as { response?: { data?: { detail?: { flags?: CardFlag[] } } } }).response?.data?.detail;
          // the server's flags for the stored draft: the ones it holds against the commit show as open
          const blocking = new Map((detail?.flags ?? []).map(flag => [flag.id, flag]));
          fixedFlags.value = new Map([...fixedFlags.value].filter(([id]) => !blocking.has(id)));
          setFlags([...flags.value.filter(flag => !blocking.has(flag.id)), ...blocking.values()]);
          return "fix";
        }
        if (code === "version_conflict") {
          conflict.value = true;
          return "conflict";
        }
        // the card moved on (a double tap: it's committing or committed, which the page then shows), or the draft
        // didn't make a recipe (its error banner says why): show it as it is now
        await refresh();
        if (errorStatusOf(error) !== 409 || job.value?.status === "ready") {
          notifyError(error);
        }
        return "failed";
      }

      job.value.status = "committed";
      void counts.refresh();
      const added = i18n.t("recipe-ingest.review.added", { name: name || data.slug });
      // what commit left out (an organizer deleted since it was chosen)
      const warnings = (data.warnings ?? [])
        .map(warning => text.commitWarningText(warning))
        .filter((warning): warning is string => !!warning);
      const warning = warnings.length ? warnings.join(" ") : null;
      // said in the next card's review bar, or beside the queue's summary of the batch: a toast would cover the
      // header of either page on phones
      await goOn(await nextStop(data.nextJobId), {
        kind: warning ? "warning" : "success",
        text: added,
        detail: warning,
        undoJobId: jobId,
      });
      return "committed";
    }
    finally {
      committing.value = false;
    }
  }

  // ==========================================
  // Leaving

  useEventListener("beforeunload", (event: BeforeUnloadEvent) => {
    if (!conflict.value && (saving || hasPendingChanges())) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  /**
   * Before leaving the page in the app: saves what's pending now. Whether nothing is left unsaved; when the save
   * fails, the page asks before leaving.
   */
  async function saveBeforeLeaving(): Promise<boolean> {
    await save();
    return job.value?.status !== "ready" || !hasPendingChanges();
  }

  onBeforeUnmount(() => {
    unmounted = true;
    cancelRetry();
    clearNoticeTimer();
    // the debounced save would never run: send what's pending now
    if (hasPendingChanges()) {
      void save();
    }
  });

  /** Where this card sits in its batch: "Card 3 of 10", and its neighbours for previous/next */
  const position = computed(() => {
    const jobs = inReviewOrder(batch.value?.jobs ?? []);
    const index = jobs.findIndex(item => item.id === jobId);
    if (index < 0) {
      return null;
    }
    return {
      number: index + 1,
      total: jobs.length,
      previous: jobs[index - 1]?.id ?? null,
      next: jobs[index + 1]?.id ?? null,
    };
  });

  return {
    // state
    job,
    batch,
    loadState,
    draft,
    draftVersion: readonly(draftVersion),
    flags,
    proposals,
    saveState: readonly(saveState),
    isDirty,
    conflict,
    committing: readonly(committing),
    pendingAction: readonly(pendingAction),
    rereadQueue: readonly(rereadQueue),
    notice: readonly(notice),
    task,
    readOnly,
    position,
    attachCardPhoto,
    cardPhotoPublic,
    canMerge,
    previousCard: readonly(previousCard),
    mergeBlock,
    maxPagesPerCard,
    // flags
    needsALook,
    otherProposals,
    openFlags,
    infoFlags,
    openErrors,
    toCheck,
    firstErrorAnchor,
    // actions
    load,
    refresh,
    reload,
    save,
    saveBeforeLeaving,
    dismissNotice,
    runNoticeAction,
    pollState,
    resolveFlag,
    applyFlagAlternative,
    fillFlagBlank,
    keepIngredientAsText,
    useProposal,
    dismissProposal,
    dismissError,
    requestReread,
    reextract,
    rotate,
    retry,
    readWithCloud,
    uncommit,
    undoCommit,
    checkPreviousCard,
    mergeIntoPrevious,
    cancelTask,
    discard,
    saveEvalCase,
    commit,
    skip,
    goTo,
    queuePath,
    cardPath,
  };
}

export type RecipeIngestReview = ReturnType<typeof useRecipeIngestReview>;
