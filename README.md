# app-assistant

A private chat bot that any web app adds with one script tag. It asks the user a few plain questions about what they did, keeps only the facts they confirm, and writes drafts (posts, changelog entries, scripts) using nothing but those facts. The app decides what a draft looks like and where it goes. The bot never publishes anything.

## How it works

1. The app's page loads `widget.js`; the widget asks the app's own server for a session token.
2. The app signs a short-lived token (up to 1 hour) for the signed-in user's workspace and hands it to the widget.
3. The widget talks to this service with that token. The service sends the same token to the app's MCP, which exposes four tools: read context, save a draft, save material, report the cost.
4. Every number and claim in a draft must come from the facts list. The facts are shown to the user before they are kept, and a draft that fails the check is written once more, then dropped.
5. Saving happens only when the user taps. The bot has no tool that publishes.

FastAPI and LangGraph, Postgres for state, an S3-compatible bucket for files, OpenRouter for the models.

## Run locally

You need Python 3.12 with [uv](https://docs.astral.sh/uv/), a Postgres, and any S3 endpoint (MinIO works).

```bash
docker compose -f docker-compose.test.yml up -d        # Postgres on localhost:54329 (user, password and db: test)
uv sync
cp .env.example .env                                    # then fill it in
uv run uvicorn assistant.main:create_app --factory --reload
```

Set `DATABASE_URL=postgresql://test:test@localhost:54329/test` to use that Postgres. Every variable is explained in `.env.example`. Open `http://localhost:8000/demo` for the Shipnote demo. Set `DEMO_SESSION_SECRET` in `.env` first (any long random string, for example from `openssl rand -hex 32`): the server will not start without it.

## Test

```bash
TEST_DATABASE_URL=postgresql://test:test@localhost:54329/test uv run pytest
uv run ruff check . && uv run ruff format --check .
cd widget && npm install && npm test
```

## Build the widget

The built file `src/assistant/static/widget.js` is committed, so the server and the Docker image need no Node. After changing `widget/`:

```bash
cd widget && npm run build
```

## Deploy on Railway

The repo has a `Dockerfile` and a `railway.json` (Dockerfile build, health check on `/health`, restart on failure). In Railway's dashboard:

1. New project, deploy from this GitHub repo. Add the Postgres plugin; it gives you `DATABASE_URL`.
2. Create a bucket (Railway's own, or Cloudflare R2). Note its endpoint, access key, secret key, and use the bucket name `app-assistant`.
3. In the service's Variables, set `OPENROUTER_API_KEY`, `S3_ENDPOINT`, `S3_BUCKET`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_REGION`, `PUBLIC_BASE_URL` (the final public URL, for example `https://bot.example.com`), and `DEMO_SESSION_SECRET` (run `openssl rand -hex 32` and paste the result). Reference the Postgres plugin's `DATABASE_URL`. Railway sets `PORT` itself.
4. Optional: add a custom domain under Settings, Networking, then update `PUBLIC_BASE_URL` to match and redeploy.
5. Check `https://<domain>/health` returns `{"ok":true}`, then open `https://<domain>/demo` on a phone.

Migrations run on start. If a pack's secret variable is missing, the service refuses to start and says which one.

## Add an app

Two parts: a pack file here, and a few things in the app.

### 1. A pack: `apps/<id>.yaml`

`apps/demo.yaml` is a complete example. Fields:

| Field | Meaning |
|---|---|
| `id` | App id. It is the `iss` claim of the app's tokens and the `data-app` of the script tag. |
| `name` | Name shown in the widget. |
| `mcp_url` | The app's MCP endpoint. `{base}` becomes `PUBLIC_BASE_URL`. |
| `secret_env` | Name of the environment variable holding the app's signing secret. Set it here and in the app. |
| `allowed_origins` | Origins allowed to use the widget; the `Origin` header is checked on every `/v1` request. |
| `interview_guide` | What to ask about, in plain words. |
| `writes` | What to write: per item `kind`, `count`, `instructions`, and optional `max_chars`, `no_links`, `no_dashes`, `may_carry_image`. |
| `tools` | The app's tool names for `context`, `save_draft`, `save_material`, `charge`. |
| `models` | `main`, `cheap`, `vision` (OpenRouter ids) and optional `fallbacks`. |
| `files` | Optional caps: `image_mb`, `pdf_mb`, `pdf_pages`, `voice_seconds`, `per_message`, `kinds` (image, pdf, voice). |
| `message_usd_cap` | Most the bot may spend on one message. |
| `demo` | `true` only for the public demo (daily caps, files deleted after a day). |

### 2. Four tools on the app's MCP

- `assistant_context`: what the bot may know about the workspace (what it writes for, past drafts, kept facts).
- `save_draft`: saves a draft the user tapped, with its `media` (images) if any.
- `save_material`: keeps facts for later, with `draft_id` when the facts came with a draft.
- `assistant_charge`: records the cost of one message, once per message id.

The app's MCP accepts the session token as `Authorization: Bearer` and treats it as a "draft" key for that workspace only.

### 3. A session endpoint in the app

The widget calls `POST <data-session-url>` with `credentials: "include"`, so the endpoint is authenticated by the app's own sign-in cookie. It answers JSON `{"token": "<jwt>"}`:

```json
{ "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..." }
```

Any non-2xx answer (or a body without a `token`) makes the widget show "Please sign in again." The token is HS256, signed with the pack's secret, with claims `iss` (the app id), `sub` (the workspace id: letters, digits and `: _ . -`, up to 200), `access: "draft"`, `iat`, and `exp` at most 1 hour after `iat`.

```python
import time, jwt

# SECRET: the value of the pack's `secret_env` environment variable, shared with the bot.


def mint(workspace_id: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {"iss": "myapp", "sub": workspace_id, "access": "draft", "iat": now, "exp": now + 3600},
        SECRET,
        algorithm="HS256",
    )
```

```ts
import jwt from "jsonwebtoken";

// SECRET: the value of the pack's `secret_env` environment variable, shared with the bot.

export const mint = (workspaceId: string) => {
  const now = Math.floor(Date.now() / 1000);
  return jwt.sign({ iss: "myapp", sub: workspaceId, access: "draft", iat: now, exp: now + 3600 }, SECRET, {
    algorithm: "HS256",
  });
};
```

Then add the script tag to the page:

```html
<script src="https://<bot-domain>/widget.js" data-app="myapp" data-session-url="/api/assistant/session" data-label="Write with me"></script>
```

### 4. Delete a workspace's data

When a workspace is deleted in the app, call this from the app's server:

```
DELETE https://<bot-domain>/v1/apps/<app-id>/workspaces/<workspace-id>
Authorization: Bearer <token>
```

The token is minted like the session token, but with `access: "delete"` and the same `sub` as the workspace. It removes the workspace's threads, checkpoints and stored files, and answers `{"deleted_threads": n}`.
