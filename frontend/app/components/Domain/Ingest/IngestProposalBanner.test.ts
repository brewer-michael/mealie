import { mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, describe, expect, test } from "vitest";
import IngestProposalBanner from "./IngestProposalBanner.vue";
import type { CardProposal } from "~/lib/api/types/recipe-ingest";

const stubs = {
  VAlert: { template: "<div class=\"alert\"><slot /></div>" },
  VBtn: {
    props: ["disabled"],
    emits: ["click"],
    template: "<button type=\"button\" :disabled=\"disabled\" @click=\"$emit('click')\"><slot /></button>",
  },
};

const region: CardProposal = {
  id: "p1",
  kind: "region",
  target: { field: "steps", ref: "s2" },
  text: "Microwave on high for 2 minutes.",
  readable: true,
  alternatives: [],
};

const wrappers: VueWrapper[] = [];

function mountBanner(props: { proposal: CardProposal; compact?: boolean; label?: string | null; readonly?: boolean }) {
  const wrapper = mount(IngestProposalBanner, { props, global: { stubs } });
  wrappers.push(wrapper);
  return wrapper;
}

function buttons(wrapper: VueWrapper) {
  return wrapper.findAll("button").map(b => b.text());
}

async function click(wrapper: VueWrapper, text: string) {
  await wrapper.findAll("button").find(b => b.text() === text)!.trigger("click");
}

describe("IngestProposalBanner", () => {
  afterEach(() => {
    wrappers.forEach(wrapper => wrapper.unmount());
    wrappers.length = 0;
  });

  test("a region re-read can replace the line, be added to its end, or be dismissed", async () => {
    const wrapper = mountBanner({ proposal: region, label: "Step: 2" });

    expect(wrapper.text()).toContain("Step: 2");
    expect(wrapper.text()).toContain("New reading: \"Microwave on high for 2 minutes.\"");
    expect(buttons(wrapper)).toEqual(["Replace", "Add to the end", "Dismiss"]);

    await click(wrapper, "Replace");
    await click(wrapper, "Add to the end");
    await click(wrapper, "Dismiss");
    expect(wrapper.emitted("use")).toEqual([["replace"], ["append"]]);
    expect(wrapper.emitted("dismiss")).toHaveLength(1);
  });

  test("inside its flag's item it's one Use button", async () => {
    const wrapper = mountBanner({ proposal: region, compact: true });

    expect(buttons(wrapper)).toEqual(["Use", "Dismiss"]);
    await click(wrapper, "Use");
    expect(wrapper.emitted("use")).toEqual([["replace"]]);
  });

  test("a reading for a new line can only be added", async () => {
    const wrapper = mountBanner({ proposal: { ...region, target: { field: "ingredients", ref: null }, text: "1 egg" } });

    expect(buttons(wrapper)).toEqual(["Add to the end", "Dismiss"]);
    await click(wrapper, "Add to the end");
    expect(wrapper.emitted("use")).toEqual([["append"]]);
  });

  test("an area nothing could be read in can only be dismissed, and OCR readings say so", () => {
    const unreadable = mountBanner({ proposal: { ...region, readable: false, text: "" } });
    expect(unreadable.text()).toContain("Nothing could be read there.");
    expect(buttons(unreadable)).toEqual(["Dismiss"]);

    const ocr = mountBanner({ proposal: { ...region, viaOcr: true } });
    expect(ocr.text()).toContain("Read with OCR");
  });

  test("a whole-card re-read of an edited card offers the new reading or keeping mine", async () => {
    const full: CardProposal = { id: "p2", kind: "full", draft: { name: "Banana Mug Cake" } };
    const wrapper = mountBanner({ proposal: full });

    expect(wrapper.text()).toContain("The card was read again, and you've edited it since.");
    expect(buttons(wrapper)).toEqual(["Use the new reading", "Keep mine"]);
    await click(wrapper, "Use the new reading");
    await click(wrapper, "Keep mine");
    expect(wrapper.emitted("use")).toEqual([["replace"]]);
    expect(wrapper.emitted("dismiss")).toHaveLength(1);
  });

  test("nothing can be used while the editor is read-only", () => {
    const wrapper = mountBanner({ proposal: region, readonly: true });
    expect(wrapper.findAll("button").every(b => b.attributes("disabled") !== undefined)).toBe(true);
  });
});
