"""One message turn: the caps, the app's credits, the graph, then `done` or `failed`.

It runs as its own asyncio task that feeds a queue the SSE response drains, so a client that goes away never
cancels it. Each model call is charged as soon as it ends, once, by `"<message id>:<call number>"`, so a turn cut off
(a Vercel function can be) has still recorded every call that finished. The message id reaches the browser only in
`done`, after every charge. Only ids, timings, costs and
error kinds are logged, never text.

The events a turn streams, in the order they can come: `thread` (a new chat's id and title), `status` (`{"step"}`: what
the bot is doing now, one of `reading`, `listening`, `thinking`, `writing`, `checking`; no user text, never charged,
and it may repeat), `transcript` (a voice note's words), `text` (`{"delta"}`), `note`, `card`, then `done` or `failed`.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from langchain_core.messages import HumanMessage
from psycopg.types.json import Jsonb

from assistant.auth import Session
from assistant.checks import Fact
from assistant.db import new_id
from assistant.files.intake import FileRecord, UploadError
from assistant.files.read import FileReading, Meter, read_file, read_link
from assistant.graph.build import build_graph, thread_id
from assistant.limits import (
    DEMO_MESSAGES_PER_IP_DAY,
    DEMO_USD_KEY,
    USER_MESSAGES_PER_HOUR,
    demo_ip_key,
    hit,
    remaining,
    user_key,
)
from assistant.packs import AppPack
from assistant.tools import AppTools, ToolError

log = logging.getLogger("assistant.turn")

TITLE_CHARS = 60
LIMIT = "You've sent a lot of messages this hour. Try again a bit later."
DEMO_IP_LIMIT = "The demo has had enough for today from here. Come back tomorrow."
RESTING = "The demo is resting until tomorrow."
OUT_OF_CREDITS = "You're out of credits in this app."
APP_DOWN = "The app couldn't be reached. Try again in a minute."
OUR_FAULT = "Something went wrong on our side. Nothing was saved."
FILE_GONE = "That file can't be found anymore."

Emit = Callable[[str, dict], Awaitable[None]]
_RUNNING: set[asyncio.Task] = set()  # strong references, so a running turn is never garbage-collected


@dataclass
class TurnInput:
    pack: AppPack
    session: Session
    thread_id: str | None  # an existing thread of this session's workspace, or None to start one
    text: str
    file_ids: list[str]
    ip: str


class _Refused(Exception):
    def __init__(self, error: str, message: str):
        super().__init__(error)
        self.body = {"error": error, "message": message}


def tool_failure(e: ToolError) -> dict[str, str]:
    return {"error": e.kind, "message": e.user_message or APP_DOWN}


def start(state: Any, turn: TurnInput) -> asyncio.Queue:
    """Starts the turn in its own task; the queue yields `(kind, payload)` events, then None."""
    queue: asyncio.Queue = asyncio.Queue()

    async def emit(kind: str, payload: dict) -> None:
        queue.put_nowait((kind, payload))

    async def run() -> None:
        try:
            await run_turn(state, turn, emit)
        finally:
            queue.put_nowait(None)

    track(asyncio.create_task(run()))
    return queue


def track(task: asyncio.Task) -> None:
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)


async def drain(timeout: float) -> None:
    """On shutdown: lets running turns finish (and charge) for up to `timeout` seconds."""
    if _RUNNING:
        await asyncio.wait(set(_RUNNING), timeout=timeout)


async def _gate(state: Any, t: TurnInput, tools: AppTools) -> None:
    """The caps, then the app's credits: raises _Refused (or the app's ToolError) before any model call."""
    pool, pack, session = state.pool, t.pack, t.session
    if not await hit(pool, user_key(session.app, session.workspace), timedelta(hours=1), USER_MESSAGES_PER_HOUR):
        raise _Refused("limit", LIMIT)
    if pack.demo:
        if not await hit(pool, demo_ip_key(t.ip), timedelta(days=1), DEMO_MESSAGES_PER_IP_DAY):
            raise _Refused("limit", DEMO_IP_LIMIT)
        if await remaining(pool, DEMO_USD_KEY, state.settings.demo_daily_usd_cap) <= 0:
            raise _Refused("resting", RESTING)
        return
    context = await tools.context()
    if context.credits_left is not None and context.credits_left <= 0:
        raise _Refused("out_of_credits", context.out_of_credits_message or OUT_OF_CREDITS)


async def _open_thread(pool, t: TurnInput) -> tuple[str, bool]:
    """The turn's thread id and whether it was just created; touches updated_at."""
    async with pool.connection() as conn:
        if t.thread_id is not None:
            await conn.execute(
                "update threads set updated_at = now() where id = %s and app = %s and workspace = %s",
                (t.thread_id, t.session.app, t.session.workspace),
            )
            return t.thread_id, False
        chat_id = new_id()
        await conn.execute(
            "insert into threads (id, app, workspace, title) values (%s, %s, %s, %s)",
            (chat_id, t.session.app, t.session.workspace, t.text[:TITLE_CHARS]),
        )
        return chat_id, True


