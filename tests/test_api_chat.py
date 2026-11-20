"""The chat API end to end: FastAPI routes on a fresh Postgres schema, moto S3, FakeLlm and FakeTools."""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import httpx
import jwt
import pytest
from fakes import FakeLlm, FakeTools, Out, default_context
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from moto import mock_aws
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from starlette.requests import Request

from assistant.api import chat, turn
from assistant.api.deps import client_ip
from assistant.auth import mint_session, verify_session
from assistant.config import Settings
from assistant.db import migrate, new_id
from assistant.files.storage import Storage
from assistant.graph.build import serializer, thread_id
from assistant.limits import DEMO_MESSAGES_PER_IP_DAY, DEMO_USD_KEY, USER_MESSAGES_PER_HOUR, hit, remaining, user_key
from assistant.llm import LlmError
from assistant.main import create_app
from assistant.packs import AppPack, Models, WriteSpec
from assistant.tools import ToolError

pytestmark = pytest.mark.db

SECRET = "k" * 64
ORIGIN = "https://app.example"
PNG = (Path(__file__).parent / "fixtures" / "shot.png").read_bytes()
FACT = "Replies go out in 2 minutes instead of 2 hours"
LAUNCH = "Auto-reply now answers in 2 minutes instead of 2 hours."
CHANGELOG = "Auto-reply\n- Replies go out in 2 minutes, not 2 hours."
APP_DOWN = "The app couldn't be reached. Try again in a minute."
OUR_FAULT = "Something went wrong on our side. Nothing was saved."
OK = {"unsupported": []}


def make_pack(pack_id: str = "real", demo: bool = False) -> AppPack:
    return AppPack(
        id=pack_id,
        name="Test app",
        mcp_url="https://app.example/mcp",
        secret_env="TEST_CHAT_SECRET",
        allowed_origins=[ORIGIN],
        interview_guide="Ask what shipped and for one number.",
        writes=[
            WriteSpec(
                kind="launch_post",
                count=1,
                max_chars=280,
                no_links=True,
                no_dashes=True,
                may_carry_image=True,
                instructions="A short launch post.",
            ),
            WriteSpec(kind="changelog", count=1, max_chars=600, instructions="A short changelog entry."),
        ],
        tools={"context": "c", "save_draft": "s", "save_material": "m", "charge": "ch"},
        models=Models(main="main/model", cheap="cheap/model", vision="vis/model"),
        files={"per_message": 2, "kinds": ["image", "voice"]} if demo else {},
        message_usd_cap=0.05,
        demo=demo,
    )


def happy_script() -> dict:
    piece = lambda kind, text: {"spec_kind": kind, "text": text, "fact_ids": ["F1"], "image_file_id": None}
    return {
        "INTERVIEW": [{"enough": True, "just_write": False, "question": None}],
        "EXTRACT_FACTS": [{"facts": [{"text": FACT, "source": "typed", "file_id": None}]}],
        "WRITER": [{"pieces": [piece("launch_post", LAUNCH), piece("changelog", CHANGELOG)]}],
        "CLAIM_CHECK": [OK, OK],
    }


def ask_script() -> dict:
    return {
        "INTERVIEW": [{"enough": False, "just_write": False, "question": "a number"}],
        "ASK_BACK": ["What changed for users?"],
    }


def raw_token(app: str, workspace: str, access: str, ttl: int = 600) -> str:
    now = int(time.time())
    claims = {"iss": app, "sub": workspace, "access": access, "iat": now, "exp": now + ttl}
    return jwt.encode(claims, SECRET, algorithm="HS256")


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        kind, data = None, []
        for line in block.split("\n"):
            if line.startswith("event:"):
                kind = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        if kind:
            events.append((kind, json.loads("\n".join(data))))
    return events


