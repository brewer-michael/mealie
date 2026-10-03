# Phase 3: Mealie MCP server (design, as built)

Implements the "Phase 3" row of [`AI_INTEGRATION_PLAN.md`](../AI_INTEGRATION_PLAN.md) §9. Phase 1's tool registry
([`PHASE1.md`](PHASE1.md) §5) is exposed as an MCP server at `/api/mcp`, so that:

- **Home Assistant's** built-in MCP client can connect. It authenticates with OAuth, and its voice assistant can then
  search recipes, read steps, check the plan and add to the shopping list.
- **Claude Desktop, Claude Code** and other MCP clients can connect with a Mealie API token, or with OAuth.

The research behind every decision here, with file:line citations into Home Assistant core and the MCP SDK and
probes that ran HA's own client code against a test server, is summarised in the
"[Facts this design depends on](#facts-this-design-depends-on)" section.

## Facts this design depends on

These were verified on 2026-10-03 against HA core `dev` (= 2026.10.0b0) and 2026.9.4, and `mcp` 1.26.0, 1.28.1,
1.30.0 and 2.3.0.

**HA's MCP client: discovery**
- **First contact:** HA first sends `initialize` without a token and needs a **401** back.
  - It reads `resource_metadata="…"` and `scope="…"` from the `WWW-Authenticate` header.
  - It then fetches protected-resource metadata (RFC 9728) from the header URL, `/.well-known/oauth-protected-resource/api/mcp`
    and `/.well-known/oauth-protected-resource` **concurrently, taking the first 2xx**.
  - It requires the metadata's `resource` to equal the URL the user typed, **as an exact string**.
- **Authorization server metadata:** HA then fetches RFC 8414 / OIDC metadata the same way (first 2xx wins).
  - **Mealie's SPA answers any unknown GET with 200 HTML**, which wins that race intermittently and makes HA fail.
    Every `/.well-known/*` path Mealie doesn't serve must return a **JSON 404**.

**HA's MCP client: OAuth**
- **No dynamic client registration.** The user enters a client ID and secret under Application Credentials, so HA is
  always a **confidential client**.
- **HA sends no PKCE and no `resource` parameter.** OAuth 2.1 only allows dropping PKCE for confidential clients
  (with OIDC nonce), so Mealie has a **per-client "PKCE optional" flag** that is valid only for confidential clients.
  The MCP SDK's built-in authorization server always requires PKCE, which is why Mealie runs its own.
- **Redirect URI:** `https://my.home-assistant.io/redirect/oauth`, or `<ha>/auth/external/callback`.
- **Token requests:** `client_secret_post`. The response must include `expires_in`. Refresh tokens are effectively
  required, and a 4xx on refresh forces the user to re-consent.

**HA's MCP client: calling tools**
- Streamable HTTP is tried first. HA falls back to SSE only on a 405 or an MCP error. Each tool call is 4 POSTs
  (initialize, initialized, tools/call, tools/list), with 5 s per POST and 10 s overall.
- HA converts every tool's input schema. One failure drops **all** tools. All 8 registry schemas convert cleanly.
- The whole `CallToolResult` is sent to the model, so don't duplicate the data in `structuredContent`.
- HA keeps tool names as they are, and prefixes them `mealie__…` only when several LLM APIs are selected.
  Annotations and titles aren't shown to the model.

**MCP SDK and spec**
- `mcp==1.30.0` works with HA's 1.26 and 1.28 clients, and adds packages without changing any locked version.
  2.3.0 also works with HA; moving to it later stays inside one module.
- FastMCP is avoided: it regenerates schemas from function signatures and rejects unknown Host headers by default.
  The low-level `Server` plus `StreamableHTTPSessionManager(stateless=True, json_response=True)` is used instead.
- The current spec (2026-07-28) requires protected-resource metadata for servers that do auth, recommends a `scope`
  hint and RFC 9207 `iss`, forbids token passthrough and requires audience validation. Spec-following clients
  (Claude Code) use PKCE S256, `resource` and loopback redirect URIs on any port.

