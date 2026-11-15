"""Durable DSH review tasks and frozen evidence assembly."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from typing import cast as typing_cast
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, ForeignKeyConstraint, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Session, SQLModel, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.ai_workbench.knowledge_refs import (
    sanitize_public,
    snapshot_knowledge_refs,
)
from app.modules.ai_workbench.models import (
    AIOptimizer,
    AIOptimizerVersion,
    BuildOccurrence,
)
from app.modules.ai_workbench.plans import AIDailyPlanRow
from app.modules.automation_rules.models import AutomationRuleEvidence


def _now() -> datetime:
    return datetime.now(UTC)


class DSHReviewTask(SQLModel, table=True):
    """One fenced, tenant-scoped DSH review request."""

    __tablename__ = "ai_dsh_review_task"
    __table_args__ = (
        Index(
            "ix_ai_dsh_review_optimizer_created",
            "tenant_id",
            "optimizer_id",
            "created_at",
        ),
        UniqueConstraint("tenant_id", "request_id", name="uq_ai_dsh_review_request"),
        ForeignKeyConstraint(
            ["tenant_id", "optimizer_id"],
            ["ai_optimizer.tenant_id", "ai_optimizer.id"],
            name="fk_ai_dsh_review_optimizer",
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    optimizer_id: UUID
    request_id: UUID
    request_digest: str = Field(default="", max_length=64)
    dsh_task_id: str = Field(max_length=160)
    status: str = Field(default="PENDING", max_length=16)
    authorization_expires_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), index=False, unique=False, nullable=False
        )
    )
    fence: str = Field(max_length=64)
    restart_generation: int = 0
    cancellation_requested: bool = False
    optimizer_version: int
    evidence: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    # 私有冻结正文不进入领取响应；初始 evidence 仅含摘要和目录。
    evidence_store: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    result: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    created_by: UUID = Field(foreign_key="user.id")
    created_at: datetime = Field(
        default_factory=_now,
        sa_column=Column(
            DateTime(timezone=True),
            index=False,
            unique=False,
            nullable=False,
            default=_now,
        ),
    )
    started_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True), index=False, unique=False, nullable=True
        ),
    )
    finished_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True), index=False, unique=False, nullable=True
        ),
    )


def authorize(session: Session, context: TenantContext, *, write: bool = False) -> None:
    # Import lazily: permissions resolves ``app.models.User`` and importing it
    # while the metadata registry is loading would create a circular import
    # for direct DSH worker consumers.
    from app.modules.tenants.permissions import require_tenant

    require_tenant(
        session,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        action="strategy_write" if write else "read",
    )


def load_optimizer(
    session: Session, context: TenantContext, optimizer_id: UUID
) -> tuple[AIOptimizer, AIOptimizerVersion]:
    optimizer = session.exec(
        select(AIOptimizer).where(
            col(AIOptimizer.tenant_id) == context.tenant_id,
            col(AIOptimizer.id) == optimizer_id,
        )
    ).one_or_none()
    if optimizer is None:
        raise DomainError("ai_optimizer_not_found", "ai_optimizer_not_found")
    version = session.exec(
        select(AIOptimizerVersion).where(
            col(AIOptimizerVersion.tenant_id) == context.tenant_id,
            col(AIOptimizerVersion.optimizer_id) == optimizer.id,
            col(AIOptimizerVersion.number) == optimizer.latest_version,
        )
    ).one_or_none()
    if version is None:
        raise DomainError(
            "ai_optimizer_version_missing", "ai_optimizer_version_missing"
        )
    return optimizer, version


def _digest(value: Any) -> str:
    payload = json.dumps(
        sanitize_public(value), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except TypeError, ValueError:
        return False
    return True


def frozen_evidence_ids(evidence: Mapping[str, Any] | None) -> set[str]:
    """Return citable IDs; on-demand tasks grant IDs only after successful delivery.

    Review proposals may cite report snapshots, business runs, operation
    exports, build occurrences, plans, or rule evidence.  Keeping this list
    explicit prevents a proposal from citing a live row that was never frozen.
    """
    if not isinstance(evidence, Mapping):
        return set()

    if evidence.get("reading_contract") == "on-demand-v1":
        return {
            *evidence.get("read_evidence_ids", []),
            *(str(row["id"]) for row in evidence.get("knowledge", [])),
            *(str(row["id"]) for row in evidence.get("rule_backtests", [])),
        }

    fields_by_category: dict[str, tuple[str, ...]] = {
        "knowledge": ("id",),
        "rule_backtests": ("id",),
        "plans": ("id",),
        "build_occurrences": ("id", "strategy_version_id"),
        "rule_evidence": ("id",),
        "reporting": ("id", "version_id"),
        "business": ("id", "run_id"),
        "operation_logs": ("id", "export_id"),
    }
    result: set[str] = set()
    for category, fields in fields_by_category.items():
        value = evidence.get(category)
        if category == "reporting" and isinstance(value, Mapping):
            rows = value.get("snapshots", ())
            nested = []
            for snapshot in rows if isinstance(rows, list) else ():
                if isinstance(snapshot, Mapping):
                    nested.append(snapshot)
                    refs = snapshot.get("evidence", ())
                    if isinstance(refs, list):
                        nested.extend(refs)
            rows = nested
        elif category == "business" and isinstance(value, Mapping):
            rows = []
            for key in ("runs", "publications"):
                candidate = value.get(key, ())
                if isinstance(candidate, list):
                    rows.extend(candidate)
        elif category == "operation_logs" and isinstance(value, Mapping):
            rows = []
            for key in ("coverage", "exports"):
                candidate = value.get(key, ())
                if isinstance(candidate, list):
                    rows.extend(candidate)
        else:
            rows = value if isinstance(value, list) else ()
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            for field in fields:
                item = row.get(field)
                if item is not None:
                    result.add(str(item))
    # 只接纳已冻结类别中的来源标识，不递归接受任意 UUID/对象标识。
    for row in evidence.get("material_orders", {}).get("rows", []):
        if isinstance(row, Mapping) and _is_uuid(row.get("id")):
            result.add(row["id"])
    for row in evidence.get("attribution", {}).get("rows", []):
        for report in row.get("reports", []):
            if _is_uuid(report.get("id")):
                result.add(report["id"])
    history = evidence.get("history", {})
    if isinstance(history, Mapping):
        for category in ("reviews", "proposals"):
            for row in history.get(category, []):
                if isinstance(row, Mapping) and row.get("id"):
                    result.add(str(row["id"]))
    pack = evidence.get("data_pack", {})
    if isinstance(pack, Mapping):
        for category, id_field in (
            ("business", "run_id"),
            ("operation_events", "export_id"),
        ):
            for group in pack.get(category, []):
                for row in group.get("rows", []):
                    if row.get(id_field):
                        result.add(str(row[id_field]))
    return result


def frozen_evidence(
    session: Session,
    *,
    context: TenantContext,
    optimizer: AIOptimizer,
    version: AIOptimizerVersion,
    evidence_ids: tuple[UUID, ...] = (),
    knowledge_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble read-only snapshots from existing plans, builds and rule evidence."""
    cutoff = _now()
    plan_rows = session.exec(
        select(AIDailyPlanRow)
        .where(
            col(AIDailyPlanRow.tenant_id) == context.tenant_id,
            col(AIDailyPlanRow.optimizer_id) == optimizer.id,
        )
        .order_by(
            col(AIDailyPlanRow.business_date).desc(), col(AIDailyPlanRow.position)
        )
        .limit(201)
    ).all()
    occurrences = session.exec(
        select(BuildOccurrence)
        .where(
            col(BuildOccurrence.tenant_id) == context.tenant_id,
            col(BuildOccurrence.optimizer_id) == optimizer.id,
            col(BuildOccurrence.bc_id) == version.bc_id,
            col(BuildOccurrence.created_at) <= cutoff,
        )
        .order_by(col(BuildOccurrence.scheduled_at).desc())
        .limit(201)
    ).all()
    occurrences = [
        row
        for row in occurrences
        if set(row.advertiser_ids).issubset(set(version.advertiser_ids))
    ]
    all_rule_evidence = session.exec(
        select(AutomationRuleEvidence)
        .where(
            col(AutomationRuleEvidence.tenant_id) == context.tenant_id,
            col(AutomationRuleEvidence.optimizer_id) == optimizer.id,
            col(AutomationRuleEvidence.bc_id) == version.bc_id,
            col(AutomationRuleEvidence.created_at) <= cutoff,
        )
        .order_by(col(AutomationRuleEvidence.created_at).desc())
        .limit(201)
    ).all()
    all_rule_evidence = [
        row
        for row in all_rule_evidence
        if set(row.advertiser_ids).issubset(set(version.advertiser_ids))
    ]
    rule_evidence = (
        [row for row in all_rule_evidence if row.id in set(evidence_ids)]
        if evidence_ids
        else all_rule_evidence
    )
    # Freeze IDs, revisions and digests from append-only reporting, business and
    # operation stores so later source updates never change this review input.
    from app.modules.ai_workbench.review_data import build_data_pack
    from app.modules.business.models import BusinessPublication, BusinessRun
    from app.modules.operation_logs.models import OperationCoverage, OperationExport

    data_pack = build_data_pack(
        session, context=context, version=version, cutoff=cutoff
    )
    from app.modules.ai_workbench.attribution import build_attribution_pack
    from app.modules.ai_workbench.execution_observations import freeze_rule_execution
    from app.modules.ai_workbench.review_orders import freeze_orders
    from app.modules.ai_workbench.rule_history import rule_snapshot_report

    attribution = build_attribution_pack(
        session,
        context=context,
        optimizer_id=optimizer.id,
        version=version,
        cutoff=cutoff,
        account_windows=data_pack["accounts"],
    )
    data_pack["mapping_gaps"] = [
        key
        for kind, key in (
            ("strategy", "strategy_version_to_report_object"),
            ("material_strategy", "material_production_strategy_to_report_object"),
        )
        if not any(item["kind"] == kind for item in attribution["rows"])
    ]
    data_pack["mapping_scope"] = (
        "Exact identities only; see attribution rows/conflicts/truncated and unmapped_policy, not full-account causal attribution"
    )
    material_orders = freeze_orders(
        session,
        context=context,
        optimizer_id=optimizer.id,
        version=version,
        cutoff=cutoff,
    )
    reporting_snapshots = data_pack["reporting"]
    business_scopes = session.exec(
        select(BusinessRun)
        .where(
            col(BusinessRun.tenant_id) == context.tenant_id,
            col(BusinessRun.bc_id) == version.bc_id,
            col(BusinessRun.created_at) <= cutoff,
            col(BusinessRun.finished_at) <= cutoff,
        )
        .order_by(col(BusinessRun.created_at).desc())
        .limit(50)
    ).all()
    # 经营运行可能覆盖整 BC；只接受冻结账户输入可证明属于当前范围的记录。
    allowed_accounts = set(version.advertiser_ids)
    business_scopes = [
        row
        for row in business_scopes
        if isinstance(row.inputs.get("accounts"), dict)
        and row.inputs["accounts"]
        and set(row.inputs["accounts"]).issubset(allowed_accounts)
    ]
    allowed_run_ids = {row.id for row in business_scopes}
    business_pubs = session.exec(
        select(BusinessPublication).where(
            col(BusinessPublication.tenant_id) == context.tenant_id,
            col(BusinessPublication.bc_id) == version.bc_id,
        )
    ).all()
    business_pubs = [row for row in business_pubs if row.run_id in allowed_run_ids]
    operation_coverage = session.exec(
        select(OperationCoverage)
        .join(
            OperationExport,
            (col(OperationExport.id) == col(OperationCoverage.export_id))
            & (col(OperationExport.tenant_id) == col(OperationCoverage.tenant_id))
            & (
                col(OperationExport.advertiser_id)
                == col(OperationCoverage.advertiser_id)
            ),
        )
        .where(
            col(OperationCoverage.tenant_id) == context.tenant_id,
            col(OperationCoverage.advertiser_id).in_(version.advertiser_ids),
            col(OperationExport.bc_id) == version.bc_id,
            col(OperationExport.observed_at) <= cutoff,
        )
    ).all()
    operation_exports = session.exec(
        select(OperationExport)
        .where(
            col(OperationExport.tenant_id) == context.tenant_id,
            col(OperationExport.bc_id) == version.bc_id,
            col(OperationExport.advertiser_id).in_(version.advertiser_ids),
            col(OperationExport.observed_at) <= cutoff,
        )
        .order_by(col(OperationExport.observed_at).desc())
        .limit(50)
    ).all()
    # These snapshots intentionally retain IDs/status/version only.  Full report
    # and business rows remain in their own append-only stores and are referenced.
    from app.modules.ai_workbench.models import (
        AIOptimizerRuleBinding,
        AIOptimizerStrategyBinding,
    )
    from app.modules.automation_rules.models import AutomationRule
    from app.modules.strategies.models import StrategyVersion

    binding = typing_cast(
        AIOptimizerStrategyBinding,
        session.get(AIOptimizerStrategyBinding, version.strategy_binding_id),
    )

    strategy = typing_cast(
        StrategyVersion, session.get(StrategyVersion, binding.strategy_version_id)
    )

    rule_ids = session.exec(
        select(col(AIOptimizerRuleBinding.rule_id)).where(
            col(AIOptimizerRuleBinding.tenant_id) == context.tenant_id,
            col(AIOptimizerRuleBinding.optimizer_id) == optimizer.id,
            col(AIOptimizerRuleBinding.active).is_(True),
        )
    ).all()
    bound_rules = (
        session.exec(
            select(AutomationRule).where(
                col(AutomationRule.tenant_id) == context.tenant_id,
                col(AutomationRule.id).in_(rule_ids),
            )
        ).all()
        if rule_ids
        else []
    )
    frozen = sanitize_public(
        {
            "data_pack": data_pack,
            "strategy": {
                "id": strategy.id,
                "strategy_id": strategy.strategy_id,
                "version": strategy.number,
                "config": strategy.config,
            },
            "rules": [
                {
                    "id": row.id,
                    "version": row.version,
                    "definition": row.definition,
                    "name": row.name,
                    "enabled": row.enabled,
                }
                for row in bound_rules
            ],
            "optimizer": {
                "id": str(optimizer.id),
                "version": version.number,
                "daily_spend": version.daily_spend,
                "target_roas": version.target_roas,
                "priority": version.priority,
                "timezone": version.timezone,
                "bc_id": version.bc_id,
                "advertiser_ids": list(version.advertiser_ids),
                "optimization_mode": version.optimization_mode,
                "authorization_limits": version.authorization_limits,
                "strategy_binding_id": str(version.strategy_binding_id),
            },
            "execution_truncated": {
                "plans": len(plan_rows) > 200,
                "builds": len(occurrences) > 200,
                "rules": len(all_rule_evidence) > 200,
            },
            "plans": [
                {
                    "id": str(row.id),
                    "business_date": str(row.business_date),
                    "title": row.title,
                    "position": row.position,
                    "scheduled_time": row.scheduled_time.isoformat(),
                    "order_materials": row.order_materials,
                    "dispatch_error": row.last_dispatch_error,
                }
                for row in plan_rows[:200]
            ],
            "build_occurrences": [
                {
                    "id": str(row.id),
                    "status": row.status,
                    "scheduled_at": row.scheduled_at,
                    "strategy_version_id": str(row.strategy_version_id),
                    "evidence": row.evidence,
                }
                for row in occurrences[:200]
            ],
            "rule_evidence": [
                {
                    "id": str(row.id),
                    "rule_id": str(row.rule_id) if row.rule_id else None,
                    "rule_version": row.rule_version,
                    "created_at": row.created_at,
                    "data_freshness": row.data_freshness,
                    "publication_versions": row.publication_versions,
                    "definition": row.definition,
                    "condition_thresholds": row.data_freshness.get(
                        "threshold_conditions", []
                    ),
                    "skipped_accounts": row.skipped_accounts,
                    "historical_inputs_digest": row.data_freshness.get(
                        "evaluation_inputs", {}
                    ).get("digest"),
                    "historical_replay": rule_snapshot_report(
                        row,
                        object_limit=20,
                        fact_cell_limit=1000,
                        report_byte_limit=20000,
                    ),
                    "execution_observations": freeze_rule_execution(
                        session,
                        context=context,
                        optimizer_id=optimizer.id,
                        version=version,
                        evidence=row,
                        cutoff=cutoff,
                    ),
                    "hits": row.hits,
                    "action_ledger_refs": row.action_ledger_refs,
                    "management_tasks": row.management_tasks,
                    "submission_receipt_summary": row.readback_status,
                }
                for row in rule_evidence[:200]
            ],
            # Reporting/business/operation stores are append-only sources owned by
            # their modules.  Freeze their identity/version references here instead
            # of re-querying live rows during a later DSH decision.
            "reporting": {
                "source": "reporting_store",
                "account_ids": list(version.advertiser_ids),
                "optimizer_version": version.number,
                "snapshots": [
                    # 正文只保存在 data_pack，索引不重复复制汇总/覆盖证据。
                    {
                        key: row[key]
                        for key in (
                            "id",
                            "status",
                            "dimension",
                            "advertiser_id",
                            "maturity_horizon",
                        )
                        if key in row
                    }
                    for row in reporting_snapshots
                ],
            },
            "business": {
                "source": "business_store",
                "bc_id": version.bc_id,
                "optimizer_version": version.number,
                "runs": [
                    {
                        "id": str(row.id),
                        "scope_revision": row.scope_revision,
                        "source_signature": row.source_signature,
                        "status": row.status,
                        "finished_at": row.finished_at,
                    }
                    for row in business_scopes
                ],
                "publications": [
                    {"quarter": row.quarter, "run_id": str(row.run_id)}
                    for row in business_pubs
                ],
            },
            "operation_logs": {
                "source": "operation_log_store",
                "account_ids": list(version.advertiser_ids),
                "optimizer_version": version.number,
                "coverage": [
                    {
                        "advertiser_id": row.advertiser_id,
                        "stat_date": row.stat_date,
                        "export_id": str(row.export_id),
                        "digest": row.digest,
                        "revision": row.revision,
                    }
                    for row in operation_coverage
                ],
                "exports": [
                    {
                        "id": str(row.id),
                        "digest": row.digest,
                        "parser_version": row.parser_version,
                        "observed_at": row.observed_at,
                    }
                    for row in operation_exports
                ],
            },
            "attribution": attribution,
            "material_orders": material_orders,
            "knowledge_refs": snapshot_knowledge_refs(knowledge_refs),
            "captured_at": cutoff,
        }
    )
    # 知识与外部参考同样按需读取，不在初始输入复制候选目录。
    frozen["knowledge"] = []
    frozen["knowledge_selection"] = {
        "mode": "TASK_SCOPED_SEARCH_AND_READ",
        "max_entries": 20,
        "max_body_chars": 32000,
        "catalog_exhaustive": False,
        "policy": "search latest confirmed versions at captured_at; expired/unverified are context only",
    }
    from app.modules.ai_workbench.capabilities import catalog
    from app.modules.ai_workbench.learning import freeze_history

    frozen["history"] = freeze_history(
        session,
        context=context,
        optimizer_id=optimizer.id,
        version=version,
        data_pack=data_pack,
    )
    frozen["capabilities"] = catalog(version)
    requested = {str(item) for item in evidence_ids}
    if requested and not requested.issubset(frozen_evidence_ids(frozen)):
        raise DomainError(
            "ai_evidence_not_found", "requested evidence is outside optimizer scope"
        )
    # 正文在创建任务时拆到私有存储；不能先按模型输入预算截断冻结源。
    return frozen


