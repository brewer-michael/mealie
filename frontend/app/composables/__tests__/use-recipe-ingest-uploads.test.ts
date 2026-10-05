import { flushPromises } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import {
  BATCH_HEARTBEAT_MS,
  CANNOT_SHRINK,
  CAPTURE_MODE_STORAGE_KEY,
  DATA_SAVER_MAX_SIDE,
  DATA_SAVER_STORAGE_KEY,
  LOCAL_ONLY_STORAGE_KEY,
  MAX_CONCURRENT_UPLOADS,
  PREVIEW_WIDTH,
  REENCODE_MAX_SIDE,
  batchesToSeal,
  canJoin,
  cardsToStart,
  draftCards,
  emptyUploadQueue,
  groupPhotosIntoCards,
  joinCards,
  photoName,
  prepareRecipeIngestLogout,
  recipeIngestPhotosNotUploaded,
  reduceUploadQueue,
  resetRecipeIngestUploads,
  retryDelay,
  splitCard,
  swapSides,
  useRecipeIngestUploads,
} from "../use-recipe-ingest-uploads";
import type { DraftCard, UploadQueueAction, UploadQueueState } from "../use-recipe-ingest-uploads";
import { classicPdf, file, objectStreamPdf, tiff } from "./use-recipe-ingest-files.fixtures";
import {
  onRecipeIngestLogout,
  resetRecipeIngestCounts,
  setRecipeIngestSessionCheck,
  useRecipeIngestCounts,
} from "../use-recipe-ingest";
import { clearComposableCaches } from "../use-clear-composable-caches";
import { carryReviewNotice, takeCarriedReviewNotice } from "../use-recipe-ingest-review";
import { memoryUploadStorage } from "../use-recipe-ingest-upload-storage";
import type { UploadStorage } from "../use-recipe-ingest-upload-storage";
import type { IngestResponse } from "~/lib/api/types/recipe-ingest";

const api = vi.hoisted(() => ({
  upload: vi.fn(),
  createBatch: vi.fn(),
  sealBatch: vi.fn(),
  touchBatch: vi.fn(),
  getCounts: vi.fn(),
}));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }));

vi.mock("~/composables/api", () => ({
  useUserApi: () => ({ recipeIngest: api }),
}));
vi.mock("~/composables/use-toast", () => ({
  alert: toast,
}));

// ==========================================
// Helpers

/** The first bytes the server (and `addPhotos`) tells a format by */
const JPEG_HEAD = new Uint8Array([0xFF, 0xD8, 0xFF, 0xE0]);
const HEIC_HEAD = new Uint8Array([0, 0, 0, 24, ...new TextEncoder().encode("ftypheic")]);

let photoCount = 0;
function photo(name?: string, type = "image/jpeg"): File {
  photoCount += 1;
  const head = type === "image/heic" ? HEIC_HEAD : JPEG_HEAD;
  return new File([head, `photo ${photoCount}`], name ?? `IMG_${photoCount}.jpg`, { type });
}

function ok<T>(data: T) {
  return Promise.resolve({ data, error: null, response: null });
}

/** A failed request as the API client returns it: no data, and axios's error */
function failed(status: number | null, detail?: unknown, headers: Record<string, string> = {}) {
  const error = status === null
    ? { message: "Network Error" }
    : { response: { status, headers, data: detail === undefined ? {} : { detail } } };
  return Promise.resolve({ data: null, error, response: null });
}

