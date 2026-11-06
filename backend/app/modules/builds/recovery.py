"""Permanent recovery requests; bounded local scheduling, never provider calls."""

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.accounts.access import resolve_account_access
from app.modules.accounts.models import TenantBC
from app.modules.accounts.routing import verify_route
from app.modules.builds.corrections import resolved_sql
from app.modules.builds.execution_models import (
    ExecutionStep,
    Submission,
    SubmissionUnit,
)
from app.modules.builds.execution_schemas import Recovery
from app.modules.builds.execution_state import (
    CONFIRMED_ABSENT,
    UNARMED_RETRY,
    UNSENT_ATTEMPT,
    confirmed_absent_attempt,
    evidence,
    safely_unsent_attempt,
)
from app.modules.builds.preview_models import BuildUnit
from app.modules.builds.receipt_completion import obsolete_readback_sql
from app.modules.builds.recovery_models import (
    RecoveryKind,
    RecoveryReceipt,
    SubmissionRecovery,
    SubmissionRecoveryRequest,
)
from app.modules.builds.routes import load_preview_route, verify_unit_route
from app.modules.tenants.permissions import require_tenant

from .ad_readiness import included_ad_sql
from .continuation import superseded_sql

TASK_NAME = "builds.recover_submission"
register_dispatch_task(TASK_NAME, "control")
PAGE_SIZE = 100
LEASE_SECONDS = 90
REPAIR_SECONDS = 120

# These are repairs of frozen input, not retryable transport failures.
INTENT_ERRORS = "'scene_no_longer_supported','new_preview_required','copy_too_long','blank_copy','invalid_copy'"
COVER_SCOPE = """c.id=s.cover_job_id AND c.tenant_id=s.tenant_id AND c.bc_id=s.bc_id AND c.superseded_by_id IS NULL
AND c.material_id=s.material_id AND c.advertiser_id=u.advertiser_id AND c.connection_id=u.connection_id"""
COVER_RETRY = f"""EXISTS (SELECT 1 FROM material_cover_job c WHERE {COVER_SCOPE}
AND (c.status='BLOCKED' OR (s.status='FAILED' AND c.status='PENDING'))
AND c.request_armed_at IS NULL AND c.known_image_id IS NULL
AND c.dispatch_id IS NULL AND (c.claimed_until IS NULL OR c.claimed_until <= :now)
AND NOT EXISTS (SELECT 1 FROM material_cover_receipt cr WHERE cr.tenant_id=c.tenant_id AND cr.job_id=c.id))"""
BASE = """
FROM execution_step s JOIN build_unit u ON u.tenant_id=s.tenant_id AND u.preview_id=s.preview_id AND u.id=s.unit_id
JOIN submission_unit su ON su.tenant_id=s.tenant_id AND su.submission_id=s.submission_id AND su.unit_id=s.unit_id
WHERE s.tenant_id=:tenant AND s.submission_id=:submission AND su.expanded AND su.disposition='INCLUDED'
AND s.dispatch_id IS NULL AND (s.lease_expires_at IS NULL OR s.lease_expires_at <= :now)
"""
ACCOUNT = """
AND EXISTS (SELECT 1 FROM bc_account_access a
 JOIN tiktok_connection c ON c.tenant_id=a.tenant_id AND c.id=a.connection_id
 JOIN tenant_bc b ON b.tenant_id=a.tenant_id AND b.bc_id=a.bc_id
 JOIN advertiser_account aa ON aa.tenant_id=a.tenant_id AND aa.advertiser_id=a.advertiser_id
 JOIN build_route_context r ON r.tenant_id=s.tenant_id AND r.preview_id=s.preview_id AND r.connection_id=a.connection_id
 JOIN bc_connection_binding binding ON binding.tenant_id=r.tenant_id AND binding.bc_id=r.bc_id AND binding.connection_id=r.connection_id AND binding.kind=r.channel AND binding.status='ACTIVE' AND binding.revision=r.binding_revision
 JOIN connection_authorization auth ON auth.tenant_id=r.tenant_id AND auth.connection_id=r.connection_id AND auth.authorization_revision=r.authorization_revision
 WHERE a.connection_id=u.connection_id AND c.kind=r.channel AND c.authorization_revision=r.authorization_revision AND c.adapter_contract_revision=r.adapter_contract_revision
 AND auth.source<>'UNKNOWN' AND auth.permission_summary->'build_authorized'='true'::jsonb AND jsonb_array_length(auth.scopes)>0
 AND auth.verified_at <= :now AND a.checked_at <= :now
 AND a.tenant_id=s.tenant_id AND a.bc_id=s.bc_id AND a.advertiser_id=u.advertiser_id
 AND a.in_bc AND a.authorized AND a.active AND a.can_build AND a.permission_state='VERIFIED'
 AND c.status='ACTIVE' AND NOT b.ownership_conflict AND NOT aa.ownership_conflict
 AND aa.remote_status IN ('ENABLE','STATUS_ENABLE') AND trim(aa.currency)<>'' AND trim(aa.timezone)<>''
 AND aa.currency=u.currency AND aa.timezone=u.timezone)
"""
# 恢复也只等待该账户实际保留的素材；已排除素材不再是冻结组的依赖。
GROUP_READY = """EXISTS (SELECT 1 FROM planned_group g
 WHERE g.tenant_id=s.tenant_id AND g.preview_id=s.preview_id AND g.unit_id=s.unit_id
 AND (s.kind='CAMPAIGN' OR g.id=s.group_id)
 AND EXISTS (SELECT 1 FROM frozen_group_material gm WHERE gm.tenant_id=g.tenant_id AND gm.preview_id=g.preview_id AND gm.drama_id=g.drama_id AND gm.group_no=g.group_no
 AND NOT EXISTS (SELECT 1 FROM preview_skipped_material skipped WHERE skipped.tenant_id=gm.tenant_id AND skipped.unit_id=g.unit_id AND skipped.material_id=gm.material_id))
 AND NOT EXISTS (SELECT 1 FROM frozen_group_material gm
 WHERE gm.tenant_id=g.tenant_id AND gm.preview_id=g.preview_id AND gm.drama_id=g.drama_id AND gm.group_no=g.group_no
 AND NOT EXISTS (SELECT 1 FROM preview_skipped_material skipped WHERE skipped.tenant_id=gm.tenant_id AND skipped.unit_id=g.unit_id AND skipped.material_id=gm.material_id)
 AND NOT EXISTS (SELECT 1 FROM execution_step ms WHERE ms.tenant_id=s.tenant_id AND ms.submission_id=s.submission_id AND ms.unit_id=s.unit_id AND ms.kind='MATERIAL' AND ms.material_id=gm.material_id AND ms.status='SUCCEEDED')))
"""
# CAROUSEL 的共享父对象只等一条完整广告；原 VIDEO 组屏障保持。

