import { mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, describe, expect, test } from "vitest";
import { reactive } from "vue";
import IngestIngredientRow from "./IngestIngredientRow.vue";
import type { IngestNamedOption } from "~/composables/use-recipe-ingest-review";
import type { CardDraftIngredient, CardFlag } from "~/lib/api/types/recipe-ingest";

const foods: IngestNamedOption[] = [
  { id: "f-oil", name: "coconut oil" },
  { id: "f-egg", name: "egg", pluralName: "eggs" },
];
const units: IngestNamedOption[] = [{ id: "u-tbsp", name: "tablespoon", abbreviation: "tbsp" }];

const field = (className: string) => ({
  props: ["modelValue", "label", "readonly"],
  emits: ["update:modelValue"],
  template: `<label class="${className}">{{ label }}<input :value="typeof modelValue === 'object' && modelValue ? modelValue.name : modelValue" :readonly="readonly" @change="$emit('update:modelValue', $event.target.value)"></label>`,
});

const stubs = {
  VIcon: { props: ["icon", "color", "title"], template: "<i class=\"icon\" :data-color=\"color\" :title=\"title\" />" },
  VChip: { template: "<span class=\"chip\"><slot /></span>" },
  VSpacer: { template: "<span />" },
  VBtn: { emits: ["click"], template: "<button type=\"button\" @click=\"$emit('click')\"><slot /></button>" },
  VTextField: field("text-field"),
  VCombobox: field("combobox"),
};

function oil(overrides: Partial<CardDraftIngredient> = {}): CardDraftIngredient {
  return reactive({
    referenceId: "i1",
    originalText: "1 T. coconut oil (melted)",
    quantity: 1,
    unit: { id: "u-tbsp", name: "tablespoon" },
    food: { id: "f-oil", name: "coconut oil" },
    note: "(melted)",
    display: "1 tablespoon coconut oil (melted)",
    ...overrides,
  });
}

const wrappers: VueWrapper[] = [];

function mountRow(ingredient: CardDraftIngredient, props: { expanded?: boolean; canCreateFoods?: boolean; flags?: CardFlag[]; infos?: CardFlag[] } = {}) {
  const wrapper = mount(IngestIngredientRow, {
    props: { modelValue: ingredient, foodOptions: foods, unitOptions: units, ...props },
    global: { mocks: { $globals: { icons: {} } }, stubs },
  });
  wrappers.push(wrapper);
  return wrapper;
}

function input(wrapper: VueWrapper, label: string) {
  const found = wrapper.findAll("label").find(l => l.text() === label);
  if (!found) {
    throw new Error(`No ${label} field`);
  }
  return found.get("input");
}

