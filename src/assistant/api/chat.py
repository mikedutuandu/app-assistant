"""The widget's routes under `/v1/chat/{app}`, and the app's server-to-server workspace deletion."""

import asyncio
import json
import logging
import re
from datetime import timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from langchain_core.messages import AIMessage, HumanMessage
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, StringConstraints
from sse_starlette import EventSourceResponse, ServerSentEvent

from assistant.api.deps import ApiError, bearer, client_ip, pack_for, session_for
from assistant.api.turn import TurnInput, start, tool_failure
from assistant.auth import AuthError, Session, verify_delete_token
from assistant.checks import Fact
from assistant.db import new_id
from assistant.files.intake import PENDING, UploadError, UploadNotFound, check_claim, finish_upload, start_upload
from assistant.graph.build import build_graph
from assistant.graph.build import thread_id as graph_thread_id
from assistant.limits import hit
from assistant.packs import AppPack
from assistant.tools import ToolError

log = logging.getLogger("assistant.api")

THREADS_LISTED = 30
MAX_TEXT_CHARS = 4000
UPLOADS_PER_HOUR = 30
ACTIONS_PER_HOUR = 60
THREAD_DELETES_PER_HOUR = 60
MAX_CONTEXT_TURNS = 10
MAX_CONTEXT_CHARS = 2000
HOST_THREAD_ID = re.compile(r"^[0-9A-Z]{26}$")  # a ULID, like the ids the bot makes itself
UPLOAD_THREAD_TITLE = "Files"
PENDING_STALE = "2 minutes"  # a tap whose run died mid-way may be tried again after this

Auth = Annotated[tuple[AppPack, Session], Depends(session_for)]
router = APIRouter(prefix="/v1/chat/{app}")
workspaces = APIRouter()


async def owned_thread(pool, session: Session, chat_id: str) -> None:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select 1 from threads where id = %s and app = %s and workspace = %s",
            (chat_id, session.app, session.workspace),
        )
        if await cur.fetchone() is None:
            raise ApiError(404, "thread")


def host_thread_id(chat_id: str) -> str:
    """A thread id the host app chose for its own chat: a 26-character ULID, or 400."""
    if not HOST_THREAD_ID.fullmatch(chat_id):
        raise ApiError(400, "thread")
    return chat_id


