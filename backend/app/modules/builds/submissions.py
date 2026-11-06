"""Idempotent submission and bounded local expansion; callers own transactions."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import func, insert, text
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.core.pagination import Page
from app.modules.accounts.access import resolve_account_access
from app.modules.accounts.models import TenantBC
from app.modules.accounts.routing import verify_route
from app.modules.builds.execution_models import (
    DraftUnitReservation,
    ExecutionStep,
    Submission,
    SubmissionRequest,
    SubmissionUnit,
)
from app.modules.builds.execution_schemas import (
    ExecutionUnit,
    StepClaim,
    StepPublic,
    SubmissionReceipt,
    SubmissionUnitPublic,
    SubmissionView,
)
from app.modules.builds.models import BuildDraft
from app.modules.builds.preview_models import BuildPreview, BuildUnit
from app.modules.builds.preview_schemas import (
    budget_strategy_label,
    frozen_bid_strategy,
)
from app.modules.builds.previews import load_frozen_unit
from app.modules.builds.route_views import execution_route_view
from app.modules.builds.routes import (
    load_preview_route,
    save_attempt_context,
    verify_unit_route,
)
from app.modules.tenants.permissions import require_tenant

from .ad_readiness import included_ad_sql, included_group_sql
from .submission_counts import INCLUDED_OBJECT_COUNTS


def authorize(session: Session, context: TenantContext, action: str = "read") -> None:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action=action
    )


def submission_row(
    session: Session, context: TenantContext, identity: UUID, *, lock: bool = False
) -> Submission:
    stmt = select(Submission).where(
        Submission.tenant_id == context.tenant_id, Submission.id == identity
    )
    if lock:
        stmt = stmt.with_for_update(key_share=True)
    row = session.exec(stmt.execution_options(populate_existing=True)).one_or_none()
    if row is None:
        raise DomainError("resource_not_found", "提交不存在")
    return row


# All earlier eligible frozen scopes count, even before their workers expand.
# 排除组合不占位；只有已冻结并在受理时复核的续建计划能接续旧范围。
COVERED = """(EXISTS (
 SELECT 1 FROM build_submission old JOIN build_unit old_u
 ON old_u.tenant_id=old.tenant_id AND old_u.preview_id=old.preview_id
 WHERE old.tenant_id=:tenant AND old.draft_id=:draft AND old.ordinal<:ordinal
 AND old_u.drama_id=u.drama_id AND old_u.advertiser_id=u.advertiser_id
 AND old_u.complete AND old_u.readiness IN ('READY','PREPARING'))
 AND coalesce(u.scene_snapshot->'continuation'->>'state','')<>'CONTINUE')"""


def params(row: Submission) -> dict[str, Any]:
    return {
        "tenant": row.tenant_id,
        "submission": row.id,
        "preview": row.preview_id,
        "draft": row.draft_id,
        "ordinal": row.ordinal,
        "unit": None,
    }


def submit_preview(
    session: Session, *, context: TenantContext, preview_id: UUID, request_id: UUID
) -> SubmissionReceipt:
    authorize(session, context, "build")
    # A request can race across different draft locks. Serialize exactly that
    # tenant-owned ledger key before reading either parent; no external call.
    SQLAlchemySession.execute(
        session,
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"submission:{context.tenant_id}:{request_id}"},
    )
    alias = session.get(
        SubmissionRequest, (context.tenant_id, request_id), populate_existing=True
    )
    if alias is not None:
        if alias.preview_id != preview_id:
            raise DomainError("idempotency_conflict", "幂等键已绑定其他预览")
        saved = submission_row(session, context, alias.submission_id)
        return SubmissionReceipt(submission_id=saved.id, status=saved.status)
    preview = session.exec(
        select(BuildPreview).where(
            BuildPreview.tenant_id == context.tenant_id, BuildPreview.id == preview_id
        )
    ).one_or_none()
    if preview is None:
        raise DomainError("preview_not_found", "预览不存在")
    draft = session.exec(
        select(BuildDraft)
        .where(
            BuildDraft.tenant_id == context.tenant_id, BuildDraft.id == preview.draft_id
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    preview = session.exec(
        select(BuildPreview)
        .where(BuildPreview.id == preview_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    existing = session.exec(
        select(Submission).where(
            Submission.tenant_id == context.tenant_id,
            Submission.preview_id == preview_id,
        )
    ).one_or_none()
    if existing is None:
        from .creative_units import preview_contract

        # 先解析所有历史提交重放；V1 仅禁止创建新的执行任务。
        if (
            preview_contract(
                session, tenant_id=context.tenant_id, preview_id=preview.id
            ).version
            == 1
        ):
            raise DomainError(
                "preview_contract_expired", "历史预览仅供查看，请重新生成预览"
            )
        route = load_preview_route(session, context=context, preview_id=preview.id)
        verify_route(
            session, context=context, route=route, advertiser_id=None, capability="read"
        )
        bc = session.get(
            TenantBC, (context.tenant_id, preview.bc_id), populate_existing=True
        )
        if bc is None or bc.ownership_conflict:
            raise DomainError("account_ownership_conflict", "BC 不可提交")
        if preview.status != "FROZEN" or not preview.content_digest:
            raise DomainError("preview_not_frozen", "预览尚未冻结")
        if draft.revision != preview.draft_revision:
            raise DomainError("draft_revision_conflict", "草稿已变更")
        eligible = session.exec(
            select(BuildUnit.id)
            .where(
                BuildUnit.tenant_id == context.tenant_id,
                BuildUnit.preview_id == preview_id,
                col(BuildUnit.complete).is_(True),
                col(BuildUnit.readiness).in_(["READY", "PREPARING"]),
            )
            .limit(1)
        ).first()
        if eligible is None:
            reason = SQLAlchemySession.execute(
                session,
                text(
                    "SELECT scene_snapshot->'continuation'->>'reason' FROM build_unit WHERE tenant_id=:tenant AND preview_id=:preview AND scene_snapshot->'continuation'->>'state'='BLOCKED' ORDER BY id LIMIT 1"
                ),
                {"tenant": context.tenant_id, "preview": preview.id},
            ).scalar_one_or_none()
            raise DomainError(reason or "preview_not_submittable", "没有可提交组合")
        from .continuation import validate_continuations

        validate_continuations(session, context=context, preview=preview)
        ordinal = (
            session.exec(
                select(func.coalesce(func.max(Submission.ordinal), 0)).where(
                    Submission.tenant_id == context.tenant_id,
                    Submission.draft_id == draft.id,
                )
            ).one()
            + 1
        )
        remaining = SQLAlchemySession.execute(
            session,
            text(
                "SELECT u.id FROM build_unit u WHERE u.tenant_id=:tenant AND u.preview_id=:preview AND u.complete AND u.readiness IN ('READY','PREPARING') AND NOT "
                + COVERED
                + " LIMIT 1"
            ),
            {
                "tenant": context.tenant_id,
                "preview": preview.id,
                "draft": draft.id,
                "ordinal": ordinal,
            },
        ).first()
        if remaining is None:
            # 旧任务即使部分失败也已占用范围；说明去重原因，不能误报成版本或请求键冲突。
            raise DomainError(
                "preview_scope_already_submitted",
                "当前草稿的可执行范围已被先前提交覆盖",
            )
        existing = Submission(
            tenant_id=context.tenant_id,
            bc_id=preview.bc_id,
            preview_id=preview.id,
            draft_id=draft.id,
            actor_id=context.actor_id,
            ordinal=ordinal,
        )
        session.add(existing)
        session.flush()
        # Permission changes after this point are checked again per bounded unit
        # and before every effect. No task reads mutable draft execution inputs.
        from app.modules.builds.submission_tasks import queue_expansion

        queue_expansion(session, existing)
    session.add(
        SubmissionRequest(
            tenant_id=context.tenant_id,
            request_id=request_id,
            submission_id=existing.id,
            preview_id=existing.preview_id,
            bc_id=existing.bc_id,
        )
    )
    session.flush()
    return SubmissionReceipt(submission_id=existing.id, status=existing.status)


# Preserve correlation even before freshly generated plan tables are analyzed.
# Without these optimizer boundaries PostgreSQL can repeatedly join every ad to
# every group in the preview, or materialize all steps in a large submission.
# 所有素材依赖均按账户的冻结排除证据展开；不能因素材后来恢复而重新加入。

BLUEPRINTS = (
    """
