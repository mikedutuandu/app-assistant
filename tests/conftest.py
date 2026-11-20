import os
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from chat_api import SECRET, Api, make_pack
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from moto import mock_aws
from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from ulid import ULID

from assistant.config import Settings
from assistant.db import migrate
from assistant.files.storage import Storage
from assistant.graph.build import serializer
from assistant.main import create_app


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    return Settings(
        openrouter_api_key="test-openrouter-key",
        database_url="postgresql://user:pass@localhost:5432/test",
        s3_endpoint=None,
        s3_bucket="test-bucket",
        s3_access_key="test-access",
        s3_secret_key="test-secret",
        packs_dir=tmp_path / "apps",
        public_base_url="http://testserver",
        run_lifespan=False,
    )


@pytest.fixture
async def pool() -> AsyncIterator[AsyncConnectionPool]:
    """A pool on a fresh schema of TEST_DATABASE_URL, dropped after the test. Skips without it."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set")
    schema = f"test_{str(ULID()).lower()}"
    async with await AsyncConnection.connect(url, autocommit=True) as admin:
        await admin.execute(sql.SQL("create schema {}").format(sql.Identifier(schema)))
    conninfo = make_conninfo(url, options=f"-c search_path={schema}")
    pool = AsyncConnectionPool(conninfo, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()
        async with await AsyncConnection.connect(url, autocommit=True) as admin:
            await admin.execute(sql.SQL("drop schema {} cascade").format(sql.Identifier(schema)))


@pytest.fixture
async def api(  # the chat API on a fresh schema, moto S3, FakeLlm and FakeTools (tests/chat_api.py)
    pool: AsyncConnectionPool, test_settings: Settings, monkeypatch
) -> AsyncIterator[Api]:
    monkeypatch.setenv("TEST_CHAT_SECRET", SECRET)
    await migrate(pool)
    saver_pool = AsyncConnectionPool(
        pool.conninfo,
        min_size=1,
        max_size=2,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await saver_pool.open()
    try:
        checkpointer = AsyncPostgresSaver(saver_pool, serde=serializer())  # type: ignore[arg-type]
        await checkpointer.setup()
        with mock_aws():
            storage = Storage(test_settings)
            storage._client.create_bucket(
                Bucket=test_settings.s3_bucket,
                CreateBucketConfiguration={"LocationConstraint": test_settings.s3_region},
            )
            app = create_app(test_settings)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
                api = Api(app, c, pool, storage, checkpointer)
                app.state.pool = pool
                app.state.checkpointer = checkpointer
                app.state.storage = storage
                app.state.http = c
                app.state.packs = {"real": make_pack(), "demo": make_pack("demo", demo=True)}
                app.state.llm = api.llm
                app.state.tools_for = lambda pack, session: api.tools
                yield api
    finally:
        await saver_pool.close()


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