function accepted(batchId = "b1", jobId = "j1"): IngestResponse {
  return {
    batchId,
    jobs: [{ id: jobId, status: "processing", pageCount: 1, reviewPath: `/g/home/recipes/cards/${jobId}` }],
    rejected: [],
    summary: "1 recipe card queued.",
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

/** The photos each upload request carried */
function uploadedPhotos() {
  return api.upload.mock.calls.map(call => call[0] as Blob[]);
}

function uploadOptions(n: number) {
  return api.upload.mock.calls[n]?.[1] as { batchId?: string; position?: number; localOnly?: boolean };
}

const keys = () => {
  let n = 0;
  return () => `k${++n}`;
};

function cards<T>(...groups: T[][]): DraftCard<T>[] {
  return groups.map((photos, i) => ({ key: `c${i}`, photos }));
}

const photosOf = <T>(list: DraftCard<T>[]) => list.map(card => card.photos);

beforeEach(() => {
  vi.clearAllMocks();
  resetRecipeIngestUploads();
  resetRecipeIngestCounts();
  localStorage.clear();
  api.createBatch.mockImplementation(() => ok({ id: "b1", source: "app" }));
  api.sealBatch.mockImplementation((id: string) => ok({ id, source: "app" }));
  api.touchBatch.mockImplementation((id: string) => ok({ id, source: "app" }));
  api.getCounts.mockImplementation(() => ok({ processing: 1, ready: 0, needsAttention: 0, failed: 0 }));
  api.upload.mockImplementation(() => ok(accepted()));
});

afterEach(() => {
  resetRecipeIngestUploads();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

// ==========================================
// Pure helpers

describe("grouping photos into cards", () => {
  test("one side: every photo is a card", () => {
    expect(groupPhotosIntoCards(["a", "b", "c"], "one-side")).toEqual([["a"], ["b"], ["c"]]);
  });

  test("front & back: pairs in selection order, a lone last front is a card of its own", () => {
    expect(groupPhotosIntoCards(["a1", "a2", "b1", "b2", "c1"], "front-and-back"))
      .toEqual([["a1", "a2"], ["b1", "b2"], ["c1"]]);
    expect(groupPhotosIntoCards([], "front-and-back")).toEqual([]);
  });

  test("swap turns a card's front and back round", () => {
    const swapped = swapSides(cards(["a1", "a2"], ["b1", "b2"]), 0);
    expect(photosOf(swapped)).toEqual([["a2", "a1"], ["b1", "b2"]]);
    expect(swapped[0]?.locked).toBe(true);
    // A one-photo card has nothing to swap
    expect(photosOf(swapSides(cards(["a1"]), 0))).toEqual([["a1"]]);
  });

  test("a card without a back: Split keeps its front and pairs every photo after it again", () => {
    // B has no back, so the pairing slipped by one photo from there on
    const paired = draftCards(["a1", "a2", "b1", "c1", "c2", "d1", "d2"], "front-and-back", keys());
    expect(photosOf(paired)).toEqual([["a1", "a2"], ["b1", "c1"], ["c2", "d1"], ["d2"]]);

    const fixed = splitCard(paired, 1, "front-and-back", keys());
    expect(photosOf(fixed)).toEqual([["a1", "a2"], ["b1"], ["c1", "c2"], ["d1", "d2"]]);
    expect(fixed.map(card => !!card.locked)).toEqual([false, true, false, false]);
  });

  test("Join adds the next photo to a card and pairs the rest again", () => {
    const paired = draftCards(["a1", "b1", "b2", "c1", "c2"], "front-and-back", keys());
    expect(photosOf(paired)).toEqual([["a1", "b1"], ["b2", "c1"], ["c2"]]);

    // Split the first card, then join its front... with nothing: a1 is a one-sided card
    const split = splitCard(paired, 0, "front-and-back", keys());
    expect(photosOf(split)).toEqual([["a1"], ["b1", "b2"], ["c1", "c2"]]);

    // A three-page card: join pulls the next card's front in
    const joined = joinCards(split, 1, "front-and-back", keys());
    expect(photosOf(joined)).toEqual([["a1"], ["b1", "b2", "c1"], ["c2"]]);
  });

  test("Join leaves cards the user already changed alone", () => {
    const list: DraftCard<string>[] = [
      { key: "a", photos: ["a1"] },
      { key: "b", photos: ["b1", "b2"], locked: true },
      { key: "c", photos: ["c1", "c2"] },
    ];
    expect(photosOf(joinCards(list, 0, "front-and-back", keys()))).toEqual([["a1", "b1"], ["b2"], ["c1", "c2"]]);
  });

  test("Join stops at the page limit and at the last card", () => {
    const list = cards(["a1", "a2", "a3", "a4"], ["b1"]);
    expect(canJoin(list, 0, 4)).toBe(false);
    expect(canJoin(list, 1, 4)).toBe(false);
    expect(photosOf(joinCards(list, 0, "front-and-back", keys(), 4))).toEqual(photosOf(list));
  });

  /** A PDF of 3 pages, one of 1 page, and one whose pages aren't known; the rest are photos */
  const documents: Record<string, number | null> = { doc3: 3, doc1: 1, unknown: null };
  const pages = (photo: string) => (photo in documents ? documents[photo] ?? null : 1);

  test("front & back: a document of several pages is a card of its own; a one-page one pairs like a photo", () => {
    expect(groupPhotosIntoCards(["a1", "doc3", "b1", "b2", "doc1", "c1", "unknown", "d1"], "front-and-back", pages))
      .toEqual([["a1"], ["doc3"], ["b1", "b2"], ["doc1", "c1"], ["unknown"], ["d1"]]);
    expect(groupPhotosIntoCards(["doc3", "a1"], "one-side", pages)).toEqual([["doc3"], ["a1"]]);
  });

  test("Join counts a document's pages, and never joins one whose pages aren't known", () => {
    const list = cards(["doc3"], ["a1"], ["b1"], ["unknown"]);
    expect(canJoin(list, 0, 4, pages)).toBe(true);
    expect(canJoin(list, 0, 3, pages)).toBe(false);
    expect(canJoin(cards(["a1"], ["doc3"]), 0, 4, pages)).toBe(true);
    expect(canJoin(cards(["a1", "a2"], ["doc3"]), 0, 4, pages)).toBe(false);
    expect(canJoin(list, 2, 4, pages)).toBe(false);
    expect(canJoin(cards(["unknown"], ["a1"]), 0, 4, pages)).toBe(false);
    expect(photosOf(joinCards(list, 0, "one-side", keys(), 4, pages))).toEqual([["doc3", "a1"], ["b1"], ["unknown"]]);
    expect(photosOf(joinCards(list, 0, "one-side", keys(), 3, pages))).toEqual(photosOf(list));
  });
});

describe("the queue reducer", () => {
  function run(...actions: UploadQueueAction[]): UploadQueueState {
    return actions.reduce(reduceUploadQueue, emptyUploadQueue());
  }
  const blob = new Blob(["x"]);
  const add = (key: string, batchKey = "B"): UploadQueueAction =>
    ({ type: "add-card", key, batchKey, photos: [blob] });

  test("cards get capture positions in their batch, and join the open batch", () => {
    const state = run(add("c1"), add("c2"), { type: "seal-requested", batchKey: "B" }, add("c3", "C"));
    expect(state.cards.map(card => [card.batchKey, card.position])).toEqual([["B", 0], ["B", 1], ["C", 0]]);
    expect(state.openBatchKey).toBe("C");
  });

  test("at most two cards upload at a time, in capture order", () => {
    const state = run(add("c1"), add("c2"), add("c3"), { type: "start", key: "c1" });
    expect(cardsToStart(state, 0).map(card => card.key)).toEqual(["c2"]);
    expect(cardsToStart(run(add("c1"), add("c2"), add("c3")), 0)).toHaveLength(MAX_CONCURRENT_UPLOADS);
  });

  test("a retry waits for its time", () => {
    const state = run(add("c1"), { type: "start", key: "c1" }, { type: "attempt-failed", key: "c1", error: null, retryAt: 5000 });
    expect(cardsToStart(state, 4999)).toEqual([]);
    expect(cardsToStart(state, 5000).map(card => card.key)).toEqual(["c1"]);
  });

  test("backoff doubles from 2 s, takes a longer Retry-After, and stays under a minute", () => {
    expect([0, 1, 2].map(n => retryDelay(n))).toEqual([2000, 4000, 8000]);
    expect(retryDelay(0, 10)).toBe(10_000);
    expect(retryDelay(0, 600)).toBe(60_000);
  });

  test("a 202 naming another batch is adopted", () => {
    const state = run(
      add("c1"),
      { type: "batch-created", batchKey: "B", serverId: "b1" },
      { type: "start", key: "c1" },
      { type: "uploaded", key: "c1", response: accepted("b2") },
    );
    expect(state.batches[0]).toMatchObject({ serverId: "b2", serverIds: ["b1", "b2"] });
    expect(state.cards[0]).toMatchObject({ status: "done", photos: [] });
  });

  test("a duplicate is done, pointing at the earlier card; other rejections fail for good", () => {
    const duplicate = run(add("c1"), { type: "start", key: "c1" }, {
      type: "uploaded",
      key: "c1",
      response: { batchId: "b1", jobs: [], rejected: [{ index: 0, reason: "duplicate", duplicateOf: "j0" }], summary: "" },
    });
    expect(duplicate.cards[0]).toMatchObject({ status: "done", duplicateOf: "j0", photos: [blob] });

    const unreadable = run(add("c1"), { type: "start", key: "c1" }, {
      type: "uploaded",
      key: "c1",
      response: { batchId: "b1", jobs: [], rejected: [{ index: 0, reason: "unreadable_image" }], summary: "" },
    });
    expect(unreadable.cards[0]).toMatchObject({ status: "failed", error: "unreadable_image", retryable: false });
  });

  test("a batch seals once every card has settled, and once only", () => {
    let state = run(
      add("c1"),
      add("c2"),
      { type: "batch-created", batchKey: "B", serverId: "b1" },
      { type: "seal-requested", batchKey: "B" },
      { type: "start", key: "c1" },
      { type: "uploaded", key: "c1", response: accepted("b1") },
    );
    expect(batchesToSeal(state)).toEqual([]);

    state = run(...[
      add("c1"),
      add("c2"),
      { type: "batch-created", batchKey: "B", serverId: "b1" },
      { type: "seal-requested", batchKey: "B" },
      { type: "start", key: "c1" },
      { type: "uploaded", key: "c1", response: accepted("b1") },
      { type: "start", key: "c2" },
      { type: "failed", key: "c2", error: null, retryable: true },
    ] as UploadQueueAction[]);
    expect(batchesToSeal(state)).toEqual([{ batchKey: "B", serverId: "b1" }]);

    state = reduceUploadQueue(state, { type: "sealed", batchKey: "B", serverId: "b1" });
    expect(batchesToSeal(state)).toEqual([]);
    // The uploaded card is forgotten; the failed one stays for Retry
    expect(state.cards.map(card => card.key)).toEqual(["c2"]);
  });
});

// ==========================================
// The singleton

describe("the upload queue", () => {
  test("a card uploads with its batch and capture position; the batch is created once", async () => {
    const queue = useRecipeIngestUploads();
    const [a, b, c] = [photo(), photo(), photo()];

    queue.takePhoto(a);
    queue.takePhoto(b);
    queue.takePhoto(c);
    await flushPromises();

    expect(api.createBatch).toHaveBeenCalledOnce();
    expect(uploadedPhotos()).toEqual([[a], [b], [c]]);
    expect([0, 1, 2].map(uploadOptions)).toEqual([
      { batchId: "b1", position: 0, localOnly: false },
      { batchId: "b1", position: 1, localOnly: false },
      { batchId: "b1", position: 2, localOnly: false },
    ]);
    expect(queue.isUploading.value).toBe(false);
    expect(queue.uploadedCount.value).toBe(3);
    expect(queue.lastUploadBatchId.value).toBe("b1");
    // The counts singleton is refreshed for the sidebar
    expect(api.getCounts).toHaveBeenCalled();
  });

  test("two cards upload at a time", async () => {
    const pending = [deferred<unknown>(), deferred<unknown>(), deferred<unknown>()];
    let n = 0;
    api.upload.mockImplementation(() => pending[n++]!.promise);
    const queue = useRecipeIngestUploads();

    await queue.addPhotos([photo(), photo(), photo()]);
    queue.uploadDrafts();
    await flushPromises();
    expect(api.upload).toHaveBeenCalledTimes(2);
    expect(queue.cards.value.map(card => card.status)).toEqual(["uploading", "uploading", "waiting"]);

    pending[0]!.resolve({ data: accepted(), error: null });
    await flushPromises();
    expect(api.upload).toHaveBeenCalledTimes(3);

    pending[1]!.resolve({ data: accepted(), error: null });
    pending[2]!.resolve({ data: accepted(), error: null });
    await flushPromises();
    expect(queue.isUploading.value).toBe(false);
  });

  test("upload progress is shown", async () => {
    const pending = deferred<unknown>();
    api.upload.mockImplementation((_files, _options, config) => {
      config.onUploadProgress({ loaded: 50, total: 200 });
      return pending.promise;
    });
    const queue = useRecipeIngestUploads();
    queue.takePhoto(photo());
    await flushPromises();

    expect(queue.cards.value[0]).toMatchObject({ status: "uploading", progress: 0.25 });
    pending.resolve({ data: accepted(), error: null });
    await flushPromises();
  });

  test("three retries with backoff, then Retry", async () => {
    vi.useFakeTimers();
    api.upload.mockImplementation(() => failed(null));
    const queue = useRecipeIngestUploads();
    queue.takePhoto(photo());
    await flushPromises();

    expect(api.upload).toHaveBeenCalledTimes(1);
    expect(queue.cards.value[0]?.status).toBe("retrying");

    for (const [delay, calls] of [[2000, 2], [4000, 3], [8000, 4]] as const) {
      await vi.advanceTimersByTimeAsync(delay - 1);
      expect(api.upload).toHaveBeenCalledTimes(calls - 1);
      await vi.advanceTimersByTimeAsync(1);
      expect(api.upload).toHaveBeenCalledTimes(calls);
    }
    expect(queue.cards.value[0]).toMatchObject({ status: "failed", retryable: true });
    await vi.advanceTimersByTimeAsync(60_000);
    expect(api.upload).toHaveBeenCalledTimes(4);

    api.upload.mockImplementation(() => ok(accepted()));
    queue.retry(queue.cards.value[0]!.key);
    await flushPromises();
    expect(api.upload).toHaveBeenCalledTimes(5);
    expect(queue.isUploading.value).toBe(false);
  });

  test("a rate limit waits as long as Retry-After asks", async () => {
    vi.useFakeTimers();
    api.upload
      .mockImplementationOnce(() => failed(429, { code: "too_many_jobs", message: "Too many" }, { "retry-after": "30" }))
      .mockImplementation(() => ok(accepted()));
    const queue = useRecipeIngestUploads();
    queue.takePhoto(photo());
    await flushPromises();

    await vi.advanceTimersByTimeAsync(29_999);
    expect(api.upload).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(api.upload).toHaveBeenCalledTimes(2);
    expect(toast.error).not.toHaveBeenCalled();
  });

  test("attempts don't toast: every queue request is quiet, and the card says why it's retrying or failed", async () => {
    vi.useFakeTimers();
    const paused = { code: "paused_for_restore", message: "Recipe cards are paused" };
    api.createBatch.mockImplementationOnce(() => failed(503, paused, { "retry-after": "60" }));
    api.upload.mockImplementation(() => failed(503, paused, { "retry-after": "60" }));
    const queue = useRecipeIngestUploads();
    queue.takePhoto(photo());
    await flushPromises();

    // the batch couldn't be created: the card waits for the restore like an upload would
    expect(api.createBatch).toHaveBeenCalledWith({ suppressAlert: true });
    expect(queue.cards.value[0]).toMatchObject({ status: "retrying", error: "paused_for_restore" });

    await vi.advanceTimersByTimeAsync(3 * 60_000);
    expect(api.upload).toHaveBeenCalledTimes(3);
    for (const call of api.upload.mock.calls) {
      expect(call[2]).toMatchObject({ suppressAlert: true, signal: expect.any(AbortSignal) });
    }
    expect(queue.cards.value[0]).toMatchObject({ status: "failed", error: "paused_for_restore", retryable: true });
    queue.done();
    await flushPromises();
    expect(api.sealBatch).toHaveBeenCalledWith("b1", { suppressAlert: true });
  });

  test("an error that retrying won't fix fails at once, without another toast", async () => {
    api.upload.mockImplementation(() => failed(400, { code: "ai_not_enabled", message: "AI isn't set up" }));
    const queue = useRecipeIngestUploads();
    queue.takePhoto(photo());
    await flushPromises();

    expect(api.upload).toHaveBeenCalledOnce();
    expect(queue.cards.value[0]).toMatchObject({ status: "failed", error: "ai_not_enabled", retryable: true });
    expect(toast.error).not.toHaveBeenCalled();
  });

  test("a 413 re-encodes the photos once in the browser, at most 3072 px as JPEG", async () => {
    const drawn: number[][] = [];
    const close = vi.fn();
    vi.stubGlobal("createImageBitmap", vi.fn(async () => ({ width: 6000, height: 4000, close })));
    vi.stubGlobal("OffscreenCanvas", class {
      constructor(public width: number, public height: number) {
        drawn.push([width, height]);
      }

      getContext() {
        return { fillStyle: "", fillRect: vi.fn(), drawImage: vi.fn() };
      }

      convertToBlob(options: { type: string; quality: number }) {
        return Promise.resolve(new Blob([`small ${options.quality}`], { type: options.type }));
      }
    });
    api.upload
      .mockImplementationOnce(() => failed(413))
      .mockImplementation(() => ok(accepted()));
    const queue = useRecipeIngestUploads();
    const [front, back] = [photo("IMG_0001.HEIC", "image/heic"), photo("back.jpeg")];
    queue.mode.value = "front-and-back";
    queue.takePhoto(front);
    queue.takePhoto(back);
    await flushPromises();

    expect(api.upload).toHaveBeenCalledTimes(2);
    expect(createImageBitmap).toHaveBeenCalledWith(front, { imageOrientation: "from-image" });
    expect(drawn).toEqual([[REENCODE_MAX_SIDE, 2048], [REENCODE_MAX_SIDE, 2048]]);
    expect(close).toHaveBeenCalledTimes(2);
    const [sent] = uploadedPhotos().slice(1);
    expect(sent?.map(file => [(file as File).name, file.type])).toEqual([
      ["IMG_0001.jpg", "image/jpeg"],
      ["back.jpg", "image/jpeg"],
    ]);
    // Same batch and position as the first attempt
    expect(uploadOptions(1)).toEqual(uploadOptions(0));
    expect(queue.isUploading.value).toBe(false);
  });

  test("a second 413 after re-encoding fails for good", async () => {
    vi.stubGlobal("createImageBitmap", vi.fn(async () => ({ width: 100, height: 100, close: vi.fn() })));
    vi.stubGlobal("OffscreenCanvas", class {
      getContext() {
        return { fillRect: vi.fn(), drawImage: vi.fn() };
      }

      convertToBlob() {
        return Promise.resolve(new Blob(["x"], { type: "image/jpeg" }));
      }
    });
    api.upload.mockImplementation(() => failed(413));
    const queue = useRecipeIngestUploads();
    queue.takePhoto(photo());
    await flushPromises();

    expect(api.upload).toHaveBeenCalledTimes(2);
    expect(queue.cards.value[0]).toMatchObject({ status: "failed", error: "too_large", retryable: false });
  });

  test("a photo the browser can't decode to make it smaller fails for good, saying so", async () => {
    vi.stubGlobal("createImageBitmap", vi.fn(async () => {
      throw new Error("decode");
    }));
    api.upload.mockImplementation(() => failed(413));
    const queue = useRecipeIngestUploads();
    queue.takePhoto(photo("IMG_0001.HEIC", "image/heic"));
    await flushPromises();

    expect(api.upload).toHaveBeenCalledOnce();
    expect(queue.cards.value[0]).toMatchObject({ status: "failed", error: CANNOT_SHRINK, retryable: false });
  });

  test("a duplicate is shown as Already scanned, whether the server answers 202 or 400", async () => {
    api.upload
      .mockImplementationOnce(() => ok({
        batchId: "b1",
        jobs: [],
        rejected: [{ index: 0, filename: "IMG_1.jpg", reason: "duplicate", duplicateOf: "j-earlier" }],
        summary: "",
      }))
      .mockImplementationOnce(() => failed(400, {
        batchId: "b1",
        jobs: [],
        rejected: [{ index: 0, filename: "IMG_2.jpg", reason: "duplicate", duplicateOf: "j-other" }],
        summary: "",
      }));
    const queue = useRecipeIngestUploads();
    queue.takePhoto(photo());
    queue.takePhoto(photo());
    await flushPromises();

    expect(queue.cards.value.map(card => [card.status, card.duplicateOf])).toEqual([
      ["done", "j-earlier"],
      ["done", "j-other"],
    ]);
    expect(api.upload).toHaveBeenCalledTimes(2);
    // Nothing new was queued
    expect(queue.uploadedCount.value).toBe(0);
  });

  test("cards already scanned or refused for good aren't counted as queued", async () => {
    api.upload
      .mockImplementationOnce(() => ok({ batchId: "b1", jobs: [], rejected: [{ index: 0, reason: "duplicate", duplicateOf: "j0" }], summary: "" }))
      .mockImplementationOnce(() => ok({ batchId: "b1", jobs: [], rejected: [{ index: 0, reason: "unreadable_image" }], summary: "" }))
      .mockImplementationOnce(() => failed(400, { code: "ai_not_enabled", message: "AI isn't set up" }))
      .mockImplementation(() => ok(accepted()));
    const queue = useRecipeIngestUploads();
    [photo(), photo(), photo(), photo()].forEach(queue.takePhoto);
    await flushPromises();

    expect(queue.cards.value.map(card => [card.status, card.retryable])).toEqual([
      ["done", false],
      ["failed", false],
      ["failed", true],
      ["done", false],
    ]);
    // the card that can be retried may still be taken
    expect(queue.openBatchCardCount.value).toBe(2);
  });

  test("a card goes to the batch a 202 names, and so do the cards after it", async () => {
    api.upload.mockImplementationOnce(() => ok(accepted("b2")));
    const queue = useRecipeIngestUploads();
    queue.takePhoto(photo());
    await flushPromises();
    queue.takePhoto(photo());
    await flushPromises();
    queue.done();
    await flushPromises();

    expect(uploadOptions(0).batchId).toBe("b1");
    expect(uploadOptions(1).batchId).toBe("b2");
    expect(api.sealBatch.mock.calls.map(call => call[0])).toContain("b2");
  });

  test("Done seals the batch only after the card still uploading has finished", async () => {
    const pending = deferred<unknown>();
    api.upload.mockImplementationOnce(() => ok(accepted())).mockImplementationOnce(() => pending.promise);
    const queue = useRecipeIngestUploads();
    queue.takePhoto(photo());
    await flushPromises();
    queue.takePhoto(photo());
    await flushPromises();

    queue.done();
    await flushPromises();
    expect(api.sealBatch).not.toHaveBeenCalled();
    expect(queue.openBatch.value).toBeNull();

    pending.resolve({ data: accepted(), error: null });
    await flushPromises();
    expect(api.sealBatch).toHaveBeenCalledExactlyOnceWith("b1", { suppressAlert: true });
    // Both cards went into the sealed batch
    expect([0, 1].map(n => uploadOptions(n).batchId)).toEqual(["b1", "b1"]);

    // Photos after Done start a new batch
    api.createBatch.mockImplementation(() => ok({ id: "b9", source: "app" }));
    queue.takePhoto(photo());
    await flushPromises();
    expect(uploadOptions(2)).toMatchObject({ batchId: "b9", position: 0 });
    expect(api.sealBatch).toHaveBeenCalledOnce();
  });

  test("Done waits for a card's retries, and seals once the card has failed for good", async () => {
    vi.useFakeTimers();
    api.upload.mockImplementation(() => failed(503, { code: "paused_for_restore", message: "Paused" }));
    const queue = useRecipeIngestUploads();
    queue.takePhoto(photo());
    await flushPromises();
    queue.done();
    await flushPromises();
    expect(api.sealBatch).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(2000 + 4000 + 8000);
    expect(queue.cards.value[0]?.status).toBe("failed");
    expect(api.sealBatch).toHaveBeenCalledExactlyOnceWith("b1", { suppressAlert: true });
  });

  test("front & back: the camera takes a front, then its back, and the card uploads", async () => {
    const queue = useRecipeIngestUploads();
    queue.mode.value = "front-and-back";
    const [front, back, retaken, lone] = [photo(), photo(), photo(), photo()];

    queue.takePhoto(front);
    expect(queue.pendingFront.value).toBe(front);
    expect(api.upload).not.toHaveBeenCalled();

    queue.retake(retaken);
    expect(queue.pendingFront.value).toBe(retaken);
    queue.takePhoto(back);
    expect(queue.pendingFront.value).toBeNull();

    queue.takePhoto(lone);
    queue.noBack();
    await flushPromises();
    expect(uploadedPhotos()).toEqual([[retaken, back], [lone]]);
  });

  test("Done queues a front waiting for its back and the photos not uploaded yet", async () => {
    const queue = useRecipeIngestUploads();
    queue.mode.value = "front-and-back";
    const [front, a, b] = [photo(), photo(), photo()];
    queue.takePhoto(front);
    await queue.addPhotos([a, b]);
    expect(queue.drafts.value.map(card => card.photos)).toEqual([[a, b]]);

    queue.done();
    await flushPromises();
    expect(uploadedPhotos()).toEqual([[front], [a, b]]);
    expect(api.sealBatch).toHaveBeenCalledExactlyOnceWith("b1", { suppressAlert: true });
  });

  test("chosen photos pair up as drafts, with Swap, Split and Join, and upload when asked", async () => {
    const queue = useRecipeIngestUploads();
    queue.mode.value = "front-and-back";
    const [a1, a2, b1, c1, c2] = [photo(), photo(), photo(), photo(), photo()];
    await queue.addPhotos([a1, a2, b1, c1, c2]);
    expect(queue.drafts.value.map(card => card.photos)).toEqual([[a1, a2], [b1, c1], [c2]]);

    queue.splitDraft(1);
    expect(queue.drafts.value.map(card => card.photos)).toEqual([[a1, a2], [b1], [c1, c2]]);
    queue.swapDraft(2);
    expect(queue.drafts.value.map(card => card.photos)).toEqual([[a1, a2], [b1], [c2, c1]]);
    queue.joinDraft(1);
    expect(queue.drafts.value.map(card => card.photos)).toEqual([[a1, a2], [b1, c2], [c1]]);
    queue.removeDraft(2);
    expect(api.upload).not.toHaveBeenCalled();

    queue.uploadDrafts();
    await flushPromises();
    expect(uploadedPhotos()).toEqual([[a1, a2], [b1, c2]]);
    expect(queue.drafts.value).toEqual([]);
  });

  test("switching to one side splits the drafts and sends a waiting front", async () => {
    const queue = useRecipeIngestUploads();
    queue.mode.value = "front-and-back";
    const [front, a, b] = [photo(), photo(), photo()];
    queue.takePhoto(front);
    await queue.addPhotos([a, b]);

    queue.mode.value = "one-side";
    await flushPromises();
    expect(queue.drafts.value.map(card => card.photos)).toEqual([[a], [b]]);
    expect(uploadedPhotos()).toEqual([[front]]);
  });

  test("the mode is remembered in this browser", () => {
    useRecipeIngestUploads().mode.value = "front-and-back";
    expect(localStorage.getItem(CAPTURE_MODE_STORAGE_KEY)).toBe("front-and-back");

    resetRecipeIngestUploads();
    expect(useRecipeIngestUploads().mode.value).toBe("front-and-back");
  });

  test("storage that throws (private browsing) doesn't break capture", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("denied");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("denied");
    });
    try {
      const queue = useRecipeIngestUploads();
      expect(queue.mode.value).toBe("one-side");
      queue.mode.value = "front-and-back";
      expect(queue.mode.value).toBe("front-and-back");
    }
    finally {
      vi.restoreAllMocks();
    }
  });

  test("a batch kept on this server sends localOnly with every card", async () => {
    const queue = useRecipeIngestUploads();
    queue.localOnly.value = true;
    queue.takePhoto(photo());
    await flushPromises();
    expect(uploadOptions(0).localOnly).toBe(true);
    expect(queue.cards.value[0]?.localOnly).toBe(true);
  });

  test("keeping cards on this server after some have gone finishes their batch; the next card starts a new one", async () => {
    api.createBatch
      .mockImplementationOnce(() => ok({ id: "b1", source: "app" }))
      .mockImplementation(() => ok({ id: "b2", source: "app" }));
    api.upload.mockImplementation((_files, options: { batchId: string }) => ok(accepted(options.batchId)));
    const queue = useRecipeIngestUploads();
    queue.takePhoto(photo());
    queue.takePhoto(photo());
    queue.takePhoto(photo());
    await flushPromises();
    const first = queue.openBatch.value?.key;

    queue.localOnly.value = true;
    // the server stored those three as cloud cards: their batch is finished, and the panel says why
    expect(queue.sentBeforeLocalOnlyChange.value).toBe(3);
    expect(queue.localOnlyFinishedBatch.value).toBe(true);
    await flushPromises();
    expect(api.sealBatch).toHaveBeenCalledExactlyOnceWith("b1", { suppressAlert: true });
    expect(queue.openBatch.value).toBeNull();
    // sealed: those cards are done with, so the note about them goes; the reason stays until the next photo
    expect(queue.sentBeforeLocalOnlyChange.value).toBe(0);
    expect(queue.localOnlyFinishedBatch.value).toBe(true);

    queue.takePhoto(photo());
    expect(queue.localOnlyFinishedBatch.value).toBe(false);
    await flushPromises();
    expect([0, 1, 2, 3].map(n => uploadOptions(n).localOnly)).toEqual([false, false, false, true]);
    expect(uploadOptions(3)).toMatchObject({ batchId: "b2", position: 0 });
    expect(queue.openBatch.value?.key).not.toBe(first);
  });

  test("keeping cards on this server while no card has gone keeps the batch, and its cards go with the new setting", async () => {
    vi.useFakeTimers();
    api.upload.mockImplementationOnce(() => failed(null)).mockImplementation(() => ok(accepted()));
    const queue = useRecipeIngestUploads();
    queue.takePhoto(photo());
    await flushPromises();
    expect(queue.cards.value[0]?.status).toBe("retrying");

    const batch = queue.openBatch.value?.key;
    queue.localOnly.value = true;
    expect(queue.sentBeforeLocalOnlyChange.value).toBe(0);
    expect(queue.openBatch.value?.key).toBe(batch);

    await vi.advanceTimersByTimeAsync(2000);
    expect([0, 1].map(n => uploadOptions(n).localOnly)).toEqual([false, true]);
    expect(api.sealBatch).not.toHaveBeenCalled();
  });

  describe("every card not sent yet takes the switch's setting, whatever batch it's in", () => {
    /** Holds the first two uploads, so two cards are on their way and the others wait */
    function holdFirstTwo() {
      const held = [deferred<unknown>(), deferred<unknown>()];
      let n = 0;
      api.upload.mockImplementation((_files, options: { batchId: string }) =>
        (n < 2 ? held[n++]!.promise : ok(accepted(options.batchId))));
      return () => held.forEach(h => h.resolve({ data: accepted(), error: null }));
    }
    const sentLocalOnly = () => api.upload.mock.calls.map(call => (call[1] as { localOnly: boolean }).localOnly);

    test("switched off and straight back on mid-batch: no card goes to the cloud", async () => {
      const release = holdFirstTwo();
      const queue = useRecipeIngestUploads();
      queue.localOnly.value = true;
      await queue.addPhotos([photo(), photo(), photo(), photo(), photo()]);
      queue.uploadDrafts();
      await flushPromises();
      expect(queue.cards.value.map(card => card.status))
        .toEqual(["uploading", "uploading", "waiting", "waiting", "waiting"]);

      // the two on their way stay on this server, and their batch is finished
      queue.localOnly.value = false;
      expect(queue.sentBeforeLocalOnlyChange.value).toBe(2);
      expect(queue.openBatch.value).toBeNull();
      // back on: the waiting cards of the finished batch take it too, and nothing went with the other setting
      queue.localOnly.value = true;
      expect(queue.sentBeforeLocalOnlyChange.value).toBe(0);

      release();
      await flushPromises();
      expect(sentLocalOnly()).toEqual([true, true, true, true, true]);
    });

    test("switched on after Done: the cards still waiting stay on this server", async () => {
      const release = holdFirstTwo();
      const queue = useRecipeIngestUploads();
      await queue.addPhotos([photo(), photo(), photo(), photo(), photo()]);
      queue.uploadDrafts();
      queue.done();
      await flushPromises();

      queue.localOnly.value = true;
      // the two on their way went with the cloud setting
      expect(queue.sentBeforeLocalOnlyChange.value).toBe(2);

      release();
      await flushPromises();
      expect(sentLocalOnly()).toEqual([false, false, true, true, true]);
      // every card is in and the batch is sealed: the note about the two has nothing left to say
      expect(api.sealBatch).toHaveBeenCalledOnce();
      expect(queue.sentBeforeLocalOnlyChange.value).toBe(0);
    });

    test("a card that failed, retried after the switch changed, goes with the new setting", async () => {
      api.upload.mockImplementationOnce(() => failed(400, { code: "ai_not_enabled", message: "AI isn't set up" }));
      const queue = useRecipeIngestUploads();
      queue.takePhoto(photo());
      queue.done();
      await flushPromises();
      expect(queue.cards.value[0]).toMatchObject({ status: "failed", retryable: true });
      expect(api.sealBatch).toHaveBeenCalledOnce();

      queue.localOnly.value = true;
      expect(queue.sentBeforeLocalOnlyChange.value).toBe(0);
      queue.retry(queue.cards.value[0]!.key);
      await flushPromises();
      expect(sentLocalOnly()).toEqual([false, true]);
    });

    test("a card on its way when the switch changed goes again with the new setting when that attempt fails", async () => {
      vi.useFakeTimers();
      const held = deferred<unknown>();
      api.upload.mockImplementationOnce(() => held.promise).mockImplementation(() => ok(accepted()));
      const queue = useRecipeIngestUploads();
      queue.takePhoto(photo());
      queue.done();
      await flushPromises();

      queue.localOnly.value = true;
      expect(queue.sentBeforeLocalOnlyChange.value).toBe(1);
      held.resolve({ data: null, error: { message: "Network Error" } });
      await flushPromises();
      // it never reached the server, so no card went with the cloud setting
      expect(queue.cards.value[0]?.status).toBe("retrying");
      expect(queue.sentBeforeLocalOnlyChange.value).toBe(0);

      await vi.advanceTimersByTimeAsync(2000);
      expect(sentLocalOnly()).toEqual([false, true]);
      expect(queue.sentBeforeLocalOnlyChange.value).toBe(0);
    });

    test("a card still waiting for its batch when the switch changed goes with the new setting", async () => {
      const batch = deferred<unknown>();
      api.createBatch.mockImplementationOnce(() => batch.promise);
      const queue = useRecipeIngestUploads();
      queue.takePhoto(photo());
      queue.done();
      await flushPromises();
      expect(queue.cards.value[0]?.status).toBe("uploading");

      queue.localOnly.value = true;
      expect(queue.sentBeforeLocalOnlyChange.value).toBe(0);
      batch.resolve({ data: { id: "b1", source: "app" }, error: null });
      await flushPromises();
      expect(sentLocalOnly()).toEqual([true]);
    });
  });

  test("leaving the page warns while photos are pending", async () => {
    const pending = deferred<unknown>();
    api.upload.mockImplementation(() => pending.promise);
    const queue = useRecipeIngestUploads();
    const leave = () => {
      const event = new Event("beforeunload", { cancelable: true });
      window.dispatchEvent(event);
      return event.defaultPrevented;
    };

    expect(leave()).toBe(false);
    queue.takePhoto(photo());
    await flushPromises();
    expect(leave()).toBe(true);

    pending.resolve({ data: accepted(), error: null });
    await flushPromises();
    expect(leave()).toBe(false);
  });

  test("signing out forgets the queue and stops its uploads, so the next user neither sees nor sends the photos", async () => {
    vi.useFakeTimers();
    const signals: AbortSignal[] = [];
    const pending = deferred<unknown>();
    api.upload
      .mockImplementationOnce((_files, _options, config: { signal: AbortSignal }) => {
        signals.push(config.signal);
        return pending.promise;
      })
      .mockImplementationOnce(() => failed(503, { code: "paused_for_restore" }, { "retry-after": "60" }))
      .mockImplementation(() => ok(accepted()));
    const queue = useRecipeIngestUploads();
    const counts = useRecipeIngestCounts();
    counts.set({ processing: 2, ready: 3, needsAttention: 0, failed: 0 });
    queue.takePhoto(photo());
    queue.takePhoto(photo());
    await queue.addPhotos([photo()]);
    await flushPromises();
    expect(queue.cards.value.map(card => card.status)).toEqual(["uploading", "retrying"]);

    carryReviewNotice("j1", { kind: "success", text: "Added Banana Mug Cake" });
    clearComposableCaches();
    expect(takeCarriedReviewNotice("j1")).toBeNull(); // the next user's review page shows nothing of this one's
    expect(signals[0]?.aborted).toBe(true);
    expect(queue.cards.value).toEqual([]);
    expect(queue.drafts.value).toEqual([]);
    expect(queue.hasPending.value).toBe(false);
    expect(counts.counts.value).toBeNull();

    // the retry that was due, and the upload that was in flight, change nothing
    pending.resolve({ data: accepted(), error: null });
    await vi.advanceTimersByTimeAsync(120_000);
    expect(api.upload).toHaveBeenCalledTimes(2);
    expect(useRecipeIngestUploads().cards.value).toEqual([]);
  });

  test("a lost batch (404) is created again on the next attempt", async () => {
    vi.useFakeTimers();
    api.upload
      .mockImplementationOnce(() => failed(404, { code: "not_found", message: "Not found" }))
      .mockImplementation(() => ok(accepted("b2")));
    api.createBatch
      .mockImplementationOnce(() => ok({ id: "b1", source: "app" }))
      .mockImplementation(() => ok({ id: "b2", source: "app" }));
    const queue = useRecipeIngestUploads();
    queue.takePhoto(photo());
    await flushPromises();
    await vi.advanceTimersByTimeAsync(2000);

    expect(api.createBatch).toHaveBeenCalledTimes(2);
    expect(uploadOptions(1).batchId).toBe("b2");
    expect(queue.isUploading.value).toBe(false);
  });
});

// ==========================================
// Thumbnails, shrinking, data saver (docs/ai/PHASE2.md §1.1)

/** createImageBitmap and a canvas whose JPEGs say what they were drawn from, at what size */
function stubImageDecoding(options: { width?: number; height?: number; fail?: (photo: Blob) => boolean } = {}) {
  const calls: { photo: Blob; options: ImageBitmapOptions | undefined }[] = [];
  const decode = vi.fn(async (photo: Blob, bitmapOptions?: ImageBitmapOptions) => {
    calls.push({ photo, options: bitmapOptions });
    if (options.fail?.(photo)) {
      throw new DOMException("The source image could not be decoded.", "InvalidStateError");
    }
    // a browser that honours resizeWidth hands back the small size
    const width = bitmapOptions?.resizeWidth ?? options.width ?? 4032;
    const height = bitmapOptions?.resizeWidth
      ? Math.round((options.height ?? 3024) * bitmapOptions.resizeWidth / (options.width ?? 4032))
      : options.height ?? 3024;
    return { width, height, close: vi.fn() };
  });
  vi.stubGlobal("createImageBitmap", decode);
  vi.stubGlobal("OffscreenCanvas", class {
    constructor(public width: number, public height: number) {}

    getContext() {
      return { fillStyle: "", fillRect: vi.fn(), drawImage: vi.fn() };
    }

    convertToBlob(convert: { type: string; quality: number }) {
      return Promise.resolve(new Blob([`${this.width}x${this.height} q${convert.quality}`], { type: convert.type }));
    }
  });
  return { decode, calls };
}

/** Object URLs that say which blob they show */
function stubObjectUrls() {
  const urls = new Map<string, Blob>();
  let n = 0;
  const revoked: string[] = [];
  vi.stubGlobal("URL", Object.assign(Object.create(URL), {
    createObjectURL: (blob: Blob) => {
      const url = `blob:test-${++n}`;
      urls.set(url, blob);
      return url;
    },
    revokeObjectURL: (url: string) => revoked.push(url),
  }));
  return { urls, revoked };
}

function blobText(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.onerror = () => reject(reader.error);
    reader.readAsText(blob);
  });
}

