"""Tokens cross-checked between the bot and the JS package (sdk/js, `@app-assistant/host`).

Each side pins its own fixture and checks the other's, at the fixture's fixed time:
- `bot-token.json` is what the bot's `mint_session` makes; the package's vitest verifies it with `verifyToken`.
  This test fails if `mint_session` would now make another token; `UPDATE_SDK_FIXTURES=1` rewrites the file.
- `package-token.json` is what the package's `signToken` makes (its vitest pins it); here the bot checks it with
  `verify_session` and `verify_delete_token`.
HS256 is deterministic, so a fixed secret and a fixed clock give the same token every run.
"""

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import jwt.api_jwt
import pytest

from assistant import auth
from assistant.auth import AuthError, mint_session, verify_delete_token, verify_session

FIXTURES = Path(__file__).resolve().parents[2] / "sdk" / "js" / "test" / "fixtures"
SECRET = "fixture-secret-not-real-00000000000000000000000000000000000000"  # obviously fake, for these files only
APP_ID = "shipnote"
WORKSPACE = "ws_42"
NOW = 1795000000


@dataclass(frozen=True)
class Key:
    id: str
    shared: str

    def secret(self) -> str:
        return self.shared


def freeze(monkeypatch: pytest.MonkeyPatch, now: float) -> None:
    """The bot's clock (auth's `datetime`/`time`, and PyJWT's own `iat` check) stopped at `now`."""

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(now, tz)

    monkeypatch.setattr(auth, "datetime", Frozen)
    monkeypatch.setattr(auth, "time", SimpleNamespace(time=lambda: now))
    monkeypatch.setattr(jwt.api_jwt, "datetime", Frozen)


def test_bot_token_fixture_is_what_mint_session_makes(monkeypatch: pytest.MonkeyPatch) -> None:
    freeze(monkeypatch, NOW)
    token = mint_session(Key(APP_ID, SECRET), WORKSPACE, timedelta(hours=1))
    made = {
        "note": "Made by tests/contract/test_sdk_token_fixture.py from the bot's mint_session. The secret is fake.",
        "secret": SECRET,
        "app_id": APP_ID,
        "workspace": WORKSPACE,
        "now": NOW,
        "claims": jwt.decode(token, options={"verify_signature": False}),
        "token": token,
    }
    path = FIXTURES / "bot-token.json"
    if os.environ.get("UPDATE_SDK_FIXTURES"):
        path.write_text(json.dumps(made, indent=2) + "\n")
    assert path.exists(), "run once with UPDATE_SDK_FIXTURES=1 to write it"
    assert json.loads(path.read_text()) == made
    assert made["claims"] == {"iss": APP_ID, "sub": WORKSPACE, "access": "draft", "iat": NOW, "exp": NOW + 3600}


def test_package_tokens_verify_in_the_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = json.loads((FIXTURES / "package-token.json").read_text())
    key = Key(fixture["app_id"], fixture["secret"])
    freeze(monkeypatch, fixture["now"] + 5)

    session = verify_session(fixture["draft"], key)
    assert session.app == fixture["app_id"]
    assert session.workspace == fixture["workspace"]
    assert session.expires_at == datetime.fromtimestamp(fixture["now"] + fixture["ttl_seconds"], UTC)
    assert verify_delete_token(fixture["delete"], key) == fixture["workspace"]

    with pytest.raises(AuthError) as wrong:
        verify_session(fixture["delete"], key)
    assert wrong.value.reason == "wrong_access"
    with pytest.raises(AuthError) as other:
        verify_session(fixture["draft"], Key(fixture["app_id"], "another-secret-not-real-" + "0" * 40))
    assert other.value.reason == "invalid"

    freeze(monkeypatch, fixture["now"] + fixture["ttl_seconds"] + 1)
    with pytest.raises(AuthError) as late:
        verify_session(fixture["draft"], key)
    assert late.value.reason == "expired"
