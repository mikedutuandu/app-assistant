# Deploying app-assistant

Two ways to run it in production: **Vercel** (one Python Function, Postgres on Neon, files in an S3-compatible bucket)
or **your own server with Docker** (the bot, Postgres and files on local disk). Every variable is explained in
[`.env.example`](../.env.example).

## Vercel

The service runs as one Python Function (Fluid compute), built from `pyproject.toml` and `vercel.json`; the entrypoint
is the root-level `app.py`, which imports the app from `src/assistant/asgi.py`. Python 3.12 comes from `.python-version`.

### 1. Import the project

Use the **Deploy with Vercel** button in the README, or **Add New → Project** and import your fork. The framework preset
is **FastAPI**. Leave the build and output settings alone: `vercel.json` sets the function (300 s, test files left out)
and the daily clean-up cron.

### 2. Postgres on Neon

Add Neon from the Vercel Marketplace (the project's **Storage** tab → **Create database**), for Production and Preview.
Neon sets `DATABASE_URL` (pooled) and `DATABASE_URL_UNPOOLED` (direct), or the same names with a `NEON_` prefix if you
choose one; the bot reads both spellings and uses the **direct** connection for everything (app queries, checkpoints,
migrations), because a transaction pooler would lose `search_path` and advisory locks between statements.

The bot keeps every table, LangGraph's checkpoints included, in its own schema, `DB_SCHEMA` (default `assistant`),
created on the first start. On a cold start the migrations and LangGraph's checkpoint setup run under one Postgres
advisory lock, so several instances starting together run them once while the others wait.

### 3. A bucket for files

A function's disk is temporary, so on Vercel files need an S3-compatible bucket. Set `S3_ENDPOINT`, `S3_REGION`,
`S3_BUCKET`, `S3_ACCESS_KEY` and `S3_SECRET_KEY`.

- **Neon Object Storage.** Neon console → your project → **Object Storage** → enable it → **Create bucket**. Then
  **Connect** → **Storage** → **Parameters only** → **Reveal credential**: `AWS_ENDPOINT_URL_S3` is `S3_ENDPOINT`,
  `AWS_REGION` is `S3_REGION`, `AWS_ACCESS_KEY_ID` is `S3_ACCESS_KEY` and `AWS_SECRET_ACCESS_KEY` is `S3_SECRET_KEY`
  (keep its prefix).
- **Cloudflare R2, AWS S3, MinIO** work the same way (`S3_REGION=auto` for R2).

The browser uploads straight to the bucket with a presigned `PUT` (Vercel caps a request body at 4.5 MB, so files never
pass through the function), sending `content-type` and `if-none-match: *` (the link writes once). At startup the bot sets
the bucket's CORS itself (`PutBucketCors`: every app's `allowed_origins` and `PUBLIC_BASE_URL`'s origin; `GET` and `PUT`;
headers `content-type` and `if-none-match`). If that call fails it logs one warning (`could not set the bucket's CORS`)
and starts anyway; then set it by hand, for example on S3 or R2:

```json
[{"AllowedOrigins": ["https://your-app.example", "https://bot.example.com"], "AllowedMethods": ["PUT", "GET"], "AllowedHeaders": ["if-none-match", "content-type"], "MaxAgeSeconds": 600}]
```

The store must honour `If-None-Match` on a `PUT`, or an upload link could replace a file the bot already checked. The bot
sniffs the stored bytes and deletes anything that is not an image, a PDF or a voice format; the per-app caps (image,
PDF, voice) are enforced by the bot.

### 4. Function region

Settings → Functions → **Function Region**: the Vercel region closest to your database (`iad1`, Washington, D.C., for
Neon in AWS US East). A function far from its database pays that distance on every query, checkpoint write and storage
call, dozens of times per turn. The region is left out of `vercel.json` because it depends on where your database is.

### 5. Environment variables

Settings → Environment Variables, for Production and Preview (mark every secret **Sensitive**). A Preview uses the
production database and bucket, so only deploy previews you trust.

| Variable | Value |
|---|---|
| `OPENROUTER_API_KEY` | your OpenRouter key |
| `DATABASE_URL`, `DATABASE_URL_UNPOOLED` | set by the Neon integration |
| `S3_ENDPOINT`, `S3_REGION`, `S3_BUCKET`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` | the bucket above |
| `PUBLIC_BASE_URL` | the production domain, `https://bot.example.com` (no trailing slash); set it after the first deploy, then redeploy |
| `DEMO_SESSION_SECRET` | `openssl rand -hex 32` (the `/demo` page) |
| `<APP>_SESSION_SECRET` | one per app you register (`apps/registry.yaml` or `APPS`), shared with that app |
| `CRON_SECRET` | `openssl rand -hex 32`: Vercel Cron sends it to `/internal/cleanup`, which refuses everything else (off while unset) |
| `DB_SCHEMA`, `DB_POOL_MAX`, `SAVER_POOL_MAX` | leave unset for `assistant`, 2 and 1 |
| `MAX_MESSAGE_USD`, `ALLOWED_MODELS`, `MAX_APP_DAILY_USD` | your ceilings over every app ([docs/integration.md](integration.md)) |