describe("thumbnails", () => {
  test("each photo gets a small thumbnail, made once, two at a time; the full photo is never shown", async () => {
    const { calls } = stubImageDecoding();
    const { urls, revoked } = stubObjectUrls();
    const queue = useRecipeIngestUploads();
    const photos = [photo(), photo(), photo()];
    await queue.addPhotos(photos);

    // asking starts it; the template shows a placeholder meanwhile
    expect(photos.map(p => queue.previewState(p))).toEqual(["pending", "pending", "pending"]);
    expect(queue.previewUrl(photos[0]!)).toBeNull();
    await Promise.resolve();
    expect(calls).toHaveLength(2);
    await flushPromises();
    expect(calls).toHaveLength(3);

    expect(calls[0]!.options).toEqual({ imageOrientation: "from-image", resizeWidth: PREVIEW_WIDTH, resizeQuality: "medium" });
    const url = queue.previewUrl(photos[0]!)!;
    expect(await blobText(urls.get(url)!)).toBe(`${PREVIEW_WIDTH}x240 q0.8`);
    // never an object URL of an original
    expect([...urls.values()].some(blob => photos.includes(blob as File))).toBe(false);

    // asked again: the same thumbnail
    queue.previewUrl(photos[0]!);
    await flushPromises();
    expect(calls).toHaveLength(3);

    // a photo that leaves the tray lets its thumbnail go
    queue.removeDraft(0);
    expect(revoked).toContain(url);
  });

  test("a browser that ignores resizeWidth still gets a small thumbnail", async () => {
    const { decode } = stubImageDecoding();
    decode.mockImplementation(async () => ({ width: 4000, height: 3000, close: vi.fn() }));
    const { urls } = stubObjectUrls();
    const queue = useRecipeIngestUploads();
    const chosen = photo();
    await queue.addPhotos([chosen]);
    queue.previewUrl(chosen);
    await flushPromises();

    expect(await blobText(urls.get(queue.previewUrl(chosen)!)!)).toBe(`${PREVIEW_WIDTH}x240 q0.8`);
  });

  test("a photo the browser can't decode (HEIC outside Safari) gets a placeholder", async () => {
    stubImageDecoding({ fail: p => p.type === "image/heic" });
    stubObjectUrls();
    const queue = useRecipeIngestUploads();
    const [heic, jpeg] = [photo("IMG_0001.HEIC", "image/heic"), photo()];
    await queue.addPhotos([heic, jpeg]);
    queue.previewState(heic);
    queue.previewState(jpeg);
    await flushPromises();

    expect(queue.previewState(heic)).toBe("unavailable");
    expect(queue.previewUrl(heic)).toBeNull();
    expect(queue.previewState(jpeg)).toBe("ready");
    expect(photoName(heic)).toBe("IMG_0001.HEIC");

    // the browser couldn't show a thumbnail after all
    queue.markPreviewBroken(jpeg);
    expect(queue.previewState(jpeg)).toBe("unavailable");
  });
});