class Api:
    def __init__(self, app, client: httpx.AsyncClient, pool: AsyncConnectionPool, storage: Storage, checkpointer):
        self.app = app
        self.client = client
        self.pool = pool
        self.storage = storage
        self.checkpointer = checkpointer
        self.llm = FakeLlm()
        self.tools = FakeTools()

    def headers(self, workspace: str = "ws1", app: str = "real", **extra: str) -> dict:
        token = mint_session(self.app.state.packs[app], workspace)
        return {"Authorization": f"Bearer {token}", "Origin": ORIGIN, **extra}

    async def send(
        self,
        text: str,
        thread: str | None = None,
        file_ids: list[str] | None = None,
        workspace: str = "ws1",
        app: str = "real",
        **headers: str,
    ) -> list[tuple[str, dict]]:
        body = {"thread_id": thread, "text": text, "file_ids": file_ids or []}
        r = await self.client.post(
            f"/v1/chat/{app}/messages", json=body, headers=self.headers(workspace, app, **headers)
        )
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/event-stream")
        return parse_sse(r.text)

    async def upload(self, data: bytes, thread: str | None = None, workspace: str = "ws1") -> httpx.Response:
        form = {"thread_id": thread} if thread else {}
        files = {"file": ("shot.png", data, "image/png")}
        return await self.client.post("/v1/chat/real/files", data=form, files=files, headers=self.headers(workspace))

    async def act(self, thread: str, body: dict, workspace: str = "ws1") -> httpx.Response:
        return await self.client.post(
            f"/v1/chat/real/threads/{thread}/actions", json=body, headers=self.headers(workspace)
        )

    async def count(self, table: str, workspace: str = "ws1") -> int:
        async with self.pool.connection() as conn:
            cur = await conn.execute(f"select count(*) from {table} where workspace = %s", (workspace,))
            return (await cur.fetchone())[0]

    async def thread_row(self, workspace: str = "ws1", app: str = "real", title: str = "t") -> str:
        tid = new_id()
        async with self.pool.connection() as conn:
            await conn.execute(
                "insert into threads (id, app, workspace, title) values (%s, %s, %s, %s)", (tid, app, workspace, title)
            )
        return tid

    async def piece_row(self, thread: str, dropped: str | None = None, image_file_id: str | None = None) -> str:
        pid = new_id()
        facts = [{"id": "f1", "text": FACT, "numbers": [2.0, 2.0], "source": "typed", "file_id": None}]
        async with self.pool.connection() as conn:
            await conn.execute(
                "insert into pieces (id, app, workspace, thread_id, message_id, spec_kind, text, image_file_id,"
                " fact_ids, facts, dropped) values (%s, 'real', 'ws1', %s, 'm1', 'launch_post', %s, %s, %s, %s, %s)",
                (pid, thread, LAUNCH, image_file_id, Jsonb(["f1"]), Jsonb(facts), dropped),
            )
        return pid


