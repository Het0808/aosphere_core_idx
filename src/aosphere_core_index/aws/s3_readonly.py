"""Read-only access to the source S3 bucket.

Read-only is enforced two ways:
  1. IAM — the principal should only have GetObject/ListBucket (see
     infra/readonly-s3-policy.json).
  2. Code — this client only ever exposes list/head/get. There is no code path
     that calls put/delete on the source bucket.

A startup guard confirms the caller is in the expected account before any work.
"""

from __future__ import annotations

import os
from pathlib import Path

import boto3
from botocore.client import BaseClient
from botocore.config import Config as BotoConfig

from aosphere_core_index.config import settings

# Operations this module is permitted to perform against the source bucket.
_ALLOWED_OPS = frozenset({"list_objects_v2", "get_object", "head_object", "get_bucket_location"})


class ReadOnlyS3:
    """A thin read-only wrapper over an S3 client for the source bucket."""

    # botocore's default connection pool is 10. Reading an extraction run fans out 32 concurrent
    # listings and GETs, and past the pool size urllib3 discards a connection instead of reusing
    # it — every discarded connection is a fresh TLS handshake on the next request. Measured on
    # run 2026-08-21-02 with 64 "Connection pool is full, discarding connection" warnings, every
    # discard a fresh TLS handshake on the next request. Sized ABOVE the gallery's fan-out (64)
    # on purpose: at pool 40 against 64 threads the warnings came straight back. This is a CAP,
    # not a preallocation, so raising it costs a caller that never fans out nothing.
    _POOL = max(10, int(os.getenv("ACI_S3_MAX_POOL", "80")))

    def __init__(self, bucket: str | None = None, region: str | None = None) -> None:
        self.bucket = bucket or settings.source_bucket
        self._s3: BaseClient = boto3.client(
            "s3", region_name=region or settings.aws_region,
            config=BotoConfig(max_pool_connections=self._POOL))

    def assert_identity(self) -> dict:
        """Confirm we are authenticated and in the expected account.

        Returns the STS caller identity. Raises if the account is unexpected,
        which protects against accidentally pointing at the wrong environment.
        """
        sts = boto3.client("sts", region_name=settings.aws_region)
        ident = sts.get_caller_identity()
        if ident["Account"] != settings.expected_account:
            raise RuntimeError(
                f"Refusing to run: account {ident['Account']} != expected "
                f"{settings.expected_account}. Check your AWS credentials."
            )
        return ident

    def list_keys(self, prefix: str, suffix: str | None = None) -> list[str]:
        """List all object keys under a prefix, optionally filtered by suffix."""
        paginator = self._s3.get_paginator("list_objects_v2")
        keys: list[str] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if suffix is None or key.endswith(suffix):
                    keys.append(key)
        return keys

    def exists(self, key: str) -> bool:
        """Whether one object is there, without fetching it. A viewer is ~7MB, so the screen must
        be able to ask "is it there?" without paying for it."""
        try:
            self._s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:                                    # noqa: BLE001
            return False

    def list_objects(self, prefix: str, suffix: str | None = None) -> list[dict]:
        """Like list_keys, but keeps size and LastModified.

        Callers that need to know WHEN something was written (which extraction run is the most
        recently active, say) would otherwise have to HEAD every key — the list response already
        carries it."""
        paginator = self._s3.get_paginator("list_objects_v2")
        out: list[dict] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                if suffix is not None and not obj["Key"].endswith(suffix):
                    continue
                out.append({"key": obj["Key"], "size": obj.get("Size", 0),
                            "last_modified": obj.get("LastModified")})
        return out

    def list_level(self, prefix: str) -> list[str]:
        """Keys sitting DIRECTLY under a prefix — one level, no recursion.

        The difference from list_keys is the whole cost of reading an extraction run. A job
        directory holds three artefacts the gallery wants and several hundred objects it does
        not (the stage trees, the page snapshots, the source PDF); a recursive listing of one
        run measured 380,609 keys and 64 seconds, a one-level listing of each job dir is a
        handful of rows. Delimiter='/' also means a nested retry attempt comes back as a
        COMMON PREFIX rather than a key, so an attempt's scorecard cannot be mistaken for a
        second document."""
        paginator = self._s3.get_paginator("list_objects_v2")
        out: list[str] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix, Delimiter="/"):
            for obj in page.get("Contents", []):
                out.append(obj["Key"])
        return out

    def list_level_objects(self, prefix: str) -> list[dict]:
        """`list_level`, keeping each key's size.

        A promotion copies objects and wants to report how many BYTES it is about to move
        before it moves them; the listing already carries the size, so asking for it here
        costs nothing and saves a HEAD per object."""
        paginator = self._s3.get_paginator("list_objects_v2")
        out: list[dict] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix, Delimiter="/"):
            for obj in page.get("Contents", []):
                out.append({"key": obj["Key"], "size": obj.get("Size", 0)})
        return out

    def list_common_prefixes(self, prefix: str) -> list[str]:
        """List immediate 'folder' prefixes under a prefix (delimiter='/')."""
        paginator = self._s3.get_paginator("list_objects_v2")
        prefixes: list[str] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix, Delimiter="/"):
            for cp in page.get("CommonPrefixes", []):
                prefixes.append(cp["Prefix"])
        return prefixes

    def list_level_with_prefixes(self, prefix: str) -> tuple[list[dict], list[str]]:
        """`list_level_objects` and `list_common_prefixes` for the SAME prefix, from ONE
        paginated call instead of two — every page of list_objects_v2 already carries both
        `Contents` and `CommonPrefixes`; the two single-purpose methods above each throw
        half of that away. Worth a dedicated method only because a caller needing both for
        the same prefix (walk_run_jobs, checking for a nested subdirectory beside the
        artefact files it already lists) would otherwise pay for the listing twice."""
        paginator = self._s3.get_paginator("list_objects_v2")
        keys: list[dict] = []
        prefixes: list[str] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix, Delimiter="/"):
            for obj in page.get("Contents", []):
                keys.append({"key": obj["Key"], "size": obj.get("Size", 0)})
            for cp in page.get("CommonPrefixes", []):
                prefixes.append(cp["Prefix"])
        return keys, prefixes

    def get_bytes(self, key: str) -> bytes:
        """Fetch an object's raw bytes."""
        resp = self._s3.get_object(Bucket=self.bucket, Key=key)
        return resp["Body"].read()

    def download(self, key: str, dest: Path) -> Path:
        """Download an object to a local path, caching by key (no re-download)."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            dest.write_bytes(self.get_bytes(key))
        return dest
