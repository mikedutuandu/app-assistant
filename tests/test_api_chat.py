"""The chat API end to end: FastAPI routes on a fresh Postgres schema, moto S3, FakeLlm and FakeTools."""

import asyncio
import json
import logging
import time
from collections import Counter
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from chat_api import (
    APP_DOWN,
    FACT,
    LAUNCH,
    ORIGIN,
    OUR_FAULT,
    PNG,
    REPLY,
    Api,
    ask_script,
    happy_script,
    raw_token,
)
from fakes import FakeLlm, FakeTools, Out, default_context, marker
from starlette.requests import Request

from assistant.api import chat, turn
from assistant.api.deps import client_ip
from assistant.auth import mint_session, verify_session
from assistant.config import Settings
from assistant.db import new_id
from assistant.graph.build import thread_id
from assistant.limits import DEMO_MESSAGES_PER_IP_DAY, DEMO_USD_KEY, USER_MESSAGES_PER_HOUR, hit, remaining, user_key
from assistant.llm import LlmError
from assistant.tools import ToolError

pytestmark = pytest.mark.db


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


async def test_message_streams_thread_draft_done_and_charges_once(api: Api):
    api.llm.script = happy_script()
    events = await api.send(FACT)
    kinds = [k for k, _ in events]
    assert kinds[0] == "thread"
    assert kinds[-1] == "done"
    assert kinds.count("draft") == 1 and kinds.count("done") == 1 and "card" not in kinds
    thread = of(events, "thread")[0]
    assert thread["title"] == FACT[:60]
    [done] = of(events, "done")
    assert done["cost_usd"] == pytest.approx(0.004)  # four scripted model calls at 0.001
    assert api.tools.charges == [
        {"cost_usd": pytest.approx(0.001), "ref": f"{done['message_id']}:{n}"} for n in range(1, 5)
    ]
    # the credits check, the graph's context, then one charge after each of the four model calls (sent without
    # waiting, so in any order around the fact saved between them)
    assert api.tools.calls[:2] == ["context", "context"]
    assert Counter(api.tools.calls[2:]) == {"charge": 4, "save_material": 1}
    [draft] = of(events, "draft")
    assert draft["posts"] == [LAUNCH] and draft["kind"] == "x_post"
    assert of(events, "saved") == [{"kind": "facts", "items": [FACT]}]
    assert "".join(p["delta"] for p in of(events, "text")) == REPLY

    r = await api.client.get(f"/v1/chat/real/threads/{thread['thread_id']}", headers=api.headers())
    assert r.status_code == 200
    body = r.json()
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert [m["text"] for m in body["messages"]] == [FACT, REPLY]

    r = await api.client.get("/v1/chat/real/threads", headers=api.headers())
    assert [(t["id"], t["title"]) for t in r.json()] == [(thread["thread_id"], FACT[:60])]


async def test_charge_refs_reach_the_browser_only_after_their_charges(api: Api):
    """The app charges once per ref: a browser that learned a ref first could spend it with a 0 charge."""
    timeline: list[tuple[str, str]] = []
    seen_at_charge: list[str] = []

    class Recording(FakeTools):
        async def charge(self, cost_usd: float, ref: str) -> None:
            # What the browser could read right now: the thread.
            [tid] = [json.loads(p)["thread_id"] for k, p in timeline if k == "thread"]
            r = await api.client.get(f"/v1/chat/real/threads/{tid}", headers=api.headers())
            seen_at_charge.append(r.text)
            timeline.append(("charge", ref))
            await super().charge(cost_usd, ref)

    api.tools = Recording()
    api.llm.script = happy_script()
    pack = api.app.state.packs["real"]
    session = verify_session(mint_session(pack, "ws1"), pack)

    async def emit(kind: str, payload: dict) -> None:
        timeline.append((kind, json.dumps(payload)))

    await turn.run_turn(api.app.state, turn.TurnInput(pack, session, None, FACT, [], "1.2.3.4"), emit)
    kinds = [k for k, _ in timeline]
    assert kinds.count("charge") == 4
    assert kinds.index("done") == len(kinds) - 1  # every charge came before `done`
    message_id = json.loads(dict(timeline)["done"])["message_id"]
    refs = [p for k, p in timeline if k == "charge"]
    assert sorted(refs) == [f"{message_id}:{n}" for n in range(1, 5)]  # sent without waiting: any order
    # Nothing the browser was sent or could read before `done` carries the message id, so no ref.
    before = [p for k, p in timeline if k not in ("charge", "done")]
    assert all(message_id not in p for p in before)
    assert len(seen_at_charge) == 4 and all(message_id not in r for r in seen_at_charge)


async def test_a_slow_charge_does_not_hold_up_the_next_model_call(api: Api):
    """Charging is a round trip to the app (2-3 s live): the turn goes on while it is in flight, and `done` still
    waits for every charge."""
    timeline: list[str] = []
    release = asyncio.Event()

    class SlowCharge(FakeTools):
        async def charge(self, cost_usd: float, ref: str) -> None:
            timeline.append(f"charge-start {ref.split(':')[1]}")
            await release.wait()
            timeline.append("charge-end")
            await super().charge(cost_usd, ref)

    class Counting(FakeLlm):
        def _started(self) -> None:
            timeline.append("model")
            if timeline.count("model") == 2:
                release.set()  # the second call started while the first charge was still waiting

        async def chat_tools(self, *args, **kwargs):
            await asyncio.sleep(0)  # a real call waits on the network: the charges sent so far get to start
            self._started()
            return await super().chat_tools(*args, **kwargs)

        async def chat_json(self, *args, **kwargs):
            await asyncio.sleep(0)
            self._started()
            return await super().chat_json(*args, **kwargs)

    api.tools = SlowCharge()
    counting = Counting()
    counting.script = happy_script()
    api.app.state.llm = counting
    pack = api.app.state.packs["real"]
    session = verify_session(mint_session(pack, "ws1"), pack)
    events: list[str] = []

    async def emit(kind: str, payload: dict) -> None:
        events.append(kind)
        timeline.append(f"event {kind}")

    await asyncio.wait_for(
        turn.run_turn(api.app.state, turn.TurnInput(pack, session, None, FACT, [], "1.2.3.4"), emit), timeout=5
    )
    assert timeline.index("charge-start 1") < timeline.index("model", timeline.index("charge-start 1"))
    assert events[-1] == "done"
    assert timeline.count("charge-end") == 4 and timeline.index("event done") > max(
        i for i, x in enumerate(timeline) if x == "charge-end"
    )


