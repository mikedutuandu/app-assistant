# Phase 4: the integration standard

Date: 21 Nov 2026. Status: design, for Mike's approval.
Builds on: `2026-11-20-one-assistant-design.md` (Phases 1–3 built; Phase 4 there is the outline this design fills in).
Repos: `mikedutuandu/app-assistant` (the bot, the contract, the npm package) and `mikedutuandu/ai-mono` (HookPlaybook and `products/_template` move onto it).

## Why

Today HookPlaybook is a special case: the bot knows it from a YAML file in the bot's repo, calls it through MCP tools with HookPlaybook-specific names, and HookPlaybook carries its own copies of the token signing, the bot's SSE parsing and the widget loader. Adding StreakPlan (Phase 5), a client's app (Upwork) or a stranger's app (open source) would mean editing the bot each time.

Mike wants one standard any app can follow, so adding the assistant is "install a package, write four small functions, set two values", and so the bot can be open-sourced later (Mike, 20 Nov 2026). It is also the shape Upwork clients understand: a contract, an SDK and a one-click deploy.

## Mike's decisions

- **The npm package is published to npm now** (21 Nov 2026), publicly, as `@app-assistant/host`, though the bot's repo stays private until Phase 5 is done. Mike creates the free npm org `app-assistant` and a publish token once.
- **The contract is four plain HTTPS endpoints** (approach A below), not MCP (21 Nov 2026).

## Goals

- A written contract, `docs/integration.md`, that a developer in any language can implement in an afternoon, and that the bot's tests check.
- One npm package: `/server` implements the contract on standard `Request`/`Response` (Next.js, Hono, Cloudflare Workers, Node); `/react` is the chat window as a component; a `<script>` tag gives the same window to sites without npm.
- An app's pack (its prompts, write kinds, models, limits) lives in the app and is served by it; adding an app needs no code change in the bot.
- The bot runs anywhere: `docker compose up` with no cloud account (Postgres and files on local disk), or a one-click Vercel deploy with Neon.
- HookPlaybook runs on the package, and `products/_template` in ai-mono includes it (off until its two values are set).

## Non-goals

- Making the bot's repo public, a licence, a brand name (Phase 5, when Mike is happy).
- StreakPlan (Phase 5).
- SDKs in other languages (the contract is the SDK for them).
- Removing MCP support from the bot: it stays for apps that already use it, behind the same contract tests.
- Changing what the assistant does in a conversation.

## Anti-goals (a failure even if it works)

- An app that can raise the bot owner's spending without limit by editing its own pack.
- A step after a merge (Mike works from his phone): no hand-run migrations or deploy commands.
- HookPlaybook breaking for a minute during the move: every step keeps the old path working until the new one is live.
- The package leaking a secret to the browser (the token is signed server-side only, as in Phase 3).

## Approaches considered

1. **Four plain HTTPS endpoints (chosen).** `POST {base}/context`, `/drafts`, `/material`, `/charge` with small JSON bodies. Easiest to implement and debug in any language; the package does it for JS apps.
2. Keep MCP as the contract. Less bot change, but every adopter implements JSON-RPC framing for four calls; the package would be an MCP server kit. Rejected by Mike.
3. A hosted service with webhooks and dashboards. Far more to build and run; not what open source needs. Rejected.

