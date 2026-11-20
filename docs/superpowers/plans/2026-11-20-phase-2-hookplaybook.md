# The writing bot, Phase 2 (HookPlaybook): implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A "Write with me" button in HookPlaybook's dashboard opens the writing bot. The bot reads the user's voice, posts and credits; saves drafts (with an uploaded screenshot) that wait on the Schedule; and keeps the facts the user confirms so the weekly planner finally has real material.

**Architecture:**
- **HookPlaybook (ai-mono, `products/hookplaybook`)** gains four pieces:
  - a signed session token (HS256, a shared secret)
  - token support in the existing user MCP (`/api/v1/mcp`), as a `draft`-level caller of kind `"assistant"`
  - four assistant-only MCP tools, never listed for API keys or in the public docs
  - a `told_facts` table that `shippedOf` merges into the planner's material
- **The bot (app-assistant)** gains a `hookplaybook` pack.
- **Two pull requests:** ai-mono (tasks 1–9) and app-assistant (task 10). Each is opened directly against `main`.

**Tech Stack:** HookPlaybook's own stack (Next.js on vinext, Cloudflare Workers, D1 with Drizzle, R2, Vitest), `crypto.subtle` for HMAC. Bot side: Python pack YAML.

**Spec:** `docs/superpowers/specs/2026-11-20-app-writing-bot-design.md` (app-assistant), section "Phases", item 2. Code map: the controller's research of 20 Nov 2026 (paths below are from it). **Before touching HookPlaybook, read `products/hookplaybook/CLAUDE.md`.**

**Ruling (planning):**
- The spec says HookPlaybook's `POST /drafts` gains `media`. This plan adds pictures only through the assistant's own `save_draft` tool, and leaves the public API and its docs unchanged.
- Why: it's the smaller surface (the public API fetching any URL would be a new SSRF and abuse path for API keys), and no public docs or "18 tools" tests change.
- Cost if wrong: API-key users still can't attach pictures; that can be added later.

## Global Constraints

**The token**
- `ASSISTANT_TOKEN_SECRET` (Worker secret) signs and verifies tokens: HS256, claims `iss "hookplaybook"`, `sub` = workspace id, `access "draft"` (or `"delete"` for the delete call), `iat`, `exp ≤ iat + 3600`. The bot holds the same value as `HOOKPLAYBOOK_SESSION_SECRET`.
- The workspace comes only from the token's signed `sub`, never from a request body or argument.

