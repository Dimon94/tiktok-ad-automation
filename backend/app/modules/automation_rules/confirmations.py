"""待确认批次：确认后执行的命中先放在这里，由人确认后执行。"""

from datetime import UTC, datetime
from uuid import UUID

from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.automation_rules.evaluate import evaluate
from app.modules.automation_rules.inputs import load_inputs
from app.modules.automation_rules.models import (
    AutomationConfirmation,
    AutomationRule,
    AutomationRuleHit,
    AutomationRuleRun,
)
from app.modules.automation_rules.schemas import (
    ConfirmationDetail,
    ConfirmationHit,
    ConfirmationItem,
    ConfirmationPage,
    RuleDefinition,
)
from app.modules.tenants.permissions import require_tenant

EXECUTE_TASK = "automation.execute"
register_dispatch_task(EXECUTE_TASK, "ad-management")


def open_batch(
    session: Session, *, run: AutomationRuleRun, rule: AutomationRule, now: datetime
) -> AutomationConfirmation:
    batch = AutomationConfirmation(
        tenant_id=rule.tenant_id, rule_id=rule.id, run_id=run.id, created_at=now
    )
    session.add(batch)
    for hit in session.exec(
        select(AutomationRuleHit).where(col(AutomationRuleHit.run_id) == run.id)
    ).all():
        hit.outcome = "PENDING_CONFIRMATION"
    run.execution_status = "AWAITING_CONFIRMATION"
    session.flush()
    return batch


def expire_open(
    session: Session, *, rule_id: UUID, now: datetime, optimizer_id: UUID | None = None
) -> int:
    """规则开始新一轮运行时，上一轮没处理的批次失效，避免按过期的结论执行。"""
    batches = session.exec(
        select(AutomationConfirmation)
        .join(
            AutomationRuleRun,
            col(AutomationRuleRun.id) == col(AutomationConfirmation.run_id),
        )
        .where(
            col(AutomationConfirmation.rule_id) == rule_id,
            col(AutomationConfirmation.status) == "PENDING",
            col(AutomationRuleRun.optimizer_id) == optimizer_id,
        )
        .with_for_update(of=AutomationConfirmation)
    ).all()
    for batch in batches:
        batch.status, batch.decided_at = "EXPIRED", now
        run = session.get(AutomationRuleRun, batch.run_id)
        if run is not None:
            run.execution_status = "EXPIRED"
            for hit in session.exec(
                select(AutomationRuleHit).where(
                    col(AutomationRuleHit.run_id) == run.id,
                    col(AutomationRuleHit.outcome) == "PENDING_CONFIRMATION",
                )
            ).all():
                hit.outcome = "NOT_SELECTED"
    session.flush()
    return len(batches)


def _load(
    session: Session, context: TenantContext, confirmation_id: UUID
) -> AutomationConfirmation:
    batch = session.get(AutomationConfirmation, confirmation_id)
    if batch is None or batch.tenant_id != context.tenant_id:
        raise DomainError("automation_confirmation_not_found", "待确认批次不存在")
    return batch


def _hits(session: Session, run_id: UUID) -> list[AutomationRuleHit]:
    return list(
        session.exec(
            select(AutomationRuleHit)
            .where(col(AutomationRuleHit.run_id) == run_id)
            .order_by(col(AutomationRuleHit.sequence))
        ).all()
    )


def _item(
    batch: AutomationConfirmation, rule_name: str, hits: list[AutomationRuleHit]
) -> ConfirmationItem:
    return ConfirmationItem(
        id=batch.id,
        rule_id=batch.rule_id,
        rule_name=rule_name,
        run_id=batch.run_id,
        status=batch.status,
        created_at=batch.created_at,
        hit_count=len(hits),
        target_count=sum(hit.target_count for hit in hits),
    )


def list_confirmations(
    session: Session, *, context: TenantContext, status: str | None
) -> ConfirmationPage:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    statement = (
        select(AutomationConfirmation, col(AutomationRule.name))
        .join(
            AutomationRule,
            col(AutomationRule.id) == col(AutomationConfirmation.rule_id),
        )
        .where(col(AutomationConfirmation.tenant_id) == context.tenant_id)
        .order_by(col(AutomationConfirmation.created_at).desc())
        .limit(200)
    )
    if status:
        statement = statement.where(col(AutomationConfirmation.status) == status)
    rows = session.exec(statement).all()
    items = [_item(batch, name, _hits(session, batch.run_id)) for batch, name in rows]
    return ConfirmationPage(items=items, total=len(items))


