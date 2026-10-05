// The app's error page (docs/ai/PHASE2.md §3.9): a page opened while a backup is restored says so as Mealie, with no
// status code and the tab titled "Mealie"; any other error is Nuxt's own error page. Fork-owned.
import { mount } from "@vue/test-utils";
import { defineComponent, h } from "vue";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import ErrorPage from "./error.vue";
import type { NuxtError } from "#app";
import { restorePageError } from "~/composables/use-recipe-ingest-restore";
import en from "~/lang/messages/en-US.json";

vi.mock("#app/components/nuxt-error-page.vue", () => ({
  default: defineComponent({
    name: "NuxtErrorPage",
    props: { error: { type: Object, default: null } },
    setup: props => () => h("div", { class: "nuxt-error-page" }, `${props.error?.statusCode} ${props.error?.statusMessage}`),
  }),
}));

const t = (key: string) => key.split(".").reduce<any>((at, part) => at?.[part], en) as string;

/** The server's answer while a backup is restored, as axios rejects with it */
const paused = {
  response: { status: 503, headers: { "retry-after": "60" }, data: { detail: { code: "paused_for_restore" } } },
};

const useHead = vi.fn();

beforeEach(() => {
  vi.useFakeTimers();
  useHead.mockReset();
  vi.stubGlobal("useHead", useHead);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("the app's error page", () => {
  test("a page opened while a backup is restored says so as Mealie: no status code, the tab titled Mealie", () => {
    const error = restorePageError(paused, t, vi.fn()) as NuxtError;
    const wrapper = mount(ErrorPage, { props: { error } });

    expect(wrapper.find(".nuxt-error-page").exists()).toBe(false);
    expect(wrapper.get(".restore-page__title").text()).toBe("A backup is being restored");
    expect(wrapper.get(".restore-page__text").text()).toBe("This page will open when it's done.");
    expect(wrapper.text()).not.toContain("503");
    expect(wrapper.find("svg path").attributes("d")).toBeTruthy();
    expect(useHead).toHaveBeenCalledWith({ title: "Mealie" });
  });

  test("the restore page follows the light or dark choice the user made in this browser", () => {
    const error = restorePageError(paused, t, vi.fn()) as NuxtError;
    localStorage.setItem("vueuse-color-scheme", "dark");
    expect(mount(ErrorPage, { props: { error } }).get(".restore-page").classes()).toContain("restore-page--dark");
    localStorage.setItem("vueuse-color-scheme", "auto");
    expect(mount(ErrorPage, { props: { error } }).get(".restore-page").classes()).toEqual(["restore-page"]);
  });

  test.each([
    ["a page that isn't there", { statusCode: 404, statusMessage: "Page Not Found", message: "Not found" }],
    ["a proxy's 503", { statusCode: 503, statusMessage: "Service Unavailable", message: "Service Unavailable" }],
    ["a 503 that only looks like the restore's", { statusCode: 503, statusMessage: "A backup is being restored", data: { code: "other" } }],
  ])("%s is Nuxt's own error page, as it is", (_name, error) => {
    const wrapper = mount(ErrorPage, { props: { error: error as unknown as NuxtError } });
    expect(wrapper.find(".restore-page").exists()).toBe(false);
    expect(wrapper.get(".nuxt-error-page").text()).toBe(`${error.statusCode} ${error.statusMessage}`);
    expect(useHead).not.toHaveBeenCalled();
  });
});
