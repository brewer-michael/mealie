/**
 * The recipe card upload queue (docs/ai/PHASE2.md §1.1, §1.4): photos grouped into cards, one upload request per card,
 * two at a time, three retries with backoff, one browser re-encode after a 413, a batch created with the first card
 * and sealed once Done was tapped and every card of the batch has uploaded or failed for good.
 *
 * Everything lives at module level, so the queue keeps going while the user reviews a card and comes back. The
 * transitions are a pure reducer (`reduceUploadQueue`); the requests, timers and preview URLs live around it.
 * Fork-owned.
 */
import type { AxiosProgressEvent } from "axios";
import { computed, effectScope, markRaw, readonly, ref, shallowRef, watch } from "vue";
import type { EffectScope } from "vue";
import { useUserApi } from "~/composables/api";
import { errorCodeOf, errorStatusOf, useRecipeIngestCounts } from "~/composables/use-recipe-ingest";
import type { IngestedJob, IngestRejected, IngestResponse } from "~/lib/api/types/recipe-ingest";
import type { RecipeIngestAPI } from "~/lib/api/user/recipe-ingest";

/** One photo per card, or a front and a back */
export type CaptureMode = "one-side" | "front-and-back";

export const MAX_CONCURRENT_UPLOADS = 2;
/** Automatic retries after the first attempt; then the card waits for Retry */
export const MAX_AUTO_RETRIES = 3;
export const RETRY_BASE_DELAY_MS = 2000;
export const MAX_RETRY_DELAY_MS = 60_000;
/** A 413 re-encodes the card's photos once, to at most this many pixels on the long side */
export const REENCODE_MAX_SIDE = 3072;
export const REENCODE_QUALITY = 0.9;
/** The server's limit (`limits.maxPagesPerCard`) */
export const DEFAULT_MAX_PAGES_PER_CARD = 4;
export const CAPTURE_MODE_STORAGE_KEY = "mealie.recipe-ingest.capture-mode";

// ==========================================
// Grouping photos into cards (pure)

/** A card being put together from chosen photos, before it's queued */
export interface DraftCard<T = Blob> {
  key: string;
  /** Front first */
  photos: T[];
  /** Changed by Swap, Split or Join: re-pairing leaves it alone */
  locked?: boolean;
}

let keyCounter = 0;
function newKey(prefix: string): string {
  keyCounter += 1;
  return `${prefix}-${Date.now().toString(36)}-${keyCounter}`;
}

/** Photos into cards, in selection order: one each, or pairs (front, back) with a lone last front */
export function groupPhotosIntoCards<T>(photos: readonly T[], mode: CaptureMode): T[][] {
  if (mode === "one-side") {
    return photos.map(photo => [photo]);
  }
  const cards: T[][] = [];
  for (let i = 0; i < photos.length; i += 2) {
    cards.push(photos.slice(i, i + 2));
  }
  return cards;
}

/** Draft cards for chosen photos */
export function draftCards<T>(photos: readonly T[], mode: CaptureMode, makeKey = () => newKey("draft")): DraftCard<T>[] {
  return groupPhotosIntoCards(photos, mode).map(group => ({ key: makeKey(), photos: group }));
}

/**
 * Pairs `extra` photos and the unlocked cards right after `index` again, so one Split or Join fixes a pairing that
 * slipped by a photo. Cards the user already changed (locked) and everything after them stay as they are.
 */
function repairAfter<T>(
  cards: readonly DraftCard<T>[],
  index: number,
  extra: readonly T[],
  mode: CaptureMode,
  makeKey: () => string,
): DraftCard<T>[] {
  let end = index + 1;
  while (end < cards.length && !cards[end]?.locked) {
    end += 1;
  }
  const photos = [...extra, ...cards.slice(index + 1, end).flatMap(card => card.photos)];
  return [...cards.slice(0, index + 1), ...draftCards(photos, mode, makeKey), ...cards.slice(end)];
}

/** Swaps a card's front and back */
export function swapSides<T>(cards: readonly DraftCard<T>[], index: number): DraftCard<T>[] {
  const card = cards[index];
  if (!card || card.photos.length < 2) {
    return [...cards];
  }
  const photos = [card.photos[1], card.photos[0], ...card.photos.slice(2)] as T[];
  return cards.map((c, i) => (i === index ? { ...c, photos, locked: true } : c));
}

/** Keeps a card's front as a card of its own; its other photos pair up with the cards after it */
export function splitCard<T>(
  cards: readonly DraftCard<T>[],
  index: number,
  mode: CaptureMode,
  makeKey = () => newKey("draft"),
): DraftCard<T>[] {
  const card = cards[index];
  if (!card || card.photos.length < 2) {
    return [...cards];
  }
  const kept = cards.map((c, i) => (i === index ? { ...c, photos: card.photos.slice(0, 1), locked: true } : c));
  return repairAfter(kept, index, card.photos.slice(1), mode, makeKey);
}

