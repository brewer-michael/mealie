import { flushPromises, mount, type DOMWrapper, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { nextTick } from "vue";
import IngestCapture from "./IngestCapture.vue";
import { classicPdf, file, ftypHead, JPEG_HEAD, objectStreamPdf } from "~/composables/__tests__/use-recipe-ingest-files.fixtures";
import { resetRecipeIngestCounts } from "~/composables/use-recipe-ingest";
import {
  BATCH_HEARTBEAT_MS,
  CAPTURE_MODE_STORAGE_KEY,
  resetRecipeIngestUploads,
  useRecipeIngestUploads,
} from "~/composables/use-recipe-ingest-uploads";

const api = vi.hoisted(() => ({
  upload: vi.fn(),
  createBatch: vi.fn(),
  sealBatch: vi.fn(),
  touchBatch: vi.fn(),
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
  VSwitch: {
    props: ["modelValue", "label"],
    emits: ["update:modelValue"],
    template: `<label class="switch"><input type="checkbox" :checked="modelValue"
      @change="$emit('update:modelValue', $event.target.checked)">{{ label }}</label>`,
  },
  VAlert: {
    emits: ["click:close"],
    template: "<div class=\"alert\"><slot /><button class=\"alert-close\" @click=\"$emit('click:close')\" /></div>",
  },
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
  return new File([JPEG_HEAD, `photo ${photoCount}`], `IMG_${photoCount}.jpg`, { type: "image/jpeg" });
}

/** Drops files on the drop zone, as the browser does */
async function dropFiles(wrapper: VueWrapper, files: File[]) {
  const drop = new Event("drop", { bubbles: true, cancelable: true });
  Object.defineProperty(drop, "dataTransfer", {
    value: { items: files.map(file => ({ kind: "file", type: file.type })), files, dropEffect: "none" },
  });
  wrapper.get(".drop-zone").element.dispatchEvent(drop);
  await flushPromises();
  return drop;
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
  api.touchBatch.mockResolvedValue({ data: { id: "b1", source: "app" }, error: null });
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
  vi.useRealTimers();
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

  test("a card already scanned isn't counted as queued", async () => {
    api.upload.mockResolvedValueOnce({
      data: { batchId: "b1", jobs: [], rejected: [{ index: 0, reason: "duplicate", duplicateOf: "j0" }], summary: "" },
      error: null,
    });
    const wrapper = mountCapture();
    await pick(wrapper.get<HTMLInputElement>(".camera-input"), [photo()]);
    await flushPromises();
    expect(wrapper.find(".cards-queued").exists()).toBe(false);
    expect(button(wrapper, ".take-photo").text()).toBe("Next card");

    await pick(wrapper.get<HTMLInputElement>(".camera-input"), [photo()]);
    await flushPromises();
    expect(wrapper.get(".cards-queued").text()).toBe("1 card queued");
  });

  test("front & back: Take photo, then Back side, then Next card", async () => {
    useRecipeIngestUploads().mode.value = "front-and-back";
    const wrapper = mountCapture();
    const [front, back] = [photo(), photo()];

    await pick(wrapper.get<HTMLInputElement>(".camera-input"), [front]);
    expect(button(wrapper, ".take-photo").text()).toBe("Back side");
    expect(wrapper.get(".pending-front").text()).toContain("Front");
    expect(wrapper.find(".pending-front .no-back").exists()).toBe(true);
    expect(wrapper.find(".pending-front .retake").exists()).toBe(true);
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
    await flushPromises();
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
    expect(cards()[1]!.findAll(".ingest-capture-photo")).toHaveLength(1);
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
    await flushPromises();
    const cards = () => wrapper.findAll(".draft-card");
    expect(cards()).toHaveLength(3);

    await cards()[0]!.get(".draft-join").trigger("click");
    expect(cards()).toHaveLength(2);
    expect(cards()[0]!.find(".draft-join").exists()).toBe(false);
  });

  test("keeps its open batch open while it's shown, and stops when it closes", async () => {
    vi.useFakeTimers();
    const wrapper = mountCapture();
    await pick(wrapper.get<HTMLInputElement>(".camera-input"), [photo()]);
    await flushPromises();

    await vi.advanceTimersByTimeAsync(BATCH_HEARTBEAT_MS);
    expect(api.touchBatch).toHaveBeenCalledExactlyOnceWith("b1", { suppressAlert: true });

    wrapper.unmount();
    wrappers.length = 0;
    await vi.advanceTimersByTimeAsync(4 * BATCH_HEARTBEAT_MS);
    expect(api.touchBatch).toHaveBeenCalledOnce();
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
    const drop = await dropFiles(wrapper, [photo(), photo()]);

    expect(drop.defaultPrevented).toBe(true);
    expect(wrapper.findAll(".draft-card")).toHaveLength(2);
    expect(wrapper.get(".drop-zone").text()).toContain("Drop photos or PDFs here");
    expect(wrapper.find(".skipped-files").exists()).toBe(false);
  });
});

describe("IngestCapture: photos and PDFs only", () => {
  const SKIPPED_ONE = "Skipped 1 file: only photos (JPEG, PNG, WebP, HEIC, AVIF, TIFF) and PDFs can be scanned.";

  test("Choose offers photos and PDFs; the camera, photos", () => {
    const wrapper = mountCapture();
    expect(wrapper.get(".choose-input").attributes("accept")).toBe("image/*,application/pdf");
    expect(wrapper.get(".camera-input").attributes("accept")).toBe("image/*");
  });

  test("a dropped text file is skipped, and the panel says so; the browser doesn't open it", async () => {
    const wrapper = mountCapture();
    await nextTick();
    const drop = await dropFiles(wrapper, [file(["2 cups flour"], "notes.txt", "text/plain")]);

    expect(drop.defaultPrevented).toBe(true);
    expect(wrapper.find(".drafts").exists()).toBe(false);
    expect(wrapper.get(".skipped-unsupported").text()).toBe(`${SKIPPED_ONE} notes.txt`);
    expect(wrapper.find(".skipped-too-many-pages").exists()).toBe(false);
    // under the buttons, so the shutter doesn't move
    const children = Array.from(wrapper.get(".ingest-capture").element.children);
    expect(children.indexOf(wrapper.get(".skipped-files").element))
      .toBeGreaterThan(children.indexOf(wrapper.get(".capture-actions").element));

    await wrapper.get(".skipped-files .alert-close").trigger("click");
    expect(wrapper.find(".skipped-files").exists()).toBe(false);
    expect(api.upload).not.toHaveBeenCalled();
  });

  test("a mixed drop keeps the photos and the PDF, and names what it skipped", async () => {
    useRecipeIngestUploads().mode.value = "front-and-back";
    const wrapper = mountCapture();
    await nextTick();
    const [a, b] = [photo(), photo()];
    const scan = objectStreamPdf(2, { name: "scan.pdf" });
    await dropFiles(wrapper, [
      a,
      file(["2 cups flour"], "notes.txt", "text/plain"),
      file(["PK\u0003\u0004"], "recipe.docx"),
      scan,
      b,
    ]);
    // a compressed PDF takes a few more turns to read
    await vi.waitFor(() => expect(wrapper.findAll(".draft-card")).toHaveLength(3));

    expect(wrapper.get(".skipped-unsupported").text())
      .toBe("Skipped 2 files: only photos (JPEG, PNG, WebP, HEIC, AVIF, TIFF) and PDFs can be scanned. notes.txt, recipe.docx");
    const cards = wrapper.findAll(".draft-card");
    expect(cards.map(card => card.get("h4").text())).toEqual(["Card 1", "Card 2 · 2 pages", "Card 3"]);
    // the PDF: a placeholder with its name, never decoded
    expect(cards[1]!.get(".photo-placeholder").text()).toBe("scan.pdf");

    await button(wrapper, ".upload-drafts").trigger("click");
    await flushPromises();
    expect(photosSent()).toEqual([[a], [scan], [b]]);
  });

  test("Join counts a PDF's pages against the limit", async () => {
    const wrapper = mountCapture({ maxPagesPerCard: 3 });
    await pick(wrapper.get<HTMLInputElement>(".choose-input"), [classicPdf(2), photo(), photo()]);
    await flushPromises();
    const cards = () => wrapper.findAll(".draft-card");
    expect(cards()).toHaveLength(3);

    await cards()[0]!.get(".draft-join").trigger("click");
    expect(cards().map(card => card.get("h4").text())).toEqual(["Card 1 · 3 pages", "Card 2"]);
    expect(cards()[0]!.find(".draft-join").exists()).toBe(false);
  });

  test("a PDF with more pages than a card can have is skipped, and the panel says so", async () => {
    const wrapper = mountCapture({ maxPagesPerCard: 4 });
    await pick(wrapper.get<HTMLInputElement>(".choose-input"), [objectStreamPdf(6, { name: "booklet.pdf" }), photo()]);
    await vi.waitFor(() => expect(wrapper.find(".skipped-files").exists()).toBe(true));

    expect(wrapper.findAll(".draft-card")).toHaveLength(1);
    expect(wrapper.get(".skipped-too-many-pages").text())
      .toBe("Skipped 1 file with more than 4 pages: a card can have at most 4. booklet.pdf");
    expect(wrapper.find(".skipped-unsupported").exists()).toBe(false);

    // the next files replace the notice
    await pick(wrapper.get<HTMLInputElement>(".choose-input"), [photo()]);
    await flushPromises();
    expect(wrapper.find(".skipped-files").exists()).toBe(false);
  });

  test("a long list of skipped files names the first five", async () => {
    const wrapper = mountCapture();
    const texts = Array.from({ length: 7 }, (_, index) => file(["x"], `note-${index + 1}.txt`, "text/plain"));
    await pick(wrapper.get<HTMLInputElement>(".choose-input"), texts);
    await flushPromises();

    expect(wrapper.get(".skipped-names").text()).toBe("note-1.txt, note-2.txt, note-3.txt, note-4.txt, note-5.txt and 2 more");
  });

  test("a PDF given to the camera button (a computer's file picker) goes to the tray as a card of its own", async () => {
    useRecipeIngestUploads().mode.value = "front-and-back";
    const wrapper = mountCapture();
    await pick(wrapper.get<HTMLInputElement>(".camera-input"), [classicPdf(1, "front.pdf")]);
    await flushPromises();

    expect(wrapper.find(".pending-front").exists()).toBe(false);
    expect(wrapper.findAll(".draft-card")).toHaveLength(1);
    expect(api.upload).not.toHaveBeenCalled();
  });
});

describe("IngestCapture on a phone", () => {
  test("the shutter, Choose and Done share one row in every state; the waiting front comes below it", async () => {
    useRecipeIngestUploads().mode.value = "front-and-back";
    const wrapper = mountCapture();
    const row = () => wrapper.get(".capture-actions");
    const inRow = () => row().findAll("button").map(b => b.classes().find(c => ["take-photo", "choose-photos", "done"].includes(c)));

    expect(inRow()).toEqual(["take-photo", "choose-photos"]);
    await pick(wrapper.get<HTMLInputElement>(".camera-input"), [photo()]);
    // a front taken: Back side, Choose and Done stay where they were; No back and Retake go with the front
    expect(inRow()).toEqual(["take-photo", "choose-photos", "done"]);
    expect(row().find(".no-back").exists()).toBe(false);

    const children = Array.from(wrapper.get(".ingest-capture").element.children);
    expect(children.indexOf(wrapper.get(".pending-front").element))
      .toBeGreaterThan(children.indexOf(row().element));
    // a short label on phones, the full one from sm up
    expect(wrapper.get(".choose-photos .d-sm-none").text()).toBe("Choose");
    expect(wrapper.get(".choose-photos .d-none.d-sm-inline").text()).toBe("Choose photos");
  });
});

describe("IngestCapture thumbnails", () => {
  test("a photo the browser can't show is a card placeholder with its name", async () => {
    vi.stubGlobal("createImageBitmap", vi.fn(async () => {
      throw new DOMException("The source image could not be decoded.", "InvalidStateError");
    }));
    try {
      const wrapper = mountCapture();
      useRecipeIngestUploads().mode.value = "front-and-back";
      await pick(wrapper.get<HTMLInputElement>(".choose-input"), [
        new File([ftypHead("heic"), "a"], "IMG_0001.HEIC", { type: "image/heic" }),
        new File([ftypHead("heic"), "b"], "IMG_0002.HEIC", { type: "image/heic" }),
      ]);
      await flushPromises();

      const placeholders = wrapper.findAll(".draft-card .photo-placeholder");
      expect(placeholders).toHaveLength(2);
      expect(placeholders.map(p => p.text())).toEqual(["IMG_0001.HEIC", "IMG_0002.HEIC"]);
      expect(placeholders.map(p => p.attributes("aria-label"))).toEqual(["Front: IMG_0001.HEIC", "Back: IMG_0002.HEIC"]);
      expect(wrapper.find(".draft-card img").exists()).toBe(false);
    }
    finally {
      vi.unstubAllGlobals();
    }
  });

  test("a thumbnail the browser fails to show turns into the placeholder", async () => {
    vi.stubGlobal("createImageBitmap", undefined);
    vi.stubGlobal("URL", Object.assign(Object.create(URL), { createObjectURL: () => "blob:original", revokeObjectURL: vi.fn() }));
    try {
      const wrapper = mountCapture();
      await pick(wrapper.get<HTMLInputElement>(".choose-input"), [photo()]);
      await flushPromises();

      const img = wrapper.get(".draft-card img");
      expect(img.attributes("src")).toBe("blob:original");
      await img.trigger("error");
      expect(wrapper.find(".draft-card img").exists()).toBe(false);
      expect(wrapper.get(".draft-card .photo-placeholder").text()).toMatch(/^IMG_\d+\.jpg$/);
    }
    finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("IngestCapture options", () => {
  test("Data saver is off by default and remembered in this browser", async () => {
    const wrapper = mountCapture();
    const toggle = wrapper.get<HTMLInputElement>(".data-saver input");
    expect(toggle.element.checked).toBe(false);

    await toggle.setValue(true);
    expect(useRecipeIngestUploads().dataSaver.value).toBe(true);
    expect(localStorage.getItem("mealie.recipe-ingest.data-saver")).toBe("true");
  });

  test("a queue that can't be kept on this device says so, until dismissed", async () => {
    const wrapper = mountCapture();
    expect(wrapper.find(".storage-failed").exists()).toBe(false);
    useRecipeIngestUploads().storageFailed.value = true;
    await nextTick();
    expect(wrapper.get(".storage-failed").text()).toContain("keep this page open");
    await wrapper.get(".storage-failed .alert-close").trigger("click");
    expect(wrapper.find(".storage-failed").exists()).toBe(false);
  });
});