describe("a photo too large for the server", () => {
  test.each(["too_large", "too_many_pixels"])("refused as %s, it's made smaller once and sent again", async (reason) => {
    stubImageDecoding({ width: 16000, height: 12000 });
    api.upload
      .mockImplementationOnce(() => failed(400, {
        code: "nothing_accepted",
        batchId: "b1",
        jobs: [],
        rejected: [{ index: 0, filename: "big.jpg", reason }],
        summary: "",
      }))
      .mockImplementation(() => ok(accepted()));
    const queue = useRecipeIngestUploads();
    queue.takePhoto(photo("big.jpg"));
    await flushPromises();

    expect(api.upload).toHaveBeenCalledTimes(2);
    const [resent] = uploadedPhotos()[1]!;
    expect(await blobText(resent!)).toBe(`${REENCODE_MAX_SIDE}x2304 q0.9`);
    expect(uploadOptions(1)).toEqual(uploadOptions(0));
    expect(queue.cards.value[0]).toMatchObject({ status: "done", reencoded: true });
  });

  test("still refused after shrinking: the server's reason, for good", async () => {
    stubImageDecoding();
    const refused = { batchId: "b1", jobs: [], rejected: [{ index: 0, reason: "too_large" }], summary: "" };
    api.upload.mockImplementation(() => failed(400, refused));
    const queue = useRecipeIngestUploads();
    queue.takePhoto(photo());
    await flushPromises();

    expect(api.upload).toHaveBeenCalledTimes(2);
    expect(queue.cards.value[0]).toMatchObject({ status: "failed", error: "too_large", retryable: false });
  });

  test("a HEIC photo this browser can't decode can't be made smaller: the card says so", async () => {
    stubImageDecoding({ fail: () => true });
    api.upload.mockImplementation(() => failed(400, {
      batchId: "b1",
      jobs: [],
      rejected: [{ index: 0, reason: "too_many_pixels" }],
      summary: "",
    }));
    const queue = useRecipeIngestUploads();
    queue.takePhoto(photo("IMG_0001.HEIC", "image/heic"));
    await flushPromises();

    expect(api.upload).toHaveBeenCalledOnce();
    expect(queue.cards.value[0]).toMatchObject({ status: "failed", error: CANNOT_SHRINK, retryable: false });
  });

  test("other refusals aren't sent again", async () => {
    const { decode } = stubImageDecoding();
    api.upload.mockImplementation(() => failed(400, {
      batchId: "b1",
      jobs: [],
      rejected: [{ index: 0, reason: "unreadable_image" }],
      summary: "",
    }));
    const queue = useRecipeIngestUploads();
    queue.takePhoto(photo());
    await flushPromises();

    expect(decode).not.toHaveBeenCalled();
    expect(queue.cards.value[0]).toMatchObject({ status: "failed", error: "unreadable_image" });
  });
});

