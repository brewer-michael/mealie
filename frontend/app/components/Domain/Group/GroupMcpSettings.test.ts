import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import GroupMcpSettings from "./GroupMcpSettings.vue";
import { formatDateTime } from "~/composables/use-mcp";
import type { McpClientCreated, McpClientOut } from "~/lib/api/types/mcp";

const api = vi.hoisted(() => ({
  getClients: vi.fn(),
  deleteClient: vi.fn(),
  rotateClientSecret: vi.fn(),
}));
const copyText = vi.hoisted(() => vi.fn());
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));

vi.mock("~/composables/api", () => ({
  useUserApi: () => ({ mcp: api }),
}));
vi.mock("~/composables/use-copy", () => ({
  useCopy: () => ({ copyText }),
}));
vi.mock("~/composables/use-toast", () => ({
  alert: toast,
}));
// The dialog has its own tests; here it only reports what it saved
vi.mock("./GroupMcpClientDialog.vue", () => ({
  default: {
    name: "GroupMcpClientDialog",
    props: ["modelValue", "client"],
    emits: ["created", "updated", "update:modelValue"],
    template: "<div class=\"client-dialog\" :data-open=\"modelValue\" :data-client=\"client?.id ?? ''\" />",
  },
}));

const SECRET = "mmcp_cs_Y2xpZW50LXNlY3JldC1zaG93bi1vbmNl";

function client(overrides: Partial<McpClientOut> = {}): McpClientOut {
  return {
    id: "c1",
    groupId: "g1",
    name: "Home Assistant",
    clientId: "mmcp_0123456789abcdef01234567",
    isConfidential: true,
    pkceOptional: true,
    allowWriteScope: false,
    redirectUris: [
      "https://my.home-assistant.io/redirect/oauth",
      "http://homeassistant.local:8123/auth/external/callback",
    ],
    lastUsedAt: null,
    ...overrides,
  };
}

const claude = client({
  id: "c2",
  name: "Claude Code",
  clientId: "mmcp_fedcba9876543210fedcba98",
  isConfidential: false,
  pkceOptional: false,
  allowWriteScope: true,
  redirectUris: ["http://127.0.0.1/callback"],
  lastUsedAt: "2026-10-02T18:30:00Z",
});

const wrappers: VueWrapper[] = [];
const slot = (tag = "div", className = "") => ({ template: `<${tag} class="${className}"><slot /></${tag}>` });

async function mountSettings() {
  const wrapper = mount(GroupMcpSettings, {
    global: {
      mocks: { $globals: { icons: {} } },
      stubs: {
        BaseCardSectionTitle: {
          props: ["title"],
          template: "<div><h3>{{ title }}</h3><slot name=\"append-title\" /></div>",
        },
        BaseButton: {
          props: ["text", "disabled"],
          template: "<button type=\"button\" :disabled=\"disabled\">{{ text }}</button>",
        },
        NuxtLink: {
          props: ["to"],
          template: "<a :href=\"to\"><slot /></a>",
        },
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
        VCardText: slot(),
        VCard: slot("div", "client-card"),
        VCardItem: slot(),
        VCardTitle: slot("h4"),
        VCardSubtitle: slot("div", "subtitle"),
        VCardActions: slot(),
        VSpacer: slot(),
        VAlert: {
          props: ["title", "type"],
          template: "<div class=\"alert\" :data-type=\"type\"><strong v-if=\"title\">{{ title }}</strong><slot /></div>",
        },
        VBtn: {
          props: ["disabled", "loading"],
          template: "<button type=\"button\" :disabled=\"disabled\"><slot /></button>",
        },
        VTextField: {
          props: ["modelValue", "label"],
          template: "<div><input readonly :data-label=\"label\" :value=\"modelValue\"><slot name=\"append-inner\" /></div>",
        },
      },
    },
  });
  wrappers.push(wrapper);
  await flushPromises();
  return wrapper;
}

function button(wrapper: VueWrapper, text: string, within?: string) {
  const root = within ? wrapper.get(within) : wrapper;
  const found = root.findAll("button").find(b => b.text() === text || b.attributes("aria-label") === text);
  if (!found) {
    throw new Error(`No ${text} button`);
  }
  return found;
}

function clientCard(wrapper: VueWrapper, name: string) {
  const card = wrapper.findAll(".client-card").find(c => c.get("h4").text() === name);
  if (!card) {
    throw new Error(`No ${name} card`);
  }
  return card;
}

