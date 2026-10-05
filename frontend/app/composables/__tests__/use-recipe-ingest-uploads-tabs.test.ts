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
/** While `reading` is set, a chosen file is read only once it settles (a big PDF takes a moment) */
const fileReads = vi.hoisted(() => ({ reading: null as Promise<void> | null }));
vi.mock("~/composables/use-recipe-ingest-files", async (importOriginal) => {
  const real = await importOriginal<typeof import("~/composables/use-recipe-ingest-files")>();
  return {
    ...real,
    inspectScanFile: async (file: Blob) => {
      await fileReads.reading;
      return real.inspectScanFile(file);
    },
  };
});

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
/** The same, with every photo of the card and its "Keep these cards on this server" */
const requests: { tab: string; photos: string[]; localOnly: boolean | undefined }[] = [];
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
type UploadStorage = import("../use-recipe-ingest-upload-storage").UploadStorage;

interface Tab {
  name: string;
  uploads: UploadsModule;
  storage: StorageModule;
  queue: ReturnType<UploadsModule["useRecipeIngestUploads"]>;
}

const tabs: Tab[] = [];
let browser: IDBFactory;

/** A tab of the browser, not signed in yet: its own modules (and its own page, when `page` is given) */
async function loadTab(name: string, page?: FakePage): Promise<Tab> {
  vi.resetModules();
  if (page) {
    // the module keeps the page it's loaded in
    vi.stubGlobal("document", page);
  }
  const storage = await import("../use-recipe-ingest-upload-storage");
  const uploads = await import("../use-recipe-ingest-uploads");
  if (page) {
    vi.stubGlobal("document", undefined);
  }
  const queue = uploads.useRecipeIngestUploads();
  const tab = { name, uploads, storage, queue };
  tabs.push(tab);
  return tab;
}

/** Signs the tab in as `userId`, with the browser's IndexedDB (or `open`'s storage) */
async function connectTab(tab: Tab, userId = "u1", open?: (id: string) => UploadStorage | null) {
  const previous = sendingTab;
  sendingTab = tab.name;
  await tab.queue.connect(userId, open ?? (id => tab.storage.indexedDbUploadStorage(tab.storage.uploadStorageName(id), browser)));
  sendingTab = previous || tab.name;
}

/** A tab of the browser, signed in as `userId`: its own modules, the browser's IndexedDB */
async function openTab(name: string, userId = "u1", page?: FakePage): Promise<Tab> {
  const tab = await loadTab(name, page);
  await connectTab(tab, userId);
  await settle();
  return tab;
}

/** A tab's page, shown or in the background */
class FakePage extends EventTarget {
  visibilityState: "visible" | "hidden" = "visible";

  show(visible: boolean) {
    this.visibilityState = visible ? "visible" : "hidden";
    this.dispatchEvent(new Event("visibilitychange"));
  }
}

/** The browser's localStorage, shared by its tabs (Node has none) */
function sharedLocalStorage() {
  const store = new Map<string, string>();
  const storage = {
    store,
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key),
  };
  vi.stubGlobal("localStorage", storage);
  return storage;
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

/** A promise that settles when `open` is called */
function gate() {
  let open = () => {};
  const opened = new Promise<void>((resolve) => {
    open = resolve;
  });
  return { opened, open };
}

/** A tab's storage whose writes wait, one by one, for `next()` while `on` (a slow IndexedDB) */
function gatedSaves() {
  const waiting: (() => void)[] = [];
  const saves = {
    on: false,
    waiting,
    /** Lets the oldest write waiting go */
    next() {
      waiting.shift()?.();
    },
    /** Lets every write go, the ones waiting and the ones to come, and waits for them */
    async all() {
      saves.on = false;
      while (waiting.length) {
        saves.next();
        await settle();
      }
    },
    wrap(real: UploadStorage): UploadStorage {
      return {
        ...real,
        save: (change, tab) => (saves.on
          ? new Promise<void>(resolve => waiting.push(resolve)).then(() => real.save(change, tab))
          : real.save(change, tab)),
      };
    },
  };
  return saves;
}

/** The text of each photo of the tab's cards, by card */
async function cardTexts(tab: Tab) {
  return Promise.all(tab.queue.cards.value.map(async card =>
    (await Promise.all(card.photos.map(async p => (await p.text()).slice(JPEG_HEAD.length)))).join("+")));
}

/** Uploads that answer at once, each a job */
function answeringUploads() {
  let jobs = 0;
  api.upload.mockImplementation(async (photos: Blob[], options: { localOnly?: boolean; batchId?: string }) => {
    const tab = sendingTab;
    const texts = await Promise.all(photos.map(async p => (await p.text()).slice(JPEG_HEAD.length)));
    sent.push({ tab, photo: texts[0]! });
    requests.push({ tab, photos: texts, localOnly: options?.localOnly });
    jobs += 1;
    return {
      data: { jobs: [{ id: `job-${jobs}`, position: 0, status: "processing" }], rejected: [], batchId: options.batchId ?? "server-batch" },
      error: null,
    };
  });
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
  requests.length = 0;
  api.upload.mockImplementation(async (photos: Blob[], options: { localOnly?: boolean }) => {
    const tab = sendingTab;
    const texts = await Promise.all(photos.map(async p => (await p.text()).slice(JPEG_HEAD.length)));
    sent.push({ tab, photo: texts[0]! });
    requests.push({ tab, photos: texts, localOnly: options?.localOnly });
    return new Promise(() => {});
  });
});

