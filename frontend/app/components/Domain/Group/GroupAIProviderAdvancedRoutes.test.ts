import { mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, describe, expect, test } from "vitest";
import GroupAIProviderAdvancedRoutes from "./GroupAIProviderAdvancedRoutes.vue";
import { emptyRoutes, type AIProviderRoutes } from "~/composables/use-ai-provider-routing";

const wrappers: VueWrapper[] = [];
const slotStub = { template: "<div><slot /></div>" };

function mountAdvanced(routes: AIProviderRoutes) {
  const wrapper = mount(GroupAIProviderAdvancedRoutes, {
    props: { modelValue: routes, providers: [{ id: "a", name: "A" }, { id: "b", name: "B" }] },
    global: {
      stubs: {
        GroupAIProviderRouteSelect: {
          name: "GroupAIProviderRouteSelect",
          props: ["modelValue", "providers", "routeSlot", "primaryId"],
          template: "<div class=\"route-select\" :data-slot=\"routeSlot\" :data-ids=\"modelValue.join()\" />",
        },
        VExpansionPanels: slotStub,
        VExpansionPanel: slotStub,
        VExpansionPanelTitle: slotStub,
        VExpansionPanelText: slotStub,
      },
    },
  });

  wrappers.push(wrapper);
  return wrapper;
}

describe("GroupAIProviderAdvancedRoutes", () => {
  afterEach(() => {
    wrappers.forEach(wrapper => wrapper.unmount());
    wrappers.length = 0;
  });

  test("lists the slots without a primary", () => {
    const wrapper = mountAdvanced({ ...emptyRoutes(), fast: ["b", "a"] });
    const selects = wrapper.findAll(".route-select");

    expect(selects.map(select => select.attributes("data-slot"))).toEqual(["planner", "fast", "embedding"]);
    expect(selects[1]?.attributes("data-ids")).toBe("b,a");
    expect(wrapper.text()).toContain("To move a provider to the end, remove it and add it again.");
  });

  test("an edited list replaces only its own slot", async () => {
    const routes = { ...emptyRoutes(), default: ["a"] };
    const wrapper = mountAdvanced(routes);

    wrapper.findAllComponents({ name: "GroupAIProviderRouteSelect" })[2]?.vm.$emit("update:modelValue", ["b"]);
    await wrapper.vm.$nextTick();

    expect(wrapper.emitted("update:modelValue")?.[0]?.[0]).toEqual({ ...emptyRoutes(), default: ["a"], embedding: ["b"] });
    expect(routes.embedding).toEqual([]);
  });
});