## Architecture

```
HA / Claude ──POST /api/mcp──► McpEndpoint (ASGI)   ──► StreamableHTTPSessionManager ──► mcp Server
                                   │ verify token in worker thread                       │ list_tools / call_tool
                                   │ (MCP OAuth token or Mealie API token)               ▼
                                   ▼                                         Phase 1 tool registry
                               401 + WWW-Authenticate (resource_metadata, scope)   (ToolContext, run_blocking)

/.well-known/oauth-protected-resource[/api/mcp]   RFC 9728 metadata
/.well-known/oauth-authorization-server[/api/mcp] RFC 8414 metadata
/.well-known/*                                    JSON 404 (keeps the SPA out)
/api/oauth/authorize ──► SPA /oauth/consent ──(log in if needed)──► POST decision ──► redirect with code
/api/oauth/token  /api/oauth/revoke
```

All new code is fork-owned:
- **Server:** `mealie/routes/ai/mcp.py`, `mealie/services/ai/mcp/`.
- **OAuth:** `mealie/routes/oauth/`, `mealie/services/oauth/`.
- **Models:** `mealie/db/models/ai_mcp.py`.

The only upstream touch point is one router include in `mealie/app.py`, because the `/.well-known` routes live at
the root, outside `/api`. MCP OAuth tokens are opaque rather than JWTs, so upstream's JWT check already refuses them on
the REST API: an MCP connection can only ever reach the tools.

## 1. MCP endpoint

- **Mounting:** `Route("/mcp")` and `Route("/mcp/")` on the `ai` APIRouter, which is mounted under `/api`. The
  endpoint is an ASGI class instance.
  - `APIRouter(lifespan=…)` creates a **new** session manager per app lifespan, so `app.py` needs no change for this.
  - The endpoint answers anything but POST with **405** itself, so nothing reaches the SPA.
- **Server:** `serverInfo.name = "Mealie"`, `version = APP_VERSION`, capability `tools` only.
- **`tools/list` touches no database.** Tools come from the registry in a fixed order, with:
  - the registry's `input_schema`, served verbatim (flat, no `$ref`);
  - a `title`;
  - annotations: reads get `readOnlyHint` and `idempotentHint`; writes get `readOnlyHint=false` and
    `destructiveHint=false`; all tools get `openWorldHint=false`.
  - **Write tools are listed only when the caller has a write grant** (§3).
- **`tools/call`:**
  - Arguments are validated with the tool's pydantic model.
  - The handler runs through the registry with a `ToolContext`. Building it touches no database (PHASE1 §5), and all
    database work runs in worker threads.
  - Calls are capped at **4 s** counted from the request's arrival (at least 1 s for the tool itself), so HA's 5 s
    per-POST budget holds. On timeout the result is a speakable `isError`: "Mealie took too long to answer. Try
    again." (`timeout`) for reads, and for writes, which usually still land, "Mealie is still saving that, and it may
    still go through. Check before asking again." (`timeout_pending`, `may_have_applied: true`). The tool keeps
    running and its outcome is logged; at shutdown the server waits up to 10 s for such tools and their events.
  - At most **16 tools run at once** per process, counting timed-out ones still running. Past that a call gets
    "Mealie is busy right now. Try again in a moment." (`busy`) instead of queueing.
  - **Result:** a single `TextContent` with compact JSON of the tool result (`speech` first, then the data). No
    `structuredContent` or `outputSchema` for now.
  - **Errors:** unknown tool, `ToolNotFoundError`, `ToolError`, pydantic validation errors and missing write grants
    all come back as `isError: true` with the speakable message, never as JSON-RPC or HTTP errors. The text is
    `{"speech", "error", ["errors"]}` with `error` one of `unknown_tool`, `invalid_arguments` (plus pydantic's error
    list), `not_found`, `tool_error`, `write_not_allowed`, `timeout`, `timeout_pending`, `busy` or
    `internal_error`. The write grant is checked before the arguments.
  - **The `tools/call` handler is registered directly** (`server.request_handlers[CallToolRequest]`), not through the
    SDK's decorator. The decorator keeps a process-wide tool cache that callers with different grants would share,
    re-lists tools on every miss, logs the raw tool name, and sends raw exception text to the client.
  - **Requests over 64 KiB get 413** (the largest real call is about 10 KB). The SDK logs parts of malformed
    requests, so this also bounds what a caller can put in the log. The SDK's per-request INFO loggers are set to
    WARNING; Mealie logs one line per call.
  - Write tools publish the same events as REST, with `integration_id = "mcp:<client name>"` (or
    `"mcp:API token"`). They're sent in the background after the answer, so webhook and notifier latency doesn't
    count against the 4 s, and a delivery failure is logged.
