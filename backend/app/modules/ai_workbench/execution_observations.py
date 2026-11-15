"""冻结广告管理实际状态；受理回执和平台状态观察始终是两种证据。"""

from collections import Counter
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlmodel import Session, col, select

from app.modules.ad_management.models import (
    ManagementReceipt,
    ManagementRequestAttempt,
    ManagementTask,
    ManagementTaskItem,
)
from app.modules.ai_workbench.knowledge_refs import sanitize_public
from app.modules.automation_rules.models import AutomationActionLedger

MAX_ITEMS = 500
MAX_RECEIPTS = 10000
OBSERVATION_KINDS = {"state_observation", "state_observation_request"}


def execution_status(states: list[str], *, missing: bool = False) -> str:
    """部分未知不能由其他项的成功掩盖；差异、未知、待核查均保留逐项明细。"""
    if "OBSERVED_DIFFERENT" in states:
        return "OBSERVED_DIFFERENT"
    if missing or "UNKNOWN" in states:
        return "UNKNOWN"
    if "READBACK_REQUIRED" in states:
        return "READBACK_REQUIRED"
    if "FAILED" in states:
        return "FAILED"
    return "VERIFIED" if states else "CHECKED_WITHOUT_ACTION"


def _observed(receipt, source, cutoff):
    if receipt is None:
        return None
    response = receipt.response
    state = response.get("state")
    try:
        remote_at = datetime.fromisoformat(response["remote_observed_at"])
        valid_time = source.request_at <= remote_at <= receipt.observed_at <= cutoff
    except KeyError, TypeError, ValueError:
        valid_time = False
    valid = (
        valid_time
        and response.get("value") is not None
        and (
            (state == "OBSERVED_MATCH" and response.get("matches") is True)
            or (state == "OBSERVED_DIFFERENT" and response.get("matches") is False)
        )
    )
    return {
        "receipt_id": receipt.id,
        "attempt_id": source.id,
        "state": state,
        "actual_value": response.get("value"),
        "matches": response.get("matches"),
        "remote_observed_at": response.get("remote_observed_at"),
        "recorded_at": receipt.observed_at,
        "reason": response.get("reason"),
        "valid": valid,
    }


