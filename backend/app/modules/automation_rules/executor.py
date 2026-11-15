"""执行规则命中：频控、反向冲突判断，再经广告管理的冻结预览与任务提交执行。

不直接调用 TikTok：写入、重试、写前观测核对与写后定向刷新全部由广告管理负责。
"""

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import case, func, or_, text
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import EntityRef
from app.modules.ad_management.previews import prepare_preview
from app.modules.ad_management.submissions import submit_management_task
from app.modules.ads.models import AdObject
from app.modules.ai_workbench.rule_binding import (
    RuleBindingScope,
    validate_rule_binding,
)
from app.modules.automation_rules.actions import (
    action_field,
    action_key,
    frequency_allows,
    group_by_bc,
    mutation_for,
    opposite,
    selection_payload,
    target_index,
    use_key,
)
from app.modules.automation_rules.evaluate import evaluate
from app.modules.automation_rules.inputs import load_inputs
from app.modules.automation_rules.models import (
    AutomationActionLedger,
    AutomationRule,
    AutomationRuleEvidence,
    AutomationRuleHit,
    AutomationRuleRun,
)
from app.modules.automation_rules.pending import pending_targets
from app.modules.automation_rules.schemas import RuleDefinition
from app.modules.automation_rules.statuses import blocked_material_recoveries
from app.modules.reporting.query_models import FrozenSelectionRecord
from app.modules.reporting.selection import create_target_selection
from app.modules.tenants.permissions import require_tenant

logger = logging.getLogger(__name__)

MANAGEMENT_SOURCE = "automation_rule"
# 执行摘要中逐条列出的不支持对象上限。
MAX_DETAILS = 20


@dataclass(frozen=True)
class RuleMatch:
    name: str
    action: str | None = None
    keys: frozenset[str] = frozenset()
    unknown_keys: frozenset[str] = frozenset()
    unknown_accounts: frozenset[str] = frozenset()


def _summary() -> dict[str, Any]:
    return {
        "submitted": 0,
        "skipped_frequency": 0,
        "skipped_pending": 0,
        "skipped_parent_status": 0,
        "conflicts": 0,
        "no_change": 0,
        "unsupported": 0,
        "failed": 0,
        "failed_bcs": [],
        "tasks": [],
        "conflict_details": [],
        "unsupported_details": [],
    }


def _restore_bc_summary(
    summary: dict[str, Any],
    counts: dict[str, int],
    unsupported_details: list[dict[str, Any]],
) -> None:
    """Restore all mutable BC-local summary state after a savepoint rollback."""
    summary.update(counts)
    summary["unsupported_details"] = list(unsupported_details)


def lock_tenant(session: Session, tenant_id: UUID) -> None:
    # 同一租户的执行串行化：后到的执行一定能看到先到者写下的台账，冲突判断才可靠。
    session.connection().execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"automation-exec:{tenant_id}"},
    )


def _selection(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    targets: list[dict[str, Any]],
    run: AutomationRuleRun,
    now: datetime,
) -> FrozenSelectionRecord:
    """规则命中直接写成一条冻结选择，复用广告管理的预览与提交校验。"""
    refs, uses = selection_payload(targets, context.tenant_id)
    filters = {
        "source": MANAGEMENT_SOURCE,
        "rule_id": str(run.rule_id),
        "run_id": str(run.id),
    }
    return create_target_selection(
        session,
        context=context,
        bc_id=bc_id,
        refs=refs,
        uses=uses,
        filters=filters,
        now=now,
    )


def _target_key(
    index: dict[tuple[Any, ...], str], ref: EntityRef, material_use: Any
) -> str:
    if material_use is not None:
        found = index.get(
            (
                "use",
                *use_key(
                    material_use.ad_ref.advertiser_id,
                    material_use.ad_ref.remote_id,
                    material_use.platform_material_id,
                    material_use.ad_material_id,
                    material_use.material_type,
                ),
            )
        )
        if found:
            return found
        return f"material:{ref.advertiser_id}:{material_use.platform_material_id}"
    # 系列或广告组被展开到下级（如预算归属的系列）时，没有对应规则目标，按对象身份记账。
    return index.get(
        ("object", ref.advertiser_id, ref.kind, ref.remote_id),
        f"{ref.kind}:{ref.advertiser_id}:{ref.remote_id}",
    )