afterEach(() => {
  fileReads.reading = null;
  tabs.forEach(tab => tab.uploads.resetRecipeIngestUploads());
  tabs.length = 0;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  SuspendableChannel.suspended = false;
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
      await a.queue.addPhotos([photo("tray front"), photo("tray back"), photo("tray alone")]);
    });
    const b = await openTab("B");
    expect(b.queue.queueElsewhere.value).toBe(true);

    await as(b, () => b.queue.takeOverQueue());
    await settle(100);
    expect(b.queue.queueElsewhere.value).toBe(false);
    expect(a.queue.queueElsewhere.value).toBe(true);
    expect(a.queue.drafts.value).toEqual([]);
    expect(await Promise.all(b.queue.drafts.value.map(async draft => Promise.all(draft.photos.map(p => p.text()))))).toEqual([
      [expect.stringContaining("tray front"), expect.stringContaining("tray back")],
      [expect.stringContaining("tray alone")],
    ]);
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

  test("the tab keeping the queue logs out: its stored queue is deleted, and nothing creates it again", async () => {
    const a = await openTab("A");
    await as(a, () => a.queue.takePhoto(photo("card one")));
    const b = await openTab("B");
    expect(await databases()).toEqual(["mealie-recipe-cards-u1"]);

    // A's header: Log out, then the session goes (every tab forgets the queue)
    await as(a, () => a.uploads.prepareRecipeIngestLogout(1000));
    await as(a, () => {
      a.uploads.resetRecipeIngestUploads();
      b.uploads.resetRecipeIngestUploads();
    });
    // longer than a lease is renewed
    await new Promise(resolve => setTimeout(resolve, a.storage.QUEUE_RENEW_MS + 500));
    expect(await databases()).toEqual([]);
  }, 10_000);

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

/** The browser's own BroadcastChannel, kept before a test stubs it */
const RealBroadcastChannel = globalThis.BroadcastChannel;

/**
 * The BroadcastChannel of a tab that's suspended in the background while `suspended` is set (iOS): nothing it says goes
 * out, nothing said reaches it; once it runs again, it hears and is heard
 */
class SuspendableChannel {
  static suspended = false;
  onmessage: ((event: MessageEvent) => void) | null = null;
  private readonly channel: BroadcastChannel;

  constructor(name: string) {
    this.channel = new RealBroadcastChannel(name);
    this.channel.onmessage = (event: MessageEvent) => {
      if (!SuspendableChannel.suspended) {
        this.onmessage?.(event);
      }
    };
  }

  postMessage(message: unknown) {
    if (!SuspendableChannel.suspended) {
      this.channel.postMessage(message);
    }
  }

  close() {
    this.channel.close();
  }
}

/** A BroadcastChannel whose messages take a second to arrive (a busy browser, a tab in the background) */
class SlowChannel {
  onmessage: ((event: MessageEvent) => void) | null = null;
  private readonly channel: BroadcastChannel;

  constructor(name: string) {
    this.channel = new RealBroadcastChannel(name);
    this.channel.onmessage = (event: MessageEvent) => this.onmessage?.(event);
  }

  postMessage(message: unknown) {
    setTimeout(() => {
      try {
        this.channel.postMessage(message);
      }
      catch {
        // closed meanwhile
      }
    }, 1000);
  }

  close() {
    setTimeout(() => this.channel.close(), 1100);
  }
}

/** A BroadcastChannel of a tab frozen in the background: nothing it says goes out, nothing said reaches it */
class FrozenChannel {
  onmessage: ((event: MessageEvent) => void) | null = null;
  constructor(public name: string) {}
  postMessage() {}
  close() {}
}

describe("a tab that lost the queue without noticing (frozen in the background while another took it)", () => {
  test("Use this tab takes it after a moment; once the frozen tab wakes, it writes nothing and sends only what it was given", async () => {
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

    // A runs again before it's told: a photo and a chosen file. Nothing is written; B never had them, so A sends them
    // (once each), and nothing B sends
    await as(a, async () => {
      a.queue.takePhoto(photo("card two"));
      await a.queue.addPhotos([photo("tray")]);
    });
    const after = await storage.load();
    expect([...after.records.keys()].sort()).toEqual([...before.records.keys()].sort());
    expect(after.photos.size).toBe(before.photos.size);
    expect(sent.slice(0, 2).map(request => request.photo)).toEqual(["card one", "card one"]);
    expect(sent.slice(2).map(request => `${request.tab}:${request.photo}`).sort()).toEqual(["A:card two", "A:tray"]);
    expect(a.queue.queueElsewhere.value).toBe(true);
    expect(a.queue.uploadingLeftovers.value).toBe(true);

    locks.wake();
    await settle();
    expect(a.queue.queueElsewhere.value).toBe(true);
    expect(b.queue.queueElsewhere.value).toBe(false);
  });
});

