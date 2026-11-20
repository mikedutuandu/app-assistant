"""Private S3-compatible object storage (Supabase Storage, GCS, R2, S3, MinIO). Sync boto3 and httpx: call it with
asyncio.to_thread."""

from urllib.parse import quote

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import ClientError

from assistant.config import Settings


class StorageError(Exception):
    """The store answered, but did not do all it was asked."""


UPLOAD_SECONDS = 600  # an S3 presigned PUT lives 10 minutes (Supabase's own signed upload links live 2 hours)
_SIGN_TIMEOUT = 10.0
_MISSING = {"404", "NoSuchKey", "NotFound"}


class Storage:
    def __init__(self, settings: Settings):
        self.bucket = settings.s3_bucket
        self._supabase = (
            (settings.supabase_url.rstrip("/"), settings.supabase_service_key)
            if settings.supabase_url and settings.supabase_service_key
            else None
        )
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint or None,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
            config=Config(
                s3={"addressing_style": "path"},
                # SigV4 everywhere: with a named region boto3 would otherwise presign with SigV2.
                signature_version="s3v4",
                # boto3 >= 1.36 streams uploads as aws-chunked with a CRC32 trailer by default, which
                # S3-compatible stores (Supabase Storage, GCS) may refuse: send checksums only when an API needs one.
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    def put(self, key: str, data: bytes, mime: str) -> None:
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=mime)

    def get(self, key: str) -> bytes:
        return self._client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def head(self, key: str) -> int | None:
        """The object's size in bytes, or None when there is no such object."""
        try:
            return self._client.head_object(Bucket=self.bucket, Key=key)["ContentLength"]
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in _MISSING:
                return None
            raise

    def upload_target(self, key: str, mime: str) -> dict:
        """Where the browser PUTs one file's bytes: this key only, never over an object already there.

        On Supabase, its own signed upload link (the browser may call it: Supabase allows CORS on it); elsewhere, an S3
        presigned PUT. The browser must send exactly `headers` with the bytes.
        """
        if self._supabase is not None:
            base, service_key = self._supabase
            path = quote(f"{self.bucket}/{key}", safe="/")
            r = httpx.post(
                f"{base}/storage/v1/object/upload/sign/{path}",
                headers={"Authorization": f"Bearer {service_key}", "apikey": service_key, "x-upsert": "false"},
                timeout=_SIGN_TIMEOUT,
            )
            if r.status_code != 200:
                raise StorageError(f"signed upload link refused: {r.status_code}")
            url = r.json()["url"]  # relative to the Storage API: /object/upload/sign/{bucket}/{key}?token=...
            if not url.startswith(("https://", "http://")):
                url = f"{base}/storage/v1/{url.lstrip('/')}"
            return {"url": url, "method": "PUT", "headers": {"content-type": mime, "x-upsert": "false"}}
        url = self._client.generate_presigned_url(
            "put_object",
            # If-None-Match is signed, so the store refuses a PUT without it and refuses any PUT once the key exists:
            # the link writes once and can't replace the object finish checked.
            Params={"Bucket": self.bucket, "Key": key, "ContentType": mime, "IfNoneMatch": "*"},
            ExpiresIn=UPLOAD_SECONDS,
        )
        return {"url": url, "method": "PUT", "headers": {"content-type": mime, "if-none-match": "*"}}

    def signed_url(self, key: str, seconds: int = 300) -> str:
        return self._client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=seconds
        )

    def object_url(self, key: str) -> str:
        """The object's address without a signature, as signed URLs spell it: the endpoint's path, the bucket, the key."""
        return self.signed_url(key, 1).split("?", 1)[0]

    def delete_prefix(self, prefix: str) -> int:
        """Delete every object whose key starts with `prefix`; returns how many.

        Raises StorageError when the store reports any key it could not delete (DeleteObjects answers 200 even then).
        """
        deleted = 0
        for page in self._client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if not keys:
                continue
            response = self._client.delete_objects(Bucket=self.bucket, Delete={"Objects": keys, "Quiet": True})
            errors = response.get("Errors") or []
            if errors:
                codes = sorted({e.get("Code", "?") for e in errors})
                raise StorageError(f"could not delete {len(errors)} of {len(keys)} objects: {', '.join(codes)}")
            deleted += len(keys)
        return deleted
