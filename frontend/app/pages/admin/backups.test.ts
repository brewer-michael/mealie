// Fork: a restore refused as busy (503) shows only the server's message, which the axios interceptor toasts, and
// the dialog stays open for a retry (docs/ai/PHASE2.md §3.9)
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, type MockInstance, test, vi } from "vitest";
import BackupsPage from "./backups.vue";
import { alert } from "~/composables/use-toast";

const api = vi.hoisted(() => ({ getAll: vi.fn(), restore: vi.fn(), create: vi.fn(), delete: vi.fn() }));

vi.mock("~/composables/api", () => ({
  useAdminApi: () => ({ backups: api }),
}));

const BUSY = "Mealie is still saving changes. Try the restore again in a minute.";

function failure(status: number, message?: string) {
  return {
    data: null,
    response: null,
    error: { response: { status, data: message ? { detail: { message, error: true } } : "Internal Server Error" } },
  };
}

const wrappers: VueWrapper[] = [];
const toast = {} as { error: MockInstance; success: MockInstance };

async function openRestoreDialog() {
  const wrapper = mount(BackupsPage, {
    global: {
      mocks: { $globals: { icons: {} } },
      stubs: {
        BaseDialog: {
          props: ["modelValue", "title"],
          template: `<div v-if="modelValue" class="dialog" :data-title="title"><slot /><slot name="custom-card-action" /></div>`,
        },
        BaseButton: {
          props: ["disabled"],
          emits: ["click"],
          template: `<button class="base-button" :disabled="disabled" @click="$emit('click', $event)"><slot /></button>`,
        },
        VCheckbox: {
          props: ["modelValue"],
          emits: ["update:modelValue"],
          template: `<input type="checkbox" class="confirm" :checked="modelValue" @change="$emit('update:modelValue', $event.target.checked)" />`,
        },
        VDataTable: {
          props: ["items"],
          template: `<div><div v-for="item in items" :key="item.name" class="row"><slot name="item.actions" :item="item" /></div></div>`,
        },
        AppButtonUpload: true,
        BaseCardSectionTitle: true,
        NuxtLink: true,
      },
    },
  });
  wrappers.push(wrapper);
  await flushPromises();

  const restoreRow = wrapper.findAll(".row .base-button").find(button => button.text() === "Backup Restore");
  expect(restoreRow).toBeDefined();
  await restoreRow!.trigger("click");
  await wrapper.find(".dialog .confirm").setValue(true);
  return wrapper;
}

function restoreButton(wrapper: VueWrapper) {
  const button = wrapper.findAll(".dialog .base-button").find(b => b.text() === "Restore Backup");
  expect(button).toBeDefined();
  return button!;
}

describe("admin backups page: restore", () => {
  beforeEach(() => {
    vi.stubGlobal("definePageMeta", vi.fn());
    vi.stubGlobal("useSeoMeta", vi.fn());
    vi.stubGlobal("useHead", vi.fn());
    vi.spyOn(console, "log").mockImplementation(() => {});
    toast.error = vi.spyOn(alert, "error").mockImplementation(() => {});
    toast.success = vi.spyOn(alert, "success").mockImplementation(() => {});
    api.getAll.mockResolvedValue({
      data: { imports: [{ name: "mealie_2026.10.04.zip", date: "2026-10-04T10:00:00", size: "1 MB" }], templates: [] },
    });
  });

  afterEach(() => {
    wrappers.splice(0).forEach(wrapper => wrapper.unmount());
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    vi.clearAllMocks();
  });

  test("a busy restore adds no toast of its own and keeps the dialog open for a retry", async () => {
    const wrapper = await openRestoreDialog();
    api.restore.mockResolvedValueOnce(failure(503, BUSY));

    await restoreButton(wrapper).trigger("click");
    await flushPromises();

    expect(api.restore).toHaveBeenCalledWith("mealie_2026.10.04.zip");
    expect(toast.error).not.toHaveBeenCalled();
    expect(toast.success).not.toHaveBeenCalled();
    expect(wrapper.find(".dialog").exists()).toBe(true);
    expect(restoreButton(wrapper).attributes("disabled")).toBeUndefined();
  });

  test("another failure the server explained closes the dialog without a second toast", async () => {
    const wrapper = await openRestoreDialog();
    api.restore.mockResolvedValueOnce(failure(400, "database backup schema version does not match current database"));

    await restoreButton(wrapper).trigger("click");
    await flushPromises();

    expect(toast.error).not.toHaveBeenCalled();
    expect(wrapper.find(".dialog").exists()).toBe(false);
  });

  test("a failure without a message from the server shows the generic one", async () => {
    const wrapper = await openRestoreDialog();
    api.restore.mockResolvedValueOnce(failure(500));

    await restoreButton(wrapper).trigger("click");
    await flushPromises();

    expect(toast.error).toHaveBeenCalledTimes(1);
    expect(toast.error).toHaveBeenCalledWith("Restore failed. Check your server logs for more details");
    expect(wrapper.find(".dialog").exists()).toBe(false);
  });
});