describe.each([
  ["Web Locks", () => vi.stubGlobal("navigator", { locks: fakeLocks() })],
  ["a lease in IndexedDB, without Web Locks", () => vi.stubGlobal("navigator", {})],
])("the queue moving to another tab, agreeing over %s", (_name, setUp) => {
  beforeEach(() => setUp());

  test("two tabs taking the queue at the same moment: one sends the stored card, once", async () => {
    const x = await openTab("X");
    await as(x, () => x.queue.takePhoto(photo("card one")));
    // X closes before the card got anywhere
    await as(x, () => x.uploads.resetRecipeIngestUploads());
    sent.length = 0;

    const a = await loadTab("A");
    const b = await loadTab("B");
    sendingTab = "A or B";
    await Promise.all([connectTab(a), connectTab(b)]);
    await settle(100);
    expect([a.queue.queueElsewhere.value, b.queue.queueElsewhere.value].sort()).toEqual([false, true]);
    expect(sent.map(request => request.photo)).toEqual(["card one"]);
  });

  test("a card goes with the \"Keep these cards on this server\" its tab had, when the tab closes before it went", async () => {
    const remembered = sharedLocalStorage();
    const a = await openTab("A");
    // opened before the switch changed
    const b = await openTab("B");
    await as(a, () => {
      a.queue.localOnly.value = true;
    });
    expect(remembered.store.get("mealie.recipe-ingest.local-only.u1")).toBe("true");
    await as(a, () => a.queue.takePhoto(photo("private card")));

    await as(b, () => a.uploads.resetRecipeIngestUploads());
    await settle(100);
    expect(requests).toEqual([
      { tab: "A", photos: ["private card"], localOnly: true },
      { tab: "B", photos: ["private card"], localOnly: true },
    ]);
    // and B's switch says so too
    expect(b.queue.localOnly.value).toBe(true);
  });

  test("Use this tab: the cards go with what the other tab chose, even where this tab can't read the remembered choice", async () => {
    sharedLocalStorage();
    const a = await openTab("A");
    const b = await openTab("B");
    api.createBatch.mockImplementation(() => new Promise(() => {})); // the card never goes from A
    await as(a, () => {
      a.queue.localOnly.value = true;
      a.queue.takePhoto(photo("private card"));
    });
    // B can't read this browser's storage (a private window), so its switch stays as it was
    vi.stubGlobal("localStorage", {
      getItem: () => {
        throw new DOMException("denied", "SecurityError");
      },
    });
    api.createBatch.mockResolvedValue({ data: { id: "server-batch", source: "app" }, error: null });

    await as(b, () => b.queue.takeOverQueue());
    await settle(100);
    expect(b.queue.queueElsewhere.value).toBe(false);
    expect(requests).toEqual([{ tab: "B", photos: ["private card"], localOnly: true }]);
  });

  test("photos given to a tab after its queue went to another tab go from that tab, and the other tab counts them", async () => {
    const a = await openTab("A");
    await as(a, () => {
      a.queue.mode.value = "front-and-back";
      a.queue.takePhoto(photo("card front"));
      a.queue.takePhoto(photo("card back"));
    });
    const b = await openTab("B");
    await as(b, () => b.queue.takeOverQueue());
    await settle(100);
    expect(a.queue.queueElsewhere.value).toBe(true);
    expect(a.queue.uploadingLeftovers.value).toBe(false);
    requests.length = 0;

    // A's camera and file picker were still open as the queue went: what they give A now goes from A, as cards (a
    // front doesn't wait for a back that can't come, chosen files pair as in the tray)
    await as(a, async () => {
      a.queue.takePhoto(photo("late photo"));
      await a.queue.addPhotos([photo("late front"), photo("late back")]);
    });
    await settle(50);
    expect(requests.map(request => `${request.tab}:${request.photos.join("+")}`).sort()).toEqual([
      "A:late front+late back",
      "A:late photo",
    ]);
    expect(a.queue.cards.value.map(card => card.status)).toEqual(["uploading", "uploading"]);
    expect(a.queue.uploadingLeftovers.value).toBe(true);
    expect(b.queue.cards.value).toHaveLength(1);
    // B's logout asks about them: its card's two photos and A's three
    expect(b.uploads.recipeIngestPhotosNotUploaded.value).toBe(5);
  });

  test("a card taken over goes with the switch when it changes here while the card waits for its batch", async () => {
    sharedLocalStorage();
    const a = await openTab("A");
    api.createBatch.mockImplementation(() => new Promise(() => {})); // the card never goes from A
    await as(a, () => a.queue.takePhoto(photo("card one")));
    const b = await openTab("B");
    let create!: (answer: unknown) => void;
    api.createBatch.mockImplementation(() => new Promise((resolve) => {
      create = resolve;
    }));

    await as(b, () => b.queue.takeOverQueue());
    await settle(100);
    expect(b.queue.queueElsewhere.value).toBe(false);
    expect(b.queue.cards.value.map(card => [card.status, card.localOnlyChoice])).toEqual([["uploading", false]]);
    // the user turns "Keep these cards on this server" on in B before the card's batch is there
    await as(b, () => {
      b.queue.localOnly.value = true;
    });
    await as(b, () => create({ data: { id: "server-batch", source: "app" }, error: null }));
    expect(requests).toEqual([{ tab: "B", photos: ["card one"], localOnly: true }]);
  });

  test("a front waiting for its back keeps the queue in its tab: Use this tab waits for the back, then takes the queue", async () => {
    sharedLocalStorage();
    const a = await openTab("A");
    const b = await openTab("B");
    expect(b.queue.mode.value).toBe("one-side");
    await as(a, () => {
      a.queue.mode.value = "front-and-back";
      a.queue.takePhoto(photo("card front"));
    });

    // the user clicks Use this tab in B while A's camera is still open for the back
    const closeB = await as(b, () => b.queue.openCardsPage());
    const started = Date.now();
    await as(b, () => b.queue.takeOverQueue());
    await settle(100);
    // answered at once, rather than taken after QUEUE_HAND_OVER_MS
    expect(Date.now() - started).toBeLessThan(a.uploads.QUEUE_HAND_OVER_MS);
    expect(b.queue.queueElsewhere.value).toBe(true);
    expect(b.queue.queueFrontWaitingElsewhere.value).toBe(true);
    expect(a.queue.queueElsewhere.value).toBe(false);
    expect(await a.queue.pendingFront.value!.text()).toContain("card front");

    // A's camera gives it the back: the card goes from A, and then the queue goes to B, as asked
    await as(a, () => a.queue.takePhoto(photo("card back")));
    await settle(100);
    expect(b.queue.queueElsewhere.value).toBe(false);
    expect(b.queue.queueFrontWaitingElsewhere.value).toBe(false);
    expect(a.queue.queueElsewhere.value).toBe(true);
    // B follows A's mode, and its next photo is a front of its own
    expect(b.queue.mode.value).toBe("front-and-back");
    expect(b.queue.pendingFront.value).toBeNull();
    await as(b, () => b.queue.takePhoto(photo("next front")));
    expect(await b.queue.pendingFront.value!.text()).toContain("next front");
    // the card went whole, from A (and again from B, which read it back as A's upload was on its way): never a side
    // alone, and never with a photo of the other tab
    expect(new Set(requests.map(request => request.photos.join("+")))).toEqual(new Set(["card front+card back"]));
    closeB();
  });

  test("a front waiting for its back keeps the queue in its tab until No back is tapped there", async () => {
    sharedLocalStorage();
    const a = await openTab("A");
    const b = await openTab("B");
    await as(a, () => {
      a.queue.mode.value = "front-and-back";
      a.queue.takePhoto(photo("lone front"));
    });
    const closeB = await as(b, () => b.queue.openCardsPage());
    await as(b, () => b.queue.takeOverQueue());
    await settle(100);
    expect(b.queue.queueFrontWaitingElsewhere.value).toBe(true);

    await as(a, () => a.queue.noBack());
    await settle(100);
    expect(b.queue.queueElsewhere.value).toBe(false);
    expect(new Set(requests.map(request => request.photos.join("+")))).toEqual(new Set(["lone front"]));
    closeB();
  });

  test("Use this tab put off while a front waits is let go with the cards page it was asked on", async () => {
    sharedLocalStorage();
    const a = await openTab("A");
    const b = await openTab("B");
    await as(a, () => {
      a.queue.mode.value = "front-and-back";
      a.queue.takePhoto(photo("card front"));
    });
    const closeB = await as(b, () => b.queue.openCardsPage());
    await as(b, () => b.queue.takeOverQueue());
    await settle(100);
    expect(b.queue.queueFrontWaitingElsewhere.value).toBe(true);
    closeB();
    expect(b.queue.queueFrontWaitingElsewhere.value).toBe(false);

    // the back comes later: the queue stays with A, where the user is
    await as(a, () => a.queue.takePhoto(photo("card back")));
    await settle(100);
    expect(a.queue.queueElsewhere.value).toBe(false);
    expect(b.queue.queueElsewhere.value).toBe(true);
  });

  test("a front taken from a tab that didn't hand it over waits in the tray, and its back, given to that tab, goes alone", async () => {
    sharedLocalStorage();
    // A hears nothing and says nothing (frozen, or too busy to answer Use this tab in time)
    vi.stubGlobal("BroadcastChannel", FrozenChannel);
    const a = await openTab("A");
    await as(a, () => {
      a.queue.mode.value = "front-and-back";
      a.queue.takePhoto(photo("card front"));
    });
    vi.stubGlobal("BroadcastChannel", RealBroadcastChannel);
    const b = await openTab("B");
    expect(b.queue.queueElsewhere.value).toBe(true);

    // B takes the queue after QUEUE_HAND_OVER_MS (over Web Locks it's stolen; over the lease it's taken over)
    await as(b, () => b.queue.takeOverQueue());
    await settle(100);
    expect(b.queue.queueElsewhere.value).toBe(false);
    // A finds out (at once over Web Locks, at its next renewal over the lease)
    await new Promise(resolve => setTimeout(resolve, b.storage.QUEUE_RENEW_MS + 500));
    await settle();
    expect(a.queue.queueElsewhere.value).toBe(true);

    // the front is in B's tray, a card of its own, not waiting for a photo B takes
    expect(b.queue.pendingFront.value).toBeNull();
    expect(await Promise.all(b.queue.drafts.value.map(async draft => Promise.all(draft.photos.map(p => p.text()))))).toEqual([
      [expect.stringContaining("card front")],
    ]);
    // A's camera gives it the back: it goes alone, from A
    await as(a, () => a.queue.takePhoto(photo("card back")));
    // B's next photo is a front of its own, and the tray's front isn't joined to a file chosen in B
    await as(b, async () => {
      b.queue.takePhoto(photo("next front"));
      await b.queue.addPhotos([photo("chosen")]);
    });
    await settle(50);
    expect(requests.map(request => `${request.tab}:${request.photos.join("+")}`)).toEqual(["A:card back"]);
    expect(await b.queue.pendingFront.value!.text()).toContain("next front");
    expect(await Promise.all(b.queue.drafts.value.map(async draft => Promise.all(draft.photos.map(p => p.text()))))).toEqual([
      [expect.stringContaining("card front")],
      [expect.stringContaining("chosen")],
    ]);
  }, 20_000);

  test("losing the queue while the capture page is shown doesn't keep the batch open once the queue is back", async () => {
    const a = await openTab("A");
    const releaseCapture = await as(a, () => a.queue.keepBatchOpen());
    await as(a, () => a.queue.takePhoto(photo("card one")));
    expect(a.queue.openBatch.value?.serverId).toBe("server-batch");
    const b = await openTab("B");
    await as(b, () => b.queue.takeOverQueue());
    await settle(100);
    expect(a.queue.queueElsewhere.value).toBe(true);
    // A's capture page closes after the queue left (the cards page shows "Use this tab" instead)
    releaseCapture();
    api.touchBatch.mockClear();

    // B closes: A keeps the queue again, with no capture page shown
    await as(a, () => b.uploads.resetRecipeIngestUploads());
    await settle(100);
    expect(a.queue.queueElsewhere.value).toBe(false);
    expect(a.queue.openBatch.value?.serverId).toBe("server-batch");
    expect(api.touchBatch).not.toHaveBeenCalled();
  });

  test("a cards page in a background tab doesn't ask for the queue; shown again, it does", async () => {
    const wants: string[] = [];
    const listener = new BroadcastChannel("mealie-recipe-cards-u1-queue");
    listener.onmessage = (event: MessageEvent<{ type: string }>) => {
      if (event.data?.type === "want") {
        wants.push(event.data.type);
      }
    };
    const pageA = new FakePage();
    const pageB = new FakePage();
    const a = await openTab("A", "u1", pageA);
    const b = await openTab("B", "u1", pageB);
    // B's cards page opens in a tab in the background (the user stays in A)
    pageB.show(false);
    const closeB = await as(b, () => b.queue.openCardsPage());
    await settle(50);
    expect(wants).toEqual([]);
    expect(b.queue.queueElsewhere.value).toBe(true);

    // the user opens A's cards page, then goes to B: B's cards page, shown, asks for the queue, and A, whose cards page
    // is in the background now, lets its idle queue go
    const closeA = await as(a, () => a.queue.openCardsPage());
    await as(a, () => pageA.show(false));
    pageB.show(true);
    await settle(100);
    expect(wants).toEqual(["want"]);
    expect(b.queue.queueElsewhere.value).toBe(false);
    expect(a.queue.queueElsewhere.value).toBe(true);
    closeA();
    closeB();
    listener.close();
  });

  test.each([
    ["Use this tab in the other tab", false],
    ["the other tab's cards page shown, this one's hidden", true],
  ])("files still being read as the queue goes to another tab (%s) go from this tab, once, and are counted", async (_how, byPage) => {
    const pageA = new FakePage();
    const pageB = new FakePage();
    const a = await openTab("A", "u1", pageA);
    const b = await openTab("B", "u1", pageB);
    // B's cards page is in a tab in the background, or in a window beside A's (Use this tab)
    pageB.show(!byPage);
    const closeA = await as(a, () => a.queue.openCardsPage());
    const closeB = await as(b, () => b.queue.openCardsPage());
    expect(a.queue.queueElsewhere.value).toBe(false);
    let read!: () => void;
    fileReads.reading = new Promise<void>((resolve) => {
      read = resolve;
    });
    // A: two files chosen, read for a moment (a big PDF is read whole)
    sendingTab = "A";
    const adding = a.queue.addPhotos([photo("file one"), photo("file two")]);
    await settle();
    // meanwhile the queue goes to B
    if (byPage) {
      await as(a, () => pageA.show(false));
      pageB.show(true);
    }
    else {
      await as(b, () => b.queue.takeOverQueue());
    }
    await settle(100);
    expect(a.queue.queueElsewhere.value).toBe(true);

    sendingTab = "A";
    read();
    expect(await adding).toEqual({ unsupported: [], tooManyPages: [] });
    await settle(50);
    expect(requests.map(request => `${request.tab}:${request.photos.join("+")}`).sort()).toEqual(["A:file one", "A:file two"]);
    expect(a.queue.uploadingLeftovers.value).toBe(true);
    expect(b.queue.drafts.value).toEqual([]);
    expect(b.queue.cards.value).toEqual([]);
    // B's logout asks about them
    expect(b.uploads.recipeIngestPhotosNotUploaded.value).toBe(2);
    closeA();
    closeB();
  });

  test("a photo that comes while the hand-over's last write is on its way is written too, and goes to the other tab", async () => {
    const saves = gatedSaves();
    const a = await loadTab("A");
    await connectTab(a, "u1", id => saves.wrap(a.storage.indexedDbUploadStorage(a.storage.uploadStorageName(id), browser)));
    await settle();
    await as(a, () => a.queue.takePhoto(photo("card one")));
    const b = await openTab("B");

    // card two's write is slow; Use this tab in B meanwhile: A writes what changed last first
    saves.on = true;
    await as(a, () => a.queue.takePhoto(photo("card two")));
    sendingTab = "B";
    const taking = b.queue.takeOverQueue();
    await settle();
    saves.next();
    await settle();
    // the hand-over's own write is on its way when A's camera gives it card three
    expect(saves.waiting.length).toBeGreaterThan(0);
    expect(a.queue.queueElsewhere.value).toBe(false);
    await as(a, () => a.queue.takePhoto(photo("card three")));
    sendingTab = "B";
    await saves.all();
    await taking;
    await settle(100);

    expect(a.queue.queueElsewhere.value).toBe(true);
    expect(b.queue.queueElsewhere.value).toBe(false);
    expect(a.queue.cards.value).toEqual([]);
    expect(a.queue.uploadingLeftovers.value).toBe(false);
    expect(await cardTexts(b)).toEqual(["card one", "card two", "card three"]);
    // two at a time
    expect(b.queue.cards.value.map(card => card.status)).toEqual(["uploading", "uploading", "waiting"]);
    expect(b.uploads.recipeIngestPhotosNotUploaded.value).toBe(3);
  });

  test("a file read while the hand-over's last write is on its way is written too, and is in the other tab's tray", async () => {
    const saves = gatedSaves();
    const a = await loadTab("A");
    await connectTab(a, "u1", id => saves.wrap(a.storage.indexedDbUploadStorage(a.storage.uploadStorageName(id), browser)));
    await settle();
    await as(a, () => a.queue.takePhoto(photo("card one")));
    const b = await openTab("B");
    let read!: () => void;
    fileReads.reading = new Promise<void>((resolve) => {
      read = resolve;
    });
    sendingTab = "A";
    const adding = a.queue.addPhotos([photo("file one")]);

    saves.on = true;
    await as(a, () => a.queue.takePhoto(photo("card two")));
    sendingTab = "B";
    const taking = b.queue.takeOverQueue();
    await settle();
    saves.next();
    await settle();
    // the hand-over's own write is on its way when the file has been read
    expect(saves.waiting.length).toBeGreaterThan(0);
    read();
    expect(await adding).toEqual({ unsupported: [], tooManyPages: [] });
    await settle();
    await saves.all();
    await taking;
    await settle(100);

    expect(b.queue.queueElsewhere.value).toBe(false);
    expect(a.queue.uploadingLeftovers.value).toBe(false);
    expect(await Promise.all(b.queue.drafts.value.map(async draft => Promise.all(draft.photos.map(p => p.text()))))).toEqual([
      [expect.stringContaining("file one")],
    ]);
    expect(b.uploads.recipeIngestPhotosNotUploaded.value).toBe(3);
  });

  test("photos that keep coming while the hand-over writes: what its last write didn't take goes from this tab", async () => {
    const saves = gatedSaves();
    const a = await loadTab("A");
    await connectTab(a, "u1", id => saves.wrap(a.storage.indexedDbUploadStorage(a.storage.uploadStorageName(id), browser)));
    await settle();
    const b = await openTab("B");

    saves.on = true;
    const taken = ["card 0"];
    await as(a, () => a.queue.takePhoto(photo("card 0")));
    sendingTab = "B";
    const taking = b.queue.takeOverQueue();
    await settle();
    saves.next();
    await settle();
    // a photo while each of the hand-over's writes is on its way
    for (let write = 0; write < a.uploads.HAND_OVER_WRITES; write++) {
      expect(saves.waiting).toHaveLength(1);
      const card = `card ${write + 1}`;
      taken.push(card);
      await as(a, () => a.queue.takePhoto(photo(card)));
      sendingTab = "B";
      saves.next();
      await settle();
    }
    await saves.all();
    await taking;
    await settle(100);

    expect(a.queue.queueElsewhere.value).toBe(true);
    expect(b.queue.queueElsewhere.value).toBe(false);
    // every card is in one tab or the other, never in neither nor in both: the last one, never written, goes from A
    const inA = await cardTexts(a);
    const inB = await cardTexts(b);
    expect(inA).toEqual([taken.at(-1)]);
    expect([...inA, ...inB].sort()).toEqual(taken);
    expect(a.queue.uploadingLeftovers.value).toBe(true);
    expect(b.uploads.recipeIngestPhotosNotUploaded.value).toBe(taken.length);
  });
});

