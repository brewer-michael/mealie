import { mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, describe, expect, test } from "vitest";
import GroupAIProviderSettingsEditor from "./GroupAIProviderSettingsEditor.vue";
import { emptyRoutes, type AIProviderRoutes } from "~/composables/use-ai-provider-routing";
import type { AIProviderSettingsOut } from "~/lib/api/types/user";

const wrappers: VueWrapper[] = [];

const settings: AIProviderSettingsOut = {
  defaultProviderId: "a",
  audioProviderId: null,
  imageProviderId: "b",
  providers: [
    { id: "a", name: "A" },
    { id: "b", name: "B" },
  ],
  aiEnabled: true,
  audioProviderEnabled: false,
  imageProviderEnabled: true,
  ocrFallbackEnabled: false,
};

const slotStub = { template: "<div><slot /></div>" };

type EditorProps = InstanceType<typeof GroupAIProviderSettingsEditor>["$props"];

function mountEditor(routes?: AIProviderRoutes, extraProps: Partial<EditorProps> = {}) {
  // Without routes, like the admin's group page, which doesn't bind them
  const props: EditorProps = routes
    ? {
        "modelValue": settings,
        ...extraProps,
        routes,
        "onUpdate:routes": (value: AIProviderRoutes | null) => wrapper.setProps({ routes: value ?? undefined }),
      }
    : { modelValue: settings, ...extraProps };

  const wrapper = mount(GroupAIProviderSettingsEditor, {
    props,
    global: {
      mocks: {
        $globals: { icons: {} },
      },
      stubs: {
        BaseCardSectionTitle: { template: "<div><slot name=\"append-title\" /></div>" },
        BaseButton: { template: "<button type=\"button\" />" },
        BaseButtonGroup: { template: "<div />" },
        GroupAIProviderDialog: { template: "<div />" },
        GroupAIProviderRouteSelect: {
          name: "GroupAIProviderRouteSelect",
          props: ["modelValue", "providers", "routeSlot", "primaryId"],
          template: "<div class=\"route-select\" :data-slot=\"routeSlot\" :data-primary=\"primaryId\" />",
        },
        GroupAIProviderAdvancedRoutes: {
          name: "GroupAIProviderAdvancedRoutes",
          props: ["modelValue", "providers"],
          template: "<div class=\"advanced-routes\" />",
        },
        VAlert: { template: "<div class=\"alert\"><slot /></div>" },
        VAutocomplete: { template: "<div />" },
        VCard: slotStub,
        VCardText: slotStub,
        VCardSubtitle: slotStub,
        VRow: slotStub,
        VCol: slotStub,
        VExpansionPanels: slotStub,
        VExpansionPanel: slotStub,
        VExpansionPanelTitle: slotStub,
        VExpansionPanelText: slotStub,
        VTooltip: slotStub,
        VIcon: slotStub,
      },
    },
  });

  wrappers.push(wrapper);
  return wrapper;
}

describe("GroupAIProviderSettingsEditor", () => {
  afterEach(() => {
    wrappers.forEach(wrapper => wrapper.unmount());
    wrappers.length = 0;
  });

  test("hides the fallback lists without routes (e.g. the admin's group page)", () => {
    const wrapper = mountEditor();

    expect(wrapper.findAll(".route-select")).toHaveLength(0);
    expect(wrapper.find(".advanced-routes").exists()).toBe(false);
    expect(wrapper.find(".alert").exists()).toBe(false);
  });

  test("shows each slot's fallbacks with the slot's primary", () => {
    const wrapper = mountEditor(emptyRoutes());
    const selects = wrapper.findAll(".route-select");

    // Default, Audio and Image, in the editor's order
    expect(selects.map(select => select.attributes("data-slot"))).toEqual(["default", "audio", "image"]);
    expect(selects.map(select => select.attributes("data-primary"))).toEqual(["a", undefined, "b"]);
    expect(wrapper.find(".advanced-routes").exists()).toBe(true);
  });

  test("an edited list replaces only its own slot", async () => {
    const routes = { ...emptyRoutes(), default: ["b"] };
    const wrapper = mountEditor(routes);

    const audio = wrapper.findAllComponents({ name: "GroupAIProviderRouteSelect" })[1];
    audio?.vm.$emit("update:modelValue", ["b", "a"]);
    await wrapper.vm.$nextTick();

    expect(wrapper.emitted("update:routes")?.[0]?.[0]).toEqual({ ...emptyRoutes(), default: ["b"], audio: ["b", "a"] });
    expect(routes.audio).toEqual([]);
  });

  test("the Advanced lists update the routes", async () => {
    const wrapper = mountEditor(emptyRoutes());

    const advanced = wrapper.findComponent({ name: "GroupAIProviderAdvancedRoutes" });
    advanced.vm.$emit("update:modelValue", { ...emptyRoutes(), fast: ["b"] });
    await wrapper.vm.$nextTick();

    expect(wrapper.emitted("update:routes")?.[0]?.[0]).toEqual({ ...emptyRoutes(), fast: ["b"] });
  });

  test("warns about a provider whose API key can't be read", () => {
    const warning = "This provider's API key can't be read. Enter it again.";

    // Only the group's own settings page knows, so there's no warning without its list
    expect(mountEditor().text()).not.toContain(warning);

    // "B" was saved with a key the server can't decrypt
    const wrapper = mountEditor(emptyRoutes(), { unreadableKeyProviderIds: ["b"] });
    expect(wrapper.text().split(warning)).toHaveLength(2);
    expect(wrapper.text()).toMatch(new RegExp(`\\bB\\s+${warning}`));
  });

  test("says when the fallback lists couldn't be loaded", () => {
    const wrapper = mountEditor(undefined, { routesLoadFailed: true });

    expect(wrapper.findAll(".route-select")).toHaveLength(0);
    expect(wrapper.get(".alert").text()).toBe("Fallback providers couldn't be loaded. Reload the page to edit them.");
  });
});
