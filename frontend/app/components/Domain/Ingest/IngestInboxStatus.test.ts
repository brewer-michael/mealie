import { mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, describe, expect, test } from "vitest";
import IngestInboxStatus from "./IngestInboxStatus.vue";
import type { IngestInboxInfo, RecipeIngestionSettingsOut } from "~/lib/api/types/recipe-ingest";

const wrappers: VueWrapper[] = [];

function settings(inbox: Partial<IngestInboxInfo>, overrides: Partial<RecipeIngestionSettingsOut> = {}): RecipeIngestionSettingsOut {
  return {
    enabled: true,
    canReadCards: true,
    readerRunning: true,
    limits: {
      maxUploadBytes: 104857600,
      maxFileBytes: 31457280,
      maxImagesPerRequest: 20,
      maxPagesPerCard: 4,
      maxPixels: 100000000,
      maxJpegPixels: 256000000,
    },
    inbox: { enabled: true, folder: "home/family", waiting: 0, waitingReason: null, rejections: [], ...inbox },
    ...overrides,
  };
}

function mountStatus(value: RecipeIngestionSettingsOut | null) {
  const wrapper = mount(IngestInboxStatus, {
    props: { settings: value },
    global: {
      stubs: {
        VAlert: {
          props: ["type"],
          template: "<div class=\"alert\" :data-type=\"type\"><slot /></div>",
        },
      },
    },
  });
  wrappers.push(wrapper);
  return wrapper;
}

function when(at: string) {
  return new Intl.DateTimeFormat("en-US", { dateStyle: "medium", timeStyle: "short" }).format(new Date(at));
}

afterEach(() => {
  wrappers.forEach(wrapper => wrapper.unmount());
  wrappers.length = 0;
});

describe("IngestInboxStatus", () => {
  test("nothing waiting and nothing refused: nothing to say", () => {
    expect(mountStatus(settings({})).find(".ingest-inbox-status").exists()).toBe(false);
    expect(mountStatus(null).find(".ingest-inbox-status").exists()).toBe(false);
  });

  test("photos waiting say why, by the server's reason", () => {
    const quota = mountStatus(settings({ waiting: 3, waitingReason: "quota" }));
    expect(quota.get(".inbox-waiting").attributes("data-type")).toBe("warning");
    expect(quota.get(".inbox-waiting").text())
      .toBe("3 photos are waiting in the inbox: too many of your group's cards are being read. They're added as those finish.");

    expect(mountStatus(settings({ waiting: 1, waitingReason: "cannot_read" })).get(".inbox-waiting").text())
      .toBe("1 photo is waiting in the inbox: AI isn't set up to read recipe cards.");
    expect(mountStatus(settings({ waiting: 2, waitingReason: "local_only_unavailable" })).get(".inbox-waiting").text())
      .toBe("2 photos are waiting in the inbox: your group keeps cards on this server, and no AI provider on your network can read them.");
  });

  test("photos waiting without a reason are being added, unless nothing on the server reads cards", () => {
    const adding = mountStatus(settings({ waiting: 2 }));
    expect(adding.get(".inbox-waiting").attributes("data-type")).toBe("info");
    expect(adding.get(".inbox-waiting").text()).toBe("2 photos in the inbox are being added.");

    const stopped = mountStatus(settings({ waiting: 2 }, { readerRunning: false }));
    expect(stopped.get(".inbox-waiting").attributes("data-type")).toBe("warning");
    expect(stopped.get(".inbox-waiting").text()).toBe("2 photos are waiting in the inbox: nothing on the server is reading cards.");
  });

  test("the server stops counting at 1000", () => {
    expect(mountStatus(settings({ waiting: 1000, waitingReason: "quota" })).get(".inbox-waiting").text())
      .toMatch(/^1000\+ photos are waiting in the inbox: /);
  });

  test("lists the photos it refused lately, with why and when, and where they are", () => {
    const wrapper = mountStatus(settings({
      rejections: [
        { name: "Grandma's card", reason: "no_permission", at: "2026-10-04T14:00:00Z" },
        { name: "IMG_0001.JPG", reason: "too_many_pixels", at: "2026-10-04T13:00:00" },
        { name: "scan.png", reason: "too_many_pixels", at: "2026-10-04T12:00:00Z" },
        { name: "IMG_0002.jpg", reason: "duplicate", at: null },
        { name: "notes.txt", reason: null, at: "2026-10-03T08:30:00Z" },
      ],
    }));

    expect(wrapper.get(".inbox-rejections .text-subtitle-2").text()).toBe("Not added from the inbox");
    const rows = wrapper.findAll(".inbox-rejection");
    expect(rows.map(row => row.get(".inbox-rejection-name").text()))
      .toEqual(["Grandma's card", "IMG_0001.JPG", "scan.png", "IMG_0002.jpg", "notes.txt"]);
    expect(rows.map(row => row.get(".inbox-rejection-reason").text())).toEqual([
      "Mealie may not move this out of the inbox folder. Give Mealie's group write access to the household folder, and to a card folder itself (umask 002).",
      // a JPEG may have more pixels than other photos
      "This photo has more than 256 megapixels.",
      "This photo has more than 100 megapixels.",
      "Already scanned",
      "Its note in the failed folder says why.",
    ]);
    // a time without an offset is UTC
    expect(rows[1]!.get(".inbox-rejection-time").text()).toBe(`· ${when("2026-10-04T13:00:00Z")}`);
    expect(rows[0]!.get(".inbox-rejection-time").text()).toBe(`· ${when("2026-10-04T14:00:00Z")}`);
    expect(rows[3]!.find(".inbox-rejection-time").exists()).toBe(false);
    expect(wrapper.get(".inbox-failed-folder").text())
      .toBe("Refused photos are in the failed folder of home/family, each with a note saying why.");
    expect(wrapper.find(".inbox-waiting").exists()).toBe(false);
  });

  test("photos it may not move stay where they are: no word of the failed folder", () => {
    const wrapper = mountStatus(settings({ rejections: [{ name: "Grandma's card", reason: "no_permission", at: null }] }));
    expect(wrapper.findAll(".inbox-rejection")).toHaveLength(1);
    expect(wrapper.find(".inbox-failed-folder").exists()).toBe(false);
  });

  test("nothing when the inbox is off, or the server doesn't take cards", () => {
    const off = settings({ waiting: 3, waitingReason: "quota" });
    off.inbox = { ...off.inbox, enabled: false };
    expect(mountStatus(off).find(".ingest-inbox-status").exists()).toBe(false);
    expect(mountStatus(settings({ waiting: 3 }, { enabled: false })).find(".ingest-inbox-status").exists()).toBe(false);
  });
});
