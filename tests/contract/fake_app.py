"""A small app that serves the four endpoints of contract 1, written from docs/integration.md only.

It keeps everything in memory for one test: the profile `/context` answers, the drafts, the facts, the charges, the
credits left, and the answer of each Idempotency-Key per workspace (None while one is being worked on).
"""

import time
from typing import Any

import jwt
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

MAX_TTL_SECONDS = 3600
LEEWAY_SECONDS = 30
EDIT_BASE = "https://app.example/drafts/"
IN_FLIGHT = "Still saving that. Try again in a moment."
NO_CREDITS = "You're out of credits. Top up in Billing."


class Refused(Exception):
    def __init__(self, status: int, error: str, message: str | None = None):
        super().__init__(error)
        self.status = status
        self.body: dict[str, Any] = {"error": error} | ({"message": message} if message is not None else {})


class FakeApp:
    def __init__(
        self,
        app_id: str,
        secret: str,
        *,
        context: dict | None = None,
        credits_left: float | None = None,
        done: list[dict] = (),  # [{"path", "key", "answer"}] for the token's workspace
        in_flight: list[dict] = (),  # [{"path", "key"}]
        workspace: str | None = None,  # the workspace `done` and `in_flight` belong to
    ):
        self.app_id = app_id
        self.secret = secret
        self.profile = context if context is not None else {"language": "en"}
        self.credits_left = credits_left
        self.drafts: list[dict] = []
        self.materials: list[dict] = []
        self.charges: list[dict] = []
        self.keys: dict[tuple[str, str, str], dict | None] = {}
        for d in done:
            self.keys[(workspace or "", d["path"], d["key"])] = d["answer"]
        for f in in_flight:
            self.keys[(workspace or "", f["path"], f["key"])] = None
        self.app = self._build()

    def workspace(self, request: Request) -> str:
        """The token's `sub`, checked as the doc says: signature, iss, access, expiry, at most an hour long."""
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise Refused(401, "unauthorized")
        try:
            claims = jwt.decode(
                token,
                self.secret,
                algorithms=["HS256"],
                options={"require": ["iss", "sub", "iat", "exp"], "verify_exp": False},
                leeway=LEEWAY_SECONDS,
            )
        except jwt.InvalidTokenError:
            raise Refused(401, "unauthorized") from None
        if (
            claims["iss"] != self.app_id
            or claims.get("access") != "draft"
            or claims["exp"] <= time.time()
            or claims["exp"] - claims["iat"] > MAX_TTL_SECONDS
        ):
            raise Refused(401, "unauthorized")
        return claims["sub"]

    def _build(self) -> FastAPI:
        app = FastAPI()

        async def handle(request: Request, path: str, work, keyed: bool) -> JSONResponse:
            try:
                ws = self.workspace(request)
                try:
                    body = await request.json()
                except ValueError:
                    raise Refused(400, "invalid", "The body is not JSON.") from None
                if not isinstance(body, dict):
                    raise Refused(400, "invalid", "The body must be a JSON object.")
                if not keyed:
                    return JSONResponse(work(ws, body))
                key = request.headers.get("idempotency-key", "")
                if not key or len(key) > 200:
                    raise Refused(400, "invalid", "Idempotency-Key is missing.")
                slot = (ws, path, key)
                if slot in self.keys:
                    answer = self.keys[slot]
                    if answer is None:
                        raise Refused(409, "in_flight", IN_FLIGHT)
                    return JSONResponse(answer)
                self.keys[slot] = None  # claimed: a second request with this key now gets 409
                try:
                    answer = work(ws, body)
                except BaseException:
                    del self.keys[slot]  # nothing was done: the key may be tried again
                    raise
                self.keys[slot] = answer
                return JSONResponse(answer)
            except Refused as r:
                return JSONResponse(r.body, status_code=r.status)

        @app.post("/api/assistant/context")
        async def context(request: Request) -> JSONResponse:
            return await handle(request, "/context", lambda ws, body: self.profile, keyed=False)

        @app.post("/api/assistant/drafts")
        async def drafts(request: Request) -> JSONResponse:
            return await handle(request, "/drafts", self._draft, keyed=True)

        @app.post("/api/assistant/material")
        async def material(request: Request) -> JSONResponse:
            return await handle(request, "/material", self._material, keyed=True)

        @app.post("/api/assistant/charge")
        async def charge(request: Request) -> JSONResponse:
            return await handle(request, "/charge", self._charge, keyed=True)

        return app

    def _draft(self, ws: str, body: dict) -> dict:
        text, thread, media = body.get("text"), body.get("thread"), body.get("media")
        if not isinstance(text, str) or not text.strip():
            raise Refused(400, "invalid", "The draft is empty.")
        if not isinstance(thread, list) or not all(isinstance(p, str) for p in thread):
            raise Refused(400, "invalid", "thread must be a list of posts.")
        fields = ("url", "alt", "mime")
        if not isinstance(media, list) or not all(
            isinstance(m, dict) and all(isinstance(m.get(f), str) for f in fields) for m in media
        ):
            raise Refused(400, "invalid", "media must be a list of {url, alt, mime}.")
        draft_id = f"d_{len(self.drafts) + 1}"
        self.drafts.append({"workspace": ws, "id": draft_id, "text": text, "thread": thread, "media": media})
        return {"draft_id": draft_id, "edit_url": EDIT_BASE + draft_id}

    def _material(self, ws: str, body: dict) -> dict:
        facts, draft_id = body.get("facts"), body.get("draft_id")
        if not isinstance(facts, list) or not all(isinstance(f, str) for f in facts):
            raise Refused(400, "invalid", "facts must be a list of sentences.")
        if draft_id is not None and not isinstance(draft_id, str):
            raise Refused(400, "invalid", "draft_id must be a string.")
        self.materials.append({"workspace": ws, "facts": facts, "draft_id": draft_id})
        return {"ok": True}

    def _charge(self, ws: str, body: dict) -> dict:
        cost, ref = body.get("cost_usd"), body.get("ref")
        if isinstance(cost, bool) or not isinstance(cost, int | float) or cost < 0:
            raise Refused(400, "invalid", "cost_usd must be a number, 0 or more.")
        if not isinstance(ref, str) or not ref:
            raise Refused(400, "invalid", "ref is missing.")
        if self.credits_left is not None:
            if self.credits_left <= 0:
                raise Refused(402, "out_of_credits", NO_CREDITS)
            self.credits_left -= cost  # may go slightly below zero: a call is charged after it ran
        self.charges.append({"workspace": ws, "cost_usd": cost, "ref": ref})
        return {"ok": True}
