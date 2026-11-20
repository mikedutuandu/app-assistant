"""The demo's four endpoints of contract 1 (docs/integration.md): POST /demo/api/assistant/{context,drafts,material,charge}.

The bot reaches the demo the way it reaches any app (`RestHttpTools`, `api_base` in apps/demo.yaml). Everything a
visitor saves is kept per demo workspace ("demo:<cookie id>") and deleted after a day (cleanup.py).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb

from assistant.api.deps import ApiError, bearer
from assistant.auth import AuthError, Session, verify_session
from assistant.db import new_id

log = logging.getLogger("assistant.demo")

router = APIRouter()
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TEXT_CHARS = 4000
MAX_THREAD_POSTS = 25  # the later posts of a thread
MAX_FACTS = 100
MAX_DRAFTS_KEPT = 100  # per demo workspace, until the daily cleanup
MAX_FACTS_KEPT = 300
MAX_KEY_CHARS = 200
DEMO_FULL = "The demo is full for today."
RECENT_POSTS = 10
VOICE = "friendly, plain, a small team's founder"


class Failure(Exception):
    """An answer that is not 200: JSON `{error, message}` with this status (docs/integration.md, error codes)."""

    def __init__(self, status: int, error: str, message: str | None = None):
        super().__init__(error)
        self.status = status
        self.body: dict[str, str] = {"error": error} | ({"message": message} if message is not None else {})


def invalid(message: str) -> Failure:
    return Failure(400, "invalid", message)


def draft_key(workspace: str, draft_id: str) -> str:
    return f"demo/{workspace}/drafts/{draft_id}"


def image_allowed(storage, url: str, workspace: str) -> bool:
    """Only a URL of this visitor's own files in our bucket: same scheme and host, and the path under
    "<endpoint path>/<bucket>/demo/<workspace>/" (path-style: "/bucket/..." on S3, "/storage/v1/s3/bucket/..." on
    Supabase). Anything else on the same host (another bucket, Supabase's REST API) is refused."""
    allowed = urlsplit(storage.object_url(f"demo/{workspace}/"))
    parts = urlsplit(url)
    if (parts.scheme, parts.netloc) != (allowed.scheme, allowed.netloc) or not parts.path.startswith(allowed.path):
        return False
    path = unquote(parts.path)
    return "\\" not in path and not any(segment in (".", "..") for segment in path.split("/"))


async def _copy_image(state, url: str, workspace: str, key: str) -> None:
    if not image_allowed(state.storage, url, workspace):
        raise invalid("That image is not one this demo can copy.")
    data = bytearray()
    mime = "application/octet-stream"
    async with state.http.stream("GET", url, timeout=20, follow_redirects=False) as response:
        if response.status_code != 200:
            raise invalid("The image could not be read.")
        mime = response.headers.get("content-type", mime)
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > MAX_IMAGE_BYTES:
                raise invalid("That image is too big.")
    await asyncio.to_thread(state.storage.put, key, bytes(data), mime)


async def _context(state, session: Session, _body: dict, _key: str | None) -> dict:
    async with state.pool.connection() as conn:
        cur = await conn.execute(
            "select text from demo_drafts where workspace = %s order by created_at desc, id desc limit %s",
            (session.workspace, RECENT_POSTS),
        )
        posts = [r[0] for r in await cur.fetchall()]
    return {
        "language": "en",
        "voice": VOICE,
        "topics": ["product updates"],
        "recent_posts": posts,
        "patterns": [],
        "credits_left": None,  # the demo has no credits: the bot records its spend against a daily cap instead
    }


async def _existing_draft(pool, workspace: str, idem_key: str) -> str | None:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select id from demo_drafts where workspace = %s and idem_key = %s", (workspace, idem_key)
        )
        row = await cur.fetchone()
    return row[0] if row else None


