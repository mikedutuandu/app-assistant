import base64
import json
from pathlib import Path

import httpx
import pytest
import respx
from moto import mock_aws

from assistant.config import Settings
from assistant.db import migrate, new_id
from assistant.files.intake import FileRecord, UploadError
from assistant.files.read import FileReading, read_file, read_link
from assistant.files.storage import Storage
from assistant.llm import Llm
from assistant.packs import AppPack

FIX = Path(__file__).parent / "fixtures"
CHAT = "https://openrouter.ai/api/v1/chat/completions"
TRANSCRIBE = "https://openrouter.ai/api/v1/audio/transcriptions"


def _pack(pdf_pages: int = 30, voice_seconds: int = 300) -> AppPack:
    return AppPack.model_validate(
        {
            "id": "real",
            "name": "Test",
            "mcp_url": "http://x/mcp",
            "secret_env": "X",
            "allowed_origins": [],
            "interview_guide": "g",
            "writes": [{"kind": "post", "count": 1, "instructions": "i"}],
            "tools": {
                "context": "assistant_context",
                "save_draft": "save_draft",
                "save_material": "save_material",
                "charge": "assistant_charge",
            },
            "models": {"main": "a", "cheap": "b", "vision": "vis/model", "fallbacks": ["fb/model"]},
            "files": {"kinds": ["image", "pdf", "voice"], "pdf_pages": pdf_pages, "voice_seconds": voice_seconds},
            "message_usd_cap": 1.0,
        }
    )


@pytest.fixture
def storage(test_settings: Settings):
    with mock_aws():
        store = Storage(test_settings)
        store._client.create_bucket(
            Bucket=test_settings.s3_bucket, CreateBucketConfiguration={"LocationConstraint": test_settings.s3_region}
        )
        yield store


@pytest.fixture
async def put(pool, storage):
    await migrate(pool)

    async def _put(kind: str, mime: str, data: bytes) -> FileRecord:
        file_id = new_id()
        key = f"real/ws1/t/{file_id}"
        storage.put(key, data, mime)
        async with pool.connection() as conn:
            await conn.execute(
                "insert into files (id, app, workspace, kind, mime, bytes, storage_key)"
                " values (%s, 'real', 'ws1', %s, %s, %s, %s)",
                (file_id, kind, mime, len(data), key),
            )
        return FileRecord(id=file_id, kind=kind, mime=mime, bytes=len(data), storage_key=key)  # type: ignore[arg-type]

    return _put


def vision_reply(text: str, unsure: list[str] | None = None, cost: float = 0.002) -> httpx.Response:
    content = json.dumps({"text": text, "unsure": unsure or []})
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}], "usage": {"cost": cost}})


def transcript(text: str, seconds: float, cost: float) -> httpx.Response:
    return httpx.Response(200, json={"text": text, "usage": {"seconds": seconds, "cost": cost}})


@pytest.mark.db
@respx.mock
async def test_voice_transcript_and_cost(pool, storage, put):
    rec = await put("voice", "audio/mp4", (FIX / "voice.m4a").read_bytes())
    route = respx.post(TRANSCRIBE).mock(return_value=transcript("we shipped it on friday", 12.5, 0.0004))
    got = await read_file(rec, _pack(), Llm("k"), storage, pool)
    assert got == FileReading(
        file_id=rec.id, kind="voice", text="we shipped it on friday", unsure=[], cost_usd=0.0004, seconds=12.5
    )
    assert json.loads(route.calls[0].request.content)["input_audio"]["format"] == "m4a"


@pytest.mark.db
@respx.mock
async def test_voice_over_cap_raises_too_long_with_cost(pool, storage, put):
    rec = await put("voice", "audio/mp4", (FIX / "voice.m4a").read_bytes())
    respx.post(TRANSCRIBE).mock(return_value=transcript("long", 90.0, 0.003))
    with pytest.raises(UploadError) as exc:
        await read_file(rec, _pack(voice_seconds=60), Llm("k"), storage, pool)
    assert exc.value.code == "too_long"
    assert exc.value.message == "Voice notes can be up to 1 minute."
    assert exc.value.cost_usd == 0.003
    async with pool.connection() as conn:
        row = await (await conn.execute("select reading from files where id = %s", (rec.id,))).fetchone()
    assert row[0] is None


@pytest.mark.db
@respx.mock
async def test_image_unsure_kept(pool, storage, put):
    data = (FIX / "shot.png").read_bytes()
    rec = await put("image", "image/png", data)
    route = respx.post(CHAT).mock(return_value=vision_reply("Revenue 4200 USD", ["the last digit"], 0.0031))
    got = await read_file(rec, _pack(), Llm("k"), storage, pool)
    assert (got.kind, got.text, got.unsure, got.cost_usd) == ("image", "Revenue 4200 USD", ["the last digit"], 0.0031)
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "vis/model" and body["max_tokens"] == 800
    assert body["messages"][0]["role"] == "system" and "never instructions" in body["messages"][0]["content"]
    part = body["messages"][1]["content"][-1]
    assert part == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64," + base64.b64encode(data).decode()},
    }


