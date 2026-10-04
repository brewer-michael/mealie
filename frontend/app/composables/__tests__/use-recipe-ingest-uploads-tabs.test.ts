// @vitest-environment node
// Two tabs of one browser (docs/ai/PHASE2.md §1.1): one IDBFactory is the origin's IndexedDB, one lock manager its Web
// Locks, Node's BroadcastChannel its channels, and each tab is a module instance of its own (`vi.resetModules`).
// Node's Blob survives IndexedDB's structured clone (jsdom's doesn't), as a browser's does.
import { IDBFactory } from "fake-indexeddb";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

const api = vi.hoisted(() => ({
  upload: vi.fn(),
  createBatch: vi.fn(),
  sealBatch: vi.fn(),
  touchBatch: vi.fn(),
  getCounts: vi.fn(),
}));
vi.mock("~/composables/api", () => ({ useUserApi: () => ({ recipeIngest: api }) }));
vi.mock("~/composables/use-toast", () => ({ alert: { success: vi.fn(), error: vi.fn(), info: vi.fn() } }));

const JPEG_HEAD = new Uint8Array([0xFF, 0xD8, 0xFF, 0xE0]);

function photo(text: string) {
  return new File([JPEG_HEAD, text], `${text}.jpg`, { type: "image/jpeg" });
}

/** Lets requests, IndexedDB transactions and channel messages run */
async function settle(rounds = 20) {
  for (let i = 0; i < rounds; i++) {
    await new Promise(resolve => setTimeout(resolve, 5));
  }
}

/** What each upload request carried: the tab that sent it and its first photo's text */
const sent: { tab: string; photo: string }[] = [];
/** The tab whose module sends the next requests (the mock is shared, as the server is) */
let sendingTab = "";

/**
 * The browser's Web Locks, enough for the queue: exclusive locks, `ifAvailable`, `signal` and `steal`. A lock is
 * granted a moment later, as a browser does; a stolen lock's request rejects with an AbortError, at once or, for a tab
 * frozen in the background, when `wake` says it runs again.
 */
function fakeLocks() {
  let frozen = false;
  const heldBack: (() => void)[] = [];
  interface Request {
    callback: (lock: { name: string } | null) => unknown;
    resolve: (value: unknown) => void;
    reject: (error: unknown) => void;
  }
  const held = new Map<string, Request>();
  const lines = new Map<string, Request[]>();
  const line = (name: string) => {
    if (!lines.has(name)) {
      lines.set(name, []);
    }
    return lines.get(name)!;
  };
  function release(name: string, request: Request) {
    if (held.get(name) !== request) {
      return;
    }
    held.delete(name);
    const next = line(name).shift();
    if (next) {
      grant(name, next);
    }
  }
  function grant(name: string, request: Request) {
    held.set(name, request);
    queueMicrotask(() => {
      Promise.resolve()
        .then(() => request.callback({ name }))
        .then(
          (value) => {
            release(name, request);
            request.resolve(value);
          },
          (error) => {
            release(name, request);
            request.reject(error);
          },
        );
    });
  }
  return {
    held,
    /** The tab holding a lock that's stolen is frozen: it's told when `wake` is called */
    freeze() {
      frozen = true;
    },
    wake() {
      frozen = false;
      heldBack.splice(0).forEach(tell => tell());
    },
    request(name: string, options: { ifAvailable?: boolean; steal?: boolean; signal?: AbortSignal }, callback: Request["callback"]) {
      return new Promise((resolve, reject) => {
        const request: Request = { callback, resolve, reject };
        if (options.steal) {
          const old = held.get(name);
          held.delete(name);
          const tell = () => old?.reject(new DOMException("The lock was stolen", "AbortError"));
          if (frozen) {
            heldBack.push(tell);
          }
          else {
            tell();
          }
          grant(name, request);
          return;
        }
        const busy = held.has(name) || line(name).length > 0;
        if (options.ifAvailable && busy) {
          queueMicrotask(() => Promise.resolve(callback(null)).then(resolve, reject));
          return;
        }
        if (!busy) {
          grant(name, request);
          return;
        }
        line(name).push(request);
        options.signal?.addEventListener("abort", () => {
          const waiting = line(name);
          if (waiting.includes(request)) {
            waiting.splice(waiting.indexOf(request), 1);
            reject(new DOMException("The request was aborted", "AbortError"));
          }
        });
      });
    },
  };
}

type UploadsModule = typeof import("../use-recipe-ingest-uploads");
type StorageModule = typeof import("../use-recipe-ingest-upload-storage");

