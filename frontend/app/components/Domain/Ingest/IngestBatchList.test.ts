import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import IngestBatchList from "./IngestBatchList.vue";
import BaseDialog from "~/components/global/BaseDialog.vue";
import {
  leaveRecipeIngestCommitNotice,
  resetRecipeIngestCounts,
  resetRecipeIngestReviewState,
  resetRecipeIngestSettings,
  takeRecipeIngestCommitNotice,
  useRecipeIngestSettings,
} from "~/composables/use-recipe-ingest";
import { takeCarriedReviewNotice } from "~/composables/use-recipe-ingest-review";
import { resetRecipeIngestUploads, useRecipeIngestUploads } from "~/composables/use-recipe-ingest-uploads";
import type { RecipeIngestJobsQuery } from "~/lib/api/user/recipe-ingest";
import type { RecipeIngestionJobSummary } from "~/lib/api/types/recipe-ingest";

const api = vi.hoisted(() => ({
  getJobs: vi.fn(),
  getJob: vi.fn(),
  getSettings: vi.fn(),
  readWithCloud: vi.fn(),
  getJobState: vi.fn(),
  commitClean: vi.fn(),
  getCounts: vi.fn(),
  retry: vi.fn(),
  cancel: vi.fn(),
  discard: vi.fn(),
  uncommit: vi.fn(),
  upload: vi.fn(),
  createBatch: vi.fn(),
  sealBatch: vi.fn(),
}));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
const router = vi.hoisted(() => ({ push: vi.fn() }));

vi.mock("~/composables/api", () => ({
  useUserApi: () => ({ recipeIngest: api }),
}));
vi.mock("~/composables/use-toast", () => ({
  alert: toast,
}));

const slot = (tag = "div", className = "") => ({ template: `<${tag} class="${className}"><slot /></${tag}>` });

const stubs = {
  VListItem: {
    template: "<div class=\"list-item\"><slot name=\"prepend\" /><slot /><slot name=\"append\" /></div>",
  },
  VListItemTitle: slot("h5"),
  VList: slot(),
  VAvatar: slot("span"),
  VImg: { props: ["src"], template: "<img :src=\"src\">" },
  VIcon: { template: "<i />" },
  VChip: { template: "<span class=\"chip\"><slot /></span>" },
  VProgressCircular: { template: "<span class=\"spinner\" />" },
  VProgressLinear: { template: "<progress />" },
  VSpacer: slot(),
  VAlert: {
    props: ["type"],
    template: "<div class=\"alert\" :data-type=\"type\"><slot /><slot name=\"append\" /></div>",
  },
  VCardText: slot(),
  VBtn: {
    props: ["to", "loading"],
    template: "<a v-if=\"to\" class=\"btn\" :href=\"to\"><slot /></a><button v-else type=\"button\" class=\"btn\"><slot /></button>",
  },
  NuxtLink: { props: ["to"], template: "<a :href=\"to\"><slot /></a>" },
  BaseDialog: {
    props: ["modelValue", "title"],
    emits: ["confirm", "update:modelValue"],
    template: `
      <div v-if="modelValue" class="confirm-dialog" :data-title="title">
        <slot />
        <button type="button" class="dialog-confirm" @click="$emit('confirm'); $emit('update:modelValue', false)">OK</button>
      </div>
    `,
  },
  VCardTitle: slot(),
};

const HOUR = 60 * 60 * 1000;
const DAY = 24 * HOUR;

function ago(ms: number) {
  return new Date(Date.now() - ms).toISOString();
}

function job(overrides: Partial<RecipeIngestionJobSummary> = {}): RecipeIngestionJobSummary {
  return {
    id: "j1",
    batchId: "b1",
    position: 0,
    status: "ready",
    source: "app",
    title: "Banana Mug Cake",
    pageCount: 1,
    thumbUrl: null,
    draftVersion: 1,
    errorCount: 0,
    warningCount: 0,
    task: null,
    error: null,
    recipe: null,
    localOnly: false,
    createdAt: ago(HOUR),
    // an added card was added half an hour ago, unless it says otherwise
    committedAt: overrides.status === "committed" ? ago(HOUR / 2) : null,
    ...overrides,
  };
}

/** The server's jobs; GET /jobs filters, orders and pages them like the real route */
let serverJobs: RecipeIngestionJobSummary[] = [];

/** One page of a list, as GET /jobs answers it; `perPage` -1 is all of it */
function page(items: RecipeIngestionJobSummary[], number = 1, perPage = -1) {
  if (perPage < 0) {
    return { data: { page: 1, per_page: -1, total: items.length, total_pages: 1, items }, error: null };
  }
  const totalPages = Math.max(1, Math.ceil(items.length / perPage));
  const pageItems = items.slice((number - 1) * perPage, number * perPage);
  return { data: { page: number, per_page: perPage, total: items.length, total_pages: totalPages, items: pageItems }, error: null };
}

function msOf(value: string | null | undefined) {
  return value ? new Date(value).getTime() : 0;
}

/** GET /jobs over `jobs`: status, batch and `committedSince` filters; newest card first, or latest added first */
function listJobs(jobs: RecipeIngestionJobSummary[], query: RecipeIngestJobsQuery) {
  const statuses = query.status ? [query.status].flat() : null;
  const since = query.committedSince ? new Date(query.committedSince).getTime() : null;
  const items = jobs.filter(j => (!statuses || statuses.includes(j.status))
    && (!query.batchId || j.batchId === query.batchId)
    && (since === null || (!!j.committedAt && msOf(j.committedAt) >= since)));
  if (query.orderBy === "committedAt") {
    items.sort((a, b) => msOf(b.committedAt) - msOf(a.committedAt));
  }
  return page(items, query.page ?? 1, query.perPage ?? 50);
}

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

function jobsCalls(): RecipeIngestJobsQuery[] {
  return api.getJobs.mock.calls.map(call => call[0] as RecipeIngestJobsQuery);
}

const wrappers: VueWrapper[] = [];

/**
 * The real BaseDialog, on an overlay that hears every key pressed inside it as Vuetify's does, with its Cancel and
 * Confirm buttons: what Enter does there is the dialog's to say
 */
const realDialog = {
  BaseDialog,
  VDialog: { props: ["modelValue"], template: "<div v-if=\"modelValue\" class=\"overlay\"><slot /></div>" },
  VBottomSheet: { props: ["modelValue"], template: "<div v-if=\"modelValue\" class=\"overlay\"><slot /></div>" },
  BaseDialogContent: {
    props: ["title", "canConfirm"],
    emits: ["cancel", "confirm"],
    template: `
      <div class="confirm-dialog" :data-title="title">
        <slot />
        <button type="button" class="dialog-cancel" @click="$emit('cancel')">Cancel</button>
        <button v-if="canConfirm" type="button" class="dialog-confirm" @click="$emit('confirm')">OK</button>
      </div>
    `,
  },
};

async function mountList(props: { batchId?: string | null } = {}, dialogStubs: Record<string, unknown> = {}) {
  const wrapper = mount(IngestBatchList, {
    props: { groupSlug: "home", ...props },
    global: {
      mocks: { $globals: { icons: { lock: "lock", delete: "delete", alertCircle: "alert" } }, $vuetify: { display: { xs: false } } },
      stubs: { ...stubs, ...dialogStubs },
    },
  });
  wrappers.push(wrapper);
  await flushPromises();
  return wrapper;
}

function batchSections(wrapper: VueWrapper) {
  return wrapper.findAll(".ingest-batch");
}

function rowTitles(wrapper: VueWrapper, selector = ".ingest-batch") {
  return wrapper.findAll(`${selector} .job-title`).map(title => title.text());
}

function setVisibility(state: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", { value: state, configurable: true });
  document.dispatchEvent(new Event("visibilitychange"));
}

