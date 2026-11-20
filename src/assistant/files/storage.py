"""Private S3-compatible object storage (Supabase Storage, GCS, R2, S3, MinIO). Sync boto3: call it with asyncio.to_thread."""

import boto3
from botocore.config import Config

from assistant.config import Settings


class StorageError(Exception):
    """The store answered, but did not do all it was asked."""


class Storage:
    def __init__(self, settings: Settings):
        self.bucket = settings.s3_bucket
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

    def signed_url(self, key: str, seconds: int = 300) -> str:
        return self._client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=seconds
        )

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
