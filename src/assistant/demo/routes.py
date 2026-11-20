"""The public Shipnote demo: its page, its anonymous session, and the drafts a visitor has saved."""

import asyncio
import re
import secrets
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from assistant.api.deps import ApiError, client_ip
from assistant.auth import mint_session
from assistant.limits import hit, ip_key

router = APIRouter()
COOKIE = "demo_id"
COOKIE_MAX_AGE = 86400
SESSIONS_PER_HOUR = 10
DRAFTS_LISTED = 50
_ID = re.compile(r"[0-9a-f]{32}")
_PAGE = (Path(__file__).parent / "page.html").read_text()


def _demo_id(request: Request) -> str | None:
    value = request.cookies.get(COOKIE, "")
    return value if _ID.fullmatch(value) else None


@router.get("/demo")
async def page() -> HTMLResponse:
    return HTMLResponse(_PAGE, headers={"Cache-Control": "no-cache"})


@router.post("/demo/api/assistant/session")
async def session(request: Request) -> JSONResponse:
    state = request.app.state
    pack = state.packs.get("demo")
    if pack is None:
        raise ApiError(404, "app")
    if not await hit(state.pool, f"demo-session:{ip_key(client_ip(request))}", timedelta(hours=1), SESSIONS_PER_HOUR):
        return JSONResponse(
            {"error": "limit", "message": "Too many demo sessions from here. Try again in a bit."}, status_code=429
        )
    demo_id = _demo_id(request) or secrets.token_hex(16)
    response = JSONResponse({"token": mint_session(pack, "demo:" + demo_id)})
    https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(COOKIE, demo_id, max_age=COOKIE_MAX_AGE, path="/", httponly=True, samesite="lax", secure=https)
    return response


@router.get("/demo/api/drafts")
async def drafts(request: Request) -> list[dict]:
    demo_id = _demo_id(request)
    if demo_id is None:
        return []
    state = request.app.state
    async with state.pool.connection() as conn:
        cur = await conn.execute(
            "select id, text, image_key, created_at from demo_drafts where workspace = %s"
            " order by created_at desc, id desc limit %s",
            ("demo:" + demo_id, DRAFTS_LISTED),
        )
        rows = await cur.fetchall()
    out = []
    for draft_id, text, image_key, created_at in rows:
        url = await asyncio.to_thread(state.storage.signed_url, image_key, 300) if image_key else None
        out.append({"id": draft_id, "text": text, "image_url": url, "created_at": created_at.isoformat()})
    return out
