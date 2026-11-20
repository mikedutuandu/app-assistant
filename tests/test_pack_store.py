"""The pack store: local YAML packs, packs an app serves itself (fetched, cached, kept in Postgres), the registry of
trusted apps, and the bot owner's ceilings (MAX_MESSAGE_USD, ALLOWED_MODELS, MAX_APP_DAILY_USD)."""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from chat_api import APP_DOWN, ORIGIN, SECRET, Api, ask_script, make_pack, parse_sse, raw_token
from pack_schema import SCHEMA_PATH
from pydantic import ValidationError

from assistant.config import Settings
from assistant.db import migrate
from assistant.limits import remaining
from assistant.pack_store import PackStore, RegistryEntry, clamp_pack, load_registry, registry_path, spend_key
from assistant.packs import AppPack, PackError, ServedPack, load_packs, pack_json_schema

SITE = "https://shop.example"
WELL_KNOWN = f"{SITE}/.well-known/app-assistant.json"
SECRET_ENV = "TEST_CHAT_SECRET"  # the api fixture sets it to chat_api.SECRET


def served(**over) -> dict:
    """A pack as an app serves it: contract 1, REST, no secret and no tool names."""
    pack = {
        "contract": 1,
        "id": "shop",
        "name": "Shop",
        "api_base": f"{SITE}/api/assistant",
        "allowed_origins": [SITE],
        "guide": "Ask what sold and one number.",
        "writes": [{"kind": "x_post", "count": 1, "max_chars": 280, "instructions": "A short post."}],
        "models": {"chat": "a/chat", "writer": "a/writer", "main": "a/main", "cheap": "a/cheap", "vision": "a/vision"},
        "message_usd_cap": 0.05,
    }
    return pack | over


class App:
    """The app's pack route behind an httpx.MockTransport: counts fetches; `status`/`body` say what it answers."""

    def __init__(self, body: dict | str | None = None, status: int = 200):
        self.body = served() if body is None else body
        self.status = status
        self.fetches = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == WELL_KNOWN
        self.fetches += 1
        if isinstance(self.body, str):
            return httpx.Response(self.status, text=self.body)
        return httpx.Response(self.status, json=self.body)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


ENTRY = RegistryEntry(id="shop", site=SITE, secret_env=SECRET_ENV)


def store(
    settings: Settings, pool=None, app: App | None = None, local: dict | None = None, clock=None, **ceilings
) -> PackStore:
    settings = settings.model_copy(update=ceilings) if ceilings else settings
    return PackStore(
        local if local is not None else {},
        settings,
        registry=[ENTRY],
        pool=pool,
        http=(app or App()).client(),
        **({"clock": clock} if clock else {}),
    )


@pytest.fixture
async def db(pool):
    await migrate(pool)
    return pool


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_ENV, SECRET)


# ---- the pack's shape ----


def test_a_pack_is_reached_by_exactly_one_of_mcp_url_and_api_base():
    base = make_pack().model_dump()
    rest = AppPack.model_validate({**base, "mcp_url": None, "api_base": "https://a.example/api", "tools": None})
    assert (rest.api_base, rest.mcp_url, rest.tools, rest.contract) == ("https://a.example/api", None, None, 1)
    with pytest.raises(ValidationError, match="exactly one of mcp_url and api_base"):
        AppPack.model_validate({**base, "api_base": "https://a.example/api"})
    with pytest.raises(ValidationError, match="exactly one of mcp_url and api_base"):
        AppPack.model_validate({**base, "mcp_url": None})
    with pytest.raises(ValidationError, match="missing: context, save_draft, save_material, charge"):
        AppPack.model_validate({**base, "tools": None})  # MCP still needs its four tool names


def test_a_served_pack_has_no_secret_tools_or_demo():
    pack = ServedPack.model_validate(served(secret_env="STOLEN", demo=True, tools={"context": "x"}))
    assert not {"secret_env", "demo", "tools", "mcp_url"} & set(pack.model_dump())
    with pytest.raises(ValidationError):
        ServedPack.model_validate(served(contract=2))
    with pytest.raises(ValidationError):
        ServedPack.model_validate(served(api_base="ftp://shop.example/api"))


