"""Where raw snapshots, curated tables, frozen predictions and site files live (folder or S3)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class Store(Protocol):
    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        cache_control: str | None = None,
    ) -> str:
        """Write `data` under `key`, replacing anything there, and return its URI."""

    def put_bytes_if_absent(self, key: str, data: bytes) -> bool:
        """Write `data` only if nothing is stored under `key`. True if it was written.

        This is what makes a frozen prediction final: a second write for the same match is refused,
        even if two runs overlap.
        """

    def get_bytes(self, key: str) -> bytes:
        """Read the object under `key`. Raises FileNotFoundError if there is none."""

    def list_keys(self, prefix: str) -> list[str]:
        """All keys that start with `prefix`, sorted."""


class LocalStore:
    """Writes under a root folder. Used for local runs and tests."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError(f"key {key!r} points outside the store")
        return path

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        cache_control: str | None = None,
    ) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path.as_uri()

    def put_bytes_if_absent(self, key: str, data: bytes) -> bool:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as handle:
                handle.write(data)
        except FileExistsError:
            return False
        return True

    def get_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def list_keys(self, prefix: str) -> list[str]:
        return sorted(
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_file() and path.relative_to(self.root).as_posix().startswith(prefix)
        )


def _error_code(exc: Exception) -> str | None:
    return getattr(exc, "response", {}).get("Error", {}).get("Code")


class S3Store:
    """Writes to one S3 bucket. The bucket has versioning on, so every write is kept."""

    def __init__(self, bucket: str, *, client: Any = None) -> None:
        if client is None:
            import boto3  # imported here so local runs and tests need no AWS setup

            client = boto3.client("s3")
        self.bucket = bucket
        self.client = client

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        cache_control: str | None = None,
    ) -> str:
        extra: dict[str, str] = {}
        if content_type:
            extra["ContentType"] = content_type
        if cache_control:
            extra["CacheControl"] = cache_control
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, **extra)
        return f"s3://{self.bucket}/{key}"

    def put_bytes_if_absent(self, key: str, data: bytes) -> bool:
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=data, IfNoneMatch="*")
        except Exception as exc:
            # 412: the object already exists. 409: another write for this key is in flight.
            if _error_code(exc) in {"PreconditionFailed", "ConditionalRequestConflict"}:
                return False
            raise
        return True

    def get_bytes(self, key: str) -> bytes:
        try:
            return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except Exception as exc:
            if _error_code(exc) in {"NoSuchKey", "404"}:
                raise FileNotFoundError(f"s3://{self.bucket}/{key}") from exc
            raise

    def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            page = self.client.list_objects_v2(**kwargs)
            keys.extend(item["Key"] for item in page.get("Contents", []))
            if not page.get("IsTruncated"):
                return sorted(keys)
            token = page["NextContinuationToken"]
