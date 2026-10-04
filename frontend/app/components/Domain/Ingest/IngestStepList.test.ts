import { mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, describe, expect, test } from "vitest";
import { reactive } from "vue";
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
  VBtn: { emits: ["click"], template: "<button type=\"button\" @click=\"$emit('click')\"><slot /></button>" },
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

function mountWith<T>(component: T, model: ReviewDraft, flags: CardFlag[] = [], readonly = false) {
  const wrapper = mount(component as never, {
    props: { modelValue: model, flags, readonly },
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

  test("nothing can be added or removed while read-only", () => {
    const wrapper = mountWith(IngestStepList, draft(), [], true);
    expect(wrapper.findAll("button")).toHaveLength(0);
  });
});
