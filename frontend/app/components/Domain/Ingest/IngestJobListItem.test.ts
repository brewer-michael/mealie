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
    errorCount: 0,
    warningCount: 0,
    task: null,
    error: null,
    recipe: null,
    localOnly: false,
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

  test("a card being read shows its progress; one waiting says so", () => {
    expect(status(mountItem(job({
      status: "processing",
      title: null,
      thumbUrl: null,
      task: { kind: "extract", state: "running", progressKey: "recipe-ingest.progress.reading-card" },
    })))).toBe("Reading the card");
    expect(status(mountItem(job({ status: "processing", task: { kind: "extract", state: "queued" } }))))
      .toBe("Waiting to be read");
    expect(status(mountItem(job({ status: "processing", task: { kind: "extract", state: "running" } }))))
      .toBe("Reading");

    const untitled = mountItem(job({ status: "processing", title: null, thumbUrl: null }));
    expect(untitled.get(".job-title").text()).toBe("Untitled card");
    expect(untitled.find("img").exists()).toBe(false);
    expect(untitled.find(".spinner").exists()).toBe(true);
    expect(untitled.find(".job-review").exists()).toBe(false);
  });

  test("a failed card says why, and offers Retry", async () => {
    const wrapper = mountItem(job({ status: "failed", error: { code: "no_recipe_found", params: {} } }));
    expect(status(wrapper)).toBe("Failed: No recipe was found on this card.");
    expect(wrapper.get(".job-status").attributes("data-color")).toBe("error");

    await wrapper.get(".job-retry").trigger("click");
    await wrapper.get(".job-discard").trigger("click");
    expect(wrapper.emitted("retry")?.[0]?.[0]).toMatchObject({ id: "j1" });
    expect(wrapper.emitted("discard")?.[0]?.[0]).toMatchObject({ id: "j1" });
  });

  test("a provider failure shows the detail it was stored with", () => {
    const wrapper = mountItem(job({ status: "failed", error: { code: "provider_failed", params: { detail: "HTTP 500" } } }));
    expect(status(wrapper)).toBe("Failed: The AI provider couldn't read the card (HTTP 500).");
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

  test("a card being added, and the local-only badge", () => {
    const wrapper = mountItem(job({ status: "committing", localOnly: true }));
    expect(status(wrapper)).toBe("Adding");
    expect(wrapper.get(".job-local-only").text()).toBe("Local only");
    expect(wrapper.find(".job-discard").exists()).toBe(false);
  });
});
