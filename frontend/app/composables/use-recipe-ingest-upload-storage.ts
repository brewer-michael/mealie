/**
 * Where the recipe card upload queue is kept between visits (docs/ai/PHASE2.md §1.1): an IndexedDB database per user,
 * so a reload, a closed tab or iOS dropping a background tab doesn't lose photos not uploaded yet. The queue writes
 * small records (cards, batches, the tray, the front waiting for its back) and each photo once, by id; it deletes a
 * card's record once the card has uploaded.
 *
 * One tab of the browser keeps a user's queue at a time (`openQueueLock`: a Web Lock, or where there is none, as on a
 * server reached over plain http, a lease in this database that the tab keeping the queue renews). The tab that takes
 * the queue claims the database (`claim`), and a write names its tab, so a tab that lost the queue without noticing yet
 * (frozen in the background, a lock taken over) writes nothing. A database deleted by a logout in another tab isn't
 * created again by this one. Fork-owned.
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
  /**
   * The queue's lease, where the browser has no Web Locks (`leaseQueueLock`), in one transaction: `holder` takes it, or
   * keeps it, until `until` (ms since the epoch) when no tab holds it, its holder's time ran out or it's `holder`'s tab's
   * already; `over` takes it over, from any tab (`true`) or from the hold named (its tab said it let go of that one).
   * Answers whether `holder` holds it now (`QueueLeaseAnswer`).
   */
  lease(holder: QueueLeaseHolder, until: number, over?: true | QueueLeaseHolder): Promise<QueueLeaseAnswer>;
  /** Lets the lease go, when it's `tab`'s */
  releaseLease(tab: string): Promise<void>;
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
/** The record of the queue's lease (`QueueLease`); `load` leaves it out */
const LEASE_KEY = "lease";

/**
 * A tab's hold of the queue's lease: the tab, and which of its holds (a new one each time it takes the lease, renewals
 * keep it), so a tab's word that it let go of one hold can't take over a later one
 */
export interface QueueLeaseHolder {
  tab: string;
  hold: number;
}

/** Which tab holds the queue's lease (and its hold), and until when unless it's renewed (ms since the epoch) */
interface QueueLease extends QueueLeaseHolder {
  until: number;
}

/**
 * Whether a tab holds the queue's lease now: `"taken"` when it took it from another tab's hold that wasn't let go (by
 * force, or as its time ran out), so that tab may still be open (frozen in the background)
 */
export type QueueLeaseAnswer = boolean | "taken";