/** Whether `joinCards` can add the next card's first photo to this card */
export function canJoin<T>(cards: readonly DraftCard<T>[], index: number, maxPages = DEFAULT_MAX_PAGES_PER_CARD) {
  const card = cards[index];
  return !!card && index + 1 < cards.length && card.photos.length < maxPages;
}

/** Adds the next card's first photo to this card (its back, or another page); the rest pair up again */
export function joinCards<T>(
  cards: readonly DraftCard<T>[],
  index: number,
  mode: CaptureMode,
  makeKey = () => newKey("draft"),
  maxPages = DEFAULT_MAX_PAGES_PER_CARD,
): DraftCard<T>[] {
  const next = cards[index + 1];
  if (!next || !canJoin(cards, index, maxPages)) {
    return [...cards];
  }
  const taken = next.photos.slice(0, 1);
  const left = next.photos.slice(1);
  const joined = cards.map((c, i) => (i === index ? { ...c, photos: [...c.photos, ...taken], locked: true } : c));
  if (next.locked) {
    // A card the user put together keeps its other photos
    return left.length
      ? joined.map((c, i) => (i === index + 1 ? { ...c, photos: left } : c))
      : joined.filter((_, i) => i !== index + 1);
  }
  return repairAfter(joined.filter((_, i) => i !== index + 1), index, left, mode, makeKey);
}

/** Drops a draft card and its photos */
export function removeCard<T>(cards: readonly DraftCard<T>[], index: number): DraftCard<T>[] {
  return cards.filter((_, i) => i !== index);
}

// ==========================================
// The queue reducer (pure)

export type UploadCardStatus = "waiting" | "uploading" | "retrying" | "re-encoding" | "done" | "failed";

export interface UploadBatch {
  /** The app's own key for the batch */
  key: string;
  /** The server's batch; created with the first card, replaced when a 202 names another one */
  serverId: string | null;
  /** Every server batch this batch's cards went to, oldest first: Done seals each of them */
  serverIds: string[];
  /** Keep this batch's cards on this server */
  localOnly: boolean;
  /** Done was tapped: seal once every card has settled */
  sealing: boolean;
  /** Server batches already sealed */
  sealedIds: string[];
  /** The next card's capture position */
  nextPosition: number;
}

export interface UploadCard {
  key: string;
  batchKey: string;
  /** Capture order in the batch, from 0 */
  position: number;
  /** Front first; dropped once the card is uploaded, except the front of a card with something to show */
  photos: readonly Blob[];
  status: UploadCardStatus;
  /** Upload progress, 0 to 1 */
  progress: number;
  /** Automatic retries used */
  retries: number;
  /** When a `retrying` card is due (ms since the epoch) */
  retryAt: number | null;
  /** Re-encoded after a 413 (only once) */
  reencoded: boolean;
  jobs: IngestedJob[];
  /** Photos the server didn't use */
  rejected: IngestRejected[];
  /** The earlier job holding the same card */
  duplicateOf: string | null;
  /** The API error code or rejection reason of the last failure; null for network and other errors */
  error: string | null;
  /** Whether Retry is worth offering */
  retryable: boolean;
}

export interface UploadQueueState {
  /** Oldest first */
  batches: UploadBatch[];
  /** In capture order */
  cards: UploadCard[];
  /** The batch new cards join; null until the first card, and again after Done */
  openBatchKey: string | null;
}

export type UploadQueueAction
  = | { type: "add-card"; key: string; batchKey: string; photos: readonly Blob[]; localOnly: boolean }
    | { type: "start"; key: string }
    | { type: "progress"; key: string; progress: number }
    | { type: "re-encoding"; key: string }
    | { type: "re-encoded"; key: string; photos: readonly Blob[] }
    | { type: "batch-created"; batchKey: string; serverId: string }
    | { type: "batch-lost"; batchKey: string; serverId: string }
    | { type: "uploaded"; key: string; response: IngestResponse }
    | { type: "attempt-failed"; key: string; error: string | null; retryAt: number }
    | { type: "failed"; key: string; error: string | null; retryable: boolean }
    | { type: "retry"; key: string }
    | { type: "remove"; key: string }
    | { type: "seal-requested"; batchKey: string }
    | { type: "sealed"; batchKey: string; serverId: string }
    | { type: "set-local-only"; batchKey: string; localOnly: boolean };

export function emptyUploadQueue(): UploadQueueState {
  return { batches: [], cards: [], openBatchKey: null };
}

/** Uploaded, or failed for good (until the user taps Retry) */
export function isSettled(card: UploadCard): boolean {
  return card.status === "done" || card.status === "failed";
}

