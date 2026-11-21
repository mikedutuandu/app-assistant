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
MAX_TOOLS = 100  # an app's tools the bot keeps (as many as a pack may name)
# The model is shown at most this many of an app's tools at once, in the app's order, with `find_app_tools` to load
# the rest by what the user asks for: every tool's description costs tokens on every call, so an app can offer all it
# does without each message paying for all of it (21 Nov 2026).
UP_FRONT_TOOLS = 20
FIND_TOOL = "find_app_tools"
FOUND_MAX = 8  # tools one search loads
LOADED_KEEP = 16  # tools found earlier in a chat, kept offered (the most recent)
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
    text: str  # the result's text content, fitted to RESULT_CHARS (fit_answer)
    is_error: bool
    raw: dict | None = None  # the result as the app sent it (content, structuredContent), for a card; None if too big


class AppActions(Protocol):
    async def list_tools(self) -> list[AppTool]: ...

    async def call(self, name: str, arguments: dict, key: str | None = None) -> ActionResult: ...

    async def view(self, uri: str) -> AppView | None: ...


def offered(tools: list[AppTool], pack: AppPack, reserved: set[str]) -> list[AppTool]:
    """The app's tools the model is offered: those the pack allows (all, when it names none), never one that has the
    name of one of the bot's own tools, at most MAX_TOOLS: in the pack's order when it names them (most used first:
    the model sees the first UP_FRONT_TOOLS at once), else in the server's."""
    allowed = set(pack.actions.tools) if pack.actions and pack.actions.tools is not None else None
    out: list[AppTool] = []
    for tool in tools:
        if allowed is not None and tool.name not in allowed:
            continue
        if tool.name in reserved:
            log.warning("app %s: tool %r has the name of one of the bot's own, left out", pack.id, tool.name)
            continue
        out.append(tool)
    if pack.actions and pack.actions.tools is not None:
        order = {name: i for i, name in enumerate(pack.actions.tools)}
        out.sort(key=lambda t: order[t.name])
    return out[:MAX_TOOLS]


_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"a", "an", "the", "my", "of", "to", "for", "and", "or", "in", "on", "it", "its", "this", "that", "me", "i"}


def _words(text: str) -> set[str]:
    out = set()
    for w in _WORD.findall(text.lower()):
        if w in _STOP:
            continue
        out.add(w)
        if len(w) > 3 and w.endswith("s"):
            out.add(w[:-1])  # keywords -> keyword
    return out


def find_tools(tools: list[AppTool], query: str, offered_now: set[str]) -> list[AppTool]:
    """The app's tools that best match what the user wants, not offered already, best first: a word of the query in
    the tool's name counts 3, in its title 2, in its description 1. Ties keep the app's order."""
    wanted = _words(query)
    if not wanted:
        return []
    scored = []
    for i, tool in enumerate(tools):
        if tool.name in offered_now:
            continue
        name, title, about = _words(tool.name.replace("_", " ")), _words(tool.title), _words(tool.description)
        score = sum(3 * (w in name) + 2 * (w in title) + (w in about) for w in wanted)
        if score:
            scored.append((-score, i, tool))
    return [tool for _, _, tool in sorted(scored)[:FOUND_MAX]]


def find_spec(more: int) -> dict:
    """`find_app_tools`, as the model is told about it, when the app has `more` tools than it was shown."""
    return {
        "type": "function",
        "function": {
            "name": FIND_TOOL,
            "description": (
                f"The app has {more} more tools than the ones listed here. When the user wants something none of "
                "these does, search them by what the user wants, in a few words (like 'delete an article' or "
                "'search console'). The tools found can be called from your next step."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "What the user wants to do or see."}},
                "required": ["query"],
            },
        },
    }


def found_text(tools: list[AppTool]) -> str:
    """What a search found, for the model."""
    if not tools:
        return "No tool matched: the app may have no tool for that here. Say so, or try other words once."
    lines = [
        f"- {t.name} ({'reads' if t.read_only else 'changes something: runs only after the user taps OK'}): "
        f"{t.title}. {t.description}"[:300]
        for t in tools
    ]
    return "Found, and you can call them now:\n" + "\n".join(lines)


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


