import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { defineComponent } from "vue";
import IngestRegionDialog from "./IngestRegionDialog.vue";
import { normalizeDraft, rereadTargets, rereadTargetValue } from "~/composables/use-recipe-ingest-review";
import type { PageOut } from "~/lib/api/types/recipe-ingest";

const cropper = vi.hoisted(() => ({
  result: { coordinates: { left: 0, top: 0, width: 0, height: 0 }, image: { width: 2048, height: 1536 } },
  refresh: vi.fn(),
  props: [] as Record<string, unknown>[],
}));

vi.mock("vue-advanced-cropper", async () => ({
  Cropper: (await import("vue")).defineComponent({
    name: "Cropper",
    props: ["src", "canvas", "checkOrientation", "defaultSize"],
    created() {
      cropper.props.push({ ...this.$props });
    },
    methods: {
      getResult: () => cropper.result,
      refresh: () => cropper.refresh(),
    },
    template: "<div class=\"cropper\" :data-src=\"src\" />",
  }),
}));

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

function mountDialog(props: { initialTarget?: string | null; initialPage?: number; pages?: PageOut[] } = {}) {
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
    // opened from the toolbar, the first field is preselected
    expect((wrapper.get("select").element as HTMLSelectElement).value).toBe("name");

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
});
