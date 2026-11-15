"""一次规则运行：判断、持久化命中、按需投递飞书通知。阶段 1 只预警，不改广告。"""

import logging
from dataclasses import asdict
from datetime import datetime
from typing import Any
from uuid import UUID

import httpx
from billiard.exceptions import SoftTimeLimitExceeded  # type: ignore[import-untyped]
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.ai_workbench.evidence import persist_evidence
from app.modules.ai_workbench.rule_binding import RuleBindingScope, resolve_rule_binding
from app.modules.automation_rules import feishu
from app.modules.automation_rules.confirmations import (
    EXECUTE_TASK,
    expire_open,
    open_batch,
)
from app.modules.automation_rules.data_issues import actionable_input_issues
from app.modules.automation_rules.evaluate import Evaluation, evaluate
from app.modules.automation_rules.inputs import load_inputs
from app.modules.automation_rules.models import (
    AutomationConfirmation,
    AutomationRule,
    AutomationRuleHit,
    AutomationRuleRun,
)
from app.modules.automation_rules.schemas import RuleDefinition
from app.modules.automation_rules.settings_service import load_feishu
from app.modules.tenants.permissions import require_tenant

logger = logging.getLogger(__name__)

EXECUTION_ERRORS = {
    "rule_owner_forbidden": "权限失效",
    "automation_disabled": "规则已停用",
    "execution_error": "执行异常",
}
SEND_TASK = "automation.send_feishu"
# 投递方在入队前就要能解析队列，注册放在投递所在模块。
register_dispatch_task(SEND_TASK, "control")


def _existing(
    session: Session, rule_id: UUID, slot_at: datetime, optimizer_id: UUID | None = None
) -> AutomationRuleRun | None:
    query = select(AutomationRuleRun).where(
        col(AutomationRuleRun.rule_id) == rule_id,
        col(AutomationRuleRun.slot_at) == slot_at,
    )
    if optimizer_id is None:
        query = query.where(col(AutomationRuleRun.optimizer_id).is_(None))
    else:
        query = query.where(col(AutomationRuleRun.optimizer_id) == optimizer_id)
    return session.exec(query).first()


def _start(
    session: Session,
    rule: AutomationRule,
    slot_at: datetime,
    now: datetime,
    scope: RuleBindingScope | None = None,
) -> AutomationRuleRun | None:
    """同一槽位只允许一次运行；重复投递时返回 None 交由调用方读取已有运行。"""
    run = AutomationRuleRun(
        tenant_id=rule.tenant_id,
        rule_id=rule.id,
        optimizer_id=scope.optimizer_id if scope else None,
        optimizer_version=scope.optimizer_version if scope else None,
        binding_version=scope.optimizer_version if scope else None,
        binding_id=scope.binding_id if scope else None,
        authorization_revision=scope.authorization_revision if scope else None,
        authorization_revisions=dict(scope.authorization_revisions) if scope else {},
        rule_version=rule.version,
        definition=rule.definition,
        slot_at=slot_at,
        started_at=now,
    )
    try:
        with session.begin_nested():
            session.add(run)
            session.flush()
    except IntegrityError:
        return None
    return run


def _finish_failed(
    run: AutomationRuleRun, code: str, now: datetime
) -> AutomationRuleRun:
    run.status, run.error_code, run.finished_at = "FAILED", code, now
    return run


def _write_hits(
    session: Session, rule: AutomationRule, run: AutomationRuleRun, result: Evaluation
) -> None:
    for sequence, hit in enumerate(result.hits):
        session.add(
            AutomationRuleHit(
                tenant_id=rule.tenant_id,
                run_id=run.id,
                sequence=sequence,
                # 剧名与对象键来自平台命名，长度不受控，按列宽截断。
                subject_key=hit.subject_key[:300],
                subject_label=hit.subject_label[:500],
                drama_name=hit.drama[:300] if hit.drama else None,
                advertiser_id=hit.advertiser_id,
                bc_id=hit.bc_id,
                currency=hit.currency,
                metrics=hit.metrics,
                targets=[asdict(target) for target in hit.targets],
                target_count=hit.target_count,
                outcome="NOTIFIED",
            )
        )
    session.flush()


