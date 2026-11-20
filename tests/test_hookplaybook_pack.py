"""HookPlaybook's pack (apps/hookplaybook.yaml): its values, its tokens as HookPlaybook mints them, its origin and its
workspace deletion. HookPlaybook's side lives in ai-mono (products/hookplaybook)."""

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fakes import default_context
from test_api_chat import PNG, Api, api, parse_sse  # noqa: F401 - `api` is the fixture

from assistant import auth
from assistant.auth import AuthError, verify_delete_token, verify_session
from assistant.graph.build import thread_id
from assistant.packs import load_packs
from assistant.tools import AppContext

APPS = Path("apps")
SITE = "https://hookplaybook.com"
SECRET_ENV = "HOOKPLAYBOOK_SESSION_SECRET"
# HookPlaybook's own test vector (shared/core/test/assistant-token.test.ts): secret "s", iat 1700000000, a draft
# token for workspace w1 living an hour.
VECTOR = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJob29rcGxheWJvb2siLCJzdWIiOiJ3MSIsImFjY2VzcyI6ImRyYWZ0IiwiaWF0"
    "IjoxNzAwMDAwMDAwLCJleHAiOjE3MDAwMDM2MDB9.mziVF0uIL9cWwnniIulBLSgZXpOVadZj-nZVatfHPd4"
)
VECTOR_NOW = 1700000100


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def hookplaybook_token(secret: str, sub: str, access: str, ttl: int = 3600, now: int | None = None) -> str:
    """A token made the way HookPlaybook's signAssistantToken makes it (shared/core/src/assistant-token.ts)."""
    now = int(time.time()) if now is None else now
    compact = lambda o: json.dumps(o, separators=(",", ":")).encode()  # JSON.stringify's compact form
    header = _b64url(compact({"alg": "HS256", "typ": "JWT"}))
    payload = _b64url(compact({"iss": "hookplaybook", "sub": sub, "access": access, "iat": now, "exp": now + ttl}))
    body = f"{header}.{payload}"
    return f"{body}.{_b64url(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())}"


def pack():
    return load_packs(APPS, base_url="http://testserver")["hookplaybook"]


# ---- the pack ----


def test_pack_values():
    p = pack()
    assert (p.id, p.name) == ("hookplaybook", "HookPlaybook")
    assert p.mcp_url == "https://hookplaybook.com/api/v1/mcp"
    assert p.secret_env == SECRET_ENV
    assert p.allowed_origins == [SITE]
    assert p.tools == {
        "context": "assistant_context",
        "save_draft": "save_draft",
        "save_material": "save_material",
        "charge": "assistant_charge",
    }
    [post] = p.writes  # no x_thread in Phase 2
    assert (post.kind, post.count, post.max_chars) == ("x_post", 2, 280)
    assert post.no_links and post.no_dashes and post.may_carry_image
    for words in ["voice", "hook", "one point", "plain words"]:
        assert words in post.instructions, words
    assert p.files.model_dump() == {
        "image_mb": 5,
        "pdf_mb": 20,
        "pdf_pages": 30,
        "voice_seconds": 300,
        "per_message": 5,
        "kinds": ["image", "pdf", "voice"],
    }
    assert p.message_usd_cap == 0.10
    assert p.demo is False
    demo = load_packs(APPS, base_url="http://testserver")["demo"]
    assert p.models == demo.models
    for topic in ["what happened", "who it helps", "a number or result", "what they learned", "screenshot"]:
        assert topic in p.interview_guide, topic


def test_pack_never_names_create_draft():
    # HookPlaybook's public create_draft takes no pictures: the bot saves through save_draft only.
    assert "create_draft" not in (APPS / "hookplaybook.yaml").read_text()
    assert "create_draft" not in pack().tools.values()
    source = Path("src/assistant")
    for path in [*source.rglob("*.py"), *source.rglob("*.yaml")]:
        assert "create_draft" not in path.read_text(), path