- **Origin:** if an `Origin` header is present and isn't the request's own origin, the endpoint returns **403**.
  This stops ordinary cross-site requests. It can't stop DNS rebinding on its own (the rebound Host matches the
  Origin); what does is that only bearer tokens authenticate, never cookies.

## 2. Bearer authentication

`McpEndpoint` is an ASGI wrapper in front of the session manager.

- **Accepted tokens:**
  1. **MCP OAuth access tokens**: opaque, with prefix `mmcp_at_`, looked up by SHA-256 in `mcp_oauth_tokens`. They
     must be unexpired and unrevoked, and their `resource` must be this server's `/api/mcp` URL (audience check).
  2. **Mealie long-lived API tokens**: the JWTs from Profile → API Tokens, validated exactly as upstream's
     `validate_long_live_token` does.
- **Rejected:** session JWTs and cookies. A browser session can't drive MCP.
- **Lookups run in a worker thread** (`run_in_threadpool` + `session_context()`), never on the event loop.
- **Cache:** results are cached for 60 s, keyed by the token's SHA-256, because HA authenticates 4 POSTs per call.
  A cache hit is checked on the event loop (`cached_mcp_principal`, no database); only a miss goes to a worker
  thread. Revoking, disconnecting, deleting a client, API token or user, or any change to a user drops the affected
  entries once the change is committed, so in a single-process Mealie (the default) it applies at once. Other worker
  processes notice within 60 s.
- **Failure:** `401` with
  `WWW-Authenticate: Bearer error="invalid_token", resource_metadata="<origin>/.well-known/oauth-protected-resource/api/mcp", scope="mcp:read mcp:write"`.
  Without any credentials the `error` parameter is left out (RFC 6750 §3.1); HA reads only `resource_metadata` and
  `scope`.
  - `<origin>` comes from the request's scheme and Host. Uvicorn already trusts `X-Forwarded-Proto` from configured
    proxies. The Docker image trusts only the container's gateway, so a TLS proxy in another container isn't trusted;
    a plain `http` request for an `https` `BASE_URL`'s host and port is therefore upgraded to `https`.
- **Request identity:** the authenticated user, client name (or "API token"), scopes and write grant are attached to
  the request scope for the tool layer.

## 3. Scopes and write grants

- **Scopes:** `mcp:read` (all read tools) and `mcp:write` (`add_to_shopping_list`, `plan_meal`).
- **OAuth:** the consent page shows "Allow changes (shopping list, meal plan)", **unchecked by default**, and only
  when the client is allowed to request writes. The token's scopes are what the user approved.
- **API tokens:** a per-token "Allow AI assistants to make changes" flag in `mcp_api_token_grants`, **off by
  default**, set in Profile → API Tokens.
- **Without a write grant,** write tools are hidden from `tools/list`, and calling one returns `isError` saying
  changes aren't allowed for this connection. HA doesn't step up on a 403.

## 4. Authorization server

This is hand-written and minimal, covering only what MCP clients need. Each rule cites its RFC section in the code.

### Clients

Clients are registered in Mealie, under Group Settings → AI assistants. There is no dynamic client registration.

**Table `mcp_oauth_clients`:**

