import { mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, describe, expect, test } from "vitest";
import IngestTranscription from "./IngestTranscription.vue";

const wrappers: VueWrapper[] = [];

afterEach(() => {
  wrappers.forEach(wrapper => wrapper.unmount());
  wrappers.length = 0;
});

describe("IngestTranscription", () => {
  test("shows what the card says with its markers picked out", () => {
    const wrapper = mount(IngestTranscription, { props: { text: "[illegible] banana\nMicrowave [blank] minutes" } });
    wrappers.push(wrapper);

    expect(wrapper.get("pre").text()).toBe("[illegible] banana\nMicrowave [blank] minutes");
    expect(wrapper.findAll(".ingest-transcription__marker").map(marker => marker.text())).toEqual(["[illegible]", "[blank]"]);
  });

  test("says when nothing was kept", () => {
    const wrapper = mount(IngestTranscription, { props: { text: "" } });
    wrappers.push(wrapper);
    expect(wrapper.text()).toBe("No transcription was kept for this card.");
  });
});
