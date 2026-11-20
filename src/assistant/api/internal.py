"""Endpoints called by Vercel Cron, not by browsers."""

import hmac

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from assistant.api.deps import ApiError
from assistant.demo.cleanup import cleanup_demo

router = APIRouter()


@router.get("/internal/cleanup")
async def cleanup(request: Request) -> JSONResponse:
    """The daily clean-up (vercel.json crons). Answers only to `Authorization: Bearer ${CRON_SECRET}`."""
    secret = request.app.state.settings.cron_secret
    if not secret:
        return JSONResponse({"error": "off"}, status_code=503)
    sent = request.headers.get("authorization", "")
    if not hmac.compare_digest(sent.encode(), f"Bearer {secret}".encode()):
        raise ApiError(401, "unauthorized")
    return JSONResponse(await cleanup_demo(request.app.state))
