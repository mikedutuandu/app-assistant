# Phase 2: speed and the bot's data on Vercel — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure every step of a turn, run the bot in US east next to HookPlaybook's D1, move its data to Neon (Postgres plus Neon Object Storage, installed through the Vercel Marketplace), and polish two conversation slips found in the last review.

**Architecture:** One timing object per turn records each model call, tool call and graph step and logs one line at the end. The database code becomes safe behind a transaction pooler and takes a separate unpooled URL for LangGraph's checkpoints. Uploads keep the existing S3 path (boto3, presigned PUT), pointed at Neon Object Storage, which is S3-compatible, so no new storage backend is written; the bucket's CORS is set at startup. HookPlaybook logs the time of each step of the assistant's tool calls (its own PR) so the 2–3 s per call can be found and fixed in a follow-up plan, once measured.

**Tech Stack:** Python 3.12, FastAPI, LangGraph, psycopg 3 + psycopg_pool, boto3, pytest; Vercel (Python runtime, `vercel.json`); Neon (Postgres, Object Storage) via the Vercel Marketplace. HookPlaybook: TypeScript, Cloudflare Workers, D1, vitest.

**Spec:** `docs/superpowers/specs/2026-11-20-one-assistant-design.md` (section 5 "Speed"; Phases item 2).

**Decision taken while planning (Claude, 20 Nov 2026, for Mike to confirm):** files go to **Neon Object Storage** instead of Vercel Blob. Both arrive through the Vercel Marketplace, but Neon's is S3-compatible (boto3, SigV4 presigned PUT/GET, batch delete, list by prefix, `PutBucketCors`), so the bot's existing S3 code serves it with only settings changed, and S3 is also what the open-source Phase 4 needs. Vercel Blob would need a new backend and a different browser-upload protocol.

## Global Constraints

- Merge is deploy; migrations only add; a merge to `main` must be all a change needs (Mike).
- **Nothing breaks the live deployment before Mike switches it:** with today's settings (Supabase `DATABASE_URL`, `SUPABASE_URL`/`SUPABASE_SERVICE_KEY`, no `DATABASE_URL_UNPOOLED`) the bot behaves exactly as now.
- Neon's pooled `DATABASE_URL` runs PgBouncer in transaction mode: no server-side prepared statements on the app pool; LangGraph's saver uses `DATABASE_URL_UNPOOLED` when set (the names the Vercel Neon integration injects), else `DATABASE_URL`.
- Region: the function runs in `iad1` (US east), next to HookPlaybook's D1 (`enam`) and the Neon project (AWS `us-east-1`).
- Timing logs carry names and milliseconds only: never message text, prompts, file contents, URLs with signatures, tokens or keys.
- The context is fetched once per writing conversation and refreshed when older than 10 minutes (spec: "about 10 minutes is a fine start").
- Every model call stays charged through `on_cost`; no change to charging.
- Python: `TEST_DATABASE_URL=postgresql://test:test@localhost:5432/test uv run pytest` (with `REQUIRE_CONVERSATION_RECORDINGS=1` before committing), `uv run ruff check`, `uv run ruff format --check`. HookPlaybook: its own vitest and typecheck per folder; never `pnpm prettier:write` with paths.
- The OpenRouter key, when a live run needs it, is loaded only inside the command that runs it, never printed, written or committed.

## Review Focus

1. A turn that fails, is capped or is cancelled still logs its timing line exactly once — Task 1 test `test_timing_is_logged_once_when_the_turn_fails`.
2. The timing line never contains the user's words or a signed URL — Task 1 test `test_timing_line_holds_no_user_text`.
3. The app pool behind a transaction pooler never prepares statements — Task 2 test `test_app_pool_never_prepares_statements`.
4. Setting the bucket's CORS fails at startup (no permission, or a store without CORS) — the bot still starts and logs one warning — Task 3 test `test_cors_failure_does_not_stop_startup`.
5. The bot uploads to Neon Object Storage and Neon ignores `If-None-Match` — the finish step still refuses an object whose size differs from what was promised — Task 3 test `test_finish_checks_size_on_s3_backend` (existing behaviour, pinned).