/** Whether a card uploaded with something to show: an earlier scan, or photos the server didn't use */
export function hasNote(card: UploadCard): boolean {
  return card.status === "done" && (!!card.duplicateOf || card.rejected.length > 0);
}

function withServerId(batch: UploadBatch, serverId: string): UploadBatch {
  return {
    ...batch,
    serverId,
    serverIds: batch.serverIds.includes(serverId) ? batch.serverIds : [...batch.serverIds, serverId],
  };
}

function updateCard(state: UploadQueueState, key: string, update: (card: UploadCard) => UploadCard): UploadQueueState {
  return { ...state, cards: state.cards.map(card => (card.key === key ? update(card) : card)) };
}

function updateBatch(
  state: UploadQueueState,
  key: string,
  update: (batch: UploadBatch) => UploadBatch,
): UploadQueueState {
  return { ...state, batches: state.batches.map(batch => (batch.key === key ? update(batch) : batch)) };
}

/** After a seal: forget the batch's uploaded cards with nothing to show, and the batch once it has no cards left */
function pruneSealed(state: UploadQueueState, batchKey: string): UploadQueueState {
  const batch = state.batches.find(b => b.key === batchKey);
  const cards = state.cards.filter(c => c.batchKey === batchKey);
  if (!batch || batch.key === state.openBatchKey || !cards.every(isSettled)) {
    return state;
  }
  const kept = state.cards.filter(c => c.batchKey !== batchKey || c.status !== "done" || hasNote(c));
  const batches = kept.some(c => c.batchKey === batchKey) ? state.batches : state.batches.filter(b => b.key !== batchKey);
  return { ...state, cards: kept, batches };
}

function applyUploaded(state: UploadQueueState, key: string, response: IngestResponse): UploadQueueState {
  const card = state.cards.find(c => c.key === key);
  if (!card) {
    return state;
  }
  const jobs = response.jobs ?? [];
  const rejected = response.rejected ?? [];
  const duplicate = rejected.find(r => r.reason === "duplicate");

  let next = state;
  // A sealed batch is never reopened: the server put the card in another batch, which later cards join
  const batchId = response.batchId;
  if (batchId) {
    next = updateBatch(next, card.batchKey, batch => withServerId(batch, batchId));
  }

  if (!jobs.length && !duplicate) {
    // Nothing was usable, and sending the same photos again won't change that
    return updateCard(next, key, c => ({
      ...c,
      status: "failed",
      progress: 0,
      retryAt: null,
      rejected,
      error: rejected[0]?.reason ?? null,
      retryable: false,
    }));
  }
  return updateCard(next, key, (c) => {
    const done: UploadCard = {
      ...c,
      status: "done",
      progress: 1,
      retryAt: null,
      jobs,
      rejected,
      duplicateOf: duplicate?.duplicateOf ?? null,
      error: null,
      retryable: false,
    };
    // The photos aren't needed any more, except the front as the thumbnail of a card with something to show
    return { ...done, photos: hasNote(done) ? c.photos.slice(0, 1) : [] };
  });
}

