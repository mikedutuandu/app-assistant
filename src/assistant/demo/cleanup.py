"""Everything the demo keeps is deleted after a day: drafts, facts, files, threads and their storage objects.

Also every app's reserved uploads that were never finished. Run once a day by Vercel Cron (GET /internal/cleanup).

One failure never stops the run: a delete that fails (a storage error, a network blip) is logged by its error kind
only, its row is kept so the next run tries again, and the run carries on, step by step.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from assistant.graph.build import thread_id as graph_thread_id

DEMO_TTL = timedelta(days=1)
log = logging.getLogger("assistant.cleanup")


async def _delete_objects(storage, keys: list[str], step: str) -> set[str]:
    """Deletes each key's objects; returns the keys whose delete failed."""
    failed: set[str] = set()
    for key in keys:
        try:
            await asyncio.to_thread(storage.delete_prefix, key)
        except Exception as e:  # noqa: BLE001 - kept for the next run; logged by kind only (keys carry ids)
            failed.add(key)
            log.warning("cleanup delete failed step=%s error=%s", step, type(e).__name__)
    return failed


async def _pending(state, now: datetime, cutoff: datetime) -> dict:
    """An upload the browser PUT (or never finished) leaves an unchecked object: remove it, its row last so a failed
    delete is tried again. Every app, not only the demo."""
    async with state.pool.connection() as conn:
        cur = await conn.execute("select id, storage_key from files where kind = 'pending' and expires_at < %s", (now,))
        stale = await cur.fetchall()
    failed = await _delete_objects(state.storage, [key for _, key in stale], "pending")
    done = [file_id for file_id, key in stale if key not in failed]
    if not done:
        return {"pending": 0}
    async with state.pool.connection() as conn:
        cur = await conn.execute("delete from files where kind = 'pending' and id = any(%s)", (done,))
        return {"pending": cur.rowcount}


async def _drafts(state, now: datetime, cutoff: datetime) -> dict:
    """Day-old drafts (each row after its image) and facts."""
    async with state.pool.connection() as conn:
        cur = await conn.execute("select id, image_key from demo_drafts where created_at < %s", (cutoff,))
        old = await cur.fetchall()
    failed = await _delete_objects(state.storage, [key for _, key in old if key], "drafts")
    done = [draft_id for draft_id, key in old if key not in failed]
    async with state.pool.connection() as conn:
        cur = await conn.execute("delete from demo_drafts where id = any(%s)", (done,))
        drafts = cur.rowcount
        cur = await conn.execute("delete from demo_facts where created_at < %s", (cutoff,))
        return {"drafts": drafts, "facts": cur.rowcount}


async def _threads(state, now: datetime, cutoff: datetime) -> dict:
    """Day-old threads with their checkpoints, files and storage prefix, and the demo's expired files.

    Storage keys are read before the rows go (a thread's files cascade with it). A thread with any failed delete is
    kept whole, with its files, for the next run."""
    pool, storage = state.pool, state.storage
    async with pool.connection() as conn:
        cur = await conn.execute("select id, workspace from threads where app = 'demo' and updated_at < %s", (cutoff,))
        threads = await cur.fetchall()
    ids = [t[0] for t in threads]
    stuck: set[str] = set()
    for chat_id, workspace in threads:
        try:
            await state.checkpointer.adelete_thread(graph_thread_id("demo", workspace, chat_id))
        except Exception as e:  # noqa: BLE001 - kept for the next run; logged by kind only
            stuck.add(chat_id)
            log.warning("cleanup delete failed step=checkpoints error=%s", type(e).__name__)
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select storage_key, thread_id from files where app = 'demo' and (thread_id = any(%s) or expires_at < %s)",
            (ids, now),
        )
        files = await cur.fetchall()
    failed_keys = await _delete_objects(storage, [key for key, _ in files], "files")
    stuck |= {thread for key, thread in files if key in failed_keys and thread in ids}
    # An expired upload link can still create an object in the thread with no row: take the whole prefix.
    for chat_id, workspace in threads:
        if await _delete_objects(storage, [f"demo/{workspace}/{chat_id}/"], "threads"):
            stuck.add(chat_id)
    gone = [i for i in ids if i not in stuck]
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            "delete from files where app = 'demo' and (thread_id = any(%s) or expires_at < %s)"
            " and not storage_key = any(%s) and (thread_id is null or not thread_id = any(%s))",
            (gone, now, list(failed_keys), list(stuck)),
        )
        files_deleted = cur.rowcount
        cur = await conn.execute("delete from threads where app = 'demo' and id = any(%s)", (gone,))
        return {"files": files_deleted, "threads": cur.rowcount}


async def cleanup_demo(state, now: datetime | None = None) -> dict:
    """Delete demo data older than a day and every app's expired pending uploads; returns how many of each."""
    now = now or datetime.now(UTC)
    cutoff = now - DEMO_TTL
    out = {"drafts": 0, "facts": 0, "files": 0, "threads": 0, "pending": 0}
    for step, run in (("pending", _pending), ("drafts", _drafts), ("threads", _threads)):
        try:
            out |= await run(state, now, cutoff)
        except Exception as e:  # noqa: BLE001 - the next steps still run; this one is tried again tomorrow
            log.error("cleanup step failed step=%s error=%s", step, type(e).__name__)
    return out
