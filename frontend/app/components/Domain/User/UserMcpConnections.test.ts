import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import UserMcpConnections from "./UserMcpConnections.vue";
import { formatDateTime } from "~/composables/use-mcp";
import type { McpConnectionOut } from "~/lib/api/types/mcp";

const api = vi.hoisted(() => ({ getConnections: vi.fn(), disconnect: vi.fn() }));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));

vi.mock("~/composables/api", () => ({
  useUserApi: () => ({ mcp: api }),
}));
vi.mock("~/composables/use-toast", () => ({
  alert: toast,
}));

const homeAssistant: McpConnectionOut = {
  clientId: "c1",
  clientName: "Home Assistant",
  scopes: ["mcp:read"],
  createdAt: "2026-10-01T08:00:00Z",
  lastUsedAt: null,
};

const claude: McpConnectionOut = {
  clientId: "c2",
  clientName: "Claude Code",
  scopes: ["mcp:read", "mcp:write"],
  createdAt: "2026-09-20T08:00:00Z",
  lastUsedAt: "2026-10-02T18:30:00Z",
};

const wrappers: VueWrapper[] = [];
const slot = (tag = "div", className = "") => ({ template: `<${tag} class="${className}"><slot /></${tag}>` });

async function mountConnections() {
  const wrapper = mount(UserMcpConnections, {
    global: {
      mocks: { $globals: { icons: {} } },
      stubs: {
        BaseDialog: {
          props: ["modelValue", "title"],
          emits: ["confirm", "update:modelValue"],
          template: `
            <div v-if="modelValue" class="confirm-dialog" :data-title="title">
              <slot />
              <button type="button" class="dialog-confirm" @click="$emit('confirm'); $emit('update:modelValue', false)">Confirm</button>
            </div>
          `,
        },
        AppLoader: { template: "<div class=\"loader\" />" },
        VAlert: slot("div", "alert"),
        VList: slot("ul"),
        VListItem: { template: "<li class=\"connection\"><slot /><slot name=\"append\" /></li>" },
        VListItemTitle: slot("div", "title"),
        VListItemSubtitle: slot("div", "subtitle"),
        VDivider: { template: "<hr>" },
        VCardText: slot(),
        VBtn: {
          props: ["disabled", "loading"],
          template: "<button type=\"button\" :disabled=\"disabled\"><slot /></button>",
        },
      },
    },
  });
  wrappers.push(wrapper);
  await flushPromises();
  return wrapper;
}

function connection(wrapper: VueWrapper, name: string) {
  const found = wrapper.findAll(".connection").find(c => c.get(".title").text() === name);
  if (!found) {
    throw new Error(`No ${name} connection`);
  }
  return found;
}

describe("UserMcpConnections", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getConnections.mockResolvedValue({ data: [homeAssistant, claude] });
  });

  afterEach(() => {
    wrappers.forEach(wrapper => wrapper.unmount());
    wrappers.length = 0;
  });

  test("lists each app's permissions, when it was connected and last used", async () => {
    const wrapper = await mountConnections();

    const ha = connection(wrapper, "Home Assistant").findAll(".subtitle").map(s => s.text());
    expect(ha[0]).toBe("Can read recipes, meal plans and shopping lists");
    expect(ha[1]).toBe(`Connected ${formatDateTime(homeAssistant.createdAt, "en-US")} · Never used`);

    const other = connection(wrapper, "Claude Code").findAll(".subtitle").map(s => s.text());
    expect(other[0]).toBe("Can read recipes, meal plans and shopping lists, and make changes");
    expect(other[1]).toContain(`Last used ${formatDateTime(claude.lastUsedAt!, "en-US")}`);
  });

  test("disconnecting asks first, then removes the app", async () => {
    api.disconnect.mockResolvedValue({ data: homeAssistant });
    const wrapper = await mountConnections();

    await connection(wrapper, "Home Assistant").get("button").trigger("click");
    expect(api.disconnect).not.toHaveBeenCalled();
    expect(wrapper.get(".confirm-dialog").attributes("data-title")).toBe("Disconnect App");
    expect(wrapper.get(".confirm-dialog").text()).toContain(
      "Home Assistant stops working with your Mealie account right away. To use it again, connect it again from the app.",
    );

    await wrapper.get(".dialog-confirm").trigger("click");
    await flushPromises();

    expect(api.disconnect).toHaveBeenCalledExactlyOnceWith("c1");
    expect(wrapper.findAll(".connection").map(c => c.get(".title").text())).toEqual(["Claude Code"]);
    expect(toast.success).toHaveBeenCalledWith("App disconnected");
  });

  test("a failed disconnect keeps the app", async () => {
    api.disconnect.mockResolvedValue({ data: null });
    const wrapper = await mountConnections();

    await connection(wrapper, "Home Assistant").get("button").trigger("click");
    await wrapper.get(".dialog-confirm").trigger("click");
    await flushPromises();

    expect(wrapper.findAll(".connection")).toHaveLength(2);
    expect(toast.error).toHaveBeenCalledWith("Failed to disconnect the app");
  });

  test("says when no apps are connected", async () => {
    api.getConnections.mockResolvedValue({ data: [] });
    const wrapper = await mountConnections();

    expect(wrapper.get(".no-connections").text())
      .toBe("You haven't connected any apps. Apps you approve, such as Home Assistant, appear here.");
  });

  test("a failed load isn't shown as no apps", async () => {
    api.getConnections.mockResolvedValue({ data: null });
    const wrapper = await mountConnections();

    expect(wrapper.get(".alert").text()).toContain("Couldn't load your connected apps");
    expect(wrapper.find(".no-connections").exists()).toBe(false);
  });
});
