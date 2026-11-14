"""创意素材查询的唯一事实入口：先筛选每条经济事实，再跨广告合计。"""

from sqlalchemy import text

from app.modules.reporting.store_models import ReportSyncState

# 派生覆盖必须同时确认两份源报表及身份修订；未重建的旧投影不能标完整。
PERIOD_SQL = """SELECT b.tenant_id,b.advertiser_id,b.bucket AS stat_date,b.derived_revision,
    b.derived_observed_at,
    CASE WHEN b.derived_revision>0 AND b.derived_revision=b.source_revision
        AND b.derived_status='COMPLETE' AND a.status='COMPLETE' AND c.status='COMPLETE'
        AND a.observed_at IS NOT NULL AND c.observed_at IS NOT NULL
        AND abs(extract(epoch FROM a.observed_at-c.observed_at))<=1800
        THEN 'COMPLETE' ELSE 'STALE' END AS status
    FROM read_bucket_state b
    LEFT JOIN report_sync_state a ON a.tenant_id=b.tenant_id AND a.advertiser_id=b.advertiser_id
        AND a.stat_date=b.bucket AND a.report_kind='ad_day'
    LEFT JOIN report_sync_state c ON c.tenant_id=b.tenant_id AND c.advertiser_id=b.advertiser_id
        AND c.stat_date=b.bucket AND c.report_kind='creative_day'
    WHERE b.tenant_id=:tenant AND b.advertiser_id=ANY(:advs)
        AND b.kind='creative_material' AND b.bucket BETWEEN :start AND :end"""


def creative_states(session, *, tenant_id, advertisers, start, end):
    rows = (
        session.connection()
        .execute(
            text(PERIOD_SQL),
            {
                "tenant": tenant_id,
                "advs": list(advertisers),
                "start": start,
                "end": end,
            },
        )
        .mappings()
    )
    return [
        ReportSyncState(
            tenant_id=tenant_id,
            advertiser_id=r["advertiser_id"],
            report_kind="creative_material",
            stat_date=r["stat_date"],
            status=r["status"],
            publication_version=r["derived_revision"],
            observed_at=r["derived_observed_at"],
            last_success_at=r["derived_observed_at"],
        )
        for r in rows
    ]


