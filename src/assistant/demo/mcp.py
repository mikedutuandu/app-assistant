"""The demo app's own MCP server (`POST /demo/mcp`), so the public demo shows the assistant using an app's actions
(docs/integration.md, "Actions"): what any app's server does, kept tiny. Stateless JSON over Streamable HTTP, checked
with the user's token as the four endpoints are.

Tools: `list_entries` and `count_entries` read the entries the user saved in this demo; `delete_entry` removes one,
so the assistant runs it only after the user taps Confirm in the chat.
"""

import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from assistant.api.deps import ApiError, bearer
from assistant.auth import AuthError, Session, verify_session

log = logging.getLogger("assistant.demo")
router = APIRouter()

PROTOCOL_VERSION = "2025-06-18"
LISTED = 20

TOOLS = [
    {
        "name": "list_entries",
        "title": "List saved entries",
        "description": "The entries the user saved in Shipnote, newest first: id, text and when.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "count_entries",
        "title": "Count saved entries",
        "description": "How many entries the user has saved in Shipnote.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "delete_entry",
        "title": "Delete an entry",
        "description": "Deletes one saved entry by its id (from list_entries).",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
]


def _text(text: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": error}


async def _call(state: Any, session: Session, name: str, arguments: dict) -> dict:
    async with state.pool.connection() as conn:
        if name == "list_entries":
            cur = await conn.execute(
                "select id, text, created_at from demo_drafts where workspace = %s order by created_at desc limit %s",
                (session.workspace, LISTED),
            )
            rows = [{"id": r[0], "text": r[1], "saved_at": r[2].isoformat()} for r in await cur.fetchall()]
            return _text(json.dumps(rows, ensure_ascii=False) if rows else "No entries saved yet.")
        if name == "count_entries":
            cur = await conn.execute("select count(*) from demo_drafts where workspace = %s", (session.workspace,))
            count = (await cur.fetchone())[0]
            return _text(f"{count} saved {'entry' if count == 1 else 'entries'}.")
        if name == "delete_entry":
            entry = arguments.get("id")
            if not isinstance(entry, str) or not entry:
                return _text("Give the id of an entry from list_entries.", error=True)
            cur = await conn.execute(
                "delete from demo_drafts where id = %s and workspace = %s returning id", (entry, session.workspace)
            )
            gone = await cur.fetchone()
            return _text("Deleted." if gone else "No such entry.", error=gone is None)
    raise KeyError(name)


@router.post("/demo/mcp")
async def demo_mcp(request: Request) -> Response:
    state = request.app.state
    pack = await state.packs.get("demo")
    if pack is None:
        return JSONResponse({"error": "app"}, status_code=404)
    try:
        session = verify_session(bearer(request), pack)
    except (AuthError, ApiError):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        message = await request.json()
    except ValueError:
        message = None
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request."}})
    if "id" not in message:  # a notification
        return Response(status_code=202)
    rid, method, params = message["id"], message.get("method"), message.get("params") or {}
    if method == "initialize":
        result: dict = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "shipnote-demo", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call" and isinstance(params, dict):
        name, arguments = params.get("name"), params.get("arguments") or {}
        if name not in {t["name"] for t in TOOLS} or not isinstance(arguments, dict):
            return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "Unknown tool."}})
        result = await _call(state, session, name, arguments)
        log.info("demo mcp tool=%s", name)
    else:
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Unknown method."}})
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": result})