describe.each([
  ["Web Locks", () => vi.stubGlobal("navigator", { locks: fakeLocks() })],
  ["a lease in IndexedDB, without Web Locks", () => vi.stubGlobal("navigator", {})],
])("a tab whose storage failed (full), agreeing over %s", (_name, setUp) => {
  beforeEach(() => setUp());

  /** Tab A, whose storage refuses photos once `full` is set */
  async function openFullTab(full: { on: boolean }) {
    const a = await loadTab("A");
    await connectTab(a, "u1", (id) => {
      const real = a.storage.indexedDbUploadStorage(a.storage.uploadStorageName(id), browser);
      return {
        ...real,
        save: (change, tab) => (full.on && change.putPhotos.size
          ? Promise.reject(new DOMException("The quota has been exceeded.", "QuotaExceededError"))
          : real.save(change, tab)),
      };
    });
    await settle();
    return a;
  }

  test("Use this tab: the tab keeping photos only in memory keeps the queue, and the other tab is told why", async () => {
    const full = { on: false };
    const a = await openFullTab(full);
    await as(a, () => a.queue.takePhoto(photo("card one")));
    full.on = true;
    await as(a, async () => {
      a.queue.takePhoto(photo("card two"));
      await a.queue.addPhotos([photo("tray")]);
    });
    expect(a.queue.storageFailed.value).toBe(true);

    const b = await openTab("B");
    const started = Date.now();
    await as(b, () => b.queue.takeOverQueue());
    await settle(100);
    // answered at once, rather than taken after QUEUE_HAND_OVER_MS
    expect(Date.now() - started).toBeLessThan(a.uploads.QUEUE_HAND_OVER_MS);
    expect(b.queue.queueElsewhere.value).toBe(true);
    expect(b.queue.queueKeptInMemoryElsewhere.value).toBe(true);
    expect(a.queue.queueElsewhere.value).toBe(false);
    expect(a.queue.cards.value).toHaveLength(2);
    expect(a.queue.drafts.value).toHaveLength(1);
    // and an idle queue isn't handed to B's cards page either
    const close = await as(b, () => b.queue.openCardsPage());
    await settle(100);
    expect(b.queue.queueElsewhere.value).toBe(true);
    close();
  });

  test("a failed card kept only in memory stays when another tab's cards page opens", async () => {
    const a = await openFullTab({ on: true });
    api.upload.mockResolvedValue({ data: null, error: { response: { status: 422, data: { detail: {} } } } });
    await as(a, () => {
      a.queue.takePhoto(photo("card one"));
      a.queue.done();
    });
    await settle(50);
    expect(a.queue.cards.value.map(card => [card.status, card.retryable])).toEqual([["failed", true]]);

    const b = await openTab("B");
    const close = await as(b, () => b.queue.openCardsPage());
    await settle(100);
    expect(b.queue.queueElsewhere.value).toBe(true);
    expect(a.queue.cards.value).toHaveLength(1);
    close();
  });
});