def _merge_target(targets: dict[str, dict[str, Any]], target: dict[str, Any]) -> None:
    """同一运行内多个命中指向同一对象时只执行一次；素材目标可能只含各自命中广告里的
    使用（按广告判断执行到素材），合并后才不会漏掉其他命中广告里的使用。"""
    existing = targets.get(target["key"])
    if existing is None:
        targets[target["key"]] = target
        return
    uses = list(existing.get("material_uses") or ())
    for use in target.get("material_uses") or ():
        if use not in uses:
            uses.append(use)
    targets[target["key"]] = {**existing, "material_uses": uses}


def execute_hits(
    session: Session,
    *,
    run: AutomationRuleRun,
    rule: AutomationRule,
    selected_keys: set[str] | None,
    now: datetime,
    actor_id: UUID,
    require_manage: bool,
) -> dict[str, Any]:
    """以 actor_id 身份执行：自动执行为规则最后修改人（须仍是租户管理员），
    人工确认为确认人；管理任务与审计记录都归属于该身份。"""
    summary = _summary()
    lock_tenant(session, rule.tenant_id)
    context = TenantContext(rule.tenant_id, actor_id, "operator")
    try:
        require_tenant(
            session,
            actor_id=actor_id,
            tenant_id=rule.tenant_id,
            action="ads_manage",
        )
        if require_manage:
            require_tenant(
                session,
                actor_id=actor_id,
                tenant_id=rule.tenant_id,
                action="manage",
            )
    except DomainError:
        summary["error"] = "rule_owner_forbidden"
        run.execution_status, run.execution_summary = "FAILED", summary
        session.flush()
        return summary
    scope: RuleBindingScope | None = None
    if run.optimizer_id is not None:
        # Optimizer executions are fenced to the exact rule/optimizer snapshot
        # used during evaluation; edits or revoked authorization fail closed.
        if run.rule_version != rule.version:
            summary["error"] = "rule_version_changed"
            run.execution_status, run.execution_summary = "FAILED", summary
            session.flush()
            return summary
        try:
            scope = validate_rule_binding(
                session,
                RuleBindingScope(
                    tenant_id=run.tenant_id,
                    optimizer_id=run.optimizer_id,
                    rule_id=run.rule_id,
                    bc_id="",
                    advertiser_ids=(),
                    authorization_revision=run.authorization_revision or 0,
                    authorization_revisions=tuple(
                        sorted(run.authorization_revisions.items())
                    ),
                    optimizer_version=run.optimizer_version or 0,
                    rule_version=run.rule_version,
                    binding_id=run.binding_id,
                ),
            )
        except DomainError:
            summary["error"] = "optimizer_scope_changed"
            run.execution_status, run.execution_summary = "FAILED", summary
            session.flush()
            return summary
        allowed = set(scope.advertiser_ids)
    else:
        allowed = None
    definition = RuleDefinition.model_validate(run.definition)
    action = definition.action
    key = action_key(action)
    hits = list(
        session.exec(
            select(AutomationRuleHit)
            .where(col(AutomationRuleHit.run_id) == run.id)
            .order_by(col(AutomationRuleHit.sequence))
        ).all()
    )
    # 每个执行对象记住来自哪些命中，结果回写到命中上。
    owners: dict[str, list[AutomationRuleHit]] = defaultdict(list)
    targets: dict[str, dict[str, Any]] = {}
    for hit in hits:
        if selected_keys is not None and hit.subject_key not in selected_keys:
            hit.outcome = "NOT_SELECTED"
            continue
        for target in hit.targets:
            if (
                scope is not None
                and allowed is not None
                and (
                    target.get("bc_id") != scope.bc_id
                    or target.get("advertiser_id") not in allowed
                )
            ):
                hit.outcome = "NOT_SELECTED"
                continue
            _merge_target(targets, target)
            owners[target["key"]].append(hit)
    outcome: dict[str, str] = {}
    history = _ledger(session, rule.tenant_id, list(targets), rule_id=rule.id)
    # 每条对方规则只按当前数据重新判断一次，多个对象共用结果。
    matching: dict[UUID, RuleMatch] = {}
    runnable: dict[str, dict[str, Any]] = {}
    blocked = (
        blocked_material_recoveries(session, rule.tenant_id, targets)
        if action.type == "ENABLE"
        else set()
    )
    for target_key, target in targets.items():
        if target_key in blocked:
            outcome[target_key] = "SKIPPED_PARENT_STATUS"
            summary["skipped_parent_status"] += 1
            continue
        rows = [row for row in history if row.target_key == target_key]
        # 反向冲突：同一字段最近一次是被另一条规则做了相反动作，且那条规则按当前
        # 数据仍命中该对象——两条规则条件重叠，继续执行只会来回反复。对方规则
        # 已不命中（如延迟归因后 ROAS 回升）说明数据变了，相反动作正常执行。
        last = next(
            (
                row
                for row in reversed(rows)
                if action_field(row.action) == action_field(key)
            ),
            None,
        )
        if (
            last is not None
            and last.rule_id is not None
            and last.rule_id != rule.id
            and opposite(key, last.action)
        ):
            if last.rule_id not in matching:
                matching[last.rule_id] = _still_matching(
                    session, rule.tenant_id, last.rule_id, now
                )
            current = matching[last.rule_id]
            # 历史动作不代表规则现在的方向；改为同向/其他字段后不再互相冲突。
            if (
                current.action is not None
                and opposite(key, current.action)
                and (
                    target_key in current.unknown_keys
                    or target["advertiser_id"] in current.unknown_accounts
                )
            ):
                outcome[target_key] = "FAILED"
                summary["failed"] += 1
                detail = {
                    "bc_id": target["bc_id"],
                    "reason": "关联规则数据未更新或指标缺失，无法判断反向冲突，本轮未执行",
                }
                if detail not in summary["failed_bcs"]:
                    summary["failed_bcs"].append(detail)
                continue
            if (
                current.action is not None
                and opposite(key, current.action)
                and target_key in current.keys
            ):
                outcome[target_key] = "CONFLICT"
                summary["conflicts"] += 1
                summary["conflict_details"].append(
                    {
                        "target_key": target_key,
                        "rule_name": current.name,
                        "action": last.action,
                    }
                )
                continue
        own = [
            row.created_at
            for row in rows
            if row.rule_id == rule.id and row.action == key
        ]
        if not frequency_allows(action.frequency, own, now):
            outcome[target_key] = "SKIPPED_FREQUENCY"
            summary["skipped_frequency"] += 1
            continue
        runnable[target_key] = target
    operation = {
        "PAUSE": "set_status",
        "ENABLE": "set_status",
        "BUDGET": "update_budget",
        "ROAS": "update_roas",
    }[action.type]
    if action.level == "material" and action.type in {"PAUSE", "ENABLE"}:
        operation = "set_material_status"
    for target_key in pending_targets(session, rule.tenant_id, runnable, operation):
        runnable.pop(target_key)
        outcome[target_key] = "SKIPPED_PENDING"
        summary["skipped_pending"] += 1
    for bc_id, bc_targets in group_by_bc(runnable.values()).items():
        # 保存点回滚时，本 BC 已计入的计数与结果一并撤销，整个 BC 记为失败。
        counts = {
            key: value for key, value in summary.items() if isinstance(value, int)
        }
        before = dict(outcome)
        before_unsupported_details = list(summary["unsupported_details"])
        try:
            with session.begin_nested():
                submitted = _execute_bc(
                    session,
                    context=context,
                    run=run,
                    rule=rule,
                    bc_id=bc_id,
                    targets=bc_targets,
                    action_name=key,
                    now=now,
                    outcome=outcome,
                    summary=summary,
                    optimizer_scope=scope,
                )
                if submitted:
                    summary["tasks"].append({"bc_id": bc_id, "task_id": str(submitted)})
        except Exception as exc:
            if isinstance(exc, DomainError):
                reason = exc.code
            elif isinstance(exc, HTTPException):
                reason = str(exc.detail)
            else:
                reason = "unexpected_error"
                logger.exception(
                    "automation execution crashed for bc",
                    extra={"rule_id": str(rule.id), "bc_id": bc_id},
                )
            logger.warning(
                "automation execution failed for bc",
                extra={"rule_id": str(rule.id), "bc_id": bc_id, "reason": reason},
            )
            # Preview classification may have appended unsupported details
            # before the final scope fence failed.  Restore the whole BC save
            # point, not only integer counters, so the failed evidence cannot
            # describe work that was rolled back.
            _restore_bc_summary(summary, counts, before_unsupported_details)
            outcome.clear()
            outcome.update(before)
            summary["failed_bcs"].append({"bc_id": bc_id, "reason": reason})
            for target in bc_targets:
                outcome[target["key"]] = "FAILED"
                summary["failed"] += 1
    for target_key, value in outcome.items():
        for hit in owners[target_key]:
            # 一个命中涉及多个对象时，按最需要关注的结果展示。
            if _priority(value) > _priority(hit.outcome):
                hit.outcome = value
    if summary["submitted"] == 0:
        # 没有提交任何变更：有失败记为失败，否则全部被频控、反向冲突等跳过。
        run.execution_status = "FAILED" if summary["failed"] else "SKIPPED"
    elif summary["failed"] or summary["conflicts"]:
        run.execution_status = "PARTIAL"
    else:
        run.execution_status = "SUBMITTED"
    run.execution_summary = summary
    if run.evidence_id is not None:
        evidence = session.get(AutomationRuleEvidence, run.evidence_id)
        if evidence is not None:
            evidence.readback_status = {
                "execution_status": run.execution_status,
                "submitted": summary.get("submitted", 0),
                "failed": summary.get("failed", 0),
            }
    session.flush()
    return summary