describe("IngestIngredientRow", () => {
  afterEach(() => {
    wrappers.forEach(wrapper => wrapper.unmount());
    wrappers.length = 0;
  });

  test("reads like the card, with its link status", () => {
    const wrapper = mountRow(oil());

    expect(wrapper.get(".ingest-ingredient__text").text()).toBe("1 tablespoon coconut oil (melted)");
    expect(wrapper.get(".ingest-ingredient__linked").attributes("title")).toBe("Linked");
    expect(wrapper.find(".chip").exists()).toBe(false);
    expect(wrapper.find("input").exists()).toBe(false);
  });

  test("a food that isn't linked is a New food, or Kept as text for members who can't add foods", () => {
    const salt = oil({ food: { id: null, name: "salt" }, unit: { id: null, name: "pinch" } });

    expect(mountRow(salt, { canCreateFoods: true }).findAll(".chip").map(chip => chip.text())).toEqual(["New food", "New unit"]);
    expect(mountRow(salt, { canCreateFoods: false }).findAll(".chip").map(chip => chip.text())).toEqual(["Kept as text", "New unit"]);
  });

  test("a flagged line gets its colour and icon", () => {
    const flag: CardFlag = { id: "check_parse:ingredients:i1", kind: "check_parse", severity: "warning", source: "parser", field: "ingredients", ref: "i1" };
    const wrapper = mountRow(oil(), { flags: [flag] });

    expect(wrapper.classes()).toContain("ingest-ingredient--warning");
    expect(wrapper.find(".icon[data-color=\"warning\"]").exists()).toBe(true);
  });

  test("tapping the line opens it", async () => {
    const wrapper = mountRow(oil());
    await wrapper.get(".ingest-ingredient__line").trigger("click");
    await wrapper.get(".ingest-ingredient__line").trigger("keydown", { key: "Enter" });
    expect(wrapper.emitted("toggle")).toHaveLength(2);
  });

  test("open, it edits amount, unit, food and note, and shows the card's line", async () => {
    const ingredient = oil();
    const wrapper = mountRow(ingredient, { expanded: true });

    expect(wrapper.text()).toContain("On the card: 1 T. coconut oil (melted)");
    expect((input(wrapper, "Amount").element as HTMLInputElement).value).toBe("1");

    await input(wrapper, "Amount").setValue("1 1/2");
    expect(ingredient.quantity).toBe(1.5);
    expect(ingredient.display).toBe("1 1/2 tablespoon coconut oil (melted)");

    // a typed name that matches one of the group's foods (here by its plural) is linked to it
    await input(wrapper, "Food").setValue("Eggs");
    expect(ingredient.food).toEqual({ id: "f-egg", name: "egg" });

    // any other name is kept as a name, for commit to link or create
    await input(wrapper, "Food").setValue("ripe banana");
    expect(ingredient.food).toEqual({ id: null, name: "ripe banana" });

    await input(wrapper, "Unit").setValue("TBSP");
    expect(ingredient.unit).toEqual({ id: "u-tbsp", name: "tablespoon" });

    await input(wrapper, "Note").setValue("mashed");
    expect(ingredient.display).toBe("1 1/2 tablespoon ripe banana mashed");
  });

  test("an amount that isn't a number yet stays in the box", async () => {
    const ingredient = oil();
    const wrapper = mountRow(ingredient, { expanded: true });

    await input(wrapper, "Amount").setValue("1 1/");
    expect(ingredient.quantity).toBeNull();
    expect((input(wrapper, "Amount").element as HTMLInputElement).value).toBe("1 1/");
  });

  test("open, it says quietly how the line was read", () => {
    const shorthand: CardFlag = {
      id: "shorthand_read:ingredients:i1",
      kind: "shorthand_read",
      severity: "info",
      source: "parser",
      field: "ingredients",
      ref: "i1",
      params: { from: "T.", to: "tbsp" },
    };
    expect(mountRow(oil(), { infos: [shorthand] }).find(".ingest-ingredient__info").exists()).toBe(false);

    const wrapper = mountRow(oil(), { expanded: true, infos: [shorthand] });
    expect(wrapper.get(".ingest-ingredient__info").text()).toBe("Abbreviation written out: \"T.\" on the card was read as \"tbsp\".");
  });

  test("a new food's note says it's kept as text when the reviewer can't add foods", () => {
    const salt = oil({ food: { id: null, name: "salt" } });
    const newFood: CardFlag = {
      id: "new_food:ingredients:i1",
      kind: "new_food",
      severity: "info",
      source: "parser",
      field: "ingredients",
      ref: "i1",
      params: { name: "salt" },
    };
    expect(mountRow(salt, { expanded: true, infos: [newFood], canCreateFoods: false }).get(".ingest-ingredient__info").text())
      .toContain("you can't add foods, so it's kept in the note");
    expect(mountRow(salt, { expanded: true, infos: [newFood], canCreateFoods: true }).get(".ingest-ingredient__info").text())
      .toContain("It's added when you commit the card.");
  });

  test("a line can be removed", async () => {
    const wrapper = mountRow(oil(), { expanded: true });
    await wrapper.findAll("button").find(b => b.text() === "Delete")!.trigger("click");
    expect(wrapper.emitted("remove")).toHaveLength(1);
  });
});
