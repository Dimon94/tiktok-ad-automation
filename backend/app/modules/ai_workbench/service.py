"""Tenant-scoped AI optimizer transactions.

The service deliberately keeps all reads and writes behind ``TenantContext``.
No TikTok or other external calls are made here; downstream tasks consume the
immutable version snapshot created by these short database transactions.
"""

import hashlib
import json
from typing import Any, cast
from typing import cast as typing_cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.core.pagination import count_rows
from app.modules.accounts.access import usable_grants
from app.modules.accounts.models import BCAccountAccess
from app.modules.ai_workbench.models import (
    AIOptimizer,
    AIOptimizerRuleBinding,
    AIOptimizerStrategyBinding,
    AIOptimizerVersion,
)
from app.modules.ai_workbench.schemas import (
    OptimizerCreate,
    OptimizerFields,
    OptimizerPage,
    OptimizerPublic,
    OptimizerUpdate,
)
from app.modules.automation_rules.models import AutomationRule
from app.modules.strategies.models import StrategyVersion
from app.modules.tenants.models import AuditEvent
from app.modules.tenants.permissions import require_tenant


def _authorize(
    session: Session, context: TenantContext, *, write: bool = False
) -> None:
    require_tenant(
        session,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        action="strategy_write" if write else "read",
    )


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def _request_lock(session: Session, *, tenant_id: UUID, request_id: UUID) -> None:
    """Serialize one tenant/request before checking the idempotency row."""
    key = int.from_bytes(
        hashlib.sha256(f"ai-optimizer:{tenant_id}:{request_id}".encode()).digest()[:8],
        signed=True,
    )
    cast(SQLAlchemySession, session).execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": key}
    )


def _optimizer(
    session: Session, context: TenantContext, optimizer_id: UUID, *, lock: bool = False
) -> AIOptimizer:
    query = select(AIOptimizer).where(
        col(AIOptimizer.tenant_id) == context.tenant_id,
        col(AIOptimizer.id) == optimizer_id,
    )
    if lock:
        query = query.with_for_update()
    row = session.exec(query.execution_options(populate_existing=True)).one_or_none()
    if row is None:
        raise DomainError("ai_optimizer_not_found", "ai_optimizer_not_found")
    return row


def _validate_scope(
    session: Session, *, tenant_id: UUID, bc_id: str, advertiser_ids: tuple[str, ...]
) -> None:
    # Reuse the canonical access contract: BC ownership, active connection,
    # authorized/VERIFIED evidence, and advertiser metadata are all required.
    grants = session.exec(
        usable_grants(tenant_id=tenant_id, bc_id=bc_id, action="build").where(
            col(BCAccountAccess.advertiser_id).in_(advertiser_ids)
        )
    ).all()
    if {grant.advertiser_id for grant in grants} != set(advertiser_ids):
        raise DomainError("ai_invalid_scope", "ai_invalid_scope")


def _validate_strategy(
    session: Session, *, tenant_id: UUID, strategy_version_id: UUID
) -> StrategyVersion:
    row = session.exec(
        select(StrategyVersion).where(
            col(StrategyVersion.tenant_id) == tenant_id,
            col(StrategyVersion.id) == strategy_version_id,
        )
    ).one_or_none()
    if row is None:
        raise DomainError(
            "ai_strategy_binding_required", "ai_strategy_binding_required"
        )
    return row


def _guard_shared_strategy(
    session: Session,
    *,
    tenant_id: UUID,
    optimizer_id: UUID,
    strategy_version_id: UUID,
) -> None:
    # A shared immutable strategy version cannot silently inherit one optimizer's
    # authorization. Callers must append an optimizer-owned version explicitly.
    strategy = session.exec(
        select(StrategyVersion)
        .where(
            col(StrategyVersion.tenant_id) == tenant_id,
            col(StrategyVersion.id) == strategy_version_id,
        )
        .with_for_update()
    ).one_or_none()
    if strategy is None:
        raise DomainError(
            "ai_strategy_binding_required", "ai_strategy_binding_required"
        )
    existing = session.exec(
        select(AIOptimizerStrategyBinding)
        .where(
            col(AIOptimizerStrategyBinding.tenant_id) == tenant_id,
            col(AIOptimizerStrategyBinding.strategy_version_id) == strategy_version_id,
        )
        .with_for_update()
    ).all()
    if any(row.optimizer_id != optimizer_id for row in existing):
        raise DomainError("ai_strategy_shared", "ai_strategy_shared")


