import { mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, describe, expect, test } from "vitest";
import IngestEvalCaseDialog from "./IngestEvalCaseDialog.vue";
import BaseDialog from "~/components/global/BaseDialog.vue";

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
  VChipGroup: {
    props: ["modelValue"],
    emits: ["update:modelValue"],
    provide() {
      return { chipGroup: this };
    },
    template: "<div class=\"tags\" :data-selected=\"(modelValue ?? []).join(',')\"><slot /></div>",
  },
  VChip: {
    props: ["value"],
    inject: ["chipGroup"],
    template: `
      <button type="button" class="tag" :data-tag="value" @click="toggle"><slot /></button>
    `,
    methods: {
      toggle(this: { value: string; chipGroup: { modelValue?: string[]; $emit: (event: string, value: string[]) => void } }) {
        const selected = this.chipGroup.modelValue ?? [];
        this.chipGroup.$emit("update:modelValue", selected.includes(this.value) ? selected.filter(tag => tag !== this.value) : [...selected, this.value]);
      },
    },
  },
  VTextarea: {
    props: ["modelValue", "label", "errorMessages"],
    emits: ["update:modelValue"],
    template: `
      <label class="notes">{{ label }}
        <textarea :value="modelValue" @input="$emit('update:modelValue', $event.target.value)" />
        <span class="notes-error">{{ errorMessages }}</span>
      </label>
    `,
  },
  VCheckbox: {
    props: ["modelValue", "label"],
    emits: ["update:modelValue"],
    template: "<label class=\"verified\"><input type=\"checkbox\" :checked=\"modelValue\" @change=\"$emit('update:modelValue', $event.target.checked)\">{{ label }}</label>",
  },
};

/**
 * The real BaseDialog, on an overlay that hears every key pressed inside the dialog as Vuetify's does: what Enter does
 * there is the dialog's to say
 */
const realDialog = {
  BaseDialog,
  VDialog: { props: ["modelValue"], template: "<div v-if=\"modelValue\" class=\"overlay\"><slot /></div>" },
  BaseDialogContent: {
    props: ["title", "submitText", "submitDisabled"],
    emits: ["submit"],
    template: `
      <div class="dialog" :data-title="title">
        <slot />
        <button type="button" class="submit" :disabled="submitDisabled" @click="$emit('submit')">{{ submitText }}</button>
      </div>
    `,
  },
};

const wrappers: VueWrapper[] = [];

function mountDialog(props: Record<string, unknown> = {}, dialogStubs: Record<string, unknown> = {}) {
  const wrapper = mount(IngestEvalCaseDialog, {
    props: { modelValue: true, recipeName: "Banana Mug Cake", ...props },
    global: { mocks: { $globals: { icons: {} }, $vuetify: { display: { xs: false } } }, stubs: { ...stubs, ...dialogStubs } },
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

    expect(wrapper.emitted("save")).toEqual([[{ slug: "banana-mug-cake", verified: true, tags: [], notes: "" }]]);
  });

  test("the card can be described with tags and notes, which are saved with it", async () => {
    const wrapper = mountDialog();

    expect(wrapper.findAll(".tag").map(tag => tag.text())).toEqual(["Handwritten", "Printed", "Faded"]);
    await wrapper.get("[data-tag=faded]").trigger("click");
    await wrapper.get("[data-tag=handwritten]").trigger("click");
    await wrapper.get("[data-tag=faded]").trigger("click");
    await wrapper.get("[data-tag=faded]").trigger("click");
    expect(wrapper.get(".tags").attributes("data-selected")).toBe("handwritten,faded");
    expect(wrapper.get(".notes").text()).toContain("Notes");
    await wrapper.get(".notes textarea").setValue("  Pencil, water-stained at the bottom  ");
    await wrapper.get(".submit").trigger("click");

    // in the order the chips show, and the notes without the spaces around them
    expect(wrapper.emitted("save")).toEqual([[{
      slug: "banana-mug-cake",
      verified: false,
      tags: ["handwritten", "faded"],
      notes: "Pencil, water-stained at the bottom",
    }]]);
  });

  test("notes longer than the server keeps can't be saved", async () => {
    const wrapper = mountDialog();

    await wrapper.get(".notes textarea").setValue("x".repeat(2001));
    expect(wrapper.get(".submit").attributes("disabled")).toBeDefined();
    expect(wrapper.get(".notes-error").text()).toBe("At most 2000 characters");
    await wrapper.get(".notes textarea").setValue("x".repeat(2000));
    expect(wrapper.get(".submit").attributes("disabled")).toBeUndefined();
  });

  test("opening it again starts afresh", async () => {
    const wrapper = mountDialog();
    await wrapper.get("[data-tag=printed]").trigger("click");
    await wrapper.get(".notes textarea").setValue("Typed card");

    await wrapper.setProps({ modelValue: false });
    await wrapper.setProps({ modelValue: true, recipeName: "Lemon Bars" });

    expect((wrapper.get(".slug input").element as HTMLInputElement).value).toBe("lemon-bars");
    expect(wrapper.get(".tags").attributes("data-selected")).toBe("");
    expect((wrapper.get(".notes textarea").element as HTMLTextAreaElement).value).toBe("");
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

  test("Return in the notes starts a new line: it doesn't save the case; Return in the name does", async () => {
    const wrapper = mountDialog({}, realDialog);

    await wrapper.get(".notes textarea").setValue("Faded pencil");
    for (const options of [{}, { shiftKey: true }, { isComposing: true }]) {
      await wrapper.get(".notes textarea").trigger("keydown", { key: "Enter", ...options });
    }
    await wrapper.get(".verified input").trigger("keydown", { key: "Enter" });
    expect(wrapper.emitted("save")).toBeUndefined();
    expect(wrapper.find(".overlay").exists()).toBe(true);

    // a name being typed with an input method isn't sent mid-word
    await wrapper.get(".slug input").trigger("keydown", { key: "Enter", isComposing: true });
    expect(wrapper.emitted("save")).toBeUndefined();
    await wrapper.get(".slug input").trigger("keydown", { key: "Enter" });
    expect(wrapper.emitted("save")).toEqual([[{ slug: "banana-mug-cake", verified: false, tags: [], notes: "Faded pencil" }]]);
  });
});
