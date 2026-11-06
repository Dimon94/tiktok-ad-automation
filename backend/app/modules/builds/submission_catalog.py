"""Local task catalog. SQL filters precede paging; counts aggregate only that page."""

from datetime import datetime
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.core.pagination import Page
from app.modules.accounts.models import TenantBC
from app.modules.accounts.resolver import decode_cursor, encode_cursor
from app.modules.builds.corrections import resolved_sql
from app.modules.builds.execution_schemas import (
    ObjectCounts,
    StepPublic,
    SubmissionAdPublic,
    SubmissionEventPublic,
    SubmissionGroupPublic,
    SubmissionListItem,
    SubmissionMaterialPublic,
    SubmissionMetadata,
    SubmissionUnitPublic,
)
from app.modules.builds.preview_models import BuildPreview
from app.modules.builds.preview_schemas import build_structure_summary
from app.modules.builds.receipt_completion import obsolete_readback_sql
from app.modules.builds.submissions import (
    _page_scope,
    aggregate_status,
    authorize,
    submission_row,
)

from .ad_readiness import included_ad_sql, included_group_sql
from .submission_counts import INCLUDED_OBJECT_COUNTS

StatusGroup = Literal["all", "active", "attention", "completed"]


def _scalar_count(session: Session, query: str, params: dict[str, Any]) -> int:
    return int(cast(SASession, session).execute(text(query), params).scalar_one())


METADATA = """
SELECT s.id submission_id,coalesce(nullif(actor.full_name,''),actor.username) actor_name,
 st.name || ' v' || sv.number::text strategy_label,
 (SELECT string_agg(names.display_name,'、' ORDER BY names.display_name) FROM (
 SELECT DISTINCT pc.display_name FROM preview_drama d
 JOIN promotion_link l ON l.tenant_id=d.tenant_id AND l.id=d.link_id
 JOIN provider_connection pc ON pc.tenant_id=l.tenant_id AND pc.id=l.connection_id
 WHERE d.tenant_id=s.tenant_id AND d.preview_id=s.preview_id) names) provider_name
FROM build_submission s JOIN "user" actor ON actor.id=s.actor_id
JOIN build_preview p ON p.tenant_id=s.tenant_id AND p.id=s.preview_id
JOIN strategy_version sv ON sv.tenant_id=p.tenant_id AND sv.id=p.strategy_version_id
JOIN strategy st ON st.tenant_id=sv.tenant_id AND st.id=sv.strategy_id
"""


def metadata(
    session: Session, *, tenant_id: UUID, submission_id: UUID
) -> SubmissionMetadata:
    row = (
        cast(SASession, session)
        .execute(
            text(METADATA + " WHERE s.tenant_id=:tenant AND s.id=:submission"),
            {"tenant": tenant_id, "submission": submission_id},
        )
        .mappings()
        .one()
    )
    return SubmissionMetadata.model_validate(row)


# Page-level equivalent of the authoritative summary's frozen object partition.
# MATERIAL/CTA/READBACK are dependency facts, never additional advertising objects.