# ---- tokens ----


def test_hookplaybook_token_algorithm_matches_its_vector():
    assert hookplaybook_token("s", "w1", "draft", 3600, now=1700000000) == VECTOR


@pytest.mark.filterwarnings("ignore::jwt.warnings.InsecureKeyLengthWarning")  # the vector's secret is "s"
def test_hookplaybooks_token_verifies_with_this_packs_secret(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(SECRET_ENV, "s")
    monkeypatch.setattr(auth, "time", SimpleNamespace(time=lambda: VECTOR_NOW))
    session = verify_session(VECTOR, pack())
    assert (session.app, session.workspace, session.access, session.token) == ("hookplaybook", "w1", "draft", VECTOR)
    assert int(session.expires_at.timestamp()) == 1700003600

    monkeypatch.setenv(SECRET_ENV, "another")
    with pytest.raises(AuthError, match="invalid"):
        verify_session(VECTOR, pack())


def test_hookplaybooks_delete_token_verifies(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(SECRET_ENV, "x" * 64)
    token = hookplaybook_token("x" * 64, "ws_hp", "delete", ttl=300)
    assert verify_delete_token(token, pack()) == "ws_hp"
    with pytest.raises(AuthError, match="wrong_access"):
        verify_session(token, pack())


def test_context_reads_hookplaybooks_kept_facts():
    # assistant_context's answer, as HookPlaybook gives it (shared/core/src/assistant.ts).
    answer = {
        "voice": "Plain and direct.",
        "topics": ["indie hacking"],
        "recent_posts": ["Shipped billing today."],
        "patterns": ["Before/after (3.1× the author's usual reach)"],
        "credits_left": 420,
        "out_of_credits_message": "You're out of credits.",
        "language": "en",
        "kept_facts": [{"id": "t1", "text": "We hit 120 paying users in March"}],
    }
    ctx = AppContext.model_validate(answer)
    assert [(k.id, k.text) for k in ctx.kept_facts] == [("t1", "We hit 120 paying users in March")]
    assert default_context().kept_facts == []


# ---- through the API ----

SECRET = "h" * 64


@pytest.fixture
def hp(api: Api, monkeypatch: pytest.MonkeyPatch) -> Api:  # noqa: F811 - `api` is the imported fixture
    monkeypatch.setenv(SECRET_ENV, SECRET)
    api.app.state.packs["hookplaybook"] = pack()
    return api


def _widget(workspace: str) -> dict[str, str]:
    """Headers as the widget sends them from HookPlaybook's dashboard: the bearer token, no cookie."""
    return {"Authorization": f"Bearer {hookplaybook_token(SECRET, workspace, 'draft')}", "Origin": SITE}


@pytest.mark.db
async def test_cors_lets_hookplaybooks_dashboard_in_without_credentials(hp: Api):
    preflight = {
        "Origin": SITE,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    }
    r = await hp.client.options("/v1/chat/hookplaybook/messages", headers=preflight)
    assert r.status_code == 204
    assert r.headers["access-control-allow-origin"] == SITE
    assert r.headers["access-control-allow-headers"] == "Authorization, Content-Type"
    assert "access-control-allow-credentials" not in r.headers  # the widget sends a bearer token, never a cookie

    r = await hp.client.get("/v1/chat/hookplaybook/threads", headers=_widget("ws_hp"))
    assert r.status_code == 200 and r.json() == []
    assert r.headers["access-control-allow-origin"] == SITE
    assert "access-control-allow-credentials" not in r.headers

    for other in ["https://www.hookplaybook.com", "http://hookplaybook.com", "https://app.example"]:
        r = await hp.client.options("/v1/chat/hookplaybook/messages", headers={**preflight, "Origin": other})
        assert r.status_code == 403, other
        r = await hp.client.get("/v1/chat/hookplaybook/threads", headers={**_widget("ws_hp"), "Origin": other})
        assert (r.status_code, r.json()) == (403, {"error": "origin"}), other


@pytest.mark.db
async def test_delete_as_hookplaybook_calls_it_removes_chats_facts_and_files(hp: Api):
    fact = "We hit 120 paying users in March"
    post = "120 paying users in March. Here is what worked."

    async def chat(workspace: str) -> str:
        files = {"file": ("shot.png", PNG, "image/png")}
        r = await hp.client.post("/v1/chat/hookplaybook/files", files=files, headers=_widget(workspace))
        assert r.status_code == 200, r.text
        up = r.json()
        hp.llm.add("IMAGE_READER", {"text": "a chart", "unsure": []})
        hp.llm.add("INTERVIEW", {"enough": True, "just_write": False, "question": None})
        hp.llm.add("EXTRACT_FACTS", {"facts": [{"text": fact, "source": "typed", "file_id": None}]})
        x_post = {"spec_kind": "x_post", "text": post, "fact_ids": ["F1"], "image_file_id": None}
        hp.llm.add("WRITER", {"pieces": [x_post]})
        hp.llm.add("CLAIM_CHECK", {"unsupported": []})
        body = {"thread_id": up["thread_id"], "text": fact, "file_ids": [up["file_id"]]}
        r = await hp.client.post("/v1/chat/hookplaybook/messages", json=body, headers=_widget(workspace))
        events = parse_sse(r.text)
        assert [k for k, _ in events][-1] == "done", events
        keep = {"action_id": f"keep-{workspace}", "kind": "keep_facts", "facts": [{"text": fact}]}
        url = f"/v1/chat/hookplaybook/threads/{up['thread_id']}/actions"
        assert (await hp.client.post(url, json=keep, headers=_widget(workspace))).status_code == 200
        return up["thread_id"]

    hp.tools._context = default_context(credits_left=100)
    gone, kept = await chat("ws_hp"), await chat("ws_other")
    config = {"configurable": {"thread_id": thread_id("hookplaybook", "ws_hp", gone)}}
    assert await hp.checkpointer.aget_tuple(config) is not None

    # As HookPlaybook's forgetAssistantData calls it (app/lib/assistant.ts): a delete token living 300 s, no Origin.
    token = hookplaybook_token(SECRET, "ws_hp", "delete", ttl=300)
    r = await hp.client.delete("/v1/apps/hookplaybook/workspaces/ws_hp", headers={"authorization": f"Bearer {token}"})
    assert (r.status_code, r.json()) == (200, {"deleted_threads": 1})

    async with hp.pool.connection() as conn:
        for table in ["threads", "files", "pieces", "actions"]:
            cur = await conn.execute(
                f"select workspace, count(*) from {table} where app = 'hookplaybook' group by workspace"
            )
            assert dict(await cur.fetchall()) == {"ws_other": 1}, table
    assert await hp.checkpointer.aget_tuple(config) is None  # the chat's facts lived in its checkpoints
    other = {"configurable": {"thread_id": thread_id("hookplaybook", "ws_other", kept)}}
    assert await hp.checkpointer.aget_tuple(other) is not None
    listed = hp.storage._client.list_objects_v2(Bucket=hp.storage.bucket)
    keys = [o["Key"] for o in listed.get("Contents", [])]
    assert len(keys) == 1 and keys[0].startswith(f"hookplaybook/ws_other/{kept}/")

    # The token deletes only its own workspace, and a draft token deletes nothing.
    r = await hp.client.delete(
        "/v1/apps/hookplaybook/workspaces/ws_other", headers={"authorization": f"Bearer {token}"}
    )
    assert (r.status_code, r.json()) == (403, {"error": "workspace"})
    draft = hookplaybook_token(SECRET, "ws_other", "draft")
    r = await hp.client.delete(
        "/v1/apps/hookplaybook/workspaces/ws_other", headers={"authorization": f"Bearer {draft}"}
    )
    assert (r.status_code, r.json()) == (401, {"error": "wrong_access"})
