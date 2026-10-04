import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { ref } from "vue";
import AppHeader from "./AppHeader.vue";
import { resetRecipeIngestUploads, useRecipeIngestUploads } from "~/composables/use-recipe-ingest-uploads";

/** The logout button's fork hook (docs/ai/PHASE2.md §1.1): photos not uploaded yet aren't dropped without asking */
const api = vi.hoisted(() => ({ upload: vi.fn(), createBatch: vi.fn(), sealBatch: vi.fn(), getCounts: vi.fn() }));
const signOut = vi.hoisted(() => vi.fn());

vi.mock("~/composables/api", () => ({
  useUserApi: () => ({ recipeIngest: api }),
}));
vi.mock("~/composables/use-logged-in-state", () => ({
  useLoggedInState: () => ({ loggedIn: ref(true), isOwnGroup: ref(true) }),
}));

const wrappers: VueWrapper[] = [];
/** A JPEG's first bytes: chosen files are told apart by them */
const JPEG_HEAD = new Uint8Array([0xFF, 0xD8, 0xFF, 0xE0]);

function mountHeader() {
  const wrapper = mount(AppHeader, {
    global: {
      mocks: { $globals: { icons: new Proxy({}, { get: (_target, name) => String(name) }) } },
      stubs: {
        VAppBar: { template: "<header><slot /></header>" },
        RouterLink: { template: "<a><slot /></a>" },
        VToolbarTitle: true,
        VSpacer: true,
        VResponsive: true,
        VTextField: true,
        VIcon: true,
        RecipeDialogSearch: true,
        VBtn: { template: "<button type=\"button\" class=\"btn\" @click=\"$emit('click')\"><slot /></button>", emits: ["click"] },
        VCardText: { template: "<p><slot /></p>" },
        BaseDialog: {
          props: ["modelValue"],
          emits: ["confirm", "update:modelValue"],
          template: `<div v-if="modelValue" class="confirm-dialog"><slot />
            <button type="button" class="dialog-confirm" @click="$emit('confirm'); $emit('update:modelValue', false)" /></div>`,
        },
      },
    },
  });
  wrappers.push(wrapper);
  return wrapper;
}

/** The logout button: the last button in the bar */
function logoutButton(wrapper: VueWrapper) {
  return wrapper.findAll("header > .btn").at(-1)!;
}

describe("logging out from the header", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetRecipeIngestUploads();
    vi.stubGlobal("useMealieAuth", () => ({ user: ref({ groupSlug: "home" }), signOut }));
    vi.stubGlobal("useRoute", () => ({ params: { groupSlug: "home" } }));
    vi.stubGlobal("useDisplay", () => ({ xs: ref(false), smAndUp: ref(true) }));
    api.createBatch.mockResolvedValue({ data: { id: "b1", source: "app" }, error: null });
    api.sealBatch.mockResolvedValue({ data: { id: "b1", source: "app" }, error: null });
    api.getCounts.mockResolvedValue({ data: { ready: 0 }, error: null });
  });

  afterEach(() => {
    wrappers.forEach(wrapper => wrapper.unmount());
    wrappers.length = 0;
    resetRecipeIngestUploads();
    vi.unstubAllGlobals();
  });

  test("with nothing waiting to upload, logs out at once", async () => {
    const wrapper = mountHeader();
    await logoutButton(wrapper).trigger("click");
    await flushPromises();
    expect(wrapper.find(".confirm-dialog").exists()).toBe(false);
    expect(signOut).toHaveBeenCalledExactlyOnceWith("/login?direct=1");
  });

  test("with photos not uploaded, asks first; Log out anyway seals the open batch, then logs out", async () => {
    api.upload.mockImplementation(() => new Promise(() => {})); // still uploading
    const queue = useRecipeIngestUploads();
    queue.takePhoto(new File(["a"], "IMG_1.jpg", { type: "image/jpeg" }));
    await queue.addPhotos([new File([JPEG_HEAD, "b"], "IMG_2.jpg", { type: "image/jpeg" })]);
    await flushPromises();
    const wrapper = mountHeader();

    await logoutButton(wrapper).trigger("click");
    await flushPromises();
    expect(signOut).not.toHaveBeenCalled();
    expect(wrapper.get(".confirm-dialog").text()).toBe("2 photos haven't been uploaded. Log out anyway?");

    await wrapper.get(".dialog-confirm").trigger("click");
    await flushPromises();
    expect(api.sealBatch).toHaveBeenCalledExactlyOnceWith("b1", { suppressAlert: true });
    expect(signOut).toHaveBeenCalledExactlyOnceWith("/login?direct=1");
  });

  test("closing the question keeps the user signed in", async () => {
    await useRecipeIngestUploads().addPhotos([new File([JPEG_HEAD, "b"], "IMG_2.jpg", { type: "image/jpeg" })]);
    const wrapper = mountHeader();
    await logoutButton(wrapper).trigger("click");
    expect(wrapper.get(".confirm-dialog").text()).toBe("1 photo hasn't been uploaded. Log out anyway?");
    expect(signOut).not.toHaveBeenCalled();
  });
});