@pytest.fixture
async def api(pool: AsyncConnectionPool, test_settings: Settings, monkeypatch) -> AsyncIterator[Api]:
    monkeypatch.setenv("TEST_CHAT_SECRET", SECRET)
    await migrate(pool)
    saver_pool = AsyncConnectionPool(
        pool.conninfo,
        min_size=1,
        max_size=2,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await saver_pool.open()
    try:
        checkpointer = AsyncPostgresSaver(saver_pool, serde=serializer())  # type: ignore[arg-type]
        await checkpointer.setup()
        with mock_aws():
            storage = Storage(test_settings)
            storage._client.create_bucket(
                Bucket=test_settings.s3_bucket,
                CreateBucketConfiguration={"LocationConstraint": test_settings.s3_region},
            )
            app = create_app(test_settings)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
                api = Api(app, c, pool, storage, checkpointer)
                app.state.pool = pool
                app.state.checkpointer = checkpointer
                app.state.storage = storage
                app.state.http = c
                app.state.packs = {"real": make_pack(), "demo": make_pack("demo", demo=True)}
                app.state.llm = api.llm
                app.state.tools_for = lambda pack, session: api.tools
                yield api
    finally:
        await saver_pool.close()


def of(events: list[tuple[str, dict]], kind: str) -> list[dict]:
    return [p for k, p in events if k == kind]


# ---- auth, origin, CORS ----


async def test_wrong_origin_403(api: Api):
    for origin in ["https://evil.example", "", "null", ORIGIN + "/"]:
        r = await api.client.get("/v1/chat/real/threads", headers=api.headers(Origin=origin))
        assert r.status_code == 403, origin
        assert r.json() == {"error": "origin"}


async def test_origin_absent_allowed(api: Api):
    headers = api.headers()
    del headers["Origin"]
    r = await api.client.get("/v1/chat/real/threads", headers=headers)
    assert r.status_code == 200
    assert r.json() == []
    assert "access-control-allow-origin" not in r.headers


async def test_unknown_app_404(api: Api):
    r = await api.client.get("/v1/chat/nope/threads", headers={"Origin": ORIGIN})
    assert r.status_code == 404
    assert r.json() == {"error": "app"}


async def test_expired_token_401_expired(api: Api):
    expired = mint_session(api.app.state.packs["real"], "ws1", ttl=timedelta(seconds=-1))
    r = await api.client.get("/v1/chat/real/threads", headers={"Authorization": f"Bearer {expired}", "Origin": ORIGIN})
    assert r.status_code == 401
    assert r.json() == {"error": "expired"}
    for auth in [None, "Bearer", "Bearer not-a-token", "Basic abc"]:
        headers = {"Origin": ORIGIN} | ({"Authorization": auth} if auth else {})
        r = await api.client.get("/v1/chat/real/threads", headers=headers)
        assert (r.status_code, r.json()) == (401, {"error": "invalid"}), auth
    # A token for another app is refused too.
    r = await api.client.get("/v1/chat/real/threads", headers=api.headers(app="demo"))
    assert (r.status_code, r.json()) == (401, {"error": "wrong_app"})


async def test_cors_allows_exactly_the_packs_origins(api: Api):
    preflight = {"Origin": ORIGIN, "Access-Control-Request-Method": "POST"}
    r = await api.client.options("/v1/chat/real/messages", headers=preflight)
    assert r.status_code == 204
    assert r.headers["access-control-allow-origin"] == ORIGIN
    assert r.headers["access-control-allow-methods"] == "GET, POST, DELETE"
    assert r.headers["access-control-allow-headers"] == "Authorization, Content-Type"
    assert r.headers["access-control-max-age"] == "600"
    assert "Origin" in r.headers["vary"]

    r = await api.client.options("/v1/chat/real/messages", headers={**preflight, "Origin": "https://evil.example"})
    assert r.status_code == 403
    assert "access-control-allow-origin" not in r.headers

    r = await api.client.get("/v1/chat/real/threads", headers=api.headers())
    assert r.headers["access-control-allow-origin"] == ORIGIN
    assert "Origin" in r.headers["vary"]
    r = await api.client.get("/v1/chat/real/threads", headers=api.headers(Origin="https://evil.example"))
    assert "access-control-allow-origin" not in r.headers


def test_client_ip_is_rightmost_forwarded_for():
    def request(headers: dict[str, str]) -> Request:
        raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
        return Request({"type": "http", "headers": raw, "client": ("10.0.0.1", 1234)})

    assert client_ip(request({"X-Forwarded-For": "1.1.1.1, 203.0.113.9"})) == "203.0.113.9"
    assert client_ip(request({"X-Forwarded-For": " 203.0.113.9 "})) == "203.0.113.9"
    assert client_ip(request({})) == "10.0.0.1"


# ---- messages ----


async def test_message_streams_thread_card_done_and_charges_once(api: Api):
    api.llm.script = happy_script()
    events = await api.send(FACT)
    kinds = [k for k, _ in events]
    assert kinds[0] == "thread"
    assert kinds[-1] == "done"
    assert kinds.count("card") == 1 and kinds.count("done") == 1
    thread = of(events, "thread")[0]
    assert thread["title"] == FACT[:60]
    [done] = of(events, "done")
    assert done["cost_usd"] == pytest.approx(0.005)  # five scripted model calls at 0.001
    assert api.tools.charges == [{"cost_usd": pytest.approx(0.005), "ref": done["message_id"]}]
    assert api.tools.calls == ["context", "context", "charge"]  # credits check, the graph's context, the charge

    [card] = of(events, "card")
    async with api.pool.connection() as conn:
        cur = await conn.execute(
            "select id, thread_id, message_id, text, fact_ids, facts, dropped from pieces order by created_at"
        )
        rows = await cur.fetchall()
    assert [r[0] for r in rows] == [p["id"] for p in card["pieces"]]
    assert {r[1] for r in rows} == {thread["thread_id"]}
    assert {r[2] for r in rows} == {done["message_id"]}
    assert [r[3] for r in rows] == [LAUNCH, CHANGELOG]
    assert [f["text"] for f in rows[0][5]] == [FACT] and rows[0][4] == [rows[0][5][0]["id"]]

    r = await api.client.get(f"/v1/chat/real/threads/{thread['thread_id']}", headers=api.headers())
    assert r.status_code == 200
    body = r.json()
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert body["messages"][0]["text"] == FACT
    assert body["messages"][1]["card"]["pieces"][0]["text"] == LAUNCH
    assert [p["id"] for p in body["pieces"]] == [p["id"] for p in card["pieces"]]

    r = await api.client.get("/v1/chat/real/threads", headers=api.headers())
    assert [(t["id"], t["title"]) for t in r.json()] == [(thread["thread_id"], FACT[:60])]


async def test_second_message_in_thread_has_no_thread_event(api: Api):
    api.llm.script = ask_script()
    first = await api.send("shipped auto-reply")
    tid = of(first, "thread")[0]["thread_id"]
    api.llm.script = ask_script()
    second = await api.send("it is fast", thread=tid)
    assert second[0][0] != "thread"
    assert of(second, "done")
    async with api.pool.connection() as conn:
        cur = await conn.execute("select count(*) from threads")
        assert (await cur.fetchone())[0] == 1


async def test_out_of_credits_fails_with_apps_words_and_no_graph_run(api: Api):
    words = "You're out of credits. Top up in Billing."
    api.tools = FakeTools(context=default_context(credits_left=0, out_of_credits_message=words))
    events = await api.send("shipped auto-reply")
    assert events == [("failed", {"error": "out_of_credits", "message": words})]
    assert api.llm.calls == []
    assert api.tools.calls == ["context"]
    assert await api.count("threads") == 0

    api.tools = FakeTools(context=default_context(credits_left=0, out_of_credits_message=None))
    events = await api.send("shipped auto-reply")
    assert events == [("failed", {"error": "out_of_credits", "message": "You're out of credits in this app."})]


class SecondContextFails(FakeTools):
    """The credits check reaches the app; the graph's own context call finds it down."""

    async def context(self):
        if self.calls.count("context") >= 1:
            self.errors["context"] = ToolError("unavailable")
        return await super().context()


async def test_tool_down_mid_turn_fails_and_charges_partial_once(api: Api):
    up = await api.upload(PNG)
    assert up.status_code == 200, up.text
    tid, fid = up.json()["thread_id"], up.json()["file_id"]
    api.tools = SecondContextFails()
    api.llm.script = {"IMAGE_READER": [Out({"text": "40 accounts on", "unsure": []}, cost=0.01)]}
    events = await api.send("here is the screenshot", thread=tid, file_ids=[fid])
    assert events[-1] == ("failed", {"error": "unavailable", "message": APP_DOWN})
    assert of(events, "done") == []
    assert api.tools.calls == ["context", "context", "charge"]
    [charge] = api.tools.charges
    assert charge["cost_usd"] == pytest.approx(0.01)
    assert len(charge["ref"]) == 26


async def test_client_going_away_does_not_stop_the_turn_or_its_charge(api: Api):
    api.llm.script = happy_script()
    pack = api.app.state.packs["real"]
    session = verify_session(mint_session(pack, "ws1"), pack)
    queue = turn.start(api.app.state, turn.TurnInput(pack, session, None, FACT, [], "203.0.113.9"))
    events = chat._events(queue)
    first = await events.__anext__()
    assert first.event == "thread"
    await events.aclose()  # what the SSE response does when the client disconnects
    await asyncio.wait_for(asyncio.gather(*turn._RUNNING), timeout=10)
    assert len(api.tools.charges) == 1
    assert await api.count("pieces") == 2


async def test_model_failure_fails_with_our_words_and_charges_its_cost(api: Api):
    api.llm.script = {"INTERVIEW": [LlmError("unavailable", "down", cost_usd=0.002)]}
    events = await api.send("shipped auto-reply")
    assert events[-1] == ("failed", {"error": "server", "message": OUR_FAULT})
    assert [c["cost_usd"] for c in api.tools.charges] == [pytest.approx(0.002)]


async def test_charge_failure_still_ends_done(api: Api):
    api.tools = FakeTools(errors={"charge": ToolError("unavailable")})
    api.llm.script = ask_script()
    events = await api.send("shipped auto-reply")
    assert [k for k, _ in events][-1] == "done"
    assert api.tools.calls.count("charge") == 1


async def test_user_cap_fails_before_anything(api: Api):
    assert await hit(
        api.pool, user_key("real", "ws1"), timedelta(hours=1), USER_MESSAGES_PER_HOUR, USER_MESSAGES_PER_HOUR
    )
    events = await api.send("shipped auto-reply")
    assert events == [
        ("failed", {"error": "limit", "message": "You've sent a lot of messages this hour. Try again a bit later."})
    ]
    assert api.llm.calls == [] and api.tools.calls == []


async def test_empty_message_400(api: Api):
    body = {"thread_id": None, "text": "  ", "file_ids": []}
    r = await api.client.post("/v1/chat/real/messages", json=body, headers=api.headers())
    assert r.status_code == 400
    assert r.json()["error"] == "empty"


async def test_too_many_files_400(api: Api):
    tid = await api.thread_row()
    body = {"thread_id": tid, "text": "hi", "file_ids": [new_id() for _ in range(6)]}
    r = await api.client.post("/v1/chat/real/messages", json=body, headers=api.headers())
    assert r.status_code == 400
    assert r.json() == {"error": "too_many_files", "message": "You can send up to 5 files at a time."}
    body = {"thread_id": tid, "text": "hi", "file_ids": [new_id() for _ in range(3)]}
    r = await api.client.post("/v1/chat/demo/messages", json=body, headers=api.headers(app="demo"))
    assert r.json() == {"error": "too_many_files", "message": "You can send up to 2 files at a time."}


async def test_file_of_another_thread_404(api: Api):
    up = (await api.upload(PNG)).json()
    other = await api.thread_row()
    for thread in [other, None]:
        body = {"thread_id": thread, "text": "hi", "file_ids": [up["file_id"]]}
        r = await api.client.post("/v1/chat/real/messages", json=body, headers=api.headers())
        assert r.status_code == 404, thread
        assert r.json() == {"error": "file"}
    assert api.llm.calls == []


async def test_thread_of_other_workspace_404(api: Api):
    api.llm.script = ask_script()
    events = await api.send("shipped auto-reply")
    tid = of(events, "thread")[0]["thread_id"]

    r = await api.client.get(f"/v1/chat/real/threads/{tid}", headers=api.headers("ws2"))
    assert (r.status_code, r.json()) == (404, {"error": "thread"})
    body = {"thread_id": tid, "text": "hi", "file_ids": []}
    r = await api.client.post("/v1/chat/real/messages", json=body, headers=api.headers("ws2"))
    assert (r.status_code, r.json()) == (404, {"error": "thread"})
    assert (await api.upload(PNG, thread=tid, workspace="ws2")).status_code == 404
    r = await api.act(tid, {"action_id": "a1", "kind": "keep_facts", "facts": [{"text": "x"}]}, workspace="ws2")
    assert (r.status_code, r.json()) == (404, {"error": "thread"})
    r = await api.client.get("/v1/chat/real/threads", headers=api.headers("ws2"))
    assert r.json() == []

    r = await api.client.get(f"/v1/chat/real/threads/{tid}", headers=api.headers())
    assert r.status_code == 200
    assert [m["text"] for m in r.json()["messages"]] == ["shipped auto-reply", "What changed for users?"]


# ---- demo ----


async def test_demo_records_spend_instead_of_charging(api: Api, test_settings: Settings):
    api.llm.script = ask_script()
    events = await api.send("shipped auto-reply", app="demo", **{"X-Forwarded-For": "203.0.113.9"})
    [done] = of(events, "done")
    assert "charge" not in api.tools.calls
    left = await remaining(api.pool, DEMO_USD_KEY, test_settings.demo_daily_usd_cap)
    assert left == pytest.approx(test_settings.demo_daily_usd_cap - done["cost_usd"])


async def test_demo_ip_cap_and_dollar_cap(api: Api, test_settings: Settings):
    from assistant.limits import demo_ip_key

    full = demo_ip_key("203.0.113.9")
    assert await hit(api.pool, full, timedelta(days=1), DEMO_MESSAGES_PER_IP_DAY, DEMO_MESSAGES_PER_IP_DAY)
    events = await api.send("hi", app="demo", **{"X-Forwarded-For": "198.51.100.1, 203.0.113.9"})
    assert events == [
        ("failed", {"error": "limit", "message": "The demo has had enough for today from here. Come back tomorrow."})
    ]

    cap = test_settings.demo_daily_usd_cap
    assert await hit(api.pool, DEMO_USD_KEY, timedelta(days=1), cap, cap)
    events = await api.send("hi", app="demo", **{"X-Forwarded-For": "198.51.100.7"})
    assert events == [("failed", {"error": "resting", "message": "The demo is resting until tomorrow."})]
    assert api.llm.calls == []


# ---- uploads ----


async def test_upload_creates_thread_and_returns_file(api: Api):
    r = await api.upload(PNG)
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "image" and len(body["file_id"]) == 26
    r = await api.client.get("/v1/chat/real/threads", headers=api.headers())
    assert [(t["id"], t["title"]) for t in r.json()] == [(body["thread_id"], "Files")]
    again = (await api.upload(PNG, thread=body["thread_id"])).json()
    assert again["thread_id"] == body["thread_id"]


async def test_upload_refused_type_400_and_leaves_no_thread(api: Api):
    r = await api.upload(b"just some text, not a file type")
    assert r.status_code == 400
    assert r.json() == {"error": "type", "message": "This file type isn't supported here."}
    assert await api.count("threads") == 0


async def test_upload_body_too_big_413(api: Api):
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (26 * 1024 * 1024)
    r = await api.upload(big)
    assert r.status_code == 413
    assert r.json() == {"error": "too_big", "message": "That file is too big."}

    async def chunks():  # no Content-Length: the running cap stops it
        yield b'--b\r\nContent-Disposition: form-data; name="file"; filename="a.png"\r\n\r\n'
        for _ in range(27):
            yield b"\x00" * (1024 * 1024)

    headers = api.headers(**{"Content-Type": "multipart/form-data; boundary=b"})
    r = await api.client.post("/v1/chat/real/files", content=chunks(), headers=headers)
    assert r.status_code == 413
    assert r.json() == {"error": "too_big", "message": "That file is too big."}
    assert await api.count("threads") == 0


# ---- actions ----


async def test_add_draft_twice_same_action_one_draft(api: Api):
    api.llm.script = happy_script()
    events = await api.send(FACT)
    tid = of(events, "thread")[0]["thread_id"]
    [card] = of(events, "card")
    piece = card["pieces"][0]
    body = {"action_id": "act-1", "kind": "add_draft", "piece_id": piece["id"]}
    first = await api.act(tid, body)
    second = await api.act(tid, body)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"draft_id": "draft-1", "edit_url": "https://app.example/drafts/draft-1"}
    assert api.tools.calls.count("save_draft") == 1
    assert api.tools.drafts[0]["text"] == LAUNCH and api.tools.drafts[0]["media"] == []
    assert api.tools.drafts[0]["key"] == "act-1"
    [material] = api.tools.materials
    assert material["draft_id"] == "draft-1" and material["key"] == "act-1:facts"
    assert [f.text for f in material["facts"]] == [FACT]


