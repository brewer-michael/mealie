# Phase 3: Mealie MCP server (design)

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
HA / Claude ──POST /api/mcp──► McpBearerAuth (ASGI) ──► StreamableHTTPSessionManager ──► mcp Server
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
  - Calls are capped at **4 s**, so HA's 5 s per-POST budget holds. On timeout the result is a speakable
    `isError` ("Mealie took too long to answer. Try again.").
  - **Result:** a single `TextContent` with compact JSON of the tool result (`speech` first, then the data). No
    `structuredContent` or `outputSchema` for now.
  - **Errors:** unknown tool, `ToolNotFoundError`, `ToolError`, pydantic validation errors and missing write grants
    all come back as `isError: true` with the speakable message, never as JSON-RPC or HTTP errors.
  - Write tools publish the same events as REST, with `integration_id = "mcp:<client name>"`.
- **Origin:** if an `Origin` header is present and isn't the request's own origin, the endpoint returns **403**, as
  the spec requires against DNS rebinding.

## 2. Bearer authentication

`McpBearerAuth` is an ASGI wrapper in front of the session manager.

- **Accepted tokens:**
  1. **MCP OAuth access tokens**: opaque, with prefix `mmcp_at_`, looked up by SHA-256 in `mcp_oauth_tokens`. They
     must be unexpired and unrevoked, and their `resource` must be this server's `/api/mcp` URL (audience check).
  2. **Mealie long-lived API tokens**: the JWTs from Profile → API Tokens, validated exactly as upstream's
     `validate_long_live_token` does.
- **Rejected:** session JWTs and cookies. A browser session can't drive MCP.
- **Lookups run in a worker thread** (`run_in_threadpool` + `session_context()`), never on the event loop.
- **Cache:** results are cached for 60 s, keyed by the token's SHA-256, because HA authenticates 4 POSTs per call. A
  revocation can therefore take up to 60 s to apply; this is documented.
- **Failure:** `401` with
  `WWW-Authenticate: Bearer error="invalid_token", resource_metadata="<origin>/.well-known/oauth-protected-resource/api/mcp", scope="mcp:read mcp:write"`.
  - `<origin>` comes from the request's scheme and Host. Uvicorn already trusts `X-Forwarded-Proto` from configured
    proxies.
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
on any port (RFC 8252 §7.3), for Claude Code.

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
- the user changes their password (upstream's `tokens_valid_after` is honoured at verification);
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

- **Group Settings → AI assistants (MCP):**
  - the MCP URL to copy;
  - the client list (name, client ID, redirect URIs, writes allowed, last used);
  - add a client (with the Home Assistant preset); edit; rotate secret (shown once); delete.
- **`/oauth/consent`:**
  - "*Home Assistant* wants to use your Mealie: read recipes, meal plans and shopping lists", plus an optional
    "Allow changes" checkbox;
  - Approve / Deny;
  - the redirect host is shown, so users can spot a lookalike.
- **Profile → Connected apps:** your OAuth connections, with scopes and last use, and Disconnect.
- **Profile → API Tokens:** an "Allow AI assistants to make changes" switch per token.

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
