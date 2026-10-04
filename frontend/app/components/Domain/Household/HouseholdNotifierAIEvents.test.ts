import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import HouseholdNotifierAIEvents from "./HouseholdNotifierAIEvents.vue";

const api = vi.hoisted(() => ({
  getNotifierEvents: vi.fn(),
  updateNotifierEvents: vi.fn(),
  testNotifierEvents: vi.fn(),
}));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));

vi.mock("~/composables/api", () => ({
  useUserApi: () => ({ recipeIngest: api }),
}));
vi.mock("~/composables/use-toast", () => ({
  alert: toast,
}));

const wrappers: VueWrapper[] = [];

function mountToggle(notifierId = "n1") {
  const wrapper = mount(HouseholdNotifierAIEvents, {
    props: { notifierId },
    global: {
      mocks: { $globals: { icons: { testTube: "test-tube" } } },
      stubs: {
        VAlert: {
          props: ["type"],
          template: "<div class=\"alert\" :data-type=\"type\"><slot /></div>",
        },
        VBtn: {
          props: ["disabled", "loading"],
          template: "<button type=\"button\" :disabled=\"disabled\" :data-loading=\"loading\"><slot /></button>",
        },
        VSwitch: {
          props: ["modelValue", "label", "hint", "disabled"],
          emits: ["update:modelValue"],
          template: `
            <label class="switch">
              <input
                type="checkbox"
                :checked="modelValue"
                :disabled="disabled"
                @change="$emit('update:modelValue', $event.target.checked)"
              >
              <span class="label">{{ label }}</span> <small class="hint">{{ hint }}</small>
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
    api.getNotifierEvents.mockResolvedValue({ data: { recipeIngestionReady: false }, error: null });
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
    expect(wrapper.get(".hint").text())
      .toBe("One notification when a batch of cards has been read, with a link to review them, and one when photos put in the inbox couldn't be added.");
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

  test("sends a test notification", async () => {
    api.testNotifierEvents.mockResolvedValue({ data: null, error: null });
    const wrapper = mountToggle("n3");
    await flushPromises();

    await button(wrapper, "Send test notification").trigger("click");
    await flushPromises();

    expect(api.testNotifierEvents).toHaveBeenCalledExactlyOnceWith("n3");
    expect(toast.success).toHaveBeenCalledExactlyOnceWith("Test notification sent");
  });

  test("says when the test notification couldn't be sent", async () => {
    api.testNotifierEvents.mockResolvedValue({ data: null, error: new Error("404") });
    const wrapper = mountToggle();
    await flushPromises();

    await button(wrapper, "Send test notification").trigger("click");
    await flushPromises();

    expect(toast.error).toHaveBeenCalledExactlyOnceWith("Couldn't send the test notification");
    expect(toast.success).not.toHaveBeenCalled();
  });

  test("a notifier that didn't get the test is said once, by the server's message", async () => {
    // a 502 `notification_failed`: the API client shows its message, so the card adds no second toast
    const failed = Object.assign(new Error("502"), {
      response: { status: 502, data: { detail: { code: "notification_failed", message: "The notifier didn't get it." } } },
    });
    api.testNotifierEvents.mockResolvedValue({ data: null, error: failed });
    const wrapper = mountToggle();
    await flushPromises();

    await button(wrapper, "Send test notification").trigger("click");
    await flushPromises();

    expect(toast.error).not.toHaveBeenCalled();
    expect(toast.success).not.toHaveBeenCalled();
  });

  test("loads another notifier's switch when it's given one", async () => {
    const wrapper = mountToggle("n1");
    await flushPromises();

    await wrapper.setProps({ notifierId: "n2" });
    await flushPromises();

    expect(api.getNotifierEvents.mock.calls).toEqual([["n1"], ["n2"]]);
  });
});
