/**
 * Mealie's MCP server (docs/ai/PHASE3.md §6): the group's OAuth clients, the consent page, the user's connected
 * apps and which of their API tokens may make changes. Fork-owned.
 */
import { toValue, type MaybeRefOrGetter } from "vue";
import { useUserApi } from "~/composables/api";
import type {
  McpClientCreate,
  McpClientCreated,
  McpClientOut,
  McpClientUpdate,
  McpConnectionOut,
  McpOAuthDecision,
  McpOAuthRequestOut,
  McpScope,
} from "~/lib/api/types/mcp";

/** The MCP endpoint's path; clients must use exactly `<origin>/api/mcp` */
export const MCP_PATH = "/api/mcp";

/** The most redirect URIs a client can have (the API's limit) */
export const MAX_REDIRECT_URIS = 10;

/** The longest client name the API takes */
export const MAX_CLIENT_NAME_LENGTH = 100;

export const SCOPE_WRITE: McpScope = "mcp:write";

/** The MCP server's address, as this browser reaches Mealie */
export function mcpServerUrl(origin: string): string {
  return origin.replace(/\/+$/, "") + MCP_PATH;
}

/**
 * Whether a consent page's `request` parameter looks like a handle the server issues (URL-safe base64). Anything
 * else is never put in an API path.
 */
export function isValidRequestHandle(handle: unknown): handle is string {
  return typeof handle === "string" && /^[\w-]{1,256}$/.test(handle);
}

/**
 * Whether the page is shown inside a frame, where another site could trick the user into clicking Approve
 * (clickjacking, RFC 9700 §4.16). A parent that can't even be compared counts as a frame.
 */
export function isFramed(win: Window | undefined = typeof window === "undefined" ? undefined : window): boolean {
  if (!win) {
    return false;
  }
  try {
    return win.top !== win.self;
  }
  catch {
    return true;
  }
}

/** Where the consent page may send the browser: only to an http(s) URL */
export function isWebUrl(url: string): boolean {
  try {
    const { protocol } = new URL(url);
    return protocol === "https:" || protocol === "http:";
  }
  catch {
    return false;
  }
}

/** Whether a connection's scopes let the app make changes */
export function canWrite(scopes: readonly McpScope[]): boolean {
  return scopes.includes(SCOPE_WRITE);
}

/** The consent page's decision. Changes are only allowed when they were on offer and the user ticked the box. */
export function consentDecision(approve: boolean, request: McpOAuthRequestOut, allowWrites: boolean): McpOAuthDecision {
  return { approve, allowWrites: approve && request.writesOffered && allowWrites };
}

