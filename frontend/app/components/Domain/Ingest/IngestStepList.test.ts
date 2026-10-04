import { mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, describe, expect, test } from "vitest";
import { reactive } from "vue";
import { VueDraggable } from "vue-draggable-plus";
import IngestStepList from "./IngestStepList.vue";
import { normalizeDraft, type ReviewDraft } from "~/composables/use-recipe-ingest-review";
import type { CardFlag } from "~/lib/api/types/recipe-ingest";

const field = {
  props: ["modelValue", "label", "appendInnerIcon", "readonly"],
  emits: ["update:modelValue"],
  template: `
    <label class="field" :data-icon="appendInnerIcon">{{ label }}
      <input :value="modelValue" :readonly="readonly" @input="$emit('update:modelValue', $event.target.value)">
    </label>
  `,
};

const stubs = {
  VTextField: field,
  VTextarea: field,
  VIcon: { template: "<i class=\"icon\" />" },
  VSpacer: { template: "<span />" },
  VBtn: {
    props: ["disabled"],
    emits: ["click"],
    template: "<button type=\"button\" :class=\"$attrs.class\" :aria-label=\"$attrs['aria-label']\" :disabled=\"disabled\" @click=\"$emit('click')\"><slot /></button>",
  },
  VueDraggable: {
    name: "VueDraggable",
    props: ["modelValue", "disabled", "handle"],
    emits: ["update:modelValue"],
    template: "<div class=\"draggable\"><slot /></div>",
  },
};

const icons = { alert: "warning-icon", alertCircle: "error-icon" };
const wrappers: VueWrapper[] = [];

function draft(): ReviewDraft {
  return reactive(normalizeDraft({
    name: "Banana Mug Cake",
    attribution: "From Grandma Jo",
    recipeServings: 1,
    prepTime: null,
    steps: [{ id: "s1", text: "Mix." }, { id: "s2", text: "Microwave on high for [blank] minutes." }],
  }));
}

function mountWith(component: typeof IngestStepList, model: ReviewDraft, flags: CardFlag[] = [], readonly = false, extra: Record<string, unknown> = {}) {
  const wrapper = mount(component, {
    props: { modelValue: model, flags, readonly, ...extra },
    global: { mocks: { $globals: { icons } }, stubs },
  });
  wrappers.push(wrapper);
  return wrapper;
}

function input(wrapper: VueWrapper, label: string) {
  const found = wrapper.findAll("label").find(l => l.text().startsWith(label));
  if (!found) {
    throw new Error(`No ${label} field`);
  }
  return found;
}

afterEach(() => {
  wrappers.forEach(wrapper => wrapper.unmount());
  wrappers.length = 0;
});

describe("IngestStepList", () => {
  test("plain title and text fields, with the flagged step highlighted", () => {
    const flags: CardFlag[] = [{ id: "blank:steps:s2", kind: "blank", severity: "error", source: "marker", field: "steps", ref: "s2" }];
    const wrapper = mountWith(IngestStepList, draft(), flags);

    expect(wrapper.findAll(".ingest-step").map(step => step.attributes("id"))).toEqual(["ingest-field-steps-s1", "ingest-field-steps-s2"]);
    expect(wrapper.get("#ingest-field-steps-s2").classes()).toContain("ingest-step--error");
    expect(input(wrapper, "Step: 2").attributes("data-icon")).toBe("error-icon");
  });

  test("steps can be edited, added and removed", async () => {
    const model = draft();
    const wrapper = mountWith(IngestStepList, model);

    await input(wrapper, "Step: 2").get("input").setValue("Microwave on high for 2 minutes.");
    await wrapper.findAll("button").find(b => b.text() === "Add step")!.trigger("click");
    expect(model.steps.map(step => step.text)).toEqual(["Mix.", "Microwave on high for 2 minutes.", ""]);
    expect(model.steps[2]!.id).toBeTruthy();

    await wrapper.findAll("button").find(b => b.text() === "Delete")!.trigger("click");
    expect(model.steps.map(step => step.text)).toEqual(["Microwave on high for 2 minutes.", ""]);
  });

  test("steps move up and down; their ids go with them, so their flags follow", async () => {
    const flags: CardFlag[] = [{ id: "blank:steps:s2", kind: "blank", severity: "error", source: "marker", field: "steps", ref: "s2" }];
    const model = draft();
    const wrapper = mountWith(IngestStepList, model, flags);

    const ups = () => wrapper.findAll(".ingest-step__move-up");
    const downs = () => wrapper.findAll(".ingest-step__move-down");
    expect(ups()[0]!.attributes("disabled")).toBeDefined();
    expect(downs()[1]!.attributes("disabled")).toBeDefined();

    await ups()[1]!.trigger("click");
    expect(model.steps.map(step => step.id)).toEqual(["s2", "s1"]);
    expect(wrapper.findAll(".ingest-step")[0]!.classes()).toContain("ingest-step--error");
    expect(input(wrapper, "Step: 1").attributes("data-icon")).toBe("error-icon");

    await downs()[0]!.trigger("click");
    expect(model.steps.map(step => step.id)).toEqual(["s1", "s2"]);
  });

  test("on desktop steps are dragged by their handles (upstream's sortable list)", async () => {
    const model = draft();
    const wrapper = mountWith(IngestStepList, model, [], false, { draggable: true });

    const sortable = wrapper.getComponent(VueDraggable);
    expect(sortable.props("disabled")).toBe(false);
    expect(sortable.props("handle")).toBe(".ingest-step__handle");
    expect(wrapper.findAll(".ingest-step__handle")).toHaveLength(2);

    sortable.vm.$emit("update:modelValue", [model.steps[1], model.steps[0]]);
    await wrapper.vm.$nextTick();
    expect(model.steps.map(step => step.id)).toEqual(["s2", "s1"]);

    expect(mountWith(IngestStepList, draft()).getComponent(VueDraggable).props("disabled")).toBe(true);
  });

  test("each step re-reads its own area of the card, when the card can be read", async () => {
    expect(mountWith(IngestStepList, draft()).find(".ingest-step__reread").exists()).toBe(false);

    const wrapper = mountWith(IngestStepList, draft(), [], false, { canReread: true });
    await wrapper.findAll(".ingest-step__reread")[1]!.trigger("click");
    expect(wrapper.emitted("reread")).toEqual([["s2"]]);
  });

  test("nothing can be added or removed while read-only", () => {
    const wrapper = mountWith(IngestStepList, draft(), [], true);
    expect(wrapper.findAll("button")).toHaveLength(0);
  });
});