_PRIORITY = [
    "NOTIFIED",
    "PENDING_CONFIRMATION",
    "NOT_SELECTED",
    "NO_CHANGE",
    "UNSUPPORTED",
    "SKIPPED_FREQUENCY",
    "SKIPPED_PARENT_STATUS",
    "SKIPPED_PENDING",
    "SUBMITTED",
    "CONFLICT",
    "FAILED",
]


def _priority(value: str) -> int:
    return _PRIORITY.index(value) if value in _PRIORITY else -1


def _ledger(
    session: Session,
    tenant_id: UUID,
    keys: list[str],
    *,
    rule_id: UUID,
) -> list[AutomationActionLedger]:
    if not keys:
        return []
    # 每个对象/字段只取真正最新的动作，不按年龄截断；本规则保留完整频控历史。
    field = case(
        (col(AutomationActionLedger.action).in_(["PAUSE", "ENABLE"]), "STATUS"),
        else_=func.split_part(col(AutomationActionLedger.action), "_", 1),
    )
    latest = (
        select(col(AutomationActionLedger.id))
        .where(
            col(AutomationActionLedger.tenant_id) == tenant_id,
            col(AutomationActionLedger.target_key).in_(keys),
        )
        .distinct(col(AutomationActionLedger.target_key), field)
        .order_by(
            col(AutomationActionLedger.target_key),
            field,
            col(AutomationActionLedger.created_at).desc(),
            col(AutomationActionLedger.id).desc(),
        )
    )
    return list(
        session.exec(
            select(AutomationActionLedger)
            .where(
                col(AutomationActionLedger.tenant_id) == tenant_id,
                col(AutomationActionLedger.target_key).in_(keys),
                or_(
                    col(AutomationActionLedger.rule_id) == rule_id,
                    col(AutomationActionLedger.id).in_(latest),
                ),
            )
            .order_by(
                col(AutomationActionLedger.created_at), col(AutomationActionLedger.id)
            )
        ).all()
    )


