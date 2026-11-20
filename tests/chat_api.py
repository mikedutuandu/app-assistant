"""Helpers for the chat API tests: a pack, tokens, SSE parsing, and the `Api` client that the `api` fixture
(conftest.py) yields."""

import json
import time
from pathlib import Path

import httpx
import jwt
from fakes import FakeLlm, FakeTools, Reply, call
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from assistant.auth import mint_session
from assistant.db import new_id
from assistant.files.storage import Storage
from assistant.packs import AppPack, Models, WriteSpec

SECRET = "k" * 64
ORIGIN = "https://app.example"
PNG = (Path(__file__).parent / "fixtures" / "shot.png").read_bytes()
FACT = "Replies go out in 2 minutes instead of 2 hours"
LAUNCH = "Auto-reply now answers in 2 minutes instead of 2 hours."
REPLY = "Here's a first go."
APP_DOWN = "The app couldn't be reached. Try again in a minute."
OUR_FAULT = "Something went wrong on our side. Nothing was saved."
OK = {"unsupported": []}


def make_pack(pack_id: str = "real", demo: bool = False) -> AppPack:
    return AppPack(
        id=pack_id,
        name="Test app",
        mcp_url="https://app.example/mcp",
        secret_env="TEST_CHAT_SECRET",
        allowed_origins=[ORIGIN],
        guide="Ask what shipped and for one number.",
        writes=[
            WriteSpec(
                kind="x_post",
                count=1,
                max_chars=280,
                no_links=True,
                no_dashes=True,
                may_carry_image=True,
                instructions="A short launch post.",
            ),
            WriteSpec(kind="x_thread", count=1, max_chars=280, max_posts=4, instructions="A short thread."),
        ],
        tools={"context": "c", "save_draft": "s", "save_material": "m", "charge": "ch"},
        models=Models(
            chat="chat/model", writer="writer/model", main="main/model", cheap="cheap/model", vision="vis/model"
        ),
        files={"per_message": 2, "kinds": ["image", "voice"]} if demo else {},
        message_usd_cap=0.05,
        demo=demo,
    )


def happy_script() -> dict:
    """The conversation model saves the fact and writes one post, which passes; then it says one line: 4 calls."""
    write = [call("save_facts", facts=[{"text": FACT}]), call("write_drafts", brief="faster replies", kind="x_post")]
    return {
        "CONVERSE": [Reply(calls=write), REPLY],
        "DRAFT_WRITER": [{"drafts": [{"posts": [LAUNCH], "fact_ids": ["F1"], "image_file_id": None}]}],
        "CLAIM_CHECK": [OK],
    }


def ask_script() -> dict:
    return {"CONVERSE": ["What changed for users?"]}


def raw_token(app: str, workspace: str, access: str, ttl: int = 600) -> str:
    now = int(time.time())
    claims = {"iss": app, "sub": workspace, "access": access, "iat": now, "exp": now + ttl}
    return jwt.encode(claims, SECRET, algorithm="HS256")


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        kind, data = None, []
        for line in block.split("\n"):
            if line.startswith("event:"):
                kind = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        if kind:
            events.append((kind, json.loads("\n".join(data))))
    return events


