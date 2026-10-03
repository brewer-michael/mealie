import { beforeEach, describe, expect, test, vi } from "vitest";
import {
  apiErrorMessage,
  buildClientCreate,
  buildClientUpdate,
  canWrite,
  cleanRedirectUris,
  clientFormFrom,
  consentDecision,
  emptyClientForm,
  formatDateTime,
  isClientFormValid,
  isFramed,
  isValidRequestHandle,
  isWebUrl,
  mcpServerUrl,
  redirectUriProblem,
  useMcpApiTokenGrant,
  useMcpClientEditor,
  useMcpClients,
  useMcpConnections,
  useMcpConsent,
} from "../use-mcp";
import type { McpClientForm } from "../use-mcp";
import type { McpClientOut, McpConnectionOut, McpOAuthRequestOut } from "~/lib/api/types/mcp";

const api = vi.hoisted(() => ({
  getClients: vi.fn(),
  createClient: vi.fn(),
  updateClient: vi.fn(),
  deleteClient: vi.fn(),
  rotateClientSecret: vi.fn(),
  getHomeAssistantPreset: vi.fn(),
  getRequest: vi.fn(),
  decideRequest: vi.fn(),
  getConnections: vi.fn(),
  disconnect: vi.fn(),
  getApiTokenGrant: vi.fn(),
  updateApiTokenGrant: vi.fn(),
}));

vi.mock("~/composables/api", () => ({
  useUserApi: () => ({ mcp: api }),
}));

const HANDLE = "Xq3_kZ-0aB1cD2eF3gH4iJ5kL6mN7oP8qR9sT0uV1wX";

function httpError(status: number, data: unknown = {}) {
  return { response: { status, data } };
}

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

function client(overrides: Partial<McpClientOut> = {}): McpClientOut {
  return {
    id: "c1",
    groupId: "g1",
    name: "Home Assistant",
    clientId: "mmcp_0123456789abcdef01234567",
    isConfidential: true,
    pkceOptional: true,
    allowWriteScope: false,
    redirectUris: ["https://my.home-assistant.io/redirect/oauth"],
    lastUsedAt: null,
    ...overrides,
  };
}

