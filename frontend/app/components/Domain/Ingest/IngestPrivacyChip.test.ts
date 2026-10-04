import { mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, describe, expect, test } from "vitest";
import IngestPrivacyChip from "./IngestPrivacyChip.vue";
import type { RecipeIngestionSettingsOut } from "~/lib/api/types/recipe-ingest";

function settings(overrides: Partial<RecipeIngestionSettingsOut> = {}): RecipeIngestionSettingsOut {
  return {
    localOnly: false,
    crossRead: false,
    canReadCards: true,
    ocrAvailable: true,
    reader: { name: "Claude Sonnet", local: false, viaOcr: false },
    localOnlyAvailable: false,
    localReadiness: null,
    limits: {
      maxUploadBytes: 104857600,
      maxFileBytes: 31457280,
      maxImagesPerRequest: 20,
      maxPagesPerCard: 4,
      maxPixels: 100000000,
      maxJpegPixels: 256000000,
    },
    inbox: { enabled: false, folder: null },
    ...overrides,
  };
}

const slot = (tag = "div", className = "") => ({ template: `<${tag} class="${className}"><slot /></${tag}>` });

const wrappers: VueWrapper[] = [];

function mountChip(props: {
  settings: RecipeIngestionSettingsOut | null;
  localOnly?: boolean;
  alreadySent?: number;
  finishedBatch?: boolean;
}) {
  const wrapper = mount(IngestPrivacyChip, {
    props: {
      ...props,
      "onUpdate:localOnly": (value: boolean) => wrapper.setProps({ localOnly: value }),
    },
    global: {
      stubs: {
        VChip: {
          props: ["prependIcon", "color"],
          template: "<button type=\"button\" class=\"chip\" :data-icon=\"prependIcon\" :data-color=\"color\"><slot /></button>",
        },
        VExpandTransition: slot(),
        VCard: slot("div", "card"),
        VCardTitle: slot("h4"),
        VCardText: slot(),
        VAlert: { props: ["type"], template: "<div class=\"alert\" :data-type=\"type\"><slot /></div>" },
        VSwitch: {
          props: ["modelValue", "label", "hint"],
          emits: ["update:modelValue"],
          template: `
            <label class="switch">
              <input type="checkbox" :checked="modelValue" @change="$emit('update:modelValue', $event.target.checked)">
              {{ label }} <small>{{ hint }}</small>
            </label>
          `,
        },
      },
    },
  });
  wrappers.push(wrapper);
  return wrapper;
}

afterEach(() => {
  wrappers.forEach(wrapper => wrapper.unmount());
  wrappers.length = 0;
});