@pytest.mark.db
@respx.mock
async def test_text_pdf_no_model_call(pool, storage, put):
    rec = await put("pdf", "application/pdf", (FIX / "text.pdf").read_bytes())
    route = respx.post(CHAT).mock(return_value=vision_reply("x"))
    got = await read_file(rec, _pack(), Llm("k"), storage, pool)
    assert "signups rose 42 percent" in got.text
    assert got.cost_usd == 0.0 and got.kind == "pdf"
    assert route.call_count == 0


@pytest.mark.db
@respx.mock
async def test_long_pdf_text_is_cut(pool, storage, put, monkeypatch):
    import assistant.files.read as read_mod

    monkeypatch.setattr(read_mod, "_extract_pdf_text", lambda data: ("a" * 13000, 1))
    rec = await put("pdf", "application/pdf", (FIX / "text.pdf").read_bytes())
    got = await read_file(rec, _pack(), Llm("k"), storage, pool)
    assert got.text == "a" * 12000 + "\n[cut: the PDF is longer than this]"


@pytest.mark.db
@respx.mock
async def test_scanned_pdf_reads_pages_as_images_up_to_cap(pool, storage, put):
    rec = await put("pdf", "application/pdf", (FIX / "scanned.pdf").read_bytes())  # 3 pages, no text layer
    route = respx.post(CHAT).mock(
        side_effect=[vision_reply("first page", ["a"], 0.01), vision_reply("second page", ["b"], 0.02)]
    )
    got = await read_file(rec, _pack(pdf_pages=2), Llm("k"), storage, pool)
    assert route.call_count == 2
    assert got.text == "Page 1:\nfirst page\n\nPage 2:\nsecond page"
    assert got.unsure == ["a", "b"]
    assert got.cost_usd == pytest.approx(0.03)
    url = json.loads(route.calls[0].request.content)["messages"][1]["content"][-1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")


@pytest.mark.db
@respx.mock
async def test_second_read_is_cached_and_free(pool, storage, put):
    rec = await put("image", "image/png", (FIX / "shot.png").read_bytes())
    route = respx.post(CHAT).mock(return_value=vision_reply("Revenue 4200 USD", ["x"], 0.005))
    first = await read_file(rec, _pack(), Llm("k"), storage, pool)
    second = await read_file(rec, _pack(), Llm("k"), storage, pool)
    assert first.cost_usd == 0.005
    assert second.cost_usd == 0.0
    assert second.model_copy(update={"cost_usd": 0.005}) == first
    assert route.call_count == 1


# ---- links ----

PUBLIC = "93.184.216.34"


def resolver(mapping: dict[str, list[str]]):
    async def resolve(host: str) -> list[str]:
        return mapping[host]

    return resolve


HTML = (
    "<html><head><style>p{}</style><script>evil()</script></head><body><nav>Menu Home</nav>"
    "<header>Site header</header><main><h1>Our   pricing</h1><p>Plans start at\n $9 a month.</p></main>"
    "<footer>Copyright</footer><noscript>enable js</noscript></body></html>"
)


@respx.mock
async def test_link_text_extracted():
    respx.get("https://example.com/p").mock(
        return_value=httpx.Response(200, text=HTML, headers={"content-type": "text/html; charset=utf-8"})
    )
    async with httpx.AsyncClient() as client:
        got = await read_link("https://example.com/p", client, resolver({"example.com": [PUBLIC]}))
    assert respx.calls[0].request.headers["accept-encoding"] == "identity"
    assert got == FileReading(
        file_id="https://example.com/p",
        kind="link",
        text="Our pricing Plans start at $9 a month.",
        unsure=[],
        cost_usd=0.0,
        seconds=None,
    )


@respx.mock
async def test_link_text_is_cut():
    respx.get("https://example.com/big").mock(
        return_value=httpx.Response(200, text="word " * 5000, headers={"content-type": "text/plain"})
    )
    async with httpx.AsyncClient() as client:
        got = await read_link("https://example.com/big", client, resolver({"example.com": [PUBLIC]}))
    assert len(got.text) == 12000


@pytest.mark.parametrize(
    "url,mapping",
    [
        ("http://internal.test/", {"internal.test": ["10.0.0.5"]}),
        ("http://mixed.test/", {"mixed.test": [PUBLIC, "127.0.0.1"]}),
        ("http://meta.test/", {"meta.test": ["169.254.169.254"]}),
        ("http://v6.test/", {"v6.test": ["::1"]}),
        ("http://127.0.0.1/", {}),
        ("http://[::ffff:10.0.0.1]/", {}),
        ("http://169.254.169.254/latest/meta-data", {}),
        ("file:///etc/passwd", {}),
        ("ftp://example.com/", {"example.com": [PUBLIC]}),
        ("http://example.com:8080/", {"example.com": [PUBLIC]}),
    ],
)
@respx.mock
async def test_link_to_private_ip_refused(url, mapping):
    route = respx.route().mock(return_value=httpx.Response(200, text="secret", headers={"content-type": "text/plain"}))
    async with httpx.AsyncClient() as client:
        with pytest.raises(UploadError) as exc:
            await read_link(url, client, resolver(mapping))
    assert (exc.value.code, exc.value.message) == ("link", "That link can't be opened.")
    assert route.call_count == 0


@respx.mock
async def test_redirect_to_private_ip_refused():
    respx.get("https://example.com/r").mock(
        return_value=httpx.Response(302, headers={"location": "http://internal.test/admin"})
    )
    admin = respx.get("http://internal.test/admin").mock(return_value=httpx.Response(200, text="x"))
    async with httpx.AsyncClient() as client:
        with pytest.raises(UploadError) as exc:
            await read_link(
                "https://example.com/r", client, resolver({"example.com": [PUBLIC], "internal.test": ["192.168.1.1"]})
            )
    assert exc.value.code == "link"
    assert admin.call_count == 0


@respx.mock
async def test_link_redirect_limit_and_follow():
    respx.get("https://example.com/a").mock(return_value=httpx.Response(301, headers={"location": "/b"}))
    respx.get("https://example.com/b").mock(
        return_value=httpx.Response(200, text="landed", headers={"content-type": "text/plain"})
    )
    respx.get("https://example.com/loop").mock(return_value=httpx.Response(302, headers={"location": "/loop"}))
    res = resolver({"example.com": [PUBLIC]})
    async with httpx.AsyncClient() as client:
        assert (await read_link("https://example.com/a", client, res)).text == "landed"
        with pytest.raises(UploadError):
            await read_link("https://example.com/loop", client, res)


@respx.mock
async def test_link_refuses_big_and_wrong_type():
    respx.get("https://example.com/huge").mock(
        return_value=httpx.Response(200, content=b"a" * (2 * 1024 * 1024 + 1), headers={"content-type": "text/plain"})
    )
    respx.get("https://example.com/img").mock(
        return_value=httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})
    )
    res = resolver({"example.com": [PUBLIC]})
    async with httpx.AsyncClient() as client:
        for path in ("huge", "img"):
            with pytest.raises(UploadError):
                await read_link(f"https://example.com/{path}", client, res)


