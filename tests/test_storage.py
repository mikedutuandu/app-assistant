"""Storage against S3-compatible stores: the wire format of each call, and failures that must not be silent."""

import pytest
from botocore.awsrequest import AWSResponse
from botocore.stub import Stubber

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