def test_the_committed_json_schema_matches_the_model():
    committed = json.loads(SCHEMA_PATH.read_text())
    assert committed == pack_json_schema(), "run `uv run python scripts/pack_schema.py` and commit the file"
    assert committed["properties"]["contract"]["const"] == 1
    assert {"contract", "id", "api_base", "writes", "models", "message_usd_cap"} <= set(committed["required"])


# ---- the registry ----


def test_registry_from_the_file_and_apps_overrides_it(tmp_path: Path):
    (tmp_path / "registry.yaml").write_text(
        "apps:\n  - id: shop\n    site: https://shop.example/\n    secret_env: SHOP_SECRET\n"
    )
    [entry] = load_registry(tmp_path / "registry.yaml", None)
    assert entry == RegistryEntry(id="shop", site="https://shop.example", secret_env="SHOP_SECRET")
    assert entry.origin == "https://shop.example"
    override = [RegistryEntry(id="other", site="https://o.example", secret_env="O")]
    assert load_registry(tmp_path / "registry.yaml", override) == override
    assert load_registry(tmp_path / "missing.yaml", None) == []


def test_registry_refuses_duplicates_and_bad_sites(tmp_path: Path):
    path = tmp_path / "registry.yaml"
    path.write_text(
        "apps:\n  - {id: a, site: https://a.example, secret_env: A}\n  - {id: a, site: https://b.example, secret_env: B}\n"
    )
    with pytest.raises(PackError, match="duplicate"):
        load_registry(path, None)
    path.write_text("apps:\n  - {id: a, site: not-a-url, secret_env: A}\n")
    with pytest.raises(PackError, match="registry"):
        load_registry(path, None)


def test_the_committed_registry_is_empty_for_now():
    assert load_registry(Path("apps/registry.yaml"), None) == []


