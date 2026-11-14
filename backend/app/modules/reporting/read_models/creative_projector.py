"""三源修订保护的创意素材投影；不覆盖旧素材数据、不人为分配金额。"""

import json
from datetime import timedelta
from typing import Any

from sqlalchemy import text

from app.modules.reporting.read_models.dirty_buckets import parameters
from app.modules.reporting.read_models.projector import ProjectionResult
from app.modules.reporting.store_models import METRIC_COLUMNS

# 调度相邻读取的工程容差，不是上游实时性保证；超出时差额保持未知、规则不可执行。
COMPARABLE_OBSERVATION_SKEW = timedelta(minutes=30)


def _insert_sql() -> str:
    columns = ",".join(METRIC_COLUMNS)
    partial = ",".join(f"{c}_has_null" for c in METRIC_COLUMNS)
    fact_values = ",".join(f"f.{c}" for c in METRIC_COLUMNS)
    fact_nulls = ",".join(f"f.{c} IS NULL" for c in METRIC_COLUMNS)
    unknown = " OR ".join(f"f.{c} IS NULL" for c in METRIC_COLUMNS)
    sums = ",".join(
        f"CASE WHEN COUNT({c})=COUNT(*) THEN SUM({c}) END AS {c}"
        for c in METRIC_COLUMNS
    )
    differences = ",".join(
        f"CASE WHEN :ready AND NOT (COALESCE(a.n,0)>0 AND a.{c} IS NULL) "
        f"AND NOT (COALESCE(c.n,0)>0 AND c.{c} IS NULL) "
        f"THEN COALESCE(a.{c},0)-COALESCE(c.{c},0) END AS {c}"
        for c in METRIC_COLUMNS
    )
    residual_values = ",".join(f"r.{c}" for c in METRIC_COLUMNS)
    residual_nulls = ",".join(f"r.{c} IS NULL" for c in METRIC_COLUMNS)
    nonzero_or_unknown = " OR ".join(
        f"r.{c} IS NULL OR r.{c}<>0" for c in METRIC_COLUMNS
    )
    return f"""
    WITH facts AS MATERIALIZED (
        SELECT * FROM creative_report_day
        WHERE tenant_id=:tenant AND advertiser_id=:advertiser AND stat_date=:bucket
    ), mapped AS (
        SELECT f.*,m.ad_remote_id,m.ad_material_id,m.material_type,m.platform_material_id,
            m.name,m.assets,m.identity_status,m.evidence AS identity_evidence
        FROM facts f LEFT JOIN ad_creative m
          ON m.tenant_id=f.tenant_id AND m.advertiser_id=f.advertiser_id AND m.creative_id=f.creative_id
    ), ads AS (
        SELECT adgroup_id,currency,timezone,MAX(campaign_id) AS campaign_id,COUNT(*) AS n,{sums}
        FROM ad_report_day WHERE tenant_id=:tenant AND advertiser_id=:advertiser AND stat_date=:bucket
        GROUP BY adgroup_id,currency,timezone
    ), creatives AS (
        SELECT adgroup_id,currency,timezone,MAX(campaign_id) AS campaign_id,COUNT(*) AS n,{sums}
        FROM facts GROUP BY adgroup_id,currency,timezone
    ), residual AS (
        SELECT COALESCE(a.adgroup_id,c.adgroup_id) AS adgroup_id,
            COALESCE(a.campaign_id,c.campaign_id) AS campaign_id,
            COALESCE(a.currency,c.currency) AS currency,COALESCE(a.timezone,c.timezone) AS timezone,
            {differences}
        FROM ads a FULL JOIN creatives c USING(adgroup_id,currency,timezone)
    )
    INSERT INTO creative_material_entity_day (
        tenant_id,advertiser_id,stat_date,currency,timezone,fetched_at,source_revision,
        fact_key,row_key,creative_id,ad_remote_id,ad_material_id,campaign_id,adgroup_id,
        material_type,platform_material_id,material_name,assets,attribution_status,source_status,source_info,evidence,
        {columns},{partial}
    )
    SELECT f.tenant_id,f.advertiser_id,f.stat_date,f.currency,f.timezone,f.fetched_at,:revision,
        'creative:'||f.creative_id,
        CASE WHEN f.identity_status='RESOLVED' AND f.platform_material_id IS NOT NULL
            THEN jsonb_build_array('material',f.material_type,f.platform_material_id)::text
            WHEN f.identity_status='COMPOSITE' THEN jsonb_build_array('carousel',f.advertiser_id,f.creative_id)::text
            ELSE jsonb_build_array('unmapped_creative',f.advertiser_id,f.creative_id)::text END,
        f.creative_id,f.ad_remote_id,f.ad_material_id,f.campaign_id,f.adgroup_id,
        CASE WHEN f.identity_status IN ('RESOLVED','COMPOSITE') THEN COALESCE(f.material_type,'UNKNOWN') ELSE 'UNKNOWN' END,
        CASE WHEN f.identity_status='RESOLVED' THEN f.platform_material_id END,
        COALESCE(NULLIF(f.name,''),'未关联创意 '||f.creative_id),COALESCE(f.assets,'[]'::jsonb),
        CASE WHEN f.identity_status IN ('RESOLVED','COMPOSITE') THEN 'RESOLVED' ELSE 'UNMAPPED' END,
        CASE WHEN NOT :ready THEN 'STALE' WHEN f.identity_status NOT IN ('RESOLVED','COMPOSITE')
            OR f.identity_status IS NULL OR {unknown} THEN 'PARTIAL' ELSE 'COMPLETE' END,
        CAST(:source_info AS jsonb) || jsonb_build_object('identity_status',f.identity_status,'identity_evidence',COALESCE(f.identity_evidence,'{{}}'::jsonb)),f.evidence,{fact_values},{fact_nulls}
    FROM mapped f
    UNION ALL
    SELECT :tenant,:advertiser,CAST(:bucket AS date),r.currency,r.timezone,clock_timestamp(),:revision,
        'difference:'||r.adgroup_id,jsonb_build_array('unattributed',:advertiser,r.adgroup_id)::text,
        NULL,NULL,NULL,r.campaign_id,r.adgroup_id,'RECONCILIATION',NULL,'未归属到素材／对账差额','[]'::jsonb,
        'RESIDUAL',CASE WHEN :ready THEN 'PARTIAL' ELSE 'STALE' END,CAST(:source_info AS jsonb),NULL::jsonb,
        {residual_values},{residual_nulls}
    FROM residual r WHERE {nonzero_or_unknown}
    """


