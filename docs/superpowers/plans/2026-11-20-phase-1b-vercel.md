# The writing bot, Phase 1b (move hosting to Vercel): implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the bot on Vercel (Python runtime, Fluid compute) with its data on Supabase, without losing any Phase 1 guarantee.

**Architecture:** The FastAPI app stays as it is and becomes one Vercel Function. Four things change because a Vercel Function isn't a long-running container:
- **Uploads** go from the browser straight to Supabase Storage. Vercel caps request bodies at 4.5 MB.
- **The demo clean-up** runs from a Vercel Cron call instead of an in-process loop.
- **Cost** is recorded after every model call, not only at the end of a turn.
- **Connections** use tiny database pools.

The Docker and Cloud Run files are removed, and `vercel.json` plus a Vercel README section are added.

**Tech Stack:** As in Phase 1. Vercel Python runtime (3.12, uv from `pyproject.toml`/`uv.lock`); Supabase Storage's own signed upload URLs.

**Spec:** `docs/superpowers/specs/2026-11-20-app-writing-bot-design.md`. Mike moved hosting to Vercel on 20 Nov 2026 (11:18).

## Global Constraints

**Vercel limits**
- Request and response bodies are at most 4.5 MB, except streamed responses. Max duration is 300 s on Hobby. Shutdown gets 500 ms. Cron runs at most once a day on Hobby.
- Nothing in the bot may rely on work continuing after the response ends, or on a background loop.

**Uploads**
- The upload caps from Phase 1 don't change (image 10 MB, PDF 20 MB and 30 pages, voice 5 minutes, 5 files a message; the demo has its smaller caps).
- The browser uploads the bytes straight to storage. The bot only issues the upload target and then checks the stored object: its real size against the caps and its type by content.
- An object that fails the check is deleted, and no `files` row is written for it.

**Cost**
- Every model call's cost is recorded as soon as the call ends, idempotently by `"<message_id>:<n>"`.
  - On real apps through `assistant_charge`, which HookPlaybook dedupes per `ref`.
  - On the demo through `hit("demo-usd", …)`.
- A turn whose connection drops loses at most the cost of the call in flight.

**Database**
- `DB_POOL_MAX` defaults to 2 and `SAVER_POOL_MAX` to 1.
- Supabase's Session pooler (port 5432) stays the documented URL.

**Other**
- `SHUTDOWN_DRAIN_SECONDS` defaults to 0.4.
- Client IP, in order: `x-vercel-forwarded-for`, then `x-real-ip`, then the rightmost `x-forwarded-for`.
- The demo clean-up endpoint is `GET /internal/cleanup`. It answers only to `Authorization: Bearer ${CRON_SECRET}` (else 401), and does nothing when `CRON_SECRET` is unset.

## Review Focus

1. **A file the browser claims is an image but is something else, or one larger than the cap,** is deleted and refused. The check reads the stored bytes, not what the browser said. (Task 1)
2. **An upload target can only write to its own key under `{app}/{workspace}/{thread}/`.** It is single-use and expires in 10 minutes. (Task 1)
3. **A dropped connection mid-turn still leaves every finished model call charged exactly once.** A retried charge with the same ref adds nothing. (Task 2)
4. **The cleanup endpoint is unusable without the cron secret.** (Task 3)
5. **A cold start on Vercel runs migrations safely when several instances start together.** It uses the existing advisory lock. (Task 4)

---

### Task 1: Direct uploads to Supabase Storage

**Files:**
- Modify: `src/assistant/files/storage.py` (adds `upload_target` and `head`), `files/intake.py` (`accept_upload` splits into `start_upload` and `finish_upload`), `api/chat.py` (two routes replace `POST /files`), `config.py` (`supabase_url: str | None`, `supabase_service_key: str | None`)
- Modify the widget: `widget/src/api.ts` (`upload` does a 3-step flow), rebuild `static/widget.js`
- Tests: `tests/test_files_intake.py`, `tests/test_api_chat.py`, `widget/test/api.test.ts`

**Interfaces:**
- `Storage.upload_target(key: str, mime: str) -> dict`:
  - When `supabase_url` and `supabase_service_key` are set: `POST {supabase_url}/storage/v1/object/upload/sign/{bucket}/{key}` with the service key returns `{"url": "<signed upload URL>", "method": "PUT", "headers": {"content-type": mime, "x-upsert": "false"}}`. The URL is Supabase's own signed upload link; Supabase allows browser CORS on it.
  - Otherwise it uses an S3 presigned PUT (`ExpiresIn` 600, `ContentType` mime) and returns `{"url", "method": "PUT", "headers": {"content-type": mime}}`.
