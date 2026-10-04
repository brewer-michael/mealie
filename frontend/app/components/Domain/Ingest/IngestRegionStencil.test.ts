import { mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, describe, expect, test } from "vitest";
import { RectangleStencil } from "vue-advanced-cropper";
import IngestRegionStencil from "./IngestRegionStencil.vue";

const wrappers: VueWrapper[] = [];

function mountStencil(component: typeof IngestRegionStencil | typeof RectangleStencil, props: Record<string, unknown> = {}) {
  const wrapper = mount(component, {
    props: { stencilCoordinates: { left: 10, top: 20, width: 300, height: 100 }, ...props },
    attachTo: document.body,
  });
  wrappers.push(wrapper);
  return wrapper;
}

/** A touch event at these points (jsdom has no `Touch`, so the list is set on a plain event) */
function touchEvent(type: string, points: { x: number; y: number }[]) {
  const event = new Event(type, { bubbles: true, cancelable: true });
  const touches = points.map(point => ({ clientX: point.x, clientY: point.y }));
  Object.defineProperty(event, "touches", { value: touches });
  Object.defineProperty(event, "changedTouches", { value: touches });
  return event;
}

/** The finger lands at 100,100 and moves straight down through `ys` */
function drag(wrapper: VueWrapper, ys: number[]) {
  const area = wrapper.get(".vue-rectangle-stencil__preview").element.parentElement!;
  area.dispatchEvent(touchEvent("touchstart", [{ x: 100, y: 100 }]));
  ys.forEach(y => window.dispatchEvent(touchEvent("touchmove", [{ x: 100, y }])));
  window.dispatchEvent(touchEvent("touchend", []));
}

function movesDown(wrapper: VueWrapper): number[] {
  return (wrapper.emitted("move") ?? []).map(([event]) => (event as { directions: { top: number } }).directions.top);
}

describe("IngestRegionStencil", () => {
  afterEach(() => {
    wrappers.forEach(wrapper => wrapper.unmount());
    wrappers.length = 0;
  });

  test("a touch drag moves the selection from the first pixel", () => {
    const wrapper = mountStencil(IngestRegionStencil);

    drag(wrapper, [104, 110, 220]);

    // each move is measured from where the finger landed
    expect(movesDown(wrapper)).toEqual([4, 10, 120]);
    expect(wrapper.emitted("move-end")).toHaveLength(1);
  });

  test("the library's own stencil waits 20 px and starts from there (what this stencil replaces)", () => {
    const wrapper = mountStencil(RectangleStencil);

    drag(wrapper, [104, 110, 220, 230]);

    // anchored at 220, so a 130 px drag moved the box 10 px
    expect(movesDown(wrapper)).toEqual([10]);
  });

  test("a second finger is a pinch, left to the image", () => {
    const wrapper = mountStencil(IngestRegionStencil);
    const area = wrapper.get(".vue-rectangle-stencil__preview").element.parentElement!;

    const pinch = touchEvent("touchstart", [{ x: 100, y: 100 }, { x: 200, y: 200 }]);
    area.dispatchEvent(pinch);
    window.dispatchEvent(touchEvent("touchmove", [{ x: 100, y: 150 }, { x: 200, y: 260 }]));

    expect(pinch.defaultPrevented).toBe(false);
    expect(wrapper.emitted("move")).toBeUndefined();
  });

  test("sits where the cropper puts it, takes the focus and names itself", () => {
    const wrapper = mountStencil(IngestRegionStencil, { label: "Selected area", describedBy: "keys-hint" });

    const root = wrapper.get(".ingest-region-stencil");
    expect(root.attributes("style")).toContain("width: 300px; height: 100px; transform: translate(10px, 20px);");
    expect(root.attributes("tabindex")).toBe("0");
    expect(root.attributes("aria-label")).toBe("Selected area");
    expect(root.attributes("aria-describedby")).toBe("keys-hint");
    // resize handles, as the library's stencil has
    expect(wrapper.findAll(".vue-handler-wrapper").length).toBe(8);
    // no aspect ratio: the cropper asks the stencil for one
    expect((wrapper.vm as unknown as { aspectRatios: () => object }).aspectRatios()).toEqual({ minimum: undefined, maximum: undefined });
  });
});