async def test_add_draft_sends_a_signed_image_url(api: Api):
    up = (await api.upload(PNG)).json()
    pid = await api.piece_row(up["thread_id"], image_file_id=up["file_id"])
    r = await api.act(up["thread_id"], {"action_id": "a-img", "kind": "add_draft", "piece_id": pid})
    assert r.status_code == 200
    [media] = api.tools.drafts[0]["media"]
    assert media["alt"] == ""
    assert f"real/ws1/{up['thread_id']}/{up['file_id']}" in media["url"]
    assert "Expires=" in media["url"] or "X-Amz-Expires=300" in media["url"]


async def test_dropped_piece_409(api: Api):
    tid = await api.thread_row()
    pid = await api.piece_row(tid, dropped="it had a link")
    r = await api.act(tid, {"action_id": "a2", "kind": "add_draft", "piece_id": pid})
    assert r.status_code == 409
    assert r.json() == {"error": "dropped", "message": "This post didn't pass the checks, so it can't be added."}
    assert api.tools.calls == []
    r = await api.act(tid, {"action_id": "a3", "kind": "add_draft", "piece_id": "nope"})
    assert (r.status_code, r.json()) == (404, {"error": "piece"})


async def test_retry_after_tool_failure_runs_again(api: Api):
    tid = await api.thread_row()
    pid = await api.piece_row(tid)
    api.tools.errors["save_material"] = ToolError("unavailable")
    body = {"action_id": "a4", "kind": "add_draft", "piece_id": pid}
    r = await api.act(tid, body)
    assert (r.status_code, r.json()) == (502, {"error": "unavailable", "message": APP_DOWN})
    del api.tools.errors["save_material"]
    r = await api.act(tid, body)
    assert r.status_code == 200 and r.json()["draft_id"] == "draft-1"
    assert api.tools.calls.count("save_draft") == 2  # the app's idempotency key keeps it one draft
    assert len(api.tools.drafts) == 1

    api.tools.errors["save_material"] = ToolError("out_of_credits", "No credits left.")
    r = await api.act(tid, {"action_id": "a5", "kind": "keep_facts", "facts": [{"text": FACT}]})
    assert (r.status_code, r.json()) == (402, {"error": "out_of_credits", "message": "No credits left."})