interface Tab {
  name: string;
  uploads: UploadsModule;
  storage: StorageModule;
  queue: ReturnType<UploadsModule["useRecipeIngestUploads"]>;
}

const tabs: Tab[] = [];
let browser: IDBFactory;

/** A tab of the browser, signed in as `userId`: its own modules, the browser's IndexedDB */
async function openTab(name: string, userId = "u1"): Promise<Tab> {
  vi.resetModules();
  const storage = await import("../use-recipe-ingest-upload-storage");
  const uploads = await import("../use-recipe-ingest-uploads");
  const queue = uploads.useRecipeIngestUploads();
  const tab = { name, uploads, storage, queue };
  tabs.push(tab);
  const previous = sendingTab;
  sendingTab = name;
  await queue.connect(userId, id => storage.indexedDbUploadStorage(storage.uploadStorageName(id), browser));
  sendingTab = previous || name;
  await settle();
  return tab;
}

/** Runs `act` as `tab`: the requests it starts are that tab's */
async function as<T>(tab: Tab, act: () => T | Promise<T>): Promise<T> {
  sendingTab = tab.name;
  const result = await act();
  await settle();
  return result;
}

async function databases() {
  return (await browser.databases()).map(db => db.name);
}

beforeEach(() => {
  vi.clearAllMocks();
  browser = new IDBFactory();
  sent.length = 0;
  sendingTab = "";
  api.createBatch.mockResolvedValue({ data: { id: "server-batch", source: "app" }, error: null });
  api.sealBatch.mockImplementation(async (id: string) => ({ data: { id, source: "app" }, error: null }));
  api.touchBatch.mockImplementation(async (id: string) => ({ data: { id, source: "app" }, error: null }));
  api.getCounts.mockResolvedValue({ data: null, error: null });
  // offline: no upload answers, unless a test says otherwise
  api.upload.mockImplementation(async (photos: Blob[]) => {
    sent.push({ tab: sendingTab, photo: (await photos[0]!.text()).slice(JPEG_HEAD.length) });
    return new Promise(() => {});
  });
});

afterEach(() => {
  tabs.forEach(tab => tab.uploads.resetRecipeIngestUploads());
  tabs.length = 0;
  vi.unstubAllGlobals();
});