For the package (Claude's calls): standard `Request`/`Response` handlers rather than per-framework adapters (one code path runs on Workers, Next.js route handlers and Hono); the React half wraps the existing widget rather than a second UI (one chat window to maintain).

## How it works

### 1. The contract (`docs/integration.md`, contract version 1)

**Who calls whom.** The browser or the app's server talks to the bot's chat API (unchanged: `/v1/chat/{app}/…`, documented as it is today). The bot calls the app's four endpoints during a turn.

**Tokens.** The app signs an HS256 JWT with its shared secret: `iss` = app id, `sub` = the user's workspace (or account) id, `access` = `draft` (chat) or `delete` (forget), `iat`, `exp` at most 1 hour later. The app hands it to the bot (browser session route or server-to-server, as Phase 3 does). When the bot calls the app, it sends the same user token as `Authorization: Bearer`, so the app knows which workspace without trusting anything else.

**The four endpoints** (JSON in and out, under the app's base URL, for example `https://hookplaybook.com/api/assistant`):

| Call | Body | Answer |
|---|---|---|
| `POST /context` | `{}` | `{name, language, voice, topics[], recent_posts[], patterns[], kept_facts[], credits_left, out_of_credits_message}` (all optional but `language`) |
| `POST /drafts` | `{text, thread[], media[{url, alt, mime}]}` | `{draft_id, edit_url}` |
| `POST /material` | `{facts[], draft_id?}` | `{ok: true}` |
| `POST /charge` | `{cost_usd, ref}` | `{ok: true}`, or 402 `{error: "out_of_credits", message}` |

`/drafts`, `/material` and `/charge` carry an `Idempotency-Key` header; the app must do the work at most once per key (the package helps, section 3). Errors are JSON `{error, message?}` with 401 (bad token), 402, 409 (in flight), 4xx/5xx. The bot's timeouts and retry rule are written down: the same as today (20 s per call, one retry), so nothing a user sees changes; the speed follow-up tunes them from real timings (Claude's call). A failed `/context` stops the turn as today.

**Pictures** (Claude's call, from the builder check): as today, the bot sends each picture as a short-lived signed GET URL (`media[{url, alt, mime}]`, valid 5 minutes) and the app fetches and copies it (HookPlaybook already does, with its SSRF guard, image-only types and a 5 MB cap). The app must be able to reach the bot's public address; the `local` storage backend serves the same signed GET from the bot (section 2).

**The pack**, served by the app at `GET {site}/.well-known/app-assistant.json`: the same fields as today's YAML except secrets and the tool names (`contract: 1`, `id`, `name`, `api_base`, `allowed_origins`, `guide`, `writes[]`, `models`, `files`, `message_usd_cap`). The bot caches it for 5 minutes and stores the last good copy in Postgres (a new table, add-only migration), so a cold instance survives the app's pack route being down; with no copy at all, that app's chats get `failed {error: "unavailable"}` (Claude's call, from the builder check).

**Contract tests.** The bot's repo holds a fake app built only from `docs/integration.md` and a test suite that runs every bot→app call against it; the package's tests run the same scenarios against `/server`.

### 2. The bot

- **Registry.** `apps/registry.yaml` (committed, no secrets) lists the apps the bot trusts: `id`, the site to fetch the pack from, and the name of the environment variable holding its secret. Self-hosters can instead set `APPS` (same data as JSON). Local packs (`apps/*.yaml`) still work, for the demo and for tests.
- **REST tools beside MCP.** A pack with `api_base` uses the new REST client; one with `mcp_url` keeps the MCP client. Both go through the same tool interface, timing and charging code.
- **The owner's ceilings.** `MAX_MESSAGE_USD` (default 0.25) caps any pack's `message_usd_cap`; `ALLOWED_MODELS` (optional) limits which models a pack may name; `MAX_APP_DAILY_USD` (optional, off by default, so Mike's deployment is unchanged) caps one app's total model spend per UTC day, counted by the bot whatever the app's `/charge` answers. A pack over a ceiling is clamped and logged, never trusted; an app over its daily cap gets `failed {error: "resting"}` (Claude's call, from the builder check: an app picks its own workspace ids, so per-workspace limits alone don't bound it).
- **Storage.** A `local` backend beside `s3` and `supabase`: files under `DATA_DIR`, uploaded with the bot's own short-lived signed PUT URL and read back by apps with a signed GET URL (both `/v1/files/…` on `PUBLIC_BASE_URL`), read from disk by the bot itself. Chosen when neither S3 nor Supabase is configured.
- **Docker.** A `Dockerfile` (Python, uv, runs migrations at start as today) and `docker-compose.yml` (bot + Postgres 16 + a data volume) so `docker compose up` gives a working bot at `localhost:8000` with the demo app.
- **One-click Vercel.** A "Deploy with Vercel" button in the README that clones the repo and adds Neon from the Marketplace, with the env vars it asks for listed. (It needs a public repo, so it is documented and tested on Mike's account now and switched on at open source.)

### 3. The npm package `@app-assistant/host` (in `sdk/js` of the bot's repo)

- **`/server`:** `createAssistantHost({ appId, secret, botUrl, basePath, pack, context, saveDraft, saveMaterial, charge, once? })` returns `{ handle(request): Promise<Response | null>, signToken(workspaceId, access, ttlSeconds?), botFetch(workspaceId, path, init), readEvents(stream) }`.
  - `handle` answers the four endpoints and the pack's well-known URL, verifies the token, parses and checks bodies, applies `Idempotency-Key` through `once` (an app-provided "do this at most once" store; without it the app's own functions must be idempotent), and returns `null` for any other path so the app's router continues.
  - `botFetch` / `readEvents` are what HookPlaybook's `lib/writer.ts` does today: server-to-server calls to the bot with a fresh token and no Origin header, and a tolerant SSE reader.
  - Runs on Web Crypto and `fetch` only: no Node built-ins, so Cloudflare Workers works.
- **`/react`:** `<AssistantChat app botUrl sessionUrl />` loads the bot's widget once and opens it; `sessionUrl` is the app's own route that returns a token (the package gives `signToken` for it).
- **Script tag:** `<script src="{botUrl}/widget.js" data-app=… data-session-url=…>` (today's widget), documented next to the React component.
- **Publishing:** a GitHub Action in the bot's repo publishes `sdk/js` to npm when a merge to `main` changes its `package.json` version, with the `NPM_TOKEN` repository secret. Semver; the contract version is in the package's README.

### 4. HookPlaybook and the template (ai-mono)

The move keeps HookPlaybook working at every step:

1. **Bot PR:** registry, REST client, ceilings, local storage, Docker, the package, contract tests. HookPlaybook stays on its YAML pack and MCP. Merging publishes the package.
2. **ai-mono PR:** HookPlaybook installs `@app-assistant/host`, serves the four endpoints at `/api/assistant/*` and its pack at `/.well-known/app-assistant.json` (its prompts move here from the bot's YAML), and replaces its own token/SSE/fetch code in `lib/writer.ts` with the package. The MCP assistant tools stay for now. `products/_template` gets the same wiring, off until `ASSISTANT_URL` and `ASSISTANT_TOKEN_SECRET` are set.
3. **Bot PR:** HookPlaybook moves into `apps/registry.yaml` (REST, remote pack). `apps/hookplaybook.yaml` stays as the fallback the bot uses only while no copy of the remote pack has ever been fetched (Claude's call, from the builder check), so the order of merges can't break chats. Opened after step 2 is live.
4. **Later, both repos:** remove HookPlaybook's MCP assistant tools and the fallback YAML.

Secrets don't change: HookPlaybook's `ASSISTANT_TOKEN_SECRET` and the bot's `HOOKPLAYBOOK_SESSION_SECRET` keep their names and value.

## Constraints

- Merging to `main` is the whole deploy in both repos; migrations only add; no manual step after a merge (Mike's one-time npm setup and the NPM_TOKEN secret are before the first merge).
- The bot's rules hold: untrusted text in `<data>` blocks, numbers only from facts, per-call charging, refs never shown to the browser.
- HookPlaybook's rules hold (workspace scoping, credits before AI, nothing posts to X, secrets server-side).
- The widget, the public demo and Ask's writer keep working through every step.

## Success

- Someone who has never seen the bot adds it to a fresh Next.js app from `docs/integration.md` and the package in under an hour, with the contract tests green.
- `docker compose up` on a clean machine gives a working demo chat with an upload.
- HookPlaybook runs on the package with no change users can see, and its pack lives in ai-mono.

## Left to the builder

- The pack's JSON schema details and the registry file's exact fields.
- The fake app's language (Python inside the bot's tests is fine) and how the package's tests share scenarios with it.
- The local storage signing scheme (HMAC over path + expiry is enough).
- How `once` is shaped (an interface with `claim(key)` / `release(key)` is a fine start; HookPlaybook already has `claimOnce`).