def _validate_rules(
    session: Session, *, tenant_id: UUID, rule_ids: tuple[UUID, ...]
) -> None:
    if len(set(rule_ids)) != len(rule_ids):
        raise DomainError("ai_invalid_rule_scope", "ai_invalid_rule_scope")
    if not rule_ids:
        return
    found = session.exec(
        select(col(AutomationRule.id)).where(
            col(AutomationRule.tenant_id) == tenant_id,
            col(AutomationRule.id).in_(rule_ids),
        )
    ).all()
    if set(found) != set(rule_ids):
        raise DomainError("ai_invalid_rule_scope", "ai_invalid_rule_scope")


def _fields(body: OptimizerCreate | OptimizerFields) -> OptimizerFields:
    return OptimizerFields.model_validate(
        body.model_dump(exclude={"name", "request_id", "active"})
    )


def _active_version(
    session: Session, *, tenant_id: UUID, optimizer: AIOptimizer
) -> AIOptimizerVersion:
    row = session.exec(
        select(AIOptimizerVersion).where(
            col(AIOptimizerVersion.tenant_id) == tenant_id,
            col(AIOptimizerVersion.optimizer_id) == optimizer.id,
            col(AIOptimizerVersion.number) == optimizer.latest_version,
        )
    ).one_or_none()
    if row is None:
        raise DomainError(
            "ai_optimizer_version_missing", "ai_optimizer_version_missing"
        )
    return row


def _active_binding(
    session: Session, *, tenant_id: UUID, optimizer_id: UUID
) -> AIOptimizerStrategyBinding:
    row = session.exec(
        select(AIOptimizerStrategyBinding).where(
            col(AIOptimizerStrategyBinding.tenant_id) == tenant_id,
            col(AIOptimizerStrategyBinding.optimizer_id) == optimizer_id,
            col(AIOptimizerStrategyBinding.active).is_(True),
        )
    ).one_or_none()
    if row is None:
        raise DomainError(
            "ai_strategy_binding_required", "ai_strategy_binding_required"
        )
    return row


def _sync_bindings(
    session: Session,
    *,
    context: TenantContext,
    optimizer_id: UUID,
    strategy_version_id: UUID,
    rule_ids: tuple[UUID, ...],
) -> AIOptimizerStrategyBinding:
    current = session.exec(
        select(AIOptimizerStrategyBinding)
        .where(
            col(AIOptimizerStrategyBinding.tenant_id) == context.tenant_id,
            col(AIOptimizerStrategyBinding.optimizer_id) == optimizer_id,
            col(AIOptimizerStrategyBinding.active).is_(True),
        )
        .with_for_update()
    ).one_or_none()
    if current is not None and current.strategy_version_id == strategy_version_id:
        binding = current
    else:
        if current is not None:
            current.active = False
            session.add(current)
        binding = AIOptimizerStrategyBinding(
            tenant_id=context.tenant_id,
            optimizer_id=optimizer_id,
            strategy_version_id=strategy_version_id,
            active=True,
            created_by=context.actor_id,
        )
        session.add(binding)
        session.flush()

    existing = session.exec(
        select(AIOptimizerRuleBinding).where(
            col(AIOptimizerRuleBinding.tenant_id) == context.tenant_id,
            col(AIOptimizerRuleBinding.optimizer_id) == optimizer_id,
        )
    ).all()
    wanted = set(rule_ids)
    for row in existing:
        row.active = row.rule_id in wanted
        session.add(row)
    known = {row.rule_id for row in existing}
    for rule_id in wanted - known:
        session.add(
            AIOptimizerRuleBinding(
                tenant_id=context.tenant_id,
                optimizer_id=optimizer_id,
                rule_id=rule_id,
                active=True,
                created_by=context.actor_id,
            )
        )
    session.flush()
    return binding


def _public(
    session: Session, optimizer: AIOptimizer, version: AIOptimizerVersion
) -> OptimizerPublic:
    rules = session.exec(
        select(col(AIOptimizerRuleBinding.rule_id)).where(
            col(AIOptimizerRuleBinding.tenant_id) == optimizer.tenant_id,
            col(AIOptimizerRuleBinding.optimizer_id) == optimizer.id,
            col(AIOptimizerRuleBinding.active).is_(True),
        )
    ).all()
    binding = typing_cast(
        AIOptimizerStrategyBinding,
        session.get(AIOptimizerStrategyBinding, version.strategy_binding_id),
    )

    return OptimizerPublic.model_validate(
        {
            "id": optimizer.id,
            "tenant_id": optimizer.tenant_id,
            "name": optimizer.name,
            "active": optimizer.active,
            "version": version.number,
            "daily_spend": version.daily_spend,
            "target_roas": version.target_roas,
            "priority": version.priority,
            "bc_id": version.bc_id,
            "advertiser_ids": tuple(version.advertiser_ids),
            "optimization_mode": version.optimization_mode,
            "timezone": version.timezone,
            "review_time": version.review_time,
            "material_config": version.material_config,
            "authorization_limits": version.authorization_limits,
            "strategy_version_id": binding.strategy_version_id,
            "rule_ids": tuple(rules),
            "strategy_binding_id": version.strategy_binding_id,
            "created_by": optimizer.created_by,
            "created_at": optimizer.created_at,
            "version_created_at": version.created_at,
            "request_id": version.request_id,
        }
    )


