"""Turns an uploaded file (or a pasted link) into plain text the bot can ask back about and write from.

Everything returned here is data read from a file or a page, never instructions: the caller (the graph) wraps it.
No text read here is ever logged.
"""

import asyncio
import base64
import io
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpx
from psycopg.types.json import Jsonb
from pydantic import BaseModel
from selectolax.lexbor import LexborHTMLParser

from assistant.files.intake import FileRecord, UploadError, check_content
from assistant.files.storage import Storage
from assistant.graph.prompts import IMAGE_READER
from assistant.llm import Llm, LlmError
from assistant.packs import AppPack

MAX_TEXT = 12_000
CUT_NOTE = "\n[cut: the PDF is longer than this]"
MIN_PDF_CHARS_PER_PAGE = 200
RENDER_SCALE = 110 / 72
PDF_PAGES_AT_ONCE = 4  # a scanned PDF's pages are read this many at a time, to stay well inside Vercel's 300 s
IMAGE_MAX_TOKENS = 800
LINK_MAX_BYTES = 2 * 1024 * 1024
LINK_TIMEOUT = 10.0
LINK_MAX_REDIRECTS = 3
LINK_ERROR = "That link can't be opened."
FILE_CHANGED = "That file changed after it was checked. Upload it again."
_AUDIO_FORMATS = {
    "audio/mp4": "m4a",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/ogg": "ogg",
    "audio/webm": "webm",
}


class Meter(Protocol):
    """What a file read reports to (the turn): `gate` before each model call, which raises to stop the read (the
    message's cost cap), and `spend` as each call ends, with its cost (a failed call's too)."""

    async def spend(self, cost_usd: float) -> None: ...

    def gate(self) -> None: ...

    async def emit(self, kind: str, payload: dict) -> None: ...  # a stream event (`status`)


class FileReading(BaseModel):
    file_id: str
    kind: str  # "image" | "pdf" | "voice" | "link"
    text: str
    unsure: list[str] = []
    cost_usd: float = 0.0  # what reading it cost, for the record: already reported call by call through the Meter
    seconds: float | None = None


class ImageRead(BaseModel):
    text: str
    unsure: list[str]


async def _read_image(data: bytes, mime: str, pack: AppPack, llm: Llm) -> tuple[ImageRead, float]:
    url = f"data:{mime};base64,{base64.b64encode(data).decode()}"
    messages = [
        {"role": "system", "content": IMAGE_READER},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Read this image."},
                {"type": "image_url", "image_url": {"url": url}},
            ],
        },
    ]
    result, cost = await llm.chat_json(messages, pack.models.vision, pack.models.fallbacks, ImageRead, IMAGE_MAX_TOKENS)
    assert isinstance(result, ImageRead)
    return result, cost


async def _metered[T](meter: Meter, call: Callable[[], Awaitable[tuple[T, float]]]) -> tuple[T, float]:
    """One model call: the cap is checked first and the cost reported as soon as it ends. An LlmError leaves with
    no cost on it, since its cost has been reported already."""
    meter.gate()
    try:
        out, cost = await call()
    except LlmError as e:
        await meter.spend(e.cost_usd)
        raise LlmError(e.kind, str(e)) from None
    await meter.spend(cost)
    return out, cost


def _extract_pdf_text(data: bytes) -> tuple[str, int]:
    import pypdf  # imported here: most messages carry no file, and a cold start should not pay for it

    reader = pypdf.PdfReader(io.BytesIO(data))
    text = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    return text, len(reader.pages)


def _render_pages(data: bytes, limit: int) -> list[bytes]:
    import pypdfium2  # imported here for the same reason; PIL loads on the first render

    pdf = pypdfium2.PdfDocument(data)
    try:
        pages: list[bytes] = []
        for i in range(min(len(pdf), limit)):
            buf = io.BytesIO()
            pdf[i].render(scale=RENDER_SCALE).to_pil().save(buf, format="PNG")
            pages.append(buf.getvalue())
        return pages
    finally:
        pdf.close()


async def _read_voice(rec: FileRecord, data: bytes, pack: AppPack, llm: Llm, meter: Meter) -> FileReading:
    async def transcribe() -> tuple[tuple[str, float], float]:
        text, seconds, cost = await llm.transcribe(data, _AUDIO_FORMATS.get(rec.mime, "m4a"))
        return (text, seconds), cost

    (text, seconds), cost = await _metered(meter, transcribe)
    if seconds > pack.files.voice_seconds:  # refused, but the transcription is already charged
        minutes = f"{pack.files.voice_seconds / 60:g}"
        unit = "minute" if minutes == "1" else "minutes"
        raise UploadError("too_long", f"Voice notes can be up to {minutes} {unit}.")
    return FileReading(file_id=rec.id, kind="voice", text=text, cost_usd=cost, seconds=seconds)


