# Adding the assistant to your app

**Contract version 1.** This page is everything an app needs to host the writing assistant: no other document, and no
change to the bot. You serve one JSON file and four small HTTPS endpoints, and you sign a short-lived token for your
signed-in user. The bot's tests check its side of this page against a fake app built only from it
(`tests/contract/`).

## Overview

```text
 browser (your page + the chat window)
   │  1. POST /api/assistant/session (your route, with the user's cookie)   ──►  your app signs a token
   │  2. chat calls with  Authorization: Bearer <token>                     ──►  the bot
   │
 the bot ── during a turn, with the same token ──►  your app's four endpoints
              POST {api_base}/context     who the user is, their voice, their credits
              POST {api_base}/drafts      save a draft the user tapped Add on
              POST {api_base}/material    keep the facts that draft was written from
              POST {api_base}/charge      what a model call cost, to take from the user's credits

 the bot ── every 5 minutes at most ──►  GET {site}/.well-known/app-assistant.json   (your pack)
```

- The bot never reads your database and never publishes anything. It can do only what these four endpoints let it do,
  as the signed-in user.
- You decide where a draft goes (your editor, a schedule) and how credits work.
- Your app needs: an **app id** (lowercase, e.g. `shipnote`), a **shared secret** (`openssl rand -hex 32`), your
  **site** (e.g. `https://shipnote.app`) and your **api_base** (e.g. `https://shipnote.app/api/assistant`, on the
  same scheme, host and port as the site).

## Tokens

The token is an HS256 JWT signed with your shared secret. Sign it on your server only: the secret never goes to a
browser.

| Claim | Value |
|---|---|
| `iss` | your app id |
| `sub` | the user's workspace (or account) id: 1–200 characters of `A-Z a-z 0-9 : _ . -` |
| `access` | `draft` for chatting, `delete` for the delete call below |
| `iat` | now, in seconds |
| `exp` | at most 1 hour after `iat` (`exp - iat <= 3600`); the bot allows 30 s of clock skew on `iat` only |

Example claims: `{"iss": "shipnote", "sub": "ws_42", "access": "draft", "iat": 1795000000, "exp": 1795003600}`.

