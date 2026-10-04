/**
 * The recipe card review page (docs/ai/PHASE2.md §6): pure helpers for batches, flags, drafts and crop regions, and
 * `useRecipeIngestReview`, the page's state: autosave carrying the draft version, the version conflict, state polling
 * while a task runs, the client-side re-read queue, commit and the ⋯ menu's actions. Fork-owned.
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
  useRecipeIngestText,
} from "~/composables/use-recipe-ingest";
import type { TranslateFn } from "~/composables/use-recipe-ingest";
import { alert } from "~/composables/use-toast";
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
  FlagResolution,
  IngestStatus,
  ProposalTarget,
  RecipeIngestionBatchJob,
  RecipeIngestionBatchOut,
  RecipeIngestionJobOut,
  RecipeIngestionJobState,
  RereadRequest,
  RotateRequest,
} from "~/lib/api/types/recipe-ingest";

/** Debounce of the draft's autosave */
export const AUTOSAVE_DELAY_MS = 1500;
/** How often the page asks for the job's state while a task runs */
export const STATE_POLL_MS = 2000;
/** A re-read region's smallest side, as a fraction of the page (the server's `MIN_REGION_SIDE`) */
export const MIN_REGION_SIDE = 0.02;
/** What the page highlights (the server's `flag_rules.HIGHLIGHTED_SEVERITIES`) */
export const HIGHLIGHTED_SEVERITIES: readonly CardFlagSeverity[] = ["error", "warning"];
/** Errors that "Keep as written" resolves (the server's `flag_rules.KEEPABLE_KINDS`); every other error is fixed */
export const KEEPABLE_KINDS: readonly CardFlagKind[] = ["illegible", "blank"];
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
  notes: (CardDraftNote & { title: string; text: string })[];
  tags: CardDraftRef[];
  categories: CardDraftRef[];
  tools: CardDraftRef[];
};

/** A plain copy of a draft (drafts are JSON) */
export function cloneDraft<T>(draft: T): T {
  return JSON.parse(JSON.stringify(draft)) as T;
}

/** A copy of the draft with its defaults filled in; ingredients and steps without an id get one */
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
    notes: (source.notes ?? []).map(note => ({ ...note, title: note.title ?? "", text: note.text ?? "" })),
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

/** A field's label ("Name", "Step: 2"); none for the card as a whole */
export function fieldLabel(t: TranslateFn, field: string, line?: number | null): string | null {
  const key = normalizeField(field);
  if (key === "steps" && line !== null && line !== undefined) {
    return t("recipe.step-index", { step: line + 1 });
  }
  return FIELD_LABELS[key] ? t(FIELD_LABELS[key]) : null;
}

