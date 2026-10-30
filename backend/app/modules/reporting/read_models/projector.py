"""按账户当地日原子重建；金额与展示身份同一提交，旧输入不能覆盖新修订。"""

from dataclasses import dataclass

from sqlalchemy import text
from sqlmodel import Session

from app.modules.reporting.read_models.dirty_buckets import ReadBucketKey, parameters
from app.modules.reporting.store_models import METRIC_COLUMNS


@dataclass(frozen=True)
class ProjectionResult:
    published: bool
    source_revision: int
    rows: int = 0


def _metric_columns() -> str:
    return ",".join([*METRIC_COLUMNS, *(f"{c}_has_null" for c in METRIC_COLUMNS)])


def _metric_select() -> str:
    return ",".join(
        [
            *(f"SUM(f.{c})" for c in METRIC_COLUMNS),
            *(f"bool_or(f.{c} IS NULL)" for c in METRIC_COLUMNS),
        ]
    )


def project_bucket(
    session: Session, key: ReadBucketKey, *, expected_version: int
) -> ProjectionResult:
    if key.kind not in {"ad_day", "material_day"}:
        raise ValueError("unsupported read bucket kind")
    params = parameters(key) | {"revision": expected_version}
    state = (
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
    if state is None or state.source_revision != expected_version:
        return ProjectionResult(False, state.source_revision if state else 0)
    if state.derived_revision == expected_version:
        return ProjectionResult(True, expected_version)
    table = "adgroup_report_day" if key.kind == "ad_day" else "material_entity_day"
    # 该行锁同时约束源写触发器：后继写入只能在重建提交后推进N+1，不会被清掉。
    session.connection().execute(
        text(
            f"DELETE FROM {table} WHERE tenant_id=:tenant AND advertiser_id=:advertiser AND stat_date=:bucket"
        ),
        params,
    )
    if key.kind == "ad_day":
        sql = f"""
            INSERT INTO adgroup_report_day
                (tenant_id,advertiser_id,stat_date,adgroup_id,currency,timezone,
                 campaign_id,campaign_name,adgroup_name,{_metric_columns()},fetched_at,source_revision)
            SELECT f.tenant_id,f.advertiser_id,f.stat_date,f.adgroup_id,f.currency,f.timezone,
                (array_agg(f.campaign_id ORDER BY f.fetched_at DESC,f.ad_id COLLATE "C"))[1],
                MAX(f.campaign_name),MAX(f.adgroup_name),{_metric_select()},MAX(f.fetched_at),:revision
            FROM ad_report_day f WHERE f.tenant_id=:tenant AND f.advertiser_id=:advertiser AND f.stat_date=:bucket
            GROUP BY f.tenant_id,f.advertiser_id,f.stat_date,f.adgroup_id,f.currency,f.timezone
        """
    else:
        sql = f"""
            INSERT INTO material_entity_day
                (tenant_id,advertiser_id,stat_date,row_key,main_material_type,currency,timezone,
                 main_material_name,post_ids,post_names,{_metric_columns()},fetched_at,source_revision)
            SELECT f.tenant_id,f.advertiser_id,f.stat_date,
                CASE WHEN v.vid IS NULL THEN jsonb_build_array('post',f.advertiser_id,f.main_material_id)::text
                    ELSE jsonb_build_array('material',v.vid)::text END AS row_key,
                f.main_material_type,f.currency,f.timezone,
                MAX(f.main_material_name),array_agg(f.main_material_id ORDER BY f.main_material_id COLLATE "C"),
                COALESCE(jsonb_object_agg(f.main_material_id,f.main_material_name)
                    FILTER (WHERE f.main_material_name IS NOT NULL),'{{}}'::jsonb),
                {_metric_select()},MAX(f.fetched_at),:revision
            FROM material_report_day f
            LEFT JOIN LATERAL (
                SELECT l.platform_material_id AS vid FROM material_post_link l
                WHERE l.tenant_id=f.tenant_id AND l.advertiser_id=f.advertiser_id
                    AND l.main_material_id=f.main_material_id AND l.main_material_type=f.main_material_type
                    AND l.main_material_type IN ('VIDEO_SPARK_ADS','VIDEO_NON_SPARK_ADS')
                    AND l.platform_material_id IS NOT NULL
                ORDER BY l.platform_material_id COLLATE "C" LIMIT 1
            ) v ON TRUE
            WHERE f.tenant_id=:tenant AND f.advertiser_id=:advertiser AND f.stat_date=:bucket
            GROUP BY f.tenant_id,f.advertiser_id,f.stat_date,row_key,f.main_material_type,f.currency,f.timezone
        """
    result = session.connection().execute(text(sql), params)
    session.connection().execute(
        text("""
        UPDATE read_bucket_state SET derived_revision=:revision,
            derived_observed_at=(SELECT COALESCE(s.observed_at,s.last_success_at) FROM report_sync_state s
                WHERE s.tenant_id=:tenant AND s.advertiser_id=:advertiser AND s.report_kind=:kind AND s.stat_date=:bucket),
            derived_status=(SELECT s.status FROM report_sync_state s
                WHERE s.tenant_id=:tenant AND s.advertiser_id=:advertiser AND s.report_kind=:kind AND s.stat_date=:bucket)
        WHERE tenant_id=:tenant AND advertiser_id=:advertiser AND kind=:kind AND bucket=:bucket
          AND source_revision=:revision
    """),
        params,
    )
    return ProjectionResult(True, expected_version, result.rowcount)


def rebuild_pending(session: Session, *, limit: int = 25) -> list[ProjectionResult]:
    """Worker一次仅领取有限桶；调用方提交后才对外承认派生进展。"""
    if not 1 <= limit <= 100:
        raise ValueError("invalid projection batch size")
    rows = (
        session.connection()
        .execute(
            text("""
        SELECT tenant_id,advertiser_id,kind,bucket,source_revision FROM read_bucket_state
        WHERE source_revision>derived_revision AND kind IN ('ad_day','material_day')
        ORDER BY observed_at,tenant_id,advertiser_id,kind,bucket
        LIMIT :limit FOR UPDATE SKIP LOCKED
    """),
            {"limit": limit},
        )
        .all()
    )
    return [
        project_bucket(
            session,
            ReadBucketKey(r.tenant_id, r.advertiser_id, r.kind, r.bucket),
            expected_version=r.source_revision,
        )
        for r in rows
    ]