**To the browser.** Add a route on your site (the chat window calls `POST /api/assistant/session` by default, with the
user's cookies and a `{}` JSON body) that answers `{"token": "<jwt>"}` for a signed-in user and 401 otherwise. The
window renews the token itself before it expires; a 401 or 403 from your route shows "Please sign in again".

**Server to server.** Your server can call the chat API with a token it signed, the same way (no `Origin` header
needed).

**Checking it on your side.** Every call the bot makes to your endpoints carries the user's own token as
`Authorization: Bearer <token>`. Verify it exactly as the bot does (signature with your secret, `iss` is your app id,
`access` is `draft`, not expired, `exp - iat <= 3600`) and use `sub` as the workspace. Trust nothing else in the
request to say who the user is. A bad or expired token gets 401.

## The chat API (browser or your server → the bot)

All routes are under the bot's address, for example `https://bot.example.com`. Each takes
`Authorization: Bearer <token>` (access `draft`). A browser request's `Origin` must be one of your pack's
`allowed_origins` (else 403 `origin`). Errors are JSON `{"error": "<code>", "message"?: "<words for the user>"}`:
401 `invalid` | `expired` | `wrong_app` | `wrong_access`, 403 `origin` | `workspace` (a delete token for another
workspace), 404 `app` | `thread` | `draft` | `file` | `upload`, 400 for a bad body (`empty`, `too_long`,
`too_many_files`, `thread`, an upload's own code), 429 `limit`, 503 `unavailable` (your pack can't be had right now;
`/messages` tells this as a `failed` event in its stream instead).

**The easy way:** `<script src="https://bot.example.com/widget.js" data-app="shipnote" data-session-url="/api/assistant/session"></script>`
(optional `data-label`, `data-position="left"`). It does everything below.

### Send a message: `POST /v1/chat/{app}/messages`

Body: `{"thread_id": null | "<id>", "text": "...", "file_ids": [], "draft_id": null}`. Optional: `new_thread: true`
with a `thread_id` you chose (26 characters `0-9A-Z`) to open a chat with your own id (or continue it), and
`context`: up to 10 `{"role": "user" | "assistant", "text"}` turns your own assistant answered (2,000 characters
each), shown to the model for this turn only. `draft_id` names the draft a message such as "Make it shorter" is about.
Text is at most 4,000 characters; files at most the pack's `files.per_message`.

The answer is a Server-Sent Events stream. Each event's `data` is JSON:

| Event | Data |
|---|---|
| `thread` | `{thread_id, title}`: a new chat was opened |
| `status` | `{step}`: `reading`, `listening`, `thinking`, `writing`, `checking` |
| `text` | `{delta}`: the reply, piece by piece |
| `transcript` | `{file_id, text}`: what a voice note said |
| `note` | `{text}`: a short line about what the bot did |
| `draft` | `{id, kind, posts, image_file_id?}`: `kind` is `x_post` or `x_thread`; show it with an Add button |
| `saved` | `{kind: "facts", items}`: facts the bot kept from this message |
| `failed` | `{error, message}`: the turn stopped; show `message` |
| `done` | `{message_id, cost_usd}` |

`failed` errors: `unavailable` (your app or pack could not be reached), `unauthorized`, `out_of_credits`, `refused`
(your endpoint said no or answered something the bot could not read), `limit`, `resting` (the bot owner's daily cap
for your app), `server` (the bot's own fault).

### Files: `POST /v1/chat/{app}/uploads`, then `.../uploads/{upload_id}/finish`

1. `POST /uploads` with `{"thread_id": null | "<id>", "name", "mime", "size"}` answers
   `{"upload_id", "thread_id", "target": {"url", "method": "PUT", "headers"}}`.
2. The browser PUTs the bytes to `target.url` with exactly `target.headers`.
3. `POST /uploads/{upload_id}/finish` with `{"thread_id"}` answers `{"file_id", "kind", "thread_id"}`; send the
   `file_id` in a message's `file_ids`.

### Add a draft: `POST /v1/chat/{app}/threads/{thread_id}/actions`

`{"action_id": "<your unique id, up to 100 characters>", "kind": "add_draft", "draft_id": "<a draft event's id>"}`. The bot calls your
`/drafts` (and `/material` with the facts) and answers `{"draft_id", "edit_url"}` from your answer. It runs once per
`action_id`; a repeat gets the same answer, and one still running is 409 `pending`. A failure is 402 (out of credits)
or 502 with `{error, message}`.

### Read and delete

- `GET /v1/chat/{app}/threads`: the user's last 30 chats, `[{id, title, updated_at}]`.
- `GET /v1/chat/{app}/threads/{id}`: `{"messages": [...]}`, each assistant message with its `drafts`
  (`id, kind, posts, added, edit_url`) and `saved` lines, so a reload shows what the stream did.
- `GET /v1/chat/{app}/threads/{id}/files/{file_id}`: a file the user sent in that chat, to show it back (a thumbnail
  in your chat): `{"url", "mime", "kind"}`, the `url` living 5 minutes. Another chat's, another workspace's or an
  unfinished upload is a 404 `file`.
- `DELETE /v1/chat/{app}/threads/{id}`: removes one chat with its files; `{"deleted": true}`.
- `DELETE /v1/apps/{app}/workspaces/{workspace}` (server to server, a token with `access: "delete"` whose `sub` is
  that workspace): removes everything the bot keeps for it; `{"deleted_threads": n}`. Call it when a user deletes
  their account.

## Your four endpoints (the bot → your app)

`POST {api_base}/context`, `/drafts`, `/material`, `/charge`. Every request has:

- `Authorization: Bearer <the user's token>`
- `Content-Type: application/json`, `Accept: application/json`
- `Idempotency-Key: <key>` on `/drafts`, `/material` and `/charge`

Answer JSON with status 200. Errors are JSON `{"error": "<code>", "message"?: "<words for the user>"}`. A code is
lowercase snake_case: lowercase letters and digits, words joined by `_` (`unauthorized`, `invalid`, `in_flight`,
`out_of_credits`). The codes below are fixed; the message is yours. If you give a `message`, the user sees it as it
is: write a sentence, not a code.

- **401 `unauthorized`** on any endpoint when the token is missing or fails a check (see Tokens).
- **400 `invalid`** for a body that isn't what this page says, a `/drafts` with an empty `text`, or a missing
  `Idempotency-Key` on `/drafts`, `/material` or `/charge`.

### `POST /context`

Body `{}`. Called at the start of every turn (and at times once more during it, so keep it cheap); if it
fails, the turn stops.

```json
{
  "language": "en",
  "name": "Mai",
  "voice": "Plain, short sentences, no hype.",
  "topics": ["release notes", "small teams"],
  "recent_posts": ["We shipped dark mode."],
  "patterns": ["Lead with the number."],
  "kept_facts": [{"id": "f1", "text": "Sign-ups doubled in March"}],
  "credits_left": 12.5,
  "out_of_credits_message": "You're out of credits. Top up in Billing."
}
```

Only `language` (an ISO code such as `en` or `vi`) is required. `credits_left` is a number in your own unit, or
`null` (or left out) when you don't count credits; at 0 or below, the turn stops with `failed out_of_credits` and your
`out_of_credits_message`. `kept_facts` are facts the user kept in earlier chats that no post has used yet.

### `POST /drafts`

```json
{"text": "First post", "thread": ["Second post", "Third post"], "media": [{"url": "https://…", "alt": "", "mime": "image/png"}]}
```

`text` is the first post, `thread` the posts after it (`[]` for a single post), `media` the pictures for the first
post (`[]` for none). Save the draft for the token's workspace and answer `{"draft_id": "d_123", "edit_url":
"https://shipnote.app/drafts/d_123"}` (`edit_url` may be `null`). Never publish it: the user does that in your app.

**Pictures.** Each `media.url` is a signed GET link to the bot's storage, valid for 5 minutes. Fetch it at once and
keep your own copy (it stops working). Accept only image types, cap the size (5 MB is plenty), and only fetch from the
bot's storage host, so the link can't be used to reach your internal network.

### `POST /material`

`{"facts": ["Sign-ups doubled in March"], "draft_id": "d_123"}` (`draft_id` may be left out). The facts the draft was
written from, in the user's own words. Keep them with the draft (and offer them back in `/context`'s `kept_facts` if
you like). Answer `{"ok": true}`.

### `POST /charge`

`{"cost_usd": 0.0123, "ref": "01JD…:3"}`. One model call's cost in US dollars, sent as soon as the call ends; `ref`
is unique per call and is also the `Idempotency-Key`. Take it from the workspace's credits (convert to your own unit)
and answer `{"ok": true}`. If the workspace has no credits left, answer **402** `{"error": "out_of_credits",
"message": "You're out of credits. Top up in Billing."}`: the bot logs it, and the next message is stopped by
`/context`'s `credits_left`. A charge that goes slightly below zero is normal: a call is charged after it ran.

### Idempotency-Key

`/drafts`, `/material` and `/charge` may be sent more than once with the same key (a retry, a double tap). Do the
work **at most once per key per workspace**:

- the first request with a key does the work and stores its answer;
- a later request with a key that finished gets the same answer again (for `/drafts`, the same `draft_id`);
- a request whose key is still being worked on gets **409** `{"error": "in_flight", "message": "Still saving that. Try again in a moment."}`.
- a request that failed (400, 402) did nothing: free the key, so the same key may be tried again.

Keep keys at least a day. Keys are at most 200 characters. The bot does not retry a 409: the user sees your message
and taps again.

### Status codes, and what the user sees

| Your answer | The bot | The user sees |
|---|---|---|
| 200 with the JSON above | goes on | |
| 200 that is not that JSON (HTML, a missing field) | `refused` | the bot's line: "The app couldn't be reached. Try again in a minute." |
| 400, 404, 409, 422 and other 4xx | `refused` | your `message`, or the same line |
| 401, 403 | `unauthorized` | your `message`, or the same line |
| 402 | `out_of_credits` | your `message`, or the same line |
| 429 | `unavailable`, no retry | your `message`, or the same line |
| 5xx, no answer in 20 s, connection refused | retried once with the same body and key, then `unavailable` | your `message`, or the same line |
| 3xx | `refused`: redirects are not followed | the same line |

The bot shows your `message`, or without one your `error` when it is not a code (so `"error": "NOT_FOUND"` or
`"error": "Drafts are full."` is shown as it is). A code is never shown: `error: "out_of_credits"` or
`"unauthorized"` alone gives the bot's own line, and `message: "out_of_credits: You're out of credits."` is shown
without its code.

**Timeouts.** 20 seconds per request, one retry on a 5xx, a timeout or a lost connection (so at most about 40 s).
Answer `/charge` and `/context` fast: they are on the path of every turn.

## Your pack: `GET {site}/.well-known/app-assistant.json`

How the assistant works in your app: what it asks about, what it may write, which models, what it may spend. Serve
it as JSON with status 200 (at most 256 KB, no redirect). The bot fetches it when needed, uses it for 5 minutes, and
keeps the last good copy, so your chats keep working while this route is down. The JSON Schema is
[`docs/app-assistant.schema.json`](app-assistant.schema.json).

```json
{
  "contract": 1,
  "id": "shipnote",
  "name": "Shipnote",
  "api_base": "https://shipnote.app/api/assistant",
  "allowed_origins": ["https://shipnote.app"],
  "guide": "A good story is one change a user can feel, with one number from the user.",
  "writes": [
    {"kind": "x_post", "count": 1, "max_chars": 280, "no_links": true, "may_carry_image": true,
     "instructions": "A short launch post in the user's voice."},
    {"kind": "x_thread", "count": 1, "max_posts": 4, "max_chars": 280, "instructions": "A short thread."}
  ],
  "models": {
    "chat": "anthropic/claude-sonnet-5.5", "writer": "anthropic/claude-sonnet-5.5",
    "main": "openai/gpt-6-luna", "cheap": "openai/gpt-6-luna", "vision": "openai/gpt-6-luna"
  },
  "files": {"image_mb": 10, "pdf_mb": 20, "pdf_pages": 10, "voice_seconds": 300, "per_message": 5,
            "kinds": ["image", "pdf", "voice"]},
  "message_usd_cap": 0.15
}
```

- `id` must be the id the bot owner registered for you, and your tokens' `iss`.
- `api_base` must be on your site's own origin (same scheme, host and port), or the pack is refused.
- `allowed_origins`: the origins your chat window runs on. Use your site's own origin: files are uploaded from the
  browser straight to the bot's storage, and the storage only accepts uploads from your registered site's origin.
- `message_usd_cap`: the most one message may spend on models, in US dollars.
- Fields the bot doesn't know are ignored, so you can serve a newer pack to an older bot. Never put a secret in it: it
  is public.

## The registry entry (the bot owner adds it)

The bot only talks to apps its owner trusts. On a bot that serves one app, its owner sets three variables:
`APP_ID=shipnote`, `APP_SITE=https://shipnote.app` and `APP_SECRET` (your shared secret; both sides keep the same value).

For several apps on one bot, send the owner your app id and site; they add to `apps/registry.yaml`:

```yaml
apps:
  - id: shipnote
    site: https://shipnote.app
    secret_env: SHIPNOTE_SESSION_SECRET
```

and set the environment variable `SHIPNOTE_SESSION_SECRET` to your shared secret (both sides keep the same value; it
is never in git). A self-hosted bot can set `APPS` instead, the same list as JSON:
`APPS='[{"id": "shipnote", "site": "https://shipnote.app", "secret_env": "SHIPNOTE_SESSION_SECRET"}]'`.

## The owner's ceilings

Your pack can't raise what the bot's owner spends. The owner sets:

| Variable | Effect |
|---|---|
| `MAX_MESSAGE_USD` (default `0.25`) | a pack's `message_usd_cap` above it is lowered to it |
| `ALLOWED_MODELS` (optional, comma-separated) | a pack naming any other model is refused, and your chats get `unavailable` |
| `MAX_APP_DAILY_USD` (optional, off by default) | over it, your app's chats get `failed resting` until the next UTC day |

File caps are also capped by the bot (images 20 MB, PDFs 50 MB and 50 pages, voice 600 s, 10 files a message). Each
lowered value is logged.

## Using the JS package

For a JavaScript app (Next.js, Hono, Cloudflare Workers, Deno, Node 20+), the npm package `@app-assistant/host` does
all of the above except knowing who is signed in. `/server` checks the token and serves the four endpoints and your
pack from four functions you write (with the idempotency, error codes and statuses of this page); `/react` is the
chat window. Its README (`sdk/js/README.md`) has the details.

```ts
import { createAssistantHost } from "@app-assistant/host/server";

export const assistant = createAssistantHost({
  appId: "shipnote",
  secret: process.env.SHIPNOTE_SESSION_SECRET!,
  pack, // your pack's JSON, served at /.well-known/app-assistant.json
  context: async (ws) => ({ language: "en", credits_left: await creditsOf(ws) }),
  saveDraft: async (ws, draft, key) => saveDraftOnce(ws, key, draft), // → {draft_id, edit_url}
  saveMaterial: async (ws, facts, draftId, key) => keepFactsOnce(ws, key, facts, draftId),
  charge: async (ws, costUsd, ref) => ((await takeCredits(ws, ref, costUsd)) ? { ok: true } : { outOfCredits: "You're out of credits." }),
  // Optional: 409 in_flight for a key being worked on. claim must be atomic and expire on its own (>= 60 s).
  once: { claim: (key) => kv.setIfAbsent(key, "1", { ttlSeconds: 120 }), release: (key) => kv.delete(key) },
});

// Every request to /api/assistant/* and /.well-known/app-assistant.json (null: not one of them):
const res = await assistant.handle(request);

// Your session route, for a signed-in user:
return Response.json({ token: await assistant.signToken(user.workspaceId, "draft") });
```

```tsx
import { AssistantChat } from "@app-assistant/host/react";

<AssistantChat app="shipnote" botUrl="https://bot.example.com" sessionUrl="/api/assistant/session" />
```

`once.claim` must be atomic and must expire on its own (at least 60 s, e.g. 2 minutes): `release` runs when a request
ends, but not when it is killed mid-work (a timeout, a redeploy), and a claim that never expires would answer 409 to
every repeat of that key forever. Without `once`, `saveDraft`, `saveMaterial` and `charge` must be idempotent per
key on their own.

The package's tests send every scenario below (its `match` not `null`) to `handle`, and check that its tokens and the
bot's verify on both sides.

### A worked example: a Next.js app

Four files; the pack, your data functions and who is signed in are the only things that are yours.

```ts
// lib/assistant.ts: the host, one per server
import { createAssistantHost } from "@app-assistant/host/server";
import pack from "./app-assistant.json";

export const assistant = createAssistantHost({
  appId: "shipnote",
  secret: process.env.SHIPNOTE_SESSION_SECRET!,
  pack,
  context: async (ws) => ({ language: "en", credits_left: await creditsOf(ws) }),
  saveDraft: async (ws, draft, key) => saveDraftOnce(ws, key, draft), // → {draft_id, edit_url}
  saveMaterial: async (ws, facts, draftId, key) => keepFactsOnce(ws, key, facts, draftId),
  charge: async (ws, costUsd, ref) =>
    (await takeCredits(ws, ref, costUsd)) ? { ok: true } : { outOfCredits: "You're out of credits." },
});
```

```ts
// app/api/assistant/[endpoint]/route.ts, and the same file at app/.well-known/app-assistant.json/route.ts
import { assistant } from "@/lib/assistant";

const handle = async (req: Request) => (await assistant.handle(req)) ?? new Response(null, { status: 404 });
export { handle as GET, handle as POST };
```

```ts
// app/api/assistant/session/route.ts: yours, because only your app knows who is signed in
import { assistant } from "@/lib/assistant";

export async function POST() {
  const user = await currentUser();
  if (!user) return Response.json({ error: "unauthorized" }, { status: 401 });
  return Response.json({ token: await assistant.signToken(user.workspaceId, "draft") });
}
```

```tsx
// in a client component of your dashboard
import { AssistantChat } from "@app-assistant/host/react";

<AssistantChat app="shipnote" botUrl="https://bot.example.com" sessionUrl="/api/assistant/session" />;
```

Your pack's `api_base` is `https://shipnote.app/api/assistant` (the route's own path), and the bot's owner registers
`shipnote` (the registry entry above). Add `once` as in the first example when two taps of Add can arrive together.