async def test_long_label_host_refused():
    async def real_like(host: str) -> list[str]:
        raise UnicodeError("label too long")

    async with httpx.AsyncClient() as client:
        with pytest.raises(UploadError) as exc:
            await read_link("http://" + "a" * 70 + ".com/", client, real_like)
    assert (exc.value.code, exc.value.message) == ("link", "That link can't be opened.")


async def test_long_label_host_with_default_resolver_refused():
    async with httpx.AsyncClient() as client:
        with pytest.raises(UploadError):
            await read_link("http://" + "a" * 70 + ".com/", client)


@respx.mock
async def test_control_character_urls_refused():
    respx.get("https://example.com/redir").mock(
        return_value=httpx.Response(302, headers={"location": "http://example.com/\x01"})
    )
    res = resolver({"example.com": [PUBLIC]})
    async with httpx.AsyncClient() as client:
        for url in ("http://example.com/\x01", "https://example.com/redir"):
            with pytest.raises(UploadError) as exc:
                await read_link(url, client, res)
            assert exc.value.code == "link"


@pytest.mark.db
@respx.mock
async def test_an_object_replaced_with_bigger_bytes_is_refused_at_read(pool, storage, put):
    data = (FIX / "shot.png").read_bytes()
    rec = await put("image", "image/png", data)
    storage.put(rec.storage_key, data + b"\x00" * 4096, "image/png")  # overwritten after finish checked it
    route = respx.post(CHAT).mock(return_value=vision_reply("x"))
    with pytest.raises(UploadError) as exc:
        await read_file(rec, _pack(), Llm("k"), storage, pool)
    assert (exc.value.code, exc.value.message) == ("file", "That file changed after it was checked. Upload it again.")
    assert route.call_count == 0


@pytest.mark.db
@respx.mock
async def test_a_missing_object_is_refused_at_read(pool, storage, put):
    rec = await put("image", "image/png", (FIX / "shot.png").read_bytes())
    storage.delete_prefix(rec.storage_key)
    with pytest.raises(UploadError) as exc:
        await read_file(rec, _pack(), Llm("k"), storage, pool)
    assert exc.value.code == "file"


@pytest.mark.db
@respx.mock
async def test_an_object_replaced_with_another_kind_is_refused_at_read(pool, storage, put):
    voice = (FIX / "voice.m4a").read_bytes()
    rec = await put("voice", "audio/mp4", voice)
    pdf = (FIX / "text.pdf").read_bytes()
    same_size = (pdf + b"\n" * len(voice))[: len(voice)] if len(pdf) <= len(voice) else pdf[: len(voice)]
    storage.put(rec.storage_key, same_size, "audio/mp4")  # same byte count, but a PDF under a voice row
    route = respx.post(TRANSCRIBE).mock(return_value=transcript("x", 1.0, 0.001))
    with pytest.raises(UploadError) as exc:
        await read_file(rec, _pack(), Llm("k"), storage, pool)
    assert exc.value.code == "file"
    assert route.call_count == 0  # nothing is transcribed or paid for