class Api:
    def __init__(self, app, client: httpx.AsyncClient, pool: AsyncConnectionPool, storage: Storage, checkpointer):
        self.app = app
        self.client = client
        self.pool = pool
        self.storage = storage
        self.checkpointer = checkpointer
        self.llm = FakeLlm()
        self.tools = FakeTools()

    def headers(self, workspace: str = "ws1", app: str = "real", **extra: str) -> dict:
        token = mint_session(self.app.state.packs[app], workspace)
        return {"Authorization": f"Bearer {token}", "Origin": ORIGIN, **extra}

    async def send(
        self,
        text: str,
        thread: str | None = None,
        file_ids: list[str] | None = None,
        workspace: str = "ws1",
        app: str = "real",
        draft_id: str | None = None,
        new_thread: bool = False,
        **headers: str,
    ) -> list[tuple[str, dict]]:
        r = await self.post_message(text, thread, file_ids, workspace, app, draft_id, new_thread, **headers)
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/event-stream")
        return parse_sse(r.text)

    async def post_message(
        self,
        text: str,
        thread: str | None = None,
        file_ids: list[str] | None = None,
        workspace: str = "ws1",
        app: str = "real",
        draft_id: str | None = None,
        new_thread: bool = False,
        context: list[dict] | None = None,
        **headers: str,
    ) -> httpx.Response:
        body: dict = {"thread_id": thread, "text": text, "file_ids": file_ids or [], "draft_id": draft_id}
        if new_thread:
            body["new_thread"] = True
        if context is not None:
            body["context"] = context
        return await self.client.post(
            f"/v1/chat/{app}/messages", json=body, headers=self.headers(workspace, app, **headers)
        )

    async def start_upload(
        self,
        size: int,
        thread: str | None = None,
        workspace: str = "ws1",
        app: str = "real",
        mime: str = "image/png",
        name: str = "shot.png",
        new_thread: bool = False,
    ) -> httpx.Response:
        body: dict = {"thread_id": thread, "name": name, "mime": mime, "size": size}
        if new_thread:
            body["new_thread"] = True
        return await self.client.post(f"/v1/chat/{app}/uploads", json=body, headers=self.headers(workspace, app))

    async def finish(self, upload_id: str, thread: str, workspace: str = "ws1", app: str = "real") -> httpx.Response:
        return await self.client.post(
            f"/v1/chat/{app}/uploads/{upload_id}/finish",
            json={"thread_id": thread},
            headers=self.headers(workspace, app),
        )

    def put(self, started: dict, data: bytes, workspace: str = "ws1", app: str = "real") -> None:
        """The browser's PUT to the upload target, straight into moto's bucket."""
        key = f"{app}/{workspace}/{started['thread_id']}/{started['upload_id']}"
        self.storage.put(key, data, started["target"]["headers"]["content-type"])

    async def upload(
        self, data: bytes, thread: str | None = None, workspace: str = "ws1", app: str = "real", mime: str = "image/png"
    ) -> httpx.Response:
        """The widget's three steps: ask for a target, PUT the bytes there, then finish."""
        r = await self.start_upload(len(data), thread, workspace, app, mime)
        if r.status_code != 200:
            return r
        started = r.json()
        self.put(started, data, workspace, app)
        return await self.finish(started["upload_id"], started["thread_id"], workspace, app)

    async def act(self, thread: str, body: dict, workspace: str = "ws1") -> httpx.Response:
        return await self.client.post(
            f"/v1/chat/real/threads/{thread}/actions", json=body, headers=self.headers(workspace)
        )

    async def count(self, table: str, workspace: str = "ws1") -> int:
        async with self.pool.connection() as conn:
            cur = await conn.execute(f"select count(*) from {table} where workspace = %s", (workspace,))
            return (await cur.fetchone())[0]

    async def thread_row(self, workspace: str = "ws1", app: str = "real", title: str = "t") -> str:
        tid = new_id()
        async with self.pool.connection() as conn:
            await conn.execute(
                "insert into threads (id, app, workspace, title) values (%s, %s, %s, %s)", (tid, app, workspace, title)
            )
        return tid

    async def piece_row(
        self, thread: str, posts: list[str] | None = None, image_file_id: str | None = None, workspace: str = "ws1"
    ) -> str:
        """A stored draft, as a `draft` event leaves it."""
        pid = new_id()
        posts = posts or [LAUNCH]
        async with self.pool.connection() as conn:
            await conn.execute(
                "insert into pieces (id, app, workspace, thread_id, message_id, spec_kind, text, image_file_id,"
                " fact_ids, facts, posts) values (%s, 'real', %s, %s, 'm1', %s, %s, %s, '[]'::jsonb, '[]'::jsonb, %s)",
                (
                    pid,
                    workspace,
                    thread,
                    "x_post" if len(posts) == 1 else "x_thread",
                    posts[0],
                    image_file_id,
                    Jsonb(posts),
                ),
            )
        return pid
