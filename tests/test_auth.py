import time
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from assistant.auth import AuthError, Session, mint_session, verify_session
from assistant.packs import AppPack

SECRET = "s" * 64


def _pack(pack_id: str = "demo", secret_env: str = "TEST_AUTH_SECRET") -> AppPack:
    return AppPack.model_validate(
        {
            "id": pack_id,
            "name": "Demo",
            "mcp_url": "http://x/mcp",
            "secret_env": secret_env,
            "allowed_origins": ["http://x"],
            "interview_guide": "Ask.",
            "writes": [{"kind": "changelog", "count": 1, "instructions": "Write."}],
            "tools": {
                "context": "assistant_context",
                "save_draft": "save_draft",
                "save_material": "save_material",
                "charge": "assistant_charge",
            },
            "models": {"main": "a/m", "cheap": "a/c", "vision": "a/v"},
            "message_usd_cap": 0.1,
        }
    )


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("TEST_AUTH_SECRET", SECRET)
    monkeypatch.setenv("TEST_OTHER_SECRET", "o" * 64)


def _raw(claims: dict, secret: str = SECRET) -> str:
    return jwt.encode(claims, secret, algorithm="HS256")


def _claims(**over) -> dict:
    now = int(time.time())
    base = {"iss": "demo", "sub": "ws-1", "access": "draft", "iat": now, "exp": now + 600}
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def _reason(token: str, pack: AppPack) -> str:
    with pytest.raises(AuthError) as err:
        verify_session(token, pack)
    assert str(err.value) == err.value.reason
    return err.value.reason


def test_round_trip():
    pack = _pack()
    token = mint_session(pack, "ws-1")
    session = verify_session(token, pack)
    assert isinstance(session, Session)
    assert (session.app, session.workspace, session.access, session.token) == ("demo", "ws-1", "draft", token)
    assert session.expires_at.tzinfo is not None
    assert session.expires_at.utcoffset() == timedelta(0)
    assert abs(session.expires_at - (datetime.now(UTC) + timedelta(hours=1))) < timedelta(seconds=5)


def test_expired_is_expired():
    pack = _pack()
    assert _reason(mint_session(pack, "ws-1", ttl=timedelta(seconds=-1)), pack) == "expired"


def test_other_app_secret_is_invalid():
    other = _pack(secret_env="TEST_OTHER_SECRET")
    assert _reason(mint_session(other, "ws-1"), _pack()) == "invalid"


def test_iss_mismatch_is_wrong_app():
    assert _reason(_raw(_claims(iss="other-app")), _pack()) == "wrong_app"


def test_access_schedule_is_wrong_access():
    assert _reason(_raw(_claims(access="schedule")), _pack()) == "wrong_access"


def test_ttl_over_one_hour_rejected():
    now = int(time.time())
    assert _reason(_raw(_claims(iat=now, exp=now + 3601)), _pack()) == "invalid"


def test_ttl_of_exactly_one_hour_is_accepted():
    now = int(time.time())
    assert verify_session(_raw(_claims(iat=now, exp=now + 3600)), _pack()).workspace == "ws-1"


def test_mint_refuses_ttl_over_one_hour():
    with pytest.raises(ValueError):
        mint_session(_pack(), "ws-1", ttl=timedelta(hours=1, seconds=1))


@pytest.mark.parametrize("missing", ["exp", "iat", "iss", "sub"])
def test_missing_required_claim_is_invalid(missing):
    assert _reason(_raw(_claims(**{missing: None})), _pack()) == "invalid"


def test_missing_access_is_wrong_access():
    assert _reason(_raw(_claims(access=None)), _pack()) == "wrong_access"


def test_garbage_and_none_algorithm_are_invalid():
    pack = _pack()
    assert _reason("not-a-token", pack) == "invalid"
    unsigned = jwt.encode(_claims(), None, algorithm="none")
    assert _reason(unsigned, pack) == "invalid"


def test_other_hmac_algorithm_is_invalid():
    assert _reason(jwt.encode(_claims(), SECRET, algorithm="HS512"), _pack()) == "invalid"


def test_non_string_sub_is_invalid():
    assert _reason(_raw(_claims(sub=123)), _pack()) == "invalid"