---

### Task 1: Per-step timing for every turn

**Files:**
- Create: `src/assistant/timing.py`
- Modify: `src/assistant/graph/turn.py` (model calls), `src/assistant/tools.py` (`McpHttpTools._call`), `src/assistant/graph/build.py` or the node wrappers (graph steps), `src/assistant/api/turn.py` (one timer per turn; log at the end), `src/assistant/graph/nodes.py` (`CONTEXT_TTL = 600`)
- Test: `tests/test_timing.py`, additions to `tests/test_api_chat.py`

**Interfaces:**
- Produces:
  - `class TurnTimer`: `__init__(self, clock: Callable[[], float] = time.perf_counter)`; `span(self, kind: Literal["model", "tool", "step"], name: str) -> AbstractAsyncContextManager[None]`; `mark_first_text(self) -> None` (first streamed text of the turn; later calls ignored); `summary(self) -> dict` → `{"total_ms": int, "first_text_ms": int | None, "model": [{"name": str, "ms": int}], "tool": [...], "step": [...]}`.
  - `log_turn(timer: TurnTimer, app: str, outcome: Literal["done", "failed", "capped", "cancelled"]) -> None`: one `INFO` line on logger `assistant.timing`, message `"turn timing %s"` with the summary plus `app` and `outcome` as compact JSON.
  - `Turn` gains `timer: TurnTimer`; `McpHttpTools.__init__` gains `timer: TurnTimer | None = None`.
- Names used: model spans are named by model id (`anthropic/claude-sonnet-5.5`); tool spans by tool name (`get_context`, `save_draft`, `save_material`, `charge`); steps by node name (`read_files`, `load_context`, `converse`).

