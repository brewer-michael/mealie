// @vitest-environment node
// Node's Blob survives IndexedDB's structured clone (jsdom's doesn't), as a browser's does
import { IDBFactory } from "fake-indexeddb";
import { describe, expect, test } from "vitest";
import {
  QUEUE_ASK_MS,
  QueueTakenError,
  channelQueueLock,
  indexedDbUploadStorage,
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

test("two tabs that took the queue at once over a BroadcastChannel: the one that took it last keeps it", async () => {
  const events = () => {
    const seen: string[] = [];
    return { seen, granted: () => seen.push("granted"), waiting: () => seen.push("waiting"), lost: () => seen.push("lost") };
  };
  const a = events();
  const b = events();
  // neither hears the other ask in time (both ask in the same moment), so both take it
  const lockA = channelQueueLock("q", a, "tab-a");
  const lockB = channelQueueLock("q", b, "tab-b");
  await new Promise(resolve => setTimeout(resolve, QUEUE_ASK_MS + 200));

  const holders = [a.seen, b.seen].filter(seen => seen.at(-1) === "granted");
  expect(holders).toHaveLength(1);
  expect([...a.seen, ...b.seen].sort()).toEqual(["granted", "granted", "lost"]);

  // the one keeping it lets it go: the other takes it
  const [keeping, other] = holders[0] === a.seen ? [lockA, b] : [lockB, a];
  keeping.release();
  await new Promise(resolve => setTimeout(resolve, QUEUE_ASK_MS + 200));
  expect(other.seen.at(-1)).toBe("granted");
  lockA.release();
  lockB.release();
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
