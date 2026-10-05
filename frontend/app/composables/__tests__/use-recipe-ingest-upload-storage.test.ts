// @vitest-environment node
// Node's Blob survives IndexedDB's structured clone (jsdom's doesn't), as a browser's does
import { IDBFactory } from "fake-indexeddb";
import { describe, expect, test, vi } from "vitest";
import {
  QUEUE_LEASE_MS,
  QueueTakenError,
  indexedDbUploadStorage,
  leaseQueueLock,
  memoryUploadStorage,
  uploadStorageName,
} from "../use-recipe-ingest-upload-storage";
import type { UploadStorage } from "../use-recipe-ingest-upload-storage";

function change(overrides: Partial<Parameters<UploadStorage["save"]>[0]> = {}) {
  return { putRecords: new Map(), deleteRecords: [], putPhotos: new Map(), deletePhotos: [], ...overrides };
}

describe.each([
  ["IndexedDB", () => indexedDbUploadStorage(uploadStorageName("u1"), new IDBFactory())],
  ["memory", () => memoryUploadStorage()],
])("the upload queue's %s storage", (_name, make) => {
  test("keeps records and photos, with their names, and forgets them all", async () => {
    const storage = make();
    const front = new File(["front"], "IMG_1.HEIC", { type: "image/heic", lastModified: 1000 });
    await storage.save(change({
      putRecords: new Map<string, unknown>([["card:c1", { key: "c1", photoIds: ["p1"] }], ["open", { batchKey: "b" }]]),
      putPhotos: new Map([["p1", front], ["p2", new Blob(["back"])]]),
    }));

    const loaded = await storage.load();
    expect(loaded.records.get("card:c1")).toEqual({ key: "c1", photoIds: ["p1"] });
    const photo = loaded.photos.get("p1") as File;
    expect([photo.name, photo.type, photo.lastModified]).toEqual(["IMG_1.HEIC", "image/heic", 1000]);
    expect(await photo.text()).toBe("front");
    expect(await loaded.photos.get("p2")!.text()).toBe("back");

    await storage.save(change({ deleteRecords: ["open"], deletePhotos: ["p2"] }));
    const after = await storage.load();
    expect([...after.records.keys()]).toEqual(["card:c1"]);
    expect([...after.photos.keys()]).toEqual(["p1"]);

    await storage.clear();
    const empty = await storage.load();
    expect([empty.records.size, empty.photos.size]).toEqual([0, 0]);
  });

  test("keeps the writes of the tab that claimed the queue last; another tab's are refused whole", async () => {
    const storage = make();
    // the first write of an unclaimed queue claims it
    await storage.save(change({ putRecords: new Map([["card:a", { key: "a" }]]) }), "tab-a");
    expect(await storage.claimedBy()).toBe("tab-a");

    await storage.claim("tab-b");
    await expect(storage.save(change({
      putRecords: new Map([["card:late", { key: "late" }]]),
      deleteRecords: ["card:a"],
      putPhotos: new Map([["p9", new Blob(["late"])]]),
    }), "tab-a")).rejects.toBeInstanceOf(QueueTakenError);
    const loaded = await storage.load();
    // nothing of the refused write, and the claim isn't a record of the queue
    expect([...loaded.records.keys()]).toEqual(["card:a"]);
    expect(loaded.photos.size).toBe(0);

    await storage.save(change({ deleteRecords: ["card:a"] }), "tab-b");
    expect((await storage.load()).records.size).toBe(0);
    expect(await storage.claimedBy()).toBe("tab-b");
  });
});

test("a database another tab deleted (a logout) isn't created again by a later write", async () => {
  const factory = new IDBFactory();
  const here = indexedDbUploadStorage(uploadStorageName("u1"), factory);
  await here.save(change({ putRecords: new Map([["open", { batchKey: "b" }]]) }), "tab-a");

  // the user logs out in another tab
  await indexedDbUploadStorage(uploadStorageName("u1"), factory).clear();
  expect((await factory.databases()).map(db => db.name)).toEqual([]);

  const refused = await here.save(change({ putRecords: new Map([["front", { photoId: "p1" }]]) }), "tab-a").catch(error => error);
  expect(refused).toBeInstanceOf(QueueTakenError);
  expect((refused as QueueTakenError).closed).toBe(true);
  expect((await factory.databases()).map(db => db.name)).toEqual([]);
});

