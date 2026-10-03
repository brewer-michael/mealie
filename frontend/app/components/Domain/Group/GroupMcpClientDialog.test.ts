import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import GroupMcpClientDialog from "./GroupMcpClientDialog.vue";
import type { McpClientCreate, McpClientOut } from "~/lib/api/types/mcp";

const api = vi.hoisted(() => ({
  createClient: vi.fn(),
  updateClient: vi.fn(),
  getHomeAssistantPreset: vi.fn(),
}));

vi.mock("~/composables/api", () => ({
  useUserApi: () => ({ mcp: api }),
}));

const HA_REDIRECT_URIS = [
  "https://my.home-assistant.io/redirect/oauth",
  "http://homeassistant.local:8123/auth/external/callback",
];

function preset(redirectUris = HA_REDIRECT_URIS): McpClientCreate {
  return { name: "Home Assistant", redirectUris, isConfidential: true, pkceOptional: true, allowWriteScope: false };
}

function savedClient(overrides: Partial<McpClientOut> = {}): McpClientOut {
  return {
    id: "c1",
    groupId: "g1",
    name: "Home Assistant",
    clientId: "mmcp_0123456789abcdef01234567",
    isConfidential: true,
    pkceOptional: true,
    allowWriteScope: false,
    redirectUris: [...HA_REDIRECT_URIS],
    ...overrides,
  };
}

const wrappers: VueWrapper[] = [];

function mountDialog(client: McpClientOut | null = null) {
  const wrapper = mount(GroupMcpClientDialog, {
    props: { modelValue: true, client },
    global: {
      mocks: { $globals: { icons: {} } },
      stubs: {
        BaseDialog: {
          props: ["submitDisabled", "title"],
          emits: ["submit"],
          template: `
            <div :data-title="title">
              <slot />
              <button class="dialog-submit" type="button" :disabled="submitDisabled" @click="$emit('submit')">Submit</button>
            </div>
          `,
        },
        VCardText: { template: "<div><slot /></div>" },
        VAlert: { template: "<div class=\"alert\"><slot /></div>" },
        VBtn: {
          props: ["disabled"],
          template: "<button type=\"button\" :disabled=\"disabled\"><slot /></button>",
        },
        VTextField: {
          props: ["modelValue", "label", "rules", "errorMessages"],
          template: `
            <div>
              <input :data-label="label" :value="modelValue" @input="$emit('update:modelValue', $event.target.value)">
              <span v-for="(rule, i) in rules ?? []" :key="i" class="rule-error">{{ rule(modelValue) === true ? "" : rule(modelValue) }}</span>
              <span v-if="errorMessages" class="field-error">{{ errorMessages }}</span>
            </div>
          `,
        },
        VCheckbox: {
          props: ["modelValue", "label"],
          template: `
            <label>
              <input type="checkbox" :data-label="label" :checked="modelValue" @change="$emit('update:modelValue', $event.target.checked)">
              {{ label }}
            </label>
          `,
        },
        VRadioGroup: {
          props: ["modelValue", "label"],
          emits: ["update:modelValue"],
          provide() {
            return {
              radioGroup: {
                select: (value: unknown) => (this as unknown as { $emit: (e: string, v: unknown) => void }).$emit("update:modelValue", value),
                selected: () => (this as unknown as { modelValue: unknown }).modelValue,
              },
            };
          },
          template: "<div class=\"radio-group\" :data-label=\"label\"><slot /></div>",
        },
        VRadio: {
          props: ["label", "value"],
          inject: ["radioGroup"],
          template: `
            <button type="button" class="radio" :aria-pressed="radioGroup.selected() === value" @click="radioGroup.select(value)">
              {{ label }}
            </button>
          `,
        },
      },
    },
  });
  wrappers.push(wrapper);
  return wrapper;
}

function field(wrapper: VueWrapper, label: string) {
  return wrapper.get(`[data-label="${label}"]`);
}

