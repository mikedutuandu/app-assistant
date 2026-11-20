"""The demo's tiny MCP server (JSON-RPC 2.0 over HTTP, stateless): the four tools the bot calls on a real app.

Everything a visitor saves is kept per demo workspace ("demo:<cookie id>") and deleted after a day (cleanup.py).
"""

import asyncio
import json
import logging
import uuid
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from assistant.api.deps import ApiError, bearer
from assistant.auth import AuthError, Session, verify_session
from assistant.db import new_id

log = logging.getLogger("assistant.demo")

router = APIRouter()
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TEXT_CHARS = 4000
MAX_FACTS = 100
RECENT_POSTS = 10
PROTOCOL_VERSION = "2025-03-26"
VOICE = "friendly, plain, a small team's founder"

_OBJECT = {"type": "object"}
TOOLS = [
    {"name": "assistant_context", "description": "The demo's voice, topics and recent drafts.", "inputSchema": _OBJECT},
    {
        "name": "save_draft",
        "description": "Save a draft post (with an optional image) for this visitor.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "media": {"type": "array", "items": _OBJECT}},
            "required": ["text"],
        },
    },
    {
        "name": "save_material",
        "description": "Keep facts for next week's posts.",
        "inputSchema": {
            "type": "object",
            "properties": {"facts": {"type": "array", "items": _OBJECT}, "draft_id": {"type": ["string", "null"]}},
            "required": ["facts"],
        },
    },
    {"name": "assistant_charge", "description": "No-op: the demo has no credits.", "inputSchema": _OBJECT},
]


class ToolFailure(Exception):
    """A tool call that fails with a message the bot passes on (JSON-RPC isError result)."""


def draft_key(workspace: str, draft_id: str) -> str:
    return f"demo/{workspace}/drafts/{draft_id}"


def _storage_host(storage) -> str:
    """The host this deployment's storage signs URLs for: the configured endpoint, else AWS."""
    return urlsplit(storage.signed_url("host-probe", 1)).netloc


async def _copy_image(state, url: str, key: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or parts.netloc != _storage_host(state.storage):
        raise ToolFailure("That image is not one this demo can copy.")
    data = bytearray()
    mime = "application/octet-stream"
    async with state.http.stream("GET", url, timeout=20, follow_redirects=False) as response:
        if response.status_code != 200:
            raise ToolFailure("The image could not be read.")
        mime = response.headers.get("content-type", mime)
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > MAX_IMAGE_BYTES:
                raise ToolFailure("That image is too big.")
    await asyncio.to_thread(state.storage.put, key, bytes(data), mime)


async def _context(state, session: Session, _args: dict, _key: str) -> dict:
    async with state.pool.connection() as conn:
        cur = await conn.execute(
            "select text from demo_drafts where workspace = %s order by created_at desc, id desc limit %s",
            (session.workspace, RECENT_POSTS),
        )
        posts = [r[0] for r in await cur.fetchall()]
    return {
        "voice": VOICE,
        "topics": ["product updates"],
        "recent_posts": posts,
        "patterns": [],
        "credits_left": None,
        "out_of_credits_message": None,
        "language": "en",
    }


async def _existing_draft(pool, workspace: str, idem_key: str) -> str | None:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select id from demo_drafts where workspace = %s and idem_key = %s", (workspace, idem_key)
        )
        row = await cur.fetchone()
    return row[0] if row else None