WITH unit_groups AS MATERIALIZED (
 SELECT g.* FROM planned_group g WHERE g.tenant_id=:tenant AND g.preview_id=:preview AND g.unit_id=:unit AND """
    + included_group_sql()
    + """
), material_candidates AS MATERIALIZED (
 SELECT DISTINCT m.material_id
 FROM unit_groups g JOIN planned_ad a ON a.tenant_id=g.tenant_id AND a.preview_id=g.preview_id AND a.group_id=g.id
 JOIN frozen_ad_material m ON m.tenant_id=g.tenant_id AND m.preview_id=g.preview_id AND m.drama_id=g.drama_id AND m.group_no=g.group_no AND m.base_ad_no=a.base_ad_no
 WHERE """
    + included_ad_sql()
    + """
 AND NOT EXISTS (SELECT 1 FROM preview_skipped_material skipped
 WHERE skipped.tenant_id=m.tenant_id AND skipped.unit_id=:unit AND skipped.material_id=m.material_id)
), candidates AS (
 SELECT 0 priority, 'MATERIAL:'||m.material_id k, NULL::text parent,
 'MATERIAL' kind, NULL::uuid group_id, NULL::uuid ad_id, m.material_id FROM material_candidates m
 UNION ALL SELECT 1,'CTA',NULL,'CTA',NULL,NULL,NULL WHERE EXISTS (SELECT 1 FROM unit_groups)
 UNION ALL SELECT 2,'CAMPAIGN',NULL,'CAMPAIGN',NULL,NULL,NULL WHERE EXISTS (SELECT 1 FROM unit_groups)
 UNION ALL SELECT 4,'ADGROUP:'||g.id,'CAMPAIGN','ADGROUP',g.id,NULL,NULL FROM unit_groups g
 UNION ALL SELECT 6,'AD:'||a.id,'ADGROUP:'||g.id,'AD',g.id,a.id,NULL
 FROM unit_groups g JOIN planned_ad a ON a.tenant_id=:tenant AND a.preview_id=:preview AND a.group_id=g.id
 WHERE """
    + included_ad_sql()
    + """)
SELECT * FROM candidates c WHERE NOT EXISTS (
 SELECT 1 FROM execution_step s WHERE s.tenant_id=:tenant AND s.submission_id=:submission
 AND s.unit_id=:unit AND s.step_key=CAST(:unit AS text)||':'||c.k OFFSET 0)