async def _save_pieces(pool, t: TurnInput, chat_id: str, message_id: str, card: dict) -> None:
    facts = {f["id"]: f for f in card.get("facts", [])}
    async with pool.connection() as conn:
        for p in card.get("pieces", []):
            told = [facts[i] for i in p.get("fact_ids", []) if i in facts]
            await conn.execute(
                "insert into pieces (id, app, workspace, thread_id, message_id, spec_kind, text, image_file_id,"
                " fact_ids, facts, dropped) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " on conflict (id) do nothing",
                (
                    p["id"],
                    t.session.app,
                    t.session.workspace,
                    chat_id,
                    message_id,
                    p["spec_kind"],
                    p["text"],
                    p.get("image_file_id"),
                    Jsonb(p.get("fact_ids", [])),
                    Jsonb([Fact.model_validate(f).model_dump() for f in told]),
                    p.get("dropped"),
                ),
            )


def _file_reader(state: Any, t: TurnInput, chat_id: str) -> Callable[[list[str], Meter], Awaitable[list[FileReading]]]:
    async def read_files(ids: list[str], meter: Meter) -> list[FileReading]:
        async with state.pool.connection() as conn:
            cur = await conn.execute(
                "select id, kind, mime, bytes, storage_key from files"
                " where id = any(%s) and app = %s and workspace = %s and thread_id = %s",
                (ids, t.session.app, t.session.workspace, chat_id),
            )
            rows = {r[0]: r for r in await cur.fetchall()}
        readings = []
        for file_id in ids:
            row = rows.get(file_id)
            if row is None:
                raise UploadError("file", FILE_GONE)
            rec = FileRecord(id=row[0], kind=row[1], mime=row[2], bytes=row[3], storage_key=row[4])
            # which kind it is is only known here: say what the bot is doing before the slow part
            await meter.emit("status", {"step": "listening" if rec.kind == "voice" else "reading"})
            readings.append(await read_file(rec, t.pack, state.llm, state.storage, state.pool, meter))
        return readings

    return read_files


def _cost_recorder(
    state: Any, t: TurnInput, tools: AppTools, message_id: str
) -> Callable[[float, int], Awaitable[None]]:
    """What the graph awaits after each model call: charges the app by `"<message_id>:<n>"` (the demo adds to its
    daily spend instead), so a retry of the same call adds nothing. Never raises: a failure is logged by kind."""

    async def on_cost(cost: float, n: int) -> None:
        if cost <= 0:
            return
        try:
            if t.pack.demo:
                await hit(state.pool, DEMO_USD_KEY, timedelta(days=1), cap=float("inf"), amount=cost)
            else:
                await tools.charge(cost, ref=f"{message_id}:{n}")
        except ToolError as e:
            log.warning("charge failed message=%s call=%d kind=%s cost=%.6f", message_id, n, e.kind, cost)
        except Exception as e:  # noqa: BLE001 - the turn goes on and ends with its own outcome
            log.error("charge failed message=%s call=%d error=%s cost=%.6f", message_id, n, type(e).__name__, cost)

    return on_cost


