"""The widget's routes under `/v1/chat/{app}`, and the app's server-to-server workspace deletion."""

import asyncio
import json
import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from langchain_core.messages import AIMessage, HumanMessage
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, StringConstraints
from sse_starlette import EventSourceResponse, ServerSentEvent
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from assistant.api.deps import ApiError, bearer, client_ip, pack_for, session_for
from assistant.api.turn import TurnInput, start, tool_failure
from assistant.auth import AuthError, Session, verify_delete_token
from assistant.checks import Fact, numbers_in
from assistant.db import new_id
from assistant.files.intake import UploadError, accept_upload
from assistant.graph.build import build_graph
from assistant.graph.build import thread_id as graph_thread_id
from assistant.packs import AppPack
from assistant.tools import ToolError

log = logging.getLogger("assistant.api")

MAX_UPLOAD_BYTES = 26 * 1024 * 1024  # the largest cap (voice, 25 MB) plus room for the multipart wrapping
THREADS_LISTED = 30
UPLOAD_THREAD_TITLE = "Files"
PENDING_STALE = "2 minutes"  # a tap whose run died mid-way may be tried again after this

Auth = Annotated[tuple[AppPack, Session], Depends(session_for)]
router = APIRouter(prefix="/v1/chat/{app}")
workspaces = APIRouter()


def _too_big() -> ApiError:
    return ApiError(413, "too_big", "That file is too big.")


async def owned_thread(pool, session: Session, chat_id: str) -> None:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select 1 from threads where id = %s and app = %s and workspace = %s",
            (chat_id, session.app, session.workspace),
        )
        if await cur.fetchone() is None:
            raise ApiError(404, "thread")


# ---- uploads ----


async def _read_form(request: Request):
    """The multipart form, read with a running cap so an oversized body is refused before it is all in memory."""
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_UPLOAD_BYTES:
        raise _too_big()
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise ApiError(400, "form", "Send the file as multipart/form-data.")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_UPLOAD_BYTES:
            raise _too_big()

    async def stream():
        yield bytes(body)

    try:
        return await MultiPartParser(request.headers, stream(), max_files=1, max_fields=4).parse()
    except MultiPartException:
        raise ApiError(400, "form", "Send the file as multipart/form-data.") from None


@router.post("/files")
async def upload(request: Request, auth: Auth) -> dict:
    pack, session = auth
    state = request.app.state
    form = await _read_form(request)
    try:
        file = form.get("file")
        if not isinstance(file, UploadFile):
            raise ApiError(400, "no_file", "Attach a file.")
        data = await file.read()
    finally:
        await form.close()
    given = form.get("thread_id")
    chat_id = given if isinstance(given, str) and given else None
    created = chat_id is None
    if chat_id is None:
        chat_id = new_id()
        async with state.pool.connection() as conn:
            await conn.execute(
                "insert into threads (id, app, workspace, title) values (%s, %s, %s, %s)",
                (chat_id, session.app, session.workspace, UPLOAD_THREAD_TITLE),
            )
    else:
        await owned_thread(state.pool, session, chat_id)
    try:
        rec = await accept_upload(pack, session, chat_id, file.filename or "", data, state.pool, state.storage)
    except Exception as e:
        if created:  # an upload-first thread that got no file is not kept
            async with state.pool.connection() as conn:
                await conn.execute("delete from threads where id = %s", (chat_id,))
        if isinstance(e, UploadError):
            raise ApiError(400, e.code, e.message) from None
        raise
    log.info("upload app=%s thread=%s file=%s kind=%s bytes=%d", pack.id, chat_id, rec.id, rec.kind, rec.bytes)
    return {"file_id": rec.id, "kind": rec.kind, "thread_id": chat_id}


# ---- messages ----


class MessageIn(BaseModel):
    thread_id: str | None = None
    text: str = ""
    file_ids: list[str] = []


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
    if not text and not file_ids:
        raise ApiError(400, "empty", "Type a message or attach a file.")
    if len(file_ids) > pack.files.per_message:
        n = pack.files.per_message
        raise ApiError(400, "too_many_files", f"You can send up to {n} file{'s' if n != 1 else ''} at a time.")
    if body.thread_id is not None:
        await owned_thread(pool, session, body.thread_id)
    if file_ids:
        if body.thread_id is None:
            raise ApiError(404, "file")  # a file always belongs to a thread
        async with pool.connection() as conn:
            cur = await conn.execute(
                "select count(*) from files where id = any(%s) and app = %s and workspace = %s and thread_id = %s",
                (file_ids, session.app, session.workspace, body.thread_id),
            )
            if (await cur.fetchone())[0] != len(file_ids):
                raise ApiError(404, "file")
    turn = TurnInput(pack, session, body.thread_id, text, file_ids, client_ip(request))
    return EventSourceResponse(_events(start(request.app.state, turn)))


# ---- actions ----

ActionId = Annotated[str, StringConstraints(min_length=1, max_length=100)]


