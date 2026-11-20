"""Private object storage: S3-compatible (Supabase Storage, GCS, R2, S3, MinIO) or, with no cloud account, files on
local disk. Sync boto3, httpx and file calls: call it with asyncio.to_thread."""

import logging
import os
import shutil
import time
from pathlib import Path
from urllib.parse import quote

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import ClientError

from assistant.config import Settings
from assistant.files.signing import sign


class StorageError(Exception):
    """The store answered, but did not do all it was asked."""


UPLOAD_SECONDS = 600  # an S3 presigned PUT lives 10 minutes (Supabase's own signed upload links live 2 hours)
_SIGN_TIMEOUT = 10.0
_MISSING = {"404", "NoSuchKey", "NotFound"}
log = logging.getLogger("assistant.storage")


def _reason(r: httpx.Response) -> str:
    """Supabase's own short reason for a refusal ("Bucket not found", "Invalid Compact JWS"), never the request."""
    try:
        body = r.json()
    except ValueError:
        return "no reason given"
    text = body.get("message") or body.get("error") if isinstance(body, dict) else None
    return str(text)[:200] if text else "no reason given"


def local_path(root: Path, key: str) -> Path:
    """Where a key lives under `root`. Refuses anything that could leave it: empty or absolute keys, `..` or `.`
    parts, empty parts (`a//b`), backslashes and NUL."""
    if not key or "\\" in key or "\x00" in key or key.startswith("/"):
        raise StorageError("bad key")
    if any(part in ("", ".", "..") for part in key.rstrip("/").split("/")):
        raise StorageError("bad key")
    path = (root / key).resolve()
    if not path.is_relative_to(root.resolve()):
        raise StorageError("bad key")
    return path


