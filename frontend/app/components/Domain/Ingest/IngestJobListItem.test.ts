import { mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, describe, expect, test } from "vitest";
import IngestJobListItem from "./IngestJobListItem.vue";
import type { RecipeIngestionJobSummary } from "~/lib/api/types/recipe-ingest";

const slot = (tag = "div", className = "") => ({ template: `<${tag} class="${className}"><slot /></${tag}>` });

const listItemStubs = {
  VListItem: {
    template: "<div class=\"list-item\"><slot name=\"prepend\" /><slot /><slot name=\"append\" /></div>",
  },
  VListItemTitle: slot("h4"),
  VAvatar: slot("span", "avatar"),
  VImg: { props: ["src"], template: "<img :src=\"src\">" },
  VIcon: { template: "<i class=\"icon\" />" },
  VChip: { props: ["color"], template: "<span class=\"chip\" :data-color=\"color\"><slot /></span>" },
  VProgressCircular: { template: "<span class=\"spinner\" />" },
  VBtn: {
    props: ["to", "loading"],
    template: "<a v-if=\"to\" class=\"btn\" :href=\"to\"><slot /></a><button v-else type=\"button\" class=\"btn\"><slot /></button>",
  },
  NuxtLink: { props: ["to"], template: "<a :href=\"to\"><slot /></a>" },
};

function job(overrides: Partial<RecipeIngestionJobSummary> = {}): RecipeIngestionJobSummary {
  return {
    id: "j1",
    batchId: "b1",
    position: 0,
    status: "ready",
    source: "app",
    sourceName: "upload/IMG_1.jpg",
    title: "Banana Mug Cake",
    pageCount: 1,
    thumbUrl: "/api/ai/ingest/jobs/j1/pages/0/thumb?v=abc",
    draftVersion: 1,
    errorCount: 0,
    warningCount: 0,
    task: null,
    error: null,
    recipe: null,
    localOnly: false,
    canDiscard: true,
    createdAt: "2026-10-03T12:00:00+00:00",
    ...overrides,
  };
}

const wrappers: VueWrapper[] = [];

function mountItem(value: RecipeIngestionJobSummary) {
  const wrapper = mount(IngestJobListItem, {
    props: { job: value, groupSlug: "home" },
    global: {
      mocks: { $globals: { icons: { lock: "lock", delete: "delete" } } },
      stubs: listItemStubs,
    },
  });
  wrappers.push(wrapper);
  return wrapper;
}

const status = (wrapper: VueWrapper) => wrapper.get(".job-status").text();

afterEach(() => {
  wrappers.forEach(wrapper => wrapper.unmount());
  wrappers.length = 0;
});

describe("IngestJobListItem", () => {
  test("a ready card: thumbnail, name linking to its review, Review and Discard", () => {
    const wrapper = mountItem(job());
    expect(wrapper.get("img").attributes("src")).toBe("/api/ai/ingest/jobs/j1/pages/0/thumb?v=abc");
    expect(wrapper.get(".job-title a").attributes("href")).toBe("/g/home/recipes/cards/j1");
    expect(wrapper.get(".job-title").text()).toBe("Banana Mug Cake");
    expect(status(wrapper)).toBe("Ready");
    expect(wrapper.get(".job-status").attributes("data-color")).toBe("success");
    expect(wrapper.get(".job-review").attributes("href")).toBe("/g/home/recipes/cards/j1");
    expect(wrapper.find(".job-retry").exists()).toBe(false);
    expect(wrapper.find(".job-discard").exists()).toBe(true);
  });

  test("a ready card with errors and warnings to check", () => {
    const wrapper = mountItem(job({ errorCount: 1, warningCount: 1 }));
    expect(status(wrapper)).toBe("Ready · 2 to check");
    expect(wrapper.get(".job-status").attributes("data-color")).toBe("warning");
  });

  test("a card being read: a short chip, and what's being done under it; one waiting says so", () => {
    const reading = mountItem(job({
      status: "processing",
      title: null,
      thumbUrl: null,
      task: { kind: "extract", state: "running", progressKey: "recipe-ingest.progress.suggesting-organizers" },
    }));
    expect(status(reading)).toBe("Reading");
    expect(reading.get(".job-caption").text()).toBe("Suggesting tags and categories");
    expect(status(mountItem(job({ status: "processing", task: { kind: "extract", state: "queued" } }))))
      .toBe("Waiting to be read");
    const running = mountItem(job({ status: "processing", task: { kind: "extract", state: "running" } }));
    expect(status(running)).toBe("Reading");
    expect(running.find(".job-caption").exists()).toBe(false);

    // not read yet: no name, so its place in the batch
    const untitled = mountItem(job({ status: "processing", position: 2, title: null, thumbUrl: null }));
    expect(untitled.get(".job-title").text()).toBe("Card 3");
    expect(untitled.find("img").exists()).toBe(false);
    expect(untitled.find(".spinner").exists()).toBe(true);
    expect(untitled.find(".job-review").exists()).toBe(false);
    // a card read without a name is untitled until the reviewer names it
    expect(mountItem(job({ title: null })).get(".job-title").text()).toBe("Untitled card");
  });

  test("a failed card says why under a short chip, and offers Retry", async () => {
    const wrapper = mountItem(job({ status: "failed", error: { code: "no_recipe_found", params: {} } }));
    expect(status(wrapper)).toBe("Failed");
    expect(wrapper.get(".job-caption").text()).toBe("No recipe was found on this card.");
    expect(wrapper.get(".job-caption").classes()).toContain("text-error");
    expect(wrapper.get(".job-status").attributes("data-color")).toBe("error");

    await wrapper.get(".job-retry").trigger("click");
    await wrapper.get(".job-discard").trigger("click");
    expect(wrapper.emitted("retry")?.[0]?.[0]).toMatchObject({ id: "j1" });
    expect(wrapper.emitted("discard")?.[0]?.[0]).toMatchObject({ id: "j1" });
  });

  test("a provider failure shows the detail it was stored with", () => {
    const wrapper = mountItem(job({ status: "failed", error: { code: "provider_failed", params: { detail: "HTTP 500" } } }));
    expect(wrapper.get(".job-caption").text()).toBe("The AI provider couldn't read the card (HTTP 500).");
  });

  test("an added card links to its recipe and can't be discarded", () => {
    const wrapper = mountItem(job({
      status: "committed",
      recipe: { id: "r1", slug: "banana-mug-cake", name: "Banana Mug Cake" },
    }));
    expect(status(wrapper)).toBe("Added");
    expect(wrapper.get(".job-title a").attributes("href")).toBe("/g/home/r/banana-mug-cake");
    expect(wrapper.get(".job-view-recipe").attributes("href")).toBe("/g/home/r/banana-mug-cake");
    expect(wrapper.find(".job-discard").exists()).toBe(false);
  });

  test("Discard shows only to who may discard the card", () => {
    expect(mountItem(job({ canDiscard: false })).find(".job-discard").exists()).toBe(false);
    expect(mountItem(job({ status: "failed", canDiscard: false })).find(".job-discard").exists()).toBe(false);
    expect(mountItem(job({ canDiscard: true })).find(".job-discard").exists()).toBe(true);
  });

  test("a card being added, and the local-only badge", () => {
    const wrapper = mountItem(job({ status: "committing", localOnly: true }));
    expect(status(wrapper)).toBe("Adding");
    expect(wrapper.get(".job-local-only").text()).toBe("Local only");
    expect(wrapper.find(".job-discard").exists()).toBe(false);
  });
});