async def open_host_thread(pool, session: Session, chat_id: str, title: str) -> bool:
    """Creates the thread with the host's id for this workspace, or continues it when it is already this
    workspace's; 404 when the id belongs to another workspace or app. True when it was just created."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "insert into threads (id, app, workspace, title) values (%s, %s, %s, %s) on conflict (id) do nothing"
            " returning 1",
            (chat_id, session.app, session.workspace, title),
        )
        created = await cur.fetchone() is not None
    if not created:
        await owned_thread(pool, session, chat_id)
    return created


# ---- uploads ----


# The browser PUTs the bytes straight to storage (Vercel refuses request bodies over 4.5 MB): it asks for a target,
# uploads there, then asks the bot to check what arrived.


class UploadIn(BaseModel):
    thread_id: str | None = None
    new_thread: bool = False  # with a thread_id: open the thread with that id (the host's), or continue it
    name: Annotated[str, StringConstraints(max_length=500)] = ""  # the browser's; never trusted or stored
    mime: Annotated[str, StringConstraints(max_length=200)] = ""
    size: int = Field(ge=0)


class FinishIn(BaseModel):
    thread_id: str


async def _drop_if_empty(state, session: Session, chat_id: str) -> None:
    """An upload-first thread that got no file is not kept: one `/uploads` opened, still with no file and no messages."""
    config = {"configurable": {"thread_id": graph_thread_id(session.app, session.workspace, chat_id)}}
    if await state.checkpointer.aget_tuple(config) is not None:
        return
    async with state.pool.connection() as conn:
        await conn.execute(
            "delete from threads where id = %s and app = %s and workspace = %s and title = %s"
            " and not exists (select 1 from files where thread_id = threads.id)"
            " and not exists (select 1 from pieces where thread_id = threads.id)",
            (chat_id, session.app, session.workspace, UPLOAD_THREAD_TITLE),
        )


@router.post("/uploads")
async def start_uploading(body: UploadIn, request: Request, auth: Auth) -> dict:
    """Reserves one upload: returns its id, the thread (opened if none was given) and where to PUT the bytes."""
    pack, session = auth
    state = request.app.state
    try:
        mime = check_claim(pack, body.mime, body.size)
    except UploadError as e:
        raise ApiError(400, e.code, e.message) from None
    if not await hit(state.pool, f"upload:{session.app}:{session.workspace}", timedelta(hours=1), UPLOADS_PER_HOUR):
        raise ApiError(429, "limit", "You've uploaded a lot this hour. Try again a bit later.")
    chat_id = body.thread_id
    created = chat_id is None
    if chat_id is not None and body.new_thread:
        created = await open_host_thread(state.pool, session, host_thread_id(chat_id), UPLOAD_THREAD_TITLE)
    elif chat_id is None:
        chat_id = new_id()
        async with state.pool.connection() as conn:
            await conn.execute(
                "insert into threads (id, app, workspace, title) values (%s, %s, %s, %s)",
                (chat_id, session.app, session.workspace, UPLOAD_THREAD_TITLE),
            )
    else:
        await owned_thread(state.pool, session, chat_id)
    try:
        upload_id, target = await start_upload(pack, session, chat_id, mime, body.size, state.pool, state.storage)
    except Exception:
        if created:  # an upload-first thread that got no file is not kept
            async with state.pool.connection() as conn:
                await conn.execute("delete from threads where id = %s", (chat_id,))
        raise
    return {"upload_id": upload_id, "thread_id": chat_id, "target": target}


@router.post("/uploads/{upload_id}/finish")
async def finish_uploading(upload_id: str, body: FinishIn, request: Request, auth: Auth) -> dict:
    """Checks what arrived in storage and makes it a file of the thread; a repeat answers the same."""
    pack, session = auth
    state = request.app.state
    try:
        rec = await finish_upload(pack, session, body.thread_id, upload_id, state.pool, state.storage)
    except UploadNotFound:
        raise ApiError(404, "upload") from None
    except UploadError as e:
        if e.code != "missing":
            await _drop_if_empty(state, session, body.thread_id)
        raise ApiError(400, e.code, e.message) from None
    log.info("upload app=%s thread=%s file=%s kind=%s bytes=%d", pack.id, body.thread_id, rec.id, rec.kind, rec.bytes)
    return {"file_id": rec.id, "kind": rec.kind, "thread_id": body.thread_id}


# ---- messages ----


class ContextTurn(BaseModel):
    role: Literal["user", "assistant"]
    text: str


class MessageIn(BaseModel):
    thread_id: str | None = None
    new_thread: bool = False  # with a thread_id: open the thread with that id (the host's), or continue it
    context: list[ContextTurn] = []  # the chat's other turns, answered by the app's own assistant, oldest first
    text: str = ""
    file_ids: list[str] = []
    draft_id: str | None = None  # the draft the message is about: its Shorter or Another angle


async def _events(queue: asyncio.Queue):
    while (item := await queue.get()) is not None:
        kind, payload = item
        yield ServerSentEvent(event=kind, data=json.dumps(payload, ensure_ascii=False))


@router.post("/messages")
async def messages(body: MessageIn, request: Request, auth: Auth) -> EventSourceResponse:
    pack, session = auth
    pool = request.app.state.pool
    text = body.text.strip()
    file_ids = list(dict.fromkeys(body.file_ids))
    if not text and not file_ids and body.draft_id is None:
        raise ApiError(400, "empty", "Type a message or attach a file.")
    if len(text) > MAX_TEXT_CHARS:
        raise ApiError(400, "too_long", "That message is too long. Keep it under 4,000 characters.")
    if len(file_ids) > pack.files.per_message:
        n = pack.files.per_message
        raise ApiError(400, "too_many_files", f"You can send up to {n} file{'s' if n != 1 else ''} at a time.")
    if len(body.context) > MAX_CONTEXT_TURNS or any(len(c.text) > MAX_CONTEXT_CHARS for c in body.context):
        raise ApiError(400, "too_long", "That chat is too long to send along.")
    opens = body.new_thread and body.thread_id is not None
    if opens:
        chat_id = host_thread_id(body.thread_id)
        async with pool.connection() as conn:
            cur = await conn.execute("select app, workspace from threads where id = %s", (chat_id,))
            row = await cur.fetchone()
        if row is not None and row != (session.app, session.workspace):
            raise ApiError(404, "thread")
        opens = row is None  # a thread that exists and is this workspace's is simply continued
    elif body.thread_id is not None:
        await owned_thread(pool, session, body.thread_id)
    if body.draft_id is not None:
        if body.thread_id is None:
            raise ApiError(404, "draft")  # a draft always belongs to a thread
        async with pool.connection() as conn:
            cur = await conn.execute(
                "select 1 from pieces where id = %s and app = %s and workspace = %s and thread_id = %s",
                (body.draft_id, session.app, session.workspace, body.thread_id),
            )
            if await cur.fetchone() is None:
                raise ApiError(404, "draft")
    if file_ids:
        if body.thread_id is None:
            raise ApiError(404, "file")  # a file always belongs to a thread
        async with pool.connection() as conn:
            cur = await conn.execute(
                "select count(*) from files where id = any(%s) and app = %s and workspace = %s and thread_id = %s"
                " and kind <> %s",
                (file_ids, session.app, session.workspace, body.thread_id, PENDING),
            )
            if (await cur.fetchone())[0] != len(file_ids):
                raise ApiError(404, "file")
    ask = [(c.role, c.text.strip()) for c in body.context if c.text.strip()]
    turn = TurnInput(pack, session, body.thread_id, text, file_ids, client_ip(request), body.draft_id, opens, ask)
    return EventSourceResponse(_events(start(request.app.state, turn)))


# ---- actions ----

ActionId = Annotated[str, StringConstraints(min_length=1, max_length=100)]


class AddDraftIn(BaseModel):
    action_id: ActionId
    kind: Literal["add_draft"]
    draft_id: str


ActionIn = AddDraftIn

_CLAIM = f"""
insert into actions (id, app, workspace, thread_id, kind, result)
values (%s, %s, %s, %s, %s, 'null'::jsonb)
on conflict (id) do update set created_at = now()
where actions.app = excluded.app and actions.workspace = excluded.workspace
  and actions.result = 'null'::jsonb and actions.created_at < now() - interval '{PENDING_STALE}'
