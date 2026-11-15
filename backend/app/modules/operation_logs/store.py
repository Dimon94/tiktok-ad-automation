"""完整日志原子发布；重放仅更新检查时间，不制造经营来源修订。"""

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any
from typing import cast as typing_cast

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlmodel import Session

from app.modules.operation_logs.models import (
    OperationCoverage,
    OperationExport,
    OperationLogEvent,
)
from app.modules.operation_logs.parser import PARSER_VERSION, OperationEvent, utc_window
from app.modules.reporting.store_models import ReportSyncJob
from app.modules.sync_control.archive_storage import ArchiveReceipt


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()
    ).hexdigest()


def publish_export(
    session: Session,
    *,
    job: ReportSyncJob,
    content_digest: str,
    receipt: ArchiveReceipt,
    events: Sequence[OperationEvent],
    start_at: datetime,
    end_at: datetime,
    timezone: str,
    observed_at: datetime,
) -> bool:
    advertiser = job.advertiser_ids[0]
    from app.modules.operation_logs.attribution import attribute_events

    events = attribute_events(
        session, tenant_id=job.tenant_id, advertiser_id=advertiser, events=tuple(events)
    )
    # 全账户串行，避免两个重叠窗口把新覆盖倒退到旧观测。
    typing_cast(SQLAlchemySession, session).execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
        {"key": f"operation-log:{job.tenant_id}:{advertiser}"},
    )
    if session.get(OperationExport, job.id) is not None:
        return False
    exported = OperationExport(
        id=job.id,
        tenant_id=job.tenant_id,
        advertiser_id=advertiser,
        bc_id=job.bc_id,
        frozen_route=job.frozen_route,
        task_id=job.progress["operation_log"]["task_id"],
        start_at=start_at,
        end_at=end_at,
        timezone=timezone,
        observed_at=observed_at,
        digest=content_digest,
        archive_key=receipt.key,
        byte_length=receipt.size,
        parser_version=PARSER_VERSION,
        event_count=len(events),
    )
    session.add(exported)
    session.flush()
    values = [
        {
            "export_id": job.id,
            "identity": e.identity,
            "occurred_at": e.occurred_at,
            "object_type": e.object_type,
            "object_id": e.object_id,
            "details": list(e.details),
            "source": e.source,
            "attribution": e.attribution,
        }
        for e in events
        if start_at <= e.occurred_at < end_at
    ]
    for offset in range(0, len(values), 500):
        typing_cast(SQLAlchemySession, session).execute(
            insert(OperationLogEvent), values[offset : offset + 500]
        )
    day = job.start_date
    changed = False
    while day <= job.end_date:
        lower, upper = utc_window(day, day, timezone)
        if start_at <= lower and upper <= end_at:
            day_digest = digest(
                [
                    PARSER_VERSION,
                    timezone,
                    sorted(
                        (e.identity, e.attribution)
                        for e in events
                        if lower <= e.occurred_at < upper
                    ),
                ]
            )
            coverage = session.get(OperationCoverage, (job.tenant_id, advertiser, day))
            if coverage is None:
                coverage = OperationCoverage(
                    tenant_id=job.tenant_id,
                    advertiser_id=advertiser,
                    stat_date=day,
                    export_id=job.id,
                    digest=day_digest,
                    checked_at=observed_at,
                )
                changed = True
            elif coverage.checked_at <= observed_at:
                if coverage.digest != day_digest:
                    coverage.revision += 1
                    coverage.digest = day_digest
                    changed = True
                # 原文版本可更新，经营冻结按摘要+选定不可变export读取。
                coverage.export_id = job.id
                coverage.checked_at = observed_at
            session.add(coverage)
        day += timedelta(days=1)
    return changed