export function reduceUploadQueue(state: UploadQueueState, action: UploadQueueAction): UploadQueueState {
  switch (action.type) {
    case "add-card": {
      let next = state;
      let batch = next.batches.find(b => b.key === action.batchKey);
      if (!batch) {
        batch = {
          key: action.batchKey,
          serverId: null,
          serverIds: [],
          localOnly: action.localOnly,
          sealing: false,
          sealedIds: [],
          nextPosition: 0,
        };
        next = { ...next, batches: [...next.batches, batch] };
      }
      const card: UploadCard = {
        key: action.key,
        batchKey: batch.key,
        position: batch.nextPosition,
        photos: action.photos,
        status: "waiting",
        progress: 0,
        retries: 0,
        retryAt: null,
        reencoded: false,
        jobs: [],
        rejected: [],
        duplicateOf: null,
        error: null,
        retryable: false,
      };
      next = updateBatch(next, batch.key, b => ({ ...b, nextPosition: b.nextPosition + 1 }));
      return {
        ...next,
        cards: [...next.cards, card],
        openBatchKey: batch.sealing ? next.openBatchKey : batch.key,
      };
    }
    case "start":
      return updateCard(state, action.key, c => ({ ...c, status: "uploading", progress: 0, retryAt: null }));
    case "progress":
      return updateCard(state, action.key, c => ({ ...c, progress: Math.min(Math.max(action.progress, 0), 1) }));
    case "re-encoding":
      return updateCard(state, action.key, c => ({ ...c, status: "re-encoding", progress: 0 }));
    case "re-encoded":
      return updateCard(state, action.key, c => ({
        ...c,
        photos: action.photos,
        status: "waiting",
        reencoded: true,
        progress: 0,
      }));
    case "batch-created":
      return updateBatch(state, action.batchKey, b => (b.serverId ? b : withServerId(b, action.serverId)));
    case "batch-lost":
      return updateBatch(state, action.batchKey, b => (b.serverId === action.serverId ? { ...b, serverId: null } : b));
    case "uploaded":
      return applyUploaded(state, action.key, action.response);
    case "attempt-failed":
      return updateCard(state, action.key, c => ({
        ...c,
        status: "retrying",
        progress: 0,
        retries: c.retries + 1,
        retryAt: action.retryAt,
        error: action.error,
        retryable: true,
      }));
    case "failed":
      return updateCard(state, action.key, c => ({
        ...c,
        status: "failed",
        progress: 0,
        retryAt: null,
        error: action.error,
        retryable: action.retryable,
      }));
    case "retry":
      return updateCard(state, action.key, c => (c.status === "failed" && c.retryable
        ? { ...c, status: "waiting", retries: 0, retryAt: null, error: null, rejected: [] }
        : c));
    case "remove": {
      const card = state.cards.find(c => c.key === action.key);
      if (!card || card.status === "uploading" || card.status === "re-encoding") {
        return state;
      }
      return pruneSealedIfDone({ ...state, cards: state.cards.filter(c => c.key !== action.key) }, card.batchKey);
    }
    case "seal-requested": {
      const next = updateBatch(state, action.batchKey, b => ({ ...b, sealing: true }));
      return { ...next, openBatchKey: next.openBatchKey === action.batchKey ? null : next.openBatchKey };
    }
    case "sealed": {
      const next = updateBatch(state, action.batchKey, b => (b.sealedIds.includes(action.serverId)
        ? b
        : { ...b, sealedIds: [...b.sealedIds, action.serverId] }));
      return pruneSealed(next, action.batchKey);
    }
    case "set-local-only":
      return updateBatch(state, action.batchKey, b => ({ ...b, localOnly: action.localOnly }));
  }
}

/** A sealed batch whose last card was removed goes too */
function pruneSealedIfDone(state: UploadQueueState, batchKey: string): UploadQueueState {
  const batch = state.batches.find(b => b.key === batchKey);
  if (!batch || !isBatchFinished(state, batch)) {
    return state;
  }
  return pruneSealed(state, batchKey);
}

/** Done was tapped, and every server batch the cards went to is sealed, with every card settled */
export function isBatchFinished(state: UploadQueueState, batch: UploadBatch): boolean {
  const settled = state.cards.filter(c => c.batchKey === batch.key).every(isSettled);
  return batch.sealing && settled && batch.serverIds.every(id => batch.sealedIds.includes(id));
}

/** Cards to start now: waiting ones, and retries that are due, in capture order, up to the free slots */
export function cardsToStart(state: UploadQueueState, now: number, limit = MAX_CONCURRENT_UPLOADS): UploadCard[] {
  const active = state.cards.filter(c => c.status === "uploading" || c.status === "re-encoding").length;
  const free = limit - active;
  if (free <= 0) {
    return [];
  }
  return state.cards
    .filter(c => c.status === "waiting" || (c.status === "retrying" && (c.retryAt ?? 0) <= now))
    .slice(0, free);
}

/**
 * Server batches to seal now: Done was tapped and every card of the batch has uploaded or failed for good. A batch
 * whose cards went to more than one server batch (a sealed batch is never reopened) seals each of them.
 */
export function batchesToSeal(state: UploadQueueState): { batchKey: string; serverId: string }[] {
  return state.batches
    .filter(batch => batch.sealing && state.cards.filter(c => c.batchKey === batch.key).every(isSettled))
    .flatMap(batch => batch.serverIds
      .filter(serverId => !batch.sealedIds.includes(serverId))
      .map(serverId => ({ batchKey: batch.key, serverId })));
}

/** When the next retry is due */
export function nextRetryAt(state: UploadQueueState): number | null {
  const due = state.cards.filter(c => c.status === "retrying" && c.retryAt !== null).map(c => c.retryAt as number);
  return due.length ? Math.min(...due) : null;
}

/** How long to wait before automatic retry `retry` (0-based): 2 s, 4 s, 8 s, or longer when the server says so */
export function retryDelay(retry: number, retryAfterSeconds: number | null = null): number {
  const backoff = RETRY_BASE_DELAY_MS * 2 ** retry;
  const asked = retryAfterSeconds ? retryAfterSeconds * 1000 : 0;
  return Math.min(Math.max(backoff, asked), MAX_RETRY_DELAY_MS);
}

// ==========================================
// Re-encoding after a 413

function jpegName(photo: Blob): string {
  const name = photo instanceof File && photo.name ? photo.name : "photo.jpg";
  return name.replace(/\.[^./]*$/, "") + ".jpg";
}

