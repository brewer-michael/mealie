import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import UserMcpApiTokenWriteSwitch from "./UserMcpApiTokenWriteSwitch.vue";

const api = vi.hoisted(() => ({ getApiTokenGrant: vi.fn(), updateApiTokenGrant: vi.fn() }));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));

vi.mock("~/composables/api", () => ({
  useUserApi: () => ({ mcp: api }),
}));
vi.mock("~/composables/use-toast", () => ({
  alert: toast,
}));

const VSwitch = {
  name: "VSwitch",
  props: ["modelValue", "label", "disabled"],
  emits: ["update:modelValue"],
  template: `
    <label>
      <input type="checkbox" role="switch" :checked="modelValue" :disabled="disabled" @change="$emit('update:modelValue', $event.target.checked)">
      {{ label }}
    </label>
  `,
};

const wrappers: VueWrapper[] = [];

async function mountSwitch(tokenId = 7) {
  const wrapper = mount(UserMcpApiTokenWriteSwitch, {
    props: { tokenId },
    global: { stubs: { VSwitch } },
  });
  wrappers.push(wrapper);
  await flushPromises();
  return wrapper;
}

function switchState(wrapper: VueWrapper) {
  return wrapper.getComponent(VSwitch).props() as { modelValue: boolean; disabled: boolean };
}

describe("UserMcpApiTokenWriteSwitch", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getApiTokenGrant.mockImplementation(async (tokenId: number) => ({ data: { tokenId, allowWrites: false } }));
  });

  afterEach(() => {
    wrappers.forEach(wrapper => wrapper.unmount());
    wrappers.length = 0;
  });

  test("shows whether AI assistants may make changes with the token", async () => {
    api.getApiTokenGrant.mockResolvedValue({ data: { tokenId: 7, allowWrites: true } });
    const wrapper = await mountSwitch();

    expect(api.getApiTokenGrant).toHaveBeenCalledExactlyOnceWith(7);
    expect(wrapper.text()).toBe("Allow AI assistants to make changes");
    expect(switchState(wrapper)).toMatchObject({ modelValue: true, disabled: false });
  });

  test("turning it on saves the grant", async () => {
    api.updateApiTokenGrant.mockResolvedValue({ data: { tokenId: 7, allowWrites: true } });
    const wrapper = await mountSwitch();

    await wrapper.get("input").setValue(true);
    await flushPromises();

    expect(api.updateApiTokenGrant).toHaveBeenCalledExactlyOnceWith(7, { allowWrites: true });
    expect(switchState(wrapper).modelValue).toBe(true);
  });

  test("a failed save keeps the saved value and says so", async () => {
    api.updateApiTokenGrant.mockResolvedValue({ data: null });
    const wrapper = await mountSwitch();

    await wrapper.get("input").setValue(true);
    await flushPromises();

    expect(switchState(wrapper).modelValue).toBe(false);
    expect(toast.error).toHaveBeenCalledWith("Failed to update the token");
  });

  test("can't be changed until the grant has loaded", async () => {
    api.getApiTokenGrant.mockResolvedValue({ data: null });
    const wrapper = await mountSwitch();

    expect(switchState(wrapper).disabled).toBe(true);
    expect(wrapper.text()).toContain("Couldn't load whether AI assistants may make changes with this token");
  });

  test("follows the row's token", async () => {
    const wrapper = await mountSwitch(7);

    await wrapper.setProps({ tokenId: 8 });
    await flushPromises();

    expect(api.getApiTokenGrant).toHaveBeenLastCalledWith(8);
  });
});
