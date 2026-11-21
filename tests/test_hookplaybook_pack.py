"""HookPlaybook's pack (apps/hookplaybook.yaml): its values, its tokens as HookPlaybook mints them, its origin and its
workspace deletion. HookPlaybook's own side is its own (closed) code; these tests hold the bot to what it sends."""

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml
from chat_api import PNG, Api, parse_sse
from fakes import Reply, call, default_context

from assistant import auth
from assistant.auth import AuthError, verify_delete_token, verify_session
from assistant.db import migrate
from assistant.graph.build import thread_id
from assistant.pack_store import PackStore, RegistryEntry, load_registry, registry_path
from assistant.packs import load_packs
from assistant.tools import AppContext
from assistant.tools import tools_for as real_tools_for

APPS = Path("apps")
SITE = "https://hookplaybook.com"
SECRET_ENV = "HOOKPLAYBOOK_SESSION_SECRET"
# HookPlaybook's own test vector (from its token tests): secret "s", iat 1700000000, a draft
# token for workspace w1 living an hour.
VECTOR = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJob29rcGxheWJvb2siLCJzdWIiOiJ3MSIsImFjY2VzcyI6ImRyYWZ0IiwiaWF0"
    "IjoxNzAwMDAwMDAwLCJleHAiOjE3MDAwMDM2MDB9.mziVF0uIL9cWwnniIulBLSgZXpOVadZj-nZVatfHPd4"
)
VECTOR_NOW = 1700000100


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def hookplaybook_token(secret: str, sub: str, access: str, ttl: int = 3600, now: int | None = None) -> str:
    """A token made the way HookPlaybook makes it."""
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
    post, thread = p.writes
    assert (post.kind, post.count, post.max_chars) == ("x_post", 1, 280)
    assert (thread.kind, thread.count, thread.max_chars, thread.max_posts) == ("x_thread", 1, 280, 8)
    assert thread.no_links and thread.no_dashes and thread.may_carry_image
    assert post.no_links and post.no_dashes and post.may_carry_image
    for words in ["voice", "hook", "one point", "plain words"]:
        assert words in post.instructions, words
    assert p.files.model_dump() == {
        "image_mb": 5,
        "pdf_mb": 20,
        "pdf_pages": 10,
        "voice_seconds": 300,
        "per_message": 5,
        "kinds": ["image", "pdf", "voice"],
    }
    assert p.message_usd_cap == 0.10
    assert p.demo is False
    demo = load_packs(APPS, base_url="http://testserver")["demo"]
    assert p.models == demo.models
    for topic in ["what happened", "who it helps", "number or result", "learned", "screenshot", "Never invent"]:
        assert topic in p.guide, topic


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
    # assistant_context's answer, as HookPlaybook gives it.
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
def hp(api: Api, monkeypatch: pytest.MonkeyPatch) -> Api:
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
        claim = {"thread_id": None, "name": "shot.png", "mime": "image/png", "size": len(PNG)}
        r = await hp.client.post("/v1/chat/hookplaybook/uploads", json=claim, headers=_widget(workspace))
        assert r.status_code == 200, r.text
        started = r.json()
        hp.put(started, PNG, workspace, "hookplaybook")
        url = f"/v1/chat/hookplaybook/uploads/{started['upload_id']}/finish"
        r = await hp.client.post(url, json={"thread_id": started["thread_id"]}, headers=_widget(workspace))
        assert r.status_code == 200, r.text
        up = r.json()
        hp.llm.add("IMAGE_READER", {"text": "a chart", "unsure": []})
        write = [
            call("save_facts", facts=[{"text": fact}]),
            call("write_drafts", brief="the March milestone", kind="x_post"),
        ]
        hp.llm.add("CONVERSE", Reply(calls=write), "Here's one.")
        hp.llm.add("DRAFT_WRITER", {"drafts": [{"posts": [post], "fact_ids": ["F1"]}]})
        hp.llm.add("CLAIM_CHECK", {"unsupported": []})
        body = {"thread_id": up["thread_id"], "text": fact, "file_ids": [up["file_id"]]}
        r = await hp.client.post("/v1/chat/hookplaybook/messages", json=body, headers=_widget(workspace))
        events = parse_sse(r.text)
        assert [k for k, _ in events][-1] == "done", events
        assert [p["posts"] for k, p in events if k == "draft"] == [[post]]
        [shown] = [p for k, p in events if k == "draft"]  # stored by its event; Add to Schedule makes an action
        keep = {"action_id": f"add-{workspace}", "kind": "add_draft", "draft_id": shown["id"]}
        url = f"/v1/chat/hookplaybook/threads/{up['thread_id']}/actions"
        assert (await hp.client.post(url, json=keep, headers=_widget(workspace))).status_code == 200
        return up["thread_id"]

    hp.tools._context = default_context(credits_left=100)
    gone, kept = await chat("ws_hp"), await chat("ws_other")
    config = {"configurable": {"thread_id": thread_id("hookplaybook", "ws_hp", gone)}}
    assert await hp.checkpointer.aget_tuple(config) is not None

    # As HookPlaybook calls it when a workspace is deleted: a delete token living 300 s, no Origin.
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