// ==========================================
// Chosen and dropped files: what the server reads, and PDFs

const names = (list: readonly DraftCard[]) => list.map(card => card.photos.map(photoName));

/** A PDF whose full read waits for `release` (its first bytes come at once) */
function slowPdf(name = "slow.pdf") {
  const pdf = classicPdf(1, name);
  const gate = deferred<null>();
  const read = pdf.arrayBuffer.bind(pdf);
  Object.defineProperty(pdf, "arrayBuffer", { value: () => gate.promise.then(read) });
  return { pdf, release: () => gate.resolve(null) };
}

describe("chosen and dropped files", () => {
  test("files the server doesn't read are left out, and named", async () => {
    const queue = useRecipeIngestUploads();
    const result = await queue.addPhotos([
      photo("a.jpg"),
      file(["2 cups flour"], "notes.txt", "text/plain"),
      file(["GIF89a"], "card.gif", "image/gif"),
      photo("b.jpg"),
    ]);

    expect(result).toEqual({ unsupported: ["notes.txt", "card.gif"], tooManyPages: [] });
    expect(names(queue.drafts.value)).toEqual([["a.jpg"], ["b.jpg"]]);
    expect(await queue.addPhotos([file(["x"], "notes.txt", "text/plain")])).toEqual({
      unsupported: ["notes.txt"],
      tooManyPages: [],
    });
    expect(queue.drafts.value).toHaveLength(2);
  });

  test("a PDF is a card of its own, with its pages; one with more pages than a card can have is left out", async () => {
    const queue = useRecipeIngestUploads();
    queue.mode.value = "front-and-back";
    const [a, b, c] = [photo("a.jpg"), photo("b.jpg"), photo("c.jpg")];
    const scan = classicPdf(2, "scan.pdf");
    const result = await queue.addPhotos([a, scan, objectStreamPdf(5, { name: "long.pdf" }), b, c], 4);

    expect(result).toEqual({ unsupported: [], tooManyPages: ["long.pdf"] });
    expect(names(queue.drafts.value)).toEqual([["a.jpg"], ["scan.pdf"], ["b.jpg", "c.jpg"]]);
    expect(queue.pagesOf(scan)).toBe(2);
    expect(queue.canJoinDraft(0, 4)).toBe(true);
    expect(queue.canJoinDraft(0, 2)).toBe(false);
    expect(queue.canJoinDraft(1, 3)).toBe(true);
    expect(queue.canJoinDraft(1, 2)).toBe(false);

    queue.uploadDrafts();
    await flushPromises();
    expect(uploadedPhotos()).toEqual([[a], [scan], [b, c]]);
  });

  test("files added one after another keep their order, even when a PDF before them takes longer to read", async () => {
    const queue = useRecipeIngestUploads();
    const { pdf, release } = slowPdf();
    const first = queue.addPhotos([pdf]);
    const second = queue.addPhotos([photo("b.jpg")]);
    await flushPromises();
    expect(queue.drafts.value).toEqual([]);

    release();
    await Promise.all([first, second]);
    expect(names(queue.drafts.value)).toEqual([["slow.pdf"], ["b.jpg"]]);
  });

  test("a logout while files are read adds nothing", async () => {
    const queue = useRecipeIngestUploads();
    const { pdf, release } = slowPdf();
    const adding = queue.addPhotos([pdf, photo()]);
    resetRecipeIngestUploads();
    release();
    await adding;

    expect(useRecipeIngestUploads().drafts.value).toEqual([]);
  });

  test("a PDF goes as it is: no thumbnail decode, no data saver, no re-encode when it's refused as too large", async () => {
    const { decode } = stubImageDecoding();
    stubObjectUrls();
    api.upload.mockImplementation(() => failed(400, {
      batchId: "b1",
      jobs: [],
      rejected: [{ index: 0, filename: "scan.pdf", reason: "too_large" }],
      summary: "",
    }));
    const queue = useRecipeIngestUploads();
    queue.dataSaver.value = true;
    const scan = classicPdf(2);
    await queue.addPhotos([scan]);
    expect(queue.previewState(scan)).toBe("unavailable");
    expect(queue.isPdf(scan)).toBe(true);

    queue.uploadDrafts();
    await flushPromises();
    expect(decode).not.toHaveBeenCalled();
    expect(uploadedPhotos()).toEqual([[scan]]);
    expect(queue.cards.value[0]).toMatchObject({ status: "failed", error: "too_large", retryable: false });
  });

  test("a card of a photo and a PDF refused for the photo's size: only the photo is made smaller", async () => {
    stubImageDecoding({ width: 16000, height: 12000 });
    api.upload
      .mockImplementationOnce(() => failed(400, {
        batchId: "b1",
        jobs: [],
        rejected: [{ index: 0, filename: "big.jpg", reason: "too_many_pixels" }],
        summary: "",
      }))
      .mockImplementation(() => ok(accepted()));
    const queue = useRecipeIngestUploads();
    const [big, scan] = [photo("big.jpg"), classicPdf(1)];
    await queue.addPhotos([big, scan]);
    queue.joinDraft(0);
    queue.uploadDrafts();
    await flushPromises();

    const [resent, document] = uploadedPhotos()[1]!;
    expect(await blobText(resent!)).toBe(`${REENCODE_MAX_SIDE}x2304 q0.9`);
    expect(document).toBe(scan);
  });

  test("with data saver, a multi-page TIFF goes as it is: a smaller copy would keep one page", async () => {
    const { decode } = stubImageDecoding();
    const queue = useRecipeIngestUploads();
    queue.dataSaver.value = true;
    const scan = tiff([{}, {}]);
    await queue.addPhotos([scan]);
    queue.uploadDrafts();
    await flushPromises();

    expect(decode).not.toHaveBeenCalled();
    expect(uploadedPhotos()).toEqual([[scan]]);
  });

  test("a PDF read back from this device's storage is counted again before it can be joined", async () => {
    const queue = useRecipeIngestUploads();
    const restored = new File([classicPdf(3)], "scan.pdf", { type: "application/pdf" });
    expect(queue.pagesOf(restored)).toBeNull();
    await flushPromises();
    expect(queue.pagesOf(restored)).toBe(3);
    expect(queue.pagesOf(photo())).toBe(1);
  });
});

