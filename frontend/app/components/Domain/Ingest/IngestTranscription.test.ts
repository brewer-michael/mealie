import { mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, describe, expect, test } from "vitest";
import IngestTranscription from "./IngestTranscription.vue";
import { MAX_TRANSCRIPTION } from "~/composables/use-recipe-ingest-review";

const stubs = {
  VBtn: {
    props: ["disabled", "loading"],
    emits: ["click"],
    template: "<button type=\"button\" :class=\"$attrs.class\" :disabled=\"disabled\" :data-loading=\"loading\" @click=\"$emit('click')\"><slot /></button>",
  },
  VTextarea: {
    props: ["modelValue", "errorMessages", "disabled"],
    emits: ["update:modelValue"],
    template: `
      <label class="textarea">
        <textarea :value="modelValue" :disabled="disabled" @input="$emit('update:modelValue', $event.target.value)" />
        <span v-if="errorMessages" class="error">{{ errorMessages }}</span>
      </label>
    `,
  },
};

/** The text as a reviewer reviewing a card sees it, with its v-model:editing wired up */
function mountEditable(props: { text?: string | null; canRebuild?: boolean; rebuilding?: boolean } = {}) {
  const wrapper: VueWrapper = mount(IngestTranscription, {
    props: {
      "text": "Banana Mug Cake\n1 [illegible] banana",
      "canRebuild": true,
      "editing": false,
      "onUpdate:editing": (value: boolean) => wrapper.setProps({ editing: value }),
      ...props,
    },
    global: { stubs, mocks: { $globals: { icons: { edit: "edit" } } } },
  });
  wrappers.push(wrapper);
  return wrapper;
}

const wrappers: VueWrapper[] = [];

afterEach(() => {
  wrappers.forEach(wrapper => wrapper.unmount());
  wrappers.length = 0;
});

describe("IngestTranscription", () => {
  test("shows what the card says with its markers picked out", () => {
    const wrapper = mount(IngestTranscription, { props: { text: "[illegible] banana\nMicrowave [blank] minutes" } });
    wrappers.push(wrapper);

    expect(wrapper.get("pre").text()).toBe("[illegible] banana\nMicrowave [blank] minutes");
    expect(wrapper.findAll(".ingest-transcription__marker").map(marker => marker.text())).toEqual(["[illegible]", "[blank]"]);
  });

  test("says when nothing was kept", () => {
    const wrapper = mount(IngestTranscription, { props: { text: "" } });
    wrappers.push(wrapper);
    expect(wrapper.text()).toBe("No transcription was kept for this card.");
  });

  test("Edit turns the text into a box to correct; Rebuild from this text sends the corrected text", async () => {
    const wrapper = mountEditable();
    expect(wrapper.find("textarea").exists()).toBe(false);

    await wrapper.get(".ingest-transcription__edit").trigger("click");
    const box = wrapper.get("textarea");
    expect((box.element as HTMLTextAreaElement).value).toBe("Banana Mug Cake\n1 [illegible] banana");
    expect(wrapper.text()).toContain("Correct the text, then rebuild the recipe from it. The photos aren't read again.");

    await box.setValue("Banana Mug Cake\n1 ripe banana");
    await wrapper.get(".ingest-transcription__rebuild").trigger("click");
    expect(wrapper.emitted("rebuild")).toEqual([["Banana Mug Cake\n1 ripe banana"]]);
    // the page leaves editing once the rebuild is on its way; until then the box stays as typed
    expect(wrapper.find("textarea").exists()).toBe(true);
  });

  test("Cancel leaves the text as it was, and Edit starts again from the card's text", async () => {
    const wrapper = mountEditable();
    await wrapper.get(".ingest-transcription__edit").trigger("click");
    await wrapper.get("textarea").setValue("something else");
    await wrapper.get(".ingest-transcription__cancel").trigger("click");

    expect(wrapper.emitted("rebuild")).toBeUndefined();
    expect(wrapper.get("pre").text()).toBe("Banana Mug Cake\n1 [illegible] banana");
    await wrapper.get(".ingest-transcription__edit").trigger("click");
    expect((wrapper.get("textarea").element as HTMLTextAreaElement).value).toBe("Banana Mug Cake\n1 [illegible] banana");
  });

  test("an empty or too long text can't be sent, nor any while the card can't be rebuilt", async () => {
    const wrapper = mountEditable({ text: "" });
    await wrapper.get(".ingest-transcription__edit").trigger("click");
    const rebuild = () => wrapper.get(".ingest-transcription__rebuild");
    expect(rebuild().attributes("disabled")).toBeDefined();

    await wrapper.get("textarea").setValue("x".repeat(MAX_TRANSCRIPTION + 1));
    expect(rebuild().attributes("disabled")).toBeDefined();
    expect(wrapper.get(".error").text()).toBe(`At most ${MAX_TRANSCRIPTION} characters`);

    await wrapper.get("textarea").setValue("Banana Mug Cake");
    expect(rebuild().attributes("disabled")).toBeUndefined();
    // a task started meanwhile: the box keeps what was typed, but nothing is sent
    await wrapper.setProps({ canRebuild: false });
    expect(rebuild().attributes("disabled")).toBeDefined();
    expect((wrapper.get("textarea").element as HTMLTextAreaElement).value).toBe("Banana Mug Cake");
  });

  test("a card nothing can rebuild (not ready, or being read) offers no Edit", () => {
    const wrapper = mountEditable({ canRebuild: false });
    expect(wrapper.find(".ingest-transcription__edit").exists()).toBe(false);
  });
});