def filtered_ctes(filters, params, *, exact_ad=None):
    """目录只补筛选字段；不以当前目录连接扩增或丢弃历史事实。"""
    from app.modules.reporting.row_set import _entity_conditions, _text_conditions

    def conditions(alias):
        checks = _entity_conditions(filters, params, e="ae", p="p")
        if exact_ad is not None:
            checks.append(f"{alias}.ad_remote_id={params.add('exact_ad', exact_ad)}")
        checks += _text_conditions(
            filters,
            params,
            ids=f"array_remove(ARRAY[{alias}.row_key,{alias}.platform_material_id,{alias}.creative_id,{alias}.advertiser_id,{alias}.ad_remote_id,{alias}.adgroup_id,{alias}.campaign_id],NULL)",
            haystack=f"concat_ws(' ',{alias}.row_key,{alias}.platform_material_id,{alias}.creative_id,{alias}.advertiser_id,{alias}.material_name,{alias}.ad_remote_id,{alias}.adgroup_id,{alias}.campaign_id,a.name,ae.name,g.name,c.name,p.drama_name,p.provider_label)",
        )
        return " AND ".join(checks) or "TRUE"

    def joins(alias):
        return f"""LEFT JOIN advertiser_account a ON a.tenant_id={alias}.tenant_id AND a.advertiser_id={alias}.advertiser_id
        LEFT JOIN ad_entity_read ae ON ae.tenant_id={alias}.tenant_id AND ae.advertiser_id={alias}.advertiser_id AND ae.kind='ad' AND ae.remote_id={alias}.ad_remote_id
        LEFT JOIN ad_entity_read g ON g.tenant_id={alias}.tenant_id AND g.advertiser_id={alias}.advertiser_id AND g.kind='adgroup' AND g.remote_id={alias}.adgroup_id
        LEFT JOIN ad_entity_read c ON c.tenant_id={alias}.tenant_id AND c.advertiser_id={alias}.advertiser_id AND c.kind='campaign' AND c.remote_id={alias}.campaign_id
        LEFT JOIN campaign_name_current p ON p.tenant_id={alias}.tenant_id AND p.advertiser_id={alias}.advertiser_id AND p.campaign_remote_id={alias}.campaign_id"""

    fact_condition = conditions("f")
    # 无目录/文本筛选时未知与差额本身已在结果中，不需要额外扫描覆盖缺口。
    has_attribution_filter = fact_condition != "TRUE"
    return f"""creative_selected AS NOT MATERIALIZED (
        SELECT f.* FROM creative_material_entity_day f {joins("f")}
        WHERE f.tenant_id=:tenant AND f.advertiser_id=ANY(:advs)
            AND f.stat_date BETWEEN :start AND :end AND {fact_condition}
    ), directory_material AS NOT MATERIALIZED (
        SELECT u.*,jsonb_build_array('material',u.material_type,u.platform_material_id)::text AS row_key,
            u.name AS material_name,NULL::text AS creative_id,ae.parent_remote_id AS adgroup_id,g.parent_remote_id AS campaign_id
        FROM material_ad_member_current u
        LEFT JOIN ad_entity_read ae ON ae.tenant_id=u.tenant_id AND ae.advertiser_id=u.advertiser_id AND ae.kind='ad' AND ae.remote_id=u.ad_remote_id
        LEFT JOIN ad_entity_read g ON g.tenant_id=u.tenant_id AND g.advertiser_id=u.advertiser_id AND g.kind='adgroup' AND g.remote_id=ae.parent_remote_id
        WHERE u.tenant_id=:tenant AND u.advertiser_id=ANY(:advs) AND u.material_type='VIDEO'
    ), directory_selected AS NOT MATERIALIZED (
        SELECT u.* FROM directory_material u {joins("u")} WHERE {conditions("u")}
    ), coverage_ads AS NOT MATERIALIZED (
        SELECT d.*,d.ad_id AS ad_remote_id,NULL::text AS row_key,NULL::text AS platform_material_id,
            NULL::text AS creative_id,NULL::text AS material_name
        FROM ad_report_day d WHERE d.tenant_id=:tenant AND d.advertiser_id=ANY(:advs)
            AND d.stat_date BETWEEN :start AND :end
    ), coverage_ad_selected AS NOT MATERIALIZED (
        -- 广告总账和当前目录是覆盖范围证据，不产生或分配素材金额。
        SELECT d.* FROM coverage_ads d {joins("d")}
        WHERE {conditions("d")} OR EXISTS (
            SELECT 1 FROM directory_selected u WHERE u.advertiser_id=d.advertiser_id AND u.ad_remote_id=d.ad_remote_id
        )
    ), relevant_groups AS (
        SELECT advertiser_id,stat_date,adgroup_id,currency,timezone FROM coverage_ad_selected
        UNION SELECT advertiser_id,stat_date,adgroup_id,currency,timezone FROM creative_selected
    ), unallocated_groups AS (
        SELECT DISTINCT r.advertiser_id,r.stat_date,r.adgroup_id,r.currency,r.timezone
        FROM creative_material_entity_day r
        JOIN relevant_groups g USING(advertiser_id,stat_date,adgroup_id,currency,timezone)
        WHERE r.tenant_id=:tenant AND r.advertiser_id=ANY(:advs) AND r.stat_date BETWEEN :start AND :end
            AND {"TRUE" if has_attribution_filter else "FALSE"} AND r.attribution_status IN ('RESIDUAL','UNMAPPED')
            AND NOT EXISTS (SELECT 1 FROM creative_selected included
                WHERE included.advertiser_id=r.advertiser_id AND included.stat_date=r.stat_date
                    AND included.fact_key=r.fact_key AND included.currency=r.currency AND included.timezone=r.timezone)
    ), unallocated_rows AS (
        SELECT DISTINCT u.row_key,g.* FROM directory_selected u
        JOIN coverage_ads d ON d.advertiser_id=u.advertiser_id AND d.ad_remote_id=u.ad_remote_id
        JOIN unallocated_groups g ON g.advertiser_id=d.advertiser_id AND g.stat_date=d.stat_date
            AND g.adgroup_id=d.adgroup_id AND g.currency=d.currency AND g.timezone=d.timezone
    ), creative_filtered AS NOT MATERIALIZED (
        SELECT f.*,EXISTS (SELECT 1 FROM unallocated_groups g
            WHERE g.advertiser_id=f.advertiser_id AND g.stat_date=f.stat_date AND g.adgroup_id=f.adgroup_id
                AND g.currency=f.currency AND g.timezone=f.timezone) AS unallocated
        FROM creative_selected f
    ), creative_directory AS (
        SELECT u.* FROM directory_selected u
        WHERE {"TRUE" if filters.include_empty else "FALSE"} OR EXISTS (
            SELECT 1 FROM creative_selected f WHERE f.row_key=u.row_key AND f.advertiser_id=u.advertiser_id
                AND f.ad_remote_id=u.ad_remote_id AND f.ad_material_id=u.ad_material_id
                AND f.attribution_status='RESOLVED'
        )
    ), creative_uses AS (
        SELECT * FROM creative_directory WHERE ad_material_id IS NOT NULL
    )"""


