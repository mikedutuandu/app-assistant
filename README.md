# App Assistant

[![CI](https://github.com/mikedutuandu/app-assistant/actions/workflows/ci.yml/badge.svg)](https://github.com/mikedutuandu/app-assistant/actions/workflows/ci.yml)
[![npm](https://img.shields.io/npm/v/@app-assistant/host)](https://www.npmjs.com/package/@app-assistant/host)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A writing assistant you add to your web app. It chats with your users like a person, asks one thing at a time about
what they actually did, and turns it into a post or a thread in their voice, **never inventing a number**. Users can
attach screenshots, PDFs and voice notes. Your app decides where a draft goes; the bot never publishes anything.

It runs as its own service (FastAPI + LangGraph, Postgres, any S3-compatible bucket, models through OpenRouter), and
your app talks to it through a small, versioned contract: one token, four HTTPS endpoints, and a JSON "pack" that
describes what to write.

**In production:** it is the "Write with HookPlaybook" page of [HookPlaybook](https://hookplaybook.com), an X growth app.

## How it works

The code, step by step and for someone new to LangGraph: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

```
your page ── chat window (script tag or React) ──► app-assistant ──► OpenRouter (chat + writer models)
   │                                                     │
   └─ your server signs a short-lived token              └─ calls your app: /context  /drafts  /material  /charge
```

1. Your server signs a short-lived token (HS256, up to 1 hour) for the signed-in user's workspace.
2. The chat window talks to the bot with that token; the bot sends the same token to your app's four endpoints: read
   context (the user's voice, recent posts), save a draft, keep facts the user told it, report what a message cost.
3. A chat model holds the conversation and keeps what the user says as facts; a writer model drafts from those facts
   only. Every number and claim in a draft is checked against what the user gave or a file showed: a draft that fails
   is rewritten once, then dropped.
4. A draft reaches your app only when the user taps to save it. There is no tool that publishes.

## Try it in two minutes (Docker)

No cloud account needed: the bot, Postgres 16 and files on local disk.

```bash
git clone https://github.com/mikedutuandu/app-assistant && cd app-assistant
export OPENROUTER_API_KEY=sk-or-...    # needed for real replies
docker compose up --build
```

Open <http://localhost:8000/demo> for **Shipnote**, a demo app built on the same contract (`apps/demo.yaml`,
`src/assistant/demo/`). `http://localhost:8000/health` answers `{"ok": true}`. Your data stays in Docker volumes until
`docker compose down -v`.

## Add it to your app

There are two parts, and you need both: **the bot**, a service you run (it holds the conversation and calls the
models), and **your app**, which tells the bot who the user is and keeps the drafts. The npm package is the second
part; it does not replace the bot. The whole contract is one page: **[docs/integration.md](docs/integration.md)**
(version 1).

### Part 1: run the bot

Deploy it (the button below, or Docker on your own server) and tell it about your app with three variables:

```bash
APP_ID=shipnote                     # your app's id, used in the tokens and the chat window
APP_SITE=https://shipnote.app       # where your app serves its pack
APP_SECRET=...                      # openssl rand -hex 32; your app keeps the same value
```

The bot only talks to apps its owner names this way, so a stranger's app can't spend your model credit. One bot can
serve several apps: list them in `apps/registry.yaml` or `APPS` instead ([docs/integration.md](docs/integration.md#the-registry-entry-the-bot-owner-adds-it)).

### Part 2: connect your app

1. **Serve the pack and the four endpoints.** The pack is JSON at `{site}/.well-known/app-assistant.json`; the
   endpoints are `POST {api_base}/context`, `/drafts`, `/material` and `/charge`. In a JavaScript app,
   [`@app-assistant/host`](sdk/js/README.md) does both from four functions you write:

   ```bash
   npm install @app-assistant/host
   ```

   In any other language, follow the contract; its scenarios (`tests/contract/scenarios.json`) check your app.

2. **Put the chat window on a page**, with a session route that signs the token for the signed-in user (with
   `APP_SECRET`):

   ```tsx
   <AssistantChat app="shipnote" botUrl="https://bot.example.com" sessionUrl="/api/assistant/session" />
   ```

   or one script tag:

   ```html
   <script src="https://bot.example.com/widget.js" data-app="shipnote" data-session-url="/api/assistant/session"></script>
   ```

A pack file kept on the bot, and apps reached over MCP instead of HTTPS: [docs/packs.md](docs/packs.md).

## Deploy

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2Fmikedutuandu%2Fapp-assistant&project-name=app-assistant&repository-name=app-assistant&env=OPENROUTER_API_KEY,PUBLIC_BASE_URL,APP_ID,APP_SITE,APP_SECRET,DEMO_SESSION_SECRET,CRON_SECRET,S3_ENDPOINT,S3_REGION,S3_BUCKET,S3_ACCESS_KEY,S3_SECRET_KEY&envDescription=OpenRouter%20key,%20the%20bot%27s%20public%20URL,%20your%20app%27s%20id%20and%20site,%20three%20secrets%20%28openssl%20rand%20-hex%2032%29,%20and%20a%20bucket%20for%20files&envLink=https%3A%2F%2Fgithub.com%2Fmikedutuandu%2Fapp-assistant%2Fblob%2Fmain%2Fdocs%2FDEPLOY.md)

- **Vercel:** the button clones the repo into your account. Add Neon from the Vercel Marketplace for Postgres, and an
  S3-compatible bucket for files (Neon Object Storage, Cloudflare R2, S3).
- **Your own server:** the Docker setup above, with your own secrets and domain.

Step by step, with the checks to run after: **[docs/DEPLOY.md](docs/DEPLOY.md)**. Every variable is explained in
[`.env.example`](.env.example). As the bot's owner you set ceilings over every app: a message's spend cap
(`MAX_MESSAGE_USD`), the models an app may use (`ALLOWED_MODELS`) and one app's daily spend (`MAX_APP_DAILY_USD`).

## Run locally without Docker

You need Python 3.12 with [uv](https://docs.astral.sh/uv/) and a Postgres.

```bash
docker compose -f docker-compose.test.yml up -d        # a Postgres on localhost:54329 (user, password and db: test)
uv sync
cp .env.example .env                                    # then fill it in
uv run uvicorn assistant.main:create_app --factory --reload
```

Set `DATABASE_URL=postgresql://test:test@localhost:54329/test`, then open <http://localhost:8000/demo> (the
`.env.example` demo secret works on localhost only; set your own with `openssl rand -hex 32` before going online). Files go on local disk (`DATA_DIR`) unless you set `S3_*`. An app whose secret
variable is unset is off (the log says which); with none set, the server does not start.

## Develop

```bash
TEST_DATABASE_URL=postgresql://test:test@localhost:54329/test uv run pytest
uv run ruff check . && uv run ruff format --check .
(cd widget && npm ci && npm test)      # the chat window behind the script tag
(cd sdk/js && npm ci && npm test)      # @app-assistant/host
```

The built `src/assistant/static/widget.js` is committed, so the server needs no Node; after changing `widget/`, run
`npm run build` there. [CONTRIBUTING.md](CONTRIBUTING.md) has the rest.

**Conversation tests.** `tests/conversations/cases.yaml` holds about 20 real chats ("hi", "just write it", a screenshot
with a number, Vietnamese, "say we have 10k users"...), each with notes a judge model decides and hard rules checked in
code (at most one question per reply, the language, every number in a draft given by the user). Recordings in
`tests/conversations/recorded/` replay in CI with no key; re-record after changing a prompt, a model or a case:

```bash
OPENROUTER_API_KEY=... uv run python scripts/converse_eval.py --live            # real models, then the judge
OPENROUTER_API_KEY=... uv run python scripts/converse_eval.py --live --record   # and save the cases that pass
uv run python scripts/converse_eval.py                                          # replay the recordings, no key
```

## Layout

| Path | What it is |
|---|---|
| `src/assistant/` | the service: chat API, LangGraph agent, tools, file intake, storage, the demo app |
| `apps/` | packs kept on the bot (`demo.yaml`; `hookplaybook.yaml`, a real one), and `registry.yaml`: the apps that serve their own pack |
| `widget/` | the chat window behind the script tag |
| `sdk/js/` | `@app-assistant/host`: the contract for JavaScript apps, and the React chat window |
| `docs/` | the contract, its JSON schema, deployment, packs |
| `tests/contract/` | language-neutral scenarios any app can be checked against |

## License

[MIT](LICENSE). Security issues: please report them privately, as [SECURITY.md](SECURITY.md) says.
