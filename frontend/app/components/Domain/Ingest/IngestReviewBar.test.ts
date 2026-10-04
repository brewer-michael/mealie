import { mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, describe, expect, test } from "vitest";
import IngestReviewBar from "./IngestReviewBar.vue";
import type { SaveState } from "~/composables/use-recipe-ingest-review";

const stubs = {
  VSpacer: { template: "<span />" },
  VBtn: {
    props: ["disabled", "loading", "color"],
    emits: ["click"],
    template: "<button type=\"button\" :class=\"$attrs.class\" :data-color=\"color\" :data-loading=\"loading\" :disabled=\"disabled\" @click=\"$emit('click')\"><slot /></button>",
  },
};

const wrappers: VueWrapper[] = [];

function mountBar(props: { errorCount?: number; disabled?: boolean; committing?: boolean; saveState?: SaveState; fixed?: boolean } = {}) {
  const wrapper = mount(IngestReviewBar, { props, global: { mocks: { $globals: { icons: {} } }, stubs } });
  wrappers.push(wrapper);
  return wrapper;
}

function primary(wrapper: VueWrapper) {
  return wrapper.get(".ingest-review-bar__primary");
}

describe("IngestReviewBar", () => {
  afterEach(() => {
    wrappers.forEach(wrapper => wrapper.unmount());
    wrappers.length = 0;
  });

  test("a clean card commits in one tap", async () => {
    const wrapper = mountBar();

    expect(primary(wrapper).text()).toBe("Commit & next");
    expect(primary(wrapper).attributes("disabled")).toBeUndefined();
    await primary(wrapper).trigger("click");
    expect(wrapper.emitted("commit")).toHaveLength(1);
  });

  test("while errors remain the primary button reads how many to fix and leads to them, never committing", async () => {
    const wrapper = mountBar({ errorCount: 1 });

    expect(primary(wrapper).text()).toBe("1 to fix");
    expect(primary(wrapper).attributes("data-color")).toBe("error");
    expect(wrapper.findAll("button").some(b => b.text() === "Commit & next")).toBe(false);

    await primary(wrapper).trigger("click");
    expect(wrapper.emitted("fix")).toHaveLength(1);
    expect(wrapper.emitted("commit")).toBeUndefined();

    await wrapper.setProps({ errorCount: 3 });
    expect(primary(wrapper).text()).toBe("3 to fix");
  });

  test("commit is disabled while the card can't be committed, and spins while committing", async () => {
    const wrapper = mountBar({ disabled: true });
    expect(primary(wrapper).attributes("disabled")).toBeDefined();

    await wrapper.setProps({ disabled: false, committing: true });
    expect(primary(wrapper).attributes("disabled")).toBeDefined();
    expect(primary(wrapper).attributes("data-loading")).toBe("true");
    expect(wrapper.get(".ingest-review-bar__skip").attributes("disabled")).toBeDefined();
  });

  test("Skip goes on to the next card", async () => {
    const wrapper = mountBar({ errorCount: 2 });
    await wrapper.get(".ingest-review-bar__skip").trigger("click");
    expect(wrapper.emitted("skip")).toHaveLength(1);
  });

  test("says whether the draft is saved", async () => {
    const wrapper = mountBar();
    expect(wrapper.find(".ingest-review-bar__saved").exists()).toBe(false);

    await wrapper.setProps({ saveState: "saving" });
    expect(wrapper.get(".ingest-review-bar__saved").text()).toBe("Saving");
    await wrapper.setProps({ saveState: "saved" });
    expect(wrapper.get(".ingest-review-bar__saved").text()).toBe("Saved");
    await wrapper.setProps({ saveState: "error" });
    expect(wrapper.get(".ingest-review-bar__saved").text()).toBe("Couldn't save");
  });

  test("is pinned to the bottom of a phone's screen", () => {
    expect(mountBar({ fixed: true }).classes()).toContain("ingest-review-bar--fixed");
    expect(mountBar().classes()).not.toContain("ingest-review-bar--fixed");
  });
});
