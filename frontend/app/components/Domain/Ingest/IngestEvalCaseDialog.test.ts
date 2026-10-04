import { mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, describe, expect, test } from "vitest";
import IngestEvalCaseDialog from "./IngestEvalCaseDialog.vue";

const stubs = {
  BaseDialog: {
    props: ["modelValue", "title", "submitText", "submitDisabled"],
    emits: ["submit"],
    template: `
      <div v-if="modelValue" class="dialog" :data-title="title">
        <slot />
        <button type="button" class="submit" :disabled="submitDisabled" @click="$emit('submit')">{{ submitText }}</button>
      </div>
    `,
  },
  VCardText: { template: "<div><slot /></div>" },
  VTextField: {
    props: ["modelValue", "label", "errorMessages"],
    emits: ["update:modelValue"],
    template: `
      <label class="slug">{{ label }}
        <input :value="modelValue" @input="$emit('update:modelValue', $event.target.value)">
        <span class="error">{{ errorMessages }}</span>
      </label>
    `,
  },
  VCheckbox: {
    props: ["modelValue", "label"],
    emits: ["update:modelValue"],
    template: "<label class=\"verified\"><input type=\"checkbox\" :checked=\"modelValue\" @change=\"$emit('update:modelValue', $event.target.checked)\">{{ label }}</label>",
  },
};

const wrappers: VueWrapper[] = [];

function mountDialog(props: Record<string, unknown> = {}) {
  const wrapper = mount(IngestEvalCaseDialog, {
    props: { modelValue: true, recipeName: "Banana Mug Cake", ...props },
    global: { mocks: { $globals: { icons: {} } }, stubs },
  });
  wrappers.push(wrapper);
  return wrapper;
}

describe("IngestEvalCaseDialog", () => {
  afterEach(() => {
    wrappers.forEach(wrapper => wrapper.unmount());
    wrappers.length = 0;
  });

  test("names the case after the recipe and saves it with the verified tick", async () => {
    const wrapper = mountDialog();

    expect(wrapper.get(".dialog").attributes("data-title")).toBe("Save as eval case");
    expect((wrapper.get(".slug input").element as HTMLInputElement).value).toBe("banana-mug-cake");

    await wrapper.get(".verified input").setValue(true);
    await wrapper.get(".submit").trigger("click");

    expect(wrapper.emitted("save")).toEqual([[{ slug: "banana-mug-cake", verified: true }]]);
  });

  test("a name the server wouldn't take can't be saved", async () => {
    const wrapper = mountDialog();

    await wrapper.get(".slug input").setValue("Banana Mug Cake");

    expect(wrapper.get(".submit").attributes("disabled")).toBeDefined();
    expect(wrapper.get(".error").text()).toBe("Lowercase letters, numbers and dashes");
  });

  test("says when the name is taken, until it's changed", async () => {
    const wrapper = mountDialog({ exists: true });

    expect(wrapper.get(".error").text()).toBe("An eval case with this name already exists");
    await wrapper.get(".slug input").setValue("banana-mug-cake-2");
    expect(wrapper.emitted("update:exists")).toEqual([[false]]);
  });
});