describe("data saver", () => {
  test("off (the default): the photos go as they are", async () => {
    const { decode } = stubImageDecoding();
    const queue = useRecipeIngestUploads();
    const original = photo();
    expect(queue.dataSaver.value).toBe(false);
    queue.takePhoto(original);
    await flushPromises();

    expect(uploadedPhotos()).toEqual([[original]]);
    expect(decode).not.toHaveBeenCalled();
  });

  test("on: each photo goes at most 4096 px, remembered in this browser", async () => {
    stubImageDecoding({ width: 8000, height: 6000 });
    const queue = useRecipeIngestUploads();
    queue.dataSaver.value = true;
    expect(localStorage.getItem(DATA_SAVER_STORAGE_KEY)).toBe("true");
    const big = new File(["x".repeat(10_000)], "IMG_1.jpg", { type: "image/jpeg" });
    queue.takePhoto(big);
    await flushPromises();

    const [sent] = uploadedPhotos()[0]!;
    expect(sent).not.toBe(big);
    expect((sent as File).name).toBe("IMG_1.jpg");
    expect(await blobText(sent!)).toBe(`${DATA_SAVER_MAX_SIDE}x3072 q0.9`);

    resetRecipeIngestUploads();
    expect(useRecipeIngestUploads().dataSaver.value).toBe(true);
  });

  test("on: a photo that would only grow, or that can't be decoded, goes as it is", async () => {
    stubImageDecoding({ fail: p => p.type === "image/heic" });
    const queue = useRecipeIngestUploads();
    queue.dataSaver.value = true;
    const [small, heic] = [photo("small.jpg"), photo("IMG_2.HEIC", "image/heic")];
    queue.takePhoto(small);
    queue.takePhoto(heic);
    await flushPromises();

    expect(uploadedPhotos()).toEqual([[small], [heic]]);
  });
});

// ==========================================
// Kept between visits (IndexedDB per user)

describe("the queue kept on this device", () => {
  /** A reload: everything in memory is gone, the storage stays */
  async function reload(storage: UploadStorage, userId = "u1") {
    resetRecipeIngestUploads();
    const queue = useRecipeIngestUploads();
    await queue.connect(userId, () => storage);
    await flushPromises();
    return queue;
  }

  test("photos not uploaded come back after a reload, and upload once", async () => {
    const storage = memoryUploadStorage();
    api.upload.mockImplementation(() => new Promise(() => {})); // offline: never answers
    const queue = useRecipeIngestUploads();
    await queue.connect("u1", () => storage);
    queue.mode.value = "front-and-back";
    const [front, back, trayA, trayB, waiting] = [photo("a.jpg"), photo("b.jpg"), photo("c.jpg"), photo("d.jpg"), photo("e.jpg")];
    queue.takePhoto(front);
    queue.takePhoto(back);
    await queue.addPhotos([trayA, trayB]);
    queue.takePhoto(waiting);
    await flushPromises();

    expect(api.upload).toHaveBeenCalledOnce();
    expect(storage.photos.size).toBe(5);

    api.upload.mockReset();
    api.upload.mockImplementation(() => ok(accepted()));
    const after = await reload(storage);

    // the card goes again, with its batch and position; the tray and the waiting front are back
    expect(uploadedPhotos().map(sent => sent.map(file => (file as File).name))).toEqual([["a.jpg", "b.jpg"]]);
    expect(uploadOptions(0)).toMatchObject({ batchId: "b1", position: 0 });
    expect(after.drafts.value.map(card => card.photos.map(p => (p as File).name))).toEqual([["c.jpg", "d.jpg"]]);
    expect((after.pendingFront.value as File).name).toBe("e.jpg");
    expect(after.openBatch.value).not.toBeNull();

    // uploaded: no longer kept
    expect([...storage.records.keys()].filter(key => key.startsWith("card:"))).toEqual([]);
    after.uploadDrafts();
    after.noBack();
    after.done();
    await flushPromises();
    expect(api.upload).toHaveBeenCalledTimes(3);
    expect(storage.photos.size).toBe(0);
    expect([...storage.records.keys()]).toEqual([]);

    // nothing left to resume
    await reload(storage);
    expect(api.upload).toHaveBeenCalledTimes(3);
  });

  test("a card that failed for good comes back failed, with its Retry", async () => {
    const storage = memoryUploadStorage();
    api.upload.mockImplementation(() => failed(400, { code: "ai_not_enabled" }));
    const queue = useRecipeIngestUploads();
    await queue.connect("u1", () => storage);
    queue.takePhoto(photo());
    await flushPromises();

    const after = await reload(storage);
    expect(after.cards.value.map(card => [card.status, card.error, card.retryable])).toEqual([
      ["failed", "ai_not_enabled", true],
    ]);
    expect(api.upload).toHaveBeenCalledOnce();
  });

  test("another user's queue is neither shown nor sent; a logout deletes the user's", async () => {
    const storages = new Map([["u1", memoryUploadStorage()], ["u2", memoryUploadStorage()]]);
    api.upload.mockImplementation(() => new Promise(() => {}));
    const queue = useRecipeIngestUploads();
    await queue.connect("u1", id => storages.get(id)!);
    await queue.addPhotos([photo()]);
    queue.localOnly.value = true;
    await flushPromises();
    expect(storages.get("u1")!.photos.size).toBe(1);

    // someone else signs in on this device
    await queue.connect("u2", id => storages.get(id)!);
    await flushPromises();
    expect(queue.drafts.value).toEqual([]);
    expect(queue.localOnly.value).toBe(false);
    expect(storages.get("u1")!.photos.size).toBe(1);

    // the first user comes back, then logs out (the header's Log out)
    await queue.connect("u1", id => storages.get(id)!);
    await flushPromises();
    expect(queue.drafts.value).toHaveLength(1);
    expect(queue.localOnly.value).toBe(true);
    await prepareRecipeIngestLogout();
    clearComposableCaches();
    await flushPromises();
    expect(storages.get("u1")!.photos.size).toBe(0);
    expect(storages.get("u1")!.records.size).toBe(0);
    expect(localStorage.getItem(`${LOCAL_ONLY_STORAGE_KEY}.u1`)).toBeNull();
  });

  test("a sign-out the user didn't choose (a changed password) keeps their queue and switch for when they're back", async () => {
    const storage = memoryUploadStorage();
    api.upload.mockImplementation(() => new Promise(() => {}));
    let queue = useRecipeIngestUploads();
    await queue.connect("u1", () => storage);
    queue.localOnly.value = true;
    queue.takePhoto(photo("a.jpg"));
    await flushPromises();

    clearComposableCaches();
    expect(useRecipeIngestUploads().cards.value).toEqual([]);
    expect(storage.photos.size).toBe(1);

    api.upload.mockReset();
    api.upload.mockImplementation(() => ok(accepted()));
    queue = useRecipeIngestUploads();
    await queue.connect("u1", () => storage);
    await flushPromises();
    // it goes as the user left it: kept on this server
    expect(uploadOptions(0)).toMatchObject({ localOnly: true });
    expect(uploadedPhotos().map(sent => sent.map(file => (file as File).name))).toEqual([["a.jpg"]]);
  });

  test("storage that fails keeps the queue in memory, and says so once", async () => {
    const storage = memoryUploadStorage();
    storage.save = vi.fn(() => Promise.reject(new DOMException("Quota exceeded", "QuotaExceededError")));
    vi.spyOn(console, "error").mockImplementation(() => {});
    const queue = useRecipeIngestUploads();
    await queue.connect("u1", () => storage);
    queue.takePhoto(photo());
    await flushPromises();

    expect(queue.storageFailed.value).toBe(true);
    expect(api.upload).toHaveBeenCalledOnce();
    queue.storageFailed.value = false;
    queue.takePhoto(photo());
    await flushPromises();
    expect(storage.save).toHaveBeenCalledOnce();
    expect(queue.storageFailed.value).toBe(false);
    vi.restoreAllMocks();
  });

  test("storage that fills up later: cards stored before that upload are deleted from it, so the next visit doesn't send them again", async () => {
    const storage = memoryUploadStorage();
    const save = storage.save;
    // the write with the third photo goes over the quota, as a whole (an IndexedDB transaction)
    storage.save = vi.fn((change, tab) => ([...change.putPhotos.values()].some(p => (p as File).name === "two.jpg")
      ? Promise.reject(new DOMException("Quota exceeded", "QuotaExceededError"))
      : save(change, tab)));
    vi.spyOn(console, "error").mockImplementation(() => {});
    const answers = new Map<string, (value: unknown) => void>();
    api.upload.mockImplementation((photos: File[]) => new Promise(resolve => answers.set(photos[0]!.name, resolve)));
    const queue = useRecipeIngestUploads();
    await queue.connect("u1", () => storage);

    queue.takePhoto(photo("kept.jpg"));
    await flushPromises();
    queue.takePhoto(photo("one.jpg"));
    await flushPromises();
    queue.takePhoto(photo("two.jpg"));
    await flushPromises();
    expect(queue.storageFailed.value).toBe(true);
    expect([...storage.records.keys()].filter(key => key.startsWith("card:"))).toHaveLength(2);

    // the cards upload (two at a time); the first never answers (still on its way when the page closes)
    answers.get("one.jpg")!({ data: accepted("b1", "j1"), error: null, response: null });
    await flushPromises();
    answers.get("two.jpg")!({ data: accepted("b1", "j2"), error: null, response: null });
    await flushPromises();
    expect([...storage.records.keys()].filter(key => key.startsWith("card:"))).toHaveLength(1);
    expect(storage.photos.size).toBe(1);

    // the next visit sends only the card that was still on its way
    resetRecipeIngestUploads();
    api.upload.mockReset();
    api.upload.mockImplementation(() => ok(accepted()));
    const after = useRecipeIngestUploads();
    await after.connect("u1", () => storage);
    await flushPromises();
    expect(uploadedPhotos().map(sent => sent.map(file => (file as File).name))).toEqual([["kept.jpg"]]);
    vi.restoreAllMocks();
  });
});