def confirmation_detail(
    session: Session,
    *,
    context: TenantContext,
    confirmation_id: UUID,
    now: datetime | None = None,
) -> ConfirmationDetail:
    """用运行时的规则定义快照和当前数据重新判断，并排给出判断时与现在的指标。"""
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    batch = _load(session, context, confirmation_id)
    run = session.get(AutomationRuleRun, batch.run_id)
    rule = session.get(AutomationRule, batch.rule_id)
    assert run is not None and rule is not None
    definition = RuleDefinition.model_validate(run.definition)
    from app.modules.ai_workbench.rule_binding import (
        RuleBindingScope,
        validate_rule_binding,
    )

    # 确认预览复用执行器的范围/绑定代数核验，聚合数据不能包含其他优化师账户。
    optimizer_scope = None
    if run.optimizer_id is not None:
        optimizer_scope = validate_rule_binding(
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
    inputs = load_inputs(
        session,
        tenant_id=context.tenant_id,
        definition=definition,
        now=now or datetime.now(UTC),
        # 与原运行判断同一批账户：按账户时区运行时只比较该槽位的账户。
        slot_at=run.slot_at,
        optimizer_scope=optimizer_scope,
    )
    current = {hit.subject_key: hit for hit in evaluate(definition, inputs).hits}
    # 未命中的对象也给出现在的指标，便于判断差异。
    hits = _hits(session, run.id)
    items = []
    for hit in hits:
        now_hit = current.get(hit.subject_key)
        items.append(
            ConfirmationHit(
                subject_key=hit.subject_key,
                subject_label=hit.subject_label,
                drama_name=hit.drama_name,
                advertiser_id=hit.advertiser_id,
                target_count=hit.target_count,
                metrics_then=hit.metrics,
                metrics_now=now_hit.metrics if now_hit else {},
                still_matching=now_hit is not None,
            )
        )
    return ConfirmationDetail(
        confirmation=_item(batch, rule.name, hits),
        action=definition.action,
        skipped_accounts=inputs.skipped_accounts,
        items=items,
    )


def _open_batch(
    session: Session, context: TenantContext, confirmation_id: UUID
) -> tuple[AutomationConfirmation, AutomationRuleRun, AutomationRule]:
    require_tenant(
        session,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        action="ads_manage",
    )
    batch = _load(session, context, confirmation_id)
    session.refresh(batch, with_for_update=True)
    if batch.status != "PENDING":
        raise DomainError("automation_confirmation_closed", "该批次已处理或已失效")
    run = session.get(AutomationRuleRun, batch.run_id)
    rule = session.get(AutomationRule, batch.rule_id)
    assert run is not None and rule is not None
    return batch, run, rule


def confirm(
    session: Session,
    *,
    context: TenantContext,
    confirmation_id: UUID,
    subject_keys: list[str],
    now: datetime | None = None,
) -> ConfirmationItem:
    batch, run, rule = _open_batch(session, context, confirmation_id)
    hits = _hits(session, run.id)
    known = {hit.subject_key for hit in hits}
    selected = list(dict.fromkeys(subject_keys))
    if not selected or not set(selected) <= known:
        raise DomainError("automation_confirmation_invalid", "所选对象不属于该批次")
    now = now or datetime.now(UTC)
    batch.status, batch.decided_by, batch.decided_at = (
        "CONFIRMED",
        context.actor_id,
        now,
    )
    run.execution_status = "CONFIRMED"
    enqueue_after_commit(
        session,
        context=TenantContext(rule.tenant_id, rule.updated_by, "operator"),
        task_name=EXECUTE_TASK,
        task_key=f"automation-exec:{run.id}",
        payload={"run_id": str(run.id), "mode": "CONFIRM", "selected_keys": selected},
    )
    session.flush()
    return _item(batch, rule.name, hits)


def dismiss(
    session: Session,
    *,
    context: TenantContext,
    confirmation_id: UUID,
    now: datetime | None = None,
) -> ConfirmationItem:
    batch, run, rule = _open_batch(session, context, confirmation_id)
    batch.status, batch.decided_by = "DISMISSED", context.actor_id
    batch.decided_at = now or datetime.now(UTC)
    run.execution_status = "DISMISSED"
    hits = _hits(session, run.id)
    for hit in hits:
        hit.outcome = "NOT_SELECTED"
    session.flush()
    return _item(batch, rule.name, hits)