def freeze_rule_execution(
    session: Session,
    *,
    context,
    optimizer_id: UUID,
    version,
    evidence,
    cutoff: datetime,
) -> dict:
    """仅沿同租户/优化师台账读取同 BC、授权账户对象；输出独立 JSON 值副本。"""
    allowed = set(version.advertiser_ids)
    if (
        evidence.tenant_id != context.tenant_id
        or evidence.optimizer_id != optimizer_id
        or evidence.bc_id != version.bc_id
        or not set(evidence.advertiser_ids).issubset(allowed)
        or evidence.created_at > cutoff
    ):
        return {"status": "OUT_OF_SCOPE", "items": [], "truncated": False}
    ledgers = session.exec(
        select(AutomationActionLedger).where(
            col(AutomationActionLedger.tenant_id) == context.tenant_id,
            col(AutomationActionLedger.optimizer_id) == optimizer_id,
            col(AutomationActionLedger.evidence_id) == evidence.id,
            col(AutomationActionLedger.created_at) <= cutoff,
        )
    ).all()
    task_ids = {row.management_task_id for row in ledgers if row.management_task_id}
    rows = (
        session.exec(
            select(ManagementTaskItem, ManagementTask)
            .join(
                ManagementTask,
                (col(ManagementTask.id) == col(ManagementTaskItem.task_id))
                & (col(ManagementTask.tenant_id) == col(ManagementTaskItem.tenant_id)),
            )
            .where(
                col(ManagementTask.tenant_id) == context.tenant_id,
                col(ManagementTask.bc_id) == version.bc_id,
                col(ManagementTask.created_at) <= cutoff,
                col(ManagementTask.id).in_(task_ids),
                col(ManagementTaskItem.ref)["advertiser_id"].astext.in_(allowed),
                col(ManagementTaskItem.ref)["tenant_id"].astext
                == str(context.tenant_id),
            )
            .order_by(
                col(ManagementTask.id),
                col(ManagementTaskItem.position),
                col(ManagementTaskItem.id),
            )
            .limit(MAX_ITEMS + 1)
        ).all()
        if task_ids
        else []
    )
    item_ids = [item.id for item, _ in rows[:MAX_ITEMS]]
    receipts = (
        session.exec(
            select(ManagementReceipt, ManagementRequestAttempt)
            .join(
                ManagementRequestAttempt,
                (col(ManagementRequestAttempt.id) == col(ManagementReceipt.attempt_id))
                & (
                    col(ManagementRequestAttempt.tenant_id)
                    == col(ManagementReceipt.tenant_id)
                ),
            )
            .where(
                col(ManagementReceipt.tenant_id) == context.tenant_id,
                col(ManagementRequestAttempt.task_item_id).in_(item_ids),
            )
            .order_by(
                col(ManagementReceipt.observed_at).desc(),
                col(ManagementReceipt.id).desc(),
            )
            .limit(MAX_RECEIPTS + 1)
        ).all()
        if item_ids
        else []
    )
    by_item = {}
    for receipt, attempt in receipts[:MAX_RECEIPTS]:
        by_item.setdefault(attempt.task_item_id, []).append((receipt, attempt))
    frozen: list[dict[str, Any]] = []
    for item, task in rows[:MAX_ITEMS]:
        history = by_item.get(item.id, [])
        eligible = [
            (r, a)
            for r, a in history
            if r.observed_at <= cutoff and a.request_at <= cutoff
        ]
        latest_attempt = max((a.attempt for _, a in eligible), default=None)
        eligible = [(r, a) for r, a in eligible if a.attempt == latest_attempt]
        mutation = next(
            (r for r, _ in eligible if r.response.get("kind") not in OBSERVATION_KINDS),
            None,
        )
        observed = next(
            (
                (r, a)
                for r, a in eligible
                if r.response.get("kind") == "state_observation"
            ),
            None,
        )
        observation = _observed(*observed, cutoff) if observed else None
        # 管理项无历史版本；若已有截断后的回执，不能把当前可变状态投射到过去。
        current_visible = task.updated_at <= cutoff and all(
            r.observed_at <= cutoff and a.request_at <= cutoff for r, a in history
        )
        result = (
            mutation.outcome
            if mutation
            else (item.execution_result if current_visible else "UNKNOWN")
        )
        state = (
            observation["state"]
            if observation
            else item.observation_state
            if current_visible
            else None
        )
        if observation and observation["valid"]:
            status = "VERIFIED" if observation["matches"] else "OBSERVED_DIFFERENT"
        elif observation or result == "UNKNOWN" or state == "RECONCILE_FAILED":
            status = "UNKNOWN"
        elif result in {"REJECTED", "NOT_SENT", "CONFLICT", "UNSUPPORTED", "CANCELLED"}:
            status = "FAILED"
        else:
            status = "READBACK_REQUIRED"
        frozen.append(
            {
                "task_id": task.id,
                "item_id": item.id,
                "advertiser_id": item.ref["advertiser_id"],
                "object": {key: item.ref.get(key) for key in ("kind", "remote_id")},
                "material_use": item.material_use,
                "operation": item.capability.get("operation"),
                "original_value": item.original_value,
                "desired_value": item.final_value,
                "execution_result": result,
                "execution_receipt_id": mutation.id if mutation else None,
                "execution_recorded_at": mutation.observed_at if mutation else None,
                "observation_state": state,
                "observation": observation,
                "status": status,
            }
        )
    truncated = len(rows) > MAX_ITEMS or len(receipts) > MAX_RECEIPTS
    # 缺失/越界引用不可误报为没有动作，更不能拼接未校验的任务 JSON。
    missing = (
        truncated
        or any(row.management_task_id is None for row in ledgers)
        or task_ids != {task.id for _, task in rows[:MAX_ITEMS]}
        or bool(set(evidence.action_ledger_refs) - {str(row.id) for row in ledgers})
        or bool(evidence.management_tasks and not ledgers)
    )
    states = [row["status"] for row in frozen]
    return sanitize_public(
        {
            "status": execution_status(states, missing=missing),
            "cutoff": cutoff,
            "items": frozen,
            "action_ledger_refs": [str(row.id) for row in ledgers],
            "counts": dict(Counter(states)),
            "truncated": truncated,
            "missing_evidence": missing,
            "interpretation": "OBSERVED_STATE_ONLY_NOT_MUTATION_CAUSALITY",
        }
    )