ORDER BY priority,k LIMIT :limit
"""
)


def expand_submission(
    session: Session, *, context: TenantContext, submission_id: UUID, limit: int = 100
) -> bool:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("expansion limit must be between 1 and 100")
    authorize(session, context, "build")
    row = submission_row(session, context, submission_id, lock=True)
    if row.actor_id != context.actor_id:
        raise DomainError("action_forbidden", "提交操作者不匹配")
    route = load_preview_route(session, context=context, preview_id=row.preview_id)
    verify_route(
        session, context=context, route=route, advertiser_id=None, capability="read"
    )
    if row.expanded:
        return True
    used = 0
    while used < limit:
        unit = session.exec(
            select(SubmissionUnit)
            .where(
                SubmissionUnit.tenant_id == context.tenant_id,
                SubmissionUnit.submission_id == row.id,
                col(SubmissionUnit.expanded).is_(False),
            )
            .order_by(col(SubmissionUnit.unit_id))
            .limit(1)
        ).first()
        if unit is None:
            after = row.progress.get("after")
            stmt = select(BuildUnit).where(
                BuildUnit.tenant_id == context.tenant_id,
                BuildUnit.preview_id == row.preview_id,
            )
            if after:
                stmt = stmt.where(BuildUnit.id > UUID(after))
            frozen = session.exec(stmt.order_by(col(BuildUnit.id)).limit(1)).first()
            if frozen is None:
                row.expanded = True
                row.progress = {"phase": "done"}
                row.updated_at = datetime.now(UTC)
                session.add(row)
                session.flush()
                return True
            if frozen.connection_id != route.connection_id:
                raise DomainError("frozen_route_changed", "执行组合连接与父预览不一致")
            covered = bool(
                SQLAlchemySession.execute(
                    session,
                    text(
                        "SELECT "
                        + COVERED
                        + " FROM build_unit u WHERE u.id=:unit AND u.tenant_id=:tenant"
                    ),
                    dict(params(row), unit=frozen.id),
                ).scalar_one()
            )
            reason = (
                "previous_submission"
                if covered
                else (
                    frozen.reason_codes[0] if frozen.reason_codes else "preview_blocked"
                )
            )
            included = (
                frozen.complete
                and frozen.readiness in {"READY", "PREPARING"}
                and not covered
            )
            unit = SubmissionUnit(
                tenant_id=row.tenant_id,
                submission_id=row.id,
                unit_id=frozen.id,
                preview_id=row.preview_id,
                bc_id=row.bc_id,
                disposition="INCLUDED" if included else "EXCLUDED",
                reason_code=None if included else reason,
                expanded=not included,
            )
            session.add(unit)
            if (
                included
                and frozen.scene_snapshot.get("continuation", {}).get("state")
                != "CONTINUE"
            ):
                session.add(
                    DraftUnitReservation(
                        tenant_id=row.tenant_id,
                        draft_id=row.draft_id,
                        drama_id=frozen.drama_id,
                        advertiser_id=frozen.advertiser_id,
                        bc_id=row.bc_id,
                        submission_id=row.id,
                        preview_id=row.preview_id,
                        unit_id=frozen.id,
                    )
                )
            row.progress = {"phase": "units", "after": str(frozen.id)}
            session.add(row)
            session.flush()
            used += 1
            continue
        frozen = session.exec(
            select(BuildUnit).where(
                BuildUnit.tenant_id == row.tenant_id,
                BuildUnit.id == unit.unit_id,
                BuildUnit.preview_id == row.preview_id,
            )
        ).one()
        candidates = (
            SQLAlchemySession.execute(
                session,
                text(BLUEPRINTS),
                dict(
                    params(row),
                    unit=frozen.id,
                    drama=frozen.drama_id,
                    limit=limit - used,
                ),
            )
            .mappings()
            .all()
        )
        if not candidates:
            unit.expanded = True
            from app.modules.builds.submission_tasks import queue_execution_unit

            queue_execution_unit(session, submission=row, unit=unit)
            session.add(unit)
            session.flush()
            used += 1
            continue
        from .continuation import record_reused_steps, reused_step_values

        step_rows = []
        for candidate in candidates:
            key = f"{frozen.id}:{candidate['k']}"
            parent = (
                uuid5(row.id, f"{frozen.id}:{candidate['parent']}")
                if candidate["parent"]
                else None
            )
            step_rows.append(
                ExecutionStep(
                    id=uuid5(row.id, key),
                    tenant_id=row.tenant_id,
                    submission_id=row.id,
                    preview_id=row.preview_id,
                    bc_id=row.bc_id,
                    unit_id=frozen.id,
                    kind=candidate["kind"],
                    step_key=key,
                    group_id=candidate["group_id"],
                    planned_ad_id=candidate["ad_id"],
                    material_id=candidate["material_id"],
                    parent_step_id=parent,
                    **reused_step_values(session, unit=frozen, key=candidate["k"]),
                ).model_dump()
            )
        # One bounded VALUES statement checks self-reference FKs at statement
        # completion. Parents are either in this page or a committed prior page;
        # deterministic IDs, the locked cursor and rollback semantics are unchanged.
        # RETURNING enables PostgreSQL insertmanyvalues with a cached one-row
        # compilation template. Explicit .values(all_rows) recompiles thousands
        # of bind parameters for every page and defeats the batching benefit.
        SQLAlchemySession.execute(
            session,
            insert(ExecutionStep)
            .returning(col(ExecutionStep.id))
            .execution_options(render_nulls=True),
            step_rows,
        ).all()
        record_reused_steps(
            session,
            identities=[
                r["id"] for r in step_rows if r["resolved"].get("reused_from_step_id")
            ],
        )
        used += len(step_rows)
    row.updated_at = datetime.now(UTC)
    session.add(row)
    return False


def load_execution_unit(
    session: Session, *, context: TenantContext, submission_id: UUID, unit_id: UUID
) -> ExecutionUnit:
    authorize(session, context)
    row = submission_row(session, context, submission_id)
    unit = session.get(SubmissionUnit, (context.tenant_id, submission_id, unit_id))
    if unit is None or unit.disposition != "INCLUDED":
        raise DomainError("resource_not_found", "执行组合不存在")
    frozen = load_frozen_unit(session, context=context, unit_id=unit_id)
    if frozen.preview_id != row.preview_id:
        raise DomainError("resource_not_found", "执行组合不存在")
    return ExecutionUnit(submission_id=row.id, actor_id=row.actor_id, frozen=frozen)


def claim_step(
    session: Session,
    *,
    context: TenantContext,
    step_id: UUID,
    owner: UUID,
    lease_seconds: int = 60,
) -> StepClaim | None:
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 960:
        raise ValueError("invalid execution lease")
    authorize(session, context, "build")
    step = session.exec(
        select(ExecutionStep)
        .where(
            ExecutionStep.tenant_id == context.tenant_id, ExecutionStep.id == step_id
        )
        .with_for_update()
    ).one_or_none()
    if step is None:
        raise DomainError("resource_not_found", "执行步骤不存在")
    row = submission_row(session, context, step.submission_id)
    if row.actor_id != context.actor_id:
        raise DomainError("action_forbidden", "提交操作者不匹配")
    now = datetime.now(UTC)
    if (
        step.status in {"SUCCEEDED", "FAILED", "UNKNOWN"}
        or step.remote_id
        or step.phase == "REQUEST_ARMED"
        or step.due_at > now
        or (step.lease_expires_at is not None and step.lease_expires_at > now)
    ):
        return None
    unit_state = session.get(SubmissionUnit, (row.tenant_id, row.id, step.unit_id))
    if unit_state is None or not unit_state.expanded:
        return None
    if step.kind == "CAMPAIGN":
        cta = session.exec(
            select(ExecutionStep.status).where(
                ExecutionStep.tenant_id == row.tenant_id,
                ExecutionStep.submission_id == row.id,
                ExecutionStep.unit_id == step.unit_id,
                ExecutionStep.kind == "CTA",
            )
        ).one_or_none()
        if (
            cta != "SUCCEEDED"
            or group_material_state(
                session, context=context, submission_id=row.id, unit_id=step.unit_id
            )
            != "READY"
        ):
            return None
    if (
        step.kind == "ADGROUP"
        and group_material_state(
            session,
            context=context,
            submission_id=row.id,
            unit_id=step.unit_id,
            group_id=step.group_id,
        )
        != "READY"
    ):
        return None
    if step.parent_step_id:
        parent = session.get(ExecutionStep, step.parent_step_id)
        if parent is None or parent.status != "SUCCEEDED":
            return None
    frozen = session.exec(
        select(BuildUnit).where(
            BuildUnit.tenant_id == row.tenant_id, BuildUnit.id == step.unit_id
        )
    ).one()
    route = verify_unit_route(session, context=context, unit=frozen, capability="build")
    access = resolve_account_access(
        session,
        context=context,
        bc_id=row.bc_id,
        advertiser_id=frozen.advertiser_id,
        action="build",
        connection_id=route.connection_id,
    )
    if access.connection_id != frozen.connection_id:
        raise DomainError("account_access_denied", "冻结账户授权已变更")
    request_attempt_id = None
    if step.request_body is not None:
        from app.modules.builds.execution_state import (
            approved_absent_resend,
            first_armed_attempt_id,
            safely_retryable_attempt,
        )

        if approved_absent_resend(session, step=step):
            # 已核实未创建、经显式恢复批准：新本地 attempt 记证据，原样重发冻结正文。
            request_attempt_id = first_armed_attempt_id(session, step=step)
            step.attempt += 1
        elif not safely_retryable_attempt(session, step=step):
            raise DomainError("create_result_unknown", "原请求只能核查，不可再次创建")
    else:
        step.attempt += 1
    step.status, step.phase = "RUNNING", "CLAIMED"
    attempt_id = save_attempt_context(session, step=step)
    step.lease_token, step.lease_expires_at = (
        owner,
        now + timedelta(seconds=lease_seconds),
    )
    step.updated_at = now
    session.add(step)
    session.flush()
    return StepClaim(
        step_id=step.id,
        tenant_id=row.tenant_id,
        submission_id=row.id,
        preview_id=row.preview_id,
        unit_id=step.unit_id,
        actor_id=row.actor_id,
        bc_id=row.bc_id,
        advertiser_id=frozen.advertiser_id,
        kind=step.kind,
        group_id=step.group_id,
        planned_ad_id=step.planned_ad_id,
        material_id=step.material_id,
        parent_step_id=step.parent_step_id,
        lease_token=owner,
        lease_expires_at=step.lease_expires_at,
        attempt=step.attempt,
        attempt_id=attempt_id,
        route=route,
        dispatch_revision=step.dispatch_revision,
        request_attempt_id=request_attempt_id,
    )


def aggregate_status(states: Any) -> str:
    values = set(states)
    if values.intersection({"UNKNOWN", "MISMATCH"}):
        return "NEEDS_REVIEW"
    if values.intersection({"PENDING", "RUNNING", "RETRYABLE"}):
        return "RUNNING"
    if values == {"SUCCEEDED"}:
        return "COMPLETED"
    if "SUCCEEDED" in values and "FAILED" in values:
        return "PARTIAL"
    if values == {"FAILED"}:
        return "FAILED"
    return "QUEUED"


SCOPE_CTE = (
    # 覆盖判断包含续建计划；只计算一次，避免 NOT EXISTS/JSON 条件被展开到
    # 各类对象统计的每个分支，造成大批账户的规划器膨胀。
    "WITH scope_basis AS MATERIALIZED (SELECT u.*, "
    + COVERED
    + """ covered
    FROM build_unit u WHERE u.tenant_id=:tenant AND u.preview_id=:preview),
    scope AS (SELECT b.*, (b.complete AND b.readiness IN ('READY','PREPARING') AND NOT b.covered) included FROM scope_basis b)
    """
)


def get_submission(
    session: Session, *, context: TenantContext, submission_id: UUID
) -> SubmissionView:
    from app.modules.builds.corrections import resolved_sql
    from app.modules.builds.execution_schemas import ObjectCounts, SubmissionView
    from app.modules.builds.receipt_completion import obsolete_readback_sql

    authorize(session, context)
    row = submission_row(session, context, submission_id)
    preview = session.get(BuildPreview, row.preview_id)
    assert preview
    scope = (
        SQLAlchemySession.execute(
            session,
            text(
                SCOPE_CTE
                + INCLUDED_OBJECT_COUNTS
                + """SELECT count(*) planned_c,coalesce(sum(scope.group_count),0) planned_g,coalesce(sum(scope.ad_count),0) planned_a,
 count(*) FILTER(WHERE included) submitted_c,coalesce(sum(c.group_count) FILTER(WHERE included),0) submitted_g,
 coalesce(sum(c.ad_count) FILTER(WHERE included),0) submitted_a,
 count(*) FILTER(WHERE NOT included) excluded_units,count(DISTINCT drama_id) dramas,count(DISTINCT advertiser_id) accounts
 FROM scope LEFT JOIN included_object_counts c
 ON c.tenant_id=scope.tenant_id AND c.preview_id=scope.preview_id AND c.unit_id=scope.id"""
            ),
            params(row),
        )
        .mappings()
        .one()
    )
    outcome = (
        SQLAlchemySession.execute(
            session,
            text(
                SCOPE_CTE
                + """,
 failed_ancestors AS MATERIALIZED (
 SELECT unit_id,bool_or(kind='CTA') cta_failed,bool_or(kind='CAMPAIGN') campaign_failed
 FROM execution_step WHERE tenant_id=:tenant AND submission_id=:submission
 AND status='FAILED' AND kind IN ('CTA','CAMPAIGN') GROUP BY unit_id),
 failed_adgroups AS MATERIALIZED (
 SELECT DISTINCT unit_id,group_id FROM execution_step
 WHERE tenant_id=:tenant AND submission_id=:submission AND status='FAILED' AND kind='ADGROUP'),
 material_failed_groups AS MATERIALIZED (
 SELECT DISTINCT g.id group_id,g.unit_id
 FROM execution_step ms JOIN planned_group g
 ON g.tenant_id=ms.tenant_id AND g.preview_id=:preview AND g.unit_id=ms.unit_id
 JOIN frozen_group_material m ON m.tenant_id=g.tenant_id AND m.preview_id=g.preview_id
 AND m.drama_id=g.drama_id AND m.group_no=g.group_no AND m.material_id=ms.material_id
 AND NOT EXISTS (SELECT 1 FROM preview_skipped_material skipped
 WHERE skipped.tenant_id=m.tenant_id AND skipped.unit_id=g.unit_id AND skipped.material_id=m.material_id)
 WHERE ms.tenant_id=:tenant AND ms.submission_id=:submission AND ms.kind='MATERIAL' AND ms.status='FAILED' AND NOT EXISTS (SELECT 1 FROM preview_contract pc WHERE pc.tenant_id=ms.tenant_id AND pc.preview_id=:preview AND pc.media_kind='CAROUSEL')),
 material_failed_units AS MATERIALIZED (
 SELECT mg.unit_id FROM material_failed_groups mg GROUP BY mg.unit_id
 HAVING count(*)=(SELECT count(*) FROM planned_group g
 WHERE g.tenant_id=:tenant AND g.preview_id=:preview AND g.unit_id=mg.unit_id)),
 objects AS (
 SELECT u.id unit_id,'CAMPAIGN' kind,NULL::uuid group_id,NULL::uuid ad_id FROM scope u WHERE included
 UNION ALL SELECT u.id,'ADGROUP',g.id,NULL FROM scope u JOIN planned_group g ON g.tenant_id=u.tenant_id AND g.preview_id=u.preview_id AND g.unit_id=u.id WHERE included AND """
                + included_group_sql()
                + """
 UNION ALL SELECT u.id,'AD',g.id,a.id FROM scope u JOIN planned_group g ON g.tenant_id=u.tenant_id AND g.preview_id=u.preview_id AND g.unit_id=u.id JOIN planned_ad a ON a.tenant_id=g.tenant_id AND a.preview_id=g.preview_id AND a.group_id=g.id WHERE included AND """
                + included_ad_sql()
                + """),
 results AS (
 SELECT o.kind, CASE
 WHEN """
                + resolved_sql("s")
                + """ THEN 'succeeded'
 WHEN nullif(trim(s.remote_id),'') IS NOT NULL THEN 'succeeded'
 WHEN s.status IN ('SUCCEEDED','UNKNOWN') THEN 'unknown'
 WHEN s.status='FAILED' OR fa.cta_failed
 OR (o.kind!='CAMPAIGN' AND fa.campaign_failed)
 OR (o.kind='AD' AND fg.group_id IS NOT NULL)
 OR (o.kind='CAMPAIGN' AND mu.unit_id IS NOT NULL)
 OR (o.kind IN ('ADGROUP','AD') AND mg.group_id IS NOT NULL) THEN 'failed'
 ELSE 'pending' END outcome
 FROM objects o LEFT JOIN execution_step s ON s.tenant_id=:tenant AND s.submission_id=:submission AND s.unit_id=o.unit_id AND s.kind=o.kind AND s.group_id IS NOT DISTINCT FROM o.group_id AND s.planned_ad_id IS NOT DISTINCT FROM o.ad_id
 LEFT JOIN failed_ancestors fa ON fa.unit_id=o.unit_id
 LEFT JOIN failed_adgroups fg ON fg.unit_id=o.unit_id AND fg.group_id=o.group_id
 LEFT JOIN material_failed_groups mg ON mg.unit_id=o.unit_id AND mg.group_id=o.group_id
 LEFT JOIN material_failed_units mu ON mu.unit_id=o.unit_id)
 SELECT kind,outcome,count(*) n FROM results GROUP BY kind,outcome"""
            ),
            params(row),
        )
        .mappings()
        .all()
    )
    counts = {
        key: ObjectCounts() for key in ["succeeded", "failed", "unknown", "pending"]
    }
    fields = {
        "CAMPAIGN": "campaign_count",
        "ADGROUP": "adgroup_count",
        "AD": "ad_count",
    }
    for result in outcome:
        setattr(counts[result["outcome"]], fields[result["kind"]], result["n"])
    stage = (
        SQLAlchemySession.execute(
            session,
            text(
                "SELECT kind,CASE WHEN "
                + resolved_sql("s")
                + " THEN 'VERIFIED_REPLACEMENT' ELSE status END status,count(*) n,bool_or(mismatch AND NOT "
                + resolved_sql("s")
                + ") mismatch FROM execution_step s WHERE tenant_id=:tenant AND submission_id=:submission AND NOT "
                + obsolete_readback_sql("s")
                + " GROUP BY kind,CASE WHEN "
                + resolved_sql("s")
                + " THEN 'VERIFIED_REPLACEMENT' ELSE status END"
            ),
            params(row),
        )
        .mappings()
        .all()
    )
    stage_counts = {f"{r['kind']}:{r['status']}": r["n"] for r in stage}
    states = {
        "SUCCEEDED" if r["status"] == "VERIFIED_REPLACEMENT" else r["status"]
        for r in stage
    }
    if any(r["mismatch"] for r in stage):
        states.add("MISMATCH")
    if not row.expanded:
        states.add("PENDING" if stage else "QUEUED")
    if any(getattr(counts["unknown"], name) for name in fields.values()):
        states.add("UNKNOWN")
    status = aggregate_status(states)
    failed_objects = sum(getattr(counts["failed"], field) for field in fields.values())
    pending_objects = sum(
        getattr(counts["pending"], field) for field in fields.values()
    )
    succeeded_objects = sum(
        getattr(counts["succeeded"], field) for field in fields.values()
    )
    if status != "NEEDS_REVIEW" and failed_objects and not pending_objects:
        # Descendants that can never run do not keep an all-failed task running
        # while the executor records their explicit dependency_failed facts.
        status = "PARTIAL" if succeeded_objects else "FAILED"
    if not stage and not row.expanded:
        status = "QUEUED"
    if row.expanded and scope["submitted_c"] == 0:
        status = "COMPLETED"
    planned = ObjectCounts(
        campaign_count=scope["planned_c"],
        adgroup_count=scope["planned_g"],
        ad_count=scope["planned_a"],
    )
    submitted = ObjectCounts(
        campaign_count=scope["submitted_c"],
        adgroup_count=scope["submitted_g"],
        ad_count=scope["submitted_a"],
    )
    excluded = ObjectCounts(
        **{
            name: getattr(planned, name) - getattr(submitted, name)
            for name in fields.values()
        }
    )
    from app.modules.builds.historical_read import historical_read_available
    from app.modules.builds.recovery import recovery_summary
    from app.modules.builds.submission_catalog import metadata

    recovery = recovery_summary(session, context=context, submission=row)
    budget_strategy = preview.config.get("budget_strategy", "SERIES")
    bid_strategy = frozen_bid_strategy(
        scene_snapshot={},
        preview_config=preview.config,
        target_roas=preview.target_roas,
    )
    from .continuation import submission_facts

    return SubmissionView(
        **submission_facts(session, row),
        corrected_ad_count=stage_counts.get("AD:VERIFIED_REPLACEMENT", 0),
        execution_route=execution_route_view(
            session, context=context, preview_id=row.preview_id
        ),
        recovery_mode=(
            "ORIGINAL_READ"
            if recovery.can_reconcile
            else "REAUTHORIZE_READ"
            if historical_read_available(session, context=context, submission_id=row.id)
            else "BLOCKED"
        ),
        error_code=row.error_code,
        **metadata(
            session, tenant_id=context.tenant_id, submission_id=row.id
        ).model_dump(),
        submission_id=row.id,
        preview_id=row.preview_id,
        draft_id=row.draft_id,
        batch_short_id=preview.batch_short_id,
        bc_id=row.bc_id,
        status=status,
        expanded=row.expanded,
        currency=preview.config["currency"],
        daily_budget_sum=str(
            preview.budget
            * (
                scope["submitted_g"]
                if budget_strategy == "ADGROUP"
                else scope["submitted_c"]
            )
        ),
        daily_budget_label=budget_strategy_label(budget_strategy),
        budget_strategy=budget_strategy,
        bid_strategy=bid_strategy,
        planned=planned,
        submitted=submitted,
        excluded=excluded,
        stage_counts=stage_counts,
        recovery=recovery,
        excluded_unit_count=scope["excluded_units"],
        drama_count=scope["dramas"],
        account_count=scope["accounts"],
        created_at=row.created_at,
        updated_at=row.updated_at,
        succeeded=counts["succeeded"],
        failed=counts["failed"],
        unknown=counts["unknown"],
        pending=counts["pending"],
    )


def _page_scope(
    context: TenantContext,
    row: Submission,
    kind: str,
    limit: int,
    cursor: str | None,
    filters: dict[str, str | None],
) -> tuple[dict[str, str | None], UUID | None]:
    from app.modules.accounts.resolver import decode_cursor

    if type(limit) is not int or not 1 <= limit <= 100:
        raise DomainError("invalid_page_size", "分页范围为1到100")
    scope = {
        "tenant": str(context.tenant_id),
        "bc": row.bc_id,
        "submission": str(row.id),
        "kind": kind,
        "limit": str(limit),
        **filters,
    }
    after = decode_cursor(cursor, scope=scope)
    try:
        identity = UUID(after) if after else None
    except ValueError, TypeError, AttributeError:
        raise DomainError("invalid_cursor", "分页游标无效") from None
    return scope, identity


def get_submission_units(
    session: Session,
    *,
    context: TenantContext,
    submission_id: UUID,
    cursor: str | None = None,
    limit: int = 50,
    advertiser_id: str | None = None,
    drama_id: UUID | None = None,
    excluded_only: bool = False,
) -> Page[SubmissionUnitPublic]:
    from app.core.pagination import Page
    from app.modules.accounts.resolver import encode_cursor
    from app.modules.builds.execution_schemas import SubmissionUnitPublic

    authorize(session, context)
    row = submission_row(session, context, submission_id)
    scope, after = _page_scope(
        context,
        row,
        "units",
        limit,
        cursor,
        {
            "advertiser": advertiser_id,
            "drama": str(drama_id) if drama_id else None,
            "excluded": str(excluded_only),
        },
    )
    page_params = dict(
        params(row),
        after=str(after) if after else None,
        advertiser=advertiser_id,
        drama=str(drama_id) if drama_id else None,
        excluded=excluded_only,
        limit=limit + 1,
    )
    total = int(
        SQLAlchemySession.execute(
            session,
            text(
                SCOPE_CTE
                + """SELECT count(*) FROM scope u