def _still_matching(
    session: Session, tenant_id: UUID, rule_id: UUID, now: datetime
) -> RuleMatch:
    """对方规则按当前数据命中的执行对象。

    已删除、已关闭、改为仅通知或定义失效的规则不会再执行，不构成冲突。忽略其作用
    状态：被它暂停的对象已不在「启用中」范围内，仍要判断它的条件是否成立。
    """
    other = session.get(AutomationRule, rule_id)
    if other is None or other.tenant_id != tenant_id or not other.enabled:
        return RuleMatch("")
    try:
        definition = RuleDefinition.model_validate(other.definition)
    except ValidationError:
        return RuleMatch(other.name)
    if definition.mode == "NOTIFY":
        return RuleMatch(other.name)
    inputs = load_inputs(
        session, tenant_id=tenant_id, definition=definition, now=now, strict_facts=True
    )
    result = evaluate(definition, inputs, ignore_status=True, strict_facts=True)
    unknown_accounts = {
        row["advertiser_id"]
        for row in inputs.skipped_accounts
        if row.get("reason") == "STALE_REPORT"
    }
    if unknown_accounts and definition.aggregation != "EACH":
        # 合计条件不能用缺少账户的部分事实断言不命中。
        unknown_accounts.update(inputs.accounts)
    return RuleMatch(
        name=other.name,
        action=action_key(definition.action),
        keys=frozenset(target.key for hit in result.hits for target in hit.targets),
        unknown_keys=frozenset(result.unknown_targets),
        unknown_accounts=frozenset(unknown_accounts),
    )