async def _save_draft(state, session: Session, body: dict, key: str | None) -> dict:
    assert key is not None
    text = body.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
        raise invalid("The draft needs some text.")
    more = body.get("thread")
    if more is None:
        more = []
    if not isinstance(more, list) or len(more) > MAX_THREAD_POSTS:
        raise invalid("The thread is too long.")
    if not all(isinstance(p, str) and len(p) <= MAX_TEXT_CHARS for p in more):
        raise invalid("A post in the thread is not valid.")
    more = [p.strip() for p in more if p.strip()]
    media = body.get("media")
    if media is not None and not isinstance(media, list):
        raise invalid("The pictures are not valid.")
    existing = await _existing_draft(state.pool, session.workspace, key)
    if existing is not None:
        return {"draft_id": existing, "edit_url": None}
    async with state.pool.connection() as conn:
        cur = await conn.execute("select count(*) from demo_drafts where workspace = %s", (session.workspace,))
        if (await cur.fetchone())[0] >= MAX_DRAFTS_KEPT:
            raise Failure(429, "demo_full", DEMO_FULL)
    draft_id = new_id()
    image_key = None
    first = media[0] if media and isinstance(media[0], dict) else None
    if first is not None and isinstance(first.get("url"), str):
        image_key = draft_key(session.workspace, draft_id)
        await _copy_image(state, first["url"], session.workspace, image_key)
    async with state.pool.connection() as conn:
        cur = await conn.execute(
            "insert into demo_drafts (id, workspace, text, posts, image_key, idem_key) values (%s, %s, %s, %s, %s, %s)"
            " on conflict (workspace, idem_key) do nothing returning id",
            (draft_id, session.workspace, text.strip(), Jsonb(more) if more else None, image_key, key),
        )
        inserted = await cur.fetchone()
    if inserted is None:  # a parallel call with the same key won; its row is the draft
        if image_key:
            await asyncio.to_thread(state.storage.delete_prefix, image_key)
        draft_id = await _existing_draft(state.pool, session.workspace, key) or draft_id
    return {"draft_id": draft_id, "edit_url": None}


async def _save_material(state, session: Session, body: dict, key: str | None) -> dict:
    assert key is not None
    facts = body.get("facts")
    if not isinstance(facts, list) or len(facts) > MAX_FACTS or not all(isinstance(f, str) for f in facts):
        raise invalid("There are no facts to keep.")
    texts = [t[:MAX_TEXT_CHARS] for t in (f.strip() for f in facts) if t]
    draft_id = body.get("draft_id")
    if draft_id is not None and not isinstance(draft_id, str):
        raise invalid("That draft is not valid.")
    async with state.pool.connection() as conn:
        # A repeat of this key adds nothing, so its rows don't count against the cap.
        cur = await conn.execute(
            "select count(*), count(*) filter (where idem_key = %s) from demo_facts where workspace = %s",
            (key, session.workspace),
        )
        total, same_key = await cur.fetchone()
        if total - same_key + len(texts) > MAX_FACTS_KEPT:
            raise Failure(429, "demo_full", DEMO_FULL)
        for text in texts:
            await conn.execute(
                "insert into demo_facts (id, workspace, text, draft_id, idem_key) values (%s, %s, %s, %s, %s)"
                " on conflict (workspace, idem_key, text) do nothing",
                (new_id(), session.workspace, text, draft_id, key),
            )
    return {"ok": True}


async def _charge(_state, _session: Session, body: dict, _key: str | None) -> dict:
    """The demo has no credits: the bot records a demo turn's spend against its own daily cap instead of charging
    (api/turn.py), so this only checks the body and says ok."""
    cost, ref = body.get("cost_usd"), body.get("ref")
    if isinstance(cost, bool) or not isinstance(cost, int | float) or cost < 0 or not isinstance(ref, str) or not ref:
        raise invalid("A charge needs cost_usd and ref.")
    return {"ok": True}


Handler = Callable[[Any, Session, dict, str | None], Awaitable[dict]]
# endpoint -> (handler, whether it needs an Idempotency-Key)
ENDPOINTS: dict[str, tuple[Handler, bool]] = {
    "context": (_context, False),
    "drafts": (_save_draft, True),
    "material": (_save_material, True),
    "charge": (_charge, True),
}


async def _serve(request: Request, endpoint: str) -> JSONResponse:
    state = request.app.state
    pack = await state.packs.get("demo")
    if pack is None:
        return JSONResponse({"error": "app"}, status_code=404)
    try:
        session = verify_session(bearer(request), pack)
    except (AuthError, ApiError):  # a bad, expired or other app's token, or no bearer header
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    handler, keyed = ENDPOINTS[endpoint]
    try:
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise invalid("The request body must be a JSON object.")
        key = request.headers.get("idempotency-key")
        if keyed and (not key or len(key) > MAX_KEY_CHARS):
            raise invalid("The Idempotency-Key header is missing or too long.")
        out = await handler(state, session, body, key)
    except Failure as e:
        return JSONResponse(e.body, status_code=e.status)
    log.info("demo endpoint=%s", endpoint)
    return JSONResponse(out)


def _route(endpoint: str) -> None:
    async def handle(request: Request) -> JSONResponse:
        return await _serve(request, endpoint)

    router.add_api_route(f"/demo/api/assistant/{endpoint}", handle, methods=["POST"], name=f"demo_{endpoint}")


for _endpoint in ENDPOINTS:
    _route(_endpoint)