describe("a tab whose storage failed loses the queue while frozen in the background", () => {
  test("once it wakes, it sends the photos only it holds, and nothing the other tab sends", async () => {
    const locks = fakeLocks();
    vi.stubGlobal("navigator", { locks });
    vi.stubGlobal("BroadcastChannel", SuspendableChannel);
    const full = { on: false };
    const a = await loadTab("A");
    await connectTab(a, "u1", (id) => {
      const real = a.storage.indexedDbUploadStorage(a.storage.uploadStorageName(id), browser);
      return {
        ...real,
        save: (change, tab) => (full.on && change.putPhotos.size
          ? Promise.reject(new DOMException("The quota has been exceeded.", "QuotaExceededError"))
          : real.save(change, tab)),
      };
    });
    await settle();
    await as(a, () => a.queue.takePhoto(photo("card one")));
    full.on = true;
    await as(a, async () => {
      a.queue.takePhoto(photo("card two"));
      await a.queue.addPhotos([photo("tray")]);
    });
    vi.unstubAllGlobals();
    vi.stubGlobal("navigator", { locks });

    // A is frozen: it doesn't answer, so B takes the queue after a moment, and gets what the storage holds
    SuspendableChannel.suspended = true;
    const b = await openTab("B");
    locks.freeze();
    await as(b, () => b.queue.takeOverQueue());
    await settle();
    expect(b.queue.queueElsewhere.value).toBe(false);
    expect(sent.filter(request => request.tab === "B").map(request => request.photo)).toEqual(["card one"]);

    // A wakes: card two and the tray are only in A, so A sends them; card one is B's now
    sent.length = 0;
    SuspendableChannel.suspended = false;
    await as(a, () => locks.wake());
    await settle(50);
    expect(sent.map(request => request.photo).sort()).toEqual(["card two", "tray"]);
    expect(a.queue.queueElsewhere.value).toBe(true);
    expect(a.queue.uploadingLeftovers.value).toBe(true);
    // a logout in B counts them
    expect(b.uploads.recipeIngestPhotosNotUploaded.value).toBe(3);
  });
});