describe.each([
  ["IndexedDB", () => indexedDbUploadStorage(uploadStorageName("u1"), new IDBFactory())],
  ["memory", () => memoryUploadStorage()],
])("the queue's lease in the %s storage", (_name, make) => {
  test("goes to one tab at a time: renewed by its holder, taken when it runs out or by force", async () => {
    const storage = make();
    const now = Date.now();
    const a = { tab: "tab-a", hold: 1 };
    const b = { tab: "tab-b", hold: 1 };
    const c = { tab: "tab-c", hold: 1 };
    expect(await storage.lease(a, now + 1000)).toBe(true);
    expect(await storage.lease(b, now + 1000)).toBe(false);
    // its holder renews it
    expect(await storage.lease(a, now + 2000)).toBe(true);
    // "Use this tab" takes it over (from a tab that didn't let it go)
    expect(await storage.lease(b, now + 2000, true)).toBe("taken");
    expect(await storage.lease(a, now + 3000)).toBe(false);
    // a holder frozen in the background: its lease runs out, and another tab takes it
    expect(await storage.lease(b, now - 1)).toBe(true);
    expect(await storage.lease(a, now + 3000)).toBe("taken");
    // taken from the hold that tab said it let go of (it closed), and from no other
    expect(await storage.lease(c, now + 3000, b)).toBe(false);
    expect(await storage.lease(c, now + 3000, a)).toBe(true);
    expect(await storage.lease(a, now + 3000, b)).toBe(false);
    expect(await storage.lease(a, now + 3000, c)).toBe(true);
    // let go by its holder only
    await storage.releaseLease("tab-b");
    expect(await storage.lease(b, now + 3000)).toBe(false);
    await storage.releaseLease("tab-a");
    expect(await storage.lease(b, now + 3000)).toBe(true);
    // not a record of the queue
    expect((await storage.load()).records.size).toBe(0);
  });

  test("a tab's word that it let go of one hold doesn't take over a later hold of that tab", async () => {
    const storage = make();
    const now = Date.now();
    // tab A let go of its first hold (it said "free"), then took the lease again before tab B asked
    expect(await storage.lease({ tab: "tab-a", hold: 1 }, now + 1000)).toBe(true);
    await storage.releaseLease("tab-a");
    expect(await storage.lease({ tab: "tab-a", hold: 2 }, now + 1000)).toBe(true);
    expect(await storage.lease({ tab: "tab-b", hold: 1 }, now + 1000, { tab: "tab-a", hold: 1 })).toBe(false);
    // renewals keep the hold, and its word for that one is enough
    expect(await storage.lease({ tab: "tab-a", hold: 2 }, now + 2000)).toBe(true);
    expect(await storage.lease({ tab: "tab-b", hold: 1 }, now + 2000, { tab: "tab-a", hold: 2 })).toBe(true);
  });
});

/** What each tab is told as the queue comes and goes */
function lockEvents() {
  const seen: string[] = [];
  return {
    seen,
    granted: (forced: boolean) => seen.push(forced ? "granted by force" : "granted"),
    waiting: () => seen.push("waiting"),
    lost: () => seen.push("lost"),
  };
}

test("two tabs asking for the queue at once without Web Locks: one gets it, the other when it's let go", async () => {
  const factory = new IDBFactory();
  const a = lockEvents();
  const b = lockEvents();
  // each tab with its own connection to the database, asking in the same moment
  const lockA = leaseQueueLock(indexedDbUploadStorage(uploadStorageName("u1"), factory), "q", a, "tab-a");
  const lockB = leaseQueueLock(indexedDbUploadStorage(uploadStorageName("u1"), factory), "q", b, "tab-b");
  await new Promise(resolve => setTimeout(resolve, 200));

  expect([...a.seen, ...b.seen].sort()).toEqual(["granted", "waiting"]);
  const [keeping, keepingLock, other] = a.seen[0] === "granted" ? [a, lockA, b] : [b, lockB, a];
  expect(await keepingLock.stillHeld()).toBe(true);

  // the one keeping it lets it go: the other takes it at once (it's told over the channel)
  keepingLock.release();
  await new Promise(resolve => setTimeout(resolve, 200));
  expect(other.seen).toEqual(["waiting", "granted"]);
  expect(keeping.seen).toEqual(["granted"]);
  lockA.release();
  lockB.release();
});

