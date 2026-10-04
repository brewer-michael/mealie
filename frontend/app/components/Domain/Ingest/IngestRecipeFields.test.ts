import { mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, describe, expect, test } from "vitest";
import { reactive } from "vue";
import IngestRecipeFields from "./IngestRecipeFields.vue";
import { normalizeDraft, type ReviewDraft } from "~/composables/use-recipe-ingest-review";
import type { CardFlag } from "~/lib/api/types/recipe-ingest";

const field = {
  props: ["modelValue", "label", "appendInnerIcon", "readonly", "errorMessages"],
  emits: ["update:modelValue"],
  template: `
    <label class="field" :data-icon="appendInnerIcon">{{ label }}
      <input :value="modelValue" :readonly="readonly" @input="$emit('update:modelValue', $event.target.value)">
      <small v-if="errorMessages" class="error-message">{{ errorMessages }}</small>
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

function mountWith(component: typeof IngestRecipeFields, model: ReviewDraft, flags: CardFlag[] = [], readonly = false) {
  const wrapper = mount(component, {
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

describe("IngestRecipeFields", () => {
  test("edits the recipe's details; emptied optional fields go back to null", async () => {
    const model = draft();
    const wrapper = mountWith(IngestRecipeFields, model);

    await input(wrapper, "Name").get("input").setValue("Banana Mug Cake for One");
    await input(wrapper, "Prep time").get("input").setValue("5 minutes");
    await input(wrapper, "From").get("input").setValue("");

    expect(model.name).toBe("Banana Mug Cake for One");
    expect(model.prepTime).toBe("5 minutes");
    expect(model.attribution).toBeNull();
  });

  test("servings are typed as text and stored as a number", async () => {
    const model = draft();
    const wrapper = mountWith(IngestRecipeFields, model);

    await input(wrapper, "Servings").get("input").setValue("1 1/2");
    expect(model.recipeServings).toBe(1.5);
    await input(wrapper, "Servings").get("input").setValue("2-");
    expect(model.recipeServings).toBeNull();
    expect((input(wrapper, "Servings").get("input").element as HTMLInputElement).value).toBe("2-");
  });

  test("servings that aren't one number say they won't be kept, and point to Yield for a range", async () => {
    const model = draft();
    const wrapper = mountWith(IngestRecipeFields, model);

    await input(wrapper, "Servings").get("input").setValue("4-6");
    expect(model.recipeServings).toBeNull();
    expect(input(wrapper, "Servings").get(".error-message").text()).toBe("Type a number. For a range like 4 to 6, use Yield.");

    await input(wrapper, "Servings").get("input").setValue("4");
    expect(model.recipeServings).toBe(4);
    expect(input(wrapper, "Servings").find(".error-message").exists()).toBe(false);
    await input(wrapper, "Servings").get("input").setValue("");
    expect(input(wrapper, "Servings").find(".error-message").exists()).toBe(false);
  });

  test("a flagged field gets its colour and icon, whatever case the server names it in", () => {
    const flags: CardFlag[] = [
      { id: "missing_name:name:", kind: "missing_name", severity: "error", source: "validator", field: "name", ref: null },
      { id: "not_on_card:prep_time:", kind: "not_on_card", severity: "warning", source: "validator", field: "prep_time", ref: null },
    ];
    const wrapper = mountWith(IngestRecipeFields, draft(), flags);

    expect(wrapper.get("#ingest-field-name").classes()).toContain("ingest-field--error");
    expect(input(wrapper, "Name").attributes("data-icon")).toBe("error-icon");
    expect(wrapper.get("#ingest-field-prepTime").classes()).toContain("ingest-field--warning");
    expect(input(wrapper, "Prep time").attributes("data-icon")).toBe("warning-icon");
    expect(input(wrapper, "Description").attributes("data-icon")).toBeUndefined();
  });
});