describe("the tab keeping the queue is suspended in the background (Web Locks)", () => {
  test("a logout in another tab counts its photos from the stored queue, so it asks first", async () => {
    const locks = fakeLocks();
    vi.stubGlobal("navigator", { locks });
    vi.stubGlobal("BroadcastChannel", SuspendableChannel);
    const a = await openTab("A");
    await as(a, async () => {
      a.queue.takePhoto(photo("card one"));
      await a.queue.addPhotos([photo("tray one"), photo("tray two")]);
    });
    vi.unstubAllGlobals();
    vi.stubGlobal("navigator", { locks });

    // iOS suspends A in the background before B opens
    SuspendableChannel.suspended = true;
    const b = await openTab("B");
    expect(b.queue.queueElsewhere.value).toBe(true);
    // A never answered: B's own count is 0
    expect(b.uploads.recipeIngestPhotosNotUploaded.value).toBe(0);
    expect(await b.uploads.countRecipeIngestPhotosNotUploaded(50)).toBe(3);
  });

  test("one that answers is counted as it says", async () => {
    vi.stubGlobal("navigator", { locks: fakeLocks() });
    const a = await openTab("A");
    await as(a, () => a.queue.takePhoto(photo("card one")));
    const b = await openTab("B");
    expect(await b.uploads.countRecipeIngestPhotosNotUploaded(50)).toBe(1);
  });
});