def project_creative_bucket(session, key, *, expected_version):
    """输入修订行锁覆盖三源写入；旧修订不能确认或覆盖后继版本。"""
    params = parameters(key) | {"revision": expected_version}
    row = (
        session.connection()
        .execute(
            text("""
        SELECT source_revision,derived_revision FROM read_bucket_state
        WHERE tenant_id=:tenant AND advertiser_id=:advertiser AND kind=:kind AND bucket=:bucket
        FOR UPDATE
    """),
            params,
        )
        .first()
    )
    if row is None or row.source_revision != expected_version:
        return ProjectionResult(False, row.source_revision if row else 0)
    if row.derived_revision == expected_version:
        return ProjectionResult(True, expected_version)
    states = (
        session.connection()
        .execute(
            text("""
        SELECT report_kind,status,observed_at,publication_version FROM report_sync_state
        WHERE tenant_id=:tenant AND advertiser_id=:advertiser AND stat_date=:bucket
          AND report_kind IN ('ad_day','creative_day')
    """),
            params,
        )
        .all()
    )
    by_kind = {s.report_kind: s for s in states}
    ready = len(by_kind) == 2 and all(
        s.status == "COMPLETE" and s.observed_at for s in states
    )
    observed = [s.observed_at for s in states if s.observed_at]
    if ready:
        ready = max(observed) - min(observed) <= COMPARABLE_OBSERVATION_SKEW
    info: dict[str, Any] = {
        s.report_kind: {
            "status": s.status,
            "observed_at": s.observed_at.isoformat() if s.observed_at else None,
            "version": s.publication_version,
        }
        for s in states
    }
    info["comparable"] = bool(ready)
    params |= {"ready": bool(ready), "source_info": json.dumps(info)}
    session.connection().execute(
        text("""DELETE FROM creative_material_entity_day
        WHERE tenant_id=:tenant AND advertiser_id=:advertiser AND stat_date=:bucket"""),
        params,
    )
    result = session.connection().execute(text(_insert_sql()), params)
    params |= {
        "observed": min(observed) if len(observed) == 2 else None,
        "status": "COMPLETE" if ready else "STALE",
    }
    session.connection().execute(
        text("""
        UPDATE read_bucket_state SET derived_revision=:revision,derived_observed_at=:observed,derived_status=:status
        WHERE tenant_id=:tenant AND advertiser_id=:advertiser AND kind=:kind AND bucket=:bucket AND source_revision=:revision
    """),
        params,
    )
    return ProjectionResult(True, expected_version, result.rowcount)