COUNTS = (
    """
, scope_basis AS (
 SELECT p.id submission_id,u.*, EXISTS (
  SELECT 1 FROM build_submission old JOIN build_unit ou ON ou.tenant_id=old.tenant_id AND ou.preview_id=old.preview_id
  WHERE old.tenant_id=p.tenant_id AND old.draft_id=p.draft_id AND old.ordinal<p.ordinal
  AND ou.drama_id=u.drama_id AND ou.advertiser_id=u.advertiser_id
  AND ou.complete AND ou.readiness IN ('READY','PREPARING')) covered
 FROM page p JOIN build_unit u ON u.tenant_id=p.tenant_id AND u.preview_id=p.preview_id
), scope AS (
 SELECT b.*, b.complete AND b.readiness IN ('READY','PREPARING') AND NOT b.covered included FROM scope_basis b
)"""
    + INCLUDED_OBJECT_COUNTS
    + """, totals AS (
 SELECT submission_id,count(DISTINCT drama_id) drama_count,count(DISTINCT advertiser_id) account_count,
 count(*) FILTER(WHERE NOT included) excluded_unit_count,count(*) FILTER(WHERE included) submitted_c,
 coalesce(sum(c.group_count) FILTER(WHERE included),0) submitted_g,
 coalesce(sum(c.ad_count) FILTER(WHERE included),0) submitted_a
 FROM scope LEFT JOIN included_object_counts c
 ON c.tenant_id=scope.tenant_id AND c.preview_id=scope.preview_id AND c.unit_id=scope.id
 GROUP BY submission_id
), failed_ancestors AS MATERIALIZED (
 SELECT p.id submission_id,s.unit_id,bool_or(s.kind='CTA') cta_failed,bool_or(s.kind='CAMPAIGN') campaign_failed
 FROM page p JOIN execution_step s ON s.tenant_id=p.tenant_id AND s.submission_id=p.id
 WHERE s.status='FAILED' AND s.kind IN ('CTA','CAMPAIGN') GROUP BY p.id,s.unit_id
), failed_adgroups AS MATERIALIZED (
 SELECT DISTINCT p.id submission_id,s.unit_id,s.group_id FROM page p
 JOIN execution_step s ON s.tenant_id=p.tenant_id AND s.submission_id=p.id
 WHERE s.status='FAILED' AND s.kind='ADGROUP'
), material_failed_groups AS MATERIALIZED (
 SELECT DISTINCT p.id submission_id,p.preview_id,g.id group_id,g.unit_id
 FROM page p JOIN execution_step ms ON ms.tenant_id=p.tenant_id AND ms.submission_id=p.id
 JOIN planned_group g ON g.tenant_id=p.tenant_id AND g.preview_id=p.preview_id AND g.unit_id=ms.unit_id
 JOIN frozen_group_material m ON m.tenant_id=g.tenant_id AND m.preview_id=g.preview_id
 AND m.drama_id=g.drama_id AND m.group_no=g.group_no AND m.material_id=ms.material_id
 AND NOT EXISTS (SELECT 1 FROM preview_skipped_material skipped
 WHERE skipped.tenant_id=m.tenant_id AND skipped.unit_id=g.unit_id AND skipped.material_id=m.material_id)
 WHERE ms.kind='MATERIAL' AND ms.status='FAILED' AND NOT EXISTS (SELECT 1 FROM preview_contract pc WHERE pc.tenant_id=ms.tenant_id AND pc.preview_id=p.preview_id AND pc.media_kind='CAROUSEL')
), material_failed_units AS MATERIALIZED (
 SELECT mg.submission_id,mg.unit_id FROM material_failed_groups mg
 GROUP BY mg.submission_id,mg.preview_id,mg.unit_id
 HAVING count(*)=(SELECT count(*) FROM planned_group g
 WHERE g.tenant_id=:tenant AND g.preview_id=mg.preview_id AND g.unit_id=mg.unit_id)
), objects AS (
 SELECT u.submission_id,u.id unit_id,'CAMPAIGN' kind,NULL::uuid group_id,NULL::uuid ad_id FROM scope u WHERE included
 UNION ALL SELECT u.submission_id,u.id,'ADGROUP',g.id,NULL FROM scope u JOIN planned_group g ON g.tenant_id=u.tenant_id AND g.preview_id=u.preview_id AND g.unit_id=u.id WHERE included AND """
    + included_group_sql()
    + """
 UNION ALL SELECT u.submission_id,u.id,'AD',g.id,a.id FROM scope u JOIN planned_group g ON g.tenant_id=u.tenant_id AND g.preview_id=u.preview_id AND g.unit_id=u.id JOIN planned_ad a ON a.tenant_id=g.tenant_id AND a.preview_id=g.preview_id AND a.group_id=g.id WHERE included AND """
    + included_ad_sql()
    + """
), results AS (
 SELECT o.submission_id,o.kind, CASE
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
 FROM objects o LEFT JOIN execution_step s ON s.tenant_id=:tenant AND s.submission_id=o.submission_id AND s.unit_id=o.unit_id AND s.kind=o.kind AND s.group_id IS NOT DISTINCT FROM o.group_id AND s.planned_ad_id IS NOT DISTINCT FROM o.ad_id
 LEFT JOIN failed_ancestors fa ON fa.submission_id=o.submission_id AND fa.unit_id=o.unit_id
 LEFT JOIN failed_adgroups fg ON fg.submission_id=o.submission_id AND fg.unit_id=o.unit_id AND fg.group_id=o.group_id
 LEFT JOIN material_failed_groups mg ON mg.submission_id=o.submission_id AND mg.unit_id=o.unit_id AND mg.group_id=o.group_id
 LEFT JOIN material_failed_units mu ON mu.submission_id=o.submission_id AND mu.unit_id=o.unit_id
), result_counts AS (
 SELECT submission_id,kind,outcome,count(*) n FROM results GROUP BY submission_id,kind,outcome
), outcomes AS (
 SELECT submission_id,jsonb_object_agg(kind || ':' || outcome,n) values FROM result_counts GROUP BY submission_id
)
"""
)


# 历史任务的缓存状态可能只因自动核查而停在 NEEDS_REVIEW。只投影受影响
# 的任务，保留所有步骤原状态；列表、筛选和总数必须使用同一可见结果。
CATALOG_SUBMISSIONS = (
    """catalog_submissions AS (
 SELECT original.*,CASE WHEN original.expanded AND EXISTS (
 SELECT 1 FROM execution_step s WHERE s.tenant_id=original.tenant_id
 AND s.submission_id=original.id AND """
    + obsolete_readback_sql("s")
    + """) THEN (WITH page AS (SELECT original.*) """
    + COUNTS
    + """
 SELECT CASE
 WHEN bool_or((e.status='UNKNOWN' OR e.mismatch OR
 (e.kind IN ('CAMPAIGN','ADGROUP','AD') AND e.status='SUCCEEDED' AND nullif(trim(e.remote_id),'') IS NULL)) AND NOT """
    + resolved_sql("e")
    + """) THEN 'NEEDS_REVIEW'
 WHEN EXISTS (SELECT 1 FROM result_counts WHERE outcome='failed')
 AND NOT EXISTS (SELECT 1 FROM result_counts WHERE outcome='pending')
 THEN CASE WHEN EXISTS (SELECT 1 FROM result_counts WHERE outcome='succeeded')
 THEN 'PARTIAL' ELSE 'FAILED' END
 WHEN bool_or(e.status IN ('PENDING','RUNNING','RETRYABLE') AND NOT """
    + resolved_sql("e")
    + """) THEN 'RUNNING'
 WHEN bool_and(e.status='SUCCEEDED' OR """
    + resolved_sql("e")
    + """) THEN 'COMPLETED'
 WHEN bool_or(e.status='SUCCEEDED' OR """
    + resolved_sql("e")
    + """) AND bool_or(e.status='FAILED' AND NOT """
    + resolved_sql("e")
    + """) THEN 'PARTIAL'
 WHEN bool_and(e.status='FAILED') THEN 'FAILED' ELSE 'QUEUED' END
 FROM execution_step e WHERE e.tenant_id=original.tenant_id
 AND e.submission_id=original.id AND NOT """
    + obsolete_readback_sql("e")
    + """) ELSE original.status END visible_status
 FROM build_submission original WHERE original.tenant_id=:tenant)"""
)