CAROUSEL_READY = (
    """EXISTS (SELECT 1 FROM planned_group g JOIN planned_ad a ON a.tenant_id=g.tenant_id AND a.preview_id=g.preview_id AND a.group_id=g.id
 WHERE g.tenant_id=s.tenant_id AND g.preview_id=s.preview_id AND g.unit_id=s.unit_id
 AND (s.kind='CAMPAIGN' OR g.id=s.group_id) AND (s.kind<>'AD' OR a.id=s.planned_ad_id)
 AND """
    + included_ad_sql()
    + """
 AND EXISTS (SELECT 1 FROM frozen_ad_material m WHERE m.tenant_id=a.tenant_id AND m.preview_id=a.preview_id AND m.drama_id=g.drama_id AND m.group_no=g.group_no AND m.base_ad_no=a.base_ad_no)
 AND NOT EXISTS (SELECT 1 FROM frozen_ad_material m WHERE m.tenant_id=a.tenant_id AND m.preview_id=a.preview_id AND m.drama_id=g.drama_id AND m.group_no=g.group_no AND m.base_ad_no=a.base_ad_no
 AND NOT EXISTS (SELECT 1 FROM execution_step ms WHERE ms.tenant_id=s.tenant_id AND ms.submission_id=s.submission_id AND ms.unit_id=s.unit_id AND ms.kind='MATERIAL' AND ms.material_id=m.material_id AND ms.status='SUCCEEDED')))"""
)
IS_CAROUSEL = "EXISTS (SELECT 1 FROM preview_contract pc WHERE pc.tenant_id=s.tenant_id AND pc.preview_id=s.preview_id AND pc.media_kind='CAROUSEL')"
GROUP_READY = f"(CASE WHEN {IS_CAROUSEL} THEN {CAROUSEL_READY} ELSE {GROUP_READY} END)"
READ_ACCOUNT = """
AND EXISTS (SELECT 1 FROM bc_account_access a
 JOIN tiktok_connection c ON c.tenant_id=a.tenant_id AND c.id=a.connection_id
 JOIN tenant_bc b ON b.tenant_id=a.tenant_id AND b.bc_id=a.bc_id
 JOIN advertiser_account aa ON aa.tenant_id=a.tenant_id AND aa.advertiser_id=a.advertiser_id
 JOIN build_route_context r ON r.tenant_id=s.tenant_id AND r.preview_id=s.preview_id AND r.connection_id=a.connection_id
 JOIN bc_connection_binding binding ON binding.tenant_id=r.tenant_id AND binding.bc_id=r.bc_id AND binding.connection_id=r.connection_id AND binding.kind=r.channel AND binding.status='ACTIVE' AND binding.revision=r.binding_revision
 JOIN connection_authorization auth ON auth.tenant_id=r.tenant_id AND auth.connection_id=r.connection_id AND auth.authorization_revision=r.authorization_revision
 WHERE a.connection_id=u.connection_id AND c.kind=r.channel AND c.authorization_revision=r.authorization_revision AND c.adapter_contract_revision=r.adapter_contract_revision
 AND auth.source<>'UNKNOWN' AND auth.permission_summary->'read_authorized'='true'::jsonb
 AND auth.verified_at <= :now AND a.checked_at <= :now
 AND a.tenant_id=s.tenant_id AND a.bc_id=s.bc_id AND a.advertiser_id=u.advertiser_id
 AND a.in_bc AND a.authorized AND a.active
 AND c.status='ACTIVE' AND NOT b.ownership_conflict AND NOT aa.ownership_conflict
 AND trim(aa.currency)<>'' AND trim(aa.timezone)<>'')
"""
DEPENDENCIES_READY = f"""
AND (s.parent_step_id IS NULL OR EXISTS (SELECT 1 FROM execution_step p WHERE p.tenant_id=s.tenant_id AND p.submission_id=s.submission_id AND p.id=s.parent_step_id AND p.status='SUCCEEDED' AND p.remote_id IS NOT NULL))
AND (s.kind<>'CAMPAIGN' OR EXISTS (SELECT 1 FROM execution_step cta WHERE cta.tenant_id=s.tenant_id AND cta.submission_id=s.submission_id AND cta.unit_id=s.unit_id AND cta.kind='CTA' AND cta.status='SUCCEEDED' AND cta.remote_id IS NOT NULL))
AND (s.kind NOT IN ('CAMPAIGN','ADGROUP') OR {GROUP_READY})
AND (s.kind<>'AD' OR NOT {IS_CAROUSEL} OR {CAROUSEL_READY})
"""

