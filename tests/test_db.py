import os
from pathlib import Path

import pytest

from assistant.config import Settings
from assistant.db import migrate, new_id
from assistant.main import create_app
from assistant.packs import PackError
from assistant.tools import McpHttpTools

pytestmark = pytest.mark.db


async def test_migrate_applies_once(pool):
    assert await migrate(pool) == ["001_init.sql", "002_pieces.sql"]
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


async def test_lifespan_opens_migrates_and_sets_up_checkpointer(test_settings):
    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("TEST_DATABASE_URL is not set")
    settings = Settings(
        **{
            **test_settings.model_dump(),
            "database_url": os.environ["TEST_DATABASE_URL"],
            "packs_dir": Path("apps"),
            "run_lifespan": True,
        }
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
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
