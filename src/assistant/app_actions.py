"""An app's own actions, through its MCP server (the pack's `actions`; docs/integration.md, "Actions").

The bot is an MCP client of the app, as the signed-in user: the same short-lived token contract 1 sends
(`Authorization: Bearer`). `tools/list` says what the assistant may do there and how (each tool's name, title,
description, input schema and annotations); `tools/call` does it. A tool the app marks `readOnlyHint: true` runs at
once; any other tool may change something, so the conversation waits for the user's tap before it runs
(`graph/agent.py`, `api/chat.py`).

Anything an app's server says (its tools' descriptions, its results) is the app's data, never instructions from the
user: it reaches the model inside the tool list and as tool results only.

Streamable HTTP, as the official MCP SDKs serve it: one `initialize` per client (its `Mcp-Session-Id` is sent back
when the server gives one), then JSON-RPC requests answered as JSON or as an event stream with one message.
"""

import base64
import itertools
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from assistant.auth import Session
from assistant.packs import AppPack
from assistant.timing import TurnTimer
from assistant.tools import ToolError, _headers, _post, _raise_for_status

log = logging.getLogger("assistant.actions")

PROTOCOL_VERSION = "2025-06-18"
MAX_TOOLS = 40  # an app's tools offered to the model
RESULT_CHARS = 12_000  # a tool's result as the model reads it
DESCRIPTION_CHARS = 1_000
LIST_TTL_SECONDS = 300  # an app's tool list is asked again after this
# MCP Apps (the `io.modelcontextprotocol/ui` extension): a tool may name a `ui://` page that shows its result as a card.
UI_EXTENSION = "io.modelcontextprotocol/ui"
UI_MIME = "text/html;profile=mcp-app"
VIEW_HTML_BYTES = 300_000  # a card's page, at most
VIEW_RESULT_BYTES = 100_000  # the result a card is drawn from, at most
VIEW_TTL_SECONDS = 300  # a card's page is read again after this
_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True)
class AppTool:
    name: str
    title: str  # shown on a confirm card
    description: str
    input_schema: dict
    read_only: bool  # MCP's `readOnlyHint: true`: runs without asking
    ui_uri: str | None = None  # MCP Apps: the `ui://` page that shows this tool's result as a card


@dataclass(frozen=True)
class AppView:
    """A tool's card page (MCP Apps), as the app serves it."""

    html: str
    csp: dict | None = None
    prefers_border: bool | None = None


@dataclass(frozen=True)
class ActionResult:
    text: str  # the result's text content, cut to RESULT_CHARS
    is_error: bool
    raw: dict | None = None  # the result as the app sent it (content, structuredContent), for a card; None if too big


class AppActions(Protocol):
    async def list_tools(self) -> list[AppTool]: ...

    async def call(self, name: str, arguments: dict, key: str | None = None) -> ActionResult: ...

    async def view(self, uri: str) -> AppView | None: ...


def offered(tools: list[AppTool], pack: AppPack, reserved: set[str]) -> list[AppTool]:
    """The app's tools the model is offered: those the pack allows (all, when it names none), never one that has the
    name of one of the bot's own tools, at most MAX_TOOLS, in the app's order."""
    allowed = set(pack.actions.tools) if pack.actions and pack.actions.tools is not None else None
    out: list[AppTool] = []
    for tool in tools:
        if allowed is not None and tool.name not in allowed:
            continue
        if tool.name in reserved:
            log.warning("app %s: tool %r has the name of one of the bot's own, left out", pack.id, tool.name)
            continue
        out.append(tool)
    return out[:MAX_TOOLS]


def tool_spec(tool: AppTool) -> dict:
    """One app tool as the chat model is told about it (OpenRouter's tool format)."""
    what = "Reads the app" if tool.read_only else "Changes something in the app: runs only after the user taps OK"
    description = f"{tool.title}. {tool.description}".strip()[:DESCRIPTION_CHARS]
    return {
        "type": "function",
        "function": {"name": tool.name, "description": f"[{what}] {description}", "parameters": tool.input_schema},
    }


