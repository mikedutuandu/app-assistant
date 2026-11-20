from pathlib import Path

from psycopg_pool import AsyncConnectionPool
from ulid import ULID

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_LOCK = 4_815_162_342  # advisory lock key: one instance migrates at a time


async def open_pool(url: str) -> AsyncConnectionPool:
    pool = AsyncConnectionPool(url, min_size=1, max_size=10, open=False)
    await pool.open()
    return pool


async def close_pool(pool: AsyncConnectionPool) -> None:
    await pool.close()


def new_id() -> str:
    return str(ULID())


async def migrate(pool: AsyncConnectionPool) -> list[str]:
    """Apply the missing migrations/*.sql in name order, each in its own transaction.

    Table names are unqualified, so they land in the connection's search_path schema.
    """
    applied: list[str] = []
    async with pool.connection() as conn:
        # Autocommit, so each file below is a real transaction (not a savepoint in an outer one).
        await conn.set_autocommit(True)
        try:
            # Session-level lock across the whole run: two instances starting together take turns.
            await conn.execute("select pg_advisory_lock(%s)", (_MIGRATION_LOCK,))
            try:
                await conn.execute("create table if not exists schema_migrations (name text primary key)")
                for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                    cur = await conn.execute("select 1 from schema_migrations where name = %s", (path.name,))
                    if await cur.fetchone():
                        continue
                    async with conn.transaction():
                        await conn.execute(path.read_text())
                        await conn.execute("insert into schema_migrations (name) values (%s)", (path.name,))
                    applied.append(path.name)
            finally:
                await conn.execute("select pg_advisory_unlock(%s)", (_MIGRATION_LOCK,))
        finally:
            await conn.set_autocommit(False)
    return applied
