"""Where raw snapshots and curated tables are written: a local folder or an S3 bucket."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class Store(Protocol):
    def put_bytes(self, key: str, data: bytes) -> str:
        """Write `data` under `key` and return its URI."""

    def get_bytes(self, key: str) -> bytes: ...


class LocalStore:
    """Writes under a root folder. Used for local runs and tests."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError(f"key {key!r} points outside the store")
        return path

    def put_bytes(self, key: str, data: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path.as_uri()

    def get_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()


class S3Store:
    """Writes to one S3 bucket. The bucket has versioning on, so every write is kept."""

    def __init__(self, bucket: str, *, client: Any = None) -> None:
        if client is None:
            import boto3  # imported here so local runs and tests need no AWS setup

            client = boto3.client("s3")
        self.bucket = bucket
        self.client = client

    def put_bytes(self, key: str, data: bytes) -> str:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)
        return f"s3://{self.bucket}/{key}"

    def get_bytes(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
