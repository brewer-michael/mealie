import { mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, describe, expect, test } from "vitest";
import GroupAIProviderRouteSelect from "./GroupAIProviderRouteSelect.vue";
import type { AIProviderSlot } from "~/lib/api/types/group";

const wrappers: VueWrapper[] = [];

const providers = [
  { id: "a", name: "A" },
  { id: "b", name: "B" },
  { id: "c", name: "C" },
];

function mountSelect(routeSlot: AIProviderSlot, modelValue: string[], primaryId?: string | null) {
  const wrapper = mount(GroupAIProviderRouteSelect, {
    props: {
      routeSlot,
      providers,
      primaryId,
      modelValue,
      "onUpdate:modelValue": (value: string[]) => wrapper.setProps({ modelValue: value }),
    },
    global: {
      stubs: {
        VAutocomplete: {
          name: "VAutocomplete",
          props: ["modelValue", "label", "hint", "items", "disabled"],
          template: "<div />",
        },
      },
    },
  });

  wrappers.push(wrapper);
  return wrapper;
}

function autocomplete(wrapper: VueWrapper) {
  return wrapper.findComponent({ name: "VAutocomplete" });
}

describe("GroupAIProviderRouteSelect", () => {
  afterEach(() => {
    wrappers.forEach(wrapper => wrapper.unmount());
    wrappers.length = 0;
  });

  test.each([
    ["default", "Default Fallbacks", "when the default provider fails"],
    ["audio", "Audio Fallbacks", "when the audio provider fails"],
    ["image", "Image Fallbacks", "when the image provider fails"],
  ] as const)("%s has its own label and hint", (slot, label, hint) => {
    const field = autocomplete(mountSelect(slot, [], "a"));

    expect(field.props("label")).toBe(label);
    expect(field.props("hint")).toContain(hint);
    expect(field.props("hint")).toContain("To move a provider to the end, remove it and add it again.");
    expect(field.props("disabled")).toBe(false);
  });

  test("doesn't offer or show the slot's primary as a fallback", () => {
    const field = autocomplete(mountSelect("default", ["a", "c", "deleted"], "a"));

    expect(field.props("items").map((item: { id: string }) => item.id)).toEqual(["b", "c"]);
    expect(field.props("modelValue")).toEqual(["c"]);
  });

  test.each([
    ["default", "a default provider"],
    ["audio", "an audio provider"],
    ["image", "an image provider"],
  ] as const)("%s can't be edited without its primary", (slot, primary) => {
    const field = autocomplete(mountSelect(slot, ["b"], null));

    expect(field.props("disabled")).toBe(true);
    expect(field.props("hint")).toBe(`Fallbacks are only used with ${primary}. Choose one first.`);
    // Kept as they are until a primary is chosen
    expect(field.props("modelValue")).toEqual(["b"]);
  });

  test.each([
    ["planner", "Planner"],
    ["fast", "Fast Tasks"],
    ["embedding", "Embeddings"],
  ] as const)("%s has no primary and is always editable", (slot, label) => {
    const field = autocomplete(mountSelect(slot, ["a", "b"]));

    expect(field.props("label")).toBe(label);
    expect(field.props("disabled")).toBe(false);
    expect(field.props("items")).toHaveLength(3);
    expect(field.props("modelValue")).toEqual(["a", "b"]);
  });

  test("an edit replaces the list in the selected order", async () => {
    const wrapper = mountSelect("fast", ["a"]);

    autocomplete(wrapper).vm.$emit("update:modelValue", ["c", "a"]);
    await wrapper.vm.$nextTick();

    expect(wrapper.emitted("update:modelValue")?.[0]?.[0]).toEqual(["c", "a"]);
  });
});