test("a tab that closes says so, and the waiting tab takes the queue at once, though the closing tab's write didn't go", async () => {
  const page = new EventTarget();
  vi.stubGlobal("window", page);
  try {
    const factory = new IDBFactory();
    const a = lockEvents();
    const b = lockEvents();
    // the page unloads before its IndexedDB write can let the lease go
    const unloading = { ...indexedDbUploadStorage(uploadStorageName("u1"), factory), releaseLease: () => new Promise<void>(() => {}) };
    const lockA = leaseQueueLock(unloading, "q", a, "tab-a");
    await new Promise(resolve => setTimeout(resolve, 100));
    const lockB = leaseQueueLock(indexedDbUploadStorage(uploadStorageName("u1"), factory), "q", b, "tab-b");
    await new Promise(resolve => setTimeout(resolve, 100));
    expect([a.seen, b.seen]).toEqual([["granted"], ["waiting"]]);

    page.dispatchEvent(new Event("pagehide"));
    await new Promise(resolve => setTimeout(resolve, 100));
    expect(b.seen).toEqual(["waiting", "granted"]);
    lockA.release();
    lockB.release();
  }
  finally {
    vi.unstubAllGlobals();
  }
});

/** A tab's sessionStorage (Node has none): one per tab, kept across its reloads */
function tabSession() {
  const store = new Map<string, string>();
  return {
    store,
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key),
  };
}

/** A page's `pageshow`: `persisted` when it comes back from the back-forward cache */
function pageShow(persisted: boolean) {
  return Object.assign(new Event("pageshow"), { persisted });
}

