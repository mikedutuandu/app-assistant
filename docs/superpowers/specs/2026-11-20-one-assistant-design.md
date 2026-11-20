# One assistant in HookPlaybook: Ask HookPlaybook writes with you

Date: 20 Nov 2026. Status: design, for Mike's approval.
Repos: `mikedutuandu/ai-mono` (HookPlaybook) and `mikedutuandu/app-assistant` (the writing bot).
Builds on: `2026-11-20-app-writing-bot-design.md` (Phases 1, 1b and 2, all merged and live).

## Why

HookPlaybook now has two chats side by side: **Ask HookPlaybook** (`/dashboard/ask`, a polished full page that answers questions about the user's own account) and **Write with me** (the bot's small floating panel, which interviews the user and writes posts from their real facts). Mike tried Write with me live on 20 Nov 2026 and found three problems:

1. **Two buttons confuse.** Which one writes a post? Ask even has its own "Write a thread from my notes" skill.
2. **It feels like a machine.** Every message runs the same pipeline (facts, write, check, cards). It writes from two thin facts, shows rejected drafts and an internal "What I used" box, plays the user's typos back verbatim, answers "hi" by running the whole pipeline, and uses fixed English lines.
3. **It is slow.** Live turns took 8 to 32 s, even for "hi". From the logs: each model call waited 2 to 3 s for HookPlaybook to confirm its charge (fixed separately in app-assistant PR #6); each call to HookPlaybook's tools takes 2 to 3 s; and "hi" runs about 5 model calls in a row.

Mike's decisions (20 Nov 2026):
- One assistant, on **Ask's screen and Ask's look**.
- Keep the name **Ask HookPlaybook**.
- Writing starts both from a chip ("Write a post with me") **and** when Ask notices a clear request to write a post.
- The writing conversation must feel natural, like talking to a person.

## Goals

- One place to talk to HookPlaybook: questions about the account and writing posts, in the same chats and the same history.
- Writing feels like a good ghostwriter: it talks first, asks one thing at a time, plays back what it understood in clean words, writes when there is a story, shows the draft as text in the chat, and takes "shorter", "another angle" or "add the number" as normal messages.
- Fast: first words of any reply within about 1 s; a checked draft within about 10 to 15 s of the message that asks for it, with "Writing…" shown meanwhile (p50, live, excluding file reading).
- Everything that keeps posts honest stays: numbers only from the user's facts, the repeat check, the claim check. It just stays quiet unless it changes something.

## Non-goals

- Rebuilding Ask's own skills (recap, flops, trending, growth plan, people, score) in the bot. Ask's engine keeps them.
- StreakPlan, SiteSeed (Phases 3 and 5) and designed images (Phase 4).
- Changing Ask's look beyond what writing needs (attach, voice, draft actions).
- Removing the bot's widget: the public demo and other apps keep using it.

## Anti-goals (a failure even if it works)

- Two chat histories, or a writing chat that can't be found in Ask's list later.
- A "smarter" bot that invents a number, a result or a feeling the user didn't give.
- Losing anything Ask does today (streaming, skills, action cards, thumbs, Write it again, phone layout).
- The bot's session token readable by the page's JavaScript.
- A writing chat that still reads as a form: boxes for facts, visible rejected drafts, internal reasons.

## Approaches considered

1. **One screen, two engines (chosen).** Ask's page and history; Ask's engine answers account questions; the bot writes posts. Keeps everything that works on both sides; the bot stays reusable for other apps through its API.
2. **Move everything into the bot.** One engine, but weeks of rebuilding Ask's HookPlaybook-specific skills and rules, and the bot loses its "works in any app" shape. Rejected.
3. **Keep two chats, restyle the bot's panel.** Least work, but leaves the confusion that started this. Rejected.

Within approach 1, how Ask's page talks to the bot (Claude's call):