class AddDraftIn(BaseModel):
    action_id: ActionId
    kind: Literal["add_draft"]
    piece_id: str


class KeepFactsIn(BaseModel):
    action_id: ActionId
    kind: Literal["keep_facts"]
    facts: list[Fact] = Field(min_length=1, max_length=100)


ActionIn = Annotated[AddDraftIn | KeepFactsIn, Field(discriminator="kind")]

_CLAIM = f"""
insert into actions (id, app, workspace, thread_id, kind, result)
values (%s, %s, %s, %s, %s, 'null'::jsonb)
on conflict (id) do update set created_at = now()
where actions.result = 'null'::jsonb and actions.created_at < now() - interval '{PENDING_STALE}'
returning 1
"""


async def _piece(pool, session: Session, chat_id: str, piece_id: str) -> tuple[str, str | None, list[Fact]]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select text, image_file_id, facts, dropped from pieces"
            " where id = %s and app = %s and workspace = %s and thread_id = %s",
            (piece_id, session.app, session.workspace, chat_id),
        )
        row = await cur.fetchone()
    if row is None:
        raise ApiError(404, "piece")
    if row[3] is not None:
        raise ApiError(409, "dropped", "This post didn't pass the checks, so it can't be added.")
    return row[0], row[1], [Fact.model_validate(f) for f in row[2]]


async def _image_url(state, session: Session, file_id: str) -> list[dict]:
    async with state.pool.connection() as conn:
        cur = await conn.execute(
            "select storage_key from files where id = %s and app = %s and workspace = %s",
            (file_id, session.app, session.workspace),
        )
        row = await cur.fetchone()
    if row is None:
        return []
    return [{"url": await asyncio.to_thread(state.storage.signed_url, row[0], 300), "alt": ""}]


@router.post("/threads/{thread_id}/actions")
async def act(thread_id: str, body: ActionIn, request: Request, auth: Auth) -> dict:
    """A tap on "Add to Schedule" or "Keep for next week": runs once per action_id; a repeat gets the same result."""
    pack, session = auth
    state = request.app.state
    await owned_thread(state.pool, session, thread_id)
    piece = await _piece(state.pool, session, thread_id, body.piece_id) if isinstance(body, AddDraftIn) else None

    key = f"{session.app}:{session.workspace}:{body.action_id}"
    async with state.pool.connection() as conn:
        claim = await conn.execute(_CLAIM, (key, session.app, session.workspace, thread_id, body.kind))
        if await claim.fetchone() is None:
            cur = await conn.execute("select result from actions where id = %s", (key,))
            row = await cur.fetchone()
            if row is None or row[0] is None:
                raise ApiError(409, "pending", "Still saving that. Try again in a moment.")
            return row[0]

    tools = state.tools_for(pack, session)
    try:
        if piece is not None:
            text, image_file_id, facts = piece
            media = await _image_url(state, session, image_file_id) if image_file_id else []
            draft = await tools.save_draft(text, media, key=body.action_id)
            if facts:
                await tools.save_material(facts, draft.id, key=body.action_id + ":facts")
            result: dict = {"draft_id": draft.id, "edit_url": draft.edit_url}
        else:
            facts = [
                f.model_copy(update={"text": f.text.strip(), "numbers": numbers_in(f.text)})
                for f in body.facts  # type: ignore[union-attr]
                if f.text.strip()
            ]
            if not facts:
                raise ApiError(400, "empty", "There are no facts to keep.")
            await tools.save_material(facts, None, key=body.action_id)
            result = {"kept": len(facts)}
    except Exception as e:
        async with state.pool.connection() as conn:  # free the action so the user can tap again
            await conn.execute("delete from actions where id = %s", (key,))
        if isinstance(e, ToolError):
            log.warning("action failed app=%s action=%s kind=%s", pack.id, body.kind, e.kind)
            raise ApiError(402 if e.kind == "out_of_credits" else 502, **tool_failure(e)) from None
        raise
    async with state.pool.connection() as conn:
        await conn.execute("update actions set result = %s where id = %s", (Jsonb(result), key))
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
    out: dict = {"role": role, "text": text}
    if "card" in m.additional_kwargs:
        out["card"] = m.additional_kwargs["card"]
    return out


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
    messages = [m for m in map(_message, snapshot.values.get("messages", [])) if m is not None]
    async with state.pool.connection() as conn:
        cur = await conn.execute(
            "select id, message_id, spec_kind, text, image_file_id, fact_ids, facts, dropped, created_at from pieces"
            " where app = %s and workspace = %s and thread_id = %s order by created_at, id",
            (session.app, session.workspace, thread_id),
        )
        rows = await cur.fetchall()
    keys = ["id", "message_id", "spec_kind", "text", "image_file_id", "fact_ids", "facts", "dropped", "created_at"]
    pieces = [dict(zip(keys, r, strict=True)) | {"created_at": r[8].isoformat()} for r in rows]
    return {"messages": messages, "pieces": pieces}


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