type Canvas2D = CanvasRenderingContext2D | OffscreenCanvasRenderingContext2D;

/** Draws the bitmap over white (a transparent PNG would turn black as a JPEG) */
function drawOnWhite(context: Canvas2D | null, bitmap: ImageBitmap, width: number, height: number) {
  if (!context) {
    throw new Error("No 2D canvas");
  }
  context.fillStyle = "#ffffff";
  context.fillRect(0, 0, width, height);
  context.drawImage(bitmap, 0, 0, width, height);
}

/**
 * A photo as a JPEG of at most `REENCODE_MAX_SIDE` pixels on the long side, upright as the browser shows it.
 * Rejects when the browser can't decode it.
 */
export async function reencodePhoto(photo: Blob): Promise<File> {
  const bitmap = await createImageBitmap(photo, { imageOrientation: "from-image" });
  try {
    const scale = Math.min(1, REENCODE_MAX_SIDE / Math.max(bitmap.width, bitmap.height));
    const width = Math.max(1, Math.round(bitmap.width * scale));
    const height = Math.max(1, Math.round(bitmap.height * scale));

    let blob: Blob | null;
    if (typeof OffscreenCanvas !== "undefined") {
      const canvas = new OffscreenCanvas(width, height);
      drawOnWhite(canvas.getContext("2d"), bitmap, width, height);
      blob = await canvas.convertToBlob({ type: "image/jpeg", quality: REENCODE_QUALITY });
    }
    else {
      const canvas = document.createElement("canvas");
      canvas.width = width;
      canvas.height = height;
      drawOnWhite(canvas.getContext("2d"), bitmap, width, height);
      blob = await new Promise<Blob | null>(resolve => canvas.toBlob(resolve, "image/jpeg", REENCODE_QUALITY));
    }
    if (!blob) {
      throw new Error("The photo couldn't be encoded");
    }
    return new File([blob], jpegName(photo), { type: "image/jpeg" });
  }
  finally {
    bitmap.close();
  }
}

// ==========================================
// The singleton

type BatchResult = { id: string } | { error: unknown };

const state = shallowRef<UploadQueueState>(emptyUploadQueue());
const drafts = shallowRef<DraftCard[]>([]);
/** The front of a two-sided card, waiting for its back */
const pendingFront = shallowRef<Blob | null>(null);
const modeRef = ref<CaptureMode | null>(null);
/** Keep the open batch's cards (and the next batch's) on this server */
const localOnlyRef = ref(false);
/** Counts successful uploads, so the job list can reload at once */
const uploadedCount = ref(0);
/** The server batch of the last successful upload */
const lastUploadBatchId = ref<string | null>(null);

let api: RecipeIngestAPI | null = null;
let refreshCounts: (() => Promise<unknown>) | null = null;
const batchRequests = new Map<string, Promise<BatchResult>>();
const sealsInFlight = new Set<string>();
const sealFailures = new Map<string, number>();
let retryTimer: ReturnType<typeof setTimeout> | null = null;
/** Bumped by a reset, so requests still in flight change nothing */
let generation = 0;
let scope: EffectScope | null = null;
const previews = new Map<Blob, string>();

const hasPending = computed(
  () => state.value.cards.some(card => !isSettled(card)) || drafts.value.length > 0 || pendingFront.value !== null,
);

function dispatch(action: UploadQueueAction) {
  state.value = reduceUploadQueue(state.value, action);
  releaseUnusedPreviews();
}

function findCard(key: string) {
  return state.value.cards.find(card => card.key === key);
}

function findBatch(key: string) {
  return state.value.batches.find(batch => batch.key === key);
}

function client(): RecipeIngestAPI {
  if (!api) {
    throw new Error("useRecipeIngestUploads() must be called before uploading");
  }
  return api;
}

// ---- preview URLs

/** An object URL showing a photo; revoked once the photo leaves the queue */
function previewUrl(photo: Blob): string {
  let url = previews.get(photo);
  if (url === undefined) {
    try {
      url = URL.createObjectURL(photo);
    }
    catch {
      // no object URLs here (server rendering, tests): no preview
      url = "";
    }
    previews.set(photo, url);
  }
  return url;
}

function releaseUnusedPreviews() {
  if (!previews.size) {
    return;
  }
  const used = new Set<Blob>([
    ...state.value.cards.flatMap(card => card.photos),
    ...drafts.value.flatMap(card => card.photos),
    ...(pendingFront.value ? [pendingFront.value] : []),
  ]);
  for (const [photo, url] of previews) {
    if (!used.has(photo)) {
      if (url && typeof URL.revokeObjectURL === "function") {
        URL.revokeObjectURL(url);
      }
      previews.delete(photo);
    }
  }
}

