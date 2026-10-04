import { mount } from "@vue/test-utils";
import { ref } from "vue";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import GroupPage from "./index.vue";

const mocks = vi.hoisted(() => ({
  updateAIProviderSettings: vi.fn(),
  refresh: vi.fn(),
  loadRoutes: vi.fn(),
  saveRoutes: vi.fn(),
  createOne: vi.fn(),
  updateOne: vi.fn(),
  deleteOne: vi.fn(),
  loadUsage: vi.fn(),
  loadKeyStatus: vi.fn(),
  error: vi.fn(),
  success: vi.fn(),
}));

const settings = {
  defaultProviderId: "a",
  audioProviderId: null,
  imageProviderId: null,
  providers: [{ id: "a", name: "A" }],
  aiEnabled: true,
  audioProviderEnabled: false,
  imageProviderEnabled: false,
  ocrFallbackEnabled: false,
};

vi.mock("~/composables/use-groups", () => ({
  useGroupSelf: () => ({
    group: ref({ aiProviderSettings: { ...settings }, preferences: {} }),
    actions: { updateAIProviderSettings: mocks.updateAIProviderSettings, refresh: mocks.refresh },
  }),
}));
vi.mock("~/composables/use-ai-providers", () => ({
  useAIProviders: () => ({ createOne: mocks.createOne, updateOne: mocks.updateOne, deleteOne: mocks.deleteOne }),
}));
vi.mock("~/composables/use-ai-provider-routing", () => ({
  useAIProviderKeyStatus: () => ({ unreadableIds: ref([]), load: mocks.loadKeyStatus }),
  useAIProviderRoutes: () => ({
    routes: ref(null),
    loadFailed: ref(false),
    load: mocks.loadRoutes,
    save: mocks.saveRoutes,
  }),
}));
vi.mock("~/composables/use-toast", () => ({
  alert: { error: mocks.error, success: mocks.success },
}));
vi.mock("~/components/Domain/Group/GroupPreferencesEditor.vue", () => ({ default: { render: () => null } }));
vi.mock("~/components/Domain/Group/GroupAIProviderSettingsEditor.vue", () => ({ default: { render: () => null } }));
vi.mock("~/components/Domain/Group/GroupAIProviderUsage.vue", () => ({ default: { render: () => null } }));
vi.mock("~/components/Domain/Group/GroupMcpSettings.vue", () => ({ default: { render: () => null } }));
vi.mock("~/components/Domain/Group/GroupRecipeCardSettings.vue", () => ({ default: { render: () => null } }));

type PageVM = {
  refGroupAISettingsForm: { validate: () => boolean } | null;
  refAIProviderUsage: { load: () => void } | null;
  handleAISettingsSubmit: () => Promise<void>;
  handleCreateProvider: (data: unknown) => Promise<void>;
  handleUpdateProvider: (id: string, data: unknown) => Promise<void>;
  handleDeleteProvider: (id: string) => Promise<void>;
};

describe("group settings page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal("definePageMeta", vi.fn());
    vi.stubGlobal("useSeoMeta", vi.fn());
  });

  afterEach(() => vi.unstubAllGlobals());

  // Exercises the page's handlers without rendering its forms
  async function withPage(run: (vm: PageVM) => Promise<void>) {
    const wrapper = mount({ ...GroupPage, render: () => null });
    const vm = wrapper.vm as unknown as PageVM;
    vm.refGroupAISettingsForm = { validate: () => true };
    vm.refAIProviderUsage = { load: mocks.loadUsage };
    try {
      await run(vm);
    }
    finally {
      wrapper.unmount();
    }
  }

  test("saves the fallback routes against the saved settings", async () => {
    const saved = { ...settings, defaultProviderId: "b" };
    mocks.updateAIProviderSettings.mockResolvedValue(saved);
    mocks.saveRoutes.mockResolvedValue(true);

    await withPage(vm => vm.handleAISettingsSubmit());

    expect(mocks.saveRoutes).toHaveBeenCalledExactlyOnceWith(saved);
    expect(mocks.success).toHaveBeenCalledOnce();
    expect(mocks.error).not.toHaveBeenCalled();
  });

  test("doesn't save the fallback routes when the settings weren't saved", async () => {
    mocks.updateAIProviderSettings.mockResolvedValue(undefined);

    await withPage(vm => vm.handleAISettingsSubmit());

    expect(mocks.saveRoutes).not.toHaveBeenCalled();
    expect(mocks.error).toHaveBeenCalledExactlyOnceWith("Settings update failed");
    expect(mocks.success).not.toHaveBeenCalled();
  });

  test("reports fallback routes that weren't saved", async () => {
    mocks.updateAIProviderSettings.mockResolvedValue(settings);
    mocks.saveRoutes.mockResolvedValue(false);

    await withPage(vm => vm.handleAISettingsSubmit());

    expect(mocks.error).toHaveBeenCalledExactlyOnceWith("Failed to update fallback providers");
    expect(mocks.success).not.toHaveBeenCalled();
  });

  test("loads which API keys can't be read", async () => {
    await withPage(async () => {});

    expect(mocks.loadKeyStatus).toHaveBeenCalledOnce();
  });

  test.each([
    ["created", (vm: PageVM) => vm.handleCreateProvider({}), mocks.createOne],
    ["updated", (vm: PageVM) => vm.handleUpdateProvider("a", {}), mocks.updateOne],
    ["deleted", (vm: PageVM) => vm.handleDeleteProvider("a"), mocks.deleteOne],
  ])("reloads the usage and key status when a provider is %s", async (_, run, request) => {
    request.mockResolvedValue({ data: {} });

    await withPage(run);

    expect(mocks.refresh).toHaveBeenCalled();
    expect(mocks.loadUsage).toHaveBeenCalledOnce();
    // Once on mount, once after the change
    expect(mocks.loadKeyStatus).toHaveBeenCalledTimes(2);
  });

  test("doesn't reload them when a provider change fails", async () => {
    mocks.createOne.mockResolvedValue({ data: null });

    await withPage(vm => vm.handleCreateProvider({}));

    expect(mocks.loadUsage).not.toHaveBeenCalled();
    expect(mocks.loadKeyStatus).toHaveBeenCalledOnce();
  });
});
