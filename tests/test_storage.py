"""Storage against S3-compatible stores: the wire format of each call, and failures that must not be silent."""

import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import requests
import respx
from botocore.awsrequest import AWSResponse
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from chat_api import PNG
from moto import mock_aws

from assistant.config import Settings
from assistant.files.storage import Storage, StorageError
from assistant.main import cors_origins, create_app

SUPABASE = "https://ref.supabase.co/storage/v1/s3"


class _Raw:
    def __init__(self, body: bytes = b""):
        self.body = body

    def stream(self, **_kw):
        yield self.body

    def read(self, *_a, **_kw) -> bytes:
        return self.body


def _supabase(test_settings: Settings) -> Storage:
    return Storage(test_settings.model_copy(update={"s3_endpoint": SUPABASE, "s3_region": "ap-southeast-1"}))


def test_put_sends_a_plain_body_without_aws_chunked_checksums(test_settings: Settings):
    storage = _supabase(test_settings)
    sent: dict = {}

    def capture(request, **_kw):
        sent["url"] = request.url
        sent["headers"] = {k.lower(): v.decode() if isinstance(v, bytes) else v for k, v in request.headers.items()}
        return AWSResponse(request.url, 200, {}, _Raw())

    storage._client.meta.events.register("before-send.s3.PutObject", capture)
    storage.put("app/ws/t/f1", b"hello", "image/png")

    headers = sent["headers"]
    assert sent["url"] == f"{SUPABASE}/test-bucket/app/ws/t/f1"
    assert "aws-chunked" not in headers.get("content-encoding", "")
    assert "x-amz-trailer" not in headers
    assert "transfer-encoding" not in headers
    assert "STREAMING" not in headers["x-amz-content-sha256"]
    assert headers["content-length"] == "5"


def test_signed_url_is_sigv4_on_a_regional_s3_compatible_endpoint(test_settings: Settings):
    url = _supabase(test_settings).signed_url("app/ws/t/f1", 300)
    assert url.startswith(f"{SUPABASE}/test-bucket/app/ws/t/f1?")
    assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url
    assert "AWSAccessKeyId=" not in url


def test_delete_prefix_raises_when_the_store_reports_errors(test_settings: Settings):
    storage = _supabase(test_settings)
    with Stubber(storage._client) as stub:
        stub.add_response(
            "list_objects_v2",
            {"Contents": [{"Key": "a/1"}, {"Key": "a/2"}], "IsTruncated": False},
            {"Bucket": "test-bucket", "Prefix": "a/"},
        )
        stub.add_response(
            "delete_objects",
            {"Errors": [{"Key": "a/2", "Code": "AccessDenied", "Message": "no"}]},
            {"Bucket": "test-bucket", "Delete": {"Objects": [{"Key": "a/1"}, {"Key": "a/2"}], "Quiet": True}},
        )
        with pytest.raises(StorageError, match="1 of 2") as raised:
            storage.delete_prefix("a/")
    assert "AccessDenied" in str(raised.value)


def test_delete_prefix_counts_when_the_store_reports_no_errors(test_settings: Settings):
    storage = _supabase(test_settings)
    with Stubber(storage._client) as stub:
        stub.add_response(
            "list_objects_v2",
            {"Contents": [{"Key": "a/1"}], "IsTruncated": False},
            {"Bucket": "test-bucket", "Prefix": "a/"},
        )
        stub.add_response(
            "delete_objects",
            {"Errors": []},
            {"Bucket": "test-bucket", "Delete": {"Objects": [{"Key": "a/1"}], "Quiet": True}},
        )
        assert storage.delete_prefix("a/") == 1


# ---- direct uploads ----

SUPABASE_URL = "https://ref.supabase.co"