- **(a) Through HookPlaybook's server (chosen).** The page posts to a HookPlaybook route; HookPlaybook mints the session token server-side, calls the bot, and passes the bot's stream through. One history in HookPlaybook's own `chat_messages`, normal cookie sign-in, no CORS, and the token never reaches the browser.
- (b) The browser calls the bot directly, as the widget does. Less HookPlaybook code, but two histories, and the token sits in page JavaScript (the Phase 2 review's I4).

## How it works

### 1. Who answers a message

Each message in an Ask chat is answered by one of two engines:

- **Ask** (today's engine, unchanged): questions about the account, the 12 skills.
- **Writer** (the bot): writing posts.

The writer answers when:
- the user taps **Write a post with me** (a new chip on the empty chat, first in the "+" menu's Write group), or attaches a file or a voice note; or
- Ask's router judges the message a clear request to write a post ("help me write a post about…", "turn this into a tweet", "draft something about my launch"); or
- the chat already has a writing conversation in progress and the message continues it ("shorter", "use the second one", "the number was 120").

A message in a writing chat that is clearly an account question ("when should I post this?") goes to Ask, in the same chat; the writing conversation stays where it was for the next writing message.

**The router** is one small, fast model call (or rules first, then the model only when unsure; the builder's call), given the message and whether a writing conversation is in progress. It returns `ask` or `write`. When unsure, the answer is `ask` for a new chat and `write` inside a writing conversation. The chip and attachments skip the router. A wrong route costs one answer, and the user can tap the chip or say "write it".

Ask's skill **Write a thread from my notes** becomes a writer conversation started with the notes. **Score this draft** stays Ask's.

### 2. The writing conversation (bot, conversation-first)

Mike tried the bot again on 20 Nov 2026 (16:54): "hi" got the same question about his chatbot twice, "how are you" got it again, and "my name mike" was turned into two posts ("I'm Mike. What are you building?") with "my name mike" under "What I used". His words: "I expect the bot to communicate well, like a human, not like a machine that can only ask and answer some specific thing."

So the bot stops being a fixed pipeline that treats every message as a fact for a post. **It becomes one conversational model that holds the conversation the way a good assistant does, and writes posts through tools it chooses to call.**

**The conversation model** reads the whole conversation, the context (voice, recent posts, kept facts) and the facts so far, and replies to the message in front of it: it answers small talk and questions naturally, remembers the user's name and what they said, asks one thing at a time when the story is thin, plays back what it understood in clean words, and decides when to write. Its reply streams as `text`.

**Its tools** (called by the model, run by the bot's code):

| Tool | What it does |
|---|---|
| `write_drafts(brief, count)` | Writes `count` drafts (default 1) from the facts the user confirmed, in their voice, then runs every check (numbers only from the user's facts, the claim check, the repeat check, X's length). A draft that fails is rewritten once; one that still fails is dropped and the tool says why in a short line the model can pass on in its own words. Each passing draft is sent as a `draft` event. |
| `revise_draft(draft_id, instruction)` | "Shorter", "another angle", "add the number": the same checks. |
| `save_facts(facts)` | Saves confirmed facts, in clean sentences, through the app's `save_material`. |
| `read_file` / `read_link` | As today: files and links are read and their facts come back to the model. |

The model only calls `write_drafts` when the story has what happened plus why it matters or who it's for (a number or detail if the user has one), or when the user asks for a post. It never treats a greeting, a name or a question as news.

Rules (they hold whatever the model decides; most are checked in code, the rest by the conversation tests below):
- **One question per message.** Never a list of questions.
- **Clean words.** Facts are restated in clean, correct sentences in the user's language. The user's typos never come back. What gets stored is the confirmed clean sentence.
- **Drafts in the chat.** One draft by default, shown as the post's text with small actions under it: **Add to Schedule**, **Another angle**, **Shorter**, **Edit** (opens HookPlaybook's editor on the saved draft). "Give me 3" gets 3. A draft the user added shows "Added to your Schedule" in place of the button.
- **Checks stay, quietly.** Nothing the user sees says "Not added", lists reasons or shows a "What I used" box.
- **Facts saved quietly.** The chat shows one quiet line ("I'll remember this for next time"). Kept facts from earlier chats are offered in the greeting when relevant, in the model's own words.
- **Voice and language.** Everything the bot says is written by the model in the user's language and voice. No fixed English sentences in the conversation (only error states are fixed text).
- **Numbers.** The model may only put a number in a draft that appears in the user's facts; the tool enforces it, not the model's good will.

**Conversation tests.** A set of about 20 real conversations, each with what a good reply does and must not do, run against the live models before any release and in CI with recorded answers. They start with Mike's screenshots:
- "hi" → greets back (by name if known), at most one light question; no post.
- "how are you" → answers like a person; no repeated question.
- "my name mike" → "Nice to meet you, Mike"; no post, nothing saved as a fact.
- "i deployed chat bot help user plan weet" → plays it back cleanly ("You shipped a chatbot that helps people plan their week of posts?"), one question.
- "just write it" with thin facts → writes one honest draft from what it has, no invented numbers.
- "shorter" after a draft → a shorter version of that draft.
- a screenshot with a number → the number is played back and may be used.
- a message in Vietnamese → the reply is in Vietnamese.
- a question about the product or the account → a natural answer or a hand-off to Ask, never a post.
A judge model scores each conversation against its notes; the bot is not done until all pass.

The bot keeps one LangGraph thread per writing conversation (as today), now a tool-calling agent loop instead of the fixed node sequence.

**Stream events** (bot API, also used by the widget): `thread`, `status`, `text`, `transcript`, `failed`, `done`, plus:
- `draft`: `{id, text, media?}`, one per draft that passed its checks;
- `saved`: `{kind: "facts" | "draft", text}` for the quiet lines.
The `card` event, the "What I used" block and the Keep button are retired; the widget renders `draft` and `saved` the same way Ask does.

Actions on a draft (`add`, `shorter`, `another_angle`) go through the bot's actions endpoint (as `add_draft` does today); "Shorter" and "Another angle" are also accepted as ordinary messages.

**One conversation, two engines** (Claude's call, from the builder check): each engine sees what the other said in the same chat. A draft's text is also saved in `chat_messages.content`, so Ask understands "when should I post this?". The bot's `POST /messages` gains an optional `context` field with the chat's recent turns that the bot didn't write (Ask's answers), wrapped as untrusted data like everything else the bot reads.

### 3. Ask's page

- **Composer:** an attach button (images, PDFs) and a voice button, next to "+". Attaching starts or continues a writing conversation. Uploads keep going straight from the browser to Supabase Storage (the bot's signed upload links), with the small JSON calls (`uploads`, `finish`) passed through HookPlaybook's server.
- **Answers:** a writer answer looks like an Ask answer (plain text, no box, the same quiet icons: Copy, thumbs; Write it again on the last one, which re-asks the writer). A draft is the post's text in a soft block with its actions underneath, the same weight as Ask's action cards.
- **Waiting:** Ask's own `Thinking` line shows the writer's `status` steps ("Reading your screenshot", "Writing", "Checking the numbers").
- **History:** writer messages are saved in `chat_messages` like Ask's, so the chat list, titles, deleting a chat and "New chat" work unchanged.
- **Phone:** the same as Ask today; attach and voice fit in the one-row composer.
- The floating **Write with me** button and the widget injection are removed from the dashboard (sidebar and phone). The privacy page keeps naming the bot.

### 4. HookPlaybook's server

- A route (for example `POST /api/ask/write`, beside `/api/ask/stream`) that: checks the viewer, the chat id and `ASK_PER_HOUR` as Ask does; saves the user's message; mints a short session token for the workspace (`signAssistantToken`, server-side only); calls the bot's `POST /v1/chat/hookplaybook/messages` with it; passes the bot's events to the page as server-sent events in Ask's own format; and saves the writer's answer (and its drafts) in `chat_messages` as it goes, so a page that drops the connection can read it later as Ask's answers can today.
- Pass-through routes for uploads (`uploads`, `finish`) and draft actions.
- The browser session route `POST /api/assistant/session` is removed: nothing in the browser needs a token any more.
- **Deleting a chat deletes the bot's copy** (Claude's call, from the builder check: the privacy page names the bot): `deleteChat` calls a new bot endpoint that deletes one thread (its checkpoints, pieces and files), with a delete token scoped to that thread, best effort like `forgetAssistantData`.
- `chat_messages` gains what a writer turn needs (migration that only adds), for example: which engine answered (`ask` | `writer`), the bot's thread id for the chat, and a JSON payload for a draft (its piece id, text, media, added or not). Exact columns are the builder's call.
- The router call (section 1) runs here, logged in `ai_calls` as its own step and charged like Ask's own AI.
- Credits: unchanged. Ask's answers are charged as today; the writer's model calls are charged through `assistant_charge` (once per call, as now).

### 5. Speed

Targets (p50, live, measured from the user's tap):

| | Target |
|---|---|
| First words of a normal reply | about 1 s |
| A complete, checked draft | about 10 to 15 s (Sonnet with thinking; "Writing…" shows meanwhile) |

What gets there:
- **Charges never block** the next call (app-assistant PR #6).
- **A normal reply is one streamed call** of the conversation model; drafting and checking run only when it calls `write_drafts` or `revise_draft`.
- **The context is fetched once per writing conversation**, not every message, and refreshed only when it is old (the builder sets how old; about 10 minutes is a fine start).
- **HookPlaybook's tool calls get measured and fixed.** Each takes 2 to 3 s today. Find out where the time goes (D1 queries, their region, `authenticate`, the idempotency claim) and bring a tool call under about 300 ms. This may include caching the session's workspace check or batching queries.
- **The bot, its database and HookPlaybook's D1 in the same region** (US east, Mike's answer above).
- The time of each step is logged per turn (model calls, tool calls, database), so the targets can be checked.

## Mike's answers to the builder check (20 Nov 2026)

- **Models: quality wins for drafts.** The conversation runs on a fast model (first words in about 1 s); `write_drafts` and `revise_draft` write with HookPlaybook's main model, Claude Sonnet 5.5 with thinking, as HookPlaybook's rule "quality before cost in what people read" asks. A checked draft may take 10 to 15 s.
- **The bot learns threads.** The pack gains an `x_thread` output; `write_drafts` can write a thread (each post checked, the thread checked as a whole); HookPlaybook's `save_draft` accepts a thread (it saves `thread: null` today). Ask's "Write a thread from my notes" and "Repurpose my best post" both move to the writer, so one place writes every post. Trending and Find my story keep giving ideas in Ask, each with a "Write it with me" action that starts a writer conversation from the idea.
- **The bot moves to US east.** The bot runs in Vercel `iad1` with a Supabase project in `us-east-1` (askdocs with its own `assistant` schema, or a new project), next to HookPlaybook's D1 (`enam`). Its data so far is test chats only, so the move is new environment variables, not a migration. Under 300 ms per HookPlaybook tool call is the target once both sit in the same region.

## Constraints

- Merging to `main` is the whole deploy in both repos; no manual steps after a merge. Migrations only add.
- HookPlaybook's rules hold: RLS-style workspace scoping on every query, credits checked before any AI, nothing posts to X without the user's approval, secrets server-side only.
- The bot's rules hold: untrusted text stays inside `<data>` blocks, numbers only from facts, per-call charging with refs never shown to the browser before they are charged.
- The widget and the public demo must keep working; the demo gets the same conversation-first behaviour.
- Ask's existing tests and features stay green.

## Success

- Mike uses only Ask HookPlaybook for a week, for questions and for writing, and doesn't miss the old Write with me panel.
- In that week, writing chats reach a draft he adds to his Schedule, and he never sees a raw typo, a rejected draft or a "What I used" box.
- Live p50 timings meet the speed table.

## Left to the builder

- The router: rules plus a model, or a model alone; the model.
- The exact `chat_messages` columns and the SSE format mapping between the bot and Ask's page.
- Whether `draft` follows streamed text or replaces it.
- How the conversation prompt and tool descriptions are written, and the judge model for the conversation tests.
- The cause and fix of HookPlaybook's slow tool calls, once measured.
- How the widget renders `draft` and `saved` (match Ask's look as closely as the widget's size allows).

## Phases

This replaces the Phase 2 "Write with me" entry point and comes before Phase 3 (StreakPlan), which will use the same conversation-first bot. Until it ships, the floating Write with me button is switched off in HookPlaybook (Claude's recommendation to Mike, 20 Nov 2026), so users never see the old behaviour.

1. **The bot, conversation-first:** the agent with tools, the rules, the conversation tests (passing), `draft`/`saved` events, the widget and the demo updated. Ships to the public demo first.
2. **Speed:** HookPlaybook's tool latency measured and fixed, the context fetched once per conversation, per-step timing logs, the bot's region settled.
3. **Ask hosts the writer:** router, chip, attach and voice, the server route and pass-throughs, shared context, history, chat delete, removal of the floating button and the session route.
