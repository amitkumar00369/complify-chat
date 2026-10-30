"""File storage (async API): S3 in production, local folder for dev/tests (STORAGE_BACKEND).

Keys are the same on both backends (standards/IEC_60335/part1/..., reports/{id}/...), so data
copied from local storage to S3 keeps working without changing the database.
boto3 is synchronous -> calls run in a worker thread so the event loop is never blocked.
"""
import asyncio
import logging
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from app.config.settings import get_settings
from app.utils.exceptions import ConfigError, StorageError

logger = logging.getLogger(__name__)


class StorageBackend(Protocol):
    async def put(self, key: str, data: bytes, content_type: str = "application/pdf",
                  metadata: dict[str, str] | None = None) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def exists(self, key: str) -> bool: ...


class S3Storage:
    def __init__(self, bucket: str, region: str, access_key: str | None,
                 secret_key: str | None, endpoint_url: str | None = None, client=None):
        import boto3

        self.bucket = bucket
        self.client = client or boto3.client(
            "s3",
            region_name=region,
            aws_access_key_id=access_key or None,       # None -> default AWS chain (IAM role etc.)
            aws_secret_access_key=secret_key or None,
            endpoint_url=endpoint_url or None,
        )

    def _put(self, key, data, content_type, metadata):
        self.client.put_object(
            Bucket=self.bucket, Key=key, Body=data, ContentType=content_type,
            Metadata={k: v.encode("ascii", "ignore").decode() for k, v in (metadata or {}).items()},
            ServerSideEncryption="AES256",
        )

    async def put(self, key, data, content_type="application/pdf", metadata=None):
        try:
            await asyncio.to_thread(self._put, key, data, content_type, metadata)
        except Exception as exc:
            raise StorageError(f"S3 upload failed for '{key}': {exc}") from exc

    async def get(self, key):
        try:
            obj = await asyncio.to_thread(self.client.get_object, Bucket=self.bucket, Key=key)
            return await asyncio.to_thread(obj["Body"].read)
        except Exception as exc:
            raise StorageError(f"S3 download failed for '{key}': {exc}") from exc

    async def exists(self, key):
        try:
            await asyncio.to_thread(self.client.head_object, Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False


class LocalStorage:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise StorageError(f"Invalid storage key '{key}'")
        return path

    def _write(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    async def put(self, key, data, content_type="application/pdf", metadata=None):
        await asyncio.to_thread(self._write, key, data)

    async def get(self, key):
        try:
            return await asyncio.to_thread(self._path(key).read_bytes)
        except FileNotFoundError as exc:
            raise StorageError(f"File not found in storage: '{key}'") from exc

    async def exists(self, key):
        return self._path(key).exists()


@lru_cache
def get_storage() -> StorageBackend:
    s = get_settings()
    if s.storage_backend == "local":
        logger.info("Using LOCAL storage at %s", s.local_storage_dir)
        return LocalStorage(s.local_storage_dir)
    if not s.s3_bucket:
        raise ConfigError("S3_BUCKET is not set (or use STORAGE_BACKEND=local)")
    logger.info("Using S3 storage bucket=%s region=%s", s.s3_bucket, s.aws_region)
    return S3Storage(s.s3_bucket, s.aws_region, s.aws_access_key_id,
                     s.aws_secret_access_key, s.s3_endpoint_url)
