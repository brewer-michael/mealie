/**
 * Where the recipe card upload queue is kept between visits (docs/ai/PHASE2.md §1.1): an IndexedDB database per user,
 * so a reload, a closed tab or iOS dropping a background tab doesn't lose photos not uploaded yet. The queue writes
 * small records (cards, batches, the tray, the front waiting for its back) and each photo once, by id; it deletes a
 * card's record once the card has uploaded. Several tabs write records of their own, so they don't overwrite each
 * other. Fork-owned.
 */

/** A stored photo: the bytes, and what a `File` needs to be made again */
export interface StoredPhoto {
  blob: Blob;
  name: string;
  type: string;
  lastModified: number;
}

/** What `load` answers: the records by key, and the photos by id */
export interface StoredUploads {
  records: Map<string, unknown>;
  photos: Map<string, Blob>;
}

/** One write: records and photos to put, and keys of each to delete */
export interface UploadStorageChange {
  putRecords: ReadonlyMap<string, unknown>;
  deleteRecords: readonly string[];
  putPhotos: ReadonlyMap<string, Blob>;
  deletePhotos: readonly string[];
}

export interface UploadStorage {
  load(): Promise<StoredUploads>;
  /** Writes the change in one transaction; rejects when it couldn't be written */
  save(change: UploadStorageChange): Promise<void>;
  /** Forgets everything (logout) */
  clear(): Promise<void>;
}

const DB_VERSION = 1;
const RECORDS = "records";
const PHOTOS = "photos";

/** The database of a user's queue */
export function uploadStorageName(userId: string): string {
  return `mealie-recipe-cards-${userId}`;
}

function toStoredPhoto(photo: Blob): StoredPhoto {
  const file = photo instanceof File ? photo : null;
  return {
    blob: photo,
    name: file?.name ?? "",
    type: photo.type,
    lastModified: file?.lastModified ?? Date.now(),
  };
}

/** The photo as it was added: a `File` with its name when it had one (the upload sends the name) */
function fromStoredPhoto(stored: StoredPhoto | Blob): Blob {
  if (stored instanceof Blob) {
    return stored;
  }
  if (!stored.name || typeof File === "undefined") {
    return stored.blob;
  }
  return new File([stored.blob], stored.name, { type: stored.type, lastModified: stored.lastModified });
}

function requestResult<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("IndexedDB request failed"));
  });
}

function transactionDone(transaction: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error ?? new Error("IndexedDB transaction failed"));
    transaction.onabort = () => reject(transaction.error ?? new Error("IndexedDB transaction aborted"));
  });
}

/** The user's queue in IndexedDB; the database is created on first use, and deleted by `clear` */
export function indexedDbUploadStorage(name: string, factory: IDBFactory = indexedDB): UploadStorage {
  let opening: Promise<IDBDatabase> | null = null;

  function open(): Promise<IDBDatabase> {
    if (!opening) {
      opening = new Promise<IDBDatabase>((resolve, reject) => {
        const request = factory.open(name, DB_VERSION);
        request.onupgradeneeded = () => {
          const db = request.result;
          if (!db.objectStoreNames.contains(RECORDS)) {
            db.createObjectStore(RECORDS);
          }
          if (!db.objectStoreNames.contains(PHOTOS)) {
            db.createObjectStore(PHOTOS);
          }
        };
        request.onsuccess = () => {
          const db = request.result;
          // another tab deletes the database on logout: let it
          db.onversionchange = () => {
            db.close();
            opening = null;
          };
          resolve(db);
        };
        request.onerror = () => reject(request.error ?? new Error("IndexedDB couldn't be opened"));
        request.onblocked = () => reject(new Error("IndexedDB is blocked by another tab"));
      });
      opening.catch(() => {
        opening = null;
      });
    }
    return opening;
  }

  async function readAll(store: IDBObjectStore): Promise<Map<string, unknown>> {
    // getAll and getAllKeys both answer in key order
    const [keys, values] = await Promise.all([requestResult(store.getAllKeys()), requestResult(store.getAll())]);
    return new Map(keys.map((key, index) => [String(key), values[index]]));
  }

  /** Whether the database exists; true where the browser can't say (it's then created empty by the read) */
  async function exists(): Promise<boolean> {
    if (opening || typeof factory.databases !== "function") {
      return true;
    }
    try {
      return (await factory.databases()).some(db => db.name === name);
    }
    catch {
      return true;
    }
  }

  return {
    async load() {
      // a user who never queued a photo gets no database just for looking
      if (!(await exists())) {
        return { records: new Map(), photos: new Map() };
      }
      const db = await open();
      const transaction = db.transaction([RECORDS, PHOTOS], "readonly");
      const [records, photos] = await Promise.all([
        readAll(transaction.objectStore(RECORDS)),
        readAll(transaction.objectStore(PHOTOS)),
      ]);
      return {
        records,
        photos: new Map([...photos].map(([id, stored]) => [id, fromStoredPhoto(stored as StoredPhoto | Blob)])),
      };
    },

    async save(change) {
      const db = await open();
      const transaction = db.transaction([RECORDS, PHOTOS], "readwrite");
      const done = transactionDone(transaction);
      const records = transaction.objectStore(RECORDS);
      const photos = transaction.objectStore(PHOTOS);
      change.putPhotos.forEach((photo, id) => photos.put(toStoredPhoto(photo), id));
      change.deletePhotos.forEach(id => photos.delete(id));
      change.putRecords.forEach((record, key) => records.put(record, key));
      change.deleteRecords.forEach(key => records.delete(key));
      await done;
    },

    async clear() {
      try {
        // emptied first: deleting waits for other tabs to let go of the database
        const db = await open();
        const transaction = db.transaction([RECORDS, PHOTOS], "readwrite");
        const done = transactionDone(transaction);
        transaction.objectStore(RECORDS).clear();
        transaction.objectStore(PHOTOS).clear();
        await done;
        db.close();
      }
      finally {
        opening = null;
        await new Promise<void>((resolve) => {
          const request = factory.deleteDatabase(name);
          request.onsuccess = () => resolve();
          request.onerror = () => resolve();
          request.onblocked = () => resolve();
        });
      }
    },
  };
}

/** The same in memory: for tests, and for a browser without IndexedDB nothing is kept */
export function memoryUploadStorage(): UploadStorage & { records: Map<string, unknown>; photos: Map<string, Blob> } {
  const records = new Map<string, unknown>();
  const photos = new Map<string, Blob>();
  return {
    records,
    photos,
    load() {
      return Promise.resolve({ records: new Map(records), photos: new Map(photos) });
    },
    save(change) {
      change.putPhotos.forEach((photo, id) => photos.set(id, photo));
      change.deletePhotos.forEach(id => photos.delete(id));
      // a structured clone, as IndexedDB stores it
      change.putRecords.forEach((record, key) => records.set(key, JSON.parse(JSON.stringify(record))));
      change.deleteRecords.forEach(key => records.delete(key));
      return Promise.resolve();
    },
    clear() {
      records.clear();
      photos.clear();
      return Promise.resolve();
    },
  };
}

/** The user's IndexedDB storage; null where the browser has none (then the queue lives in memory only) */
export function openUploadStorage(userId: string): UploadStorage | null {
  if (typeof indexedDB === "undefined" || !indexedDB) {
    return null;
  }
  return indexedDbUploadStorage(uploadStorageName(userId));
}
