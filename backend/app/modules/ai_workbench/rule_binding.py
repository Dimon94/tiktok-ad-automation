"""Optimizer-scoped bindings for the existing automatic-rule engine.

The optimizer owns the account boundary; the rule still owns its definition.
This module only resolves/intersects those two scopes and fences the immutable
optimizer/rule versions immediately before a management submission.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlmodel import Session, col, select

from app.core.errors import DomainError
from app.modules.ai_workbench.models import (
    AIOptimizer,
    AIOptimizerRuleBinding,
    AIOptimizerVersion,
)
from app.modules.automation_rules.models import AutomationRule
from app.modules.automation_rules.schemas import RuleDefinition


@dataclass(frozen=True)
class RuleBindingScope:
    tenant_id: UUID
    optimizer_id: UUID
    rule_id: UUID
    bc_id: str
    advertiser_ids: tuple[str, ...]
    authorization_revision: int
    authorization_revisions: tuple[tuple[str, int], ...] = ()
    optimizer_version: int = 0
    rule_version: int = 0
    binding_id: UUID | None = None


def _rule_accounts(definition: RuleDefinition, *, bc_id: str) -> set[str] | None:
    scope = definition.accounts
    if scope.mode == "ACCOUNTS":
        return set(scope.advertiser_ids)
    if scope.mode == "BC":
        return set() if bc_id not in scope.bc_ids else None
    return None


def _current_revision(
    session: Session, *, tenant_id: UUID, bc_id: str, advertiser_ids: set[str]
) -> tuple[set[str], int, tuple[tuple[str, int], ...]]:
    # Keep account access imports lazy: account routing imports tenant models,
    # which in turn register the full model graph during application startup.
    from app.modules.accounts.access import usable_grants
    from app.modules.accounts.models import BCAccountAccess, TikTokConnection

    if not advertiser_ids:
        return set(), 0, ()
    grants = session.exec(
        usable_grants(tenant_id=tenant_id, bc_id=bc_id, action="read").where(
            col(BCAccountAccess.advertiser_id).in_(advertiser_ids)
        )
    ).all()
    found = {grant.advertiser_id for grant in grants}
    if found != advertiser_ids:
        raise DomainError(
            "ai_invalid_rule_scope", "optimizer account authorization is stale"
        )
    revision_rows = session.exec(
        select(
            col(TikTokConnection.id), col(TikTokConnection.authorization_revision)
        ).where(
            col(TikTokConnection.tenant_id) == tenant_id,
            col(TikTokConnection.id).in_([grant.connection_id for grant in grants]),
        )
    ).all()
    by_connection = {
        connection_id: int(revision) for connection_id, revision in revision_rows
    }
    if any(grant.connection_id not in by_connection for grant in grants):
        raise DomainError(
            "ai_invalid_rule_scope", "authorization revision is unavailable"
        )
    revisions = tuple(
        sorted(
            (
                f"{advertiser_id}:{grant.connection_id}",
                by_connection[grant.connection_id],
            )
            for grant in grants
            for advertiser_id in [grant.advertiser_id]
        )
    )
    return found, max((revision for _, revision in revisions), default=0), revisions


def resolve_rule_binding(
    session: Session,
    *,
    tenant_id: UUID,
    optimizer_id: UUID,
    rule_id: UUID,
    require_active: bool = True,
) -> RuleBindingScope:
    optimizer = session.exec(
        select(AIOptimizer).where(
            col(AIOptimizer.tenant_id) == tenant_id, col(AIOptimizer.id) == optimizer_id
        )
    ).one_or_none()
    if optimizer is None:
        raise DomainError("ai_optimizer_not_found", "ai_optimizer_not_found")
    if require_active and not optimizer.active:
        raise DomainError("ai_optimizer_inactive", "optimizer is disabled")
    version = session.exec(
        select(AIOptimizerVersion).where(
            col(AIOptimizerVersion.tenant_id) == tenant_id,
            col(AIOptimizerVersion.optimizer_id) == optimizer_id,
            col(AIOptimizerVersion.number) == optimizer.latest_version,
        )
    ).one_or_none()
    if version is None:
        raise DomainError(
            "ai_optimizer_version_missing", "ai_optimizer_version_missing"
        )
    binding = session.exec(
        select(AIOptimizerRuleBinding).where(
            col(AIOptimizerRuleBinding.tenant_id) == tenant_id,
            col(AIOptimizerRuleBinding.optimizer_id) == optimizer_id,
            col(AIOptimizerRuleBinding.rule_id) == rule_id,
            col(AIOptimizerRuleBinding.active).is_(True),
        )
    ).one_or_none()
    if binding is None:
        raise DomainError("ai_rule_not_bound", "rule is not assigned to this optimizer")
    rule = session.exec(
        select(AutomationRule).where(
            col(AutomationRule.tenant_id) == tenant_id,
            col(AutomationRule.id) == rule_id,
        )
    ).one_or_none()
    if rule is None:
        raise DomainError("automation_rule_not_found", "automation_rule_not_found")
    if require_active and not rule.enabled:
        raise DomainError("automation_disabled", "rule is disabled")
    definition = RuleDefinition.model_validate(rule.definition)
    requested = _rule_accounts(definition, bc_id=version.bc_id)
    allowed = set(version.advertiser_ids)
    if requested is not None:
        allowed &= requested
    if not allowed:
        raise DomainError(
            "ai_invalid_rule_scope", "rule and optimizer account scopes do not overlap"
        )
    authorized, revision, revisions = _current_revision(
        session, tenant_id=tenant_id, bc_id=version.bc_id, advertiser_ids=allowed
    )
    return RuleBindingScope(
        tenant_id=tenant_id,
        optimizer_id=optimizer_id,
        rule_id=rule_id,
        bc_id=version.bc_id,
        advertiser_ids=tuple(sorted(authorized)),
        authorization_revision=revision,
        authorization_revisions=revisions,
        optimizer_version=version.number,
        rule_version=rule.version,
        binding_id=binding.id,
    )


def validate_rule_binding(
    session: Session, scope: RuleBindingScope, *, require_active: bool = True
) -> RuleBindingScope:
    """Re-read the binding and authorization immediately before submission."""
    current = resolve_rule_binding(
        session,
        tenant_id=scope.tenant_id,
        optimizer_id=scope.optimizer_id,
        rule_id=scope.rule_id,
        require_active=require_active,
    )
    if (
        current.optimizer_version != scope.optimizer_version
        or current.rule_version != scope.rule_version
        or current.binding_id != scope.binding_id
        or (scope.bc_id and current.bc_id != scope.bc_id)
        or (scope.advertiser_ids and current.advertiser_ids != scope.advertiser_ids)
        or (
            scope.authorization_revision is not None
            and current.authorization_revision != scope.authorization_revision
        )
        or (
            scope.authorization_revisions
            and current.authorization_revisions != scope.authorization_revisions
        )
    ):
        raise DomainError("ai_rule_binding_changed", "optimizer rule binding changed")
    return current
