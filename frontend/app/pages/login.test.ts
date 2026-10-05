// Fork: signing in while a backup is restored says so (docs/ai/PHASE2.md §3.9). The server refuses the sign-in with
// 503 paused_for_restore and "A backup is being restored. Try again in a minute.", which the axios interceptor toasts;
// the login page doesn't cover it with "Something went wrong". Fork-owned test of upstream's page.
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, type MockInstance, test, vi } from "vitest";
import { ref } from "vue";
import LoginPage from "./login.vue";
import { alert } from "~/composables/use-toast";

const signIn = vi.hoisted(() => vi.fn());
const RESTORING = "A backup is being restored. Try again in a minute.";

function refused(status: number, detail: Record<string, unknown> = {}, headers: Record<string, string> = {}) {
  return { message: `Request failed with status code ${status}`, response: { status, headers, data: { detail } } };
}

const wrappers: VueWrapper[] = [];
let toastError: MockInstance;

async function signInWith(error: unknown) {
  signIn.mockRejectedValueOnce(error);
  const wrapper = mount(LoginPage, {
    global: {
      mocks: {
        $globals: { icons: {} },
        $appInfo: { allowPasswordLogin: true, enableOidc: false, allowSignup: false },
        $vuetify: { theme: { current: { dark: false } } },
      },
      stubs: {
        VTextField: {
          props: ["modelValue", "autocomplete"],
          emits: ["update:modelValue"],
          template: `<input :class="autocomplete" :value="modelValue" @input="$emit('update:modelValue', $event.target.value)" />`,
        },
        VForm: { emits: ["submit"], template: `<form @submit="$emit('submit', $event)"><slot /></form>` },
        ...Object.fromEntries(["VContainer", "VAlert", "VCard", "VCardTitle", "VCardText", "VCardActions", "VToolbar", "VToolbarTitle"]
          .map(name => [name, { template: "<div><slot /></div>" }])),
        VBtn: { template: "<button type=\"submit\"><slot /></button>" },
        VCheckbox: true,
        VDivider: true,
        VIcon: true,
        AppButtonCopy: true,
        AppLogo: true,
        NuxtLink: true,
      },
    },
  });
  wrappers.push(wrapper);
  await flushPromises();
  await wrapper.get("input.username").setValue("cook@example.com");
  await wrapper.get("input.current-password").setValue("a password");
  await wrapper.get("form").trigger("submit");
  await flushPromises();
  return wrapper;
}

beforeEach(() => {
  vi.stubGlobal("definePageMeta", vi.fn());
  vi.stubGlobal("useSeoMeta", vi.fn());
  vi.stubGlobal("useRoute", () => ({ query: {}, params: {} }));
  vi.stubGlobal("useRouter", () => ({ push: vi.fn() }));
  vi.stubGlobal("useMealieAuth", () => ({ user: ref(null), loggedIn: ref(false), signIn, oauthSignIn: vi.fn() }));
  vi.stubGlobal("useNuxtApp", () => ({
    $globals: { icons: {} },
    $appInfo: { allowPasswordLogin: true, enableOidc: false },
    $axios: { get: vi.fn().mockResolvedValue({ data: { isDemo: false, isFirstLogin: false } }) },
  }));
  vi.stubGlobal("useAsyncData", (_key: string, load: () => Promise<unknown>) => {
    void load();
    return {};
  });
  vi.stubGlobal("useDefaultActivity", () => ({ getDefaultActivityRoute: () => null }));
  vi.spyOn(console, "log").mockImplementation(() => {});
  toastError = vi.spyOn(alert, "error").mockImplementation(() => {});
});

afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount());
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("signing in while a backup is restored", () => {
  test("leaves the server's message, which the axios interceptor toasted, in place", async () => {
    await signInWith(refused(503, { code: "paused_for_restore", message: RESTORING }, { "retry-after": "60" }));
    expect(signIn).toHaveBeenCalledOnce();
    expect(toastError).not.toHaveBeenCalled();
  });

  test("says so itself when the answer carries no message", async () => {
    await signInWith(refused(503, { code: "paused_for_restore" }, { "retry-after": "60" }));
    expect(toastError).toHaveBeenCalledExactlyOnceWith(RESTORING);
  });

  test("other failures are told as before", async () => {
    await signInWith(refused(401));
    await signInWith(refused(503, {}));
    expect(toastError.mock.calls).toEqual([["Invalid Credentials"], ["Something Went Wrong!"]]);
  });
});