- `Storage.head(key) -> int | None`: the object's size, or None when missing.
- `POST /v1/chat/{app}/uploads` with `{thread_id | null, name, mime, size}`:
  - It checks the claimed size and type against the pack's caps first (a fast refusal, not trusted).
  - It creates the thread if needed, applies the per-user upload limit, and reserves `upload_id = new_id()` with key `{app}/{workspace}/{thread}/{upload_id}`.
  - It stores a pending row (`files` with `kind = 'pending'` and `expires_at` now + 15 min).
  - It returns `{upload_id, thread_id, target}`.
- `POST /v1/chat/{app}/uploads/{upload_id}/finish`:
  1. Loads the pending row (same app, workspace and thread, else 404).
  2. Calls `head()`: missing gives 400 `{"error":"missing","message":"The file didn't arrive. Try again."}`; over the cap deletes it and returns `too_big`.
  3. Reads the object and sniffs it exactly as `accept_upload` did, deleting it and returning `UploadError` on failure.
  4. Turns the row into the real kind (`image`, `pdf` or `voice`), clears `expires_at` (or keeps a day for the demo), and returns `{file_id, kind, thread_id}`.

  Calling finish twice returns the same result.
- `POST /files` is removed. The widget's `upload(threadId, file, onProgress)` keeps its signature: it calls `/uploads`, PUTs to `target.url` with `target.headers` (XHR for progress), then calls `/finish`.
- The demo clean-up (Task 3) also deletes expired `pending` rows and their objects.

- [ ] **Step 1: write failing tests.**
  - `upload_target uses Supabase signed upload when configured` (respx on the sign endpoint)
  - `upload_target falls back to S3 presign` (moto)
  - `finish refuses missing object`
  - `finish deletes and refuses a PDF uploaded as an image name to the demo` (the demo takes no PDFs)
  - `finish deletes and refuses an object over the cap`
  - `finish is idempotent`
  - `another workspace's upload id is 404`
  - widget: `upload calls uploads, PUTs with the target headers, then finish`
- [ ] **Step 2:** run them. Expected: FAIL.
- [ ] **Step 3:** implement, and rebuild the widget.
- [ ] **Step 4:** run the full Python and widget suites. Expected: PASS.
- [ ] **Step 5:** commit "feat: uploads go straight to storage (Vercel's 4.5 MB limit)".

### Task 2: Record cost after every model call