| Column | Notes |
|---|---|
| `id` | GUID |
| `group_id` | FK |
| `name` | |
| `client_id` | Random and public |
| `client_secret_hash` | Nullable for public clients. SHA-256 of a random secret of at least 256 bits, compared in constant time |
| `is_confidential` | |
| `pkce_optional` | Allowed only when confidential |
| `allow_write_scope` | |
| `redirect_uris` | JSON list of exact strings |
| `created_by` | |
| `created_at`, `last_used_at` | |

**Redirect URIs** match exactly. The exception is a loopback `http://127.0.0.1|localhost|[::1]` URI, which matches
on any port (RFC 8252 §7.3), for Claude Code. Host names must be ASCII (the `xn--` form for international names),
and the consent page shows the punycode host, so a lookalike such as a Cyrillic "і" can't pass for the real host.
Plain `http` is allowed only for loopback and local-network hosts.

**The UI:**
- offers a **Home Assistant preset**, prefilling both HA redirect URIs and setting confidential, PKCE optional and
  read-only by default;
- shows the client secret **once**;
- can rotate the secret;
- deleting a client revokes all its tokens.

### Authorization endpoint

`GET /api/oauth/authorize` handles RFC 6749 §4.1.1.

1. **Validate `client_id` and `redirect_uri` first.** If either is invalid, show an error page and **never
   redirect** (§4.1.2.1).
2. **Then validate the rest:**
   - `response_type=code`;
   - `scope` must be a subset of the client's allowed scopes (empty means `mcp:read`);
   - PKCE: `code_challenge_method` must be `S256` (plain is refused). It's required unless the client is
     confidential with `pkce_optional`;
   - `resource`, if present, must be this server's MCP URL;
   - `state` is passed through untouched.

   These errors redirect back with `error`, `state` and `iss`.