/** A note's position from a flag's or proposal's `ref` (the server keys notes by index); null without one */
export function noteIndex(ref: string | null | undefined): number | null {
  return ref && /^\d+$/.test(ref) ? Number(ref) : null;
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
    const index = noteIndex(ref);
    const notes = index === null ? draft.notes ?? [] : (draft.notes ?? []).slice(index, index + 1);
    return notes.map(note => [note.title, note.text].filter(Boolean).join("\n")).join("\n");
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

function replaceFirst(text: string, fragment: string, replacement: string): string | null {
  const index = fragment ? text.indexOf(fragment) : -1;
  return index < 0 ? null : text.slice(0, index) + replacement + text.slice(index + fragment.length);
}

/**
 * The text with one of a flag's alternatives applied: it replaces the flagged part when the text holds it, else the
 * alternative is the whole new text (a second reading's line, or a reading that has changed since).
 */
export function applyAlternative(text: string, flag: CardFlag, alternative: string): string {
  const fragment = flagFragment(flag);
  return (fragment ? replaceFirst(text, fragment, alternative) : null) ?? alternative;
}

/** The text with a typed value in place of the marker (or flagged part); unchanged when the value is empty or the
 * marker is already gone */
export function fillBlank(text: string, value: string, fragment: string = MARKERS.blank): string {
  const typed = value.trim();
  if (!typed) {
    return text;
  }
  return replaceFirst(text, fragment, typed) ?? text;
}

export interface TextSegment {
  text: string;
  mark: boolean;
}

/** The text cut around every occurrence of the flagged part, for highlighting it; a blank shows as "___" */
export function highlightSegments(text: string, fragment: string | null): TextSegment[] {
  const show = (part: string) => part.split(MARKERS.blank).join("___");
  if (!fragment || !text.includes(fragment)) {
    return text ? [{ text: show(text), mark: false }] : [];
  }
  const segments: TextSegment[] = [];
  text.split(fragment).forEach((part, index) => {
    if (index > 0) {
      segments.push({ text: show(fragment), mark: true });
    }
    if (part) {
      segments.push({ text: show(part), mark: false });
    }
  });
  return segments;
}

/**
 * An ingredient with a flag's fix applied. A line kept as text changes in its note. A parsed line changes in the
 * part that holds the flagged text (note, amount, unit or food: an edited unit or food is linked again by name at
 * commit); when no part holds it, the fixed line is kept as text.
 */
export function fixIngredient(
  ingredient: CardDraftIngredient,
  flag: CardFlag,
  replacement: string,
  mode: "alternative" | "fill",
): CardDraftIngredient {
  const fragment = flagFragment(flag) ?? (mode === "fill" ? MARKERS.blank : null);
  const fix = (text: string) => (mode === "fill" ? fillBlank(text, replacement, fragment ?? MARKERS.blank) : applyAlternative(text, flag, replacement));

  if (!isParsedIngredient(ingredient)) {
    return withDisplay({ ...ingredient, note: fix(ingredient.note || ingredient.originalText || "") });
  }
  if (fragment) {
    if (ingredient.note?.includes(fragment)) {
      return withDisplay({ ...ingredient, note: fix(ingredient.note) });
    }
    const amount = parseQuantity(fragment);
    const newAmount = parseQuantity(replacement);
    if (amount !== null && newAmount !== null && amount === ingredient.quantity) {
      return withDisplay({ ...ingredient, quantity: newAmount });
    }
    if (ingredient.unit?.name?.includes(fragment)) {
      return withDisplay({ ...ingredient, unit: { id: null, name: fix(ingredient.unit.name).trim() } });
    }
    if (ingredient.food?.name?.includes(fragment)) {
      return withDisplay({ ...ingredient, food: { id: null, name: fix(ingredient.food.name).trim() } });
    }
  }
  const line = fix(ingredient.originalText || ingredientDisplay(ingredient));
  return withDisplay({ ...ingredient, quantity: null, unit: null, food: null, note: line });
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
  const fix = (text: string) => (mode === "fill" ? fillBlank(text, replacement, fragment) : applyAlternative(text, flag, replacement));

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
    // the flag's note by its index; without one, the first note holding the flagged part
    const index = noteIndex(flag.ref);
    const note = index === null
      ? draft.notes.find(item => (item.text ?? "").includes(fragment) || (item.title ?? "").includes(fragment))
      : draft.notes[index];
    if (!note) {
      return false;
    }
    // the marker can be in the note's title (the server checks both); otherwise the text changes
    const part = !(note.text ?? "").includes(fragment) && (note.title ?? "").includes(fragment) ? "title" : "text";
    const text = fix(note[part] ?? "");
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
    line = noteIndex(flag.ref) ?? 0;
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
  /** The line as it reads now, and the part the flag is about */
  text: string;
  fragment: string | null;
  /** Re-read results for this line, shown inside the item */
  proposals: CardProposal[];
  anchor: string;
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
      const index = noteIndex(flag.ref);
      line = index !== null && index < (draft.notes?.length ?? 0) ? index : null;
    }
    return {
      flag,
      state,
      field,
      line: line !== null && line < 0 ? null : line,
      text: fieldText(draft, field, flag.ref),
      fragment: flagFragment(flag),
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
    draft.notes.push({ title: "", text });
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
  /** "name", "ingredient", "step", "new-ingredient", ...: which label the dialog shows */
  kind: TextField | "ingredient" | "step" | "new-ingredient" | "new-step" | "note";
  /** The line's text, or the step's number (from 1) */
  text: string;
}

/**
 * Every line a re-read can be for: each field, each ingredient and step by its `ref`, and a new ingredient, step or
 * note (a target without a `ref`, for a line the reading missed), which `applyProposal` adds.
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
  options.push({ value: "notes:new", target: { field: "notes", ref: null }, kind: "note", text: "" });
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

// ==========================================
// Eval cases

/** An eval case's name (the server's `EVAL_CASE_SLUG_PATTERN`) */
export const EVAL_CASE_SLUG = /^[a-z0-9][a-z0-9-]{0,63}$/;

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

/**
 * The review page's state for one job. Edits to `draft` autosave after `AUTOSAVE_DELAY_MS` with the draft version;
 * a stale version sets `conflict` (the page's "Reload this card" dialog). While a task runs the page polls the
 * job's state every `STATE_POLL_MS`, picks up proposals and a replaced draft, and sends queued re-reads one at a
 * time once the job is idle.
 */
export function useRecipeIngestReview(jobId: string, options: RecipeIngestReviewOptions) {
  const api = useUserApi();
  const i18n = useI18n();
  const text = useRecipeIngestText();
  const counts = useRecipeIngestCounts();

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
  /** The ⋯ menu action in flight (`rotate`, `reextract`, `retry`, `discard`, `eval`), so its button can spin */
  const pendingAction = ref<string | null>(null);
  /** Re-reads waiting for the job to be idle, sent one at a time */
  const rereadQueue = ref<RereadRequest[]>([]);

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

  async function sendSave() {
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
      return;
    }

    // keep what wasn't saved for the next try, unless it changed since
    for (const [id, resolution] of Object.entries(resolutions)) {
      if (!pendingResolutions.has(id)) {
        pendingResolutions.set(id, resolution);
      }
    }
    proposalIds.forEach(id => pendingProposalIds.add(id));
    pendingClearError ||= clearError;

    if (errorCodeOf(error) === "version_conflict") {
      // the 409 has no message, so nothing was toasted: the page shows its "Reload this card" dialog
      conflict.value = true;
      saveState.value = "idle";
      return;
    }
    saveState.value = "error";
  }

  /** Saves pending changes now; waits for a save in flight first. Does nothing in a conflict or when nothing changed. */
  async function save(): Promise<void> {
    while (saving) {
      await saving;
    }
    if (conflict.value || job.value?.status !== "ready" || !hasPendingChanges()) {
      return;
    }
    saving = sendSave().finally(() => {
      saving = null;
    });
    await saving;
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
    alert.error(code ? text.ingestErrorText(code) : i18n.t("events.something-went-wrong"));
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
      alert.info(i18n.t("recipe-ingest.review.busy"));
      return false;
    }
    notifyError(error);
    return false;
  }

  /** Re-reads a region; while a task runs (or other re-reads wait) it joins the queue */
  async function requestReread(request: RereadRequest) {
    if (task.value || rereadQueue.value.length > 0) {
      rereadQueue.value = [...rereadQueue.value, request];
      alert.info(i18n.t("recipe-ingest.review.reread-queued"));
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
        alert.info(text.ingestErrorText("busy"));
      }
      else {
        notifyError(error);
      }
      return false;
    });
  }

  /** Turns a page clockwise, then offers to read the card again */
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
        alert.info(i18n.t("recipe-ingest.review.rotate-hint"), null, {
          action: { message: i18n.t("recipe-ingest.review.read-again"), onClick: () => void reextract() },
        });
        return true;
      }
      if (!alreadyToasted(error) && errorStatusOf(error) === 409) {
        // a task is running: the page can't turn it under the reader
        alert.info(text.ingestErrorText("busy"));
      }
      else {
        notifyError(error);
      }
      return false;
    });
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

  /** The batch's next ready card (wrapping round), fresh from the server; else the queue */
  async function nextPath(preferred?: string | null): Promise<{ path: string; last: boolean }> {
    const fresh = await loadBatch();
    const next = preferred && preferred !== jobId ? preferred : nextCardInBatch(fresh?.jobs ?? batch.value?.jobs ?? [], jobId);
    return next ? { path: cardPath(next), last: false } : { path: queuePath(), last: true };
  }

  /** "Skip": the next card to review, keeping this one for later */
  async function skip() {
    await save();
    const { path, last } = await nextPath();
    if (last) {
      alert.info(i18n.t("recipe-ingest.review.last-card"));
    }
    await options.navigate(path);
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
      savedDraft.value = stableStringify(draft.value);
      pendingResolutions.clear();
      pendingProposalIds.clear();
      pendingClearError = false;
      alert.success(i18n.t("recipe-ingest.queue.discarded"));
      void counts.refresh();
      const { path } = await nextPath();
      await options.navigate(path);
      return true;
    });
  }

  /** Saves the reviewed card to the group's eval set (managers) */
  async function saveEvalCase(slug: string, verified: boolean): Promise<"saved" | "exists" | "failed"> {
    return (await runAction("eval", async () => {
      await save();
      const { data, error } = await api.recipeIngest.saveEvalCase(jobId, { slug, verified });
      if (data) {
        alert.success(i18n.t("recipe-ingest.eval.saved", { slug: data.slug }));
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
   * "Commit & next": waits for the pending save, commits with the version it returned, then goes to the batch's next
   * ready card, or after the last one to the queue filtered to the batch, which sums it up. Unresolved errors don't
   * commit: the result is `"fix"` and the page scrolls to the first.
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
      if (hasPendingChanges() && saveState.value === "error") {
        alert.error(i18n.t("recipe-ingest.review.save-failed"));
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
      // after the batch's last card the queue opens on the batch, whose own line sums it up
      const { path, last } = await nextPath(data.nextJobId);
      if (!last) {
        // the next card's page says it above its review bar: a toast would cover that page's header on phones
        leaveRecipeIngestCommitNotice({ text: added, warning });
      }
      else if (warning) {
        alert.warning(warning, added);
      }
      else {
        alert.success(added);
      }
      await options.navigate(path);
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

  onBeforeUnmount(() => {
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
    task,
    readOnly,
    position,
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
    pollState,
    resolveFlag,
    applyFlagAlternative,
    fillFlagBlank,
    useProposal,
    dismissProposal,
    dismissError,
    requestReread,
    reextract,
    rotate,
    retry,
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