WHERE (CAST(:advertiser AS text) IS NULL OR u.advertiser_id=:advertiser)
AND (CAST(:drama AS uuid) IS NULL OR u.drama_id=CAST(:drama AS uuid))
AND (NOT :excluded OR NOT u.included)"""
            ),
            page_params,
        ).scalar_one()
    )
    results = (
        SQLAlchemySession.execute(
            session,
            text(
                SCOPE_CTE
                + """SELECT u.id,u.drama_id,d.title,u.advertiser_id,u.campaign_name,u.included,u.covered,u.reason_codes,
 coalesce(su.expanded,false) expanded
 FROM scope u JOIN preview_drama d ON d.tenant_id=u.tenant_id AND d.preview_id=u.preview_id AND d.drama_id=u.drama_id
 LEFT JOIN submission_unit su ON su.tenant_id=u.tenant_id AND su.submission_id=:submission AND su.unit_id=u.id
 WHERE (CAST(:after AS uuid) IS NULL OR u.id>CAST(:after AS uuid))
 AND (CAST(:advertiser AS text) IS NULL OR u.advertiser_id=:advertiser)
 AND (CAST(:drama AS uuid) IS NULL OR u.drama_id=CAST(:drama AS uuid))
 AND (NOT :excluded OR NOT u.included) ORDER BY u.id LIMIT :limit"""
            ),
            page_params,
        )
        .mappings()
        .all()
    )
    items = [
        SubmissionUnitPublic(
            unit_id=r["id"],
            drama_id=r["drama_id"],
            title=r["title"],
            advertiser_id=r["advertiser_id"],
            campaign_name=r["campaign_name"],
            disposition="INCLUDED" if r["included"] else "EXCLUDED",
            reason_codes=[]
            if r["included"]
            else (
                ["previous_submission"]
                if r["covered"]
                else (r["reason_codes"] or ["preview_blocked"])
            ),
            expanded=r["expanded"],
        )
        for r in results[:limit]
    ]
    from app.modules.builds.submission_catalog import enrich_units

    enrich_units(
        session,
        context=context,
        submission_id=row.id,
        preview_id=row.preview_id,
        items=items,
    )
    return Page(
        items=items,
        next_cursor=encode_cursor(scope=scope, last_id=str(items[-1].unit_id))
        if len(results) > limit
        else None,
        total=total,
    )


def get_submission_steps(
    session: Session,
    *,
    context: TenantContext,
    submission_id: UUID,
    cursor: str | None = None,
    limit: int = 50,
    advertiser_id: str | None = None,
    drama_id: UUID | None = None,
    kind: str | None = None,
    result: str | None = None,
) -> Page[StepPublic]:
    from app.core.pagination import Page, count_rows
    from app.modules.accounts.resolver import encode_cursor
    from app.modules.builds.execution_schemas import StepPublic
    from app.modules.builds.receipt_completion import obsolete_readback_sql

    authorize(session, context)
    row = submission_row(session, context, submission_id)
    if kind is not None and kind not in {
        "MATERIAL",
        "CTA",
        "CAMPAIGN",
        "ADGROUP",
        "AD",
        "READBACK",
    }:
        raise DomainError("invalid_resolve_request", "步骤种类无效")
    if result is not None and result not in {
        "QUEUED",
        "PENDING",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "RETRYABLE",
        "UNKNOWN",
        "MISMATCH",
    }:
        raise DomainError("invalid_resolve_request", "步骤结果无效")
    scope, after = _page_scope(
        context,
        row,
        "steps",
        limit,
        cursor,
        {
            "advertiser": advertiser_id,
            "drama": str(drama_id) if drama_id else None,
            "stepkind": kind,
            "result": result,
        },
    )
    stmt = (
        select(ExecutionStep)
        .join(
            BuildUnit,
            (col(BuildUnit.id) == ExecutionStep.unit_id)
            & (col(BuildUnit.tenant_id) == ExecutionStep.tenant_id),
        )
        .where(
            ExecutionStep.tenant_id == row.tenant_id,
            ExecutionStep.submission_id == row.id,
            text("NOT " + obsolete_readback_sql("execution_step")),
        )
    )
    if advertiser_id:
        stmt = stmt.where(BuildUnit.advertiser_id == advertiser_id)
    if drama_id:
        stmt = stmt.where(BuildUnit.drama_id == drama_id)
    if kind:
        stmt = stmt.where(ExecutionStep.kind == kind)
    if result == "MISMATCH":
        from app.modules.builds.corrections import resolved_sql

        stmt = stmt.where(
            col(ExecutionStep.mismatch).is_(True),
            text("NOT " + resolved_sql("execution_step")),
        )
    elif result:
        from app.modules.builds.corrections import resolved_sql

        resolved = resolved_sql("execution_step")
        if result == "SUCCEEDED":
            stmt = stmt.where(
                text("(execution_step.status='SUCCEEDED' OR " + resolved + ")")
            )
        else:
            stmt = stmt.where(ExecutionStep.status == result, text("NOT " + resolved))
    total = count_rows(session, stmt)
    if after:
        stmt = stmt.where(ExecutionStep.id > after)
    rows = session.exec(stmt.order_by(col(ExecutionStep.id)).limit(limit + 1)).all()
    items = [
        StepPublic(
            step_id=s.id,
            unit_id=s.unit_id,
            kind=s.kind,
            group_id=s.group_id,
            planned_ad_id=s.planned_ad_id,
            material_id=s.material_id,
            status=s.status,
            remote_id=s.remote_id,
            error_code=s.error_code,
            operation_status=s.operation_status,
            review_status=s.review_status,
            mismatch=s.mismatch,
            checked_at=s.checked_at,
        )
        for s in rows[:limit]
    ]
    from app.modules.builds.submission_catalog import enrich_steps

    enrich_steps(session, context=context, submission_id=row.id, items=items)
    return Page(
        items=items,
        next_cursor=encode_cursor(scope=scope, last_id=str(items[-1].step_id))
        if len(rows) > limit
        else None,
        total=total,
    )


# 与展开及最终 AD 围栏使用同一保留集合，避免等待根本不会创建的素材步骤。
GROUP_MATERIAL_SQL = """
SELECT g.id group_id,g.unit_id,
 count(m.material_id)>0 AND coalesce(bool_and(ms.status='SUCCEEDED'),false) AND count(m.material_id)=count(ms.id) ready,
 coalesce(bool_or(ms.status='FAILED'),false) failed
