"""目录head缓存当前有效欠账年龄；历史最小值不能成为新请求的优先级。"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text, tuple_
from sqlmodel import Session, col, select

from app.modules.sync_control.models import SyncSchedule, SyncScope

DirectoryHeadKey = tuple[UUID, str]

# 年龄、优先发现和正式领取共用此谓词；已覆盖的旧frozen执行不能借新版本欠账插队。
CURRENT_DIRECTORY_EXECUTION_SQL = """
 e.status='PENDING' AND e.owner_epoch=s.owner_epoch AND r.owner_epoch=s.owner_epoch
 AND e.tenant_id=h.tenant_id AND r.tenant_id=h.tenant_id
 AND q.tenant_id=h.tenant_id AND q.scope_id=e.scope_id
 AND r.advertiser_id=s.advertiser_id AND q.source=h.work_class
 AND q.satisfied_revision<e.frozen_revision
 AND r.status IN ('QUEUED','RUNNING','ADMISSION_WAIT','WAITING_REMOTE')
"""


def refresh_directory_pending_age(
    session: Session, heads: Sequence[SyncSchedule]
) -> int:
    """只改传入head的年龄；不改active/due/owner或正在使用的grant。"""
    if not heads:
        return 0
    rows = (
        session.connection()
        .execute(
            text(f"""
        SELECT h.scope_id,h.work_class,min(r.created_at) oldest
        FROM jsonb_to_recordset(CAST(:heads AS jsonb))
             AS h(scope_id uuid,tenant_id uuid,work_class text)
        JOIN sync_scope s ON s.id=h.scope_id AND s.tenant_id=h.tenant_id
        JOIN sync_execution_scope e ON e.scope_id=s.id
        JOIN sync_requirement q ON q.id=e.requirement_id
        JOIN ad_directory_run r ON r.id=e.directory_run_id
        WHERE {CURRENT_DIRECTORY_EXECUTION_SQL}
        GROUP BY h.scope_id,h.work_class
        """),
            {
                "heads": json.dumps(
                    [
                        {
                            "scope_id": str(h.scope_id),
                            "tenant_id": str(h.tenant_id),
                            "work_class": h.work_class,
                        }
                        for h in heads
                    ]
                )
            },
        )
        .mappings()
        .all()
    )
    ages: dict[DirectoryHeadKey, datetime] = {
        (row["scope_id"], row["work_class"]): row["oldest"] for row in rows
    }
    changed = 0
    for head in heads:
        age = ages.get((head.scope_id, head.work_class))
        if head.oldest_pending_at != age:
            head.oldest_pending_at = age
            changed += 1
    return changed


@dataclass(frozen=True)
class DirectoryAgeRepairBatch:
    scanned: int
    changed: int
    after: DirectoryHeadKey | None


def repair_directory_pending_ages(
    session: Session, *, after: DirectoryHeadKey | None = None, limit: int = 64
) -> DirectoryAgeRepairBatch:
    """全新写入者上线后有界纠正active缓存；锁跳过的头须回绕复查。"""
    if not 1 <= limit <= 256:
        raise ValueError("directory age repair batch must be between 1 and 256")
    statement = (
        select(SyncSchedule)
        .join(SyncScope, col(SyncScope.id) == col(SyncSchedule.scope_id))
        .where(
            col(SyncSchedule.active).is_(True),
            col(SyncScope.resource).like("directory:%"),
        )
        .order_by(col(SyncSchedule.scope_id), col(SyncSchedule.work_class))
        .limit(limit)
        .with_for_update(of=SyncSchedule, skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if after is not None:
        statement = statement.where(
            tuple_(col(SyncSchedule.scope_id), col(SyncSchedule.work_class)) > after
        )
    heads = session.exec(statement).all()
    changed = refresh_directory_pending_age(session, heads)
    return DirectoryAgeRepairBatch(
        len(heads),
        changed,
        (heads[-1].scope_id, heads[-1].work_class) if heads else None,
    )