def create_optimizer(
    session: Session, *, context: TenantContext, body: OptimizerCreate
) -> OptimizerPublic:
    _authorize(session, context, write=True)
    fields = _fields(body)
    request_id = body.request_id
    payload = {
        "kind": "create",
        "name": body.name.strip(),
        "active": body.active,
        **fields.model_dump(mode="json"),
    }
    digest = _digest(payload)
    _request_lock(session, tenant_id=context.tenant_id, request_id=request_id)
    existing = session.exec(
        select(AIOptimizerVersion).where(
            col(AIOptimizerVersion.tenant_id) == context.tenant_id,
            col(AIOptimizerVersion.request_id) == request_id,
        )
    ).one_or_none()
    if existing:
        if existing.request_digest != digest:
            raise DomainError("idempotency_conflict", "idempotency_conflict")
        optimizer = _optimizer(session, context, existing.optimizer_id)
        return _public(session, optimizer, existing)
    _validate_scope(
        session,
        tenant_id=context.tenant_id,
        bc_id=fields.bc_id,
        advertiser_ids=fields.advertiser_ids,
    )
    _validate_strategy(
        session,
        tenant_id=context.tenant_id,
        strategy_version_id=fields.strategy_version_id,
    )
    _validate_rules(session, tenant_id=context.tenant_id, rule_ids=fields.rule_ids)
    optimizer = AIOptimizer(
        tenant_id=context.tenant_id,
        name=body.name.strip(),
        active=body.active,
        latest_version=0,
        created_by=context.actor_id,
    )
    session.add(optimizer)
    session.flush()
    _guard_shared_strategy(
        session,
        tenant_id=context.tenant_id,
        optimizer_id=optimizer.id,
        strategy_version_id=fields.strategy_version_id,
    )
    binding = _sync_bindings(
        session,
        context=context,
        optimizer_id=optimizer.id,
        strategy_version_id=fields.strategy_version_id,
        rule_ids=fields.rule_ids,
    )
    optimizer.latest_version = 1
    version = AIOptimizerVersion(
        tenant_id=context.tenant_id,
        optimizer_id=optimizer.id,
        number=1,
        daily_spend=fields.daily_spend,
        target_roas=fields.target_roas,
        priority=fields.priority,
        bc_id=fields.bc_id,
        advertiser_ids=list(fields.advertiser_ids),
        optimization_mode=fields.optimization_mode,
        timezone=fields.timezone,
        review_time=fields.review_time,
        material_config=fields.material_config,
        authorization_limits=fields.authorization_limits,
        strategy_binding_id=binding.id,
        request_id=request_id,
        request_digest=digest,
        created_by=context.actor_id,
    )
    session.add(version)
    session.add(
        AuditEvent(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            action="ai_optimizer.create",
            target_id=str(optimizer.id),
            details={"request_id": str(request_id), "digest": digest, "version": 1},
        )
    )
    session.flush()
    return _public(session, optimizer, version)


def get_optimizer(
    session: Session, *, context: TenantContext, optimizer_id: UUID
) -> OptimizerPublic:
    _authorize(session, context)
    optimizer = _optimizer(session, context, optimizer_id)
    return _public(
        session,
        optimizer,
        _active_version(session, tenant_id=context.tenant_id, optimizer=optimizer),
    )


def list_optimizers(session: Session, *, context: TenantContext) -> OptimizerPage:
    _authorize(session, context)
    statement = (
        select(AIOptimizer)
        .where(col(AIOptimizer.tenant_id) == context.tenant_id)
        .order_by(col(AIOptimizer.created_at).desc())
    )
    rows = session.exec(statement).all()
    return OptimizerPage(
        items=[
            _public(
                session,
                row,
                _active_version(session, tenant_id=context.tenant_id, optimizer=row),
            )
            for row in rows
        ],
        total=count_rows(session, statement),
    )