An app whose secret variable is unset is off: the startup log says `app 'x' is off: set X_SESSION_SECRET to serve it`.
With no app left, the function does not start. Do not set `RUN_LIFESPAN=false` in production (it skips the database and
storage). `SHUTDOWN_DRAIN_SECONDS` stays at `0.4`: Vercel gives a function about 500 ms at shutdown.

### 6. Deploy and check

Deploy once; after that every push to your production branch redeploys, and a change to a variable applies from the
next deployment.

1. `curl https://<domain>/health` answers `{"ok":true}`; the log shows `uploads: s3` and no error.
2. Open `https://<domain>/demo` on a phone and send a message: a reply streams in.
3. Attach a small photo: the network tab shows `POST /v1/chat/demo/uploads`, a `PUT` straight to the bucket (not to
   the bot), then `POST .../uploads/.../finish`. A `.txt` file renamed to `.png` must be refused.
4. In the first deploy's build log, confirm Vercel used `app.py` as the entrypoint and applied the 300 s limit.

**Check the bucket once**, from your machine with the real `S3_*` values in `.env`. It does each call the service
makes, then cleans up:

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

### Connection budget

Each instance keeps one connection for its own queries and one for LangGraph's checkpoints while it is alive (idle or
suspended ones included), and opens up to `DB_POOL_MAX` + `SAVER_POOL_MAX` (3) when busy. So the budget is **instances
alive × 2, up to × 3 when busy**; keep it under your database's connection limit. Each connection is checked before
use, so one dropped while the instance slept is replaced instead of failing a request.

### Limits on Vercel's Hobby plan

A function runs at most 300 s per request (a chat turn is one streamed response, so a turn is cut at 5 minutes), and a
cron runs at most once a day, which is why the clean-up (`GET /internal/cleanup`: the demo's data older than a day, and
every app's expired, unfinished uploads) runs daily at 03:00 UTC. Nothing relies on work continuing after a response
ends: every model call is charged as soon as it finishes. A scanned PDF is read as images, one model call per page, 4 at
a time, with the message's cost cap checked before each; cap the pages with a pack's `files.pdf_pages`. If a stream is
cut anyway, the chat window says "The answer was cut off. Try again."

The demo's per-IP limit reads the client IP from `x-vercel-forwarded-for`, then `x-real-ip`, then the rightmost
`x-forwarded-for` entry, all set by Vercel's edge. Putting another proxy in front changes what they hold and needs a
change in `client_ip`.

### Supabase instead of Neon

- **Postgres:** use the **Session pooler** string (port 5432) as `DATABASE_URL` and leave `DATABASE_URL_UNPOOLED`
  empty; percent-encode special characters in the password. Not the Transaction pooler (port 6543: migrations hold a
  session advisory lock and psycopg uses prepared statements), and not the direct host `db.REF.supabase.co` (IPv6 only
  on most plans; Vercel Functions use IPv4).
- **Keep the tables private:** never add the bot's schema (`DB_SCHEMA`) to Project Settings → Data API → **Exposed
  schemas**, and never set `DB_SCHEMA=public`.
- **Files:** a private Storage bucket (file size limit 25 MB, allowed MIME types for images, PDF and audio, no wildcard),
  its S3 endpoint and access key as `S3_*`, and the project URL and a secret key (`sb_secret_…`) as `SUPABASE_URL` and
  `SUPABASE_SERVICE_KEY`: the browser then uploads through Supabase signed upload links, and the bot does not touch CORS.
  If either `SUPABASE_*` is missing, uploads fall back to the S3 presigned `PUT`; the startup log names the one in use
  (`uploads: supabase` or `uploads: s3`).
- **Region:** set Function Region to the Supabase project's region (for example `sin1` for `ap-southeast-1`).

## Your own server (Docker)

`docker compose up --build` runs the bot, Postgres 16 and local file storage with no cloud account (see the README).
To put it online:

- Set your own secrets (`openssl rand -hex 32`) in the shell or a `.env` next to `docker-compose.yml`. The compose
  file's defaults are public, so the bot refuses to start with them unless `PUBLIC_BASE_URL` is `localhost`,
  `127.0.0.1` or `::1`.
- Set `PUBLIC_BASE_URL` to your domain (upload and picture links are built from it), put the bot behind HTTPS, and
  change the port mapping from `127.0.0.1:8000:8000` to `8000:8000`.
- Local files live on one machine's disk: run one bot container (or share the volume). Use a bucket (`S3_*`) for
  several machines.
