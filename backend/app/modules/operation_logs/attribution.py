"""只在应用、对象、分钟和前后值均唯一吻合时采用本工具的操作来源。"""

from collections import Counter
from dataclasses import replace
from typing import Any
from typing import cast as typing_cast
from uuid import UUID

from sqlalchemy.orm import Session as SQLAlchemySession
from sqlmodel import Session, col, select

from app.core.config import settings
from app.modules.ad_management.models import (
    ManagementRequestAttempt,
    ManagementTaskItem,
)
from app.modules.automation_rules.models import (
    AutomationActionLedger,
    AutomationRuleRun,
)
from app.modules.operation_logs.parser import OperationEvent
from app.modules.operation_logs.replay import BID_FIELDS, number


def _key(event: OperationEvent) -> tuple[Any, ...] | None:
    changes = [d for d in event.details if d["name"].strip().lower() in BID_FIELDS]
    if len(changes) != 1:
        return None
    pairs = changes[0].get("before_after", [])
    if len(pairs) != 1 or not isinstance(pairs[0], dict):
        return None
    before, after = number(pairs[0].get("before")), number(pairs[0].get("after"))
    if before is None or after is None:
        return None
    return event.object_id, event.occurred_at, before, after


def attribute_events(
    session: Session,
    *,
    tenant_id: UUID,
    advertiser_id: str,
    events: tuple[OperationEvent, ...],
) -> tuple[OperationEvent, ...]:
    candidates = [
        e
        for e in events
        if e.raw.get("App ID") == settings.TIKTOK_APP_ID
        and e.source == "Marketing API"
        and _key(e) is not None
    ]
    if not candidates:
        return events
    earliest, latest = (
        min(e.occurred_at for e in candidates),
        max(e.occurred_at for e in candidates),
    )
    from datetime import timedelta

    from sqlalchemy import select as sa_select

    rows = (
        typing_cast(SQLAlchemySession, session)
        .execute(
            sa_select(ManagementTaskItem, ManagementRequestAttempt)
            .join(
                ManagementRequestAttempt,
                (
                    col(ManagementRequestAttempt.tenant_id)
                    == col(ManagementTaskItem.tenant_id)
                )
                & (
                    col(ManagementRequestAttempt.task_item_id)
                    == col(ManagementTaskItem.id)
                ),
            )
            .where(
                col(ManagementTaskItem.tenant_id) == tenant_id,
                col(ManagementTaskItem.execution_result) == "ACCEPTED",
                col(ManagementRequestAttempt.outcome) == "ACCEPTED",
                col(ManagementRequestAttempt.request_at) >= earliest,
                col(ManagementRequestAttempt.request_at)
                < latest + timedelta(minutes=1),
            )
        )
        .all()
    )
    task_ids = {item.task_id for item, _ in rows}
    modes = (
        session.exec(
            select(
                col(AutomationActionLedger.management_task_id),
                col(AutomationRuleRun.mode),
            )
            .join(
                AutomationRuleRun,
                (
                    col(AutomationRuleRun.tenant_id)
                    == col(AutomationActionLedger.tenant_id)
                )
                & (col(AutomationRuleRun.id) == col(AutomationActionLedger.run_id)),
            )
            .where(
                col(AutomationActionLedger.tenant_id) == tenant_id,
                col(AutomationActionLedger.management_task_id).in_(task_ids),
            )
        ).all()
        if task_ids
        else []
    )
    automatic = {task for task, mode in modes if mode == "AUTO"}
    local: dict[tuple[Any, ...], list[str]] = {}
    for item, attempt in rows:
        ref = item.ref
        if ref.get("advertiser_id") != advertiser_id:
            continue
        before, after = number(item.original_value), number(item.final_value)
        key = (
            ref.get("remote_id"),
            attempt.request_at.replace(second=0, microsecond=0),
            before,
            after,
        )
        local.setdefault(key, []).append(
            "AUTOMATED" if item.task_id in automatic else "MANUAL"
        )
    multiplicity = Counter(_key(e) for e in candidates)
    result = []
    for event in events:
        event_key = _key(event)
        matches = (
            local.get(event_key, [])
            if event in candidates and event_key is not None
            else []
        )
        result.append(
            replace(event, attribution=matches[0])
            if len(matches) == 1 and multiplicity[event_key] == 1
            else event
        )
    return tuple(result)
