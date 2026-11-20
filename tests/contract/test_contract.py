"""The bot's side of contract 1 (docs/integration.md): every scenario in scenarios.json, sent by `RestHttpTools` to
the fake app (fake_app.py, built from the doc only), and one whole turn and Add through the chat API."""

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import jwt
import pytest
from chat_api import FACT, LAUNCH, PNG, SECRET, Api, happy_script, make_pack
from fake_app import FakeApp

from assistant.auth import Session
from assistant.checks import Fact
from assistant.pack_store import PackStore
from assistant.packs import AppPack, Models
from assistant.tools import TIMEOUT_SECONDS, RestHttpTools, ToolError, tools_for

SCENARIOS = json.loads((Path(__file__).parent / "scenarios.json").read_text())
APP_ID = "shipnote"
APP_SECRET = "s" * 64
WORKSPACE = "ws_1"
SITE = "https://app.example"
API_BASE = f"{SITE}/api/assistant"


def token(kind: str = "valid") -> str:
    now = int(time.time())
    iat, exp = (now - 7200, now - 3600) if kind == "expired" else (now, now + 600)
    secret = "o" * 64 if kind == "wrong_secret" else APP_SECRET
    claims = {"iss": APP_ID, "sub": WORKSPACE, "access": "draft", "iat": iat, "exp": exp}
    return jwt.encode(claims, secret, algorithm="HS256")


def rest_pack() -> AppPack:
    return AppPack(
        id=APP_ID,
        name="Shipnote",
        api_base=API_BASE,
        secret_env="UNUSED",
        allowed_origins=[SITE],
        guide="",
        writes=[],
        models=Models(chat="c", writer="w", main="m", cheap="c", vision="v"),
        message_usd_cap=0.1,
    )


def reply(status: int, body) -> httpx.Response:
    if isinstance(body, str):
        headers = {"content-type": "text/html"} | ({"location": f"{SITE}/sign-in"} if 300 <= status < 400 else {})
        return httpx.Response(status, text=body, headers=headers)
    return httpx.Response(status, json=body)


class Wire(httpx.AsyncBaseTransport):
    """Between the bot and the fake app: records every request, plays the scenario's faults, and answers for an app
    that breaks the contract (`setup.broken`)."""

    def __init__(self, scenario: dict, app: FakeApp):
        self.scenario = scenario
        self.inner = httpx.ASGITransport(app=app.app)
        self.faults = list(scenario["setup"].get("faults", []))
        self.sent: list[httpx.Request] = []
        self.answered: list[httpx.Response] = []  # the fake app's own answers

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.sent.append(request)
        attempt = len(self.sent)
        if attempt <= len(self.faults):
            fault = self.faults[attempt - 1]
            if fault.get("timeout"):
                raise httpx.ReadTimeout("no answer", request=request)
            if fault.get("unreachable"):
                raise httpx.ConnectError("connection refused", request=request)
            return reply(fault["status"], fault.get("body"))
        if self.scenario["setup"].get("broken"):
            return reply(self.scenario["response_status"], self.scenario["response_body"])
        response = await self.inner.handle_async_request(request)
        content = await response.aread()
        answer = httpx.Response(response.status_code, headers=response.headers, content=content)
        self.answered.append(answer)
        return answer


async def run_call(tools: RestHttpTools, call: dict):
    args = call["args"]
    match call["method"]:
        case "context":
            return (await tools.context()).model_dump()
        case "save_draft":
            saved = await tools.save_draft(args["text"], args["thread"], args["media"], args["key"])
            return saved.model_dump()
        case "save_material":
            facts = [Fact(text=text) for text in args["facts"]]
            return await tools.save_material(facts, args["draft_id"], args["key"])
        case "charge":
            return await tools.charge(args["cost_usd"], args["ref"])
    raise AssertionError(f"unknown call {call['method']}")


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["name"] for s in SCENARIOS])
async def test_scenario(scenario: dict):
    setup = scenario["setup"]
    tok = token(setup.get("token", "valid"))
    app = FakeApp(
        APP_ID,
        APP_SECRET,
        context=setup.get("context"),
        credits_left=setup.get("credits_left"),
        done=setup.get("done", []),
        in_flight=setup.get("in_flight", []),
        workspace=WORKSPACE,
    )
    wire = Wire(scenario, app)
    session = Session(app=APP_ID, workspace=WORKSPACE, access="draft", token=tok, expires_at=datetime.now(UTC))
    expect = scenario["expect"]
    async with httpx.AsyncClient(transport=wire) as client:
        tools = RestHttpTools(rest_pack(), session, client)
        if "returns" in expect:
            assert await run_call(tools, scenario["call"]) == expect["returns"]
        else:
            with pytest.raises(ToolError) as e:
                await run_call(tools, scenario["call"])
            assert (e.value.kind, e.value.user_message) == (expect["error"], expect["message"])
            assert tok not in str(e.value)

    # What the bot sent, on every attempt (a retry is the same request with the same key).
    want = scenario["request"]
    assert len(wire.sent) == expect.get("attempts", 1)
    for sent in wire.sent:
        assert sent.method == want["method"]
        assert str(sent.url) == API_BASE + want["path"]
        for name, value in want["headers"].items():
            if value is None:
                assert name not in sent.headers, name
            else:
                assert sent.headers.get(name) == value.replace("<token>", tok), name
        assert json.loads(sent.content) == want["body"]
        assert sent.extensions["timeout"]["read"] == TIMEOUT_SECONDS

    # What an app that follows the doc answers: the fake app's own answer is the scenario's.
    for answer in wire.answered:
        assert answer.status_code == scenario["response_status"]
        assert answer.json() == scenario["response_body"]