def read_facts(
    session,
    *,
    tenant_id,
    advertisers,
    filters,
    row_keys=None,
    directory=False,
    exact_ad=None,
    observations=False,
):
    from app.modules.reporting.row_set import _Params

    params = _Params()
    params.values.update(
        tenant=tenant_id,
        advs=list(advertisers),
        start=filters.start_date,
        end=filters.end_date,
    )
    ctes = filtered_ctes(filters, params, exact_ad=exact_ad)
    where = ""
    if row_keys is not None:
        params.values["row_keys"] = list(row_keys)
        where = "WHERE row_key=ANY(:row_keys)"
    if observations:
        return (
            session.connection()
            .execute(
                text(f"""WITH {ctes}
            SELECT f.*,r.fetched_at AS fact_observed_at,r.sync_job_id AS fact_sync_job_id
            FROM creative_filtered f LEFT JOIN creative_report_day r
            ON r.tenant_id=f.tenant_id AND r.advertiser_id=f.advertiser_id
            AND r.creative_id=f.creative_id AND r.stat_date=f.stat_date
            {where.replace("row_key=", "f.row_key=")}
        """),
                params.values,
            )
            .mappings()
            .all()
        )
    return (
        session.connection()
        .execute(
            text(
                f"WITH {ctes} SELECT * FROM {'creative_directory' if directory else 'creative_filtered'} {where}"
            ),
            params.values,
        )
        .mappings()
        .all()
    )


def trend_rows(session, *, tenant_id, advertisers, filters, row_keys):
    """趋势也在合计前应用快照筛选，按原始行键保留素材类型隔离。"""
    from app.modules.reporting.row_set import _Params
    from app.modules.reporting.store_models import METRIC_COLUMNS

    params = _Params()
    params.values.update(
        tenant=tenant_id,
        advs=list(advertisers),
        start=filters.start_date,
        end=filters.end_date,
        row_keys=list(row_keys),
    )
    ctes = filtered_ctes(filters, params)
    columns = ",".join(
        f"sum({m}) AS {m},count({m}) AS n_{m},bool_or({m}_has_null) AS null_{m}"
        for m in METRIC_COLUMNS
    )
    return (
        session.connection()
        .execute(
            text(f"""WITH {ctes}
        SELECT stat_date AS at,currency,timezone,count(*) AS n_rows,
            bool_or(source_status<>'COMPLETE' OR unallocated) AS source_incomplete,{
                columns
            }
        FROM creative_filtered WHERE {"row_key=ANY(:row_keys)" if row_keys else "TRUE"}
        GROUP BY stat_date,currency,timezone
        UNION ALL
        SELECT DISTINCT g.stat_date,g.currency,g.timezone,0,true,
            {
                ",".join(
                    f"NULL::numeric AS {m},0 AS n_{m},true AS null_{m}"
                    for m in METRIC_COLUMNS
                )
            }
        FROM unallocated_groups g
        WHERE {
                "TRUE"
                if not row_keys
                else '''EXISTS (SELECT 1 FROM unallocated_rows u
            WHERE u.advertiser_id=g.advertiser_id AND u.stat_date=g.stat_date AND u.adgroup_id=g.adgroup_id
                AND u.currency=g.currency AND u.timezone=g.timezone AND u.row_key=ANY(:row_keys))
            OR EXISTS (SELECT 1 FROM creative_selected f WHERE f.advertiser_id=g.advertiser_id
                AND f.stat_date=g.stat_date AND f.adgroup_id=g.adgroup_id AND f.currency=g.currency
                AND f.timezone=g.timezone AND f.row_key=ANY(:row_keys))'''
            }
        ORDER BY at
    """),
            params.values,
        )
        .mappings()
        .all()
    )


UNALLOCATED_NOTE = "广告组存在未归属创意或对账差额，不能分摊到所选广告或素材。"
_SPECIAL_NAMES = {
    "unattributed": "未归属到素材／对账差额",
    "unmapped_creative": "未关联创意 ",
    "carousel": "轮播创意 ",
}


def material_display_name(key, fallback):
    label = _SPECIAL_NAMES.get(key[0])
    return (
        fallback
        if label is None
        else label + ("" if key[0] == "unattributed" else key[2])
    )


def material_name_sql(key, fallback):
    clauses = [
        f"WHEN ({key}::jsonb)->>0='{kind}' THEN '{label}'"
        + ("" if kind == "unattributed" else f"||(({key}::jsonb)->>2)")
        for kind, label in _SPECIAL_NAMES.items()
    ]
    return "CASE " + " ".join(clauses) + f" ELSE {fallback} END"


def read_unallocated(
    session, *, tenant_id, advertisers, filters, row_keys=None, exact_ad=None
):
    """覆盖缺口独立于幸存创意事实，空结果也必须带回相关组的证据。"""
    from app.modules.reporting.row_set import _Params

    params = _Params()
    params.values.update(
        tenant=tenant_id,
        advs=list(advertisers),
        start=filters.start_date,
        end=filters.end_date,
    )
    ctes = filtered_ctes(filters, params, exact_ad=exact_ad)
    source = "unallocated_groups"
    where = ""
    if row_keys is not None:
        source = "unallocated_rows"
        params.values["row_keys"] = list(row_keys)
        where = "WHERE row_key=ANY(:row_keys)"
    return (
        session.connection()
        .execute(text(f"WITH {ctes} SELECT * FROM {source} {where}"), params.values)
        .mappings()
        .all()
    )
