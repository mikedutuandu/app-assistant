"""Checks an uploaded file by its real content against the app pack's caps, then stores it privately."""

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


class UploadError(Exception):
    def __init__(self, code: str, message: str, cost_usd: float = 0.0):
        super().__init__(message)
        self.code = code
        self.message = message
        self.cost_usd = cost_usd  # spent before the refusal (a too-long voice note was still transcribed)


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


async def accept_upload(
    pack: AppPack,
    session: Session,
    thread_id: str,
    name: str,  # accepted for the caller's convenience; deliberately not trusted or stored
    data: bytes,
    pool,
    storage: Storage,
) -> FileRecord:
    found = sniff(data)
    if found is None:
        raise UploadError("type", "This file type isn't supported here.")
    kind, mime = found
    if kind not in pack.files.kinds:
        raise UploadError("kind_off", f"This app doesn't take {_KIND_WORDS[kind]} files.")
    cap_mb = {"image": pack.files.image_mb, "pdf": pack.files.pdf_mb, "voice": VOICE_MAX_MB}[kind]
    if len(data) > cap_mb * 1024 * 1024:
        raise UploadError("too_big", f"{_BIG_WORDS[kind]} can be up to {cap_mb} MB.")

    file_id = new_id()
    key = f"{session.app}/{session.workspace}/{thread_id}/{file_id}"
    expires_at = datetime.now(UTC) + DEMO_FILE_TTL if pack.demo else None
    await asyncio.to_thread(storage.put, key, data, mime)
    try:
        async with pool.connection() as conn:
            await conn.execute(
                "insert into files (id, app, workspace, thread_id, kind, mime, bytes, storage_key, expires_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (file_id, session.app, session.workspace, thread_id, kind, mime, len(data), key, expires_at),
            )
    except Exception:
        await asyncio.to_thread(storage.delete_prefix, key)
        raise
    return FileRecord(id=file_id, kind=kind, mime=mime, bytes=len(data), storage_key=key)
