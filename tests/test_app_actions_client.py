"""The bot as an MCP client of the app (`app_actions.McpAppActions`), against a fake Streamable HTTP server."""

import json
from datetime import UTC, datetime

import httpx
import pytest
from graph_harness import make_pack

from assistant import app_actions
from assistant.app_actions import RESULT_CHARS, AppTool, McpAppActions, offered
from assistant.auth import Session
from assistant.packs import Actions
from assistant.tools import ToolError

URL = "https://app.example/api/mcp"
TOOLS = [
    {
        "name": "get_stats",
        "title": "Get stats",
        "description": "Numbers.",
        "inputSchema": {"type": "object"},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "move_item",
        "description": "Moves it.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"destructiveHint": False},
    },
    {"name": "bad name!", "description": "x"},
    {"name": "plain", "description": "no schema"},
]


@pytest.fixture(autouse=True)
def fresh_cache():
    app_actions._lists.clear()
    app_actions._views.clear()


def pack(tools=None):
    return make_pack().model_copy(update={"actions": Actions(mcp_url=URL, tools=tools)})


def session():
    return Session(app="demo", workspace="ws1", access="draft", token="tok", expires_at=datetime.now(UTC))


class Server:
    def __init__(self, stream: bool = False, session_id: str | None = "s-1"):
        self.seen: list[httpx.Request] = []
        self.stream = stream
        self.session_id = session_id
        self.answers: dict[str, dict] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        body = json.loads(request.content)
        if "id" not in body:
            return httpx.Response(202)
        method = body["method"]
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "x"}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "resources/read":
            result = self.answers.get(body["params"]["uri"], {"contents": []})
        else:
            result = self.answers.get(body["params"]["name"], {"content": [{"type": "text", "text": "done"}]})
        message = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        headers = {"mcp-session-id": self.session_id} if self.session_id else {}
        if self.stream:
            return httpx.Response(
                200,
                text=f"event: message\ndata: {json.dumps(message)}\n\n",
                headers={**headers, "content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=message, headers=headers)


def client(server) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(server))


async def test_lists_tools_after_initialize_with_the_users_token_and_reads_the_annotations():
    server = Server()
    async with client(server) as c:
        tools = await McpAppActions(pack(), session(), c).list_tools()
    assert [t.name for t in tools] == ["get_stats", "move_item", "plain"]  # a malformed name is left out
    assert [t.read_only for t in tools] == [True, False, False]  # no annotation: it may change things
    assert tools[0].title == "Get stats" and tools[1].title == "Move item"
    assert tools[2].input_schema == {"type": "object", "properties": {}}
    methods = [json.loads(r.content).get("method") for r in server.seen]
    assert methods == ["initialize", "notifications/initialized", "tools/list"]
    assert all(r.headers["authorization"] == "Bearer tok" for r in server.seen)
    assert server.seen[-1].headers["mcp-session-id"] == "s-1"
    assert "text/event-stream" in server.seen[0].headers["accept"]


async def test_an_event_stream_answer_is_read():
    server = Server(stream=True, session_id=None)
    server.answers["get_stats"] = {"content": [{"type": "text", "text": "1,200 views"}]}
    async with client(server) as c:
        result = await McpAppActions(pack(), session(), c).call("get_stats", {})
    assert (result.text, result.is_error) == ("1,200 views", False)
    assert "mcp-session-id" not in server.seen[-1].headers


async def test_a_change_carries_its_idempotency_key():
    server = Server()
    async with client(server) as c:
        await McpAppActions(pack(), session(), c).call("move_item", {"id": "a"}, key="confirm-1")
    assert server.seen[-1].headers["idempotency-key"] == "confirm-1"
    assert json.loads(server.seen[-1].content)["params"] == {"name": "move_item", "arguments": {"id": "a"}}


async def test_results_are_text_cut_long_and_error_results_marked():
    server = Server()
    server.answers["get_stats"] = {"content": [{"type": "text", "text": "x" * (RESULT_CHARS + 50)}]}
    server.answers["move_item"] = {"content": [{"type": "text", "text": "No such item."}], "isError": True}
    server.answers["plain"] = {"content": [], "structuredContent": {"ok": 1}}
    async with client(server) as c:
        actions = McpAppActions(pack(), session(), c)
        long = await actions.call("get_stats", {})
        bad = await actions.call("move_item", {})
        structured = await actions.call("plain", {})
    # Not JSON: cut, and the model is told (a JSON answer is shortened instead: test_fit_answer.py).
    assert long.text.startswith("x" * RESULT_CHARS) and long.text.endswith("Ask for less, or for one item.)")
    assert (bad.text, bad.is_error) == ("No such item.", True)
    assert structured.text == '{"ok": 1}'


async def test_a_json_rpc_error_and_an_http_refusal_are_tool_errors():
    def refusing(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "tools/call":
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -1, "message": "Nope."}}
            )
        if "id" not in body:
            return httpx.Response(202)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(refusing)) as c:
        with pytest.raises(ToolError) as e:
            await McpAppActions(pack(), session(), c).call("x", {})
    assert (e.value.kind, e.value.user_message) == ("refused", "Nope.")

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401))) as c:
        with pytest.raises(ToolError) as e:
            await McpAppActions(pack(), session(), c).list_tools()
    assert e.value.kind == "unauthorized"