async def _save_draft(state, session: Session, args: dict, key: str) -> dict:
    text = args.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
        raise ToolFailure("The draft needs some text.")
    existing = await _existing_draft(state.pool, session.workspace, key)
    if existing is not None:
        return {"id": existing, "edit_url": None}
    draft_id = new_id()
    image_key = None
    media = args.get("media")
    first = media[0] if isinstance(media, list) and media and isinstance(media[0], dict) else None
    if first is not None and isinstance(first.get("url"), str):
        image_key = draft_key(session.workspace, draft_id)
        await _copy_image(state, first["url"], image_key)
    async with state.pool.connection() as conn:
        cur = await conn.execute(
            "insert into demo_drafts (id, workspace, text, image_key, idem_key) values (%s, %s, %s, %s, %s)"
            " on conflict (workspace, idem_key) do nothing returning id",
            (draft_id, session.workspace, text.strip(), image_key, key),
        )
        inserted = await cur.fetchone()
    if inserted is None:  # a parallel call with the same key won; its row is the draft
        if image_key:
            await asyncio.to_thread(state.storage.delete_prefix, image_key)
        draft_id = await _existing_draft(state.pool, session.workspace, key) or draft_id
    return {"id": draft_id, "edit_url": None}


async def _save_material(state, session: Session, args: dict, key: str) -> dict:
    facts = args.get("facts")
    if not isinstance(facts, list) or len(facts) > MAX_FACTS:
        raise ToolFailure("There are no facts to keep.")
    texts = [f["text"].strip() for f in facts if isinstance(f, dict) and isinstance(f.get("text"), str)]
    texts = [t[:MAX_TEXT_CHARS] for t in texts if t]
    draft_id = args.get("draft_id")
    if draft_id is not None and not isinstance(draft_id, str):
        raise ToolFailure("That draft is not valid.")
    async with state.pool.connection() as conn:
        for text in texts:
            await conn.execute(
                "insert into demo_facts (id, workspace, text, draft_id, idem_key) values (%s, %s, %s, %s, %s)"
                " on conflict (workspace, idem_key, text) do nothing",
                (new_id(), session.workspace, text, draft_id, key),
            )
    return {}


async def _charge(_state, _session: Session, _args: dict, _key: str) -> dict:
    return {}


HANDLERS = {
    "assistant_context": _context,
    "save_draft": _save_draft,
    "save_material": _save_material,
    "assistant_charge": _charge,
}


def _rpc_error(rpc_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}})


def _result(rpc_id: Any, result: dict) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": result})


@router.post("/demo/mcp")
async def mcp(request: Request) -> Response:
    state = request.app.state
    pack = state.packs.get("demo")
    if pack is None:
        return JSONResponse({"error": "app"}, status_code=404)
    try:
        session = verify_session(bearer(request), pack)
    except AuthError as e:
        return JSONResponse({"error": e.reason}, status_code=401)
    except ApiError:  # no bearer header
        return JSONResponse({"error": "invalid"}, status_code=401)
    try:
        body = await request.json()
    except ValueError:
        return _rpc_error(None, -32700, "Parse error")
    if not isinstance(body, dict) or not isinstance(body.get("method"), str):
        return _rpc_error(None, -32600, "Invalid request")
    rpc_id, method = body.get("id"), body["method"]
    if "id" not in body:  # a notification: no answer
        return Response(status_code=202)
    if method == "initialize":
        return _result(
            rpc_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "shipnote-demo", "version": "1"},
            },
        )
    if method == "tools/list":
        return _result(rpc_id, {"tools": TOOLS})
    if method != "tools/call":
        return _rpc_error(rpc_id, -32601, "Method not found")
    params = body.get("params")
    name = params.get("name") if isinstance(params, dict) else None
    handler = HANDLERS.get(name) if isinstance(name, str) else None
    if handler is None:
        return _rpc_error(rpc_id, -32602, "Unknown tool")
    arguments = params.get("arguments")
    arguments = arguments if isinstance(arguments, dict) else {}
    key = request.headers.get("idempotency-key") or str(uuid.uuid4())
    try:
        out = await handler(state, session, arguments, key)
    except ToolFailure as e:
        return _result(rpc_id, {"isError": True, "content": [{"type": "text", "text": str(e)}]})
    log.info("demo tool=%s", name)
    return _result(rpc_id, {"content": [{"type": "text", "text": json.dumps(out)}]})
