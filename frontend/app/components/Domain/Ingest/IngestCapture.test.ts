import { flushPromises, mount, type DOMWrapper, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { nextTick } from "vue";
import IngestCapture from "./IngestCapture.vue";
import { resetRecipeIngestCounts } from "~/composables/use-recipe-ingest";
import {
  CAPTURE_MODE_STORAGE_KEY,
  resetRecipeIngestUploads,
  useRecipeIngestUploads,
} from "~/composables/use-recipe-ingest-uploads";

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

const stubs = {
  VBtnToggle: {
    props: ["modelValue"],
    emits: ["update:modelValue"],
    template: `
      <div class="btn-toggle" :data-value="modelValue"
        @click="$event.target.dataset.value && $emit('update:modelValue', $event.target.dataset.value)">
        <slot />
      </div>
    `,
  },
  VBtn: {
    props: ["value", "disabled"],
    template: "<button type=\"button\" :data-value=\"value\" :disabled=\"disabled\"><slot /></button>",
  },
  VIcon: { template: "<i />" },
  VSpacer: slot(),
  VRow: slot(),
  VCol: slot(),
  VCard: slot("div", "card"),
  VCardTitle: slot("h4"),
  VCardActions: slot(),
};

const wrappers: VueWrapper[] = [];

function mountCapture(props: { maxPagesPerCard?: number } = {}) {
  const wrapper = mount(IngestCapture, {
    props,
    global: {
      mocks: { $globals: { icons: { upload: "upload" } } },
      stubs,
    },
  });
  wrappers.push(wrapper);
  return wrapper;
}

let photoCount = 0;
function photo(): File {
  photoCount += 1;
  return new File([`photo ${photoCount}`], `IMG_${photoCount}.jpg`, { type: "image/jpeg" });
}

/** Picks files in an input; returns what its value was set to afterwards */
async function pick(input: Pick<DOMWrapper<HTMLInputElement>, "element" | "trigger">, files: File[]) {
  const values: string[] = [];
  Object.defineProperty(input.element, "files", { value: files, configurable: true });
  Object.defineProperty(input.element, "value", {
    configurable: true,
    get: () => (values.length ? values[values.length - 1] : "C:\\fakepath\\photo.jpg"),
    set: (value: string) => values.push(value),
  });
  await input.trigger("change");
  return values;
}

function button(wrapper: VueWrapper, selector: string) {
  return wrapper.get<HTMLButtonElement>(selector);
}

function photosSent() {
  return api.upload.mock.calls.map(call => call[0] as File[]);
}

beforeEach(() => {
  vi.clearAllMocks();
  resetRecipeIngestUploads();
  resetRecipeIngestCounts();
  localStorage.clear();
  api.createBatch.mockResolvedValue({ data: { id: "b1", source: "app" }, error: null });
  api.sealBatch.mockResolvedValue({ data: { id: "b1", source: "app" }, error: null });
  api.getCounts.mockResolvedValue({ data: { processing: 1, ready: 0 }, error: null });
  api.upload.mockResolvedValue({
    data: { batchId: "b1", jobs: [{ id: "j1", status: "processing", pageCount: 1, reviewPath: "" }], rejected: [], summary: "" },
    error: null,
  });
});

afterEach(() => {
  wrappers.forEach(wrapper => wrapper.unmount());
  wrappers.length = 0;
  resetRecipeIngestUploads();
});

describe("IngestCapture", () => {
  test("One side or Front & back, remembered in this browser", async () => {
    const wrapper = mountCapture();
    expect(wrapper.get(".btn-toggle").attributes("data-value")).toBe("one-side");

    await button(wrapper, ".mode-front-and-back").trigger("click");
    expect(wrapper.get(".btn-toggle").attributes("data-value")).toBe("front-and-back");
    expect(localStorage.getItem(CAPTURE_MODE_STORAGE_KEY)).toBe("front-and-back");
  });

  test("one side: each photo is a card and uploads at once", async () => {
    const wrapper = mountCapture();
    expect(button(wrapper, ".take-photo").text()).toBe("Take photo");
    expect(wrapper.get<HTMLInputElement>(".camera-input").attributes("capture")).toBe("environment");

    const [first] = [photo()];
    const reset = await pick(wrapper.get<HTMLInputElement>(".camera-input"), [first]);
    await flushPromises();

    expect(reset).toEqual([""]);
    expect(photosSent()).toEqual([[first]]);
    expect(button(wrapper, ".take-photo").text()).toBe("Next card");
    expect(wrapper.get(".cards-queued").text()).toBe("1 card queued");
  });

  test("front & back: Take photo, then Back side, then Next card", async () => {
    useRecipeIngestUploads().mode.value = "front-and-back";
    const wrapper = mountCapture();
    const [front, back] = [photo(), photo()];

    await pick(wrapper.get<HTMLInputElement>(".camera-input"), [front]);
    expect(button(wrapper, ".take-photo").text()).toBe("Back side");
    expect(wrapper.get(".pending-front").text()).toBe("Front");
    expect(wrapper.find(".no-back").exists()).toBe(true);
    expect(wrapper.find(".retake").exists()).toBe(true);
    expect(api.upload).not.toHaveBeenCalled();

    await pick(wrapper.get<HTMLInputElement>(".camera-input"), [back]);
    await flushPromises();
    expect(photosSent()).toEqual([[front, back]]);
    expect(button(wrapper, ".take-photo").text()).toBe("Next card");
    expect(wrapper.find(".pending-front").exists()).toBe(false);
  });

  test("Retake replaces the front; No back sends it alone", async () => {
    useRecipeIngestUploads().mode.value = "front-and-back";
    const wrapper = mountCapture();
    const [blurry, sharp] = [photo(), photo()];

    await pick(wrapper.get<HTMLInputElement>(".camera-input"), [blurry]);
    await button(wrapper, ".retake").trigger("click");
    await pick(wrapper.get<HTMLInputElement>(".camera-input"), [sharp]);
    expect(button(wrapper, ".take-photo").text()).toBe("Back side");

    await button(wrapper, ".no-back").trigger("click");
    await flushPromises();
    expect(photosSent()).toEqual([[sharp]]);
  });

  test("chosen photos pair up as cards, with Swap, Split, Join and Remove, then upload", async () => {
    useRecipeIngestUploads().mode.value = "front-and-back";
    const wrapper = mountCapture();
    const [a1, a2, b1, c1, c2] = [photo(), photo(), photo(), photo(), photo()];

    const reset = await pick(wrapper.get<HTMLInputElement>(".choose-input"), [a1, a2, b1, c1, c2]);
    expect(reset).toEqual([""]);
    expect(wrapper.get<HTMLInputElement>(".choose-input").attributes("multiple")).toBeDefined();
    expect(wrapper.get<HTMLInputElement>(".choose-input").attributes("capture")).toBeUndefined();

    const cards = () => wrapper.findAll(".draft-card");
    expect(cards().map(card => card.get("h4").text())).toEqual(["Card 1", "Card 2", "Card 3"]);
    expect(cards()[0]!.findAll("figcaption").map(caption => caption.text())).toEqual(["Front", "Back"]);
    // A lone photo has no back to swap or split
    expect(cards()[2]!.find(".draft-swap").exists()).toBe(false);
    expect(cards()[2]!.find(".draft-join").exists()).toBe(false);

    // B has no back: Split it, and C pairs up again
    await cards()[1]!.get(".draft-split").trigger("click");
    expect(cards()).toHaveLength(3);
    expect(cards()[1]!.findAll("img")).toHaveLength(1);
    expect(cards()[2]!.findAll("figcaption").map(caption => caption.text())).toEqual(["Front", "Back"]);

    await cards()[2]!.get(".draft-swap").trigger("click");
    await cards()[0]!.get(".draft-remove").trigger("click");
    expect(api.upload).not.toHaveBeenCalled();

    await button(wrapper, ".upload-drafts").trigger("click");
    await flushPromises();
    expect(photosSent()).toEqual([[b1], [c2, c1]]);
    expect(wrapper.find(".drafts").exists()).toBe(false);
  });

  test("Join is offered up to the page limit", async () => {
    const wrapper = mountCapture({ maxPagesPerCard: 2 });
    await pick(wrapper.get<HTMLInputElement>(".choose-input"), [photo(), photo(), photo()]);
    const cards = () => wrapper.findAll(".draft-card");
    expect(cards()).toHaveLength(3);

    await cards()[0]!.get(".draft-join").trigger("click");
    expect(cards()).toHaveLength(2);
    expect(cards()[0]!.find(".draft-join").exists()).toBe(false);
  });

  test("Done seals the batch once its cards have uploaded", async () => {
    const wrapper = mountCapture();
    expect(wrapper.find(".done").exists()).toBe(false);

    await pick(wrapper.get<HTMLInputElement>(".camera-input"), [photo()]);
    await flushPromises();
    await button(wrapper, ".done").trigger("click");
    await flushPromises();

    expect(api.sealBatch).toHaveBeenCalledExactlyOnceWith("b1", { suppressAlert: true });
    expect(wrapper.find(".done").exists()).toBe(false);
    expect(button(wrapper, ".take-photo").text()).toBe("Take photo");
  });

  test("photos dropped on the drop zone become cards", async () => {
    const wrapper = mountCapture();
    await nextTick();
    const files = [photo(), photo()];
    const drop = new Event("drop", { bubbles: true, cancelable: true });
    Object.defineProperty(drop, "dataTransfer", {
      value: { items: files.map(file => ({ kind: "file", type: file.type })), files, dropEffect: "none" },
    });
    wrapper.get(".drop-zone").element.dispatchEvent(drop);
    await nextTick();

    expect(wrapper.findAll(".draft-card")).toHaveLength(2);
    expect(wrapper.get(".drop-zone").text()).toContain("Drop photos here");
  });
});
