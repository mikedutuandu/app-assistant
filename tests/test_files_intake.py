from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from moto import mock_aws

from assistant.auth import Session
from assistant.config import Settings
from assistant.db import migrate, new_id
from assistant.files.intake import UploadError, accept_upload
from assistant.files.storage import Storage
from assistant.packs import AppPack

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.4\n" + b"0" * 64
VOICE_M4A = (Path(__file__).parent / "fixtures" / "voice.m4a").read_bytes()


def _pack(demo: bool = False, kinds: list[str] | None = None) -> AppPack:
    return AppPack.model_validate(
        {
            "id": "demo" if demo else "real",
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
            "models": {"main": "a", "cheap": "b", "vision": "c"},
            "files": {"kinds": kinds or ["image", "voice"] if demo else ["image", "pdf", "voice"]},
            "message_usd_cap": 1.0,
            "demo": demo,
        }
    )


def _session(pack: AppPack) -> Session:
    return Session(
        app=pack.id,
        workspace="ws1",
        access="draft",
        token="t",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
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
async def thread_id(pool):
    await migrate(pool)
    tid = new_id()
    async with pool.connection() as conn:
        await conn.execute("insert into threads (id, app, workspace) values (%s, 'real', 'ws1')", (tid,))
        await conn.execute("insert into threads (id, app, workspace) values (%s, 'demo', 'ws1')", (tid + "d",))
    return tid


async def test_empty_s3_endpoint_loads_as_none(monkeypatch):
    for k, v in {
        "OPENROUTER_API_KEY": "k",
        "DATABASE_URL": "postgresql://x",
        "S3_ENDPOINT": "",
        "S3_BUCKET": "b",
        "S3_ACCESS_KEY": "a",
        "S3_SECRET_KEY": "s",
        "PUBLIC_BASE_URL": "http://x",
    }.items():
        monkeypatch.setenv(k, v)
    assert Settings(_env_file=None).s3_endpoint is None


@pytest.mark.db
async def test_png_named_pdf_is_by_content(pool, storage, thread_id):
    pack = _pack()
    rec = await accept_upload(pack, _session(pack), thread_id, "x.pdf", PNG, pool, storage)
    assert (rec.kind, rec.mime) == ("image", "image/png")
    assert rec.storage_key == f"real/ws1/{thread_id}/{rec.id}"
    assert storage.get(rec.storage_key) == PNG
    async with pool.connection() as conn:
        cur = await conn.execute("select kind, mime, bytes, expires_at from files where id = %s", (rec.id,))
        assert await cur.fetchone() == ("image", "image/png", len(PNG), None)


@pytest.mark.db
async def test_pdf_named_png_is_by_content(pool, storage, thread_id):
    pack = _pack(demo=True)
    with pytest.raises(UploadError) as err:
        await accept_upload(pack, _session(pack), thread_id + "d", "x.png", PDF, pool, storage)
    assert err.value.code == "kind_off"
    assert err.value.message == "This app doesn't take PDF files."


@pytest.mark.db
async def test_12mb_image_too_big_message_says_10_mb(pool, storage, thread_id):
    pack = _pack()
    with pytest.raises(UploadError) as err:
        await accept_upload(
            pack, _session(pack), thread_id, "big.png", PNG + b"\x00" * (12 * 1024 * 1024), pool, storage
        )
    assert err.value.code == "too_big"
    assert err.value.message == "Images can be up to 10 MB."


@pytest.mark.db
async def test_unknown_content_is_type_error(pool, storage, thread_id):
    pack = _pack()
    with pytest.raises(UploadError) as err:
        await accept_upload(pack, _session(pack), thread_id, "a.png", b"just some text here", pool, storage)
    assert (err.value.code, err.value.message) == ("type", "This file type isn't supported here.")


@pytest.mark.db
async def test_iphone_m4a_accepted_as_voice(pool, storage, thread_id):
    pack = _pack()
    rec = await accept_upload(pack, _session(pack), thread_id, "Voice Memo.m4a", VOICE_M4A, pool, storage)
    assert (rec.kind, rec.mime) == ("voice", "audio/mp4")


@pytest.mark.db
async def test_demo_file_expires_in_a_day(pool, storage, thread_id):
    pack = _pack(demo=True)
    rec = await accept_upload(pack, _session(pack), thread_id + "d", "a.png", PNG, pool, storage)
    async with pool.connection() as conn:
        cur = await conn.execute("select expires_at from files where id = %s", (rec.id,))
        (expires_at,) = await cur.fetchone()
    assert timedelta(hours=23, minutes=55) < expires_at - datetime.now(UTC) <= timedelta(days=1)


def test_storage_signed_url_and_delete_prefix(storage):
    for i in range(1, 4):
        storage.put(f"a/w/t/{i}", b"x", "image/png")
    storage.put("a/w/other/1", b"x", "image/png")
    assert "a/w/t/1" in storage.signed_url("a/w/t/1")
    assert storage.delete_prefix("a/w/t/") == 3
    assert storage.get("a/w/other/1") == b"x"
    assert storage.delete_prefix("a/w/t/") == 0
