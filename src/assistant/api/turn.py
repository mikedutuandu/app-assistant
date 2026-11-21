"""One message turn: the caps, the app's credits, the graph, then `done` or `failed`.

It runs as its own asyncio task that feeds a queue the SSE response drains, so a client that goes away never
cancels it. Each model call is charged as soon as it ends, once, by `"<message id>:<call number>"`, so a turn cut off
(a Vercel function can be) has still recorded every call that finished. The message id reaches the browser only in
`done`, after every charge. Only ids, timings, costs and
error kinds are logged, never text.

The events a turn streams, in the order they can come: `thread` (a new chat's id and title), `status` (`{"step"}`: what
the bot is doing now, one of `reading`, `listening`, `thinking`, `writing`, `checking`; no user text, never charged,
and it may repeat), `transcript` (a voice note's words), `text` (`{"delta"}`), `note`, `saved` (`{"kind": "facts",
"items"}`), `draft` (`{"id", "kind", "posts", "image_file_id"?, "post_at"?, "post_times"?}`), then `done` or `failed`. A `draft` is stored (in
`pieces`, with its posts) and a `saved` line is stored (in `saved_lines`) before it is sent on, so a reload shows both.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from langchain_core.messages import HumanMessage
from psycopg.types.json import Jsonb

from assistant.api.deps import APP_DOWN
from assistant.auth import Session
from assistant.checks import Fact
from assistant.db import new_id
from assistant.files.intake import FileRecord, UploadError
from assistant.files.read import FileReading, Meter, read_file, read_link
from assistant.graph.agent import confirm_lines
from assistant.graph.build import build_graph, thread_id
from assistant.graph.turn import SEEN_VIEW
from assistant.limits import (
    DEMO_MESSAGES_PER_IP_DAY,
    DEMO_USD_KEY,
    USER_MESSAGES_PER_HOUR,
    demo_ip_key,
    hit,
    remaining,
    user_key,
)
from assistant.pack_store import spend_key
from assistant.packs import AppPack
from assistant.timing import Outcome, TimedLlm, TurnTimer, current, log_turn
from assistant.tools import AppTools, McpHttpTools, RestHttpTools, ToolError

log = logging.getLogger("assistant.turn")

TITLE_CHARS = 60
SPEND_WINDOW = timedelta(days=2)  # the spend key names its UTC day; the row outlives that day
LIMIT = "You've sent a lot of messages this hour. Try again a bit later."
DEMO_IP_LIMIT = "The demo has had enough for today from here. Come back tomorrow."
RESTING = "The demo is resting until tomorrow."
APP_RESTING = "This app's assistant is resting until tomorrow."
OUT_OF_CREDITS = "You're out of credits in this app."
OUR_FAULT = "Something went wrong on our side. Nothing was saved."
FILE_GONE = "That file can't be found anymore."

Emit = Callable[[str, dict], Awaitable[None]]


class ModelFailed(Exception):
    """A model call failed after the turn's tools had already shown drafts or saved facts (kept in the thread)."""


_RUNNING: set[asyncio.Task] = set()  # strong references, so a running turn is never garbage-collected


@dataclass
class TurnInput:
    pack: AppPack
    session: Session
    thread_id: str | None  # an existing thread of this session's workspace, or None to start one
    text: str
    file_ids: list[str]
    ip: str
    draft_id: str | None = None  # the draft this message is about (a tap on its Shorter or Another angle)
    opens: bool = False  # thread_id is the host's own id for a thread that does not exist yet: create it with that id
    ask: list[tuple[str, str]] = field(default_factory=list)  # the chat's other turns (role, text), oldest first


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
    daily = state.settings.max_app_daily_usd  # the owner's ceiling on one app's spend today (off when unset)
    if daily is not None and await remaining(pool, spend_key(pack.id), daily) <= 0:
        raise _Refused("resting", APP_RESTING)
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
        if t.thread_id is not None and t.opens:
            cur = await conn.execute(
                "insert into threads (id, app, workspace, title) values (%s, %s, %s, %s) on conflict (id) do nothing"
                " returning 1",
                (t.thread_id, t.session.app, t.session.workspace, t.text[:TITLE_CHARS]),
            )
            if await cur.fetchone() is not None:
                return t.thread_id, True
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


