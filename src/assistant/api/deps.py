"""What every `/v1` request needs: its app pack, the origin check, the session token, the client IP, and CORS.

Errors are JSON bodies the widget reads (`{"error": ...}`), never FastAPI's `{"detail": ...}`.
"""

import logging
import re
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from assistant.auth import AuthError, Session, verify_session
from assistant.packs import AppPack

log = logging.getLogger("assistant.api")
SERVER_ERROR = {"error": "server", "message": "Something went wrong on our side."}
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


def pack_for(app: str, request: Request) -> AppPack:
    pack = request.app.state.packs.get(app)
    if pack is None:
        raise ApiError(404, "app")
    return pack


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
    pack = pack_for(app, request)
    origin = request.headers.get("origin")
    if origin is not None and not origin_allowed(pack, origin):
        raise ApiError(403, "origin")
    try:
        session = verify_session(bearer(request), pack)
    except AuthError as e:
        raise ApiError(401, e.reason) from None
    return pack, session


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
        pack = scope["app"].state.packs.get(match.group(1))
        allowed = origin is not None and pack is not None and origin_allowed(pack, origin)

        if scope["method"] == "OPTIONS" and b"access-control-request-method" in headers:
            if not allowed:
                response = JSONResponse({"error": "origin" if pack else "app"}, 403 if pack else 404)
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
