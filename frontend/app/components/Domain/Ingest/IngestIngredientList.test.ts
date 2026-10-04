import { mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, describe, expect, test, vi } from "vitest";
import { reactive } from "vue";
import { VueDraggable } from "vue-draggable-plus";
import IngestIngredientList from "./IngestIngredientList.vue";
import { normalizeDraft, type ReviewDraft } from "~/composables/use-recipe-ingest-review";
import type { CardFlag } from "~/lib/api/types/recipe-ingest";

vi.mock("~/composables/store/use-food-store", async () => {
  const { ref } = await import("vue");
  return { useFoodStore: () => ({ store: ref([]) }) };
});
vi.mock("~/composables/store/use-unit-store", async () => {
  const { ref } = await import("vue");
  return { useUnitStore: () => ({ store: ref([]) }) };
});

const field = {
  props: ["modelValue", "label"],
  template: "<label class=\"field\">{{ label }}<input :value=\"modelValue\"></label>",
};

const stubs = {
  VIcon: { props: ["icon", "color"], template: "<i class=\"icon\" :data-color=\"color\" />" },
  VChip: { template: "<span class=\"chip\"><slot /></span>" },
  VSpacer: { template: "<span />" },
  VBtn: {
    props: ["disabled"],
    emits: ["click"],
    template: "<button type=\"button\" :class=\"$attrs.class\" :aria-label=\"$attrs['aria-label']\" :disabled=\"disabled\" @click=\"$emit('click')\"><slot /></button>",
  },
  VTextField: field,
  VCombobox: field,
  VueDraggable: {
    name: "VueDraggable",
    props: ["modelValue", "disabled", "handle"],
    emits: ["update:modelValue"],
    template: "<div class=\"draggable\" :data-disabled=\"disabled\" :data-handle=\"handle\"><slot /></div>",
  },
};

const wrappers: VueWrapper[] = [];

function draft(): ReviewDraft {
  return reactive(normalizeDraft({
    ingredients: [
      { referenceId: "i1", originalText: "1 T. coconut oil", note: "1 T. coconut oil" },
      { referenceId: "i2", originalText: "1/4 t. salt", note: "1/4 t. salt" },
      { referenceId: "i3", originalText: "1 banana", note: "1 banana" },
    ],
  }));
}

const unsure: CardFlag = { id: "unsure:ingredients:i2", kind: "unsure", severity: "warning", source: "model", field: "ingredients", ref: "i2" };

function mountList(model: ReviewDraft, props: Record<string, unknown> = {}) {
  const wrapper = mount(IngestIngredientList, {
    props: { modelValue: model, flags: [unsure], ...props },
    global: { mocks: { $globals: { icons: {} } }, stubs },
  });
  wrappers.push(wrapper);
  return wrapper;
}

const order = (model: ReviewDraft) => model.ingredients.map(item => item.referenceId);

describe("IngestIngredientList", () => {
  afterEach(() => {
    wrappers.forEach(wrapper => wrapper.unmount());
    wrappers.length = 0;
  });

  test("the open line moves up and down; its id goes with it, so its flag follows", async () => {
    const model = draft();
    const wrapper = mountList(model, { expanded: "i2" });

    await wrapper.get("[aria-label=\"Move up\"]").trigger("click");
    expect(order(model)).toEqual(["i2", "i1", "i3"]);
    // the flag is on the line, wherever it is now
    expect(wrapper.findAll(".ingest-ingredient")[0]!.classes()).toContain("ingest-ingredient--warning");
    // at the top, up is off
    expect(wrapper.get("[aria-label=\"Move up\"]").attributes("disabled")).toBeDefined();

    await wrapper.get("[aria-label=\"Move down\"]").trigger("click");
    await wrapper.get("[aria-label=\"Move down\"]").trigger("click");
    expect(order(model)).toEqual(["i1", "i3", "i2"]);
    expect(wrapper.get("[aria-label=\"Move down\"]").attributes("disabled")).toBeDefined();
    expect(wrapper.findAll(".ingest-ingredient")[2]!.classes()).toContain("ingest-ingredient--warning");
  });

  test("on desktop the lines are dragged by their handles (upstream's sortable list)", async () => {
    const model = draft();
    const wrapper = mountList(model, { draggable: true });

    const sortable = wrapper.getComponent(VueDraggable);
    expect(sortable.props("disabled")).toBe(false);
    expect(sortable.props("handle")).toBe(".ingest-ingredient__handle");
    expect(wrapper.findAll(".ingest-ingredient__handle")).toHaveLength(3);

    sortable.vm.$emit("update:modelValue", [model.ingredients[2], model.ingredients[0], model.ingredients[1]]);
    await wrapper.vm.$nextTick();
    expect(order(model)).toEqual(["i3", "i1", "i2"]);
  });

  test("no dragging on phones (scrolling) or while read-only", () => {
    expect(mountList(draft()).getComponent(VueDraggable).props("disabled")).toBe(true);
    expect(mountList(draft(), { draggable: true, readonly: true }).getComponent(VueDraggable).props("disabled")).toBe(true);
  });

  test("the open line's Re-read asks for its own area of the card", async () => {
    const wrapper = mountList(draft(), { expanded: "i3", canReread: true });
    await wrapper.get(".ingest-ingredient__reread").trigger("click");
    expect(wrapper.emitted("reread")).toEqual([["i3"]]);
  });
});
