"""Tenant/optimizer-scoped DSH proposals and publication decisions."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field as SQLField
from sqlmodel import Session, SQLModel, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.ai_workbench.knowledge_refs import sanitize_public
from app.modules.ai_workbench.models import (
    AIOptimizerRuleBinding,
    AIOptimizerStrategyBinding,
    AIOptimizerVersion,
)
from app.modules.ai_workbench.reviews import (
    DSHReviewTask,
    authorize,
    frozen_evidence_ids,
    load_optimizer,
)
from app.modules.automation_rules.models import AutomationRule
from app.modules.automation_rules.schemas import RuleDefinition, RuleUpdate
from app.modules.strategies.models import Strategy, StrategyVersion
from app.modules.strategies.schemas import StrategyConfig


class ProposalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["strategy", "rule"]
    operation: Literal["create", "update"] = "update"
    target_id: UUID | None = None
    after: dict[str, Any] = Field(default_factory=dict)
    rationale: str = Field(default="", max_length=4000)
    problem: str = Field(default="", max_length=2000)
    expected_effect: str = Field(default="", max_length=2000)
    validation: str = Field(default="", max_length=2000)
    evidence_ids: tuple[UUID, ...] = ()


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    evidence_ids: tuple[UUID, ...] = ()
    knowledge_refs: list[dict[str, Any]] = Field(default_factory=list)
    proposals: tuple[ProposalInput, ...] = ()
    authorization_ttl_seconds: int = Field(default=300, ge=30, le=900)
    dsh_task_id: str | None = Field(default=None, max_length=160)


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    reason: str | None = None


class AIProposal(SQLModel, table=True):
    __tablename__ = "ai_optimizer_proposal"
    __table_args__ = (
        Index(
            "ix_ai_proposal_optimizer_status",
            "tenant_id",
            "optimizer_id",
            "status",
            "created_at",
        ),
        UniqueConstraint("tenant_id", "request_id", name="uq_ai_proposal_request"),
        ForeignKeyConstraint(
            ["tenant_id", "optimizer_id"],
            ["ai_optimizer.tenant_id", "ai_optimizer.id"],
            name="fk_ai_proposal_optimizer",
        ),
        CheckConstraint("kind IN ('strategy','rule')", name="ck_ai_proposal_kind"),
        CheckConstraint(
            "operation IN ('create','update')", name="ck_ai_proposal_operation"
        ),
        CheckConstraint(
            "status IN ('PENDING','ACCEPTED','REJECTED','STALE')",
            name="ck_ai_proposal_status",
        ),
    )
    id: UUID = SQLField(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = SQLField(foreign_key="tenant.id")
    optimizer_id: UUID
    review_task_id: UUID = SQLField(foreign_key="ai_dsh_review_task.id")
    kind: str = SQLField(max_length=16)
    operation: str = SQLField(default="update", max_length=16)
    target_id: UUID | None = None
    status: str = SQLField(default="PENDING", max_length=16)
    before: dict[str, Any] = SQLField(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    after: dict[str, Any] = SQLField(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    evidence: dict[str, Any] = SQLField(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    authorization: dict[str, Any] = SQLField(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    base_optimizer_version: int
    base_strategy_version_id: UUID | None = None
    base_rule_version: int | None = None
    base_rule_id: UUID | None = None
    published_version_id: UUID | None = None
    decision_by: UUID | None = SQLField(default=None, foreign_key="user.id")
    decision_reason: str | None = SQLField(default=None, max_length=1000)
    decision_request_id: UUID | None = None
    decision_request_digest: str | None = SQLField(default=None, max_length=64)
    decision_at: datetime | None = SQLField(
        default=None,
        sa_column=Column(
            DateTime(timezone=True), index=False, unique=False, nullable=True
        ),
    )
    request_id: UUID
    created_by: UUID = SQLField(foreign_key="user.id")
    created_at: datetime = SQLField(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(
            DateTime(timezone=True),
            index=False,
            unique=False,
            nullable=False,
            default=lambda: datetime.now(UTC),
        ),
    )


def _public(row: AIProposal) -> dict[str, Any]:
    names = (
        "id",
        "tenant_id",
        "optimizer_id",
        "review_task_id",
        "kind",
        "operation",
        "target_id",
        "status",
        "before",
        "after",
        "evidence",
        "authorization",
        "base_optimizer_version",
        "base_strategy_version_id",
        "base_rule_id",
        "base_rule_version",
        "published_version_id",
        "decision_by",
        "decision_reason",
        "decision_at",
        "request_id",
        "created_at",
    )
    return {name: getattr(row, name) for name in names}


def list_proposals(
    session: Session,
    *,
    context: TenantContext,
    optimizer_id: UUID,
    status: str | None = None,
) -> list[dict[str, Any]]:
    authorize(session, context)
    query = (
        select(AIProposal)
        .where(
            col(AIProposal.tenant_id) == context.tenant_id,
            col(AIProposal.optimizer_id) == optimizer_id,
        )
        .order_by(col(AIProposal.created_at).desc())
    )
    if status is not None:
        query = query.where(col(AIProposal.status) == status)
    return [_public(row) for row in session.exec(query).all()]


def _within_authorization(
    *,
    version: AIOptimizerVersion,
    kind: str,
    before: dict[str, Any],  # noqa: ARG001 — 保留发布预检调用合同
    after: dict[str, Any],
) -> tuple[bool, str | None]:
    # 优化模式控制是否需要人工确认；历史细粒度授权不再参与判定。
    if kind == "strategy":
        return True, None
    else:
        if not isinstance(version.bc_id, str) or not isinstance(
            version.advertiser_ids, (list, tuple, set)
        ):
            raise DomainError("ai_invalid_rule_scope", "optimizer scope is malformed")
        if not all(isinstance(item, str) for item in version.advertiser_ids):
            raise DomainError(
                "ai_invalid_rule_scope", "optimizer advertiser scope is malformed"
            )
        if not isinstance(after, Mapping):
            raise DomainError("ai_invalid_rule_scope", "rule scope must be an object")
        definition = after.get("definition", after)
        if not isinstance(definition, Mapping):
            raise DomainError(
                "ai_invalid_rule_scope", "rule definition scope must be an object"
            )
        accounts = definition.get("accounts")
        # Older proposal payloads placed the account IDs directly under the
        # definition (or omitted ``mode`` inside ``accounts``).  Normalize
        # those unambiguous, bounded shapes before applying the strict scope
        # checks; an empty/broad shape still fails closed below.
        if accounts is None:
            legacy = {
                key: definition[key]
                for key in ("bc_ids", "advertiser_ids")
                if key in definition
            }
            if "advertiser_ids" in legacy:
                legacy["mode"] = "ACCOUNTS"
            elif "bc_ids" in legacy:
                legacy["mode"] = "BC"
            accounts = legacy
        elif isinstance(accounts, Mapping) and "mode" not in accounts:
            accounts = dict(accounts)
            if "advertiser_ids" in accounts:
                accounts["mode"] = "ACCOUNTS"
            elif "bc_ids" in accounts:
                accounts["mode"] = "BC"
        if not isinstance(accounts, Mapping):
            raise DomainError(
                "ai_invalid_rule_scope", "nested rule accounts scope must be an object"
            )
        mode = accounts.get("mode")
        if mode not in {"ALL", "BC", "ACCOUNTS"}:
            raise DomainError("ai_invalid_rule_scope", "account scope mode is required")
        advertiser_ids = accounts.get("advertiser_ids")
        bc_ids = accounts.get("bc_ids")
        if advertiser_ids is not None and (
            not isinstance(advertiser_ids, (list, tuple, set))
            or not all(isinstance(item, str) for item in advertiser_ids)
        ):
            raise DomainError(
                "ai_invalid_rule_scope", "advertiser scope must be a list"
            )
        if bc_ids is not None and (
            not isinstance(bc_ids, (list, tuple, set))
            or not all(isinstance(item, str) for item in bc_ids)
        ):
            raise DomainError(
                "ai_invalid_rule_scope", "business center scope must be a list"
            )
        advertiser_scope = set(advertiser_ids or ())
        bc_scope = set(bc_ids or ())
        if mode == "ALL":
            # ALL ignores account IDs at execution time and would therefore
            # grant an AUTO proposal a broader scope than the optimizer.
            return False, "account_scope_not_authorized"
        if mode == "BC" and not bc_scope:
            raise DomainError("ai_invalid_rule_scope", "bc scope requires bc_ids")
        if mode == "ACCOUNTS" and not advertiser_scope:
            raise DomainError(
                "ai_invalid_rule_scope", "account scope requires advertiser_ids"
            )
        if mode == "BC" and advertiser_scope:
            raise DomainError(
                "ai_invalid_rule_scope", "bc scope cannot include advertiser_ids"
            )
        if mode == "ACCOUNTS" and bc_scope:
            raise DomainError(
                "ai_invalid_rule_scope", "account scope cannot include bc_ids"
            )
        if advertiser_scope and not advertiser_scope.issubset(
            set(version.advertiser_ids)
        ):
            return False, "advertiser_scope_out_of_scope"
        if mode == "BC" and bc_scope != {version.bc_id}:
            return False, "bc_scope_out_of_scope"
        if definition.get("bc_id") is not None and not isinstance(
            definition["bc_id"], str
        ):
            raise DomainError(
                "ai_invalid_rule_scope", "business center scope must be a string"
            )
        if definition.get("bc_id") is not None and definition["bc_id"] != version.bc_id:
            return False, "bc_scope_out_of_scope"
    return True, None


def _strategy_baseline(
    session: Session,
    *,
    tenant_id: UUID,
    optimizer_id: UUID,
    version: AIOptimizerVersion,
) -> StrategyVersion:
    binding = session.exec(
        select(AIOptimizerStrategyBinding).where(
            col(AIOptimizerStrategyBinding.tenant_id) == tenant_id,
            col(AIOptimizerStrategyBinding.optimizer_id) == optimizer_id,
            col(AIOptimizerStrategyBinding.id) == version.strategy_binding_id,
            col(AIOptimizerStrategyBinding.active).is_(True),
        )
    ).one_or_none()
    if binding is None:
        raise DomainError(
            "ai_strategy_binding_required", "ai_strategy_binding_required"
        )
    row = session.exec(
        select(StrategyVersion).where(
            col(StrategyVersion.tenant_id) == tenant_id,
            col(StrategyVersion.id) == binding.strategy_version_id,
        )
    ).one_or_none()
    if row is None:
        raise DomainError(
            "ai_strategy_binding_required", "ai_strategy_binding_required"
        )
    return row


def create_proposal(
    session: Session,
    *,
    context: TenantContext,
    optimizer_id: UUID,
    review_task: DSHReviewTask,
    change: ProposalInput,
    request_id: UUID | None = None,
) -> AIProposal:
    authorize(session, context, write=True)
    if (
        review_task.tenant_id != context.tenant_id
        or review_task.optimizer_id != optimizer_id
    ):
        raise DomainError(
            "ai_review_scope_mismatch", "review task is outside optimizer scope"
        )
    optimizer, version = load_optimizer(session, context, optimizer_id)
    if review_task.optimizer_version != version.number:
        raise DomainError("ai_proposal_stale", "review task optimizer version is stale")
    frozen_ids = frozen_evidence_ids(review_task.evidence)
    if any(str(item) not in frozen_ids for item in change.evidence_ids):
        raise DomainError(
            "ai_evidence_not_found",
            "proposal evidence is outside the frozen review task",
        )
    request_id = request_id or uuid4()
    existing = session.exec(
        select(AIProposal).where(
            col(AIProposal.tenant_id) == context.tenant_id,
            col(AIProposal.request_id) == request_id,
        )
    ).one_or_none()
    if existing is not None:
        if existing.optimizer_id != optimizer_id:
            raise DomainError(
                "ai_proposal_scope_mismatch",
                "request is already used by another optimizer",
            )
        return existing
    try:
        if change.operation == "create" and change.target_id is not None:
            raise DomainError("ai_proposal_invalid", "新增提案不能指定已有目标")
        if change.kind == "strategy":
            StrategyConfig.model_validate(change.after.get("config", change.after))
        else:
            RuleDefinition.model_validate(change.after.get("definition", change.after))
    except ValueError as exc:
        raise DomainError("ai_proposal_invalid", "提案配置不满足领域 Schema") from exc
    if change.kind == "strategy":
        if change.operation == "create":
            before = {}
            baseline = None
        else:
            baseline = _strategy_baseline(
                session,
                tenant_id=context.tenant_id,
                optimizer_id=optimizer_id,
                version=version,
            )
            before = {
                "version_id": str(baseline.id),
                "version": baseline.number,
                "config": baseline.config,
            }
        base_strategy_version_id, base_rule_id, base_rule_version = (
            baseline.id if baseline else None,
            None,
            None,
        )
    else:
        if change.operation == "create":
            accounts = change.after.get("definition", change.after).get("accounts", {})
            if accounts.get("mode") != "ACCOUNTS":
                raise DomainError(
                    "ai_invalid_rule_scope", "AI 新建规则必须显式限定管理账户"
                )
            before = {}
            base_strategy_version_id, base_rule_id, base_rule_version = (
                None,
                None,
                None,
            )
            allowed, reason = _within_authorization(
                version=version, kind=change.kind, before=before, after=change.after
            )
            # 新规则的账户范围仍在 RuleDefinition 中强制校验，发布时绑定到当前投手。
            row = AIProposal(
                tenant_id=context.tenant_id,
                optimizer_id=optimizer.id,
                review_task_id=review_task.id,
                kind=change.kind,
                operation=change.operation,
                target_id=None,
                status="PENDING",
                before={},
                after=sanitize_public(change.after),
                evidence={
                    "evidence_ids": [str(item) for item in change.evidence_ids],
                    "review_task_id": str(review_task.id),
                },
                authorization={
                    "allowed": allowed,
                    "reason": reason,
                    "mode": version.optimization_mode,
                },
                base_optimizer_version=version.number,
                request_id=request_id,
                created_by=context.actor_id,
            )
            session.add(row)
            session.flush()
            return row
        if change.target_id is None:
            raise DomainError("ai_proposal_target_required", "rule target is required")
        binding = session.exec(
            select(AIOptimizerRuleBinding).where(
                col(AIOptimizerRuleBinding.tenant_id) == context.tenant_id,
                col(AIOptimizerRuleBinding.optimizer_id) == optimizer_id,
                col(AIOptimizerRuleBinding.rule_id) == change.target_id,
                col(AIOptimizerRuleBinding.active).is_(True),
            )
        ).one_or_none()
        rule = session.exec(
            select(AutomationRule).where(
                col(AutomationRule.tenant_id) == context.tenant_id,
                col(AutomationRule.id) == change.target_id,
            )
        ).one_or_none()
        if binding is None or rule is None:
            raise DomainError(
                "ai_rule_not_bound", "rule is not assigned to this optimizer"
            )
        frozen_rule = next(
            (
                r
                for r in review_task.evidence.get("rules", [])
                if r.get("id") == str(rule.id)
            ),
            None,
        )
        if (
            frozen_rule is None
            or frozen_rule.get("version") != rule.version
            or any(
                frozen_rule.get(key) != getattr(rule, key)
                for key in ("definition", "name", "enabled")
            )
        ):
            raise DomainError("ai_proposal_stale", "规则冻结基线已改变，请重新复盘")
        before = {
            "rule_id": str(rule.id),
            "version": frozen_rule["version"],
            "name": frozen_rule["name"],
            "enabled": frozen_rule["enabled"],
            "definition": frozen_rule["definition"],
        }
        base_strategy_version_id, base_rule_id, base_rule_version = (
            None,
            rule.id,
            rule.version,
        )
    allowed, reason = _within_authorization(
        version=version, kind=change.kind, before=before, after=change.after
    )
    row = AIProposal(
        tenant_id=context.tenant_id,
        optimizer_id=optimizer.id,
        review_task_id=review_task.id,
        kind=change.kind,
        operation=change.operation,
        target_id=change.target_id,
        status="PENDING",
        before=sanitize_public(before),
        after=sanitize_public(change.after),
        evidence={
            "evidence_ids": [str(item) for item in change.evidence_ids],
            "review_task_id": str(review_task.id),
            "captured_optimizer_version": version.number,
            "rationale": change.rationale,
            "problem": change.problem,
            "expected_effect": change.expected_effect,
            "validation": change.validation,
        },
        authorization={
            "allowed": allowed,
            "reason": reason,
            "mode": version.optimization_mode,
            "checked_at": datetime.now(UTC).isoformat(),
        },
        base_optimizer_version=version.number,
        base_strategy_version_id=base_strategy_version_id,
        base_rule_version=base_rule_version,
        base_rule_id=base_rule_id,
        request_id=request_id,
        created_by=context.actor_id,
    )
    session.add(row)
    session.flush()
    return row


def _mark(
    row: AIProposal, *, context: TenantContext, status: str, reason: str | None = None
) -> AIProposal:
    row.status, row.decision_by, row.decision_reason, row.decision_at = (
        status,
        context.actor_id,
        reason,
        datetime.now(UTC),
    )
    return row


def _publish_strategy(
    session: Session, *, context: TenantContext, proposal: AIProposal
) -> UUID:
    from app.modules.ai_workbench import service as optimizer_service
    from app.modules.ai_workbench.schemas import OptimizerUpdate
    from app.modules.strategies import service as strategy_service

    optimizer, current = load_optimizer(session, context, proposal.optimizer_id)
    if proposal.operation == "create":
        try:
            config = StrategyConfig.model_validate(
                proposal.after.get("config", proposal.after)
            )
        except Exception as exc:
            raise DomainError(
                "ai_proposal_invalid", "strategy proposal is invalid"
            ) from exc
        created = strategy_service.create_strategy(
            session,
            context=context,
            name=proposal.after.get("name", "AI 投手策略"),
            config=config,
            request_id=uuid4(),
        )
        latest = session.exec(
            select(StrategyVersion)
            .where(
                col(StrategyVersion.tenant_id) == context.tenant_id,
                col(StrategyVersion.strategy_id) == created,
            )
            .order_by(col(StrategyVersion.number).desc())
        ).first()
        if latest is None:
            raise DomainError("ai_proposal_invalid", "新策略版本创建失败")
        result = optimizer_service.update_optimizer(
            session,
            context=context,
            optimizer_id=optimizer.id,
            body=OptimizerUpdate(strategy_version_id=latest.id, request_id=uuid4()),
        )
        return result.strategy_version_id
    baseline = _strategy_baseline(
        session, tenant_id=context.tenant_id, optimizer_id=optimizer.id, version=current
    )
    if baseline.id != proposal.base_strategy_version_id:
        raise DomainError("ai_proposal_stale", "strategy base version is stale")
    strategy = session.exec(
        select(Strategy)
        .where(
            col(Strategy.tenant_id) == context.tenant_id,
            col(Strategy.id) == baseline.strategy_id,
        )
        .with_for_update()
    ).one()
    refs = session.exec(
        select(AIOptimizerStrategyBinding).where(
            col(AIOptimizerStrategyBinding.tenant_id) == context.tenant_id,
            col(AIOptimizerStrategyBinding.strategy_version_id) == baseline.id,
            col(AIOptimizerStrategyBinding.active).is_(True),
        )
    ).all()
    if any(ref.optimizer_id != optimizer.id for ref in refs):
        raise DomainError(
            "ai_strategy_shared", "shared strategy must be copied and owned first"
        )
    try:
        config = StrategyConfig.model_validate(
            proposal.after.get("config", proposal.after)
        )
    except Exception as exc:
        raise DomainError(
            "ai_proposal_invalid", "strategy proposal is invalid"
        ) from exc
    new_version_id = strategy_service.append_version(
        session,
        context=context,
        strategy_id=strategy.id,
        config=config,
        expected_version=baseline.number,
        request_id=uuid4(),
    )
    # Optimizer versions are immutable.  Use the existing append-version
    # service to publish a new optimizer snapshot pointing at the new strategy
    # version instead of mutating the snapshot captured by the review task.
    from app.modules.ai_workbench.schemas import OptimizerUpdate

    result = optimizer_service.update_optimizer(
        session,
        context=context,
        optimizer_id=optimizer.id,
        body=OptimizerUpdate(strategy_version_id=new_version_id, request_id=uuid4()),
    )
    return result.strategy_version_id


def _publish_rule(
    session: Session, *, context: TenantContext, proposal: AIProposal
) -> UUID:
    from app.modules.ai_workbench import service as optimizer_service
    from app.modules.automation_rules import service as rule_service

    _, _ = load_optimizer(session, context, proposal.optimizer_id)
    if proposal.operation == "create":
        from app.modules.automation_rules.schemas import RuleCreate

        try:
            body = RuleCreate(
                name=proposal.after.get("name", "AI 投手规则"),
                enabled=proposal.after.get("enabled", True),
                definition=RuleDefinition.model_validate(
                    proposal.after.get("definition", proposal.after)
                ),
            )
        except Exception as exc:
            raise DomainError(
                "ai_proposal_invalid", "rule proposal is invalid"
            ) from exc
        created = rule_service.create_rule(session, context=context, body=body)
        from app.modules.ai_workbench.schemas import OptimizerUpdate

        ids = tuple(
            session.exec(
                select(col(AIOptimizerRuleBinding.rule_id)).where(
                    col(AIOptimizerRuleBinding.tenant_id) == context.tenant_id,
                    col(AIOptimizerRuleBinding.optimizer_id) == proposal.optimizer_id,
                    col(AIOptimizerRuleBinding.active).is_(True),
                )
            ).all()
        )
        optimizer_service.update_optimizer(
            session,
            context=context,
            optimizer_id=proposal.optimizer_id,
            body=OptimizerUpdate(rule_ids=ids + (created.id,), request_id=uuid4()),
        )
        return created.id
    rule_id = proposal.base_rule_id or proposal.target_id
    if rule_id is None:
        raise DomainError("ai_proposal_target_required", "rule target is required")
    rule = session.exec(
        select(AutomationRule)
        .where(
            col(AutomationRule.tenant_id) == context.tenant_id,
            col(AutomationRule.id) == rule_id,
        )
        .with_for_update()
    ).one_or_none()
    if rule is None or rule.version != proposal.base_rule_version:
        raise DomainError("ai_proposal_stale", "rule base version is stale")
    other = session.exec(
        select(AIOptimizerRuleBinding).where(
            col(AIOptimizerRuleBinding.tenant_id) == context.tenant_id,
            col(AIOptimizerRuleBinding.rule_id) == rule_id,
            col(AIOptimizerRuleBinding.optimizer_id) != proposal.optimizer_id,
            col(AIOptimizerRuleBinding.active).is_(True),
        )
    ).first()
    if other is not None:
        raise DomainError(
            "ai_rule_shared", "shared rule must be copied and owned first"
        )
    try:
        definition = RuleDefinition.model_validate(
            proposal.after.get("definition", proposal.after)
        )
        body = RuleUpdate(
            name=proposal.after.get("name", rule.name),
            enabled=proposal.after.get("enabled", rule.enabled),
            definition=definition,
            expected_version=rule.version,
        )
    except Exception as exc:
        raise DomainError("ai_proposal_invalid", "rule proposal is invalid") from exc
    rule_service.update_rule(session, context=context, rule_id=rule.id, body=body)
    return rule.id


def _decision_digest(action: str, request_id: UUID, reason: str | None) -> str:
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps(
            {"action": action, "request_id": str(request_id), "reason": reason},
            sort_keys=True,
        ).encode()
    ).hexdigest()


def accept_proposal(
    session: Session,
    *,
    context: TenantContext,
    optimizer_id: UUID,
    proposal_id: UUID,
    request_id: UUID | None = None,
    automatic: bool = False,
) -> AIProposal:
    authorize(session, context, write=True)
    row = session.exec(
        select(AIProposal)
        .where(
            col(AIProposal.tenant_id) == context.tenant_id,
            col(AIProposal.optimizer_id) == optimizer_id,
            col(AIProposal.id) == proposal_id,
        )
        .with_for_update()
    ).one_or_none()
    if row is None:
        raise DomainError("ai_proposal_not_found", "ai_proposal_not_found")
    request_id = request_id or uuid4()
    digest = _decision_digest("accept", request_id, None)
    if row.status in {"ACCEPTED", "REJECTED", "STALE"}:
        if row.decision_request_digest != digest:
            raise DomainError(
                "ai_decision_conflict",
                "proposal decision retry conflicts with the original request",
            )
        return row
    row.decision_request_id = request_id
    row.decision_request_digest = digest
    _, current = load_optimizer(session, context, optimizer_id)
    if current.number != row.base_optimizer_version:
        return _mark(
            row,
            context=context,
            status="STALE",
            reason="optimizer_base_version_changed",
        )
    # 自动执行受预授权约束；人工采纳本身是这一条具体变更的授权，仍重验账户与能力边界。
    if automatic:
        if (
            current.optimization_mode != "AUTO"
            or row.authorization.get("allowed") is not True
        ):
            raise DomainError("ai_publication_not_authorized", "自动发布超出授权")
    else:
        manual = current.model_copy(
            update={
                "optimization_mode": "MANUAL",
                "authorization_limits": {"strategy": True, "rule": True},
            }
        )
        allowed, reason = _within_authorization(
            version=manual, kind=row.kind, before=row.before, after=row.after
        )
        if not allowed:
            raise DomainError("ai_publication_not_authorized", reason or "采纳范围无效")
        row.authorization = {**row.authorization, "explicit_human_approval": True}
    try:
        published = (
            _publish_strategy(session, context=context, proposal=row)
            if row.kind == "strategy"
            else _publish_rule(session, context=context, proposal=row)
        )
    except DomainError as exc:
        if exc.code in {
            "ai_proposal_stale",
            "version_conflict",
            "automation_rule_version_conflict",
        }:
            return _mark(row, context=context, status="STALE", reason=exc.code)
        raise
    row.published_version_id = published
    row.authorization = {
        **row.authorization,
        "decision_mode": "AUTO" if automatic else "MANUAL",
        "executor": "DSH_SERVICE" if automatic else "USER",
        "authorization_actor_id": str(context.actor_id),
    }
    _mark(row, context=context, status="ACCEPTED")
    from app.modules.tenants.models import AuditEvent

    session.add(
        AuditEvent(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            action="ai_proposal.accept",
            target_id=str(row.id),
            details={
                "optimizer_id": str(optimizer_id),
                "review_task_id": str(row.review_task_id),
                "kind": row.kind,
                "mode": "AUTO" if automatic else "MANUAL",
                "executor": "DSH_SERVICE" if automatic else "USER",
            },
        )
    )
    session.flush()
    return row


def reject_proposal(
    session: Session,
    *,
    context: TenantContext,
    optimizer_id: UUID,
    proposal_id: UUID,
    reason: str | None = None,
    request_id: UUID | None = None,
) -> AIProposal:
    authorize(session, context, write=True)
    row = session.exec(
        select(AIProposal)
        .where(
            col(AIProposal.tenant_id) == context.tenant_id,
            col(AIProposal.optimizer_id) == optimizer_id,
            col(AIProposal.id) == proposal_id,
        )
        .with_for_update()
    ).one_or_none()
    if row is None:
        raise DomainError("ai_proposal_not_found", "ai_proposal_not_found")
    request_id = request_id or uuid4()
    digest = _decision_digest("reject", request_id, reason)
    if row.status in {"ACCEPTED", "REJECTED", "STALE"}:
        if row.decision_request_digest != digest:
            raise DomainError(
                "ai_decision_conflict",
                "proposal decision retry conflicts with the original request",
            )
        return row
    row.decision_request_id = request_id
    row.decision_request_digest = digest
    _mark(row, context=context, status="REJECTED", reason=reason)
    from app.modules.tenants.models import AuditEvent

    session.add(
        AuditEvent(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            action="ai_proposal.reject",
            target_id=str(row.id),
            details={
                "optimizer_id": str(optimizer_id),
                "review_task_id": str(row.review_task_id),
            },
        )
    )
    session.flush()
    return row


def publish_auto_proposals(
    session: Session,
    *,
    context: TenantContext,
    optimizer_id: UUID,
    review_task_id: UUID,
) -> list[AIProposal]:
    _, version = load_optimizer(session, context, optimizer_id)
    if version.optimization_mode != "AUTO":
        return []
    rows = session.exec(
        select(AIProposal)
        .where(
            col(AIProposal.tenant_id) == context.tenant_id,
            col(AIProposal.optimizer_id) == optimizer_id,
            col(AIProposal.review_task_id) == review_task_id,
            col(AIProposal.status) == "PENDING",
        )
        .order_by(col(AIProposal.kind), col(AIProposal.created_at))
    ).all()
    return [
        accept_proposal(
            session,
            context=context,
            optimizer_id=optimizer_id,
            proposal_id=row.id,
            request_id=row.request_id,
            automatic=True,
        )
        for row in rows
        if row.authorization.get("allowed") is True
    ]