**When the feature is off**
- With `ASSISTANT_TOKEN_SECRET` unset, the feature is off: `/api/assistant/session` answers 503, a token is never accepted, and no button shows.
- With `ASSISTANT_URL` (a `vars` value: the bot's base URL) unset, no button shows and no delete call is made.
- So a merge with no secrets changes nothing visible.

**What a token can and can't do**
- A token is a `draft` caller of `kind: "assistant"`. It can never schedule, post, approve, change billing, or touch keys or members.
- The four assistant tools are listed and callable only for `kind === "assistant"`. They are never in `API_TOOLS`, `API_ENDPOINTS`, `/api/v1/docs` or `/developers`, so the tests that count "18" stay unchanged.

**Credits**
- Charging AI spend writes one `ai_calls` row (step `"assistant-chat"`) once per `ref` (`claimOnce`), so it counts in `creditPoolOf`, Billing's history and admin AI spend.
- When credits are out, `assistant_charge` and `assistant_context` report `credits_left: 0`, and the charge refuses with 402 and the code `out_of_credits`.

**Images**
- A draft's picture is copied into R2 under `"<workspaceId>/<newId()>"` through `fetchPublicImage` (PNG, JPEG, WebP or GIF, ≤ 5 MB), then stored through `cleanMedia`. The alt text goes in `MediaItem.name` (≤ 120 characters).

**Told facts**
- Facts are kept only when the user confirms them (the bot calls `save_material` on a tap).
- They live in `told_facts`, are fresh for `SHIPPED_FRESH_DAYS` (14), and are capped at `TOLD_PER_WEEK = 2` posts a week, separately from GitHub's `SHIPPED_PER_WEEK`.
- They are marked used when a post tells them.
- They are listed on Sources with Remove, and included in "Download my data".

**HookPlaybook's own rules** (from its CLAUDE.md), all apply:
- `sameOrigin` plus the JSON content-type check on mutating handlers
- `viewerOf` for the workspace
- `blockedForAdmin` for "Log in as" (an admin can't mint a token)
- migrations only add
- D1's 100-value limit
- `db()` in the app
- privacy page and `legal.updated` when data goes to a new service
- docs kept true (PRODUCT.md "API and MCP" row, CODE-MAP.md, CREDITS.md, SYSTEM.md)
- `products/TEMPLATE-SYNC.md` "Waiting to port" gets one line for the generic parts: token auth on the user MCP, and Idempotency-Key on the MCP route
- Prettier (120 columns)
- phone layout checked at 390 px

**Delete**
- Removing a space or deleting an account calls `DELETE {ASSISTANT_URL}/v1/apps/hookplaybook/workspaces/{id}` with a delete token (`access "delete"`, the same secret, 5-minute expiry).
- It is best-effort: a 5 s timeout, `reportError` on failure, never blocking the local delete.

## Review Focus

1. **A token for workspace A is never usable for workspace B.** A tampered `sub`, a different secret, `alg: none`, an expired token, or `access: "schedule"` all give 401. An `hp_live_` key still works exactly as before. (Tasks 1 and 2)
2. **An API key can't see or call the assistant tools.** `tools/list` for a key has 18 tools; for a token it has the draft tools plus the 4 assistant tools; `tools/call assistant_charge` with a key gives an error. (Task 6)
3. **The same `ref` charged twice adds one row; an `Idempotency-Key` retry of `save_draft` makes one draft.** This must hold under parallel calls too. (Tasks 3 and 6)
4. **An image URL that is unreachable, too big, the wrong type, or on a private address** leaves a draft with no picture and an error the bot shows. It never leaves a draft with a broken media key. (Task 6)
5. **Told facts never crowd out GitHub facts or each other past their caps,** and a fact told in a post is never offered again. (Task 5)

---

### Task 1: Session tokens and the session endpoint

**Files:**
- Create: `products/hookplaybook/shared/core/src/assistant-token.ts`, `shared/core/test/assistant-token.test.ts`, `app/app/api/assistant/session/route.ts`, `app/test/assistant-session.test.ts`
- Modify: `app/lib/env.ts` (`SecretName` gets `ASSISTANT_TOKEN_SECRET`), `app/.dev.vars.example`, `app/.env.production.example`, `app/wrangler.jsonc` (`vars.ASSISTANT_URL: ""`) and `worker-configuration.d.ts` (run `pnpm cf-typegen`)

**Interfaces:**
- Produces: `signAssistantToken(secret: string, claims: {sub: string; access: "draft" | "delete"; ttlSeconds?: number}, now?: number) => Promise<string>` (default ttl 3600, refuses > 3600).
- Produces: `verifyAssistantToken(secret: string, token: string, access: "draft" | "delete", now?: number) => Promise<{workspaceId: string} | null>`. It checks:
  - header `{"alg":"HS256","typ":"JWT"}` exactly
  - the signature, compared in constant time
  - `iss === "hookplaybook"`, the required `access`, and `sub` matching `^[A-Za-z0-9:_.-]{1,200}$`
  - `exp > now - 30`, `iat <= now + 30` and `exp - iat <= 3600`

  It returns null on any failure.
- Produces: `isAssistantToken(bearer: string) => boolean` (three base64url segments, not `hp_live_`).
- Route `POST /api/assistant/session`, in order:
  1. 503 `{error:"off"}` when the secret is unset
  2. `sameOrigin` (403) and JSON content type (415)
  3. `viewerOf(request)`: 401 when signed out
  4. `blockedForAdmin(viewer)`: 403
  5. `accessNow` plus `canUse`: 402 on an ended trial
  6. `hitLimit("assistant-session:<ws>", 60, 3600)`: 429

  Then `{token}` with `cache-control: no-store`. Any member may mint, not only the owner.

- [ ] **Step 1: write the failing core tests.**
  - `round trip`, `tampered sub is null`, `other secret is null`, `alg none is null`
  - `access schedule is null for draft`, `expired is null`, `ttl over an hour refused`, `iat 31 s ahead is null`
  - A known vector: an HS256 token minted by PyJWT for secret "s", claims `{iss:"hookplaybook", sub:"w1", access:"draft", iat:1700000000, exp:1700003600}`, verifies at now 1700000100. Generate it once with Python and paste the literal into the test.
- [ ] **Step 2:** run `cd products/hookplaybook/shared/core && pnpm test assistant-token`. Expected: FAIL.
- [ ] **Step 3:** implement `assistant-token.ts` with `crypto.subtle` (`importKey("raw", …, {name:"HMAC", hash:"SHA-256"})`), and `base64url` from `crypto.ts`.
- [ ] **Step 4: write the failing route tests.**
  - signed out 401
  - "Log in as" 403
  - ended trial 402
  - secret unset 503
  - success returns a token that verifies for the viewer's workspace
  - cross-origin 403
- [ ] **Step 5:** implement the route, following `app/app/api/runs/route.ts`'s checks.
- [ ] **Step 6:** run `pnpm test` and `pnpm typecheck` in `shared/core` and `app`, then `pnpm prettier:check` at the root. Expected: all pass.
- [ ] **Step 7:** commit "feat(hookplaybook): signed sessions for the writing bot".

### Task 2: The user MCP accepts a session token

**Files:**
- Modify: `shared/core/src/api.ts` (`ApiCaller` gets `kind: "key" | "assistant"`; `apiCallerOf` sets `kind: "key"`), `app/lib/api-keys.ts` (`authenticate`)
- Test: `app/test/api.test.ts` (new cases)

**Interfaces:**
- Consumes: `isAssistantToken` and `verifyAssistantToken` (Task 1).
- Produces: `authenticate` builds a caller from a valid draft token: `{keyId: "assistant:<ws>", workspaceId, userId: null, access: "draft", name: "Write with me", kind: "assistant"}`.
  - The checks after it are unchanged: `canUse` 402, and `hitLimit("api:assistant:<ws>")` at 60 a minute.
  - It never writes `api_keys.lastUsedAt`.
  - With the secret unset, or a token that fails verification, the answer is the same 401 an unknown key gets.

- [ ] **Step 1: write the failing tests.**
  - `token caller reads me`: GET `/api/v1/me` with a token returns 200 for the token's workspace.
  - `tampered token is 401`.
  - `token cannot schedule`: `schedule_post` is refused like a draft key.
  - `hp_live key unchanged`: an existing key test still passes.
  - `secret unset rejects tokens`.
- [ ] **Step 2:** run them. Expected: FAIL.
- [ ] **Step 3:** implement. Branch on `isAssistantToken(bearer)` before `apiCallerOf`.
- [ ] **Step 4:** run `app` tests and typecheck. Expected: PASS.
- [ ] **Step 5:** commit "feat(hookplaybook): the user MCP accepts writing-bot sessions".

### Task 3: Idempotency-Key on the MCP route

**Files:**
- Modify: `app/lib/api-keys.ts` (`answerMcp`), test in `app/test/api.test.ts`

**Interfaces:**
- Produces: when an MCP POST carries `Idempotency-Key`, `answerMcp`:
  1. validates it with `idempotencyKey(request)` (400 on bad)
  2. claims `api_replays` with id `"<caller.keyId>:<key>"` and the hash of `POST /api/v1/mcp` plus the raw body, through the same `claimReplay`
  3. runs the call, then `settleReplay`s the response (status and body)

  The same key with the same body replays with `Idempotent-Replayed: true`; the same key with a different body gives 422; a parallel call gives 409. Without the header, behaviour is unchanged.
- `forgetWorkspace` (`app/lib/ops.ts`) also deletes `api_replays` where `id LIKE 'assistant:<ws>:%'`.

- [ ] **Step 1: write the failing tests.**
  - `replay returns the first answer and makes one draft` (save_draft twice with the same key; uses Task 6's tool, so write the test with `create_draft` first and extend it in Task 6)
  - `same key different body 422`
  - `no header unchanged`
- [ ] **Step 2:** run them. Expected: FAIL.
- [ ] **Step 3:** implement.
- [ ] **Step 4:** run `app` tests. Expected: PASS.
- [ ] **Step 5:** commit "feat(hookplaybook): Idempotency-Key on the user MCP".

### Task 4: Told facts: table and core

**Files:**
- Modify: `shared/db/src/schema.ts` (new `toldFacts`), then run `pnpm db:generate` in `app/` (migration `0049_*`, meta, `shared/db/src/migrations.ts`)
- Create: `shared/core/src/told.ts`, `shared/core/test/told.test.ts`
- Modify: `shared/core/src/shipped.ts` (`shippedOf` merges told facts, `markShippedUsed` marks both tables)

**Interfaces:**
- Produces the table `told_facts`: `id` (ULID), `workspaceId` (references workspaces, cascade), `fact` (text, ≤ 300), `draftId` (text, null), `usedAt` (text, null), timestamps. Indexes on `(workspaceId, createdAt)` and `(workspaceId, usedAt)`.
- Produces:
  - `TOLD_PER_WEEK = 2`.
  - `saveTold(db, workspaceId, facts: string[], draftId: string | null) => Promise<number>`. It trims, drops empty items, caps 30 facts at 300 characters each, and skips a fact whose text equals an unused fact from the last 14 days. With `draftId` set, rows are saved with `usedAt = now` (told by that draft). It returns the count saved.
  - `toldOf(db, workspaceId, now?) => Promise<ShippedFact[]>`: unused facts from the last 14 days, newest first, at most `TOLD_PER_WEEK - toldUsedThisWeek`, each `{id, fact, product: null, grouped: false}`.
  - `listTold(db, workspaceId, limit = 50)` and `removeTold(db, workspaceId, id) => Promise<boolean>`.
- Modifies: `shippedOf` returns GitHub facts plus `toldOf`'s, within each source's own cap. `posts` counts both. `markShippedUsed(db, ws, ids)` updates `shipped_items` and `told_facts`. `toldThisWeek` counts only `shipped_items`.

- [ ] **Step 1: write the failing tests.**
  - `saveTold trims, dedupes, caps`
  - `saved with draft id is already used`
  - `toldOf caps at 2 a week and ignores used and old ones`
  - `shippedOf keeps GitHub's 2 and adds told 2`
  - `markShippedUsed marks a told fact used`
  - `removeTold only in its workspace`
  - Also extend `shipped.test.ts` and `coach-material.test.ts` so `weekMaterialOf().shipped` counts told facts.
- [ ] **Step 2:** run `cd shared/core && pnpm test told shipped coach-material`. Expected: FAIL.
- [ ] **Step 3:** implement. Run `pnpm db:generate` in `app/`, then commit the migration, meta and `migrations.ts`.
- [ ] **Step 4:** run the core tests (the migrations test included) and typecheck. Expected: PASS.
- [ ] **Step 5:** commit "feat(hookplaybook): facts told to the writing bot feed the weekly plan".

### Task 5: The writer tells told facts honestly

**Files:**
- Modify: `shared/core/src/shipped.ts` (`shippedNote`), test `shared/core/test/shipped-posts.test.ts`

**Interfaces:**
- `ShippedFact` gets `from?: "github" | "told"` (undefined means github). `toldOf` sets `"told"`.
- `shippedNote(facts)` lists them under two headings:
  - GitHub facts keep the existing line.
  - Told facts get: "(things they told us themselves; use their facts, never add numbers, results or feelings they didn't give)".
- `usedFacts` and `imageFor` work for both, with no image for told facts.

- [ ] **Step 1: write the failing test.** `shippedNote` with one fact of each kind has both headings, and a told fact never has the "from their code" line.
- [ ] **Step 2:** run it. Expected: FAIL.
- [ ] **Step 3:** implement.
- [ ] **Step 4:** run it. Expected: PASS.
- [ ] **Step 5:** commit "feat(hookplaybook): the writer tells told facts as the user's own".

### Task 6: The four assistant tools

**Files:**
- Create: `shared/core/src/assistant.ts` (`ASSISTANT_TOOLS`, `callAssistantTool`), `shared/core/test/assistant.test.ts`
- Modify:
  - `app/lib/api-keys.ts`: `answerMcp` lists `[...toolsFor(access), ...ASSISTANT_TOOLS]` only for `kind === "assistant"`; `run()` dispatches assistant tool names to `callAssistantTool`, refusing them for `kind === "key"`; it passes `AssistantDeps`.
  - `app/app/api/v1/mcp/route.ts`: builds the deps.
  - `shared/core/src/ai-credits.ts`: `AI_USES` and `stepLine` name "assistant-chat" as "Write with me".
  - `app/lib/ai-budget.ts`: moves `AI_CREDITS_TEXT` to core as `AI_CREDITS_TEXT` in `plan-of.ts` and re-exports it.

**Interfaces:**
- `AssistantDeps = {saveMedia(key: string, file: {bytes: Uint8Array; type: string}): Promise<void>; fetchImage?: typeof fetchPublicImage; origin: string}`
- `callAssistantTool(db, caller, name, args, deps) => Promise<unknown>`. Tools:
  - **`assistant_context` `{}`** returns `{voice, topics, recent_posts, patterns, credits_left, out_of_credits_message, language}`:
    - `voice` is `voicePrompt(voiceOf())`, plain text, at most 2,000 characters.
    - `topics` is `topicsOf(voice.topics)` or the niche list.
    - `recent_posts` is the texts of `recentOwnPosts(db, ws, 30)`, newest 15.
    - `patterns` is the top 5 hook shapes from `hookLibrary` for the workspace's niches and languages, as short strings.
    - `credits_left` comes from `creditPoolOf`.
    - `out_of_credits_message` is `AI_CREDITS_TEXT`.
    - `language` is a BCP-47 tag mapped from `voice.language` (English→en, Vietnamese→vi, else "en").
  - **`save_draft` `{text, media: [{url, alt}]}`**:
    - `text` must be 1–2,000 characters; `media` has at most 1 item.
    - It downloads with `fetchPublicImage(url, {maxBytes: 5 MB, timeoutMs: 10000})`. On failure it throws `ToolError("The picture couldn't be copied; the post was not saved.")`.
    - It saves to R2 as `"<ws>/<newId()>"`, then inserts the draft exactly as `apiCreateDraft` does: `source "user"`, `sourceLabel "Made with Write with me"`, `media = JSON.stringify(cleanMedia([{key, type, name: alt.slice(0,120)}], ws))`.
    - It returns `{id, edit_url: "<origin>/dashboard?edit=<id>"}`. A draft counts against `API_DRAFTS_PER_DAY`, as in `run()`.
  - **`save_material` `{facts: [{text}], draft_id?}`**: calls `saveTold(db, ws, texts, draft_id ?? null)`. A `draft_id` must be this workspace's draft, else a ToolError. Returns `{kept}`.
  - **`assistant_charge` `{cost_usd, ref}`**:
    - `cost_usd` must be finite and 0–5; `ref` must be 1–100 characters.
    - If `creditPoolOf(...).left <= 0`, it throws `ApiError(402, "out_of_credits: " + AI_CREDITS_TEXT)`.
    - Otherwise it runs `claimOnce(db, "assistant-charge:<ws>:<ref>", 7)`. When the claim is fresh it inserts an `ai_calls` row `{workspaceId: ws, step: "assistant-chat", model: "assistant", ok: true, costUsd: cost_usd, ms: 0}`, and calls `releaseOnce` if the insert throws.
    - Returns `{charged: boolean}`.
- Every tool's text output is data. `ASSISTANT_TOOLS` descriptions say so.

- [ ] **Step 1: write the failing tests (core).**
  - context fields and types, plus credits 0 when used up
  - save_draft with a stubbed fetchImage saves a draft with one media item under the workspace prefix and the alt as name
  - a bad image leaves no draft
  - text over 2,000 refused
  - save_material with a foreign draft_id refused
  - charge twice with the same ref adds one `ai_calls` row
  - a parallel double charge adds one row
  - charge when out of credits gives 402 with out_of_credits
- [ ] **Step 2: write the failing tests (app, MCP).**
  - `tools/list` for a key = 18 tools; for a token = draft tools + 4
  - `tools/call assistant_charge` with a key → isError
  - `save_draft` twice with the same Idempotency-Key → one draft (finishes Task 3's test)
- [ ] **Step 3:** run them. Expected: FAIL.
- [ ] **Step 4:** implement.
- [ ] **Step 5:** run core and app tests and typecheck. Expected: PASS.
- [ ] **Step 6:** commit "feat(hookplaybook): the writing bot's four tools".

### Task 7: Told facts on Sources and in the export

**Files:**
- Modify: `app/app/dashboard/sources/page.tsx` (a "Facts you told the writing bot" section, outside the `rules.sources === 0` branch, shown when there is at least one), `app/app/dashboard/sources/actions.ts` (`removeToldFact`), `app/app/api/account/export/route.ts` (adds `toldFacts`), tests `app/test/sources-page.test.ts`

**Interfaces:**
- The section lists `listTold(db(), ws, 50)`. Each row shows the fact, "Used in a post" or "Not used yet", and the date.
- Each row has `RowActions` › `ConfirmButton` "Remove", calling `removeToldFact` (with `blockedForAdmin`, `revalidatePath("/dashboard/sources")`, `actionDone("Removed.")`).

- [ ] **Step 1:** write the failing tests, in the existing source-text style: the section renders from `listTold`, Remove calls `removeToldFact`, and the export lists `toldFacts`.
- [ ] **Step 2:** run them. Expected: FAIL.
- [ ] **Step 3:** implement. Check the page at 390 px wide.
- [ ] **Step 4:** run the app tests. Expected: PASS.
- [ ] **Step 5:** commit "feat(hookplaybook): told facts on Sources, with Remove".

### Task 8: "Write with me" in the dashboard, and the privacy page

**Files:**
- Create: `app/app/dashboard/write-with-me.tsx` (a client component)
- Modify:
  - `app/app/dashboard/layout.tsx`: pass `assistantUrl` (from `vars.ASSISTANT_URL`, and only when the secret is set) to Shell.
  - `Shell.tsx`: a "Write with me" button after "Ask HookPlaybook", plus a phone icon beside Ask's in `phoneActions`.
  - `app/app/privacy/page.tsx` and `product.config.ts` (`legal.updated`).
  - Tests: `app/test/write-with-me.test.ts`.

**Interfaces:**
- `WriteWithMe({assistantUrl})`:
  - On first click it injects `<script src="${assistantUrl}/widget.js" data-app="hookplaybook" data-session-url="/api/assistant/session" data-label="Write with me" data-position="right">` once, waits for it to load, then opens the panel.
  - The widget mounts its own button. The dashboard button calls `window.dispatchEvent(new CustomEvent("assistant:open"))`. Add an `assistant:open` listener to the widget in Task 10.
  - The theme comes from CSS variables on the dashboard root: `--assistant-accent` (the kit's primary), `--assistant-radius` 12px, `--assistant-font` (the kit's font).
- With no `assistantUrl`, nothing renders and no script loads. The layout adds no database read (keep `frame-speed`/`fast-menus` tests green).
- Privacy: one paragraph names the writing bot (run by HookPlaybook, on Google Cloud Run and Supabase) and its AI provider (OpenRouter). It says what goes there: what the user types, the files they attach, and their voice and recent posts. Bump `legal.updated`.

- [ ] **Step 1:** write the failing tests: hidden without the URL; the script is injected once with the exact attributes; the privacy page names the service.
- [ ] **Step 2:** run them. Expected: FAIL.
- [ ] **Step 3:** implement. Check at 390 px.
- [ ] **Step 4:** run the app tests (frame-speed included). Expected: PASS.
- [ ] **Step 5:** commit "feat(hookplaybook): Write with me in the dashboard".

### Task 9: Delete the bot's data with the workspace, docs, template sync

**Files:**
- Modify: `app/lib/ops.ts` (`forgetWorkspace` calls `forgetAssistantData(workspaceId)`), new `app/lib/assistant.ts` (`forgetAssistantData`), tests `app/test/assistant-forget.test.ts`
- Docs: `products/hookplaybook/docs/PRODUCT.md` (a "Write with me" row and the "API and MCP" row mentioning sessions), `CODE-MAP.md`, `CREDITS.md` ("Write with me" spend), `SYSTEM.md`, `products/TEMPLATE-SYNC.md` (one "Waiting to port" line)

**Interfaces:**
- `forgetAssistantData(workspaceId)`: when `ASSISTANT_URL` and the secret are set, it makes `fetch(DELETE {ASSISTANT_URL}/v1/apps/hookplaybook/workspaces/{id}, {headers: {authorization: "Bearer " + signAssistantToken(secret, {sub: id, access: "delete", ttlSeconds: 300})}, signal: AbortSignal.timeout(5000)})`.
  - A non-2xx answer or an error calls `reportError(..., "assistant-forget")`. It never throws, and it runs inside `waitUntil` when available.

- [ ] **Step 1:** write the failing tests: a call is made with a delete token for that workspace; nothing happens when unset; a failure doesn't throw.
- [ ] **Step 2:** run them. Expected: FAIL.
- [ ] **Step 3:** implement, then update the docs.
- [ ] **Step 4:** run all HookPlaybook tests and typechecks (`shared/core`, `app`, `jobs`, `shared/db`) and the root `pnpm prettier:check`. Expected: PASS.
- [ ] **Step 5:** commit "feat(hookplaybook): forget the writing bot's data with the workspace; docs".
- [ ] **Step 6:** push the branch, open the ai-mono PR against `main`, and tell Mike right away.

### Task 10: The bot's HookPlaybook pack (app-assistant)

**Files:**
- Create: `apps/hookplaybook.yaml`, `tests/test_hookplaybook_pack.py`
- Modify: `widget/src/index.ts` (listen for `assistant:open` to open the panel; rebuild `widget.js`), README ("Apps": HookPlaybook's secret name)

**Interfaces:**
- The pack has these fields:
  - `id: hookplaybook`, `name: HookPlaybook`, `mcp_url: https://hookplaybook.com/api/v1/mcp`
  - `secret_env: HOOKPLAYBOOK_SESSION_SECRET`, `allowed_origins: ["https://hookplaybook.com"]`
  - `tools`: `context: assistant_context`, `save_draft: save_draft`, `save_material: save_material`, `charge: assistant_charge`
  - `writes`:
    - one `x_post` (count 2, `max_chars` 280, `no_links: true`, `no_dashes: true`, `may_carry_image: true`, instructions: posts in the user's voice using their winning hook shapes, one point each, plain words)
    - one `x_thread` is left out for Phase 2
  - files: image 5 MB (HookPlaybook's limit), PDF 20 MB and 30 pages, voice 300 s, 5 per message, kinds image, pdf and voice
  - `message_usd_cap: 0.10`
  - models are the same as the demo's until Mike picks others
  - an `interview_guide` for X posts: what happened, who it helps, a number or result, what they learned, and whether there's a screenshot
- Production refuses to start without `HOOKPLAYBOOK_SESSION_SECRET` (existing behaviour). The README says to set it to the same value as HookPlaybook's `ASSISTANT_TOKEN_SECRET`.

- [ ] **Step 1:** write the failing tests: the pack loads with these values; a token minted by HookPlaybook's algorithm (the Task 1 vector) verifies with this pack's secret; `assistant:open` opens the panel (vitest).
- [ ] **Step 2:** run them. Expected: FAIL.
- [ ] **Step 3:** implement. Run `npm run build` and the full Python and widget suites.
- [ ] **Step 4:** commit "feat: HookPlaybook pack". Push, open the app-assistant PR against `main`, and tell Mike.

---

## After both merge (Mike)

1. Generate one secret (`openssl rand -hex 32`). Set it as HookPlaybook's `ASSISTANT_TOKEN_SECRET` (with `pnpm secrets`) and as the bot's `HOOKPLAYBOOK_SESSION_SECRET` (in Secret Manager).
2. Set HookPlaybook's `vars.ASSISTANT_URL` to the bot's Cloud Run URL, and redeploy by merging the vars change.
3. Open the dashboard and use "Write with me" on his own account for a week (spec, Phase 2 success line).
