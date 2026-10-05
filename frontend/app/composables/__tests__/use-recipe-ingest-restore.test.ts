// A page opened or reloaded while a backup is restored (docs/ai/PHASE2.md §3.9): the app's first request is answered
// 503 paused_for_restore, so the page says so, asks again every few seconds (whatever Retry-After says) and opens once
// the restore is over. Fork-owned.
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import {
  RESTORE_CHECK_DEFAULT_MS,
  RESTORE_CHECK_MAX_MS,
  restorePageData,
  restorePageError,
  restoreRetryAfterMs,
  waitForRestoreEnd,
} from "../use-recipe-ingest-restore";
import en from "~/lang/messages/en-US.json";

const axiosGet = vi.hoisted(() => vi.fn());
vi.mock("axios", () => ({ default: { get: axiosGet } }));

/** The server's answer while a backup is restored, as axios rejects with it */
function paused(retryAfter: string | null = "60") {
  return {
    message: "Request failed with status code 503",
    response: {
      status: 503,
      headers: retryAfter === null ? {} : { "retry-after": retryAfter },
      data: { detail: { code: "paused_for_restore" } },
    },
  };
}

const offline = { message: "Network Error" };
const restoreText = en["recipe-ingest"].restore;
const t = (key: string) => key.split(".").reduce<any>((at, part) => at?.[part], en) as string;

