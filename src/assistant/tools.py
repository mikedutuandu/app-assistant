"""The app's own tools, reached with the user's session token: over the four HTTPS endpoints of contract 1
(`RestHttpTools`, docs/integration.md) or over MCP (`McpHttpTools`, JSON-RPC over HTTP). Both fail the same way for
the same answers (`ToolError` kinds and the app's own words), with the same timeout and one retry.

The bot never touches an app's database: it can only do what the signed-in user could.
"""

import itertools
import json
import re
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, Field, ValidationError

from assistant.auth import Session
from assistant.checks import Fact
from assistant.packs import AppPack
from assistant.timing import TurnTimer

TIMEOUT_SECONDS = 20
ToolErrorKind = Literal["out_of_credits", "unauthorized", "unavailable", "refused"]
_CODE_PREFIX = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*\s*:\s*")
_BARE_CODE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")
# Contract 1 says `error` is a code: there, one plain word (`unauthorized`, `invalid`) is a code too, never shown.
_REST_CODE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")


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


def _strip_code(text: str, code: re.Pattern[str] = _BARE_CODE) -> str | None:
    text = text.strip()
    if code.match(text):
        return None
    return _CODE_PREFIX.sub("", text).strip() or None


def _http_message(response: httpx.Response, code: re.Pattern[str] = _BARE_CODE) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        for field in ("message", "error"):
            if isinstance(body.get(field), str) and body[field].strip():
                return _strip_code(body[field], code)
    return None


async def _post(
    client: httpx.AsyncClient, url: str, payload: dict, headers: dict[str, str], code: re.Pattern[str] = _BARE_CODE
) -> httpx.Response:
    """POSTs once, and once more after a 5xx or no answer (a timeout, a lost connection), with the same body and
    Idempotency-Key. `unavailable` when neither try got an answer below 500."""
    response: httpx.Response | None = None
    for _attempt in range(2):
        try:
            response = await client.post(
                url, json=payload, headers=headers, timeout=TIMEOUT_SECONDS, follow_redirects=False
            )
        except httpx.TransportError:
            response = None
            continue
        if response.status_code >= 500:
            continue
        break
    if response is None or response.status_code >= 500:
        raise ToolError("unavailable", _http_message(response, code) if response is not None else None)
    return response


def _raise_for_status(response: httpx.Response, code: re.Pattern[str] = _BARE_CODE) -> None:
    status = response.status_code
    kind: ToolErrorKind | None = None
    if status in (401, 403):
        kind = "unauthorized"
    elif status == 429:
        kind = "unavailable"
    elif status == 402:
        kind = "out_of_credits"
    elif status >= 400:
        kind = "refused"
    if kind is not None:
        raise ToolError(kind, _http_message(response, code))