- [ ] **Step 1: Write the failing tests:** `test_spans_are_measured` (fake clock: a model span 0→120 ms and a tool span 120→150 → `summary()["model"] == [{"name": "m", "ms": 120}]`, tool `[{"name": "save_draft", "ms": 30}]`); `test_first_text_is_the_first_mark_only`; `test_turn_logs_one_timing_line` (API: one `done` turn → exactly one `assistant.timing` record, outcome `done`, with `first_text_ms` set); `test_timing_is_logged_once_when_the_turn_fails` (a model failure → one record, outcome `failed`); `test_timing_line_holds_no_user_text` (the user's message text and the context's posts are absent from the record); `test_context_is_reused_for_ten_minutes` (`CONTEXT_TTL == 600` and a second turn within it makes no `get_context` call).
- [ ] **Step 2:** run them. Expected: FAIL (module missing).
- [ ] **Step 3:** implement; charge calls (`charge`) are timed but run off the critical path as today (PR #6), so their span may end after `done`'s timing is taken — exclude `charge` from `total_ms`'s critical path by recording it only if it finished before the line is logged.
- [ ] **Step 4:** run the full Python suite. Expected: PASS.
- [ ] **Step 5:** commit "feat: log the time of every step of a turn".

### Task 2: Database ready for Neon, region US east

**Files:**
- Modify: `src/assistant/config.py` (`database_url_unpooled: str | None = None`, `""` → None like the other optional fields), `src/assistant/db.py` (`open_pool`: `kwargs={"prepare_threshold": None}`), `src/assistant/main.py` (saver pool on `settings.database_url_unpooled or settings.database_url`), `vercel.json` (`"regions": ["iad1"]`), `.env.example`, `README.md` (deploy section: the two URLs)
- Test: `tests/test_db.py`, `tests/test_config.py` (create if absent)

**Interfaces:**
- Produces: `Settings.database_url_unpooled: str | None`; `saver_url(settings: Settings) -> str` in `src/assistant/db.py` (returns the unpooled URL when set, else `database_url`).

- [ ] **Step 1: Write the failing tests:** `test_app_pool_never_prepares_statements` (open the app pool on `TEST_DATABASE_URL`, run the same query 6 times, then `select count(*) from pg_prepared_statements` on that connection is 0); `test_saver_uses_the_unpooled_url_when_set`; `test_saver_falls_back_to_database_url`; `test_empty_unpooled_url_is_unset`; `test_vercel_runs_in_iad1` (`json.load(open("vercel.json"))["regions"] == ["iad1"]`).
- [ ] **Step 2:** run them. Expected: FAIL.
- [ ] **Step 3:** implement.
- [ ] **Step 4:** run the full Python suite. Expected: PASS.
- [ ] **Step 5:** commit "feat: ready for Neon's pooled and direct URLs; run in US east".

### Task 3: Uploads on Neon Object Storage (S3 path)

**Files:**
- Modify: `src/assistant/files/storage.py` (`Storage.ensure_cors(origins: list[str]) -> None`), `src/assistant/main.py` (at startup, when `storage.upload_backend == "s3"`, call `ensure_cors` with every pack's `allowed_origins` plus `public_base_url`'s origin; any exception → one `WARNING` on `assistant.storage`, startup continues), `.env.example`, `README.md` (Neon Object Storage settings: `S3_ENDPOINT`, `S3_BUCKET`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_REGION`; leave `SUPABASE_*` unset)
- Test: `tests/test_storage.py`

**Interfaces:**
- Consumes: `Settings`, `AppPack.allowed_origins` (already resolved by `packs.py`).
- Produces: `Storage.ensure_cors(origins: list[str]) -> None` — one `put_bucket_cors` with a single rule: `AllowedOrigins` = the sorted, de-duplicated origins; `AllowedMethods` `["GET", "PUT"]`; `AllowedHeaders` `["content-type", "if-none-match"]`; `MaxAgeSeconds` 3600.

- [ ] **Step 1: Write the failing tests** (botocore `Stubber`): `test_ensure_cors_sets_one_rule_for_the_origins` (expected params exactly as above for `["https://b.example", "https://a.example", "https://a.example"]`); `test_cors_failure_does_not_stop_startup` (the stubbed call raises `ClientError` → app starts, one warning logged); `test_cors_is_not_set_on_the_supabase_backend`; `test_finish_checks_size_on_s3_backend` (an upload whose stored size differs from the promised size is refused, with the S3 backend — pin the existing check).
- [ ] **Step 2:** run them. Expected: FAIL.
- [ ] **Step 3:** implement.
- [ ] **Step 4:** run the full Python suite. Expected: PASS.
- [ ] **Step 5:** commit "feat: uploads work on Neon Object Storage; the bucket's CORS is set at startup".

### Task 4: HookPlaybook logs the time of each assistant tool call (ai-mono, own PR)

**Files:**
- Modify: `products/hookplaybook/shared/core/src/assistant.ts` (the four assistant tools' handler) and the MCP route that authenticates the token (find it from `assistant.ts`'s callers)
- Test: `products/hookplaybook/shared/core/test/assistant.test.ts`

**Interfaces:**
- Produces: one `console.log` per assistant tool call: `JSON.stringify({ kind: "assistant_tool_timing", tool, ms_total, ms_auth, ms_claim, ms_work, colo })`, where `ms_claim` is the idempotency claim (0 when the tool has none), `ms_work` the tool's own queries, and `colo` Cloudflare's `request.cf?.colo` when available (else `null`). No user data, no token. Behaviour and answers unchanged.

- [ ] **Step 1: Write the failing test:** `assistant tool calls log their timing` (spy on `console.log`; a `save_draft` call logs one line with `kind === "assistant_tool_timing"`, `tool === "save_draft"`, numeric `ms_total >= ms_auth`, and no draft text in the line).
- [ ] **Step 2:** run it. Expected: FAIL.
- [ ] **Step 3:** implement with `performance.now()` (or `Date.now()` where the Workers clock only advances on I/O).
- [ ] **Step 4:** run the core tests and typecheck. Expected: PASS (the known `flow.test.ts` failures excepted).
- [ ] **Step 5:** commit "chore(hookplaybook): time each step of the assistant's tool calls"; push; open the ai-mono PR against `main`; tell Mike. It can merge in any order with the bot's PR.

### Task 5: Two conversation slips (review N2, N3) and re-recording

**Files:**
- Modify: `scripts/converse_eval.py` (`questions()`), `src/assistant/graph/agent.py` (the "Drafts in this chat" block), `src/assistant/graph/prompts.py` only if needed, `tests/conversations/recorded/*` (re-recorded)
- Test: `tests/test_converse_eval.py`, `tests/test_agent.py`

**Interfaces:**
- Consumes, from the last review:
  - **N2:** `questions()` (`scripts/converse_eval.py`, `_ONE_MORE` / `_EXAMPLES`) counts "X, or <wh-question>?" as one ask, so the bot's observed two-ask form passes. Count `or` followed by a wh-word or "do/did/have you" as one more ask (keep "or are you just / or should I" as a choice), and apply the examples cut only to `or`, not to "and do you".
  - **N3:** after `write_drafts`, the next round's system prompt lists the draft just written under "Drafts in this chat" (`agent.py`), and next to the rule about writing a new version when a draft already exists, the model calls the chat's first draft "a fresh take" / "bản mới" (6 recordings). Mark drafts written in this message as such in the system prompt.

- [ ] **Step 1: Write the failing tests:** `test_or_with_a_new_question_counts_as_two_asks` (`questions("Who asked for it most, or what made you ship it now?") == 2`; `questions("Who is it for, or do you have a number?") == 2`; `questions("Is 10k real, or are you just pitching it?") == 1`); `test_drafts_written_this_message_are_marked_new` (after `write_drafts` in a turn, the next round's system prompt marks those drafts "written just now, in this message", and a chat with no earlier drafts never calls the first one another version).
- [ ] **Step 2:** run them. Expected: FAIL.
- [ ] **Step 3:** implement.
- [ ] **Step 4:** re-record the cases the replay reports as stale: `--live --record` per case, at most 2 tries each, spend limit $2; then the full suite with `REQUIRE_CONVERSATION_RECORDINGS=1`. Expected: PASS, 22/22 recorded.
- [ ] **Step 5:** commit "fix: one ask counts 'or' questions; a first draft is never called another version".

### Task 6: Switch-over guide, then the pull request

**Files:**
- Create: `docs/DEPLOY.md` (Mike's one-time switch-over, written for his phone)
- Modify: `README.md` (link), the spec's Phases (Phase 2: built, switch-over pending)

- [ ] **Step 1:** write `docs/DEPLOY.md`: (1) in Vercel, Marketplace → Neon → install on the `app-assistant` project, region AWS US East (N. Virginia), which adds `DATABASE_URL` and `DATABASE_URL_UNPOOLED`; (2) in the Neon console, enable Object Storage, create bucket `app-assistant`, create a credential, and add `S3_ENDPOINT`, `S3_BUCKET`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_REGION` to the Vercel project; (3) remove `SUPABASE_URL` and `SUPABASE_SERVICE_KEY`; (4) redeploy; the startup migration builds the tables and the CORS rule; (5) check: `/demo` chat, an image upload, and a `turn timing` line in the Vercel logs; (6) the Supabase project can be paused after a week.
- [ ] **Step 2:** run the full Python and widget suites, ruff, `npm run build` (widget.js unchanged). Expected: PASS.
- [ ] **Step 3:** commit "docs: switching the bot's data to Neon on Vercel"; push; open the app-assistant PR against `main`; tell Mike, with the switch-over guide's link.

After merge and switch-over, a short follow-up plan reads the `turn timing` and `assistant_tool_timing` logs and fixes what is slow (target: tool call under about 300 ms, first words about 1 s).
