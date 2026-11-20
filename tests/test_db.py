import os
from pathlib import Path

import pytest
from psycopg import AsyncConnection, sql
from pydantic import ValidationError

from assistant.config import Settings
from assistant.db import migrate, new_id, open_pool, open_saver_pool
from assistant.main import create_app
from assistant.packs import PackError
from assistant.tools import RestHttpTools

pytestmark = pytest.mark.db


async def test_migrate_applies_once(pool):
    assert await migrate(pool) == [
        "001_init.sql",
        "002_pieces.sql",
        "003_demo.sql",
        "004_drafts.sql",
        "005_saved_lines.sql",
        "006_demo_thread.sql",
        "007_remote_packs.sql",
        "008_settings.sql",
    ]
    assert await migrate(pool) == []


async def test_migrate_creates_tables(pool):
    await migrate(pool)
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select table_name from information_schema.tables where table_schema = current_schema()"
        )
        names = {row[0] for row in await cur.fetchall()}
    assert {"threads", "files", "counters", "actions", "pieces", "saved_lines", "schema_migrations"} <= names


async def test_failed_migration_keeps_earlier_ones(pool, tmp_path, monkeypatch):
    (tmp_path / "001_ok.sql").write_text("create table first_ok (id int);")
    (tmp_path / "002_bad.sql").write_text("create table second_bad (id int); select * from does_not_exist;")
    monkeypatch.setattr("assistant.db.MIGRATIONS_DIR", tmp_path)
    with pytest.raises(Exception, match="does_not_exist"):
        await migrate(pool)
    async with pool.connection() as conn:
        cur = await conn.execute("select name from schema_migrations")
        assert [r[0] for r in await cur.fetchall()] == ["001_ok.sql"]
        cur = await conn.execute("select to_regclass('first_ok'), to_regclass('second_bad')")
        assert await cur.fetchone() == ("first_ok", None)
    # a fixed second migration then applies alone
    (tmp_path / "002_bad.sql").write_text("create table second_bad (id int);")
    assert await migrate(pool) == ["002_bad.sql"]


def test_new_id_sorts_by_time():
    import time

    first = new_id()
    time.sleep(0.01)
    second = new_id()
    assert first < second
    assert len(first) == 26


async def _tables(url: str, schema: str) -> set[str]:
    async with await AsyncConnection.connect(url) as conn:
        cur = await conn.execute("select table_name from information_schema.tables where table_schema = %s", (schema,))
        return {r[0] for r in await cur.fetchall()}


async def test_pools_use_their_own_schema_and_migrate_creates_it(fresh_schema):
    url, schema = fresh_schema
    pool = await open_pool(url, schema, max_size=3)
    saver = await open_saver_pool(url, schema, max_size=2)
    try:
        assert (pool.max_size, saver.max_size) == (3, 2)
        assert await migrate(pool, schema) == [
            "001_init.sql",
            "002_pieces.sql",
            "003_demo.sql",
            "004_drafts.sql",
            "005_saved_lines.sql",
            "006_demo_thread.sql",
            "007_remote_packs.sql",
            "008_settings.sql",
        ]
        async with pool.connection() as conn:
            cur = await conn.execute("select current_schema()")
            assert (await cur.fetchone())[0] == schema
        async with saver.connection() as conn:  # dict rows, autocommit: LangGraph's saver
            cur = await conn.execute("select current_schema() as s")
            assert (await cur.fetchone())["s"] == schema
        assert {"threads", "actions", "schema_migrations"} <= await _tables(url, schema)
        assert await migrate(pool, schema) == []
    finally:
        await saver.close()
        await pool.close()


async def test_pools_check_each_connection_before_handing_it_out(fresh_schema):
    """On Fluid compute an instance can sit suspended with its connections open; one the pooler or a NAT dropped
    meanwhile is caught by the check at checkout, not by the user's first message. Idle ones close after a minute."""
    from psycopg_pool import AsyncConnectionPool

    url, schema = fresh_schema
    pool = await open_pool(url, schema, max_size=2)
    saver = await open_saver_pool(url, schema, max_size=1)
    try:
        for p in (pool, saver):
            assert p._check == AsyncConnectionPool.check_connection
            assert p.max_idle == 60
        # A connection killed while idle is replaced at checkout instead of failing the query.
        async with pool.connection() as conn:
            pid = conn.info.backend_pid
        async with await AsyncConnection.connect(url, autocommit=True) as admin:
            await admin.execute("select pg_terminate_backend(%s)", (pid,))
        async with pool.connection() as conn:
            cur = await conn.execute("select 1")
            assert await cur.fetchone() == (1,)
    finally:
        await saver.close()
        await pool.close()


@pytest.mark.parametrize("name", ["assistant", "_x9", "a" * 41])
def test_db_schema_accepts_plain_names(test_settings, name):
    assert Settings(**{**test_settings.model_dump(), "db_schema": name}).db_schema == name


@pytest.mark.parametrize("name", ["Assistant", "a-b", "1abc", "a b", "a;drop", "", "a" * 42, "public.x"])
def test_db_schema_refuses_other_names(test_settings, name):
    with pytest.raises(ValidationError):
        Settings(**{**test_settings.model_dump(), "db_schema": name})


def test_pool_sizes_default_to_a_small_budget(test_settings):
    assert (test_settings.db_schema, test_settings.db_pool_max, test_settings.saver_pool_max) == ("assistant", 2, 1)


