"""不可变归档blob和已提交marker；只有完整回读校验才产生持久回执。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, BinaryIO
from uuid import UUID

from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from app.core.config import Settings, settings
from app.modules.materials.storage import make_s3

MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
READ_CHUNK_BYTES = 256 * 1024
KINDS = frozenset({"report", "directory", "balance"})


class ArchiveStorageError(RuntimeError):
    pass


def archive_prefix(tenant_id: UUID, kind: str, run_id: UUID) -> str:
    if kind not in KINDS:
        raise ValueError("invalid archive kind")
    return f"tenants/{tenant_id}/sync-runs/v1/{kind}/{run_id}/"


def archive_key(tenant_id: UUID, kind: str, run_id: UUID, digest: str) -> str:
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("invalid archive digest")
    return archive_prefix(tenant_id, kind, run_id) + f"blobs/{digest}.jsonl"


@dataclass(frozen=True)
class ArchiveReceipt:
    key: str
    sha256: str
    size: int


class S3RunArchive:
    def __init__(self, client: Any, bucket: str):
        if not bucket:
            raise ArchiveStorageError("archive_storage_not_configured")
        self.client, self.bucket = client, bucket

    @classmethod
    def configured(cls, config: Settings | None = None) -> S3RunArchive:
        config = config or settings
        archive_bucket = getattr(config, "SYNC_ARCHIVE_BUCKET", "").strip()
        if not archive_bucket or archive_bucket == config.S3_BUCKET.strip():
            raise ArchiveStorageError("archive_private_bucket_not_configured")
        # 素材桶可能有公开分发域名；归档必须显式使用独立私有目标。
        return cls(
            make_s3(config.model_copy(update={"S3_BUCKET": archive_bucket})),
            archive_bucket,
        )

    def _put(
        self,
        key: str,
        payload: BinaryIO | bytes,
        *,
        digest: str,
        size: int,
        content_type: str,
    ) -> None:
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=payload,
                ContentLength=size,
                ContentType=content_type,
                Metadata={"sha256": digest},
                IfNoneMatch="*",
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") not in {
                "PreconditionFailed",
                "412",
            }:
                raise

    def _verify(
        self, key: str, *, expected_sha256: str, expected_size: int
    ) -> ArchiveReceipt:
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        body = response["Body"]
        try:
            size = response.get("ContentLength")
            if (
                type(size) is not int
                or size != expected_size
                or not 0 < size <= MAX_ARCHIVE_BYTES
            ):
                raise ArchiveStorageError("archive_length_mismatch")
            digest, count = sha256(), 0
            while chunk := body.read(READ_CHUNK_BYTES):
                count += len(chunk)
                if count > expected_size:
                    raise ArchiveStorageError("archive_length_mismatch")
                digest.update(chunk)
            if count != size or digest.hexdigest() != expected_sha256:
                raise ArchiveStorageError("archive_checksum_mismatch")
            return ArchiveReceipt(key, digest.hexdigest(), count)
        finally:
            body.close()

    def persist(
        self,
        *,
        tenant_id: UUID,
        kind: str,
        run_id: UUID,
        payload: BinaryIO,
        sha256_digest: str,
        size: int,
    ) -> ArchiveReceipt:
        if not 0 < size <= MAX_ARCHIVE_BYTES:
            raise ArchiveStorageError("archive_too_large")
        key = archive_key(tenant_id, kind, run_id, sha256_digest)
        payload.seek(0)
        self._put(
            key,
            payload,
            digest=sha256_digest,
            size=size,
            content_type="application/x-ndjson",
        )
        # 不能只信metadata/ETag；逐块回读全部正文验证SHA256及字节数。
        return self._verify(key, expected_sha256=sha256_digest, expected_size=size)

    def finalize(
        self, *, tenant_id: UUID, kind: str, run_id: UUID, receipt: ArchiveReceipt
    ) -> None:
        if receipt.key != archive_key(tenant_id, kind, run_id, receipt.sha256):
            raise ArchiveStorageError("archive_identity_mismatch")
        key = (
            archive_prefix(tenant_id, kind, run_id) + f"committed/{receipt.sha256}.json"
        )
        marker = json.dumps(
            {
                "schema_version": 1,
                "tenant_id": str(tenant_id),
                "kind": kind,
                "run_id": str(run_id),
                "digest": receipt.sha256,
                "object_key": receipt.key,
                "byte_length": receipt.size,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        digest = sha256(marker).hexdigest()
        self._put(
            key,
            marker,
            digest=digest,
            size=len(marker),
            content_type="application/json",
        )
        self._verify(key, expected_sha256=digest, expected_size=len(marker))

    def committed(
        self,
        *,
        tenant_id: UUID,
        kind: str,
        run_id: UUID,
        cursor: str | None = None,
        limit: int = 25,
    ) -> tuple[list[ArchiveReceipt], str | None]:
        """只列正式提交marker；raw候选blob不能冒充审计完成版本。"""
        if not 1 <= limit <= 100:
            raise ValueError("invalid archive resolution limit")
        prefix = archive_prefix(tenant_id, kind, run_id) + "committed/"
        params = {"Bucket": self.bucket, "Prefix": prefix, "MaxKeys": limit}
        if cursor:
            params["ContinuationToken"] = cursor
        response = self.client.list_objects_v2(**params)
        receipts = []
        for item in response.get("Contents", []):
            key = item["Key"]
            if not key.startswith(prefix):
                raise ArchiveStorageError("archive_identity_mismatch")
            result = self.client.get_object(Bucket=self.bucket, Key=key)
            body = result["Body"]
            try:
                marker = body.read(16 * 1024 + 1)
                if len(marker) > 16 * 1024 or len(marker) != result.get(
                    "ContentLength"
                ):
                    raise ArchiveStorageError("archive_marker_invalid")
                document = json.loads(marker)
            finally:
                body.close()
            expected = archive_key(tenant_id, kind, run_id, document["digest"])
            if (
                document.get("schema_version") != 1
                or document.get("tenant_id") != str(tenant_id)
                or document.get("kind") != kind
                or document.get("run_id") != str(run_id)
                or document.get("object_key") != expected
                or key != prefix + document["digest"] + ".json"
                or type(document.get("byte_length")) is not int
                or not 0 < document["byte_length"] <= MAX_ARCHIVE_BYTES
            ):
                raise ArchiveStorageError("archive_identity_mismatch")
            receipts.append(
                ArchiveReceipt(expected, document["digest"], document["byte_length"])
            )
        return receipts, response.get("NextContinuationToken")