async def test_the_list_is_kept_for_a_few_minutes_per_app_and_workspace():
    server = Server()
    async with client(server) as c:
        await McpAppActions(pack(), session(), c).list_tools()
        await McpAppActions(pack(), session(), c).list_tools()
    assert sum(json.loads(r.content).get("method") == "tools/list" for r in server.seen) == 1


def test_offered_keeps_the_packs_choice_and_the_bots_names():
    from assistant.app_actions import AppTool

    tools = [AppTool(n, n, "", {"type": "object"}, True) for n in ("a", "b", "save_facts")]
    assert [t.name for t in offered(tools, pack(), {"save_facts"})] == ["a", "b"]
    assert [t.name for t in offered(tools, pack(tools=["b"]), {"save_facts"})] == ["b"]


# ---- MCP Apps: a tool's card (Mike, 21 Nov 2026: show the app's data as SuperX does, not only text) ----

CARD = "<!doctype html><html><body><div id=card></div></body></html>"


async def test_the_client_says_it_shows_cards_and_reads_each_tools_card():
    server = Server()
    TOOLS.append(
        {
            "name": "list_posts",
            "description": "Posts.",
            "inputSchema": {"type": "object"},
            "annotations": {"readOnlyHint": True},
            "_meta": {"ui": {"resourceUri": "ui://cards/posts"}},
        }
    )
    TOOLS.append(
        {"name": "card_only", "description": "x", "_meta": {"ui": {"resourceUri": "ui://x", "visibility": ["app"]}}}
    )
    try:
        tools = await McpAppActions(pack(), session(), client(server)).list_tools()
    finally:
        del TOOLS[-2:]
    init = json.loads(server.seen[0].content)
    assert init["params"]["capabilities"] == {
        "extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}
    }
    by_name = {t.name: t for t in tools}
    assert by_name["list_posts"].ui_uri == "ui://cards/posts"
    assert by_name["get_stats"].ui_uri is None
    assert "card_only" not in by_name  # only the card itself may call it, never the model


async def test_a_cards_page_is_read_once_and_kept():
    server = Server()
    meta = {"ui": {"csp": {"resourceDomains": ["https://pbs.example"]}, "prefersBorder": True}}
    server.answers["ui://cards/posts"] = {
        "contents": [{"uri": "ui://cards/posts", "mimeType": "text/html;profile=mcp-app", "text": CARD, "_meta": meta}]
    }
    actions = McpAppActions(pack(), session(), client(server))
    page = await actions.view("ui://cards/posts")
    assert page is not None and page.html == CARD and page.prefers_border is True
    assert page.csp == {"resourceDomains": ["https://pbs.example"]}
    assert await actions.view("ui://cards/posts") == page
    assert [json.loads(r.content).get("method") for r in server.seen].count("resources/read") == 1


async def test_a_card_that_isnt_html_or_is_too_big_is_left_out():
    server = Server()
    server.answers["ui://a"] = {"contents": [{"uri": "ui://a", "mimeType": "application/json", "text": "{}"}]}
    big = "x" * (app_actions.VIEW_HTML_BYTES + 1)
    server.answers["ui://b"] = {"contents": [{"uri": "ui://b", "mimeType": "text/html;profile=mcp-app", "text": big}]}
    actions = McpAppActions(pack(), session(), client(server))
    assert await actions.view("ui://a") is None
    assert await actions.view("ui://b") is None


async def test_a_result_keeps_what_the_card_is_drawn_from():
    server = Server()
    server.answers["get_stats"] = {
        "content": [{"type": "text", "text": "posts: 10"}],
        "structuredContent": {"posts": [{"text": "hi", "views": 79}]},
    }
    result = await McpAppActions(pack(), session(), client(server)).call("get_stats", {})
    assert result.raw == {
        "content": [{"type": "text", "text": "posts: 10"}],
        "structuredContent": {"posts": [{"text": "hi", "views": 79}]},
    }


def test_offered_follows_the_packs_order_when_it_names_tools():
    tools = [AppTool(n, n, "", {"type": "object"}, True) for n in ("a", "b", "c")]
    assert [t.name for t in offered(tools, pack(tools=["c", "a"]), set())] == ["c", "a"]