class Storage:
    def __init__(self, settings: Settings, signing_key: str | None = None):
        self.bucket = settings.s3_bucket
        self._local: Path | None = None
        if settings.storage_backend == "local":
            # Files under data_dir/objects, their content types under data_dir/types (same keys). Uploads and reads by
            # apps go through this bot's own signed /v1/files links (api/files.py).
            self.upload_backend = "local"
            self.bucket = "local"
            self._local = settings.data_dir
            self._base = settings.public_base_url.rstrip("/")
            self._secret = signing_key or settings.files_signing_key
            self._supabase = None
            self._client = None
            return
        self._supabase = (
            (settings.supabase_url.rstrip("/"), settings.supabase_service_key)
            if settings.supabase_url and settings.supabase_service_key
            else None
        )
        # Where the browser uploads: Supabase signed upload links, or an S3 presigned PUT when either SUPABASE_* is unset.
        self.upload_backend = "supabase" if self._supabase is not None else "s3"
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

    # -- local backend ------------------------------------------------------------------------------------------------

    @property
    def signing_secret(self) -> str:
        """The key the /v1/files links are signed with (local backend)."""
        if not self._secret:
            raise StorageError("no signing key for local files")
        return self._secret

    def _objects(self, key: str) -> Path:
        assert self._local is not None
        return local_path(self._local / "objects", key)

    def _types(self, key: str) -> Path:
        assert self._local is not None
        return local_path(self._local / "types", key)

    def object_path(self, key: str) -> Path:
        """The file of a key on disk (local backend), after the same checks as every other call."""
        return self._objects(key)

    def content_type(self, key: str) -> str:
        try:
            return self._types(key).read_text()
        except FileNotFoundError:
            return "application/octet-stream"

    def write_once(self, key: str, data: bytes, mime: str) -> bool:
        """Stores a new object; False, writing nothing, when the key already exists. Atomic: a reader never sees half."""
        path = self._objects(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
        tmp.write_bytes(data)
        try:
            os.link(tmp, path)  # fails when the key exists: the link writes once
        except FileExistsError:
            return False
        finally:
            tmp.unlink(missing_ok=True)
        types = self._types(key)
        types.parent.mkdir(parents=True, exist_ok=True)
        types.write_text(mime)
        return True

    def _sign_url(self, method: str, key: str, seconds: int) -> str:
        if not self._secret:
            raise StorageError("no signing key for local files")
        exp = int(time.time()) + seconds
        sig = sign(self._secret, method, key, exp)
        return f"{self._base}/v1/files/{quote(key, safe='/')}?exp={exp}&sig={sig}"

    # -- the three backends -------------------------------------------------------------------------------------------

    def put(self, key: str, data: bytes, mime: str) -> None:
        if self._local is not None:
            path = self._objects(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.parent / f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
            tmp.write_bytes(data)
            os.replace(tmp, path)
            types = self._types(key)
            types.parent.mkdir(parents=True, exist_ok=True)
            types.write_text(mime)
            return
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=mime)

    def get(self, key: str) -> bytes:
        if self._local is not None:
            try:
                return self._objects(key).read_bytes()
            except FileNotFoundError:
                raise StorageError("no such object") from None
        return self._client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def head(self, key: str) -> int | None:
        """The object's size in bytes, or None when there is no such object."""
        if self._local is not None:
            path = self._objects(key)
            return path.stat().st_size if path.is_file() else None
        try:
            return self._client.head_object(Bucket=self.bucket, Key=key)["ContentLength"]
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in _MISSING:
                return None
            raise

    def ensure_cors(self, origins: list[str]) -> None:
        """Let the browser PUT and GET from these origins: one rule, replacing the bucket's CORS configuration."""
        if self._local is not None:
            return  # /v1/files answers CORS itself
        self._client.put_bucket_cors(
            Bucket=self.bucket,
            CORSConfiguration={
                "CORSRules": [
                    {
                        "AllowedOrigins": sorted(set(origins)),
                        "AllowedMethods": ["GET", "PUT"],
                        "AllowedHeaders": ["content-type", "if-none-match"],
                        "MaxAgeSeconds": 3600,
                    }
                ]
            },
        )

    def upload_target(self, key: str, mime: str) -> dict:
        """Where the browser PUTs one file's bytes: this key only, never over an object already there.

        On Supabase, its own signed upload link (the browser may call it: Supabase allows CORS on it); elsewhere, an S3
        presigned PUT; on the local backend, a signed PUT to this bot's /v1/files for 10 minutes. The browser must send
        exactly `headers` with the bytes.
        """
        if self._local is not None:
            self._objects(key)  # a bad key fails here, not at the browser
            return {
                "url": self._sign_url("PUT", key, UPLOAD_SECONDS),
                "method": "PUT",
                "headers": {"content-type": mime},
            }
        if self._supabase is not None:
            base, service_key = self._supabase
            path = quote(f"{self.bucket}/{key}", safe="/")
            r = httpx.post(
                f"{base}/storage/v1/object/upload/sign/{path}",
                headers={"Authorization": f"Bearer {service_key}", "apikey": service_key, "x-upsert": "false"},
                timeout=_SIGN_TIMEOUT,
            )
            if r.status_code != 200:
                reason = _reason(r)
                log.warning("signed upload link refused: status=%s reason=%s", r.status_code, reason)
                raise StorageError(f"signed upload link refused: {r.status_code} {reason}")
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
        if self._local is not None:
            self._objects(key)
            return self._sign_url("GET", key, seconds)
        return self._client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=seconds
        )

    def object_url(self, key: str) -> str:
        """The object's address without a signature, as signed URLs spell it: the endpoint's path, the bucket, the key."""
        if self._local is not None:
            return f"{self._base}/v1/files/{quote(key, safe='/')}"
        return self.signed_url(key, 1).split("?", 1)[0]

    def delete_prefix(self, prefix: str) -> int:
        """Delete every object whose key starts with `prefix`; returns how many.

        Raises StorageError when the store reports any key it could not delete (DeleteObjects answers 200 even then).
        """
        if self._local is not None:
            return self._delete_local(prefix)
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

    def _delete_local(self, prefix: str) -> int:
        """A prefix is a folder ("a/b/": everything in it) or the start of one key's name ("a/b/f1" is that file)."""
        assert self._local is not None
        target = self._objects(prefix)  # refuses a bad prefix
        deleted = 0
        if target.is_dir() and prefix.endswith("/"):
            deleted = sum(1 for p in target.rglob("*") if p.is_file())
            shutil.rmtree(target)
            shutil.rmtree(self._types(prefix), ignore_errors=True)
        else:
            parent = target.parent
            if parent.is_dir():
                for p in list(parent.iterdir()):
                    if p.name.startswith(target.name) and not p.name.startswith(".") and p.is_file():
                        p.unlink()
                        self._types(str(p.relative_to((self._local / "objects").resolve()))).unlink(missing_ok=True)
                        deleted += 1
        return deleted