/** Whether `holder` may take or keep `lease` now (`over`: as `UploadStorage.lease`) */
function mayLease(
  lease: QueueLease | undefined,
  holder: QueueLeaseHolder,
  over?: true | QueueLeaseHolder,
): QueueLeaseAnswer {
  if (!lease || lease.tab === holder.tab || (!!over && over !== true && lease.tab === over.tab && lease.hold === over.hold)) {
    return true;
  }
  return over === true || lease.until <= Date.now() ? "taken" : false;
}

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
      records.delete(LEASE_KEY);
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

    async lease(holder, until, over) {
      // a read and a write in one transaction: of two tabs asking at once, one takes it and the other is told
      const db = await open();
      const transaction = db.transaction([RECORDS], "readwrite");
      const done = transactionDone(transaction);
      const records = transaction.objectStore(RECORDS);
      let held: QueueLeaseAnswer = false;
      const request = records.get(LEASE_KEY);
      request.onsuccess = () => {
        held = mayLease(request.result as QueueLease | undefined, holder, over);
        if (held) {
          records.put({ tab: holder.tab, hold: holder.hold, until } satisfies QueueLease, LEASE_KEY);
        }
      };
      await done;
      return held;
    },

    async releaseLease(tab) {
      if (closed) {
        return;
      }
      const db = await open();
      const transaction = db.transaction([RECORDS], "readwrite");
      const done = transactionDone(transaction);
      const records = transaction.objectStore(RECORDS);
      const request = records.get(LEASE_KEY);
      request.onsuccess = () => {
        if ((request.result as QueueLease | undefined)?.tab === tab) {
          records.delete(LEASE_KEY);
        }
      };
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
        // forgotten for good: nothing from this tab opens it again (a lease renewed, a late write) and creates it anew
        closed = true;
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
  /** The queue's lease */
  leased: QueueLease | undefined;
} {
  const store = {
    records: new Map<string, unknown>(),
    photos: new Map<string, Blob>(),
    owner: undefined as string | undefined,
    leased: undefined as QueueLease | undefined,
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
    lease(holder: QueueLeaseHolder, until: number, over?: true | QueueLeaseHolder): Promise<QueueLeaseAnswer> {
      const held = mayLease(store.leased, holder, over);
      if (held) {
        store.leased = { tab: holder.tab, hold: holder.hold, until };
      }
      return Promise.resolve(held);
    },
    releaseLease(tab: string): Promise<void> {
      if (store.leased?.tab === tab) {
        store.leased = undefined;
      }
      return Promise.resolve();
    },
    clear(): Promise<void> {
      store.records.clear();
      store.photos.clear();
      store.owner = undefined;
      store.leased = undefined;
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
  /**
   * This tab keeps the queue now. `forced`: it took the queue from a tab that didn't let it go (stolen from it, or its
   * lease ran out), which may still be open, frozen in the background; otherwise that tab let it go, or closed.
   */
  granted: (forced: boolean) => void;
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
  /** Whether this tab still keeps the queue, as far as the lock can tell now (asked before each upload) */
  stillHeld: () => Promise<boolean>;
}

/** The queue as a Web Lock: the browser hands it to the next tab in line when the tab keeping it lets go or closes */
export function webLocksQueueLock(locks: LockManager, name: string, events: QueueLockEvents): QueueLock {
  let stopped = false;
  /** This tab keeps the queue (the browser holds the lock for it, or refused Web Locks) */
  let holding = false;
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
        holding = true;
        events.granted(!!options.steal);
      });
    }).catch(() => {
      // the queue was taken over while this tab kept it (a wait given up changes nothing)
      if (mine && letGo === mine && !stopped) {
        letGo = null;
        holding = false;
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
  }, () => {
    holding = true;
    events.granted(false);
  });

  return {
    yield() {
      const go = letGo;
      if (go && !stopped) {
        letGo = null;
        holding = false;
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
      holding = false;
      cancelWait?.();
      cancelWait = null;
      const go = letGo;
      letGo = null;
      go?.();
    },
    // the browser says when the lock is taken over (`lost`); a tab frozen meanwhile finds out from the stored claim
    stillHeld: () => Promise.resolve(holding),
  };
}

/** How long the queue's lease lasts unless it's renewed: longer than a background tab's timers wait (once a minute) */
export const QUEUE_LEASE_MS = 90_000;
/** How often the tab keeping the queue renews its lease, and a tab waiting for it asks for it again */
export const QUEUE_RENEW_MS = 5000;
/** A tab that let the queue go to the tab that asked for it doesn't take it back for this long */
export const QUEUE_YIELD_MS = 3000;

interface QueueLeaseMessage {
  /** "free": the lease was let go; "taken": a tab took it over */
  type: "free" | "taken";
  tab: string;
  /** With "free": the hold let go of (`QueueLeaseHolder`); only that one is taken over on its word */
  hold?: number;
}

/**
 * The sessionStorage key where a document unloaded while holding the lease leaves its hold for the next document of the
 * same tab (a reload, a full navigation): nobody else hears its "free", and its own release of the lease doesn't finish
 * while the page unloads
 */
export function queueLeaseHandOverKey(name: string): string {
  return `${name}-lease-tab`;
}

/** This tab's sessionStorage; null where there is none, or it can't be used (it throws in some sandboxed frames) */
function tabSessionStorage(): Storage | null {
  try {
    return typeof sessionStorage === "undefined" ? null : sessionStorage;
  }
  catch {
    return null;
  }
}

/** The hold a document of this tab left when it was unloaded, read once (a duplicated tab copies sessionStorage) */
function takeLeftHold(key: string): QueueLeaseHolder | undefined {
  const session = tabSessionStorage();
  try {
    const stored = session?.getItem(key) ?? null;
    session?.removeItem(key);
    const holder = stored ? (JSON.parse(stored) as Partial<QueueLeaseHolder>) : null;
    return holder && typeof holder.tab === "string" && typeof holder.hold === "number"
      ? { tab: holder.tab, hold: holder.hold }
      : undefined;
  }
  catch {
    return undefined;
  }
}

/**
 * The queue without Web Locks (a server reached over plain http has none): a lease in the queue's database
 * (`UploadStorage.lease`), which a tab takes in one transaction, so of two tabs asking at once only one gets it. The
 * tab keeping the queue renews it every `QUEUE_RENEW_MS`; the others ask as often, and get it once it's let go or has
 * run out (a tab closed, or frozen in the background, for `QUEUE_LEASE_MS`). The tabs say "free" and "taken" over a
 * BroadcastChannel, so a waiting tab asks at once and a tab whose lease was taken over stops at once. A reloaded page
 * takes its previous document's lease at once (`queueLeaseHandOverKey`).
 */
export function leaseQueueLock(
  storage: Pick<UploadStorage, "lease" | "releaseLease">,
  name: string,
  events: QueueLockEvents,
  tab: string,
  makeChannel: ((name: string) => BroadcastChannel) | null = typeof BroadcastChannel === "function"
    ? channelName => new BroadcastChannel(channelName)
    : null,
): QueueLock {
  let state: "asking" | "waiting" | "holding" | "stopped" = "asking";
  /** After letting the queue go, this tab doesn't ask for it again on its own before this time */
  let yieldedUntil = 0;
  /**
   * Bumped when this tab lets the queue go, takes it by force or stops: a lease request asked for before that (a renewal
   * in flight) changes nothing, so a tab that just let the queue go isn't granted it again by its own late renewal
   */
  let epoch = 0;
  /** This tab's hold of the lease: the one its last lease write named (renewals keep it, a new take is the next one) */
  let hold = 0;
  /** Lease requests run one after another */
  let chain: Promise<unknown> = Promise.resolve();
  let channel: BroadcastChannel | null = null;
  try {
    channel = makeChannel?.(`${name}-lease`) ?? null;
  }
  catch {
    channel = null;
  }
  const handOverKey = queueLeaseHandOverKey(name);

  /** Read again after an await: `release` may have run meanwhile */
  const stopped = () => state === "stopped";

  const post = (message: Omit<QueueLeaseMessage, "tab">) => {
    try {
      channel?.postMessage({ ...message, tab } satisfies QueueLeaseMessage);
    }
    catch {
      // closed
    }
  };

  /** Lets the lease go, and says so, naming the hold let go of (`released`: the hold when the tab decided to) */
  function letGo(released = hold) {
    return storage.releaseLease(tab).catch(() => undefined).then(() => post({ type: "free", hold: released }));
  }

  /**
   * Takes or renews the lease (`over`: takes it over, from any tab, or from the hold of the tab that said it let go of
   * it), and tells the tab what changed. A request asked for before the tab let the queue go, took it by force or
   * stopped (`epoch`) neither grants nor loses anything.
   */
  function attempt(over?: true | QueueLeaseHolder): Promise<void> {
    const asked = epoch;
    const run = chain.then(async () => {
      if (state === "stopped" || epoch !== asked) {
        return;
      }
      // a renewal keeps this tab's hold; a take is a new one
      const writing = state === "holding" ? hold : hold + 1;
      let held: QueueLeaseAnswer;
      try {
        held = await storage.lease({ tab, hold: writing }, Date.now() + QUEUE_LEASE_MS, over);
      }
      catch {
        // the database can't say (it's full, or a logout elsewhere deleted it): a tab keeping the queue keeps it, and a
        // tab just asking keeps it too, as before there were tabs (its writes fail the same way)
        held = state !== "waiting";
      }
      if (held) {
        hold = writing;
      }
      if (stopped()) {
        // released meanwhile
        if (held) {
          await letGo();
        }
        return;
      }
      if (epoch !== asked) {
        // let go of meanwhile: a lease this request took or renewed goes with the release chained after it
        return;
      }
      if (held && state !== "holding") {
        state = "holding";
        if (over === true) {
          post({ type: "taken" });
        }
        events.granted(held === "taken");
      }
      else if (!held && state === "holding") {
        state = "waiting";
        events.lost();
      }
      else if (!held && state === "asking") {
        state = "waiting";
        events.waiting();
      }
    });
    chain = run.catch(() => undefined);
    return run;
  }

  if (channel) {
    channel.onmessage = (event: MessageEvent<QueueLeaseMessage>) => {
      const message = event.data;
      if (!message || message.tab === tab) {
        return;
      }
      if (message.type === "free" && state === "waiting") {
        // a tab closing can't be relied on to let its lease go in time (an IndexedDB write while the page unloads), so
        // its word is enough: the hold it let go of is taken from it (a later one of that tab isn't)
        void attempt({ tab: message.tab, hold: message.hold as number });
      }
      else if (message.type === "taken" && state === "holding") {
        void attempt();
      }
    };
  }

  const renew = setInterval(() => {
    // a tab that just let the queue go leaves it to the tab that asked for it a moment
    if (state === "holding" || (state === "waiting" && Date.now() >= yieldedUntil)) {
      void attempt();
    }
  }, QUEUE_RENEW_MS);

  // a tab that closes lets the queue go at once, rather than when its lease runs out: it says so (a message goes out
  // even as the page unloads) and lets the lease go if it can. Neither reaches the next document of the same tab (a
  // reload), so the hold is left for it in sessionStorage too. A page kept in the back-forward cache finds out at its
  // next renewal whether another tab took it meanwhile, and holds anew if not, so the "free" it said can't take that over.
  const onPageHide = () => {
    if (state === "holding") {
      const released = hold;
      post({ type: "free", hold: released });
      try {
        tabSessionStorage()?.setItem(handOverKey, JSON.stringify({ tab, hold: released } satisfies QueueLeaseHolder));
      }
      catch {
        // full or refused: the next document waits for the lease to run out, as another tab would
      }
      chain = chain.then(() => letGo(released));
    }
  };
  const onPageShow = (event: PageTransitionEvent) => {
    if (!event.persisted) {
      return;
    }
    // back from the back-forward cache: this page holds anew, and takes the hold the page it replaced in this tab left
    // (that one is in the cache now, and finds out before it sends anything)
    const left = takeLeftHold(handOverKey);
    if (state === "holding") {
      hold += 1;
    }
    if (left) {
      void attempt(left);
    }
  };
  if (typeof window !== "undefined") {
    window.addEventListener("pagehide", onPageHide);
    window.addEventListener("pageshow", onPageShow);
  }

  // the lease of the document this one replaced in the same tab (a reload): it can't let it go itself
  void attempt(takeLeftHold(handOverKey));

  return {
    yield() {
      if (state === "holding") {
        state = "waiting";
        epoch += 1;
        // the tab that asked for it takes it before this one asks again
        yieldedUntil = Date.now() + QUEUE_YIELD_MS;
        const released = hold;
        chain = chain.then(() => letGo(released));
      }
    },
    steal() {
      if (state === "holding" || state === "stopped") {
        return;
      }
      epoch += 1;
      void attempt(true);
    },
    release() {
      const holding = state === "holding";
      state = "stopped";
      epoch += 1;
      clearInterval(renew);
      if (typeof window !== "undefined") {
        window.removeEventListener("pagehide", onPageHide);
        window.removeEventListener("pageshow", onPageShow);
      }
      const closing = holding ? chain.then(() => letGo()) : chain;
      void closing.finally(() => channel?.close());
    },
    stillHeld() {
      if (state !== "holding") {
        return Promise.resolve(false);
      }
      // renewed in the same transaction that checks it: a tab whose lease was taken over (it was frozen while its lease
      // ran out) finds out here, before it sends anything
      return attempt().then(() => state === "holding");
    },
  };
}

/** Nothing to agree on (no Web Locks, no storage to hold a lease): the tab keeps the queue (as before there were two) */
function soleQueueLock(events: QueueLockEvents): QueueLock {
  events.granted(false);
  return { yield() {}, steal() {}, release() {}, stillHeld: () => Promise.resolve(true) };
}

/**
 * The user's queue among this browser's tabs: a Web Lock where there is one, else a lease in the queue's storage, else
 * this tab
 */
export function openQueueLock(
  name: string,
  events: QueueLockEvents,
  tab: string,
  storage: Pick<UploadStorage, "lease" | "releaseLease"> | null = null,
): QueueLock {
  const locks = typeof navigator !== "undefined" ? navigator.locks : undefined;
  if (locks && typeof locks.request === "function") {
    return webLocksQueueLock(locks, name, events);
  }
  if (storage) {
    return leaseQueueLock(storage, name, events, tab);
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
