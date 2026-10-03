import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { computed, ref } from "vue";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import ConsentPage from "./consent.vue";
import type { McpOAuthRequestOut } from "~/lib/api/types/mcp";

const api = vi.hoisted(() => ({ getRequest: vi.fn(), decideRequest: vi.fn() }));
// jsdom's window.top can't be redefined; the helper itself is tested with fake windows
const frame = vi.hoisted(() => ({ framed: false }));

vi.mock("~/composables/api", () => ({
  useUserApi: () => ({ mcp: api }),
}));
vi.mock("~/composables/use-mcp", async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  isFramed: () => frame.framed,
}));

const HANDLE = "Xq3_kZ-0aB1cD2eF3gH4iJ5kL6mN7oP8qR9sT0uV1wX";
const REDIRECT_TO = "https://my.home-assistant.io/redirect/oauth?code=SECRET_CODE&state=s&iss=http%3A%2F%2Fmealie";

const navigateTo = vi.fn();
const signOut = vi.fn();
const status = ref<"loading" | "authenticated" | "unauthenticated">("authenticated");
const route = { fullPath: `/oauth/consent?request=${HANDLE}`, query: { request: HANDLE } as Record<string, unknown> };

const wrappers: VueWrapper[] = [];

function consentRequest(overrides: Partial<McpOAuthRequestOut> = {}): McpOAuthRequestOut {
  return {
    clientName: "Home Assistant",
    scopes: ["mcp:read", "mcp:write"],
    writesOffered: true,
    redirectHost: "my.home-assistant.io",
    expiresAt: "2026-10-03T12:10:00Z",
    ...overrides,
  };
}

const slot = (tag = "div", className = "") => ({ template: `<${tag} class="${className}"><slot /></${tag}>` });

async function mountPage() {
  const wrapper = mount(ConsentPage, {
    global: {
      mocks: { $globals: { icons: {} } },
      stubs: {
        VContainer: slot(),
        VCard: slot("section"),
        VToolbar: slot(),
        VToolbarTitle: slot(),
        VCardTitle: slot("h1"),
        VCardText: slot(),
        VCardActions: slot(),
        VSpacer: slot(),
        VAlert: slot("div", "alert"),
        AppLoader: { template: "<div class=\"loader\" />" },
        VBtn: {
          props: ["disabled", "loading", "to", "href"],
          template: `
            <a v-if="href" :href="href"><slot /></a>
            <button v-else type="button" :disabled="disabled" :data-to="to"><slot /></button>
          `,
        },
        VCheckbox: {
          props: ["modelValue", "label", "disabled"],
          emits: ["update:modelValue"],
          template: `
            <label class="checkbox">
              <input type="checkbox" :checked="modelValue" :disabled="disabled" @change="$emit('update:modelValue', $event.target.checked)">
              {{ label }}
            </label>
          `,
        },
      },
    },
  });
  wrappers.push(wrapper);
  await flushPromises();
  return wrapper;
}

function button(wrapper: VueWrapper, text: string) {
  const found = wrapper.findAll("button").find(b => b.text() === text);
  if (!found) {
    throw new Error(`No ${text} button`);
  }
  return found;
}

