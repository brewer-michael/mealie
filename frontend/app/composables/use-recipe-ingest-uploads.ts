/**
 * The recipe card upload queue (docs/ai/PHASE2.md §1.1, §1.4): photos grouped into cards, one upload request per card,
 * two at a time, three retries with backoff, one browser re-encode when the server finds a photo too large, a batch
 * created with the first card and sealed once Done was tapped and every card of the batch has uploaded or failed for
 * good. While the capture page is shown, its open batch is touched every 3 minutes, so a pause in a stack doesn't end
 * the batch (the server ends an app batch after 10 idle minutes).
 *
 * Everything lives at module level, so the queue keeps going while the user reviews a card and comes back, and it is
 * kept in IndexedDB per user (`use-recipe-ingest-upload-storage.ts`), so a reload or a closed tab resumes it. One tab
 * of the browser keeps the user's queue at a time: another of their tabs neither reads it back nor sends it, and its
 * cards page offers "Use this tab". The transitions are a pure reducer (`reduceUploadQueue`); the requests, timers,
 * storage and previews live around it. Fork-owned.
 */
import type { AxiosProgressEvent } from "axios";
import { computed, effectScope, markRaw, readonly, ref, shallowRef, watch } from "vue";
import type { EffectScope } from "vue";
import { useUserApi } from "~/composables/api";
import {
  errorCodeOf,
  errorStatusOf,
  recipeIngestSignedIn,
  resetRecipeIngestCounts,
  resetRecipeIngestReviewState,
  resetRecipeIngestSettings,
  runRecipeIngestLogoutTasks,
  useRecipeIngestCounts,
} from "~/composables/use-recipe-ingest";
import { inspectScanFile, mayBeDocument } from "~/composables/use-recipe-ingest-files";
import type { ScanFile } from "~/composables/use-recipe-ingest-files";
import { resetCarriedReviewNotice } from "~/composables/use-recipe-ingest-review";
import {
  QueueTakenError,
  openQueueChannel,
  openQueueLock,
  openUploadStorage,
  uploadStorageName,
} from "~/composables/use-recipe-ingest-upload-storage";
import type { QueueLock, UploadStorage } from "~/composables/use-recipe-ingest-upload-storage";
import type { IngestedJob, IngestRejected, IngestResponse } from "~/lib/api/types/recipe-ingest";
import type { RecipeIngestAPI } from "~/lib/api/user/recipe-ingest";

/** One photo per card, or a front and a back */
export type CaptureMode = "one-side" | "front-and-back";

export const MAX_CONCURRENT_UPLOADS = 2;
/** Automatic retries after the first attempt; then the card waits for Retry */
export const MAX_AUTO_RETRIES = 3;
export const RETRY_BASE_DELAY_MS = 2000;
export const MAX_RETRY_DELAY_MS = 60_000;
/**
 * A 413, or a photo refused as too large or with too many pixels, re-encodes the card's photos once, to at most this
 * many pixels on the long side
 */
export const REENCODE_MAX_SIDE = 3072;
export const REENCODE_QUALITY = 0.9;
/** Data saver: photos go at most this size, the server's page size (`page.jpg`), so nothing it keeps is lost */
export const DATA_SAVER_MAX_SIDE = 4096;
/** The card's error when the server found a photo too large and this browser can't decode it to make it smaller */
export const CANNOT_SHRINK = "cannot-shrink";
/** Thumbnails are made once per photo at this width (the tray shows 72 px), never from the full photo */
export const PREVIEW_WIDTH = 320;
export const PREVIEW_QUALITY = 0.8;
/** Thumbnails decoded at a time: each decode holds a full photo (48-190 MB) for a moment */
export const PREVIEW_CONCURRENCY = 2;
/** The server's limit (`limits.maxPagesPerCard`) */
export const DEFAULT_MAX_PAGES_PER_CARD = 4;
/** How often the capture page touches its open batch: well inside the server's 10 idle minutes for an app batch */
export const BATCH_HEARTBEAT_MS = 3 * 60_000;
/** How often a cards page in a tab that doesn't keep the queue asks for it (the tab keeping it lets go when it's idle) */
export const QUEUE_WANT_MS = 10_000;
/** "Use this tab" waits this long for the tab keeping the queue to hand it over, then takes it */
export const QUEUE_HAND_OVER_MS = 2000;
export const CAPTURE_MODE_STORAGE_KEY = "mealie.recipe-ingest.capture-mode";
export const DATA_SAVER_STORAGE_KEY = "mealie.recipe-ingest.data-saver";
/** "Keep these cards on this server", per user: `<key>.<user id>` */
export const LOCAL_ONLY_STORAGE_KEY = "mealie.recipe-ingest.local-only";

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

/** The card pages a file holds: 1 for a photo, a PDF's or a multi-page TIFF's pages; null when they aren't known */
export type PagesOf<T> = (photo: T) => number | null;

const onePageEach = () => 1;

/** The pages of a card's files; null when one of them isn't known */
export function cardPages<T>(photos: readonly T[], pages: PagesOf<T> = onePageEach): number | null {
  let total = 0;
  for (const photo of photos) {
    const count = pages(photo);
    if (count === null) {
      return null;
    }
    total += count;
  }
  return total;
}

/**
 * Photos into cards, in selection order: one each, or pairs (front, back) with a lone last front. A document of
 * several pages (a PDF, a multi-page TIFF), or one whose pages aren't known, is a card of its own: its pages are the
 * card's sides, as the server and the inbox take it. A one-page document pairs like a photo.
 */
export function groupPhotosIntoCards<T>(
  photos: readonly T[],
  mode: CaptureMode,
  pages: PagesOf<T> = onePageEach,
): T[][] {
  if (mode === "one-side") {
    return photos.map(photo => [photo]);
  }
  const cards: T[][] = [];
  let front: T[] | null = null;
  for (const photo of photos) {
    if (pages(photo) !== 1) {
      if (front) {
        cards.push(front);
        front = null;
      }
      cards.push([photo]);
    }
    else if (front) {
      cards.push([...front, photo]);
      front = null;
    }
    else {
      front = [photo];
    }
  }
  if (front) {
    cards.push(front);
  }
  return cards;
}

/** Draft cards for chosen photos */
export function draftCards<T>(
  photos: readonly T[],
  mode: CaptureMode,
  makeKey = () => newKey("draft"),
  pages: PagesOf<T> = onePageEach,
): DraftCard<T>[] {
  return groupPhotosIntoCards(photos, mode, pages).map(group => ({ key: makeKey(), photos: group }));
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
  pages: PagesOf<T>,
): DraftCard<T>[] {
  let end = index + 1;
  while (end < cards.length && !cards[end]?.locked) {
    end += 1;
  }
  const photos = [...extra, ...cards.slice(index + 1, end).flatMap(card => card.photos)];
  return [...cards.slice(0, index + 1), ...draftCards(photos, mode, makeKey, pages), ...cards.slice(end)];
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
  pages: PagesOf<T> = onePageEach,
): DraftCard<T>[] {
  const card = cards[index];
  if (!card || card.photos.length < 2) {
    return [...cards];
  }
  const kept = cards.map((c, i) => (i === index ? { ...c, photos: card.photos.slice(0, 1), locked: true } : c));
  return repairAfter(kept, index, card.photos.slice(1), mode, makeKey, pages);
}

/**
 * Whether `joinCards` can add the next card's first photo to this card: the pages of both must be known and fit the
 * server's limit
 */
export function canJoin<T>(
  cards: readonly DraftCard<T>[],
  index: number,
  maxPages = DEFAULT_MAX_PAGES_PER_CARD,
  pages: PagesOf<T> = onePageEach,
) {
  const card = cards[index];
  const next = cards[index + 1]?.photos[0];
  if (!card || next === undefined) {
    return false;
  }
  const have = cardPages(card.photos, pages);
  const adding = pages(next);
  return have !== null && adding !== null && have + adding <= maxPages;
}