**Files:**
- Modify: `src/assistant/graph/turn.py` (`Turn` gets an async `on_cost(cost_usd: float, n: int)` callback, called after every model call with that call's cost, failed calls included), `api/turn.py` (passes `on_cost`, which charges `ref=f"{message_id}:{n}"` on real apps and `hit(DEMO_USD_KEY, amount=cost)` on the demo; the end-of-turn charge is removed)
- Tests: `tests/test_graph.py`, `tests/test_api_chat.py`

**Interfaces:**
- `on_cost` never raises; failures are logged by kind only.
- `done` still reports the turn's total `cost_usd`.
- The `Llm` stream cost is known only at the end of a call, so a call cut off mid-stream is not charged (accepted).

- [ ] **Step 1: write failing tests.**
  - `each model call charges once with its own ref`
  - `a turn whose client disconnects after two calls has charged both`
  - `demo spend recorded per call`
  - `retry of the same ref charges once` (FakeTools dedupes by ref, as HookPlaybook does)
- [ ] **Step 2:** run them. Expected: FAIL.
- [ ] **Step 3:** implement.
- [ ] **Step 4:** run the full suite. Expected: PASS.
- [ ] **Step 5:** commit "feat: charge after every model call".

### Task 3: Demo clean-up from Vercel Cron

**Files:**
- Modify: `src/assistant/main.py` (removes the hourly loop and the startup clean-up), `demo/cleanup.py` (also expires `pending` uploads), new route in `demo/routes.py` or `api/internal.py` (`GET /internal/cleanup`), `config.py` (`cron_secret: str | None`)
- Tests: `tests/test_demo.py`

**Interfaces:**
- The route answers 401 without the exact bearer (compared with `hmac.compare_digest`), and 503 `{"error":"off"}` when `CRON_SECRET` is unset. Otherwise it runs `cleanup_demo(app.state)` and returns its counts.

- [ ] **Step 1: write failing tests.** `cleanup needs the cron secret`, `cleanup runs with it and removes day-old rows`, `no background loop started` (the lifespan creates no task).
- [ ] **Step 2:** run them. Expected: FAIL.
- [ ] **Step 3:** implement.
- [ ] **Step 4:** run the full suite. Expected: PASS.
- [ ] **Step 5:** commit "feat: demo clean-up runs from Vercel Cron".

### Task 4: Vercel deploy files, pools, IP, docs

**Files:**
- Create: `vercel.json`, `src/assistant/asgi.py` (`app = create_app()` for Vercel's entrypoint)
- Modify:
  - `pyproject.toml`: `[tool.vercel] entrypoint = "assistant.asgi:app"`
  - `config.py`: defaults `db_pool_max = 2`, `saver_pool_max = 1`, `shutdown_drain_seconds = 0.4`
  - `api/deps.py`: client IP order
  - `README.md`: "Deploy on Vercel" replaces "Deploy on Google Cloud Run", and every other Cloud Run or Secret Manager line (the HookPlaybook section's `gcloud run services update` line included) becomes its Vercel equivalent
  - `.env.example`: adds `CRON_SECRET`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`
  - the spec and Phase 1 plan: one dated line each, "20 Nov 2026: Mike moved hosting from Cloud Run to Vercel."
- Delete: `Dockerfile`, `.dockerignore`

**Interfaces:**
- `vercel.json`:

  ```json
  {"$schema":"https://openapi.vercel.sh/vercel.json",
   "functions":{"src/assistant/asgi.py":{"maxDuration":300,"excludeFiles":"{tests/**,widget/**,docs/**,.superpowers/**,**/test_*.py}"}},
   "crons":[{"path":"/internal/cleanup","schedule":"0 3 * * *"}]}
  ```

  The schedule is daily so it works on Hobby. `/widget.js` keeps being served by the app route.
- README "Deploy on Vercel":
  1. Import the GitHub repo in Vercel; the framework preset is FastAPI, Python 3.12 from `.python-version`.
  2. Set the environment variables: everything in `.env.example`, with `DATABASE_URL` = the Supabase Session pooler, the Supabase S3 values, `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` (for the signed upload links), `CRON_SECRET` (`openssl rand -hex 32`), `PUBLIC_BASE_URL` = the production domain, and each pack's session secret (`HOOKPLAYBOOK_SESSION_SECRET`, the same value as HookPlaybook's `ASSISTANT_TOKEN_SECRET`; the service refuses to start without it).
  3. Deploy; a push to `main` redeploys.
  4. Check: `curl https://<domain>/health`, then `/demo`.

  It also covers:
  - the connection budget (each active instance holds up to `DB_POOL_MAX + SAVER_POOL_MAX` = 3; check Supabase's pooler limit)
  - the Hobby limits (300 s turns, daily clean-up)
  - the bucket check from Phase 1, plus one browser upload test.

- [ ] **Step 1: write failing tests.**
  - `asgi module exposes app`
  - `client ip prefers x-vercel-forwarded-for`, `then x-real-ip`, `then rightmost x-forwarded-for`
  - `pool defaults are 2 and 1`
  - `vercel.json is valid and points at the entrypoint` (load the JSON; check the function key file exists)
- [ ] **Step 2:** run them. Expected: FAIL.
- [ ] **Step 3:** implement, delete the Docker files, and update the docs.
- [ ] **Step 4:** run the full Python and widget suites, and `ruff`. Expected: PASS.
- [ ] **Step 5:** commit "chore: deploy on Vercel".

### Task 5: Faster cold starts

Added 20 Nov 2026 after research on LangGraph on Vercel: every cold start imports everything, but most messages need no PDF or image library.

**Files:**
- Modify: `src/assistant/files/read.py` and `files/intake.py` (import `pypdfium2`, `pypdf` and `PIL` inside the functions that use them), any other module that imports them at the top
- Create: `scripts/cold_start.py` (prints the seconds `import assistant.asgi` takes, best of 3 fresh interpreters)
- Tests: `tests/test_cold_start.py`

**Interfaces:**
- `import assistant.asgi` leaves `pypdfium2`, `pypdf` and `PIL` out of `sys.modules` (checked in a fresh interpreter with `subprocess`).
- Reading a PDF or an image still works exactly as before (the existing file tests stay green).
- README "Deploy on Vercel" gets one line with the measured import time before and after, and how to re-measure (`uv run python scripts/cold_start.py`).

- [ ] **Step 1: write the failing test.** `importing the app loads no PDF or image library`.
- [ ] **Step 2:** run it. Expected: FAIL.
- [ ] **Step 3:** measure, move the imports, measure again.
- [ ] **Step 4:** run the full Python suite and `ruff`. Expected: PASS.
- [ ] **Step 5:** commit "perf: load PDF and image libraries only when a file arrives". Open the PR against `main` and tell Mike.

## After merge (Mike, with Claude's help if he says so)

1. Create the Vercel project from `mikedutuandu/app-assistant` and set the environment variables (Task 4's README). Creating it in Mike's account needs his go-ahead.
2. Deploy a preview and check four things: the cold start time, one full streamed chat turn on `/demo`, one browser upload straight to Supabase, and the database connections held (Supabase dashboard).
