# Packs on the bot, and apps over MCP

The usual way to add an app is the contract in [integration.md](integration.md): the app serves its own pack at
`{site}/.well-known/app-assistant.json` and the bot owner adds one line to `apps/registry.yaml`. This page is for the
two other ways the bot can know an app: a **pack file on the bot** (`apps/<id>.yaml`, as the `/demo` app does), and an
app reached over **MCP** instead of the four HTTPS endpoints.

## A pack file: `apps/<id>.yaml`

[`apps/demo.yaml`](../apps/demo.yaml) is a complete example; [`docs/app-assistant.schema.json`](app-assistant.schema.json)
is the schema. Fields:

| Field | Meaning |
|---|---|
| `id` | App id. It is the `iss` claim of the app's tokens and the `data-app` of the script tag. |
| `name` | Name shown in the chat window. |
| `api_base` | The app's four endpoints of contract 1 (`{api_base}/context`, `/drafts`, `/material`, `/charge`). `{base}` becomes `PUBLIC_BASE_URL`. Set this or `mcp_url`, not both. |
| `mcp_url` | Or the app's MCP endpoint (below). `{base}` becomes `PUBLIC_BASE_URL`. |
| `secret_env` | Name of the environment variable holding the app's signing secret (never the secret itself). Unset, the app is off. |
| `allowed_origins` | Origins allowed to use the chat window; the `Origin` header is checked on every `/v1` request. |
| `guide` | What a good story from this app has, in plain words, and what the bot must never do. It steers the conversation and the writer. |
| `writes` | What the app wants written, any kinds it likes (a changelog entry, a LinkedIn post, a product description, an email). Per item: `kind` (an id: lowercase letters, digits and `_`), `name` (what the app calls it, shown to the model), optional `about` (one line: what it is and when to write it), `parts` (`one` piece, or `several` in order, like a thread or a carousel; default `one`), `count`, `instructions`, and optional `max_posts` (several parts: the most parts), `max_chars` (per part), `no_links`, `no_dashes`, `may_carry_image` (a picture the user sent in the chat may go with it, when they ask for it). `x_post` and `x_thread` need no `name`, `about` or `parts`: they default to an X post and an X thread. |
| `tools` | With `mcp_url` only: the app's tool names for `context`, `save_draft`, `save_material`, `charge`. |
| `models` | OpenRouter ids: `chat` (the conversation) with `chat_reasoning` (`none` or `low`; default `low`) and optional `chat_temperature`, `writer` (the drafts) with `writer_reasoning`, `main`, `cheap`, `vision`, and optional `fallbacks`. A model whose thinking is mandatory thinks at high effort when sent `none`: use `low`. |
| `actions` | Optional: the app's own MCP server, `{mcp_url, tools?, starters?}` (docs/integration.md, "Actions"). Read-only tools run at once; the others wait for the user's Confirm. `writes` may then be `[]`. |
| `files` | Optional caps: `image_mb`, `pdf_mb`, `pdf_pages`, `voice_seconds`, `per_message`, `kinds` (image, pdf, voice). |
| `message_usd_cap` | Most the bot may spend on one message (never above the owner's `MAX_MESSAGE_USD`). |
| `demo` | `true` only for a public demo (daily caps, files deleted after a day). |

A pack file for an app that is also in the registry is its fallback: used only until the bot has fetched the app's own
pack once (the bot then keeps the fetched copy for 5 minutes in memory and in Postgres for the next cold start).

## An app over MCP

Instead of `api_base`, a pack can name `mcp_url` and four tools. The app's MCP accepts the user's token as
`Authorization: Bearer` and treats it as a "draft" caller for that workspace only.

- `assistant_context`: what the bot may know about the workspace: `voice`, `topics`, `recent_posts`, `patterns`,
  `credits_left`, `out_of_credits_message`, `language`, and optionally `kept_facts` (`[{"id", "text"}]`, facts the user
  kept earlier and has not used, offered when the chat opens).
- `save_draft`: saves a draft the user tapped: `{text, thread?, media?}`. `text` is the first post; `thread` is the
  posts after it; `media` is `[{url, alt?}]` and the picture stays on the first post.
- `save_material`: keeps facts for later, with `draft_id` when the facts came with a draft.
- `assistant_charge`: records the cost of one model call, once per `ref` (`"<message id>:<n>"`). A message makes several
  calls, each charged once; a retry with the same ref adds nothing.

The tokens, the session route, the chat window and deleting a workspace's data are the same as in
[integration.md](integration.md).