describe("OAuth consent page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    frame.framed = false;
    status.value = "authenticated";
    route.query = { request: HANDLE };
    vi.stubGlobal("definePageMeta", vi.fn());
    vi.stubGlobal("useSeoMeta", vi.fn());
    vi.stubGlobal("useRoute", () => route);
    vi.stubGlobal("navigateTo", navigateTo);
    vi.stubGlobal("useAuthBackend", () => ({
      status: computed(() => status.value),
      data: computed(() => ({ fullName: "Jane Doe", email: "jane@example.com" })),
      signOut,
    }));
    api.getRequest.mockResolvedValue({ data: consentRequest(), error: null });
    api.decideRequest.mockResolvedValue({ data: { redirectTo: REDIRECT_TO }, error: null });
  });

  afterEach(() => {
    wrappers.forEach(wrapper => wrapper.unmount());
    wrappers.length = 0;
    vi.unstubAllGlobals();
  });

  test("sends a signed-out user to log in, and back here afterwards", async () => {
    status.value = "unauthenticated";
    await mountPage();

    expect(navigateTo).toHaveBeenCalledExactlyOnceWith(
      `/login?redirect=${encodeURIComponent(`/oauth/consent?request=${HANDLE}`)}`,
      { replace: true },
    );
    expect(api.getRequest).not.toHaveBeenCalled();
  });

  test("waits for the session before loading the request", async () => {
    status.value = "loading";
    const wrapper = await mountPage();
    expect(api.getRequest).not.toHaveBeenCalled();
    expect(wrapper.find(".loader").exists()).toBe(true);

    status.value = "authenticated";
    await flushPromises();
    expect(api.getRequest).toHaveBeenCalledExactlyOnceWith(HANDLE);
  });

  test("shows who is asking, for what, the account and where the browser goes next", async () => {
    const wrapper = await mountPage();

    expect(wrapper.get("h1").text()).toBe("Home Assistant wants to use your Mealie");
    expect(wrapper.get("h1 strong").text()).toBe("Home Assistant");
    expect(wrapper.text()).toContain("Read your recipes, meal plans and shopping lists");
    expect(wrapper.text()).toContain("Signed in as Jane Doe (jane@example.com)");
    expect(wrapper.text()).toContain("You'll be sent back to my.home-assistant.io");
  });

  test("changes are offered unchecked, so approving grants reading only", async () => {
    const wrapper = await mountPage();

    const checkbox = wrapper.get(".checkbox input");
    expect(wrapper.get(".checkbox").text()).toBe("Allow changes (add to shopping list, plan meals)");
    expect((checkbox.element as HTMLInputElement).checked).toBe(false);

    await button(wrapper, "Approve").trigger("click");
    await flushPromises();

    expect(api.decideRequest).toHaveBeenCalledExactlyOnceWith(HANDLE, { approve: true, allowWrites: false });
    expect(navigateTo).toHaveBeenCalledExactlyOnceWith(REDIRECT_TO, { external: true, replace: true });
  });

  test("ticking Allow changes grants them", async () => {
    const wrapper = await mountPage();

    await wrapper.get(".checkbox input").setValue(true);
    await button(wrapper, "Approve").trigger("click");
    await flushPromises();

    expect(api.decideRequest).toHaveBeenCalledExactlyOnceWith(HANDLE, { approve: true, allowWrites: true });
  });

  test("doesn't offer changes the client can't have", async () => {
    api.getRequest.mockResolvedValue({ data: consentRequest({ scopes: ["mcp:read"], writesOffered: false }), error: null });
    const wrapper = await mountPage();

    expect(wrapper.find(".checkbox").exists()).toBe(false);
    await button(wrapper, "Approve").trigger("click");
    await flushPromises();

    expect(api.decideRequest).toHaveBeenCalledExactlyOnceWith(HANDLE, { approve: true, allowWrites: false });
  });

  test("denying sends the browser back to the app too", async () => {
    const wrapper = await mountPage();

    await wrapper.get(".checkbox input").setValue(true);
    await button(wrapper, "Deny").trigger("click");
    await flushPromises();

    expect(api.decideRequest).toHaveBeenCalledExactlyOnceWith(HANDLE, { approve: false, allowWrites: false });
    expect(navigateTo).toHaveBeenCalledExactlyOnceWith(REDIRECT_TO, { external: true, replace: true });
  });

  test("never shows the code it sends the browser back with", async () => {
    const wrapper = await mountPage();

    await button(wrapper, "Approve").trigger("click");
    await flushPromises();

    expect(wrapper.text()).toContain("Taking you back to my.home-assistant.io…");
    expect(wrapper.html()).not.toContain("SECRET_CODE");
    expect(button(wrapper, "Approve").attributes("disabled")).toBeDefined();
    expect(button(wrapper, "Deny").attributes("disabled")).toBeDefined();
  });

  test("an expired, answered or other account's request explains what to do", async () => {
    // The API answers 404 for all of these, including a request from another group's client
    api.getRequest.mockResolvedValue({ data: null, error: { response: { status: 404 } } });
    const wrapper = await mountPage();

    expect(wrapper.get(".alert").text()).toBe("This request has expired, was already answered, or isn't for this "
      + "Mealie account. Start connecting again from the app, or switch to the right account.");
    expect(wrapper.text()).toContain("Signed in as Jane Doe (jane@example.com)");
    expect(button(wrapper, "Go to Mealie").attributes("data-to")).toBe("/");
    expect(wrapper.findAll("button").some(b => b.text() === "Approve")).toBe(false);

    await button(wrapper, "Not you? Switch account").trigger("click");
    expect(signOut).toHaveBeenCalledExactlyOnceWith(
      `/login?direct=1&redirect=${encodeURIComponent(`/oauth/consent?request=${HANDLE}`)}`,
    );
  });

  test("inside a frame, nothing can be approved: it opens in its own tab instead", async () => {
    frame.framed = true;
    const wrapper = await mountPage();

    expect(wrapper.text()).toContain("For your safety, apps can't be approved on a page shown inside another website.");
    expect(wrapper.findAll("button").filter(b => ["Approve", "Deny"].includes(b.text()))).toHaveLength(0);
    expect(wrapper.find(".checkbox").exists()).toBe(false);
    // Not even who's asking: nothing is loaded
    expect(api.getRequest).not.toHaveBeenCalled();
    expect(wrapper.text()).not.toContain("Home Assistant");

    const open = wrapper.get("a");
    expect(open.text()).toBe("Open in a New Tab");
    expect(open.attributes()).toMatchObject({
      href: `/oauth/consent?request=${HANDLE}`,
      target: "_blank",
      rel: "noopener",
    });
  });

  test("a signed-out user inside a frame isn't sent to log in there", async () => {
    frame.framed = true;
    status.value = "unauthenticated";
    const wrapper = await mountPage();

    expect(navigateTo).not.toHaveBeenCalled();
    expect(wrapper.get("a").text()).toBe("Open in a New Tab");
  });

  test("a link without a request is incomplete", async () => {
    route.query = {};
    const wrapper = await mountPage();

    expect(api.getRequest).not.toHaveBeenCalled();
    expect(wrapper.get(".alert").text()).toBe("This link is incomplete. Start connecting again from the app.");
  });

  test("a failed load can be retried", async () => {
    api.getRequest.mockResolvedValueOnce({ data: null, error: { response: { status: 500 } } });
    const wrapper = await mountPage();

    expect(wrapper.get(".alert").text()).toBe("Couldn't load this request. Check your connection and try again.");
    await button(wrapper, "Try Again").trigger("click");
    await flushPromises();

    expect(api.getRequest).toHaveBeenCalledTimes(2);
    expect(wrapper.get("h1").text()).toBe("Home Assistant wants to use your Mealie");
  });

  test("a decision that couldn't be saved can be tried again", async () => {
    api.decideRequest.mockResolvedValueOnce({ data: null, error: { response: { status: 500 } } });
    const wrapper = await mountPage();

    await button(wrapper, "Approve").trigger("click");
    await flushPromises();

    expect(navigateTo).not.toHaveBeenCalled();
    expect(wrapper.get(".alert").text()).toBe("Couldn't save your answer. Try again.");
    expect(button(wrapper, "Approve").attributes("disabled")).toBeUndefined();
  });

  test("switching account signs out and comes back here after logging in", async () => {
    const wrapper = await mountPage();

    await button(wrapper, "Not you? Switch account").trigger("click");

    expect(signOut).toHaveBeenCalledExactlyOnceWith(
      `/login?direct=1&redirect=${encodeURIComponent(`/oauth/consent?request=${HANDLE}`)}`,
    );
    status.value = "unauthenticated";
    await flushPromises();
    expect(navigateTo).not.toHaveBeenCalled();
  });
});