describe("choices this browser remembers, changed in another of its tabs", () => {
  /** Another tab writes the choice: this one is told by a `storage` event */
  function changedElsewhere(key: string, value: string | null) {
    if (value === null) {
      localStorage.removeItem(key);
    }
    else {
      localStorage.setItem(key, value);
    }
    window.dispatchEvent(new StorageEvent("storage", { key, newValue: value, storageArea: localStorage }));
  }

  test("this tab follows them, without writing them again, and a card not sent yet goes with the switch", async () => {
    const created = deferred<unknown>();
    api.createBatch.mockImplementation(() => created.promise);
    const queue = useRecipeIngestUploads();
    await queue.connect("u1", () => memoryUploadStorage());
    queue.takePhoto(photo("waiting.jpg"));
    await flushPromises();

    changedElsewhere(`${LOCAL_ONLY_STORAGE_KEY}.u1`, "true");
    changedElsewhere(CAPTURE_MODE_STORAGE_KEY, "front-and-back");
    changedElsewhere(DATA_SAVER_STORAGE_KEY, "true");
    expect([queue.localOnly.value, queue.mode.value, queue.dataSaver.value]).toEqual([true, "front-and-back", true]);
    // turned off again elsewhere (removed, not written as "false")
    changedElsewhere(DATA_SAVER_STORAGE_KEY, null);
    expect(queue.dataSaver.value).toBe(false);

    created.resolve({ data: { id: "b1", source: "app" }, error: null, response: null });
    await flushPromises();
    expect(uploadOptions(0)).toMatchObject({ localOnly: true });
    // another user's switch isn't this one's
    changedElsewhere(`${LOCAL_ONLY_STORAGE_KEY}.u2`, null);
    expect(queue.localOnly.value).toBe(true);
  });

  test("a card read back from another tab's queue goes with that tab's switch, until the switch changes here", async () => {
    const storage = memoryUploadStorage();
    api.upload.mockImplementation(() => new Promise(() => {}));
    const first = useRecipeIngestUploads();
    await first.connect("u1", () => storage);
    first.localOnly.value = true;
    first.takePhoto(photo("private.jpg"));
    first.takePhoto(photo("also-private.jpg"));
    await flushPromises();
    expect(uploadOptions(0)).toMatchObject({ localOnly: true });

    // the next tab can't read what this browser remembers (a private window)
    resetRecipeIngestUploads();
    localStorage.clear();
    api.upload.mockReset();
    const sent = deferred<unknown>();
    api.upload.mockImplementationOnce(() => ok(accepted("b1", "j1")))
      .mockImplementationOnce(() => sent.promise);
    const queue = useRecipeIngestUploads();
    await queue.connect("u1", () => storage);
    await flushPromises();
    expect(queue.localOnly.value).toBe(false);
    expect(uploadOptions(0)).toMatchObject({ localOnly: true });
    expect(uploadOptions(1)).toMatchObject({ localOnly: true });

    // the second card's attempt fails: when it goes again, it's with the switch as it is here once it's changed
    sent.resolve({ data: null, error: { message: "Network Error" }, response: null });
    await flushPromises();
    queue.localOnly.value = true;
    queue.localOnly.value = false;
    expect(queue.cards.value.map(card => card.localOnlyChoice)).toEqual([null, null]);
    await vi.waitFor(() => expect(api.upload).toHaveBeenCalledTimes(3), { timeout: 5000 });
    expect(uploadOptions(2)).toMatchObject({ localOnly: false });
  });

  test("a card read back after a reload goes with the switch when it changes while the card waits for its batch", async () => {
    const storage = memoryUploadStorage();
    // offline: the card is stored with the switch off, and never gets its batch
    api.createBatch.mockImplementation(() => new Promise(() => {}));
    const before = useRecipeIngestUploads();
    await before.connect("u1", () => storage);
    before.takePhoto(photo("card-one.jpg"));
    await flushPromises();
    expect(api.upload).not.toHaveBeenCalled();

    // the page is reloaded: the card is read back with the switch it was stored with, and waits for its batch again
    resetRecipeIngestUploads();
    const created = deferred<unknown>();
    api.createBatch.mockImplementation(() => created.promise);
    const queue = useRecipeIngestUploads();
    await queue.connect("u1", () => storage);
    await flushPromises();
    expect(queue.cards.value.map(card => [card.status, card.localOnlyChoice])).toEqual([["uploading", false]]);

    queue.localOnly.value = true;
    created.resolve({ data: { id: "b1", source: "app" }, error: null, response: null });
    await flushPromises();
    await vi.waitFor(() => expect(api.upload).toHaveBeenCalledTimes(1));
    expect(uploadOptions(0)).toMatchObject({ localOnly: true });
    expect(queue.cards.value.map(card => card.localOnly)).toEqual([true]);
  });

  test("a front read back while this tab is in One side mode pairs with the next photo, as its \"Back side\" says", async () => {
    const storage = memoryUploadStorage();
    const queue = useRecipeIngestUploads();
    await queue.connect("u1", () => storage);
    queue.mode.value = "front-and-back";
    queue.takePhoto(photo("front.jpg"));
    await flushPromises();

    resetRecipeIngestUploads();
    localStorage.setItem(CAPTURE_MODE_STORAGE_KEY, "one-side");
    const after = useRecipeIngestUploads();
    await after.connect("u1", () => storage);
    await flushPromises();
    expect(after.mode.value).toBe("one-side");
    expect((after.pendingFront.value as File).name).toBe("front.jpg");
    after.takePhoto(photo("back.jpg"));
    await flushPromises();
    expect(uploadedPhotos().map(sent => sent.map(file => (file as File).name))).toEqual([["front.jpg", "back.jpg"]]);
  });
});

// ==========================================
// Failures elsewhere, logout, Scan again

describe("a card that fails for good", () => {
  test("while no cards page is open, is counted for the layout until one opens", async () => {
    api.upload.mockImplementation(() => failed(400, { code: "ai_not_enabled" }));
    const queue = useRecipeIngestUploads();
    queue.takePhoto(photo());
    await flushPromises();
    expect(queue.failedWhileAway.value).toBe(1);

    const close = queue.openCardsPage();
    expect(queue.failedWhileAway.value).toBe(0);
    queue.takePhoto(photo());
    await flushPromises();
    // the page shows it
    expect(queue.failedWhileAway.value).toBe(0);
    close();
    queue.takePhoto(photo());
    await flushPromises();
    expect(queue.failedWhileAway.value).toBe(1);
  });
});