def _execute_bc(
    session: Session,
    *,
    context: TenantContext,
    run: AutomationRuleRun,
    rule: AutomationRule,
    bc_id: str,
    targets: list[dict[str, Any]],
    action_name: str,
    now: datetime,
    outcome: dict[str, str],
    summary: dict[str, Any],
    optimizer_scope: RuleBindingScope | None = None,
) -> UUID | None:
    definition = RuleDefinition.model_validate(run.definition)
    selection = _selection(
        session, context=context, bc_id=bc_id, targets=targets, run=run, now=now
    )
    mutation = mutation_for(definition.action)
    index = target_index(targets)
    owners = _OwnerMap(session, rule.tenant_id, targets, index)
    preview = prepare_preview(session, context, bc_id, selection.id, mutation)
    # 一个规则目标可能对应多个预览项（素材在多条广告中的使用、预算持有者），
    # 先按目标汇总再定结果：任一项可执行即视为提交，避免已提交却未记台账。
    results: dict[str, list[Any]] = defaultdict(list)
    for item in preview.items:
        for key in owners.keys(item.ref, item.material_use):
            if key not in outcome:
                results[key].append(item)
    pending: dict[str, Any] = {}
    for key, items in results.items():
        executable = [item for item in items if item.execution_result == "PENDING"]
        if executable:
            pending[key] = executable[0]
        elif any(
            item.execution_result not in ("NO_CHANGE", "UNSUPPORTED") for item in items
        ):
            outcome[key] = "FAILED"
            summary["failed"] += 1
        elif any(item.execution_result == "NO_CHANGE" for item in items):
            outcome[key] = "NO_CHANGE"
            summary["no_change"] += 1
        else:
            # 广告管理判定不可操作（如普通广告的素材不能单独启停），不是执行失败。
            outcome[key] = "UNSUPPORTED"
            summary["unsupported"] += 1
            if len(summary["unsupported_details"]) < MAX_DETAILS:
                summary["unsupported_details"].append(
                    {"target_key": key, "reason": items[0].reason or ""}
                )
    for target in targets:
        if target["key"] not in outcome and target["key"] not in pending:
            # 预览没有为它生成可执行项（例如目录中不存在），视为失败而不是静默跳过。
            outcome[target["key"]] = "FAILED"
            summary["failed"] += 1
    if not pending:
        return None
    if optimizer_scope is not None:
        # This is intentionally the last local operation before handing the
        # frozen preview to the management outbox.  Rechecking earlier in the
        # executor alone would leave a race window while previewing.
        validate_rule_binding(session, optimizer_scope)
    task = submit_management_task(
        session,
        context,
        preview.preview_id,
        preview.digest,
        uuid5(NAMESPACE_URL, f"automation:{run.id}:{bc_id}"),
    )
    for target_key, item in pending.items():
        outcome[target_key] = "SUBMITTED"
        summary["submitted"] += 1
        ledger = AutomationActionLedger(
            tenant_id=rule.tenant_id,
            rule_id=rule.id,
            run_id=run.id,
            target_key=target_key,
            action=action_name,
            original_value=None
            if item.original_value is None
            else str(item.original_value),
            final_value=None if item.final_value is None else str(item.final_value),
            management_task_id=task.task_id,
            created_at=now,
            optimizer_id=run.optimizer_id,
            rule_version=run.rule_version,
            evidence_id=run.evidence_id,
        )
        session.add(ledger)
        if run.evidence_id is not None:
            evidence = session.get(AutomationRuleEvidence, run.evidence_id)
            if evidence is not None:
                evidence.action_ledger_refs = [
                    *evidence.action_ledger_refs,
                    str(ledger.id),
                ]
                task_ref = {"bc_id": bc_id, "task_id": str(task.task_id)}
                if task_ref not in evidence.management_tasks:
                    evidence.management_tasks = [*evidence.management_tasks, task_ref]
    session.flush()
    return task.task_id


