import { mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, describe, expect, test } from "vitest";
import IngestNeedsALook from "./IngestNeedsALook.vue";
import { buildNeedsALook, normalizeDraft } from "~/composables/use-recipe-ingest-review";
import type { CardFlag } from "~/lib/api/types/recipe-ingest";

const draft = normalizeDraft({
  name: "Banana Mug Cake",
  ingredients: [{ referenceId: "i1", originalText: "1/4 t. salt", quantity: 0.25, food: { id: null, name: "salt" } }],
  steps: [{ id: "s1", text: "Microwave on high for [blank] minutes." }],
});

const blank: CardFlag = { id: "blank:steps:s1", kind: "blank", severity: "error", source: "marker", field: "steps", ref: "s1" };
const unsure: CardFlag = {
  id: "unsure:ingredients:i1",
  kind: "unsure",
  severity: "warning",
  source: "model",
  field: "ingredients",
  ref: "i1",
  params: { text: "1/4" },
  alternatives: ["1/2"],
};
const info: CardFlag = { id: "new_food:ingredients:i1", kind: "new_food", severity: "info", source: "parser", field: "ingredients", ref: "i1", params: { name: "salt" } };

const slot = (tag = "div", className = "") => ({ template: `<${tag} class="${className}"><slot /></${tag}>` });
const stubs = {
  VCard: slot("section", "card"),
  VCardTitle: slot("h3"),
  VDivider: { template: "<hr>" },
  VIcon: { props: ["icon"], template: "<i />" },
  VSpacer: slot(),
  VChip: { emits: ["click"], template: "<button type=\"button\" class=\"chip\" @click=\"$emit('click')\"><slot /></button>" },
  VBtn: { emits: ["click"], template: "<button type=\"button\" @click=\"$emit('click')\"><slot /></button>" },
  VTextField: { props: ["label"], template: "<div><input :aria-label=\"label\"></div>" },
  VAlert: slot(),
};

const wrappers: VueWrapper[] = [];

function mountPanel(seen: CardFlag[], current: CardFlag[], fixed: string[] = []) {
  const { items } = buildNeedsALook(seen, current, new Set(fixed), draft, []);
  const wrapper = mount(IngestNeedsALook, {
    props: { items },
    global: { mocks: { $globals: { icons: {} } }, stubs },
  });
  wrappers.push(wrapper);
  return wrapper;
}

describe("IngestNeedsALook", () => {
  afterEach(() => {
    wrappers.forEach(wrapper => wrapper.unmount());
    wrappers.length = 0;
  });

  test("lists the errors and warnings in reading order, with how many are left", () => {
    const wrapper = mountPanel([blank, unsure, info], [blank, unsure, info]);

    expect(wrapper.get("h3").text()).toBe("Needs a look (2)");
    // infos show quietly elsewhere; ingredients come before steps
    expect(wrapper.findAll("[data-flag]").map(item => item.attributes("data-flag"))).toEqual([
      "unsure:ingredients:i1",
      "blank:steps:s1",
    ]);
  });

  test("passes each one-tap fix up to the page", async () => {
    const wrapper = mountPanel([blank, unsure], [blank, unsure]);

    await wrapper.get(".chip").trigger("click");
    const looksRight = wrapper.findAll("button").find(b => b.text() === "Looks right")!;
    await looksRight.trigger("click");
    const keep = wrapper.findAll("button").find(b => b.text() === "Keep blank")!;
    await keep.trigger("click");

    expect(wrapper.emitted("alternative")).toEqual([[unsure, "1/2"]]);
    expect(wrapper.emitted("resolve")).toEqual([[unsure, "dismissed"], [blank, "kept"]]);
  });

  test("once everything is resolved or fixed it says there's nothing left", () => {
    const wrapper = mountPanel([blank, unsure], [{ ...unsure, resolution: "dismissed" }], []);

    expect(wrapper.get("h3").text()).toBe("Nothing left to check");
    expect(wrapper.findAll("[data-flag]")).toHaveLength(2);
  });

  test("a clean card shows nothing", () => {
    const wrapper = mountPanel([info], [info]);
    expect(wrapper.find(".card").exists()).toBe(false);
  });
});