def execute_run(
    session: Session,
    *,
    rule: AutomationRule,
    slot_at: datetime,
    now: datetime,
    optimizer_scope: RuleBindingScope | None = None,
    optimizer_id: UUID | None = None,
) -> AutomationRuleRun:
    if optimizer_scope is None and optimizer_id is not None:
        optimizer_scope = resolve_rule_binding(
            session,
            tenant_id=rule.tenant_id,
            optimizer_id=optimizer_id,
            rule_id=rule.id,
        )
    existing = _existing(
        session,
        rule.id,
        slot_at,
        optimizer_scope.optimizer_id if optimizer_scope else None,
    )
    if existing is not None:
        return existing
    run = _start(session, rule, slot_at, now, optimizer_scope)
    if run is None:
        found = _existing(
            session,
            rule.id,
            slot_at,
            optimizer_scope.optimizer_id if optimizer_scope else None,
        )
        assert found is not None
        return found
    # 新一轮运行开始，上一轮仍未确认的批次失效。
    expire_open(session, rule_id=rule.id, now=now, optimizer_id=run.optimizer_id)
    try:
        # 后台运行以规则最后修改人的身份重建权限；此人已离开租户时规则不再运行。
        require_tenant(
            session, actor_id=rule.updated_by, tenant_id=rule.tenant_id, action="read"
        )
    except DomainError:
        return _finish_failed(run, "rule_owner_forbidden", now)
    # 判断与命中写入放在保存点里：数据库错误或超时只回滚这一部分，
    # 运行记录仍以失败状态提交，槽位不会无记录地丢失。
    try:
        with session.begin_nested():
            definition = RuleDefinition.model_validate(rule.definition)
            inputs = load_inputs(
                session,
                tenant_id=rule.tenant_id,
                definition=definition,
                now=now,
                slot_at=slot_at,
                optimizer_scope=optimizer_scope,
            )
            result = evaluate(definition, inputs)
            _write_hits(session, rule, run, result)
            persist_evidence(
                session,
                rule=rule,
                run=run,
                result=result,
                skipped_accounts=inputs.skipped_accounts,
                scope=optimizer_scope,
                freshness=inputs.freshness,
                publication_versions=inputs.publication_versions,
                inputs=inputs,
            )
    except SoftTimeLimitExceeded:
        logger.warning("automation rule timed out", extra={"rule_id": str(rule.id)})
        return _finish_failed(run, "evaluation_timeout", now)
    except Exception:
        logger.exception(
            "automation rule evaluation failed", extra={"rule_id": str(rule.id)}
        )
        return _finish_failed(run, "evaluation_failed", now)
    run.checked_count = result.checked
    run.hit_count = len(result.hits)
    run.skipped_count = result.skipped_count
    run.skipped_accounts = inputs.skipped_accounts
    run.status, run.finished_at = "COMPLETE", now
    run.mode = definition.mode
    rule.last_run_at = now
    context = TenantContext(rule.tenant_id, rule.updated_by, "operator")
    if result.hits and definition.mode == "AUTO":
        # 自动执行：交给广告管理队列执行，执行完成后再发结果卡片。
        run.execution_status = "QUEUED"
        enqueue_after_commit(
            session,
            context=context,
            task_name=EXECUTE_TASK,
            task_key=f"automation-exec:{run.id}",
            payload={"run_id": str(run.id), "mode": "AUTO"},
        )
        session.flush()
        return run
    if result.hits and definition.mode != "NOTIFY":
        # 确认后执行：先生成待确认批次，由人确认。
        open_batch(session, run=run, rule=rule, now=now)
    notify = definition.notification
    issues = actionable_input_issues(inputs.skipped_accounts)
    stale_only = not result.hits and not notify.notify_without_hits and bool(issues)
    if (
        notify.enabled
        and stale_only
        and _same_issues_as_last_sent(session, rule.id, run.id, issues)
    ):
        # 真正的数据异常未变化时不重复提醒；普通待报表已在上方排除。
        pass
    elif notify.enabled and (result.hits or notify.notify_without_hits or issues):
        # 汇总模式下预警等到固定时刻合并发送；待确认批次在下次运行即失效，仍立即发送。
        if (
            notify.timing == "SUMMARY"
            and run.execution_status != "AWAITING_CONFIRMATION"
        ):
            run.notify_status = "SUMMARY"
        else:
            queue_notification(session, run=run, context=context)
    session.flush()
    return run


