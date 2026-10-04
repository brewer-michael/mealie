import { mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, describe, expect, test } from "vitest";
import { reactive } from "vue";
import IngestNoteList from "./IngestNoteList.vue";
import { normalizeDraft, type ReviewDraft } from "~/composables/use-recipe-ingest-review";
import type { CardFlag } from "~/lib/api/types/recipe-ingest";

const field = {
  props: ["modelValue", "label", "appendInnerIcon", "readonly"],
  emits: ["update:modelValue"],
  template: `
    <label class="field" :data-icon="appendInnerIcon">{{ label }}
      <input :value="modelValue" :readonly="readonly" @input="$emit('update:modelValue', $event.target.value)">
    </label>
  `,
};

const stubs = {
  VTextField: field,
  VTextarea: field,
  VIcon: { template: "<i class=\"icon\" />" },
  VSpacer: { template: "<span />" },
  VBtn: {
    props: ["disabled"],
    emits: ["click"],
    template: "<button type=\"button\" :class=\"$attrs.class\" :aria-label=\"$attrs['aria-label']\" :disabled=\"disabled\" @click=\"$emit('click')\"><slot /></button>",
  },
};

const icons = { alert: "warning-icon", alertCircle: "error-icon" };
const wrappers: VueWrapper[] = [];
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function draft(): ReviewDraft {
  return reactive(normalizeDraft({
    name: "Banana Mug Cake",
    notes: [
      { id: "n1", title: "From", text: "Grandma Jo, 1962" },
      { id: "n2", title: "", text: "Doubles [illegible] in a 9x13 pan" },
    ],
  }));
}

function mountWith(model: ReviewDraft, flags: CardFlag[] = [], readonly = false, extra: Record<string, unknown> = {}) {
  const wrapper = mount(IngestNoteList, {
    props: { modelValue: model, flags, readonly, ...extra },
    global: { mocks: { $globals: { icons } }, stubs },
  });
  wrappers.push(wrapper);
  return wrapper;
}

function input(wrapper: VueWrapper, label: string, index = 0) {
  const found = wrapper.findAll("label").filter(l => l.text().startsWith(label))[index];
  if (!found) {
    throw new Error(`No ${label} field`);
  }
  return found;
}

function button(wrapper: VueWrapper, text: string, index = 0) {
  const found = wrapper.findAll("button").filter(b => b.text() === text)[index];
  if (!found) {
    throw new Error(`No ${text} button`);
  }
  return found;
}

afterEach(() => {
  wrappers.forEach(wrapper => wrapper.unmount());
  wrappers.length = 0;
});

describe("IngestNoteList", () => {
  test("a flagged note gets a coloured edge and an icon, like a step; the flag finds its note by id", () => {
    const flags: CardFlag[] = [
      { id: "illegible:notes:n2", kind: "illegible", severity: "error", source: "marker", field: "notes", ref: "n2" },
      { id: "unsure:notes:n1", kind: "unsure", severity: "warning", source: "model", field: "notes", ref: "n1", params: { text: "1962" } },
    ];
    const wrapper = mountWith(draft(), flags);

    expect(wrapper.findAll(".ingest-note").map(note => note.attributes("id"))).toEqual(["ingest-field-notes-n1", "ingest-field-notes-n2"]);
    expect(wrapper.get("#ingest-field-notes-n1").classes()).toContain("ingest-note--warning");
    expect(wrapper.get("#ingest-field-notes-n2").classes()).toContain("ingest-note--error");
    expect(input(wrapper, "Note 1").attributes("data-icon")).toBe("warning-icon");
    expect(input(wrapper, "Note 2").attributes("data-icon")).toBe("error-icon");
  });

  test("a resolved flag, or one on another note, leaves a note plain", () => {
    const flags: CardFlag[] = [
      { id: "unsure:notes:n1", kind: "unsure", severity: "warning", source: "model", field: "notes", ref: "n1", resolution: "dismissed" },
      { id: "illegible:notes:gone", kind: "illegible", severity: "error", source: "marker", field: "notes", ref: "gone" },
    ];
    const wrapper = mountWith(draft(), flags);

    expect(wrapper.findAll(".ingest-note--error, .ingest-note--warning")).toHaveLength(0);
    expect(input(wrapper, "Note 1").attributes("data-icon")).toBeUndefined();
  });

  test("the flag stays with its note when another note is deleted or the text is edited", async () => {
    const model = draft();
    const flags: CardFlag[] = [{ id: "illegible:notes:n2", kind: "illegible", severity: "error", source: "marker", field: "notes", ref: "n2" }];
    const wrapper = mountWith(model, flags);

    await button(wrapper, "Delete", 0).trigger("click");
    expect(model.notes.map(note => note.id)).toEqual(["n2"]);
    expect(wrapper.get("#ingest-field-notes-n2").classes()).toContain("ingest-note--error");

    // editing keeps the note's id, so the server keeps its flag's resolution
    await input(wrapper, "Note 1").get("input").setValue("Doubles well in a 9x13 pan");
    expect(model.notes).toEqual([{ id: "n2", title: "", text: "Doubles well in a 9x13 pan" }]);
    await input(wrapper, "Title").get("input").setValue("Tip");
    expect(model.notes[0]).toEqual({ id: "n2", title: "Tip", text: "Doubles well in a 9x13 pan" });
  });

  test("a new note gets an id of its own", async () => {
    const model = draft();
    const wrapper = mountWith(model);

    await button(wrapper, "Add note").trigger("click");
    await button(wrapper, "Add note").trigger("click");
    expect(model.notes).toHaveLength(4);
    expect(model.notes[2]).toMatchObject({ title: "", text: "" });
    expect(model.notes[2]!.id).toMatch(UUID);
    expect(new Set(model.notes.map(note => note.id)).size).toBe(4);
    expect(wrapper.findAll(".ingest-note")).toHaveLength(4);
  });

  test("each note can be re-read from the card, by its id", async () => {
    const wrapper = mountWith(draft(), [], false, { canReread: true });

    await button(wrapper, "Re-read", 1).trigger("click");
    expect(wrapper.emitted("reread")).toEqual([["n2"]]);
    // not on a card that can't be re-read
    await wrapper.setProps({ canReread: false });
    expect(wrapper.findAll("button").filter(b => b.text() === "Re-read")).toHaveLength(0);
  });

  test("read only: the fields can't be changed and there's nothing to add or delete", () => {
    const wrapper = mountWith(draft(), [], true);

    expect(input(wrapper, "Note 1").get("input").attributes("readonly")).toBeDefined();
    expect(wrapper.findAll("button")).toHaveLength(0);
  });

  test("no notes: just the way to add one", () => {
    const wrapper = mountWith(reactive(normalizeDraft({ name: "Banana Mug Cake" })));

    expect(wrapper.findAll(".ingest-note")).toHaveLength(0);
    expect(wrapper.findAll("button").map(b => b.text())).toEqual(["Add note"]);
  });
});
