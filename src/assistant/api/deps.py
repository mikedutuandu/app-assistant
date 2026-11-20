"""What every `/v1` request needs: its app pack, the origin check, the session token, the client IP, and CORS.

Errors are JSON bodies the widget reads (`{"error": ...}`), never FastAPI's `{"detail": ...}`. Packs come from
`app.state.packs` (a PackStore): an app nobody registered is 404 `app`; a registered app whose pack can't be had right
now is 503 `unavailable` (the chat route answers it as a `failed` event instead).
"""

import logging
import re
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from assistant.auth import AuthError, Session, verify_session
from assistant.packs import AppKey, AppPack

log = logging.getLogger("assistant.api")
SERVER_ERROR = {"error": "server", "message": "Something went wrong on our side."}
APP_DOWN = "The app couldn't be reached. Try again in a minute."
_CHAT_PATH = re.compile(r"^/v1/chat/([^/]+)/")
_CORS_HEADERS = [
    (b"access-control-allow-methods", b"GET, POST, DELETE"),
    (b"access-control-allow-headers", b"Authorization, Content-Type"),
    (b"access-control-max-age", b"600"),
]


class ApiError(Exception):
    """An HTTP error with the JSON body the widget reads."""

    def __init__(self, status: int, error: str, message: str | None = None):
        super().__init__(error)
        self.status = status
        self.body: dict[str, str] = {"error": error} | ({"message": message} if message is not None else {})


class Unavailable(ApiError):
    """A registered app whose pack can't be had now: none fetched, no stored copy, no local pack."""

    def __init__(self) -> None:
        super().__init__(503, "unavailable", APP_DOWN)


async def key_for(app: str, request: Request) -> AppKey:
    """What checks an app's tokens: its pack, or its registry entry while the pack is unavailable."""
    store = request.app.state.packs
    key: AppKey | None = await store.get(app) or store.entry(app)
    if key is None:
        raise ApiError(404, "app")
    return key


async def allowed_origins(store: Any, app: str) -> list[str] | None:
    """The origins the app's chat may be called from; None for an app nobody registered. While a registered app's
    pack is unavailable, its site's origin, so the widget can read why."""
    pack = await store.get(app)
    if pack is not None:
        return pack.allowed_origins
    entry = store.entry(app)
    return [entry.origin] if entry is not None else None


def origin_allowed(pack: AppPack, origin: str) -> bool:
    return origin != "" and origin in pack.allowed_origins


def bearer(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise ApiError(401, "invalid")
    return token.strip()


async def session_for(app: str, request: Request) -> tuple[AppPack, Session]:
    """The pack and the signed-in session of a widget request.

    An Origin header must be one of the pack's origins. A request without one (not a browser) is let through:
    the bearer token, not a cookie, is what grants access, so there is no cross-site request to forge.
    """
    store = request.app.state.packs
    pack = await store.get(app)
    if pack is None:
        entry = store.entry(app)
        if entry is None:
            raise ApiError(404, "app")
        _verified(request, entry)  # a bad token is 401 as always, before saying the app is unavailable
        raise Unavailable()
    origin = request.headers.get("origin")
    if origin is not None and not origin_allowed(pack, origin):
        raise ApiError(403, "origin")
    return pack, _verified(request, pack)


async def session_or_unavailable(app: str, request: Request) -> tuple[AppPack, Session] | None:
    """`session_for`, but None (not 503) for an unavailable app: the chat route answers that as a `failed` event."""
    try:
        return await session_for(app, request)
    except Unavailable:
        return None


def _verified(request: Request, key: AppKey) -> Session:
    try:
        return verify_session(bearer(request), key)
    except AuthError as e:
        raise ApiError(401, e.reason) from None


def client_ip(request: Request) -> str:
    """The visitor's IP as Vercel's edge reports it: `x-vercel-forwarded-for`, then `x-real-ip`, then the rightmost
    `x-forwarded-for` entry (the one our own proxy appended), else the socket's peer."""
    for name in ("x-vercel-forwarded-for", "x-real-ip"):
        value = request.headers.get(name, "").split(",")[0].strip()
        if value:
            return value
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        last = forwarded.split(",")[-1].strip()
        if last:
            return last
    return request.client.host if request.client else ""


Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]


class ChatCors:
    """CORS for `/v1/chat/{app}/...`: exactly the pack's own origins, never "*".

    A pure ASGI middleware, so it never buffers the streamed (SSE) responses.
    """

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        match = _CHAT_PATH.match(scope.get("path", "")) if scope["type"] == "http" else None
        if match is None:
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        origin = headers.get(b"origin", b"").decode("latin-1") if b"origin" in headers else None
        origins = await allowed_origins(scope["app"].state.packs, match.group(1))
        known = origins is not None
        allowed = origin is not None and origin != "" and origin in (origins or [])

        if scope["method"] == "OPTIONS" and b"access-control-request-method" in headers:
            if not allowed:
                response = JSONResponse({"error": "origin" if known else "app"}, 403 if known else 404)
                response.headers["Vary"] = "Origin"
                await response(scope, receive, send)
                return
            start_headers = [
                (b"access-control-allow-origin", origin.encode("latin-1")),  # type: ignore[union-attr]
                *_CORS_HEADERS,
                (b"vary", b"Origin"),
            ]
            await send({"type": "http.response.start", "status": 204, "headers": start_headers})
            await send({"type": "http.response.body", "body": b""})
            return

        async def send_with_cors(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                extra = [(b"vary", b"Origin")]
                if allowed:
                    extra.append((b"access-control-allow-origin", origin.encode("latin-1")))  # type: ignore[union-attr]
                message["headers"] = [*message.get("headers", []), *extra]
            await send(message)

        await self.app(scope, receive, send_with_cors)


class CatchAll:
    """Any unhandled error becomes a JSON 500 the widget can read, logged by its type and the path's ids only.

    No traceback is logged and nothing is re-raised (the server would log one): an exception message or a frame
    can hold the user's text. It sits inside ChatCors, so an allowed origin still gets its CORS header.
    """

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message: MutableMapping[str, Any]) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception as e:  # noqa: BLE001 - the last line of defence: answer in JSON, log the kind only
            log.error("unhandled error=%s method=%s path=%s", type(e).__name__, scope.get("method"), scope.get("path"))
            if not started:
                await JSONResponse(SERVER_ERROR, status_code=500)(scope, receive, send)


def install(app: FastAPI) -> None:
    """The JSON error handler, the catch-all and the CORS middleware (outermost)."""

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(exc.body, status_code=exc.status)

    app.add_middleware(CatchAll)
    app.add_middleware(ChatCors)  # added last, so it wraps CatchAll's 500s too