// ---- leaving the page with photos pending

function warnBeforeUnload(event: BeforeUnloadEvent) {
  event.preventDefault();
  // Browsers show their own text; some still want a value set
  event.returnValue = "";
}

function ensureScope() {
  if (scope) {
    return;
  }
  scope = effectScope(true);
  scope.run(() => {
    watch(hasPending, (pending) => {
      if (typeof window === "undefined") {
        return;
      }
      if (pending) {
        window.addEventListener("beforeunload", warnBeforeUnload);
      }
      else {
        window.removeEventListener("beforeunload", warnBeforeUnload);
      }
    }, { immediate: true });
  });
}

// ---- requests

function retryAfterSeconds(error: unknown): number | null {
  const headers = (error as { response?: { headers?: Record<string, unknown> } } | null)?.response?.headers;
  const value = Number(headers?.["retry-after"]);
  return Number.isFinite(value) && value > 0 ? value : null;
}

/** A failed upload's `detail` when it is an upload result (a 400 where no photo was accepted) */
function rejectedBody(error: unknown): IngestResponse | null {
  const detail = (error as { response?: { data?: { detail?: unknown } } } | null)?.response?.data?.detail;
  if (detail && typeof detail === "object" && Array.isArray((detail as IngestResponse).rejected)) {
    return detail as IngestResponse;
  }
  return null;
}

/** Network errors, timeouts, rate limits, a restore's pause and server errors are worth retrying on their own */
function isTransient(status: number | null): boolean {
  return status === null || status === 404 || status === 408 || status === 425 || status === 429 || status >= 500;
}

async function ensureBatch(batchKey: string, gen: number): Promise<BatchResult> {
  const batch = findBatch(batchKey);
  if (batch?.serverId) {
    return { id: batch.serverId };
  }
  let request = batchRequests.get(batchKey);
  if (!request) {
    request = (async (): Promise<BatchResult> => {
      try {
        const { data, error } = await client().createBatch();
        if (data?.id) {
          if (gen === generation) {
            dispatch({ type: "batch-created", batchKey, serverId: data.id });
          }
          return { id: findBatch(batchKey)?.serverId ?? data.id };
        }
        return { error };
      }
      finally {
        batchRequests.delete(batchKey);
      }
    })();
    batchRequests.set(batchKey, request);
  }
  return await request;
}

function afterUploaded(response: IngestResponse) {
  lastUploadBatchId.value = response.batchId ?? null;
  uploadedCount.value += 1;
  void refreshCounts?.();
}

async function handleFailure(key: string, error: unknown, batchId: string | null, gen: number) {
  const card = findCard(key);
  if (!card) {
    return;
  }
  const status = errorStatusOf(error);
  const code = errorCodeOf(error);

  if (status === 413) {
    if (card.reencoded) {
      dispatch({ type: "failed", key, error: code ?? "too_large", retryable: false });
      return;
    }
    // Usually a proxy's body limit: the card's photos go again, smaller
    dispatch({ type: "re-encoding", key });
    let photos: File[];
    try {
      photos = await Promise.all(card.photos.map(photo => reencodePhoto(photo)));
    }
    catch {
      if (gen === generation) {
        dispatch({ type: "failed", key, error: code ?? "too_large", retryable: false });
      }
      return;
    }
    if (gen === generation) {
      dispatch({ type: "re-encoded", key, photos: photos.map(photo => markRaw(photo)) });
    }
    return;
  }

  const body = status === 400 ? rejectedBody(error) : null;
  if (body) {
    dispatch({ type: "uploaded", key, response: body });
    return;
  }

  if (status === 404 && batchId) {
    // The batch is gone (discarded elsewhere, a restore): the next attempt creates another one
    dispatch({ type: "batch-lost", batchKey: card.batchKey, serverId: batchId });
  }

  if (isTransient(status) && card.retries < MAX_AUTO_RETRIES) {
    const retryAt = Date.now() + retryDelay(card.retries, retryAfterSeconds(error));
    dispatch({ type: "attempt-failed", key, error: code, retryAt });
    return;
  }
  // Errors that carry a `detail.message` were already toasted by the axios interceptor: the card just says so
  dispatch({ type: "failed", key, error: code, retryable: true });
}