async def run_turn(state: Any, t: TurnInput, emit: Emit) -> None:
    started = time.monotonic()
    message_id = new_id()
    tools = state.tools_for(t.pack, t.session)
    try:
        await _gate(state, t, tools)
    except _Refused as r:
        log.info("turn refused app=%s message=%s reason=%s", t.pack.id, message_id, r.body["error"])
        await emit("failed", r.body)
        return
    except ToolError as e:
        log.warning("turn context failed app=%s message=%s kind=%s", t.pack.id, message_id, e.kind)
        await emit("failed", tool_failure(e))
        return
    except Exception as e:  # noqa: BLE001 - told to the user as our fault; logged by kind only
        log.error("turn gate failed app=%s message=%s error=%s", t.pack.id, message_id, type(e).__name__)
        await emit("failed", {"error": "server", "message": OUR_FAULT})
        return

    try:
        chat_id, created = await _open_thread(state.pool, t)
    except Exception as e:  # noqa: BLE001 - told to the user as our fault; logged by kind only
        log.error("turn thread failed app=%s message=%s error=%s", t.pack.id, message_id, type(e).__name__)
        await emit("failed", {"error": "server", "message": OUR_FAULT})
        return
    if created:
        await emit("thread", {"thread_id": chat_id, "title": t.text[:TITLE_CHARS]})

    unsaved: list[str] = []  # a card whose pieces could not be stored: its Add buttons would never work

    async def on_event(kind: str, payload: dict) -> None:
        if kind == "card":
            try:
                await _save_pieces(state.pool, t, chat_id, message_id, payload)
            except Exception as e:  # noqa: BLE001 - the graph must never fail because of an event
                unsaved.append(type(e).__name__)
                log.error("pieces not saved message=%s error=%s", message_id, type(e).__name__)
        await emit(kind, payload)

    graph = build_graph(
        t.pack,
        state.llm,
        lambda: tools,
        state.checkpointer,
        on_event,
        _file_reader(state, t, chat_id),
        lambda url: read_link(url, state.http),
        _cost_recorder(state, t, tools, message_id),
    )
    config = {"configurable": {"thread_id": thread_id(t.session.app, t.session.workspace, chat_id)}}
    failure: Exception | None = None
    try:
        final = await graph.ainvoke({"messages": [HumanMessage(t.text)], "file_ids": t.file_ids}, config)
        cost = float(final.get("cost_usd", 0.0) or 0.0)
    except Exception as e:  # noqa: BLE001 - every failure is told to the user and its cost still charged
        failure = e
        cost = float(getattr(e, "cost_usd", 0.0) or 0.0)  # what the turn spent; never the checkpoint's on top

    took = time.monotonic() - started
    if failure is None and unsaved:
        log.error("turn failed app=%s message=%s error=pieces_not_saved cost=%.6f", t.pack.id, message_id, cost)
        await emit("failed", {"error": "server", "message": OUR_FAULT})
    elif failure is None:
        log.info(
            "turn done app=%s thread=%s message=%s cost=%.6f took=%.2fs", t.pack.id, chat_id, message_id, cost, took
        )
        await emit("done", {"message_id": message_id, "cost_usd": cost})
    elif isinstance(failure, ToolError):
        log.warning("turn failed app=%s message=%s kind=%s cost=%.6f", t.pack.id, message_id, failure.kind, cost)
        await emit("failed", tool_failure(failure))
    else:
        log.error(
            "turn failed app=%s message=%s error=%s cost=%.6f", t.pack.id, message_id, type(failure).__name__, cost
        )
        await emit("failed", {"error": "server", "message": OUR_FAULT})