function dialog(wrapper: VueWrapper) {
  return wrapper.getComponent({ name: "GroupMcpClientDialog" });
}

describe("GroupMcpSettings", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getClients.mockResolvedValue({ data: [client(), claude] });
  });

  afterEach(() => {
    wrappers.forEach(wrapper => wrapper.unmount());
    wrappers.length = 0;
  });

  test("shows the MCP server URL to copy", async () => {
    const wrapper = await mountSettings();
    const url = `${window.location.origin}/api/mcp`;

    expect((wrapper.get("[data-label=\"MCP Server URL\"]").element as HTMLInputElement).value).toBe(url);
    await button(wrapper, "Copy MCP server URL").trigger("click");
    expect(copyText).toHaveBeenCalledExactlyOnceWith(url);
  });

  test("points to API tokens, and to the setting that shows them", async () => {
    const wrapper = await mountSettings();
    const note = wrapper.get(".api-token-note");

    expect(note.text()).toBe("Claude Desktop, Claude Code and other MCP clients can also connect with an API token from "
      + "your profile instead of an OAuth client. To see API tokens, turn on \"Show advanced features\" in your profile settings.");
    expect(note.findAll("a").map(a => [a.text(), a.attributes("href")])).toEqual([
      ["API token from your profile", "/user/profile/api-tokens"],
      ["profile settings", "/user/profile/edit"],
    ]);
  });

  test("lists each client's ID, redirect URIs, writes and last use", async () => {
    const wrapper = await mountSettings();

    const ha = clientCard(wrapper, "Home Assistant");
    expect(ha.get(".subtitle").text()).toBe("Confidential client (has a secret) · Read-only");
    expect(ha.text()).toContain("mmcp_0123456789abcdef01234567");
    expect(ha.findAll("li").map(li => li.text())).toEqual(client().redirectUris);
    expect(ha.text()).toContain("Never used");
    await button(wrapper, "Copy client ID", ".client-card").trigger("click");
    expect(copyText).toHaveBeenCalledExactlyOnceWith("mmcp_0123456789abcdef01234567");

    const other = clientCard(wrapper, "Claude Code");
    expect(other.get(".subtitle").text()).toBe("Public client (no secret) · Can ask to make changes");
    expect(other.text()).toContain(`Last used ${formatDateTime("2026-10-02T18:30:00Z", "en-US")}`);
    // A public client has no secret to rotate
    expect(other.findAll("button").some(b => b.text() === "Rotate Secret")).toBe(false);
  });

  test("shows a new client's secret once, until it's dismissed", async () => {
    const wrapper = await mountSettings();
    await button(wrapper, "Add Client").trigger("click");
    expect(dialog(wrapper).props("modelValue")).toBe(true);
    expect(dialog(wrapper).props("client")).toBeNull();

    const created: McpClientCreated = { ...client({ id: "c3", clientId: "mmcp_new" }), clientSecret: SECRET };
    dialog(wrapper).vm.$emit("created", created);
    await flushPromises();

    const panel = wrapper.get(".new-secret");
    expect(panel.get("h4").text()).toBe("Client secret for Home Assistant");
    // Only the warning is in the (low-contrast) alert, not the fields or the instructions
    const warning = panel.get(".alert[data-type=\"warning\"]");
    expect(warning.text()).toBe("Copy the client secret now. It won't be shown again.");
    expect(warning.find("input").exists()).toBe(false);
    expect((panel.get("[data-label=\"Client ID\"]").element as HTMLInputElement).value).toBe("mmcp_new");
    expect((panel.get("[data-label=\"Client Secret\"]").element as HTMLInputElement).value).toBe(SECRET);
    expect(panel.text()).toContain("Enter the client ID and secret in the app.");
    await button(wrapper, "Copy client secret").trigger("click");
    expect(copyText).toHaveBeenCalledWith(SECRET);
    expect(toast.success).toHaveBeenCalledWith("Client added");
    expect(api.getClients).toHaveBeenCalledTimes(2);

    await button(wrapper, "Done").trigger("click");

    expect(wrapper.find(".new-secret").exists()).toBe(false);
    expect(wrapper.html()).not.toContain(SECRET);
  });

  test("a secret shown can't be replaced before it's dismissed", async () => {
    const wrapper = await mountSettings();
    dialog(wrapper).vm.$emit("created", { ...client({ id: "c3", clientId: "mmcp_new" }), clientSecret: SECRET });
    await flushPromises();

    // Neither another client nor a rotated secret can take the panel's place
    expect(button(wrapper, "Add Client").attributes("disabled")).toBeDefined();
    expect(button(wrapper, "Rotate Secret", ".mcp-client").attributes("disabled")).toBeDefined();
    expect(wrapper.get(".secret-pending").text())
      .toBe("Copy the client secret above and select Done before adding another client or creating a new secret.");

    // A public client added meanwhile (say, from another tab's dialog) has no secret and leaves it alone
    dialog(wrapper).vm.$emit("created", { ...claude, clientSecret: null });
    await flushPromises();
    expect((wrapper.get("[data-label=\"Client Secret\"]").element as HTMLInputElement).value).toBe(SECRET);

    await button(wrapper, "Done").trigger("click");

    expect(button(wrapper, "Add Client").attributes("disabled")).toBeUndefined();
    expect(button(wrapper, "Rotate Secret", ".mcp-client").attributes("disabled")).toBeUndefined();
    expect(wrapper.find(".secret-pending").exists()).toBe(false);
  });

  test("a public client has no secret to show", async () => {
    const wrapper = await mountSettings();

    dialog(wrapper).vm.$emit("created", { ...claude, clientSecret: null });
    await flushPromises();

    expect(wrapper.find(".new-secret").exists()).toBe(false);
    expect(button(wrapper, "Add Client").attributes("disabled")).toBeUndefined();
  });

  test("edits a client", async () => {
    const wrapper = await mountSettings();

    await button(wrapper, "Edit", ".client-card").trigger("click");
    expect(dialog(wrapper).props("client")).toMatchObject({ id: "c1" });

    dialog(wrapper).vm.$emit("updated", client());
    await flushPromises();
    expect(toast.success).toHaveBeenCalledWith("Client updated");
    expect(api.getClients).toHaveBeenCalledTimes(2);
  });

  test("rotating a secret asks first, then shows the new secret once", async () => {
    api.rotateClientSecret.mockResolvedValue({ data: { clientId: client().clientId, clientSecret: SECRET } });
    const wrapper = await mountSettings();

    await button(wrapper, "Rotate Secret").trigger("click");
    expect(api.rotateClientSecret).not.toHaveBeenCalled();
    expect(wrapper.get(".confirm-dialog").text())
      .toContain("The current secret stops working right away. Enter the new secret in Home Assistant to keep it connected.");

    await wrapper.get(".dialog-confirm").trigger("click");
    await flushPromises();

    expect(api.rotateClientSecret).toHaveBeenCalledExactlyOnceWith("c1");
    expect((wrapper.get("[data-label=\"Client Secret\"]").element as HTMLInputElement).value).toBe(SECRET);
  });

  test("deleting a client asks first: it will lose access", async () => {
    api.deleteClient.mockResolvedValue({ data: client() });
    const wrapper = await mountSettings();

    await button(wrapper, "Delete", ".client-card").trigger("click");
    expect(api.deleteClient).not.toHaveBeenCalled();
    expect(wrapper.get(".confirm-dialog").attributes("data-title")).toBe("Delete Client");
    expect(wrapper.get(".confirm-dialog").text()).toContain("Home Assistant will lose access.");

    await wrapper.get(".dialog-confirm").trigger("click");
    await flushPromises();

    expect(api.deleteClient).toHaveBeenCalledExactlyOnceWith("c1");
    expect(wrapper.findAll(".client-card").map(card => card.get("h4").text())).toEqual(["Claude Code"]);
    expect(toast.success).toHaveBeenCalledWith("Client deleted");
  });

  test("says when there are no clients", async () => {
    api.getClients.mockResolvedValue({ data: [] });
    const wrapper = await mountSettings();

    expect(wrapper.get(".no-clients").text())
      .toBe("There are no OAuth clients yet. Add one to connect Home Assistant or another app.");
  });

  test("a failed load isn't shown as no clients, and can be retried", async () => {
    api.getClients.mockResolvedValueOnce({ data: null });
    const wrapper = await mountSettings();

    expect(wrapper.get(".alert[data-type=\"error\"]").text()).toContain("Couldn't load the OAuth clients");
    expect(wrapper.find(".no-clients").exists()).toBe(false);

    await button(wrapper, "Try Again").trigger("click");
    await flushPromises();
    expect(wrapper.findAll(".client-card")).toHaveLength(2);
    expect(wrapper.find(".alert[data-type=\"error\"]").exists()).toBe(false);
  });
});
