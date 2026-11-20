import json

import httpx
import pytest
import respx

from assistant.auth import Session
from assistant.checks import Fact
from assistant.packs import AppPack, Models
from assistant.tools import AppContext, McpHttpTools, SavedDraft, ToolError

URL = "https://app.test/mcp"
TOKEN = "tok-secret-123"


def make_pack() -> AppPack:
    return AppPack(
        id="demo",
        name="Demo",
        mcp_url=URL,
        secret_env="X",
        allowed_origins=["https://app.test"],
        guide="",
        writes=[],
        tools={
            "context": "assistant_context",
            "save_draft": "save_draft",
            "save_material": "save_material",
            "charge": "assistant_charge",
        },
        models=Models(chat="chat/model", writer="writer/model", main="m", cheap="c", vision="v"),
        message_usd_cap=1.0,
    )


def make_tools(client: httpx.AsyncClient) -> McpHttpTools:
    from datetime import UTC, datetime

    session = Session(app="demo", workspace="w1", access="draft", token=TOKEN, expires_at=datetime.now(UTC))
    return McpHttpTools(make_pack(), session, client)


def ok(payload) -> httpx.Response:
    body = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": json.dumps(payload)}]}}
    return httpx.Response(200, json=body)


def tool_error(text: str) -> httpx.Response:
    body = {"jsonrpc": "2.0", "id": 1, "result": {"isError": True, "content": [{"type": "text", "text": text}]}}
    return httpx.Response(200, json=body)


@respx.mock
async def test_context_parses():
    route = respx.post(URL).mock(
        return_value=ok(
            {
                "voice": "plain",
                "topics": ["a"],
                "recent_posts": ["p1"],
                "credits_left": 12.5,
                "out_of_credits_message": None,
            }
        )
    )
    async with httpx.AsyncClient() as client:
        ctx = await make_tools(client).context()
    assert ctx == AppContext(
        voice="plain", topics=["a"], recent_posts=["p1"], credits_left=12.5, out_of_credits_message=None
    )
    assert ctx.language == "en" and ctx.patterns == []
    req = route.calls[0].request
    body = json.loads(req.content)
    assert body["method"] == "tools/call"
    assert body["params"] == {"name": "assistant_context", "arguments": {}}
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert "idempotency-key" not in req.headers


@respx.mock
async def test_save_draft_sends_idempotency_key_and_media():
    route = respx.post(URL).mock(return_value=ok({"id": "d1", "edit_url": "https://app.test/d/d1"}))
    media = [{"file_id": "f1"}]
    async with httpx.AsyncClient() as client:
        saved = await make_tools(client).save_draft("hello", [], media, "key-1")
    assert saved == SavedDraft(id="d1", edit_url="https://app.test/d/d1")
    req = route.calls[0].request
    assert req.headers["idempotency-key"] == "key-1"
    assert json.loads(req.content)["params"] == {
        "name": "save_draft",
        "arguments": {"text": "hello", "media": media},
    }


@respx.mock
async def test_save_draft_sends_the_rest_of_a_thread_as_thread():
    route = respx.post(URL).mock(return_value=ok({"id": "d1", "edit_url": None}))
    async with httpx.AsyncClient() as client:
        await make_tools(client).save_draft("one", ["two", "three"], [], "key-2")
    args = json.loads(route.calls[0].request.content)["params"]["arguments"]
    assert args == {"text": "one", "thread": ["two", "three"], "media": []}


@respx.mock
async def test_save_material_and_charge():
    route = respx.post(URL).mock(return_value=ok({"ok": True}))
    fact = Fact(id="f1", text="grew 3x", numbers=[3])
    async with httpx.AsyncClient() as client:
        tools = make_tools(client)
        assert await tools.save_material([fact], "d1", "k-m") is None
        assert await tools.charge(0.02, "msg-1") is None
    m, c = route.calls[0].request, route.calls[1].request
    assert json.loads(m.content)["params"]["arguments"] == {"facts": [fact.model_dump()], "draft_id": "d1"}
    assert m.headers["idempotency-key"] == "k-m"
    assert json.loads(c.content)["params"] == {
        "name": "assistant_charge",
        "arguments": {"cost_usd": 0.02, "ref": "msg-1"},
    }
    assert c.headers["idempotency-key"] == "msg-1"
    assert json.loads(m.content)["id"] < json.loads(c.content)["id"]