async def test_lifespan_opens_migrates_and_sets_up_checkpointer(test_settings, monkeypatch, fresh_schema, capsys):
    url, schema = fresh_schema
    monkeypatch.setenv("DEMO_SESSION_SECRET", "d" * 64)
    monkeypatch.setenv("HOOKPLAYBOOK_SESSION_SECRET", "h" * 64)
    settings = Settings(
        **{
            **test_settings.model_dump(),
            "database_url": url,
            "db_schema": schema,
            "db_pool_max": 3,
            "saver_pool_max": 2,
            "packs_dir": Path("apps"),
            "run_lifespan": True,
        }
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        # Every table, LangGraph's checkpoints too, is in the bot's own schema (Supabase does not expose it).
        tables = await _tables(url, schema)
        assert {"threads", "files", "demo_drafts", "schema_migrations"} <= tables
        assert {"checkpoints", "checkpoint_writes", "checkpoint_blobs", "checkpoint_migrations"} <= tables
        assert app.state.pool.max_size == 3
        assert app.state.pool is not None
        assert app.state.checkpointer is not None
        assert app.state.checkpointer.serde.__class__.__name__ == "JsonPlusSerializer"
        assert app.state.packs["demo"].api_base == "http://testserver/demo/api/assistant"
        assert app.state.llm._headers()["HTTP-Referer"] == "http://testserver"
        assert app.state.storage.bucket == "test-bucket"
        http = app.state.http
        assert not http.is_closed
        assert isinstance(app.state.tools_for(app.state.packs["demo"], None), RestHttpTools)
        async with app.state.pool.connection() as conn:
            cur = await conn.execute("select count(*) from threads")
            assert (await cur.fetchone())[0] >= 0
    assert http.is_closed
    assert "uploads: s3" in capsys.readouterr().out  # a missing SUPABASE_* shows in the log


async def test_lifespan_fails_without_packs(test_settings):
    settings = Settings(**{**test_settings.model_dump(), "run_lifespan": True})  # packs_dir is an empty tmp dir
    app = create_app(settings)
    with pytest.raises(PackError, match="no app packs"):
        async with app.router.lifespan_context(app):
            pass


async def test_lifespan_fails_without_hookplaybooks_secret(test_settings, monkeypatch):
    monkeypatch.setenv("DEMO_SESSION_SECRET", "d" * 64)
    monkeypatch.delenv("HOOKPLAYBOOK_SESSION_SECRET", raising=False)
    settings = Settings(**{**test_settings.model_dump(), "packs_dir": Path("apps"), "run_lifespan": True})
    app = create_app(settings)
    with pytest.raises(PackError, match="HOOKPLAYBOOK_SESSION_SECRET"):
        async with app.router.lifespan_context(app):
            pass


async def test_lifespan_fails_when_a_pack_secret_is_unset(test_settings, monkeypatch):
    monkeypatch.delenv("DEMO_SESSION_SECRET", raising=False)
    settings = Settings(**{**test_settings.model_dump(), "packs_dir": Path("apps"), "run_lifespan": True})
    app = create_app(settings)
    with pytest.raises(PackError, match="DEMO_SESSION_SECRET"):
        async with app.router.lifespan_context(app):
            pass


async def test_lifespan_starts_no_background_loop(test_settings, monkeypatch, fresh_schema):
    """Vercel runs nothing between requests: the demo clean-up comes from Cron (GET /internal/cleanup)."""
    import asyncio

    url, schema = fresh_schema
    monkeypatch.setenv("DEMO_SESSION_SECRET", "d" * 64)
    monkeypatch.setenv("HOOKPLAYBOOK_SESSION_SECRET", "h" * 64)
    settings = Settings(
        **{
            **test_settings.model_dump(),
            "database_url": url,
            "db_schema": schema,
            "packs_dir": Path("apps"),
            "run_lifespan": True,
        }
    )
    created: list[str] = []
    real_create_task = asyncio.create_task

    def spy(coro, **kwargs):
        created.append(getattr(coro, "__qualname__", repr(coro)))
        return real_create_task(coro, **kwargs)

    monkeypatch.setattr(asyncio, "create_task", spy)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        pass
    assert not [name for name in created if "cleanup" in name.lower()], created
    from assistant.demo import cleanup

    assert not hasattr(cleanup, "cleanup_loop")


async def test_several_first_starts_at_once_all_succeed(test_settings, monkeypatch, fresh_schema):
    """Vercel can start several instances on the first deploy: the bot's migrations and LangGraph's checkpoint
    setup both run under the one advisory lock, so none of them fails on a fresh schema."""
    import asyncio

    url, base = fresh_schema
    monkeypatch.setenv("DEMO_SESSION_SECRET", "d" * 64)
    monkeypatch.setenv("HOOKPLAYBOOK_SESSION_SECRET", "h" * 64)

    async def start(schema: str) -> set[str]:
        settings = Settings(
            **{
                **test_settings.model_dump(),
                "database_url": url,
                "db_schema": schema,
                "packs_dir": Path("apps"),
                "run_lifespan": True,
            }
        )
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            return await _tables(url, schema)

    schemas = [f"{base}_{i}" for i in range(3)]
    try:
        for schema in schemas:  # a few rounds: one race can pass by luck
            results = await asyncio.gather(*(start(schema) for _ in range(4)), return_exceptions=True)
            assert [r for r in results if isinstance(r, BaseException)] == []
            assert all({"threads", "checkpoints", "checkpoint_migrations"} <= r for r in results)
    finally:
        async with await AsyncConnection.connect(url, autocommit=True) as admin:
            for schema in schemas:
                await admin.execute(sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(schema)))


async def test_app_pool_never_prepares_statements():
    """Behind a transaction pooler (Neon's pooled URL) server-side prepared statements break: the app pool must not prepare."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set")
    pool = await open_pool(url)
    try:
        async with pool.connection() as conn:
            for _ in range(6):
                await conn.execute("select 1")
            cur = await conn.execute("select count(*) from pg_prepared_statements")
            row = await cur.fetchone()
        assert row is not None and row[0] == 0
    finally:
        await pool.close()