/** Adds the next card's first photo to this card (its back, or another page); the rest pair up again */
export function joinCards<T>(
  cards: readonly DraftCard<T>[],
  index: number,
  mode: CaptureMode,
  makeKey = () => newKey("draft"),
  maxPages = DEFAULT_MAX_PAGES_PER_CARD,
  pages: PagesOf<T> = onePageEach,
): DraftCard<T>[] {
  const next = cards[index + 1];
  if (!next || !canJoin(cards, index, maxPages, pages)) {
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
  return repairAfter(joined.filter((_, i) => i !== index + 1), index, left, mode, makeKey, pages);
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
  /**
   * "Keep these cards on this server" as the card's current upload request carried it; null until the request goes.
   * The server stores it with the card, so the switch only reaches cards that haven't gone yet.
   */
  localOnly: boolean | null;
  /** Automatic retries used */
  retries: number;
  /** When a `retrying` card is due (ms since the epoch) */
  retryAt: number | null;
  /** Re-encoded because the server found a photo too large (only once) */
  reencoded: boolean;
  /** Made smaller before the upload (data saver) */
  downscaled: boolean;
  /** Sent with `allowDuplicate` ("Scan again" on a card already scanned) */
  allowDuplicate: boolean;
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
  = | { type: "add-card"; key: string; batchKey: string; photos: readonly Blob[] }
    | { type: "start"; key: string }
    | { type: "sending"; key: string; localOnly: boolean }
    | { type: "progress"; key: string; progress: number }
    | { type: "re-encoding"; key: string }
    | { type: "re-encoded"; key: string; photos: readonly Blob[] }
    | { type: "downscaled"; key: string; photos: readonly Blob[] }
    | { type: "batch-created"; batchKey: string; serverId: string }
    | { type: "batch-lost"; batchKey: string; serverId: string }
    | { type: "batch-ended"; batchKey: string; serverId: string }
    | { type: "uploaded"; key: string; response: IngestResponse }
    | { type: "attempt-failed"; key: string; error: string | null; retryAt: number }
    | { type: "failed"; key: string; error: string | null; retryable: boolean }
    | { type: "retry"; key: string }
    | { type: "scan-again"; key: string }
    | { type: "remove"; key: string }
    | { type: "seal-requested"; batchKey: string }
    | { type: "sealed"; batchKey: string; serverId: string };

export function emptyUploadQueue(): UploadQueueState {
  return { batches: [], cards: [], openBatchKey: null };
}

/** Uploaded, or failed for good (until the user taps Retry) */
export function isSettled(card: UploadCard): boolean {
  return card.status === "done" || card.status === "failed";
}

/** Failed for good: none of its photos could be used, and sending them again won't change that */
export function isRefused(card: UploadCard): boolean {
  return card.status === "failed" && !card.retryable;
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
    // The photos aren't needed any more, except all of a card already scanned (for Scan again) and the front as the
    // thumbnail of a card with photos the server didn't use
    const photos = done.duplicateOf ? c.photos : hasNote(done) ? c.photos.slice(0, 1) : [];
    return { ...done, photos };
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
        localOnly: null,
        retries: 0,
        retryAt: null,
        reencoded: false,
        downscaled: false,
        allowDuplicate: false,
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
      return updateCard(state, action.key, c => ({ ...c, status: "uploading", progress: 0, retryAt: null, localOnly: null }));
    case "sending":
      return updateCard(state, action.key, c => ({ ...c, localOnly: action.localOnly }));
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
    case "downscaled":
      return updateCard(state, action.key, c => ({ ...c, photos: action.photos, downscaled: true }));
    case "batch-created":
      return updateBatch(state, action.batchKey, b => (b.serverId ? b : withServerId(b, action.serverId)));
    case "batch-lost":
      return updateBatch(state, action.batchKey, b => (b.serverId === action.serverId ? { ...b, serverId: null } : b));
    case "batch-ended":
      // sealed on the server (idle while the page was away) or gone: it's neither touched nor sealed again, and the
      // next card starts a new server batch
      return updateBatch(state, action.batchKey, b => ({
        ...b,
        serverId: b.serverId === action.serverId ? null : b.serverId,
        sealedIds: b.sealedIds.includes(action.serverId) ? b.sealedIds : [...b.sealedIds, action.serverId],
      }));
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
    case "scan-again":
      // queued again even though the server has the same photos: it was asked to, by the user
      return updateCard(state, action.key, c => (c.status === "done" && c.duplicateOf && c.photos.length
        ? {
            ...c,
            status: "waiting",
            progress: 0,
            retries: 0,
            retryAt: null,
            allowDuplicate: true,
            duplicateOf: null,
            rejected: [],
            jobs: [],
            error: null,
          }
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
// Re-encoding and thumbnails

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

/** The bitmap drawn at `width` x `height`, as a JPEG */
async function encodeJpeg(bitmap: ImageBitmap, width: number, height: number, quality: number): Promise<Blob> {
  let blob: Blob | null;
  if (typeof OffscreenCanvas !== "undefined") {
    const canvas = new OffscreenCanvas(width, height);
    drawOnWhite(canvas.getContext("2d"), bitmap, width, height);
    blob = await canvas.convertToBlob({ type: "image/jpeg", quality });
  }
  else {
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    drawOnWhite(canvas.getContext("2d"), bitmap, width, height);
    blob = await new Promise<Blob | null>(resolve => canvas.toBlob(resolve, "image/jpeg", quality));
  }
  if (!blob) {
    throw new Error("The photo couldn't be encoded");
  }
  return blob;
}

/**
 * A photo as a JPEG of at most `maxSide` pixels on the long side, upright as the browser shows it. Rejects when the
 * browser can't decode it (HEIC outside Safari).
 */
export async function reencodePhoto(photo: Blob, maxSide = REENCODE_MAX_SIDE): Promise<File> {
  const bitmap = await createImageBitmap(photo, { imageOrientation: "from-image" });
  try {
    const scale = Math.min(1, maxSide / Math.max(bitmap.width, bitmap.height));
    const width = Math.max(1, Math.round(bitmap.width * scale));
    const height = Math.max(1, Math.round(bitmap.height * scale));
    const blob = await encodeJpeg(bitmap, width, height, REENCODE_QUALITY);
    return new File([blob], jpegName(photo), { type: "image/jpeg" });
  }
  finally {
    bitmap.close();
  }
}

/**
 * Data saver: the photo at most `DATA_SAVER_MAX_SIDE` on the long side, when that makes it smaller. A photo this
 * browser can't decode goes as it is.
 */
export async function shrinkForUpload(photo: Blob): Promise<Blob> {
  try {
    const smaller = await reencodePhoto(photo, DATA_SAVER_MAX_SIDE);
    return smaller.size < photo.size ? smaller : photo;
  }
  catch {
    return photo;
  }
}

/**
 * A thumbnail of the photo, `width` pixels wide, upright as the browser shows it: the browser decodes the photo
 * straight to that size (`resizeWidth`), and one that ignores the option is scaled on the canvas. Only the small JPEG
 * is kept, so a tray of 40 photos holds 40 small images instead of 40 decoded photos. Rejects when the browser can't
 * decode the photo (HEIC outside Safari, a PDF).
 */
export async function makePreview(photo: Blob, width = PREVIEW_WIDTH): Promise<Blob> {
  const bitmap = await createImageBitmap(photo, {
    imageOrientation: "from-image",
    resizeWidth: width,
    resizeQuality: "medium",
  });
  try {
    const scale = Math.min(1, width / bitmap.width);
    const scaledWidth = Math.max(1, Math.round(bitmap.width * scale));
    const scaledHeight = Math.max(1, Math.round(bitmap.height * scale));
    return await encodeJpeg(bitmap, scaledWidth, scaledHeight, PREVIEW_QUALITY);
  }
  finally {
    bitmap.close();
  }
}

/** The name a photo was chosen or taken with; "" for one without */
export function photoName(photo: Blob): string {
  return photo instanceof File ? photo.name : "";
}

// ==========================================
// The singleton

type BatchResult = { id: string } | { error: unknown };

const state = shallowRef<UploadQueueState>(emptyUploadQueue());
const drafts = shallowRef<DraftCard[]>([]);
/** The front of a two-sided card, waiting for its back */
const pendingFront = shallowRef<Blob | null>(null);
const modeRef = ref<CaptureMode | null>(null);
/** "Keep these cards on this server": every upload request carries it as it is when the request goes */
const localOnlyRef = ref(false);
/** Data saver, as this browser remembers it; null until read */
const dataSaverRef = ref<boolean | null>(null);
/** Counts successful uploads, so the job list can reload at once */
const uploadedCount = ref(0);
/** The server batch of the last successful upload */
const lastUploadBatchId = ref<string | null>(null);
/** The cards that had gone with the other setting when "Keep these cards on this server" last changed */
const sentBeforeChange = shallowRef<ReadonlySet<string>>(new Set());
/** The last change of "Keep these cards on this server" finished the open batch; until the next photo or Done */
const finishedBySwitch = ref(false);
/** Cards that failed for good while no cards page was open, since one was last opened */
const failedAway = ref(0);
/** The cards pages open now (they show a failed card themselves) */
let cardsPageViews = 0;
/** The queue couldn't be kept on this device: it lives in memory only (shown once) */
const storageFailedNotice = ref(false);

let api: RecipeIngestAPI | null = null;
let refreshCounts: (() => Promise<unknown>) | null = null;
const batchRequests = new Map<string, Promise<BatchResult>>();
const sealsInFlight = new Set<string>();
const sealFailures = new Map<string, number>();
let retryTimer: ReturnType<typeof setTimeout> | null = null;
/** Capture pages shown now: while there's one, the open batch is touched every `BATCH_HEARTBEAT_MS` */
let heartbeatHolders = 0;
let heartbeatTimer: ReturnType<typeof setTimeout> | null = null;
let heartbeatInFlight = false;
/** The uploads in flight, aborted by a reset (logout), so no photo leaves after its user has gone */
const uploadsInFlight = new Set<AbortController>();
/** Bumped by a reset, so requests still in flight change nothing */
let generation = 0;
let scope: EffectScope | null = null;

const hasPending = computed(
  () => state.value.cards.some(card => !isSettled(card)) || drafts.value.length > 0 || pendingFront.value !== null,
);

/** Whether this tab keeps the user's queue, another of their tabs does, or neither is settled (no user, or not yet) */
const keeper = ref<"here" | "elsewhere" | null>(null);
/** Photos not uploaded yet in the tab that keeps the queue, when that's another tab (it says so on every change) */
const photosElsewhere = ref(0);

/** The photos of this tab's queue a logout would drop */
const photosHere = computed(() =>
  state.value.cards
    .filter(card => card.status !== "done" && !isRefused(card))
    .reduce((count, card) => count + card.photos.length, 0)
    + drafts.value.reduce((count, card) => count + card.photos.length, 0)
    + (pendingFront.value ? 1 : 0),
);

/**
 * Photos a logout would drop: of cards not uploaded (on their way, or failed with Retry), in the tray, and a front
 * waiting for its back, in this tab or in the user's tab that keeps the queue. Cards the server refused for good don't
 * count.
 */
export const recipeIngestPhotosNotUploaded = computed(() =>
  photosHere.value + (keeper.value === "elsewhere" ? photosElsewhere.value : 0));

function dispatch(action: UploadQueueAction) {
  const before = state.value;
  state.value = reduceUploadQueue(before, action);
  noteFailure(before, action);
  forgetStaleNotes();
  releaseUnusedPreviews();
  schedulePersist();
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

/** A card that just failed for good while no cards page is open: the layout says so (toast, sidebar badge) */
function noteFailure(before: UploadQueueState, action: UploadQueueAction) {
  if (!("key" in action) || cardsPageViews > 0) {
    return;
  }
  const was = before.cards.find(card => card.key === action.key)?.status;
  if (findCard(action.key)?.status === "failed" && was !== "failed") {
    failedAway.value += 1;
  }
}

// ---- thumbnails

export type PreviewState = "pending" | "ready" | "unavailable";

interface Preview {
  state: PreviewState;
  url: string | null;
}

const previews = new Map<Blob, Preview>();
/** Bumped when a preview changes, so the templates showing it render again */
const previewVersion = ref(0);
const previewQueue: Blob[] = [];
let previewsRunning = 0;

function objectUrl(blob: Blob): string | null {
  try {
    return URL.createObjectURL(blob);
  }
  catch {
    // no object URLs here (server rendering, tests)
    return null;
  }
}

function revokeUrl(url: string | null) {
  if (url && typeof URL.revokeObjectURL === "function") {
    URL.revokeObjectURL(url);
  }
}

function setPreview(photo: Blob, preview: Preview) {
  previews.set(photo, preview);
  previewVersion.value += 1;
}

/** Makes the queued thumbnails, `PREVIEW_CONCURRENCY` at a time */
function pumpPreviews() {
  while (previewsRunning < PREVIEW_CONCURRENCY && previewQueue.length) {
    const photo = previewQueue.shift() as Blob;
    if (previews.get(photo)?.state !== "pending") {
      continue; // the photo left the queue meanwhile
    }
    previewsRunning += 1;
    makePreview(photo)
      .then((small) => {
        if (previews.get(photo)?.state === "pending") {
          const url = objectUrl(small);
          setPreview(photo, url ? { state: "ready", url } : { state: "unavailable", url: null });
        }
      })
      .catch(() => {
        // a photo this browser can't decode (HEIC outside Safari, a PDF): a placeholder with its name
        if (previews.get(photo)?.state === "pending") {
          setPreview(photo, { state: "unavailable", url: null });
        }
      })
      .finally(() => {
        previewsRunning -= 1;
        pumpPreviews();
      });
  }
}

/** The photo's preview, asked for the first time: made in the background */
function requestPreview(photo: Blob): Preview {
  const known = previews.get(photo);
  if (known) {
    return known;
  }
  let preview: Preview;
  if (isPdf(photo)) {
    // no browser decodes a PDF as an image: the placeholder with its name, without reading it
    preview = { state: "unavailable", url: null };
  }
  else if (typeof createImageBitmap !== "function") {
    // no way to make a small one: the photo itself, which the browser shows if it can (`markPreviewBroken` if not)
    const url = objectUrl(photo);
    preview = url ? { state: "ready", url } : { state: "unavailable", url: null };
  }
  else {
    preview = { state: "pending", url: null };
    previewQueue.push(photo);
    // not while the template that asked renders
    queueMicrotask(pumpPreviews);
  }
  previews.set(photo, preview);
  return preview;
}

/** An object URL of the photo's thumbnail; null while it's made, or when the browser can't show the photo */
function previewUrl(photo: Blob): string | null {
  void previewVersion.value;
  return requestPreview(photo).url;
}

function previewState(photo: Blob): PreviewState {
  void previewVersion.value;
  return requestPreview(photo).state;
}

/** The browser couldn't show the thumbnail after all (`<img @error>`): a placeholder instead */
function markPreviewBroken(photo: Blob) {
  const preview = previews.get(photo);
  if (preview && preview.state !== "unavailable") {
    revokeUrl(preview.url);
    setPreview(photo, { state: "unavailable", url: null });
  }
}

/** Forgets the thumbnails of photos that left the queue, and revokes their URLs */
function releaseUnusedPreviews() {
  if (!previews.size) {
    return;
  }
  const used = new Set<Blob>([
    ...state.value.cards.flatMap(card => card.photos),
    ...drafts.value.flatMap(card => card.photos),
    ...(pendingFront.value ? [pendingFront.value] : []),
  ]);
  let released = false;
  for (const [photo, preview] of previews) {
    if (!used.has(photo)) {
      revokeUrl(preview.url);
      previews.delete(photo);
      released = true;
    }
  }
  if (released) {
    const waiting = previewQueue.filter(photo => previews.has(photo));
    previewQueue.splice(0, previewQueue.length, ...waiting);
  }
}

// ---- what a file holds (`use-recipe-ingest-files.ts`)

const inspections = new WeakMap<Blob, Promise<ScanFile | null>>();
const inspected = new WeakMap<Blob, ScanFile | null>();
/** Bumped when a file has been read, so what depends on its pages (Join) renders again */
const inspectedVersion = ref(0);

/** Reads the file once: its format and the card pages it holds; null for a file the server doesn't read */
function inspect(photo: Blob): Promise<ScanFile | null> {
  let pending = inspections.get(photo);
  if (!pending) {
    pending = inspectScanFile(photo)
      .catch(() => null)
      .then((found) => {
        inspected.set(photo, found);
        inspectedVersion.value += 1;
        return found;
      });
    inspections.set(photo, pending);
  }
  return pending;
}

/**
 * The card pages a file holds: 1 for a photo; a document's (a PDF, a multi-page TIFF) once it has been read, null
 * until then or when its pages can't be counted. Photos from the camera aren't read: a page each.
 */
function pagesOf(photo: Blob): number | null {
  void inspectedVersion.value;
  if (inspected.has(photo)) {
    const found = inspected.get(photo);
    return found ? found.pages : 1;
  }
  if (!mayBeDocument(photo)) {
    return 1;
  }
  void inspect(photo); // one read back from this device's storage
  return null;
}

/** A PDF, by what it was read as, else by its type and name */
function isPdf(photo: Blob): boolean {
  const found = inspected.get(photo);
  if (found) {
    return found.kind === "pdf";
  }
  return photo.type === "application/pdf" || (photo.type === "" && /\.pdf$/i.test(photoName(photo)));
}

/** A JPEG, by what it was read as, else by its type and name: its pixel limit is higher (`images.py`) */
function isJpeg(photo: Blob): boolean {
  const found = inspected.get(photo);
  if (found) {
    return found.kind === "jpeg";
  }
  return photo.type === "image/jpeg" || (photo.type === "" && /\.jpe?g$/i.test(photoName(photo)));
}

/** A photo the browser may make smaller: one page, not a PDF (a multi-page TIFF would lose its other pages) */
function canReencode(photo: Blob): boolean {
  return !isPdf(photo) && pagesOf(photo) === 1;
}

// ---- keeping the queue between visits (per user)

/** The signed-in user whose queue this is; null until `connect` */
let owner: string | null = null;
/** Where the user's queue is kept, whichever tab keeps it; null when it lives in memory only */
let storageHandle: UploadStorage | null = null;
/** Where this tab writes the queue: the storage while this tab keeps it; null otherwise, or in memory only */
let storage: UploadStorage | null = null;
/**
 * A storage whose write failed (full, or broken): the queue isn't written to it any more, but deletes still go to it,
 * so a card that uploads later isn't sent again from it on the next visit (deletes free space, too)
 */
let deletesOnly: UploadStorage | null = null;
/** The stored queue being read back: writes wait for it */
let restoring: Promise<void> | null = null;
/** `connect`: settled once this tab knows who keeps the queue, and read it back if it's this one */
let connecting: Promise<void> | null = null;
/** This tab, as it names itself to the user's other tabs and in the stored queue */
const tabId = `tab-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
/** Which of the user's tabs keeps the queue */
let queueLock: QueueLock | null = null;
/** How the user's tabs tell each other about the queue: its photos, "Use this tab", a logout */
let channel: BroadcastChannel | null = null;
let persistQueued = false;
let persistChain: Promise<void> = Promise.resolve();
/** What the storage holds: each record as JSON, and the photo ids */
const writtenRecords = new Map<string, string>();
const writtenPhotos = new Set<string>();
const photoIds = new WeakMap<Blob, string>();

/** A card as it is stored: its photos by id; nothing about an attempt in flight */
type StoredCard = Omit<UploadCard, "photos" | "progress" | "retryAt"> & { photoIds: string[] };

interface StoredDraft {
  key: string;
  photoIds: string[];
  locked: boolean;
  index: number;
}

function photoId(photo: Blob): string {
  let id = photoIds.get(photo);
  if (!id) {
    id = newKey("photo");
    photoIds.set(photo, id);
  }
  return id;
}

/**
 * The records the queue is kept as, and the photos they name: every batch, every card not uploaded yet, the tray,
 * the front waiting for its back and the open batch
 */
function snapshot(): { records: Map<string, string>; photos: Map<string, Blob> } {
  const records = new Map<string, string>();
  const photos = new Map<string, Blob>();
  const ids = (list: readonly Blob[]) => list.map((photo) => {
    const id = photoId(photo);
    photos.set(id, photo);
    return id;
  });
  const current = state.value;
  for (const batch of current.batches) {
    records.set(`batch:${batch.key}`, JSON.stringify(batch));
  }
  for (const card of current.cards) {
    if (card.status === "done") {
      continue; // uploaded: nothing to send again
    }
    const { photos: cardPhotos, progress: _progress, retryAt: _retryAt, ...rest } = card;
    const stored: StoredCard = { ...rest, photoIds: ids(cardPhotos) };
    records.set(`card:${card.key}`, JSON.stringify(stored));
  }
  drafts.value.forEach((draft, index) => {
    const stored: StoredDraft = { key: draft.key, photoIds: ids(draft.photos), locked: !!draft.locked, index };
    records.set(`draft:${draft.key}`, JSON.stringify(stored));
  });
  if (pendingFront.value) {
    records.set("front", JSON.stringify({ photoId: ids([pendingFront.value])[0] }));
  }
  if (current.openBatchKey) {
    records.set("open", JSON.stringify({ batchKey: current.openBatchKey }));
  }
  return { records, photos };
}

/**
 * The storage failed (full, or broken): the queue lives in memory from now on, and the capture page says so once. With
 * the storage whose write failed, what it holds is still known, so deletes keep going to it: a card stored before
 * the failure that uploads later isn't sent again on the next visit. Once a delete fails too, nothing more goes to it.
 */
function storageFailed(error: unknown, failed: UploadStorage | null = null) {
  console.error(error);
  storage = null;
  deletesOnly = failed;
  if (!failed) {
    writtenRecords.clear();
    writtenPhotos.clear();
  }
  storageFailedNotice.value = true;
}

/** Where the next write goes: the storage, or the failed one that still takes deletes */
function writeTarget(): UploadStorage | null {
  return storage ?? deletesOnly;
}

/** The photos the stored records name, other than those of the records about to go */
function photosNamed(leaving: readonly string[]): Set<string> {
  const gone = new Set(leaving);
  const named = new Set<string>();
  writtenRecords.forEach((json, key) => {
    if (gone.has(key)) {
      return;
    }
    const record = JSON.parse(json) as { photoIds?: string[]; photoId?: string };
    record.photoIds?.forEach(id => named.add(id));
    if (record.photoId) {
      named.add(record.photoId);
    }
  });
  return named;
}

/**
 * Writes what changed since the last write: new and changed records, new photos, and what's gone. To a failed storage,
 * only what's gone: a photo goes once no record left names it, as a card's record there may still name its old photos.
 */
async function persistOnce() {
  persistQueued = false;
  const target = writeTarget();
  const onlyDeletes = target !== null && target === deletesOnly;
  if (restoring) {
    await restoring;
  }
  if (!target || target !== writeTarget()) {
    return;
  }
  const { records, photos } = snapshot();
  const putRecords = new Map<string, unknown>();
  if (!onlyDeletes) {
    records.forEach((json, key) => {
      if (writtenRecords.get(key) !== json) {
        putRecords.set(key, JSON.parse(json));
      }
    });
  }
  const deleteRecords = [...writtenRecords.keys()].filter(key => !records.has(key));
  const putPhotos = onlyDeletes ? new Map<string, Blob>() : new Map([...photos].filter(([id]) => !writtenPhotos.has(id)));
  const kept = onlyDeletes ? photosNamed(deleteRecords) : new Set<string>();
  const deletePhotos = [...writtenPhotos].filter(id => !photos.has(id) && !kept.has(id));
  if (!putRecords.size && !deleteRecords.length && !putPhotos.size && !deletePhotos.length) {
    return;
  }
  try {
    // named by this tab: once another tab has taken the queue over, nothing is written
    await target.save({ putRecords, deleteRecords, putPhotos, deletePhotos }, tabId);
  }
  catch (error) {
    if (target === writeTarget()) {
      if (error instanceof QueueTakenError) {
        queueTaken(error);
      }
      else {
        storageFailed(error, onlyDeletes ? null : target);
      }
    }
    return;
  }
  if (target !== writeTarget()) {
    return;
  }
  putRecords.forEach((_record, key) => writtenRecords.set(key, records.get(key) as string));
  deleteRecords.forEach(key => writtenRecords.delete(key));
  putPhotos.forEach((_photo, id) => writtenPhotos.add(id));
  deletePhotos.forEach(id => writtenPhotos.delete(id));
}

/** Writes the queue soon: changes made together (and progress, which isn't stored) cost one write */
function schedulePersist() {
  if (!writeTarget() || persistQueued) {
    return;
  }
  persistQueued = true;
  persistChain = persistChain.then(persistOnce).catch(error => console.error(error));
}

/** A stored card as the queue takes it back: whatever was in flight goes again (the server spots a duplicate) */
function restoredCard(stored: Omit<StoredCard, "photoIds">, photos: Blob[]): UploadCard {
  const failed = stored.status === "failed";
  return {
    ...stored,
    photos,
    status: failed ? "failed" : "waiting",
    progress: 0,
    retryAt: null,
    localOnly: failed ? stored.localOnly : null,
    downscaled: !!stored.downscaled,
    allowDuplicate: !!stored.allowDuplicate,
  };
}

/** Reads the stored queue back, before anything added meanwhile, and resumes it */
async function restoreFrom(target: UploadStorage, gen: number) {
  let loaded: Awaited<ReturnType<UploadStorage["load"]>>;
  try {
    // this tab's from now on: a tab that kept it before writes nothing more
    await target.claim(tabId);
    loaded = await target.load();
  }
  catch (error) {
    if (target === storage && gen === generation) {
      if (error instanceof QueueTakenError) {
        queueTaken(error);
      }
      else {
        storageFailed(error);
      }
    }
    return;
  }
  if (gen !== generation || target !== storage) {
    return;
  }

  const photo = (id: string | undefined) => {
    const found = id ? loaded.photos.get(id) : undefined;
    if (!found || !id) {
      return null;
    }
    photoIds.set(found, id);
    return markRaw(found);
  };
  const allPhotos = (ids: string[] | undefined) => {
    const found = (ids ?? []).map(photo);
    return found.length && found.every(Boolean) ? (found as Blob[]) : null;
  };

  const batches: UploadBatch[] = [];
  const cards: UploadCard[] = [];
  const trays: StoredDraft[] = [];
  let front: Blob | null = null;
  let openBatchKey: string | null = null;
  for (const [key, value] of loaded.records) {
    writtenRecords.set(key, JSON.stringify(value));
    if (key.startsWith("batch:")) {
      batches.push(value as UploadBatch);
    }
    else if (key.startsWith("card:")) {
      const { photoIds: ids, ...rest } = value as StoredCard;
      const cardPhotos = allPhotos(ids);
      // a card whose photos are gone can't be sent; its record goes with the next write
      if (cardPhotos && rest.status !== "done") {
        cards.push(restoredCard(rest, cardPhotos));
      }
    }
    else if (key.startsWith("draft:")) {
      trays.push(value as StoredDraft);
    }
    else if (key === "front") {
      front = photo((value as { photoId?: string }).photoId);
    }
    else if (key === "open") {
      openBatchKey = (value as { batchKey?: string }).batchKey ?? null;
    }
  }
  loaded.photos.forEach((_photo, id) => writtenPhotos.add(id));

  const current = state.value;
  const knownBatches = new Set(current.batches.map(batch => batch.key));
  const knownCards = new Set(current.cards.map(card => card.key));
  const restoredBatches = batches.filter(batch => !knownBatches.has(batch.key));
  const order = new Map(restoredBatches.map((batch, index) => [batch.key, index]));
  const restoredCards = cards
    .filter(card => !knownCards.has(card.key) && order.has(card.batchKey))
    .sort((a, b) => (order.get(a.batchKey) ?? 0) - (order.get(b.batchKey) ?? 0) || a.position - b.position);
  const reopened = restoredBatches.find(batch => batch.key === openBatchKey && !batch.sealing);
  state.value = {
    batches: [...restoredBatches, ...current.batches],
    cards: [...restoredCards, ...current.cards],
    openBatchKey: current.openBatchKey ?? reopened?.key ?? null,
  };

  const knownDrafts = new Set(drafts.value.map(draft => draft.key));
  const restoredDrafts = trays
    .filter(draft => !knownDrafts.has(draft.key))
    .sort((a, b) => a.index - b.index)
    .flatMap((draft) => {
      const draftPhotos = allPhotos(draft.photoIds);
      return draftPhotos ? [{ key: draft.key, photos: draftPhotos, locked: draft.locked || undefined }] : [];
    });
  if (restoredDrafts.length) {
    drafts.value = [...restoredDrafts, ...drafts.value];
  }
  if (front && !pendingFront.value) {
    pendingFront.value = front;
  }
  schedulePersist();
  pump();
  // a capture page shown before the queue came back keeps the restored open batch open from now
  void beat();
}

function localOnlyKey(userId: string): string {
  return `${LOCAL_ONLY_STORAGE_KEY}.${userId}`;
}

function readLocalOnly(userId: string): boolean {
  try {
    return localStorage.getItem(localOnlyKey(userId)) === "true";
  }
  catch {
    return false;
  }
}

/** Remembers "Keep these cards on this server" for the user, in this browser */
function writeLocalOnly(localOnly: boolean) {
  if (!owner) {
    return;
  }
  try {
    if (localOnly) {
      localStorage.setItem(localOnlyKey(owner), "true");
    }
    else {
      localStorage.removeItem(localOnlyKey(owner));
    }
  }
  catch {
    // private browsing: the switch lasts for this visit
  }
}

function forgetLocalOnly(userId: string) {
  try {
    localStorage.removeItem(localOnlyKey(userId));
  }
  catch {
    // nothing was stored
  }
}

/**
 * Whose queue this is: the default layout calls it with the signed-in user. The tab that keeps their queue (the first,
 * or the next once it closes or hands it over) reads it back and resumes it; their "Keep these cards on this server"
 * comes back. Another user's queue in memory is forgotten here; it stays stored for that user, and a logout deletes it.
 * Settles once the tab knows whether it keeps the queue (and has read it back if so).
 */
function connectUser(userId: string | null, open: (userId: string) => UploadStorage | null): Promise<void> {
  if (userId === owner) {
    return connecting ?? Promise.resolve();
  }
  if (owner !== null) {
    resetRecipeIngestUploads();
  }
  owner = userId;
  if (!userId) {
    return Promise.resolve();
  }
  localOnlyRef.value = readLocalOnly(userId);
  try {
    storageHandle = open(userId);
  }
  catch (error) {
    storageFailed(error);
    storageHandle = null;
  }
  if (!storageHandle) {
    // nothing is kept between visits, so nothing is shared with other tabs: this tab's queue is its own
    keeper.value = "here";
    return Promise.resolve();
  }

  const name = uploadStorageName(userId);
  channel = openQueueChannel(name);
  if (channel) {
    channel.onmessage = onQueueMessage;
  }
  let settle!: () => void;
  const settled = new Promise<void>((resolve) => {
    settle = resolve;
  });
  connecting = settled;
  const current = () => owner === userId && connecting === settled;
  queueLock = openQueueLock(name, {
    granted: () => {
      if (current()) {
        void startKeeping().finally(settle);
      }
    },
    waiting: () => {
      if (current()) {
        keptElsewhere();
        settle();
      }
    },
    lost: () => {
      if (current()) {
        stopKeeping();
      }
    },
  }, tabId);
  return settled;
}

// ---- which of the user's tabs keeps the queue

type QueueMessage
  = | { type: "photos"; count: number }
    | { type: "photos?" }
    | { type: "want" }
    | { type: "hand-over" }
    | { type: "logout"; id: string }
    | { type: "logout-done"; id: string };

/** Answers to a logout's "logout", by its id */
const logoutAnswers = new Map<string, () => void>();
/** While a cards page is open in a tab that doesn't keep the queue, it asks for it now and then */
let wantTimer: ReturnType<typeof setInterval> | null = null;
let handingOver = false;

function post(message: QueueMessage) {
  try {
    channel?.postMessage(message);
  }
  catch {
    // closed
  }
}

/** The tab keeping the queue tells the user's other tabs how many of its photos aren't uploaded (their logout asks) */
function announcePhotos() {
  if (keeper.value === "here") {
    post({ type: "photos", count: photosHere.value });
  }
}

/** Nothing on its way: no card to send, nothing in the tray, no front waiting, no batch open or waiting to be sealed */
function isIdle(): boolean {
  const current = state.value;
  return !hasPending.value
    && !current.openBatchKey
    && !current.batches.some(batch => batch.sealing && !isBatchFinished(current, batch));
}

function onQueueMessage(event: MessageEvent<QueueMessage>) {
  const message = event.data;
  switch (message?.type) {
    case "photos":
      if (keeper.value === "elsewhere") {
        photosElsewhere.value = message.count;
      }
      break;
    case "photos?":
      announcePhotos();
      break;
    case "want":
      // a cards page is open in another tab: an idle queue goes there, unless this tab shows a cards page too
      if (keeper.value === "here" && cardsPageViews === 0 && isIdle()) {
        void handOver();
      }
      break;
    case "hand-over":
      if (keeper.value === "here") {
        void handOver();
      }
      break;
    case "logout":
      void stopForLogout(message.id);
      break;
    case "logout-done":
      logoutAnswers.get(message.id)?.();
      break;
  }
}

/** This tab keeps the queue now: it claims the stored queue, reads it back and resumes it */
function startKeeping(): Promise<void> {
  keeper.value = "here";
  photosElsewhere.value = 0;
  updateWanting();
  const target = storageHandle;
  if (!target) {
    return Promise.resolve();
  }
  storage = target;
  deletesOnly = null;
  const reading = restoreFrom(target, generation).catch(error => storageFailed(error)).finally(() => {
    if (restoring === reading) {
      restoring = null;
    }
    announcePhotos();
  });
  restoring = reading;
  return reading;
}

/** Another tab keeps the queue: this one only says so (and counts that tab's photos for a logout) */
function keptElsewhere() {
  keeper.value = "elsewhere";
  post({ type: "photos?" });
  updateWanting();
}

/**
 * This tab doesn't keep the queue any more (another tab took it, or asked for it): nothing more is sent or written,
 * and the queue in memory goes; the tab keeping it now reads it back from the storage
 */
function stopKeeping() {
  forgetQueueInMemory();
  storage = null;
  deletesOnly = null;
  restoring = null;
  persistQueued = false;
  persistChain = Promise.resolve();
  writtenRecords.clear();
  writtenPhotos.clear();
  keptElsewhere();
}

/**
 * Whether this tab still keeps the stored queue, as the storage says: false once another tab has claimed it, or a
 * logout in another tab deleted it. A storage that fails otherwise doesn't stop the uploads.
 */
async function stillKeeping(): Promise<boolean> {
  const target = storage;
  if (!target) {
    return keeper.value !== "elsewhere";
  }
  let taken: QueueTakenError | null;
  try {
    const by = await target.claimedBy();
    taken = by === null || by === tabId ? null : new QueueTakenError();
  }
  catch (error) {
    taken = error instanceof QueueTakenError ? error : null;
  }
  if (taken && target === storage) {
    queueTaken(taken);
  }
  return !taken;
}

/** The stored queue isn't this tab's any more: another tab claimed it, or a logout in another tab deleted it */
function queueTaken(error: QueueTakenError) {
  stopKeeping();
  if (error.closed) {
    // nothing more is stored from this tab (it signs out too in a moment); other tabs aren't held up by it
    storageHandle = null;
    queueLock?.release();
    queueLock = null;
  }
  else {
    queueLock?.yield();
  }
}

/** Another tab asked for the queue: what changed last is written first, then this tab lets it go and waits again */
async function handOver() {
  if (handingOver) {
    return;
  }
  handingOver = true;
  try {
    schedulePersist();
    await persistChain;
  }
  finally {
    handingOver = false;
  }
  if (keeper.value !== "here") {
    return;
  }
  stopKeeping();
  queueLock?.yield();
}

/** A cards page open in a tab that doesn't keep the queue asks for it now, and every `QUEUE_WANT_MS` while it's open */
function updateWanting() {
  const wanting = keeper.value === "elsewhere" && cardsPageViews > 0 && !!channel;
  if (wanting && wantTimer === null) {
    post({ type: "want" });
    wantTimer = setInterval(() => post({ type: "want" }), QUEUE_WANT_MS);
  }
  else if (!wanting && wantTimer !== null) {
    clearInterval(wantTimer);
    wantTimer = null;
  }
}

/**
 * "Use this tab": the tab keeping the queue hands it over (it writes what changed last first). One that doesn't
 * answer within `QUEUE_HAND_OVER_MS` (frozen in the background) has it taken; it writes nothing more once it wakes.
 * Settles once this tab keeps the queue, or has asked for it.
 */
function takeOverQueue(): Promise<void> {
  const lock = queueLock;
  if (keeper.value !== "elsewhere" || !lock) {
    return Promise.resolve();
  }
  post({ type: "hand-over" });
  return new Promise<void>((resolve) => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    const stop = watch(keeper, (now) => {
      if (now !== "elsewhere") {
        finish();
      }
    });
    function finish() {
      stop();
      if (timer !== null) {
        clearTimeout(timer);
      }
      resolve();
    }
    timer = setTimeout(() => {
      timer = null;
      if (keeper.value === "elsewhere" && queueLock === lock) {
        lock.steal();
      }
      finish();
    }, QUEUE_HAND_OVER_MS);
  });
}

/** Server batches the queue started that aren't sealed yet */
function unsealedServerBatches(): string[] {
  return [...new Set(state.value.batches.flatMap(batch =>
    batch.serverIds.filter(serverId => !batch.sealedIds.includes(serverId))))];
}

/** Stops sending and writing at once: nothing in flight changes the queue any more, and nothing more is stored */
function stopSending() {
  generation += 1;
  uploadsInFlight.forEach(controller => controller.abort());
  uploadsInFlight.clear();
  if (retryTimer !== null) {
    clearTimeout(retryTimer);
    retryTimer = null;
  }
  storage = null;
  deletesOnly = null;
}

/** Waits for all of `work` to settle, at most `timeoutMs` */
async function settleWithin(work: Promise<unknown>[], timeoutMs: number): Promise<void> {
  if (!work.length) {
    return;
  }
  let timer: ReturnType<typeof setTimeout> | undefined;
  await Promise.race([
    Promise.allSettled(work),
    new Promise<void>((resolve) => {
      timer = setTimeout(resolve, timeoutMs);
    }),
  ]);
  clearTimeout(timer);
}

/** Sealing a batch at logout is best effort: it waits at most this long */
const LOGOUT_SEAL_MS = 2500;

/**
 * The user logs out in another tab: this tab stops sending and writing at once (the stored queue is deleted next).
 * The tab that keeps the queue seals its batches first, while the session is still there, and says when it's done.
 */
async function stopForLogout(id: string) {
  const keeping = keeper.value === "here";
  const sender = api;
  const unsealed = keeping ? unsealedServerBatches() : [];
  stopSending();
  if (keeping) {
    if (sender) {
      await settleWithin(unsealed.map(serverId => sender.sealBatch(serverId, { suppressAlert: true })), LOGOUT_SEAL_MS);
    }
    post({ type: "logout-done", id });
  }
  resetRecipeIngestUploads();
}

// ---- leaving the page with photos pending

function warnBeforeUnload(event: BeforeUnloadEvent) {
  // an expired session's redirect to the login page: nothing could be uploaded from here, and the queue is kept
  if (!recipeIngestSignedIn()) {
    return;
  }
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
    // the tray and the waiting front are kept too
    watch([drafts, pendingFront], () => schedulePersist());
    // the user's other tabs count this tab's photos when they log out
    watch(photosHere, () => announcePhotos());
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

/** Rejections a smaller copy of the photo gets past (over the server's per-photo size or pixel limit) */
const SHRINKABLE_REASONS = new Set<string>(["too_large", "too_many_pixels"]);

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
        // the card that waits for the batch shows why it failed
        const { data, error } = await client().createBatch({ suppressAlert: true });
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
  const body = status === 400 ? rejectedBody(error) : null;

  // Too large for the server: a proxy's body limit (413), or a photo over its size or pixel limit (400 with nothing
  // accepted). The card's photos go again, smaller, once. A PDF or a multi-page TIFF can't be made smaller here: when
  // it's the file refused, the server's reason is the card's.
  const tooLargeIndexes = new Set(
    (body?.jobs?.length ? [] : body?.rejected ?? [])
      .filter(rejected => SHRINKABLE_REASONS.has(rejected.reason))
      .map(rejected => rejected.index),
  );
  const tooLarge = status === 413 || tooLargeIndexes.size > 0;
  const shrinkable = card.photos.some((photo, index) =>
    canReencode(photo) && (status === 413 || tooLargeIndexes.has(index)));
  if (tooLarge) {
    if (card.reencoded || !shrinkable) {
      if (body) {
        dispatch({ type: "uploaded", key, response: body });
      }
      else {
        dispatch({ type: "failed", key, error: code ?? "too_large", retryable: false });
      }
      return;
    }
    dispatch({ type: "re-encoding", key });
    let photos: Blob[];
    try {
      photos = await Promise.all(card.photos.map(photo => (canReencode(photo) ? reencodePhoto(photo) : photo)));
    }
    catch {
      // this browser can't decode the photo (HEIC outside Safari), so it can't make it smaller
      if (gen === generation) {
        dispatch({ type: "failed", key, error: CANNOT_SHRINK, retryable: false });
      }
      return;
    }
    if (gen === generation) {
      dispatch({ type: "re-encoded", key, photos: photos.map(photo => markRaw(photo)) });
    }
    return;
  }

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
  // Uploads don't toast (every attempt would): the card says why it failed
  dispatch({ type: "failed", key, error: code, retryable: true });
}

function currentDataSaver(): boolean {
  if (dataSaverRef.value === null) {
    try {
      dataSaverRef.value = localStorage.getItem(DATA_SAVER_STORAGE_KEY) === "true";
    }
    catch {
      dataSaverRef.value = false;
    }
  }
  return dataSaverRef.value;
}

function setDataSaver(on: boolean) {
  dataSaverRef.value = on;
  try {
    if (on) {
      localStorage.setItem(DATA_SAVER_STORAGE_KEY, "true");
    }
    else {
      localStorage.removeItem(DATA_SAVER_STORAGE_KEY);
    }
  }
  catch {
    // private browsing: the choice lasts for this visit
  }
}

async function runCard(key: string) {
  const gen = generation;
  dispatch({ type: "start", key });
  try {
    let card = findCard(key);
    if (!card) {
      return;
    }
    if (currentDataSaver() && !card.downscaled && !card.reencoded) {
      // data saver: the photos go at most the size the server keeps
      // a PDF or a multi-page TIFF goes as it is: re-encoding would keep one page
      const smaller = await Promise.all(card.photos.map(photo => (canReencode(photo) ? shrinkForUpload(photo) : photo)));
      if (gen !== generation) {
        return;
      }
      dispatch({ type: "downscaled", key, photos: smaller.map(photo => markRaw(photo)) });
      card = findCard(key);
      if (!card) {
        return;
      }
    }
    const batch = await ensureBatch(card.batchKey, gen);
    if (gen !== generation) {
      return;
    }
    if ("error" in batch) {
      await handleFailure(key, batch.error, null, gen);
      return;
    }

    // a tab that lost the queue without noticing (frozen in the background while another took it) sends nothing
    if (!(await stillKeeping()) || gen !== generation) {
      return;
    }

    // the switch as it is now: a card that hasn't gone yet takes its setting, whatever batch it's in
    const localOnly = localOnlyRef.value;
    dispatch({ type: "sending", key, localOnly });
    const controller = new AbortController();
    uploadsInFlight.add(controller);
    let answer: Awaited<ReturnType<RecipeIngestAPI["upload"]>>;
    try {
      answer = await client().upload(
        card.photos,
        {
          batchId: batch.id,
          position: card.position,
          localOnly,
          ...(card.allowDuplicate ? { allowDuplicate: true } : {}),
        },
        {
          onUploadProgress: (event: AxiosProgressEvent) => {
            if (event.total && gen === generation) {
              dispatch({ type: "progress", key, progress: event.loaded / event.total });
            }
          },
          signal: controller.signal,
          // attempts retry on their own, and the card shows why the last one failed
          suppressAlert: true,
        },
      );
    }
    finally {
      uploadsInFlight.delete(controller);
    }
    const { data, error } = answer;
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
    const { error } = await client().sealBatch(serverId, { suppressAlert: true });
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
  if (!api || keeper.value === "elsewhere") {
    return; // nothing can be sent before `useRecipeIngestUploads()`, nor by a tab that doesn't keep the queue
  }
  for (const { batchKey, serverId } of batchesToSeal(state.value)) {
    void sealBatch(batchKey, serverId);
  }
  for (const card of cardsToStart(state.value, Date.now())) {
    void runCard(card.key);
  }
  scheduleRetries();
}

// ---- the open batch's heartbeat

/** The server batch new cards go to, while it's open as far as this page knows */
function openServerBatch(): { batchKey: string; serverId: string } | null {
  const key = state.value.openBatchKey;
  const batch = key ? findBatch(key) : undefined;
  if (!batch || batch.sealing || !batch.serverId || batch.sealedIds.includes(batch.serverId)) {
    return null;
  }
  return { batchKey: batch.key, serverId: batch.serverId };
}

function pageVisible(): boolean {
  return typeof document === "undefined" || document.visibilityState !== "hidden";
}

function clearHeartbeatTimer() {
  if (heartbeatTimer !== null) {
    clearTimeout(heartbeatTimer);
    heartbeatTimer = null;
  }
}

function scheduleHeartbeat() {
  clearHeartbeatTimer();
  if (heartbeatHolders > 0 && pageVisible()) {
    heartbeatTimer = setTimeout(() => {
      heartbeatTimer = null;
      void beat();
    }, BATCH_HEARTBEAT_MS);
  }
}

/**
 * Touches the open batch now, then again in 3 minutes. Quiet: a batch the server ended anyway (sealed after a longer
 * absence, or gone) is let go, and the next card starts a new one without a word; other failures (offline, a
 * restore) wait for the next beat.
 */
async function beat() {
  clearHeartbeatTimer();
  if (heartbeatHolders === 0 || !pageVisible() || heartbeatInFlight) {
    return;
  }
  const open = openServerBatch();
  if (open && api) {
    const gen = generation;
    heartbeatInFlight = true;
    try {
      const { error } = await client().touchBatch(open.serverId, { suppressAlert: true });
      if (gen !== generation) {
        return;
      }
      const status = error ? errorStatusOf(error) : null;
      if (status === 409 || status === 404) {
        dispatch({ type: "batch-ended", batchKey: open.batchKey, serverId: open.serverId });
      }
    }
    finally {
      if (gen === generation) {
        heartbeatInFlight = false;
      }
    }
  }
  scheduleHeartbeat();
}

function onHeartbeatVisibility() {
  if (pageVisible()) {
    // back on the page: the batch may have been idle for minutes
    void beat();
  }
  else {
    clearHeartbeatTimer();
  }
}

function stopHeartbeat() {
  clearHeartbeatTimer();
  if (typeof document !== "undefined") {
    document.removeEventListener("visibilitychange", onHeartbeatVisibility);
  }
}

/**
 * The capture page is shown: its open batch is touched at once and every 3 minutes while the page is
 * visible, so the batch stays open through a pause. Returns what to call when the page closes.
 */
function keepBatchOpen(): () => void {
  const gen = generation;
  heartbeatHolders += 1;
  if (heartbeatHolders === 1 && typeof document !== "undefined") {
    document.addEventListener("visibilitychange", onHeartbeatVisibility);
  }
  void beat();
  let released = false;
  return () => {
    // a reset (logout) has let go already
    if (released || gen !== generation) {
      return;
    }
    released = true;
    heartbeatHolders = Math.max(0, heartbeatHolders - 1);
    if (heartbeatHolders === 0) {
      stopHeartbeat();
    }
  };
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
  finishedBySwitch.value = false;
  dispatch({
    type: "add-card",
    key: newKey("card"),
    batchKey: state.value.openBatchKey ?? newKey("batch"),
    photos: photos.map(photo => markRaw(photo)),
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
  drafts.value = draftCards(drafts.value.flatMap(card => card.photos), mode, undefined, pagesOf);
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
    finishedBySwitch.value = false;
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

/** What `addPhotos` left out, by file name */
export interface AddPhotosResult {
  /** Files the server doesn't read: not a JPEG, PNG, WebP, HEIC, AVIF or TIFF photo, nor a PDF */
  unsupported: string[];
  /** Documents with more pages than a card can have */
  tooManyPages: string[];
}

/** Chosen or dropped files wait for the ones before them, so the tray keeps the order they came in */
let addChain: Promise<unknown> = Promise.resolve();

/** Photos that can go on cards become draft cards, paired in selection order */
function addDrafts(photos: readonly Blob[]) {
  if (!photos.length) {
    return;
  }
  finishedBySwitch.value = false;
  const all = [...drafts.value.flatMap(card => card.photos), ...photos.map(photo => markRaw(photo))];
  // Locked cards keep their shape; the new photos pair up after the last of them
  let keptCount = drafts.value.length;
  while (keptCount > 0 && !drafts.value[keptCount - 1]?.locked) {
    keptCount -= 1;
  }
  const kept = drafts.value.slice(0, keptCount);
  const loose = all.slice(kept.flatMap(card => card.photos).length);
  drafts.value = [...kept, ...draftCards(loose, currentMode(), undefined, pagesOf)];
}

/**
 * Chosen or dropped files become draft cards, paired in selection order. Each file is read first, as the server will
 * (`use-recipe-ingest-files.ts`): a file it doesn't read is left out, and so is a document with more pages than
 * `maxPages`; the result names them. A PDF or a multi-page TIFF is a card of its own.
 */
function addPhotos(photos: readonly Blob[], maxPages = DEFAULT_MAX_PAGES_PER_CARD): Promise<AddPhotosResult> {
  const gen = generation;
  const added = addChain.then(async (): Promise<AddPhotosResult> => {
    const found = await Promise.all(photos.map(photo => inspect(photo)));
    const result: AddPhotosResult = { unsupported: [], tooManyPages: [] };
    if (gen !== generation) {
      return result; // signed out meanwhile
    }
    const usable = photos.filter((photo, index) => {
      const file = found[index];
      if (!file) {
        result.unsupported.push(photoName(photo));
        return false;
      }
      if (file.pages !== null && file.pages > maxPages) {
        result.tooManyPages.push(photoName(photo));
        return false;
      }
      return true;
    });
    addDrafts(usable);
    return result;
  });
  addChain = added.catch(() => undefined);
  return added;
}

function editDrafts(edit: (cards: DraftCard[], mode: CaptureMode, pages: PagesOf<Blob>) => DraftCard[]) {
  drafts.value = edit(drafts.value, currentMode(), pagesOf);
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
 * uploaded or failed for good, whether or not the server took any of them (one already scanned, or refused). Photos
 * taken after this start a new batch.
 */
function done() {
  noBack();
  uploadDrafts();
  const batchKey = state.value.openBatchKey;
  if (batchKey) {
    dispatch({ type: "seal-requested", batchKey });
  }
  sentBeforeChange.value = new Set();
  finishedBySwitch.value = false;
  pump();
}

/** The card went to the server with `localOnly`, which keeps it with the card: it's on its way, or queued as a job */
function wentWith(card: UploadCard, localOnly: boolean): boolean {
  return card.localOnly === localOnly
    && (card.status === "uploading" || (card.status === "done" && card.jobs.length > 0));
}

/**
 * "Keep these cards on this server", remembered for the user in this browser. Every card that hasn't gone yet takes
 * the new setting when it goes, whatever batch it's in: waiting, about to retry, failed (for Retry), or one whose
 * attempt fails and goes again. The server stores the setting with each card when it arrives, so the cards already on
 * their way or uploaded keep theirs (they are counted for the note), and the open batch with such cards is finished
 * (as with Done), so the next photo starts a new batch.
 */
function setLocalOnly(localOnly: boolean) {
  if (localOnly === localOnlyRef.value) {
    return;
  }
  localOnlyRef.value = localOnly;
  writeLocalOnly(localOnly);
  const current = state.value;
  // the cards of batches still in progress; a finished batch's cards stay listed only for their notes
  const inProgress = new Set(current.batches.filter(batch => !isBatchFinished(current, batch)).map(batch => batch.key));
  const gone = current.cards.filter(card => inProgress.has(card.batchKey) && wentWith(card, !localOnly));
  sentBeforeChange.value = new Set(gone.map(card => card.key));
  const batchKey = current.openBatchKey;
  if (batchKey && gone.some(card => card.batchKey === batchKey)) {
    finishedBySwitch.value = true;
    dispatch({ type: "seal-requested", batchKey });
    pump();
  }
}

/**
 * The note counts cards of batches still in progress that went with the other setting: one whose batch is sealed
 * (and so finished) no longer counts, nor one whose attempt failed, which goes again with the switch's setting (or not
 * at all)
 */
function forgetStaleNotes() {
  if (!sentBeforeChange.value.size) {
    return;
  }
  const current = state.value;
  const kept = [...sentBeforeChange.value].filter((key) => {
    const card = findCard(key);
    const batch = card ? findBatch(card.batchKey) : undefined;
    return !!card && !!batch && !isBatchFinished(current, batch) && wentWith(card, !localOnlyRef.value);
  });
  if (kept.length < sentBeforeChange.value.size) {
    sentBeforeChange.value = new Set(kept);
  }
}

function retry(key: string) {
  dispatch({ type: "retry", key });
  pump();
}

/** Sends a card marked Already scanned again, as a new card (`allowDuplicate`) */
function scanAgain(key: string) {
  dispatch({ type: "scan-again", key });
  pump();
}

function remove(key: string) {
  dispatch({ type: "remove", key });
  pump();
}

/**
 * The cards page is open, and shows failed cards itself: the count of cards that failed meanwhile starts again.
 * Returns what to call when the page closes.
 */
function openCardsPage(): () => void {
  cardsPageViews += 1;
  failedAway.value = 0;
  // the queue is kept in another tab: it comes here if it's idle there
  updateWanting();
  let closed = false;
  return () => {
    if (!closed) {
      closed = true;
      cardsPageViews = Math.max(0, cardsPageViews - 1);
      updateWanting();
    }
  };
}

/** The upload queue, shared by every component (docs/ai/PHASE2.md §1.1) */
export function useRecipeIngestUploads() {
  const clients = {
    api: api ?? useUserApi().recipeIngest,
    refresh: refreshCounts ?? useRecipeIngestCounts().refresh,
  };
  const firstUse = !api;
  api = clients.api;
  refreshCounts = clients.refresh;
  currentMode();
  currentDataSaver();
  ensureScope();
  if (firstUse) {
    pump(); // a queue read back before the first use
  }

  const openBatch = computed(() => state.value.batches.find(b => b.key === state.value.openBatchKey) ?? null);

  return {
    cards: computed(() => state.value.cards),
    batches: computed(() => state.value.batches),
    openBatch,
    /** Cards of the open batch the server took or may still take: not one already scanned, nor one refused for good */
    openBatchCardCount: computed(() => state.value.cards
      .filter(card => card.batchKey === state.value.openBatchKey && !card.duplicateOf && !isRefused(card))
      .length),
    drafts: computed(() => drafts.value),
    pendingFront: computed(() => pendingFront.value),
    mode: computed<CaptureMode>({ get: () => modeRef.value ?? "one-side", set: setMode }),
    localOnly: computed<boolean>({ get: () => localOnlyRef.value, set: setLocalOnly }),
    /** Data saver: photos go at most 4096 px (JPEG), remembered in this browser */
    dataSaver: computed<boolean>({ get: () => !!dataSaverRef.value, set: setDataSaver }),
    /** Cards of batches still in progress that went with the other setting when `localOnly` last changed */
    sentBeforeLocalOnlyChange: computed(() => sentBeforeChange.value.size),
    /** The last change of `localOnly` finished the open batch: the next photo starts a new one */
    localOnlyFinishedBatch: computed(() => finishedBySwitch.value),
    /** Photos not uploaded yet, or not yet sent */
    hasPending,
    /** Photos a logout would drop */
    photosNotUploaded: recipeIngestPhotosNotUploaded,
    /** Cards waiting, uploading or about to retry */
    isUploading: computed(() => state.value.cards.some(card => !isSettled(card))),
    /** Server batches with cards still on their way */
    uploadingBatchIds: computed(() => {
      const keys = new Set(state.value.cards.filter(card => !isSettled(card)).map(card => card.batchKey));
      return state.value.batches.filter(b => keys.has(b.key) && b.serverId).map(b => b.serverId as string);
    }),
    uploadedCount: readonly(uploadedCount),
    lastUploadBatchId: readonly(lastUploadBatchId),
    /** Cards that failed for good while no cards page was open (the sidebar badge), until one opens */
    failedWhileAway: readonly(failedAway),
    /** Another of the user's tabs keeps the queue: this one doesn't capture or send ("Use this tab" takes it over) */
    queueElsewhere: computed(() => keeper.value === "elsewhere"),
    /** Photos not uploaded yet in the tab that keeps the queue, when that's another tab */
    photosElsewhere: readonly(photosElsewhere),
    takeOverQueue,
    /** The queue couldn't be kept on this device, so it's lost if the page closes; until dismissed */
    storageFailed: computed<boolean>({
      get: () => storageFailedNotice.value,
      set: (on) => {
        storageFailedNotice.value = on;
      },
    }),
    previewUrl,
    previewState,
    markPreviewBroken,
    takePhoto,
    retake,
    noBack,
    addPhotos,
    /** The card pages a file holds (a PDF's or a multi-page TIFF's); null while unknown */
    pagesOf,
    isPdf,
    isJpeg,
    swapDraft: (index: number) => editDrafts(cards => swapSides(cards, index)),
    splitDraft: (index: number) => editDrafts((cards, mode, pages) => splitCard(cards, index, mode, undefined, pages)),
    /** Whether Join can add the next draft card's first photo to this one, within `maxPages` */
    canJoinDraft: (index: number, maxPages = DEFAULT_MAX_PAGES_PER_CARD) =>
      canJoin(drafts.value, index, maxPages, pagesOf),
    joinDraft: (index: number, maxPages = DEFAULT_MAX_PAGES_PER_CARD) =>
      editDrafts((cards, mode, pages) => joinCards(cards, index, mode, undefined, maxPages, pages)),
    removeDraft: (index: number) => editDrafts(cards => removeCard(cards, index)),
    uploadDrafts,
    done,
    retry,
    scanAgain,
    remove,
    openCardsPage,
    keepBatchOpen,
    /**
     * Whose queue this is (the default layout passes the signed-in user): their stored queue comes back and resumes.
     * `open` gives the storage (IndexedDB by default).
     */
    connect: (userId: string | null, open: (userId: string) => UploadStorage | null = openUploadStorage) => {
      const reading = connectUser(userId, open);
      // after another user's queue was forgotten: this browser's choices, and the requests, again
      api ??= clients.api;
      refreshCounts ??= clients.refresh;
      currentMode();
      currentDataSaver();
      ensureScope();
      return reading;
    },
  };
}

/**
 * A logout the user chose (the header asks first when photos haven't been uploaded): a review page's edit not saved
 * yet is saved, the uploads stop, every server batch the queue started that isn't sealed yet is sealed, so none stays
 * open and empty on the server, and what this device keeps for the user is deleted (the stored queue, the remembered
 * privacy switch). The user's other tabs stop too; the one keeping the queue seals its batches and stops writing before
 * the stored queue is deleted. All of it is best effort: it waits at most `timeoutMs`. The logout then forgets the
 * queue in memory (`resetRecipeIngestState`).
 */
export async function prepareRecipeIngestLogout(timeoutMs = 3000): Promise<void> {
  // while the session is still there
  const finishing = runRecipeIngestLogoutTasks();
  const sender = api;
  const id = newKey("logout");
  const answered = keeper.value === "elsewhere" && channel
    ? new Promise<void>(resolve => logoutAnswers.set(id, resolve))
    : null;
  post({ type: "logout", id });
  const unsealed = unsealedServerBatches();
  stopSending();
  await settleWithin([
    ...finishing,
    ...(sender ? unsealed.map(serverId => sender.sealBatch(serverId, { suppressAlert: true })) : []),
    ...(answered ? [answered] : []),
  ], timeoutMs);
  logoutAnswers.delete(id);
  await forgetStoredQueue();
}

/** Deletes what this device keeps for the signed-in user: the stored queue and the remembered privacy switch */
async function forgetStoredQueue(): Promise<void> {
  const user = owner;
  let stored = storageHandle;
  storage = null;
  deletesOnly = null;
  writtenRecords.clear();
  writtenPhotos.clear();
  if (!user) {
    return;
  }
  forgetLocalOnly(user);
  if (!stored) {
    try {
      stored = openUploadStorage(user);
    }
    catch {
      stored = null;
    }
  }
  try {
    await stored?.clear();
  }
  catch (error) {
    console.error(error);
  }
}

/** Forgets the queue in memory (photos, retries, seals, thumbnails); what's stored, and the user's choices, stay */
function forgetQueueInMemory() {
  generation += 1;
  uploadsInFlight.forEach(controller => controller.abort());
  uploadsInFlight.clear();
  if (retryTimer !== null) {
    clearTimeout(retryTimer);
    retryTimer = null;
  }
  heartbeatInFlight = false;
  state.value = emptyUploadQueue();
  drafts.value = [];
  pendingFront.value = null;
  sentBeforeChange.value = new Set();
  finishedBySwitch.value = false;
  failedAway.value = 0;
  batchRequests.clear();
  sealsInFlight.clear();
  sealFailures.clear();
  releaseUnusedPreviews();
  previewQueue.length = 0;
  addChain = Promise.resolve();
}

/** Forgets the queue in memory (between tests, on logout and when the user changes); the stored queue stays */
export function resetRecipeIngestUploads() {
  forgetQueueInMemory();
  heartbeatHolders = 0;
  stopHeartbeat();
  scope?.stop();
  scope = null;
  if (typeof window !== "undefined") {
    window.removeEventListener("beforeunload", warnBeforeUnload);
  }
  // nothing more is written for the user who was here, and the user's other tabs are left to themselves
  queueLock?.release();
  queueLock = null;
  if (channel) {
    channel.onmessage = null;
    channel.close();
    channel = null;
  }
  if (wantTimer !== null) {
    clearInterval(wantTimer);
    wantTimer = null;
  }
  logoutAnswers.clear();
  handingOver = false;
  keeper.value = null;
  photosElsewhere.value = 0;
  owner = null;
  storageHandle = null;
  storage = null;
  deletesOnly = null;
  restoring = null;
  connecting = null;
  persistQueued = false;
  persistChain = Promise.resolve();
  writtenRecords.clear();
  writtenPhotos.clear();
  storageFailedNotice.value = false;

  modeRef.value = null;
  localOnlyRef.value = false;
  dataSaverRef.value = null;
  uploadedCount.value = 0;
  lastUploadBatchId.value = null;
  api = null;
  refreshCounts = null;
}

/**
 * Forgets what recipe card ingestion holds in memory for the signed-in user: the upload queue, with its photos,
 * retries and open batch, the card counts and settings, and what review pages carry over. Called on every sign-out
 * (`clearComposableCaches`), so the next user of the device neither sees the photos nor sends them. What this device
 * stores for the user goes too when they chose to log out (`prepareRecipeIngestLogout`); after a forced sign-out (a
 * changed password, another account on the consent page) it stays for them, and their queue resumes when they sign
 * in again.
 */
export function resetRecipeIngestState() {
  resetRecipeIngestUploads();
  resetRecipeIngestCounts();
  resetRecipeIngestReviewState();
  resetCarriedReviewNotice();
  resetRecipeIngestSettings();
}