async function runCard(key: string) {
  const gen = generation;
  dispatch({ type: "start", key });
  try {
    const card = findCard(key);
    if (!card) {
      return;
    }
    const batch = await ensureBatch(card.batchKey, gen);
    if (gen !== generation) {
      return;
    }
    if ("error" in batch) {
      await handleFailure(key, batch.error, null, gen);
      return;
    }

    const localOnly = findBatch(card.batchKey)?.localOnly ?? false;
    const { data, error } = await client().upload(
      card.photos,
      { batchId: batch.id, position: card.position, localOnly },
      {
        onUploadProgress: (event: AxiosProgressEvent) => {
          if (event.total && gen === generation) {
            dispatch({ type: "progress", key, progress: event.loaded / event.total });
          }
        },
      },
    );
    if (gen !== generation) {
      return;
    }
    if (data) {
      dispatch({ type: "uploaded", key, response: data });
      if (data.jobs?.length) {
        afterUploaded(data);
      }
    }
    else {
      await handleFailure(key, error, batch.id, gen);
    }
  }
  catch (error) {
    console.error(error);
    if (gen === generation) {
      dispatch({ type: "failed", key, error: null, retryable: true });
    }
  }
  finally {
    if (gen === generation) {
      pump();
    }
  }
}

async function sealBatch(batchKey: string, serverId: string) {
  if (sealsInFlight.has(serverId)) {
    return;
  }
  const gen = generation;
  sealsInFlight.add(serverId);
  try {
    const { error } = await client().sealBatch(serverId);
    if (gen !== generation) {
      return;
    }
    const failures = (sealFailures.get(serverId) ?? 0) + (error ? 1 : 0);
    // A batch that can't be sealed seals itself after 10 idle minutes, so three tries are enough
    if (!error || failures >= MAX_AUTO_RETRIES || errorStatusOf(error) === 404) {
      sealFailures.delete(serverId);
      dispatch({ type: "sealed", batchKey, serverId });
    }
    else {
      sealFailures.set(serverId, failures);
      setTimeout(() => gen === generation && pump(), retryDelay(failures - 1));
    }
  }
  finally {
    sealsInFlight.delete(serverId);
  }
}

function scheduleRetries() {
  if (retryTimer !== null) {
    clearTimeout(retryTimer);
    retryTimer = null;
  }
  const due = nextRetryAt(state.value);
  if (due !== null) {
    retryTimer = setTimeout(() => {
      retryTimer = null;
      pump();
    }, Math.max(0, due - Date.now()));
  }
}

/** Starts what can start: seals that are due, then cards up to the free upload slots */
function pump() {
  for (const { batchKey, serverId } of batchesToSeal(state.value)) {
    void sealBatch(batchKey, serverId);
  }
  for (const card of cardsToStart(state.value, Date.now())) {
    void runCard(card.key);
  }
  scheduleRetries();
}

// ---- capture

function readMode(): CaptureMode {
  try {
    return localStorage.getItem(CAPTURE_MODE_STORAGE_KEY) === "front-and-back" ? "front-and-back" : "one-side";
  }
  catch {
    return "one-side";
  }
}

function currentMode(): CaptureMode {
  if (modeRef.value === null) {
    modeRef.value = readMode();
  }
  return modeRef.value;
}

/** Queues a complete card: it uploads as soon as a slot is free */
function enqueueCard(photos: readonly Blob[]) {
  if (!photos.length) {
    return;
  }
  dispatch({
    type: "add-card",
    key: newKey("card"),
    batchKey: state.value.openBatchKey ?? newKey("batch"),
    photos: photos.map(photo => markRaw(photo)),
    localOnly: localOnlyRef.value,
  });
  pump();
}

function setMode(mode: CaptureMode) {
  if (mode === currentMode()) {
    return;
  }
  // A front waiting for its back becomes a one-sided card
  if (mode === "one-side" && pendingFront.value) {
    const front = pendingFront.value;
    pendingFront.value = null;
    enqueueCard([front]);
  }
  modeRef.value = mode;
  try {
    localStorage.setItem(CAPTURE_MODE_STORAGE_KEY, mode);
  }
  catch {
    // private browsing: the choice lasts for this visit
  }
  drafts.value = draftCards(drafts.value.flatMap(card => card.photos), mode);
}

/** A photo from the camera: a card of its own, the front of a card, or that front's back */
function takePhoto(photo: Blob) {
  if (currentMode() === "one-side") {
    enqueueCard([photo]);
    return;
  }
  if (pendingFront.value) {
    const front = pendingFront.value;
    pendingFront.value = null;
    enqueueCard([front, photo]);
  }
  else {
    pendingFront.value = markRaw(photo);
  }
}

/** Replaces the front still waiting for its back */
function retake(photo: Blob) {
  if (pendingFront.value) {
    pendingFront.value = markRaw(photo);
    releaseUnusedPreviews();
  }
  else {
    takePhoto(photo);
  }
}

/** The front waiting for its back is a one-sided card */
function noBack() {
  const front = pendingFront.value;
  if (front) {
    pendingFront.value = null;
    enqueueCard([front]);
  }
}

