import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { defineComponent } from "vue";
import IngestRegionDialog from "./IngestRegionDialog.vue";
import IngestRegionStencil from "./IngestRegionStencil.vue";
import { normalizeDraft, rereadTargets, rereadTargetValue } from "~/composables/use-recipe-ingest-review";
import type { PageOut, RegionHintOut } from "~/lib/api/types/recipe-ingest";

type Coordinates = { left: number; top: number; width: number; height: number };
type Transform = (params: { coordinates: Coordinates; imageSize: { width: number; height: number } }) => Coordinates;

const cropper = vi.hoisted(() => ({
  result: { coordinates: { left: 0, top: 0, width: 0, height: 0 }, image: { width: 2048, height: 1536 } },
  refresh: vi.fn(),
  setCoordinates: vi.fn(),
  props: [] as Record<string, unknown>[],
  instance: null as null | { $emit: (event: string, value: unknown) => void },
}));

vi.mock("vue-advanced-cropper", async () => ({
  Cropper: (await import("vue")).defineComponent({
    name: "Cropper",
    props: ["src", "canvas", "checkOrientation", "defaultSize", "defaultPosition", "stencilComponent", "stencilProps"],
    emits: ["change", "ready"],
    created() {
      cropper.props.push({ ...this.$props });
      cropper.instance = this as unknown as { $emit: (event: string, value: unknown) => void };
    },
    methods: {
      getResult: () => cropper.result,
      refresh: () => cropper.refresh(),
      setCoordinates: (transform: Transform, options: unknown) => cropper.setCoordinates(transform, options),
    },
    // the stencil stands in for the selection the cropper draws
    template: `
      <div class="cropper" :data-src="src">
        <div class="ingest-region-stencil" tabindex="0" :aria-label="stencilProps?.label" :aria-describedby="stencilProps?.describedBy" />
      </div>
    `,
  }),
  BoundingBox: { template: "<div><slot /></div>" },
  DraggableArea: { template: "<div><slot /></div>" },
  StencilPreview: { template: "<div />" },
}));

/** Applies the transform the dialog last gave `setCoordinates` to the cropper's current selection, in whole pixels */
function lastTransform(): Coordinates {
  const transform = cropper.setCoordinates.mock.calls.at(-1)![0] as Transform;
  const moved = transform({ coordinates: cropper.result.coordinates, imageSize: cropper.result.image });
  const round = (value: number) => Math.round(value * 100) / 100;
  return { left: round(moved.left), top: round(moved.top), width: round(moved.width), height: round(moved.height) };
}

function page(index: number): PageOut {
  const base = `/api/ai/ingest/jobs/j1/pages/${index}`;
  return {
    index,
    width: 1536,
    height: 2048,
    viewWidth: 1536,
    viewHeight: 2048,
    rotation: 0,
    rotationSource: "ocr",
    oriented: true,
    pageUrl: `${base}/page?v=abc`,
    viewUrl: `${base}/view?v=abc`,
    thumbUrl: `${base}/thumb?v=abc`,
  };
}

const draft = normalizeDraft({
  name: "Banana Mug Cake",
  ingredients: [{ referenceId: "i1", originalText: "1/4 t. salt", display: "1/4 teaspoon salt" }],
  steps: [{ id: "s1", text: "Mix." }, { id: "s2", text: "Microwave on high for [blank] minutes." }],
});
const targets = rereadTargets(draft);

const stubs = {
  BaseDialog: {
    props: ["modelValue", "title", "submitText", "submitDisabled"],
    emits: ["submit", "update:modelValue"],
    template: `
      <div v-if="modelValue" class="dialog" :data-title="title">
        <slot />
        <button type="button" class="submit" :disabled="submitDisabled" @click="$emit('submit')">{{ submitText }}</button>
      </div>
    `,
  },
  VCardText: { template: "<div><slot /></div>" },
  VBtnToggle: defineComponent({
    props: ["modelValue"],
    emits: ["update:modelValue"],
    template: "<div class=\"pages\" :data-selected=\"modelValue\" @click=\"pick\"><slot /></div>",
    methods: {
      pick(event: Event) {
        const value = (event.target as HTMLElement).closest("[data-value]")?.getAttribute("data-value");
        if (value !== null && value !== undefined) {
          this.$emit("update:modelValue", Number(value));
        }
      },
    },
  }),
  VBtn: { props: ["value"], template: "<button type=\"button\" :data-value=\"value\"><slot /></button>" },
  VSelect: {
    props: ["modelValue", "items", "label"],
    emits: ["update:modelValue"],
    template: `
      <select :aria-label="label" :value="modelValue" @change="$emit('update:modelValue', $event.target.value)">
        <option v-for="item in items" :key="item.value" :value="item.value">{{ item.title }}</option>
      </select>
    `,
  },
};

