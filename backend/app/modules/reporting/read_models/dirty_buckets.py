"""持久输入修订与派生水位；重建只能确认自己冻结的修订。"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy import text
from sqlmodel import Session


@dataclass(frozen=True, order=True)
class ReadBucketKey:
    tenant_id: UUID
    advertiser_id: str
    kind: str
    bucket: date


def mark_dirty(
    session: Session, keys: Sequence[ReadBucketKey]
) -> dict[ReadBucketKey, int]:
    """同源写事务调用；排序拿锁防止多桶发布交叉死锁，重复key只推进一次。"""
    versions = {}
    for key in sorted(set(keys)):
        versions[key] = (
            session.connection()
            .execute(
                text("""
            INSERT INTO read_bucket_state
                (tenant_id,advertiser_id,kind,bucket,source_revision,derived_revision,observed_at)
            VALUES (:tenant,:advertiser,:kind,:bucket,1,0,clock_timestamp())
            ON CONFLICT (tenant_id,advertiser_id,kind,bucket)
            DO UPDATE SET source_revision=read_bucket_state.source_revision+1,
                observed_at=clock_timestamp()
            RETURNING source_revision
        """),
                parameters(key),
            )
            .scalar_one()
        )
    return versions


def parameters(key: ReadBucketKey) -> dict[str, UUID | str | date]:
    return {
        "tenant": key.tenant_id,
        "advertiser": key.advertiser_id,
        "kind": key.kind,
        "bucket": key.bucket,
    }
