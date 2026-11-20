# The writing bot: one chat assistant any app can add

Date: 20 Nov 2026. Status: approved by Mike, 20 Nov 2026.
Working name: **the writing bot** (repo `app-assistant`; the product name is still open).

## Why

1. **HookPlaybook's planner writes weak posts because it has nothing real to say.** Saved reviews (4 Nov 2026) show the same point twice in one plan, repeats of last week, and invented feelings ("Confession… is where I'd quit"). On 15 Nov Mike said the week was "questions with nothing real behind them". GitHub pull requests are today the only real material the planner gets.
2. **A chat that asks back fixes this.** The user says what happened, the bot asks for the number, the result or the lesson, then writes from those facts only, and keeps them for next week's plan. SuperX's "AI Chat Mode" is the competitor's version of the writing half.
3. **The same job exists in Mike's other apps.** In StreakPlan it is "what's your video about?" becoming a script; in SiteSeed it is "what's new in your business?" becoming article topics with real facts.
4. **It is Mike's Upwork showcase.** It's built in Python with FastAPI and LangGraph, as a separate bot any SaaS adds with one script tag, with a public demo a client can try without signing up.

## What success looks like

- Mike uses it on his own HookPlaybook account every week, and the posts it writes need little editing and contain no invented facts.
- The weekly plan uses facts told in the chat, and fewer of its posts are generic.
- A public demo link sits in Mike's Upwork profile and proposals, and a client can try it in under a minute.
- Adding the bot to a new app is a config file plus that app's tools, not new bot code.

## Goals

- One bot, in its own repo and service, that any app adds with a script tag (`<script src=".../widget.js" data-app="hookplaybook">`).
- It **asks back** until it has real facts, then writes that app's content from them.
- It reaches each app only through the app's own tools (MCP), as the signed-in user, with that user's rights.
- It keeps what it learned (the facts) and hands them to the app, so the app's own features use them.
- **The user can bring files to talk about:** images and screenshots, PDFs, and voice recordings. Facts read from them go into the same facts list as typed ones (Mike, 20 Nov 2026).
- **A post can carry the user's own image** (an uploaded screenshot goes on the draft).
- A public demo app.

## Non-goals (this version)

