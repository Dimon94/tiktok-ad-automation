"""复盘直接冻结领域报表结果，不建立供浏览器分页的短期快照。

身份、聚合、空值、素材残差复用报表领域 SQL；分析投影不展开页面动作成员，
也不重复读取同一份指标。调用者必须先校验整个复盘账户范围。
"""

import json
from collections import defaultdict
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import text

from app.modules.reporting.queries import _summary
from app.modules.reporting.read_models.coverage import projection_coverage
from app.modules.reporting.row_set import SnapshotTotals, _ctes, _summary_select
from app.modules.reporting.schemas import MATERIAL_QUERY_CONTRACT


def freeze_report(session, *, context, bc_id, filters, cutoff, states):
    """读取单账户完整集合；与页面共享 SQL 口径，但不承担交互快照生命周期。"""
    advertisers = filters.advertiser_ids
    if len(advertisers) != 1:
        raise ValueError("review_report_requires_one_account")
    dimension = filters.dimension
    ctes, params = _ctes(
        filters,
        tenant_id=context.tenant_id,
        advertisers=advertisers,
        bc_id=bc_id,
        review_labels=True,
    )
    summary_sql = _summary_select(
        frozenset(), individual=dimension not in {"drama", "material"}, all_rows=True
    )
    row_sql = _summary_select(
        frozenset(), individual=dimension not in {"drama", "material"}, per_row=True
    )
    # 各维度全部行和合计来自同一 SQL 语句；不分页，不截断，也不在 Python 重算金额。
    coverage_source = (
        "SELECT tenant_id,advertiser_id,stat_date,status FROM material_period"
        if dimension == "material"
        else "SELECT tenant_id,advertiser_id,stat_date,status FROM account_report_day_state"
        if dimension == "account"
        else "SELECT tenant_id,advertiser_id,stat_date,status FROM report_sync_state WHERE report_kind='ad_day'"
    )
    data = session.execute(
        text(f"""{ctes},
        bad AS (SELECT DISTINCT adv FROM mb WHERE incomplete
            {"UNION SELECT advertiser_id FROM unallocated_groups" if dimension == "material" else ""})
        SELECT
          (SELECT COALESCE(jsonb_agg(to_jsonb(o) ORDER BY seq), '[]'::jsonb) FROM ordered o) AS members,
          (SELECT COALESCE(jsonb_agg(to_jsonb(s)), '[]'::jsonb) FROM ({summary_sql}) s) AS buckets,
          (SELECT COALESCE(jsonb_agg(to_jsonb(s)), '[]'::jsonb) FROM ({row_sql}) s) AS row_buckets,
          (SELECT COALESCE(array_agg(adv), ARRAY[]::text[]) FROM bad) AS bad_advs,
          (SELECT COALESCE(array_agg(DISTINCT d.day::date ORDER BY d.day::date), ARRAY[]::date[])
           FROM bad b CROSS JOIN generate_series(CAST(:start AS date), CAST(:end AS date), interval '1 day') d(day)
           WHERE NOT EXISTS (SELECT 1 FROM ({coverage_source}) s WHERE s.tenant_id=:tenant
               AND s.advertiser_id=b.adv AND s.stat_date=d.day::date AND s.status='COMPLETE')) AS bad_dates,
          {"(SELECT count(DISTINCT (advertiser_id,adgroup_id,currency,timezone)) FROM unallocated_groups)" if dimension == "material" else "0"} AS unallocated_count
    """),
        params,
    ).one()
    member_data = data.members
    if dimension == "account" and not member_data:
        raise HTTPException(409, "query_membership_unavailable")
    items = _analysis_rows(
        member_data, data.row_buckets, dimension, advertisers[0], states
    )
    selected_states = [
        s
        for s in states[dimension]
        if filters.start_date
        <= (getattr(s, "bucket", None) or s.stat_date)
        <= filters.end_date
    ]
    published = [
        s.last_success_at
        for s in states["publications"]
        if filters.start_date <= s.stat_date <= filters.end_date and s.last_success_at
    ]
    if dimension == "account":
        published = [s.last_success_at for s in selected_states if s.last_success_at]
    projection = (
        projection_coverage(selected_states) if dimension == "material" else None
    )
    observations = (
        [s.derived_observed_at for s in selected_states if s.derived_observed_at]
        if dimension == "material"
        else [
            s.observed_at or s.last_success_at
            for s in selected_states
            if s.observed_at or s.last_success_at
        ]
    )
    observed = max(observations) if observations else None
    if observed and observed > cutoff:
        return {
            "status": "UNAVAILABLE",
            "reason": "publication_after_cutoff",
            "rows": [],
        }
    bad = set(data.bad_advs)
    if data.unallocated_count:
        bad.update(advertisers)
    missing = tuple(day.isoformat() for day in data.bad_dates)
    totals = SnapshotTotals(
        len(items),
        tuple(data.buckets),
        tuple(sorted(bad)),
        missing,
        not bad,
        unallocated_group_count=data.unallocated_count,
    )
    coverage = {
        "status": "EMPTY" if not items else "COMPLETE" if not bad else "INCOMPLETE",
        "rows": len(items),
    }
    if projection is not None:
        coverage.update(
            projection=projection, unallocated_group_count=data.unallocated_count
        )
        if projection["unavailable_bucket_count"]:
            coverage["status"] = "BUILDING"
        elif bad:
            coverage["status"] = "INCOMPLETE"
    versions = {"reporting": 1}
    if dimension == "material":
        versions["material_query_contract"] = MATERIAL_QUERY_CONTRACT
    if published:
        versions["max"] = int(max(published).timestamp() * 1_000_000)
    return {
        "id": str(uuid4()),
        "projection_contract": "financial-review-v1",
        "status": coverage["status"],
        "filters": filters.model_dump(mode="json"),
        "publication_versions": versions,
        "summary": _summary(totals),
        "coverage": coverage,
        "metrics_observed_at": observed.isoformat() if observed else None,
        "rows": items,
        "rows_truncated": False,
        "total": len(items),
    }