def test_scenarios_are_well_formed():
    keys = {"name", "setup", "call", "request", "response_status", "response_body", "expect"}
    calls = {"context": "/context", "save_draft": "/drafts", "save_material": "/material", "charge": "/charge"}
    for s in SCENARIOS:
        assert set(s) == keys, s["name"]
        assert calls[s["call"]["method"]] == s["request"]["path"], s["name"]
        keyed = s["request"]["path"] != "/context"
        assert (s["request"]["headers"]["idempotency-key"] is not None) == keyed, s["name"]
        assert ("returns" in s["expect"]) != ("error" in s["expect"]), s["name"]
    assert len({s["name"] for s in SCENARIOS}) == len(SCENARIOS)


# ---- a whole turn, then Add, with a pack that has api_base ----


class Recording(httpx.ASGITransport):
    def __init__(self, app):
        super().__init__(app=app)
        self.paths: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path.removeprefix("/api/assistant"))
        return await super().handle_async_request(request)


async def test_a_turn_and_an_add_go_through_the_four_endpoints(api: Api):
    fake = FakeApp("real", SECRET, context={"language": "en", "voice": "Plain.", "credits_left": 5}, credits_left=5)
    pack = AppPack.model_validate(
        {**make_pack().model_dump(), "mcp_url": None, "tools": None, "api_base": f"{SITE}/api/assistant"}
    )
    api.app.state.packs = PackStore({"real": pack}, api.app.state.settings)
    made: list = []
    wire = Recording(fake.app)
    async with httpx.AsyncClient(transport=wire) as client:

        def factory(p, s):
            made.append(tools_for(p, s, client))
            return made[-1]

        api.app.state.tools_for = factory
        api.llm.script = happy_script()
        events = await api.send(FACT)
        kinds = [k for k, _ in events]
        assert kinds[-1] == "done", events
        tid = next(p for k, p in events if k == "thread")["thread_id"]
        shown = next(p for k, p in events if k == "draft")
        r = await api.act(tid, {"action_id": "act-1", "kind": "add_draft", "draft_id": shown["id"]})
        assert r.status_code == 200, r.text

        up = (await api.upload(PNG, thread=tid)).json()
        pid = await api.piece_row(tid, image_file_id=up["file_id"])
        r2 = await api.act(tid, {"action_id": "act-2", "kind": "add_draft", "draft_id": pid})
        assert r2.status_code == 200, r2.text

    assert isinstance(made[0], RestHttpTools)
    assert wire.paths[0] == "/context"  # the turn starts by reading the context
    assert r.json() == {"draft_id": "d_1", "edit_url": "https://app.example/drafts/d_1"}
    assert fake.drafts[0] == {"workspace": "ws1", "id": "d_1", "text": LAUNCH, "thread": [], "media": []}
    [media] = fake.drafts[1]["media"]
    assert media["mime"] == "image/png" and media["alt"] == ""
    assert f"real/ws1/{tid}/{up['file_id']}" in media["url"]
    # Each model call charged once, by its own ref; together they are what the turn reported.
    done = next(p for k, p in events if k == "done")
    refs = [c["ref"] for c in fake.charges]
    assert len(refs) == len(set(refs)) == 4 and all(ref.startswith(done["message_id"] + ":") for ref in refs)
    assert sum(c["cost_usd"] for c in fake.charges) == pytest.approx(done["cost_usd"])
    # The fact was kept during the turn (no draft yet), then with the draft on Add.
    assert [(m["facts"], m["draft_id"]) for m in fake.materials] == [([FACT], None), ([FACT], "d_1")]
    # The turn's calls to the app were timed, named by endpoint.
    spans = [s["name"] for s in made[0].timer.summary()["tool"]]
    assert spans[0] == "context" and spans.count("charge") == 4