class BlocksAtClaimCheck(FakeLlm):
    """Answers every call until the claim check (the third), which waits to be let go: a turn caught in the middle
    of a call."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.at_check = asyncio.Event()
        self.go = asyncio.Event()

    async def chat_json(self, messages, model, fallbacks, schema, max_tokens, reasoning=None):
        if marker(messages) == "CLAIM_CHECK":
            self.at_check.set()
            await self.go.wait()
        return await super().chat_json(messages, model, fallbacks, schema, max_tokens, reasoning)


async def test_a_turn_cut_off_after_two_calls_has_charged_both(api: Api):
    llm = BlocksAtClaimCheck(happy_script())
    api.app.state.llm = llm
    pack = api.app.state.packs["real"]
    session = verify_session(mint_session(pack, "ws1"), pack)
    task = asyncio.create_task(
        turn.run_turn(api.app.state, turn.TurnInput(pack, session, None, FACT, [], "1.2.3.4"), _ignore)
    )
    await asyncio.wait_for(llm.at_check.wait(), timeout=10)
    task.cancel()  # the function is cut off in the middle of the third call
    with pytest.raises(asyncio.CancelledError):
        await task
    [first, second] = api.tools.charges
    assert first["ref"].endswith(":1") and second["ref"].endswith(":2")
    assert first["ref"].split(":")[0] == second["ref"].split(":")[0]
    assert [c["cost_usd"] for c in api.tools.charges] == [pytest.approx(0.001)] * 2


async def _ignore(kind: str, payload: dict) -> None:
    return None


async def test_retry_of_the_same_ref_charges_once():
    tools = FakeTools()
    await tools.charge(0.001, ref="m1:1")
    await tools.charge(0.001, ref="m1:1")
    await tools.charge(0.002, ref="m1:2")
    assert [c["ref"] for c in tools.charges] == ["m1:1", "m1:2"]


async def test_a_charge_that_raises_anything_never_stops_the_turn(api: Api):
    class Boom(FakeTools):
        async def charge(self, cost_usd: float, ref: str) -> None:
            raise RuntimeError("connection reset")

    api.tools = Boom()
    api.llm.script = happy_script()
    events = await api.send(FACT)
    assert [k for k, _ in events][-1] == "done"
    assert len(of(events, "draft")) == 1


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
    assert api.tools.calls == ["context", "charge", "context"]  # the image was read (and charged) before the failure
    [charge] = api.tools.charges
    assert charge["cost_usd"] == pytest.approx(0.01)
    message_id, n = charge["ref"].split(":")
    assert len(message_id) == 26 and n == "1"


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
    assert len(api.tools.charges) == 4
    config = {"configurable": {"thread_id": thread_id("real", "ws1", json.loads(first.data)["thread_id"])}}
    saved = await api.checkpointer.aget_tuple(config)
    assert [d.posts for d in saved.checkpoint["channel_values"]["drafts"]] == [[LAUNCH]]  # the turn ran to its end


async def test_model_failure_fails_with_our_words_and_charges_its_cost(api: Api):
    api.llm.script = {"CONVERSE": [LlmError("unavailable", "down", cost_usd=0.002)]}
    events = await api.send("shipped auto-reply")
    assert events[-1] == ("failed", {"error": "server", "message": OUR_FAULT})
    assert [c["cost_usd"] for c in api.tools.charges] == [pytest.approx(0.002)]
    assert api.tools.charges[0]["ref"].endswith(":1")


async def test_a_message_with_a_link_reads_the_page(api: Api):
    """The turn's real link reader, with the page served by a stub at the HTTP level."""
    fetched: list[str] = []

    def page(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        return httpx.Response(200, html="<html><body><p>Auto-reply launched for 40 teams</p></body></html>")

    api.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(page))
    api.llm.script = ask_script()
    events = await api.send("we shipped it, see https://93.184.216.34/launch")
    assert events[-1][0] == "done", events
    assert fetched == ["https://93.184.216.34/launch"]
    assert ("status", {"step": "reading"}) in events
    [converse] = api.llm.calls
    assert (
        '<data source="link" file_id="https://93.184.216.34/launch">Auto-reply launched for 40 teams</data>'
        in (converse["messages"][0]["content"])
    )


async def test_a_model_failure_after_a_draft_fails_the_turn_and_keeps_the_draft(api: Api):
    api.llm.script = happy_script()
    api.llm.script["CONVERSE"][1] = LlmError("unavailable", "stream failed after output", cost_usd=0.002)
    events = await api.send(FACT)
    assert len(of(events, "draft")) == 1 and of(events, "done") == []
    assert events[-1] == ("failed", {"error": "server", "message": OUR_FAULT})
    assert sorted(c["cost_usd"] for c in api.tools.charges) == [pytest.approx(x) for x in (0.001, 0.001, 0.001, 0.002)]
    tid = of(events, "thread")[0]["thread_id"]
    saved = await api.checkpointer.aget_tuple({"configurable": {"thread_id": thread_id("real", "ws1", tid)}})
    values = saved.checkpoint["channel_values"]
    assert [d.posts for d in values["drafts"]] == [[LAUNCH]] and [f.text for f in values["facts"]] == [FACT]