returning 1
"""


async def _draft(pool, session: Session, chat_id: str, draft_id: str) -> tuple[list[str], str | None, list[Fact]]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select posts, text, image_file_id, facts from pieces where id = %s and app = %s and workspace = %s"
            " and thread_id = %s",
            (draft_id, session.app, session.workspace, chat_id),
        )
        row = await cur.fetchone()
    if row is None:
        raise ApiError(404, "draft")
    return (row[0] if row[0] else [row[1]]), row[2], [Fact.model_validate(f) for f in row[3]]


async def _image_url(state, session: Session, chat_id: str, file_id: str) -> list[dict]:
    async with state.pool.connection() as conn:
        cur = await conn.execute(
            "select storage_key from files where id = %s and app = %s and workspace = %s and thread_id = %s"
            " and kind <> %s",
            (file_id, session.app, session.workspace, chat_id, PENDING),
        )
        row = await cur.fetchone()
    if row is None:
        return []
    return [{"url": await asyncio.to_thread(state.storage.signed_url, row[0], 300), "alt": ""}]


@router.post("/threads/{thread_id}/actions")
async def act(thread_id: str, body: ActionIn, request: Request, auth: Auth) -> dict:
    """A tap on "Add to Schedule": runs once per action_id; a repeat gets the same result."""
    pack, session = auth
    state = request.app.state
    if not await hit(state.pool, f"action:{session.app}:{session.workspace}", timedelta(hours=1), ACTIONS_PER_HOUR):
        raise ApiError(429, "limit", "You've done that a lot this hour. Try again a bit later.")
    await owned_thread(state.pool, session, thread_id)
    posts, image_file_id, facts = await _draft(state.pool, session, thread_id, body.draft_id)

    key = f"{session.app}:{session.workspace}:{body.action_id}"
    async with state.pool.connection() as conn:
        claim = await conn.execute(_CLAIM, (key, session.app, session.workspace, thread_id, body.kind))
        if await claim.fetchone() is None:
            # Scoped by the columns too: a composed key with colons in it could match another workspace's row.
            cur = await conn.execute(
                "select result from actions where id = %s and app = %s and workspace = %s",
                (key, session.app, session.workspace),
            )
            row = await cur.fetchone()
            if row is None or row[0] is None:
                raise ApiError(409, "pending", "Still saving that. Try again in a moment.")
            return row[0]

    tools = state.tools_for(pack, session)
    try:
        media = await _image_url(state, session, thread_id, image_file_id) if image_file_id else []
        draft = await tools.save_draft(posts[0], posts[1:], media, key=body.action_id)
        if facts:
            await tools.save_material(facts, draft.id, key=body.action_id + ":facts")
        result: dict = {"draft_id": draft.id, "edit_url": draft.edit_url}
    except Exception as e:
        async with state.pool.connection() as conn:  # free the action so the user can tap again
            await conn.execute(
                "delete from actions where id = %s and app = %s and workspace = %s",
                (key, session.app, session.workspace),
            )
        if isinstance(e, ToolError):
            log.warning("action failed app=%s action=%s kind=%s", pack.id, body.kind, e.kind)
            raise ApiError(402 if e.kind == "out_of_credits" else 502, **tool_failure(e)) from None
        raise
    async with state.pool.connection() as conn:
        await conn.execute(
            "update actions set result = %s where id = %s and app = %s and workspace = %s",
            (Jsonb(result), key, session.app, session.workspace),
        )
        await conn.execute(
            "update pieces set added_draft_id = %s, edit_url = %s"
            " where id = %s and app = %s and workspace = %s and thread_id = %s",
            (draft.id, draft.edit_url, body.draft_id, session.app, session.workspace, thread_id),
        )
    log.info("action app=%s thread=%s action=%s", pack.id, thread_id, body.kind)
    return result


# ---- reads ----


@router.get("/threads")
async def threads(request: Request, auth: Auth) -> list[dict]:
    _, session = auth
    async with request.app.state.pool.connection() as conn:
        cur = await conn.execute(
            "select id, title, updated_at from threads where app = %s and workspace = %s"
            " order by updated_at desc, id desc limit %s",
            (session.app, session.workspace, THREADS_LISTED),
        )
        rows = await cur.fetchall()
    return [{"id": r[0], "title": r[1], "updated_at": r[2].isoformat()} for r in rows]


async def _noop(*_args) -> None:
    raise RuntimeError("read-only graph")


def _message(m) -> dict | None:
    role = "user" if isinstance(m, HumanMessage) else "assistant" if isinstance(m, AIMessage) else None
    if role is None:
        return None
    text = (
        m.content
        if isinstance(m.content, str)
        else " ".join(p.get("text", "") for p in m.content if isinstance(p, dict))
    )
    return {"role": role, "text": text}


@router.get("/threads/{thread_id}")
async def thread(thread_id: str, request: Request, auth: Auth) -> dict:
    pack, session = auth
    state = request.app.state
    await owned_thread(state.pool, session, thread_id)
    graph = build_graph(
        pack, state.llm, lambda: state.tools_for(pack, session), state.checkpointer, _noop, _noop, _noop
    )
    config = {"configurable": {"thread_id": graph_thread_id(session.app, session.workspace, thread_id)}}
    snapshot = await graph.aget_state(config)
    async with state.pool.connection() as conn:
        cur = await conn.execute(
            "select id, message_id, spec_kind, posts, text, image_file_id, added_draft_id, edit_url from pieces"
            " where app = %s and workspace = %s and thread_id = %s order by created_at, id",
            (session.app, session.workspace, thread_id),
        )
        draft_rows = await cur.fetchall()
        cur = await conn.execute(
            "select message_id, items from saved_lines where app = %s and workspace = %s and thread_id = %s"
            " order by created_at, id",
            (session.app, session.workspace, thread_id),
        )
        saved_rows = await cur.fetchall()
    # The turn's message id is the app's charge ref: it only groups rows here and never leaves the bot (the browser
    # first sees it in `done`, after the charge).
    drafts: dict[str, list[dict]] = {}
    for r in draft_rows:
        drafts.setdefault(r[1], []).append(_draft_out(r))
    saved: dict[str, list[str]] = {}
    for turn_id, items in saved_rows:
        saved.setdefault(turn_id, []).extend(items)

    out: list[dict] = []
    current: str | None = None
    shown: dict | None = None  # the current turn's assistant message, once there is one

    def attach(msg: dict) -> None:
        if drafts.get(current):
            msg["drafts"] = drafts.pop(current)
        if saved.get(current):
            msg["saved"] = saved.pop(current)

    def close() -> None:
        """A turn that showed drafts or saved facts but said nothing still gets an assistant message to hold them."""
        if current is not None and shown is None and (drafts.get(current) or saved.get(current)):
            msg = {"role": "assistant", "text": ""}
            attach(msg)
            out.append(msg)

    for m in snapshot.values.get("messages", []):
        msg = _message(m)
        if msg is None:
            continue
        if msg["role"] == "user":
            close()
            current, shown = m.additional_kwargs.get("turn"), None
            out.append(msg)
        else:
            if shown is None:
                attach(msg)
                shown = msg
            out.append(msg)
    close()
    return {"messages": out}


def _draft_out(r) -> dict:
    posts = r[3] if r[3] else [r[4]]
    draft: dict = {"id": r[0], "kind": r[2], "posts": posts, "added": r[6] is not None, "edit_url": r[7]}
    if r[5] is not None:
        draft["image_file_id"] = r[5]
    return draft


@router.delete("/threads/{thread_id}")
async def delete_thread(thread_id: str, request: Request, auth: Auth) -> dict:
    """Removes one thread of the session's workspace: checkpoints, rows (files, drafts, saved lines) and storage."""
    pack, session = auth
    state = request.app.state
    if not await hit(
        state.pool, f"thread-delete:{session.app}:{session.workspace}", timedelta(hours=1), THREAD_DELETES_PER_HOUR
    ):
        raise ApiError(429, "limit", "You've deleted a lot this hour. Try again a bit later.")
    await owned_thread(state.pool, session, thread_id)
    await state.checkpointer.adelete_thread(graph_thread_id(session.app, session.workspace, thread_id))
    async with state.pool.connection() as conn, conn.transaction():
        # the rows of files, actions, pieces and saved_lines go with it (on delete cascade)
        await conn.execute(
            "delete from threads where id = %s and app = %s and workspace = %s",
            (thread_id, session.app, session.workspace),
        )
    # Rows first, storage last: a retry after a storage failure still finds the thread gone but the objects by prefix.
    await asyncio.to_thread(state.storage.delete_prefix, f"{session.app}/{session.workspace}/{thread_id}/")
    log.info("thread deleted app=%s", pack.id)
    return {"deleted": True}