async def _read_pdf(rec: FileRecord, data: bytes, pack: AppPack, llm: Llm, meter: Meter) -> FileReading:
    text, page_count = await asyncio.to_thread(_extract_pdf_text, data)
    if page_count and len(text) / page_count >= MIN_PDF_CHARS_PER_PAGE:
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT] + CUT_NOTE
        return FileReading(file_id=rec.id, kind="pdf", text=text)

    # A scanned PDF: one vision call per page, PDF_PAGES_AT_ONCE at a time. Each page is charged as it ends and
    # the cap is checked before each one starts. After a failure (or the cap) no page starts, the pages already
    # running finish (and are charged), and the first error is raised.
    pages = await asyncio.to_thread(_render_pages, data, pack.files.pdf_pages)
    slots = asyncio.Semaphore(PDF_PAGES_AT_ONCE)
    errors: list[Exception] = []

    async def read_page(png: bytes) -> tuple[ImageRead, float] | None:
        async with slots:
            if errors:
                return None
            try:
                return await _metered(meter, lambda: _read_image(png, "image/png", pack, llm))
            except Exception as e:  # noqa: BLE001 - kept and raised below, once the running pages are done
                errors.append(e)
                return None

    results = await asyncio.gather(*(read_page(png) for png in pages))
    if errors:
        raise errors[0]
    parts: list[str] = []
    unsure: list[str] = []
    spent = 0.0
    for n, result in enumerate(results, 1):
        assert result is not None
        read, cost = result
        spent += cost
        parts.append(f"Page {n}:\n{read.text}")
        unsure.extend(read.unsure)
    joined = "\n\n".join(parts)
    if len(joined) > MAX_TEXT:
        joined = joined[:MAX_TEXT] + CUT_NOTE
    return FileReading(file_id=rec.id, kind="pdf", text=joined, unsure=unsure, cost_usd=spent)


async def _checked_bytes(rec: FileRecord, pack: AppPack, storage: Storage) -> bytes:
    """The stored bytes, only if they are still what finish checked: the same size (read before the object is) and
    the same kind by content within the pack's caps. Otherwise UploadError, which the turn tells the user and skips."""
    if await asyncio.to_thread(storage.head, rec.storage_key) != rec.bytes:
        raise UploadError("file", FILE_CHANGED)
    data = await asyncio.to_thread(storage.get, rec.storage_key)
    try:
        kind, _mime = check_content(pack, data)
    except UploadError:
        raise UploadError("file", FILE_CHANGED) from None
    if len(data) != rec.bytes or kind != rec.kind:
        raise UploadError("file", FILE_CHANGED)
    return data


async def read_file(rec: FileRecord, pack: AppPack, llm: Llm, storage: Storage, pool, meter: Meter) -> FileReading:
    """The file's text, read once and kept on its row. Every model call is reported through `meter` as it ends."""
    async with pool.connection() as conn:
        row = await (await conn.execute("select reading from files where id = %s", (rec.id,))).fetchone()
    if row and row[0]:
        return FileReading.model_validate(row[0]).model_copy(update={"cost_usd": 0.0})

    data = await _checked_bytes(rec, pack, storage)
    if rec.kind == "voice":
        reading = await _read_voice(rec, data, pack, llm, meter)
    elif rec.kind == "image":
        read, cost = await _metered(meter, lambda: _read_image(data, rec.mime, pack, llm))
        reading = FileReading(file_id=rec.id, kind="image", text=read.text, unsure=read.unsure, cost_usd=cost)
    else:
        reading = await _read_pdf(rec, data, pack, llm, meter)

    async with pool.connection() as conn:
        await conn.execute("update files set reading = %s where id = %s", (Jsonb(reading.model_dump()), rec.id))
    return reading


# ---- links ----

Resolver = Callable[[str], Awaitable[list[str]]]


async def _resolve(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _blocked(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
        or str(ip) == "169.254.169.254"
    )


async def _check_url(url: str, resolve: Resolver) -> None:
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise UploadError("link", LINK_ERROR) from None
    if parts.scheme not in ("http", "https") or not host or port not in (None, 80, 443):
        raise UploadError("link", LINK_ERROR)
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            addresses = await resolve(host)
        except (OSError, ValueError):
            raise UploadError("link", LINK_ERROR) from None
    if not addresses or any(_blocked(a) for a in addresses):
        raise UploadError("link", LINK_ERROR)


def _html_to_text(html: str) -> str:
    tree = LexborHTMLParser(html)
    tree.strip_tags(["script", "style", "nav", "header", "footer", "noscript"])
    node = tree.body or tree.root
    return node.text(separator=" ") if node else ""


async def _fetch(url: str, client: httpx.AsyncClient, resolve: Resolver) -> tuple[str, str]:
    for _ in range(LINK_MAX_REDIRECTS + 1):
        await _check_url(url, resolve)
        async with client.stream(
            "GET", url, headers={"Accept-Encoding": "identity"}, follow_redirects=False, timeout=LINK_TIMEOUT
        ) as resp:
            if resp.is_redirect:
                location = resp.headers.get("location")
                if not location:
                    raise UploadError("link", LINK_ERROR)
                url = urljoin(str(resp.url), location)
                continue
            if resp.status_code >= 400:
                raise UploadError("link", LINK_ERROR)
            ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype not in ("text/html", "text/plain"):
                raise UploadError("link", LINK_ERROR)
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body.extend(chunk)
                if len(body) > LINK_MAX_BYTES:
                    raise UploadError("link", LINK_ERROR)
            return ctype, bytes(body).decode(resp.charset_encoding or "utf-8", errors="replace")
    raise UploadError("link", LINK_ERROR)


async def read_link(url: str, client: httpx.AsyncClient, resolve: Resolver = _resolve) -> FileReading:
    """Page text for a pasted link. DNS rebinding between the address check and the connect is not prevented."""
    try:
        async with asyncio.timeout(LINK_TIMEOUT):
            ctype, body = await _fetch(url, client, resolve)
    except UploadError:
        raise
    except Exception:  # noqa: BLE001 - user-supplied URL boundary: anything else is just an unopenable link
        raise UploadError("link", LINK_ERROR) from None
    text = _html_to_text(body) if ctype == "text/html" else body
    text = re.sub(r"\s+", " ", text).strip()[:MAX_TEXT]
    return FileReading(file_id=url, kind="link", text=text)