async def _save_draft(pool, t: TurnInput, chat_id: str, message_id: str, draft: dict, told: list[dict]) -> None:
    posts = draft["posts"]
    async with pool.connection() as conn:
        await conn.execute(
            "insert into pieces (id, app, workspace, thread_id, message_id, spec_kind, text, image_file_id,"
            " fact_ids, facts, posts, post_at, post_times) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " on conflict (id) do nothing",
            (
                draft["id"],
                t.session.app,
                t.session.workspace,
                chat_id,
                message_id,
                draft["kind"],
                posts[0] if posts else "",
                draft.get("image_file_id"),
                Jsonb([f["id"] for f in told]),
                Jsonb([Fact.model_validate(f).model_dump() for f in told]),
                Jsonb(posts),
                draft.get("post_at"),
                Jsonb(draft["post_times"]) if draft.get("post_times") else None,
            ),
        )


PENDING_HOURS = 24  # a change not confirmed by then can't be any more
MAX_VIEWS = 6  # an app's cards in one message, at most


def view_event(
    view_id: str, tool: str, html: str, csp: dict | None, border: bool | None, input_: dict, result: dict
) -> dict:
    """A card as the chat gets it (the `view` event, and a thread read's `views`): the app's page, what the tool was
    asked and what it answered. The chat draws the page in a sandboxed frame (MCP Apps)."""
    out: dict = {"id": view_id, "tool": tool, "html": html, "input": input_, "result": result}
    if csp is not None:
        out["csp"] = csp
    if border is not None:
        out["prefers_border"] = border
    return out


async def _save_change(pool, t: TurnInput, chat_id: str, message_id: str, change: dict) -> str:
    """Keeps a change the user is asked to confirm; its id is the card's."""
    pending_id = new_id()
    async with pool.connection() as conn:
        await conn.execute(
            "insert into pending_actions (id, app, workspace, thread_id, message_id, tool, title, arguments, lines)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                pending_id,
                t.session.app,
                t.session.workspace,
                chat_id,
                message_id,
                change["tool"],
                change["title"],
                Jsonb(change["arguments"]),
                Jsonb(change.get("lines") or []),
            ),
        )
    return pending_id


