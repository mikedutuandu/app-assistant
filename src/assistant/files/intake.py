"""Uploads in two steps: `start_upload` reserves a key and gives the browser a target to PUT the bytes to;
`finish_upload` checks the stored object by its real size and content against the app pack's caps."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Literal

import puremagic
from pydantic import BaseModel

from assistant.auth import Session
from assistant.db import new_id
from assistant.files.storage import Storage
from assistant.packs import AppPack

Kind = Literal["image", "pdf", "voice"]

# The sniffed extension of the best match -> (kind, canonical mime). The client's name and mime are never used.
_TYPES: dict[str, tuple[Kind, str]] = {
    ".png": ("image", "image/png"),
    ".jpg": ("image", "image/jpeg"),
    ".jpeg": ("image", "image/jpeg"),
    ".jfif": ("image", "image/jpeg"),
    ".webp": ("image", "image/webp"),
    ".gif": ("image", "image/gif"),
    ".pdf": ("pdf", "application/pdf"),
    ".webm": ("voice", "audio/webm"),
    ".ogg": ("voice", "audio/ogg"),
    ".oga": ("voice", "audio/ogg"),
    # An mp4 container is voice: iPhone voice notes are audio/mp4. There is no video kind.
    ".mp4": ("voice", "audio/mp4"),
    ".m4a": ("voice", "audio/mp4"),
    ".f4a": ("voice", "audio/mp4"),
    ".mp3": ("voice", "audio/mpeg"),
    ".wav": ("voice", "audio/wav"),
}
_KIND_WORDS: dict[str, str] = {"image": "image", "pdf": "PDF", "voice": "voice"}
_BIG_WORDS: dict[str, str] = {"image": "Images", "pdf": "PDFs", "voice": "Voice notes"}
VOICE_MAX_MB = 25  # voice length is checked after transcription; this only bounds the upload
DEMO_FILE_TTL = timedelta(days=1)
UPLOAD_TTL = timedelta(minutes=15)  # a reserved upload not finished by then is gone (outlives the 10-minute PUT)
PENDING = "pending"  # the files.kind of a reserved upload; never readable or sendable until it is finished
UNKNOWN_MIME = "application/octet-stream"
TOO_BIG = "That file is too big."
MISSING = "The file didn't arrive. Try again."

# What the browser may claim (lowercase, without parameters) -> the kind it claims. A fast refusal only: the stored
# bytes decide. Every type here is one `sniff` can accept, so a claim refused here could never have passed.
_CLAIMS: dict[str, Kind] = {mime: kind for kind, mime in _TYPES.values()} | {
    "image/jpg": "image",
    "image/pjpeg": "image",
    "audio/x-m4a": "voice",
    "audio/m4a": "voice",
    "audio/mp3": "voice",
    "audio/x-wav": "voice",
    "audio/wave": "voice",
    "audio/opus": "voice",
    "video/mp4": "voice",
    "video/webm": "voice",
}


class UploadError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class UploadNotFound(Exception):
    """No upload by that id in this app, workspace and thread (or it expired)."""


class FileRecord(BaseModel):
    id: str
    kind: Kind
    mime: str
    bytes: int
    storage_key: str


def sniff(data: bytes) -> tuple[Kind, str] | None:
    try:
        matches = puremagic.magic_string(data)
    except puremagic.PureError:
        return None
    if not matches:
        return None
    return _TYPES.get(matches[0].extension.lower())


def _cap_mb(pack: AppPack, kind: Kind) -> int:
    return {"image": pack.files.image_mb, "pdf": pack.files.pdf_mb, "voice": VOICE_MAX_MB}[kind]


def check_claim(pack: AppPack, mime: str, size: int) -> str:
    """The browser's claimed type and size against the pack's caps; returns the mime the upload target is signed with.

    An empty mime (the browser didn't know) passes against the largest cap: the stored content decides at finish.
    """
    base = mime.split(";", 1)[0].strip().lower()
    if not base:
        if size > max(_cap_mb(pack, k) for k in pack.files.kinds) * 1024 * 1024:
            raise UploadError("too_big", TOO_BIG)
        return UNKNOWN_MIME
    kind = _CLAIMS.get(base)
    if kind is None:
        raise UploadError("type", "This file type isn't supported here.")
    if kind not in pack.files.kinds:
        raise UploadError("kind_off", f"This app doesn't take {_KIND_WORDS[kind]} files.")
    cap_mb = _cap_mb(pack, kind)
    if size > cap_mb * 1024 * 1024:
        raise UploadError("too_big", f"{_BIG_WORDS[kind]} can be up to {cap_mb} MB.")
    return base


def check_content(pack: AppPack, data: bytes) -> tuple[Kind, str]:
    """The stored bytes by their real content against the pack's caps: (kind, canonical mime), or UploadError."""
    found = sniff(data)
    if found is None:
        raise UploadError("type", "This file type isn't supported here.")
    kind, mime = found
    if kind not in pack.files.kinds:
        raise UploadError("kind_off", f"This app doesn't take {_KIND_WORDS[kind]} files.")
    cap_mb = _cap_mb(pack, kind)
    if len(data) > cap_mb * 1024 * 1024:
        raise UploadError("too_big", f"{_BIG_WORDS[kind]} can be up to {cap_mb} MB.")
    return kind, mime


async def start_upload(
    pack: AppPack, session: Session, thread_id: str, mime: str, size: int, pool, storage: Storage
) -> tuple[str, dict]:
    """Reserves an upload in the thread: a pending `files` row for 15 minutes and a target for the browser's PUT.

    `mime` is what `check_claim` returned. Returns (upload_id, target); the upload id becomes the file id.
    """
    upload_id = new_id()
    key = f"{session.app}/{session.workspace}/{thread_id}/{upload_id}"
    async with pool.connection() as conn:
        await conn.execute(
            "insert into files (id, app, workspace, thread_id, kind, mime, bytes, storage_key, expires_at)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s, now() + %s)",
            (upload_id, session.app, session.workspace, thread_id, PENDING, mime, size, key, UPLOAD_TTL),
        )
    try:
        target = await asyncio.to_thread(storage.upload_target, key, mime)
    except Exception:
        async with pool.connection() as conn:
            await conn.execute("delete from files where id = %s and kind = %s", (upload_id, PENDING))
        raise
    return upload_id, target


async def _discard(upload_id: str, key: str, pool, storage: Storage) -> None:
    """A refused or expired upload: its object first, then its row, so a failed delete can be tried again."""
    await asyncio.to_thread(storage.delete_prefix, key)
    async with pool.connection() as conn:
        await conn.execute("delete from files where id = %s and kind = %s", (upload_id, PENDING))


async def finish_upload(
    pack: AppPack, session: Session, thread_id: str, upload_id: str, pool, storage: Storage
) -> FileRecord:
    """Checks the object the browser stored and turns the pending row into the file. A repeat gives the same file.

    Raises UploadNotFound (another app, workspace or thread, or expired), or UploadError: `missing` keeps the upload
    open for another try; any other refusal deletes the object and the row.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select kind, mime, bytes, storage_key, expires_at < now() from files"
            " where id = %s and app = %s and workspace = %s and thread_id = %s",
            (upload_id, session.app, session.workspace, thread_id),
        )
        row = await cur.fetchone()
    if row is None:
        raise UploadNotFound()
    kind, mime, size, key, expired = row
    if kind != PENDING:
        return FileRecord(id=upload_id, kind=kind, mime=mime, bytes=size, storage_key=key)
    if expired:
        await _discard(upload_id, key, pool, storage)
        raise UploadNotFound()

    stored = await asyncio.to_thread(storage.head, key)
    if stored is None:
        raise UploadError("missing", MISSING)
    # Over every cap of the pack: refused before it is read. Under it, the real kind's cap is checked once sniffed.
    if stored > max(_cap_mb(pack, k) for k in pack.files.kinds) * 1024 * 1024:
        await _discard(upload_id, key, pool, storage)
        claimed = _CLAIMS.get(mime)
        if claimed in pack.files.kinds:
            raise UploadError("too_big", f"{_BIG_WORDS[claimed]} can be up to {_cap_mb(pack, claimed)} MB.")
        raise UploadError("too_big", TOO_BIG)
    try:
        data = await asyncio.to_thread(storage.get, key)
        kind, mime = check_content(pack, data)
    except UploadError:
        await _discard(upload_id, key, pool, storage)
        raise

    expires_at = datetime.now(UTC) + DEMO_FILE_TTL if pack.demo else None
    async with pool.connection() as conn:
        await conn.execute(
            "update files set kind = %s, mime = %s, bytes = %s, expires_at = %s where id = %s and kind = %s",
            (kind, mime, len(data), expires_at, upload_id, PENDING),
        )
    return FileRecord(id=upload_id, kind=kind, mime=mime, bytes=len(data), storage_key=key)
