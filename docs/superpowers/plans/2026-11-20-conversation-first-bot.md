# Conversation-first writing bot (design Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the bot's fixed pipeline with one conversational model that talks like a person and writes posts and threads through tools, with quiet checks, `draft`/`saved` events, the widget and demo updated, and conversation tests that pass.

**Architecture:** The LangGraph thread stays (checkpoints, one per conversation) but its node sequence becomes `read_files → load_context → converse → END`. `converse` is an agent loop: the chat model (streamed, with tool definitions) replies to the user and may call `write_drafts`, `revise_draft` or `save_facts`; the bot's code runs those tools (the writer model plus every check) and feeds results back, at most `MAX_TOOL_ROUNDS` times per message. HookPlaybook's `save_draft` learns threads first, in its own PR.

**Tech Stack:** Python 3.12, FastAPI, LangGraph, psycopg, httpx (OpenRouter chat completions with `tools` and `reasoning`), pytest + respx; widget TypeScript + vitest + esbuild. HookPlaybook: TypeScript, Cloudflare Workers, D1, vitest.

**Spec:** `docs/superpowers/specs/2026-11-20-one-assistant-design.md` (section 2, "Mike's answers", Phases item 1).

## Global Constraints

- Models (Mike, 20 Nov 2026): the conversation runs on `anthropic/claude-haiku-5.5`; drafts and revisions on `anthropic/claude-sonnet-5.5` with OpenRouter `reasoning: {effort: "low"}` and 1024 extra `max_tokens` for thinking; fallbacks `[anthropic/claude-haiku-5.5, openai/gpt-6-luna]`. Both packs (`hookplaybook`, `demo`).
- One question per bot message, never a list of questions. Facts restated in clean sentences in the user's language; the user's typos never come back; stored facts are the clean sentences.
- Never treat a greeting, a name, thanks or a question as news: no draft, no saved fact.
- A number may appear in a draft only if it appears in the user's facts (`unsupported_numbers`); enforced in code.
- Every draft passes: numbers, the claim check, the repeat check (against this thread's drafts and the context's recent posts), the pack's rules (length, no links, no dashes). A failing draft is rewritten once; still failing, it is dropped and the tool returns a one-line note for the model to pass on in its own words.
- Nothing the user sees says "Not added", lists check reasons, or shows a facts box. The `card` event, `keep_facts` action and "What I used" UI are retired.
- No fixed English sentences in the conversation; only error states are fixed text.
- `MAX_TOOL_ROUNDS = 4` model calls with tools per message; after that the model must answer in text.
- Untrusted text (user messages, files, links, context) stays inside `<data source=...>` blocks (`prompts.data`).
- Every model call is charged through `on_cost` (ref `"<message_id>:<n>"`); no ref reaches the browser before it is charged (PR #6's `settle`).
- Merge is deploy; migrations only add. HookPlaybook's PR (Task 1) merges before the bot's PR.
- Python: `TEST_DATABASE_URL=postgresql://test:test@localhost:5432/test uv run pytest`, `uv run ruff check`, `uv run ruff format --check`. Widget: `npm test`, `npm run build` (commit `src/assistant/static/widget.js`). HookPlaybook: its own vitest and typecheck per folder; never `pnpm prettier:write` with paths.

## Review Focus

1. A non-news message in the middle of writing ("thanks", "lol", "how are you", "my name is Mike") gets a natural reply, no `draft`, no `saved` — Task 5 test `test_small_talk_mid_writing_writes_nothing`.
2. The model calls `write_drafts` with no confirmed facts — the tool answers "no facts yet" and the model asks a question; the turn doesn't fail — Task 4 test `test_write_drafts_without_facts_returns_a_note`.
3. The model keeps calling tools — after `MAX_TOOL_ROUNDS` the loop forces a text answer and ends — Task 5 test `test_tool_rounds_are_capped`.
4. The model sends malformed tool arguments — the tool result says so and the model continues; nothing raises — Task 5 test `test_bad_tool_arguments_are_reported_to_the_model`.
5. A thread whose third post carries a number not in the facts — caught post by post, rewritten or dropped — Task 4 test `test_thread_number_check_runs_on_every_post`.

---

### Task 1: HookPlaybook's `save_draft` accepts a thread (ai-mono)

**Files:**
- Modify: `products/hookplaybook/shared/core/src/assistant.ts` (the `save_draft` tool's schema and handler)
- Test: `products/hookplaybook/shared/core/test/assistant.test.ts`
- Docs: `products/hookplaybook/docs/PRODUCT.md` (the Write with me row: threads)

**Interfaces:**
- Produces: `save_draft` input `{ text: string, thread?: string[], media?: [{url, alt?}] }`. `thread` is the posts after the first, 0 to `API_MAX_THREAD` (24) items, each 1 to `ASSISTANT_MAX_CHARS` characters, stored in `drafts.thread` as JSON (null when empty), exactly as `apiCreateDraft` stores a thread. The picture stays on the first post. The answer is unchanged (`{id, edit_url}`).

- [ ] **Step 1: Write the failing tests** in `assistant.test.ts`: `save_draft saves a thread` (`thread: ["two", "three"]` → the draft row's `thread` is `["two","three"]`); `save_draft refuses a thread over 24 posts` (400, nothing saved); `save_draft refuses an empty post in a thread` (400).
- [ ] **Step 2:** run them. Expected: FAIL.
- [ ] **Step 3:** implement: add `thread` to the schema (array of strings, `maxItems: API_MAX_THREAD`) and validate and save it the way `readDraftInput` does; update the tool description.
- [ ] **Step 4:** run the core tests and typecheck. Expected: PASS (the 2 known `flow.test.ts` failures excepted).
- [ ] **Step 5:** commit "feat(hookplaybook): the writing bot saves threads"; push; open the ai-mono PR against `main`; tell Mike (merge before the bot's PR).

### Task 2: Tool calling and reasoning in the model client

**Files:**
- Modify: `src/assistant/llm.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Produces:
  - `ToolCall(BaseModel)`: `id: str`, `name: str`, `arguments: dict` (parsed JSON; `{}` and `bad_arguments: str` set when the JSON doesn't parse).
  - `ToolTurn(BaseModel)`: `text: str`, `calls: list[ToolCall]`, `cost_usd: float`, `model: str`.
  - `Llm.chat_tools(messages: list[dict], model: str, fallbacks: list[str], tools: list[dict], max_tokens: int, on_text: Callable[[str], Awaitable[None]] | None = None, reasoning: str | None = None) -> ToolTurn` — streams; text pieces go to `on_text`; tool-call deltas (OpenAI format `choices[0].delta.tool_calls[i]` with `id`, `function.name`, `function.arguments` fragments) are assembled by index. Same fallback rule as `chat`: no fallback once text was sent.
  - `reasoning` (`"low"` or None) also added to `chat` and `chat_json`; sent as `{"reasoning": {"effort": reasoning}}`.

- [ ] **Step 1: Write the failing tests** (respx, SSE bodies): `test_chat_tools_streams_text_and_no_calls`; `test_chat_tools_assembles_a_call_split_across_chunks` (arguments `{"count": 1}` in 3 fragments → `calls[0].arguments == {"count": 1}`); `test_chat_tools_keeps_bad_arguments` (fragment `{"count":` → `arguments == {}`, `bad_arguments == '{"count":'`); `test_chat_tools_falls_back_before_output`; `test_reasoning_is_sent` (request body has `reasoning.effort == "low"`).
- [ ] **Step 2:** run them. Expected: FAIL.
- [ ] **Step 3:** implement in `llm.py`, reusing `_once`'s streaming and `_complete`'s fallback loop (pass `tools` and `reasoning` into the body).
- [ ] **Step 4:** run `tests/test_llm.py`. Expected: PASS.
- [ ] **Step 5:** commit "feat: tool calling and reasoning in the model client".

### Task 3: Pack models and thread output

**Files:**
- Modify: `src/assistant/packs.py`, `apps/hookplaybook.yaml`, `apps/demo.yaml`
- Test: `tests/test_packs.py`, `tests/test_hookplaybook_pack.py`

**Interfaces:**
- Produces: `Models` gains `chat: str`, `writer: str`, `writer_reasoning: Literal["none", "low"] = "low"` (keeps `main`, `cheap`, `vision`, `fallbacks`). `WriteSpec.kind` is `"x_post"` or `"x_thread"`; `WriteSpec` gains `max_posts: int | None = None` (threads); `count` is the default number of drafts (1). `AppPack.interview_guide` is renamed `guide` (what the app is for and what makes a good story), used in the conversation prompt.
- Values: both packs `chat: anthropic/claude-haiku-5.5`, `writer: anthropic/claude-sonnet-5.5`, `fallbacks: [anthropic/claude-haiku-5.5, openai/gpt-6-luna]`, `main`/`cheap`/`vision` unchanged. `hookplaybook.yaml` writes: `x_post` (count 1, max_chars 280, no_links, no_dashes, may_carry_image) and `x_thread` (count 1, max_chars 280 per post, max_posts 8, no_links, no_dashes, may_carry_image on the first post). `demo.yaml`: the same two kinds with its own guide.

- [ ] **Step 1: Write the failing tests:** `test_pack_models_have_chat_and_writer` (both packs load with the values above); `test_x_thread_spec_loads` (`max_posts == 8`); `test_guide_replaces_interview_guide` (a pack still using `interview_guide` fails to load with a clear error).
- [ ] **Step 2:** run them. Expected: FAIL.
- [ ] **Step 3:** implement and update both YAML files; rewrite each `guide` in plain words as background for a person (what the app is, what a good story has: what happened, why it matters or who it's for, a number or detail if any), not as a question list.
- [ ] **Step 4:** run the pack tests. Expected: PASS.
- [ ] **Step 5:** commit "feat: chat and writer models, thread output in packs".

### Task 4: The drafting tools

**Files:**
- Create: `src/assistant/writer.py`
- Modify: `src/assistant/graph/prompts.py` (writer and claim prompts), `src/assistant/migrations/004_drafts.sql` (`alter table pieces add column posts jsonb`)
- Test: `tests/test_writer.py`

**Interfaces:**
- Consumes: `Llm.chat_json(..., reasoning=...)` (Task 2); `Models.writer`, `writer_reasoning`, `WriteSpec` (Task 3); `checks.unsupported_numbers`, `checks.repeats`, `checks.rule_problems`, `checks.Fact`.
- Produces:
  - `Draft(BaseModel)`: `id: str` (ULID), `kind: Literal["x_post", "x_thread"]`, `posts: list[str]` (one item for a post), `image_file_id: str | None`, `fact_ids: list[str]`.
  - `DraftResult(BaseModel)`: `drafts: list[Draft]`, `note: str | None` (one short line about anything dropped or refused, for the model), `cost_usd: float`.
  - `async def write_drafts(w: WriterDeps, brief: str, kind: str, count: int) -> DraftResult`
  - `async def revise_draft(w: WriterDeps, draft: Draft, instruction: str) -> DraftResult`
  - `WriterDeps` (dataclass): `pack`, `llm`, `facts: list[Fact]`, `readings`, `context`, `recent: list[str]` (this thread's earlier drafts' posts plus the context's recent posts), `spend: Callable[[float], Awaitable[None]]` (charges each call), `status: Callable[[str], Awaitable[None]]`.
  - Behaviour: no facts → `DraftResult(drafts=[], note="no facts yet")` with no model call. `count` clipped to 1..3. Each draft: writer model call (`status("writing")`), then every check on every post (`status("checking")`): numbers, the pack's rules per post, repeats, then the claim check (cheap model, `prompts.CLAIM_CHECK`). One rewrite of failing drafts with their problems; still failing → dropped, `note` names the kind of problem in plain words. A thread's posts are checked one by one and the thread as a whole for repeats.

- [ ] **Step 1: Write the failing tests** (FakeLlm): `test_write_drafts_without_facts_returns_a_note` (no model call, `note == "no facts yet"`); `test_write_drafts_returns_one_checked_post`; `test_a_number_not_in_facts_is_rewritten_then_dropped` (`drafts == []`, `note` mentions a number); `test_thread_number_check_runs_on_every_post` (number only in post 3 → rewritten); `test_repeat_of_a_recent_post_is_rewritten`; `test_revise_draft_keeps_the_checks` ("shorter" output with a new number → rewritten); `test_every_writer_call_is_charged` (spend called once per model call).
- [ ] **Step 2:** run them. Expected: FAIL.
- [ ] **Step 3:** implement `writer.py`, moving the check logic from `graph/nodes.py` (`_problems`, the claim check, `_image_allowed`) rather than duplicating it; add the migration.
- [ ] **Step 4:** run `tests/test_writer.py`. Expected: PASS.
- [ ] **Step 5:** commit "feat: drafting tools with quiet checks".

### Task 5: The conversation agent

**Files:**
- Create: `src/assistant/graph/agent.py`
- Modify: `src/assistant/graph/build.py`, `src/assistant/graph/state.py`, `src/assistant/graph/prompts.py`; delete the old nodes and prompts in `src/assistant/graph/nodes.py` that `converse` replaces (`interview`, `ask_back`, `extract_facts`, `write`, `check`, `present` and their prompts), keeping `read_files` and `load_context`
- Test: `tests/test_agent.py`; rewrite `tests/test_graph.py` for the new graph

**Interfaces:**
- Consumes: `Llm.chat_tools` (Task 2), `write_drafts`, `revise_draft`, `Draft` (Task 4), `Turn.emit`, `Turn.spend`, `AppTools.save_material`.
- Produces:
  - Graph: `START → read_files → load_context → converse → END`.
  - `async def converse(state: ChatState, t: Turn) -> dict` — builds the system prompt (`prompts.CONVERSE`: the Global Constraints' conversation rules in plain words, the pack's `guide`, the user's name if known, the context, confirmed facts, readings, this thread's drafts with their ids), calls `chat_tools` with the pack's `chat` model and `TOOL_SPECS`, streams text as `text` events, runs called tools, appends tool results as `role: "tool"` messages, repeats up to `MAX_TOOL_ROUNDS = 4`, then calls once more without tools if needed. Emits `status` `thinking` at start.
  - `TOOL_SPECS`: `write_drafts {brief: str, kind: "x_post"|"x_thread", count: 1..3}`, `revise_draft {draft_id: str, instruction: str}`, `save_facts {facts: [{text: str}] (1..10, each ≤ 300 chars)}`.
  - `save_facts`: adds the clean facts to `state.facts` (numbers parsed with `numbers_in`), calls `tools.save_material(facts, None, key)`, emits `saved {kind: "facts", items: [fact texts]}`, and returns `"saved N facts"` to the model. The context's kept facts are in the system prompt, so the model can offer them in its own words.
  - Each passing draft: `draft {id, kind, posts, image_file_id?}`; drafts stored in `ChatState.drafts: list[Draft]` (replaces `pieces`, `past_pieces`, `rounds_asked`, `ready`, `ask_hint`, `problems`, `passed`, `rewrites`, `just_write`).
  - `POST /messages` may carry `draft_id`: the user's message is then shown to the model as about that draft.

- [ ] **Step 1: Write the failing tests** (FakeLlm scripting tool calls): `test_hi_gets_a_reply_and_no_draft`; `test_small_talk_mid_writing_writes_nothing`; `test_save_facts_saves_clean_facts_and_emits_saved`; `test_write_drafts_emits_draft_events`; `test_revise_uses_the_named_draft` (message with `draft_id`); `test_tool_rounds_are_capped` (model calls a tool 6 times → 4 tool rounds then a text answer); `test_bad_tool_arguments_are_reported_to_the_model` (`bad_arguments` set → tool message "the arguments weren't valid JSON", no exception); `test_every_model_call_is_charged`; `test_user_text_is_wrapped_as_data` (the prompt carries the user's text inside `<data`).
- [ ] **Step 2:** run them. Expected: FAIL.
- [ ] **Step 3:** implement `agent.py`, rewire `build.py`, slim `state.py` (add `drafts: list[Draft]`, `user_name: str | None`; register `Draft` in `STATE_TYPES`), write `prompts.CONVERSE`.
- [ ] **Step 4:** run the full Python suite. Expected: PASS (old graph tests replaced, not skipped).
- [ ] **Step 5:** commit "feat: the bot holds a real conversation and writes through tools".

### Task 6: API, storage and actions for drafts

**Files:**
- Modify: `src/assistant/api/turn.py` (save drafts on `draft` events instead of `card`), `src/assistant/api/chat.py` (`POST /messages` accepts `draft_id`; actions: `add_draft` sends `posts` as `text` + `thread`; `keep_facts` removed; thread read returns `drafts` and `saved` lines), `src/assistant/tools.py` (`save_draft(text, thread, media, key)`), `README.md` (stream events list)
- Test: `tests/test_api_chat.py`, `tests/test_tools.py`

**Interfaces:**
- Consumes: `Draft` (Task 4), `draft`/`saved` events (Task 5), HookPlaybook `save_draft` with `thread` (Task 1).
- Produces: `AppTools.save_draft(text: str, thread: list[str], media: list[dict], key: str) -> SavedDraft`; `add_draft` action answers `{draft_id, edit_url}` and marks the draft's facts told (`save_material(facts, draft_id)`); `GET /threads/{id}` returns each assistant message with its `drafts` (id, kind, posts, added, edit_url) so a reload renders them.

- [ ] **Step 1: Write the failing tests:** `test_draft_event_is_stored`; `test_add_draft_saves_a_thread` (FakeTools records `thread == posts[1:]`); `test_add_draft_marks_facts_told`; `test_keep_facts_is_gone` (422); `test_thread_read_returns_drafts_without_charge_refs`; `test_message_with_draft_id_of_another_thread_is_404`.
- [ ] **Step 2:** run them. Expected: FAIL.
- [ ] **Step 3:** implement.
- [ ] **Step 4:** run the full Python suite. Expected: PASS.
- [ ] **Step 5:** commit "feat: drafts and threads through the API".

### Task 7: Widget renders the conversation

**Files:**
- Modify: `widget/src/api.ts` (`BotEvent` gains `draft`, `saved`; `send(..., draftId?)`), `widget/src/panel.ts` (remove card and facts rendering; render drafts and saved lines), `widget/src/styles.ts`
- Test: `widget/test/panel.test.ts`, `widget/test/api.test.ts`

**Interfaces:**
- Consumes: the `draft` and `saved` events and thread read (Tasks 5–6).
- Produces: a draft renders as the post's text in a soft block (a thread as numbered posts), with **Add to Schedule**, **Shorter**, **Another angle**; Shorter and Another angle send the messages "Make it shorter" and "Try another angle" with that `draftId`; after Add, the button becomes "Added to your Schedule" with an **Edit** link to `edit_url`. A `saved` event renders one quiet muted line: a check icon and the saved facts' texts joined by "; " (UI chrome, not conversation). Bot text keeps streaming into its bubble as today; the working indicator stays.

- [ ] **Step 1: Write the failing tests:** `renders a draft with its three actions`; `renders a thread as numbered posts`; `Shorter sends the message with the draft id`; `Add to Schedule becomes Added with an Edit link`; `renders a saved line quietly`; `no facts box or Keep button anywhere`; `reload renders drafts from the thread`.
- [ ] **Step 2:** run them. Expected: FAIL.
- [ ] **Step 3:** implement; rebuild `widget.js`.
- [ ] **Step 4:** `npm test` and `npm run build`. Expected: PASS.
- [ ] **Step 5:** commit "feat: the widget shows drafts in the conversation".

### Task 8: Conversation tests

**Files:**
- Create: `tests/conversations/cases.yaml`, `tests/conversations/recorded/` (one JSON per case), `scripts/converse_eval.py`, `tests/test_conversations.py`
- Modify: `README.md` (how to run and record)

**Interfaces:**
- Consumes: the whole bot through `run_turn` with `FakeTools` (a fixed HookPlaybook context: name Mike, two recent posts, no kept facts).
- Produces:
  - `cases.yaml`: about 20 cases, each `{name, turns: [user messages, optional file fixture], expect: [plain-language notes], hard: {drafts: 0|">=1", saved: 0|">=1", language?: "vi", max_questions_per_reply: 1}}`. Must include Mike's: `hi`, `how are you`, `my name mike`, `i deployed chat bot help user plan weet`, `just write it` with thin facts, `shorter` after a draft, a screenshot with a number, a Vietnamese message, a product question, `give me a thread`, `thanks` after a draft, a number the user never gave requested by the user ("say we have 10k users").
  - `scripts/converse_eval.py --live` runs every case against the real models (needs `OPENROUTER_API_KEY`), checks `hard` in code, asks a judge (`anthropic/claude-sonnet-5.5`) whether each `expect` note holds, prints a table, exits non-zero on any failure; `--record` saves each case's model responses to `recorded/`.
  - `tests/test_conversations.py` replays `recorded/` through a replaying `Llm` and asserts every `hard` rule (runs in CI without a key).
- [ ] **Step 1:** write `cases.yaml` and `test_conversations.py` (replay + hard rules).
- [ ] **Step 2:** write `converse_eval.py`; run `--live --record` (the controller supplies `OPENROUTER_API_KEY` from Mike or runs it after merge on a preview). Expected: every case passes; failures are fixed in the prompt (`prompts.CONVERSE`) or tools, then re-recorded.
- [ ] **Step 3:** run the full Python suite. Expected: PASS.
- [ ] **Step 4:** commit "test: conversation tests from real chats".

### Task 9: Demo, docs and the pull request

**Files:**
- Modify: `src/assistant/demo/` (the demo's tools save drafts and threads like HookPlaybook's), `src/assistant/demo/page.html` (copy about the bot), `README.md`, the spec's Phases (mark Phase 1 built)
- Test: `tests/test_demo.py`

- [ ] **Step 1: Write the failing test:** `test_demo_saves_a_thread`.
- [ ] **Step 2:** run it. Expected: FAIL.
- [ ] **Step 3:** implement; update README (events: `thread`, `status`, `text`, `transcript`, `draft`, `saved`, `failed`, `done`; actions; packs' `guide`, `chat`, `writer`).
- [ ] **Step 4:** run the full Python and widget suites, ruff, `npm run build`. Expected: PASS.
- [ ] **Step 5:** commit "docs: conversation-first bot"; push; open the app-assistant PR against `main`; tell Mike, noting it merges after the HookPlaybook PR (Task 1).
