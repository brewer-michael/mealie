import { mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, describe, expect, test } from "vitest";
import IngestCardViewer from "./IngestCardViewer.vue";
import type { PageOut } from "~/lib/api/types/recipe-ingest";

function page(index: number): PageOut {
  const base = `/api/ai/ingest/jobs/j1/pages/${index}`;
  return {
    index,
    width: 1536,
    height: 2048,
    viewWidth: 1536,
    viewHeight: 2048,
    rotation: 0,
    rotationSource: "none",
    oriented: true,
    pageUrl: `${base}/page?v=abc`,
    viewUrl: `${base}/view?v=abc`,
    thumbUrl: `${base}/thumb?v=abc`,
  };
}

const stubs = {
  VIcon: { template: "<i />" },
  VSpacer: { template: "<span />" },
  VBtnToggle: { template: "<div class=\"pages\"><slot /></div>" },
  VBtn: {
    props: ["disabled", "value"],
    emits: ["click"],
    template: "<button type=\"button\" :data-value=\"value\" :disabled=\"disabled\" @click=\"$emit('click')\"><slot /></button>",
  },
  RecipeImageLightbox: {
    props: ["modelValue", "imageUrl"],
    template: "<div class=\"lightbox\" :data-open=\"modelValue\" :data-url=\"imageUrl\" />",
  },
  IngestTranscription: { props: ["text"], template: "<pre class=\"transcription\">{{ text }}</pre>" },
};

const wrappers: VueWrapper[] = [];

function mountViewer(props: Record<string, unknown>) {
  const wrapper = mount(IngestCardViewer, {
    props: { pages: [page(0), page(1)], ...props },
    global: { mocks: { $globals: { icons: {} } }, stubs },
  });
  wrappers.push(wrapper);
  return wrapper;
}

function button(wrapper: VueWrapper, text: string) {
  const found = wrapper.findAll("button").find(b => b.text() === text || b.attributes("aria-label") === text);
  if (!found) {
    throw new Error(`No ${text} button`);
  }
  return found;
}

describe("IngestCardViewer", () => {
  afterEach(() => {
    wrappers.forEach(wrapper => wrapper.unmount());
    wrappers.length = 0;
  });

  test("on a phone, a strip of the front; tap for the full page", async () => {
    const wrapper = mountViewer({ mode: "strip" });

    expect(wrapper.classes()).toContain("ingest-card-strip");
    expect(wrapper.get("img").attributes("src")).toBe("/api/ai/ingest/jobs/j1/pages/0/view?v=abc");
    expect(wrapper.findAll(".pages button").map(b => b.text())).toEqual(["Front", "Back"]);

    await button(wrapper, "Full screen").trigger("click");
    expect(wrapper.get(".lightbox").attributes("data-open")).toBe("true");
    expect(wrapper.get(".lightbox").attributes("data-url")).toBe("/api/ai/ingest/jobs/j1/pages/0/page?v=abc");
  });

  test("swiping down folds the strip to a bar, and the bar opens it again", async () => {
    const wrapper = mountViewer({ mode: "strip" });

    await wrapper.trigger("touchstart", { touches: [{ clientY: 100 }] });
    await wrapper.trigger("touchend", { changedTouches: [{ clientY: 180 }] });
    expect(wrapper.classes()).toContain("ingest-card-strip--collapsed");
    expect(wrapper.get("img").attributes("src")).toBe("/api/ai/ingest/jobs/j1/pages/0/thumb?v=abc");

    await wrapper.get(".ingest-card-strip__bar").trigger("click");
    expect(wrapper.classes()).not.toContain("ingest-card-strip--collapsed");
  });

  test("shows the page chosen", () => {
    const wrapper = mountViewer({ mode: "strip", page: 1 });
    expect(wrapper.get("img").attributes("src")).toBe("/api/ai/ingest/jobs/j1/pages/1/view?v=abc");
  });

  test("on desktop, rotate, re-read and what the card says", async () => {
    const wrapper = mountViewer({ mode: "panel", page: 1, transcription: "Banana Mug Cake\nMicrowave [blank] minutes" });

    expect(wrapper.classes()).toContain("ingest-card-panel");
    await button(wrapper, "Rotate").trigger("click");
    await button(wrapper, "Re-read an area").trigger("click");
    expect(wrapper.emitted("rotate")).toEqual([[1]]);
    expect(wrapper.emitted("reread")).toHaveLength(1);

    await button(wrapper, "What the card says").trigger("click");
    expect(wrapper.emitted("update:transcriptionOpen")).toEqual([[true]]);
    await wrapper.setProps({ transcriptionOpen: true });
    expect(wrapper.get(".transcription").text()).toBe("Banana Mug Cake\nMicrowave [blank] minutes");
    expect(wrapper.find("img").exists()).toBe(false);
  });

  test("rotate and re-read are off while the card can't change", () => {
    const wrapper = mountViewer({ mode: "panel", readonly: true });
    expect(button(wrapper, "Rotate").attributes("disabled")).toBeDefined();
    expect(button(wrapper, "Re-read an area").attributes("disabled")).toBeDefined();
  });
});