beforeEach(() => {
  vi.useFakeTimers();
  axiosGet.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("the server's answer while a backup is restored", () => {
  test("is told from other errors, with how long to wait before asking again", () => {
    // the server's Retry-After is a minute, but a restore takes seconds: a few seconds at most
    expect(RESTORE_CHECK_MAX_MS).toBe(5000);
    expect(restoreRetryAfterMs(paused("60"))).toBe(RESTORE_CHECK_MAX_MS);
    expect(restoreRetryAfterMs(paused("2"))).toBe(2000);
    // no Retry-After, or one that makes no sense: a few seconds; a long one is cut short
    expect(restoreRetryAfterMs(paused(null))).toBe(RESTORE_CHECK_DEFAULT_MS);
    expect(restoreRetryAfterMs(paused("soon"))).toBe(RESTORE_CHECK_DEFAULT_MS);
    expect(restoreRetryAfterMs(paused("3600"))).toBe(RESTORE_CHECK_MAX_MS);
    // a proxy's 503, a server error, no answer at all: not a restore
    expect(restoreRetryAfterMs({ response: { status: 503, headers: {}, data: "Service Unavailable" } })).toBeNull();
    expect(restoreRetryAfterMs({ response: { status: 500, data: { detail: { code: "paused_for_restore" } } } })).toBeNull();
    expect(restoreRetryAfterMs(offline)).toBeNull();
    expect(restoreRetryAfterMs(null)).toBeNull();
  });
});

describe("waiting for the restore to end", () => {
  test("asks again after each Retry-After (and while the server can't be reached), and opens once it's answered", async () => {
    const ended = vi.fn();
    const check = vi.fn()
      .mockRejectedValueOnce(paused("2"))
      .mockRejectedValueOnce(offline)
      .mockResolvedValueOnce({ data: {} });
    waitForRestoreEnd(60_000, ended, check);

    await vi.advanceTimersByTimeAsync(59_999);
    expect(check).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(check).toHaveBeenCalledTimes(1);
    // still restoring: again after its Retry-After
    await vi.advanceTimersByTimeAsync(1999);
    expect(check).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(check).toHaveBeenCalledTimes(2);
    // the server restarting: again a moment later
    await vi.advanceTimersByTimeAsync(RESTORE_CHECK_DEFAULT_MS);
    expect(check).toHaveBeenCalledTimes(3);
    expect(ended).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(10 * RESTORE_CHECK_MAX_MS);
    expect(check).toHaveBeenCalledTimes(3);
  });

  test("a refusal for another reason ends the wait too: the app shows it", async () => {
    const ended = vi.fn();
    waitForRestoreEnd(1000, ended, vi.fn().mockRejectedValue({ response: { status: 500, data: {} } }));
    await vi.advanceTimersByTimeAsync(1000);
    expect(ended).toHaveBeenCalledOnce();
  });

  test("stops when told to", async () => {
    const ended = vi.fn();
    const check = vi.fn().mockResolvedValue({});
    const stop = waitForRestoreEnd(1000, ended, check);
    stop();
    await vi.advanceTimersByTimeAsync(5000);
    expect([check.mock.calls.length, ended.mock.calls.length]).toEqual([0, 0]);
  });
});

describe("the app's first request (plugins/app-info.client.ts) while a backup is restored", () => {
  async function appInfoPlugin() {
    vi.resetModules();
    vi.stubGlobal("defineNuxtPlugin", (plugin: unknown) => plugin);
    const plugin = (await import("~/plugins/app-info.client")).default as unknown as {
      setup: (nuxtApp: unknown) => Promise<{ provide: { appInfo: unknown } }>;
    };
    return plugin.setup({ $i18n: { t } });
  }

  test("fails with what the error page shows, then loads the page again within seconds of the restore's end", async () => {
    const reload = vi.fn();
    vi.stubGlobal("location", { ...window.location, reload });
    axiosGet.mockRejectedValueOnce(paused("60"));

    const error = await appInfoPlugin().catch((e: unknown) => e) as Record<string, unknown>;
    // the app's error page (app/error.vue) shows `data`'s title and text, as Mealie, with no status
    expect(error).toMatchObject({
      statusCode: 503,
      statusMessage: restoreText["page-title"],
      fatal: true,
      data: { code: "paused_for_restore", title: restoreText["page-title"], text: restoreText["page-text"] },
    });
    expect(error.message).toBe(restoreText["page-text"]);
    expect(restorePageData(error)).toEqual(error.data);
    expect(`${restoreText["page-title"]}. ${restoreText["page-text"]}`).toBe(
      "A backup is being restored. This page will open when it's done.",
    );

    // the server says to wait a minute (Retry-After: 60), but it's asked again every 5 s
    axiosGet.mockRejectedValueOnce(paused("60")).mockResolvedValueOnce({ data: { version: "v3" } });
    await vi.advanceTimersByTimeAsync(RESTORE_CHECK_MAX_MS - 1);
    expect(axiosGet).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(axiosGet).toHaveBeenCalledTimes(2);
    expect(reload).not.toHaveBeenCalled();
    // the restore ended meanwhile: the next check, 5 s later, opens the page
    await vi.advanceTimersByTimeAsync(RESTORE_CHECK_MAX_MS);
    expect(axiosGet).toHaveBeenLastCalledWith("/api/app/about");
    expect(reload).toHaveBeenCalledOnce();
  });

  test("any other failure is the app's as before, and nothing waits", async () => {
    const reload = vi.fn();
    vi.stubGlobal("location", { ...window.location, reload });
    const proxyError = { response: { status: 503, headers: {}, data: "Service Unavailable" } };
    axiosGet.mockRejectedValueOnce(proxyError);
    await expect(appInfoPlugin()).rejects.toBe(proxyError);
    await vi.advanceTimersByTimeAsync(10 * RESTORE_CHECK_MAX_MS);
    expect(axiosGet).toHaveBeenCalledOnce();
    expect(reload).not.toHaveBeenCalled();
  });

  test("answered, it provides the app's info as before", async () => {
    axiosGet.mockResolvedValueOnce({ data: { version: "v3" } });
    expect(await appInfoPlugin()).toEqual({ provide: { appInfo: { version: "v3" } } });
  });
});

test("restorePageError leaves other errors as they are", () => {
  const reload = vi.fn();
  const error = new Error("boom");
  expect(restorePageError(error, t, reload)).toBe(error);
  expect(restorePageData(error)).toBeNull();
  expect(restorePageData({ statusCode: 503, data: { code: "paused_for_restore" } })).toBeNull();
  expect(restorePageData(null)).toBeNull();
});
