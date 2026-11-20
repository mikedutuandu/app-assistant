"""The app's own tools, reached over MCP (JSON-RPC over HTTP) with the user's session token.

The bot never touches an app's database: it can only do what the signed-in user could.
"""

import itertools
import json
import re
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, ValidationError

from assistant.auth import Session
from assistant.checks import Fact
from assistant.packs import AppPack

TIMEOUT_SECONDS = 20
ToolErrorKind = Literal["out_of_credits", "unauthorized", "unavailable", "refused"]
_CODE_PREFIX = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*\s*:\s*")
_BARE_CODE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")


class ToolError(Exception):
    """A tool call failed. `user_message` is the app's own words when it gave any."""

    def __init__(self, kind: ToolErrorKind, user_message: str | None = None):
        super().__init__(kind)
        self.kind: ToolErrorKind = kind
        self.user_message = user_message
        self.cost_usd = 0.0  # set by the graph to what the turn had spent when the call failed


class KeptFact(BaseModel):
    """A fact the user kept in an earlier chat and no post has told yet."""

    id: str
    text: str


class AppContext(BaseModel):
    name: str | None = None  # the user's name, when the app knows it
    voice: str
    topics: list[str]
    recent_posts: list[str]
    patterns: list[str] = []
    credits_left: float | None
    out_of_credits_message: str | None
    language: str = "en"
    kept_facts: list[KeptFact] = []  # the user's own words, offered by the conversation model when relevant


class SavedDraft(BaseModel):
    id: str
    edit_url: str | None


class AppTools(Protocol):
    async def context(self) -> AppContext: ...

    async def save_draft(self, text: str, thread: list[str], media: list[dict], key: str) -> SavedDraft: ...

    async def save_material(self, facts: list[Fact], draft_id: str | None, key: str) -> None: ...

    async def charge(self, cost_usd: float, ref: str) -> None: ...


def _strip_code(text: str) -> str | None:
    text = text.strip()
    if _BARE_CODE.match(text):
        return None
    return _CODE_PREFIX.sub("", text).strip() or None


def _http_message(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        for field in ("message", "error"):
            if isinstance(body.get(field), str) and body[field].strip():
                return _strip_code(body[field])
    return None


class McpHttpTools:
    def __init__(self, pack: AppPack, session: Session, client: httpx.AsyncClient):
        self._pack = pack
        self._session = session
        self._client = client
        self._ids = itertools.count(1)

    async def context(self) -> AppContext:
        return self._model(AppContext, await self._call("context", {}))

    async def save_draft(self, text: str, thread: list[str], media: list[dict], key: str) -> SavedDraft:
        """`text` is the first post, `thread` the posts after it (left out for a single post)."""
        args: dict[str, Any] = {"text": text, "media": media}
        if thread:
            args["thread"] = thread
        return self._model(SavedDraft, await self._call("save_draft", args, key))

    async def save_material(self, facts: list[Fact], draft_id: str | None, key: str) -> None:
        args = {"facts": [f.model_dump() for f in facts], "draft_id": draft_id}
        await self._call("save_material", args, key)

    async def charge(self, cost_usd: float, ref: str) -> None:
        await self._call("charge", {"cost_usd": cost_usd, "ref": ref}, ref)

    @staticmethod
    def _model[T: BaseModel](model: type[T], text: str | None) -> T:
        try:
            return model.model_validate(json.loads(text or ""))
        except (ValueError, ValidationError) as exc:
            raise ToolError("refused") from exc

    async def _call(
        self,
        tool: Literal["context", "save_draft", "save_material", "charge"],
        arguments: dict[str, Any],
        key: str | None = None,
    ) -> str | None:
        """Returns the first text content of the result (None when absent)."""
        headers = {"Authorization": f"Bearer {self._session.token}"}
        if key is not None:
            headers["Idempotency-Key"] = key
        payload = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": "tools/call",
            "params": {"name": self._pack.tools[tool], "arguments": arguments},
        }
        response: httpx.Response | None = None
        for _attempt in range(2):
            try:
                response = await self._client.post(
                    self._pack.mcp_url, json=payload, headers=headers, timeout=TIMEOUT_SECONDS
                )
            except httpx.TransportError:
                response = None
                continue
            if response.status_code >= 500:
                continue
            break
        if response is None or response.status_code >= 500:
            raise ToolError("unavailable", _http_message(response) if response is not None else None)
        return self._interpret(response)

    @staticmethod
    def _interpret(response: httpx.Response) -> str | None:
        status = response.status_code
        if status in (401, 403):
            raise ToolError("unauthorized", _http_message(response))
        if status == 429:
            raise ToolError("unavailable", _http_message(response))
        if status == 402:
            raise ToolError("out_of_credits", _http_message(response))
        if status >= 400:
            raise ToolError("refused", _http_message(response))
        try:
            body = response.json()
        except ValueError as exc:
            raise ToolError("refused") from exc
        if not isinstance(body, dict):
            raise ToolError("refused")
        error = body.get("error")
        if error is not None:
            message = error.get("message") if isinstance(error, dict) else None
            message = message if isinstance(message, str) else None
            kind: ToolErrorKind = "out_of_credits" if message and "out_of_credits" in message else "refused"
            raise ToolError(kind, _strip_code(message) if message else None)
        result = body.get("result")
        if not isinstance(result, dict):
            raise ToolError("refused")
        content = result.get("content")
        text = None
        if isinstance(content, list) and content and isinstance(content[0], dict):
            first = content[0].get("text")
            text = first if isinstance(first, str) else None
        if result.get("isError"):
            kind = "out_of_credits" if text and "out_of_credits" in text else "refused"
            raise ToolError(kind, _strip_code(text) if text else None)
        return text
