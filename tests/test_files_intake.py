from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from moto import mock_aws

from assistant.auth import Session
from assistant.config import Settings
from assistant.db import migrate, new_id
from assistant.files.intake import (
    UPLOAD_TTL,
    UploadError,
    UploadNotFound,
    check_claim,
    finish_upload,
    start_upload,
)
from assistant.files.storage import Storage
from assistant.packs import AppPack

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.4\n" + b"0" * 64
VOICE_M4A = (Path(__file__).parent / "fixtures" / "voice.m4a").read_bytes()


def _pack(demo: bool = False, kinds: list[str] | None = None, image_mb: int = 10) -> AppPack:
    return AppPack.model_validate(
        {
            "id": "demo" if demo else "real",
            "name": "Test",
            "mcp_url": "http://x/mcp",
            "secret_env": "X",
            "allowed_origins": [],
            "guide": "g",
            "writes": [{"kind": "x_post", "count": 1, "instructions": "i"}],
            "tools": {
                "context": "assistant_context",
                "save_draft": "save_draft",
                "save_material": "save_material",
                "charge": "assistant_charge",
            },
            "models": {"chat": "a", "writer": "b", "main": "a", "cheap": "b", "vision": "c"},
            "files": {
                "kinds": kinds or ["image", "voice"] if demo else ["image", "pdf", "voice"],
                "image_mb": image_mb,
            },
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


async def _upload(pack, thread, data, pool, storage, mime="image/png", claimed_size=None):
    """The widget's steps: a target, the browser's PUT (here straight to moto), then finish."""
    session = _session(pack)
    upload_id, _target = await start_upload(
        pack, session, thread, mime, len(data) if claimed_size is None else claimed_size, pool, storage
    )
    storage.put(f"{pack.id}/ws1/{thread}/{upload_id}", data, mime)
    return await finish_upload(pack, session, thread, upload_id, pool, storage)


async def _rows(pool) -> list[tuple]:
    async with pool.connection() as conn:
        cur = await conn.execute("select id, kind from files order by id")
        return await cur.fetchall()


def _keys(storage) -> list[str]:
    return [o["Key"] for o in storage._client.list_objects_v2(Bucket=storage.bucket).get("Contents", [])]


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
    rec = await _upload(pack, thread_id, PNG, pool, storage, mime="application/pdf")
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
        await _upload(pack, thread_id + "d", PDF, pool, storage, mime="image/png")
    assert err.value.code == "kind_off"
    assert err.value.message == "This app doesn't take PDF files."
    assert _keys(storage) == [] and await _rows(pool) == []  # the object is deleted and no row is kept


@pytest.mark.db
async def test_12mb_image_too_big_message_says_10_mb(pool, storage, thread_id):
    pack = _pack()
    with pytest.raises(UploadError) as err:  # it claimed to be small: the stored size is what counts
        await _upload(pack, thread_id, PNG + b"\x00" * (12 * 1024 * 1024), pool, storage, claimed_size=1000)
    assert err.value.code == "too_big"
    assert err.value.message == "Images can be up to 10 MB."
    assert _keys(storage) == [] and await _rows(pool) == []


@pytest.mark.db
async def test_unknown_content_is_type_error(pool, storage, thread_id):
    pack = _pack()
    with pytest.raises(UploadError) as err:
        await _upload(pack, thread_id, b"just some text here", pool, storage)
    assert (err.value.code, err.value.message) == ("type", "This file type isn't supported here.")
    assert _keys(storage) == [] and await _rows(pool) == []


@pytest.mark.db
async def test_iphone_m4a_accepted_as_voice(pool, storage, thread_id):
    pack = _pack()
    rec = await _upload(pack, thread_id, VOICE_M4A, pool, storage, mime="audio/x-m4a")
    assert (rec.kind, rec.mime) == ("voice", "audio/mp4")


@pytest.mark.db
async def test_demo_file_expires_in_a_day(pool, storage, thread_id):
    pack = _pack(demo=True)
    rec = await _upload(pack, thread_id + "d", PNG, pool, storage)
    async with pool.connection() as conn:
        cur = await conn.execute("select expires_at from files where id = %s", (rec.id,))
        (expires_at,) = await cur.fetchone()
    assert timedelta(hours=23, minutes=55) < expires_at - datetime.now(UTC) <= timedelta(days=1)


@pytest.mark.db
async def test_start_reserves_a_pending_row_for_15_minutes(pool, storage, thread_id):
    pack = _pack()
    upload_id, target = await start_upload(pack, _session(pack), thread_id, "image/png", 1234, pool, storage)
    assert len(upload_id) == 26
    assert f"/test-bucket/real/ws1/{thread_id}/{upload_id}?" in target["url"]
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select kind, mime, bytes, storage_key, expires_at from files where id = %s", (upload_id,)
        )
        kind, mime, size, key, expires_at = await cur.fetchone()
    assert (kind, mime, size, key) == ("pending", "image/png", 1234, f"real/ws1/{thread_id}/{upload_id}")
    assert UPLOAD_TTL == timedelta(minutes=15)
    assert timedelta(minutes=14, seconds=55) < expires_at - datetime.now(UTC) <= UPLOAD_TTL


def test_claim_is_checked_against_the_packs_own_caps():
    assert check_claim(_pack(), "image/PNG", 100) == "image/png"
    assert check_claim(_pack(), "audio/webm;codecs=opus", 100) == "audio/webm"
    assert check_claim(_pack(), "", 100) == "application/octet-stream"  # the browser may not know: content decides
    cases = [
        (_pack(), "text/html", 10, ("type", "This file type isn't supported here.")),
        (_pack(demo=True), "application/pdf", 10, ("kind_off", "This app doesn't take PDF files.")),
        (_pack(image_mb=5), "image/png", 6 * 1024 * 1024, ("too_big", "Images can be up to 5 MB.")),
        (_pack(), "application/pdf", 21 * 1024 * 1024, ("too_big", "PDFs can be up to 20 MB.")),
        (_pack(), "", 26 * 1024 * 1024, ("too_big", "That file is too big.")),
    ]
    for pack, mime, size, expected in cases:
        with pytest.raises(UploadError) as err:
            check_claim(pack, mime, size)
        assert (err.value.code, err.value.message) == expected, mime


@pytest.mark.db
async def test_finish_refuses_a_missing_object_and_keeps_the_upload_open(pool, storage, thread_id):
    pack = _pack()
    session = _session(pack)
    upload_id, _ = await start_upload(pack, session, thread_id, "image/png", len(PNG), pool, storage)
    with pytest.raises(UploadError) as err:
        await finish_upload(pack, session, thread_id, upload_id, pool, storage)
    assert (err.value.code, err.value.message) == ("missing", "The file didn't arrive. Try again.")
    storage.put(f"real/ws1/{thread_id}/{upload_id}", PNG, "image/png")
    rec = await finish_upload(pack, session, thread_id, upload_id, pool, storage)
    assert (rec.id, rec.kind) == (upload_id, "image")


@pytest.mark.db
async def test_finish_twice_gives_the_same_file(pool, storage, thread_id):
    pack = _pack()
    session = _session(pack)
    first = await _upload(pack, thread_id, PNG, pool, storage)
    again = await finish_upload(pack, session, thread_id, first.id, pool, storage)
    assert again == first
    assert await _rows(pool) == [(first.id, "image")]


@pytest.mark.db
async def test_finish_of_another_workspace_or_thread_is_not_found(pool, storage, thread_id):
    pack = _pack()
    session = _session(pack)
    upload_id, _ = await start_upload(pack, session, thread_id, "image/png", len(PNG), pool, storage)
    storage.put(f"real/ws1/{thread_id}/{upload_id}", PNG, "image/png")
    other = replace(session, workspace="ws2")
    for who, thread in [
        (other, thread_id),
        (session, new_id()),
        (replace(session, app="demo"), thread_id),
    ]:
        with pytest.raises(UploadNotFound):
            await finish_upload(pack, who, thread, upload_id, pool, storage)
    assert (await finish_upload(pack, session, thread_id, upload_id, pool, storage)).id == upload_id


@pytest.mark.db
async def test_finish_after_the_upload_expired_is_not_found_and_deletes_it(pool, storage, thread_id):
    pack = _pack()
    session = _session(pack)
    upload_id, _ = await start_upload(pack, session, thread_id, "image/png", len(PNG), pool, storage)
    storage.put(f"real/ws1/{thread_id}/{upload_id}", PNG, "image/png")
    async with pool.connection() as conn:
        await conn.execute("update files set expires_at = now() - interval '1 second' where id = %s", (upload_id,))
    with pytest.raises(UploadNotFound):
        await finish_upload(pack, session, thread_id, upload_id, pool, storage)
    assert _keys(storage) == [] and await _rows(pool) == []


@pytest.mark.db
async def test_an_object_over_every_cap_is_refused_unread(pool, storage, thread_id, monkeypatch):
    pack = _pack()

    def unread(_key):
        raise AssertionError("an object over every cap must not be read")

    monkeypatch.setattr(storage, "get", unread)
    with pytest.raises(UploadError) as err:
        await _upload(pack, thread_id, PNG + b"\x00" * (26 * 1024 * 1024), pool, storage, claimed_size=1000)
    assert (err.value.code, err.value.message) == ("too_big", "Images can be up to 10 MB.")
    assert _keys(storage) == [] and await _rows(pool) == []


@pytest.mark.db
async def test_the_real_kinds_cap_counts_not_the_claimed_one(pool, storage, thread_id):
    pack = _pack()
    rec = await _upload(pack, thread_id, PDF + b"0" * (15 * 1024 * 1024), pool, storage, claimed_size=1000)
    assert (rec.kind, rec.mime) == ("pdf", "application/pdf")  # a 15 MB PDF the browser called an image is a PDF


def test_storage_signed_url_and_delete_prefix(storage):
    for i in range(1, 4):
        storage.put(f"a/w/t/{i}", b"x", "image/png")
    storage.put("a/w/other/1", b"x", "image/png")
    assert "a/w/t/1" in storage.signed_url("a/w/t/1")
    assert storage.delete_prefix("a/w/t/") == 3
    assert storage.get("a/w/other/1") == b"x"
    assert storage.delete_prefix("a/w/t/") == 0