# 只有原目标已确定未生效且依赖已结束，显式重试才可创建新的准备代数。
# failed_infos 是按目标/MID 的负证据；批次 ACK 本身既不是成功也不是失败。
MATERIAL_RETRY = """(s.cover_job_id IS NULL AND NOT s.resolved ? 'mapping' AND EXISTS (
 SELECT 1 FROM material_distribution d JOIN material_asset_operation o
 ON o.id=d.operation_id AND o.tenant_id=d.tenant_id AND o.bc_id=d.bc_id
 AND o.material_id=d.material_id AND o.advertiser_id=d.advertiser_id
 JOIN material_file f ON f.id=d.material_id AND f.tenant_id=d.tenant_id
 WHERE d.id=s.distribution_id AND d.tenant_id=s.tenant_id AND d.bc_id=s.bc_id
 AND d.superseded_by_id IS NULL AND o.superseded_by_id IS NULL
 AND d.material_id=s.material_id AND d.advertiser_id=u.advertiser_id
 AND d.status='blocked' AND o.status='failed' AND d.path=o.path
 AND d.target_route=o.frozen_route AND d.target_route->>'connection_id'=u.connection_id::text
 AND o.remote_response->'definite_no_effect'='true'::jsonb
 AND o.attempt_token IS NULL AND (o.claimed_until IS NULL OR o.claimed_until<=:now)
 AND NOT o.remote_response ?| ARRAY['video_id','mid','upload_video_id','verified_upload_video_id','conflicting_video_id']
 AND coalesce(o.remote_response->'candidates','[]'::jsonb)='[]'::jsonb
 AND o.remote_response->'read_only' IS DISTINCT FROM 'true'::jsonb
 AND o.remote_response->'share_acknowledged' IS DISTINCT FROM 'true'::jsonb
 AND o.remote_response->>'content_md5'=f.video_md5 AND length(f.video_md5)=32
 AND (NOT o.remote_response ? 'share_receipt' OR
      coalesce(o.remote_response->'share_receipt'->'failed_infos'->u.advertiser_id ? (o.remote_response->>'source_mid'),false))
 AND (o.remote_response->'send_armed' IS DISTINCT FROM 'true'::jsonb OR
      coalesce(o.remote_response->'share_receipt'->'failed_infos'->u.advertiser_id ? (o.remote_response->>'source_mid'),false) OR
      EXISTS (SELECT 1 FROM material_share_batch_member bm JOIN material_share_batch_receipt br
       ON br.tenant_id=bm.tenant_id AND br.bc_id=bm.bc_id AND br.batch_id=bm.batch_id
       WHERE bm.tenant_id=o.tenant_id AND bm.operation_id=o.id AND bm.distribution_id=d.id
       AND bm.status='failed' AND br.effect='ACKNOWLEDGED'
       AND br.share_response->'failed_infos'->u.advertiser_id ? bm.source_mid))
 AND NOT EXISTS (SELECT 1 FROM material_share_batch_member bm
  WHERE bm.tenant_id=o.tenant_id AND bm.operation_id=o.id
  AND (bm.distribution_id<>d.id OR bm.status NOT IN ('failed','not_sent') OR EXISTS (
   SELECT 1 FROM material_share_batch_receipt br WHERE br.tenant_id=bm.tenant_id AND br.batch_id=bm.batch_id
   AND NOT (br.effect IN ('FAILED','NOT_SENT') OR (br.effect='ACKNOWLEDGED'
    AND coalesce(br.share_response->'failed_infos'->u.advertiser_id ? bm.source_mid,false))))))
 AND NOT EXISTS (SELECT 1 FROM material_upload_attempt a WHERE a.tenant_id=o.tenant_id AND a.operation_id=o.id)
 AND NOT EXISTS (SELECT 1 FROM account_material a WHERE a.tenant_id=d.tenant_id AND a.bc_id=d.bc_id
  AND a.material_id=d.material_id AND a.advertiser_id=d.advertiser_id AND trim(a.video_id)<>'')
 AND NOT EXISTS (SELECT 1 FROM material_asset_operation other WHERE other.tenant_id=o.tenant_id
  AND other.bc_id=o.bc_id AND other.material_id=o.material_id AND other.advertiser_id=o.advertiser_id
  AND other.id<>o.id AND other.status<>'failed' AND other.superseded_by_id IS NULL)
 AND NOT EXISTS (SELECT 1 FROM material_distribution other WHERE other.tenant_id=d.tenant_id
  AND other.bc_id=d.bc_id AND other.material_id=d.material_id AND other.advertiser_id=d.advertiser_id
  AND other.id<>d.id AND other.status<>'blocked' AND other.superseded_by_id IS NULL)
 AND NOT EXISTS (SELECT 1 FROM pending_dispatch pd WHERE pd.tenant_id=d.tenant_id AND pd.published_at IS NULL
  AND pd.superseded_at IS NULL AND pd.retired_at IS NULL
  AND (pd.payload->>'distribution_id'=d.id::text OR pd.payload->>'operation_id'=o.id::text))
))"""
IMAGE_MATERIAL_RETRY = """(s.cover_job_id IS NULL AND NOT s.resolved ? 'mapping' AND EXISTS (
 SELECT 1 FROM material_distribution d JOIN material_asset_operation o ON o.id=d.operation_id AND o.tenant_id=d.tenant_id
 JOIN material_file f ON f.id=d.material_id AND f.tenant_id=d.tenant_id AND f.media_type='IMAGE'
 WHERE d.id=s.distribution_id AND d.tenant_id=s.tenant_id AND d.bc_id=s.bc_id AND d.material_id=s.material_id
 AND d.advertiser_id=u.advertiser_id AND d.target_route=o.frozen_route AND d.target_route->>'connection_id'=u.connection_id::text
 AND d.superseded_by_id IS NULL AND o.superseded_by_id IS NULL
 AND d.status='blocked' AND o.status='failed' AND d.path=o.path
 AND o.attempt_token IS NULL AND o.claimed_until IS NULL
 AND o.remote_response->'send_armed'='false'::jsonb
 AND o.remote_response->'source_ingest' IS DISTINCT FROM 'true'::jsonb
 AND NOT o.remote_response ? 'image_id' AND coalesce(o.remote_response->'candidates','[]'::jsonb)='[]'::jsonb
 AND o.remote_response->'share_acknowledged' IS DISTINCT FROM 'true'::jsonb
 AND NOT EXISTS (SELECT 1 FROM pending_dispatch pd WHERE pd.tenant_id=d.tenant_id AND pd.published_at IS NULL
 AND pd.superseded_at IS NULL AND pd.retired_at IS NULL
 AND (pd.payload->>'distribution_id'=d.id::text OR pd.payload->>'operation_id'=o.id::text))))"""