/** A date and time from the API (UTC, with its offset), in the user's time zone */
export function formatDateTime(value: string, locale?: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function responseStatus(error: unknown): number | null {
  return (error as { response?: { status?: number } } | null)?.response?.status ?? null;
}

/** The reason the API gave for refusing a request: a validation message, or an error's message */
export function apiErrorMessage(error: unknown): string | null {
  const detail = (error as { response?: { data?: { detail?: unknown } } } | null)?.response?.data?.detail;
  if (Array.isArray(detail)) {
    const message = detail.find(item => typeof item?.msg === "string")?.msg as string | undefined;
    return message ? message.replace(/^Value error, /, "") : null;
  }

  const message = (detail as { message?: unknown } | null | undefined)?.message;
  return typeof message === "string" ? message : null;
}

// ==========================================
// Client form

/** Home Assistant's preset, or why the Home Assistant address entered was refused */
export interface HomeAssistantPresetResult {
  preset: McpClientCreate | null;
  /** The API's reason for refusing the address, such as plain http for a host on the internet */
  refusal: string | null;
}

export interface McpClientForm {
  name: string;
  redirectUris: string[];
  isConfidential: boolean;
  pkceOptional: boolean;
  allowWriteScope: boolean;
}

export type RedirectUriProblem = "spaces" | "scheme" | "fragment" | "host" | "user-info";

export function emptyClientForm(): McpClientForm {
  return { name: "", redirectUris: [""], isConfidential: true, pkceOptional: false, allowWriteScope: false };
}

/** A form filled in from a saved client or a preset */
export function clientFormFrom(client: McpClientCreate | McpClientOut): McpClientForm {
  return {
    name: client.name,
    redirectUris: client.redirectUris.length ? [...client.redirectUris] : [""],
    isConfidential: client.isConfidential ?? true,
    pkceOptional: client.pkceOptional ?? false,
    allowWriteScope: client.allowWriteScope ?? false,
  };
}

/** Trimmed, without blanks and duplicates */
export function cleanRedirectUris(uris: readonly string[]): string[] {
  return [...new Set(uris.map(uri => uri.trim()).filter(Boolean))];
}

/**
 * Obvious mistakes in a redirect URI. The server checks the rest, such as plain `http` only being allowed for
 * local network addresses.
 */
export function redirectUriProblem(uri: string): RedirectUriProblem | null {
  const value = uri.trim();
  if (/\s/.test(value)) {
    return "spaces";
  }
  if (!value.startsWith("http://") && !value.startsWith("https://")) {
    return "scheme";
  }
  if (value.includes("#")) {
    return "fragment";
  }

  // The authority, as the server splits it: up to the first "/" or "?"
  if (value.slice(value.indexOf("//") + 2).split(/[/?]/)[0]?.includes("@")) {
    return "user-info";
  }

  try {
    return new URL(value).hostname ? null : "host";
  }
  catch {
    return "host";
  }
}

export function isClientFormValid(form: McpClientForm): boolean {
  const name = form.name.trim();
  const uris = cleanRedirectUris(form.redirectUris);
  return name.length > 0
    && name.length <= MAX_CLIENT_NAME_LENGTH
    && uris.length > 0
    && uris.length <= MAX_REDIRECT_URIS
    && uris.every(uri => redirectUriProblem(uri) === null);
}

export function buildClientCreate(form: McpClientForm): McpClientCreate {
  return {
    name: form.name.trim(),
    redirectUris: cleanRedirectUris(form.redirectUris),
    isConfidential: form.isConfidential,
    // OAuth 2.1 §7.5.1: only a client that authenticates may go without PKCE
    pkceOptional: form.isConfidential && form.pkceOptional,
    allowWriteScope: form.allowWriteScope,
  };
}

/** Whether a client is confidential can't change, so it's taken from the saved client */
export function buildClientUpdate(form: McpClientForm, isConfidential: boolean): McpClientUpdate {
  return {
    name: form.name.trim(),
    redirectUris: cleanRedirectUris(form.redirectUris),
    pkceOptional: isConfidential && form.pkceOptional,
    allowWriteScope: form.allowWriteScope,
  };
}

// ==========================================
// Composables

/** The group's OAuth clients (group managers only) */
export function useMcpClients() {
  const api = useUserApi();
  const clients = ref<McpClientOut[]>([]);
  const loading = ref(false);
  /** Whether the list has loaded at least once */
  const loaded = ref(false);
  /** Whether the last load failed; the clients loaded before it are kept */
  const loadFailed = ref(false);

  async function load() {
    loading.value = true;
    try {
      const { data } = await api.mcp.getClients();
      if (data) {
        clients.value = data;
        loaded.value = true;
      }
      loadFailed.value = !data;
    }
    finally {
      loading.value = false;
    }
  }

  /** Deletes a client, which revokes its tokens; returns whether that worked */
  async function remove(id: string) {
    const { data } = await api.mcp.deleteClient(id);
    if (data) {
      clients.value = clients.value.filter(client => client.id !== id);
    }
    return !!data;
  }

  /** A new secret for a confidential client, shown once; null if that failed */
  async function rotateSecret(id: string) {
    const { data } = await api.mcp.rotateClientSecret(id);
    return data;
  }

  return {
    clients,
    loading: readonly(loading),
    loaded: readonly(loaded),
    loadFailed: readonly(loadFailed),
    load,
    remove,
    rotateSecret,
  };
}

/** Adding and editing a client */
export function useMcpClientEditor() {
  const api = useUserApi();
  const saving = ref(false);
  /** Whether the last save was refused or failed */
  const failed = ref(false);
  /** The API's reason for refusing the last save, if it gave one */
  const failureReason = ref<string | null>(null);

  async function save<T>(request: () => Promise<{ data: T | null; error: unknown }>): Promise<T | null> {
    saving.value = true;
    clearError();
    try {
      const { data, error } = await request();
      if (!data) {
        failed.value = true;
        failureReason.value = apiErrorMessage(error);
      }
      return data;
    }
    finally {
      saving.value = false;
    }
  }

  /** The new client, with its secret if it's confidential (shown once) */
  async function create(form: McpClientForm): Promise<McpClientCreated | null> {
    return await save(() => api.mcp.createClient(buildClientCreate(form)));
  }

  async function update(client: McpClientOut, form: McpClientForm): Promise<McpClientOut | null> {
    return await save(() => api.mcp.updateClient(client.id, buildClientUpdate(form, client.isConfidential)));
  }

  /**
   * Home Assistant's client; `homeAssistantUrl` sets its second redirect URI. No preset and no refusal means
   * loading it failed.
   */
  async function homeAssistantPreset(homeAssistantUrl?: string): Promise<HomeAssistantPresetResult> {
    const { data, error } = await api.mcp.getHomeAssistantPreset(homeAssistantUrl?.trim() || undefined);
    // The API answers 400, with its reason, for an address it won't take
    const refusal = !data && responseStatus(error) === 400 ? apiErrorMessage(error) : null;
    return { preset: data, refusal };
  }

  function clearError() {
    failed.value = false;
    failureReason.value = null;
  }

  return {
    saving: readonly(saving),
    failed: readonly(failed),
    failureReason: readonly(failureReason),
    create,
    update,
    homeAssistantPreset,
    clearError,
  };
}

export type McpConsentState = "loading" | "invalid" | "not-found" | "error" | "ready" | "deciding" | "redirecting";

/** A pending authorization request, as the consent page shows it, and the user's decision */
export function useMcpConsent() {
  const api = useUserApi();
  const state = ref<McpConsentState>("loading");
  const request = ref<McpOAuthRequestOut | null>(null);
  /** Whether the last decision couldn't be recorded (the request is still pending) */
  const decisionFailed = ref(false);
  let handle = "";

  async function load(requestHandle: unknown) {
    decisionFailed.value = false;
    if (!isValidRequestHandle(requestHandle)) {
      state.value = "invalid";
      return;
    }

    handle = requestHandle;
    state.value = "loading";
    const { data, error } = await api.mcp.getRequest(handle);
    request.value = data;
    state.value = data ? "ready" : responseStatus(error) === 404 ? "not-found" : "error";
  }

  /** Records the decision; returns where to send the browser next, or null if that failed */
  async function decide(approve: boolean, allowWrites = false): Promise<string | null> {
    if (!request.value || state.value !== "ready") {
      return null;
    }

    state.value = "deciding";
    decisionFailed.value = false;
    const { data, error } = await api.mcp.decideRequest(handle, consentDecision(approve, request.value, allowWrites));
    if (data && isWebUrl(data.redirectTo)) {
      state.value = "redirecting";
      return data.redirectTo;
    }

    // Expired, or answered in another tab
    if (responseStatus(error) === 404) {
      state.value = "not-found";
      return null;
    }

    state.value = "ready";
    decisionFailed.value = true;
    return null;
  }

  return { state: readonly(state), request: readonly(request), decisionFailed: readonly(decisionFailed), load, decide };
}

/** The apps the user has connected with OAuth */
export function useMcpConnections() {
  const api = useUserApi();
  const connections = ref<McpConnectionOut[]>([]);
  const loading = ref(false);
  const loaded = ref(false);
  /** Whether the last load failed; the connections loaded before it are kept */
  const loadFailed = ref(false);

  async function load() {
    loading.value = true;
    try {
      const { data } = await api.mcp.getConnections();
      if (data) {
        connections.value = data;
        loaded.value = true;
      }
      loadFailed.value = !data;
    }
    finally {
      loading.value = false;
    }
  }

  /** Revokes every token the user gave the app; returns whether that worked */
  async function disconnect(clientId: string) {
    const { data } = await api.mcp.disconnect(clientId);
    if (data) {
      connections.value = connections.value.filter(connection => connection.clientId !== clientId);
    }
    return !!data;
  }

  return {
    connections,
    loading: readonly(loading),
    loaded: readonly(loaded),
    loadFailed: readonly(loadFailed),
    load,
    disconnect,
  };
}

/** Whether AI assistants using one of the user's API tokens may make changes */
export function useMcpApiTokenGrant(tokenId: MaybeRefOrGetter<number>) {
  const api = useUserApi();
  /** null until loaded */
  const allowWrites = ref<boolean | null>(null);
  const loading = ref(false);
  const saving = ref(false);
  const loadFailed = ref(false);

  async function load() {
    loading.value = true;
    try {
      const { data } = await api.mcp.getApiTokenGrant(toValue(tokenId));
      allowWrites.value = data ? data.allowWrites : null;
      loadFailed.value = !data;
    }
    finally {
      loading.value = false;
    }
  }

  /** Returns whether that worked; the saved value is kept if it didn't */
  async function set(value: boolean) {
    saving.value = true;
    try {
      const { data } = await api.mcp.updateApiTokenGrant(toValue(tokenId), { allowWrites: value });
      if (data) {
        allowWrites.value = data.allowWrites;
      }
      return !!data;
    }
    finally {
      saving.value = false;
    }
  }

  return {
    allowWrites: readonly(allowWrites),
    loading: readonly(loading),
    saving: readonly(saving),
    loadFailed: readonly(loadFailed),
    load,
    set,
  };
}
