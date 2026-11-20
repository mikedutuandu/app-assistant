"""The Shipnote demo: session cookie, its tiny MCP, idempotent drafts, the daily cleanup."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_api_chat import PNG, Api, api  # noqa: F401 - the shared fixture

from assistant.auth import verify_session
from assistant.demo.cleanup import cleanup_demo
from assistant.graph.build import thread_id

pytestmark = pytest.mark.db


async def session(api: Api, **headers) -> httpx.Response:
    return await api.client.post("/demo/api/assistant/session", headers=headers)


def rpc(method: str, params: dict | None = None, rpc_id: int | None = 1) -> dict:
    body: dict = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if rpc_id is not None:
        body["id"] = rpc_id
    return body


async def call(api: Api, token: str, tool: str, args: dict, key: str | None = None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} | ({"Idempotency-Key": key} if key else {})
    return await api.client.post(
        "/demo/mcp", json=rpc("tools/call", {"name": tool, "arguments": args}), headers=headers
    )


def text_of(r: httpx.Response):
    import json

    return json.loads(r.json()["result"]["content"][0]["text"])


async def test_demo_session_sets_cookie_and_token_verifies(api: Api):
    r = await session(api)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    demo_id = r.cookies["demo_id"]
    assert len(demo_id) == 32
    assert all(part in cookie for part in ("HttpOnly", "SameSite=lax", "Max-Age=86400", "Path=/"))
    assert "Secure" not in cookie
    s = verify_session(r.json()["token"], api.app.state.packs["demo"])
    assert s.workspace == f"demo:{demo_id}"
    again = await session(api)  # the cookie the client holds is kept
    assert verify_session(again.json()["token"], api.app.state.packs["demo"]).workspace == f"demo:{demo_id}"
    assert again.cookies["demo_id"] == demo_id
    https = await api.client.post("/demo/api/assistant/session", headers={"X-Forwarded-Proto": "https"})
    assert "Secure" in https.headers["set-cookie"]


async def test_demo_session_limited_to_10_an_hour_per_ip(api: Api):
    ip = {"X-Forwarded-For": "203.0.113.50"}
    api.client.cookies.clear()
    for _ in range(10):
        assert (await session(api, **ip)).status_code == 200
    r = await session(api, **ip)
    assert r.status_code == 429
    assert r.json() == {"error": "limit", "message": "Too many demo sessions from here. Try again in a bit."}
    assert (await session(api, **{"X-Forwarded-For": "203.0.113.51"})).status_code == 200


async def test_demo_mcp_rejects_bad_token(api: Api):
    for headers in ({}, {"Authorization": "Bearer nonsense"}):
        r = await api.client.post("/demo/mcp", json=rpc("tools/list"), headers=headers)
        assert r.status_code == 401
        assert r.json() == {"error": "invalid"}
    real = api.headers()["Authorization"]  # a token for another pack's id
    r = await api.client.post("/demo/mcp", json=rpc("tools/list"), headers={"Authorization": real})
    assert r.status_code == 401


async def test_demo_mcp_protocol(api: Api):
    token = (await session(api)).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}
    init = (await api.client.post("/demo/mcp", json=rpc("initialize"), headers=auth)).json()
    assert init["result"]["capabilities"] == {"tools": {}}
    tools = (await api.client.post("/demo/mcp", json=rpc("tools/list"), headers=auth)).json()["result"]["tools"]
    assert sorted(t["name"] for t in tools) == ["assistant_charge", "assistant_context", "save_draft", "save_material"]
    note = await api.client.post("/demo/mcp", json=rpc("notifications/initialized", rpc_id=None), headers=auth)
    assert note.status_code == 202 and note.content == b""
    assert text_of(await call(api, token, "assistant_charge", {"cost_usd": 0.01, "ref": "m"})) == {}
    unknown = await call(api, token, "delete_everything", {})
    assert unknown.json()["error"]["code"] == -32602


async def test_demo_save_draft_idempotent(api: Api):
    r = await session(api)
    token, demo_id = r.json()["token"], r.cookies["demo_id"]
    first = text_of(await call(api, token, "save_draft", {"text": "Auto-reply is live", "media": []}, key="k1"))
    again = text_of(await call(api, token, "save_draft", {"text": "Auto-reply is live", "media": []}, key="k1"))
    assert first == again and first["edit_url"] is None
    other = text_of(await call(api, token, "save_draft", {"text": "Second", "media": []}, key="k2"))
    assert other["id"] != first["id"]
    assert await api.count("demo_drafts", f"demo:{demo_id}") == 2

    ctx = text_of(await call(api, token, "assistant_context", {}))
    assert ctx["recent_posts"] == ["Second", "Auto-reply is live"]
    assert ctx["credits_left"] is None and ctx["voice"].startswith("friendly")

    api.client.cookies.set("demo_id", demo_id)
    listed = (await api.client.get("/demo/api/drafts")).json()
    assert [d["text"] for d in listed] == ["Second", "Auto-reply is live"]
    api.client.cookies.clear()
    assert (await api.client.get("/demo/api/drafts")).json() == []


async def test_demo_save_draft_copies_the_image_from_our_storage_only(api: Api):
    r = await session(api)
    token, demo_id = r.json()["token"], r.cookies["demo_id"]
    ws = f"demo:{demo_id}"
    api.storage.put("demo/src/shot.png", PNG, "image/png")
    url = api.storage.signed_url("demo/src/shot.png", 300)
    served = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        served["n"] += 1
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    api.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ok = text_of(await call(api, token, "save_draft", {"text": "t", "media": [{"url": url, "alt": ""}]}, key="i1"))
    assert api.storage.get(f"demo/{ws}/drafts/{ok['id']}") == PNG

    bad = await call(api, token, "save_draft", {"text": "t", "media": [{"url": "https://evil.example/x.png"}]}, "i2")
    assert bad.json()["result"]["isError"] is True
    assert served["n"] == 1  # the foreign URL was never fetched
    assert await api.count("demo_drafts", ws) == 1

    big = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * (10 * 1024 * 1024 + 1)))
    )
    api.app.state.http = big
    too_big = await call(api, token, "save_draft", {"text": "t", "media": [{"url": url}]}, key="i3")
    assert too_big.json()["result"]["isError"] is True


async def test_demo_save_material_idempotent(api: Api):
    r = await session(api)
    token, demo_id = r.json()["token"], r.cookies["demo_id"]
    args = {"facts": [{"id": "f1", "text": "Replies in 2 minutes"}, {"text": "Fixed 3 bugs"}], "draft_id": None}
    await call(api, token, "save_material", args, key="m1")
    await call(api, token, "save_material", args, key="m1")
    assert await api.count("demo_facts", f"demo:{demo_id}") == 2
    await call(api, token, "save_material", args, key="m2")
    assert await api.count("demo_facts", f"demo:{demo_id}") == 4


async def test_demo_workspace_holds_at_most_100_drafts(api: Api):
    r = await session(api)
    token, ws = r.json()["token"], f"demo:{r.cookies['demo_id']}"
    async with api.pool.connection() as conn:
        for i in range(99):
            await conn.execute(
                "insert into demo_drafts (id, workspace, text, idem_key) values (%s, %s, 'x', %s)",
                (f"d{i}", ws, f"k{i}"),
            )
    last = text_of(await call(api, token, "save_draft", {"text": "the 100th"}, key="n100"))
    full = await call(api, token, "save_draft", {"text": "one more"}, key="n101")
    assert full.json()["result"] == {
        "isError": True,
        "content": [{"type": "text", "text": "The demo is full for today."}],
    }
    assert text_of(await call(api, token, "save_draft", {"text": "the 100th"}, key="n100")) == last  # a repeat
    assert await api.count("demo_drafts", ws) == 100


async def test_demo_workspace_holds_at_most_300_facts(api: Api):
    r = await session(api)
    token, ws = r.json()["token"], f"demo:{r.cookies['demo_id']}"
    async with api.pool.connection() as conn:
        for i in range(298):
            await conn.execute(
                "insert into demo_facts (id, workspace, text, idem_key) values (%s, %s, %s, 'old')",
                (f"f{i}", ws, f"t{i}"),
            )
    two = {"facts": [{"text": "a"}, {"text": "b"}], "draft_id": None}
    assert "isError" not in (await call(api, token, "save_material", two, key="m1")).json()["result"]
    assert "isError" not in (await call(api, token, "save_material", two, key="m1")).json()["result"]  # a repeat
    full = await call(api, token, "save_material", {"facts": [{"text": "c"}], "draft_id": None}, key="m2")
    assert full.json()["result"]["isError"] is True
    assert full.json()["result"]["content"][0]["text"] == "The demo is full for today."
    assert await api.count("demo_facts", ws) == 300


async def test_demo_page_has_the_widget_tag(api: Api):
    r = await api.client.get("/demo")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert (
        '<script src="/widget.js" data-app="demo" data-session-url="/demo/api/assistant/session"'
        ' data-label="Write with me"></script>'
    ) in r.text
    assert "assistant:note" in r.text and "assistant:saved" in r.text


async def test_demo_cleanup_removes_day_old(api: Api):
    pool, storage = api.pool, api.storage
    old = datetime.now(UTC) - timedelta(days=2)
    ws = "demo:" + "a" * 32
    thread = await api.thread_row(workspace=ws, app="demo")
    keep_thread = await api.thread_row(workspace=ws, app="demo")
    storage.put(f"demo/{ws}/drafts/d_old", b"x", "image/png")
    storage.put(f"demo/{ws}/drafts/d_new", b"x", "image/png")
    storage.put("demo/f_old", b"x", "image/png")
    storage.put("demo/f_new", b"x", "image/png")
    storage.put("demo/f_thread", b"x", "image/png")
    storage.put("real/keep", b"x", "image/png")
    async with pool.connection() as conn:
        await conn.execute(
            "insert into demo_drafts (id, workspace, text, image_key, idem_key, created_at) values"
            " ('d_old', %s, 'old', %s, 'a', %s), ('d_new', %s, 'new', %s, 'b', now())",
            (ws, f"demo/{ws}/drafts/d_old", old, ws, f"demo/{ws}/drafts/d_new"),
        )
        await conn.execute(
            "insert into demo_facts (id, workspace, text, idem_key, created_at) values"
            " ('o', %s, 'old', 'a', %s), ('n', %s, 'new', 'b', now())",
            (ws, old, ws),
        )
        await conn.execute(
            "insert into files (id, app, workspace, thread_id, kind, mime, bytes, storage_key, expires_at) values"
            " ('f_old', 'demo', %s, %s, 'image', 'image/png', 1, 'demo/f_old', %s),"
            " ('f_new', 'demo', %s, %s, 'image', 'image/png', 1, 'demo/f_new', now() + interval '1 hour'),"
            " ('f_thread', 'demo', %s, %s, 'image', 'image/png', 1, 'demo/f_thread', now() + interval '1 hour')",
            (ws, keep_thread, old, ws, keep_thread, ws, thread),
        )
        await conn.execute("update threads set updated_at = %s where id = %s", (old, thread))
    deleted: list[str] = []
    original = api.checkpointer.adelete_thread

    async def spy(tid: str) -> None:
        deleted.append(tid)
        await original(tid)

    api.checkpointer.adelete_thread = spy
    api.app.state.checkpointer = api.checkpointer

    result = await cleanup_demo(api.app.state)
    assert result == {"drafts": 1, "facts": 1, "files": 2, "threads": 1}
    assert deleted == [thread_id("demo", ws, thread)]

    async with pool.connection() as conn:
        rows = lambda q: conn.execute(q)
        assert [r[0] for r in await (await rows("select id from demo_drafts")).fetchall()] == ["d_new"]
        assert [r[0] for r in await (await rows("select id from demo_facts")).fetchall()] == ["n"]
        assert [r[0] for r in await (await rows("select id from files")).fetchall()] == ["f_new"]
        assert [r[0] for r in await (await rows("select id from threads")).fetchall()] == [keep_thread]
    for gone in (f"demo/{ws}/drafts/d_old", "demo/f_old", "demo/f_thread"):
        with pytest.raises(Exception):  # noqa: B017 - botocore NoSuchKey
            storage.get(gone)
    for kept in (f"demo/{ws}/drafts/d_new", "demo/f_new", "real/keep"):
        assert storage.get(kept) == b"x"
    assert await cleanup_demo(api.app.state) == {"drafts": 0, "facts": 0, "files": 0, "threads": 0}
