import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { readTokenCookie } from "../use-token-cookie";

/**
 * The fork's hook in the session check (docs/ai/PHASE2.md §3.9): while a backup is restored the server answers
 * `/api/users/self` with 503 paused_for_restore. The user stays signed in, and the session is asked for again once the
 * restore is over. Fork-owned.
 */
vi.mock("~/composables/store", () => ({ clearAllStores: vi.fn() }));
vi.mock("~/composables/use-clear-composable-caches", () => ({ clearComposableCaches: vi.fn() }));

const TOKEN_NAME = "mealie.access_token";

function token(): string {
  const body = btoa(JSON.stringify({ sub: "abc", exp: Math.floor(Date.now() / 1000) + 86_400 }))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `header.${body}.signature`;
}

function paused() {
  return Object.assign(new Error("Service Unavailable"), {
    response: { status: 503, headers: { "retry-after": "60" }, data: { detail: { code: "paused_for_restore" } } },
  });
}

let axiosMock: { get: ReturnType<typeof vi.fn>; post: ReturnType<typeof vi.fn> };
let useAuthBackend: typeof import("../use-auth-backend").useAuthBackend;

beforeEach(async () => {
  document.cookie = `${TOKEN_NAME}=; max-age=0`;
  axiosMock = { get: vi.fn(), post: vi.fn() };
  vi.stubGlobal("useNuxtApp", () => ({ $axios: axiosMock, $appInfo: { production: false } }));
  vi.stubGlobal("useRouter", () => ({ push: vi.fn() }));
  vi.stubGlobal("useRuntimeConfig", () => ({ public: { AUTH_TOKEN: TOKEN_NAME } }));
  vi.stubGlobal("clearNuxtData", vi.fn());
  vi.stubGlobal("useCookie", (name: string) => ({
    get value() {
      return readTokenCookie(name);
    },
    set value(next: string | null) {
      document.cookie = next === null ? `${name}=; max-age=0` : `${name}=${encodeURIComponent(next)}`;
    },
  }));
  vi.resetModules();
  ({ useAuthBackend } = await import("../use-auth-backend"));
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("the session check during a backup restore", () => {
  test("waits it out and stays signed in, asking again every few seconds", async () => {
    vi.useFakeTimers();
    const auth = useAuthBackend();
    auth.setToken(token());
    axiosMock.get
      .mockRejectedValueOnce(paused())
      .mockRejectedValueOnce(paused())
      .mockResolvedValueOnce({ data: { id: "user-1" } });

    const checking = auth.getSession();
    await vi.advanceTimersByTimeAsync(4999);
    expect(axiosMock.get).toHaveBeenCalledTimes(1);
    expect(auth.status.value).toBe("loading");
    await vi.advanceTimersByTimeAsync(5001);
    await checking;

    expect(axiosMock.get).toHaveBeenCalledTimes(3);
    expect(auth.status.value).toBe("authenticated");
    expect(auth.data.value).toEqual({ id: "user-1" });
    expect(auth.token.value).not.toBeNull();
  });

  test("a restore that doesn't end gives up after a while, as any failure does, without dropping the token", async () => {
    vi.useFakeTimers();
    vi.spyOn(console, "error").mockImplementation(() => {});
    const auth = useAuthBackend();
    auth.setToken(token());
    axiosMock.get.mockRejectedValue(paused());

    const checking = auth.getSession();
    await vi.advanceTimersByTimeAsync(5 * 60_000 + 5000);
    await checking;
    expect(auth.status.value).toBe("unauthenticated");
    expect(auth.token.value).not.toBeNull();
    vi.restoreAllMocks();
  });

  test("any other failure is as before", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const auth = useAuthBackend();
    auth.setToken(token());
    axiosMock.get.mockRejectedValueOnce(Object.assign(new Error("Unauthorized"), { response: { status: 401 } }));
    await auth.getSession();
    expect(axiosMock.get).toHaveBeenCalledOnce();
    expect(auth.status.value).toBe("unauthenticated");
    expect(auth.token.value).toBeNull();
    vi.restoreAllMocks();
  });
});