def _headers(session: Session, key: str | None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {session.token}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def tools_for(pack: AppPack, session: Session, client: httpx.AsyncClient, timer: TurnTimer | None = None) -> AppTools:
    """The client for how the pack reaches its app: `api_base` (contract 1) or `mcp_url` (MCP)."""
    if pack.api_base is not None:
        return RestHttpTools(pack, session, client, timer)
    return McpHttpTools(pack, session, client, timer)


Endpoint = Literal["context", "drafts", "material", "charge"]


class _RestContext(BaseModel):
    """`/context`'s answer: only `language` is required (docs/integration.md)."""

    language: str = Field(min_length=1)
    name: str | None = None
    voice: str | None = None
    topics: list[str] = []
    recent_posts: list[str] = []
    patterns: list[str] = []
    kept_facts: list[KeptFact] = []
    credits_left: float | None = None
    out_of_credits_message: str | None = None

    def app_context(self) -> AppContext:
        return AppContext.model_validate({**self.model_dump(), "voice": self.voice or ""})


class _RestDraft(BaseModel):
    draft_id: str = Field(min_length=1)
    edit_url: str | None = None


class _RestOk(BaseModel):
    ok: Literal[True]


class RestHttpTools:
    """The app's four endpoints under `pack.api_base`: POST /context, /drafts, /material and /charge (contract 1)."""

    def __init__(self, pack: AppPack, session: Session, client: httpx.AsyncClient, timer: TurnTimer | None = None):
        assert pack.api_base is not None
        self.timer = timer  # when set, every call to the app is timed, named by its endpoint
        self._base = pack.api_base.rstrip("/")
        self._session = session
        self._client = client

    async def context(self) -> AppContext:
        return self._model(_RestContext, await self._call("context", {})).app_context()

    async def save_draft(self, text: str, thread: list[str], media: list[dict], key: str) -> SavedDraft:
        """`text` is the first post, `thread` the posts after it; each picture as `{url, alt, mime}`."""
        pictures = [{"url": m["url"], "alt": m.get("alt") or "", "mime": m.get("mime") or ""} for m in media]
        body = {"text": text, "thread": list(thread), "media": pictures}
        draft = self._model(_RestDraft, await self._call("drafts", body, key))
        return SavedDraft(id=draft.draft_id, edit_url=draft.edit_url)

    async def save_material(self, facts: list[Fact], draft_id: str | None, key: str) -> None:
        body: dict[str, Any] = {"facts": [f.text for f in facts]}
        if draft_id is not None:
            body["draft_id"] = draft_id
        self._model(_RestOk, await self._call("material", body, key))

    async def charge(self, cost_usd: float, ref: str) -> None:
        self._model(_RestOk, await self._call("charge", {"cost_usd": cost_usd, "ref": ref}, ref))

    @staticmethod
    def _model[T: BaseModel](model: type[T], body: Any) -> T:
        try:
            return model.model_validate(body)
        except ValidationError as exc:
            raise ToolError("refused") from exc

    async def _call(self, endpoint: Endpoint, body: dict[str, Any], key: str | None = None) -> Any:
        if self.timer is None:
            return await self._send(endpoint, body, key)
        async with self.timer.span("tool", endpoint):
            return await self._send(endpoint, body, key)

    async def _send(self, endpoint: Endpoint, body: dict[str, Any], key: str | None) -> Any:
        """The answer's JSON object; ToolError for anything else."""
        headers = _headers(self._session, key) | {"Accept": "application/json"}
        response = await _post(self._client, f"{self._base}/{endpoint}", body, headers, _REST_CODE)
        _raise_for_status(response, _REST_CODE)
        if not 200 <= response.status_code < 300:  # a redirect: never followed with the user's token
            raise ToolError("refused")
        try:
            answer = response.json()
        except ValueError as exc:
            raise ToolError("refused") from exc
        if not isinstance(answer, dict):
            raise ToolError("refused")
        return answer


class McpHttpTools:
    def __init__(self, pack: AppPack, session: Session, client: httpx.AsyncClient, timer: TurnTimer | None = None):
        self.timer = timer  # when set, every call to the app is timed, named by the app's tool name
        self._pack = pack
        self._session = session
        self._client = client
        self._ids = itertools.count(1)

    async def context(self) -> AppContext:
        return self._model(AppContext, await self._call("context", {}))

    async def save_draft(self, text: str, thread: list[str], media: list[dict], key: str) -> SavedDraft:
        """`text` is the first post, `thread` the posts after it (left out for a single post)."""
        # MCP apps get each picture as `{url, alt}`, as before contract 1 added `mime`.
        args: dict[str, Any] = {"text": text, "media": [{k: v for k, v in m.items() if k != "mime"} for m in media]}
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
        if self.timer is None:
            return await self._send(tool, arguments, key)
        async with self.timer.span("tool", self._pack.tools[tool]):
            return await self._send(tool, arguments, key)

    async def _send(
        self,
        tool: Literal["context", "save_draft", "save_material", "charge"],
        arguments: dict[str, Any],
        key: str | None,
    ) -> str | None:
        payload = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": "tools/call",
            "params": {"name": self._pack.tools[tool], "arguments": arguments},
        }
        assert self._pack.mcp_url is not None
        response = await _post(self._client, self._pack.mcp_url, payload, _headers(self._session, key))
        return self._interpret(response)

    @staticmethod
    def _interpret(response: httpx.Response) -> str | None:
        _raise_for_status(response)
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
