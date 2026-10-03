# Home Assistant voice for Mealie

Ask Home Assistant (HA) Assist about your Mealie recipes, meal plan and shopping list. There are two layers
([`AI_INTEGRATION_PLAN.md` §4.3](../AI_INTEGRATION_PLAN.md#43-home-assistant-and-voice-g1)), and they work side by side:

- **Layer A** (sections 1–7) needs no Mealie code: everything runs on HA's built-in Mealie integration.
- **Layer B** ([section 8](#8-layer-b-mealies-mcp-server)) connects HA's MCP integration to this Mealie build's MCP
  server, which gives the LLM richer tools: search by time and ingredients, scaled ingredients, one cooking step at a
  time, and optionally adding to the shopping list and planning meals.

**Layer A:**

- Simple questions ("What's for dinner?") are answered **locally**, with no LLM, in well under a second.
- Open-ended questions ("Find me a chicken recipe") go to an **LLM conversation agent**, which gets three Mealie
  scripts as tools.

| File | Copy to | What it does |
|---|---|---|
| [`packages/mealie_voice.yaml`](packages/mealie_voice.yaml) | `<config>/packages/mealie_voice.yaml` | Settings script, two local intent handlers, three LLM tool scripts |
| [`custom_sentences/en/mealie.yaml`](custom_sentences/en/mealie.yaml) | `<config>/custom_sentences/en/mealie.yaml` | English sentences for the local intents |

- [1. Requirements](#1-requirements)
- [2. Install](#2-install)
- [3. Set up Assist](#3-set-up-assist)
- [4. What you can say](#4-what-you-can-say)
- [5. Settings and the Mealie config entry](#5-settings-and-the-mealie-config-entry)
- [6. How it works](#6-how-it-works)
- [7. Troubleshooting](#7-troubleshooting)
- [8. Layer B: Mealie's MCP server](#8-layer-b-mealies-mcp-server)

---

## 1. Requirements

- **Home Assistant 2026.9 or newer.** Tested on 2026.9.4 and 2026.10.0b0.
- **Mealie 3.2.0 or newer.** HA 2026.10 raises the Mealie integration's minimum from v2.0.0 to v3.2.0 and refuses to
  set up against anything older. This branch is v3.28.0.
- **HA's Mealie integration**, set up with a Mealie API token (**Settings > Devices & services > Add integration >
  Mealie**: URL, API token, verify SSL).
- **For the LLM part only:** an LLM conversation agent (OpenAI, Anthropic, Google Generative AI, Ollama and so on) with
  **Control Home Assistant** set to **Assist** in its options. The local sentences work without one.

### Use a dedicated "Kitchen Voice" user for the token

The API token acts as the user who created it, so create a Mealie user just for HA rather than using your own:

1. In Mealie, go to **Admin > Manage Users** (`/admin/manage/users`) and create a user such as `Kitchen Voice`, in
   the **same household** as the meal plans and shopping lists you want to hear about. Don't make it an admin.
2. Sign in as that user and open **Profile > API Tokens** (`/user/profile/api-tokens`). Create a token named
   `Home Assistant` and copy it.
3. Use that token when you add the Mealie integration in HA.

Meal plans and shopping lists belong to a household in Mealie, so HA sees the token user's household only. Anything
added by voice (such as shopping list items) shows up as done by Kitchen Voice, and you can revoke the token without
touching anyone's account.

---

## 2. Install

1. Turn on packages in `configuration.yaml`, if you haven't already:

   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```

2. Copy the two files into your HA config directory (the folder that holds `configuration.yaml`):

   ```text
   <config>/packages/mealie_voice.yaml
   <config>/custom_sentences/en/mealie.yaml
   ```

3. Check the configuration (**Developer tools > YAML > Check configuration**), then restart HA.

After later edits you don't need to restart: run `script.reload` and `intent_script.reload` for the package, and
`conversation.reload` for the sentences (**Developer tools > Actions**).

The package adds four scripts: `script.mealie_voice_settings`, `script.mealie_search_recipes`,
`script.mealie_get_recipe` and `script.mealie_get_meal_plan`.

---

## 3. Set up Assist

### Expose the LLM tool scripts

1. Go to **Settings > Voice assistants > Expose** and select **Expose entities**.
2. Search for `Mealie:` and expose **Mealie: search recipes**, **Mealie: get recipe** and **Mealie: get meal plan**.
3. Don't expose **Mealie voice: settings**. It's an internal helper.

HA builds each LLM tool from the script's `description` and `fields`, so the tools appear to the model as
`script__mealie_search_recipes`, `script__mealie_get_recipe` and `script__mealie_get_meal_plan`.

Mealie's to-do lists are exposed by default, which also gives the LLM HA's own `todo__get_items` and list-editing
tools. Exposing the Mealie calendars (`calendar.mealie_dinner` and so on) adds HA's `calendar__get_events` tool,
which is optional because `script.mealie_get_meal_plan` covers the same ground.

### Prefer local answers

1. Go to **Settings > Voice assistants** and open your assistant.
2. Under **Conversation agent**, pick your LLM agent and turn on **Prefer handling commands locally**.

With this on, Assist tries HA's local sentences (including the ones in this package) first and only sends the request
to the LLM when nothing matches. "What's for dinner?" is then instant and free, and "Find a quick soup" still reaches
the LLM. With the built-in **Home Assistant** agent and no LLM, only the local sentences work.

### Optional: let "add eggs to the shopping list" reach Mealie

Adding items already works through HA's built-in `HassListAddItem` intent, but only by the list's full name ("add
eggs to the Mealie Supermarket list"). To make "the shopping list" mean your Mealie list:

1. Go to **Settings > Entities**, open the Mealie to-do entity (for example `todo.mealie_supermarket`), then
   **Voice assistants > Aliases**.
2. Add the alias `Shopping list`.

"Add eggs to the shopping list" and "put milk on my shopping list" then go to that Mealie list, handled locally.

### Suggested LLM instructions

Add something like this to the conversation agent's instructions:

```text
Use the Mealie tools for anything about food, recipes, cooking or the meal plan.
Search with one or two food words. Keep spoken answers to two sentences.
When reading a recipe, give one step at a time unless asked for more.
```

---

## 4. What you can say

### Local (no LLM)

| You say | Assist answers (example) |
|---|---|
| "What's for dinner?" | "Dinner today is Zoete aardappel curry traybake and Cheeseburger Sliders." |
| "What's for lunch tomorrow?" / "What is for lunch today?" | "Lunch tomorrow is Chicken curry and Boeuf bourguignon." |
| "What are we having for supper tonight?" | "Dinner today is Aquavite." (a note entry) |
| "What's for dessert on Friday?" | "Nothing is planned for dessert on Friday." |
| "What's on the menu today?" / "What's on the meal plan tomorrow?" | "Today: lunch is All-American Beef Stew; dinner is ...; snack is Mousse de saumon." |
| "What's on the shopping list?" / "Read me the grocery list" / "What do we need from the store?" | "4 items on the Supermarket list: 2 Apples, 1 can acorn squash, aubergine and 1 US cup flour." |
| "Add eggs to the shopping list" | HA's built-in intent (see the alias step above) |

Meals: breakfast (or brunch), lunch, dinner (or supper), dessert, snack, side, drink. Days: today, tonight, tomorrow,
tomorrow night, or a weekday ("on Saturday" means the next Saturday, or today if it's Saturday). Without a day, the
answer is for today. Long shopping lists stop after 8 items with "and N more".

The full sentence list is in [`custom_sentences/en/mealie.yaml`](custom_sentences/en/mealie.yaml).

### Through the LLM

| You say | Tools the agent typically calls |
|---|---|
| "Find a chicken recipe" | `mealie_search_recipes(query="chicken")` |
| "What do I need for the Sacher torte?" | `mealie_search_recipes`, then `mealie_get_recipe(recipe=<slug>)` |
| "What's step 3?" / "How much flour?" (follow-up) | `mealie_get_recipe` |
| "What's planned for next week?" | `mealie_get_meal_plan(start_date, end_date)` |
| "Is anything on this week's plan vegetarian?" | `mealie_get_meal_plan`, then `mealie_get_recipe` per entry |

What the scripts return (trimmed so the model gets only what it needs):

```jsonc
// script.mealie_search_recipes {"query": "sweet", "limit": 3}
{"query": "sweet", "count": 1, "recipes": [
  {"name": "Sweet potatoes", "slug": "sweet-potatoes", "description": "Régalez vous avec ces patates douces ...", "total_time": ""}]}

// script.mealie_get_recipe {"recipe": "original-sacher-torte-2"}
{"name": "Original Sacher-Torte (2)", "slug": "original-sacher-torte-2", "description": "...", "servings": "4 servings",
 "total_time": "2 hours 30 minutes", "prep_time": "...", "cook_time": "...",
 "ingredients": ["1 130g dark couverture chocolate (min. 55% cocoa content)", "..."],
 "steps": [{"step": 1, "text": "Preheat oven to 170°C. ..."}, "..."]}

// script.mealie_get_meal_plan {"start_date": "2024-01-21", "end_date": "2024-01-23"}
{"start_date": "2024-01-21", "end_date": "2024-01-23", "entries": [
  {"date": "2024-01-21", "meal": "dinner", "title": "Aquavite", "recipe_slug": "", "note": "Dineren met de boys"},
  {"date": "2024-01-22", "meal": "lunch", "title": "All-American Beef Stew Recipe", "recipe_slug": "all-american-beef-stew-recipe", "note": ""}]}
```

Mealie's search matches words in recipe names, descriptions and ingredients, and any single word is enough. It
doesn't understand meaning or cooking times, so the search tool's description tells the model to send one or two
food words. Semantic search and time filters arrive with Layer B.

---

## 5. Settings and the Mealie config entry

Every Mealie action needs the ID of the Mealie config entry. The package reads all of its settings from one place,
the top of `packages/mealie_voice.yaml`:

```yaml
  mealie_voice_settings:
    ...
          settings:
            config_entry_id: >-
              {%- set entities = integration_entities('mealie') | sort -%}
              {{ config_entry_id(entities | first) if entities else '' }}
            shopping_list: >-
              ...
            max_spoken_items: 8
```

| Setting | Default | Change it when |
|---|---|---|
| `config_entry_id` | Found from any Mealie entity with HA's `integration_entities()` and `config_entry_id()` template functions | You have more than one Mealie instance |
| `shopping_list` | The Mealie to-do list whose entity ID contains `shopping` or `grocer`, otherwise the first alphabetically | "What's on the shopping list" reads the wrong list. Set it to an entity ID such as `todo.mealie_supermarket` |
| `max_spoken_items` | `8` | Shopping list answers are too long or too short |

To find a config entry ID, paste this into **Developer tools > Template**:

```jinja
{% for id in integration_entities('mealie') | map('config_entry_id') | unique %}
{{ id }}: {{ config_entry_attr(id, 'title') }}
{% endfor %}
```

Then replace the `config_entry_id` template with the ID as a plain string, for example
`config_entry_id: "01J0BC4QM2YBRP6H5G933CETT7"`, and run `script.reload`. Another way: in **Developer tools >
Actions**, pick **Mealie: Get mealplan**, choose the instance in the UI, then switch to YAML mode to see its
`config_entry_id`.

---

## 6. How it works

```text
"What's for dinner?" ──► Assist ──► custom sentence match ──► intent_script MealieWhatsForMeal
                                       (local, no LLM)              └─► mealie.get_mealplan ──► spoken answer

"Find a chicken recipe" ──► no local match ──► LLM agent ──► script.mealie_search_recipes
                                                              └─► mealie.get_recipes ──► trimmed JSON ──► LLM answer
```

- **Local intents.** The sentences file defines `MealieWhatsForMeal` (slots `meal` and `day`) and
  `MealieShoppingList`. The `intent_script` handlers call a Mealie action, build a short sentence, and return it with
  `stop` + `response_variable`. The speech template reads it from `action_response`. If Mealie can't be reached, the
  action step has `continue_on_error`, and Assist says "Sorry, I couldn't reach Mealie." instead of a generic error.
- **Meal plan source.** The meal plan answers use `mealie.get_mealplan` rather than `calendar.get_events` on the
  Mealie calendars. One call returns every meal type, with stable fields (`entry_type`, `recipe.name`, `title`). It
  doesn't depend on calendar entity IDs, which change when entities are renamed and are translated in non-English
  installs (`calendar.mealie_abendessen`). It also reads Mealie live, where the calendars come from an hourly poll.
- **Shopping list source.** `mealie.get_shopping_list_items` reads the list live from Mealie and includes each item's
  `checked` flag, so only open items are read out. `todo.get_items` would read HA's copy, refreshed every 5 minutes.
- **Hidden from the LLM.** Both intent scripts set `platforms: [mealie]`. HA offers an `intent_script` to an LLM only
  when an exposed entity belongs to one of its `platforms`, and no entity has the `mealie` domain, so the LLM sees
  only the three scripts. Without this, the intents would show up as LLM tools that can't take a meal or a day.
- **LLM tools.** Each script calls one Mealie action, keeps only the fields a voice answer needs, and returns them
  with `stop` + `response_variable`. A Mealie error (for example "Recipe with ID or slug `x` not found") goes back to
  the model as the tool's error, so it can retry or tell you.

Template tip if you extend the package: Mealie action responses are plain dicts, so read lists with
`response['items']`, not `response.items` (that's the dict's `items()` method).

---

## 7. Troubleshooting

| Symptom | Check |
|---|---|
| "Sorry, I couldn't reach Mealie." | Is the Mealie integration loaded (**Settings > Devices & services > Mealie**)? On HA 2026.10+, Mealie older than 3.2.0 fails with "Minimum required version is v3.2.0". Is the token still valid? The HA log has the exact error from the `Intent Script MealieWhatsForMeal` run. |
| "Sorry, I couldn't read the shopping list from Mealie." | The token user's household has no shopping list, or `shopping_list` in the settings names an entity that doesn't exist. |
| "Nothing is planned..." but Mealie shows entries | The token user is in a different household from the meal plan. Also check that HA's time zone (**Settings > System > General**) matches yours, because "today" is HA's today. |
| Assist doesn't understand "What's for dinner?" | The sentences file must be at `<config>/custom_sentences/en/mealie.yaml` and the assistant's language must be English. Run `conversation.reload`. **Developer tools > Assist** shows how a sentence is parsed; a match from this package lists `MealieWhatsForMeal` with source `custom`. |
| The LLM answers "What's for dinner?" instead of the local intent | Turn on **Prefer handling commands locally** for the assistant. |
| The LLM doesn't use the Mealie tools | Are the three scripts exposed? Is **Control Home Assistant** set to **Assist** in the LLM agent's options? Add the suggested instructions from [section 3](#suggested-llm-instructions). |
| Several Mealie instances, wrong one used | Set `config_entry_id` in the settings script ([section 5](#5-settings-and-the-mealie-config-entry)). |
| A script fails after an edit | Open **Settings > Automations & scenes > Scripts**, then the script's **Traces**. Intent scripts write their errors to the HA log. |

---

## 8. Layer B: Mealie's MCP server

Layer B connects HA's built-in **Model Context Protocol** integration to this Mealie build's MCP server at `/api/mcp`.
Your LLM conversation agent then gets Mealie's own tools instead of the three Layer A scripts:

| Tool | What it adds over Layer A |
|---|---|
| `search_recipes` | Filters: total time ("under 30 minutes"), foods to include or leave out, tags and categories |
| `get_recipe` | Ingredients scaled to any number of servings |
| `get_cooking_step` | One step at a time, for cooking along |
| `suggest_from_ingredients` | "What can I make with chicken and rice?" |
| `whats_planned` | The plan for a day or a range, optionally one meal |
| `get_shopping_list` | The open items on a list |
| `add_to_shopping_list` | Add items, or a recipe's ingredients for N servings (only if you allow changes) |
| `plan_meal` | Put a recipe or a note on the plan (only if you allow changes) |

Each tool answers with a short `speech` sentence the assistant can read out, plus the data for follow-up questions.
The full reference, including Claude Code and other clients, is [`docs/ai/MCP.md`](../ai/MCP.md).

Keep Layer A installed: its local sentences still answer "What's for dinner?" instantly and without an LLM. Once
Layer B works, you can un-expose the three Layer A scripts (**Settings > Voice assistants > Expose**) so the LLM
doesn't see two ways of doing the same thing.

### Requirements

- **This Mealie build** (`ai-integration`). Stock Mealie has no MCP server.
- **Home Assistant 2026.9 or newer** with an LLM conversation agent.
- **One address for Mealie that works both from HA and from your browser.** You approve the connection in your
  browser, and HA then calls Mealie at the same address. Prefer HTTPS: HA verifies certificates, so a self-signed
  certificate needs a CA that HA trusts.
- **A Mealie user for HA**, such as the `Kitchen Voice` user from [section 1](#use-a-dedicated-kitchen-voice-user-for-the-token).
  The connection acts as the user who approves it, and sees that user's household.

### Step 1: register Home Assistant in Mealie

1. Sign in to Mealie as a group manager and open **Group Settings > AI Assistants (MCP)** (from your profile page,
   or `/group`).
2. Copy the **MCP Server URL**, for example `https://mealie.example.com/api/mcp`.
3. Select **Add Client**. The **Home Assistant** preset is selected and fills in:
   - **Redirect URIs:** `https://my.home-assistant.io/redirect/oauth` (used when HA's `my` integration is on, which is
     the default) and `http://homeassistant.local:8123/auth/external/callback`. If you turned `my` off and HA isn't at
     `homeassistant.local:8123`, enter its address under **Home Assistant Address** first, for example
     `http://192.168.1.20:8123`.
   - **Confidential** with **PKCE optional**: HA always sends a client secret and never sends PKCE.
   - **Allow changes** off. Turn it on if voice may add to the shopping list and plan meals.
4. Select **Create**, then copy the **Client ID** and **Client Secret** from the panel that appears, and select
   **Done**. The secret is shown only once. If you lose it, use **Rotate Secret** and enter the new one in HA.

### Step 2: add the credentials to HA

1. In HA, go to **Settings > Devices & services**, open the **⋮** menu at the top right and choose
   **Application credentials**.
2. Select **Add application credential**, pick **Model Context Protocol**, name it `Mealie`, and paste the client ID
   and secret from step 1.

If you skip this step, HA asks for the credentials when you add the integration in step 3.

### Step 3: add the MCP integration

1. Go to **Settings > Devices & services > Add integration** and choose **Model Context Protocol**.
2. Enter the MCP server URL **exactly as Mealie shows it**: the same `https://` or `http://`, a lowercase host, the port
   if there is one, and **no trailing slash**. HA compares it with the address Mealie reports, character for
   character.
3. HA sends you to Mealie's consent page. Sign in as **Kitchen Voice** (use a private window if you're signed in as
   yourself). Check that the page names Home Assistant and says you'll be sent back to `my.home-assistant.io` (or
   your HA address). Tick **Allow changes** only if you want voice to change things, then select **Approve**.
4. HA adds an entry named **Mealie**.

### Step 4: give the tools to your conversation agent

1. Go to **Settings > Devices & services**, open your LLM integration (OpenAI, Anthropic, Google, Ollama...) and
   configure its conversation agent.
2. Under **Control Home Assistant**, select **Mealie**. Keep **Assist** selected too if the agent should still
   control devices and use the Layer A scripts. With more than one selected, HA names the tools
   `mealie__search_recipes` and so on.
3. Optionally add instructions like these:

   ```text
   Use the Mealie tools for anything about food, recipes, cooking, the meal plan or the shopping list.
   Read the speech field of a Mealie result aloud as it is. Give one cooking step at a time unless asked for more.
   ```

### What you can say

| You say | Tools the agent typically calls |
|---|---|
| "Find a chicken recipe under 30 minutes" | `search_recipes(query="chicken", max_total_minutes=30)` |
| "Something vegetarian without mushrooms" | `search_recipes(tags=["vegetarian"], exclude_foods=["mushroom"])` |
| "What do I need for the lasagna, for eight people?" | `search_recipes`, then `get_recipe(slug, part="ingredients", servings=8)` |
| "What's the next step?" | `get_cooking_step(slug, step=4)` |
| "What can I make with chicken and rice?" | `suggest_from_ingredients(foods=["chicken", "rice"])` |
| "What's planned this week?" | `whats_planned(start, end)` |
| "Add the lasagna ingredients to the shopping list" | `add_to_shopping_list(recipe_slug, servings)` (changes) |
| "Put the lasagna on Friday's dinner" | `plan_meal(date, meal="dinner", recipe_slug)` (changes) |

### Allowing changes later

1. In **Group Settings > AI Assistants (MCP)**, edit the Home Assistant client and turn on **Allow changes**.
2. Sign in to Mealie as Kitchen Voice and disconnect Home Assistant under **Profile > Connected Apps**.
3. On its next request HA finds its access gone and asks you to re-authenticate the Mealie entry (look under
   **Settings > Devices & services**). Approve again with **Allow changes** ticked.

### Troubleshooting Layer B

| Symptom | Check |
|---|---|
| "OAuth resource metadata is invalid" or "Failed to connect" | The address typed in HA differs from the one Mealie shows: a trailing slash, capitals, `http` instead of `https`, or a missing port. Behind a reverse proxy, set Mealie's `BASE_URL` to its public address ([MCP guide, section 2](../ai/MCP.md#2-the-mcp-server-url)). |
| Mealie shows "This app can't connect to Mealie" | HA's redirect URI isn't registered on the client. HA uses `https://my.home-assistant.io/redirect/oauth` when its `my` integration is on, otherwise `<HA address>/auth/external/callback`. |
| HA asks for credentials again and again | The client ID or secret in **Application credentials** is wrong, or the secret was rotated. |
| The agent doesn't use the Mealie tools | Select **Mealie** under **Control Home Assistant** in the agent's options. HA re-reads the tool list every 30 minutes, or when you reload the Mealie MCP entry. |
| The agent can't add to the shopping list | The connection is read-only. See [Allowing changes later](#allowing-changes-later). |
| The agent reads the wrong household's plan | You approved as the wrong user. Disconnect it in Mealie and re-authenticate as Kitchen Voice. |
| "Mealie took too long to answer. Try again." | HA gives up after 5 seconds, so Mealie stops each tool after 4. Check Mealie's load. |

### References

- [Model Context Protocol integration](https://www.home-assistant.io/integrations/mcp/),
  [Application credentials](https://www.home-assistant.io/integrations/application_credentials/)
- HA Mealie integration: [docs](https://www.home-assistant.io/integrations/mealie/),
  [`services.yaml`](https://github.com/home-assistant/core/blob/dev/homeassistant/components/mealie/services.yaml),
  [`services.py`](https://github.com/home-assistant/core/blob/dev/homeassistant/components/mealie/services.py)
- [Intent Script](https://www.home-assistant.io/integrations/intent_script/),
  [custom sentences](https://www.home-assistant.io/voice_control/custom_sentences_yaml/),
  [template sentence syntax](https://developers.home-assistant.io/docs/voice/intent-recognition/template-sentence-syntax)
- [Scripts](https://www.home-assistant.io/integrations/script/), [Stop with a response](https://www.home-assistant.io/docs/scripts/#stop)
- [Packages](https://www.home-assistant.io/docs/configuration/packages/)
