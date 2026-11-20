"""The bot's own demo is an app of contract 1: the scenarios an app can check on its own (docs/integration.md,
"Checking your app"), sent to the demo's endpoints. The scenarios that need a state the demo does not have (a profile
of its own, credits, a key half done) are left out: the demo's context, charge and keys are fixed by its design."""

import time
from pathlib import Path

import jwt
import pytest
from chat_api import SECRET, Api
from test_contract import SCENARIOS, WORKSPACE, matches

from assistant.packs import load_packs

pytestmark = pytest.mark.db

STATELESS = {"token", "app_only"}
ALSO = {"charge: taken from the credits"}
SCENARIOS_FOR_DEMO = [
    s
    for s in SCENARIOS
    if s["match"] is not None
    and not s["request"]["body"].get("media")
    and (set(s["setup"]) <= STATELESS or s["name"] in ALSO)
]


def demo_token(kind: str) -> str:
    now = int(time.time())
    iat, exp = {"expired": (now - 7200, now - 3600), "too_long": (now, now + 3700)}.get(kind, (now, now + 600))
    claims = {
        "iss": "another-app" if kind == "wrong_iss" else "demo",
        "sub": WORKSPACE,
        "access": "delete" if kind == "delete_access" else "draft",
        "iat": iat,
        "exp": exp,
    }
    return jwt.encode(claims, "o" * 64 if kind == "wrong_secret" else SECRET, algorithm="HS256")


def test_the_demo_runs_a_fair_share_of_the_scenarios():
    assert len(SCENARIOS_FOR_DEMO) >= 14


@pytest.mark.parametrize("scenario", SCENARIOS_FOR_DEMO, ids=[s["name"] for s in SCENARIOS_FOR_DEMO])
async def test_the_demo_answers_as_the_contract_says(api: Api, monkeypatch, scenario: dict):
    monkeypatch.setenv("DEMO_SESSION_SECRET", SECRET)
    pack = load_packs(Path("apps"), base_url="http://testserver")["demo"]
    api.app.state.packs = type(api.app.state.packs)({"demo": pack}, api.app.state.settings)
    want = scenario["request"]
    tok = demo_token(scenario["setup"].get("token", "valid"))
    headers = {k: v.replace("<token>", tok) for k, v in want["headers"].items() if v is not None}
    answer = await api.client.request(
        want["method"], "/demo/api/assistant" + want["path"], json=want["body"], headers=headers
    )
    assert answer.status_code == scenario["response_status"], answer.text
    assert matches(answer.json(), scenario["match"]), (answer.json(), scenario["match"])
