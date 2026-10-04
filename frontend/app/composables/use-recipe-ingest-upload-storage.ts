/**
 * Where the recipe card upload queue is kept between visits (docs/ai/PHASE2.md §1.1): an IndexedDB database per user,
 * so a reload, a closed tab or iOS dropping a background tab doesn't lose photos not uploaded yet. The queue writes
 * small records (cards, batches, the tray, the front waiting for its back) and each photo once, by id; it deletes a
 * card's record once the card has uploaded.
 *
 * One tab of the browser keeps a user's queue at a time (`openQueueLock`: a Web Lock, or where there is none, as on a
 * server reached over plain http, a BroadcastChannel with a heartbeat). The tab that takes the queue claims the
 * database (`claim`), and a write names its tab, so a tab that lost the queue without noticing yet (frozen in the
 * background, a lock taken over) writes nothing. A database deleted by a logout in another tab isn't created again by
 * this one. Fork-owned.
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
  /**
   * Writes the change in one transaction; rejects when it couldn't be written. With `tab`, only while the queue is that
   * tab's: once another tab has claimed it, or the database was deleted, it rejects with `QueueTakenError` and writes
   * nothing. A queue no tab has claimed yet becomes `tab`'s.
   */
  save(change: UploadStorageChange, tab?: string): Promise<void>;
  /** The queue is `tab`'s from now on, when anything is stored (a queue with nothing stored is claimed by its first write) */
  claim(tab: string): Promise<void>;
  /** The tab whose queue it is; null while no tab has claimed it */
  claimedBy(): Promise<string | null>;
  /** Forgets everything (logout) */
  clear(): Promise<void>;
}

/** The queue is another tab's now, or its database was deleted (a logout elsewhere): this tab writes nothing more */
export class QueueTakenError extends Error {
  /** The database was deleted (or upgraded) by another tab, rather than claimed */
  readonly closed: boolean;

  constructor(closed = false) {
    super(closed ? "The recipe card queue was deleted by another tab" : "The recipe card queue is kept by another tab");
    this.name = "QueueTakenError";
    this.closed = closed;
  }
}