def _same_issues_as_last_sent(
    session: Session,
    rule_id: UUID,
    run_id: UUID,
    skipped: list[dict[str, str]],
) -> bool:
    last = session.exec(
        select(AutomationRuleRun)
        .where(
            col(AutomationRuleRun.rule_id) == rule_id,
            col(AutomationRuleRun.id) != run_id,
            col(AutomationRuleRun.notify_status) == "SENT",
        )
        .order_by(col(AutomationRuleRun.slot_at).desc())
        .limit(1)
    ).first()
    if last is None or last.hit_count:
        return False

    # 同账户的缺数原因或影响数量变化仍需通知，不能只比较账户集合。
    def signature(items: list[dict[str, str]]) -> set[tuple[str, str, str]]:
        return {
            (item["advertiser_id"], item.get("reason", ""), item.get("count", ""))
            for item in actionable_input_issues(items)
        }

    return signature(skipped) == signature(last.skipped_accounts)


def queue_notification(
    session: Session,
    *,
    run: AutomationRuleRun,
    context: TenantContext,
    phase: str | None = None,
) -> None:
    run.notify_status = "PENDING"
    enqueue_after_commit(
        session,
        context=context,
        task_name=SEND_TASK,
        # 同一次运行可能先后发送待确认与执行结果两张卡片，按状态区分任务标识。
        task_key=f"automation-feishu:{run.id}:{phase or run.execution_status}",
        payload={"run_id": str(run.id)},
    )


def notify_execution(
    session: Session,
    *,
    run: AutomationRuleRun,
    rule: AutomationRule,
    phase: str | None = None,
) -> None:
    """执行完成后按运行时的规则快照决定是否发执行结果卡片；汇总模式并入汇总。"""
    notification = RuleDefinition.model_validate(run.definition).notification
    if notification.enabled and notification.timing == "SUMMARY":
        run.notify_status = "SUMMARY"
    elif notification.enabled:
        queue_notification(
            session,
            run=run,
            context=TenantContext(rule.tenant_id, rule.updated_by, "operator"),
            phase=phase,
        )
    session.flush()


def failed_summary(summary: dict[str, Any]) -> bool:
    results = summary.get("results", {})
    return any(summary.get(k) for k in ("failed", "failed_bcs", "error")) or any(
        results.get(k)
        for k in ("unknown", "rejected", "not_sent", "conflict", "cancelled")
    )


def execution_brief(summary: dict[str, Any], action: str) -> str:
    """失败优先，随后展示提交及跳过数量；原始原因始终留在执行记录。"""
    parts = []
    if summary.get("failed"):
        parts.append(f"失败 {summary['failed']}")
    elif summary.get("failed_bcs"):
        parts.append("通道失败")
    if summary.get("error"):
        parts.append(EXECUTION_ERRORS.get(summary["error"], "执行异常"))
    if summary.get("submitted"):
        parts.append(f"{action}已提交 {summary['submitted']}")
    results = summary.get("results", {})
    for key, label in (
        ("accepted", "平台已受理"),
        ("pending", "等待执行/重试"),
        ("unknown", "结果未知"),
        ("rejected", "平台拒绝"),
        ("not_sent", "未发送"),
        ("conflict", "发送前冲突"),
        ("cancelled", "已取消"),
        ("no_change", "无需变更"),
    ):
        if results.get(key):
            parts.append(f"{label} {results[key]}")
    if summary.get("skipped_pending"):
        parts.append(f"等待前次操作 {summary['skipped_pending']}")
    if summary.get("conflicts"):
        parts.append(f"冲突 {summary['conflicts']}")
    skipped = sum(
        int(summary.get(k, 0) or 0)
        for k in (
            "skipped_frequency",
            "skipped_parent_status",
            "unsupported",
            "no_change",
        )
    )
    if skipped:
        parts.append(f"跳过 {skipped}")
    return "；".join(parts) or "未提交操作"


