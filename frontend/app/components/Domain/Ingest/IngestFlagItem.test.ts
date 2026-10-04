import { mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, describe, expect, test } from "vitest";
import IngestFlagItem from "./IngestFlagItem.vue";
import { buildNeedsALook, normalizeDraft, type NeedsALookItem } from "~/composables/use-recipe-ingest-review";
import type { CardDraft, CardFlag, CardProposal } from "~/lib/api/types/recipe-ingest";

const draft: CardDraft = normalizeDraft({
  name: "Banana Mug Cake",
  ingredients: [
    { referenceId: "i1", originalText: "1/4 t. salt", quantity: 0.25, unit: { id: "u1", name: "teaspoon" }, food: { id: "f1", name: "salt" } },
  ],
  steps: [{ id: "s1", text: "Microwave on high for [blank] minutes." }],
});

function flag(overrides: Partial<CardFlag> = {}): CardFlag {
  return {
    id: "blank:steps:s1",
    kind: "blank",
    severity: "error",
    source: "marker",
    field: "steps",
    ref: "s1",
    params: {},
    alternatives: [],
    resolution: null,
    ...overrides,
  };
}

function item(f: CardFlag, options: { current?: CardFlag[]; fixed?: string[]; proposals?: CardProposal[] } = {}): NeedsALookItem {
  return buildNeedsALook([f], options.current ?? [f], new Set(options.fixed ?? []), draft, options.proposals ?? []).items[0]!;
}

const slot = (tag = "div", className = "") => ({ template: `<${tag} class="${className}"><slot /></${tag}>` });
const stubs = {
  VIcon: { props: ["icon", "color"], template: "<i class=\"icon\" :data-color=\"color\" />" },
  VSpacer: slot(),
  VChip: {
    props: ["disabled"],
    emits: ["click"],
    template: "<button type=\"button\" class=\"chip\" :disabled=\"disabled\" @click=\"$emit('click')\"><slot /></button>",
  },
  VBtn: {
    props: ["disabled"],
    emits: ["click"],
    template: "<button type=\"button\" :disabled=\"disabled\" @click=\"$emit('click')\"><slot /></button>",
  },
  VTextField: {
    props: ["modelValue", "label", "disabled"],
    emits: ["update:modelValue"],
    template: "<div class=\"text-field\"><input :aria-label=\"label\" :value=\"modelValue\" :disabled=\"disabled\" @input=\"$emit('update:modelValue', $event.target.value)\"></div>",
  },
  VAlert: slot("div", "alert"),
};

const wrappers: VueWrapper[] = [];

function mountItem(value: NeedsALookItem, readonly = false, canReread = true) {
  const wrapper = mount(IngestFlagItem, {
    props: { item: value, readonly, canReread },
    global: { mocks: { $globals: { icons: {} } }, stubs },
  });
  wrappers.push(wrapper);
  return wrapper;
}

/** The line's parts as shown, and whether each is highlighted */
function segments(wrapper: VueWrapper) {
  return wrapper.get(".ingest-flag-item__line").findAll("span").map(span => [span.element.textContent, span.classes().includes("ingest-flag-item__mark")]);
}

function buttonTexts(wrapper: VueWrapper) {
  return wrapper.findAll("button").map(b => b.text());
}

function button(wrapper: VueWrapper, text: string) {
  const found = wrapper.findAll("button").find(b => b.text() === text);
  if (!found) {
    throw new Error(`No ${text} button among ${buttonTexts(wrapper).join(", ")}`);
  }
  return found;
}