describe("without Web Locks, the tab keeping the queue frozen in the background", () => {
  test("its lease runs out, another tab takes the queue, and once the frozen tab wakes it sends only what it was given", async () => {
    vi.stubGlobal("navigator", {});
    vi.stubGlobal("BroadcastChannel", FrozenChannel);
    // A's timers don't run (it renews nothing) and it hears nothing
    const frozenInterval = vi.fn(() => 0 as unknown as ReturnType<typeof setInterval>);
    const a = await loadTab("A");
    vi.stubGlobal("setInterval", frozenInterval);
    await connectTab(a);
    vi.unstubAllGlobals();
    vi.stubGlobal("navigator", {});
    await settle();
    expect(frozenInterval).toHaveBeenCalled();
    await as(a, () => a.queue.takePhoto(photo("card one")));
    const b = await openTab("B");
    expect(b.queue.queueElsewhere.value).toBe(true);

    // A's lease runs out: B asks again within QUEUE_RENEW_MS, and takes the queue
    const realNow = Date.now.bind(Date);
    vi.spyOn(Date, "now").mockImplementation(() => realNow() + b.storage.QUEUE_LEASE_MS + 1000);
    sendingTab = "B";
    await new Promise(resolve => setTimeout(resolve, b.storage.QUEUE_RENEW_MS + 500));
    await settle();
    expect(b.queue.queueElsewhere.value).toBe(false);
    expect(sent).toEqual([{ tab: "A", photo: "card one" }, { tab: "B", photo: "card one" }]);

    // A runs again before it's told: a photo and a retry. Card one is B's now and doesn't go again; card two never
    // reached the storage (B doesn't have it), so it goes from A, once
    await as(a, async () => {
      a.queue.takePhoto(photo("card two"));
      a.queue.retry(a.queue.cards.value[0]?.key ?? "");
    });
    await settle(50);
    expect(sent).toEqual([{ tab: "A", photo: "card one" }, { tab: "B", photo: "card one" }, { tab: "A", photo: "card two" }]);
    expect(a.queue.queueElsewhere.value).toBe(true);
    expect(a.queue.uploadingLeftovers.value).toBe(true);
    expect(b.queue.queueElsewhere.value).toBe(false);
    vi.restoreAllMocks();
  }, 20_000);
});

describe("a tab that loses the queue while it's writing", () => {
  test("waits for the write: a card it wrote before the other tab read the queue goes from that tab only", async () => {
    vi.stubGlobal("navigator", { locks: fakeLocks() });
    const slowSave = { on: false, gate: gate() };
    const a = await loadTab("A");
    await connectTab(a, "u1", (id) => {
      const real = a.storage.indexedDbUploadStorage(a.storage.uploadStorageName(id), browser);
      return { ...real, save: (change, tab) => (slowSave.on ? slowSave.gate.opened.then(() => real.save(change, tab)) : real.save(change, tab)) };
    });
    await settle();
    await as(a, () => a.queue.takePhoto(photo("card one")));
    // card two's write is slow
    slowSave.on = true;
    await as(a, () => a.queue.takePhoto(photo("card two")));

    // B takes the queue (A, stuck writing, doesn't hand it over in time), but reads it only a moment later
    const claimGate = gate();
    const b = await loadTab("B");
    await connectTab(b, "u1", (id) => {
      const real = b.storage.indexedDbUploadStorage(b.storage.uploadStorageName(id), browser);
      return { ...real, claim: tab => claimGate.opened.then(() => real.claim(tab)) };
    });
    await as(b, () => b.queue.takeOverQueue());
    await settle();
    expect(a.queue.queueElsewhere.value).toBe(false);
    // (A sent both cards while it kept the queue; the tab keeping it next sends again what the storage holds)
    expect(sent).toEqual([{ tab: "A", photo: "card one" }, { tab: "A", photo: "card two" }]);

    // A's write goes before B reads the queue: card two is B's to send now, not A's as well
    await as(a, () => slowSave.gate.open());
    await settle();
    expect(a.queue.queueElsewhere.value).toBe(true);
    expect(a.queue.cards.value).toEqual([]);
    await as(b, () => claimGate.open());
    await settle(50);
    expect(sent.slice(2).map(request => `${request.tab}:${request.photo}`).sort()).toEqual(["B:card one", "B:card two"]);
    expect(a.queue.uploadingLeftovers.value).toBe(false);
  }, 10_000);

  test("a photo taken while it waits for its writes is written with them: it goes from the other tab only", async () => {
    vi.stubGlobal("navigator", { locks: fakeLocks() });
    const saves = gatedSaves();
    const a = await loadTab("A");
    await connectTab(a, "u1", id => saves.wrap(a.storage.indexedDbUploadStorage(a.storage.uploadStorageName(id), browser)));
    await settle();
    await as(a, () => a.queue.takePhoto(photo("card one")));
    // card two's write is slow
    saves.on = true;
    await as(a, () => a.queue.takePhoto(photo("card two")));

    // B takes the queue (A, stuck writing, doesn't hand it over in time), but reads it only a moment later
    const claiming = gate();
    const b = await loadTab("B");
    await connectTab(b, "u1", (id) => {
      const real = b.storage.indexedDbUploadStorage(b.storage.uploadStorageName(id), browser);
      return { ...real, claim: tab => claiming.opened.then(() => real.claim(tab)) };
    });
    await as(b, () => b.queue.takeOverQueue());
    await settle();
    expect(a.queue.queueElsewhere.value).toBe(false);

    // A still shows the capture page while it waits: the user takes card three, written after card two
    await as(a, () => a.queue.takePhoto(photo("card three")));
    await as(a, () => saves.all());
    expect(a.queue.queueElsewhere.value).toBe(true);
    expect(a.queue.cards.value).toEqual([]);
    expect(a.queue.uploadingLeftovers.value).toBe(false);
    await as(b, () => claiming.open());
    await settle(50);
    expect(await cardTexts(b)).toEqual(["card one", "card two", "card three"]);
    // A never sent card three: the queue was B's by then
    expect(sent.filter(request => request.photo === "card three")).toEqual([]);
  }, 10_000);

  test("a card made smaller while it waits for its writes goes from the other tab only, as it was stored", async () => {
    vi.stubGlobal("navigator", { locks: fakeLocks() });
    sharedLocalStorage().setItem("mealie.recipe-ingest.data-saver", "true");
    // a browser's encoder, as text: a photo made smaller is "small:<its text>"; one photo's decode waits for `shrunk`
    const shrinking = { photo: "", shrunk: gate() };
    vi.stubGlobal("createImageBitmap", async (blob: Blob) => {
      const text = (await blob.text()).slice(JPEG_HEAD.length).trim();
      if (text === shrinking.photo) {
        await shrinking.shrunk.opened;
      }
      return { width: 8000, height: 6000, text, close() {} };
    });
    vi.stubGlobal("OffscreenCanvas", class {
      drawn = "";
      getContext() {
        return { fillStyle: "", fillRect: () => {}, drawImage: (bitmap: { text: string }) => (this.drawn = bitmap.text) };
      }

      async convertToBlob() {
        return new Blob([JPEG_HEAD, `small:${this.drawn}`], { type: "image/jpeg" });
      }
    });
    answeringUploads();
    const big = (text: string) => new File([JPEG_HEAD, text, " ".repeat(2000)], `${text}.jpg`, { type: "image/jpeg" });
    const saves = gatedSaves();
    const a = await loadTab("A");
    await connectTab(a, "u1", id => saves.wrap(a.storage.indexedDbUploadStorage(a.storage.uploadStorageName(id), browser)));
    await settle();
    await as(a, () => a.queue.takePhoto(big("card one")));
    expect(sent).toEqual([{ tab: "A", photo: "small:card one" }]);

    // card two: its write is slow, and so is making it smaller
    const claiming = gate();
    const b = await loadTab("B");
    await connectTab(b, "u1", (id) => {
      const real = b.storage.indexedDbUploadStorage(b.storage.uploadStorageName(id), browser);
      return { ...real, claim: tab => claiming.opened.then(() => real.claim(tab)) };
    });
    await settle();
    saves.on = true;
    shrinking.photo = "card two";
    await as(a, () => a.queue.takePhoto(big("card two")));
    // B takes the queue (A, stuck writing, doesn't hand it over in time)
    await as(b, () => b.queue.takeOverQueue());
    expect(a.queue.queueElsewhere.value).toBe(false);
    // card two is made smaller while A waits; its first write goes, then B claims the queue, so the next write (the
    // smaller photo) is refused: the storage holds card two as it was taken
    await as(a, () => shrinking.shrunk.open());
    await as(a, () => saves.next());
    await as(b, () => claiming.open());
    await as(a, () => saves.all());
    await settle(50);

    expect(a.queue.queueElsewhere.value).toBe(true);
    expect(a.queue.cards.value).toEqual([]);
    expect(a.queue.uploadingLeftovers.value).toBe(false);
    // sent once, from B, which made it smaller itself
    expect(sent).toEqual([{ tab: "A", photo: "small:card one" }, { tab: "B", photo: "small:card two" }]);
  }, 10_000);
});

