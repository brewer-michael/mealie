import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import GroupAIProviderDialog from "./GroupAIProviderDialog.vue";
import GroupAIProviderModelField from "./GroupAIProviderModelField.vue";

const api = vi.hoisted(() => ({
  getOne: vi.fn(),
  listModels: vi.fn(),
  listSavedModels: vi.fn(),
}));

vi.mock("~/composables/api", () => ({
  useUserApi: () => ({ aiProviders: api }),
}));

const wrappers: VueWrapper[] = [];

// Inputs are found by their (en-US) label; failed rules are shown after them
const inputStub = (type = "text") => ({
  props: ["modelValue", "label", "rules"],
  template: `
    <div>
      <input type="${type}" :data-label="label" :value="modelValue" @input="$emit('update:modelValue', $event.target.value)">
      <span v-for="rule in rules ?? []" :key="String(rule)" class="rule-error">{{ rule(modelValue) === true ? "" : rule(modelValue) }}</span>
    </div>
  `,
});

function mountDialog(providerId?: string) {
  const wrapper = mount(GroupAIProviderDialog, {
    props: { modelValue: true, providerId },
    global: {
      // The model field is the fork's own component, mounted for real
      components: { GroupAIProviderModelField },
      directives: { noAutofill: {} },
      mocks: {
        $globals: { icons: {} },
      },
      stubs: {
        BaseDialog: {
          name: "BaseDialog",
          template: `
            <div>
              <slot />
              <slot name="custom-card-action" />
              <button class="dialog-submit" type="button" @click="$emit('submit')" />
            </div>
          `,
        },
        BaseKeyValueEditor: { template: "<div />" },
        AppLoader: { template: "<div />" },
        VForm: { template: "<form><slot /></form>", methods: { reset() {} } },
        VCardText: { template: "<div><slot /></div>" },
        VExpansionPanels: { template: "<div><slot /></div>" },
        VExpansionPanel: { template: "<div><slot /></div>" },
        VExpansionPanelTitle: { template: "<div><slot /></div>" },
        VExpansionPanelText: { template: "<div><slot /></div>" },
        VDivider: { template: "<hr>" },
        VAlert: { template: "<div class=\"alert\"><slot /></div>" },
        VBtn: {
          props: ["disabled"],
          template: "<button type=\"button\" :disabled=\"disabled\"><slot /></button>",
        },
        VTextField: inputStub(),
        VSelect: {
          props: ["modelValue", "label", "items"],
          template: `
            <select :data-label="label" :value="modelValue" @change="$emit('update:modelValue', $event.target.value)">
              <option v-for="item in items" :key="item.value" :value="item.value">{{ item.title }}</option>
            </select>
          `,
        },
        VCombobox: {
          props: ["modelValue", "label", "items", "messages"],
          template: `
            <div>
              <input :data-label="label" :value="modelValue" @input="$emit('update:modelValue', $event.target.value)">
              <span v-for="item in items" :key="item" class="model-option">{{ item }}</span>
              <span v-if="messages" class="model-messages">{{ messages }}</span>
              <slot name="append" />
            </div>
          `,
        },
        VNumberInput: {
          props: ["modelValue", "label", "max"],
          template: `
            <input
              type="number"
              :data-label="label"
              :max="max"
              :value="modelValue"
              @input="$emit('update:modelValue', $event.target.value === '' ? null : Number($event.target.value))"
            >
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

function button(wrapper: VueWrapper, text: string) {
  const found = wrapper.findAll("button").find(b => b.text() === text);
  if (!found) {
    throw new Error(`No ${text} button`);
  }
  return found;
}

function loadModelsButton(wrapper: VueWrapper) {
  const button = wrapper.findAll("button").find(b => b.text() === "Load Models");
  if (!button) {
    throw new Error("No Load Models button");
  }
  return button;
}

describe("GroupAIProviderDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal("useNuxtApp", () => ({ $globals: { icons: {} } }));
  });

  afterEach(() => {
    wrappers.forEach(wrapper => wrapper.unmount());
    wrappers.length = 0;
    vi.unstubAllGlobals();
  });

  test("creates a Claude provider with a monthly token limit", async () => {
    const wrapper = mountDialog();
    await flushPromises();

    await field(wrapper, "Provider Name").setValue("Claude");
    await field(wrapper, "API Type").setValue("anthropic");
    await field(wrapper, "Model").setValue("claude-sonnet-5-5");
    await field(wrapper, "API Key").setValue("sk-ant-test");
    await field(wrapper, "Monthly Token Limit").setValue("250000");
    await wrapper.get(".dialog-submit").trigger("click");

    expect(wrapper.emitted("create")?.[0]?.[0]).toMatchObject({
      name: "Claude",
      model: "claude-sonnet-5-5",
      apiKey: "sk-ant-test",
      baseUrl: null,
      protocol: "anthropic",
      monthlyTokenLimit: 250000,
    });
  });

  test("the monthly token limit is capped at what the backend stores", async () => {
    const wrapper = mountDialog();
    await flushPromises();

    expect(field(wrapper, "Monthly Token Limit").attributes("max")).toBe("2147483647");
  });

  test("defaults to an OpenAI-compatible provider without a limit", async () => {
    const wrapper = mountDialog();
    await flushPromises();

    await field(wrapper, "Provider Name").setValue("OpenAI");
    await field(wrapper, "Model").setValue("gpt-5");
    await field(wrapper, "API Key").setValue("sk-test");
    await wrapper.get(".dialog-submit").trigger("click");

    expect(wrapper.emitted("create")?.[0]?.[0]).toMatchObject({ protocol: "openai", monthlyTokenLimit: null });
  });

  test("an unsaved provider needs a key to load models", async () => {
    api.listModels.mockResolvedValue({
      data: [
        { id: "claude-opus-5-5", displayName: "Claude Opus 5.5", supportsImages: true },
        { id: "claude-sonnet-5-5", displayName: "Claude Sonnet 5.5", supportsImages: true },
      ],
    });
    const wrapper = mountDialog();
    await flushPromises();
    await field(wrapper, "API Type").setValue("anthropic");

    expect(loadModelsButton(wrapper).attributes("disabled")).toBeDefined();
    expect(wrapper.get(".model-messages").text()).toBe("Enter the API key below to load the provider's models.");

    await field(wrapper, "API Key").setValue("sk-ant-test");
    expect(wrapper.find(".model-messages").exists()).toBe(false);
    await loadModelsButton(wrapper).trigger("click");
    await flushPromises();

    expect(api.listModels).toHaveBeenCalledWith(expect.objectContaining({
      protocol: "anthropic",
      baseUrl: null,
      apiKey: "sk-ant-test",
    }));
    expect(wrapper.findAll(".model-option").map(option => option.text())).toEqual([
      "claude-opus-5-5",
      "claude-sonnet-5-5",
    ]);
  });

  test("a saved provider loads models with its saved key and keeps its settings", async () => {
    api.getOne.mockResolvedValue({
      data: {
        id: "provider-id",
        name: "Claude",
        model: "claude-sonnet-5-5",
        protocol: "anthropic",
        monthlyTokenLimit: 1000000,
        timeout: 120,
      },
    });
    api.listSavedModels.mockResolvedValue({ data: [] });
    const wrapper = mountDialog("provider-id");
    await flushPromises();

    // No key needed: the saved one is used
    expect(wrapper.find(".model-messages").exists()).toBe(false);
    await loadModelsButton(wrapper).trigger("click");
    await flushPromises();

    expect(api.listSavedModels).toHaveBeenCalledWith("provider-id", expect.not.objectContaining({ apiKey: expect.anything() }));
    expect(api.listSavedModels.mock.calls[0]?.[1]).toMatchObject({ protocol: "anthropic", timeout: 120 });
    expect(wrapper.get(".alert").text()).toContain("didn't list any models");

    await wrapper.get(".dialog-submit").trigger("click");
    expect(wrapper.emitted("update")?.[0]).toEqual([
      "provider-id",
      expect.objectContaining({ protocol: "anthropic", monthlyTokenLimit: 1000000 }),
    ]);
  });

  test("asks for a saved API key that can't be read", async () => {
    api.getOne.mockResolvedValue({
      data: { id: "provider-id", name: "Claude", model: "claude-sonnet-5-5", protocol: "anthropic", apiKeySet: false },
    });
    const wrapper = mountDialog("provider-id");
    await flushPromises();

    const warning = "This provider's API key can't be read. Enter it again.";
    expect(wrapper.findAll(".alert").map(alert => alert.text())).toContain(warning);

    await field(wrapper, "API Key").setValue("sk-ant-new");
    expect(wrapper.findAll(".alert").map(alert => alert.text())).not.toContain(warning);
  });

  test("asks for the API key again when the saved one can't be reused", async () => {
    api.getOne.mockResolvedValue({
      data: {
        id: "provider-id",
        name: "Ollama",
        model: "llama4",
        protocol: "openai",
        baseUrl: "http://localhost:11434/v1",
        requestHeaders: { "X-B": "2", "X-A": "1" },
        apiKeySet: true,
      },
    });
    const wrapper = mountDialog("provider-id");
    await flushPromises();
    const notice = "To test the connection or load models with a different API type, base URL or request headers, "
      + "enter the API key again.";

    expect(wrapper.find(".alert").exists()).toBe(false);

    await field(wrapper, "Base URL").setValue("http://other-host:11434/v1");
    expect(wrapper.get(".alert").text()).toBe(notice);

    await field(wrapper, "API Key").setValue("sk-new");
    expect(wrapper.find(".alert").exists()).toBe(false);

    await field(wrapper, "API Key").setValue("");
    await field(wrapper, "Base URL").setValue("http://localhost:11434/v1");
    expect(wrapper.find(".alert").exists()).toBe(false);
  });

  test("rejects a base URL with a query string or fragment", async () => {
    const wrapper = mountDialog();
    await flushPromises();
    await field(wrapper, "Provider Name").setValue("OpenAI");
    await field(wrapper, "Model").setValue("gpt-5");
    await field(wrapper, "API Key").setValue("sk-test");
    expect(button(wrapper, "Test Connection").attributes("disabled")).toBeUndefined();

    await field(wrapper, "Base URL").setValue("https://example.com/v1?key=abc");

    expect(wrapper.text()).toContain("The base URL can't contain \"?\" or \"#\".");
    expect(button(wrapper, "Test Connection").attributes("disabled")).toBeDefined();
  });

  test("doesn't warn about a readable API key", async () => {
    api.getOne.mockResolvedValue({
      data: { id: "provider-id", name: "OpenAI", model: "gpt-5", protocol: "openai", apiKeySet: true },
    });
    const wrapper = mountDialog("provider-id");
    await flushPromises();

    expect(wrapper.find(".alert").exists()).toBe(false);
  });

  test("a failed model list says so once and leaves the model field usable", async () => {
    // The API's reason is shown by the axios plugin's toast, so the alert doesn't repeat it
    api.listModels.mockResolvedValue({
      data: null,
      error: { response: { data: { detail: { message: "AuthenticationError (HTTP 401)" } } } },
    });
    const wrapper = mountDialog();
    await flushPromises();

    await field(wrapper, "API Key").setValue("wrong");
    await loadModelsButton(wrapper).trigger("click");
    await flushPromises();

    const alert = wrapper.get(".alert");
    expect(alert.text()).toBe("Couldn't load the provider's models. You can still type a model name.");
    expect(wrapper.findAll(".model-option")).toHaveLength(0);
  });

  test("closing the dialog clears the loaded models", async () => {
    api.listModels.mockResolvedValue({
      data: [{ id: "gpt-5", displayName: null, supportsImages: true }],
    });
    const wrapper = mountDialog();
    await flushPromises();
    await field(wrapper, "API Key").setValue("sk-test");
    await loadModelsButton(wrapper).trigger("click");
    await flushPromises();
    expect(wrapper.findAll(".model-option")).toHaveLength(1);

    wrapper.findComponent({ name: "BaseDialog" }).vm.$emit("close");
    await flushPromises();

    expect(wrapper.findAll(".model-option")).toHaveLength(0);
  });
});
