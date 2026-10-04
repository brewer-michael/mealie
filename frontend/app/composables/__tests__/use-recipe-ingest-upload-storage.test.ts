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
    expect(await storage.lease("tab-a", now + 1000)).toBe(true);
    expect(await storage.lease("tab-b", now + 1000)).toBe(false);
    // its holder renews it
    expect(await storage.lease("tab-a", now + 2000)).toBe(true);
    // "Use this tab" takes it over
    expect(await storage.lease("tab-b", now + 2000, true)).toBe(true);
    expect(await storage.lease("tab-a", now + 3000)).toBe(false);
    // a holder frozen in the background: its lease runs out, and another tab takes it
    expect(await storage.lease("tab-b", now - 1)).toBe(true);
    expect(await storage.lease("tab-a", now + 3000)).toBe(true);
    // taken from the tab that said it let go (it closed), and from no other
    expect(await storage.lease("tab-c", now + 3000, "tab-b")).toBe(false);
    expect(await storage.lease("tab-c", now + 3000, "tab-a")).toBe(true);
    expect(await storage.lease("tab-a", now + 3000, "tab-b")).toBe(false);
    expect(await storage.lease("tab-a", now + 3000, "tab-c")).toBe(true);
    // let go by its holder only
    await storage.releaseLease("tab-b");
    expect(await storage.lease("tab-b", now + 3000)).toBe(false);
    await storage.releaseLease("tab-a");
    expect(await storage.lease("tab-b", now + 3000)).toBe(true);
    // not a record of the queue
    expect((await storage.load()).records.size).toBe(0);
  });
});

/** What each tab is told as the queue comes and goes */
function lockEvents() {
  const seen: string[] = [];
  return { seen, granted: () => seen.push("granted"), waiting: () => seen.push("waiting"), lost: () => seen.push("lost") };
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

test("a tab whose lease was taken over finds out before it sends anything", async () => {
  const storage = memoryUploadStorage();
  const a = lockEvents();
  const lockA = leaseQueueLock(storage, "q", a, "tab-a", null);
  await new Promise(resolve => setTimeout(resolve, 20));
  expect(a.seen).toEqual(["granted"]);

  // tab A is frozen, and tab B takes the queue over ("Use this tab") or once A's lease ran out
  expect(await storage.lease("tab-b", Date.now() + QUEUE_LEASE_MS, true)).toBe(true);
  expect(await lockA.stillHeld()).toBe(false);
  expect(a.seen).toEqual(["granted", "lost"]);
  lockA.release();
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
