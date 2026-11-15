"""旧创意调度头的端点校正；需先隔离旧派发进程，不触碰执行或历史成本。"""

from datetime import datetime
from typing import cast as typing_cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlmodel import Session

from app.integrations.tiktok.platform_quotas import sdk_endpoint

Cursor = tuple[UUID, str]


def repair_directory_endpoint_batch(
    session: Session,
    *,
    now: datetime,
    limit: int = 64,
    after: Cursor | None = None,
    apply: bool = False,
    old_dispatchers_stopped: bool = False,
) -> list[Cursor]:
    if not 1 <= limit <= 256 or now.utcoffset() is None:
        raise ValueError("bounded repair requires 1..256 rows and an aware timestamp")
    if apply and not old_dispatchers_stopped:
        raise ValueError("old dispatchers must be stopped before endpoint repair")
    # head锁与规范派发共用；有效grant或worker尚存时保持原queued/ledger合同。
    # keyset覆盖闲置头和两个工作类，跳锁行由下一轮从头复查，不在本轮忙等。
    rows = (
        typing_cast(SQLAlchemySession, session)
        .execute(
            text(
                """
        SELECT h.scope_id,h.work_class
        FROM sync_schedule h JOIN sync_scope s ON s.id=h.scope_id
        WHERE s.resource='directory:creative:SMART_PLUS'
          AND h.physical_endpoint=:old
          AND (h.leased_until IS NULL OR h.leased_until<=:now)
          AND (NOT :has_after OR (h.scope_id,h.work_class)>(:after_id,:after_class))
          AND NOT EXISTS (
            SELECT 1 FROM sync_execution_scope e JOIN ad_directory_run r ON r.id=e.directory_run_id
            WHERE e.scope_id=h.scope_id AND e.status='PENDING'
              AND r.claimed_until>:now AND r.status IN ('QUEUED','RUNNING','ADMISSION_WAIT','WAITING_REMOTE'))
        ORDER BY h.scope_id,h.work_class LIMIT :limit
    """
                + (" FOR UPDATE OF h SKIP LOCKED" if apply else "")
            ),
            {
                "old": sdk_endpoint("build.get_ads"),
                "now": now,
                "limit": limit,
                "has_after": after is not None,
                "after_id": after[0] if after else UUID(int=0),
                "after_class": after[1] if after else "",
            },
        )
        .all()
    )
    keys = [(row.scope_id, row.work_class) for row in rows]
    if apply and keys:
        session.connection().execute(
            text("""
            UPDATE sync_schedule SET physical_endpoint=:correct
            WHERE scope_id=:scope AND work_class=:work_class AND physical_endpoint=:old
        """),
            [
                {
                    "scope": key[0],
                    "work_class": key[1],
                    "old": sdk_endpoint("build.get_ads"),
                    "correct": sdk_endpoint("ads.get_ads"),
                }
                for key in keys
            ],
        )
    return keys
