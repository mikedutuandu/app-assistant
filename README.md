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

## Deploy on Google Cloud Run

The repo has a `Dockerfile`; Cloud Run builds it from source. Replace the placeholders (`PROJECT_ID`, `REGION`, and so on) with your own. Run the commands in a terminal where you are signed in with `gcloud auth login`; you paste secrets yourself, nobody else needs to see them.

**1. Switch on the Google services it uses.**

```bash
gcloud config set project PROJECT_ID
gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com \
  sqladmin.googleapis.com secretmanager.googleapis.com
```

**2. Postgres.** Pick one.

- Cloud SQL (Google's own Postgres 16):

  ```bash
  gcloud sql instances create INSTANCE --database-version=POSTGRES_16 --region=REGION --tier=db-f1-micro
  gcloud sql databases create DB --instance=INSTANCE
  gcloud sql users create USER --instance=INSTANCE --password=PASSWORD
  ```

  `DATABASE_URL` is then `postgresql://USER:PASSWORD@/DB?host=/cloudsql/PROJECT_ID:REGION:INSTANCE`, and the deploy command below needs `--add-cloudsql-instances PROJECT_ID:REGION:INSTANCE`.
- Cheaper: any external Postgres (Supabase, Neon). Use its connection URL as `DATABASE_URL` and skip `--add-cloudsql-instances`.

**3. A private bucket for files.** The service talks to Cloud Storage through its S3-compatible API.

```bash
gcloud storage buckets create gs://BUCKET --location=REGION --uniform-bucket-level-access --public-access-prevention
gcloud iam service-accounts create app-assistant-files
gcloud storage buckets add-iam-policy-binding gs://BUCKET \
  --member=serviceAccount:app-assistant-files@PROJECT_ID.iam.gserviceaccount.com --role=roles/storage.objectAdmin
gcloud storage hmac create app-assistant-files@PROJECT_ID.iam.gserviceaccount.com
```

The last command prints an access id and a secret: they are `S3_ACCESS_KEY` and `S3_SECRET_KEY`. The other values are `S3_ENDPOINT=https://storage.googleapis.com`, `S3_REGION=auto` and `S3_BUCKET=BUCKET`.

**4. Secrets in Secret Manager.** Create one per secret (`OPENROUTER_API_KEY`, `DEMO_SESSION_SECRET`, `DATABASE_URL`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`); the value comes from stdin so it never lands in your shell history:

```bash
openssl rand -hex 32 | gcloud secrets create DEMO_SESSION_SECRET --data-file=-
printf '%s' 'THE_VALUE' | gcloud secrets create OPENROUTER_API_KEY --data-file=-   # same for the others
```

Let the service read them (the default compute service account is used unless you pass `--service-account`):

```bash
gcloud projects add-iam-policy-binding PROJECT_ID \
  --member=serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com --role=roles/secretmanager.secretAccessor
```

**5. Deploy.**

```bash
gcloud run deploy app-assistant --source . --region REGION --allow-unauthenticated \
  --no-cpu-throttling --min-instances 0 --max-instances 3 --timeout 3600 --concurrency 40 --memory 1Gi \
  --add-cloudsql-instances PROJECT_ID:REGION:INSTANCE \
  --set-secrets OPENROUTER_API_KEY=OPENROUTER_API_KEY:latest,DEMO_SESSION_SECRET=DEMO_SESSION_SECRET:latest,DATABASE_URL=DATABASE_URL:latest,S3_ACCESS_KEY=S3_ACCESS_KEY:latest,S3_SECRET_KEY=S3_SECRET_KEY:latest \
  --set-env-vars S3_ENDPOINT=https://storage.googleapis.com,S3_REGION=auto,S3_BUCKET=BUCKET,PUBLIC_BASE_URL=https://TEMP
```

- `--no-cpu-throttling`: a chat turn finishes and is charged even if the user closes the chat, and the demo's clean-up runs in the background; both need CPU between requests.
- `--timeout 3600`: answers are streamed and may stay open for a while.
- Leave out `--add-cloudsql-instances` if you use an external Postgres.

`PUBLIC_BASE_URL` must be the service's own URL, which you only know after the first deploy. Deploy once, copy the URL it prints (or map a custom domain such as `bot.example.com`), then set it and redeploy:

```bash
gcloud run services update app-assistant --region REGION --update-env-vars PUBLIC_BASE_URL=https://THE-SERVICE-URL
```

**6. Check.** `curl https://<url>/health` answers `{"ok":true}`; then open `https://<url>/demo` on a phone. Migrations run on start; if a pack's secret variable is missing, the service refuses to start and says which one.

**Check the bucket once.** Before the first real user, run this from your machine with the real `S3_*` values (and the rest of the settings) in `.env`. It does each call the service makes, against the real bucket, then cleans up:

```bash
uv run python - <<'EOF'
import httpx
from assistant.config import get_settings
from assistant.files.storage import Storage

s = Storage(get_settings())
s.put("check/hello.txt", b"hello", "text/plain")                          # 1. put
assert s.get("check/hello.txt") == b"hello"                                # 2. get
url = s.signed_url("check/hello.txt", 60)
assert httpx.get(url).content == b"hello"                                  # 3. signed GET (how images reach the app)
assert httpx.get(url.split("?")[0]).status_code in (400, 401, 403, 404)    # 4. unsigned GET refused: the bucket is private
assert s.delete_prefix("check/") == 1                                      # 5. delete
print("bucket ok")
EOF
```

It prints `bucket ok`. Any error names the call that failed; fix the bucket or keys before going on.

Note on the demo's per-IP limit: Cloud Run appends the visitor's IP as the last `X-Forwarded-For` entry, and the limit relies on that. Putting a Google external load balancer in front adds its own IP last and would need a code change.

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

### 4. Delete a workspace's data

When a workspace is deleted in the app, call this from the app's server:

```
DELETE https://<bot-domain>/v1/apps/<app-id>/workspaces/<workspace-id>
Authorization: Bearer <token>
```

The token is minted like the session token, but with `access: "delete"` and the same `sub` as the workspace. It removes the workspace's threads, checkpoints and stored files, and answers `{"deleted_threads": n}`.
