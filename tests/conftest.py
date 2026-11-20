import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo
from psycopg_pool import AsyncConnectionPool
from ulid import ULID

from assistant.config import Settings


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
