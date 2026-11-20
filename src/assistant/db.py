import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from ulid import ULID

from assistant.config import Settings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_LOCK = 4_815_162_342  # advisory lock key: one instance migrates at a time
_LOCK_POLL_SECONDS = 0.1


def _use_schema(schema: str | None) -> Callable[[AsyncConnection], Awaitable[None]] | None:
    """A pool `configure` callback: every new connection works in `schema` (set per session, not as a startup
    option, so it holds through Supabase's session pooler too)."""
    if schema is None:
        return None

    async def configure(conn: AsyncConnection) -> None:
        await conn.execute(sql.SQL("set search_path to {}").format(sql.Identifier(schema)))
        if not conn.autocommit:
            await conn.commit()  # the pool takes connections back only when idle

    return configure


# Vercel's Fluid compute suspends an instance with its connections open (idle timeouts don't run meanwhile), and the
# pooler or a NAT may drop them: each checkout checks its connection first (one round trip), and idle ones close
# after a minute.
_MAX_IDLE_SECONDS = 60.0


async def open_pool(url: str, schema: str | None = None, max_size: int = 5) -> AsyncConnectionPool:
    """The app's own pool (tuple rows, transactions)."""
    pool = AsyncConnectionPool(
        url,
        min_size=1,
        max_size=max_size,
        open=False,
        configure=_use_schema(schema),
        check=AsyncConnectionPool.check_connection,
        max_idle=_MAX_IDLE_SECONDS,
        # Never prepare: behind a transaction pooler (Neon's pooled URL) a prepared statement lives on another backend.
        kwargs={"prepare_threshold": None},
    )
    await pool.open()
    return pool


def saver_url(settings: Settings) -> str:
    """LangGraph's saver connects directly (DATABASE_URL_UNPOOLED) when that is set, else through DATABASE_URL."""
    return settings.database_url_unpooled or settings.database_url


async def open_saver_pool(url: str, schema: str | None = None, max_size: int = 2) -> AsyncConnectionPool:
    """LangGraph's checkpoint saver needs autocommit connections and dict rows, so it gets its own pool."""
    pool = AsyncConnectionPool(
        url,
        min_size=1,
        max_size=max_size,
        open=False,
        configure=_use_schema(schema),
        check=AsyncConnectionPool.check_connection,
        max_idle=_MAX_IDLE_SECONDS,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open()
    return pool


async def close_pool(pool: AsyncConnectionPool) -> None:
    await pool.close()


async def _take_migration_lock(conn: AsyncConnection) -> None:
    """Wait for the migration lock by polling, not with a blocking pg_advisory_lock.

    A blocked pg_advisory_lock is a statement that holds a snapshot while it waits, and LangGraph's setup (run by
    the holder, under the lock) creates its indexes CONCURRENTLY, which waits for every older snapshot to end: the
    two would wait on each other for ever. Between polls a waiter holds no snapshot.
    """
    while True:
        cur = await conn.execute("select pg_try_advisory_lock(%s)", (_MIGRATION_LOCK,))
        row = await cur.fetchone()
        if row is not None and row[0]:
            return
        await asyncio.sleep(_LOCK_POLL_SECONDS)


def new_id() -> str:
    return str(ULID())


async def migrate(
    pool: AsyncConnectionPool,
    schema: str | None = None,
    after: Callable[[], Awaitable[None]] | None = None,
) -> list[str]:
    """Apply the missing migrations/*.sql in name order, each in its own transaction.

    Table names are unqualified, so they land in the connection's search_path schema; `schema` (the pool's) is
    created first when given. `after` (LangGraph's checkpoint setup) runs while the lock is still held: its own
    migrations take no lock, so instances starting together on a fresh schema would otherwise collide.
    """
    applied: list[str] = []
    async with pool.connection() as conn:
        # Autocommit, so each file below is a real transaction (not a savepoint in an outer one).
        await conn.set_autocommit(True)
        try:
            # Session-level lock across the whole run: two instances starting together take turns.
            await _take_migration_lock(conn)
            try:
                if schema is not None:
                    await conn.execute(sql.SQL("create schema if not exists {}").format(sql.Identifier(schema)))
                await conn.execute("create table if not exists schema_migrations (name text primary key)")
                for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                    cur = await conn.execute("select 1 from schema_migrations where name = %s", (path.name,))
                    if await cur.fetchone():
                        continue
                    async with conn.transaction():
                        await conn.execute(path.read_text())
                        await conn.execute("insert into schema_migrations (name) values (%s)", (path.name,))
                    applied.append(path.name)
                if after is not None:
                    await after()
            finally:
                await conn.execute("select pg_advisory_unlock(%s)", (_MIGRATION_LOCK,))
        finally:
            await conn.set_autocommit(False)
    return applied
