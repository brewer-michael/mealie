import type { AxiosInstance, InternalAxiosRequestConfig } from "axios";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

/**
 * The fork's hook in the axios plugin (docs/ai/PHASE2.md §3.9): while a backup is restored the server answers every
 * request, the token refresh too, with 503 paused_for_restore. That doesn't sign the user out. Fork-owned.
 */
const TOKEN_NAME = "mealie.access_token";

function refused(config: InternalAxiosRequestConfig, status: number, headers: Record<string, string> = {}) {
  const error = new Error(`HTTP ${status}`) as Error & Record<string, unknown>;
  error.config = config;
  error.response = { status, data: { detail: { code: status === 503 ? "paused_for_restore" : "x" } }, config, headers, statusText: "" };
  error.isAxiosError = true;
  return Promise.reject(error);
}

function ok(config: InternalAxiosRequestConfig) {
  return Promise.resolve({ data: { ok: true }, status: 200, statusText: "OK", headers: {}, config });
}

let refreshMock: ReturnType<typeof vi.fn>;
let setTokenMock: ReturnType<typeof vi.fn>;

async function buildClient(adapter: (config: InternalAxiosRequestConfig) => Promise<unknown>) {
  vi.resetModules();
  const plugin = (await import("../axios")).default as unknown as (nuxtApp: {
    runWithContext: <T>(fn: () => T) => T;
  }) => { provide: { axios: AxiosInstance } };
  const { provide } = plugin({ runWithContext: fn => fn() });
  provide.axios.defaults.adapter = adapter as never;
  return provide.axios;
}

beforeEach(() => {
  document.cookie = `${TOKEN_NAME}=a.valid.token`;
  refreshMock = vi.fn();
  setTokenMock = vi.fn();
  vi.stubGlobal("defineNuxtPlugin", (fn: unknown) => fn);
  vi.stubGlobal("useRuntimeConfig", () => ({ public: { AUTH_TOKEN: TOKEN_NAME } }));
  vi.stubGlobal("useAuthBackend", () => ({ refresh: refreshMock, setToken: setTokenMock }));
  Object.defineProperty(window, "location", {
    value: { pathname: "/g/home/recipes/cards", search: "", href: "" },
    writable: true,
    configurable: true,
  });
});

afterEach(() => {
  document.cookie = `${TOKEN_NAME}=; max-age=0`;
  vi.unstubAllGlobals();
});

describe("a backup restore while the token is refreshed", () => {
  test("a refresh answered 503 keeps the user signed in: the request fails with that 503, to be retried", async () => {
    const config = { headers: {} } as InternalAxiosRequestConfig;
    refreshMock.mockImplementation(() => refused(config, 503, { "retry-after": "60" }));
    const client = await buildClient(async config => refused(config, 401));

    const error = await client.post("/api/ai/ingest/batches").catch((e: unknown) => e) as { response?: { status?: number; headers?: Record<string, string> } };
    expect(refreshMock).toHaveBeenCalledOnce();
    // the caller sees a transient refusal with its Retry-After (a card upload retries then), not a dead session
    expect(error.response?.status).toBe(503);
    expect(error.response?.headers?.["retry-after"]).toBe("60");
    expect(setTokenMock).not.toHaveBeenCalled();
    expect(window.location.href).toBe("");
  });

  test("once the restore is over, the next request refreshes and goes through", async () => {
    const config = { headers: {} } as InternalAxiosRequestConfig;
    refreshMock.mockImplementationOnce(() => refused(config, 503)).mockResolvedValueOnce(undefined);
    let calls = 0;
    const client = await buildClient(async (config) => {
      calls += 1;
      return calls <= 2 ? refused(config, 401) : ok(config);
    });

    await expect(client.get("/api/recipes")).rejects.toBeDefined();
    const response = await client.get("/api/recipes");
    expect(response.status).toBe(200);
    expect(setTokenMock).not.toHaveBeenCalled();
    expect(window.location.href).toBe("");
  });

  test("a 503 on any other request never signs the user out", async () => {
    const client = await buildClient(async config => refused(config, 503));
    await expect(client.get("/api/users/self")).rejects.toBeDefined();
    expect(refreshMock).not.toHaveBeenCalled();
    expect(setTokenMock).not.toHaveBeenCalled();
    expect(window.location.href).toBe("");
  });
});