@respx.mock
def test_upload_target_uses_supabase_signed_upload_when_configured(test_settings: Settings):
    storage = Storage(
        test_settings.model_copy(update={"supabase_url": SUPABASE_URL + "/", "supabase_service_key": "svc-key"})
    )
    sign = respx.post(f"{SUPABASE_URL}/storage/v1/object/upload/sign/test-bucket/real/ws1/t1/u1").mock(
        return_value=httpx.Response(200, json={"url": "/object/upload/sign/test-bucket/real/ws1/t1/u1?token=tok"})
    )
    target = storage.upload_target("real/ws1/t1/u1", "image/png")
    assert target == {
        "url": f"{SUPABASE_URL}/storage/v1/object/upload/sign/test-bucket/real/ws1/t1/u1?token=tok",
        "method": "PUT",
        "headers": {"content-type": "image/png", "x-upsert": "false"},
    }
    sent = sign.calls.last.request
    assert sent.headers["authorization"] == "Bearer svc-key"
    assert sent.headers["apikey"] == "svc-key"
    assert sent.headers["x-upsert"] == "false"


@respx.mock
def test_upload_target_raises_when_supabase_refuses(test_settings: Settings):
    storage = Storage(test_settings.model_copy(update={"supabase_url": SUPABASE_URL, "supabase_service_key": "bad"}))
    respx.post(f"{SUPABASE_URL}/storage/v1/object/upload/sign/test-bucket/a/b").mock(
        return_value=httpx.Response(403, json={"error": "Unauthorized"})
    )
    with pytest.raises(StorageError, match="403"):
        storage.upload_target("a/b", "image/png")


@respx.mock
def test_upload_target_logs_supabase_reason(test_settings: Settings, caplog: pytest.LogCaptureFixture):
    storage = Storage(
        test_settings.model_copy(update={"supabase_url": SUPABASE_URL, "supabase_service_key": "sb_secret_x"})
    )
    body = {"statusCode": "404", "error": "Bucket not found", "message": "Bucket not found"}
    respx.post(f"{SUPABASE_URL}/storage/v1/object/upload/sign/test-bucket/a/b").mock(
        return_value=httpx.Response(400, json=body)
    )
    with caplog.at_level("WARNING", logger="assistant"), pytest.raises(StorageError, match="Bucket not found"):
        storage.upload_target("a/b", "image/png")
    assert "400" in caplog.text and "Bucket not found" in caplog.text
    assert "sb_secret_x" not in caplog.text


def test_upload_target_falls_back_to_s3_presign(test_settings: Settings):
    with mock_aws():
        storage = Storage(test_settings)
        storage._client.create_bucket(
            Bucket=test_settings.s3_bucket, CreateBucketConfiguration={"LocationConstraint": test_settings.s3_region}
        )
        target = storage.upload_target("real/ws1/t1/u1", "image/png")
        assert target["method"] == "PUT"
        assert target["headers"] == {"content-type": "image/png", "if-none-match": "*"}
        query = parse_qs(urlsplit(target["url"]).query)
        assert query["X-Amz-Expires"] == ["600"]
        signed = query["X-Amz-SignedHeaders"][0].split(";")
        assert "content-type" in signed and "if-none-match" in signed  # write-once: the store refuses an overwrite
        assert urlsplit(target["url"]).path.endswith("/test-bucket/real/ws1/t1/u1")

        assert storage.head("real/ws1/t1/u1") is None
        r = requests.put(target["url"], data=b"\x89PNG-bytes", headers=target["headers"], timeout=5)
        assert r.status_code == 200
        assert storage.head("real/ws1/t1/u1") == len(b"\x89PNG-bytes")
        again = requests.put(target["url"], data=b"other bytes, bigger", headers=target["headers"], timeout=5)
        assert again.status_code == 412  # the link can't replace the object finish checked
        assert storage.get("real/ws1/t1/u1") == b"\x89PNG-bytes"


def test_ensure_cors_sets_one_rule_for_the_origins(test_settings: Settings):
    storage = Storage(test_settings)
    with Stubber(storage._client) as stub:
        stub.add_response(
            "put_bucket_cors",
            {},
            {
                "Bucket": test_settings.s3_bucket,
                "CORSConfiguration": {
                    "CORSRules": [
                        {
                            "AllowedOrigins": ["https://a.example", "https://b.example"],
                            "AllowedMethods": ["GET", "PUT"],
                            "AllowedHeaders": ["content-type", "if-none-match"],
                            "MaxAgeSeconds": 3600,
                        }
                    ]
                },
            },
        )
        storage.ensure_cors(["https://b.example", "https://a.example", "https://a.example"])
        stub.assert_no_pending_responses()