# ---- a long answer, fitted to what the model reads ----
#
# An app's answer is often JSON, and a list of records can run far past RESULT_CHARS (Ask SiteSeed, 21 Nov 2026: three
# sites with their pages read were ~180,000 characters; cut at 12,000 the model never saw an id). Cutting the text
# breaks the JSON and drops whatever comes last. Instead: long texts are shortened first, then long lists keep their
# first items (the deepest first, so a list of sites keeps every site and trims each one's pages), and a note says what
# was left out and how to see more.

TEXT_STEPS = (2000, 500, 200, 80)  # long texts are shortened to these, in turn, until the answer fits
NOTE_ROOM = 700  # kept free for the note
SMALL_LIST_SHARE = 0.05  # a list under this share of the answer is not worth trimming
CUT_NOTE = "\n(The answer was cut here: it was too long. Ask for less, or for one item.)"


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False))


def _shorten_texts(value: Any, cap: int) -> tuple[Any, bool]:
    if isinstance(value, str):
        return (value[: cap - 1] + "…", True) if len(value) > cap else (value, False)
    if isinstance(value, list):
        pairs = [_shorten_texts(v, cap) for v in value]
        return [v for v, _ in pairs], any(c for _, c in pairs)
    if isinstance(value, dict):
        pairs = {k: _shorten_texts(v, cap) for k, v in value.items()}
        return {k: v for k, (v, _) in pairs.items()}, any(c for _, c in pairs.values())
    return value, False


def _lists_in(value: Any, path: str = "", depth: int = 0) -> list[tuple[int, int, str, list]]:
    """Every list in the answer with more than one item: (depth, size, path, the list)."""
    found: list[tuple[int, int, str, list]] = []
    if isinstance(value, list):
        if len(value) > 1:
            found.append((depth, _size(value), path or "the list", value))
        for item in value:
            found += _lists_in(item, f"{path}[]", depth + 1)
    elif isinstance(value, dict):
        for key, item in value.items():
            found += _lists_in(item, f"{path}.{key}" if path else str(key), depth + 1)
    return found


def fit_answer(text: str, limit: int = RESULT_CHARS) -> str:
    """`text` within `limit` characters, for the model. JSON stays JSON, with a note of what was left out; anything
    else is cut with a note."""
    if len(text) <= limit:
        return text
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    if not isinstance(value, (dict, list)):
        return text[:limit] + CUT_NOTE
    room = limit - NOTE_ROOM
    shortened = 0
    for cap in TEXT_STEPS:
        if _size(value) <= room:
            break
        value, changed = _shorten_texts(value, cap)
        shortened = cap if changed else shortened
    totals: dict[str, int] = {}  # a trimmed list's path: how many it had
    while _size(value) > room:
        whole = _size(value)
        lists = [x for x in _lists_in(value) if x[1] >= whole * SMALL_LIST_SHARE] or _lists_in(value)
        if not lists:
            break
        _depth, _list_size, path, items = max(lists, key=lambda x: (x[0], x[1]))
        totals[path] = max(totals.get(path, 0), len(items))
        del items[max(1, len(items) // 2) :]
    out = json.dumps(value, ensure_ascii=False)
    if len(out) > room:
        return out[:limit] + CUT_NOTE
    told = []
    if shortened:
        told.append(f"long texts are shortened to {shortened} characters")
    for path, total in totals.items():
        shown = max((len(x[3]) for x in _lists_in(value) if x[2] == path), default=1)
        told.append(f"{path}: showing {shown} of {total}")
    return (
        out
        + "\n(The answer was too long to read whole, so it was shortened: "
        + "; ".join(told)[: NOTE_ROOM - 200]
        + ". To see more, ask again with a limit or skip, a filter, or for one item.)"
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
    text = fit_answer(text)
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