# ---- deletion (server to server) ----


@workspaces.delete("/v1/apps/{app}/workspaces/{workspace}")
async def delete_workspace(app: str, workspace: str, request: Request) -> dict:
    """Removes everything the bot keeps for one workspace of an app: threads, checkpoints, files and their storage."""
    pack = pack_for(app, request)
    try:
        allowed = verify_delete_token(bearer(request), pack)
    except AuthError as e:
        raise ApiError(401, e.reason) from None
    if allowed != workspace:
        raise ApiError(403, "workspace")
    state = request.app.state
    async with state.pool.connection() as conn:
        cur = await conn.execute("select id from threads where app = %s and workspace = %s", (app, workspace))
        chat_ids = [r[0] for r in await cur.fetchall()]
    for chat_id in chat_ids:
        await state.checkpointer.adelete_thread(graph_thread_id(app, workspace, chat_id))
    async with state.pool.connection() as conn, conn.transaction():
        for table in ("actions", "pieces", "files", "threads"):
            await conn.execute(f"delete from {table} where app = %s and workspace = %s", (app, workspace))
    # Rows first, storage last: a retry after a storage failure still finds and deletes the objects by prefix.
    await asyncio.to_thread(state.storage.delete_prefix, f"{app}/{workspace}/")
    log.info("workspace deleted app=%s threads=%d", app, len(chat_ids))
    return {"deleted_threads": len(chat_ids)}