const DB_VERSION = 1;
const RECORDS = "records";
const PHOTOS = "photos";
/** The record naming the tab that keeps the queue; `load` leaves it out */
const OWNER_KEY = "owner";

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
  /** Another tab deleted the database (a logout) or upgraded it: opening it again would create it again */
  let closed = false;

  function open(): Promise<IDBDatabase> {
    if (closed) {
      return Promise.reject(new QueueTakenError(true));
    }
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
          // another tab deletes the database on logout: let it, and don't create it again with a later write
          db.onversionchange = () => {
            db.close();
            opening = null;
            closed = true;
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
      records.delete(OWNER_KEY);
      return {
        records,
        photos: new Map([...photos].map(([id, stored]) => [id, fromStoredPhoto(stored as StoredPhoto | Blob)])),
      };
    },

    async save(change, tab) {
      const db = await open();
      const transaction = db.transaction([RECORDS, PHOTOS], "readwrite");
      const done = transactionDone(transaction);
      const records = transaction.objectStore(RECORDS);
      const photos = transaction.objectStore(PHOTOS);
      const write = () => {
        change.putPhotos.forEach((photo, id) => photos.put(toStoredPhoto(photo), id));
        change.deletePhotos.forEach(id => photos.delete(id));
        change.putRecords.forEach((record, key) => records.put(record, key));
        change.deleteRecords.forEach(key => records.delete(key));
      };
      if (!tab) {
        write();
        await done;
        return;
      }
      // in the same transaction as the writes, so a claim by another tab comes wholly before or after them
      let taken = false;
      const ownerRequest = records.get(OWNER_KEY);
      ownerRequest.onsuccess = () => {
        const keeper = ownerRequest.result as string | undefined;
        if (keeper !== undefined && keeper !== tab) {
          taken = true;
          transaction.abort();
          return;
        }
        if (keeper === undefined) {
          records.put(tab, OWNER_KEY);
        }
        write();
      };
      try {
        await done;
      }
      catch (error) {
        throw taken ? new QueueTakenError() : error;
      }
    },

    async claim(tab) {
      if (!(await exists())) {
        return;
      }
      const db = await open();
      const transaction = db.transaction([RECORDS], "readwrite");
      const done = transactionDone(transaction);
      transaction.objectStore(RECORDS).put(tab, OWNER_KEY);
      await done;
    },

    async claimedBy() {
      if (!closed && !(await exists())) {
        return null;
      }
      const db = await open();
      const keeper = await requestResult(db.transaction([RECORDS], "readonly").objectStore(RECORDS).get(OWNER_KEY));
      return typeof keeper === "string" ? keeper : null;
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
export function memoryUploadStorage(): UploadStorage & {
  records: Map<string, unknown>;
  photos: Map<string, Blob>;
  /** The tab that keeps the queue */
  owner: string | undefined;
} {
  const store = {
    records: new Map<string, unknown>(),
    photos: new Map<string, Blob>(),
    owner: undefined as string | undefined,
    load(): Promise<StoredUploads> {
      return Promise.resolve({ records: new Map(store.records), photos: new Map(store.photos) });
    },
    save(change: UploadStorageChange, tab?: string): Promise<void> {
      if (tab && store.owner !== undefined && store.owner !== tab) {
        return Promise.reject(new QueueTakenError());
      }
      if (tab) {
        store.owner = tab;
      }
      change.putPhotos.forEach((photo, id) => store.photos.set(id, photo));
      change.deletePhotos.forEach(id => store.photos.delete(id));
      // a structured clone, as IndexedDB stores it
      change.putRecords.forEach((record, key) => store.records.set(key, JSON.parse(JSON.stringify(record))));
      change.deleteRecords.forEach(key => store.records.delete(key));
      return Promise.resolve();
    },
    claim(tab: string): Promise<void> {
      if (store.records.size || store.photos.size) {
        store.owner = tab;
      }
      return Promise.resolve();
    },
    claimedBy(): Promise<string | null> {
      return Promise.resolve(store.owner ?? null);
    },
    clear(): Promise<void> {
      store.records.clear();
      store.photos.clear();
      store.owner = undefined;
      return Promise.resolve();
    },
  };
  return store;
}

/** The user's IndexedDB storage; null where the browser has none (then the queue lives in memory only) */
export function openUploadStorage(userId: string): UploadStorage | null {
  if (typeof indexedDB === "undefined" || !indexedDB) {
    return null;
  }
  return indexedDbUploadStorage(uploadStorageName(userId));
}

// ==========================================
// Which tab keeps the queue

/** What the tab is told as the queue comes and goes */
export interface QueueLockEvents {
  /** This tab keeps the queue now */
  granted: () => void;
  /** Another tab keeps it: this one waits, and gets it when that tab lets it go or closes */
  waiting: () => void;
  /** Another tab took the queue over: this one stops keeping it and waits again */
  lost: () => void;
}

export interface QueueLock {
  /** Lets the queue go to the tab that asked for it, and waits for it again */
  yield: () => void;
  /** Takes the queue from the tab keeping it, which loses it */
  steal: () => void;
  /** Lets the queue go, or stops waiting for it, for good (a sign-out) */
  release: () => void;
}

/** The queue as a Web Lock: the browser hands it to the next tab in line when the tab keeping it lets go or closes */
export function webLocksQueueLock(locks: LockManager, name: string, events: QueueLockEvents): QueueLock {
  let stopped = false;
  /** Ends this tab's hold of the lock; null while it doesn't hold it */
  let letGo: (() => void) | null = null;
  /** Gives up this tab's place in the line; null while it isn't in it */
  let cancelWait: (() => void) | null = null;

  function request(options: LockOptions, unavailable?: () => void, refused?: () => void) {
    let mine: (() => void) | null = null;
    let answered = false;
    locks.request(name, options, (lock) => {
      answered = true;
      if (!lock) {
        unavailable?.();
        return undefined;
      }
      cancelWait = null;
      if (stopped) {
        return undefined;
      }
      return new Promise<void>((resolve) => {
        mine = resolve;
        letGo = resolve;
        events.granted();
      });
    }).catch(() => {
      // the queue was taken over while this tab kept it (a wait given up changes nothing)
      if (mine && letGo === mine && !stopped) {
        letGo = null;
        mine();
        events.lost();
        wait();
      }
      else if (!answered && !stopped) {
        refused?.();
      }
    });
  }

  function wait() {
    const controller = new AbortController();
    cancelWait = () => controller.abort();
    request({ signal: controller.signal });
  }

  // the queue at once when no tab keeps it, else a place in the line; where the browser refuses Web Locks (an opaque
  // origin), this tab keeps it, as before there were tabs
  request({ ifAvailable: true }, () => {
    if (!stopped) {
      events.waiting();
      wait();
    }
  }, () => events.granted());

  return {
    yield() {
      const go = letGo;
      if (go && !stopped) {
        letGo = null;
        wait();
        go();
      }
    },
    steal() {
      if (letGo || stopped) {
        return;
      }
      cancelWait?.();
      cancelWait = null;
      request({ steal: true });
    },
    release() {
      stopped = true;
      cancelWait?.();
      cancelWait = null;
      const go = letGo;
      letGo = null;
      go?.();
    },
  };
}

/** How long a tab asking for the queue waits for the tab keeping it to answer */
export const QUEUE_ASK_MS = 400;
/** How often the tab keeping the queue says so */
export const QUEUE_BEAT_MS = 5000;
/**
 * A tab waiting for the queue asks again when it hasn't heard from the tab keeping it for this long (a background tab's
 * timers may run once a minute; its answers to a question don't wait for them)
 */
export const QUEUE_STALE_MS = 20_000;

interface QueueLockMessage {
  type: "ask" | "held" | "free" | "steal";
  tab: string;
  /** When the tab saying "held" took the queue */
  since?: number;
}

/**
 * The queue without Web Locks (a server reached over plain http has none): the tabs ask over a BroadcastChannel. The
 * tab keeping the queue answers and says so every 5 s; a tab that hears nothing within `QUEUE_ASK_MS` takes it. When
 * two tabs took it at once, the one that took it last keeps it: its claim on the database is the one that counts.
 */
export function channelQueueLock(
  name: string,
  events: QueueLockEvents,
  tab: string,
  makeChannel: (name: string) => BroadcastChannel = channelName => new BroadcastChannel(channelName),
): QueueLock {
  const channel = makeChannel(`${name}-lock`);
  let state: "asking" | "waiting" | "holding" | "stopped" = "asking";
  /** When this tab took the queue */
  let since = 0;
  /** When the tab keeping the queue was last heard from */
  let heard = Date.now();
  let askTimer: ReturnType<typeof setTimeout> | null = null;

  const post = (type: QueueLockMessage["type"]) => {
    try {
      channel.postMessage({ type, tab, ...(type === "held" ? { since } : {}) } satisfies QueueLockMessage);
    }
    catch {
      // closed
    }
  };

  function hold() {
    state = "holding";
    since = Date.now();
    post("held");
    events.granted();
  }

  function ask() {
    state = "asking";
    post("ask");
    if (askTimer !== null) {
      clearTimeout(askTimer);
    }
    askTimer = setTimeout(() => {
      askTimer = null;
      if (state === "asking") {
        hold();
      }
    }, QUEUE_ASK_MS);
  }

  function lose() {
    state = "waiting";
    heard = Date.now();
    events.lost();
  }

  channel.onmessage = (event: MessageEvent<QueueLockMessage>) => {
    const message = event.data;
    if (!message || message.tab === tab || state === "stopped") {
      return;
    }
    switch (message.type) {
      case "ask":
        if (state === "holding") {
          post("held");
        }
        break;
      case "held":
        heard = Date.now();
        if (state === "asking") {
          if (askTimer !== null) {
            clearTimeout(askTimer);
            askTimer = null;
          }
          state = "waiting";
          events.waiting();
        }
        else if (state === "holding") {
          // two tabs keep it: the one that took it last keeps it, the other is told
          const theirs = message.since ?? 0;
          if (theirs > since || (theirs === since && message.tab > tab)) {
            lose();
          }
          else {
            post("held");
          }
        }
        break;
      case "free":
        if (state === "waiting") {
          // a moment apart, so that of several waiting tabs one asks first and the others hear it took the queue
          state = "asking";
          askTimer = setTimeout(ask, Math.random() * QUEUE_ASK_MS / 2);
        }
        break;
      case "steal":
        if (state === "holding") {
          lose();
        }
        else {
          heard = Date.now();
        }
        break;
    }
  };

  const beat = setInterval(() => {
    if (state === "holding") {
      post("held");
    }
    else if (state === "waiting" && Date.now() - heard > QUEUE_STALE_MS) {
      ask();
    }
  }, QUEUE_BEAT_MS);

  // a tab that closes lets the queue go at once, rather than after `QUEUE_STALE_MS`
  const onPageHide = () => {
    if (state === "holding") {
      post("free");
    }
  };
  if (typeof window !== "undefined") {
    window.addEventListener("pagehide", onPageHide);
  }

  ask();

  return {
    yield() {
      if (state === "holding") {
        state = "waiting";
        heard = Date.now();
        post("free");
      }
    },
    steal() {
      if (state === "holding" || state === "stopped") {
        return;
      }
      if (askTimer !== null) {
        clearTimeout(askTimer);
        askTimer = null;
      }
      post("steal");
      hold();
    },
    release() {
      if (state === "holding") {
        post("free");
      }
      state = "stopped";
      if (askTimer !== null) {
        clearTimeout(askTimer);
        askTimer = null;
      }
      clearInterval(beat);
      if (typeof window !== "undefined") {
        window.removeEventListener("pagehide", onPageHide);
      }
      channel.close();
    },
  };
}

/** A browser with neither Web Locks nor BroadcastChannel: the tab keeps the queue (as before there were two) */
function soleQueueLock(events: QueueLockEvents): QueueLock {
  events.granted();
  return { yield() {}, steal() {}, release() {} };
}

/** The user's queue among this browser's tabs: a Web Lock where there is one, else a BroadcastChannel, else this tab */
export function openQueueLock(name: string, events: QueueLockEvents, tab: string): QueueLock {
  const locks = typeof navigator !== "undefined" ? navigator.locks : undefined;
  if (locks && typeof locks.request === "function") {
    return webLocksQueueLock(locks, name, events);
  }
  if (typeof BroadcastChannel === "function") {
    return channelQueueLock(name, events, tab);
  }
  return soleQueueLock(events);
}

/** The channel the user's tabs tell each other about the queue on; null where the browser has none */
export function openQueueChannel(name: string): BroadcastChannel | null {
  if (typeof BroadcastChannel !== "function") {
    return null;
  }
  try {
    return new BroadcastChannel(`${name}-queue`);
  }
  catch {
    return null;
  }
}