async def test_charge_failure_still_ends_done(api: Api):
    api.tools = FakeTools(errors={"charge": ToolError("unavailable")})
    api.llm.script = happy_script()
    events = await api.send(FACT)
    assert [k for k, _ in events][-1] == "done"
    assert api.tools.calls.count("charge") == 4  # all four model calls tried to charge; the failures were only logged


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
    r = await api.act(tid, {"action_id": "a1", "kind": "add_draft", "draft_id": "x"}, workspace="ws2")
    assert (r.status_code, r.json()) == (404, {"error": "thread"})
    r = await api.client.get("/v1/chat/real/threads", headers=api.headers("ws2"))
    assert r.json() == []

    r = await api.client.get(f"/v1/chat/real/threads/{tid}", headers=api.headers())
    assert r.status_code == 200
    assert [m["text"] for m in r.json()["messages"]] == ["shipped auto-reply", "What changed for users?"]


# ---- host-chosen thread ids, outside context, deleting one thread ----

HOST_ID = "01KA0000000000000000000001"


async def test_new_thread_with_host_id(api: Api):
    api.llm.script = ask_script()
    events = await api.send("shipped auto-reply", thread=HOST_ID, new_thread=True)
    assert events[0] == ("thread", {"thread_id": HOST_ID, "title": "shipped auto-reply"})
    async with api.pool.connection() as conn:
        cur = await conn.execute("select app, workspace from threads where id = %s", (HOST_ID,))
        assert await cur.fetchone() == ("real", "ws1")


async def test_new_thread_existing_own_continues(api: Api):
    api.llm.script = ask_script()
    await api.send("shipped auto-reply", thread=HOST_ID, new_thread=True)
    api.llm.script = ask_script()
    second = await api.send("it is fast", thread=HOST_ID, new_thread=True)
    assert "thread" not in [k for k, _ in second]
    assert of(second, "done")
    assert await api.count("threads") == 1
    state = await api.checkpointer.aget_tuple({"configurable": {"thread_id": thread_id("real", "ws1", HOST_ID)}})
    humans = [m for m in state.checkpoint["channel_values"]["messages"] if m.type == "human"]
    assert len(humans) == 2


async def test_new_thread_foreign_id_404(api: Api):
    api.llm.script = ask_script()
    await api.send("shipped auto-reply", thread=HOST_ID, new_thread=True)
    r = await api.post_message("hi", thread=HOST_ID, workspace="ws2", new_thread=True)
    assert (r.status_code, r.json()) == (404, {"error": "thread"})
    r = await api.post_message("hi", thread=HOST_ID, app="demo", new_thread=True)
    assert (r.status_code, r.json()) == (404, {"error": "thread"})
    assert await api.count("threads", "ws2") == 0


async def test_new_thread_bad_id_400(api: Api):
    r = await api.post_message("hi", thread="abc", new_thread=True)
    assert (r.status_code, r.json()) == (400, {"error": "thread"})
    r = await api.post_message("hi", thread=HOST_ID.lower(), new_thread=True)
    assert (r.status_code, r.json()) == (400, {"error": "thread"})
    assert await api.count("threads") == 0


async def test_new_thread_without_id_is_the_normal_start(api: Api):
    api.llm.script = ask_script()
    events = await api.send("shipped auto-reply", new_thread=True)
    assert of(events, "thread")[0]["thread_id"] != HOST_ID


async def test_context_reaches_model(api: Api):
    api.llm.script = ask_script()
    ctx = [{"role": "user", "text": "when to post?"}, {"role": "assistant", "text": "Post at 9am <b>"}]
    r = await api.post_message("write it", context=ctx)
    assert r.status_code == 200
    [converse] = [c for c in api.llm.calls if c["name"] == "CONVERSE"]
    system = converse["messages"][0]["content"]
    assert '<data source="ask">user: when to post?\nassistant: Post at 9am <b></data>' in system


async def test_no_context_leaves_the_system_prompt_without_an_ask_block(api: Api):
    api.llm.script = ask_script()
    await api.send("shipped auto-reply")
    [converse] = [c for c in api.llm.calls if c["name"] == "CONVERSE"]
    assert 'source="ask"' not in converse["messages"][0]["content"]


async def test_context_too_long_400(api: Api):
    turns = [{"role": "user", "text": "x"}] * 11
    r = await api.post_message("hi", context=turns)
    assert (r.status_code, r.json()["error"]) == (400, "too_long")
    r = await api.post_message("hi", context=[{"role": "user", "text": "x" * 2001}])
    assert (r.status_code, r.json()["error"]) == (400, "too_long")
    r = await api.post_message("hi", context=[{"role": "user", "text": "x" * 2000}] * 10)
    assert r.status_code == 200


async def test_delete_thread_removes_rows_checkpoint_storage(api: Api):
    up = (await api.start_upload(len(PNG), thread=HOST_ID, new_thread=True)).json()
    api.put(up, PNG)
    up["file_id"] = (await api.finish(up["upload_id"], HOST_ID)).json()["file_id"]
    api.llm.script = happy_script()
    api.llm.add("IMAGE_READER", {"text": "a screenshot", "unsure": []})
    await api.send(FACT, thread=HOST_ID, file_ids=[up["file_id"]])
    await api.piece_row(HOST_ID)
    other = (await api.upload(PNG)).json()  # ws1's other thread
    other_ws = (await api.upload(PNG, workspace="ws2")).json()
    config = {"configurable": {"thread_id": thread_id("real", "ws1", HOST_ID)}}
    assert await api.checkpointer.aget_tuple(config) is not None

    r = await api.client.delete(f"/v1/chat/real/threads/{HOST_ID}", headers=api.headers())
    assert (r.status_code, r.json()) == (200, {"deleted": True})
    async with api.pool.connection() as conn:
        for table, column in [
            ("threads", "id"),
            *[(t, "thread_id") for t in ("files", "pieces", "actions", "saved_lines")],
        ]:
            cur = await conn.execute(f"select count(*) from {table} where {column} = %s", (HOST_ID,))
            assert (await cur.fetchone())[0] == 0, table
    assert await api.checkpointer.aget_tuple(config) is None
    listed = api.storage._client.list_objects_v2(Bucket=api.storage.bucket)
    keys = sorted(o["Key"] for o in listed.get("Contents", []))
    assert keys == sorted(
        [f"real/ws1/{other['thread_id']}/{other['file_id']}", f"real/ws2/{other_ws['thread_id']}/{other_ws['file_id']}"]
    )
    assert await api.count("threads") == 1 and await api.count("threads", "ws2") == 1