@pytest.mark.db
async def test_uploads_use_hookplaybooks_own_5_mb_image_cap(hp: Api):
    six_mb = PNG + b"\x00" * (6 * 1024 * 1024)
    claim = {"thread_id": None, "name": "shot.png", "mime": "image/png", "size": len(six_mb)}
    r = await hp.client.post("/v1/chat/hookplaybook/uploads", json=claim, headers=_widget("ws_hp"))
    assert (r.status_code, r.json()) == (400, {"error": "too_big", "message": "Images can be up to 5 MB."})

    r = await hp.client.post("/v1/chat/hookplaybook/uploads", json={**claim, "size": 1000}, headers=_widget("ws_hp"))
    started = r.json()
    hp.put(started, six_mb, "ws_hp", "hookplaybook")  # the stored bytes are what count, not the claim
    url = f"/v1/chat/hookplaybook/uploads/{started['upload_id']}/finish"
    r = await hp.client.post(url, json={"thread_id": started["thread_id"]}, headers=_widget("ws_hp"))
    assert (r.status_code, r.json()) == (400, {"error": "too_big", "message": "Images can be up to 5 MB."})
    assert hp.storage._client.list_objects_v2(Bucket=hp.storage.bucket).get("Contents", []) == []


# ---- from the registry: HookPlaybook serves its own pack; the YAML is the fallback ----

WELL_KNOWN = f"{SITE}/.well-known/app-assistant.json"


def registry_entry() -> RegistryEntry:
    [entry] = [e for e in load_registry(registry_path(APPS), None) if e.id == "hookplaybook"]
    return entry


def served_pack() -> dict:
    """What HookPlaybook serves: the YAML's content as contract 1, reached by REST (no secret, no tool names)."""
    raw = yaml.safe_load((APPS / "hookplaybook.yaml").read_text())
    for key in ("mcp_url", "tools", "secret_env"):
        raw.pop(key, None)
    return {**raw, "contract": 1, "api_base": f"{SITE}/api/assistant"}


def hp_store(settings, pool, *, status: int = 200) -> PackStore:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == WELL_KNOWN
        return httpx.Response(status, json=served_pack() if status == 200 else {"error": "not_found"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return PackStore({"hookplaybook": pack()}, settings, registry=[registry_entry()], pool=pool, http=http)


def test_the_registry_lists_hookplaybook_with_its_secret_env():
    entry = registry_entry()
    assert (entry.site, entry.secret_env) == (SITE, SECRET_ENV)
    assert entry.origin == SITE


async def test_bucket_cors_origins_still_include_hookplaybook(test_settings):
    packs = PackStore(
        load_packs(APPS, base_url="http://testserver"), test_settings, registry=load_registry(registry_path(APPS), None)
    )
    assert SITE in packs.origins()


@pytest.mark.db
async def test_a_reachable_pack_makes_hookplaybook_a_rest_app(test_settings, pool):
    await migrate(pool)
    served = await hp_store(test_settings, pool).get("hookplaybook")
    assert served is not None
    assert (served.api_base, served.mcp_url, served.tools) == (f"{SITE}/api/assistant", None, None)
    assert served.secret_env == SECRET_ENV and served.allowed_origins == [SITE] and served.demo is False


@pytest.mark.db
async def test_an_unreachable_pack_with_no_copy_uses_the_yaml_and_mcp(test_settings, pool):
    await migrate(pool)
    fallback = await hp_store(test_settings, pool, status=404).get("hookplaybook")
    assert fallback is not None
    assert (fallback.mcp_url, fallback.api_base) == ("https://hookplaybook.com/api/v1/mcp", None)


@pytest.mark.db
async def test_a_stored_copy_beats_the_yaml_when_the_pack_goes_down(test_settings, pool):
    await migrate(pool)
    await hp_store(test_settings, pool).get("hookplaybook")  # fetched once: kept in Postgres
    down = await hp_store(test_settings, pool, status=500).get("hookplaybook")  # a cold instance, route down
    assert down is not None
    assert (down.api_base, down.mcp_url) == (f"{SITE}/api/assistant", None)


@pytest.mark.db
@pytest.mark.parametrize(
    ("status", "client"), [(200, "RestHttpTools"), (404, "McpHttpTools")], ids=["pack served", "pack not served yet"]
)
async def test_a_hookplaybook_turn_uses_the_client_its_pack_calls_for(api: Api, status, client, monkeypatch):
    monkeypatch.setenv(SECRET_ENV, SECRET)
    api.app.state.packs = hp_store(api.app.state.settings, api.pool, status=status)
    used: list[str] = []

    def tools_for_pack(pack, session):
        used.append(type(real_tools_for(pack, session, api.client)).__name__)
        return api.tools

    api.app.state.tools_for = tools_for_pack
    api.tools._context = default_context(credits_left=100)
    api.llm.script = {"CONVERSE": ["What changed for users?"]}
    r = await api.client.post("/v1/chat/hookplaybook/messages", json={"text": "hi"}, headers=_widget("ws_hp"))
    assert [k for k, _ in parse_sse(r.text)][-1] == "done", r.text
    assert used == [client]