function value(wrapper: VueWrapper, label: string) {
  return (field(wrapper, label).element as HTMLInputElement).value;
}

function checked(wrapper: VueWrapper, label: string) {
  return (field(wrapper, label).element as HTMLInputElement).checked;
}

/** Types a Home Assistant address and leaves the field, as clicking Create does (`setValue` fires input and change) */
async function enterHomeAssistantUrl(wrapper: VueWrapper, url: string) {
  await field(wrapper, "Home Assistant Address (optional)").setValue(url);
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

const REFUSED = "Use https://, or http:// only for a local network address (http://ha.example.com:8123/auth/external/callback)";

function button(wrapper: VueWrapper, text: string) {
  const found = wrapper.findAll("button").find(b => b.text() === text);
  if (!found) {
    throw new Error(`No ${text} button`);
  }
  return found;
}

describe("GroupMcpClientDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getHomeAssistantPreset.mockResolvedValue({ data: preset() });
    Element.prototype.scrollIntoView = vi.fn();
  });

  afterEach(() => {
    wrappers.forEach(wrapper => wrapper.unmount());
    wrappers.length = 0;
  });

  test("the Home Assistant preset is confidential, PKCE optional and read-only", async () => {
    api.createClient.mockResolvedValue({ data: { ...savedClient(), clientSecret: "mmcp_cs_secret" }, error: null });
    const wrapper = mountDialog();
    await flushPromises();

    expect(api.getHomeAssistantPreset).toHaveBeenCalledExactlyOnceWith(undefined);
    expect(value(wrapper, "Name")).toBe("Home Assistant");
    expect(value(wrapper, "Redirect URI 1")).toBe(HA_REDIRECT_URIS[0]);
    expect(value(wrapper, "Redirect URI 2")).toBe(HA_REDIRECT_URIS[1]);
    expect(checked(wrapper, "PKCE optional")).toBe(true);
    expect(checked(wrapper, "Allow changes")).toBe(false);
    expect(wrapper.find("[data-label=\"Client Type\"]").exists()).toBe(false);

    await wrapper.get(".dialog-submit").trigger("click");
    await flushPromises();

    expect(api.createClient).toHaveBeenCalledExactlyOnceWith({
      name: "Home Assistant",
      redirectUris: HA_REDIRECT_URIS,
      isConfidential: true,
      pkceOptional: true,
      allowWriteScope: false,
    });
    expect(wrapper.emitted("created")?.[0]?.[0]).toMatchObject({ clientSecret: "mmcp_cs_secret" });
    expect(wrapper.emitted("update:modelValue")?.at(-1)).toEqual([false]);
  });

  test("a Home Assistant address replaces the second redirect URI", async () => {
    const wrapper = mountDialog();
    await flushPromises();
    const custom = [HA_REDIRECT_URIS[0]!, "http://192.168.1.20:8123/auth/external/callback"];
    api.getHomeAssistantPreset.mockResolvedValue({ data: preset(custom) });

    await field(wrapper, "Home Assistant Address (optional)").setValue("http://192.168.1.20:8123");
    await field(wrapper, "Home Assistant Address (optional)").trigger("change");
    await flushPromises();

    expect(api.getHomeAssistantPreset).toHaveBeenLastCalledWith("http://192.168.1.20:8123");
    expect(value(wrapper, "Redirect URI 2")).toBe("http://192.168.1.20:8123/auth/external/callback");
  });

  test("a Home Assistant address that isn't a URL isn't sent", async () => {
    const wrapper = mountDialog();
    await flushPromises();

    await enterHomeAssistantUrl(wrapper, "192.168.1.20:8123");
    await flushPromises();

    expect(api.getHomeAssistantPreset).toHaveBeenCalledOnce();
    expect(wrapper.text()).toContain("Must start with https:// or http://");
    expect(wrapper.get(".dialog-submit").attributes("disabled")).toBeDefined();
  });

  test("Create clicked right after entering a Home Assistant address waits for its redirect URI", async () => {
    api.createClient.mockResolvedValue({ data: { ...savedClient(), clientSecret: "mmcp_cs_secret" }, error: null });
    const wrapper = mountDialog();
    await flushPromises();
    const custom = [HA_REDIRECT_URIS[0]!, "http://192.168.1.20:8123/auth/external/callback"];
    const response = deferred<{ data: McpClientCreate; error: null }>();
    api.getHomeAssistantPreset.mockReturnValueOnce(response.promise);

    // Leaving the field (for the Create button) starts loading the preset; the click still counts
    await enterHomeAssistantUrl(wrapper, "http://192.168.1.20:8123");
    expect(wrapper.get(".dialog-submit").attributes("disabled")).toBeUndefined();
    await wrapper.get(".dialog-submit").trigger("click");
    await flushPromises();
    expect(api.createClient).not.toHaveBeenCalled();

    response.resolve({ data: preset(custom), error: null });
    await flushPromises();

    expect(api.createClient).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ redirectUris: custom }));
    expect(wrapper.emitted("created")).toHaveLength(1);
  });

  test("a refused Home Assistant address says why, and can't be used", async () => {
    const wrapper = mountDialog();
    await flushPromises();
    api.getHomeAssistantPreset.mockResolvedValueOnce({
      data: null,
      error: { response: { status: 400, data: { detail: { message: REFUSED } } } },
    });

    await enterHomeAssistantUrl(wrapper, "http://ha.example.com:8123");
    await flushPromises();

    expect(wrapper.get(".field-error").text()).toBe(REFUSED);
    expect(wrapper.find(".alert").exists()).toBe(false);
    expect(wrapper.get(".dialog-submit").attributes("disabled")).toBeDefined();
    await wrapper.get(".dialog-submit").trigger("click");
    await flushPromises();
    expect(api.createClient).not.toHaveBeenCalled();

    // Clearing the address goes back to the default redirect URIs
    await enterHomeAssistantUrl(wrapper, "");
    await flushPromises();

    expect(wrapper.find(".field-error").exists()).toBe(false);
    expect(value(wrapper, "Redirect URI 2")).toBe(HA_REDIRECT_URIS[1]);
    expect(wrapper.get(".dialog-submit").attributes("disabled")).toBeUndefined();
  });

  test("Create after entering an address the server refuses doesn't add the client", async () => {
    const wrapper = mountDialog();
    await flushPromises();
    api.getHomeAssistantPreset.mockResolvedValueOnce({
      data: null,
      error: { response: { status: 400, data: { detail: { message: REFUSED } } } },
    });

    await enterHomeAssistantUrl(wrapper, "http://ha.example.com:8123");
    await wrapper.get(".dialog-submit").trigger("click");
    await flushPromises();

    expect(api.createClient).not.toHaveBeenCalled();
    expect(wrapper.get(".field-error").text()).toBe(REFUSED);
  });

  test("a preset that couldn't be loaded says so", async () => {
    api.getHomeAssistantPreset.mockResolvedValueOnce({ data: null, error: { response: { status: 500 } } });
    const wrapper = mountDialog();
    await flushPromises();

    expect(wrapper.get(".alert").text())
      .toBe("Couldn't load the Home Assistant preset. Enter the details yourself, or choose another preset.");
    expect(wrapper.find(".field-error").exists()).toBe(false);
  });

  test("another client can be public, which always needs PKCE", async () => {
    api.createClient.mockResolvedValue({ data: { ...savedClient({ isConfidential: false }), clientSecret: null }, error: null });
    const wrapper = mountDialog();
    await flushPromises();

    await button(wrapper, "Other MCP client").trigger("click");
    expect(value(wrapper, "Name")).toBe("");
    expect(value(wrapper, "Redirect URI 1")).toBe("");
    expect(wrapper.find("[data-label=\"Home Assistant Address (optional)\"]").exists()).toBe(false);

    await field(wrapper, "Name").setValue("Claude Code");
    await field(wrapper, "Redirect URI 1").setValue("http://localhost/callback");
    expect(wrapper.find("[data-label=\"PKCE optional\"]").exists()).toBe(true);
    await button(wrapper, "Public: no secret, uses PKCE (desktop apps such as Claude Code)").trigger("click");
    expect(wrapper.find("[data-label=\"PKCE optional\"]").exists()).toBe(false);
    await field(wrapper, "Allow changes").setValue(true);

    await wrapper.get(".dialog-submit").trigger("click");
    await flushPromises();

    expect(api.createClient).toHaveBeenCalledExactlyOnceWith({
      name: "Claude Code",
      redirectUris: ["http://localhost/callback"],
      isConfidential: false,
      pkceOptional: false,
      allowWriteScope: true,
    });
  });

  test("redirect URIs can be added and removed", async () => {
    const wrapper = mountDialog();
    await flushPromises();

    await button(wrapper, "Add Redirect URI").trigger("click");
    await field(wrapper, "Redirect URI 3").setValue("http://127.0.0.1/callback");
    const remove = wrapper.findAll("button").find(b => b.attributes("aria-label") === "Remove redirect URI 1");
    await remove!.trigger("click");

    expect(value(wrapper, "Redirect URI 1")).toBe(HA_REDIRECT_URIS[1]);
    expect(value(wrapper, "Redirect URI 2")).toBe("http://127.0.0.1/callback");
    expect(wrapper.find("[data-label=\"Redirect URI 3\"]").exists()).toBe(false);
  });

  test("an invalid redirect URI can't be saved", async () => {
    const wrapper = mountDialog();
    await flushPromises();

    await field(wrapper, "Redirect URI 1").setValue("ftp://example.com/callback");

    expect(wrapper.text()).toContain("Must start with https:// or http://");
    expect(wrapper.get(".dialog-submit").attributes("disabled")).toBeDefined();
  });

  test("a refused save shows the reason and stays open", async () => {
    api.createClient.mockResolvedValue({
      data: null,
      error: { response: { status: 422, data: { detail: [{ msg: "Value error, Use https://, or http:// only for a local network address (http://example.com/cb)" }] } } },
    });
    const wrapper = mountDialog();
    await flushPromises();

    await wrapper.get(".dialog-submit").trigger("click");
    await flushPromises();

    expect(wrapper.get(".alert").text()).toBe("Use https://, or http:// only for a local network address (http://example.com/cb)");
    // Below the form, which may be scrolled up
    expect(Element.prototype.scrollIntoView).toHaveBeenCalledExactlyOnceWith({ block: "nearest" });
    expect(vi.mocked(Element.prototype.scrollIntoView).mock.contexts[0]).toBe(wrapper.get(".alert").element);
    expect(wrapper.emitted("created")).toBeUndefined();
    expect(wrapper.emitted("update:modelValue")).toBeUndefined();
  });

  test("edits a client without changing its type", async () => {
    const client = savedClient({ allowWriteScope: false });
    api.updateClient.mockResolvedValue({ data: { ...client, allowWriteScope: true }, error: null });
    const wrapper = mountDialog(client);
    await flushPromises();

    expect(api.getHomeAssistantPreset).not.toHaveBeenCalled();
    expect(wrapper.find(".radio-group").exists()).toBe(false);
    expect(wrapper.text()).toContain("Confidential client (has a secret). This can't be changed.");
    expect(value(wrapper, "Redirect URI 2")).toBe(HA_REDIRECT_URIS[1]);

    await field(wrapper, "Allow changes").setValue(true);
    await wrapper.get(".dialog-submit").trigger("click");
    await flushPromises();

    expect(api.updateClient).toHaveBeenCalledExactlyOnceWith("c1", {
      name: "Home Assistant",
      redirectUris: HA_REDIRECT_URIS,
      pkceOptional: true,
      allowWriteScope: true,
    });
    expect(wrapper.emitted("updated")?.[0]?.[0]).toMatchObject({ allowWriteScope: true });
  });
});
