# app-assistant

A private chat bot that any web app adds with one script tag. It asks the user a few plain questions about what they did, keeps only the facts they confirm, and writes drafts (posts, changelog entries, scripts) using nothing but those facts. The app decides what a draft looks like and where it goes. The bot never publishes anything.

## How it works

1. The app's page loads `widget.js`; the widget asks the app's own server for a session token.
2. The app signs a short-lived token (up to 1 hour) for the signed-in user's workspace and hands it to the widget.
3. The widget talks to this service with that token. The service sends the same token to the app's MCP, which exposes four tools: read context, save a draft, save material, report the cost.
4. Every number and claim in a draft must come from the facts list. The facts are shown to the user before they are kept, and a draft that fails the check is written once more, then dropped.
5. Saving happens only when the user taps. The bot has no tool that publishes.

FastAPI and LangGraph, Postgres for state (in its own schema), an S3-compatible bucket for files, OpenRouter for the models. Deployed on Vercel with the data on Supabase.

## Run locally

You need Python 3.12 with [uv](https://docs.astral.sh/uv/), a Postgres, and any S3 endpoint (MinIO works).

```bash
docker compose -f docker-compose.test.yml up -d        # Postgres on localhost:54329 (user, password and db: test)
uv sync
cp .env.example .env                                    # then fill it in
uv run uvicorn assistant.main:create_app --factory --reload
```

Set `DATABASE_URL=postgresql://test:test@localhost:54329/test` to use that Postgres. Every variable is explained in `.env.example`. Open `http://localhost:8000/demo` for the Shipnote demo. Set `DEMO_SESSION_SECRET` and `HOOKPLAYBOOK_SESSION_SECRET` in `.env` first (any long random strings, for example from `openssl rand -hex 32`): the server will not start without them.

## Test

```bash
TEST_DATABASE_URL=postgresql://test:test@localhost:54329/test uv run pytest
uv run ruff check . && uv run ruff format --check .
cd widget && npm install && npm test
```

## Build the widget

The built file `src/assistant/static/widget.js` is committed, so the server and the Vercel build need no Node. After changing `widget/`:

```bash
cd widget && npm run build
```

## Deploy on Vercel

The service runs on Vercel as one Python Function (Fluid compute), built from the repo's `pyproject.toml` and `vercel.json`; the entrypoint is the root-level `app.py`, which imports the app from `src/assistant/asgi.py`. Its data lives on **Supabase**: Postgres through the Session pooler, files in a private Supabase Storage bucket. Files go from the browser straight to Supabase (Vercel caps a request body at 4.5 MB); the bot only issues the upload link and then checks the stored file.

*20 Nov 2026: hosting moved from Cloud Run to Vercel.*

Cold start: `import assistant.asgi` takes about 1.08 s before and 1.07 s after the PDF and image libraries (`pypdf`, `pypdfium2`, Pillow) moved to load only when a file arrives (best of 3 fresh interpreters); the difference is within the 0.05 s noise. Most of the time is LangGraph, loaded through `assistant.graph.build`. Re-measure with `uv run python scripts/cold_start.py`.

**1. Postgres on Supabase.** In the Supabase dashboard, open **Connect** and copy the **Session pooler** string. It looks like `postgresql://postgres.REF:PASSWORD@aws-0-REGION.pooler.supabase.com:5432/postgres`; that is `DATABASE_URL`. If the password has characters such as `@ : / ? # %`, percent-encode them (`@` is `%40`).

- Not the **Transaction pooler** (port 6543): migrations hold a session advisory lock and psycopg uses prepared statements; both need a session of their own, which the transaction pooler does not give.
- Not the **direct** host `db.REF.supabase.co`: on most plans it has only an IPv6 address, and a Vercel Function reaches the internet over IPv4.

**Keep the tables private.** Supabase serves the schemas listed under Project Settings → Data API → **Exposed schemas** (`public` by default) to anyone holding the project's public key. The bot keeps every table, LangGraph's checkpoints included, in its own schema, `DB_SCHEMA` (default `assistant`), which it creates on its first start. **Never add that schema to Exposed schemas**, and never set `DB_SCHEMA=public`. After the first deploy, the Table Editor shows the bot's tables under the `assistant` schema and nothing new under `public`.

**2. Files in Supabase Storage.**

1. Storage → **New bucket**: give it a name (`BUCKET`) and leave **Public bucket** off. Set its **file size limit** to `25 MB` (`file_size_limit` 26214400: the largest cap of any kind, a voice note) and its **allowed MIME types** (`allowed_mime_types`) to the kinds the bot accepts, with no wildcard:

   ```text
   image/png, image/jpeg, image/jpg, image/pjpeg, image/webp, image/gif,
   application/pdf,
   audio/webm, audio/ogg, audio/opus, audio/mp4, audio/m4a, audio/x-m4a, audio/mpeg, audio/mp3, audio/wav, audio/x-wav, audio/wave,
   video/mp4, video/webm,
   application/octet-stream
   ```

   The bot sniffs the stored bytes and deletes anything that is not an image, a PDF or a voice format (webm, ogg, mp4/m4a, mp3, wav); the bucket's list is a second fence in front of it. (`application/octet-stream` is for a browser that does not know a file's type.) The per-app caps (image 10 MB, PDF 20 MB, voice 25 MB) are enforced by the bot, not by the bucket.