@respx.mock
async def test_402_or_error_code_out_of_credits_maps_kind():
    respx.post(URL).mock(
        side_effect=[
            httpx.Response(402, json={"message": "Top up to continue"}),
            tool_error("out_of_credits: You have no credits left"),
            httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 3, "error": {"code": -32000, "message": "out_of_credits"}}
            ),
        ]
    )
    async with httpx.AsyncClient() as client:
        tools = make_tools(client)
        with pytest.raises(ToolError) as e1:
            await tools.charge(1, "r1")
        with pytest.raises(ToolError) as e2:
            await tools.charge(1, "r2")
        with pytest.raises(ToolError) as e3:
            await tools.charge(1, "r3")
    assert (e1.value.kind, e1.value.user_message) == ("out_of_credits", "Top up to continue")
    assert (e2.value.kind, e2.value.user_message) == ("out_of_credits", "You have no credits left")
    assert (e3.value.kind, e3.value.user_message) == ("out_of_credits", None)


@respx.mock
async def test_401_is_unauthorized():
    respx.post(URL).mock(side_effect=[httpx.Response(401), httpx.Response(403, json={"error": "nope"})])
    async with httpx.AsyncClient() as client:
        tools = make_tools(client)
        with pytest.raises(ToolError) as e1:
            await tools.context()
        with pytest.raises(ToolError) as e2:
            await tools.context()
    assert (e1.value.kind, e1.value.user_message) == ("unauthorized", None)
    assert (e2.value.kind, e2.value.user_message) == ("unauthorized", "nope")
    assert TOKEN not in str(e1.value) + str(e2.value)


@respx.mock
async def test_5xx_is_unavailable_after_one_retry():
    route = respx.post(URL).mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as client:
        with pytest.raises(ToolError) as e:
            await make_tools(client).save_draft("t", [], [], "key-9")
    assert e.value.kind == "unavailable"
    assert route.call_count == 2
    assert {c.request.headers["idempotency-key"] for c in route.calls} == {"key-9"}


@respx.mock
async def test_retry_recovers_after_one_transport_error():
    route = respx.post(URL).mock(side_effect=[httpx.ConnectError("x"), ok({"id": "d2", "edit_url": None})])
    async with httpx.AsyncClient() as client:
        assert (await make_tools(client).save_draft("t", [], [], "k")).id == "d2"
    assert route.call_count == 2


@respx.mock
async def test_timeouts_are_unavailable_after_one_retry():
    route = respx.post(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    async with httpx.AsyncClient() as client:
        with pytest.raises(ToolError) as e:
            await make_tools(client).context()
    assert e.value.kind == "unavailable" and route.call_count == 2


@respx.mock
async def test_429_is_unavailable_without_retry_and_keeps_message():
    route = respx.post(URL).mock(return_value=httpx.Response(429, json={"message": "Slow down, try in a minute."}))
    async with httpx.AsyncClient() as client:
        with pytest.raises(ToolError) as e:
            await make_tools(client).context()
    assert (e.value.kind, e.value.user_message) == ("unavailable", "Slow down, try in a minute.")
    assert route.call_count == 1


@respx.mock
async def test_real_sentences_survive_and_codes_are_stripped_on_every_path():
    sentence = "You're out of credits. Top up in Billing."
    jsonrpc = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": sentence}}
    respx.post(URL).mock(
        side_effect=[
            httpx.Response(402, json={"error": sentence}),
            httpx.Response(402, json={"error": "out_of_credits"}),
            httpx.Response(402, json={"message": "out_of_credits: " + sentence}),
            httpx.Response(200, json=jsonrpc),
            tool_error(sentence),
            httpx.Response(403, json={"message": "token_expired"}),
        ]
    )
    expected = [sentence, None, sentence, sentence, sentence, None]
    async with httpx.AsyncClient() as client:
        tools = make_tools(client)
        for want in expected:
            with pytest.raises(ToolError) as e:
                await tools.context()
            assert e.value.user_message == want


@respx.mock
async def test_other_errors_are_refused():
    respx.post(URL).mock(
        side_effect=[
            tool_error("invalid_input: text is empty"),
            httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "error": {"code": -32602, "message": "bad args"}}),
            httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "no"}]}}
            ),
        ]
    )
    async with httpx.AsyncClient() as client:
        tools = make_tools(client)
        with pytest.raises(ToolError) as e1:
            await tools.save_draft("", [], [], "k")
        with pytest.raises(ToolError) as e2:
            await tools.context()
        with pytest.raises(ToolError) as e3:
            await tools.context()
    assert (e1.value.kind, e1.value.user_message) == ("refused", "text is empty")
    assert (e2.value.kind, e2.value.user_message) == ("refused", "bad args")
    assert e3.value.kind == "refused"


@respx.mock
async def test_calls_to_the_app_are_timed_by_the_apps_tool_name():
    from assistant.timing import TurnTimer

    respx.post(URL).mock(return_value=ok({"id": "d1", "edit_url": None}))
    async with httpx.AsyncClient() as client:
        tools = make_tools(client)
        tools.timer = TurnTimer()
        await tools.save_draft("hello", [], [], "k1")
        await tools.charge(0.01, "m:1")
    assert [s["name"] for s in tools.timer.summary()["tool"]] == ["save_draft", "assistant_charge"]