3. **Store a short-lived pending request** in `mcp_oauth_requests`, keyed by a random handle and valid for 10 min.
   The endpoint needs no login, so it's bounded: a query string over 8 KB gets the error page, a `state` over 2048
   characters is refused (HA's is about 600), and only the newest 50 pending requests per client are kept.
4. **Redirect to the SPA** at `/oauth/consent?request=<handle>`. The SPA page needs a login, so an anonymous user
   goes through `/login?redirect=…` (OIDC included) and comes back.
5. **The consent page** calls `GET /api/oauth/requests/{handle}`, which needs auth and returns:
   - the client name;
   - the requested scopes;
   - whether writes are on offer;
   - the redirect host.
6. **The user's decision** goes to `POST /api/oauth/requests/{handle}` as `{approve, allow_writes}`. This needs the
   user's bearer token, which makes it CSRF-safe. It returns `{redirect_to}`:
   - on approval, `redirect_uri?code=…&state=…&iss=<origin>` (RFC 9207);
   - on denial, `redirect_uri?error=access_denied&state=…&iss=…`.

   The SPA then navigates there.
7. **Authorization codes:** random, at least 256-bit, stored hashed, **single use** and valid for 60 s. Each code is
   bound to the client, user, redirect URI, scopes, resource and PKCE challenge.

### Token endpoint

`POST /api/oauth/token` takes form data. Client authentication is `client_secret_post`, `client_secret_basic`, or
`none` for public clients. Responses carry `Cache-Control: no-store`, and errors follow RFC 6749 §5.2 JSON.

- **`authorization_code`:**
  - the code must be valid, unexpired and unused;
  - `redirect_uri` must equal the authorize request's;
  - `code_verifier` must match when a challenge was given, and is required when PKCE is required.
  - **Reuse of a code** revokes every token issued from it (§4.1.2).
- **`refresh_token`:**
  - The old refresh token is **rotated**.
  - **Reusing a rotated refresh token revokes the whole token family**, the OAuth 2.1 reuse detection.
  - A narrower `scope` may be requested.
- **Response:** `{access_token, token_type: "Bearer", expires_in: 3600, refresh_token, scope}`.
  - Access tokens (`mmcp_at_…`) last 1 h.
  - Refresh tokens (`mmcp_rt_…`) expire after 90 days without use, and each refresh extends that.
- **Concurrency:** issuing, refreshing and revoking lock the client row first (`SELECT … FOR UPDATE`, a no-op on
  SQLite), so on PostgreSQL a disconnect or password change racing a refresh can't leave the refreshed tokens live.
- **Storage:** both token types are stored as SHA-256 in `mcp_oauth_tokens`, with:
  - client, user, scopes and resource (defaulting to this server's MCP URL when the client sent none);
  - `family_id`;
  - expiry times;
  - `revoked_at`;
  - `last_used_at`.

### Revocation

`POST /api/oauth/revoke` (RFC 7009) authenticates the client and revokes the token, including its family when it's a
refresh token. It always returns 200.

**Tokens are also revoked when:**
- the user changes their password: a listener revokes their tokens and codes in the same transaction, and
  verification also refuses anything issued before `tokens_valid_after` + 1 s (it's stored in whole seconds);
- the user, the client or the group is deleted;
- the user disconnects the app under Profile → Connected apps (`GET` / `DELETE /api/users/self/mcp/connections`).

### Metadata

These are served at the root by a fork router included in `app.py`.

- **`/.well-known/oauth-protected-resource/api/mcp` and `/.well-known/oauth-protected-resource`:**

  ```json
  {"resource": "<origin>/api/mcp", "authorization_servers": ["<origin>"],
   "scopes_supported": ["mcp:read","mcp:write"], "bearer_methods_supported": ["header"]}
  ```

- **`/.well-known/oauth-authorization-server` and `/.well-known/oauth-authorization-server/api/mcp`:**
  - `issuer` = `<origin>`, with endpoint URLs under `/api/oauth/…`;
  - `response_types_supported:["code"]`;
  - `grant_types_supported:["authorization_code","refresh_token"]`;
  - `code_challenge_methods_supported:["S256"]`;
  - `token_endpoint_auth_methods_supported:["client_secret_post","client_secret_basic","none"]`;
  - `revocation_endpoint`;
  - `scopes_supported`;
  - `authorization_response_iss_parameter_supported:true`.
- **Any other `/.well-known/…`:** a JSON 404.
- **`<origin>`** is the request's scheme and Host. Users must enter exactly `http(s)://<host>/api/mcp`: lowercase
  host, no trailing slash, the same address HA uses.

## 5. Data model

There is one migration on top of `7f3d2a91c6e8`. All tables are new and fork-owned.

| Table | Purpose |
|---|---|
| `mcp_oauth_clients` | Registered clients (§4) |
| `mcp_oauth_requests` | Pending authorization requests (10 min) |
| `mcp_oauth_codes` | Authorization codes (hashed, 60 s, single use) |
| `mcp_oauth_tokens` | Access and refresh tokens (hashed, families, revocation) |
| `mcp_api_token_grants` | `long_live_token_id` → `allow_writes` |

A daily scheduler task purges expired codes and requests, and tokens that have been expired or revoked for more than
30 days.

## 6. UI

- **Group Settings → AI Assistants (MCP)** (managers only, `GroupMcpSettings`, `GroupMcpClientDialog`):
  - the MCP Server URL to copy, with a note that API tokens work too (linking to Profile → API Tokens, which needs
    "Show advanced features");
  - the client list (name, confidential or public, read-only or "Can ask to make changes", client ID, redirect URIs,
    last used);
  - Add Client with a **Home Assistant** preset (from `GET /api/groups/mcp/presets/home-assistant`, with an
    optional Home Assistant address for the second redirect URI) or **Other MCP client**; Edit; Rotate Secret;
    Delete. The secret appears once in a panel that stays until **Done**, and Add Client and Rotate Secret wait
    for it.
- **`/oauth/consent`** (`pages/oauth/consent.vue`):
  - "*Home Assistant* wants to use your Mealie", what it can do, an optional "Allow changes (add to shopping list,
    plan meals)" checkbox (unchecked), who you're signed in as with **Switch account**, "You'll be sent back to
    *host*" (punycode), Approve and Deny;
  - signed-out users go through `/login?redirect=…` (password and OIDC) and come back;
  - inside a frame it offers only a link that opens it in a new tab (clickjacking);
  - expired, answered, foreign-group and malformed requests get a plain message and a way back to Mealie.
- **Profile → Connected Apps** (`/user/profile/connected-apps`, `UserMcpConnections`): your OAuth connections, with
  permissions in words, connected and last-used dates, and Disconnect.
- **Profile → API Tokens:** an "Allow AI assistants to make changes" switch per token (`UserMcpApiTokenWriteSwitch`).
- **Development:** Nuxt's dev proxy passes the authorize endpoint's redirect to the browser instead of following
  it (a commented fork edit in `frontend/server/api/[...].ts`), so the flow also works with `task py` + `task ui`.

## 7. Testing

- **MCP contract**, with the SDK client in-process over `httpx.ASGITransport` and the router lifespan entered by hand
  (the `api_client` fixture doesn't run lifespans):
  - initialize, list, call;
  - read-only versus write grants;
  - `isError` mapping;
  - household isolation;
  - the 4 s cap;
  - Origin rejection;
  - 405 on non-POST;
  - the trailing-slash route.
- **HA flow replay**, written independently and following HA's documented behaviour:
  - 401 header, then PRM, then AS metadata races, against the real SPA mount;
  - authorize without PKCE for the HA-preset client;
  - `client_secret_post` token exchange;
  - refresh with rotation;
  - reauth after revocation.
- **OAuth rules:**
  - PKCE required for public clients and for confidential clients without the flag;
  - S256 only;
  - exact redirect matching and the loopback port rule;
  - no redirect on a bad client or redirect URI;
  - code single use, with reuse revoking tokens;
  - refresh reuse revoking the family;
  - password change, user deletion and client deletion all revoking;
  - the audience check;
  - `no-store`.
- **Off the event loop:** auth and tool database work never run on the event loop; at least 24 concurrent calls
  with a small pool timeout succeed (PHASE1 §5 style).
- **End to end, outside CI:** run HA's real client code (the research probes) against a running Mealie.

### As built

All of the above is covered, plus what the reviews added:
- **Concurrency:** concurrent code exchanges and refreshes (exactly one wins), and a disconnect or password change
  racing a refresh. These ran on SQLite and PostgreSQL 16.
- **Password changes** revoke without backdated tokens.
- **Cache:**
  - a user moved to another household is seen at once;
  - `cached_mcp_principal` never touches the database.
- **Authorize limits** and the ASCII redirect-host rule.
- **The endpoint:**
  - the over-size 413;
  - one log line per call, and hostile tool names can't forge log lines;
  - the time budget counted from arrival;
  - the busy limit;
  - write timeouts answering `timeout_pending` and landing exactly once;
  - event failures logged and events sent after the answer;
  - shutdown waiting for running tools;
  - the HA replay going through production's middleware and asserting that the losing discovery responses are JSON
    404s.
- **Frontend:** vitest for the composables, the client dialog (preset, refused address, a click during preset
  loading), the secret panel, the consent page (the write checkbox default, framing, the foreign-group message),
  Connected Apps and the token switch.
- **Outside CI**, each run against a real production-mode Mealie with the SPA:
  - HA's own client code (mcp 1.28.1, probatio) completed discovery, authorize without PKCE, consent, token,
    refresh, the tool list (all 8 schemas converted), reads, a write and the trailing-slash URL.
  - The UI was driven in Chromium: client management, the full consent round trip from signed out, deny,
    expired requests, framing, Connected Apps, the token switch, keyboard use and 375 px.

### Known limitations

- Revocation reaches other worker processes within 60 s; the busy limit is per process.
- A write that times out usually still lands, and a client that retries it anyway creates a duplicate. The answer
  says so; there is no deduplication.
- The consent page's frame guard runs in the browser. Mealie sends no `frame-ancestors` header, because upstream
  allows embedding the app.
- Requests just under 64 KiB can still make the SDK log a few KB of escaped validation detail at WARNING.
- Sub-path installs (Mealie under `/something/`) aren't supported: the MCP URL is always `<origin>/api/mcp`.
