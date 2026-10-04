import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import HouseholdNotifierAIEvents from "./HouseholdNotifierAIEvents.vue";
import { resetRecipeIngestSettings, useRecipeIngestSettings } from "~/composables/use-recipe-ingest";
import type { RecipeIngestionSettingsOut } from "~/lib/api/types/recipe-ingest";

const api = vi.hoisted(() => ({
  getNotifierEvents: vi.fn(),
  updateNotifierEvents: vi.fn(),
  testNotifierEvents: vi.fn(),
  getSettings: vi.fn(),
}));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
/** Who is signed in: a household manager unless a test says otherwise */
const auth = vi.hoisted(() => ({ user: { value: null as null | Record<string, boolean> } }));

vi.mock("~/composables/api", () => ({
  useUserApi: () => ({ recipeIngest: api }),
}));
vi.mock("~/composables/use-toast", () => ({
  alert: toast,
}));
vi.mock("~/composables/use-mealie-auth", () => ({
  useMealieAuth: () => auth,
}));

const wrappers: VueWrapper[] = [];

/** The group's card settings, which say whether BASE_URL is set */
function settings(overrides: Partial<RecipeIngestionSettingsOut> = {}): RecipeIngestionSettingsOut {
  return {
    enabled: true,
    canReadCards: true,
    baseUrlSet: true,
    readerRunning: true,
    limits: {
      maxUploadBytes: 104857600,
      maxFileBytes: 31457280,
      maxImagesPerRequest: 20,
      maxPagesPerCard: 4,
      maxPixels: 100000000,
      maxJpegPixels: 256000000,
    },
    ...overrides,
  };
}

/** A failed request as the API client answers a quiet one: the code, without the message */
function failure(status: number, detail: Record<string, unknown>) {
  return { data: null, error: Object.assign(new Error(String(status)), { response: { status, data: { detail } } }) };
}

function mountToggle(notifierId = "n1") {
  const wrapper = mount(HouseholdNotifierAIEvents, {
    props: { notifierId },
    global: {
      mocks: { $globals: { icons: { testTube: "test-tube" } } },
      stubs: {
        VAlert: {
          props: { type: String, closable: Boolean },
          emits: ["click:close"],
          template: `<div class="alert" :data-type="type"><slot />
            <button v-if="closable" type="button" class="alert-close" aria-label="Close" @click="$emit('click:close')" /></div>`,
        },
        VBtn: {
          props: ["disabled", "loading"],
          template: "<button type=\"button\" :disabled=\"disabled\" :data-loading=\"loading\"><slot /></button>",
        },
        VSwitch: {
          props: ["modelValue", "label", "messages", "disabled"],
          emits: ["update:modelValue"],
          template: `
            <label class="switch">
              <input
                type="checkbox"
                :checked="modelValue"
                :disabled="disabled"
                @change="$emit('update:modelValue', $event.target.checked)"
              >
              <span class="label">{{ label }}</span> <small v-for="message in messages" :key="message" class="message">{{ message }}</small>
            </label>
          `,
        },
      },
    },
  });
  wrappers.push(wrapper);
  return wrapper;
}

function button(wrapper: VueWrapper, text: string) {
  const found = wrapper.findAll("button").find(b => b.text() === text);
  if (!found) {
    throw new Error(`No ${text} button`);
  }
  return found;
}

function checkbox(wrapper: VueWrapper) {
  return wrapper.get(".switch input").element as HTMLInputElement;
}