async def test_keep_facts_once(api: Api):
    tid = await api.thread_row()
    body = {"action_id": "keep-1", "kind": "keep_facts", "facts": [{"text": FACT}, {"text": "It is for small teams"}]}
    assert (await api.act(tid, body)).json() == {"kept": 2}
    assert (await api.act(tid, body)).json() == {"kept": 2}
    [material] = api.tools.materials
    assert material["draft_id"] is None and material["key"] == "keep-1"
    assert [f.text for f in material["facts"]] == [FACT, "It is for small teams"]
    r = await api.act(tid, {"action_id": "", "kind": "keep_facts", "facts": []})
    assert r.status_code == 422


# ---- delete ----


async def test_delete_workspace_removes_threads_files_and_storage(api: Api):
    up = (await api.upload(PNG)).json()
    tid = up["thread_id"]
    api.llm.script = happy_script()
    api.llm.add("IMAGE_READER", {"text": "a screenshot", "unsure": []})
    await api.send(FACT, thread=tid, file_ids=[up["file_id"]])
    assert (await api.act(tid, {"action_id": "k1", "kind": "keep_facts", "facts": [{"text": FACT}]})).status_code == 200
    other = (await api.upload(PNG, workspace="ws2")).json()
    config = {"configurable": {"thread_id": thread_id("real", "ws1", tid)}}
    assert await api.checkpointer.aget_tuple(config) is not None

    token = raw_token("real", "ws1", "delete")
    r = await api.client.delete("/v1/apps/real/workspaces/ws1", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json() == {"deleted_threads": 1}
    for table in ["threads", "files", "pieces", "actions"]:
        assert await api.count(table) == 0, table
    assert await api.checkpointer.aget_tuple(config) is None
    listed = api.storage._client.list_objects_v2(Bucket=api.storage.bucket)
    keys = [o["Key"] for o in listed.get("Contents", [])]
    assert keys == [f"real/ws2/{other['thread_id']}/{other['file_id']}"]
    assert await api.count("threads", "ws2") == 1


async def test_delete_token_wrong_access_or_workspace(api: Api):
    url = "/v1/apps/real/workspaces/ws1"
    draft = mint_session(api.app.state.packs["real"], "ws1")
    r = await api.client.delete(url, headers={"Authorization": f"Bearer {draft}"})
    assert (r.status_code, r.json()) == (401, {"error": "wrong_access"})

    delete = raw_token("real", "ws1", "delete")
    r = await api.client.get("/v1/chat/real/threads", headers={"Authorization": f"Bearer {delete}", "Origin": ORIGIN})
    assert (r.status_code, r.json()) == (401, {"error": "wrong_access"})

    r = await api.client.delete(url, headers={"Authorization": f"Bearer {raw_token('real', 'ws2', 'delete')}"})
    assert (r.status_code, r.json()) == (403, {"error": "workspace"})
    r = await api.client.delete("/v1/apps/nope/workspaces/ws1", headers={"Authorization": f"Bearer {delete}"})
    assert (r.status_code, r.json()) == (404, {"error": "app"})
    r = await api.client.delete(url)
    assert (r.status_code, r.json()) == (401, {"error": "invalid"})


async def test_pending_action_409_until_stale(api: Api):
    tid = await api.thread_row()
    async with api.pool.connection() as conn:
        await conn.execute(
            "insert into actions (id, app, workspace, thread_id, kind, result) values"
            " ('real:ws1:p1', 'real', 'ws1', %s, 'keep_facts', 'null'::jsonb)",
            (tid,),
        )
    body = {"action_id": "p1", "kind": "keep_facts", "facts": [{"text": FACT}]}
    r = await api.act(tid, body)
    assert (r.status_code, r.json()["error"]) == (409, "pending")
    assert api.tools.calls == []
    async with api.pool.connection() as conn:  # the run that claimed it died long ago
        await conn.execute("update actions set created_at = now() - interval '3 minutes' where id = 'real:ws1:p1'")
    r = await api.act(tid, body)
    assert r.json() == {"kept": 1}


async def test_action_ids_are_per_workspace(api: Api):
    body = {"action_id": "same", "kind": "keep_facts", "facts": [{"text": FACT}]}
    assert (await api.act(await api.thread_row(), body)).json() == {"kept": 1}
    other = {**body, "facts": [{"text": "a"}, {"text": "b"}]}
    assert (await api.act(await api.thread_row("ws2"), other, workspace="ws2")).json() == {"kept": 2}
    assert len(api.tools.materials) == 2


async def test_colon_ids_cannot_read_or_take_over_another_workspaces_action(api: Api):
    # "real" + "ws1:x" + "a" and "real" + "ws1" + "x:a" compose the same key "real:ws1:x:a".
    body = {"action_id": "a", "kind": "keep_facts", "facts": [{"text": FACT}]}
    assert (await api.act(await api.thread_row("ws1:x"), body, workspace="ws1:x")).json() == {"kept": 1}
    other = {"action_id": "x:a", "kind": "keep_facts", "facts": [{"text": "a"}, {"text": "b"}]}
    r = await api.act(await api.thread_row("ws1"), other, workspace="ws1")
    assert r.status_code == 409
    assert "kept" not in r.json()
    async with api.pool.connection() as conn:  # a stale pending row of the other workspace is not re-claimed
        await conn.execute(
            "update actions set result = 'null'::jsonb, created_at = now() - interval '3 minutes'"
            " where id = 'real:ws1:x:a'"
        )
    r = await api.act(await api.thread_row("ws1"), other, workspace="ws1")
    assert r.status_code == 409
    assert len(api.tools.materials) == 1
    async with api.pool.connection() as conn:
        cur = await conn.execute("select workspace from actions where id = 'real:ws1:x:a'")
        assert (await cur.fetchone())[0] == "ws1:x"


# ---- review round 1 ----


async def test_pieces_not_saved_shows_card_fails_turn_and_charges_once(api: Api, monkeypatch):
    async def broken(*_args):
        raise RuntimeError("database went away")

    monkeypatch.setattr(turn, "_save_pieces", broken)
    api.llm.script = happy_script()
    events = await api.send(FACT)
    assert len(of(events, "card")) == 1  # what was written is still shown
    assert of(events, "done") == []
    assert events[-1] == ("failed", {"error": "server", "message": OUR_FAULT})
    assert [c["cost_usd"] for c in api.tools.charges] == [pytest.approx(0.005)]


async def test_message_over_4000_chars_400(api: Api):
    body = {"thread_id": None, "text": "a" * 4001, "file_ids": []}
    r = await api.client.post("/v1/chat/real/messages", json=body, headers=api.headers())
    assert r.status_code == 400
    assert r.json() == {"error": "too_long", "message": "That message is too long. Keep it under 4,000 characters."}
    api.llm.script = ask_script()
    assert of(await api.send("a" * 3999 + "?"), "done")


async def test_workspace_with_slash_is_invalid(api: Api):
    r = await api.client.get("/v1/chat/real/threads", headers=api.headers("a/b"))
    assert (r.status_code, r.json()) == (401, {"error": "invalid"})
    token = raw_token("real", "a/b", "delete")
    r = await api.client.delete("/v1/apps/real/workspaces/a", headers={"Authorization": f"Bearer {token}"})
    assert (r.status_code, r.json()) == (401, {"error": "invalid"})


async def test_unhandled_error_is_json_500_with_cors_and_no_text_logged(api: Api, monkeypatch, caplog):
    secret_text = "my secret launch post about 40 customers"

    async def boom(*_args):
        raise ValueError(secret_text)

    with caplog.at_level("DEBUG"):
        with monkeypatch.context() as m:
            m.setattr(chat, "owned_thread", boom)
            r = await api.client.get("/v1/chat/real/threads/t1", headers=api.headers())
        assert r.status_code == 500
        assert r.json() == {"error": "server", "message": "Something went wrong on our side."}
        assert r.headers["access-control-allow-origin"] == ORIGIN

        # an action whose tool raises something unexpected frees its claim and answers in JSON too
        tid = await api.thread_row()
        api.tools.errors["save_material"] = ValueError(secret_text)
        body = {"action_id": "x1", "kind": "keep_facts", "facts": [{"text": FACT}]}
        r = await api.act(tid, body)
        assert (r.status_code, r.json()["error"]) == (500, "server")
        del api.tools.errors["save_material"]
        assert (await api.act(tid, body)).json() == {"kept": 1}
    assert secret_text not in caplog.text
    assert "Traceback" not in caplog.text
    assert "ValueError" in caplog.text


async def test_upload_limit_429(api: Api):
    assert await hit(api.pool, "upload:real:ws1", timedelta(hours=1), 30, 30)
    r = await api.upload(PNG)
    assert r.status_code == 429
    assert r.json() == {"error": "limit", "message": "You've uploaded a lot this hour. Try again a bit later."}
    assert (await api.upload(PNG, workspace="ws2")).status_code == 200


async def test_drain_waits_for_running_turns():
    done: list[str] = []

    async def slow(seconds: float, name: str) -> None:
        await asyncio.sleep(seconds)
        done.append(name)

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        openrouter_api_key="k",
        database_url="postgresql://x",
        s3_bucket="b",
        s3_access_key="a",
        s3_secret_key="s",
        public_base_url="http://t",
        shutdown_drain_seconds=0.05,
    )
    assert Settings.model_fields["shutdown_drain_seconds"].default == 8.0
    turn.track(asyncio.create_task(slow(0.01, "quick")))
    await turn.drain(settings.shutdown_drain_seconds)
    assert done == ["quick"]
    stuck = asyncio.create_task(slow(10, "stuck"))
    turn.track(stuck)
    started = time.monotonic()
    await turn.drain(settings.shutdown_drain_seconds)
    assert time.monotonic() - started < 1 and done == ["quick"]
    stuck.cancel()


async def test_add_draft_never_sends_an_image_from_another_thread(api: Api):
    elsewhere = (await api.upload(PNG)).json()
    tid = await api.thread_row()
    pid = await api.piece_row(tid, image_file_id=elsewhere["file_id"])
    r = await api.act(tid, {"action_id": "a-x", "kind": "add_draft", "piece_id": pid})
    assert r.status_code == 200
    assert api.tools.drafts[0]["media"] == []