describe("IngestJobListItem on a phone", () => {
  test("the name and the reason are laid out to wrap, and the actions can go under them", () => {
    const wrapper = mountItem(job({
      status: "failed",
      title: "Grandma's Famous Oatmeal Raisin Cookies with Brown Butter",
      error: { code: "limit_reached", params: {} },
    }));
    // the title isn't a one-line list title, and the reason isn't inside the chip
    expect(wrapper.get(".job-text .job-title").text()).toContain("Brown Butter");
    expect(wrapper.get(".job-status").text()).toBe("Failed");
    expect(wrapper.get(".job-text .job-caption").text())
      .toBe("Every AI provider for this task has reached its monthly token limit.");
    // actions follow the text in one wrapping row, not in the list item's append slot
    const body = wrapper.get(".job-body");
    expect(body.element.children[0]!.classList).toContain("job-text");
    expect(body.element.children[1]!.classList).toContain("job-actions");
    expect(body.find(".job-actions .job-retry").exists()).toBe(true);
  });
});

describe("IngestJobListItem names", () => {
  test("an inbox or API card not read yet is named by its file; an app card by its place", () => {
    expect(mountItem(job({ status: "processing", title: null, source: "inbox", sourceName: "inbox/home/kitchen/scan 3.jpg" }))
      .get(".job-title").text()).toBe("scan 3.jpg");
    expect(mountItem(job({ status: "processing", title: null, source: "api", sourceName: "upload/snapshot.jpg" }))
      .get(".job-title").text()).toBe("snapshot.jpg");
    expect(mountItem(job({ status: "processing", title: null, source: "app", position: 1, sourceName: "upload/image.jpg" }))
      .get(".job-title").text()).toBe("Card 2");
    expect(mountItem(job({ status: "failed", title: null, source: "api", sourceName: null, position: 0 }))
      .get(".job-title").text()).toBe("Card 1");
  });

  test("a failed card shows its file under its name", () => {
    const named = mountItem(job({ status: "failed", title: "Pancakes", source: "app", sourceName: "upload/IMG_7.jpg" }));
    expect(named.get(".job-source").text()).toBe("IMG_7.jpg");
    // already its title
    const inbox = mountItem(job({ status: "failed", title: null, source: "inbox", sourceName: "inbox/home/kitchen/a.jpg" }));
    expect(inbox.find(".job-source").exists()).toBe(false);
    expect(mountItem(job({ title: "Pancakes" })).find(".job-source").exists()).toBe(false);
  });
});

describe("IngestJobListItem Cancel", () => {
  test("a card being read, or waiting to be, can be cancelled", async () => {
    const wrapper = mountItem(job({ status: "processing", task: { kind: "extract", state: "running" } }));
    await wrapper.get(".job-cancel").trigger("click");
    expect(wrapper.emitted("cancel")?.[0]?.[0]).toMatchObject({ id: "j1" });
    expect(mountItem(job({ status: "processing", task: null })).find(".job-cancel").exists()).toBe(true);
    for (const other of ["ready", "failed", "committing", "committed"] as const) {
      expect(mountItem(job({ status: other })).find(".job-cancel").exists(), other).toBe(false);
    }
  });
});
