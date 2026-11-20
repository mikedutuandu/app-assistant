from pathlib import Path

from assistant.config import Settings
from assistant.db import direct_url

ROOT = Path(__file__).resolve().parents[1]


def _settings(**extra) -> Settings:
    return Settings(
        openrouter_api_key="k",
        database_url="postgresql://pooled/db",
        s3_bucket="b",
        s3_access_key="a",
        s3_secret_key="s",
        public_base_url="http://testserver",
        _env_file=None,
        **extra,
    )  # type: ignore[call-arg]


def test_direct_url_is_the_unpooled_url_when_set():
    s = _settings(database_url_unpooled="postgresql://direct/db")
    assert direct_url(s) == "postgresql://direct/db"


def test_direct_url_falls_back_to_database_url():
    assert direct_url(_settings()) == "postgresql://pooled/db"


def test_empty_unpooled_url_is_unset():
    s = _settings(database_url_unpooled="")
    assert s.database_url_unpooled is None
    assert direct_url(s) == "postgresql://pooled/db"


async def test_every_pool_and_the_migration_use_the_direct_url(monkeypatch, tmp_path):
    from assistant import main

    seen: dict[str, str] = {}

    class Stop(Exception):
        pass

    async def fake_pool(url, *a, **k):
        seen.setdefault("pools", []).append(url)  # type: ignore[arg-type]
        return object()

    async def fake_migrate(pool, *a, **k):
        raise Stop

    async def fake_close(pool):
        pass

    class FakeSaver:
        def __init__(self, *a, **k):
            self.setup = None

    pack_dir = ROOT / "apps"
    s = _settings(database_url_unpooled="postgresql://direct/db", packs_dir=pack_dir, run_lifespan=True)
    monkeypatch.setattr(main, "open_pool", fake_pool)
    monkeypatch.setattr(main, "open_saver_pool", fake_pool)
    monkeypatch.setattr(main, "migrate", fake_migrate)
    monkeypatch.setattr(main, "close_pool", fake_close)
    monkeypatch.setattr(main, "AsyncPostgresSaver", FakeSaver)
    monkeypatch.setattr(main, "get_settings", lambda: s)
    monkeypatch.setenv("DEMO_SESSION_SECRET", "x")
    monkeypatch.setenv("HOOKPLAYBOOK_SESSION_SECRET", "x")
    app = main.create_app(s)
    try:
        async with app.router.lifespan_context(app):
            pass
    except Stop:
        pass
    assert seen["pools"] == ["postgresql://direct/db", "postgresql://direct/db"]


def test_neon_prefixed_urls_are_read(monkeypatch):
    for name in ("DATABASE_URL", "DATABASE_URL_UNPOOLED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NEON_DATABASE_URL", "postgresql://neon-pooled/db")
    monkeypatch.setenv("NEON_DATABASE_URL_UNPOOLED", "postgresql://neon-direct/db")
    s = Settings(
        openrouter_api_key="k",
        s3_bucket="b",
        s3_access_key="a",
        s3_secret_key="s",
        public_base_url="http://t",
        _env_file=None,
    )  # type: ignore[call-arg]
    assert s.database_url == "postgresql://neon-pooled/db"
    assert direct_url(s) == "postgresql://neon-direct/db"


def test_unprefixed_url_wins_over_neon_prefix(monkeypatch):
    monkeypatch.setenv("DATABASE_URL_UNPOOLED", "postgresql://plain/db")
    monkeypatch.setenv("NEON_DATABASE_URL_UNPOOLED", "postgresql://neon-direct/db")
    s = _settings()
    assert direct_url(s) == "postgresql://plain/db"


def test_neon_direct_url_used_even_with_an_old_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL_UNPOOLED", raising=False)
    monkeypatch.setenv("NEON_DATABASE_URL_UNPOOLED", "postgresql://neon-direct/db")
    s = _settings()  # database_url is the old pooled one
    assert direct_url(s) == "postgresql://neon-direct/db"