describe("IngestFlagItem", () => {
  afterEach(() => {
    wrappers.forEach(wrapper => wrapper.unmount());
    wrappers.length = 0;
  });

  test("a blank shows the line with the gap, why, and a box to type what goes there", async () => {
    const wrapper = mountItem(item(flag()));

    expect(wrapper.text()).toContain("Left blank on the card");
    expect(wrapper.text()).toContain("Step: 1");
    expect(wrapper.get(".ingest-flag-item__line").text()).toBe("Microwave on high for ___ minutes.");
    expect(wrapper.get(".ingest-flag-item__mark").text()).toBe("___");
    expect(wrapper.text()).toContain("The card leaves a gap here. Fill it in or keep it blank.");
    expect(buttonTexts(wrapper)).toEqual(["Fill in", "Re-read", "Keep blank", "Edit"]);

    expect(button(wrapper, "Fill in").attributes("disabled")).toBeDefined();
    await wrapper.get("input[aria-label=\"What goes here?\"]").setValue(" 2 ");
    await button(wrapper, "Fill in").trigger("click");
    expect(wrapper.emitted("fill")).toEqual([[flag(), "2"]]);
    // the box empties for the next one
    expect((wrapper.get("input").element as HTMLInputElement).value).toBe("");

    await wrapper.get("input").setValue("3");
    await wrapper.get(".text-field").trigger("keydown", { key: "Enter" });
    expect(wrapper.emitted("fill")![1]).toEqual([flag(), "3"]);
  });

  test("an error that can be kept is kept as written; re-read and edit point at its line", async () => {
    const illegible = flag({ id: "illegible:steps:s1", kind: "illegible" });
    const wrapper = mountItem(item(illegible));

    await button(wrapper, "Keep as written").trigger("click");
    await button(wrapper, "Re-read").trigger("click");
    await button(wrapper, "Edit").trigger("click");

    expect(wrapper.emitted("resolve")).toEqual([[illegible, "kept"]]);
    expect(wrapper.emitted("reread")).toEqual([[illegible]]);
    expect(wrapper.emitted("edit")).toEqual([[illegible]]);
  });

  test("a missing name can only be fixed", () => {
    const missing = flag({ id: "missing_name:name:", kind: "missing_name", field: "name", ref: null });
    const wrapper = mountItem(item(missing));

    expect(wrapper.text()).toContain("Name missing");
    expect(buttonTexts(wrapper)).toEqual(["Re-read", "Edit"]);
  });

  test("a warning offers its alternatives as one-tap chips, and Looks right", async () => {
    const unsure = flag({
      id: "unsure:ingredients:i1",
      kind: "unsure",
      severity: "warning",
      source: "model",
      field: "ingredients",
      ref: "i1",
      params: { text: "1/4" },
      alternatives: ["1/2"],
    });
    const wrapper = mountItem(item(unsure));

    expect(wrapper.get(".ingest-flag-item__line").text()).toBe("1/4 t. salt");
    expect(wrapper.get(".ingest-flag-item__mark").text()).toBe("1/4");
    expect(wrapper.text()).toContain("It might be: \"1/2\".");
    expect(wrapper.find("input").exists()).toBe(false);

    await wrapper.get(".chip").trigger("click");
    await button(wrapper, "Looks right").trigger("click");

    expect(wrapper.get(".chip").text()).toBe("Use \"1/2\"");
    expect(wrapper.emitted("alternative")).toEqual([[unsure, "1/2"]]);
    expect(wrapper.emitted("resolve")).toEqual([[unsure, "dismissed"]]);
  });

  test("a flag on the whole card has no line to re-read or edit", () => {
    const ocr = flag({ id: "read_by_ocr:card:", kind: "read_by_ocr", severity: "warning", source: "ocr", field: "card", ref: null, params: { confidence: 49.4 } });
    const wrapper = mountItem(item(ocr));

    expect(wrapper.text()).toContain("This card was read with text recognition (confidence 49%), which makes more mistakes.");
    expect(buttonTexts(wrapper)).toEqual(["Looks right"]);
  });

  test("a flag on a whole list is re-read into a new line, or edited", async () => {
    const empty = flag({ id: "empty_section:steps:", kind: "empty_section", severity: "warning", source: "validator", ref: null, params: { section: "steps" } });
    const wrapper = mountItem(item(empty));

    expect(wrapper.text()).toContain("No steps were found on the card.");
    expect(buttonTexts(wrapper)).toEqual(["Re-read", "Looks right", "Edit"]);
    await button(wrapper, "Re-read").trigger("click");
    expect(wrapper.emitted("reread")).toEqual([[empty]]);
  });

  test("a resolved item collapses with a check mark and can be undone", async () => {
    const kept = flag({ resolution: "kept" });
    const wrapper = mountItem(item(kept));

    expect(wrapper.classes()).toContain("ingest-flag-item--resolved");
    expect(wrapper.get(".icon").attributes("data-color")).toBe("success");
    expect(wrapper.find(".ingest-flag-item__line").exists()).toBe(false);
    expect(buttonTexts(wrapper)).toEqual(["Undo"]);

    await button(wrapper, "Undo").trigger("click");
    expect(wrapper.emitted("resolve")).toEqual([[kept, null]]);
  });

  test("a fixed item just collapses", () => {
    const wrapper = mountItem(item(flag(), { fixed: ["blank:steps:s1"] }));

    expect(wrapper.classes()).toContain("ingest-flag-item--fixed");
    expect(wrapper.text()).toContain("Left blank on the card");
    expect(wrapper.findAll("button")).toHaveLength(0);
  });

  test("a re-read of its line appears inside the item, with Use and Dismiss", async () => {
    const proposal: CardProposal = { id: "p1", kind: "region", target: { field: "steps", ref: "s1" }, text: "Microwave on high for 2 minutes.", readable: true };
    const wrapper = mountItem(item(flag(), { proposals: [proposal] }));

    expect(wrapper.get(".alert").text()).toContain("New reading: \"Microwave on high for 2 minutes.\"");
    await button(wrapper, "Use").trigger("click");
    await button(wrapper, "Dismiss").trigger("click");

    expect(wrapper.emitted("use-proposal")).toEqual([[proposal, "replace"]]);
    expect(wrapper.emitted("dismiss-proposal")).toEqual([[proposal]]);
  });

  test("Re-read is off while it can't happen (the page is busy turning a page); the rest still works", () => {
    const wrapper = mountItem(item(flag({ id: "illegible:steps:s1", kind: "illegible" })), false, false);

    expect(button(wrapper, "Re-read").attributes("disabled")).toBeDefined();
    expect(button(wrapper, "Keep as written").attributes("disabled")).toBeUndefined();
  });

  test("only the occurrence the flag means is highlighted: the second 2, never the 2 of 1/2", () => {
    const step = normalizeDraft({ steps: [{ id: "s1", text: "Add 1/2 c. milk. Microwave 2 minutes." }] });
    const invented = flag({ source: "cross_read", params: { value: "2", start: 27, end: 28 } });
    const wrapper = mountItem(buildNeedsALook([invented], [invented], new Set(), step, []).items[0]!);

    expect(wrapper.findAll(".ingest-flag-item__mark").map(mark => mark.text())).toEqual(["2"]);
    expect(segments(wrapper)).toEqual([["Add 1/2 c. milk. Microwave ", false], ["2", true], [" minutes.", false]]);
  });

  describe("Check this ingredient", () => {
    const flour = normalizeDraft({
      ingredients: [
        { referenceId: "i1", originalText: "2-3 c. flour", quantity: 2, unit: { id: "u1", name: "cup" }, food: { id: "f1", name: "flour" }, note: "to 3" },
        { referenceId: "i2", originalText: "1 c. sugar, 1 c. flour", quantity: 1, unit: { id: "u1", name: "cup" }, food: { id: null, name: "sugar flour" }, note: "" },
        { referenceId: "i3", originalText: "pinch of salt", note: "pinch of salt" },
      ],
    });
    const check = (ref: string, params: Record<string, unknown>) => flag({
      id: `check_parse:ingredients:${ref}`,
      kind: "check_parse",
      severity: "warning",
      source: "parser",
      field: "ingredients",
      ref,
      params,
    });
    const itemFor = (f: CardFlag) => buildNeedsALook([f], [f], new Set(), flour, []).items[0]!;

    test("shows what the parser read and what it lost, and can keep the line as text", async () => {
      const range = check("i1", { value: "2-3", start: 0, end: 3 });
      const wrapper = mountItem(itemFor(range));

      expect(wrapper.get(".ingest-flag-item__line").text()).toBe("2-3 c. flour");
      expect(wrapper.get(".ingest-flag-item__mark").text()).toBe("2-3");
      expect(wrapper.get(".ingest-flag-item__reading").text()).toBe("Read as: 2 cup flour, to 3");
      expect(wrapper.get(".ingest-flag-item__explanation").text()).toBe("The 3 of 2-3 is kept in the note.");
      expect(buttonTexts(wrapper)).toEqual(["Re-read", "Looks right", "Keep as text", "Edit"]);

      await button(wrapper, "Keep as text").trigger("click");
      await button(wrapper, "Looks right").trigger("click");
      expect(wrapper.emitted("keep-as-text")).toEqual([[range]]);
      expect(wrapper.emitted("resolve")).toEqual([[range, "dismissed"]]);
    });

    test("an amount the fields don't hold is named, at the place the flag means", () => {
      const merged = check("i2", { value: "1", start: 12, end: 13 });
      const wrapper = mountItem(itemFor(merged));

      expect(wrapper.findAll(".ingest-flag-item__mark")).toHaveLength(1);
      expect(segments(wrapper)).toEqual([["1 c. sugar, ", false], ["1", true], [" c. flour", false]]);
      expect(wrapper.get(".ingest-flag-item__reading").text()).toBe("Read as: 1 cup sugar flour");
      expect(wrapper.get(".ingest-flag-item__explanation").text()).toBe("The 1 isn't in the amount or the note.");
    });

    test("a low-confidence parse keeps the general explanation", () => {
      const wrapper = mountItem(itemFor(check("i1", { confidence: 60 })));

      expect(wrapper.get(".ingest-flag-item__reading").text()).toBe("Read as: 2 cup flour, to 3");
      expect(wrapper.get(".ingest-flag-item__explanation").text()).toBe("This line may not have been split into amount, unit and food correctly.");
    });

    test("a line already kept as text, or another flag, has nothing to keep", () => {
      const kept = mountItem(itemFor(check("i3", { value: "1" })));
      expect(kept.find(".ingest-flag-item__reading").exists()).toBe(false);
      expect(buttonTexts(kept)).toEqual(["Re-read", "Looks right", "Edit"]);

      const unsure = flag({ id: "unsure:ingredients:i1", kind: "unsure", severity: "warning", source: "model", field: "ingredients", ref: "i1", params: { text: "2-3" } });
      const other = mountItem(itemFor(unsure));
      expect(other.find(".ingest-flag-item__reading").exists()).toBe(false);
      expect(buttonTexts(other)).not.toContain("Keep as text");
    });

    test("Keep as text is off while the card is read again", () => {
      const wrapper = mountItem(itemFor(check("i1", { value: "2-3" })), true);
      expect(button(wrapper, "Keep as text").attributes("disabled")).toBeDefined();
    });
  });

  test("nothing can be changed while the card is read again", () => {
    const wrapper = mountItem(item(flag()), true);

    const enabled = wrapper.findAll("button").filter(b => b.attributes("disabled") === undefined).map(b => b.text());
    // Edit only scrolls
    expect(enabled).toEqual(["Edit"]);
  });
});