describe("without Web Locks, the tab keeping the queue wakes after another took it", () => {
  test("its overdue renewal finds out first: a photo its camera gives it then goes from it, and is counted", async () => {
    vi.stubGlobal("navigator", {});
    // A's timers don't run while it's in the background: its lease renewal is run by hand when it wakes
    const a = await loadTab("A");
    let renewA: () => void = () => {};
    const frozenInterval = vi.fn((renew: () => void, ms?: number) => {
      if (ms === a.storage.QUEUE_RENEW_MS) {
        renewA = renew;
      }
      return 0 as unknown as ReturnType<typeof setInterval>;
    });
    vi.stubGlobal("setInterval", frozenInterval);
    await connectTab(a);
    vi.unstubAllGlobals();
    vi.stubGlobal("navigator", {});
    await settle();
    await as(a, () => a.queue.takePhoto(photo("card one")));
    const b = await openTab("B");

    // A's lease runs out: B takes the queue, and sends the stored card
    const realNow = Date.now.bind(Date);
    vi.spyOn(Date, "now").mockImplementation(() => realNow() + b.storage.QUEUE_LEASE_MS + 1000);
    sendingTab = "B";
    await new Promise(resolve => setTimeout(resolve, b.storage.QUEUE_RENEW_MS + 500));
    await settle();
    expect(b.queue.queueElsewhere.value).toBe(false);

    // A wakes: its overdue renewal finds the lease taken before the camera gives it the photo
    await as(a, () => renewA());
    expect(a.queue.queueElsewhere.value).toBe(true);
    expect(a.queue.uploadingLeftovers.value).toBe(false);
    await as(a, () => a.queue.takePhoto(photo("late photo")));
    await settle(50);
    expect(sent).toEqual([
      { tab: "A", photo: "card one" },
      { tab: "B", photo: "card one" },
      { tab: "A", photo: "late photo" },
    ]);
    expect(a.queue.uploadingLeftovers.value).toBe(true);
    // B's logout asks about it
    expect(b.uploads.recipeIngestPhotosNotUploaded.value).toBe(2);
    vi.restoreAllMocks();
  }, 20_000);
});

describe("without Web Locks, two tabs asking for the queue while their messages are slow", () => {
  test("only one of them keeps it, and the stored card is sent once", async () => {
    vi.stubGlobal("navigator", {});
    const x = await openTab("X");
    await as(x, () => x.queue.takePhoto(photo("card one")));
    await as(x, () => x.uploads.resetRecipeIngestUploads());
    sent.length = 0;

    // neither hears the other in time
    vi.stubGlobal("BroadcastChannel", SlowChannel);
    const a = await loadTab("A");
    const b = await loadTab("B");
    sendingTab = "A or B";
    const connectingA = connectTab(a);
    await new Promise(resolve => setTimeout(resolve, 100));
    await Promise.all([connectingA, connectTab(b)]);
    await settle(300);
    expect(sent.map(request => request.photo)).toEqual(["card one"]);
    // the first to ask keeps it
    expect([a.queue.queueElsewhere.value, b.queue.queueElsewhere.value]).toEqual([false, true]);
  }, 10_000);
});