/** The lease record as the database holds it */
async function storedLease(factory: IDBFactory, name: string) {
  const db = await new Promise<IDBDatabase>((resolve, reject) => {
    const request = factory.open(name);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  try {
    return await new Promise<unknown>((resolve) => {
      const request = db.transaction(["records"]).objectStore("records").get("lease");
      request.onsuccess = () => resolve(request.result);
    });
  }
  finally {
    db.close();
  }
}

test("a tab that lets the queue go while a renewal is on its way isn't granted it again: the asking tab gets it", async () => {
  const factory = new IDBFactory();
  const storageA = indexedDbUploadStorage(uploadStorageName("u1"), factory);
  const a = lockEvents();
  const b = lockEvents();
  const lockA = leaseQueueLock(storageA, "q-yield", a, "tab-a");
  await new Promise(resolve => setTimeout(resolve, 50));
  const lockB = leaseQueueLock(indexedDbUploadStorage(uploadStorageName("u1"), factory), "q-yield", b, "tab-b");
  await new Promise(resolve => setTimeout(resolve, 50));
  expect([a.seen, b.seen]).toEqual([["granted"], ["waiting"]]);

  // "Use this tab" in B: A writes what changed last, and an upload's check of the lease is queued behind that write;
  // then A lets the queue go
  const saved = storageA.save(change({ putPhotos: new Map([["p1", new Blob([new Uint8Array(200_000)])]]) }), "tab-a");
  const checked = lockA.stillHeld();
  await saved;
  lockA.yield();
  expect(await checked).toBe(false);
  await new Promise(resolve => setTimeout(resolve, 150));

  expect(a.seen).toEqual(["granted"]);
  expect(b.seen).toEqual(["waiting", "granted"]);
  expect(await lockA.stillHeld()).toBe(false);
  expect(await lockB.stillHeld()).toBe(true);
  expect(await storedLease(factory, uploadStorageName("u1"))).toMatchObject({ tab: "tab-b" });
  lockA.release();
  lockB.release();
});

test("a \"free\" that comes after its tab held the queue anew doesn't take it from that tab", async () => {
  const page = new EventTarget();
  vi.stubGlobal("window", page);
  vi.stubGlobal("sessionStorage", tabSession());
  try {
    const factory = new IDBFactory();
    const a = lockEvents();
    const b = lockEvents();
    const lockA = leaseQueueLock(indexedDbUploadStorage(uploadStorageName("u1"), factory), "q-free", a, "tab-a");
    await new Promise(resolve => setTimeout(resolve, 50));
    // B, in another tab, hears A late (a busy browser)
    vi.stubGlobal("window", undefined);
    const lockB = leaseQueueLock(indexedDbUploadStorage(uploadStorageName("u1"), factory), "q-free", b, "tab-b", (name) => {
      const late = new BroadcastChannel(name);
      const heard = late as unknown as { onmessage: ((event: MessageEvent) => void) | null };
      const relay = { onmessage: null as ((event: MessageEvent) => void) | null, postMessage: () => {}, close: () => late.close() };
      heard.onmessage = event => setTimeout(() => relay.onmessage?.(event), 100);
      return relay as unknown as BroadcastChannel;
    });
    await new Promise(resolve => setTimeout(resolve, 50));
    expect([a.seen, b.seen]).toEqual([["granted"], ["waiting"]]);

    // A goes into the back-forward cache (it says "free") and comes straight back: it holds the queue anew
    page.dispatchEvent(new Event("pagehide"));
    page.dispatchEvent(pageShow(true));
    await new Promise(resolve => setTimeout(resolve, 250));

    expect(a.seen).toEqual(["granted"]);
    expect(b.seen).toEqual(["waiting"]);
    expect(await lockA.stillHeld()).toBe(true);
    expect(await lockB.stillHeld()).toBe(false);
    lockA.release();
    lockB.release();
  }
  finally {
    vi.unstubAllGlobals();
  }
});

test("a reloaded page takes the queue at once, though its previous page's lease wasn't let go and nobody heard it", async () => {
  const page = new EventTarget();
  const session = tabSession();
  vi.stubGlobal("window", page);
  vi.stubGlobal("sessionStorage", session);
  try {
    const factory = new IDBFactory();
    const before = lockEvents();
    // the page unloads before its IndexedDB write can let the lease go, and no other tab is there to hear it
    const unloading = { ...indexedDbUploadStorage(uploadStorageName("u1"), factory), releaseLease: () => new Promise<void>(() => {}) };
    const lockBefore = leaseQueueLock(unloading, "q-reload", before, "tab-1", null);
    await new Promise(resolve => setTimeout(resolve, 50));
    expect(before.seen).toEqual(["granted"]);
    page.dispatchEvent(new Event("pagehide"));
    lockBefore.release();

    // another tab of the browser (a sessionStorage of its own) still waits for the lease to run out
    vi.stubGlobal("sessionStorage", tabSession());
    const other = lockEvents();
    const lockOther = leaseQueueLock(indexedDbUploadStorage(uploadStorageName("u1"), factory), "q-reload", other, "tab-other", null);
    await new Promise(resolve => setTimeout(resolve, 50));
    expect(other.seen).toEqual(["waiting"]);
    lockOther.release();

    // the page as it's loaded again, in the same tab
    vi.stubGlobal("sessionStorage", session);
    const after = lockEvents();
    const lockAfter = leaseQueueLock(indexedDbUploadStorage(uploadStorageName("u1"), factory), "q-reload", after, "tab-2", null);
    await new Promise(resolve => setTimeout(resolve, 50));
    expect(after.seen).toEqual(["granted"]);
    expect(await storedLease(factory, uploadStorageName("u1"))).toMatchObject({ tab: "tab-2" });
    // read once: a tab duplicated from this one later doesn't find it
    expect(session.store.size).toBe(0);
    lockAfter.release();
  }
  finally {
    vi.unstubAllGlobals();
  }
});

test("a page back from the back-forward cache takes the queue from the page it replaced in the same tab", async () => {
  const pageA = new EventTarget();
  const pageB = new EventTarget();
  vi.stubGlobal("sessionStorage", tabSession());
  try {
    const factory = new IDBFactory();
    /** A page's storage whose lease release waits while the page is in the back-forward cache */
    const cached = () => {
      const real = indexedDbUploadStorage(uploadStorageName("u1"), factory);
      let resume = () => {};
      const back = new Promise<void>((resolve) => {
        resume = resolve;
      });
      return { storage: { ...real, releaseLease: (tab: string) => back.then(() => real.releaseLease(tab)) }, resume };
    };
    const first = cached();
    const second = cached();
    const a = lockEvents();
    const b = lockEvents();
    vi.stubGlobal("window", pageA);
    const lockA = leaseQueueLock(first.storage, "q-cache", a, "tab-a", null);
    await new Promise(resolve => setTimeout(resolve, 50));
    // A navigates to B in the same tab (A goes into the cache): B takes the queue at once
    pageA.dispatchEvent(new Event("pagehide"));
    vi.stubGlobal("window", pageB);
    const lockB = leaseQueueLock(second.storage, "q-cache", b, "tab-b", null);
    await new Promise(resolve => setTimeout(resolve, 50));
    expect(b.seen).toEqual(["granted"]);

    // Back: B goes into the cache, A comes back and takes the queue from B, which finds out before it sends anything
    pageB.dispatchEvent(new Event("pagehide"));
    first.resume();
    pageA.dispatchEvent(pageShow(true));
    await new Promise(resolve => setTimeout(resolve, 50));
    expect(await lockA.stillHeld()).toBe(true);
    second.resume();
    pageB.dispatchEvent(pageShow(true));
    expect(await lockB.stillHeld()).toBe(false);
    expect(a.seen).toEqual(["granted"]);
    expect(b.seen).toEqual(["granted", "lost"]);
    lockA.release();
    lockB.release();
  }
  finally {
    vi.unstubAllGlobals();
  }
});

test("a tab whose lease was taken over finds out before it sends anything", async () => {
  const storage = memoryUploadStorage();
  const a = lockEvents();
  const lockA = leaseQueueLock(storage, "q", a, "tab-a", null);
  await new Promise(resolve => setTimeout(resolve, 20));
  expect(a.seen).toEqual(["granted"]);

  // tab A is frozen, and tab B takes the queue over ("Use this tab") or once A's lease ran out
  expect(await storage.lease({ tab: "tab-b", hold: 1 }, Date.now() + QUEUE_LEASE_MS, true)).toBe("taken");
  expect(await lockA.stillHeld()).toBe(false);
  expect(a.seen).toEqual(["granted", "lost"]);
  lockA.release();
});

test("a tab that takes the queue from a tab that didn't let it go is told so: by force, or once its lease ran out", async () => {
  const storage = memoryUploadStorage();
  const a = lockEvents();
  const b = lockEvents();
  const c = lockEvents();
  const lockA = leaseQueueLock(storage, "q-forced", a, "tab-a", null);
  await new Promise(resolve => setTimeout(resolve, 20));
  const lockB = leaseQueueLock(storage, "q-forced", b, "tab-b", null);
  await new Promise(resolve => setTimeout(resolve, 20));
  expect([a.seen, b.seen]).toEqual([["granted"], ["waiting"]]);

  // "Use this tab" in B, while A doesn't answer (frozen in the background)
  lockB.steal();
  await new Promise(resolve => setTimeout(resolve, 20));
  expect(b.seen).toEqual(["waiting", "granted by force"]);

  // B is frozen in turn: its lease runs out, and a tab opened then takes the queue
  const realNow = Date.now.bind(Date);
  vi.spyOn(Date, "now").mockImplementation(() => realNow() + QUEUE_LEASE_MS + 1000);
  const lockC = leaseQueueLock(storage, "q-forced", c, "tab-c", null);
  await new Promise(resolve => setTimeout(resolve, 20));
  expect(c.seen).toEqual(["granted by force"]);
  vi.restoreAllMocks();

  // one let go of goes to the next tab as it is
  const d = lockEvents();
  lockC.release();
  await new Promise(resolve => setTimeout(resolve, 20));
  const lockD = leaseQueueLock(storage, "q-forced", d, "tab-d", null);
  await new Promise(resolve => setTimeout(resolve, 20));
  expect(d.seen).toEqual(["granted"]);
  [lockA, lockB, lockD].forEach(lock => lock.release());
});

test("each user has a database of their own", () => {
  expect(uploadStorageName("u1")).not.toBe(uploadStorageName("u2"));
});

test("reading the queue of a user who never queued a photo creates no database", async () => {
  const factory = new IDBFactory();
  const storage = indexedDbUploadStorage(uploadStorageName("u9"), factory);
  const loaded = await storage.load();
  expect([loaded.records.size, loaded.photos.size]).toEqual([0, 0]);
  expect((await factory.databases()).map(db => db.name)).toEqual([]);

  await storage.save(change({ putRecords: new Map([["open", { batchKey: "b" }]]) }));
  expect((await factory.databases()).map(db => db.name)).toEqual([uploadStorageName("u9")]);
  expect([...(await storage.load()).records.keys()]).toEqual(["open"]);
});
