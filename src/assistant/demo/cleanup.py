"""Everything the demo keeps is deleted after a day: drafts, facts, files, threads and their storage objects."""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta

from assistant.graph.build import thread_id as graph_thread_id

log = logging.getLogger("assistant.demo")
DEMO_TTL = timedelta(days=1)
EVERY_SECONDS = 3600


async def cleanup_demo(state, now: datetime | None = None) -> dict:
    """Delete demo data older than a day; returns how many of each were removed."""
    now = now or datetime.now(UTC)
    cutoff = now - DEMO_TTL
    pool, storage = state.pool, state.storage
    out = {"drafts": 0, "facts": 0, "files": 0, "threads": 0}

    async with pool.connection() as conn:
        cur = await conn.execute("delete from demo_drafts where created_at < %s returning image_key", (cutoff,))
        keys = [r[0] for r in await cur.fetchall() if r[0]]
        out["drafts"] = cur.rowcount
        cur = await conn.execute("delete from demo_facts where created_at < %s", (cutoff,))
        out["facts"] = cur.rowcount
    for key in keys:
        await asyncio.to_thread(storage.delete_prefix, key)

    # Threads first (their files go with them), so storage keys are read before the rows cascade away.
    async with pool.connection() as conn:
        cur = await conn.execute("select id, workspace from threads where app = 'demo' and updated_at < %s", (cutoff,))
        threads = await cur.fetchall()
    for chat_id, workspace in threads:
        await state.checkpointer.adelete_thread(graph_thread_id("demo", workspace, chat_id))
    ids = [t[0] for t in threads]
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select storage_key from files where app = 'demo' and (thread_id = any(%s) or expires_at < %s)",
            (ids, now),
        )
        file_keys = [r[0] for r in await cur.fetchall()]
    for key in file_keys:
        await asyncio.to_thread(storage.delete_prefix, key)
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            "delete from files where app = 'demo' and (thread_id = any(%s) or expires_at < %s)", (ids, now)
        )
        out["files"] = cur.rowcount
        cur = await conn.execute("delete from threads where app = 'demo' and id = any(%s)", (ids,))
        out["threads"] = cur.rowcount
    return out


async def cleanup_loop(state) -> None:
    """On startup, then every hour. A failed round is logged by its error kind and tried again next hour."""
    while True:
        try:
            result = await cleanup_demo(state)
            log.info("demo cleanup %s", result)
        except Exception as e:  # noqa: BLE001 - never let the loop die
            log.error("demo cleanup failed error=%s", type(e).__name__)
        await asyncio.sleep(EVERY_SECONDS)


async def stop(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