def _lifespan_settings(test_settings: Settings, fresh_schema, **extra) -> Settings:
    url, schema = fresh_schema
    return Settings(
        **{
            **test_settings.model_dump(),
            "database_url": url,
            "db_schema": schema,
            "packs_dir": Path("apps"),
            "run_lifespan": True,
            "public_base_url": "https://bot.example/some/path",
            **extra,
        }
    )


@pytest.mark.db
async def test_cors_failure_does_not_stop_startup(test_settings, monkeypatch, fresh_schema, capsys):
    monkeypatch.setenv("DEMO_SESSION_SECRET", "d" * 64)
    monkeypatch.setenv("HOOKPLAYBOOK_SESSION_SECRET", "h" * 64)
    seen: list[list[str]] = []

    def refuse(self, origins):
        seen.append(origins)
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "PutBucketCors")

    monkeypatch.setattr(Storage, "ensure_cors", refuse)
    app = create_app(_lifespan_settings(test_settings, fresh_schema))
    async with app.router.lifespan_context(app):
        pass
    assert "https://bot.example" in seen[0] and all("/some/path" not in o for o in seen[0])
    assert capsys.readouterr().out.count("WARNING assistant.storage") == 1


@pytest.mark.db
async def test_cors_is_not_set_on_the_supabase_backend(test_settings, monkeypatch, fresh_schema):
    monkeypatch.setenv("DEMO_SESSION_SECRET", "d" * 64)
    monkeypatch.setenv("HOOKPLAYBOOK_SESSION_SECRET", "h" * 64)
    calls: list[list[str]] = []
    monkeypatch.setattr(Storage, "ensure_cors", lambda self, origins: calls.append(origins))
    settings = _lifespan_settings(
        test_settings, fresh_schema, supabase_url="https://ref.supabase.co", supabase_service_key="sb_secret_x"
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        pass
    assert calls == []


def test_cors_origins_drop_empty_wildcard_and_invalid_entries(test_settings: Settings):
    packs = {
        "a": SimpleNamespace(
            allowed_origins=["", "*", "not-a-url", "http://[bad", "ftp://x.example", "https://a.example/path"]
        ),
        "b": SimpleNamespace(allowed_origins=["http://localhost:3000"]),
    }
    settings = test_settings.model_copy(update={"public_base_url": "https://bot.example/demo"})
    found = [o for pack in packs.values() for o in pack.allowed_origins]
    assert cors_origins(found, settings) == ["https://a.example", "http://localhost:3000", "https://bot.example"]


@pytest.mark.db
async def test_a_hanging_cors_call_does_not_hold_up_startup(test_settings, monkeypatch, fresh_schema, capsys):
    monkeypatch.setenv("DEMO_SESSION_SECRET", "d" * 64)
    monkeypatch.setenv("HOOKPLAYBOOK_SESSION_SECRET", "h" * 64)
    release = threading.Event()
    monkeypatch.setattr(Storage, "ensure_cors", lambda self, origins: release.wait(5))
    monkeypatch.setattr("assistant.main.CORS_SECONDS", 0.1)
    app = create_app(_lifespan_settings(test_settings, fresh_schema))
    try:
        async with app.router.lifespan_context(app):
            pass
    finally:
        release.set()
    assert capsys.readouterr().out.count("WARNING assistant.storage") == 1


@pytest.mark.db
async def test_finish_checks_size_on_s3_backend(api):
    """The S3 backend's presigned PUT does not bind the size: finish must read the stored size and refuse a big one."""
    assert api.storage.upload_backend == "s3"
    tid = await api.thread_row()
    started = (await api.start_upload(1000, thread=tid)).json()  # it promised 1000 bytes
    api.put(started, PNG + b"\x00" * (11 * 1024 * 1024))
    r = await api.finish(started["upload_id"], tid)
    assert (r.status_code, r.json()["error"]) == (400, "too_big")
    assert api.storage._client.list_objects_v2(Bucket=api.storage.bucket).get("Contents", []) == []