async def changes_of(pool, session: Session, chat_id: str) -> list[dict]:
    """The changes asked in a thread, oldest first, as the model reads them: `{title, state, result}`."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select title, state, result, created_at < now() - %s from pending_actions"
            " where app = %s and workspace = %s and thread_id = %s order by created_at, id",
            (timedelta(hours=PENDING_HOURS), session.app, session.workspace, chat_id),
        )
        rows = await cur.fetchall()
    out = []
    for title, state, result, old in rows[-20:]:
        if state == "running":
            state = "waiting"  # being run right now: not done yet
        if state == "waiting" and old:
            state = "expired"
        out.append({"title": title, "state": state, "result": (result or "")[:500]})
    return out


async def _save_lines(pool, t: TurnInput, chat_id: str, message_id: str, saved: dict) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "insert into saved_lines (id, app, workspace, thread_id, message_id, items) values (%s, %s, %s, %s, %s, %s)",
            (new_id(), t.session.app, t.session.workspace, chat_id, message_id, Jsonb(saved.get("items", []))),
        )


def _file_reader(
    state: Any, t: TurnInput, chat_id: str, timer: TurnTimer
) -> Callable[[list[str], Meter], Awaitable[list[FileReading]]]:
    llm = TimedLlm(state.llm, timer)

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
            readings.append(await read_file(rec, t.pack, llm, state.storage, state.pool, meter))
        return readings

    return read_files


def _cost_recorder(
    state: Any, t: TurnInput, tools: AppTools, message_id: str
) -> tuple[Callable[[float, int], Awaitable[None]], Callable[[], Awaitable[None]]]:
    """What the graph calls after each model call, and what the turn awaits before it ends.

    Each call is charged by `"<message_id>:<n>"` (the demo adds to its daily spend instead), so a retry of the same
    call adds nothing. With MAX_APP_DAILY_USD set, each call is also added to the app's spend today, whatever the
    app's charge answers. The charge is sent at once but not waited for: it is a round trip to the app (2-3 s live)
    and the next model call need not wait for it. `settle` waits for every charge sent, and the turn calls it
    before `done` or `failed`, so no ref reaches the browser before its charge. Never raises: a failure is
    logged by kind."""
    pending: set[asyncio.Task[None]] = set()
    count_spend = state.settings.max_app_daily_usd is not None

    async def charge(cost: float, n: int) -> None:
        if count_spend:
            try:
                await hit(state.pool, spend_key(t.pack.id), SPEND_WINDOW, cap=float("inf"), amount=cost)
            except Exception as e:  # noqa: BLE001 - the app's charge below still goes out
                log.error(
                    "spend not counted message=%s call=%d error=%s cost=%.6f", message_id, n, type(e).__name__, cost
                )
        try:
            if t.pack.demo:
                await hit(state.pool, DEMO_USD_KEY, timedelta(days=1), cap=float("inf"), amount=cost)
            else:
                await tools.charge(cost, ref=f"{message_id}:{n}")
        except ToolError as e:
            log.warning("charge failed message=%s call=%d kind=%s cost=%.6f", message_id, n, e.kind, cost)
        except Exception as e:  # noqa: BLE001 - the turn goes on and ends with its own outcome
            log.error("charge failed message=%s call=%d error=%s cost=%.6f", message_id, n, type(e).__name__, cost)

    async def on_cost(cost: float, n: int) -> None:
        if cost <= 0:
            return
        task = asyncio.create_task(charge(cost, n))
        pending.add(task)
        task.add_done_callback(pending.discard)

    async def settle() -> None:
        while pending:
            await asyncio.gather(*list(pending), return_exceptions=True)

    return on_cost, settle


async def run_turn(state: Any, t: TurnInput, emit: Emit) -> None:
    started = time.monotonic()
    timer = TurnTimer()
    current.set(timer)  # the graph's Deps pick it up
    message_id = new_id()
    tools = state.tools_for(t.pack, t.session)
    if isinstance(tools, McpHttpTools | RestHttpTools):
        tools.timer = timer
    try:
        await _gate(state, t, tools)
    except _Refused as r:
        log.info("turn refused app=%s message=%s reason=%s", t.pack.id, message_id, r.body["error"])
        log_turn(timer, t.pack.id, "refused")
        await emit("failed", r.body)
        return
    except ToolError as e:
        log.warning("turn context failed app=%s message=%s kind=%s", t.pack.id, message_id, e.kind)
        log_turn(timer, t.pack.id, "failed")
        await emit("failed", tool_failure(e))
        return
    except Exception as e:  # noqa: BLE001 - told to the user as our fault; logged by kind only
        log.error("turn gate failed app=%s message=%s error=%s", t.pack.id, message_id, type(e).__name__)
        log_turn(timer, t.pack.id, "failed")
        await emit("failed", {"error": "server", "message": OUR_FAULT})
        return

    try:
        chat_id, created = await _open_thread(state.pool, t)
    except Exception as e:  # noqa: BLE001 - told to the user as our fault; logged by kind only
        log.error("turn thread failed app=%s message=%s error=%s", t.pack.id, message_id, type(e).__name__)
        log_turn(timer, t.pack.id, "failed")
        await emit("failed", {"error": "server", "message": OUR_FAULT})
        return
    if created:
        await emit("thread", {"thread_id": chat_id, "title": t.text[:TITLE_CHARS]})

    unsaved: list[str] = []  # a draft that could not be stored: its Add button would never work

    async def on_event(kind: str, payload: dict) -> None:
        if kind == "draft":  # the facts it was written from are stored with it, never sent on
            payload = dict(payload)
            told = payload.pop("told", [])
        if kind in ("draft", "saved"):
            try:
                if kind == "draft":
                    await _save_draft(state.pool, t, chat_id, message_id, payload, told)
                else:
                    await _save_lines(state.pool, t, chat_id, message_id, payload)
            except Exception as e:  # noqa: BLE001 - the graph must never fail because of an event
                unsaved.append(type(e).__name__)
                log.error("%s not stored message=%s error=%s", kind, message_id, type(e).__name__)
        await emit(kind, payload)

    async def on_pending(change: dict) -> str:
        return await _save_change(
            state.pool, t, chat_id, message_id, {**change, "lines": confirm_lines(change["arguments"])}
        )

    views_shown = 0

    async def on_view(read: dict) -> dict | None:
        """A read's card (MCP Apps): the app's page for it, kept with the thread, as the `view` event to show."""
        nonlocal views_shown
        if actions is None or views_shown >= MAX_VIEWS:
            return None
        # The same answer already drawn in this chat (the model reads a list again for an id): not drawn again, and
        # the model is told so (Mike, 21 Nov 2026: "every time I chat it shows the panel of 3 sites").
        async with state.pool.connection() as conn:
            seen = await (
                await conn.execute(
                    "select 1 from app_views where app = %s and workspace = %s and thread_id = %s and tool = %s"
                    " and input = %s and result = %s limit 1",
                    (
                        t.session.app,
                        t.session.workspace,
                        chat_id,
                        read["tool"],
                        Jsonb(read["input"]),
                        Jsonb(read["result"]),
                    ),
                )
            ).fetchone()
        if seen is not None:
            return SEEN_VIEW
        page = await actions.view(read["uri"])
        if page is None:
            return None
        views_shown += 1
        view_id = new_id()
        async with state.pool.connection() as conn:
            await conn.execute(
                "insert into app_views (id, app, workspace, thread_id, message_id, tool, html, csp, prefers_border,"
                " input, result) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    view_id,
                    t.session.app,
                    t.session.workspace,
                    chat_id,
                    message_id,
                    read["tool"],
                    page.html,
                    Jsonb(page.csp) if page.csp is not None else None,
                    page.prefers_border,
                    Jsonb(read["input"]),
                    Jsonb(read["result"]),
                ),
            )
        return view_event(
            view_id, read["tool"], page.html, page.csp, page.prefers_border, read["input"], read["result"]
        )

    actions = state.actions_for(t.pack, t.session) if t.pack.actions is not None else None
    if actions is not None and hasattr(actions, "timer"):
        actions.timer = timer
    on_cost, settle_charges = _cost_recorder(state, t, tools, message_id)
    graph = build_graph(
        t.pack,
        state.llm,
        lambda: tools,
        state.checkpointer,
        on_event,
        _file_reader(state, t, chat_id, timer),
        lambda url: read_link(url, state.http),
        on_cost,
        actions_factory=(lambda: actions) if actions is not None else None,
        on_pending=on_pending,
        on_view=on_view,
    )
    config = {"configurable": {"thread_id": thread_id(t.session.app, t.session.workspace, chat_id)}}
    failure: Exception | None = None
    capped = False
    try:
        # the turn's id rides on the user's message, so a reload can show each turn's drafts under its reply; its files
        # too, so a reload can show them with the message
        extra: dict = {"turn": message_id, **({"file_ids": list(t.file_ids)} if t.file_ids else {})}
        human = HumanMessage(t.text, additional_kwargs=extra)
        changes = await changes_of(state.pool, t.session, chat_id) if actions is not None else []
        inputs = {"messages": [human], "file_ids": t.file_ids, "draft_id": t.draft_id, "ask": t.ask, "changes": changes}
        final = await graph.ainvoke(inputs, config)
        cost = float(final.get("cost_usd", 0.0) or 0.0)
        capped = bool(final.get("capped"))
        if final.get("failed"):  # a model call failed after the turn's tools had run: their work is kept
            failure = ModelFailed()
    except asyncio.CancelledError:
        await asyncio.shield(settle_charges())  # the client left: what was spent is still charged
        log_turn(timer, t.pack.id, "cancelled")
        raise
    except Exception as e:  # noqa: BLE001 - every failure is told to the user and its cost still charged
        failure = e
        cost = float(getattr(e, "cost_usd", 0.0) or 0.0)  # what the turn spent; never the checkpoint's on top
    await settle_charges()  # before `done` or `failed`: no ref reaches the browser before its charge

    took = time.monotonic() - started
    outcome: Outcome = "failed" if failure is not None or unsaved else "capped" if capped else "done"
    log_turn(timer, t.pack.id, outcome)
    if failure is None and unsaved:
        log.error("turn failed app=%s message=%s error=not_stored cost=%.6f", t.pack.id, message_id, cost)
        await emit("failed", {"error": "server", "message": OUR_FAULT})
    elif failure is None:
        log.info(
            "turn done app=%s thread=%s message=%s cost=%.6f took=%.2fs", t.pack.id, chat_id, message_id, cost, took
        )
        await emit("done", {"message_id": message_id, "cost_usd": cost, **({"capped": True} if capped else {})})
    elif isinstance(failure, ToolError):
        log.warning("turn failed app=%s message=%s kind=%s cost=%.6f", t.pack.id, message_id, failure.kind, cost)
        await emit("failed", tool_failure(failure))
    else:
        log.error(
            "turn failed app=%s message=%s error=%s cost=%.6f", t.pack.id, message_id, type(failure).__name__, cost
        )
        await emit("failed", {"error": "server", "message": OUR_FAULT})