describe("HouseholdNotifierAIEvents", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetRecipeIngestSettings();
    auth.user.value = { canManageHousehold: true, canManage: false, admin: false };
    api.getNotifierEvents.mockResolvedValue({ data: { recipeIngestionReady: false }, error: null });
    api.getSettings.mockResolvedValue({ data: settings(), error: null });
  });

  afterEach(() => {
    wrappers.forEach(wrapper => wrapper.unmount());
    wrappers.length = 0;
  });

  test("shows the notifier's recipe cards switch", async () => {
    api.getNotifierEvents.mockResolvedValue({ data: { recipeIngestionReady: true }, error: null });
    const wrapper = mountToggle("n7");
    // nothing can be switched before the saved value is known
    expect(checkbox(wrapper).disabled).toBe(true);
    await flushPromises();

    expect(api.getNotifierEvents).toHaveBeenCalledExactlyOnceWith("n7");
    expect(wrapper.get("h4").text()).toBe("Recipe cards");
    expect(wrapper.get(".label").text()).toBe("Recipe cards ready to review");
    // under it: what it sends, and that it doesn't wait for the notifier's Save
    expect(wrapper.findAll(".message").map(message => message.text())).toEqual([
      "One notification when a batch of cards has been read, with a link to review them, and one when photos put in the inbox couldn't be added.",
      "Saved right away. The options above wait for Save.",
    ]);
    expect(checkbox(wrapper).checked).toBe(true);
    expect(checkbox(wrapper).disabled).toBe(false);
  });

  test("saves as soon as it's switched", async () => {
    api.updateNotifierEvents.mockResolvedValue({ data: { recipeIngestionReady: true }, error: null });
    const wrapper = mountToggle();
    await flushPromises();

    await wrapper.get(".switch input").setValue(true);
    await flushPromises();

    expect(api.updateNotifierEvents).toHaveBeenCalledExactlyOnceWith("n1", { recipeIngestionReady: true });
    expect(checkbox(wrapper).checked).toBe(true);
    expect(toast.error).not.toHaveBeenCalled();
  });

  test("a failed save switches it back and says so", async () => {
    api.updateNotifierEvents.mockResolvedValue({ data: null, error: new Error("500") });
    const wrapper = mountToggle();
    await flushPromises();

    await wrapper.get(".switch input").setValue(true);
    await flushPromises();

    expect(checkbox(wrapper).checked).toBe(false);
    expect(toast.error).toHaveBeenCalledExactlyOnceWith("Couldn't save the notifier's recipe card setting");
  });

  test("a failed load isn't shown as off, and can be retried", async () => {
    api.getNotifierEvents.mockResolvedValueOnce({ data: null, error: new Error("404") });
    const wrapper = mountToggle();
    await flushPromises();

    expect(wrapper.get(".alert[data-type=\"error\"]").text()).toContain("Couldn't load this notifier's recipe card setting");
    expect(wrapper.find(".switch").exists()).toBe(false);

    await button(wrapper, "Retry").trigger("click");
    await flushPromises();
    expect(wrapper.find(".alert").exists()).toBe(false);
    expect(checkbox(wrapper).checked).toBe(false);
  });

  test("sends a test notification, and says under its button that it was sent", async () => {
    api.testNotifierEvents.mockResolvedValue({ data: null, error: null });
    const wrapper = mountToggle("n3");
    await flushPromises();

    await button(wrapper, "Send test notification").trigger("click");
    await flushPromises();

    // quiet: the outcome shows once, here, not as a toast that doesn't say which notifier
    expect(api.testNotifierEvents).toHaveBeenCalledExactlyOnceWith("n3", { suppressAlert: true });
    expect(wrapper.get(".test-result").text()).toBe("Test notification sent");
    expect(wrapper.get(".test-result").attributes("data-type")).toBe("success");
    expect(toast.success).not.toHaveBeenCalled();

    await wrapper.get(".test-result .alert-close").trigger("click");
    expect(wrapper.find(".test-result").exists()).toBe(false);
  });

  test("someone who doesn't manage the household is told the test was sent, not that it arrived", async () => {
    // the server answers 204 to them whatever happened (it says 502 only to managers), so "sent" can't promise more
    api.testNotifierEvents.mockResolvedValue({ data: null, error: null });
    const send = async (wrapper: VueWrapper) => {
      await button(wrapper, "Send test notification").trigger("click");
      await flushPromises();
      return wrapper.get(".test-result").text();
    };

    auth.user.value = { canManageHousehold: false, canManage: false, admin: false };
    const member = mountToggle();
    await flushPromises();
    expect(await send(member)).toBe("Test notification sent. Only household managers are told when one isn't delivered.");
    expect(member.get(".test-result").attributes("data-type")).toBe("success");

    // a group manager or an admin is told when it fails, like a household manager
    for (const user of [{ canManage: true }, { admin: true }]) {
      auth.user.value = { canManageHousehold: false, canManage: false, admin: false, ...user };
      const manager = mountToggle();
      await flushPromises();
      expect(await send(manager)).toBe("Test notification sent");
    }
  });

  test("a notifier that didn't get the test (502): Test failed, and why", async () => {
    api.testNotifierEvents.mockResolvedValue(failure(502, { code: "notification_failed" }));
    const wrapper = mountToggle();
    await flushPromises();

    await button(wrapper, "Send test notification").trigger("click");
    await flushPromises();

    const result = wrapper.get(".test-result");
    expect(result.attributes("data-type")).toBe("error");
    expect(result.text()).toBe(
      "Test failed: The notifier didn't get it. Check its URL, and that the service it sends to is running.",
    );
    expect(toast.error).not.toHaveBeenCalled();
    expect(toast.success).not.toHaveBeenCalled();
  });

  test("a test that failed otherwise says why too", async () => {
    const wrapper = mountToggle();
    await flushPromises();
    const send = async () => {
      await button(wrapper, "Send test notification").trigger("click");
      await flushPromises();
      return wrapper.get(".test-result").text();
    };

    api.testNotifierEvents.mockResolvedValueOnce(failure(404, { code: "not_found" }));
    expect(await send()).toBe("Test failed: This notifier no longer exists.");
    api.testNotifierEvents.mockResolvedValueOnce(failure(503, { code: "paused_for_restore" }));
    expect(await send()).toBe("Test failed: Recipe cards are paused while a backup is restored. Try again in a minute.");
    api.testNotifierEvents.mockResolvedValueOnce({ data: null, error: new Error("Network Error") });
    expect(await send()).toBe("Test failed: The server couldn't be reached. Check your connection and try again.");
    api.testNotifierEvents.mockResolvedValueOnce(failure(500, {}));
    expect(await send()).toBe("Test failed: Something went wrong (500).");
    expect(toast.error).not.toHaveBeenCalled();
  });

  test("with BASE_URL left at localhost, warns that the links won't open on a phone, once notifications are on or tested", async () => {
    api.getSettings.mockResolvedValue({ data: settings({ baseUrlSet: false }), error: null });
    api.updateNotifierEvents.mockResolvedValue({ data: { recipeIngestionReady: true }, error: null });
    api.testNotifierEvents.mockResolvedValue({ data: null, error: null });
    const wrapper = mountToggle();
    await flushPromises();
    expect(api.getSettings).toHaveBeenCalledOnce();
    // off and untested: these notifications aren't sent, so nothing to warn about
    expect(wrapper.find(".base-url-unset").exists()).toBe(false);

    await wrapper.get(".switch input").setValue(true);
    await flushPromises();
    expect(wrapper.get(".base-url-unset").text()).toBe(
      "Links in notifications point to localhost, so they won't open on a phone. Set BASE_URL on the server to the address your phone uses.",
    );
    expect(wrapper.get(".base-url-unset").attributes("data-type")).toBe("warning");

    const off = mountToggle("n2");
    await flushPromises();
    expect(off.find(".base-url-unset").exists()).toBe(false);
    await button(off, "Send test notification").trigger("click");
    await flushPromises();
    expect(off.find(".base-url-unset").exists()).toBe(true);
  });

  test("no BASE_URL warning when it's set, or when the server doesn't take cards; settings loaded already aren't asked again", async () => {
    await useRecipeIngestSettings().load();
    api.getNotifierEvents.mockResolvedValue({ data: { recipeIngestionReady: true }, error: null });
    const wrapper = mountToggle();
    await flushPromises();
    expect(api.getSettings).toHaveBeenCalledOnce();
    expect(wrapper.find(".base-url-unset").exists()).toBe(false);

    resetRecipeIngestSettings();
    api.getSettings.mockResolvedValue({ data: settings({ enabled: false, baseUrlSet: false }), error: null });
    const disabled = mountToggle("n2");
    await flushPromises();
    expect(disabled.find(".base-url-unset").exists()).toBe(false);
  });

  test("loads another notifier's switch when it's given one", async () => {
    const wrapper = mountToggle("n1");
    await flushPromises();

    await wrapper.setProps({ notifierId: "n2" });
    await flushPromises();

    expect(api.getNotifierEvents.mock.calls).toEqual([["n1"], ["n2"]]);
  });
});