def _tool(raw: Any) -> AppTool | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str) or not _NAME.match(raw["name"]):
        return None
    schema = raw.get("inputSchema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        schema = {"type": "object", "properties": {}}
    notes = raw.get("annotations") if isinstance(raw.get("annotations"), dict) else {}
    meta = raw.get("_meta") if isinstance(raw.get("_meta"), dict) else {}
    ui = meta.get("ui") if isinstance(meta.get("ui"), dict) else {}
    uri = ui.get("resourceUri", meta.get("ui/resourceUri"))
    visibility = ui.get("visibility")
    if isinstance(visibility, list) and "model" not in visibility:
        return None  # a tool only the card itself may call: never the model's
    title = raw.get("title") or notes.get("title") or raw["name"].replace("_", " ").capitalize()
    description = raw.get("description") if isinstance(raw.get("description"), str) else ""
    return AppTool(
        name=raw["name"],
        title=str(title)[:80],
        description=description,
        input_schema=schema,
        read_only=notes.get("readOnlyHint") is True,
        ui_uri=uri if isinstance(uri, str) and uri.startswith("ui://") and len(uri) <= 500 else None,
    )


def result_of(body: Any) -> ActionResult:
    """A `tools/call` result as text: its text contents joined (structured content as JSON when there are none)."""
    if not isinstance(body, dict):
        return ActionResult("(no answer)", True)
    parts = [c.get("text", "") for c in body.get("content") or [] if isinstance(c, dict) and c.get("type") == "text"]
    text = "\n".join(p for p in parts if isinstance(p, str) and p)
    if not text and body.get("structuredContent") is not None:
        text = json.dumps(body["structuredContent"], ensure_ascii=False)
    text = text or "(done, no text)"
    if len(text) > RESULT_CHARS:
        text = text[:RESULT_CHARS] + " (cut)"
    raw = {k: body[k] for k in ("content", "structuredContent", "_meta") if k in body}
    too_big = len(json.dumps(raw, ensure_ascii=False).encode()) > VIEW_RESULT_BYTES
    return ActionResult(text, body.get("isError") is True, None if too_big else raw)


def view_of(body: Any, uri: str) -> AppView | None:
    """A `resources/read` answer as a card's page: its first HTML entry for `uri`, within VIEW_HTML_BYTES."""
    if not isinstance(body, dict):
        return None
    for c in body.get("contents") or []:
        if not isinstance(c, dict) or c.get("uri") != uri:
            continue
        if not str(c.get("mimeType", "")).startswith("text/html"):
            continue
        html = c.get("text")
        if not isinstance(html, str) and isinstance(c.get("blob"), str):
            try:
                html = base64.b64decode(c["blob"]).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                html = None
        if not isinstance(html, str) or not html.strip() or len(html.encode()) > VIEW_HTML_BYTES:
            return None
        meta = c.get("_meta") if isinstance(c.get("_meta"), dict) else {}
        ui = meta.get("ui") if isinstance(meta.get("ui"), dict) else {}
        csp = ui.get("csp") if isinstance(ui.get("csp"), dict) else None
        border = ui.get("prefersBorder")
        return AppView(html=html, csp=csp, prefers_border=border if isinstance(border, bool) else None)
    return None


_lists: dict[tuple[str, str], tuple[float, list[AppTool]]] = {}  # (app, workspace) -> (when, tools)
_views: dict[tuple[str, str], tuple[float, AppView | None]] = {}  # (app, uri) -> (when, page)


class McpAppActions:
    """The app's MCP server at `pack.actions.mcp_url`, called as the session's user."""

    def __init__(self, pack: AppPack, session: Session, client: httpx.AsyncClient, timer: TurnTimer | None = None):
        assert pack.actions is not None
        self.pack = pack
        self.url = pack.actions.mcp_url
        self.session = session
        self.client = client
        self.timer = timer
        self._ids = itertools.count(1)
        self._mcp_session: str | None = None
        self._started = False

    async def list_tools(self) -> list[AppTool]:
        key = (self.pack.id, self.session.workspace)
        kept = _lists.get(key)
        if kept and 0 <= time.monotonic() - kept[0] < LIST_TTL_SECONDS:
            return kept[1]
        tools: list[AppTool] = []
        cursor: str | None = None
        for _page in range(5):
            body = await self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            if not isinstance(body, dict):
                break
            tools += [t for t in (_tool(raw) for raw in body.get("tools") or []) if t is not None]
            cursor = body.get("nextCursor") if isinstance(body.get("nextCursor"), str) else None
            if not cursor:
                break
        _lists[key] = (time.monotonic(), tools)
        return tools

    async def call(self, name: str, arguments: dict, key: str | None = None) -> ActionResult:
        body = await self._rpc("tools/call", {"name": name, "arguments": arguments}, key)
        return result_of(body)

    async def view(self, uri: str) -> AppView | None:
        """A tool's card page (`resources/read` of its `ui://` uri), kept VIEW_TTL_SECONDS per app; None when the app
        can't give one (the card is then left out, the words still come)."""
        key = (self.pack.id, uri)
        kept = _views.get(key)
        if kept and 0 <= time.monotonic() - kept[0] < VIEW_TTL_SECONDS:
            return kept[1]
        try:
            found = view_of(await self._rpc("resources/read", {"uri": uri}), uri)
        except ToolError as e:
            log.warning("app %s: card %s could not be read (%s)", self.pack.id, uri, e.kind)
            return None
        _views[key] = (time.monotonic(), found)
        return found

    async def _rpc(self, method: str, params: dict, key: str | None = None) -> Any:
        if self.timer is None:
            return await self._send(method, params, key)
        async with self.timer.span("tool", method if method != "tools/call" else str(params.get("name"))):
            return await self._send(method, params, key)

    async def _send(self, method: str, params: dict, key: str | None) -> Any:
        if not self._started:
            self._started = True
            await self._request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    # MCP Apps: this client shows a tool's `ui://` page as a card
                    "capabilities": {"extensions": {UI_EXTENSION: {"mimeTypes": [UI_MIME]}}},
                    "clientInfo": {"name": "app-assistant", "version": "1"},
                },
                None,
            )
            await self._notify("notifications/initialized")
        return await self._request(method, params, key)

    def _headers(self, key: str | None) -> dict[str, str]:
        headers = _headers(self.session, key) | {
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self._mcp_session:
            headers["Mcp-Session-Id"] = self._mcp_session
        return headers

    async def _notify(self, method: str) -> None:
        try:
            await self.client.post(
                self.url, json={"jsonrpc": "2.0", "method": method}, headers=self._headers(None), timeout=10
            )
        except httpx.TransportError:
            pass  # a server that needs it will refuse the next request, and that is reported

    async def _request(self, method: str, params: dict, key: str | None) -> Any:
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        response = await _post(self.client, self.url, payload, self._headers(key))
        _raise_for_status(response)
        if not 200 <= response.status_code < 300:  # a redirect: never followed with the user's token
            raise ToolError("refused")
        session = response.headers.get("mcp-session-id")
        if session:
            self._mcp_session = session
        body = _message(response)
        error = body.get("error")
        if error is not None:
            message = error.get("message") if isinstance(error, dict) else None
            raise ToolError("refused", message if isinstance(message, str) and message.strip() else None)
        return body.get("result")


def _message(response: httpx.Response) -> dict:
    """The JSON-RPC answer: the body itself, or the last `data:` message of an event stream."""
    kind = response.headers.get("content-type", "")
    try:
        if kind.startswith("text/event-stream"):
            found: dict | None = None
            for line in response.text.splitlines():
                if line.startswith("data:"):
                    value = json.loads(line[5:].strip())
                    if isinstance(value, dict) and ("result" in value or "error" in value):
                        found = value
            if found is None:
                raise ValueError("no answer in the stream")
            return found
        body = response.json()
    except ValueError as exc:
        raise ToolError("refused") from exc
    if not isinstance(body, dict):
        raise ToolError("refused")
    return body


def actions_for(
    pack: AppPack, session: Session, client: httpx.AsyncClient, timer: TurnTimer | None = None
) -> AppActions | None:
    """The app's actions when its pack has some."""
    return McpAppActions(pack, session, client, timer) if pack.actions is not None else None