function form(overrides: Partial<McpClientForm> = {}): McpClientForm {
  return { ...emptyClientForm(), name: "Claude Code", redirectUris: ["http://127.0.0.1/callback"], ...overrides };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("helpers", () => {
  test("the MCP server URL is the origin plus /api/mcp", () => {
    expect(mcpServerUrl("https://mealie.example.com")).toBe("https://mealie.example.com/api/mcp");
    expect(mcpServerUrl("http://192.168.1.20:9000/")).toBe("http://192.168.1.20:9000/api/mcp");
  });

  test("only URL-safe handles are request handles", () => {
    expect(isValidRequestHandle(HANDLE)).toBe(true);
    for (const handle of ["", "..", "../users/self", "a/b", "a%2Fb", "a b", undefined, null, [HANDLE], "x".repeat(257)]) {
      expect(isValidRequestHandle(handle)).toBe(false);
    }
  });

  test("the browser is only sent to http(s) URLs", () => {
    expect(isWebUrl("https://my.home-assistant.io/redirect/oauth?code=x&state=y")).toBe(true);
    expect(isWebUrl("http://127.0.0.1:33418/callback?code=x")).toBe(true);
    expect(isWebUrl("javascript:alert(1)")).toBe(false);
    expect(isWebUrl("/relative")).toBe(false);
  });

  test("a page is framed when it isn't the top window, or when the top window can't be compared", () => {
    const top = {} as Window;
    const page = { top, self: {} } as unknown as Window;
    const crossOrigin = {
      self: {},
      get top(): Window {
        throw new DOMException("Blocked a frame from accessing a cross-origin frame", "SecurityError");
      },
    } as unknown as Window;

    expect(isFramed(window)).toBe(false);
    expect(isFramed({ top, self: top } as unknown as Window)).toBe(false);
    expect(isFramed(page)).toBe(true);
    expect(isFramed(crossOrigin)).toBe(true);
  });

  test("changes are only granted when offered and ticked", () => {
    expect(consentDecision(true, consentRequest(), true)).toEqual({ approve: true, allowWrites: true });
    expect(consentDecision(true, consentRequest(), false)).toEqual({ approve: true, allowWrites: false });
    expect(consentDecision(true, consentRequest({ writesOffered: false }), true))
      .toEqual({ approve: true, allowWrites: false });
    expect(consentDecision(false, consentRequest(), true)).toEqual({ approve: false, allowWrites: false });
  });

  test("dates are shown in the given locale, and unreadable ones as they are", () => {
    expect(formatDateTime("2026-10-02T18:30:00+00:00", "en-US")).toMatch(/^Oct \d{1,2}, 2026, \d{1,2}:\d{2}\s?[AP]M$/);
    expect(formatDateTime("soon", "en-US")).toBe("soon");
  });

  test("a connection can write with the write scope", () => {
    expect(canWrite(["mcp:read", "mcp:write"])).toBe(true);
    expect(canWrite(["mcp:read"])).toBe(false);
  });

  test.each([
    ["https://my.home-assistant.io/redirect/oauth", null],
    ["http://homeassistant.local:8123/auth/external/callback", null],
    ["  http://127.0.0.1/callback  ", null],
    ["https://example.com/a b", "spaces"],
    ["ftp://example.com/callback", "scheme"],
    ["HTTPS://example.com/callback", "scheme"],
    ["example.com/callback", "scheme"],
    ["https://example.com/callback#done", "fragment"],
    ["https://user:pass@example.com/callback", "user-info"],
    ["https://@example.com/callback", "user-info"],
    ["https://", "host"],
  ])("redirect URI %s has problem %s", (uri, problem) => {
    expect(redirectUriProblem(uri)).toBe(problem);
  });

  test("redirect URIs are trimmed, without blanks and duplicates", () => {
    expect(cleanRedirectUris([" https://a.example/cb ", "", "https://a.example/cb", "  ", "http://localhost/cb"]))
      .toEqual(["https://a.example/cb", "http://localhost/cb"]);
  });

  test("a client needs a name and at least one valid redirect URI", () => {
    expect(isClientFormValid(form())).toBe(true);
    expect(isClientFormValid(form({ name: "  " }))).toBe(false);
    expect(isClientFormValid(form({ name: "x".repeat(101) }))).toBe(false);
    expect(isClientFormValid(form({ redirectUris: ["", " "] }))).toBe(false);
    expect(isClientFormValid(form({ redirectUris: ["http://127.0.0.1/cb", "nope"] }))).toBe(false);
    expect(isClientFormValid(form({ redirectUris: Array.from({ length: 11 }, (_, i) => `https://a.example/${i}`) })))
      .toBe(false);
  });

  test("a public client never has PKCE optional", () => {
    expect(buildClientCreate(form({ name: " Claude ", isConfidential: false, pkceOptional: true, redirectUris: ["", "http://127.0.0.1/cb"] })))
      .toEqual({
        name: "Claude",
        redirectUris: ["http://127.0.0.1/cb"],
        isConfidential: false,
        pkceOptional: false,
        allowWriteScope: false,
      });
    expect(buildClientCreate(form({ pkceOptional: true, allowWriteScope: true })))
      .toMatchObject({ isConfidential: true, pkceOptional: true, allowWriteScope: true });
  });

  test("an update keeps the saved client type out of the payload", () => {
    const payload = buildClientUpdate(form({ isConfidential: true, pkceOptional: true }), false);
    expect(payload).toEqual({
      name: "Claude Code",
      redirectUris: ["http://127.0.0.1/callback"],
      pkceOptional: false,
      allowWriteScope: false,
    });
  });

  test("a preset fills the form", () => {
    expect(clientFormFrom({
      name: "Home Assistant",
      redirectUris: ["https://my.home-assistant.io/redirect/oauth", "http://homeassistant.local:8123/auth/external/callback"],
      isConfidential: true,
      pkceOptional: true,
      allowWriteScope: false,
    })).toEqual({
      name: "Home Assistant",
      redirectUris: ["https://my.home-assistant.io/redirect/oauth", "http://homeassistant.local:8123/auth/external/callback"],
      isConfidential: true,
      pkceOptional: true,
      allowWriteScope: false,
    });
  });

  test("the API's reason is read from validation errors and error messages", () => {
    expect(apiErrorMessage(httpError(422, {
      detail: [{ loc: ["body", "redirectUris"], msg: "Value error, Use https://, or http:// only for a local network address (http://example.com/cb)" }],
    }))).toBe("Use https://, or http:// only for a local network address (http://example.com/cb)");
    expect(apiErrorMessage(httpError(400, { detail: { message: "Not allowed" } }))).toBe("Not allowed");
    expect(apiErrorMessage(httpError(500))).toBeNull();
    expect(apiErrorMessage(new Error("Network Error"))).toBeNull();
  });
});

describe("useMcpConsent", () => {
  test("an invalid handle is never sent to the API", async () => {
    const consent = useMcpConsent();
    await consent.load("../../users/self");

    expect(consent.state.value).toBe("invalid");
    expect(api.getRequest).not.toHaveBeenCalled();
  });

  test("loads the request", async () => {
    api.getRequest.mockResolvedValue({ data: consentRequest(), error: null });
    const consent = useMcpConsent();
    await consent.load(HANDLE);

    expect(api.getRequest).toHaveBeenCalledWith(HANDLE);
    expect(consent.state.value).toBe("ready");
    expect(consent.request.value?.clientName).toBe("Home Assistant");
  });

  test("an expired or answered request is not found", async () => {
    api.getRequest.mockResolvedValue({ data: null, error: httpError(404) });
    const consent = useMcpConsent();
    await consent.load(HANDLE);

    expect(consent.state.value).toBe("not-found");
  });

  test("other failures can be retried", async () => {
    api.getRequest.mockResolvedValue({ data: null, error: httpError(500) });
    const consent = useMcpConsent();
    await consent.load(HANDLE);

    expect(consent.state.value).toBe("error");
  });

  test("approving returns where to send the browser", async () => {
    api.getRequest.mockResolvedValue({ data: consentRequest(), error: null });
    api.decideRequest.mockResolvedValue({ data: { redirectTo: "https://my.home-assistant.io/redirect/oauth?code=c&state=s" }, error: null });
    const consent = useMcpConsent();
    await consent.load(HANDLE);

    expect(await consent.decide(true, true)).toBe("https://my.home-assistant.io/redirect/oauth?code=c&state=s");
    expect(api.decideRequest).toHaveBeenCalledWith(HANDLE, { approve: true, allowWrites: true });
    expect(consent.state.value).toBe("redirecting");
  });

  test("a request answered elsewhere is not found", async () => {
    api.getRequest.mockResolvedValue({ data: consentRequest(), error: null });
    api.decideRequest.mockResolvedValue({ data: null, error: httpError(404) });
    const consent = useMcpConsent();
    await consent.load(HANDLE);

    expect(await consent.decide(false)).toBeNull();
    expect(consent.state.value).toBe("not-found");
  });

  test("a failed decision can be tried again", async () => {
    api.getRequest.mockResolvedValue({ data: consentRequest(), error: null });
    api.decideRequest.mockResolvedValue({ data: null, error: httpError(500) });
    const consent = useMcpConsent();
    await consent.load(HANDLE);

    expect(await consent.decide(true)).toBeNull();
    expect(consent.state.value).toBe("ready");
    expect(consent.decisionFailed.value).toBe(true);
  });

  test("never sends the browser to a non-web URL", async () => {
    api.getRequest.mockResolvedValue({ data: consentRequest(), error: null });
    api.decideRequest.mockResolvedValue({ data: { redirectTo: "javascript:alert(1)" }, error: null });
    const consent = useMcpConsent();
    await consent.load(HANDLE);

    expect(await consent.decide(true)).toBeNull();
    expect(consent.decisionFailed.value).toBe(true);
  });

  test("decides only once at a time", async () => {
    api.getRequest.mockResolvedValue({ data: consentRequest(), error: null });
    api.decideRequest.mockReturnValue(new Promise(() => {}));
    const consent = useMcpConsent();
    await consent.load(HANDLE);

    void consent.decide(true);
    expect(await consent.decide(false)).toBeNull();
    expect(api.decideRequest).toHaveBeenCalledOnce();
  });
});

describe("useMcpClients", () => {
  test("a failed refresh keeps the loaded clients", async () => {
    api.getClients.mockResolvedValueOnce({ data: [client()] });
    api.getClients.mockResolvedValueOnce({ data: null });
    const clients = useMcpClients();

    await clients.load();
    expect(clients.loaded.value).toBe(true);
    await clients.load();

    expect(clients.loadFailed.value).toBe(true);
    expect(clients.clients.value).toHaveLength(1);
  });

  test("deleting a client removes it from the list", async () => {
    api.getClients.mockResolvedValue({ data: [client(), client({ id: "c2", name: "Claude" })] });
    api.deleteClient.mockResolvedValue({ data: client() });
    const clients = useMcpClients();
    await clients.load();

    expect(await clients.remove("c1")).toBe(true);
    expect(clients.clients.value.map(c => c.id)).toEqual(["c2"]);
  });
});

describe("useMcpClientEditor", () => {
  test("creates a client from the form", async () => {
    api.createClient.mockResolvedValue({ data: { ...client(), clientSecret: "mmcp_cs_secret" }, error: null });
    const editor = useMcpClientEditor();

    const created = await editor.create(form({ name: "Home Assistant", pkceOptional: true }));

    expect(created?.clientSecret).toBe("mmcp_cs_secret");
    expect(api.createClient).toHaveBeenCalledWith(expect.objectContaining({ name: "Home Assistant", pkceOptional: true }));
    expect(editor.failed.value).toBe(false);
  });

  test("keeps the API's reason for a refused save", async () => {
    api.updateClient.mockResolvedValue({
      data: null,
      error: httpError(422, { detail: [{ msg: "Value error, Use https://, or http:// only for a local network address (http://a.example/cb)" }] }),
    });
    const editor = useMcpClientEditor();

    expect(await editor.update(client(), form())).toBeNull();
    expect(api.updateClient).toHaveBeenCalledWith("c1", expect.not.objectContaining({ isConfidential: expect.anything() }));
    expect(editor.failed.value).toBe(true);
    expect(editor.failureReason.value).toBe("Use https://, or http:// only for a local network address (http://a.example/cb)");
  });

  test("asks for the Home Assistant preset with the address entered", async () => {
    api.getHomeAssistantPreset.mockResolvedValue({ data: null });
    const editor = useMcpClientEditor();

    await editor.homeAssistantPreset(" http://192.168.1.20:8123 ");
    await editor.homeAssistantPreset("");

    expect(api.getHomeAssistantPreset).toHaveBeenNthCalledWith(1, "http://192.168.1.20:8123");
    expect(api.getHomeAssistantPreset).toHaveBeenNthCalledWith(2, undefined);
  });

  test("tells a refused Home Assistant address from a failure to load the preset", async () => {
    const editor = useMcpClientEditor();
    const refused = "Use https://, or http:// only for a local network address (http://ha.example.com/auth/external/callback)";

    api.getHomeAssistantPreset.mockResolvedValueOnce({ data: null, error: httpError(400, { detail: { message: refused } }) });
    expect(await editor.homeAssistantPreset("http://ha.example.com")).toEqual({ preset: null, refusal: refused });

    api.getHomeAssistantPreset.mockResolvedValueOnce({ data: null, error: httpError(500) });
    expect(await editor.homeAssistantPreset("http://ha.example.com")).toEqual({ preset: null, refusal: null });

    const preset = { name: "Home Assistant", redirectUris: ["https://my.home-assistant.io/redirect/oauth"] };
    api.getHomeAssistantPreset.mockResolvedValueOnce({ data: preset, error: null });
    expect(await editor.homeAssistantPreset()).toEqual({ preset, refusal: null });
  });
});

describe("useMcpConnections", () => {
  const connection: McpConnectionOut = {
    clientId: "c1",
    clientName: "Home Assistant",
    scopes: ["mcp:read"],
    createdAt: "2026-10-01T08:00:00Z",
    lastUsedAt: null,
  };

  test("disconnecting removes the app", async () => {
    api.getConnections.mockResolvedValue({ data: [connection] });
    api.disconnect.mockResolvedValue({ data: connection });
    const connections = useMcpConnections();
    await connections.load();

    expect(await connections.disconnect("c1")).toBe(true);
    expect(api.disconnect).toHaveBeenCalledWith("c1");
    expect(connections.connections.value).toEqual([]);
  });

  test("a failed disconnect keeps the app", async () => {
    api.getConnections.mockResolvedValue({ data: [connection] });
    api.disconnect.mockResolvedValue({ data: null });
    const connections = useMcpConnections();
    await connections.load();

    expect(await connections.disconnect("c1")).toBe(false);
    expect(connections.connections.value).toHaveLength(1);
  });
});

describe("useMcpApiTokenGrant", () => {
  test("loads and sets the grant", async () => {
    api.getApiTokenGrant.mockResolvedValue({ data: { tokenId: 7, allowWrites: false } });
    api.updateApiTokenGrant.mockResolvedValue({ data: { tokenId: 7, allowWrites: true } });
    const grant = useMcpApiTokenGrant(7);

    await grant.load();
    expect(grant.allowWrites.value).toBe(false);
    expect(await grant.set(true)).toBe(true);

    expect(api.updateApiTokenGrant).toHaveBeenCalledWith(7, { allowWrites: true });
    expect(grant.allowWrites.value).toBe(true);
  });

  test("a failed update keeps the saved value", async () => {
    api.getApiTokenGrant.mockResolvedValue({ data: { tokenId: 7, allowWrites: false } });
    api.updateApiTokenGrant.mockResolvedValue({ data: null });
    const grant = useMcpApiTokenGrant(7);
    await grant.load();

    expect(await grant.set(true)).toBe(false);
    expect(grant.allowWrites.value).toBe(false);
  });

  test("a failed load leaves the grant unknown", async () => {
    api.getApiTokenGrant.mockResolvedValue({ data: null });
    const grant = useMcpApiTokenGrant(7);
    await grant.load();

    expect(grant.allowWrites.value).toBeNull();
    expect(grant.loadFailed.value).toBe(true);
  });
});
