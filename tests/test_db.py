import os
from pathlib import Path

import pytest
from psycopg import AsyncConnection, sql
from pydantic import ValidationError
from ulid import ULID

from assistant.config import Settings
from assistant.db import migrate, new_id, open_pool, open_saver_pool
from assistant.main import create_app
from assistant.packs import PackError
from assistant.tools import McpHttpTools

pytestmark = pytest.mark.db


async def test_migrate_applies_once(pool):
    assert await migrate(pool) == ["001_init.sql", "002_pieces.sql", "003_demo.sql"]
    assert await migrate(pool) == []


async def test_migrate_creates_tables(pool):
    await migrate(pool)
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select table_name from information_schema.tables where table_schema = current_schema()"
        )
        names = {row[0] for row in await cur.fetchall()}
    assert {"threads", "files", "counters", "actions", "pieces", "schema_migrations"} <= names


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


@pytest.fixture
async def fresh_schema():
    """A schema name that does not exist yet; dropped (if made) after the test."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set")
    schema = f"own_{str(ULID()).lower()}"
    yield url, schema
    async with await AsyncConnection.connect(url, autocommit=True) as admin:
        await admin.execute(sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(schema)))


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
        assert await migrate(pool, schema) == ["001_init.sql", "002_pieces.sql", "003_demo.sql"]
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


@pytest.mark.parametrize("name", ["assistant", "_x9", "a" * 41])
def test_db_schema_accepts_plain_names(test_settings, name):
    assert Settings(**{**test_settings.model_dump(), "db_schema": name}).db_schema == name


@pytest.mark.parametrize("name", ["Assistant", "a-b", "1abc", "a b", "a;drop", "", "a" * 42, "public.x"])
def test_db_schema_refuses_other_names(test_settings, name):
    with pytest.raises(ValidationError):
        Settings(**{**test_settings.model_dump(), "db_schema": name})


def test_pool_sizes_default_to_a_small_budget(test_settings):
    assert (test_settings.db_schema, test_settings.db_pool_max, test_settings.saver_pool_max) == ("assistant", 5, 2)


async def test_lifespan_opens_migrates_and_sets_up_checkpointer(test_settings, monkeypatch, fresh_schema):
    url, schema = fresh_schema
    monkeypatch.setenv("DEMO_SESSION_SECRET", "d" * 64)
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
        assert app.state.packs["demo"].mcp_url == "http://testserver/demo/mcp"
        assert app.state.llm._headers()["HTTP-Referer"] == "http://testserver"
        assert app.state.storage.bucket == "test-bucket"
        http = app.state.http
        assert not http.is_closed
        assert isinstance(app.state.tools_for(app.state.packs["demo"], None), McpHttpTools)
        async with app.state.pool.connection() as conn:
            cur = await conn.execute("select count(*) from threads")
            assert (await cur.fetchone())[0] >= 0
    assert http.is_closed


async def test_lifespan_fails_without_packs(test_settings):
    settings = Settings(**{**test_settings.model_dump(), "run_lifespan": True})  # packs_dir is an empty tmp dir
    app = create_app(settings)
    with pytest.raises(PackError, match="no app packs"):
        async with app.router.lifespan_context(app):
            pass


async def test_lifespan_fails_when_a_pack_secret_is_unset(test_settings, monkeypatch):
    monkeypatch.delenv("DEMO_SESSION_SECRET", raising=False)
    settings = Settings(**{**test_settings.model_dump(), "packs_dir": Path("apps"), "run_lifespan": True})
    app = create_app(settings)
    with pytest.raises(PackError, match="DEMO_SESSION_SECRET"):
        async with app.router.lifespan_context(app):
            pass
