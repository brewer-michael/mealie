import { mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, describe, expect, test } from "vitest";
import IngestReviewBar from "./IngestReviewBar.vue";
import type { ReviewNotice, SaveState } from "~/composables/use-recipe-ingest-review";

const stubs = {
  VSpacer: { template: "<span />" },
  VIcon: { props: ["icon", "color"], template: "<i class=\"icon\" :data-icon=\"icon\" :data-color=\"color\" />" },
  VBtn: {
    props: ["disabled", "loading", "color"],
    emits: ["click"],
    template: "<button type=\"button\" :class=\"$attrs.class\" :aria-label=\"$attrs['aria-label']\" :data-color=\"color\" :data-loading=\"loading\" :disabled=\"disabled\" @click=\"$emit('click')\"><slot /></button>",
  },
};

const icons = { check: "check-icon", alert: "warning-icon", alertCircle: "error-icon", informationOutline: "info-icon", close: "close-icon" };
const wrappers: VueWrapper[] = [];

function mountBar(props: { errorCount?: number; disabled?: boolean; committing?: boolean; saveState?: SaveState; fixed?: boolean; notice?: ReviewNotice | null; actions?: boolean } = {}) {
  const wrapper = mount(IngestReviewBar, { props, global: { mocks: { $globals: { icons } }, stubs } });
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

  test("a notice shows inside the bar, above its buttons, in a live region", async () => {
    const wrapper = mountBar({ notice: { id: 1, kind: "success", text: "Added Banana Mug Cake" } });

    const region = wrapper.get("[role=status]");
    expect(region.attributes("aria-live")).toBe("polite");
    const notice = region.get(".ingest-review-bar__notice");
    expect(notice.text()).toContain("Added Banana Mug Cake");
    expect(notice.classes()).toContain("ingest-review-bar__notice--success");
    expect(notice.get(".icon").attributes("data-icon")).toBe("check-icon");
    // before Skip and Commit & next in the bar, so it takes its own room rather than covering what's above
    const html = wrapper.html();
    expect(html.indexOf("ingest-review-bar__notice")).toBeLessThan(html.indexOf("ingest-review-bar__skip"));

    await wrapper.get(".ingest-review-bar__notice-close").trigger("click");
    expect(wrapper.emitted("notice-dismiss")).toHaveLength(1);
    expect(wrapper.get(".ingest-review-bar__notice-close").attributes("aria-label")).toBe("Close");
  });

  test("a notice's button and its second line", async () => {
    const wrapper = mountBar({
      notice: {
        id: 2,
        kind: "warning",
        text: "Added Lemon Bars",
        detail: "The tag \"Desserts\" no longer exists, so it wasn't added.",
        action: { label: "Read whole card again", run: () => undefined },
      },
    });

    expect(wrapper.get(".ingest-review-bar__notice-detail").text()).toBe("The tag \"Desserts\" no longer exists, so it wasn't added.");
    expect(wrapper.get(".ingest-review-bar__notice .icon").attributes("data-icon")).toBe("warning-icon");
    await wrapper.get(".ingest-review-bar__notice-action").trigger("click");
    expect(wrapper.emitted("notice-action")).toHaveLength(1);
  });

  test("without its actions (a card that isn't ready) the bar holds only the notice", () => {
    const wrapper = mountBar({ actions: false, notice: { id: 3, kind: "error", text: "This card no longer exists." } });

    expect(wrapper.get(".ingest-review-bar__notice").classes()).toContain("ingest-review-bar__notice--error");
    expect(wrapper.find(".ingest-review-bar__skip").exists()).toBe(false);
    expect(wrapper.find(".ingest-review-bar__primary").exists()).toBe(false);
  });

  test("no notice, no strip", () => {
    expect(mountBar().find(".ingest-review-bar__notice").exists()).toBe(false);
  });
});