async def test_delete_thread_foreign_404(api: Api):
    tid = await api.thread_row()
    r = await api.client.delete(f"/v1/chat/real/threads/{tid}", headers=api.headers("ws2"))
    assert (r.status_code, r.json()) == (404, {"error": "thread"})
    r = await api.client.delete(f"/v1/chat/real/threads/{new_id()}", headers=api.headers())
    assert (r.status_code, r.json()) == (404, {"error": "thread"})
    assert await api.count("threads") == 1


async def test_delete_thread_is_limited_per_workspace(api: Api):
    for _ in range(chat.THREAD_DELETES_PER_HOUR):
        await api.client.delete(f"/v1/chat/real/threads/{new_id()}", headers=api.headers())
    r = await api.client.delete(f"/v1/chat/real/threads/{new_id()}", headers=api.headers())
    assert (r.status_code, r.json()["error"]) == (429, "limit")


async def test_upload_new_thread_with_host_id(api: Api):
    r = await api.start_upload(len(PNG), thread=HOST_ID, new_thread=True)
    assert r.status_code == 200
    assert r.json()["thread_id"] == HOST_ID
    async with api.pool.connection() as conn:
        cur = await conn.execute("select id from threads where workspace = 'ws1'")
        assert [x[0] for x in await cur.fetchall()] == [HOST_ID]
    # a second upload to the same id continues it
    assert (await api.start_upload(len(PNG), thread=HOST_ID, new_thread=True)).status_code == 200
    r = await api.start_upload(len(PNG), thread=HOST_ID, new_thread=True, workspace="ws2")
    assert (r.status_code, r.json()) == (404, {"error": "thread"})
    r = await api.start_upload(len(PNG), thread="abc", new_thread=True)
    assert (r.status_code, r.json()) == (400, {"error": "thread"})


# ---- demo ----


async def test_demo_records_spend_instead_of_charging(api: Api, test_settings: Settings):
    api.llm.script = ask_script()
    events = await api.send("shipped auto-reply", app="demo", **{"X-Forwarded-For": "203.0.113.9"})
    [done] = of(events, "done")
    assert "charge" not in api.tools.calls
    left = await remaining(api.pool, DEMO_USD_KEY, test_settings.demo_daily_usd_cap)
    assert left == pytest.approx(test_settings.demo_daily_usd_cap - done["cost_usd"])