const wrappers: VueWrapper[] = [];

type DialogProps = {
  initialTarget?: string | null;
  initialPage?: number;
  pages?: PageOut[];
  initialRegion?: RegionHintOut | null;
  locating?: boolean;
};

function mountDialog(props: DialogProps = {}) {
  const wrapper = mount(IngestRegionDialog, {
    props: { modelValue: true, pages: [page(0), page(1)], targets, ...props },
    global: { stubs },
  });
  wrappers.push(wrapper);
  return wrapper;
}

describe("IngestRegionDialog", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    cropper.refresh.mockClear();
    cropper.setCoordinates.mockClear();
    cropper.props.length = 0;
    cropper.result = { coordinates: { left: 153.6, top: 1024, width: 1228.8, height: 204.8 }, image: { width: 1536, height: 2048 } };
  });

  afterEach(() => {
    wrappers.forEach(wrapper => wrapper.unmount());
    wrappers.length = 0;
    vi.useRealTimers();
  });

  test("sends the selection as fractions of the upright page, for the line it was opened from", async () => {
    const wrapper = mountDialog({ initialTarget: rereadTargetValue(targets, "steps", "s2") });
    await flushPromises();

    expect(wrapper.get(".dialog").attributes("data-title")).toBe("Re-read an area");
    expect((wrapper.get("select").element as HTMLSelectElement).value).toBe("steps:s2");
    expect(wrapper.findAll("option").map(option => option.text())).toContain("Step: 2");
    expect(cropper.props[0]).toMatchObject({ src: "/api/ai/ingest/jobs/j1/pages/0/view?v=abc", canvas: false, checkOrientation: false });

    await wrapper.get(".submit").trigger("click");

    expect(wrapper.emitted("submit")).toEqual([[{
      page: 0,
      x: 0.1,
      y: 0.5,
      width: 0.8,
      height: 0.1,
      target: { field: "steps", ref: "s2" },
    }]]);
    expect(wrapper.emitted("update:modelValue")).toEqual([[false]]);
  });

  test("the back of the card and another field can be chosen", async () => {
    const wrapper = mountDialog();
    await flushPromises();
    // opened from the toolbar or the ⋯ menu, nothing is preselected: the reviewer says what it's for first
    expect((wrapper.get("select").element as HTMLSelectElement).value).toBe("");
    expect(wrapper.get(".submit").attributes("disabled")).toBeDefined();

    await wrapper.findAll(".pages button")[1]!.trigger("click");
    await wrapper.get("select").setValue("ingredients:i1");
    expect(wrapper.get(".cropper").attributes("data-src")).toBe("/api/ai/ingest/jobs/j1/pages/1/view?v=abc");

    await wrapper.get(".submit").trigger("click");

    expect(wrapper.emitted("submit")![0]![0]).toMatchObject({ page: 1, target: { field: "ingredients", ref: "i1" } });
  });

  test("a line the reading missed is read as a new ingredient or step", async () => {
    const wrapper = mountDialog({ initialTarget: rereadTargetValue(targets, "steps", null) });
    await flushPromises();
    const titles = wrapper.findAll("option").map(option => option.text());
    expect(titles).toContain("Add ingredient");
    expect(titles).toContain("Add step");
    expect((wrapper.get("select").element as HTMLSelectElement).value).toBe("steps:new");

    await wrapper.get(".submit").trigger("click");

    expect(wrapper.emitted("submit")![0]![0]).toMatchObject({ target: { field: "steps", ref: null } });
  });

  test("a sliver isn't sent: the dialog stays open and says to select more", async () => {
    cropper.result = { coordinates: { left: 0, top: 0, width: 1536, height: 10 }, image: { width: 1536, height: 2048 } };
    const wrapper = mountDialog({ initialTarget: "name" });
    await flushPromises();

    await wrapper.get(".submit").trigger("click");

    expect(wrapper.emitted("submit")).toBeUndefined();
    expect(wrapper.emitted("update:modelValue")).toBeUndefined();
    expect(wrapper.get("[role=alert]").text()).toBe("Select a larger area");
  });

  test("the cropper measures itself again once the dialog has opened", async () => {
    mountDialog();
    await flushPromises();
    expect(cropper.refresh).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(300);
    expect(cropper.refresh).toHaveBeenCalled();
  });

  test("a card without pages can't be sent", async () => {
    const wrapper = mountDialog({ pages: [] });
    await flushPromises();
    expect(wrapper.get(".submit").attributes("disabled")).toBeDefined();
  });
  test("the selection follows a finger from the first pixel: the dialog draws it with its own stencil", async () => {
    mountDialog({ initialTarget: "name" });
    await flushPromises();

    expect(cropper.props[0]!.stencilComponent).toBe(IngestRegionStencil);
    expect(cropper.props[0]!.stencilProps).toMatchObject({ label: "Selected area" });
  });

  test("arrow keys on the selection move it by 2% of the page; Shift and arrow keys resize it", async () => {
    // a band from 10% to 90% across, 50% to 60% down, of a 1536 x 2048 page
    const wrapper = mountDialog({ initialTarget: "name" });
    await flushPromises();
    const selection = wrapper.get(".ingest-region-stencil");
    expect(wrapper.get(`[id="${selection.attributes("aria-describedby")}"]`).text())
      .toBe("Arrow keys move the selected area. Shift and arrow keys resize it.");

    await selection.trigger("keydown", { key: "ArrowDown" });
    expect(cropper.setCoordinates).toHaveBeenCalledOnce();
    expect(cropper.setCoordinates.mock.calls[0]![1]).toEqual({ transitions: false });
    expect(lastTransform()).toEqual({ left: 153.6, top: 1064.96, width: 1228.8, height: 204.8 });

    await selection.trigger("keydown", { key: "ArrowLeft" });
    expect(lastTransform()).toEqual({ left: 122.88, top: 1024, width: 1228.8, height: 204.8 });

    await selection.trigger("keydown", { key: "ArrowRight", shiftKey: true });
    expect(lastTransform()).toEqual({ left: 153.6, top: 1024, width: 1259.52, height: 204.8 });

    await selection.trigger("keydown", { key: "ArrowUp", shiftKey: true });
    expect(lastTransform()).toEqual({ left: 153.6, top: 1024, width: 1228.8, height: 163.84 });

    // other keys, and arrows anywhere else in the dialog, leave it alone
    await selection.trigger("keydown", { key: "Enter" });
    await wrapper.get(".cropper").trigger("keydown", { key: "ArrowDown" });
    expect(cropper.setCoordinates).toHaveBeenCalledTimes(4);
  });

  test("after an arrow key a screen reader hears where the selection is", async () => {
    const wrapper = mountDialog({ initialTarget: "name" });
    await flushPromises();
    const live = wrapper.get(".ingest-region-dialog__position");
    expect(live.attributes("aria-live")).toBe("polite");

    // a drag with the pointer isn't read out
    cropper.instance!.$emit("change", cropper.result);
    await flushPromises();
    expect(live.text()).toBe("");

    await wrapper.get(".ingest-region-stencil").trigger("keydown", { key: "ArrowDown" });
    cropper.result = { coordinates: { left: 153.6, top: 1064.96, width: 1228.8, height: 204.8 }, image: { width: 1536, height: 2048 } };
    cropper.instance!.$emit("change", cropper.result);
    await flushPromises();
    expect(live.text()).toBe("10% from the left, 52% from the top, 80% wide, 10% high");
  });

  // ==========================================
  // Where the selection starts (FR-03)

  const image = { width: 1536, height: 2048 };
  /** Where the cropper last mounted starts its selection, in the image's pixels, given a selection of `size` */
  function startOf(size?: { width: number; height: number }) {
    const props = cropper.props.at(-1)!;
    const defaultSize = props.defaultSize as (params: { imageSize: typeof image }) => { width: number; height: number };
    const defaultPosition = props.defaultPosition as (params: { coordinates: { left: number; top: number; width: number; height: number }; imageSize: typeof image }) => { left: number; top: number };
    const sized = size ?? defaultSize({ imageSize: image });
    const position = defaultPosition({ coordinates: { left: 0, top: 0, ...sized }, imageSize: image });
    const round = (value: number) => Math.round(value * 100) / 100;
    return { left: round(position.left), top: round(position.top), width: round(sized.width), height: round(sized.height) };
  }

  // the server's band across the card at the line's height; the selection reaches 5% further either side, as the
  // card's writing often starts nearer its edge
  const ingredientHint: RegionHintOut = { page: 1, x: 0.05, y: 0.3, width: 0.9, height: 0.06, source: "ocr" };

  test("opened from a flagged line, the selection starts on that line, on the page it's written on", async () => {
    const wrapper = mountDialog({ initialTarget: "ingredients:i1", initialRegion: ingredientHint });
    await flushPromises();

    expect(wrapper.get(".cropper").attributes("data-src")).toBe("/api/ai/ingest/jobs/j1/pages/1/view?v=abc");
    expect(startOf()).toEqual({ left: 0, top: 614.4, width: 1536, height: 122.88 });

    // the other page has no hint: it starts with the band across the middle
    await wrapper.findAll(".pages button")[0]!.trigger("click");
    expect(wrapper.get(".cropper").attributes("data-src")).toBe("/api/ai/ingest/jobs/j1/pages/0/view?v=abc");
    expect(startOf()).toEqual({ left: 76.8, top: 819.2, width: 1382.4, height: 409.6 });
  });

  test("while the server says where the line is, the selection waits, then starts there", async () => {
    const wrapper = mountDialog({ initialTarget: "ingredients:i1", locating: true });
    await flushPromises();
    expect(wrapper.find(".cropper").exists()).toBe(false);
    expect(wrapper.find(".ingest-region-dialog__locating").exists()).toBe(true);
    expect(wrapper.get(".submit").attributes("disabled")).toBeDefined();

    await wrapper.setProps({ locating: false, initialRegion: { ...ingredientHint, page: 0 } });
    await flushPromises();
    expect(wrapper.find(".ingest-region-dialog__locating").exists()).toBe(false);
    expect(wrapper.get(".cropper").attributes("data-src")).toBe("/api/ai/ingest/jobs/j1/pages/0/view?v=abc");
    expect(startOf()).toEqual({ left: 0, top: 614.4, width: 1536, height: 122.88 });
  });

  test("a page picked while the server says where the line is stays picked", async () => {
    const wrapper = mountDialog({ initialTarget: "ingredients:i1", locating: true });
    await flushPromises();
    await wrapper.findAll(".pages button")[1]!.trigger("click");

    await wrapper.setProps({ locating: false, initialRegion: { ...ingredientHint, page: 0 } });
    await flushPromises();
    expect(wrapper.get(".cropper").attributes("data-src")).toBe("/api/ai/ingest/jobs/j1/pages/1/view?v=abc");
    // the hint is for the front: the back starts as a band
    expect(startOf()).toEqual({ left: 76.8, top: 819.2, width: 1382.4, height: 409.6 });
  });

  test("without a hint, the selection starts where the last one read on that page was, else as a band", async () => {
    const wrapper = mountDialog({ initialTarget: "name" });
    await flushPromises();
    expect(startOf()).toEqual({ left: 76.8, top: 819.2, width: 1382.4, height: 409.6 });

    // a re-read of the top of the front
    cropper.result = { coordinates: { left: 153.6, top: 204.8, width: 1228.8, height: 409.6 }, image };
    await wrapper.get(".submit").trigger("click");

    await wrapper.setProps({ modelValue: false });
    await wrapper.setProps({ modelValue: true, initialTarget: "description" });
    await flushPromises();
    expect(startOf()).toEqual({ left: 153.6, top: 204.8, width: 1228.8, height: 409.6 });

    // the back hasn't been read from yet
    await wrapper.findAll(".pages button")[1]!.trigger("click");
    expect(startOf()).toEqual({ left: 76.8, top: 819.2, width: 1382.4, height: 409.6 });

    // a hint for the line opened from still comes first
    await wrapper.setProps({ modelValue: false });
    await wrapper.setProps({ modelValue: true, initialPage: 0, initialRegion: { ...ingredientHint, page: 0 } });
    await flushPromises();
    expect(startOf()).toEqual({ left: 0, top: 614.4, width: 1536, height: 122.88 });
  });

  test("each opening starts the selection afresh", async () => {
    const wrapper = mountDialog({ initialTarget: "name" });
    await flushPromises();
    const mounted = cropper.props.length;

    await wrapper.setProps({ modelValue: false });
    await wrapper.setProps({ modelValue: true });
    await flushPromises();
    expect(cropper.props.length).toBe(mounted + 1);
  });
});
