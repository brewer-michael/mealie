import { mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import IngestOrganizerSelector from "./IngestOrganizerSelector.vue";
import RecipeOrganizerSelector from "~/components/Domain/Recipe/RecipeOrganizerSelector.vue";
import type { CardDraftRef } from "~/lib/api/types/recipe-ingest";

const createOne = vi.hoisted(() => vi.fn());

vi.mock("~/composables/store", async () => {
  const { ref } = await import("vue");
  const store = () => ({ store: ref([{ id: "t1", name: "Desserts" }, { id: "t2", name: "Quick" }]), actions: { createOne } });
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

interface Entry {
  value: string;
  name: string;
  id: string | null;
  create?: boolean;
}

/**
 * Vuetify's autocomplete as far as the selector uses it: its list (filtered with the selector's `customFilter`, which
 * gets Vuetify's internal item, each entry through the `item` slot, which gets the entry itself), its chips, its search
 * and `autoSelectFirst`, which is what Enter on the text picks.
 * The real keyboard handling is checked in Chromium (docs/ai/PHASE2.md V-03).
 */
const VAutocomplete = {
  props: ["modelValue", "search", "disabled", "items", "customFilter", "autoSelectFirst", "label"],
  emits: ["update:modelValue", "update:search"],
  computed: {
    shown(this: { items: Entry[]; search: string; customFilter: (value: string, query: string, item: { raw: Entry }) => boolean }) {
      return this.items.filter(entry => !this.search || this.customFilter(entry.name, this.search, { raw: entry }));
    },
  },
  methods: {
    choose(this: { modelValue: Entry[]; $emit: (event: string, value: unknown) => void }, entry: Entry) {
      this.$emit("update:modelValue", [...this.modelValue, entry]);
    },
  },
  template: `
    <div class="autocomplete" :data-auto-select-first="autoSelectFirst" :data-label="label">
      <span v-for="(entry, index) in modelValue" :key="index" class="selection">
        <slot name="chip" :item="entry" :index="index" />
      </span>
      <input :value="search" :disabled="disabled" @input="$emit('update:search', $event.target.value)">
      <div v-for="entry in shown" :key="entry.value" class="entry" @click="choose(entry)">
        <slot name="item" :item="entry" :props="{ title: entry.name }" />
      </div>
    </div>
  `,
};
const stubs = {
  VAutocomplete,
  VChip: {
    props: ["text"],
    emits: ["click:close"],
    template: "<span class=\"chip\">{{ text }}<button type=\"button\" class=\"chip-close\" @click=\"$emit('click:close')\">x</button></span>",
  },
  VListItem: { props: ["title"], template: "<div class=\"list-item\">{{ title }}</div>" },
};

const wrappers: VueWrapper[] = [];

function mountSelector(options: { model?: CardDraftRef[]; canCreate?: boolean; readonly?: boolean } = {}) {
  const onUpdate = vi.fn();
  const wrapper = mount(IngestOrganizerSelector, {
    props: {
      "modelValue": options.model ?? [],
      "selectorType": "tags",
      "canCreate": options.canCreate ?? false,
      "readonly": options.readonly ?? false,
      "onUpdate:modelValue": (value: CardDraftRef[]) => {
        onUpdate(value);
        void wrapper.setProps({ modelValue: value });
      },
    },
    global: { stubs, mocks: { $globals: { icons: { tags: "tags-icon", create: "create-icon" } } } },
    attachTo: document.body,
  });
  wrappers.push(wrapper);
  return { wrapper, onUpdate };
}

async function type(wrapper: VueWrapper, text: string) {
  await wrapper.get("input").setValue(text);
}

const entries = (wrapper: VueWrapper) => wrapper.findAll(".entry").map(entry => entry.text());
const autoSelectFirst = (wrapper: VueWrapper) => wrapper.get(".autocomplete").attributes("data-auto-select-first");

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal("useNuxtApp", () => ({ $globals: { icons: { tags: "tags-icon", create: "create-icon" } } }));
});

afterEach(() => {
  wrappers.forEach(wrapper => wrapper.unmount());
  wrappers.length = 0;
  vi.unstubAllGlobals();
});

describe("IngestOrganizerSelector", () => {
  test("upstream's selector creates an organizer on Enter even without its add button", async () => {
    // why the review page has its own: review never creates an organizer on the spot (docs/ai/PHASE2.md §9)
    const wrapper = mount(RecipeOrganizerSelector, {
      props: { "modelValue": [], "selectorType": "tags", "showAdd": false, "onUpdate:modelValue": vi.fn() },
      global: { stubs: { VAutocomplete: { props: ["search"], emits: ["update:search"], template: "<div><input :value=\"search\" @input=\"$emit('update:search', $event.target.value)\"></div>" } } },
    });
    wrappers.push(wrapper);
    await wrapper.get("input").setValue("Grandmas");
    await wrapper.get("input").trigger("keyup", { key: "Enter" });
    expect(createOne).toHaveBeenCalledWith({ name: "Grandmas" });
  });

  test("lists the group's tags, matching what's typed", async () => {
    const { wrapper } = mountSelector();
    expect(wrapper.get(".autocomplete").attributes("data-label")).toBe("Tags");
    expect(entries(wrapper)).toEqual(["Desserts", "Quick"]);

    await type(wrapper, "dess");
    expect(entries(wrapper)).toEqual(["Desserts"]);
    // Enter on the text picks the first match
    expect(autoSelectFirst(wrapper)).toBe("true");
  });

  test("without permission to create organizers, a new name has nothing to choose, and Enter picks nothing", async () => {
    const { wrapper } = mountSelector();

    await type(wrapper, "Grandmas");
    expect(entries(wrapper)).toEqual([]);
    expect(autoSelectFirst(wrapper)).toBe("false");
    expect(createOne).not.toHaveBeenCalled();
  });

  test("with permission, Create \"X\" is offered after the matches, and Enter on the text still doesn't choose it", async () => {
    const { wrapper } = mountSelector({ canCreate: true });

    await type(wrapper, "Grandmas");
    expect(entries(wrapper)).toEqual(["Create \"Grandmas\""]);
    expect(autoSelectFirst(wrapper)).toBe("false");

    await type(wrapper, "Dess");
    expect(entries(wrapper)).toEqual(["Desserts", "Create \"Dess\""]);
    expect(autoSelectFirst(wrapper)).toBe("true");

    // a name the group has, whatever its case, is chosen rather than created
    await type(wrapper, "desserts");
    expect(entries(wrapper)).toEqual(["Desserts"]);
  });

  test("choosing Create \"X\" adds the name alone, which commit creates; nothing is created now", async () => {
    const { wrapper, onUpdate } = mountSelector({ model: [{ id: "t2", name: "Quick" }], canCreate: true });

    await type(wrapper, "  Grandma's  ");
    await wrapper.findAll(".entry").find(entry => entry.text() === "Create \"Grandma's\"")!.trigger("click");

    expect(onUpdate).toHaveBeenLastCalledWith([{ id: "t2", name: "Quick" }, { id: null, name: "Grandma's" }]);
    expect(createOne).not.toHaveBeenCalled();
    expect((wrapper.get("input").element as HTMLInputElement).value).toBe("");
    // shown as new until it's committed, and not offered twice
    expect(wrapper.findAll(".chip").map(chip => chip.text())).toEqual(["Quickx", "Grandma's (new)x"]);
    await type(wrapper, "grandma's");
    expect(entries(wrapper)).toEqual([]);

    await wrapper.findAll(".chip-close")[1]!.trigger("click");
    expect(onUpdate).toHaveBeenLastCalledWith([{ id: "t2", name: "Quick" }]);
  });

  test("choosing a tag keeps its id and name", async () => {
    const { wrapper, onUpdate } = mountSelector({ canCreate: true });

    await type(wrapper, "Des");
    await wrapper.findAll(".entry")[0]!.trigger("click");
    expect(onUpdate).toHaveBeenLastCalledWith([{ id: "t1", name: "Desserts" }]);
  });

  test("the page's keyboard shortcuts still see Enter go up", async () => {
    const seen = vi.fn();
    window.addEventListener("keyup", seen);
    try {
      const { wrapper } = mountSelector({ canCreate: true });
      await type(wrapper, "Grandmas");
      await wrapper.get("input").trigger("keyup", { key: "Enter" });
      expect(seen).toHaveBeenCalledOnce();
      expect((seen.mock.calls[0]![0] as KeyboardEvent).key).toBe("Enter");
      expect(createOne).not.toHaveBeenCalled();
    }
    finally {
      window.removeEventListener("keyup", seen);
    }
  });

  test("read-only disables the field", () => {
    const { wrapper } = mountSelector({ readonly: true, canCreate: true });
    expect(wrapper.get("input").attributes("disabled")).toBeDefined();
  });
});