async def test_demo_spend_recorded_per_call(api: Api, test_settings: Settings):
    """Each call adds to the day's spend as it ends: a turn cut off after two calls has already counted both."""
    llm = BlocksAtClaimCheck(happy_script())
    api.app.state.llm = llm
    pack = api.app.state.packs["demo"]
    session = verify_session(mint_session(pack, "ws1"), pack)
    task = asyncio.create_task(
        turn.run_turn(api.app.state, turn.TurnInput(pack, session, None, FACT, [], "203.0.113.9"), _ignore)
    )
    await asyncio.wait_for(llm.at_check.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    left = await remaining(api.pool, DEMO_USD_KEY, test_settings.demo_daily_usd_cap)
    assert left == pytest.approx(test_settings.demo_daily_usd_cap - 0.002)
    assert api.tools.charges == []


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
    r = await api.start_upload(len(PNG))
    assert r.status_code == 200, r.text
    started = r.json()
    assert set(started) == {"upload_id", "thread_id", "target"}
    target = started["target"]
    assert target["method"] == "PUT" and target["headers"] == {"content-type": "image/png", "if-none-match": "*"}
    assert f"/real/ws1/{started['thread_id']}/{started['upload_id']}?" in target["url"]
    api.put(started, PNG)
    r = await api.finish(started["upload_id"], started["thread_id"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"file_id": started["upload_id"], "kind": "image", "thread_id": started["thread_id"]}
    r = await api.client.get("/v1/chat/real/threads", headers=api.headers())
    assert [(t["id"], t["title"]) for t in r.json()] == [(body["thread_id"], "Files")]
    again = (await api.upload(PNG, thread=body["thread_id"])).json()
    assert again["thread_id"] == body["thread_id"]


async def test_old_files_route_is_gone(api: Api):
    files = {"file": ("shot.png", PNG, "image/png")}
    r = await api.client.post("/v1/chat/real/files", files=files, headers=api.headers())
    assert r.status_code in (404, 405)


async def test_upload_claim_refused_400_before_anything(api: Api):
    r = await api.start_upload(10, mime="text/html", name="page.html")
    assert (r.status_code, r.json()) == (400, {"error": "type", "message": "This file type isn't supported here."})
    r = await api.start_upload(11 * 1024 * 1024)
    assert (r.status_code, r.json()) == (400, {"error": "too_big", "message": "Images can be up to 10 MB."})
    r = await api.start_upload(10, app="demo", mime="application/pdf", name="a.pdf")
    assert (r.status_code, r.json()) == (400, {"error": "kind_off", "message": "This app doesn't take PDF files."})
    assert await api.count("threads") == 0 and await api.count("files") == 0


async def test_upload_refused_type_400_and_leaves_no_thread(api: Api):
    r = await api.upload(b"just some text, not a file type")
    assert r.status_code == 400
    assert r.json() == {"error": "type", "message": "This file type isn't supported here."}
    assert await api.count("threads") == 0 and await api.count("files") == 0
    assert api.storage._client.list_objects_v2(Bucket=api.storage.bucket).get("Contents", []) == []


async def test_finish_refuses_missing_object(api: Api):
    started = (await api.start_upload(len(PNG))).json()
    r = await api.finish(started["upload_id"], started["thread_id"])
    assert (r.status_code, r.json()) == (400, {"error": "missing", "message": "The file didn't arrive. Try again."})
    api.put(started, PNG)  # the upload is still open: the browser may try again
    r = await api.finish(started["upload_id"], started["thread_id"])
    assert r.status_code == 200 and r.json()["file_id"] == started["upload_id"]


async def test_finish_deletes_and_refuses_a_pdf_uploaded_as_an_image_to_the_demo(api: Api):
    pdf = (Path(__file__).parent / "fixtures" / "text.pdf").read_bytes()
    r = await api.start_upload(len(pdf), app="demo", mime="image/png", name="shot.png")
    assert r.status_code == 200, r.text
    started = r.json()
    api.put(started, pdf, app="demo")
    r = await api.finish(started["upload_id"], started["thread_id"], app="demo")
    assert (r.status_code, r.json()) == (400, {"error": "kind_off", "message": "This app doesn't take PDF files."})
    assert api.storage._client.list_objects_v2(Bucket=api.storage.bucket).get("Contents", []) == []
    assert await api.count("files") == 0
    assert await api.count("threads") == 0  # the thread made for this upload got no file, so it is not kept


async def test_finish_deletes_and_refuses_an_object_over_the_cap(api: Api):
    tid = await api.thread_row()
    started = (await api.start_upload(1000, thread=tid)).json()  # it claimed 1000 bytes
    api.put(started, PNG + b"\x00" * (11 * 1024 * 1024))
    r = await api.finish(started["upload_id"], tid)
    assert (r.status_code, r.json()) == (400, {"error": "too_big", "message": "Images can be up to 10 MB."})
    assert api.storage._client.list_objects_v2(Bucket=api.storage.bucket).get("Contents", []) == []
    assert await api.count("files") == 0
    assert await api.count("threads") == 1  # a thread the user already had stays
    r = await api.finish(started["upload_id"], tid)
    assert (r.status_code, r.json()) == (404, {"error": "upload"})


async def test_finish_is_idempotent(api: Api):
    started = (await api.start_upload(len(PNG))).json()
    api.put(started, PNG)
    first = await api.finish(started["upload_id"], started["thread_id"])
    second = await api.finish(started["upload_id"], started["thread_id"])
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert await api.count("files") == 1


async def test_another_workspaces_upload_id_is_404(api: Api):
    started = (await api.start_upload(len(PNG))).json()
    api.put(started, PNG)
    r = await api.finish(started["upload_id"], started["thread_id"], workspace="ws2")
    assert (r.status_code, r.json()) == (404, {"error": "upload"})
    r = await api.finish(started["upload_id"], await api.thread_row())  # another thread of the same workspace
    assert (r.status_code, r.json()) == (404, {"error": "upload"})
    assert (await api.finish(started["upload_id"], started["thread_id"])).status_code == 200


async def test_a_pending_upload_cannot_be_sent(api: Api):
    started = (await api.start_upload(len(PNG))).json()
    api.put(started, PNG)
    body = {"thread_id": started["thread_id"], "text": "hi", "file_ids": [started["upload_id"]]}
    r = await api.client.post("/v1/chat/real/messages", json=body, headers=api.headers())
    assert (r.status_code, r.json()) == (404, {"error": "file"})
    assert api.llm.calls == []


# ---- actions ----


def add_body(action_id: str, draft_id: str) -> dict:
    return {"action_id": action_id, "kind": "add_draft", "draft_id": draft_id}


async def test_add_draft_twice_same_action_one_draft(api: Api):
    tid = await api.thread_row()
    pid = await api.piece_row(tid)
    body = add_body("act-1", pid)
    first = await api.act(tid, body)
    second = await api.act(tid, body)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"draft_id": "draft-1", "edit_url": "https://app.example/drafts/draft-1"}
    assert api.tools.calls.count("save_draft") == 1
    assert api.tools.drafts[0]["text"] == LAUNCH and api.tools.drafts[0]["media"] == []
    assert api.tools.drafts[0]["thread"] == [] and api.tools.drafts[0]["key"] == "act-1"
    assert api.tools.materials == []  # nothing told it: no facts to mark


async def test_add_draft_saves_a_thread(api: Api):
    tid = await api.thread_row()
    posts = ["First, the hook.", "Second, the number.", "Third, the ask."]
    pid = await api.piece_row(tid, posts=posts)
    assert (await api.act(tid, add_body("act-t", pid))).status_code == 200
    [saved] = api.tools.drafts
    assert saved["text"] == posts[0] and saved["thread"] == posts[1:]


async def test_add_draft_marks_facts_told(api: Api):
    api.llm.script = happy_script()
    events = await api.send(FACT)
    tid = of(events, "thread")[0]["thread_id"]
    [shown] = of(events, "draft")
    before = len(api.tools.materials)  # save_facts saved the fact once, with no draft
    r = await api.act(tid, add_body("act-f", shown["id"]))
    assert r.status_code == 200
    [material] = api.tools.materials[before:]
    assert material["draft_id"] == r.json()["draft_id"] == "draft-1" and material["key"] == "act-f:facts"
    assert [f.text for f in material["facts"]] == [FACT]


async def test_add_draft_sends_a_signed_image_url(api: Api):
    up = (await api.upload(PNG)).json()
    pid = await api.piece_row(up["thread_id"], image_file_id=up["file_id"])
    r = await api.act(up["thread_id"], add_body("a-img", pid))
    assert r.status_code == 200
    [media] = api.tools.drafts[0]["media"]
    assert media["alt"] == ""
    assert f"real/ws1/{up['thread_id']}/{up['file_id']}" in media["url"]
    assert "Expires=" in media["url"] or "X-Amz-Expires=300" in media["url"]


async def test_unknown_draft_404(api: Api):
    tid = await api.thread_row()
    r = await api.act(tid, add_body("a3", "nope"))
    assert (r.status_code, r.json()) == (404, {"error": "draft"})
    assert api.tools.calls == []


async def test_retry_after_tool_failure_runs_again(api: Api):
    api.llm.script = happy_script()
    events = await api.send(FACT)
    tid, shown = of(events, "thread")[0]["thread_id"], of(events, "draft")[0]
    api.tools.errors["save_material"] = ToolError("unavailable")
    body = add_body("a4", shown["id"])
    r = await api.act(tid, body)
    assert (r.status_code, r.json()) == (502, {"error": "unavailable", "message": APP_DOWN})
    del api.tools.errors["save_material"]
    r = await api.act(tid, body)
    assert r.status_code == 200 and r.json()["draft_id"] == "draft-1"
    assert api.tools.calls.count("save_draft") == 2  # the app's idempotency key keeps it one draft
    assert len(api.tools.drafts) == 1

    api.tools.errors["save_material"] = ToolError("out_of_credits", "No credits left.")
    r = await api.act(tid, add_body("a5", shown["id"]))
    assert (r.status_code, r.json()) == (402, {"error": "out_of_credits", "message": "No credits left."})


async def test_keep_facts_is_gone(api: Api):
    tid = await api.thread_row()
    r = await api.act(tid, {"action_id": "keep-1", "kind": "keep_facts", "facts": [{"text": FACT}]})
    assert r.status_code == 422
    assert api.tools.calls == []


# ---- delete ----


async def test_delete_workspace_removes_threads_files_and_storage(api: Api):
    up = (await api.upload(PNG)).json()
    tid = up["thread_id"]
    api.llm.script = happy_script()
    api.llm.add("IMAGE_READER", {"text": "a screenshot", "unsure": []})
    await api.send(FACT, thread=tid, file_ids=[up["file_id"]])
    pid = await api.piece_row(tid)
    assert (await api.act(tid, add_body("k1", pid))).status_code == 200
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
    pid = await api.piece_row(tid)
    async with api.pool.connection() as conn:
        await conn.execute(
            "insert into actions (id, app, workspace, thread_id, kind, result) values"
            " ('real:ws1:p1', 'real', 'ws1', %s, 'add_draft', 'null'::jsonb)",
            (tid,),
        )
    body = add_body("p1", pid)
    r = await api.act(tid, body)
    assert (r.status_code, r.json()["error"]) == (409, "pending")
    assert api.tools.calls == []
    async with api.pool.connection() as conn:  # the run that claimed it died long ago
        await conn.execute("update actions set created_at = now() - interval '3 minutes' where id = 'real:ws1:p1'")
    r = await api.act(tid, body)
    assert r.json() == {"draft_id": "draft-1", "edit_url": "https://app.example/drafts/draft-1"}


async def test_action_ids_are_per_workspace(api: Api):
    t1, t2 = await api.thread_row(), await api.thread_row("ws2")
    one, two = await api.piece_row(t1), await api.piece_row(t2, workspace="ws2")
    mine = await api.act(t1, add_body("same", one))
    theirs = await api.act(t2, add_body("same", two), workspace="ws2")
    assert mine.status_code == theirs.status_code == 200
    assert api.tools.calls.count("save_draft") == 2  # each workspace's own action ran


async def test_colon_ids_cannot_read_or_take_over_another_workspaces_action(api: Api):
    # "real" + "ws1:x" + "a" and "real" + "ws1" + "x:a" compose the same key "real:ws1:x:a".
    t1 = await api.thread_row("ws1:x")
    p1 = await api.piece_row(t1, workspace="ws1:x")
    assert (await api.act(t1, add_body("a", p1), workspace="ws1:x")).status_code == 200
    t2 = await api.thread_row("ws1")
    p2 = await api.piece_row(t2, posts=["another post"])
    other = add_body("x:a", p2)
    r = await api.act(t2, other, workspace="ws1")
    assert r.status_code == 409
    assert "draft_id" not in r.json()
    async with api.pool.connection() as conn:  # a stale pending row of the other workspace is not re-claimed
        await conn.execute(
            "update actions set result = 'null'::jsonb, created_at = now() - interval '3 minutes'"
            " where id = 'real:ws1:x:a'"
        )
    r = await api.act(t2, other, workspace="ws1")
    assert r.status_code == 409
    assert len(api.tools.drafts) == 1
    async with api.pool.connection() as conn:
        cur = await conn.execute("select workspace from actions where id = 'real:ws1:x:a'")
        assert (await cur.fetchone())[0] == "ws1:x"


async def test_actions_limited_to_60_an_hour_per_workspace(api: Api):
    tid = await api.thread_row()
    pid = await api.piece_row(tid)
    async with api.pool.connection() as conn:
        await conn.execute(
            "insert into counters (key, value, window_ends) values ('action:real:ws1', 59, now() + interval '1 hour')"
        )
    assert (await api.act(tid, add_body("k1", pid))).status_code == 200
    r = await api.act(tid, add_body("k2", pid))
    assert r.status_code == 429
    assert r.json() == {"error": "limit", "message": "You've done that a lot this hour. Try again a bit later."}
    assert len(api.tools.drafts) == 1
    other = await api.thread_row("ws2")
    other_piece = await api.piece_row(other, workspace="ws2")
    assert (await api.act(other, add_body("k3", other_piece), workspace="ws2")).status_code == 200


# ---- review round 1 ----


async def test_draft_event_is_stored(api: Api):
    api.llm.script = happy_script()
    events = await api.send(FACT)
    [shown] = of(events, "draft")
    async with api.pool.connection() as conn:
        cur = await conn.execute("select id, spec_kind, text, posts, image_file_id from pieces where workspace = 'ws1'")
        assert await cur.fetchall() == [(shown["id"], "x_post", LAUNCH, [LAUNCH], None)]
        cur = await conn.execute("select items from saved_lines where workspace = 'ws1'")
        assert await cur.fetchall() == [([FACT],)]


async def test_draft_not_stored_shows_it_fails_turn_and_charges_once(api: Api, monkeypatch):
    """A graph that shows a draft that can't be stored fails the turn (its Add button would never work)."""

    async def broken(*_args):
        raise RuntimeError("database went away")

    def drafting_graph(pack, llm, tools_factory, checkpointer, on_event, read_files, read_link, on_cost):
        class Graph:
            async def ainvoke(self, state, config):
                await on_cost(0.001, 1)
                await on_event("draft", {"id": "d1", "kind": "x_post", "posts": [LAUNCH]})
                return {"cost_usd": 0.001}

        return Graph()

    monkeypatch.setattr(turn, "_save_draft", broken)
    monkeypatch.setattr(turn, "build_graph", drafting_graph)
    events = await api.send(FACT)
    assert len(of(events, "draft")) == 1  # what was written is still shown
    assert of(events, "done") == []
    assert events[-1] == ("failed", {"error": "server", "message": OUR_FAULT})
    assert [c["cost_usd"] for c in api.tools.charges] == [pytest.approx(0.001)]


async def test_a_draft_added_before_its_turn_ends_still_marks_its_facts_told(api: Api, monkeypatch):
    """The draft's facts are stored with it when it is shown, not read from the checkpoint (written at the turn's end)."""
    shown, release = asyncio.Event(), asyncio.Event()
    told = {"id": "f1", "text": FACT, "numbers": [2.0], "source": "typed", "file_id": None}

    def slow_graph(pack, llm, tools_factory, checkpointer, on_event, read_files, read_link, on_cost):
        class Graph:
            async def ainvoke(self, state, config):
                draft = {"id": "d1", "kind": "x_post", "posts": [LAUNCH, "and more"], "told": [told]}
                await on_event("draft", draft)
                shown.set()
                await release.wait()
                return {"cost_usd": 0.0}

        return Graph()

    monkeypatch.setattr(turn, "build_graph", slow_graph)
    sending = asyncio.create_task(api.send(FACT))
    await asyncio.wait_for(shown.wait(), 5)
    async with api.pool.connection() as conn:
        cur = await conn.execute("select thread_id from pieces where id = 'd1'")
        [(tid,)] = await cur.fetchall()
    r = await api.act(tid, add_body("early", "d1"))
    assert r.status_code == 200
    [material] = api.tools.materials
    assert material["draft_id"] == r.json()["draft_id"] and [f.text for f in material["facts"]] == [FACT]
    assert api.tools.drafts[0]["thread"] == ["and more"]
    release.set()
    events = await sending
    [sent] = of(events, "draft")
    assert sent == {"id": "d1", "kind": "x_post", "posts": [LAUNCH, "and more"]}  # no fact fields reach the browser


async def test_the_draft_event_sent_to_the_browser_has_no_fact_fields(api: Api):
    api.llm.script = happy_script()
    events = await api.send(FACT)
    [shown] = of(events, "draft")
    assert set(shown) <= {"id", "kind", "posts", "image_file_id"}
    assert FACT not in json.dumps(shown)


async def test_message_with_draft_id_of_another_thread_is_404(api: Api):
    mine, other = await api.thread_row(), await api.thread_row()
    pid = await api.piece_row(other)
    for thread, draft in ((mine, pid), (None, pid), (mine, "nope")):
        body = {"thread_id": thread, "text": "Make it shorter", "file_ids": [], "draft_id": draft}
        r = await api.client.post("/v1/chat/real/messages", json=body, headers=api.headers())
        assert (r.status_code, r.json()) == (404, {"error": "draft"})
    r = await api.client.post(
        "/v1/chat/real/messages",
        json={"thread_id": other, "text": "x", "draft_id": pid},
        headers=api.headers("ws2"),
    )
    assert r.status_code == 404
    assert api.llm.calls == []


async def test_message_with_a_draft_id_reaches_the_graph(api: Api):
    api.llm.script = happy_script()
    first = await api.send(FACT)
    tid, [shown] = of(first, "thread")[0]["thread_id"], of(first, "draft")
    api.llm.add("CONVERSE", "Shorter it is.")
    events = await api.send("Make it shorter", thread=tid, draft_id=shown["id"])
    assert of(events, "done")
    last = api.llm.calls[-1]["messages"][-1]["content"]
    assert f"tapped a button on draft {shown['id']}" in last


async def test_thread_read_returns_drafts_without_charge_refs(api: Api):
    api.llm.script = happy_script()
    events = await api.send(FACT)
    tid, [shown] = of(events, "thread")[0]["thread_id"], of(events, "draft")
    [done] = of(events, "done")
    r = await api.client.get(f"/v1/chat/real/threads/{tid}", headers=api.headers())
    body = r.json()
    assert set(body) == {"messages"}
    user, bot = body["messages"]
    assert user == {"role": "user", "text": FACT}
    assert bot["text"] == REPLY and bot["saved"] == [FACT]
    assert bot["drafts"] == [{"id": shown["id"], "kind": "x_post", "posts": [LAUNCH], "added": False, "edit_url": None}]
    assert done["message_id"] not in r.text  # the app's charge ref never reaches the browser

    added = await api.act(tid, add_body("act-r", shown["id"]))
    body = (await api.client.get(f"/v1/chat/real/threads/{tid}", headers=api.headers())).json()
    assert body["messages"][1]["drafts"][0]["added"] is True
    assert body["messages"][1]["drafts"][0]["edit_url"] == added.json()["edit_url"]


async def test_a_turn_with_a_draft_and_no_words_still_shows_it_on_reload(api: Api):
    api.llm.script = happy_script()
    api.llm.script["CONVERSE"][1] = ""
    events = await api.send(FACT)
    tid = of(events, "thread")[0]["thread_id"]
    body = (await api.client.get(f"/v1/chat/real/threads/{tid}", headers=api.headers())).json()
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert body["messages"][1]["text"] == "" and len(body["messages"][1]["drafts"]) == 1


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

    own = logging.getLogger("assistant")  # it does not propagate to the root logger, where caplog listens
    monkeypatch.setattr(own, "handlers", [*own.handlers, caplog.handler])
    with caplog.at_level("DEBUG"):
        with monkeypatch.context() as m:
            m.setattr(chat, "owned_thread", boom)
            r = await api.client.get("/v1/chat/real/threads/t1", headers=api.headers())
        assert r.status_code == 500
        assert r.json() == {"error": "server", "message": "Something went wrong on our side."}
        assert r.headers["access-control-allow-origin"] == ORIGIN

        # an action whose tool raises something unexpected frees its claim and answers in JSON too
        tid = await api.thread_row()
        body = add_body("x1", await api.piece_row(tid))
        api.tools.errors["save_draft"] = ValueError(secret_text)
        r = await api.act(tid, body)
        assert (r.status_code, r.json()["error"]) == (500, "server")
        del api.tools.errors["save_draft"]
        assert (await api.act(tid, body)).json()["draft_id"] == "draft-1"
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
    assert Settings.model_fields["shutdown_drain_seconds"].default == 0.4
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
    r = await api.act(tid, add_body("a-x", pid))
    assert r.status_code == 200
    assert api.tools.drafts[0]["media"] == []


# ---- status events ----


def kinds_of(events: list[tuple[str, dict]]) -> list[str]:
    return [k for k, _ in events]


async def test_status_reading_comes_before_any_text_for_a_file(api: Api):
    up = await api.upload(PNG)
    tid, fid = up.json()["thread_id"], up.json()["file_id"]
    api.llm.script = {
        "IMAGE_READER": [Out({"text": "40 accounts on", "unsure": []}, cost=0.01)],
        **ask_script(),
    }
    events = await api.send("here is the screenshot", thread=tid, file_ids=[fid])
    assert ("status", {"step": "reading"}) in events
    first_text = kinds_of(events).index("text")
    assert events.index(("status", {"step": "reading"})) < first_text
    assert ("status", {"step": "listening"}) not in events


async def test_status_listening_for_a_voice_note(api: Api):
    voice = (Path(__file__).parent / "fixtures" / "voice.m4a").read_bytes()
    up = await api.upload(voice, mime="audio/mp4")
    assert up.status_code == 200, up.text
    tid, fid = up.json()["thread_id"], up.json()["file_id"]
    api.llm.script = {"TRANSCRIBE": [("we shipped on friday", 4.0)], **ask_script()}
    events = await api.send("", thread=tid, file_ids=[fid])
    assert ("status", {"step": "listening"}) in events
    assert ("status", {"step": "reading"}) not in events
    assert events.index(("status", {"step": "listening"})) < kinds_of(events).index("transcript")


async def test_status_steps_come_in_order_before_the_draft(api: Api):
    api.llm.script = happy_script()
    events = await api.send(FACT)
    steps = [p["step"] for k, p in events if k == "status"]
    assert steps == ["thinking", "writing", "checking"]
    assert max(i for i, (k, _) in enumerate(events) if k == "status") < kinds_of(events).index("draft")
    assert events[-1][0] == "done"


# A file the user sent, shown back in the host app's chat (HookPlaybook's Write with me, 21 Nov 2026: a message's
# pictures were never shown): a 5-minute link, for the user's own finished file in that thread only.
async def test_a_sent_file_has_a_short_lived_link_for_its_own_thread(api: Api):
    up = (await api.upload(PNG)).json()
    r = await api.client.get(f"/v1/chat/real/threads/{up['thread_id']}/files/{up['file_id']}", headers=api.headers())
    assert r.status_code == 200
    body = r.json()
    assert body["mime"] == "image/png"
    assert body["kind"] == "image"
    assert body["url"].startswith("http")


async def test_a_file_link_is_404_for_another_thread_workspace_or_an_unfinished_upload(api: Api):
    up = (await api.upload(PNG)).json()
    other = await api.thread_row()
    for thread, workspace in [(other, "ws1"), (up["thread_id"], "ws2")]:
        r = await api.client.get(
            f"/v1/chat/real/threads/{thread}/files/{up['file_id']}", headers=api.headers(workspace)
        )
        assert r.status_code == 404, (thread, workspace)
    pending = (await api.start_upload(len(PNG))).json()
    r = await api.client.get(
        f"/v1/chat/real/threads/{pending['thread_id']}/files/{pending['upload_id']}", headers=api.headers()
    )
    assert r.status_code == 404
    assert r.json() == {"error": "file"}


# A reloaded chat shows what each message sent (the widget, 21 Nov 2026: a picture showed only until the page reloaded,
# and a voice note never played): the thread lists each user message's files, by id and kind.
async def test_a_reloaded_thread_lists_each_messages_files(api: Api):
    up = (await api.upload(PNG)).json()
    tid = up["thread_id"]
    api.llm.script = happy_script()
    api.llm.add("IMAGE_READER", {"text": "a screenshot", "unsure": []})
    await api.send(FACT, thread=tid, file_ids=[up["file_id"]])
    r = await api.client.get(f"/v1/chat/real/threads/{tid}", headers=api.headers())
    users = [m for m in r.json()["messages"] if m["role"] == "user"]
    assert users[0]["files"] == [{"id": up["file_id"], "kind": "image"}]