/** Chosen or dropped photos become draft cards, paired in selection order */
function addPhotos(photos: readonly Blob[]) {
  if (!photos.length) {
    return;
  }
  const all = [...drafts.value.flatMap(card => card.photos), ...photos.map(photo => markRaw(photo))];
  // Locked cards keep their shape; the new photos pair up after the last of them
  let keptCount = drafts.value.length;
  while (keptCount > 0 && !drafts.value[keptCount - 1]?.locked) {
    keptCount -= 1;
  }
  const kept = drafts.value.slice(0, keptCount);
  const loose = all.slice(kept.flatMap(card => card.photos).length);
  drafts.value = [...kept, ...draftCards(loose, currentMode())];
}

function editDrafts(edit: (cards: DraftCard[], mode: CaptureMode) => DraftCard[]) {
  drafts.value = edit(drafts.value, currentMode());
  releaseUnusedPreviews();
}

/** Queues every draft card */
function uploadDrafts() {
  const cards = drafts.value;
  drafts.value = [];
  cards.forEach(card => enqueueCard(card.photos));
}

/**
 * Done: a front waiting for its back and the draft cards are queued, and the batch is sealed once every card has
 * uploaded or failed for good. Photos taken after this start a new batch.
 */
function done() {
  noBack();
  uploadDrafts();
  const batchKey = state.value.openBatchKey;
  if (batchKey) {
    dispatch({ type: "seal-requested", batchKey });
  }
  pump();
}

function setLocalOnly(localOnly: boolean) {
  localOnlyRef.value = localOnly;
  const batchKey = state.value.openBatchKey;
  if (batchKey) {
    dispatch({ type: "set-local-only", batchKey, localOnly });
  }
}

function retry(key: string) {
  dispatch({ type: "retry", key });
  pump();
}

function remove(key: string) {
  dispatch({ type: "remove", key });
  pump();
}

/** The upload queue, shared by every component (docs/ai/PHASE2.md §1.1) */
export function useRecipeIngestUploads() {
  if (!api) {
    api = useUserApi().recipeIngest;
  }
  if (!refreshCounts) {
    refreshCounts = useRecipeIngestCounts().refresh;
  }
  currentMode();
  ensureScope();

  const openBatch = computed(() => state.value.batches.find(b => b.key === state.value.openBatchKey) ?? null);

  return {
    cards: computed(() => state.value.cards),
    batches: computed(() => state.value.batches),
    openBatch,
    /** Cards captured in the open batch */
    openBatchCardCount: computed(() => state.value.cards.filter(c => c.batchKey === state.value.openBatchKey).length),
    drafts: computed(() => drafts.value),
    pendingFront: computed(() => pendingFront.value),
    mode: computed<CaptureMode>({ get: () => modeRef.value ?? "one-side", set: setMode }),
    localOnly: computed<boolean>({ get: () => localOnlyRef.value, set: setLocalOnly }),
    /** Photos not uploaded yet, or not yet sent */
    hasPending,
    /** Cards waiting, uploading or about to retry */
    isUploading: computed(() => state.value.cards.some(card => !isSettled(card))),
    /** Server batches with cards still on their way */
    uploadingBatchIds: computed(() => {
      const keys = new Set(state.value.cards.filter(card => !isSettled(card)).map(card => card.batchKey));
      return state.value.batches.filter(b => keys.has(b.key) && b.serverId).map(b => b.serverId as string);
    }),
    uploadedCount: readonly(uploadedCount),
    lastUploadBatchId: readonly(lastUploadBatchId),
    previewUrl,
    takePhoto,
    retake,
    noBack,
    addPhotos,
    swapDraft: (index: number) => editDrafts(cards => swapSides(cards, index)),
    splitDraft: (index: number) => editDrafts((cards, mode) => splitCard(cards, index, mode)),
    joinDraft: (index: number, maxPages = DEFAULT_MAX_PAGES_PER_CARD) =>
      editDrafts((cards, mode) => joinCards(cards, index, mode, undefined, maxPages)),
    removeDraft: (index: number) => editDrafts(cards => removeCard(cards, index)),
    uploadDrafts,
    done,
    retry,
    remove,
  };
}

/** Forgets the queue (between tests, and on logout); requests still in flight then change nothing */
export function resetRecipeIngestUploads() {
  generation += 1;
  if (retryTimer !== null) {
    clearTimeout(retryTimer);
    retryTimer = null;
  }
  scope?.stop();
  scope = null;
  if (typeof window !== "undefined") {
    window.removeEventListener("beforeunload", warnBeforeUnload);
  }
  state.value = emptyUploadQueue();
  drafts.value = [];
  pendingFront.value = null;
  modeRef.value = null;
  localOnlyRef.value = false;
  uploadedCount.value = 0;
  lastUploadBatchId.value = null;
  batchRequests.clear();
  sealsInFlight.clear();
  sealFailures.clear();
  releaseUnusedPreviews();
  api = null;
  refreshCounts = null;
}