def list_submissions(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    q: str | None = None,
    status_group: StatusGroup = "all",
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    provider_connection_id: UUID | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> Page[SubmissionListItem]:
    authorize(session, context)
    if session.get(TenantBC, (context.tenant_id, bc_id)) is None:
        raise DomainError("resource_not_found", "BC 不存在")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise DomainError("invalid_page_size", "分页范围为1到100")
    if status_group not in {"all", "active", "attention", "completed"}:
        raise DomainError("invalid_submission_filter", "状态筛选无效")
    if (
        (created_from and not created_from.tzinfo)
        or (created_to and not created_to.tzinfo)
        or (created_from and created_to and created_from >= created_to)
    ):
        raise DomainError(
            "invalid_submission_filter", "日期范围须包含时区且开始早于结束"
        )
    scope = {
        "tenant": str(context.tenant_id),
        "bc": bc_id,
        "kind": "submission_list",
        "q": q,
        "status": status_group,
        "from": created_from.isoformat() if created_from else None,
        "to": created_to.isoformat() if created_to else None,
        "provider": str(provider_connection_id) if provider_connection_id else None,
        "limit": str(limit),
    }
    after = decode_cursor(cursor, scope=scope)
    at = None
    aid = None
    if after:
        try:
            stamp, identity = after.split("|")
            at = datetime.fromisoformat(stamp)
            aid = UUID(identity)
            if not at.tzinfo:
                raise ValueError
        except ValueError, TypeError:
            raise DomainError("invalid_cursor", "分页游标无效") from None
    query = (
        "WITH "
        + CATALOG_SUBMISSIONS
        + """, metadata AS ("""
        + METADATA
        + """ WHERE s.tenant_id=:tenant), filtered AS (
 SELECT s.*,p.batch_short_id,p.config,p.target_roas,m.actor_name,m.provider_name,m.strategy_label
 FROM catalog_submissions s JOIN build_preview p ON p.tenant_id=s.tenant_id AND p.id=s.preview_id JOIN metadata m ON m.submission_id=s.id
 WHERE s.tenant_id=:tenant AND s.bc_id=:bc
 AND (CAST(:from AS timestamptz) IS NULL OR s.created_at>=:from) AND (CAST(:to AS timestamptz) IS NULL OR s.created_at<:to)
 AND (:status='all' OR (:status='active' AND s.visible_status IN ('QUEUED','RUNNING')) OR (:status='attention' AND s.visible_status IN ('PARTIAL','FAILED','NEEDS_REVIEW')) OR (:status='completed' AND s.visible_status='COMPLETED'))
 AND (CAST(:provider AS uuid) IS NULL OR EXISTS (SELECT 1 FROM preview_drama d JOIN promotion_link l ON l.tenant_id=d.tenant_id AND l.id=d.link_id WHERE d.tenant_id=s.tenant_id AND d.preview_id=s.preview_id AND l.connection_id=:provider))
 AND (CAST(:q AS text) IS NULL OR strpos(lower(p.batch_short_id),lower(:q))>0 OR strpos(s.id::text,:q)>0 OR strpos(lower(m.actor_name),lower(:q))>0 OR strpos(lower(m.strategy_label),lower(:q))>0 OR strpos(lower(coalesce(m.provider_name,'')),lower(:q))>0
 OR EXISTS (SELECT 1 FROM preview_drama d WHERE d.tenant_id=s.tenant_id AND d.preview_id=s.preview_id AND strpos(lower(d.title),lower(:q))>0)
 OR EXISTS (SELECT 1 FROM build_unit u WHERE u.tenant_id=s.tenant_id AND u.preview_id=s.preview_id AND u.advertiser_id=:q))
), page AS (
 SELECT s.* FROM filtered s
 WHERE (CAST(:after_at AS timestamptz) IS NULL OR (s.created_at,s.id)<(:after_at,CAST(:after_id AS uuid)))
 ORDER BY s.created_at DESC,s.id DESC LIMIT :size), filtered_total AS (
 SELECT count(*) total_count FROM filtered)
 """
        + COUNTS
        + """ SELECT p.*,ft.total_count,coalesce(t.drama_count,0) drama_count,coalesce(t.account_count,0) account_count,
 coalesce(p.config->>'budget_strategy','SERIES') budget_strategy,
 coalesce(p.config->>'bid_strategy',CASE WHEN p.target_roas IS NULL THEN 'HIGHEST_VALUE' ELSE 'TARGET_ROAS' END) bid_strategy,
 coalesce(t.excluded_unit_count,0) excluded_unit_count,coalesce(t.submitted_c,0) submitted_c,coalesce(t.submitted_g,0) submitted_g,coalesce(t.submitted_a,0) submitted_a,coalesce(o.values,'{}'::jsonb) outcomes,
 (SELECT count(*) FROM build_verified_replacement vr WHERE vr.tenant_id=p.tenant_id AND vr.submission_id=p.id) corrected_ad_count
 FROM filtered_total ft LEFT JOIN page p ON true LEFT JOIN totals t ON t.submission_id=p.id LEFT JOIN outcomes o ON o.submission_id=p.id ORDER BY p.created_at DESC,p.id DESC"""
    )
    query_params = {
        "tenant": context.tenant_id,
        "bc": bc_id,
        "q": q,
        "status": status_group,
        "from": created_from,
        "to": created_to,
        "provider": provider_connection_id,
        "after_at": at,
        "after_id": aid,
        "size": limit + 1,
    }
    rows = (
        cast(SASession, session)
        .execute(
            text(query),
            query_params,
        )
        .mappings()
        .all()
    )
    # 总数和当前页由同一条 SQL 返回；LEFT JOIN 保证越过末页时仍有总数行。
    total = int(rows[0]["total_count"])
    page_rows = [row for row in rows if row["id"] is not None]
    items = []
    for row in page_rows[:limit]:
        outcomes = {
            key: ObjectCounts(
                **{
                    field: row["outcomes"].get(f"{kind}:{key}", 0)
                    for field, kind in [
                        ("campaign_count", "CAMPAIGN"),
                        ("adgroup_count", "ADGROUP"),
                        ("ad_count", "AD"),
                    ]
                }
            )
            for key in ["succeeded", "failed", "unknown"]
        }
        items.append(
            SubmissionListItem(
                status=row["visible_status"],
                **{
                    k: row[k]
                    for k in [
                        "batch_short_id",
                        "bc_id",
                        "created_at",
                        "updated_at",
                        "actor_name",
                        "provider_name",
                        "strategy_label",
                        "drama_count",
                        "account_count",
                        "excluded_unit_count",
                        "corrected_ad_count",
                    ]
                },
                submission_id=row["id"],
                submitted=ObjectCounts(
                    campaign_count=row["submitted_c"],
                    adgroup_count=row["submitted_g"],
                    ad_count=row["submitted_a"],
                ),
                succeeded=outcomes["succeeded"],
                failed=outcomes["failed"],
                unknown=outcomes["unknown"],
                budget_strategy=row["budget_strategy"],
                bid_strategy=row["bid_strategy"],
            )
        )
    return Page(
        items=items,
        next_cursor=encode_cursor(
            scope=scope,
            last_id=f"{items[-1].created_at.isoformat()}|{items[-1].submission_id}",
        )
        if len(page_rows) > limit
        else None,
        total=total,
    )


def step_public(row: Any) -> StepPublic | None:
    return StepPublic.model_validate(row["step"]) if row["step"] else None


STEP_JSON = """CASE WHEN e.id IS NULL THEN NULL ELSE jsonb_build_object('step_id',e.id,'unit_id',e.unit_id,'kind',e.kind,'group_id',e.group_id,'planned_ad_id',e.planned_ad_id,'material_id',e.material_id,'status',e.status,'remote_id',e.remote_id,'error_code',e.error_code,'operation_status',e.operation_status,'review_status',e.review_status,'mismatch',e.mismatch,'checked_at',e.checked_at) END"""


def get_submission_groups(
    session: Session,
    *,
    context: TenantContext,
    submission_id: UUID,
    unit_id: UUID,
    cursor: str | None = None,
    limit: int = 50,
) -> Page[SubmissionGroupPublic]:
    authorize(session, context)
    submission = submission_row(session, context, submission_id)
    scope, after = _page_scope(
        context, submission, "groups", limit, cursor, {"unit": str(unit_id)}
    )
    params = {
        "tenant": context.tenant_id,
        "submission": submission_id,
        "preview": submission.preview_id,
        "unit": unit_id,
        "after": after,
        "size": limit + 1,
    }
    unit = (
        cast(SASession, session)
        .execute(
            text(
                "SELECT id FROM build_unit WHERE tenant_id=:tenant AND preview_id=:preview AND id=:unit"
            ),
            params,
        )
        .first()
    )
    if not unit:
        raise DomainError("resource_not_found", "组合不存在")
    total = _scalar_count(
        session,
        "SELECT count(*) FROM planned_group WHERE tenant_id=:tenant "
        "AND preview_id=:preview AND unit_id=:unit",
        params,
    )
    rows = (
        cast(SASession, session)
        .execute(
            text(
                """WITH page AS (SELECT g.* FROM planned_group g WHERE g.tenant_id=:tenant AND g.preview_id=:preview AND g.unit_id=:unit AND (CAST(:after AS uuid) IS NULL OR g.id>:after) ORDER BY g.id LIMIT :size)
 SELECT g.id group_id,g.unit_id,g.group_no,g.name,
 (SELECT count(*) FROM frozen_group_material m WHERE m.tenant_id=g.tenant_id AND m.preview_id=g.preview_id AND m.drama_id=g.drama_id AND m.group_no=g.group_no
 AND NOT EXISTS (SELECT 1 FROM preview_skipped_material skipped WHERE skipped.tenant_id=m.tenant_id AND skipped.unit_id=g.unit_id AND skipped.material_id=m.material_id)) material_count,
 (SELECT count(*) FROM planned_ad a WHERE a.tenant_id=g.tenant_id AND a.preview_id=g.preview_id AND a.group_id=g.id) ad_count,
 """
                + STEP_JSON
                + """ step FROM page g LEFT JOIN execution_step e ON e.tenant_id=:tenant AND e.submission_id=:submission AND e.unit_id=g.unit_id AND e.group_id=g.id AND e.kind='ADGROUP' ORDER BY g.id"""
            ),
            params,
        )
        .mappings()
        .all()
    )
    items = [
        SubmissionGroupPublic(
            **{
                k: r[k]
                for k in [
                    "group_id",
                    "unit_id",
                    "group_no",
                    "name",
                    "material_count",
                    "ad_count",
                ]
            },
            step=step_public(r),
        )
        for r in rows[:limit]
    ]
    enrich_corrections(
        session,
        context=context,
        submission_id=submission_id,
        items=[item.step for item in items if item.step],
    )
    return Page(
        items=items,
        next_cursor=encode_cursor(scope=scope, last_id=str(items[-1].group_id))
        if len(rows) > limit
        else None,
        total=total,
    )


def get_submission_ads(
    session: Session,
    *,
    context: TenantContext,
    submission_id: UUID,
    unit_id: UUID,
    group_id: UUID,
    cursor: str | None = None,
    limit: int = 50,
) -> Page[SubmissionAdPublic]:
    authorize(session, context)
    submission = submission_row(session, context, submission_id)
    scope, after = _page_scope(
        context,
        submission,
        "ads",
        limit,
        cursor,
        {"unit": str(unit_id), "group": str(group_id)},
    )
    params = {
        "tenant": context.tenant_id,
        "submission": submission_id,
        "preview": submission.preview_id,
        "unit": unit_id,
        "group": group_id,
        "after": after,
        "size": limit + 1,
    }
    group = (
        cast(SASession, session)
        .execute(
            text(
                "SELECT id FROM planned_group WHERE tenant_id=:tenant AND preview_id=:preview AND unit_id=:unit AND id=:group"
            ),
            params,
        )
        .first()
    )
    if not group:
        raise DomainError("resource_not_found", "素材组不存在")
    total = _scalar_count(
        session,
        "SELECT count(*) FROM planned_ad WHERE tenant_id=:tenant "
        "AND preview_id=:preview AND group_id=:group",
        params,
    )
    rows = (
        cast(SASession, session)
        .execute(
            text(
                """WITH page AS (SELECT a.* FROM planned_ad a WHERE a.tenant_id=:tenant AND a.preview_id=:preview AND a.group_id=:group AND (CAST(:after AS uuid) IS NULL OR a.id>:after) ORDER BY a.id LIMIT :size)
 SELECT a.id planned_ad_id,a.group_id,a.creative_no,a.name,a.text,a.cta_option_ids,
 coalesce(ar.included,true) included,coalesce(ar.reason_codes,'[]'::jsonb) reason_codes,"""
                + STEP_JSON
                + """ step
 FROM page a LEFT JOIN preview_ad_readiness ar ON ar.tenant_id=a.tenant_id AND ar.preview_id=a.preview_id AND ar.ad_id=a.id LEFT JOIN execution_step e ON e.tenant_id=:tenant AND e.submission_id=:submission AND e.unit_id=:unit AND e.planned_ad_id=a.id AND e.kind='AD' ORDER BY a.id"""
            ),
            params,
        )
        .mappings()
        .all()
    )
    items = [
        SubmissionAdPublic(
            **{
                k: r[k]
                for k in [
                    "planned_ad_id",
                    "included",
                    "reason_codes",
                    "group_id",
                    "creative_no",
                    "name",
                    "text",
                    "cta_option_ids",
                ]
            },
            step=step_public(r),
        )
        for r in rows[:limit]
    ]
    enrich_corrections(
        session,
        context=context,
        submission_id=submission_id,
        items=[item.step for item in items if item.step],
    )
    return Page(
        items=items,
        next_cursor=encode_cursor(scope=scope, last_id=str(items[-1].planned_ad_id))
        if len(rows) > limit
        else None,
        total=total,
    )


def get_submission_events(
    session: Session,
    *,
    context: TenantContext,
    submission_id: UUID,
    step_id: UUID | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> Page[SubmissionEventPublic]:
    authorize(session, context)
    submission = submission_row(session, context, submission_id)
    scope, _ = _page_scope(
        context,
        submission,
        "events",
        limit,
        None,
        {"step": str(step_id) if step_id else None},
    )
    after = decode_cursor(cursor, scope=scope)
    at = None
    aid = None
    if after:
        try:
            stamp, identity = after.split("|")
            at = datetime.fromisoformat(stamp)
            aid = UUID(identity)
            if not at.tzinfo:
                raise ValueError
        except ValueError, TypeError:
            raise DomainError("invalid_cursor", "分页游标无效") from None
    # 广告的 dependency_failed 没有平台响应；最多追溯广告组、系列两级失败祖先。
    # 每级都限定同一租户和任务，保留原记录的步骤身份及时间，不把父错误伪装成子错误。
    event_scope = """WITH RECURSIVE event_steps AS (
 SELECT e.id,e.parent_step_id,e.error_code,0 depth FROM execution_step e
 WHERE e.tenant_id=:tenant AND e.submission_id=:submission AND e.id=CAST(:step AS uuid)
 UNION ALL
 SELECT p.id,p.parent_step_id,p.error_code,c.depth+1
 FROM event_steps c JOIN execution_step p ON p.id=c.parent_step_id
 WHERE p.tenant_id=:tenant AND p.submission_id=:submission
 AND c.error_code='dependency_failed' AND p.status='FAILED' AND c.depth<2
)
"""
    step_filter = (
        "(CAST(:step AS uuid) IS NULL OR v.step_id IN (SELECT id FROM event_steps))"
    )
    total = _scalar_count(
        session,
        event_scope + "SELECT count(*) FROM step_evidence v WHERE v.tenant_id=:tenant "
        "AND v.submission_id=:submission AND " + step_filter,
        {
            "tenant": context.tenant_id,
            "submission": submission_id,
            "step": step_id,
        },
    )
    rows = (
        cast(SASession, session)
        .execute(
            # 仅投影错误说明与关联编号；整个 summary、请求正文及授权证据不公开。
            text(
                event_scope
                + """SELECT v.id evidence_id,v.step_id,v.attempt,v.conclusion,v.observed_at,e.unit_id,e.kind,
 v.request_id,v.summary->'remote_code' remote_code,v.summary->'remote_message' remote_message,
 d.title,u.advertiser_id,ac.name account_name
 FROM step_evidence v JOIN execution_step e ON e.tenant_id=v.tenant_id AND e.submission_id=v.submission_id AND e.id=v.step_id
 JOIN build_unit u ON u.tenant_id=e.tenant_id AND u.preview_id=e.preview_id AND u.id=e.unit_id
 JOIN preview_drama d ON d.tenant_id=u.tenant_id AND d.preview_id=u.preview_id AND d.drama_id=u.drama_id
 LEFT JOIN advertiser_account ac ON ac.tenant_id=u.tenant_id AND ac.advertiser_id=u.advertiser_id
 WHERE v.tenant_id=:tenant AND v.submission_id=:submission AND """
                + step_filter
                + """
 AND (CAST(:after_at AS timestamptz) IS NULL OR (v.observed_at,v.id)<(:after_at,CAST(:after_id AS uuid))) ORDER BY v.observed_at DESC,v.id DESC LIMIT :size"""
            ),
            {
                "tenant": context.tenant_id,
                "submission": submission_id,
                "step": step_id,
                "after_at": at,
                "after_id": aid,
                "size": limit + 1,
            },
        )
        .mappings()
        .all()
    )
    items = [SubmissionEventPublic.model_validate(r) for r in rows[:limit]]
    return Page(
        items=items,
        next_cursor=encode_cursor(
            scope=scope,
            last_id=f"{items[-1].observed_at.isoformat()}|{items[-1].evidence_id}",
        )
        if len(rows) > limit
        else None,
        total=total,
    )


def enrich_units(
    session: Session,
    *,
    context: TenantContext,
    submission_id: UUID,
    preview_id: UUID,
    items: list[SubmissionUnitPublic],
) -> None:
    if not items:
        return
    rows = (
        cast(SASession, session)
        .execute(
            text(
                """WITH page AS (
 SELECT u.* FROM build_unit u WHERE u.tenant_id=:tenant AND u.preview_id=:preview AND u.id=ANY(CAST(:ids AS uuid[]))
), counts AS (
 SELECT e.unit_id,count(*) FILTER(WHERE e.kind='ADGROUP' AND nullif(trim(e.remote_id),'') IS NOT NULL) succeeded_group_count,
 count(*) FILTER(WHERE e.kind='AD' AND (nullif(trim(e.remote_id),'') IS NOT NULL OR """
                + resolved_sql("e")
                + """)) succeeded_ad_count,
 count(*) FILTER(WHERE e.kind='MATERIAL' AND e.status='SUCCEEDED') ready_material_count,
 array_agg(DISTINCT CASE WHEN """
                + resolved_sql("e")
                + """ THEN 'SUCCEEDED' ELSE e.status END) states,bool_or(e.mismatch AND NOT """
                + resolved_sql("e")
                + """) mismatch,
 bool_or(e.kind IN ('CAMPAIGN','ADGROUP','AD') AND e.status='SUCCEEDED' AND nullif(trim(e.remote_id),'') IS NULL) unverified_success
 FROM execution_step e JOIN page u ON e.unit_id=u.id WHERE e.tenant_id=:tenant AND e.submission_id=:submission AND NOT """
                + obsolete_readback_sql("e")
                + """ GROUP BY e.unit_id
), materials AS (
 SELECT u.id unit_id,count(m.material_id) material_count,count(DISTINCT m.material_id) unique_material_count,
 CASE WHEN EXISTS (SELECT 1 FROM preview_contract pc WHERE pc.tenant_id=u.tenant_id AND pc.preview_id=u.preview_id AND (pc.version=2 OR pc.legacy_material_layout='AD'))
  THEN (SELECT count(*) FROM frozen_ad_material am WHERE am.tenant_id=u.tenant_id AND am.preview_id=u.preview_id AND am.drama_id=u.drama_id
    AND NOT EXISTS (SELECT 1 FROM preview_skipped_material skipped WHERE skipped.tenant_id=am.tenant_id AND skipped.unit_id=u.id AND skipped.material_id=am.material_id))
  WHEN EXISTS (SELECT 1 FROM preview_contract pc WHERE pc.tenant_id=u.tenant_id AND pc.preview_id=u.preview_id AND pc.version=1 AND pc.legacy_material_layout='GROUP') THEN (SELECT count(*) FROM planned_ad a JOIN planned_group g ON g.tenant_id=a.tenant_id AND g.preview_id=a.preview_id AND g.id=a.group_id
    JOIN frozen_group_material legacy_m ON legacy_m.tenant_id=g.tenant_id AND legacy_m.preview_id=g.preview_id AND legacy_m.drama_id=g.drama_id AND legacy_m.group_no=g.group_no
    WHERE a.tenant_id=u.tenant_id AND a.preview_id=u.preview_id AND g.unit_id=u.id
      AND NOT EXISTS (SELECT 1 FROM preview_skipped_material skipped WHERE skipped.tenant_id=legacy_m.tenant_id AND skipped.unit_id=u.id AND skipped.material_id=legacy_m.material_id))
 ELSE 0 END material_allocation_count
 FROM page u LEFT JOIN frozen_group_material m ON m.tenant_id=u.tenant_id AND m.preview_id=u.preview_id AND m.drama_id=u.drama_id
 AND NOT EXISTS (SELECT 1 FROM preview_skipped_material skipped WHERE skipped.tenant_id=m.tenant_id AND skipped.unit_id=u.id AND skipped.material_id=m.material_id)
 GROUP BY u.id,u.tenant_id,u.preview_id,u.drama_id
)
 SELECT u.id,ac.name account_name,u.group_count,u.ad_count,coalesce(c.succeeded_group_count,0) succeeded_group_count,coalesce(c.succeeded_ad_count,0) succeeded_ad_count,
 coalesce(c.ready_material_count,0) ready_material_count,m.material_count,m.unique_material_count,m.material_allocation_count,c.states,c.mismatch,c.unverified_success,"""
                + STEP_JSON
                + """ step FROM page u
 LEFT JOIN counts c ON c.unit_id=u.id LEFT JOIN materials m ON m.unit_id=u.id
 LEFT JOIN advertiser_account ac ON ac.tenant_id=u.tenant_id AND ac.advertiser_id=u.advertiser_id
 LEFT JOIN execution_step e ON e.tenant_id=u.tenant_id AND e.submission_id=:submission AND e.unit_id=u.id AND e.kind='CAMPAIGN'"""
            ),
            {
                "tenant": context.tenant_id,
                "submission": submission_id,
                "preview": preview_id,
                "ids": [i.unit_id for i in items],
            },
        )
        .mappings()
        .all()
    )
    indexed = {r["id"]: r for r in rows}
    preview = session.get(BuildPreview, preview_id)
    config = preview.config if preview else {}
    for item in items:
        row = indexed[item.unit_id]
        for key in [
            "account_name",
            "group_count",
            "ad_count",
            "succeeded_group_count",
            "succeeded_ad_count",
            "material_count",
            "unique_material_count",
            "material_allocation_count",
            "ready_material_count",
        ]:
            setattr(item, key, row[key])
        item.campaign_step = step_public(row)
        states = set(row["states"] or [])
        if row["unverified_success"]:
            states.add("UNKNOWN")
        if row["mismatch"]:
            states.add("MISMATCH")
        if not item.expanded:
            states.add("PENDING" if states else "QUEUED")
        item.result_status = (
            "EXCLUDED" if item.disposition == "EXCLUDED" else aggregate_status(states)
        )
        item.structure_summary = build_structure_summary(
            campaign_count=1,
            group_count=int(row["group_count"] or 0),
            ad_count=int(row["ad_count"] or 0),
            creative_count=int(config.get("creative_count", 1)),
            group_generation_mode=config.get("group_generation_mode", "FIXED"),
            ad_generation_mode=config.get("ad_generation_mode", "BY_MATERIAL"),
            material_allocation_count=int(row["material_allocation_count"] or 0),
            unique_material_count=int(row["unique_material_count"] or 0),
        )


def enrich_steps(
    session: Session,
    *,
    context: TenantContext,
    submission_id: UUID,
    items: list[StepPublic],
) -> None:
    if not items:
        return
    from app.modules.builds.historical_read import historical_read_eligible_steps

    eligible = historical_read_eligible_steps(
        session, context=context, step_ids=[item.step_id for item in items]
    )
    rows = (
        cast(SASession, session)
        .execute(
            text("""SELECT e.id,d.title,u.advertiser_id,g.group_no,a.creative_no
 FROM execution_step e JOIN build_unit u ON u.tenant_id=e.tenant_id AND u.preview_id=e.preview_id AND u.id=e.unit_id
 JOIN preview_drama d ON d.tenant_id=u.tenant_id AND d.preview_id=u.preview_id AND d.drama_id=u.drama_id
 LEFT JOIN planned_group g ON g.tenant_id=e.tenant_id AND g.preview_id=e.preview_id AND g.id=e.group_id
 LEFT JOIN planned_ad a ON a.tenant_id=e.tenant_id AND a.preview_id=e.preview_id AND a.id=e.planned_ad_id
 WHERE e.tenant_id=:tenant AND e.submission_id=:submission AND e.id=ANY(CAST(:ids AS uuid[]))"""),
            {
                "tenant": context.tenant_id,
                "submission": submission_id,
                "ids": [i.step_id for i in items],
            },
        )
        .mappings()
        .all()
    )
    indexed = {r["id"]: r for r in rows}
    for item in items:
        item.can_historical_read = item.step_id in eligible
        for key in ["title", "advertiser_id", "group_no", "creative_no"]:
            setattr(item, key, indexed[item.step_id][key])
    enrich_corrections(
        session, context=context, submission_id=submission_id, items=items
    )


def enrich_corrections(
    session: Session,
    *,
    context: TenantContext,
    submission_id: UUID,
    items: list[StepPublic],
) -> None:
    if not items:
        return
    from app.modules.builds.execution_schemas import ReplacementPublic

    rows = (
        cast(SASession, session)
        .execute(
            text("""SELECT DISTINCT ON (e.id) e.id step_id,e.kind,vr.* FROM execution_step e
 JOIN build_verified_replacement vr ON vr.tenant_id=e.tenant_id AND vr.submission_id=e.submission_id
 AND (vr.source_step_id=e.id OR vr.source_group_step_id=e.id OR (e.kind='READBACK' AND e.parent_step_id IN (vr.source_step_id,vr.source_group_step_id)))
 WHERE e.tenant_id=:tenant AND e.submission_id=:submission AND e.id=ANY(CAST(:ids AS uuid[])) ORDER BY e.id,vr.verified_at,vr.id"""),
            {
                "tenant": context.tenant_id,
                "submission": submission_id,
                "ids": [item.step_id for item in items],
            },
        )
        .mappings()
        .all()
    )
    indexed = {row["step_id"]: row for row in rows}
    for item in items:
        row = indexed.get(item.step_id)
        if row is None:
            continue
        group = (
            item.kind == "ADGROUP"
            or item.kind == "READBACK"
            and item.planned_ad_id is None
        )
        facts = row["verification"]["adgroup" if group else "ad"]
        item.correction = ReplacementPublic(
            correction_id=row["id"],
            source_step_id=row["source_step_id"],
            remote_id=row["remote_adgroup_id"] if group else row["remote_ad_id"],
            remote_adgroup_id=row["remote_adgroup_id"],
            original_adgroup_id=row["original_adgroup_id"],
            operation_status=facts["operation_status"],
            review_status=facts.get("review_status"),
            checked_at=row["verified_at"],
        )
        item.can_historical_read = False


def get_submission_materials(
    session: Session,
    *,
    context: TenantContext,
    submission_id: UUID,
    unit_id: UUID,
    group_id: UUID,
    cursor: str | None = None,
    limit: int = 50,
) -> Page[SubmissionMaterialPublic]:

    authorize(session, context)
    submission = submission_row(session, context, submission_id)
    scope, _ = _page_scope(
        context,
        submission,
        "group_materials",
        limit,
        None,
        {"unit": str(unit_id), "group": str(group_id)},
    )
    raw = decode_cursor(cursor, scope=scope)
    try:
        after = int(raw) if raw else 0
        if after < 0:
            raise ValueError
    except ValueError, TypeError:
        raise DomainError("invalid_cursor", "分页游标无效") from None
    params = {
        "tenant": context.tenant_id,
        "submission": submission_id,
        "preview": submission.preview_id,
        "unit": unit_id,
        "group": group_id,
        "after": after,
        "size": limit + 1,
    }
    group = (
        cast(SASession, session)
        .execute(
            text(
                "SELECT id FROM planned_group WHERE tenant_id=:tenant AND preview_id=:preview AND unit_id=:unit AND id=:group"
            ),
            params,
        )
        .first()
    )
    if not group:
        raise DomainError("resource_not_found", "素材组不存在")
    total = _scalar_count(
        session,
        """SELECT count(*) FROM planned_group g
JOIN frozen_group_material m ON m.tenant_id=g.tenant_id
AND m.preview_id=g.preview_id AND m.drama_id=g.drama_id
AND m.group_no=g.group_no
AND NOT EXISTS (SELECT 1 FROM preview_skipped_material skipped
 WHERE skipped.tenant_id=m.tenant_id AND skipped.unit_id=g.unit_id AND skipped.material_id=m.material_id)
WHERE g.tenant_id=:tenant AND g.preview_id=:preview
AND g.unit_id=:unit AND g.id=:group""",
        params,
    )
    rows = (
        cast(SASession, session)
        .execute(
            text(
                """WITH page AS (
 SELECT m.* FROM planned_group g JOIN frozen_group_material m ON m.tenant_id=g.tenant_id AND m.preview_id=g.preview_id AND m.drama_id=g.drama_id AND m.group_no=g.group_no
 AND NOT EXISTS (SELECT 1 FROM preview_skipped_material skipped WHERE skipped.tenant_id=m.tenant_id AND skipped.unit_id=g.unit_id AND skipped.material_id=m.material_id)
 WHERE g.tenant_id=:tenant AND g.preview_id=:preview AND g.unit_id=:unit AND g.id=:group AND m.position>:after ORDER BY m.position LIMIT :size)
 SELECT m.material_id,m.position,f.file_name,f.media_type,f.storage_state='stored' preview_available,
 CASE WHEN e.status='SUCCEEDED' THEN e.resolved->'mapping'->>'video_id' ELSE NULL END video_id,
 CASE WHEN e.status='SUCCEEDED' THEN e.resolved->'mapping'->>'image_id' ELSE NULL END image_id,
 """
                + STEP_JSON
                # 原件 BC 仅记录来源；本次范围与目标 ID 由冻结预览及执行步骤约束。
                + """ step FROM page m JOIN material_file f ON f.tenant_id=m.tenant_id AND f.id=m.material_id
 LEFT JOIN execution_step e ON e.tenant_id=m.tenant_id AND e.submission_id=:submission AND e.unit_id=:unit AND e.kind='MATERIAL' AND e.material_id=m.material_id ORDER BY m.position"""
            ),
            params,
        )
        .mappings()
        .all()
    )
    items = [
        SubmissionMaterialPublic(
            **{
                k: r[k]
                for k in [
                    "material_id",
                    "position",
                    "file_name",
                    "preview_available",
                    "media_type",
                    "video_id",
                    "image_id",
                ]
            },
            step=step_public(r),
        )
        for r in rows[:limit]
    ]
    return Page(
        items=items,
        next_cursor=encode_cursor(scope=scope, last_id=str(items[-1].position))
        if len(rows) > limit
        else None,
        total=total,
    )