def update_optimizer(
    session: Session,
    *,
    context: TenantContext,
    optimizer_id: UUID,
    body: OptimizerUpdate,
) -> OptimizerPublic:
    _authorize(session, context, write=True)
    optimizer = _optimizer(session, context, optimizer_id, lock=True)
    current = _active_version(session, tenant_id=context.tenant_id, optimizer=optimizer)
    current_binding = _active_binding(
        session, tenant_id=context.tenant_id, optimizer_id=optimizer.id
    )
    current_rules = tuple(
        session.exec(
            select(col(AIOptimizerRuleBinding.rule_id)).where(
                col(AIOptimizerRuleBinding.tenant_id) == context.tenant_id,
                col(AIOptimizerRuleBinding.optimizer_id) == optimizer.id,
                col(AIOptimizerRuleBinding.active).is_(True),
            )
        ).all()
    )
    values = {
        "daily_spend": body.daily_spend
        if body.daily_spend is not None
        else current.daily_spend,
        "target_roas": (
            body.target_roas
            if "target_roas" in body.model_fields_set
            else current.target_roas
        ),
        "priority": body.priority or current.priority,
        "bc_id": body.bc_id or current.bc_id,
        "advertiser_ids": body.advertiser_ids
        if body.advertiser_ids is not None
        else tuple(current.advertiser_ids),
        "optimization_mode": body.optimization_mode or current.optimization_mode,
        "timezone": body.timezone or current.timezone,
        "review_time": body.review_time or current.review_time,
        "material_config": body.material_config
        if body.material_config is not None
        else current.material_config,
        "authorization_limits": body.authorization_limits
        if body.authorization_limits is not None
        else current.authorization_limits,
        "strategy_version_id": body.strategy_version_id
        or current_binding.strategy_version_id,
        "rule_ids": body.rule_ids if body.rule_ids is not None else current_rules,
    }
    fields = OptimizerFields.model_validate(values)
    _validate_scope(
        session,
        tenant_id=context.tenant_id,
        bc_id=fields.bc_id,
        advertiser_ids=fields.advertiser_ids,
    )
    _validate_strategy(
        session,
        tenant_id=context.tenant_id,
        strategy_version_id=fields.strategy_version_id,
    )
    _validate_rules(session, tenant_id=context.tenant_id, rule_ids=fields.rule_ids)
    _guard_shared_strategy(
        session,
        tenant_id=context.tenant_id,
        optimizer_id=optimizer.id,
        strategy_version_id=fields.strategy_version_id,
    )
    payload = {
        "kind": "update",
        "optimizer_id": str(optimizer.id),
        "name": body.name,
        "active": body.active,
        **fields.model_dump(mode="json"),
    }
    digest = _digest(payload)
    request_id = body.request_id
    _request_lock(session, tenant_id=context.tenant_id, request_id=request_id)
    existing = session.exec(
        select(AIOptimizerVersion).where(
            col(AIOptimizerVersion.tenant_id) == context.tenant_id,
            col(AIOptimizerVersion.request_id) == request_id,
        )
    ).one_or_none()
    if existing:
        if existing.request_digest != digest:
            raise DomainError("idempotency_conflict", "idempotency_conflict")
        return _public(session, optimizer, existing)
    binding = _sync_bindings(
        session,
        context=context,
        optimizer_id=optimizer.id,
        strategy_version_id=fields.strategy_version_id,
        rule_ids=fields.rule_ids,
    )
    optimizer.latest_version += 1
    if body.name is not None:
        optimizer.name = body.name.strip()
    if body.active is not None:
        optimizer.active = body.active
    version = AIOptimizerVersion(
        tenant_id=context.tenant_id,
        optimizer_id=optimizer.id,
        number=optimizer.latest_version,
        daily_spend=fields.daily_spend,
        target_roas=fields.target_roas,
        priority=fields.priority,
        bc_id=fields.bc_id,
        advertiser_ids=list(fields.advertiser_ids),
        optimization_mode=fields.optimization_mode,
        timezone=fields.timezone,
        review_time=fields.review_time,
        material_config=fields.material_config,
        authorization_limits=fields.authorization_limits,
        strategy_binding_id=binding.id,
        request_id=request_id,
        request_digest=digest,
        created_by=context.actor_id,
    )
    session.add(version)
    session.add(
        AuditEvent(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            action="ai_optimizer.update",
            target_id=str(optimizer.id),
            details={
                "request_id": str(request_id),
                "digest": digest,
                "version": version.number,
            },
        )
    )
    session.flush()
    return _public(session, optimizer, version)
