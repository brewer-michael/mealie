import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import IngestUploadQueue from "./IngestUploadQueue.vue";
import { resetRecipeIngestCounts } from "~/composables/use-recipe-ingest";
import { resetRecipeIngestUploads, useRecipeIngestUploads } from "~/composables/use-recipe-ingest-uploads";

const api = vi.hoisted(() => ({
  upload: vi.fn(),
  createBatch: vi.fn(),
  sealBatch: vi.fn(),
  getCounts: vi.fn(),
}));

vi.mock("~/composables/api", () => ({
  useUserApi: () => ({ recipeIngest: api }),
}));

const slot = (tag = "div", className = "") => ({ template: `<${tag} class="${className}"><slot /></${tag}>` });

const wrappers: VueWrapper[] = [];

function mountQueue() {
  const wrapper = mount(IngestUploadQueue, {
    props: { groupSlug: "home" },
    global: {
      mocks: { $globals: { icons: { close: "close" } } },
      stubs: {
        VList: slot(),
        VListItem: {
          template: "<div class=\"list-item\"><slot name=\"prepend\" /><slot /><slot name=\"append\" /></div>",
        },
        VListItemTitle: slot("h4"),
        VChip: {
          props: ["to", "title"],
          template: "<a class=\"chip\" :href=\"to\" :title=\"title\"><slot /></a>",
        },
        VBtn: { template: "<button type=\"button\"><slot /></button>" },
        VIcon: { template: "<i />" },
        VProgressCircular: { template: "<span class=\"spinner\" />" },
        VProgressLinear: {
          props: ["modelValue", "indeterminate"],
          template: "<progress :value=\"modelValue\" :data-indeterminate=\"indeterminate\" />",
        },
      },
    },
  });
  wrappers.push(wrapper);
  return wrapper;
}