def create_review_task(
    session: Session,
    *,
    context: TenantContext,
    optimizer_id: UUID,
    request_id: UUID,
    evidence_ids: tuple[UUID, ...] = (),
    knowledge_refs: list[dict[str, Any]] | None = None,
    authorization_ttl_seconds: int = 300,
    dsh_task_id: str | None = None,
    request_digest: str | None = None,
) -> DSHReviewTask:
    authorize(session, context, write=True)
    if not 30 <= authorization_ttl_seconds <= 900:
        raise DomainError(
            "ai_dsh_authorization_invalid", "authorization TTL must be 30..900 seconds"
        )
    optimizer, version = load_optimizer(session, context, optimizer_id)
    existing = session.exec(
        select(DSHReviewTask).where(
            col(DSHReviewTask.tenant_id) == context.tenant_id,
            col(DSHReviewTask.request_id) == request_id,
        )
    ).one_or_none()
    if existing is not None:
        if existing.optimizer_id != optimizer.id:
            raise DomainError(
                "ai_review_scope_mismatch",
                "request is already used by another optimizer",
            )
        if request_digest is not None and existing.request_digest != request_digest:
            raise DomainError("idempotency_conflict", "review request payload differs")
        return existing
    request_digest = (
        request_digest
        or hashlib.sha256(
            json.dumps(
                {
                    "evidence_ids": [str(item) for item in evidence_ids],
                    "knowledge_refs": knowledge_refs or [],
                    "authorization_ttl_seconds": authorization_ttl_seconds,
                    "dsh_task_id": dsh_task_id,
                },
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()
    )
    frozen = frozen_evidence(
        session,
        context=context,
        optimizer=optimizer,
        version=version,
        evidence_ids=evidence_ids,
        knowledge_refs=knowledge_refs,
    )
    # A review is a durable reference to its decision snapshot.  Extend the
    # source row's retention fence as a convenience for operators that run
    # short-term cleanup before the review itself is archived; cleanup also
    # checks the frozen JSON references and therefore remains safe on retries.
    referenced = frozen_evidence_ids(frozen)
    if referenced:
        retention_until = _now() + timedelta(days=365)
        evidence_rows = session.exec(
            select(AutomationRuleEvidence).where(
                col(AutomationRuleEvidence.tenant_id) == context.tenant_id,
                col(AutomationRuleEvidence.optimizer_id) == optimizer.id,
                col(AutomationRuleEvidence.id).in_(
                    [UUID(item) for item in referenced if _is_uuid(item)]
                ),
            )
        ).all()
        for row in evidence_rows:
            if row.retention_until < retention_until:
                row.retention_until = retention_until
    from app.modules.ai_workbench.review_reading import split_evidence

    initial, store = split_evidence(frozen)
    task = DSHReviewTask(
        tenant_id=context.tenant_id,
        optimizer_id=optimizer.id,
        request_id=request_id,
        request_digest=request_digest,
        dsh_task_id=dsh_task_id or f"dsh:{context.tenant_id}:{request_id}",
        authorization_expires_at=_now() + timedelta(seconds=authorization_ttl_seconds),
        fence=uuid4().hex,
        optimizer_version=version.number,
        evidence=initial,
        evidence_store=store,
        created_by=context.actor_id,
    )
    session.add(task)
    session.flush()
    return task


def get_review_task(
    session: Session, *, context: TenantContext, optimizer_id: UUID, task_id: UUID
) -> DSHReviewTask:
    authorize(session, context)
    row = session.exec(
        select(DSHReviewTask).where(
            col(DSHReviewTask.tenant_id) == context.tenant_id,
            col(DSHReviewTask.optimizer_id) == optimizer_id,
            col(DSHReviewTask.id) == task_id,
        )
    ).one_or_none()
    if row is None:
        raise DomainError("ai_review_not_found", "ai_review_not_found")
    return row