describe.each([
  ["Web Locks", () => vi.stubGlobal("navigator", { locks: fakeLocks() })],
  // a server reached over plain http has no Web Locks
  ["a BroadcastChannel, without Web Locks", () => vi.stubGlobal("navigator", {})],
])("two tabs of one user, agreeing over %s", (_name, setUp) => {
  beforeEach(() => setUp());

  test("the second tab neither sends nor shows the first one's queue, and counts its photos for a logout", async () => {
    const a = await openTab("A");
    await as(a, async () => {
      a.queue.takePhoto(photo("card one"));
      await a.queue.addPhotos([photo("tray")]);
    });
    expect(sent).toEqual([{ tab: "A", photo: "card one" }]);

    // the user opens a recipe in a new tab: its layout connects the queue too
    const b = await openTab("B");
    expect(sent).toEqual([{ tab: "A", photo: "card one" }]);
    expect(b.queue.queueElsewhere.value).toBe(true);
    expect(b.queue.cards.value).toEqual([]);
    expect(b.queue.drafts.value).toEqual([]);
    // so closing it doesn't ask about photos it doesn't have
    expect(b.queue.hasPending.value).toBe(false);
    // but its logout drops the first tab's photos, and asks first
    expect(b.queue.photosNotUploaded.value).toBe(2);
    expect(a.queue.queueElsewhere.value).toBe(false);
    expect(a.queue.drafts.value).toHaveLength(1);
  });

  test("when the tab keeping the queue closes, the other takes it over and sends what wasn't sent, once", async () => {
    const a = await openTab("A");
    await as(a, () => a.queue.takePhoto(photo("card one")));
    const b = await openTab("B");

    // A closes: the browser lets go of what it held
    await as(b, () => a.uploads.resetRecipeIngestUploads());
    await settle(100);
    expect(b.queue.queueElsewhere.value).toBe(false);
    expect(sent).toEqual([{ tab: "A", photo: "card one" }, { tab: "B", photo: "card one" }]);
    expect(b.queue.cards.value.map(card => card.status)).toEqual(["uploading"]);
  });

  test("Use this tab: the tab keeping the queue writes what it has and hands it over", async () => {
    const a = await openTab("A");
    await as(a, async () => {
      a.queue.mode.value = "front-and-back";
      await a.queue.addPhotos([photo("tray front"), photo("tray back")]);
      a.queue.takePhoto(photo("waiting front"));
    });
    const b = await openTab("B");
    expect(b.queue.queueElsewhere.value).toBe(true);

    await as(b, () => b.queue.takeOverQueue());
    await settle(100);
    expect(b.queue.queueElsewhere.value).toBe(false);
    expect(a.queue.queueElsewhere.value).toBe(true);
    expect(a.queue.drafts.value).toEqual([]);
    expect(a.queue.pendingFront.value).toBeNull();
    expect(await Promise.all(b.queue.drafts.value[0]!.photos.map(p => p.text()))).toEqual([
      expect.stringContaining("tray front"),
      expect.stringContaining("tray back"),
    ]);
    expect(await b.queue.pendingFront.value!.text()).toContain("waiting front");
    // and A's count is B's now
    expect(a.queue.photosNotUploaded.value).toBe(3);
    expect(b.queue.photosNotUploaded.value).toBe(3);
  });

  test("an idle queue goes to the tab whose cards page opens, without asking", async () => {
    const a = await openTab("A");
    const b = await openTab("B");
    expect(b.queue.queueElsewhere.value).toBe(true);

    const close = await as(b, () => b.queue.openCardsPage());
    await settle(100);
    expect(b.queue.queueElsewhere.value).toBe(false);
    expect(a.queue.queueElsewhere.value).toBe(true);
    close();
  });

  test("a queue with photos on their way stays where it is when another cards page opens", async () => {
    const a = await openTab("A");
    await as(a, () => a.queue.takePhoto(photo("card one")));
    const b = await openTab("B");

    const close = await as(b, () => b.queue.openCardsPage());
    await settle(100);
    expect(b.queue.queueElsewhere.value).toBe(true);
    expect(a.queue.queueElsewhere.value).toBe(false);
    close();
  });

  test("a logout in the other tab: the tab keeping the queue seals its batch and stops before the queue is deleted", async () => {
    const a = await openTab("A");
    await as(a, () => a.queue.takePhoto(photo("card one")));
    const b = await openTab("B");
    expect(b.uploads.recipeIngestPhotosNotUploaded.value).toBe(1);

    // B's header: Log out (after asking about A's photo), then the session goes
    await as(b, () => b.uploads.prepareRecipeIngestLogout(1000));
    expect(api.sealBatch).toHaveBeenCalledWith("server-batch", { suppressAlert: true });
    expect(await databases()).toEqual([]);

    // A, still signed in for a moment: what it does next isn't kept
    await as(a, async () => {
      a.queue.takePhoto(photo("late"));
      await a.queue.addPhotos([photo("late tray")]);
    });
    expect(await databases()).toEqual([]);
  });
});

/** A BroadcastChannel of a tab frozen in the background: nothing it says goes out, nothing said reaches it */
class FrozenChannel {
  onmessage: ((event: MessageEvent) => void) | null = null;
  constructor(public name: string) {}
  postMessage() {}
  close() {}
}

describe("a tab that lost the queue without noticing (frozen in the background while another took it)", () => {
  test("Use this tab takes it after a moment; the frozen tab neither sends nor writes once it wakes", async () => {
    const locks = fakeLocks();
    vi.stubGlobal("navigator", { locks });
    vi.stubGlobal("BroadcastChannel", FrozenChannel);
    const a = await openTab("A");
    await as(a, () => a.queue.takePhoto(photo("card one")));
    vi.unstubAllGlobals();
    vi.stubGlobal("navigator", { locks });

    const b = await openTab("B");
    expect(b.queue.queueElsewhere.value).toBe(true);
    locks.freeze();
    await as(b, () => b.queue.takeOverQueue());
    await settle();
    expect(b.queue.queueElsewhere.value).toBe(false);
    expect(sent).toEqual([{ tab: "A", photo: "card one" }, { tab: "B", photo: "card one" }]);
    const storage = b.storage.indexedDbUploadStorage(b.storage.uploadStorageName("u1"), browser);
    const before = await storage.load();

    // A runs again before it's told: a photo, a retry, an answer... nothing goes out, nothing is written
    await as(a, async () => {
      a.queue.takePhoto(photo("card two"));
      await a.queue.addPhotos([photo("tray")]);
    });
    const after = await storage.load();
    expect([...after.records.keys()].sort()).toEqual([...before.records.keys()].sort());
    expect(after.photos.size).toBe(before.photos.size);
    expect(sent.map(request => request.photo)).toEqual(["card one", "card one"]);
    expect(a.queue.queueElsewhere.value).toBe(true);

    locks.wake();
    await settle();
    expect(a.queue.queueElsewhere.value).toBe(true);
    expect(b.queue.queueElsewhere.value).toBe(false);
  });
});