class _OwnerMap:
    """把预览项映射回产生它的规则目标键。

    预算调整会被广告管理改到实际持有预算的对象上（广告组 → 所属系列，
    或系列 → 各广告组），台账与频控仍须记在规则目标上，下次运行才能看到。
    """

    def __init__(
        self,
        session: Session,
        tenant_id: UUID,
        targets: list[dict[str, Any]],
        index: dict[tuple[Any, ...], str],
    ) -> None:
        self.session, self.tenant_id, self.index = session, tenant_id, index
        self.targets = [t for t in targets if t["kind"] != "material"]
        self._parents: dict[tuple[str, str, str], str | None] = {}

    def _parent(self, advertiser_id: str, kind: str, remote_id: str) -> str | None:
        key = (advertiser_id, kind, remote_id)
        if key not in self._parents:
            row = self.session.exec(
                select(col(AdObject.parent_remote_id)).where(
                    col(AdObject.tenant_id) == self.tenant_id,
                    col(AdObject.advertiser_id) == advertiser_id,
                    col(AdObject.kind) == kind,
                    col(AdObject.remote_id) == remote_id,
                )
            ).first()
            self._parents[key] = row
        return self._parents[key]

    def keys(self, ref: EntityRef, material_use: Any) -> list[str]:
        direct = _target_key(self.index, ref, material_use)
        if material_use is not None or (
            ("object", ref.advertiser_id, ref.kind, ref.remote_id) in self.index
        ):
            return [direct]
        found = []
        for target in self.targets:
            if target["advertiser_id"] != ref.advertiser_id:
                continue
            # 持有者是目标的上级（广告组目标 → 系列持有预算）。
            if target["kind"] == "adgroup" and ref.kind == "campaign":
                if (
                    self._parent(ref.advertiser_id, "adgroup", target["remote_id"])
                    == ref.remote_id
                ):
                    found.append(target["key"])
            # 持有者是目标的下级（系列目标 → 各广告组持有预算）。
            elif target["kind"] == "campaign" and ref.kind == "adgroup":
                if (
                    self._parent(ref.advertiser_id, "adgroup", ref.remote_id)
                    == target["remote_id"]
                ):
                    found.append(target["key"])
        return found or [direct]