FROM planned_group g
LEFT JOIN frozen_group_material m ON m.tenant_id=g.tenant_id AND m.preview_id=g.preview_id AND m.drama_id=g.drama_id AND m.group_no=g.group_no
AND NOT EXISTS (SELECT 1 FROM preview_skipped_material skipped
 WHERE skipped.tenant_id=m.tenant_id AND skipped.unit_id=g.unit_id AND skipped.material_id=m.material_id)
LEFT JOIN execution_step ms ON ms.tenant_id=g.tenant_id AND ms.submission_id=:submission AND ms.unit_id=g.unit_id AND ms.kind='MATERIAL' AND ms.material_id=m.material_id
WHERE g.tenant_id=:tenant AND g.preview_id=:preview AND (CAST(:unit AS uuid) IS NULL OR g.unit_id=CAST(:unit AS uuid))
GROUP BY g.id,g.unit_id
"""


def group_material_state(
    session: Session,
    *,
    context: TenantContext,
    submission_id: UUID,
    unit_id: UUID,
    group_id: UUID | None = None,
) -> str:
    """One group: all materials ready; whole unit: any complete group ready."""
    authorize(session, context)
    row = submission_row(session, context, submission_id)
    from .creative_units import preview_contract

    if (
        preview_contract(
            session, tenant_id=context.tenant_id, preview_id=row.preview_id
        ).media_kind
        == "CAROUSEL"
    ):
        from .ad_readiness import carousel_material_state

        return carousel_material_state(
            session,
            tenant_id=context.tenant_id,
            preview_id=row.preview_id,
            submission_id=submission_id,
            unit_id=unit_id,
            group_id=group_id,
        )
    value = (
        SQLAlchemySession.execute(
            session,
            text(
                "WITH material_groups AS ("
                + GROUP_MATERIAL_SQL
                + """
    ) SELECT coalesce(bool_or(ready),false) ready,coalesce(bool_and(failed),false) failed
    FROM material_groups WHERE CAST(:group AS uuid) IS NULL OR group_id=CAST(:group AS uuid)
    """
            ),
            dict(params(row), unit=unit_id, group=group_id),
        )
        .mappings()
        .one()
    )
    return "READY" if value["ready"] else "FAILED" if value["failed"] else "PENDING"