MATERIAL_RETRY = f"({MATERIAL_RETRY} OR {IMAGE_MATERIAL_RETRY})"

RETRY = f"""
AND ((s.status IN ('FAILED','RETRYABLE') OR (
 s.status='PENDING' AND s.kind='MATERIAL'
 AND s.error_code IN ('cover_pending','material_pending','execution_window_wait')
 AND {COVER_RETRY})) AND s.remote_id IS NULL
AND s.phase<>'REQUEST_ARMED' AND s.kind<>'READBACK' AND coalesce(s.error_code,'') NOT IN ({INTENT_ERRORS})
AND (({UNARMED_RETRY})
 OR (s.phase='DONE' AND {UNSENT_ATTEMPT}))
AND (s.kind<>'MATERIAL' OR (s.cover_job_id IS NULL AND s.distribution_id IS NULL) OR {COVER_RETRY} OR {MATERIAL_RETRY})
 OR (s.status='UNKNOWN' AND {CONFIRMED_ABSENT}))
{DEPENDENCIES_READY}
"""
# 持久等待步骤不必先消耗一次执行才能暴露依赖失败；显式批次重试可直接
# 恢复其确定未发送的封面。仍复用全部原权限、活动投递和外部效果栅栏，
# 不能把一般PENDING、已发送封面或未知视频扩展成新的上传许可。
# Two disjoint candidate branches avoid evaluating a correlated parent query
# against every pending step. Keep these fixed predicates identical to the
# partial indexes, including for PostgreSQL generic prepared plans.
RECONCILE_CANDIDATES = """(
 SELECT candidate.id FROM execution_step candidate
 WHERE candidate.tenant_id=:tenant AND candidate.submission_id=:submission
 AND candidate.dispatch_id IS NULL
 AND (candidate.status='UNKNOWN' OR candidate.mismatch
      OR (candidate.kind='MATERIAL' AND candidate.status='FAILED'))
 UNION ALL
 SELECT child.id FROM execution_step parent JOIN execution_step child
 ON child.tenant_id=parent.tenant_id AND child.submission_id=parent.submission_id
 AND child.parent_step_id=parent.id
 WHERE parent.tenant_id=:tenant AND parent.submission_id=:submission
 AND parent.remote_id IS NOT NULL AND child.dispatch_id IS NULL
 AND child.kind='READBACK' AND child.status<>'SUCCEEDED'
 AND child.status<>'UNKNOWN' AND NOT child.mismatch
) candidates JOIN execution_step s ON s.id=candidates.id"""
RETRY += " AND NOT " + resolved_sql("s")
RETRY += " AND NOT " + superseded_sql()
RECONCILE = f"""
AND NOT {resolved_sql("s")}
AND NOT {obsolete_readback_sql("s")}
AND (s.kind<>'MATERIAL' OR (s.cover_job_id IS NULL AND EXISTS (SELECT 1 FROM material_distribution d JOIN material_asset_operation o
 ON o.id=d.operation_id AND o.tenant_id=d.tenant_id AND o.bc_id=d.bc_id AND o.material_id=d.material_id AND o.advertiser_id=d.advertiser_id
 WHERE d.id=s.distribution_id AND d.tenant_id=s.tenant_id AND d.bc_id=s.bc_id AND d.material_id=s.material_id AND d.advertiser_id=u.advertiser_id
 AND d.superseded_by_id IS NULL AND o.superseded_by_id IS NULL
 AND d.status IN ('result_unknown','verifying','preparing','ready','blocked') AND o.status IN ('sending','result_unknown','verifying','succeeded')))
 OR EXISTS (SELECT 1 FROM material_cover_job c WHERE {COVER_SCOPE}
 AND c.dispatch_id IS NULL AND (c.claimed_until IS NULL OR c.claimed_until <= :now)
 AND (c.request_armed_at IS NOT NULL OR c.known_image_id IS NOT NULL
 OR EXISTS (SELECT 1 FROM material_cover_receipt cr WHERE cr.tenant_id=c.tenant_id AND cr.job_id=c.id))))
"""


def _params(row: Submission) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "tenant": row.tenant_id,
        "submission": row.id,
        "now": now,
    }


def _query(_row: Submission, kind: str, *, account: bool = True) -> str:
    base = BASE
    if kind != "RETRY":
        base = base.replace("execution_step s", RECONCILE_CANDIDATES, 1)
    return (
        base
        + (RETRY if kind == "RETRY" else RECONCILE)
        + ((ACCOUNT if kind == "RETRY" else READ_ACCOUNT) if account else "")
    )


def _submission(session: Session, context: TenantContext, identity: UUID) -> Submission:
    row = session.exec(
        select(Submission).where(
            Submission.id == identity, Submission.tenant_id == context.tenant_id
        )
    ).one_or_none()
    if row is None:
        raise DomainError("resource_not_found", "提交不存在")
    return row