def test_apps_env_is_json(test_settings: Settings, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APPS", json.dumps([{"id": "shop", "site": SITE, "secret_env": "X"}]))
    settings = Settings(**{k: v for k, v in test_settings.model_dump().items() if k != "apps"})
    assert settings.apps == [RegistryEntry(id="shop", site=SITE, secret_env="X")]


def test_ceilings_from_env(test_settings: Settings, monkeypatch: pytest.MonkeyPatch):
    assert (test_settings.max_message_usd, test_settings.allowed_models, test_settings.max_app_daily_usd) == (
        0.25,
        None,
        None,
    )
    monkeypatch.setenv("ALLOWED_MODELS", "a/chat, a/writer")
    monkeypatch.setenv("MAX_APP_DAILY_USD", "")
    fields = {k: v for k, v in test_settings.model_dump().items() if k not in ("allowed_models", "max_app_daily_usd")}
    settings = Settings(**fields)
    assert (settings.allowed_models, settings.max_app_daily_usd) == (["a/chat", "a/writer"], None)


# ---- the owner's ceilings ----


def test_clamp_caps_message_usd(test_settings: Settings, caplog):
    pack = make_pack().model_copy(update={"message_usd_cap": 5})
    with caplog.at_level(logging.WARNING, logger="assistant"):
        assert clamp_pack(pack, test_settings).message_usd_cap == 0.25
    assert "message_usd_cap" in caplog.text
    assert clamp_pack(make_pack(), test_settings).message_usd_cap == 0.05  # under the ceiling: unchanged


def test_clamp_refuses_a_model_outside_the_allowlist(test_settings: Settings):
    pack = make_pack()
    allowed = ["chat/model", "writer/model", "main/model", "cheap/model", "vis/model"]
    assert clamp_pack(pack, test_settings.model_copy(update={"allowed_models": allowed})) == pack
    with pytest.raises(PackError, match="vis/model"):
        clamp_pack(pack, test_settings.model_copy(update={"allowed_models": allowed[:-1]}))
    fallback = pack.model_copy(update={"models": pack.models.model_copy(update={"fallbacks": ["other/model"]})})
    with pytest.raises(PackError, match="other/model"):
        clamp_pack(fallback, test_settings.model_copy(update={"allowed_models": allowed}))


# ---- the store ----


async def test_local_pack_found(test_settings: Settings):
    packs = PackStore({"real": make_pack()}, test_settings)
    assert (await packs.get("real")).id == "real"
    assert await packs.get("nope") is None
    assert packs["real"].id == "real"


async def test_local_packs_are_clamped_and_a_refused_one_stops_startup(test_settings: Settings):
    big = make_pack().model_copy(update={"message_usd_cap": 5})
    assert (await PackStore({"real": big}, test_settings).get("real")).message_usd_cap == 0.25
    with pytest.raises(PackError, match="vis/model"):
        PackStore({"real": make_pack()}, test_settings.model_copy(update={"allowed_models": ["chat/model"]}))


async def test_remote_pack_fetched_cached_and_stored(test_settings: Settings, db):
    app, clock = App(), Clock()
    packs = store(test_settings, db, app, clock=clock)
    first = await packs.get("shop")
    assert first is not None
    assert (first.id, first.api_base, first.mcp_url, first.secret_env, first.site) == (
        "shop",
        f"{SITE}/api/assistant",
        None,
        SECRET_ENV,
        SITE,
    )
    assert first.demo is False and first.secret() == SECRET
    assert await packs.get("shop") == first
    assert app.fetches == 1  # cached for 5 minutes
    async with db.connection() as conn:
        cur = await conn.execute("select pack, fetched_at from remote_packs where app = 'shop'")
        [(stored, fetched_at)] = await cur.fetchall()
    assert stored == served() and fetched_at <= datetime.now(UTC)

    clock.now += 299
    await packs.get("shop")
    assert app.fetches == 1
    clock.now += 2
    app.body = served(name="Shop 2")
    assert (await packs.get("shop")).name == "Shop 2"
    assert app.fetches == 2


async def test_fetch_fails_on_a_cold_instance_uses_the_postgres_copy(test_settings: Settings, db):
    assert await store(test_settings, db, App()).get("shop") is not None  # an earlier instance fetched it
    for down in (App(status=500), App(body="<html>down</html>"), App(body={"contract": 1})):
        cold = store(test_settings, db, down, local={"shop": make_pack("shop")})
        pack = await cold.get("shop")
        assert pack is not None and pack.api_base == f"{SITE}/api/assistant"  # the copy, never the YAML
        assert down.fetches == 1


async def test_fetch_fails_with_no_copy_uses_the_local_yaml(test_settings: Settings, db):
    yaml_pack = make_pack("shop")
    packs = store(test_settings, db, App(status=503), local={"shop": yaml_pack})
    assert await packs.get("shop") == yaml_pack


async def test_nothing_at_all_is_none(test_settings: Settings, db, caplog):
    with caplog.at_level(logging.WARNING, logger="assistant"):
        assert await store(test_settings, db, App(status=404)).get("shop") is None
    assert "shop" in caplog.text and "404" in caplog.text


async def test_a_served_pack_with_another_id_is_refused(test_settings: Settings, db):
    assert await store(test_settings, db, App(served(id="other"))).get("shop") is None


async def test_a_remote_pack_is_clamped(test_settings: Settings, db):
    pack = await store(test_settings, db, App(served(message_usd_cap=5))).get("shop")
    assert pack is not None and pack.message_usd_cap == 0.25


async def test_a_remote_pack_naming_a_model_outside_the_allowlist_is_refused(test_settings: Settings, db, caplog):
    allowed = ["a/chat", "a/writer", "a/main", "a/cheap"]  # not a/vision
    with caplog.at_level(logging.WARNING, logger="assistant"):
        assert await store(test_settings, db, App(), allowed_models=allowed).get("shop") is None
    assert "a/vision" in caplog.text
    async with db.connection() as conn:
        cur = await conn.execute("select count(*) from remote_packs")
        assert (await cur.fetchone())[0] == 0  # a refused pack is never kept as the last good copy


async def test_origins_are_local_packs_and_registry_sites(test_settings: Settings):
    packs = PackStore({"real": make_pack()}, test_settings, registry=[ENTRY])
    assert packs.origins() == [ORIGIN, SITE]


# ---- through the chat API ----


def _remote(api: Api, app: App, local: dict | None = None) -> None:
    settings = api.app.state.settings
    api.app.state.packs = PackStore(
        {"real": make_pack(), **(local or {})}, settings, registry=[ENTRY], pool=api.pool, http=app.client()
    )


async def _message(api: Api, app_id: str = "shop", origin: str = SITE) -> httpx.Response:
    headers = {"Authorization": f"Bearer {raw_token(app_id, 'ws1', 'draft')}", "Origin": origin}
    return await api.client.post(f"/v1/chat/{app_id}/messages", json={"text": "hi"}, headers=headers)


@pytest.mark.db
async def test_chat_with_no_pack_at_all_answers_unavailable(api: Api):
    _remote(api, App(status=500))
    r = await _message(api)
    assert r.status_code == 200, r.text
    assert r.headers["access-control-allow-origin"] == SITE  # the widget can read why
    assert parse_sse(r.text) == [("failed", {"error": "unavailable", "message": APP_DOWN})]
    assert api.llm.calls == []
    r = await api.client.get(
        "/v1/chat/shop/threads", headers={"Authorization": f"Bearer {raw_token('shop', 'w', 'draft')}"}
    )
    assert (r.status_code, r.json()) == (503, {"error": "unavailable", "message": APP_DOWN})
    bad = {"Authorization": "Bearer nope", "Origin": SITE}
    assert (await api.client.post("/v1/chat/shop/messages", json={"text": "hi"}, headers=bad)).status_code == 401
    assert (await _message(api, "nobody")).status_code == 404  # not in the registry: as before


@pytest.mark.db
async def test_chat_with_a_remote_pack_runs_a_turn(api: Api):
    _remote(api, App())
    api.llm.script = ask_script()
    r = await _message(api)
    assert r.status_code == 200
    assert [k for k, _ in parse_sse(r.text)][-1] == "done"
    preflight = {"Origin": SITE, "Access-Control-Request-Method": "POST"}
    r = await api.client.options("/v1/chat/shop/messages", headers=preflight)
    assert (r.status_code, r.headers["access-control-allow-origin"]) == (204, SITE)


@pytest.mark.db
async def test_app_daily_spend_ceiling_rests_the_app(api: Api):
    api.app.state.settings = api.app.state.settings.model_copy(update={"max_app_daily_usd": 0.01})
    api.llm.script = ask_script()
    api.llm.cost = 0.02
    events = await api.send("shipped auto-reply")
    assert [k for k, _ in events][-1] == "done"
    assert api.tools.charges  # the app was still charged as usual
    key = spend_key("real")
    assert key == f"spend:real:{datetime.now(UTC):%Y-%m-%d}"
    assert await remaining(api.pool, key, 0.01) == pytest.approx(-0.01)
    api.llm.script = ask_script()
    calls = len(api.llm.calls)
    events = await api.send("again")
    assert events == [("failed", {"error": "resting", "message": "This app's assistant is resting until tomorrow."})]
    assert len(api.llm.calls) == calls  # no model call once the app is resting


@pytest.mark.db
async def test_no_daily_ceiling_counts_nothing(api: Api):
    api.llm.script = ask_script()
    await api.send("shipped auto-reply")
    async with api.pool.connection() as conn:
        cur = await conn.execute("select count(*) from counters where key like 'spend:%'")
        assert (await cur.fetchone())[0] == 0


async def test_startup_fails_when_a_registered_apps_secret_is_unset(test_settings: Settings, monkeypatch):
    from assistant.main import create_app

    monkeypatch.delenv("TEST_SHOP_SECRET", raising=False)
    entry = RegistryEntry(id="shop", site=SITE, secret_env="TEST_SHOP_SECRET")
    app = create_app(test_settings.model_copy(update={"run_lifespan": True, "apps": [entry]}))
    with pytest.raises(PackError, match="TEST_SHOP_SECRET"):  # the registry alone is enough packs to start
        async with app.router.lifespan_context(app):
            pass


# ---- fix round 1: a served pack only calls its own site; caps have a ceiling ----


@pytest.mark.parametrize(
    "api_base",
    [
        "https://evil.example/api/assistant",  # another host
        "http://shop.example/api/assistant",  # http while the site is https
        "http://169.254.169.254/latest",  # an internal address
        "http://localhost:9000/api",
        "https://shop.example:8443/api/assistant",  # another port
        "https://api.shop.example/assistant",  # a subdomain is another origin
    ],
)
async def test_an_api_base_off_the_registry_site_is_refused(test_settings: Settings, db, caplog, api_base):
    with caplog.at_level(logging.WARNING, logger="assistant"):
        assert await store(test_settings, db, App(served(api_base=api_base))).get("shop") is None
    assert "api_base must be on https://shop.example" in caplog.text
    async with db.connection() as conn:
        cur = await conn.execute("select count(*) from remote_packs")
        assert (await cur.fetchone())[0] == 0


async def test_an_api_base_on_the_registry_site_is_accepted(test_settings: Settings, db):
    for api_base in (f"{SITE}/api/assistant", "https://shop.example/v2/"):
        pack = await store(test_settings, db, App(served(api_base=api_base))).get("shop")
        assert pack is not None and pack.api_base == api_base.rstrip("/")


async def test_an_api_base_off_site_keeps_the_last_good_copy(test_settings: Settings, db):
    assert await store(test_settings, db, App()).get("shop") is not None
    pack = await store(test_settings, db, App(served(api_base="https://evil.example/api"))).get("shop")
    assert pack is not None and pack.api_base == f"{SITE}/api/assistant"


def test_file_caps_are_clamped_to_the_bots_maximums(test_settings: Settings, caplog):
    big = {"image_mb": 500, "pdf_mb": 10000, "pdf_pages": 100000, "voice_seconds": 9999, "per_message": 99}
    pack = make_pack().model_copy(update={"files": make_pack().files.model_copy(update=big)})
    with caplog.at_level(logging.WARNING, logger="assistant"):
        files = clamp_pack(pack, test_settings).files
    assert (files.image_mb, files.pdf_mb, files.pdf_pages, files.voice_seconds, files.per_message) == (
        20,
        50,
        50,
        600,
        10,
    )
    assert "pdf_pages" in caplog.text
    assert clamp_pack(make_pack(), test_settings).files == make_pack().files  # within: unchanged


async def test_a_served_packs_file_caps_are_clamped(test_settings: Settings, db):
    pack = await store(test_settings, db, App(served(files={"pdf_pages": 100000}))).get("shop")
    assert pack is not None and pack.files.pdf_pages == 50


@pytest.mark.parametrize("name", ["registry.yaml", "registry.yml"])
def test_either_registry_file_is_read_and_never_a_pack(tmp_path: Path, name: str):
    (tmp_path / name).write_text("apps:\n  - {id: shop, site: https://shop.example, secret_env: S}\n")
    assert registry_path(tmp_path) == tmp_path / name
    assert [e.id for e in load_registry(registry_path(tmp_path), None)] == ["shop"]
    assert load_packs(tmp_path, "http://testserver") == {}


def test_both_registry_files_is_an_error(tmp_path: Path):
    (tmp_path / "registry.yaml").write_text("apps: []\n")
    (tmp_path / "registry.yml").write_text("apps: []\n")
    with pytest.raises(PackError, match="registry.yaml and registry.yml"):
        registry_path(tmp_path)