- Publishing anything. It adds drafts and saves facts; a person approves in the app.
- One view of a person across all apps (the user-care agent idea). The design keeps the door open, but it isn't built.
- Replacing HookPlaybook's Ask page. The widget arrives beside it as "Write with me"; Ask goes only if the widget proves better.
- SiteSeed in this version. It has no user tools yet (only the operator's MCP), so it comes after those exist.
- **Images the bot designs itself** (stat cards, quote cards, before/after). That's the next phase, after HookPlaybook works; see Phases.
- Video files (StreakPlan's takes stay in StreakPlan's own upload), live voice conversation, a mobile app.

## Anti-goals (a failure even if it works)

- **It invents a fact,** number, result or feeling that the user didn't give. That is the exact problem it exists to fix.
- **It spends AI money nobody counts.** Every call is charged to the user's credits in that app, or to a capped budget for the demo.
- **One user sees or changes another user's data,** or a page on another site embeds the widget and acts for a user.
- **It becomes a fifth product to market.** It is a feature of Mike's apps and a demo for clients, not a startup.

## Approaches considered

| | How | Trade-off |
|---|---|---|
| A. Shared TypeScript package in ai-mono (`@ai-mono/chat`) | The chat engine is written once; each app plugs in its data, its writer and where results go, on its own page | Same stack, no new server; but not reusable outside ai-mono and no Python showcase |
| **B. Separate bot service, Python + FastAPI + LangGraph (picked)** | Its own repo on Railway; each app adds a widget and exposes tools; the bot calls them as the user | New stack to run, a token handoff per app, streaming through one more hop; but any app (Mike's or a client's) plugs in, and it is the Upwork showcase |
| C. Inside HookPlaybook first, lifted out later | Grows out of the existing Ask chat | Fastest; but a rewrite later and no showcase |

**Mike picked B (20 Nov 2026):** "it's a separate bot, then I can easily apply it to new apps to demonstrate on Upwork."

Decided in the conversation:
- **It's a widget the bot owns,** not a page each app draws (Mike: yes to the widget).
- **The Upwork demo is a small public demo app** with the widget, plus a short video of the real thing in HookPlaybook (Mike: "go with your advice").
- **Ask HookPlaybook stays.** The widget comes in as "Write with me". (Claude's call, not objected to.)
- **It never publishes,** and its AI cost counts against the user's credits in the app. (Claude's call, not objected to.)
- **Order:** the demo app and HookPlaybook first, StreakPlan next, SiteSeed after it has user tools. (Claude's call.)
- **One owner per deployment** (Mike, 20 Nov 2026: "go with your advice"). The repo is private.
  - One running bot serves many apps of one owner: Mike's apps and the demo.
  - An Upwork client gets **their own copy** of the same code, deployed on their servers with their own OpenRouter key; their apps plug into their copy.
  - The bot is never a multi-customer hosted service. App packs are files in the repo, and secrets are environment variables.
  - A client's copy carries only the demo pack and their own packs, never Mike's (an export script like supa-mono's `export-product`, built when the first client needs it).
- **Facts are shown before they are kept.** (Claude's call, from the builder check.)
  - After writing, the chat shows the facts it understood, each one editable, with **Keep for next week**.
  - Adding a post keeps the facts that post tells.
  - Nothing is kept without a tap.
  - The app lists kept facts with Remove (HookPlaybook: the Sources page).
- **Files in, the user's own image out, in the first version; designed images in the next phase** (Mike, 20 Nov 2026: "yes" to Claude's advice).
- **Facts already told in a post are marked used.** (Claude's call, from the builder check.) Like `shipped_items` and `markShippedUsed`, `save_material` links each fact to the draft that told it. The weekly plan counts only untold facts as material, under a weekly cap like `SHIPPED_PER_WEEK`, so it never writes the same news again.

## How it works

### What the user sees (HookPlaybook)

A "Write with me" button on the dashboard (and a small bubble on Schedule and Today) opens a panel:

```
You:  shipped auto-reply today
Bot:  Nice. What changed for users, and do you have a number yet?
You:  replies go out in 2 min instead of 2 hours, 40 users on it
Bot:  Two posts in your voice:
      ┌ "Auto-reply now answers in 2 minutes, not 2 hours…" ┐  [Add to Schedule] [Edit]
      └ "40 people turned on auto-reply this week…"         ┘  [Add to Schedule] [Edit]
      Facts I understood:  auto-reply 2 min (was 2 h) · 40 users   [Edit] [Keep for next week]
```

- "Add to Schedule" saves a draft through the app's tool. The draft waits for the user's OK on the Schedule, as every API draft does today.
- "Edit" opens that draft in the app's own editor (a link the tool returns).
- The user can paste a link (a changelog, a pull request, an article). The bot reads it through its own fetch, and treats it as data, never as instructions.
- **The user can attach files** with the panel's paperclip, or hold the mic button to record:

  ```
  You:  [screenshot: dashboard showing "Auto-reply · 40 accounts on"]  this is from today
  Bot:  I can see 40 accounts have auto-reply on. Is that since this week's launch?
  You:  [voice note, 0:24]
  Bot:  (heard: "yes since Monday, and replies now go out in two minutes")
        Two posts in your voice, one with your screenshot:
        ┌ [image] "40 accounts turned on auto-reply since Monday…" ┐  [Add to Schedule] [Edit]
  ```

  - **Images and screenshots:** read by a model that can see. It may only report what is visible; anything it isn't sure of becomes a question back, never a fact.
  - **PDFs:** the text is pulled out (a scanned PDF with no text is read page by page as images, up to a page cap). Only the parts that bear on the chat go to the writer.
  - **Voice:** turned into text (OpenRouter's Whisper model, `openai/whisper-large-v3-turbo`, as AskDocs uses). The transcript shows in the chat so the user can see what was heard, and it is then treated like a typed message.
  - **A post with the user's image:** the writer may put an uploaded image on a post when it shows that post's facts. "Add to Schedule" sends the image with the draft. The user can remove it in the card or in the app's editor.

### The parts

1. **The bot service** (new repo, Python 3.12, FastAPI, LangGraph, on Railway with Postgres).
   - `POST /v1/chat/{app}/messages`: sends a message and streams the answer back (server-sent events: text pieces, cards, done, failed).
   - `GET /v1/chat/{app}/threads` and `/threads/{id}`: the user's past chats in that app.
   - `POST /v1/chat/{app}/files`: uploads one file before it is sent with a message. It checks type and size, stores the file and returns its id.
   - **Files are stored in an S3-compatible bucket** (Cloudflare R2 or Railway's bucket, the builder's choice), private, under `app/workspace/`. They are reached only by the bot, or through short-lived signed links handed to the app.
   - `GET /widget.js`: the widget.
   - LangGraph keeps each conversation with a Postgres checkpointer, one thread per `(app, workspace, chat id)`.
   - Models go through OpenRouter (the same account and model ids Mike uses elsewhere), with a fallback model.

2. **The widget** (`widget.js`, plain TypeScript built to one file, in a shadow DOM so the host's CSS can't break it).
   - It is a button and a panel, themed by a few CSS variables the host sets (accent, radius, font).
   - It gets a session token from the host app (`data-session-url`, which defaults to `/api/assistant/session`), then talks to the bot.
   - It works on phones: full-screen panel under 640 px wide.
   - A paperclip (images, PDFs and audio files, plus the camera and photos on a phone) and a hold-to-record mic (the browser's `MediaRecorder`; on iPhone Safari, the format it records). Each file shows as a chip with its upload progress, and can be removed before sending.

3. **Each app's side.**
   - **A session endpoint.** The signed-in user's browser calls `POST /api/assistant/session`. The app answers with a short-lived signed token (HS256 with a per-app secret shared with the bot; 1 hour). It carries the workspace id, the app id and the access level `draft`.
   - **Its user tools (MCP) accept that token,** beside the existing `hp_live_…` keys, as a `draft` key would: it can read and save drafts, never schedule, post, change billing, or touch keys or members. This keeps every app rule that applies to keys today.
   - **A draft can carry an image.**
     - The app's save-draft tool takes `media: [{ url, alt }]`, where `url` is the bot's short-lived signed link. The app downloads the file into its own storage and attaches it.
     - HookPlaybook's `POST /drafts` and its MCP tool take text only today, so this is added. Its drafts already carry images from the GitHub feature (`shipped_items.image`, `media`), so the storage side exists.
     - The app keeps its own rules for media (X's sizes and types).
   - **Three new tools** in the app:
     - `save_material(facts[], told_by_draft_id?)`: keeps the facts the user confirmed. When a draft is given, the facts are marked used by it, so the app's own features use only untold facts.
     - `assistant_charge(cost_usd, ref)`: once per `ref`; takes credits at the app's price for AI and refuses when the credits are out.
     - `assistant_context()`: one call returning what the bot needs to write well (voice, niche, recent posts and drafts for the repeat check, credits left).

4. **The app pack** (a file per app in the bot repo, `apps/hookplaybook.yaml`). It lists:
   - where the app's MCP is
   - its allowed widget origins
   - what to ask about (the interview guide)
   - what to write (kind, length, count, rules such as 280 characters and no links)
   - which files it accepts and their caps (types, size, PDF pages, voice minutes), and whether a result may carry an image
   - which tool saves a result
   - the model and the per-message cost cap.

   A new app is a new pack plus its tools.

### The graph (LangGraph)

```
read_files (only when the message has files) ─┐
                                              ▼
load_context ─▶ interview ──(enough facts?)──no──▶ ask_back ─▶ [wait for user]
                    │                                              │
                   yes ◀───────────────────────────────────────────┘
                    ▼
              extract_facts ─▶ write ─▶ check ──(fails)──▶ write (once more, then drop)
                                          │
                                         ok
                                          ▼
                                   present (posts + the facts list) ─▶ [user: Add / Edit / Keep facts / more]
                                          ▼
                                   save (app tools): a draft, with the facts it tells ─▶ save_material (only on a tap)
```

- **read_files:**
  - Voice becomes a transcript (shown to the user).
  - An image becomes a short description of what is visibly there (text on screen, numbers, what the picture shows).
  - A PDF becomes its relevant text.

  This is kept on the message, so later turns don't read the file again. What a file says is data, never instructions: a screenshot of text saying "ignore your rules" is just a picture of text.
- **load_context** calls `assistant_context`, cached for the thread for a few minutes.
- **interview** decides, from the pack's guide and the facts so far, whether there is enough to write. "Enough" means at least one concrete fact (what happened) and one specific (a number, a result, a quote or a lesson), or the user saying "just write it".
- **ask_back** asks one short question at a time, at most two rounds before it writes with what it has.
- **extract_facts** turns the user's words into a list of facts (structured output). Every later claim must come from this list.
- **write** produces the pack's content in the user's voice, using the app's patterns when the app supplies them (HookPlaybook's winning hooks in their niche). It may pick one of the chat's uploaded images for a post that it supports.
- **check:**
  - every number and claim is in the facts list (a second, cheap model call plus an exact-number match)
  - no repeat of a recent post or draft (the app's recent posts from context)
  - the pack's rules (length, no links, no dashes).

  A failed piece is written once more, then dropped, saying why.
- **save** happens only on the user's tap. Tool calls are idempotent by a key the bot makes (`Idempotency-Key`, which HookPlaybook's API already accepts).

### Money and limits

- **Before each turn:** the bot checks credits from `assistant_context` and refuses with the app's own words when they're out.
- **After each turn:** it calls `assistant_charge` with what OpenRouter reported, once per message id.
- **Files cost money too.** Transcription, image reading and PDF reading are charged like any other AI call. The pack caps them. Suggested starting caps: an image up to 10 MB, a PDF up to 20 MB and 30 pages, voice up to 5 minutes, at most 5 files a message.
- **Per user per hour:** a message cap in the bot (Redis or Postgres counter), on top of the app's own API rate limit (HookPlaybook: 60 requests a minute per key).
- **The demo app** has no credits. It runs on a cheap model, a cap per IP per day, and a hard daily dollar cap for the whole demo, after which it says "the demo is resting until tomorrow." It accepts screenshots and short voice notes (smaller caps: 2 files a message, voice up to 1 minute, no PDFs), and deletes its files after a day.

### Security

- **The widget only runs on its pack's allowed origins.** The bot checks the `Origin` of every request (CORS) and the app id in the token.
- **Session tokens are short-lived and scoped to `draft`.** The bot never stores an app's long-lived key.
- **Anything read from an app or a link is data, never instructions.** The bot's system prompt says so, and tool results are wrapped as data.
- **Uploads are checked by their content, not their name.** Each file's real type is checked against the pack's list. Files are private in the bucket, and the signed links an app gets expire in minutes. Files are deleted with the conversation and with the workspace.
- **No conversation text in logs,** only ids, timings and costs (the same rule as AskDocs' WhatsApp). Conversations live in Postgres for the user's history. Deleting a workspace in an app calls the bot's `DELETE /v1/apps/{app}/workspaces/{id}` to remove them.

### The public demo app

- **What it is:** a small page in the bot repo (`/demo`), dressed as a made-up SaaS ("Shipnote: changelogs for small teams").
- **What it does:** you tell it what you shipped, it asks back, then writes a launch post and a changelog entry.
- **How it runs:**
  - Its tools are a tiny MCP served by the bot itself, storing drafts per browser session for a day. No sign-up.
  - It runs the same graph and widget as the real apps, so the demo is honest.
  - A side note on the page shows what's happening ("asked back for a number", "checked: every number is from you"). This is what an Upwork client cares about.

## Phases

1. **The bot and the demo:**
   - repo, FastAPI, the graph, Postgres checkpointer, widget
   - file uploads (images, PDFs, voice), the bucket, and `read_files`
   - the demo pack and its tiny MCP
   - deploy on Railway; the demo link works.
2. **HookPlaybook:**
   - the session endpoint, token support in the user MCP, and the three tools
   - a draft with an image through the API and MCP (`media` on `POST /drafts`)
   - `save_material` stored where the weekly plan reads material (beside `shipped_items`, counted by `weekMaterialOf` and `materialAdvice`)
   - "Write with me" on the dashboard
   - Mike uses it on his own account for a week.
3. **StreakPlan:** the same three tools on the template's user MCP; a pack that turns "what's your video about / what happened today" into a script on a video. The useful tools already exist (`create_video`, `update_video`, `add_day_note`).
4. **Images the bot designs:** stat cards ("2 h → 2 min"), quote cards and before/after. They're made from designed templates filled with facts from the facts list, so nothing is invented. An image model is tried later only if templates fall short. It comes after HookPlaybook works, and before or beside StreakPlan, whichever Mike wants first then.
5. **SiteSeed:** after it has user tools; a pack that turns business news into article topics with facts.

The HookPlaybook and StreakPlan changes follow ai-mono's rules: PRs directly against main, migrations that apply themselves, and the template gets the session endpoint and tools, since any product would want them.

## Left to the builder

- Library choices inside the bot (LangGraph version, SSE helper, the widget's build tool), and the exact table layout.
- The exact wording of prompts and the interview guide per pack.
- Railway's setup details, and whether the cap counter is Redis or Postgres.
- How HookPlaybook stores told facts (a new kind of material or a new table) within its own patterns.