def _authority(
    session: Session, context: TenantContext, row: Submission
) -> TenantContext:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="build"
    )
    original = require_tenant(
        session, actor_id=row.actor_id, tenant_id=row.tenant_id, action="build"
    )
    bc = session.get(TenantBC, (row.tenant_id, row.bc_id), populate_existing=True)
    if bc is None or bc.ownership_conflict:
        raise DomainError("account_ownership_conflict", "提交 BC 当前不可操作")
    route = load_preview_route(session, context=original, preview_id=row.preview_id)
    verify_route(
        session, context=original, route=route, advertiser_id=None, capability="read"
    )
    return original


def _receipt(
    row: SubmissionRecovery | SubmissionRecoveryRequest, *, original: bool
) -> RecoveryReceipt:
    return RecoveryReceipt.model_validate(
        {
            "recovery_id": row.recovery_id
            if isinstance(row, SubmissionRecoveryRequest)
            else row.id,
            "request_id": row.request_id,
            "submission_id": row.submission_id,
            "kind": row.kind,
            "state": "QUEUED" if original else cast(SubmissionRecovery, row).state,
            "scheduled_count": 0
            if original
            else cast(SubmissionRecovery, row).scheduled_count,
            "reason_code": None
            if original
            else cast(SubmissionRecovery, row).reason_code,
        }
    )


def get_request(
    session: Session, *, context: TenantContext, request_id: UUID
) -> RecoveryReceipt:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    row = session.get(SubmissionRecoveryRequest, (context.tenant_id, request_id))
    if row is None:
        raise DomainError("resource_not_found", "恢复请求不存在")
    return _receipt(row, original=True)


def get_recovery(
    session: Session, *, context: TenantContext, recovery_id: UUID
) -> RecoveryReceipt:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    row = session.exec(
        select(SubmissionRecovery).where(
            SubmissionRecovery.tenant_id == context.tenant_id,
            SubmissionRecovery.id == recovery_id,
        )
    ).one_or_none()
    if row is None:
        raise DomainError("resource_not_found", "恢复任务不存在")
    return _receipt(row, original=False)


def queue_recovery(
    session: Session, job: SubmissionRecovery, *, delay: int = 0
) -> None:
    job.due_at = datetime.now(UTC) + timedelta(seconds=delay)
    job.dispatch_id = enqueue_after_commit(
        session,
        context=TenantContext(
            tenant_id=job.tenant_id, actor_id=job.request_actor_id, role="operator"
        ),
        task_name=TASK_NAME,
        task_key=f"submission-recovery:{job.id}:{job.dispatch_revision}",
        payload={"recovery_id": str(job.id), "revision": job.dispatch_revision},
    )
    dispatch = session.get(PendingDispatch, job.dispatch_id)
    assert dispatch
    dispatch.available_at = job.due_at
    job.repair_after = job.due_at + timedelta(seconds=REPAIR_SECONDS)
    session.add_all([job, dispatch])


def request_recovery(
    session: Session,
    *,
    context: TenantContext,
    submission_id: UUID,
    request_id: UUID,
    kind: RecoveryKind,
) -> RecoveryReceipt:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    SASession.execute(
        session,
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
        {"key": f"build-recovery:{context.tenant_id}:{request_id}"},
    )
    old = session.get(SubmissionRecoveryRequest, (context.tenant_id, request_id))
    if old:
        if old.submission_id != submission_id or old.kind != kind:
            raise DomainError("idempotency_conflict", "恢复请求已绑定其他意图")
        return _receipt(old, original=True)
    row = _submission(session, context, submission_id)
    _authority(session, context, row)
    if not SASession.execute(
        session, text("SELECT EXISTS(SELECT 1 " + _query(row, kind) + ")"), _params(row)
    ).scalar_one():
        raise DomainError("recovery_no_candidates", "当前没有可安全恢复的步骤")
    job = SubmissionRecovery(
        tenant_id=row.tenant_id,
        submission_id=row.id,
        preview_id=row.preview_id,
        bc_id=row.bc_id,
        request_id=request_id,
        request_actor_id=context.actor_id,
        kind=kind,
    )
    session.add(job)
    session.flush()
    ledger = SubmissionRecoveryRequest(
        tenant_id=row.tenant_id,
        request_id=request_id,
        recovery_id=job.id,
        submission_id=row.id,
        bc_id=row.bc_id,
        kind=kind,
    )
    session.add(ledger)
    queue_recovery(session, job)
    session.flush()
    return _receipt(ledger, original=True)


def recovery_summary(
    session: Session, *, context: TenantContext, submission: Submission
) -> Recovery:
    try:
        _authority(session, context, submission)
    except DomainError as error:
        return Recovery(reasons=[error.code])
    params = _params(submission)
    values = (
        SASession.execute(
            session,
            text(
                "SELECT (SELECT count(*) "
                + _query(submission, "RETRY")
                + ") retryable, (SELECT count(*) "
                + _query(submission, "RECONCILE")
                + ") reconcilable"
            ),
            params,
        )
        .mappings()
        .one()
    )
    counts = int(values["retryable"]), int(values["reconcilable"])
    reasons: list[str] = []
    if not any(counts):
        blocked = SASession.execute(
            session,
            text(
                "SELECT EXISTS(SELECT 1 "
                + _query(submission, "RETRY", account=False)
                + ") OR EXISTS(SELECT 1 "
                + _query(submission, "RECONCILE", account=False)
                + ")"
            ),
            params,
        ).scalar_one()
        reasons = ["account_access_denied" if blocked else "recovery_no_candidates"]
    return Recovery(
        retryable_step_count=counts[0],
        reconcilable_step_count=counts[1],
        can_retry=counts[0] > 0,
        can_reconcile=counts[1] > 0,
        reasons=reasons,
    )


