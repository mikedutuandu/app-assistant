"""The local storage backend's own file routes: `PUT` and `GET /v1/files/{key}?exp=&sig=`.

The signed link is the whole permission (see files/signing.py): the browser PUTs an upload to the link `upload_target`
gave it, and an app reads a picture from the 5-minute link in a draft's `media`. Anything that is not exactly what the
link was made for is refused: another method, another key, an expired link, a key that climbs out of DATA_DIR, more
bytes than the upload was started for, another content type, a second write.
"""

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from assistant.api.deps import ApiError
from assistant.files.intake import PENDING
from assistant.files.signing import valid
from assistant.files.storage import StorageError

log = logging.getLogger("assistant.files")
router = APIRouter(prefix="/v1/files")
MAX_BYTES = 26 * 1024 * 1024  # a bit over the largest cap of any kind (a 25 MB voice note), for a link with no row
_CORS = {"Access-Control-Allow-Origin": "*"}  # the signed link is the capability; no cookie or token goes with it


def _checked(request: Request, method: str, key: str) -> Any:
    """The local storage when the link is good for this method and key, else the ApiError to answer."""
    storage = request.app.state.storage
    if getattr(storage, "upload_backend", None) != "local":
        raise ApiError(404, "files")
    try:
        storage.object_path(key)  # 400 for a key that could leave DATA_DIR, before anything else is said
    except StorageError:
        raise ApiError(400, "key") from None
    params = request.query_params
    if not valid(storage.signing_secret, method, key, params.get("exp", ""), params.get("sig", "")):
        raise ApiError(403, "link", "This link is wrong or has expired.")
    return storage


@router.options("/{key:path}", include_in_schema=False)
async def preflight() -> Response:
    return Response(
        status_code=204,
        headers={
            **_CORS,
            "Access-Control-Allow-Methods": "GET, PUT",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "600",
        },
    )


@router.put("/{key:path}")
async def put_file(key: str, request: Request) -> Response:
    storage = _checked(request, "PUT", key)
    async with request.app.state.pool.connection() as conn:
        cur = await conn.execute("select kind, mime, bytes from files where storage_key = %s", (key,))
        row = await cur.fetchone()
    if row is None:
        raise ApiError(404, "upload")  # a link is made only for an upload the bot started
    kind, mime, size = row
    if kind != PENDING:
        raise ApiError(409, "exists", "This file was already uploaded.")
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != mime.lower():
        raise ApiError(415, "type", "The file's type is not the one this upload was started for.")
    limit = min(size, MAX_BYTES)
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise ApiError(413, "too_big", "The file is bigger than the upload was started for.")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise ApiError(413, "too_big", "The file is bigger than the upload was started for.")
    if not await asyncio.to_thread(storage.write_once, key, bytes(body), mime):
        raise ApiError(409, "exists", "This file was already uploaded.")
    return JSONResponse({"ok": True}, headers=_CORS)


@router.get("/{key:path}")
async def get_file(key: str, request: Request) -> Response:
    storage = _checked(request, "GET", key)
    path = storage.object_path(key)
    if not path.is_file():
        raise ApiError(404, "file")
    return FileResponse(
        path,
        media_type=storage.content_type(key),
        headers={
            **_CORS,
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
        },
    )