describe("IngestPrivacyChip", () => {
  test("a cloud reader", () => {
    const chip = mountChip({ settings: settings() }).get(".chip");
    expect(chip.text()).toBe("Read by Claude Sonnet (cloud)");
    expect(chip.classes()).toContain("privacy-cloud");
  });

  test("OCR, then a cloud model", () => {
    const wrapper = mountChip({ settings: settings({ reader: { name: "Claude Sonnet", local: false, viaOcr: true } }) });
    expect(wrapper.get(".chip").text()).toBe("Read with OCR, then Claude Sonnet (cloud)");
  });

  test("a local reader without a policy says where it reads, not that the card stays", () => {
    const wrapper = mountChip({ settings: settings({ reader: { name: "Ollama", local: true, viaOcr: false } }) });
    expect(wrapper.get(".chip").text()).toBe("Read by Ollama on your network");
    expect(wrapper.get(".chip").attributes("data-color")).toBeUndefined();
  });

  test("a local-only group: a lock and Stays on this server, with nothing to opt out of", async () => {
    const wrapper = mountChip({
      settings: settings({ localOnly: true, localOnlyAvailable: true, reader: { name: "Ollama", local: true } }),
    });
    const chip = wrapper.get(".chip");
    expect(chip.text()).toBe("Stays on this server");
    expect(chip.classes()).toContain("privacy-local");
    expect(chip.attributes("data-color")).toBe("success");

    await chip.trigger("click");
    expect(wrapper.get(".group-local").text()).toBe("Your group keeps recipe cards on this server.");
    expect(wrapper.find(".switch").exists()).toBe(false);
  });

  test("tapping it offers keeping this batch on this server when local providers can read cards", async () => {
    const wrapper = mountChip({ settings: settings({ localOnlyAvailable: true }), localOnly: false });
    expect(wrapper.find(".card").exists()).toBe(false);

    await wrapper.get(".chip").trigger("click");
    expect(wrapper.get(".card h4").text()).toBe("Where photos go");
    expect(wrapper.get(".switch").text()).toContain("Keep these cards on this server");

    await wrapper.get(".switch input").setValue(true);
    expect(wrapper.emitted("update:localOnly")).toEqual([[true]]);
    expect(wrapper.get(".chip").text()).toBe("Stays on this server");
  });

  test("cards that had already gone when the switch changed are said to keep their setting", async () => {
    const wrapper = mountChip({ settings: settings({ localOnlyAvailable: true }), localOnly: true, alreadySent: 3 });
    await wrapper.get(".chip").trigger("click");
    expect(wrapper.get(".already-sent").text()).toBe("3 cards already sent aren't affected.");
    // its own box, spaced from the switch's hint
    expect(wrapper.get(".already-sent").classes()).toContain("mt-4");
    expect(wrapper.get(".already-sent").attributes("data-type")).toBe("info");

    await wrapper.setProps({ alreadySent: 1 });
    expect(wrapper.get(".already-sent").text()).toBe("1 card already sent isn't affected.");
    await wrapper.setProps({ alreadySent: 0 });
    expect(wrapper.find(".already-sent").exists()).toBe(false);
  });

  test("when the switch finished the open batch, says so until the next photo", async () => {
    const wrapper = mountChip({
      settings: settings({ localOnlyAvailable: true }),
      localOnly: true,
      alreadySent: 2,
      finishedBatch: true,
    });
    await wrapper.get(".chip").trigger("click");
    expect(wrapper.findAll(".already-sent p").map(p => p.text())).toEqual([
      "2 cards already sent aren't affected.",
      "The cards already sent finish as their own batch; the next photo starts a new one.",
    ]);
    // the batch is sealed: only why it finished is left
    await wrapper.setProps({ alreadySent: 0 });
    expect(wrapper.get(".already-sent").text())
      .toBe("The cards already sent finish as their own batch; the next photo starts a new one.");
    await wrapper.setProps({ finishedBatch: false });
    expect(wrapper.find(".already-sent").exists()).toBe(false);
  });

  test("the switch says it's remembered on this device", async () => {
    const wrapper = mountChip({ settings: settings({ localOnlyAvailable: true }) });
    await wrapper.get(".chip").trigger("click");
    expect(wrapper.get(".switch small").text()).toContain("Remembered on this device.");
  });

  test("a local-only group with nothing on the network to read cards: a warning, not the green lock", async () => {
    const wrapper = mountChip({ settings: settings({ localOnly: true, localOnlyAvailable: false }) });
    const chip = wrapper.get(".chip");
    expect(chip.text()).toBe("Kept on this server, but nothing here can read it");
    expect(chip.classes()).toContain("privacy-blocked");
    expect(chip.attributes("data-color")).toBe("warning");
    expect(chip.attributes("data-icon")).not.toBe(mountChip({ settings: settings({ localOnly: true, localOnlyAvailable: true }) })
      .get(".chip").attributes("data-icon"));
  });

  test("the switch remembered on while nothing local can read cards: a warning, and the switch to turn it off", async () => {
    const wrapper = mountChip({ settings: settings({ localOnlyAvailable: false }), localOnly: true });
    expect(wrapper.get(".chip").attributes("data-color")).toBe("warning");
    await wrapper.get(".chip").trigger("click");
    expect(wrapper.get(".keep-local-unavailable").text()).toContain("cards sent with this on fail");

    await wrapper.get(".switch input").setValue(false);
    expect(wrapper.emitted("update:localOnly")).toEqual([[false]]);
  });

  test("no batch opt-in when nothing local can read cards", async () => {
    const wrapper = mountChip({ settings: settings({ localOnlyAvailable: false }) });
    await wrapper.get(".chip").trigger("click");
    expect(wrapper.find(".switch").exists()).toBe(false);
    expect(wrapper.get(".local-unavailable").text()).toBe("No AI provider on your network can read cards.");
  });

  test("no reader: says AI isn't set up", () => {
    expect(mountChip({ settings: settings({ reader: null }) }).get(".chip").text())
      .toBe("AI isn't set up to read recipe cards.");
    expect(mountChip({ settings: null }).get(".chip").text()).toBe("AI isn't set up to read recipe cards.");
  });
});