def action_caption(definition: RuleDefinition) -> str:
    return {"PAUSE": "暂停", "ENABLE": "启用", "BUDGET": "调预算", "ROAS": "调ROAS"}[
        definition.action.type
    ]


def notification_worthy(run: AutomationRuleRun) -> bool:
    """投递时再把关，升级前排队的普通无命中报告也不继续打扰用户。"""
    if run.execution_status in ("EXPIRED", "DISMISSED"):
        return False
    notify = RuleDefinition.model_validate(run.definition).notification
    return bool(
        run.hit_count
        or run.status == "FAILED"
        or notify.notify_without_hits
        or actionable_input_issues(run.skipped_accounts)
    )


def build_run_card(
    rule: AutomationRule,
    run: AutomationRuleRun,
    *,
    confirmation_id: UUID | None = None,
) -> dict[str, Any]:
    definition = RuleDefinition.model_validate(run.definition)
    caption = feishu.rule_caption(rule.name)
    action = action_caption(definition)
    base = f"{settings.FRONTEND_HOST.rstrip('/')}/tenants/{rule.tenant_id}"
    link = f"{base}/automation-rules?tab=runs&run={run.id}"
    button = "查看详情"
    status = run.execution_status
    if run.status == "FAILED":
        title, template, content = (
            caption,
            "red",
            "规则检查失败，请查看详情",
        )
    elif status == "AWAITING_CONFIRMATION":
        title, template = caption, "yellow"
        content = f"{run.hit_count} 项待{action}；下次运行前确认"
        if confirmation_id is not None:
            link = f"{base}/automation-rules?tab=confirmations&confirmation={confirmation_id}"
            button = "去确认"
    elif status in (
        "SUBMITTED",
        "PARTIAL",
        "FAILED",
        "SKIPPED",
        "ACCEPTED",
        "NEEDS_REVIEW",
    ):
        failed = status == "FAILED" or failed_summary(run.execution_summary)
        title = caption
        template = (
            "red"
            if failed
            else {
                "SUBMITTED": "blue",
                "ACCEPTED": "green",
                "NEEDS_REVIEW": "red",
                "PARTIAL": "orange",
                "SKIPPED": "grey",
            }.get(status, "red")
        )
        content = execution_brief(run.execution_summary, action)
        if status == "FAILED" and not failed_summary(run.execution_summary):
            content = "执行失败；" + content
    elif run.hit_count:
        title, template, content = (
            caption,
            "orange",
            f"命中 {run.hit_count} 项",
        )
    elif actionable_input_issues(run.skipped_accounts):
        title, template = caption, "orange"
        accounts = {
            item["advertiser_id"]
            for item in actionable_input_issues(run.skipped_accounts)
        }
        content = f"{len(accounts)} 个账户存在数据异常"
    else:
        title, template, content = caption, "blue", "本轮无命中"
    return feishu.build_card(
        title=title, text=content, link=link, template=template, button=button
    )


def deliver_notification(
    session: Session,
    *,
    run_id: UUID,
    transport: httpx.BaseTransport | None = None,
) -> str:
    run = session.get(AutomationRuleRun, run_id)
    if run is None or run.notify_status != "PENDING":
        return run.notify_status if run else "NOT_REQUIRED"
    if not notification_worthy(run):
        run.notify_status = "NOT_REQUIRED"
        run.notify_error = None
        session.flush()
        return run.notify_status
    rule = session.get(AutomationRule, run.rule_id)
    target = load_feishu(session, run.tenant_id)
    if rule is None or target is None:
        run.notify_status, run.notify_error = "FAILED", "未配置或未启用飞书通知"
        session.flush()
        return run.notify_status
    try:
        confirmation_id = session.exec(
            select(col(AutomationConfirmation.id)).where(
                col(AutomationConfirmation.run_id) == run.id,
                col(AutomationConfirmation.status) == "PENDING",
            )
        ).first()
        card = build_run_card(rule, run, confirmation_id=confirmation_id)
        feishu.send(target[0], target[1], card, transport=transport)
    except DomainError as exc:
        run.notify_status, run.notify_error = "FAILED", str(exc.message)[:500]
    else:
        run.notify_status, run.notify_error = "SENT", None
    session.flush()
    return run.notify_status