## Checking your app

`tests/contract/scenarios.json` in the bot's repo lists the calls the bot makes. Each scenario has:

- `setup`: the app's state before the request:
  - `token`: which token the request carries: `valid` (the default), `expired`, `wrong_secret`, `wrong_iss` (another
    app's id), `delete_access` (`access: "delete"`) or `too_long` (`exp - iat` over 3600);
  - `context`: what `/context` answers for the workspace; `credits_left`: the workspace's credits;
  - `done`: keys already answered (`{path, key, answer}`: a repeat gets `answer`); `in_flight`: keys being worked on;
  - `faults`: attempts that fail before one reaches the app (a timeout, a refused connection or a status): bot-side;
  - `broken: true`: the app breaks this page and answers `response_status`/`response_body` as they are: bot-side only;
  - `app_only: true`: a request the bot never sends (no `Authorization` header): app-side only.
- `call`: what the bot does. `request`: what it sends: `method`, `path` (under `api_base`), `headers` (`<token>`
  stands for the token; a `null` header is absent) and `body`.
- `response_status` and `response_body`: what the bot's fake app answers.
- `match`: what any app must answer, or `null` (a bot-side scenario). The status must equal `response_status`, and the
  JSON body must match `match`: an object matches when each of its keys is present and matches (other keys are
  allowed); `"<string>"` matches any non-empty string, `"<string|null>"` any string or `null`; a list matches item by
  item; anything else must be equal. So error codes and `ok: true` are fixed, while ids, URLs and messages are yours.
- `expect`: what the bot then does (a return value, or an error kind and the message the user sees, and how many
  attempts). It is the bot's side only: an app ignores it.

To check your app: for each scenario whose `match` is not `null`, put your app in the `setup` state, send `request`
once to your `api_base` with a token of the named kind, and compare as above.

## Contract versions

This is contract 1: the pack says `"contract": 1`. A later version will add, never change: new optional fields in
bodies and answers, new optional endpoints. Ignore fields you don't know. A change that breaks an app would be
contract 2, with the pack's `contract` saying which one an app follows.
