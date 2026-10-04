import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import IngestBatchList from "./IngestBatchList.vue";
import { resetRecipeIngestCounts } from "~/composables/use-recipe-ingest";
import { resetRecipeIngestUploads, useRecipeIngestUploads } from "~/composables/use-recipe-ingest-uploads";
import type { RecipeIngestJobsQuery } from "~/lib/api/user/recipe-ingest";
import type { RecipeIngestionJobSummary } from "~/lib/api/types/recipe-ingest";

const api = vi.hoisted(() => ({
  getJobs: vi.fn(),
  getCounts: vi.fn(),
  retry: vi.fn(),
  discard: vi.fn(),
  upload: vi.fn(),
  createBatch: vi.fn(),
  sealBatch: vi.fn(),
}));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));

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
};

const HOUR = 60 * 60 * 1000;

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
    errorCount: 0,
    warningCount: 0,
    task: null,
    error: null,
    recipe: null,
    localOnly: false,
    createdAt: ago(HOUR),
    ...overrides,
  };
}

/** The server's jobs; GET /jobs filters them like the real route */
let serverJobs: RecipeIngestionJobSummary[] = [];

function page(items: RecipeIngestionJobSummary[]) {
  return { data: { page: 1, per_page: 100, total: items.length, total_pages: 1, items }, error: null };
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

async function mountList(props: { batchId?: string | null } = {}) {
  const wrapper = mount(IngestBatchList, {
    props: { groupSlug: "home", ...props },
    global: {
      mocks: { $globals: { icons: { lock: "lock", delete: "delete", alertCircle: "alert" } } },
      stubs,
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
  setVisibility("visible");
  serverJobs = [];
  api.getJobs.mockImplementation(async (query: RecipeIngestJobsQuery) => {
    const statuses = query.status ? [query.status].flat() : null;
    return page(serverJobs.filter(j => (!statuses || statuses.includes(j.status)) && (!query.batchId || j.batchId === query.batchId)));
  });
  api.getCounts.mockResolvedValue({ data: { processing: 0, ready: 1, needsAttention: 0, failed: 0 }, error: null });
  api.createBatch.mockResolvedValue({ data: { id: "b3", source: "app" }, error: null });
});

afterEach(() => {
  wrappers.forEach(wrapper => wrapper.unmount());
  wrappers.length = 0;
  resetRecipeIngestUploads();
  vi.useRealTimers();
});

describe("IngestBatchList", () => {
  test("batches newest first, cards in capture order, with each batch's Review and Retry failed", async () => {
    serverJobs = [
      job({ id: "a2", batchId: "older", position: 1, title: "Pancakes", createdAt: ago(5 * HOUR), source: "inbox" }),
      job({ id: "a1", batchId: "older", position: 0, title: "Waffles", createdAt: ago(5 * HOUR) }),
      job({ id: "b2", batchId: "newer", position: 1, status: "failed", title: null, error: { code: "no_recipe_found" } }),
      job({ id: "b1", batchId: "newer", position: 0, status: "processing", title: null, task: { kind: "extract", state: "queued" } }),
    ];
    const wrapper = await mountList();

    expect(batchSections(wrapper).map(section => section.attributes("data-batch"))).toEqual(["newer", "older"]);
    const [newer, older] = batchSections(wrapper);
    expect(newer!.findAll(".job-status").map(chip => chip.text())).toEqual([
      "Waiting to be read",
      "Failed: No recipe was found on this card.",
    ]);
    expect(newer!.find(".batch-review").exists()).toBe(false);
    expect(newer!.find(".batch-retry-failed").exists()).toBe(true);

    expect(older!.findAll(".job-title").map(title => title.text())).toEqual(["Waffles", "Pancakes"]);
    expect(older!.get(".batch-review").attributes("href")).toBe("/g/home/recipes/cards/review?batch=older");
    expect(older!.get(".batch-meta").text()).toBe("2 cards · Scanned in the app");
    expect(older!.get(".batch-title").text()).toMatch(/^Batch of .+/);
    expect(jobsCalls()).toContainEqual(expect.objectContaining({ status: ["processing", "ready", "failed", "committing"] }));
  });

  test("cards added in the last 7 days link to their recipes", async () => {
    serverJobs = [
      job({ id: "c1", status: "committed", title: "Banana Mug Cake", recipe: { id: "r1", slug: "banana-mug-cake" } }),
      job({ id: "c0", status: "committed", title: "Old Cake", createdAt: ago(8 * 24 * HOUR), recipe: { id: "r0", slug: "old" } }),
    ];
    const wrapper = await mountList();

    expect(batchSections(wrapper)).toHaveLength(0);
    expect(rowTitles(wrapper, ".recently-added")).toEqual(["Banana Mug Cake"]);
    expect(wrapper.get(".recently-added .job-view-recipe").attributes("href")).toBe("/g/home/r/banana-mug-cake");
    expect(wrapper.find(".no-cards").exists()).toBe(false);
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
    expect(wrapper.get(".ingest-batch .job-status").text()).toBe("Reading the card");
    // Loading the list refreshes the sidebar's count
    expect(api.getCounts).toHaveBeenCalledOnce();
    api.getJobs.mockClear();
    api.getCounts.mockClear();

    await vi.advanceTimersByTimeAsync(2999);
    expect(api.getJobs).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(jobsCalls()).toEqual([{ batchId: "b1", page: 1, perPage: 100 }]);
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

    setVisibility("visible");
    await vi.advanceTimersByTimeAsync(0);
    expect(jobsCalls()).toEqual([{ batchId: "b1", page: 1, perPage: 100 }]);
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

    expect(jobsCalls()).toContainEqual({ batchId: "b3", page: 1, perPage: 100 });
    expect(wrapper.get(".ingest-batch").attributes("data-batch")).toBe("b3");
    expect(wrapper.get(".job-status").text()).toBe("Waiting to be read");
  });

  test("Retry queues a failed card again", async () => {
    serverJobs = [job({ status: "failed", error: { code: "timeout" } })];
    const wrapper = await mountList();
    expect(wrapper.get(".job-status").text()).toBe("Failed: Reading this card took too long. Try again.");

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
    expect(jobsCalls()).toContainEqual({ batchId: "b1", page: 1, perPage: 100 });
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

  test("one batch, after its review: what was added and what's left", async () => {
    serverJobs = [
      job({ id: "c1", status: "committed", title: "Banana Mug Cake", recipe: { id: "r1", slug: "banana-mug-cake" } }),
      job({ id: "r2", position: 1, title: "Pancakes" }),
      job({ id: "x1", batchId: "other", title: "Other" }),
    ];
    const wrapper = await mountList({ batchId: "b1" });

    expect(jobsCalls()).toEqual([{ batchId: "b1", page: 1, perPage: 100 }]);
    expect(wrapper.get(".batch-summary").text()).toBe("Batch done: 1 added, 1 left to review");
    expect(rowTitles(wrapper)).toEqual(["Pancakes"]);
    expect(rowTitles(wrapper, ".recently-added")).toEqual(["Banana Mug Cake"]);
    expect(wrapper.get(".show-all").attributes("href")).toBe("/g/home/recipes/cards");
  });
});
