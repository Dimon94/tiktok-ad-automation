"""Immutable evidence snapshots for optimizer rule decisions."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlmodel import Session, col, select

from app.modules.ai_workbench.rule_binding import RuleBindingScope
from app.modules.ai_workbench.rule_history import freeze_rule_inputs
from app.modules.automation_rules.evaluate import Evaluation
from app.modules.automation_rules.inputs import Inputs
from app.modules.automation_rules.models import (
    AutomationActionLedger,
    AutomationRule,
    AutomationRuleEvidence,
    AutomationRuleRun,
)
from app.modules.automation_rules.schemas import RuleDefinition

DEFAULT_EVIDENCE_RETENTION = timedelta(days=365)


@dataclass(frozen=True)
class RuleEvidenceSnapshot:
    tenant_id: UUID
    rule_id: UUID
    rule_version: int
    definition: dict[str, Any]
    optimizer_id: UUID | None = None
    optimizer_version: int | None = None
    binding_version: int | None = None
    binding_id: UUID | None = None
    bc_id: str | None = None
    advertiser_ids: tuple[str, ...] = ()
    authorization_revision: int | None = None
    authorization_revisions: tuple[tuple[str, int], ...] = ()
    checked_metrics: dict[str, Any] = field(default_factory=dict)
    data_freshness: dict[str, Any] = field(default_factory=dict)
    publication_versions: dict[str, Any] = field(default_factory=dict)
    skipped_accounts: tuple[dict[str, Any], ...] = ()
    checked_objects: tuple[dict[str, Any], ...] = ()
    hits: tuple[dict[str, Any], ...] = ()
    action_ledger_refs: tuple[str, ...] = ()
    management_tasks: tuple[dict[str, Any], ...] = ()
    readback_status: dict[str, Any] = field(default_factory=dict)
    retention_until: datetime | None = None

    @property
    def rule_definition(self) -> dict[str, Any]:
        """Compatibility/readability alias used by evidence consumers."""
        return self.definition

    @property
    def optimizer_scope(self) -> dict[str, Any]:
        return {
            "optimizer_id": str(self.optimizer_id) if self.optimizer_id else None,
            "optimizer_version": self.optimizer_version,
            "bc_id": self.bc_id,
            "advertiser_ids": list(self.advertiser_ids),
            "authorization_revision": self.authorization_revision,
            "authorization_revisions": dict(self.authorization_revisions),
        }

    @classmethod
    def from_evaluation(
        cls,
        *,
        rule: AutomationRule,
        run: AutomationRuleRun,
        result: Evaluation,
        skipped_accounts: list[dict[str, str]],
        scope: RuleBindingScope | None = None,
        freshness: list[dict[str, Any]] | None = None,
        publication_versions: dict[str, int] | None = None,
        checked_objects: tuple[dict[str, Any], ...] | None = None,
        inputs: Inputs | None = None,
    ) -> RuleEvidenceSnapshot:
        definition = RuleDefinition.model_validate(run.definition)
        captured = freeze_rule_inputs(inputs) if inputs is not None else None
        # 阈值是规则配置，绝不能冒充运行时指标；旧调用无输入时明确不可回测。
        checked_metrics = captured["facts"] if captured else {}
        hits = tuple(
            {
                "subject_key": hit.subject_key,
                "subject_label": hit.subject_label,
                "advertiser_id": hit.advertiser_id,
                "bc_id": hit.bc_id,
                "metrics": hit.metrics,
                "target_count": hit.target_count,
                "targets": [asdict(target) for target in hit.targets],
            }
            for hit in result.hits
        )
        derived_checked_objects = tuple(
            {
                "key": target.key,
                "kind": target.kind,
                "advertiser_id": target.advertiser_id,
                "bc_id": target.bc_id,
            }
            for hit in result.hits
            for target in hit.targets
        )
        return cls(
            tenant_id=rule.tenant_id,
            rule_id=rule.id,
            rule_version=run.rule_version,
            definition=deepcopy(run.definition),
            optimizer_id=scope.optimizer_id if scope else run.optimizer_id,
            optimizer_version=scope.optimizer_version
            if scope
            else run.optimizer_version,
            binding_version=scope.optimizer_version if scope else run.binding_version,
            binding_id=scope.binding_id if scope else run.binding_id,
            bc_id=scope.bc_id if scope else None,
            advertiser_ids=scope.advertiser_ids if scope else (),
            authorization_revision=scope.authorization_revision
            if scope
            else run.authorization_revision,
            authorization_revisions=(
                scope.authorization_revisions
                if scope
                else tuple(run.authorization_revisions.items())
            ),
            checked_metrics=checked_metrics,
            data_freshness={
                "evaluated_at": run.finished_at.isoformat()
                if run.finished_at
                else run.started_at.isoformat(),
                "threshold_conditions": [
                    condition.model_dump(mode="json")
                    for condition in definition.conditions
                ],
                "evaluation_inputs": captured["metadata"]
                if captured
                else {"complete": False, "reason": "historical_inputs_unavailable"},
                "checked_count": result.checked,
                "rows": list(freshness or []),
                "sources": sorted({item.get("source", "") for item in freshness or []}),
                "stat_dates": sorted(
                    {
                        item["stat_date"]
                        for item in freshness or []
                        if item.get("stat_date")
                    }
                ),
                "timezones": sorted(
                    {
                        item["timezone"]
                        for item in freshness or []
                        if item.get("timezone")
                    }
                ),
            },
            publication_versions=publication_versions or {},
            skipped_accounts=tuple(deepcopy(skipped_accounts)),
            checked_objects=tuple(captured["objects"])
            if captured
            else (
                checked_objects
                if checked_objects is not None
                else derived_checked_objects
            ),
            hits=hits,
            retention_until=datetime.now(UTC) + DEFAULT_EVIDENCE_RETENTION,
        )

    def persist(
        self,
        session: Session,
        *,
        run_id: UUID | None = None,
    ) -> AutomationRuleEvidence:
        row = AutomationRuleEvidence(
            tenant_id=self.tenant_id,
            run_id=run_id,
            rule_id=self.rule_id,
            rule_version=self.rule_version,
            definition=self.definition,
            optimizer_id=self.optimizer_id,
            optimizer_version=self.optimizer_version,
            binding_version=self.binding_version,
            binding_id=self.binding_id,
            bc_id=self.bc_id,
            advertiser_ids=list(self.advertiser_ids),
            authorization_revision=self.authorization_revision,
            authorization_revisions=dict(self.authorization_revisions),
            checked_metrics=self.checked_metrics,
            data_freshness=self.data_freshness,
            publication_versions=self.publication_versions,
            skipped_accounts=list(self.skipped_accounts),
            checked_objects=list(self.checked_objects),
            hits=list(self.hits),
            action_ledger_refs=list(self.action_ledger_refs),
            management_tasks=list(self.management_tasks),
            readback_status=self.readback_status,
            retention_until=self.retention_until
            or (datetime.now(UTC) + DEFAULT_EVIDENCE_RETENTION),
        )
        session.add(row)
        session.flush()
        return row


def persist_evidence(
    session: Session,
    *,
    rule: AutomationRule,
    run: AutomationRuleRun,
    result: Evaluation,
    skipped_accounts: list[dict[str, str]],
    scope: RuleBindingScope | None = None,
    freshness: list[dict[str, Any]] | None = None,
    publication_versions: dict[str, int] | None = None,
    checked_objects: tuple[dict[str, Any], ...] | None = None,
    inputs: Inputs | None = None,
) -> AutomationRuleEvidence:
    snapshot = RuleEvidenceSnapshot.from_evaluation(
        rule=rule,
        run=run,
        result=result,
        skipped_accounts=skipped_accounts,
        scope=scope,
        freshness=freshness,
        publication_versions=publication_versions,
        checked_objects=checked_objects,
        inputs=inputs,
    )
    row = snapshot.persist(session, run_id=run.id)
    run.evidence_id = row.id
    return row


def referenced_evidence_ids(
    session: Session, *, tenant_id: UUID | None = None
) -> set[UUID]:
    """Return evidence IDs still referenced by durable decision records.

    Rule runs and action ledgers keep direct foreign-key references.  DSH review
    tasks and proposals store a sanitized frozen JSON document, so those IDs are
    collected through :func:`frozen_evidence_ids` without loading any live
    reporting rows.  This lets normal retention cleanup remove transient rows
    while preserving the explanation for an accepted/rejected decision.
    """
    run_query = select(col(AutomationRuleRun.evidence_id)).where(
        col(AutomationRuleRun.evidence_id).is_not(None)
    )
    ledger_query = select(col(AutomationActionLedger.evidence_id)).where(
        col(AutomationActionLedger.evidence_id).is_not(None)
    )
    if tenant_id is not None:
        run_query = run_query.where(col(AutomationRuleRun.tenant_id) == tenant_id)
        ledger_query = ledger_query.where(
            col(AutomationActionLedger.tenant_id) == tenant_id
        )
    result = {
        value for value in session.exec(run_query).all() if isinstance(value, UUID)
    }
    result.update(
        value for value in session.exec(ledger_query).all() if isinstance(value, UUID)
    )

    # Keep these imports lazy: reviews/proposals import the evidence helpers and
    # importing their SQLModel tables at module load time would create a cycle.
    from app.modules.ai_workbench.proposals import AIProposal
    from app.modules.ai_workbench.reviews import DSHReviewTask, frozen_evidence_ids

    review_query = select(DSHReviewTask).where(col(DSHReviewTask.evidence).is_not(None))
    proposal_query = select(AIProposal).where(col(AIProposal.evidence).is_not(None))
    if tenant_id is not None:
        review_query = review_query.where(col(DSHReviewTask.tenant_id) == tenant_id)
        proposal_query = proposal_query.where(col(AIProposal.tenant_id) == tenant_id)
    for task in session.exec(review_query).all():
        for value in frozen_evidence_ids(task.evidence) | {
            str(row["id"])
            for source in task.evidence_store.values()
            if source["category"] == "rule_evidence"
            for row in source["payload"]
        }:
            try:
                result.add(UUID(value))
            except TypeError, ValueError:
                continue
    for proposal in session.exec(proposal_query).all():
        for value in frozen_evidence_ids(proposal.evidence):
            try:
                result.add(UUID(value))
            except TypeError, ValueError:
                continue
    return result


def cleanup_evidence(
    session: Session,
    *,
    now: datetime | None = None,
    retention_days: int = 365,
    tenant_id: UUID | None = None,
    limit: int = 500,
) -> int:
    """Delete expired, unreferenced rule evidence in a bounded batch.

    ``retention_until`` is the configured retention fence for unreferenced
    snapshots.  Any snapshot cited by a run, action ledger, review or proposal
    is always retained, even after its rule is deleted or reporting refreshes
    the source data.
    """
    if retention_days < 1:
        raise ValueError("retention_days must be positive")
    if limit < 1:
        raise ValueError("limit must be positive")
    now = now or datetime.now(UTC)
    # ``retention_until`` already stores the full retention fence (normally
    # creation time + 365 days).  Subtracting ``retention_days`` here would
    # apply the same window twice and retain rows for roughly 730 days.
    # Keep ``retention_days`` as a validated compatibility argument while the
    # persisted fence remains authoritative.
    cutoff = now
    protected = referenced_evidence_ids(session, tenant_id=tenant_id)
    query = (
        select(AutomationRuleEvidence)
        .where(col(AutomationRuleEvidence.retention_until) <= cutoff)
        .order_by(col(AutomationRuleEvidence.created_at))
        .limit(limit)
    )
    if tenant_id is not None:
        query = query.where(col(AutomationRuleEvidence.tenant_id) == tenant_id)
    removed = 0
    for row in session.exec(query).all():
        if row.id in protected:
            continue
        session.delete(row)
        removed += 1
    session.flush()
    return removed


# Descriptive aliases keep the maintenance entry point discoverable to the
# automation cleanup task and to operators invoking a one-off cleanup.
cleanup_rule_evidence = cleanup_evidence
collect_referenced_evidence_ids = referenced_evidence_ids
