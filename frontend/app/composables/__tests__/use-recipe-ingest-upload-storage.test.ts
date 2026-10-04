// @vitest-environment node
// Node's Blob survives IndexedDB's structured clone (jsdom's doesn't), as a browser's does
import { IDBFactory } from "fake-indexeddb";
import { describe, expect, test } from "vitest";
import {
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
