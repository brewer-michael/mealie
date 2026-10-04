import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import ReviewPage from "./review.vue";
import { rememberedRecipeIngestBatch, resetRecipeIngestReviewState } from "~/composables/use-recipe-ingest";
import type { RecipeIngestionBatchJob, RecipeIngestionBatchOut } from "~/lib/api/types/recipe-ingest";

const api = vi.hoisted(() => ({ getBatch: vi.fn() }));

vi.mock("~/composables/api", () => ({
  useUserApi: () => ({ recipeIngest: api }),
}));

const route = { params: { groupSlug: "home" }, query: {} as Record<string, string> };
const router = { replace: vi.fn() };
const wrappers: VueWrapper[] = [];

function job(id: string, position: number, overrides: Partial<RecipeIngestionBatchJob> = {}): RecipeIngestionBatchJob {
  return { id, position, status: "ready", errorCount: 0, warningCount: 0, ...overrides };
}

function batch(jobs: RecipeIngestionBatchJob[]): RecipeIngestionBatchOut {
  return { id: "b1", source: "app", jobs };
}

/** Opens the page as a notification does, and answers where it went */
async function open(query: Record<string, string>) {
  route.query = query;
  const wrapper = mount(ReviewPage, { global: { stubs: { AppLoader: { template: "<div class=\"loader\" />" } } } });
  wrappers.push(wrapper);
  await flushPromises();
  expect(router.replace).toHaveBeenCalledOnce();
  return router.replace.mock.calls[0]![0] as string;
}

describe("the batch review landing page (notifications open it)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetRecipeIngestReviewState();
    vi.stubGlobal("definePageMeta", vi.fn());
    vi.stubGlobal("useSeoMeta", vi.fn());
    vi.stubGlobal("useRoute", () => route);
    vi.stubGlobal("useRouter", () => router);
  });

  afterEach(() => {
    wrappers.forEach(wrapper => wrapper.unmount());
    wrappers.length = 0;
    vi.unstubAllGlobals();
  });

  test("opens the first ready card, in capture order, that has something to check", async () => {
    api.getBatch.mockResolvedValue({
      data: batch([
        job("j3", 2, { warningCount: 1 }),
        job("j1", 0),
        job("j0", 3, { status: "processing" }),
        job("j2", 1, { errorCount: 2 }),
      ]),
      error: null,
    });

    expect(await open({ batch: "b1" })).toBe("/g/home/recipes/cards/j2");
    expect(api.getBatch).toHaveBeenCalledWith("b1");
    // the card shows its place in the batch at once
    expect(rememberedRecipeIngestBatch("b1")?.jobs).toHaveLength(4);
  });

  test("with nothing to check, opens the first ready card", async () => {
    api.getBatch.mockResolvedValue({
      data: batch([job("j2", 1), job("j1", 0), job("j0", 2, { status: "failed" })]),
      error: null,
    });
    expect(await open({ batch: "b1" })).toBe("/g/home/recipes/cards/j1");
  });

  test("with no card ready, goes to the queue showing the batch", async () => {
    api.getBatch.mockResolvedValue({
      data: batch([job("j1", 0, { status: "processing" }), job("j2", 1, { status: "committed" })]),
      error: null,
    });
    expect(await open({ batch: "b 1" })).toBe("/g/home/recipes/cards?batch=b%201");
  });

  test.each([
    ["an unknown or purged batch (404)", { response: { status: 404, data: { detail: { code: "not_found" } } } }],
    ["another household's batch", { response: { status: 404, data: { detail: { code: "not_found" } } } }],
    ["a failed request", { message: "Network Error" }],
  ])("%s: the queue says it isn't available", async (_name, error) => {
    api.getBatch.mockResolvedValue({ data: null, error });
    expect(await open({ batch: "b1" })).toBe("/g/home/recipes/cards?unavailable=1");
  });

  test("without a batch, the queue", async () => {
    expect(await open({})).toBe("/g/home/recipes/cards");
    expect(api.getBatch).not.toHaveBeenCalled();
  });
});