2. Storage's **S3** settings page (`https://supabase.com/dashboard/project/REF/storage/s3`): make sure the S3 connection is enabled; it shows the endpoint and the region.
3. Create an **access key** there: it shows an access key id and a secret once. They give full access to every bucket of the project (they bypass Storage's row-level policies), so they go into Vercel environment variables only (mark them Sensitive).
4. From Project Settings → API: the project URL (`SUPABASE_URL`, `https://REF.supabase.co`) and the **secret (service role) key** (`SUPABASE_SERVICE_KEY`). The bot uses them to ask Supabase for the signed upload links.

> **Warning.** If either `SUPABASE_URL` or `SUPABASE_SERVICE_KEY` is missing, uploads **silently fall back to the S3 presigned PUT** (see "Alternative: another S3-compatible store"). Nothing fails at startup, and on Supabase's S3 endpoint that fallback may not work from a browser. Set both.

The S3 values are `S3_ENDPOINT=` the endpoint shown there (`https://REF.supabase.co/storage/v1/s3`; the `https://REF.storage.supabase.co/storage/v1/s3` form works too), `S3_REGION=` the region shown there (for example `ap-southeast-1`), `S3_BUCKET=BUCKET`, and the key id and secret as `S3_ACCESS_KEY` and `S3_SECRET_KEY`. The bot still reads, checks and deletes files through them.

**3. Connection budget.** Each instance keeps one connection open for its own queries and one for LangGraph's checkpoints for as long as it is alive, idle or suspended ones included (Fluid compute does not run idle timeouts while an instance is suspended, and instances of the previous deployment linger for a while). When busy it opens up to `DB_POOL_MAX` (default 2) plus `SAVER_POOL_MAX` (default 1): 3. So the budget is **instances alive × 2, up to × 3 when busy**. Fluid compute runs many requests on one instance, so a demo's traffic is a few instances at most, but Vercel can start more under load. Keep that number under the Session pooler's pool size (Database settings → Connection pooling; 15 by default on every compute size up to Medium: about 5 busy instances). To go beyond that, raise the pool size (a larger compute) or lower `DB_POOL_MAX` first. Each connection is checked (one round trip) before use, so one the pooler dropped while the instance slept is replaced instead of failing a request, and extra idle connections close after a minute.

**4. Import the project.** In Vercel: **Add New → Project**, import the GitHub repo. The framework preset is **FastAPI**; Python 3.12 comes from `.python-version`. Leave the build and output settings alone: `vercel.json` sets the function (300 s, test files left out) and the daily clean-up cron.

Then put the function next to the database: Settings → Functions → **Function Region**, the Vercel region closest to the Supabase project's region (for example `sin1` for `ap-southeast-1`; Supabase shows its region under Project Settings → General). New projects run in `iad1` (Washington, D.C.), and from there every query, checkpoint write and storage call to Singapore costs about 230 ms, dozens of times per turn, and a cold start gains seconds. Hobby allows one region. You can also set it in `vercel.json` as `"regions": ["sin1"]`; the repo leaves it out because the right region depends on where your Supabase project is.

**5. Environment variables.** Settings → Environment Variables, for Production (mark every secret Sensitive). Everything in `.env.example`:

| Variable | Value |
|---|---|
| `OPENROUTER_API_KEY` | the OpenRouter key |
| `DATABASE_URL` | the Supabase **Session pooler** string (port 5432) |
| `DB_SCHEMA`, `DB_POOL_MAX`, `SAVER_POOL_MAX` | leave unset for `assistant`, 2 and 1 |
| `S3_ENDPOINT`, `S3_REGION`, `S3_BUCKET`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` | the Supabase S3 values above |
| `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` | the project URL and the secret key, for the signed upload links |
| `CRON_SECRET` | `openssl rand -hex 32`: Vercel Cron sends it as `Authorization: Bearer ...` to `/internal/cleanup`, which refuses everything else (and is off while it is unset) |
| `PUBLIC_BASE_URL` | the production domain, `https://bot.example.com` (no trailing slash) |
| `DEMO_SESSION_SECRET` | `openssl rand -hex 32` |
| `HOOKPLAYBOOK_SESSION_SECRET` | the same value as HookPlaybook's `ASSISTANT_TOKEN_SECRET`; the service refuses to start without it |

Do not set `RUN_LIFESPAN=false` in production (it skips the database and storage). `SHUTDOWN_DRAIN_SECONDS` stays at `0.4`: Vercel gives a function about 500 ms at shutdown.

**6. Deploy.** Deploy once; after that every push to `main` redeploys. A change to an environment variable applies from the next deployment (redeploy to pick it up). On a cold start the bot's migrations and LangGraph's checkpoint setup both run under one Postgres advisory lock, so several instances starting together (the first deploy, or an upgrade that adds a migration) run them once while the others wait.

**7. Check.** `curl https://<domain>/health` answers `{"ok":true}`; then open `https://<domain>/demo` on a phone. If a pack's secret variable is missing, the function fails to start and the logs say which one. In the first deploy's build log, confirm Vercel used `app.py` as the entrypoint (the root-level `app.py` imports the app from `src/assistant/asgi.py`); if it says it found no entrypoint, or the function limits (300 s) are not applied, fix that before anything else.

**Limits on the Hobby plan.** A function runs at most 300 s per request (a chat turn is one streamed response, so a turn is cut at 5 minutes); request bodies are at most 4.5 MB (files do not pass through the function); a cron runs at most once a day, which is why the demo's clean-up (`GET /internal/cleanup`, deleting the demo's data older than a day and every app's expired, unfinished uploads) is daily at 03:00 UTC. Nothing in the bot relies on work continuing after a response ends: every model call is charged as soon as it finishes, and there is no background loop.

**Check the bucket once.** Before the first real user, run this from your machine with the real `S3_*` values (and the rest of the settings) in `.env`. It does each call the service makes, against the real bucket, then cleans up (the bytes are stored as `image/png` so the bucket's MIME list accepts them; the bot's own content check is not involved):

```bash
uv run python - <<'PY'
import httpx
from assistant.config import get_settings
from assistant.files.storage import Storage

s = Storage(get_settings())
s.put("check/hello.png", b"hello", "image/png")                            # 1. put
assert s.get("check/hello.png") == b"hello"                                # 2. get
url = s.signed_url("check/hello.png", 60)
assert httpx.get(url).content == b"hello"                                  # 3. signed GET (how images reach the app)
assert httpx.get(url.split("?")[0]).status_code in (400, 401, 403, 404)    # 4. unsigned GET refused: the bucket is private
assert s.delete_prefix("check/") == 1                                      # 5. delete
print("bucket ok")
PY
```

It prints `bucket ok`. Any error names the call that failed; fix the bucket or keys before going on.

**Then test one upload in a browser.** Open `https://<domain>/demo`, attach a small photo and send it with a message. The browser's network tab shows `POST /v1/chat/demo/uploads`, then a `PUT` straight to `https://REF.supabase.co/storage/v1/object/upload/sign/...` (not to the bot), then `POST .../uploads/.../finish`. If the `PUT` goes to an S3 address instead, `SUPABASE_URL` or `SUPABASE_SERVICE_KEY` is missing in Vercel. Also try a `.txt` file renamed to `.png`: the bot must refuse it.

Note on the demo's per-IP limit: the client IP is `x-vercel-forwarded-for`, then `x-real-ip`, then the rightmost `x-forwarded-for` entry. They are set by Vercel's edge, which replaces what a visitor sends. Putting another proxy in front (a CDN, say) changes what they hold and needs a code change in `client_ip`.

### Alternative: another S3-compatible store

Without `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` the bot issues an S3 presigned PUT instead (Cloudflare R2, AWS S3, MinIO). The browser sends `content-type` and `if-none-match: *` on that PUT (the link writes once), so the bucket's **CORS** must allow your app's origins (the pack's `allowed_origins`, and `PUBLIC_BASE_URL` for the demo) with:

- methods `PUT` (and `GET` if you read from the browser);
- allowed headers `if-none-match` and `content-type`.

For example, on S3 or R2:

```json
[{"AllowedOrigins": ["https://hookplaybook.com", "https://bot.example.com"], "AllowedMethods": ["PUT"], "AllowedHeaders": ["if-none-match", "content-type"], "MaxAgeSeconds": 600}]
```

Set `S3_ENDPOINT`, `S3_REGION` (`auto` for R2), `S3_BUCKET`, `S3_ACCESS_KEY` and `S3_SECRET_KEY` for it, and run the bucket check above against it too. The store must honour `If-None-Match` on a PUT, or an upload link could replace a file already checked.

## Apps

| Pack | App | Secret variable |
|---|---|---|
| `apps/demo.yaml` | Shipnote, the public demo at `/demo` | `DEMO_SESSION_SECRET` |
| `apps/hookplaybook.yaml` | HookPlaybook (hookplaybook.com), "Write with me" in its dashboard | `HOOKPLAYBOOK_SESSION_SECRET` |

The service refuses to start while any pack's secret variable is unset.

### HookPlaybook

HookPlaybook's side is in its own repo (`products/hookplaybook`): the four tools on its MCP (`https://hookplaybook.com/api/v1/mcp`), the session route `/api/assistant/session`, and the call that deletes a workspace's data here when the workspace is deleted there.

- **The secret.** Generate one value (`openssl rand -hex 32`). Set it as `HOOKPLAYBOOK_SESSION_SECRET` in the bot's **Vercel project settings** (Settings → Environment Variables, for Production), and the same value as HookPlaybook's `ASSISTANT_TOKEN_SECRET`. Redeploy the bot after adding it.
- **Origin.** Only `https://hookplaybook.com` (the dashboard is at `/dashboard` on it) may use the widget.
- **Posts.** Two X posts per card (280 characters, no links, no long dashes, may carry one of the user's pictures). Saving goes through `save_draft`, which copies the picture into the draft.
- **Kept facts.** `assistant_context` returns `kept_facts` (up to 10 facts the user kept earlier and no post has told yet). The chat's first question offers them; one the user picks counts as their own fact and is kept with the kept fact's own id and exact text, so `save_material` sends HookPlaybook the text it stored.
- **Charge.** `assistant_charge` charges once per `ref` (the message id). The browser first sees that id in the `done` event, after the charge.

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

- `assistant_context`: what the bot may know about the workspace: `voice`, `topics`, `recent_posts`, `patterns`, `credits_left`, `out_of_credits_message`, `language`, and optionally `kept_facts` (`[{"id", "text"}]`, facts the user kept earlier and has not used, offered when the chat opens).
- `save_draft`: saves a draft the user tapped, with its `media` (images) if any.
- `save_material`: keeps facts for later, with `draft_id` when the facts came with a draft.
- `assistant_charge`: records the cost of one message, once per message id.

The app's MCP accepts the session token as `Authorization: Bearer` and treats it as a "draft" key for that workspace only.

### 3. A session endpoint in the app

The widget calls `POST <data-session-url>` with `credentials: "include"`, `content-type: application/json` and the body `{}`, so the endpoint is authenticated by the app's own sign-in cookie. It answers JSON `{"token": "<jwt>"}`:

```json
{ "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..." }
```

A 401 or 403 (or a 2xx without a `token`, such as a redirect to a sign-in page) makes the widget show "Please sign in again." and stop. A network error or any other status (a 5xx while the app restarts) shows "Couldn't reach the app. Try again." and the next tap tries again; a message that could not be sent stays in the box with its files. The token is HS256, signed with the pack's secret, with claims `iss` (the app id), `sub` (the workspace id: letters, digits and `: _ . -`, up to 200), `access: "draft"`, `iat`, and `exp` at most 1 hour after `iat`.

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

The widget shows its own button. A page that has its own button opens the panel with `window.dispatchEvent(new CustomEvent("assistant:open"))`.

### 4. Delete a workspace's data

When a workspace is deleted in the app, call this from the app's server:

```
DELETE https://<bot-domain>/v1/apps/<app-id>/workspaces/<workspace-id>
Authorization: Bearer <token>
```

The token is minted like the session token, but with `access: "delete"` and the same `sub` as the workspace. It removes the workspace's threads, checkpoints and stored files, and answers `{"deleted_threads": n}`.