function photo() {
  return new File(["photo"], "IMG_1.jpg", { type: "image/jpeg" });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

const accepted = { batchId: "b1", jobs: [{ id: "j1", status: "processing", pageCount: 1, reviewPath: "" }], rejected: [], summary: "" };

function rows(wrapper: VueWrapper) {
  return wrapper.findAll(".upload-card");
}

beforeEach(() => {
  vi.clearAllMocks();
  resetRecipeIngestUploads();
  resetRecipeIngestCounts();
  localStorage.clear();
  api.createBatch.mockResolvedValue({ data: { id: "b1", source: "app" }, error: null });
  api.sealBatch.mockResolvedValue({ data: { id: "b1", source: "app" }, error: null });
  api.getCounts.mockResolvedValue({ data: { processing: 1 }, error: null });
});

afterEach(() => {
  wrappers.forEach(wrapper => wrapper.unmount());
  wrappers.length = 0;
  resetRecipeIngestUploads();
  vi.useRealTimers();
});

describe("IngestUploadQueue", () => {
  test("shows each card uploading, two at a time, with progress", async () => {
    const pending = [deferred<unknown>(), deferred<unknown>(), deferred<unknown>()];
    let n = 0;
    api.upload.mockImplementation((_files, _options, config) => {
      if (n === 0) {
        config.onUploadProgress({ loaded: 40, total: 100 });
      }
      return pending[n++]!.promise;
    });
    const queue = useRecipeIngestUploads();
    const wrapper = mountQueue();
    expect(wrapper.find(".ingest-upload-queue").exists()).toBe(false);

    queue.takePhoto(photo());
    queue.takePhoto(photo());
    queue.takePhoto(photo());
    await flushPromises();

    expect(rows(wrapper).map(row => [row.get("h4").text(), row.get(".upload-status").text()])).toEqual([
      ["Card 1", "Uploading 40%"],
      ["Card 2", "Uploading"],
      ["Card 3", "Waiting to upload"],
    ]);

    pending.forEach(p => p.resolve({ data: accepted, error: null }));
    await flushPromises();
    // Uploaded cards move to the job list
    expect(wrapper.find(".ingest-upload-queue").exists()).toBe(false);
  });

  test("a duplicate shows Already scanned, linking to the earlier card, until it's dismissed", async () => {
    api.upload.mockResolvedValue({
      data: { batchId: "b1", jobs: [], rejected: [{ index: 0, reason: "duplicate", duplicateOf: "j-earlier" }], summary: "" },
      error: null,
    });
    const queue = useRecipeIngestUploads();
    const wrapper = mountQueue();
    queue.takePhoto(photo());
    await flushPromises();

    const chip = wrapper.get(".already-scanned .chip");
    expect(chip.text()).toBe("Already scanned");
    expect(chip.attributes("href")).toBe("/g/home/recipes/cards/j-earlier");
    expect(chip.attributes("title")).toBe("Open the earlier card");
    expect(wrapper.find(".upload-retry").exists()).toBe(false);

    await wrapper.get(".upload-remove").trigger("click");
    expect(rows(wrapper)).toHaveLength(0);
  });

  test("photos the server didn't use are listed with the reason", async () => {
    api.upload.mockResolvedValue({
      data: {
        batchId: "b1",
        jobs: [],
        rejected: [{ index: 0, filename: "card.pdf", reason: "pdf_not_supported" }],
        summary: "",
      },
      error: null,
    });
    const queue = useRecipeIngestUploads();
    const wrapper = mountQueue();
    queue.takePhoto(photo());
    await flushPromises();

    expect(wrapper.get(".upload-rejected").text())
      .toBe("Not used: PDFs can't be scanned. Upload a photo of the card instead.");
    // Sending the same file again can't help
    expect(wrapper.find(".upload-retry").exists()).toBe(false);
  });

  test("retrying, then failed with Retry, and the server's reason when it gave one", async () => {
    vi.useFakeTimers();
    api.upload.mockResolvedValue({
      data: null,
      error: { response: { status: 503, headers: {}, data: { detail: { code: "paused_for_restore", message: "Paused" } } } },
    });
    const queue = useRecipeIngestUploads();
    const wrapper = mountQueue();
    queue.takePhoto(photo());
    await flushPromises();

    expect(wrapper.get(".upload-status").text()).toBe("Retrying");
    // it goes again by itself
    expect(wrapper.get(".upload-detail").text())
      .toBe("Recipe cards are paused while a backup is restored. Trying again in a minute.");

    await vi.advanceTimersByTimeAsync(2000 + 4000 + 8000);
    expect(wrapper.get(".upload-status").text()).toBe("Upload failed");
    // now it waits for Retry
    expect(wrapper.get(".upload-detail").text())
      .toBe("Recipe cards are paused while a backup is restored. Try again in a minute.");

    api.upload.mockResolvedValue({ data: accepted, error: null });
    await wrapper.get(".upload-retry").trigger("click");
    await flushPromises();
    expect(rows(wrapper)).toHaveLength(0);
    expect(api.upload).toHaveBeenCalledTimes(5);
  });

  test("a network failure just says Upload failed", async () => {
    api.upload.mockResolvedValue({ data: null, error: { message: "Network Error" } });
    const queue = useRecipeIngestUploads();
    const wrapper = mountQueue();
    queue.takePhoto(photo());
    await flushPromises();
    expect(wrapper.get(".upload-status").text()).toBe("Retrying");
    expect(wrapper.find(".upload-detail").exists()).toBe(false);
  });

  test("Done with a card still uploading says the batch finishes when it's in", async () => {
    const pending = deferred<unknown>();
    api.upload.mockReturnValue(pending.promise);
    const queue = useRecipeIngestUploads();
    const wrapper = mountQueue();
    queue.takePhoto(photo());
    await flushPromises();
    queue.done();
    await flushPromises();

    expect(wrapper.get(".sealing").text()).toBe("Finishing the batch when the last cards are uploaded");
    expect(api.sealBatch).not.toHaveBeenCalled();

    pending.resolve({ data: accepted, error: null });
    await flushPromises();
    expect(api.sealBatch).toHaveBeenCalledExactlyOnceWith("b1", { suppressAlert: true });
    expect(wrapper.find(".sealing").exists()).toBe(false);
  });
});