beforeEach(() => {
  vi.clearAllMocks();
  resetRecipeIngestUploads();
  resetRecipeIngestCounts();
  resetRecipeIngestReviewState();
  resetRecipeIngestSettings();
  setVisibility("visible");
  vi.stubGlobal("useRouter", () => router);
  serverJobs = [];
  api.getJobs.mockImplementation(async (query: RecipeIngestJobsQuery) => listJobs(serverJobs, query));
  api.getCounts.mockResolvedValue({ data: { processing: 0, ready: 1, needsAttention: 0, failed: 0 }, error: null });
  api.getJobState.mockImplementation(async (id: string) => {
    const found = serverJobs.find(j => j.id === id);
    return found
      ? { data: { draftVersion: found.draftVersion, status: found.status, task: found.task ?? null, error: found.error ?? null }, error: null }
      : { data: null, error: { response: { status: 404, data: { detail: { code: "not_found" } } } } };
  });
  api.createBatch.mockResolvedValue({ data: { id: "b3", source: "app" }, error: null });
});

afterEach(() => {
  wrappers.forEach(wrapper => wrapper.unmount());
  wrappers.length = 0;
  resetRecipeIngestUploads();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("IngestBatchList", () => {
  test("batches newest first, cards in capture order, with each batch's Review and Retry failed", async () => {
    // a batch's cards share its source, and these two arrived together (one timestamp, so the test can't flake)
    const arrived = ago(5 * HOUR);
    serverJobs = [
      job({ id: "a2", batchId: "older", position: 1, title: "Pancakes", createdAt: arrived, source: "inbox" }),
      job({ id: "a1", batchId: "older", position: 0, title: "Waffles", createdAt: arrived, source: "inbox" }),
      job({ id: "b2", batchId: "newer", position: 1, status: "failed", title: null, error: { code: "no_recipe_found" } }),
      job({ id: "b1", batchId: "newer", position: 0, status: "processing", title: null, task: { kind: "extract", state: "queued" } }),
    ];
    const wrapper = await mountList();

    expect(batchSections(wrapper).map(section => section.attributes("data-batch"))).toEqual(["newer", "older"]);
    const [newer, older] = batchSections(wrapper);
    expect(newer!.findAll(".job-status").map(chip => chip.text())).toEqual(["Waiting to be read", "Failed"]);
    expect(newer!.get(".job-caption").text()).toBe("No recipe was found on this card.");
    expect(newer!.get(".batch-meta").text()).toBe("2 cards · Scanned in the app");
    expect(newer!.find(".batch-review").exists()).toBe(false);
    expect(newer!.find(".batch-retry-failed").exists()).toBe(true);

    expect(older!.findAll(".job-title").map(title => title.text())).toEqual(["Waffles", "Pancakes"]);
    expect(older!.get(".batch-review").attributes("href")).toBe("/g/home/recipes/cards/review?batch=older");
    expect(older!.get(".batch-meta").text()).toBe("2 cards · From the inbox");
    expect(older!.get(".batch-title").text()).toMatch(/^Batch of .+/);
    expect(jobsCalls()).toContainEqual(expect.objectContaining({ status: ["processing", "ready", "failed", "committing"] }));
  });

  test("cards added in the last 7 days link to their recipes", async () => {
    serverJobs = [
      job({ id: "c1", status: "committed", title: "Banana Mug Cake", recipe: { id: "r1", slug: "banana-mug-cake" } }),
      job({ id: "c0", status: "committed", title: "Old Cake", createdAt: ago(9 * DAY), committedAt: ago(8 * DAY), recipe: { id: "r0", slug: "old" } }),
    ];
    const wrapper = await mountList();

    expect(batchSections(wrapper)).toHaveLength(0);
    expect(rowTitles(wrapper, ".recently-added")).toEqual(["Banana Mug Cake"]);
    expect(wrapper.get(".recently-added .job-view-recipe").attributes("href")).toBe("/g/home/r/banana-mug-cake");
    expect(wrapper.find(".no-cards").exists()).toBe(false);
  });

  test("the last 7 days go by when a card was added, latest first: one uploaded earlier and added today is there", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-04T12:00:00Z"));
    serverJobs = [
      // uploaded 8 days ago, added an hour ago
      job({ id: "late", status: "committed", title: "Scones", createdAt: ago(8 * DAY), committedAt: ago(HOUR), recipe: { id: "r1", slug: "scones" } }),
      // uploaded today, added before it
      job({ id: "early", status: "committed", title: "Fudge", createdAt: ago(3 * HOUR), committedAt: ago(2 * HOUR), recipe: { id: "r2", slug: "fudge" } }),
      // uploaded and added 10 days ago
      job({ id: "old", status: "committed", title: "Old Cake", createdAt: ago(10 * DAY), committedAt: ago(10 * DAY), recipe: { id: "r3", slug: "old" } }),
    ];
    const wrapper = await mountList();

    expect(rowTitles(wrapper, ".recently-added")).toEqual(["Scones", "Fudge"]);
    const query = jobsCalls().find(call => call.status === "committed")!;
    expect(query.orderBy).toBe("committedAt");
    expect((query.committedSince as Date).toISOString()).toBe("2026-09-27T12:00:00.000Z");
  });

  test("a card of the list that's added while it's open moves to the added cards by when it was added", async () => {
    vi.useFakeTimers();
    serverJobs = [
      job({ id: "p1", status: "processing", createdAt: ago(8 * DAY), title: null, task: { kind: "extract", state: "running" } }),
      job({ id: "c1", position: 1, status: "committed", title: "Fudge", committedAt: ago(2 * HOUR), recipe: { id: "r1", slug: "fudge" } }),
    ];
    const wrapper = await mountList();
    expect(rowTitles(wrapper, ".recently-added")).toEqual(["Fudge"]);

    // read and added since (an older upload): it's polled with its batch, and leads the added cards
    serverJobs = serverJobs.map(j => (j.id === "p1"
      ? { ...j, status: "committed", task: null, title: "Scones", committedAt: ago(1000), recipe: { id: "r2", slug: "scones" } }
      : j));
    await vi.advanceTimersByTimeAsync(3000);
    expect(rowTitles(wrapper, ".recently-added")).toEqual(["Scones", "Fudge"]);
  });

  test("more open cards than a load fetches: says so, and Load older cards fetches the next ones and keeps them", async () => {
    // 12 pages of open cards (one card a page here); a load fetches the first 10
    const open = Array.from({ length: 12 }, (_, i) => job({
      id: `o${i}`,
      batchId: `b${i}`,
      title: `Card ${i}`,
      createdAt: ago((i + 1) * HOUR),
    }));
    api.getJobs.mockImplementation(async (query: RecipeIngestJobsQuery) => {
      if (query.status !== "committed" && !query.batchId) {
        const number = query.page ?? 1;
        return { data: { page: number, per_page: 100, total: 1200, total_pages: 12, items: open.slice(number - 1, number) }, error: null };
      }
      return listJobs([], query);
    });
    const wrapper = await mountList();

    expect(batchSections(wrapper)).toHaveLength(10);
    expect(jobsCalls().filter(call => call.status !== "committed").map(call => call.page)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
    expect(wrapper.get(".load-older-open").text()).toContain("Showing the newest 10 cards.");

    api.getJobs.mockClear();
    await wrapper.get(".load-older-open .load-older").trigger("click");
    await flushPromises();
    expect(batchSections(wrapper)).toHaveLength(12);
    expect(rowTitles(wrapper).at(-1)).toBe("Card 11");
    expect(wrapper.find(".load-older-open").exists()).toBe(false);
    // the whole list again, now 20 pages at most: it stops at the last one
    expect(jobsCalls().filter(call => call.status !== "committed").map(call => call.page)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]);

    // a reload (coming back to the page) keeps the older cards
    setVisibility("hidden");
    setVisibility("visible");
    await flushPromises();
    expect(batchSections(wrapper)).toHaveLength(12);
  });

  test("more cards added this week than a load fetches: Load older cards fetches the next ones", async () => {
    serverJobs = Array.from({ length: 60 }, (_, i) => job({
      id: `c${i}`,
      batchId: `b${i}`,
      status: "committed",
      title: `Recipe ${i}`,
      committedAt: ago((i + 1) * 60_000),
      recipe: { id: `r${i}`, slug: `recipe-${i}` },
    }));
    const wrapper = await mountList();

    expect(rowTitles(wrapper, ".recently-added")).toHaveLength(50);
    expect(rowTitles(wrapper, ".recently-added")[0]).toBe("Recipe 0");
    await wrapper.get(".load-older-recent").trigger("click");
    await flushPromises();
    expect(rowTitles(wrapper, ".recently-added")).toHaveLength(60);
    expect(rowTitles(wrapper, ".recently-added").at(-1)).toBe("Recipe 59");
    expect(wrapper.find(".load-older-recent").exists()).toBe(false);
  });

  test("a batch shows whole: all of its cards in one request", async () => {
    serverJobs = Array.from({ length: 150 }, (_, i) => job({ id: `j${i}`, position: i, title: `Card ${i}` }));
    const wrapper = await mountList({ batchId: "b1" });

    expect(jobsCalls()).toEqual([{ batchId: "b1", perPage: -1 }]);
    expect(rowTitles(wrapper)).toHaveLength(150);
    expect(wrapper.find(".load-older-open").exists()).toBe(false);
  });

  test("says when there are no cards", async () => {
    const wrapper = await mountList();
    expect(wrapper.get(".no-cards").text()).toBe("No recipe cards yet. Scan some above.");
  });

  test("a failed load isn't shown as no cards, and can be retried", async () => {
    api.getJobs.mockResolvedValueOnce({ data: null, error: { message: "Network Error" } });
    serverJobs = [job()];
    const wrapper = await mountList();

    expect(wrapper.get(".load-failed").text()).toContain("Couldn't load the cards");
    expect(wrapper.find(".no-cards").exists()).toBe(false);

    await wrapper.get(".load-failed button").trigger("click");
    await flushPromises();
    expect(wrapper.find(".load-failed").exists()).toBe(false);
    expect(rowTitles(wrapper)).toEqual(["Banana Mug Cake"]);
  });

  test("polls the batch being read every 3 s, and stops once it's read", async () => {
    vi.useFakeTimers();
    const added = job({ id: "c1", position: 1, status: "committed", recipe: { id: "r1", slug: "banana-mug-cake" } });
    serverJobs = [
      job({ status: "processing", task: { kind: "extract", state: "running", progressKey: "recipe-ingest.progress.reading-card" } }),
      added,
    ];
    const wrapper = await mountList();
    expect(wrapper.get(".ingest-batch .job-status").text()).toBe("Reading");
    expect(wrapper.get(".ingest-batch .job-caption").text()).toBe("Reading the card");
    // Loading the list refreshes the sidebar's count
    expect(api.getCounts).toHaveBeenCalledOnce();
    api.getJobs.mockClear();
    api.getCounts.mockClear();

    await vi.advanceTimersByTimeAsync(2999);
    expect(api.getJobs).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(jobsCalls()).toEqual([{ batchId: "b1", perPage: -1 }]);
    expect(api.getCounts).not.toHaveBeenCalled();

    serverJobs = [job({ status: "ready", warningCount: 2 }), added];
    await vi.advanceTimersByTimeAsync(3000);
    expect(wrapper.get(".ingest-batch .job-status").text()).toBe("Ready · 2 to check");
    // The sidebar's count follows the card that became ready
    expect(api.getCounts).toHaveBeenCalledOnce();

    api.getJobs.mockClear();
    await vi.advanceTimersByTimeAsync(30_000);
    expect(api.getJobs).not.toHaveBeenCalled();
  });

  test("nothing being read: no polling", async () => {
    vi.useFakeTimers();
    serverJobs = [job()];
    await mountList();
    api.getJobs.mockClear();
    await vi.advanceTimersByTimeAsync(30_000);
    expect(api.getJobs).not.toHaveBeenCalled();
  });

  test("doesn't poll while the page is hidden, and catches up when it's back", async () => {
    vi.useFakeTimers();
    serverJobs = [job({ status: "processing", task: { kind: "extract", state: "running" } })];
    await mountList();
    api.getJobs.mockClear();

    setVisibility("hidden");
    await vi.advanceTimersByTimeAsync(30_000);
    expect(api.getJobs).not.toHaveBeenCalled();

    // back on the page: the whole list once, then the batch being read every 3 s
    setVisibility("visible");
    await vi.advanceTimersByTimeAsync(0);
    expect(jobsCalls()).toEqual([
      { status: ["processing", "ready", "failed", "committing"], page: 1, perPage: 100 },
      { status: "committed", committedSince: expect.any(Date), orderBy: "committedAt", page: 1, perPage: 50 },
    ]);
    api.getJobs.mockClear();
    await vi.advanceTimersByTimeAsync(3000);
    expect(jobsCalls()).toEqual([{ batchId: "b1", perPage: -1 }]);
  });

  test("an upload shows up at once", async () => {
    api.upload.mockResolvedValue({
      data: { batchId: "b3", jobs: [{ id: "n1", status: "processing", pageCount: 1, reviewPath: "" }], rejected: [], summary: "" },
      error: null,
    });
    const wrapper = await mountList();
    expect(wrapper.find(".no-cards").exists()).toBe(true);
    api.getJobs.mockClear();

    serverJobs = [job({ id: "n1", batchId: "b3", status: "processing", title: null, task: { kind: "extract", state: "queued" } })];
    useRecipeIngestUploads().takePhoto(new File(["x"], "IMG_9.jpg", { type: "image/jpeg" }));
    await flushPromises();

    expect(jobsCalls()).toContainEqual({ batchId: "b3", perPage: -1 });
    expect(wrapper.get(".ingest-batch").attributes("data-batch")).toBe("b3");
    expect(wrapper.get(".job-status").text()).toBe("Waiting to be read");
  });

  test("Retry queues a failed card again", async () => {
    serverJobs = [job({ status: "failed", error: { code: "timeout" } })];
    const wrapper = await mountList();
    expect(wrapper.get(".job-status").text()).toBe("Failed");
    expect(wrapper.get(".job-caption").text()).toBe("Reading this card took too long. Try again.");

    const answered = deferred();
    api.retry.mockImplementation(async (id: string) => {
      const task = { kind: "extract", state: "queued" } as const;
      serverJobs = serverJobs.map(j => (j.id === id ? { ...j, status: "processing", task, error: null } : j));
      await answered.promise;
      return { data: { draftVersion: 0, status: "processing", task }, error: null };
    });
    await wrapper.get(".job-retry").trigger("click");
    expect(api.retry).toHaveBeenCalledExactlyOnceWith("j1");
    expect(wrapper.findComponent({ name: "IngestJobListItem" }).props("busy")).toBe(true);

    answered.resolve();
    await flushPromises();
    expect(wrapper.get(".job-status").text()).toBe("Waiting to be read");
    expect(wrapper.findComponent({ name: "IngestJobListItem" }).props("busy")).toBe(false);
    // ...and it's watched until it's read
    expect(jobsCalls()).toContainEqual({ batchId: "b1", perPage: -1 });
  });

  test("Retry failed retries every failed card of the batch", async () => {
    serverJobs = [
      job({ id: "f1", status: "failed", error: { code: "timeout" } }),
      job({ id: "ok", position: 1 }),
      job({ id: "f2", position: 2, status: "failed", error: { code: "interrupted" } }),
    ];
    api.retry.mockResolvedValue({ data: { draftVersion: 0, status: "processing", task: { kind: "extract", state: "queued" } }, error: null });
    const wrapper = await mountList();

    await wrapper.get(".batch-retry-failed").trigger("click");
    await flushPromises();
    expect(api.retry.mock.calls.map(call => call[0]).sort()).toEqual(["f1", "f2"]);
  });

  test("Discard asks first, then removes the card", async () => {
    serverJobs = [job(), job({ id: "j2", position: 1, title: "Pancakes" })];
    api.discard.mockResolvedValue({ data: null, error: null });
    const wrapper = await mountList();

    await wrapper.findAll(".job-discard")[0]!.trigger("click");
    expect(api.discard).not.toHaveBeenCalled();
    expect(wrapper.get(".confirm-dialog").text()).toContain("Discard this card? Its photos are deleted.");

    await wrapper.get(".dialog-confirm").trigger("click");
    await flushPromises();
    expect(api.discard).toHaveBeenCalledExactlyOnceWith("j1");
    expect(rowTitles(wrapper)).toEqual(["Pancakes"]);
    expect(toast.success).toHaveBeenCalledWith("Card discarded");
  });

  test("Enter on the Discard question's Cancel doesn't discard the card", async () => {
    serverJobs = [job(), job({ id: "j2", position: 1, title: "Pancakes" })];
    api.discard.mockResolvedValue({ data: null, error: null });
    const wrapper = await mountList({}, realDialog);

    await wrapper.findAll(".job-discard")[0]!.trigger("click");
    // a keyboard user on Cancel presses Enter (the browser then clicks Cancel)
    await wrapper.get(".confirm-dialog .dialog-cancel").trigger("keydown", { key: "Enter" });
    await flushPromises();
    expect(api.discard).not.toHaveBeenCalled();
    await wrapper.get(".confirm-dialog .dialog-cancel").trigger("click");
    expect(wrapper.find(".confirm-dialog").exists()).toBe(false);

    // OK still discards
    await wrapper.findAll(".job-discard")[0]!.trigger("click");
    await wrapper.get(".confirm-dialog .dialog-confirm").trigger("click");
    await flushPromises();
    expect(api.discard).toHaveBeenCalledExactlyOnceWith("j1");
  });

  test("a refused discard says why", async () => {
    serverJobs = [job()];
    api.discard.mockResolvedValue({ data: null, error: { response: { status: 403, data: { detail: { code: "forbidden" } } } } });
    const wrapper = await mountList();

    await wrapper.get(".job-discard").trigger("click");
    await wrapper.get(".dialog-confirm").trigger("click");
    await flushPromises();
    expect(toast.error).toHaveBeenCalledExactlyOnceWith("You don't have permission to do this with this card.");
    expect(rowTitles(wrapper)).toEqual(["Banana Mug Cake"]);
  });

  test("one batch, after its review: what was added and what's left", async () => {
    serverJobs = [
      job({ id: "c1", status: "committed", title: "Banana Mug Cake", recipe: { id: "r1", slug: "banana-mug-cake" } }),
      job({ id: "r2", position: 1, title: "Pancakes" }),
      job({ id: "x1", batchId: "other", title: "Other" }),
    ];
    const wrapper = await mountList({ batchId: "b1" });

    expect(jobsCalls()).toEqual([{ batchId: "b1", perPage: -1 }]);
    expect(wrapper.get(".batch-summary").text()).toBe("Batch done: 1 added, 1 left to review");
    expect(rowTitles(wrapper)).toEqual(["Pancakes"]);
    expect(rowTitles(wrapper, ".recently-added")).toEqual(["Banana Mug Cake"]);
    expect(wrapper.get(".show-all").attributes("href")).toBe("/g/home/recipes/cards");
  });

  test("one batch: cards still being read or failed are counted, and it's done only when none is being read", async () => {
    vi.useFakeTimers();
    serverJobs = [
      job({ id: "c1", status: "committed", title: "Banana Mug Cake", recipe: { id: "r1", slug: "banana-mug-cake" } }),
      job({ id: "c2", position: 1, status: "committed", title: "Scones", recipe: { id: "r2", slug: "scones" } }),
      job({ id: "p1", position: 2, status: "processing", title: null, task: { kind: "extract", state: "running" } }),
      job({ id: "p2", position: 3, status: "processing", title: null, task: { kind: "extract", state: "queued" } }),
      job({ id: "f1", position: 4, status: "failed", title: null, error: { code: "timeout" } }),
    ];
    const wrapper = await mountList({ batchId: "b1" });
    expect(wrapper.get(".batch-summary").text()).toBe("2 added, 2 still being read, 1 failed");

    // the cards are read: one to review, one more failed
    serverJobs = serverJobs.map((j) => {
      if (j.id === "p1") {
        return { ...j, status: "ready", task: null, title: "Pancakes" };
      }
      return j.id === "p2" ? { ...j, status: "failed", task: null, error: { code: "no_recipe_found" } } : j;
    });
    await vi.advanceTimersByTimeAsync(3000);
    expect(wrapper.get(".batch-summary").text()).toBe("Batch done: 2 added, 1 left to review, 2 failed");
  });

  test("one batch with nothing added yet still says what's going on", async () => {
    serverJobs = [
      job({ id: "r1" }),
      job({ id: "p1", position: 1, status: "processing", title: null, task: { kind: "extract", state: "running" } }),
    ];
    const wrapper = await mountList({ batchId: "b1" });
    expect(wrapper.get(".batch-summary").text()).toBe("1 left to review, 1 still being read");
  });

  test("the review's notice for the batch's last card shows inline by the summary, and can be dismissed", async () => {
    serverJobs = [
      job({ id: "c1", status: "committed", title: "Banana Mug Cake", recipe: { id: "r1", slug: "banana-mug-cake" } }),
      job({ id: "p1", position: 1, status: "processing", title: null, task: { kind: "extract", state: "running" } }),
    ];
    leaveRecipeIngestCommitNotice({ text: "Added Banana Mug Cake · 1 card is still being read", warning: null });
    const wrapper = await mountList({ batchId: "b1" });

    const notice = wrapper.get(".commit-notice");
    expect(notice.text()).toContain("Added Banana Mug Cake · 1 card is still being read");
    expect(notice.attributes("data-type")).toBe("success");
    // it follows the summary line, inside the list: never a toast over the page title
    expect(wrapper.find(".batch-summary + .commit-notice").exists()).toBe(true);
    expect(toast.success).not.toHaveBeenCalled();
    // taken once: another visit doesn't show it again
    expect(takeRecipeIngestCommitNotice()).toBeNull();

    await notice.get(".commit-notice-close").trigger("click");
    expect(wrapper.find(".commit-notice").exists()).toBe(false);
  });

  test("a commit notice with a warning says what was left out", async () => {
    serverJobs = [job({ id: "c1", status: "committed", recipe: { id: "r1", slug: "banana-mug-cake" } })];
    leaveRecipeIngestCommitNotice({ text: "Added Banana Mug Cake", warning: "The tag \"Dessert\" was left out: it was deleted." });
    const wrapper = await mountList({ batchId: "b1" });

    const notice = wrapper.get(".commit-notice");
    expect(notice.attributes("data-type")).toBe("warning");
    expect(notice.get(".commit-notice-detail").text()).toBe("The tag \"Dessert\" was left out: it was deleted.");
  });

  test("Undo on the review's \"Added …\" takes the card just added back to review, and opens it", async () => {
    serverJobs = [
      job({ id: "c1", status: "committed", title: "Banana Mug Cake", recipe: { id: "r1", slug: "banana-mug-cake" } }),
      job({ id: "p1", position: 1, status: "processing", title: null, task: { kind: "extract", state: "running" } }),
    ];
    leaveRecipeIngestCommitNotice({ text: "Added Banana Mug Cake · 1 card is still being read", warning: null, undoJobId: "c1" });
    api.uncommit.mockResolvedValue({ data: { status: "ready", draftVersion: 3, task: null, error: null }, error: null });
    const wrapper = await mountList({ batchId: "b1" });
    api.getCounts.mockClear();

    const undo = wrapper.get(".commit-notice .commit-notice-undo");
    expect(undo.text()).toBe("Undo");
    await undo.trigger("click");
    await flushPromises();

    expect(api.uncommit).toHaveBeenCalledExactlyOnceWith("c1", {});
    expect(router.push).toHaveBeenCalledExactlyOnceWith("/g/home/recipes/cards/c1");
    // the card's page says it's back
    expect(takeCarriedReviewNotice("c1")).toEqual({ kind: "success", text: "The card is back for review.", detail: null });
    expect(api.getCounts).toHaveBeenCalled();
  });

  test("Undo on a card whose recipe was edited since deletes nothing: it says so and links to the card", async () => {
    serverJobs = [job({ id: "c1", status: "committed", recipe: { id: "r1", slug: "banana-mug-cake" } })];
    leaveRecipeIngestCommitNotice({ text: "Added Banana Mug Cake", warning: null, undoJobId: "c1" });
    api.uncommit.mockResolvedValue({ data: null, error: { response: { status: 409, data: { detail: { code: "recipe_edited" } } } } });
    const wrapper = await mountList({ batchId: "b1" });

    await wrapper.get(".commit-notice-undo").trigger("click");
    await flushPromises();

    const notice = wrapper.get(".commit-notice");
    expect(notice.attributes("data-type")).toBe("warning");
    expect(notice.get(".commit-notice-text").text())
      .toBe("The recipe was changed after this card was added. Going back to review would delete those changes.");
    expect(notice.get(".commit-notice-open").attributes("href")).toBe("/g/home/recipes/cards/c1");
    expect(notice.find(".commit-notice-undo").exists()).toBe(false);
    expect(router.push).not.toHaveBeenCalled();
    expect(toast.error).not.toHaveBeenCalled();
  });

  test("an Undo refused otherwise says why; once the card isn't added any more, Undo goes", async () => {
    serverJobs = [job({ id: "c1", status: "committed", recipe: { id: "r1", slug: "banana-mug-cake" } })];
    leaveRecipeIngestCommitNotice({ text: "Added Banana Mug Cake", warning: null, undoJobId: "c1" });
    const wrapper = await mountList({ batchId: "b1" });

    // the server couldn't be reached: Undo stays, to try again
    api.uncommit.mockResolvedValueOnce({ data: null, error: { message: "Network Error" } });
    await wrapper.get(".commit-notice-undo").trigger("click");
    await flushPromises();
    expect(toast.error).toHaveBeenLastCalledWith("The server couldn't be reached. Check your connection and try again.");
    expect(wrapper.find(".commit-notice-undo").exists()).toBe(true);

    // taken back to review elsewhere meanwhile
    api.uncommit.mockResolvedValueOnce({ data: null, error: { response: { status: 409, data: { detail: { code: "invalid_status", status: "ready" } } } } });
    await wrapper.get(".commit-notice-undo").trigger("click");
    await flushPromises();
    expect(toast.error).toHaveBeenLastCalledWith("This card has changed since this page loaded. Reload it and try again.");
    expect(wrapper.find(".commit-notice-undo").exists()).toBe(false);
    expect(wrapper.get(".commit-notice-text").text()).toBe("Added Banana Mug Cake");
    expect(router.push).not.toHaveBeenCalled();
  });

  test("a notice without a card to undo has no Undo", async () => {
    serverJobs = [job({ id: "c1", status: "committed", recipe: { id: "r1", slug: "banana-mug-cake" } })];
    leaveRecipeIngestCommitNotice({ text: "That was the last card.", warning: null });
    const wrapper = await mountList({ batchId: "b1" });
    expect(wrapper.find(".commit-notice-undo").exists()).toBe(false);
  });

  test("cards that arrive elsewhere show up: the counts are checked every 20 s, and a change reloads the list", async () => {
    vi.useFakeTimers();
    serverJobs = [job()];
    const wrapper = await mountList();
    api.getJobs.mockClear();
    api.getCounts.mockClear();

    // nothing changed: the counts are checked, and the list stays
    await vi.advanceTimersByTimeAsync(20_000);
    expect(api.getCounts).toHaveBeenCalledOnce();
    expect(api.getJobs).not.toHaveBeenCalled();

    // a card from the inbox
    serverJobs = [...serverJobs, job({ id: "i1", batchId: "inbox-b", title: null, status: "processing", source: "inbox", sourceName: "inbox/home/kitchen/scan.jpg" })];
    api.getCounts.mockResolvedValue({ data: { processing: 1, ready: 1, needsAttention: 0, failed: 0 }, error: null });
    await vi.advanceTimersByTimeAsync(20_000);
    expect(jobsCalls()).toEqual([
      { status: ["processing", "ready", "failed", "committing"], page: 1, perPage: 100 },
      { status: "committed", committedSince: expect.any(Date), orderBy: "committedAt", page: 1, perPage: 50 },
    ]);
    expect(rowTitles(wrapper)).toContain("scan.jpg");

    // one reload per change
    api.getJobs.mockClear();
    serverJobs = serverJobs.map(j => (j.id === "i1" ? { ...j, status: "ready", title: "Scones" } : j));
    await vi.advanceTimersByTimeAsync(20_000);
    expect(api.getJobs).not.toHaveBeenCalledWith(expect.objectContaining({ status: "committed" }));
  });

  test("a hidden page checks nothing", async () => {
    vi.useFakeTimers();
    serverJobs = [job()];
    await mountList();
    api.getJobs.mockClear();
    api.getCounts.mockClear();

    setVisibility("hidden");
    await vi.advanceTimersByTimeAsync(120_000);
    expect(api.getCounts).not.toHaveBeenCalled();
    expect(api.getJobs).not.toHaveBeenCalled();
  });

  test("a refused Retry says why, and the row shows the card as it is now", async () => {
    serverJobs = [job({ status: "failed", error: { code: "timeout" } })];
    const wrapper = await mountList();

    // retried on another device meanwhile: the server says the card changed, without a message
    serverJobs = [job({ status: "processing", task: { kind: "extract", state: "queued" } })];
    api.retry.mockResolvedValue({ data: null, error: { response: { status: 409, data: { detail: { code: "invalid_status", status: "processing" } } } } });
    await wrapper.get(".job-retry").trigger("click");
    await flushPromises();

    expect(toast.error).toHaveBeenCalledExactlyOnceWith("This card has changed since this page loaded. Reload it and try again.");
    expect(jobsCalls()).toContainEqual({ batchId: "b1", perPage: -1 });
    expect(wrapper.get(".job-status").text()).toBe("Waiting to be read");
  });

  test("a Retry the server never answered says the server couldn't be reached; an answer without a code names its status", async () => {
    serverJobs = [job({ status: "failed", error: { code: "timeout" } })];
    api.retry.mockResolvedValue({ data: null, error: { message: "Network Error" } });
    const wrapper = await mountList();
    await wrapper.get(".job-retry").trigger("click");
    await flushPromises();
    expect(toast.error).toHaveBeenCalledExactlyOnceWith("The server couldn't be reached. Check your connection and try again.");

    api.retry.mockResolvedValue({ data: null, error: { response: { status: 500, data: {} } } });
    await wrapper.get(".job-retry").trigger("click");
    await flushPromises();
    expect(toast.error).toHaveBeenLastCalledWith("Something went wrong (500).");
  });

  test("a refusal the API client already showed isn't shown twice", async () => {
    serverJobs = [job({ status: "failed", error: { code: "timeout" } })];
    api.retry.mockResolvedValue({ data: null, error: { response: { status: 503, data: { detail: { code: "paused_for_restore", message: "Paused" } } } } });
    const wrapper = await mountList();
    await wrapper.get(".job-retry").trigger("click");
    await flushPromises();
    expect(toast.error).not.toHaveBeenCalled();
  });

  test("Cancel stops reading a card; it shows as cancelled and can be retried", async () => {
    serverJobs = [job({ status: "processing", title: null, task: { kind: "extract", state: "queued" } })];
    api.cancel.mockImplementation(async (id: string) => {
      const error = { code: "cancelled" as const, params: {} };
      serverJobs = serverJobs.map(j => (j.id === id ? { ...j, status: "failed" as const, task: null, error } : j));
      return { data: { draftVersion: 0, status: "failed", task: null, error }, error: null };
    });
    const wrapper = await mountList();

    await wrapper.get(".job-cancel").trigger("click");
    await flushPromises();
    expect(api.cancel).toHaveBeenCalledExactlyOnceWith("j1");
    expect(wrapper.get(".job-status").text()).toBe("Failed");
    expect(wrapper.get(".job-caption").text()).toBe("Cancelled.");
    expect(wrapper.find(".job-retry").exists()).toBe(true);
  });

  test("a refused Cancel says why", async () => {
    serverJobs = [job({ status: "processing", task: { kind: "extract", state: "running" } })];
    api.cancel.mockResolvedValue({ data: null, error: { response: { status: 404, data: { detail: { code: "not_found" } } } } });
    const wrapper = await mountList();
    serverJobs = [];

    await wrapper.get(".job-cancel").trigger("click");
    await flushPromises();
    expect(toast.error).toHaveBeenCalledExactlyOnceWith("This card no longer exists.");
    expect(wrapper.find(".job-cancel").exists()).toBe(false);
  });
});

describe("adding a batch's clean cards", () => {
  const clean = (id: string, position: number, title: string) => job({ id, position, title });

  function commitAll() {
    api.commitClean.mockImplementation(async (_batchId: string, payload: { jobIds: string[] }) => {
      const recipe = (id: string) => ({ id: `r-${id}`, slug: `recipe-${id}` });
      serverJobs = serverJobs.map(j => (payload.jobIds.includes(j.id) ? { ...j, status: "committed", committedAt: ago(0), recipe: recipe(j.id) } : j));
      return {
        data: { committed: payload.jobIds.map(id => ({ jobId: id, recipeId: `r-${id}`, slug: `recipe-${id}` })), skipped: [] },
        error: null,
      };
    });
  }

  test("a batch with two or more clean cards offers to add them; a flagged, unread or lone clean card doesn't count", async () => {
    serverJobs = [
      clean("a", 0, "Banana Mug Cake"),
      clean("b", 1, "Scones"),
      job({ id: "c", position: 2, title: "Pancakes", warningCount: 1 }),
      job({ id: "d", position: 3, title: "Waffles", errorCount: 1 }),
      job({ id: "e", position: 4, status: "processing", title: null, task: { kind: "extract", state: "running" } }),
      job({ id: "f", position: 5, title: "Fudge", task: { kind: "reread", state: "queued" } }),
      job({ id: "x", batchId: "b2", title: "Lone", createdAt: ago(3 * HOUR) }),
    ];
    const wrapper = await mountList();

    const [b1, b2] = batchSections(wrapper);
    expect(b1!.get(".batch-add-clean").text()).toBe("Add 2 clean cards");
    expect(b2!.find(".batch-add-clean").exists()).toBe(false);
  });

  test("asks first, listing the cards, then adds them and says so", async () => {
    serverJobs = [
      { ...clean("a", 0, "Banana Mug Cake"), draftVersion: 3 },
      { ...clean("b", 1, "Scones"), draftVersion: 5 },
      job({ id: "c", position: 2, title: "Pancakes", warningCount: 2 }),
    ];
    commitAll();
    const wrapper = await mountList();

    await wrapper.get(".batch-add-clean").trigger("click");
    await flushPromises();
    expect(api.commitClean).not.toHaveBeenCalled();
    const dialog = wrapper.get(".confirm-dialog");
    expect(dialog.attributes("data-title")).toBe("Add 2 cards as recipes?");
    expect(dialog.text()).toContain("Nothing is highlighted on these cards. They're added as they were read:");
    expect(dialog.findAll(".clean-card").map(item => item.text())).toEqual(["Banana Mug Cake", "Scones"]);
    // the household's recipes need a login: nothing to warn about
    expect(dialog.find(".clean-public").exists()).toBe(false);

    await wrapper.get(".dialog-confirm").trigger("click");
    await flushPromises();
    expect(api.commitClean).toHaveBeenCalledExactlyOnceWith("b1", { jobIds: ["a", "b"], draftVersions: { a: 3, b: 5 } });
    // the batch's list gave each card's draft version: no request per card
    expect(api.getJobState).not.toHaveBeenCalled();

    const notice = wrapper.get(".commit-notice");
    expect(notice.attributes("data-type")).toBe("success");
    expect(notice.text()).toContain("Added 2 cards");
    // by its batch, which is still in the list
    expect(wrapper.find(".ingest-batch[data-batch='b1'] .commit-notice").exists()).toBe(true);
    // the list follows: the flagged card stays to review, the two are in Recently added
    expect(rowTitles(wrapper)).toEqual(["Pancakes"]);
    expect(rowTitles(wrapper, ".recently-added").sort()).toEqual(["Banana Mug Cake", "Scones"]);
    expect(api.getCounts).toHaveBeenCalled();
    expect(toast.success).not.toHaveBeenCalled();
  });

  test("once every card of the batch is added, the notice stays at the top of the list", async () => {
    serverJobs = [
      clean("a", 0, "Banana Mug Cake"),
      clean("b", 1, "Scones"),
      job({ id: "x", batchId: "b2", title: "Other", createdAt: ago(3 * HOUR) }),
    ];
    commitAll();
    const wrapper = await mountList();

    await wrapper.get(".batch-add-clean").trigger("click");
    await flushPromises();
    await wrapper.get(".dialog-confirm").trigger("click");
    await flushPromises();

    expect(batchSections(wrapper).map(section => section.attributes("data-batch"))).toEqual(["b2"]);
    expect(wrapper.find(".ingest-batch .commit-notice").exists()).toBe(false);
    expect(wrapper.get(".commit-notice").text()).toContain("Added 2 cards");

    await wrapper.get(".commit-notice-close").trigger("click");
    expect(wrapper.find(".commit-notice").exists()).toBe(false);
  });

  test("cards left out are listed with the reason", async () => {
    serverJobs = [clean("a", 0, "Banana Mug Cake"), clean("b", 1, "Scones"), clean("c", 2, "Fudge")];
    api.commitClean.mockImplementation(async () => {
      serverJobs = serverJobs.map(j => (j.id === "a" ? { ...j, status: "committed", committedAt: ago(0), recipe: { id: "r-a", slug: "banana" } } : j));
      return {
        data: {
          committed: [{ jobId: "a", recipeId: "r-a", slug: "banana" }],
          skipped: [{ jobId: "b", code: "version_conflict" }, { jobId: "c", code: "not_clean" }],
        },
        error: null,
      };
    });
    const wrapper = await mountList();

    await wrapper.get(".batch-add-clean").trigger("click");
    await flushPromises();
    await wrapper.get(".dialog-confirm").trigger("click");
    await flushPromises();

    const notice = wrapper.get(".commit-notice");
    expect(notice.attributes("data-type")).toBe("warning");
    expect(notice.get(".commit-notice-text").text()).toBe("Added 1 card");
    expect(notice.get(".commit-notice-detail").text()).toBe("2 were left to review:");
    expect(notice.findAll(".commit-notice-item").map(item => item.text())).toEqual([
      "Scones: changed after this list loaded",
      "Fudge: has something to check now",
    ]);
  });

  test("a card that changed before the question isn't listed", async () => {
    serverJobs = [clean("a", 0, "Banana Mug Cake"), clean("b", 1, "Scones"), clean("c", 2, "Fudge")];
    commitAll();
    const wrapper = await mountList();

    // meanwhile, elsewhere: Scones got a flag, Fudge is being read again
    serverJobs = serverJobs.map((j) => {
      if (j.id === "b") {
        return { ...j, warningCount: 1 };
      }
      return j.id === "c" ? { ...j, task: { kind: "reread", state: "queued" } } : j;
    });
    await wrapper.get(".batch-add-clean").trigger("click");
    await flushPromises();

    expect(wrapper.get(".confirm-dialog").attributes("data-title")).toBe("Add 1 card as a recipe?");
    expect(wrapper.findAll(".clean-card").map(item => item.text())).toEqual(["Banana Mug Cake"]);
    await wrapper.get(".dialog-confirm").trigger("click");
    await flushPromises();
    expect(api.commitClean).toHaveBeenCalledExactlyOnceWith("b1", { jobIds: ["a"], draftVersions: { a: 1 } });
  });

  test("none left clean by then: says so, without asking", async () => {
    serverJobs = [clean("a", 0, "Banana Mug Cake"), clean("b", 1, "Scones")];
    const wrapper = await mountList();
    serverJobs = serverJobs.map(j => ({ ...j, warningCount: 1 }));

    await wrapper.get(".batch-add-clean").trigger("click");
    await flushPromises();
    expect(wrapper.find(".confirm-dialog").exists()).toBe(false);
    expect(wrapper.get(".commit-notice").attributes("data-type")).toBe("info");
    expect(wrapper.get(".commit-notice").text()).toContain("No cards in this batch are clean any more.");
    expect(wrapper.find(".batch-add-clean").exists()).toBe(false);
  });

  test("a failed check or add says so", async () => {
    serverJobs = [clean("a", 0, "Banana Mug Cake"), clean("b", 1, "Scones")];
    const wrapper = await mountList();

    // reading the batch again fails
    api.getJobs.mockResolvedValueOnce({ data: null, error: { message: "Network Error" } });
    await wrapper.get(".batch-add-clean").trigger("click");
    await flushPromises();
    expect(wrapper.find(".confirm-dialog").exists()).toBe(false);
    expect(wrapper.get(".commit-notice").attributes("data-type")).toBe("error");
    expect(wrapper.get(".commit-notice").text()).toContain("Couldn't check the cards. Try again.");

    api.commitClean.mockResolvedValue({ data: null, error: { response: { status: 500, data: {} } } });
    await wrapper.get(".batch-add-clean").trigger("click");
    await flushPromises();
    await wrapper.get(".dialog-confirm").trigger("click");
    await flushPromises();
    expect(wrapper.get(".commit-notice").attributes("data-type")).toBe("error");
    expect(wrapper.get(".commit-notice").text()).toContain("Couldn't add the cards");
  });

  test("a big batch goes 5 cards a request, one after another, saying how far it got; one notice sums it up", async () => {
    serverJobs = Array.from({ length: 12 }, (_, i) => ({ ...clean(`c${i}`, i, `Card ${i}`), draftVersion: i + 1 }));
    const answers: Array<() => void> = [];
    let inFlight = 0;
    let mostInFlight = 0;
    api.commitClean.mockImplementation(async (_batchId: string, payload: { jobIds: string[]; draftVersions: Record<string, number> }) => {
      inFlight += 1;
      mostInFlight = Math.max(mostInFlight, inFlight);
      await new Promise<void>(resolve => answers.push(resolve));
      inFlight -= 1;
      serverJobs = serverJobs.map(j => (payload.jobIds.includes(j.id) ? { ...j, status: "committed", committedAt: ago(0) } : j));
      return {
        data: { committed: payload.jobIds.map(id => ({ jobId: id, recipeId: `r-${id}`, slug: id })), skipped: [] },
        error: null,
      };
    });
    const wrapper = await mountList();

    await wrapper.get(".batch-add-clean").trigger("click");
    await flushPromises();
    await wrapper.get(".dialog-confirm").trigger("click");
    await flushPromises();
    expect(wrapper.get(".commit-notice").attributes("data-type")).toBe("info");
    expect(wrapper.get(".commit-notice-text").text()).toBe("Adding 5 of 12 cards…");
    answers.shift()!();
    await flushPromises();
    expect(wrapper.get(".commit-notice-text").text()).toBe("Adding 10 of 12 cards…");
    answers.shift()!();
    await flushPromises();
    expect(wrapper.get(".commit-notice-text").text()).toBe("Adding 12 of 12 cards…");
    answers.shift()!();
    await flushPromises();

    expect(api.commitClean.mock.calls.map(call => call[1])).toEqual([
      { jobIds: ["c0", "c1", "c2", "c3", "c4"], draftVersions: { c0: 1, c1: 2, c2: 3, c3: 4, c4: 5 } },
      { jobIds: ["c5", "c6", "c7", "c8", "c9"], draftVersions: { c5: 6, c6: 7, c7: 8, c8: 9, c9: 10 } },
      { jobIds: ["c10", "c11"], draftVersions: { c10: 11, c11: 12 } },
    ]);
    expect(mostInFlight).toBe(1);
    expect(wrapper.get(".commit-notice").attributes("data-type")).toBe("success");
    expect(wrapper.get(".commit-notice-text").text()).toBe("Added 12 cards");
  });

  test("more cards than the server takes in one request (100) are all added", async () => {
    serverJobs = Array.from({ length: 120 }, (_, i) => clean(`c${i}`, i, `Card ${i}`));
    commitAll();
    const wrapper = await mountList();

    await wrapper.get(".batch-add-clean").trigger("click");
    await flushPromises();
    await wrapper.get(".dialog-confirm").trigger("click");
    await flushPromises();

    expect(api.commitClean).toHaveBeenCalledTimes(24);
    expect(api.commitClean.mock.calls.every(call => (call[1] as { jobIds: string[] }).jobIds.length <= 5)).toBe(true);
    expect(wrapper.get(".commit-notice-text").text()).toBe("Added 120 cards");
  });

  test("a request refused part way stops there and says what was added so far", async () => {
    serverJobs = Array.from({ length: 12 }, (_, i) => clean(`c${i}`, i, `Card ${i}`));
    commitAll();
    const added = api.commitClean.getMockImplementation()!;
    api.commitClean
      .mockImplementationOnce(added)
      .mockImplementationOnce(async () => ({ data: null, error: { response: { status: 504, data: {} } } }));
    const wrapper = await mountList();

    await wrapper.get(".batch-add-clean").trigger("click");
    await flushPromises();
    await wrapper.get(".dialog-confirm").trigger("click");
    await flushPromises();

    expect(api.commitClean).toHaveBeenCalledTimes(2);
    const notice = wrapper.get(".commit-notice");
    expect(notice.attributes("data-type")).toBe("warning");
    expect(notice.get(".commit-notice-text").text()).toBe("Added 5 cards");
    expect(notice.get(".commit-notice-detail").text()).toBe("The other 7 cards weren't added. Try again.");
    // the 7 stay in the batch, clean, to add again
    expect(wrapper.get(".batch-add-clean").text()).toBe("Add 7 clean cards");
  });

  test("a restore pausing the server stops the run: the cards it left wait, and the rest aren't sent", async () => {
    serverJobs = Array.from({ length: 12 }, (_, i) => clean(`c${i}`, i, `Card ${i}`));
    api.commitClean.mockImplementation(async (_batchId: string, payload: { jobIds: string[] }) => {
      const [first, ...rest] = payload.jobIds;
      serverJobs = serverJobs.map(j => (j.id === first ? { ...j, status: "committed", committedAt: ago(0) } : j));
      return {
        data: {
          committed: [{ jobId: first, recipeId: "r", slug: "s" }],
          skipped: rest.map(id => ({ jobId: id, code: "paused_for_restore" })),
        },
        error: null,
      };
    });
    const wrapper = await mountList();

    await wrapper.get(".batch-add-clean").trigger("click");
    await flushPromises();
    await wrapper.get(".dialog-confirm").trigger("click");
    await flushPromises();

    expect(api.commitClean).toHaveBeenCalledOnce();
    const notice = wrapper.get(".commit-notice");
    expect(notice.get(".commit-notice-text").text()).toBe("Added 1 card");
    expect(notice.get(".commit-notice-detail").text()).toBe("The other 7 cards weren't added. Try again. 4 were left to review:");
    expect(notice.findAll(".commit-notice-item")).toHaveLength(4);
    expect(notice.get(".commit-notice-item").text()).toBe("Card 1: wasn't added while a backup is restored");
  });

  test("Enter on the question's Cancel doesn't add the cards", async () => {
    serverJobs = [clean("a", 0, "Banana Mug Cake"), clean("b", 1, "Scones")];
    commitAll();
    const wrapper = await mountList({}, realDialog);

    await wrapper.get(".batch-add-clean").trigger("click");
    await flushPromises();
    await wrapper.get(".confirm-dialog .dialog-cancel").trigger("keydown", { key: "Enter" });
    await flushPromises();
    expect(api.commitClean).not.toHaveBeenCalled();
    await wrapper.get(".confirm-dialog .dialog-cancel").trigger("click");
    expect(wrapper.find(".confirm-dialog").exists()).toBe(false);

    await wrapper.get(".batch-add-clean").trigger("click");
    await flushPromises();
    await wrapper.get(".confirm-dialog .dialog-confirm").trigger("click");
    await flushPromises();
    expect(api.commitClean).toHaveBeenCalledOnce();
  });

  test("where the household's recipes are seen without a login, the question warns about the card photos turned on", async () => {
    serverJobs = [clean("a", 0, "Banana Mug Cake"), clean("b", 1, "Scones")].map(j => ({ ...j, householdRecipesPublic: true }));
    const wrapper = await mountList();

    await wrapper.get(".batch-add-clean").trigger("click");
    await flushPromises();
    // there, a card's photo is the picture or attached only where someone turned that on for the card (§6.4)
    expect(wrapper.get(".confirm-dialog .clean-public").text()).toBe(
      "Recipes in this household can be seen without logging in. A card's photo is used as the recipe's picture, or "
      + "attached, only where that was turned on for the card, and anyone can then see it. Open a card to check.",
    );
  });

  test("a refusal the API client already showed (a restore running) isn't shown again", async () => {
    serverJobs = [clean("a", 0, "Banana Mug Cake"), clean("b", 1, "Scones")];
    api.commitClean.mockResolvedValue({
      data: null,
      error: { response: { status: 503, data: { detail: { code: "paused_for_restore", message: "Paused" } } } },
    });
    const wrapper = await mountList();

    await wrapper.get(".batch-add-clean").trigger("click");
    await flushPromises();
    await wrapper.get(".dialog-confirm").trigger("click");
    await flushPromises();
    expect(wrapper.find(".commit-notice").exists()).toBe(false);
  });
});

describe("a failed card kept on this server that nothing local could read", () => {
  const stuck = (id: string, position = 0) => job({
    id,
    position,
    status: "failed",
    title: null,
    localOnly: true,
    error: { code: "local_only_unavailable", params: {} },
  });

  /** What can read cards now, as the cards page loads it */
  async function loadSettings(localOnlyAvailable: boolean) {
    api.getSettings.mockResolvedValue({ data: { localOnly: false, localOnlyAvailable, limits: {} }, error: null });
    await useRecipeIngestSettings().load();
  }

  test("offers Read with cloud providers on its row where its card page would, asking first; no Retry that fails the same way", async () => {
    await loadSettings(false);
    serverJobs = [stuck("mine"), stuck("theirs", 1), job({ id: "other", position: 2, status: "failed", error: { code: "timeout", params: {} } })];
    api.getJob.mockImplementation(async (id: string) => ({
      data: { id, permissions: { canReadWithCloud: id === "mine" } },
      error: null,
    }));
    api.readWithCloud.mockImplementation(async (id: string) => {
      const queued = { status: "processing" as const, task: { kind: "extract" as const, state: "queued" as const }, error: null };
      serverJobs = serverJobs.map(j => (j.id === id ? { ...j, ...queued, localOnly: false } : j));
      return { data: { draftVersion: 1, ...queued }, error: null };
    });
    api.retry.mockResolvedValue({ data: { draftVersion: 1, status: "processing", task: { kind: "extract", state: "queued" }, error: null }, error: null });
    const wrapper = await mountList({}, {
      BaseDialog: {
        props: ["modelValue", "title"],
        template: "<div v-if=\"modelValue\" class=\"confirm-dialog\" :data-title=\"title\"><slot /><slot name=\"card-actions\" /></div>",
      },
    });

    const rows = wrapper.findAll(".ingest-job");
    expect(rows.map(row => row.find(".job-read-with-cloud").exists())).toEqual([true, false, false]);
    expect(rows.map(row => row.find(".job-retry").exists())).toEqual([false, false, true]);
    // Retry failed leaves out the cards Retry can't read
    await wrapper.get(".batch-retry-failed").trigger("click");
    await flushPromises();
    expect(api.retry).toHaveBeenCalledExactlyOnceWith("other");

    await rows[0]!.get(".job-read-with-cloud").trigger("click");
    await flushPromises();
    expect(api.readWithCloud).not.toHaveBeenCalled();
    expect(wrapper.text()).toContain("Reading it with a cloud provider sends its photos to that provider, outside your network.");
    await wrapper.get(".cloud-confirm").trigger("click");
    await flushPromises();
    expect(api.readWithCloud).toHaveBeenCalledExactlyOnceWith("mine");
    expect(wrapper.findAll(".ingest-job")[0]!.get(".job-status").text()).toBe("Waiting to be read");
  });

  test("once something local can read cards, Retry is back", async () => {
    await loadSettings(true);
    serverJobs = [stuck("mine")];
    api.getJob.mockResolvedValue({ data: { id: "mine", permissions: { canReadWithCloud: false } }, error: null });
    const wrapper = await mountList();
    expect(wrapper.find(".job-retry").exists()).toBe(true);
    expect(wrapper.find(".job-read-with-cloud").exists()).toBe(false);
  });
});

describe("Review batch while the batch's clean cards are added", () => {
  test("is disabled until they're added", async () => {
    serverJobs = [job({ id: "a", position: 0, title: "Scones" }), job({ id: "b", position: 1, title: "Fudge" }), job({ id: "c", position: 2, title: "Pie", warningCount: 1 })];
    let answer!: () => void;
    api.commitClean.mockImplementation(async (_batchId: string, payload: { jobIds: string[] }) => {
      await new Promise<void>((resolve) => {
        answer = resolve;
      });
      serverJobs = serverJobs.map(j => (payload.jobIds.includes(j.id) ? { ...j, status: "committed", committedAt: ago(0) } : j));
      return { data: { committed: payload.jobIds.map(id => ({ jobId: id, recipeId: `r-${id}`, slug: id })), skipped: [] }, error: null };
    });
    const wrapper = await mountList();
    expect(wrapper.get(".batch-review").attributes("disabled")).toBe("false");

    await wrapper.get(".batch-add-clean").trigger("click");
    await flushPromises();
    await wrapper.get(".dialog-confirm").trigger("click");
    await flushPromises();
    expect(wrapper.get(".batch-review").attributes("disabled")).toBe("true");

    answer();
    await flushPromises();
    expect(wrapper.get(".batch-review").attributes("disabled")).toBe("false");
  });
});
