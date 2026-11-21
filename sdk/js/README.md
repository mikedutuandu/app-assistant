# @app-assistant/host

Host the app-assistant writing bot in a JavaScript app. It implements your side of
[contract 1](https://github.com/mikedutuandu/app-assistant/blob/main/docs/integration.md) on Web `Request`/`Response`,
so it runs on Cloudflare Workers, Next.js, Hono, Deno and Node 20+ (it needs the global Web Crypto).

- `@app-assistant/host/server`: verifies the user's token and serves the bot's four endpoints and your pack from four
  functions you write; signs tokens for your signed-in users; calls the bot as a user. Web Crypto and `fetch` only.
- `@app-assistant/host/react`: `<AssistantChat>`, the chat window.

## Install

```bash
npm install @app-assistant/host
```

You need, from the bot's owner: your **app id** (lowercase, e.g. `shipnote`), the **shared secret** (`openssl rand
-hex 32`, kept in your server's environment, never in git or a browser) and the bot's address.

## The server: four functions

```ts
// lib/assistant.ts
import { createAssistantHost } from "@app-assistant/host/server";
import pack from "./app-assistant.json"; // your pack: docs/app-assistant.schema.json

export const assistant = createAssistantHost({
  appId: "shipnote",
  secret: process.env.ASSISTANT_SECRET!,
  botUrl: "https://bot.example.com", // for botFetch only
  pack, // served at /.well-known/app-assistant.json; checked when created
  // basePath: "/api/assistant",     // the default

  // POST /context: the user's profile, on every turn. Only `language` is required.
  context: async (ws) => ({ language: "en", name: await nameOf(ws), credits_left: await creditsOf(ws) }),

  // POST /drafts: save the draft the user tapped Add on (never publish it). The same key again → the same answer.
  saveDraft: async (ws, { text, thread, media }, key) => {
    const draft = await db.drafts.findOrCreate({ ws, key, text, thread, media }); // copy media URLs now: they expire
    return { draft_id: draft.id, edit_url: `https://shipnote.app/drafts/${draft.id}` };
  },

  // POST /material: keep the facts the draft was written from. The same key again → do nothing.
  saveMaterial: async (ws, facts, draftId, key) => db.facts.insertOnce({ ws, key, facts, draftId }),

  // POST /charge: one model call's cost in US dollars. The same ref again → no second charge.
  charge: async (ws, costUsd, ref) => {
    if (await db.charges.has(ws, ref)) return { ok: true };
    if ((await creditsOf(ws)) <= 0) return { outOfCredits: "You're out of credits. Top up in Billing." };
    await db.charges.take(ws, ref, costUsd);
    return { ok: true };
  },

  // Optional: two requests with the same Idempotency-Key at once. claim → false while one is running (→ 409);
  // release is called when it ends. The key is "{workspace}/{drafts|material|charge}/{Idempotency-Key}".
  once: { claim: (key) => kv.setIfAbsent(key, "1", { ttlSeconds: 120 }), release: (key) => kv.delete(key) },

  // Optional: where caught errors go (a function that threw, a release that failed). Default: console.error.
  onError: (error, where) => log.error(where, error),
});
```

**Scheduling (optional).** If your app can schedule posts, add the user's next free times to `context`, labelled
in their own time zone; the bot then picks one for each draft and shows a Schedule button:

```ts
context: async (ws) => ({
  language: "en",
  post_times: (await freeSlots(ws, 5)).map((t) => ({ at: t.toISOString(), label: label(t, tzOf(ws)), why: "one of your best hours" })),
}),
saveDraft: async (ws, { text, thread, media, schedule_at }, key) => {
  const draft = await db.drafts.findOrCreate({ ws, key, text, thread, media });
  const answer = { draft_id: draft.id, edit_url: `https://shipnote.app/drafts/${draft.id}` };
  if (!schedule_at) return answer; // Save draft
  const ok = await schedule(draft.id, schedule_at); // the user tapped Schedule: that tap is their OK
  return ok ? { ...answer, scheduled_at: schedule_at } : { ...answer, scheduled_at: null, schedule_note: "That time is taken now." };
},
```

**Actions (optional).** To let the assistant read and do things in your app, point your pack's `actions.mcp_url` at
your own MCP server (any official MCP SDK) and accept the assistant's token there:

```ts
import { verifyAssistantToken } from "@app-assistant/host/server";

const user = await verifyAssistantToken(request, process.env.ASSISTANT_SECRET!, "shipnote");
if (!user) return new Response(null, { status: 401 });
// ... answer MCP for user.workspaceId. Mark read-only tools with annotations.readOnlyHint: true; any other tool
// runs only after the user taps Confirm in the chat.
```

**`once.claim` must be atomic and must expire on its own** (at least 60 s; 2 minutes is a good choice). `release`
runs when a request ends, but not when the request is killed mid-work (a function timeout, a redeploy): a claim with
no expiry would then stay forever, and every repeat of that key would get 409 (the bot doesn't retry a 409, so that
draft could never be added). A failed `release` is reported to `onError` and never changes the answer. **Without
`once`**, two requests with the same key can run at the same time, so your functions must be idempotent per key on
their own (a unique index on the key, for example).

`handle` answers what the contract says for you: 401 `unauthorized` for a missing or bad token, 400 `invalid` for a
bad body or a missing `Idempotency-Key`, 402 `out_of_credits` with your sentence, 409 `in_flight`, and 500 `server`
when one of your functions throws (the error is logged with `console.error`, never sent).

## The two routes

Send two routes to `assistant.handle(request)`: `{basePath}/*` (POST) and `/.well-known/app-assistant.json` (GET).
It answers `null` for any other request, so you can pass it on.

**Next.js (App Router)**

```ts
// app/api/assistant/[endpoint]/route.ts  and  app/.well-known/app-assistant.json/route.ts
import { assistant } from "@/lib/assistant";

const handle = async (req: Request) => (await assistant.handle(req)) ?? new Response(null, { status: 404 });
export { handle as GET, handle as POST };
```

**Cloudflare Workers, Hono**

```ts
app.use("*", async (c, next) => (await assistant.handle(c.req.raw)) ?? next());
```

**The session route** is yours, because only your app knows who is signed in. The chat window calls it with the
user's cookies:

```ts
// app/api/assistant/session/route.ts
export async function POST() {
  const user = await currentUser();
  if (!user) return Response.json({ error: "unauthorized" }, { status: 401 });
  return Response.json({ token: await assistant.signToken(user.workspaceId, "draft") });
}
```

## The chat window

```tsx
import { AssistantChat } from "@app-assistant/host/react";

<AssistantChat app="shipnote" botUrl="https://bot.example.com" sessionUrl="/api/assistant/session" />;
```

Optional props: `label` (the button, default "Write with AI"), `position` (`"left"`), and `scheduleLabel` (the word
before a draft's time picker when your `context` sends `post_times`: "Post at", "Send at"; default "Publish at").

It loads the bot's `widget.js` once per page; the widget draws its own floating open button and panel. Optional:
`label` (the button's words), `position` (`"left"` or `"right"`), and `children` for an open button of your own
in the page (`className` styles it). `openAssistant()` opens the chat from anywhere.

Not on React? The same thing is one script tag:

```html
<script src="https://bot.example.com/widget.js" data-app="shipnote" data-session-url="/api/assistant/session" async></script>
```

## More on the server

- `assistant.signToken(workspaceId, "draft" | "delete", ttlSeconds?)`: a token, 1 hour at most (the default).
- `assistant.botFetch(workspaceId, path, init?)`: `fetch(botUrl + path)` with a fresh token for that workspace. A
  `DELETE /v1/apps/{app}/workspaces/{workspace}` gets a `delete` token: call it when a user deletes their account.
- `assistant.readEvents(response.body)`: the events of a message's stream, `{event, data}` with `data` parsed.

```ts
const res = await assistant.botFetch(ws, "/v1/chat/shipnote/messages", {
  method: "POST",
  body: JSON.stringify({ thread_id: null, text: "We shipped dark mode", file_ids: [] }),
});
for await (const { event, data } of assistant.readEvents(res.body!)) if (event === "draft") console.log(data);
```

- `signToken(secret, claims)` and `verifyToken(secret, token, appId, access)` are exported on their own too.