describe("logging out", () => {
  test("counts the photos it would drop, and seals the batches the queue started", async () => {
    api.upload
      .mockImplementationOnce(() => ok(accepted()))
      .mockImplementation(() => new Promise(() => {}));
    const queue = useRecipeIngestUploads();
    queue.takePhoto(photo());
    await flushPromises();
    expect(recipeIngestPhotosNotUploaded.value).toBe(0);

    queue.takePhoto(photo());
    await queue.addPhotos([photo(), photo()]);
    await flushPromises();
    expect(recipeIngestPhotosNotUploaded.value).toBe(3);

    await prepareRecipeIngestLogout();
    expect(api.sealBatch).toHaveBeenCalledExactlyOnceWith("b1", { suppressAlert: true });
  });

  test("a review page's edit not saved yet is saved first, while the session is still there", async () => {
    const saved = deferred<undefined>();
    const save = vi.fn(() => saved.promise);
    const stop = onRecipeIngestLogout(save);
    let finished = false;
    void prepareRecipeIngestLogout().then(() => {
      finished = true;
    });
    await flushPromises();
    expect(save).toHaveBeenCalledOnce();
    expect(finished).toBe(false);

    saved.resolve(undefined);
    await flushPromises();
    expect(finished).toBe(true);
    stop();
    await prepareRecipeIngestLogout();
    expect(save).toHaveBeenCalledOnce();
  });

  test("a save that doesn't answer doesn't hold the logout up either", async () => {
    vi.useFakeTimers();
    const stop = onRecipeIngestLogout(() => new Promise(() => {}));
    let finished = false;
    void prepareRecipeIngestLogout(3000).then(() => {
      finished = true;
    });
    await vi.advanceTimersByTimeAsync(3000);
    expect(finished).toBe(true);
    stop();
  });

  test("photos pending ask before the page is left, unless the session has gone (an expired session's redirect)", async () => {
    api.upload.mockImplementation(() => new Promise(() => {}));
    const queue = useRecipeIngestUploads();
    queue.takePhoto(photo());
    await flushPromises();
    const leave = () => {
      const event = new Event("beforeunload", { cancelable: true });
      window.dispatchEvent(event);
      return event.defaultPrevented;
    };
    let signedIn = true;
    setRecipeIngestSessionCheck(() => signedIn);
    expect(leave()).toBe(true);
    signedIn = false;
    expect(leave()).toBe(false);
    setRecipeIngestSessionCheck(null);
  });

  test("a sealing server that doesn't answer doesn't hold the logout up", async () => {
    vi.useFakeTimers();
    api.sealBatch.mockImplementation(() => new Promise(() => {}));
    const queue = useRecipeIngestUploads();
    queue.takePhoto(photo());
    await flushPromises();

    let finished = false;
    void prepareRecipeIngestLogout(3000).then(() => {
      finished = true;
    });
    await vi.advanceTimersByTimeAsync(3000);
    expect(finished).toBe(true);
  });
});

describe("Done", () => {
  test.each([
    ["already scanned", () => ok({ batchId: "b1", jobs: [], rejected: [{ index: 0, reason: "duplicate", duplicateOf: "j0" }], summary: "" })],
    ["refused", () => failed(400, { batchId: "b1", jobs: [], rejected: [{ index: 0, reason: "unreadable_image" }], summary: "" })],
  ])("seals the batch when its only card was %s", async (_name, answer) => {
    api.upload.mockImplementation(answer);
    const queue = useRecipeIngestUploads();
    queue.takePhoto(photo());
    await flushPromises();
    queue.done();
    await flushPromises();

    expect(api.sealBatch).toHaveBeenCalledExactlyOnceWith("b1", { suppressAlert: true });
  });
});

describe("Scan again", () => {
  test("sends a card marked Already scanned again, whole, as a new card", async () => {
    api.upload.mockImplementationOnce(() => ok({
      batchId: "b1",
      jobs: [],
      rejected: [{ index: 0, reason: "duplicate", duplicateOf: "j-earlier" }],
      summary: "",
    }));
    const queue = useRecipeIngestUploads();
    queue.mode.value = "front-and-back";
    const [front, back] = [photo(), photo()];
    queue.takePhoto(front);
    queue.takePhoto(back);
    await flushPromises();
    expect(queue.cards.value[0]).toMatchObject({ status: "done", duplicateOf: "j-earlier" });

    queue.scanAgain(queue.cards.value[0]!.key);
    await flushPromises();
    expect(uploadedPhotos()).toEqual([[front, back], [front, back]]);
    expect(uploadOptions(1)).toEqual({ batchId: "b1", position: 0, localOnly: false, allowDuplicate: true });
    expect(queue.cards.value[0]).toMatchObject({ status: "done", duplicateOf: null });
    expect(queue.uploadedCount.value).toBe(1);
  });
});

describe("an open batch while the capture page is open", () => {
  function setVisibility(state: "visible" | "hidden") {
    Object.defineProperty(document, "visibilityState", { value: state, configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
  }

  function touched() {
    return api.touchBatch.mock.calls.map(call => call[0] as string);
  }

  let stops: (() => void)[] = [];

  beforeEach(() => {
    setVisibility("visible");
    stops = [];
  });

  afterEach(() => {
    stops.forEach(stop => stop());
    setVisibility("visible");
  });

  /** The capture page mounts: what it does with `keepBatchOpen` */
  function openCapturePage(queue: ReturnType<typeof useRecipeIngestUploads>) {
    const stop = queue.keepBatchOpen();
    stops.push(stop);
    return stop;
  }

  test("is touched every 3 minutes, quietly, so a pause in the stack doesn't end it", async () => {
    vi.useFakeTimers();
    const queue = useRecipeIngestUploads();
    openCapturePage(queue);
    await vi.advanceTimersByTimeAsync(BATCH_HEARTBEAT_MS);
    // no batch yet: nothing to keep open
    expect(api.touchBatch).not.toHaveBeenCalled();

    queue.takePhoto(photo());
    await flushPromises();
    await vi.advanceTimersByTimeAsync(BATCH_HEARTBEAT_MS - 1);
    expect(api.touchBatch).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(api.touchBatch).toHaveBeenCalledExactlyOnceWith("b1", { suppressAlert: true });

    // a 12-minute pause (the server ends an app batch after 10 idle minutes): touched all along
    await vi.advanceTimersByTimeAsync(4 * BATCH_HEARTBEAT_MS);
    expect(touched()).toEqual(["b1", "b1", "b1", "b1", "b1"]);
    expect(BATCH_HEARTBEAT_MS).toBe(3 * 60_000);

    queue.takePhoto(photo());
    await flushPromises();
    expect(uploadOptions(1).batchId).toBe("b1");
    expect(api.createBatch).toHaveBeenCalledOnce();
  });

  test("not once the page is closed, nor after Done", async () => {
    vi.useFakeTimers();
    const queue = useRecipeIngestUploads();
    const close = openCapturePage(queue);
    queue.takePhoto(photo());
    await flushPromises();

    close();
    await vi.advanceTimersByTimeAsync(4 * BATCH_HEARTBEAT_MS);
    expect(api.touchBatch).not.toHaveBeenCalled();

    openCapturePage(queue);
    expect(touched()).toEqual(["b1"]);
    queue.done();
    await flushPromises();
    await vi.advanceTimersByTimeAsync(4 * BATCH_HEARTBEAT_MS);
    expect(touched()).toEqual(["b1"]);
  });

  test("not while the page is hidden; at once when it's back", async () => {
    vi.useFakeTimers();
    const queue = useRecipeIngestUploads();
    queue.takePhoto(photo());
    await flushPromises();
    // a capture page opened with a batch already open touches it at once
    openCapturePage(queue);
    expect(touched()).toEqual(["b1"]);

    setVisibility("hidden");
    await vi.advanceTimersByTimeAsync(4 * BATCH_HEARTBEAT_MS);
    expect(touched()).toEqual(["b1"]);

    setVisibility("visible");
    await flushPromises();
    expect(touched()).toEqual(["b1", "b1"]);
    await vi.advanceTimersByTimeAsync(BATCH_HEARTBEAT_MS);
    expect(touched()).toEqual(["b1", "b1", "b1"]);
  });

  test("ended anyway (409): the next photo starts a new batch, without a word; Done seals that one", async () => {
    vi.useFakeTimers();
    const queue = useRecipeIngestUploads();
    queue.takePhoto(photo());
    await flushPromises();
    // the page was away for longer than 10 minutes: the server sealed the batch
    api.touchBatch.mockImplementation(() => failed(409, { code: "batch_sealed" }));
    api.createBatch.mockImplementation(() => ok({ id: "b2", source: "app" }));
    api.upload.mockImplementation(() => ok(accepted("b2", "j2")));
    openCapturePage(queue);
    await flushPromises();

    // a sealed batch isn't touched again
    await vi.advanceTimersByTimeAsync(2 * BATCH_HEARTBEAT_MS);
    expect(touched()).toEqual(["b1"]);

    queue.takePhoto(photo());
    await flushPromises();
    expect(api.createBatch).toHaveBeenCalledTimes(2);
    expect(uploadOptions(1)).toMatchObject({ batchId: "b2", position: 1 });
    expect(toast.error).not.toHaveBeenCalled();
    expect(toast.info).not.toHaveBeenCalled();

    api.touchBatch.mockImplementation((id: string) => ok({ id, source: "app" }));
    await vi.advanceTimersByTimeAsync(BATCH_HEARTBEAT_MS);
    expect(touched()).toEqual(["b1", "b2"]);

    queue.done();
    await flushPromises();
    expect(api.sealBatch.mock.calls.map(call => call[0])).toEqual(["b2"]);
  });

  test("a batch gone from the server (404) is treated the same", async () => {
    vi.useFakeTimers();
    const queue = useRecipeIngestUploads();
    queue.takePhoto(photo());
    await flushPromises();
    api.touchBatch.mockImplementation(() => failed(404, { code: "not_found" }));
    api.createBatch.mockImplementation(() => ok({ id: "b2", source: "app" }));
    openCapturePage(queue);
    await flushPromises();

    queue.takePhoto(photo());
    await flushPromises();
    expect(uploadOptions(1).batchId).toBe("b2");
  });

  test("a touch that fails otherwise (offline, a restore) keeps the batch and tries again at the next beat", async () => {
    vi.useFakeTimers();
    const queue = useRecipeIngestUploads();
    queue.takePhoto(photo());
    await flushPromises();
    api.touchBatch
      .mockImplementationOnce(() => failed(null))
      .mockImplementationOnce(() => failed(503, { code: "paused_for_restore" }));
    openCapturePage(queue);
    await vi.advanceTimersByTimeAsync(2 * BATCH_HEARTBEAT_MS);
    expect(touched()).toEqual(["b1", "b1", "b1"]);

    queue.takePhoto(photo());
    await flushPromises();
    expect(uploadOptions(1).batchId).toBe("b1");
    expect(api.createBatch).toHaveBeenCalledOnce();
  });

  test("an open batch that comes back after a reload is touched at once by the page already shown", async () => {
    const storage = memoryUploadStorage();
    const first = useRecipeIngestUploads();
    await first.connect("u1", () => storage);
    first.takePhoto(photo());
    await flushPromises();
    expect(first.openBatch.value?.serverId).toBe("b1");

    // a reload: the page is shown before the stored queue has been read back
    resetRecipeIngestUploads();
    const queue = useRecipeIngestUploads();
    openCapturePage(queue);
    expect(api.touchBatch).not.toHaveBeenCalled();
    await queue.connect("u1", () => storage);
    await flushPromises();
    expect(touched()).toEqual(["b1"]);
  });

  test("a logout stops the beat", async () => {
    vi.useFakeTimers();
    const queue = useRecipeIngestUploads();
    queue.takePhoto(photo());
    await flushPromises();
    openCapturePage(queue);
    api.touchBatch.mockClear();

    resetRecipeIngestUploads();
    await vi.advanceTimersByTimeAsync(4 * BATCH_HEARTBEAT_MS);
    expect(api.touchBatch).not.toHaveBeenCalled();
  });
});
