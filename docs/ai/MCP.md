# Connecting AI assistants to Mealie (MCP)

Mealie serves its voice-friendly tools over the [Model Context Protocol](https://modelcontextprotocol.io) (MCP) at
**`/api/mcp`**. An MCP client (Home Assistant's voice assistants, Claude Code, Claude Desktop and others) can then
search your recipes, read a recipe out step by step, check the meal plan and shopping list, and, if you allow it, add
to the shopping list and plan meals.

- [1. The tools](#1-the-tools)
- [2. The MCP server URL](#2-the-mcp-server-url)
- [3. Home Assistant](#3-home-assistant)
- [4. Claude Code](#4-claude-code)
- [5. Claude apps and other clients](#5-claude-apps-and-other-clients)
- [6. Permissions](#6-permissions)
- [7. Removing access](#7-removing-access)
- [8. Troubleshooting](#8-troubleshooting)
- [9. How it works](#9-how-it-works)

The design and the research behind it are in [`PHASE3.md`](PHASE3.md).

---

## 1. The tools

| Tool | What it does | Changes data |
|---|---|---|
| `search_recipes` | Find recipes by words, total time (`max_total_minutes`), foods to include or exclude, tags and categories | No |
| `get_recipe` | A recipe's summary, ingredients, steps or everything, with ingredients scaled to any number of `servings` | No |
| `get_cooking_step` | One step of a recipe, for reading out while you cook | No |
| `suggest_from_ingredients` | Recipes that use the foods you have, best matches first | No |
| `whats_planned` | The meal plan for a day or a range, optionally one meal | No |
| `get_shopping_list` | The open items on a shopping list | No |
| `add_to_shopping_list` | Add items, or a recipe's ingredients (scaled), to a shopping list | **Yes** |
| `plan_meal` | Put a recipe or a note on the meal plan | **Yes** |

Every result starts with a short `speech` field (at most two sentences) that a voice assistant can read out as it
is, followed by the data a model needs for follow-up questions (slugs, step counts and so on).

The tools act as the Mealie user who approved the connection (or who owns the API token), and see that user's
household only. No tool deletes or overwrites anything. The two tools that change data are hidden unless the
connection is allowed to make changes ([section 6](#6-permissions)).

The same tools are also available over REST at `GET /api/ai/tools` and `POST /api/ai/tools/{name}`, for scripts.

---

## 2. The MCP server URL

Group managers find it under **Group Settings > AI Assistants (MCP)**. It is:

```text
http(s)://<the address you use for Mealie>/api/mcp
```

Enter it **exactly** as Mealie shows it:

- the same scheme (`https://` or `http://`) and port as you use to reach Mealie;
- a lowercase host name;
- **no trailing slash**.

Clients that use OAuth (Home Assistant among them) compare the address you typed with the one Mealie reports for
itself, character for character, and refuse to connect if they differ. Mealie reports the address it was reached
at, so if Home Assistant reaches Mealie by IP address, use the IP address here too.

**Use HTTPS** whenever Mealie is reachable from outside your network. Over plain HTTP, tokens cross the network
unencrypted.

**Behind a reverse proxy that handles HTTPS,** set Mealie's `BASE_URL` to its public address (for example
`BASE_URL=https://mealie.example.com`). The Docker image trusts the proxy's `X-Forwarded-Proto` header only from the
container's gateway, so with a proxy in another container Mealie would otherwise think it's reached over plain HTTP.
A plain-HTTP request for an `https` `BASE_URL`'s host is treated as HTTPS.

---

## 3. Home Assistant

Home Assistant (HA) connects with its built-in **Model Context Protocol** integration and OAuth: you register HA in
Mealie, enter its client ID and secret in HA, and approve the connection in your browser. The step-by-step guide is
[Layer B in the Home Assistant guide](../home-assistant/README.md#8-layer-b-mealies-mcp-server).

---

## 4. Claude Code

Claude Code connects with a Mealie API token in a header (quickest) or with OAuth (the token never leaves Claude
Code's credential store, and reaches only the tools).

### With an API token

1. Sign in as the user Claude should act as and open **Profile > API Tokens**. Create a token and copy it. To let
   Claude add to the shopping list and plan meals, switch on **Allow AI assistants to make changes** for that token.
2. Add the server:

   ```sh
   claude mcp add --transport http mealie https://mealie.example.com/api/mcp \
     --header "Authorization: Bearer <API token>"
   ```

   To share the setup in a project's `.mcp.json` without committing the token, read it from an environment variable:

   ```json
   {
     "mcpServers": {
       "mealie": {
         "type": "http",
         "url": "https://mealie.example.com/api/mcp",
         "headers": { "Authorization": "Bearer ${MEALIE_TOKEN}" }
       }
     }
   }
   ```

A Mealie API token works on the whole Mealie API, not just the tools, so keep it as safe as a password.

### With OAuth

1. In Mealie, open **Group Settings > AI Assistants (MCP)** and add a client:
   - **Name:** `Claude Code`;
   - **Redirect URI:** `http://localhost/callback`. Claude Code listens on a free port each time; Mealie matches
     `http://localhost` redirect URIs on any port, so this one URI is enough;
   - leave **PKCE optional** off;
   - turn on **Allow changes** if Claude may add to the shopping list and plan meals.

   Save, then copy the client ID and secret. The secret is shown only once.
2. Add the server. Claude Code asks for the secret (or reads it from `MCP_CLIENT_SECRET`) and keeps it in your
   system's credential store:

   ```sh
   claude mcp add --transport http --client-id <client ID> --client-secret \
     mealie https://mealie.example.com/api/mcp
   ```

3. In Claude Code, run `/mcp`, pick `mealie` and choose **Authenticate**. Your browser opens Mealie's consent page.
   Sign in as the user Claude should act as, tick **Allow changes** if you want to, and select **Approve**.

Claude Code's reference: [Connect Claude Code to tools via MCP](https://code.claude.com/docs/en/mcp).

---

## 5. Claude apps and other clients

### Claude (web, Desktop and mobile apps)

Claude's apps add MCP servers as **custom connectors** (**Customize > Connectors > Add custom connector**). Claude
connects to them from Anthropic's cloud, so **Mealie must be reachable from the internet over HTTPS**. A Mealie
that's only on your home network can't be added this way; use Claude Code instead (its **Code** tab in Claude
Desktop works the same way as the CLI).

With OAuth (recommended):

1. In Mealie, open **Group Settings > AI Assistants (MCP)** and add a client named `Claude` with the redirect URI
   `https://claude.ai/api/mcp/auth_callback`. Make it confidential (or public, if you'd rather not store a secret in
   Claude), leave **PKCE optional** off, and turn on **Allow changes** if Claude may make changes.
2. In Claude, add a custom connector with the MCP server URL. Under authentication choose **Use your own OAuth
   client** and enter the client ID, and the secret if the client has one.
3. Connect, sign in to Mealie as the user Claude should act as, and select **Approve**.

Claude can also send a fixed **Request header** (`authorization: Bearer <API token>`) instead, where your plan
offers that option.

Anthropic's references: [custom connectors](https://claude.com/docs/connectors/custom/add-unlisted) and
[connector authentication](https://claude.com/docs/connectors/building/authentication).

### Other MCP clients

Any client that speaks **Streamable HTTP** can connect:

- with a header `Authorization: Bearer <Mealie API token>`; or
- with OAuth and a client you register in Mealie: authorization code, PKCE S256, `client_secret_post`,
  `client_secret_basic` or no secret for a public client. Clients find Mealie's OAuth endpoints themselves from the
  `WWW-Authenticate` header of the first `401` (RFC 9728 and RFC 8414).

Mealie doesn't offer dynamic client registration. A client that insists on it can't use OAuth with Mealie; give it
an API token header instead.

---

## 6. Permissions

A connection can **read** (every read tool) or **read and change** (the two write tools as well). It's read-only
unless you allow changes:

| Connection | How to allow changes | Default |
|---|---|---|
| OAuth client (Home Assistant, Claude with a client ID) | A group manager turns on **Allow changes** for the client in **Group Settings > AI Assistants (MCP)**. The user then ticks **Allow changes** on the consent page when connecting. | Off |
| Mealie API token | **Profile > API Tokens**, switch **Allow AI assistants to make changes** on that token | Off |

Without that permission the write tools don't appear in the client's tool list at all, and calling one anyway
returns an error saying changes aren't allowed for this connection.

**Use a dedicated user.** A connection acts as the user who approved it. Create a non-admin Mealie user such as
`Kitchen Voice` in the household you want the assistant to see, and connect as that user. Anything the assistant
adds then shows up as done by `Kitchen Voice`, and you can disconnect it without touching anyone's own account
([Home Assistant guide, section 1](../home-assistant/README.md#use-a-dedicated-kitchen-voice-user-for-the-token)).

**MCP tokens only reach the tools.** OAuth access tokens issued for MCP are not Mealie session tokens: the rest of
Mealie's API refuses them. Mealie API tokens can reach the whole API as before, so prefer OAuth where the client
supports it.

To give an existing OAuth connection permission to make changes, turn on **Allow changes** for its client, then
disconnect it ([section 7](#7-removing-access)) and connect again, ticking **Allow changes** on the consent page.

---

## 7. Removing access

| To | Do this |
|---|---|
| Disconnect one app from your account | **Profile > Connected Apps**, then **Disconnect** |
| Cut off a client for everyone in the group | **Group Settings > AI Assistants (MCP)**, delete the client. All its tokens stop working. |
| Replace a leaked client secret | **Rotate Secret** on the client, then enter the new secret in the app |
| Stop an API token | Delete it under **Profile > API Tokens** |

Changing your password also ends all of your MCP connections. Each app then needs to be connected again.

All of these take effect at once. The exception is a Mealie run with several worker processes
(`UVICORN_WORKERS` or `WORKER_PER_CORE` above 1): each worker remembers a checked token for up to **60 seconds**, so
a connection can keep working for up to a minute after you remove it.

---

## 8. Troubleshooting

| Symptom | Check |
|---|---|
| HA: "OAuth resource metadata is invalid" or "Failed to connect" | The address you typed differs from the one in **Group Settings > AI Assistants (MCP)**: a trailing slash, capitals in the host, `http` instead of `https`, or a missing port. Behind a reverse proxy, set `BASE_URL` ([section 2](#2-the-mcp-server-url)). |
| Mealie shows "This app can't connect to Mealie" instead of the consent page | The page says why. "Asked to return to an address that isn't registered": edit the client and add the app's exact redirect URI (Mealie never redirects to an unregistered address). "Isn't registered with Mealie": the client ID is wrong, or the client was deleted. |
| The app reports `invalid_client` | Wrong client ID or secret, or the secret was rotated. |
| The app reports `invalid_grant` when refreshing | The connection was removed, the user's password changed, or the refresh token was used twice (Mealie then revokes the whole connection as a precaution). Connect again. |
| The write tools are missing | The connection is read-only ([section 6](#6-permissions)). |
| The assistant sees the wrong meal plan or shopping list | It's connected as a user in another household. Disconnect and connect again as the right user. |
| "Mealie took too long to answer. Try again." | Each tool call has 4 seconds, because Home Assistant gives up after 5. Check Mealie's load and database. |
| "Mealie is still saving that, and it may still go through." | A change (adding to the shopping list, planning a meal) took longer than 4 seconds. It usually still lands, so check the list or plan before asking again. |
| "Mealie is busy right now. Try again in a moment." | Too many tool calls are running at once (16 per Mealie process). |
| 403 from `/api/mcp` | The request carried an `Origin` header from another site. MCP clients don't send one; browsers do. |
| 413 from `/api/mcp` | The request was over 64 KB. No tool call needs that much. |
| An app keeps working right after you removed it | With several worker processes, allow up to 60 seconds ([section 7](#7-removing-access)). |

Mealie logs each tool call at `INFO` level as `MCP tool <name> for user <user id> via <client>: <outcome> (<ms> ms)`.
Mealie doesn't log tokens or tool arguments.

---

## 9. How it works

```text
MCP client ──POST /api/mcp──► bearer check ──► MCP server ("Mealie") ──► tool registry ──► recipes, plans, lists
                                   │
                                   └─ no or bad token: 401 + WWW-Authenticate (where to find the OAuth metadata)

/.well-known/oauth-protected-resource      which authorization server protects /api/mcp (RFC 9728)
/.well-known/oauth-authorization-server    Mealie's OAuth endpoints (RFC 8414)
/api/oauth/authorize ──► /oauth/consent (sign in, approve) ──► back to the app with a code
/api/oauth/token, /api/oauth/revoke
```

- **Transport:** Streamable HTTP, stateless, JSON responses. Every request is a `POST`; anything else gets 405.
  Requests with an `Origin` header from another site get 403. That stops ordinary cross-site requests; the real
  protection against browser attacks such as DNS rebinding is that only an `Authorization` header is accepted,
  never cookies.
- **Tokens accepted:** OAuth access tokens issued by Mealie for `/api/mcp`, and Mealie API tokens. Browser sessions
  are refused. Without a token the `401` carries
  `WWW-Authenticate: Bearer resource_metadata="<origin>/.well-known/oauth-protected-resource/api/mcp", scope="mcp:read mcp:write"`;
  with a bad one it also carries `error="invalid_token"`.
- **Results:** one text block of compact JSON, `speech` first. Failures come back as tool errors (`isError`) with a
  `speech` sentence and an `error` code: `unknown_tool`, `invalid_arguments` (with the validation errors),
  `not_found`, `tool_error`, `write_not_allowed`, `timeout`, `timeout_pending` (a change that may still land),
  `busy` or `internal_error`.
- **Events:** changes made through MCP send the same notifications and webhooks as changes in the app, with the
  integration ID `mcp:<client name>` (or `mcp:API token`), so automations can tell them apart.
- **OAuth:** authorization code with PKCE (S256). PKCE can be made optional only for a confidential client, which
  Home Assistant needs because it doesn't send PKCE. Access tokens last an hour. Refresh tokens rotate on every use
  and expire after 90 days without use. Tokens and secrets are stored only as hashes.
- **Redirect URIs** match exactly. The exception is `http://127.0.0.1`, `http://localhost` and `http://[::1]`
  addresses, which match on any port (for desktop apps that pick a free port), as long as the host and path match.
  Plain `http://` is only accepted for local-network addresses, and host names must be ASCII (use the `xn--` form
  for international names), so the consent page can't show a lookalike host.