def _job(
    session: Session, context: TenantContext, identity: UUID
) -> SubmissionRecovery:
    row = session.exec(
        select(SubmissionRecovery)
        .where(
            SubmissionRecovery.tenant_id == context.tenant_id,
            SubmissionRecovery.id == identity,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if row is None:
        raise DomainError("resource_not_found", "恢复任务不存在")
    if row.request_actor_id != context.actor_id:
        raise DomainError("action_forbidden", "恢复请求操作者不匹配")
    return row


def _owned(job: SubmissionRecovery, token: UUID, revision: int) -> bool:
    return (
        job.state == "RUNNING"
        and job.lease_token == token
        and job.dispatch_revision == revision
        and job.lease_expires_at is not None
        and job.lease_expires_at > datetime.now(UTC)
    )


def _material_reconciliation(
    session: Session, *, step: ExecutionStep, unit: BuildUnit, context: TenantContext
) -> bool:
    if step.cover_job_id:
        from app.modules.builds.cover_execution import cover_matches_step
        from app.modules.materials.cover_models import MaterialCoverJob
        from app.modules.materials.covers import request_cover_reconciliation

        cover = session.get(MaterialCoverJob, step.cover_job_id)
        if (
            cover is None
            or cover.superseded_by_id
            or not cover_matches_step(cover, step, unit)
        ):
            return False
        request_cover_reconciliation(session, context=context, job_id=cover.id)
        step.status, step.phase, step.error_code = (
            "UNKNOWN",
            "DONE",
            "cover_result_unknown",
        )
        step.lease_token = step.lease_expires_at = None
        session.add(step)
        evidence(
            session,
            step=step,
            claim=None,
            conclusion="COVER_RECONCILIATION_REQUESTED",
            summary={"cover_job_id": str(cover.id)},
        )
        return True

    from app.modules.materials.distribution import queue_distribution
    from app.modules.materials.models import (
        MaterialAssetOperation,
        MaterialDistribution,
    )

    dist = session.get(MaterialDistribution, step.distribution_id)
    if (
        dist is None
        or dist.superseded_by_id
        or (
            dist.tenant_id,
            dist.bc_id,
            dist.material_id,
            dist.advertiser_id,
        )
        != (step.tenant_id, step.bc_id, step.material_id, unit.advertiser_id)
    ):
        return False
    require_tenant(
        session, actor_id=dist.actor_id, tenant_id=step.tenant_id, action="build"
    )
    operation = session.exec(
        select(MaterialAssetOperation)
        .where(
            MaterialAssetOperation.id == dist.operation_id,
            MaterialAssetOperation.tenant_id == step.tenant_id,
        )
        .with_for_update()
    ).one_or_none()
    if (
        operation is None
        or operation.superseded_by_id
        or operation.status
        not in {
            "sending",
            "result_unknown",
            "verifying",
            "succeeded",
        }
    ):
        return False
    queue_distribution(
        session, dist, operation, kind="verify", observe=True, read_only=True
    )
    step.status, step.phase, step.error_code = (
        "UNKNOWN",
        "DONE",
        "material_result_unknown",
    )
    step.lease_token, step.lease_expires_at = None, None
    session.add(step)
    evidence(
        session,
        step=step,
        claim=None,
        conclusion="MATERIAL_RECONCILIATION_REQUESTED",
        summary={"distribution_id": str(dist.id)},
    )
    return True


def _retry_material(
    session: Session,
    *,
    step: ExecutionStep,
    unit: BuildUnit,
    row: Submission,
    context: TenantContext,
) -> dict[str, str] | None:
    from app.modules.materials.distribution import ensure_target_asset
    from app.modules.materials.models import (
        MaterialAssetOperation,
        MaterialDistribution,
    )
    from app.modules.materials.readiness import load_material
    from app.modules.materials.routes import (
        load_material_route,
        require_material_route,
        require_same_route,
    )

    assert step.material_id and step.distribution_id
    # 与现有准备/回执处理保持素材→操作锁顺序；执行步骤已由恢复事务持锁。
    load_material(
        session,
        context=context,
        bc_id=step.bc_id,
        material_id=step.material_id,
        lock=True,
    )
    old = session.get(
        MaterialDistribution, step.distribution_id, populate_existing=True
    )
    assert old and old.operation_id
    old_operation = session.exec(
        select(MaterialAssetOperation)
        .where(
            MaterialAssetOperation.id == old.operation_id,
            MaterialAssetOperation.tenant_id == step.tenant_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    # 持有发送方同一把锁后再次检查负证据，防止摘要与执行之间有迟到回执。
    if not SASession.execute(
        session,
        text("SELECT EXISTS(SELECT 1 " + _query(row, "RETRY") + " AND s.id=:step)"),
        {**_params(row), "step": step.id},
    ).scalar_one():
        return None
    route = verify_unit_route(session, context=context, unit=unit, capability="build")
    require_same_route(
        load_material_route(old.target_route, context=context, bc_id=step.bc_id), route
    )
    require_tenant(
        session, actor_id=old.actor_id, tenant_id=step.tenant_id, action="build"
    )
    from app.modules.materials.models import MaterialFile

    material = session.get(MaterialFile, step.material_id)
    if material is not None and material.media_type == "IMAGE":
        from app.modules.materials.image_distribution import retry_unsent_image_target

        retry_unsent_image_target(session, context=context, distribution_id=old.id)
        return {
            "distribution_id": str(old.id),
            "operation_id": str(old.operation_id),
            "retry_kind": "IMAGE_UNSENT",
        }
    if old.source_route is not None:
        from app.modules.materials.models import AccountMaterial

        source = session.get(AccountMaterial, old.source_asset_id)
        if source is None or source.video_id != old_operation.remote_response.get(
            "source_video_id"
        ):
            raise DomainError(
                "material_remote_source_unavailable", "原素材来源已不可用"
            )
        require_material_route(
            session,
            context=context,
            route=load_material_route(
                old.source_route, context=context, bc_id=source.bc_id
            ),
            bc_id=source.bc_id,
            advertiser_id=source.advertiser_id,
            capability="read",
        )
    prepared = ensure_target_asset(
        session,
        context=context,
        bc_id=step.bc_id,
        material_id=step.material_id,
        advertiser_id=unit.advertiser_id,
        task_key=f"build:{step.id}",
        route=route,
    )
    new = (
        session.get(MaterialDistribution, prepared.task_id)
        if prepared.task_id
        else None
    )
    # 新准备仍走唯一入口；选源、路径或授权变更整笔回滚，不能把共享失败降级为上传。
    if (
        new is None
        or new.id == old.id
        or new.operation_id == old.operation_id
        or (
            new.tenant_id,
            new.bc_id,
            new.material_id,
            new.advertiser_id,
            new.path,
            new.target_route,
            new.source_route,
            new.source_asset_id,
        )
        != (
            old.tenant_id,
            old.bc_id,
            old.material_id,
            old.advertiser_id,
            old.path,
            old.target_route,
            old.source_route,
            old.source_asset_id,
        )
    ):
        raise DomainError("material_retry_dependency_changed", "原素材准备依赖已改变")
    step.distribution_id = new.id
    return {
        "old_distribution_id": str(old.id),
        "new_distribution_id": str(new.id),
        "old_operation_id": str(old.operation_id),
        "new_operation_id": str(new.operation_id),
    }


def _schedule(
    session: Session,
    *,
    job: SubmissionRecovery,
    row: Submission,
    step_id: UUID,
    original: TenantContext,
) -> bool:
    from app.modules.builds.dispatch import queue_step, wake_unit

    locator = session.exec(
        select(
            ExecutionStep.unit_id, ExecutionStep.parent_step_id, ExecutionStep.kind
        ).where(
            ExecutionStep.tenant_id == row.tenant_id,
            ExecutionStep.submission_id == row.id,
            ExecutionStep.id == step_id,
        )
    ).one_or_none()
    if locator is None:
        return False
    session.exec(
        select(SubmissionUnit)
        .where(
            SubmissionUnit.tenant_id == row.tenant_id,
            SubmissionUnit.submission_id == row.id,
            SubmissionUnit.unit_id == locator[0],
        )
        .with_for_update()
    ).one()
    # Readback reconciliation owns source before child; never reverse that order.
    if locator[2] == "READBACK" and locator[1]:
        session.exec(
            select(ExecutionStep)
            .where(
                ExecutionStep.tenant_id == row.tenant_id, ExecutionStep.id == locator[1]
            )
            .with_for_update()
        ).one()
    step = session.exec(
        select(ExecutionStep)
        .where(ExecutionStep.tenant_id == row.tenant_id, ExecutionStep.id == step_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    parameters = {**_params(row), "step": step_id}
    if not SASession.execute(
        session,
        text("SELECT EXISTS(SELECT 1 " + _query(row, job.kind) + " AND s.id=:step)"),
        parameters,
    ).scalar_one():
        return False
    unit = session.get(BuildUnit, step.unit_id)
    assert unit
    route = verify_unit_route(
        session,
        context=original,
        unit=unit,
        capability="build" if job.kind == "RETRY" else "read",
    )
    if job.kind == "RETRY":
        access = resolve_account_access(
            session,
            context=original,
            bc_id=row.bc_id,
            advertiser_id=unit.advertiser_id,
            action="build",
            connection_id=route.connection_id,
        )
        if access.connection_id != unit.connection_id or (
            access.currency,
            access.timezone,
        ) != (unit.currency, unit.timezone):
            return False
    if job.kind == "RECONCILE" and step.kind == "MATERIAL":
        return _material_reconciliation(session, step=step, unit=unit, context=original)
    if job.kind == "RETRY":
        if step.error_code == "scene_intent_changed":
            from .execution import _current_scene
            from .submissions import load_execution_unit

            # 发版后的元数据误差可显式重试，但真实业务变化仍不得续建；
            # 过期证据交执行器按原路线刷新，发送前仍会再次核对冻结意图。
            frozen = load_execution_unit(
                session, context=original, submission_id=row.id, unit_id=step.unit_id
            ).frozen
            try:
                _current_scene(session, original, frozen)
            except DomainError as error:
                if error.code != "scene_refresh_required":
                    return False
        frozen_retry = step.request_body is not None
        # 结果未知但已核实未创建的系列/广告组：批准一次原样重发（同 request_id）。
        absent = frozen_retry and confirmed_absent_attempt(session, step=step)
        if (
            frozen_retry
            and not absent
            and not safely_unsent_attempt(session, step=step, recovery=True)
        ):
            return False
        audit: dict[str, Any] = {"recovery_id": str(job.id)}
        if absent:
            audit["confirmed_absent"] = True
        if step.kind == "MATERIAL" and step.distribution_id and not step.cover_job_id:
            material_audit = _retry_material(
                session, step=step, unit=unit, row=row, context=original
            )
            if material_audit is None:
                return False
            audit.update(material_audit)
        if frozen_retry:
            audit.update(
                {
                    "attempt_id": str(step.attempt_id),
                    "body_digest": step.request_body_digest,
                    "transport_failure_count": step.resolved.get(
                        "transport_failure_count", 0
                    ),
                }
            )
        # 只有显式新恢复可重置三次传输上限；正文、attempt 与原路线完全保留。
        step.resolved = {**step.resolved, "transport_failure_count": 0}
        if step.kind == "AD" and not frozen_retry:
            from app.modules.builds.cover_execution import retry_ad_covers

            from .creative_units import preview_contract

            if (
                preview_contract(
                    session, tenant_id=step.tenant_id, preview_id=step.preview_id
                ).media_kind
                == "VIDEO"
            ):
                retry_ad_covers(session, context=original, step=step, unit=unit)
        if step.kind == "MATERIAL" and step.cover_job_id:
            from app.modules.materials.covers import request_cover_retry

            request_cover_retry(session, context=original, job_id=step.cover_job_id)
        step.phase, step.lease_token, step.lease_expires_at, step.error_code = (
            "IDLE",
            None,
            None,
            None,
        )
        step.status = "PENDING"
        evidence(
            session,
            step=step,
            claim=None,
            conclusion="RETRY_REQUESTED",
            summary=audit,
        )
    else:
        evidence(
            session,
            step=step,
            claim=None,
            conclusion="RECONCILIATION_REQUESTED",
            summary={"recovery_id": str(job.id)},
        )
    queue_step(session, step=step, submission=row, reconcile=job.kind == "RECONCILE")
    wake_unit(session, unit_id=step.unit_id, context=original)
    return True


def process_recovery(
    *, database_engine: Any, context: TenantContext, payload: dict[str, Any]
) -> None:
    from app.modules.builds.dispatch import payload_identity

    identity, revision = payload_identity(payload, "recovery_id")
    token = uuid4()
    with Session(database_engine) as session, session.begin():
        job = _job(session, context, identity)
        if (
            job.state in {"COMPLETED", "FAILED"}
            or job.dispatch_revision != revision
            or job.due_at > datetime.now(UTC)
        ):
            return
        if job.lease_expires_at and job.lease_expires_at > datetime.now(UTC):
            return
        job.state, job.lease_token = "RUNNING", token
        job.lease_expires_at = datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS)
        job.repair_after = datetime.now(UTC) + timedelta(seconds=REPAIR_SECONDS)
        session.add(job)
    try:
        with Session(database_engine) as session:
            loaded_job = session.get(SubmissionRecovery, identity)
            assert loaded_job
            job = loaded_job
            row = _submission(session, context, job.submission_id)
            _authority(session, context, row)
            parameters = {**_params(row), "cursor": job.cursor_step_id}
            identities = list(
                SASession.execute(
                    session,
                    text(
                        "SELECT s.id "
                        + _query(row, job.kind)
                        + " AND (CAST(:cursor AS uuid) IS NULL OR s.id>CAST(:cursor AS uuid)) ORDER BY s.id LIMIT 100"
                    ),
                    parameters,
                ).scalars()
            )
        for step_id in identities:
            with Session(database_engine) as session, session.begin():
                job = _job(session, context, identity)
                if not _owned(job, token, revision):
                    return
                row = _submission(session, context, job.submission_id)
                original = _authority(session, context, row)
                scheduled = _schedule(
                    session, job=job, row=row, step_id=step_id, original=original
                )
                job.cursor_step_id = step_id
                job.scanned_count += 1
                job.scheduled_count += int(scheduled)
                job.updated_at = datetime.now(UTC)
                session.add(job)
        with Session(database_engine) as session, session.begin():
            job = _job(session, context, identity)
            if not _owned(job, token, revision):
                return
            job.lease_token, job.lease_expires_at = None, None
            job.updated_at = datetime.now(UTC)
            if len(identities) == PAGE_SIZE:
                job.dispatch_revision += 1
                queue_recovery(session, job)
            else:
                job.state, job.finished_at, job.dispatch_id = (
                    "COMPLETED",
                    datetime.now(UTC),
                    None,
                )
                session.add(job)
    except DomainError as error:
        with Session(database_engine) as session, session.begin():
            job = _job(session, context, identity)
            if _owned(job, token, revision):
                job.state, job.reason_code = "FAILED", error.code
                job.finished_at, job.updated_at = datetime.now(UTC), datetime.now(UTC)
                job.lease_token, job.lease_expires_at, job.dispatch_id = (
                    None,
                    None,
                    None,
                )
                session.add(job)


def repair_recoveries(*, database_engine: Any, limit: int = 100) -> int:
    from sqlalchemy import or_
    from sqlmodel import col

    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("repair limit must be between 1 and 100")
    now = datetime.now(UTC)
    with Session(database_engine) as session, session.begin():
        jobs = session.exec(
            select(SubmissionRecovery)
            .where(
                col(SubmissionRecovery.state).in_(["QUEUED", "RUNNING"]),
                SubmissionRecovery.repair_after <= now,
                or_(
                    col(SubmissionRecovery.lease_expires_at).is_(None),
                    col(SubmissionRecovery.lease_expires_at) <= now,
                ),
            )
            .order_by(col(SubmissionRecovery.repair_after), col(SubmissionRecovery.id))
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for job in jobs:
            dispatch = session.exec(
                select(PendingDispatch)
                .where(PendingDispatch.id == job.dispatch_id)
                .with_for_update()
            ).one_or_none()
            expected = {"recovery_id": str(job.id), "revision": job.dispatch_revision}
            if dispatch is None:
                queue_recovery(session, job)
            elif (
                dispatch.tenant_id,
                dispatch.actor_id,
                dispatch.task_name,
                dispatch.payload,
            ) != (job.tenant_id, job.request_actor_id, TASK_NAME, expected):
                job.state, job.reason_code = "FAILED", "dispatch_payload_invalid"
                job.lease_token, job.lease_expires_at = None, None
                job.finished_at = now
            elif dispatch.published_at is not None:
                dispatch.published_at, dispatch.available_at = None, now
                session.add(dispatch)
            # Unpublished current generation retains publisher backoff and ID.
            job.repair_after = now + timedelta(seconds=REPAIR_SECONDS)
            session.add(job)
        return len(jobs)
