import { mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import IngestOrganizerSelector from "./IngestOrganizerSelector.vue";
import RecipeOrganizerSelector from "~/components/Domain/Recipe/RecipeOrganizerSelector.vue";
import type { CardDraftRef } from "~/lib/api/types/recipe-ingest";

const createOne = vi.hoisted(() => vi.fn());

vi.mock("~/composables/store", async () => {
  const { ref } = await import("vue");
  const store = () => ({ store: ref([{ id: "t1", name: "Desserts" }]), actions: { createOne } });
  return {
    useCategoryStore: store,
    useFoodStore: store,
    useHouseholdStore: store,
    useLabelStore: store,
    useTagStore: store,
    useToolStore: store,
  };
});
vi.mock("~/composables/store/use-user-store", async () => {
  const { ref } = await import("vue");
  return { useUserStore: () => ({ store: ref([]), actions: { createOne } }) };
});

/** Upstream's selector as it renders it: its listeners land on the field's root, and typing updates the search */
const stubs = {
  VAutocomplete: {
    props: ["modelValue", "search", "disabled"],
    emits: ["update:modelValue", "update:search"],
    template: `
      <div class="autocomplete">
        <input :value="search" :disabled="disabled" @input="$emit('update:search', $event.target.value)">
      </div>
    `,
  },
};

const wrappers: VueWrapper[] = [];

function mountSelector(component: typeof IngestOrganizerSelector | typeof RecipeOrganizerSelector, model: CardDraftRef[] = []) {
  const wrapper = mount(component as typeof IngestOrganizerSelector, {
    props: { "modelValue": model, "selectorType": "tags", "showAdd": false, "onUpdate:modelValue": vi.fn() },
    global: { stubs },
    attachTo: document.body,
  });
  wrappers.push(wrapper);
  return wrapper;
}

async function typeAndPressEnter(wrapper: VueWrapper, text: string) {
  const input = wrapper.get("input");
  await input.setValue(text);
  await input.trigger("keyup", { key: "Enter" });
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal("useNuxtApp", () => ({ $globals: { icons: {} } }));
});

afterEach(() => {
  wrappers.forEach(wrapper => wrapper.unmount());
  wrappers.length = 0;
  vi.unstubAllGlobals();
});

describe("IngestOrganizerSelector", () => {
  test("upstream's selector creates an organizer on Enter even without its add button", async () => {
    // why the review page doesn't use it bare (docs/ai/PHASE2.md §9: review never creates organizers)
    await typeAndPressEnter(mountSelector(RecipeOrganizerSelector), "Grandmas");
    expect(createOne).toHaveBeenCalledWith({ name: "Grandmas" });
  });

  test("Enter on text that matches no organizer creates nothing", async () => {
    const wrapper = mountSelector(IngestOrganizerSelector);
    expect(wrapper.findComponent(RecipeOrganizerSelector).props("showAdd")).toBe(false);

    await typeAndPressEnter(wrapper, "Grandmas");
    expect(createOne).not.toHaveBeenCalled();
  });

  test("the page's keyboard shortcuts still see Enter go up", async () => {
    const seen = vi.fn();
    window.addEventListener("keyup", seen);
    try {
      await typeAndPressEnter(mountSelector(IngestOrganizerSelector), "Grandmas");
      expect(seen).toHaveBeenCalledOnce();
      expect((seen.mock.calls[0]![0] as KeyboardEvent).key).toBe("Enter");
    }
    finally {
      window.removeEventListener("keyup", seen);
    }
  });

  test("read-only disables the field", () => {
    const wrapper = mount(IngestOrganizerSelector, {
      props: { modelValue: [], selectorType: "categories", readonly: true },
      global: { stubs },
    });
    wrappers.push(wrapper);
    expect(wrapper.get("input").attributes("disabled")).toBeDefined();
    expect(wrapper.findComponent(RecipeOrganizerSelector).props("selectorType")).toBe("categories");
  });
});
