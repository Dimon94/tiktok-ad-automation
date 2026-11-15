"""Scheduled AI optimizer handoff into the existing TK-ADA build pipeline.

This module owns only the local occurrence ledger.  It never calls TikTok or a
provider directly; preparation, preview and submission remain delegated to the
existing builds services and their durable outbox tasks.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import or_, text
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.ai_workbench.models import (
    AIOptimizer,
    AIOptimizerStrategyBinding,
    AIOptimizerVersion,
    BuildOccurrence,
)
from app.modules.ai_workbench.plans import AIDailyPlanRow
from app.modules.builds import drafts, previews, submissions
from app.modules.builds.preview_models import BuildPreview
from app.modules.providers.models import ProviderApplication
from app.modules.tenants.permissions import require_tenant

OCCURRENCE_NAMESPACE = UUID("63c5c8b5-4c82-4d9d-938b-6f3091daec85")
TASK_NAME = "ai_workbench.dispatch_occurrence"
register_dispatch_task(TASK_NAME, "builds")
TERMINAL = frozenset({"COMPLETE", "FAILED", "UNKNOWN", "PARTIAL"})
MATERIAL_WAIT_ERRORS = frozenset(
    {
        "material_unavailable",
        "material_not_ready",
        "material_preparing",
        "draft_inputs_empty",
        "preview_not_ready",
        "preview_incomplete",
        "draft_not_ready",
        "copy_material_unavailable",
    }
)


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode()
    ).hexdigest()


def _scheduled_at(row: AIDailyPlanRow) -> datetime:
    # 将用户输入的当地时间转为 UTC；不能把北京时间直接标记为 UTC。
    return datetime.combine(
        row.business_date, row.scheduled_time, tzinfo=ZoneInfo(row.timezone)
    ).astimezone(UTC)


class BuildHandoff:
    """Resolve and execute one tenant-scoped daily-plan row occurrence."""

    def __init__(self, session: Session, *, context: TenantContext):
        self.session = session
        self.context = context

    def _authorize(self) -> None:
        require_tenant(
            self.session,
            actor_id=self.context.actor_id,
            tenant_id=self.context.tenant_id,
            action="build",
        )

    def _resolve_snapshot(
        self,
        plan_row_id: UUID,
        *,
        caller_strategy_version_id: UUID | None,
        allow_inactive: bool = False,
        allow_missing_provider: bool = False,
    ) -> tuple[AIDailyPlanRow, AIOptimizer, AIOptimizerVersion, UUID]:
        row = self.session.exec(
            select(AIDailyPlanRow)
            .where(
                col(AIDailyPlanRow.tenant_id) == self.context.tenant_id,
                col(AIDailyPlanRow.id) == plan_row_id,
            )
            .with_for_update()
        ).one_or_none()
        if row is None:
            raise DomainError(
                "ai_daily_plan_row_not_found", "ai_daily_plan_row_not_found"
            )
        optimizer = self.session.exec(
            select(AIOptimizer)
            .where(
                col(AIOptimizer.tenant_id) == self.context.tenant_id,
                col(AIOptimizer.id) == row.optimizer_id,
            )
            .with_for_update()
        ).one_or_none()
        if optimizer is None:
            raise DomainError("ai_optimizer_not_found", "ai_optimizer_not_found")
        if not optimizer.active and not allow_inactive:
            raise DomainError("ai_optimizer_inactive", "ai_optimizer_inactive")
        binding = self.session.exec(
            select(AIOptimizerStrategyBinding).where(
                col(AIOptimizerStrategyBinding.tenant_id) == self.context.tenant_id,
                col(AIOptimizerStrategyBinding.optimizer_id) == optimizer.id,
                col(AIOptimizerStrategyBinding.active).is_(True),
            )
        ).one_or_none()
        if binding is None:
            raise DomainError(
                "ai_strategy_binding_required", "ai_strategy_binding_required"
            )
        if (
            caller_strategy_version_id is not None
            and caller_strategy_version_id != binding.strategy_version_id
        ):
            raise DomainError(
                "ai_strategy_binding_mismatch", "ai_strategy_binding_mismatch"
            )
        version = self.session.exec(
            select(AIOptimizerVersion).where(
                col(AIOptimizerVersion.tenant_id) == self.context.tenant_id,
                col(AIOptimizerVersion.optimizer_id) == optimizer.id,
                col(AIOptimizerVersion.number) == optimizer.latest_version,
            )
        ).one_or_none()
        if version is None:
            raise DomainError(
                "ai_optimizer_version_missing", "ai_optimizer_version_missing"
            )
        if version.strategy_binding_id != binding.id:
            raise DomainError(
                "ai_strategy_binding_mismatch", "ai_strategy_binding_mismatch"
            )
        # Provider rows are validated when the plan is saved; repeat the scoped
        # check here because an application can be disabled after scheduling.
        application = self.session.exec(
            select(ProviderApplication).where(
                col(ProviderApplication.tenant_id) == self.context.tenant_id,
                col(ProviderApplication.connection_id) == row.provider_connection_id,
                col(ProviderApplication.external_id) == row.application_id,
            )
        ).one_or_none()
        if application is None and not allow_missing_provider:
            raise DomainError(
                "ai_provider_application_not_found", "ai_provider_application_not_found"
            )
        return row, optimizer, version, binding.strategy_version_id

    def create_occurrence(
        self,
        plan_row_id: UUID,
        request_id: UUID,
        *,
        strategy_version_id: UUID | None = None,
        enqueue: bool = True,
        skip_reason: str | None = None,
        allow_inactive: bool = False,
        allow_missing_provider: bool = False,
    ) -> BuildOccurrence:
        """Create or return a frozen occurrence and its local outbox message.

        A caller may provide a strategy only as an assertion.  It can never
        select a cross-optimizer or historical strategy version.
        """
        self._authorize()
        row, optimizer, version, resolved_strategy = self._resolve_snapshot(
            plan_row_id,
            caller_strategy_version_id=strategy_version_id,
            allow_inactive=allow_inactive,
            allow_missing_provider=allow_missing_provider,
        )
        scheduled_at = _scheduled_at(row)
        payload = {
            "optimizer_id": str(optimizer.id),
            "plan_row_id": str(row.id),
            "strategy_version_id": str(resolved_strategy),
            "scheduled_at": scheduled_at.isoformat(),
            "bc_id": version.bc_id,
            "advertiser_ids": list(version.advertiser_ids),
            "provider_connection_id": str(row.provider_connection_id),
            "application_id": row.application_id,
            "title": row.title,
            "promotion_url": row.promotion_url,
            "external_drama_id": row.external_drama_id,
            "order_materials": row.order_materials,
        }
        digest = _digest(payload)
        # Serialize request and schedule checks without relying on caller-side
        # Celery task IDs for deduplication.
        key = int.from_bytes(
            hashlib.sha256(
                f"ai-occurrence:{self.context.tenant_id}:{request_id}".encode()
            ).digest()[:8],
            signed=True,
        )
        SQLAlchemySession.execute(
            self.session, text("SELECT pg_advisory_xact_lock(:key)"), {"key": key}
        )
        schedule_key = int.from_bytes(
            hashlib.sha256(
                f"ai-schedule:{self.context.tenant_id}:{optimizer.id}:{row.id}:{resolved_strategy}:{scheduled_at.isoformat()}".encode()
            ).digest()[:8],
            signed=True,
        )
        SQLAlchemySession.execute(
            self.session,
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": schedule_key},
        )
        prior = self.session.exec(
            select(BuildOccurrence).where(
                col(BuildOccurrence.tenant_id) == self.context.tenant_id,
                col(BuildOccurrence.request_id) == request_id,
            )
        ).one_or_none()
        if prior is not None:
            if prior.request_digest != digest:
                raise DomainError("idempotency_conflict", "idempotency_conflict")
            return prior
        existing = self.session.exec(
            select(BuildOccurrence).where(
                col(BuildOccurrence.tenant_id) == self.context.tenant_id,
                col(BuildOccurrence.optimizer_id) == optimizer.id,
                col(BuildOccurrence.plan_row_id) == row.id,
                col(BuildOccurrence.strategy_version_id) == resolved_strategy,
                col(BuildOccurrence.scheduled_at) == scheduled_at,
            )
        ).one_or_none()
        if existing is not None:
            return existing
        occurrence = BuildOccurrence(
            tenant_id=self.context.tenant_id,
            optimizer_id=optimizer.id,
            plan_row_id=row.id,
            request_id=request_id,
            request_digest=digest,
            strategy_version_id=resolved_strategy,
            bc_id=version.bc_id,
            advertiser_ids=list(version.advertiser_ids),
            provider_connection_id=row.provider_connection_id,
            application_id=row.application_id,
            title=row.title,
            promotion_url=row.promotion_url,
            external_drama_id=row.external_drama_id,
            order_materials=row.order_materials,
            scheduled_at=scheduled_at,
            evidence={
                "snapshot": payload,
                **({"skip_reason": skip_reason} if skip_reason else {}),
            },
        )
        if skip_reason and skip_reason != "future":
            occurrence.status = "FAILED"
            occurrence.error_code = skip_reason
        self.session.add(occurrence)
        self.session.flush()
        if enqueue and not skip_reason:
            occurrence.dispatch_id = enqueue_after_commit(
                self.session,
                context=self.context,
                task_name=TASK_NAME,
                task_key=f"ai-build-occurrence:{occurrence.id}",
                payload={"occurrence_id": str(occurrence.id)},
            )
        self.session.add(occurrence)
        self.session.flush()
        return occurrence

    def dispatch_occurrence(self, occurrence_id: UUID) -> BuildOccurrence:
        """Advance one occurrence through local draft/preview/submission stages."""
        self._authorize()
        occurrence = self.session.exec(
            select(BuildOccurrence)
            .where(
                col(BuildOccurrence.tenant_id) == self.context.tenant_id,
                col(BuildOccurrence.id) == occurrence_id,
            )
            .with_for_update()
        ).one_or_none()
        if occurrence is None:
            raise DomainError(
                "ai_build_occurrence_not_found", "ai_build_occurrence_not_found"
            )
        if occurrence.status in TERMINAL:
            # UNKNOWN is deliberately terminal until an explicit reconciliation
            # changes evidence; a retry must never create a second submission.
            return occurrence
        row = self.session.get(AIDailyPlanRow, occurrence.plan_row_id)
        now = datetime.now(UTC)
        if row is not None and (
            occurrence.scheduled_at > now
            or now.astimezone(ZoneInfo(row.timezone)).date() > row.business_date
        ):
            # 消息可能提前投递或排队跨日：执行边界也必须检查到期与截止。
            future = occurrence.scheduled_at > now
            occurrence.status = "WAITING_MATERIAL" if future else "FAILED"
            occurrence.error_code = (
                "ai_schedule_not_due" if future else "schedule_expired"
            )
            self.session.add(occurrence)
            self.session.flush()
            return occurrence
        optimizer = self.session.get(
            AIOptimizer, occurrence.optimizer_id, populate_existing=True
        )
        if optimizer is None or not optimizer.active:
            # 已投递的任务也必须遵守暂停，恢复后沿用同一发生记录续接。
            occurrence.status = "WAITING_MATERIAL"
            occurrence.error_code = "ai_optimizer_inactive"
            self.session.add(occurrence)
            self.session.flush()
            return occurrence
        try:
            if occurrence.draft_id is None:
                draft_request = uuid5(OCCURRENCE_NAMESPACE, f"draft:{occurrence.id}")
                occurrence.draft_id = drafts.create_draft(
                    self.session,
                    context=self.context,
                    bc_id=occurrence.bc_id,
                    strategy_version_id=occurrence.strategy_version_id,
                    provider_connection_id=occurrence.provider_connection_id,
                    execution_connection_id=None,
                    application_id=occurrence.application_id,
                    drama_lines=[occurrence.title],
                    account_lines=list(occurrence.advertiser_ids),
                    link_config={},
                    manual_links=(
                        [
                            {
                                "line_no": 1,
                                "url": occurrence.promotion_url,
                                "material_identifier": occurrence.title,
                                "external_drama_id": occurrence.external_drama_id or "",
                                "protected_base": "",
                            }
                        ]
                        if occurrence.promotion_url
                        else []
                    ),
                    request_id=draft_request,
                )
            draft_request = uuid5(OCCURRENCE_NAMESPACE, f"prepare:{occurrence.id}")
            drafts.prepare_draft(
                self.session,
                context=self.context,
                draft_id=occurrence.draft_id,
                request_id=draft_request,
            )
            draft = drafts.get_draft(
                self.session, context=self.context, draft_id=occurrence.draft_id
            )
            # 准备任务通过 outbox 异步执行，先提交本事务，再由后续调度恢复。
            if draft.status != "READY":
                occurrence.status = "WAITING_MATERIAL"
                occurrence.evidence = {
                    **occurrence.evidence,
                    "preparation_status": draft.status,
                }
                occurrence.updated_at = datetime.now(UTC)
                self.session.add(occurrence)
                self.session.flush()
                return occurrence
            preview_id = occurrence.preview_id
            if preview_id is None:
                preview_id = previews.generate_preview(
                    self.session,
                    context=self.context,
                    draft_id=occurrence.draft_id,
                    expected_revision=draft.revision,
                )
                occurrence.preview_id = preview_id
            preview = self.session.get(BuildPreview, occurrence.preview_id)
            if preview is None or preview.status not in {"FROZEN"}:
                occurrence.status = "WAITING_MATERIAL"
                occurrence.evidence["preview_status"] = getattr(
                    preview, "status", "BUILDING"
                )
            else:
                self.session.refresh(optimizer)
                if not optimizer.active:
                    occurrence.status = "WAITING_MATERIAL"
                    occurrence.error_code = "ai_optimizer_inactive"
                    self.session.add(occurrence)
                    self.session.flush()
                    return occurrence
                receipt = submissions.submit_preview(
                    self.session,
                    context=self.context,
                    preview_id=preview.id,
                    request_id=uuid5(OCCURRENCE_NAMESPACE, f"submit:{occurrence.id}"),
                )
                occurrence.submission_id = receipt.submission_id
                occurrence.status = "DISPATCHED"
            occurrence.error_code = None
        except DomainError as error:
            occurrence.error_code = error.code
            occurrence.status = (
                "WAITING_MATERIAL" if error.code in MATERIAL_WAIT_ERRORS else "FAILED"
            )
        occurrence.updated_at = datetime.now(UTC)
        self.session.add(occurrence)
        self.session.flush()
        return occurrence

    def schedule_retry(self, occurrence_id: UUID) -> BuildOccurrence:
        """Re-arm a material wait through a new durable outbox generation."""
        self._authorize()
        occurrence = self.session.exec(
            select(BuildOccurrence)
            .where(
                col(BuildOccurrence.tenant_id) == self.context.tenant_id,
                col(BuildOccurrence.id) == occurrence_id,
            )
            .with_for_update()
        ).one_or_none()
        if occurrence is None:
            raise DomainError(
                "ai_build_occurrence_not_found", "ai_build_occurrence_not_found"
            )
        if occurrence.status != "WAITING_MATERIAL":
            return occurrence
        generation = int(occurrence.evidence.get("retry_generation", 0)) + 1
        occurrence.evidence = {**occurrence.evidence, "retry_generation": generation}
        occurrence.status = "PENDING"
        occurrence.dispatch_id = enqueue_after_commit(
            self.session,
            context=self.context,
            task_name=TASK_NAME,
            task_key=f"ai-build-occurrence:{occurrence.id}:retry:{generation}",
            payload={"occurrence_id": str(occurrence.id)},
        )
        occurrence.updated_at = datetime.now(UTC)
        self.session.add(occurrence)
        self.session.flush()
        return occurrence

    def reconcile_occurrence(
        self,
        occurrence_id: UUID,
        *,
        state: str,
        evidence: dict[str, Any] | None = None,
    ) -> BuildOccurrence:
        """Import an existing build readback without replaying unknown work."""
        if state not in {"PARTIAL", "UNKNOWN", "COMPLETE", "FAILED", "DISPATCHED"}:
            raise DomainError(
                "ai_build_occurrence_state_invalid", "ai_build_occurrence_state_invalid"
            )
        self._authorize()
        occurrence = self.session.exec(
            select(BuildOccurrence)
            .where(
                col(BuildOccurrence.tenant_id) == self.context.tenant_id,
                col(BuildOccurrence.id) == occurrence_id,
            )
            .with_for_update()
        ).one_or_none()
        if occurrence is None:
            raise DomainError(
                "ai_build_occurrence_not_found", "ai_build_occurrence_not_found"
            )
        if occurrence.status == "UNKNOWN" and state == "DISPATCHED":
            if not (evidence or {}).get("explicit_retry"):
                raise DomainError(
                    "ai_build_occurrence_retry_required",
                    "ai_build_occurrence_retry_required",
                )
        occurrence.status = state
        if evidence:
            occurrence.evidence = {**occurrence.evidence, "readback": evidence}
        occurrence.updated_at = datetime.now(UTC)
        self.session.add(occurrence)
        self.session.flush()
        return occurrence

    # A concise alias used by result workers and tests.
    reconcile = reconcile_occurrence

    def record_submission_result(
        self,
        submission_id: UUID,
        *,
        state: str,
        evidence: dict[str, Any] | None = None,
    ) -> BuildOccurrence | None:
        """Import the aggregate submission result after all steps settle."""
        mapped_step = {
            "SUCCEEDED": "COMPLETE",
            "VERIFIED_REPLACEMENT": "COMPLETE",
            "NOT_REQUIRED": "COMPLETE",
            "MISMATCH": "PARTIAL",
            "FAILED": "FAILED",
            "UNKNOWN": "UNKNOWN",
        }.get(state)
        if mapped_step is None:
            return None
        from app.modules.builds.execution_models import Submission

        submission = self.session.get(Submission, submission_id)
        if submission is None or submission.status not in {
            "COMPLETED",
            "PARTIAL",
            "FAILED",
            "NEEDS_REVIEW",
        }:
            # finish_delivery can be called for every child step; until the
            # existing finalizer reports an aggregate terminal status there is
            # no occurrence transition to record.
            return None
        if submission.status == "NEEDS_REVIEW":
            from app.modules.builds.execution_models import (
                ExecutionStep,
                SubmissionUnit,
            )
            from app.modules.builds.receipt_completion import obsolete_readback_sql

            active_unit = self.session.exec(
                select(col(SubmissionUnit.unit_id))
                .where(
                    col(SubmissionUnit.tenant_id) == self.context.tenant_id,
                    col(SubmissionUnit.submission_id) == submission_id,
                    col(SubmissionUnit.disposition) == "INCLUDED",
                    or_(
                        col(SubmissionUnit.expanded).is_(False),
                        col(SubmissionUnit.dispatch_id).is_not(None),
                    ),
                )
                .limit(1)
            ).first()
            active_step = self.session.exec(
                select(col(ExecutionStep.id))
                .where(
                    col(ExecutionStep.tenant_id) == self.context.tenant_id,
                    col(ExecutionStep.submission_id) == submission_id,
                    col(ExecutionStep.status).in_(["QUEUED", "RUNNING"]),
                    text("NOT " + obsolete_readback_sql("execution_step")),
                )
                .limit(1)
            ).first()
            if active_unit is not None or active_step is not None:
                return None
        mapped = {
            "COMPLETED": "COMPLETE",
            "PARTIAL": "PARTIAL",
            "FAILED": "FAILED",
            # Existing aggregate status uses NEEDS_REVIEW for UNKNOWN or
            # mismatch evidence; preserve the occurrence's explicit UNKNOWN
            # reconciliation state until a later readback resolves it.
            "NEEDS_REVIEW": "UNKNOWN",
        }[submission.status]
        occurrence = self.session.exec(
            select(BuildOccurrence)
            .where(
                col(BuildOccurrence.tenant_id) == self.context.tenant_id,
                col(BuildOccurrence.submission_id) == submission_id,
            )
            .with_for_update()
        ).one_or_none()
        if occurrence is None:
            return None
        if occurrence.status == "COMPLETE" and mapped != "COMPLETE":
            return occurrence
        if occurrence.status == "FAILED" and mapped in {"PARTIAL", "UNKNOWN"}:
            return occurrence
        if occurrence.status == "UNKNOWN" and mapped == "DISPATCHED":
            return occurrence
        occurrence.status = mapped
        occurrence.evidence = {
            **occurrence.evidence,
            "readback": {
                "state": state,
                "aggregate_status": submission.status,
                **(evidence or {}),
            },
        }
        occurrence.updated_at = datetime.now(UTC)
        self.session.add(occurrence)
        self.session.flush()
        return occurrence