def _analysis_rows(members, buckets, dimension, advertiser_id, states):
    """复盘只冻结分析身份和指标，不携带可执行 refs/使用位置/实时余额。

    金额在 SQL 中转文本，避免 jsonb 数值经过 float 丢精度；派生指标继续复用
    报表的 Decimal 口径。目录名称是辅助标签，不作为历史可执行性证据。
    """
    by_key = defaultdict(list)
    for bucket in buckets:
        by_key[bucket["row_key"]].append(bucket)
    labels = states.get("review_labels", {})
    account_name = states.get("account_name") or advertiser_id
    result = []
    for member in members:
        key = member["row_key"]
        values = by_key[key]
        display = {"advertiser_id": advertiser_id, "account_name": account_name}
        if dimension == "account":
            display.update(name=account_name, remote_id=advertiser_id)
        elif dimension == "material":
            identity = json.loads(key)
            name = member["review_name"] or identity[2]
            display.update(
                name=name,
                material_name=name,
                main_material_id=identity[2],
                material_type=identity[1]
                if identity[0] == "material"
                else identity[0].upper(),
            )
        else:
            rid = (
                key.split(":campaign:", 1)[1]
                if dimension == "campaign"
                else member["member_keys"][0][1]
            )
            display.update(labels.get(rid, {}))
            name = display.get("name") or rid
            if dimension == "drama" and ":drama:" in key:
                name = json.loads(key.split(":drama:", 1)[1])[0]
            display.update(name=name, remote_id=rid)
            if dimension == "drama":
                display["drama_name"] = name
        coverage = {
            "status": "INCOMPLETE"
            if any(b["incomplete"] for b in values)
            else "COMPLETE",
            "advertiser_ids": [advertiser_id],
            "action_evidence": "NOT_INCLUDED",
        }
        if dimension == "material":
            identity = json.loads(key)
            if identity[0] != "material":
                coverage["reason"] = {
                    "unattributed": "group_reconciliation_difference",
                    "unmapped_creative": "creative_identity_unmapped",
                    "carousel": "composite_creative_not_allocated_to_images",
                }.get(identity[0], "creative_identity_unavailable")
        result.append(
            {
                "row_key": key,
                "display": display,
                "metric_buckets": _summary(
                    SnapshotTotals(1, tuple(values), (), (), True)
                )["buckets"],
                "coverage": coverage,
            }
        )
    return result
