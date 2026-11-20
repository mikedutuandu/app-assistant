# Phase 4: the integration standard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Any app adds the assistant by following one contract (four HTTPS endpoints + a served pack), with an npm package that does it for JS apps; the bot runs from Docker or Vercel; HookPlaybook runs on the package.

**Architecture:** The bot gains a pack store (local YAML packs, plus remote packs fetched from a registry of trusted apps, cached in memory and Postgres), a REST tool client beside the MCP one, owner ceilings, and a local-disk storage backend. A TypeScript package in `sdk/js` implements the contract on Web `Request`/`Response`. HookPlaybook moves onto the package in steps that each keep the old path working.

**Tech Stack:** Python 3.12, FastAPI, psycopg, httpx, pytest (bot); TypeScript, tsup, vitest (package); Cloudflare Workers, Next.js/vinext, D1 (HookPlaybook).

**Spec:** `docs/superpowers/specs/2026-11-21-integration-standard-design.md` (read it whole before any task).

**Repos and order:**
- Tasks 1–5: `/home/claude/app-assistant`, branch `phase-4-integration-standard` → PR A. Merging publishes the package (needs `NPM_TOKEN`, Mike's one-time step).
- Tasks 6–7: `/home/claude/ai-mono`, branch `hookplaybook-assistant-host` (from origin/main) → PR B, merged after the package version it pins is on npm.
- Task 8: app-assistant, a new branch from main → PR C, opened after PR B is live.

## Global Constraints

- Merging to `main` is the whole deploy in both repos; migrations only add; no manual step after a merge.
- Contract version 1. Endpoints `POST {api_base}/context`, `/drafts`, `/material`, `/charge`; pack at `GET {site}/.well-known/app-assistant.json`; token HS256 with `iss` = app id, `sub` = workspace, `access` ∈ {`draft`,`delete`}, `exp - iat` ≤ 3600 s.
- `/drafts` body `{text, thread: string[], media: [{url, alt, mime}]}` → `{draft_id, edit_url}`; `/material` `{facts: string[], draft_id?}` → `{ok: true}`; `/charge` `{cost_usd, ref}` → `{ok: true}` or 402 `{error: "out_of_credits", message}`; `/context` `{}` → `{language, name?, voice?, topics?, recent_posts?, patterns?, kept_facts?, credits_left?, out_of_credits_message?}`. `/drafts`, `/material`, `/charge` carry `Idempotency-Key`. Errors JSON `{error, message?}`.
- Bot→app timeouts and retries stay as today (20 s per call, one retry); a failed `/context` stops the turn.
- Owner ceilings: `MAX_MESSAGE_USD` default 0.25 clamps `message_usd_cap`; `ALLOWED_MODELS` optional allowlist; `MAX_APP_DAILY_USD` optional, off by default, over it → `failed {error: "resting"}`.
- Remote packs: cache 5 min in memory; last good copy in Postgres; none at all → `failed {error: "unavailable"}`; a local YAML with the same id is used only while no remote copy was ever fetched.
- The bot's existing rules hold (untrusted text in `<data>`, numbers only from facts, per-call charging); the widget, the demo and HookPlaybook's Ask writer keep working at every step; conversation recordings still replay (`REQUIRE_CONVERSATION_RECORDINGS=1`).
- Package: name `@app-assistant/host`, exports `./server` and `./react`, ESM, Web Crypto + `fetch` only in `./server` (no Node built-ins), React ≥ 18 as a peer dependency of `./react` only.
- Never print, write or commit a secret. Never `pnpm prettier:write` with paths in ai-mono.

## Review Focus

1. **The app's pack route is down on a cold bot instance** → chats keep working from the Postgres copy; a brand-new app with no copy gets `unavailable`, never a 500. → Task 1 tests.
2. **A pack asks for more than the owner allows** (cap 5 USD, an unlisted model) → clamped/refused and logged; the turn runs within the ceiling. → Task 1 tests.
3. **The bot calls an app that answers 401/402/409/500 or HTML** → the same ToolError kinds and user messages as the MCP client today. → Task 2 contract tests.
4. **HookPlaybook between merges** (PR A merged, PR B not; PR B merged, PR C not) → Ask's writer works unchanged. → Task 6 test that the MCP tools still answer; Task 8 test that the YAML fallback is used until a remote copy exists.
5. **`/server` on Cloudflare Workers** → no Node built-ins; a token signed by the package verifies in the bot and vice versa. → Task 4 cross-check test against a token from the bot's `mint_session`.

---

### Task 1 (bot): the pack store, the registry and the owner's ceilings

**Files:** Create `src/assistant/pack_store.py`, `apps/registry.yaml` (empty list for now: `apps: []`), `src/assistant/migrations/007_remote_packs.sql`; Modify `src/assistant/packs.py` (pack JSON schema: `api_base` as an alternative to `mcp_url`, `contract: 1`, `site`), `main.py`, `api/deps.py` (`session_for`, `ChatCors`), `config.py`; Test `tests/test_pack_store.py`.

**Interfaces — Produces:**
- `AppPack` gains `api_base: str | None`, `mcp_url: str | None` (exactly one set), `contract: int = 1`; `tools` is required only with `mcp_url`.
- `class PackStore`: `async def get(self, app_id: str) -> AppPack | None`; `def origins(self) -> list[str]` (local packs' allowed_origins + each registry site's origin, for CORS at startup); built in lifespan as `app.state.packs: PackStore`.
- Registry entry: `{id, site, secret_env}`; `APPS` env (JSON list of the same) overrides the file when set.
- Table `remote_packs(app text primary key, pack jsonb not null, fetched_at timestamptz not null)`.
- `clamp_pack(pack, settings) -> AppPack` applies `MAX_MESSAGE_USD` and `ALLOWED_MODELS` (a model outside the list → the pack is refused with a logged reason).
- Daily spend: a `counters`-style row `spend:{app}:{YYYY-MM-DD}` incremented by each charged cost; checked before a turn when `MAX_APP_DAILY_USD` is set.

- [ ] **Step 1: Failing tests** in `tests/test_pack_store.py`: local pack found; remote pack fetched from `{site}/.well-known/app-assistant.json` (httpx MockTransport), cached for 5 min (one fetch for two gets), stored in `remote_packs`; fetch fails + Postgres copy → that copy; fetch fails + no copy + local YAML same id → the YAML; nothing → `None` and the chat route answers `failed {error: "unavailable"}`; pack with `message_usd_cap: 5` → 0.25; model not in `ALLOWED_MODELS` → refused; `MAX_APP_DAILY_USD=0.01` after a 0.02 turn → next turn `failed {error: "resting"}`; existing pack and API tests unchanged.
- [ ] **Step 2: Run** `TEST_DATABASE_URL=postgresql://test:test@localhost:54329/test uv run pytest tests/test_pack_store.py -v` → FAIL.
- [ ] **Step 3: Implement.** Every `app.state.packs[...]` use goes through `PackStore.get`.
- [ ] **Step 4: Run** the full suite with `REQUIRE_CONVERSATION_RECORDINGS=1` + ruff → green.
- [ ] **Step 5: Commit** `feat: packs can come from the app; the owner's ceilings`.

### Task 2 (bot): the REST client, the contract and its tests

**Files:** Create `docs/integration.md`, `tests/contract/fake_app.py`, `tests/contract/scenarios.json`, `tests/contract/test_contract.py`; Modify `src/assistant/tools.py` (`RestHttpTools`, a `tools_for(pack, session, client, timer)` factory), `README.md`.

**Interfaces — Produces:** `RestHttpTools` implements `AppTools` against `pack.api_base` with the Global Constraints' shapes; same `ToolError` kinds and messages as `McpHttpTools` for 401/402/409/5xx/bad JSON; `Idempotency-Key` = the key argument. `tests/contract/scenarios.json`: a list of `{name, call, request, response_status, response_body, expect}` used by both the Python tests and Task 4's TS tests.

- [ ] **Step 1: Failing tests**: each scenario run against `fake_app.py` (a FastAPI app built only from `docs/integration.md`) through `RestHttpTools`; a full happy turn with a REST pack through the `api` fixture (draft added via `/drafts`, charges via `/charge`).
- [ ] **Step 2–4:** run → FAIL; implement; full suite + ruff → green.
- [ ] **Step 5: Commit** `feat: the contract: four HTTPS endpoints any app can serve`.

### Task 3 (bot): local storage, Docker, Vercel button

**Files:** Modify `src/assistant/files/storage.py` (backend `local`), `config.py` (`data_dir`), `api/` (new `files.py` routes `PUT/GET /v1/files/{key}` with `sig` and `exp` query, HMAC-SHA256 over `method:key:exp`, keyed by `FILES_SIGNING_KEY`; when unset, a random key is generated once and stored in a new `settings` table (add-only migration 008)); Create `Dockerfile`, `docker-compose.yml`, `.dockerignore`; Modify `README.md` (Docker and "Deploy with Vercel" sections), `.env.example`; Test `tests/test_local_storage.py`.

- [ ] **Step 1: Failing tests**: upload_target → PUT with a valid sig stores bytes; wrong/expired sig → 403; signed GET returns the bytes with the stored content type; a path with `..` → 400; `delete_prefix` removes files; the backend is chosen when no S3/Supabase settings are set.
- [ ] **Step 2–4:** run → FAIL; implement; full suite + ruff; then `docker compose up -d --build` and `curl localhost:8000/health` → `{"ok": true}`, and one demo upload through the API if Docker runs here (say so if it doesn't).
- [ ] **Step 5: Commit** `feat: run anywhere: local files, Docker, a Vercel button`.

### Task 4 (bot repo): the npm package `@app-assistant/host`

**Files:** Create `sdk/js/{package.json, tsconfig.json, tsup.config.ts, README.md, src/server/index.ts, src/server/token.ts, src/server/handle.ts, src/server/bot.ts, src/react/index.tsx, test/*.test.ts}`; `.github/workflows/sdk.yml` (test on PR; publish on push to main when `sdk/js/package.json` version is not on npm, with `NPM_TOKEN`, `--provenance`, `--access public`).

**Interfaces — Produces (`./server`):**
- `signToken(secret: string, claims: {appId: string; workspaceId: string; access: "draft" | "delete"; ttlSeconds?: number}): Promise<string>`; `verifyToken(secret, token, appId, access): Promise<{workspaceId: string} | null>`.
- `createAssistantHost(opts: { appId: string; secret: string; botUrl?: string; basePath?: string /* default "/api/assistant" */; pack?: object /* served at /.well-known/app-assistant.json */; context(ws: string): Promise<AppContext>; saveDraft(ws: string, d: {text; thread; media}, key: string): Promise<{draft_id; edit_url}>; saveMaterial(ws, facts: string[], draftId: string | undefined, key: string): Promise<void>; charge(ws, costUsd: number, ref: string): Promise<{ok: true} | {outOfCredits: string}>; once?: {claim(key: string): Promise<boolean>} }) => { handle(req: Request): Promise<Response | null>; signToken(ws, access, ttl?): Promise<string>; botFetch(ws: string, path: string, init?: RequestInit): Promise<Response>; readEvents(body: ReadableStream<Uint8Array>): AsyncGenerator<{event: string; data: unknown}> }`.
- `./react`: `AssistantChat({app, botUrl, sessionUrl, label?, position?})` loads `${botUrl}/widget.js` once with the data attributes the widget reads and renders an open button.

- [ ] **Step 1: Failing tests** (vitest): each `tests/contract/scenarios.json` case through `handle`; token round trip; a token minted by the bot (a fixture string produced by `mint_session` in a pytest helper, checked in) verifies; `handle` returns `null` for other paths; `readEvents` handles CRLF, multi-line data and comments; the `./server` bundle has no `node:` imports (a test reads the built file).
- [ ] **Step 2–4:** run → FAIL; implement; `cd sdk/js && npm ci && npm test && npm run build` → green; `npm pack --dry-run` lists only `dist`, README, package.json.
- [ ] **Step 5: Commit** `feat: @app-assistant/host, the contract for JS apps`.

### Task 5 (bot repo): the demo and docs on the standard

**Files:** Modify `apps/demo.yaml` → served by the bot's own demo routes as a REST app (`api_base: {base}/demo/api/assistant`) using the same handlers shape; `docs/integration.md` (worked example: Next.js route + `createAssistantHost`); `README.md` (adding an app in 3 steps); Test `tests/test_demo.py` still green.

- [ ] Steps: failing test that the demo turn goes through `RestHttpTools`; implement; full suite + recordings + ruff; commit `feat: the demo app follows the standard too`.

### Task 6 (ai-mono): HookPlaybook serves the contract with the package

**Files:** `products/hookplaybook/app/package.json` (dep `@app-assistant/host` pinned to Task 4's version), `app/lib/assistant-host.ts` (`createAssistantHost` wired to the existing `callAssistantTool` functions in `shared/core/src/assistant.ts` through a thin adapter, `once` → `claimOnce`), `app/app/api/assistant/[...path]/route.ts`, `app/app/.well-known/app-assistant.json/route.ts` (the pack: guide, writes, models, files, cap copied from `/home/claude/app-assistant/apps/hookplaybook.yaml`, `api_base: https://hookplaybook.com/api/assistant`), `app/lib/writer.ts` (use the package's `signToken`, `botFetch`, `readEvents`; delete its own copies); tests `app/test/assistant-host.test.ts`. The MCP assistant tools stay.

- [ ] Steps: failing tests (each endpoint with a real token; 401 without; idempotent drafts; the well-known pack validates against the bot's schema — copy a JSON schema file from Task 1 or check required fields; the MCP route still lists the four assistant tools); implement; app + core tests, typecheck in app/core/jobs; commit `feat(hookplaybook): the assistant through @app-assistant/host`.

### Task 7 (ai-mono): the template and docs

**Files:** `products/_template` (same wiring, off until `ASSISTANT_URL` + `ASSISTANT_TOKEN_SECRET`), `products/hookplaybook/CLAUDE.md`, `docs/PRODUCT.md`, `docs/CODE-MAP.md`, `products/NEW-PRODUCT.md` (adding the assistant), `products/TEMPLATE-SYNC.md` (mark the SSE proxy line ported).

- [ ] Steps: template tests/typecheck green; commit `feat(template): the assistant, off until set up`.

### Task 8 (bot, PR C after PR B is live): HookPlaybook from the registry

**Files:** `apps/registry.yaml` (`- {id: hookplaybook, site: https://hookplaybook.com, secret_env: HOOKPLAYBOOK_SESSION_SECRET}`); `apps/hookplaybook.yaml` stays as the fallback; test that with the registry entry and a reachable pack the REST client is used, and with no reachable pack and no stored copy the YAML/MCP is used.

- [ ] Steps: failing test; implement; full suite; commit `feat: HookPlaybook's pack comes from HookPlaybook`.
