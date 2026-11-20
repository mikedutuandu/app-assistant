"""Storage against S3-compatible stores: the wire format of each call, and failures that must not be silent."""

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import requests
import respx
from botocore.awsrequest import AWSResponse
from botocore.stub import Stubber
from moto import mock_aws

from assistant.config import Settings
from assistant.files.storage import Storage, StorageError

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
