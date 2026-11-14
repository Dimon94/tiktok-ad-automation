"""冻结来源上的确定性全量扫描；金额使用 Decimal，按口径分组后计算比值。"""

import heapq
import json
from decimal import Decimal
from uuid import uuid5

from fastapi import HTTPException

from app.core.errors import DomainError
from app.modules.ai_workbench.review_store import (
    ReviewArtifact,
    ReviewSource,
    deliver,
    scoped,
)
from app.modules.ai_workbench.reviews import _digest


def analyze_for_task(
    session,
    *,
    task,
    dimension,
    maturity_horizon,
    advertiser_id=None,
    sort_by="spend",
    descending=True,
    offset=0,
    limit=20,
    min_spend=None,
    max_spend=None,
    max_roas=None,
):
    if dimension not in {
        "account",
        "campaign",
        "drama",
        "material",
    } or maturity_horizon not in {"d0", "d3", "d7"}:
        raise HTTPException(422, "invalid_analysis_dimension")
    if not 1 <= limit <= 100 or not 0 <= offset <= 100000:
        raise HTTPException(422, "invalid_analysis_page")
    revenue = f"revenue_{maturity_horizon}"
    if sort_by not in {"spend", revenue, "roas", "profit"}:
        raise HTTPException(422, "sort_metric_must_match_maturity_horizon")
    try:
        thresholds = {
            k: Decimal(str(v)) if v is not None else None
            for k, v in (
                ("min_spend", min_spend),
                ("max_spend", max_spend),
                ("max_roas", max_roas),
            )
        }
        if any(
            v is not None and (not v.is_finite() or v < 0) for v in thresholds.values()
        ):
            raise ValueError()
        if (
            thresholds["min_spend"] is not None
            and thresholds["max_spend"] is not None
            and thresholds["min_spend"] > thresholds["max_spend"]
        ):
            raise ValueError()
    except ValueError, ArithmeticError:
        raise HTTPException(422, "invalid_analysis_threshold") from None
    scope = task.evidence["optimizer"]["advertiser_ids"]
    if advertiser_id is not None and advertiser_id not in scope:
        raise HTTPException(403, "account_outside_review_scope")
    query = scoped(ReviewSource, task).where(
        ReviewSource.category == "reporting",
        ReviewSource.dimension == dimension,
        ReviewSource.maturity_horizon == maturity_horizon,
    )
    if advertiser_id:
        query = query.where(ReviewSource.advertiser_id == advertiser_id)
    query = query.order_by(ReviewSource.advertiser_id, ReviewSource.id)
    groups = {}
    sources = []
    coverage = {
        "requested_accounts": 1 if advertiser_id else len(scope),
        "frozen_accounts": 0,
        "available_accounts": 0,
        "excluded_accounts": 0,
        "frozen_rows": 0,
        "reported_rows": 0,
        "truncated_rows": 0,
        "ranked_buckets": 0,
        "eligible_buckets": 0,
        "excluded_buckets": 0,
        "matching_buckets": 0,
    }
    seen_accounts = set()

    def candidates():
        sequence = 0
        for source in session.exec(query.execution_options(yield_per=1)):
            report = source.payload
            if _digest(report) != source.digest:
                raise DomainError("ai_evidence_changed", "冻结证据摘要不一致")
            sources.append({"id": str(source.id), "sha256": source.digest})
            seen_accounts.add(source.advertiser_id)
            rows = report.get("rows", [])
            coverage["frozen_rows"] += len(rows)
            total = report.get("total", len(rows))
            coverage["reported_rows"] += total
            coverage["truncated_rows"] += max(0, total - len(rows))
            mature_status = report.get("maturity_evidence", {}).get("status", "MISSING")
            source_eligible = (
                mature_status == "AVAILABLE"
                and not report.get("rows_truncated", False)
                and report.get("status")
                not in {"UNAVAILABLE", "MISSING", "PARTIAL", "STALE"}
            )
            coverage["available_accounts"] += int(source_eligible)
            for row in rows:
                for bucket in row.get("metric_buckets", []):
                    values, availability = (
                        bucket.get("values", {}),
                        bucket.get("availability", {}),
                    )
                    spend = (
                        Decimal(str(values["spend"]))
                        if values.get("spend") is not None
                        else None
                    )
                    earned = (
                        Decimal(str(values[revenue]))
                        if values.get(revenue) is not None
                        else None
                    )
                    eligible = (
                        source_eligible
                        and all(
                            availability.get(k) == "AVAILABLE"
                            for k in ("spend", revenue)
                        )
                        and spend is not None
                        and earned is not None
                    )
                    partition = {
                        k: bucket.get(k)
                        for k in (
                            "currency",
                            "timezone",
                            "attribution",
                            "optimization_goal",
                        )
                    }
                    partition.update(
                        dimension=dimension,
                        maturity_horizon=maturity_horizon,
                        maturity_status=mature_status,
                        start_date=report.get("window", {}).get("start_date"),
                        end_date=report.get("window", {}).get("end_date"),
                    )
                    group_key = json.dumps(partition, sort_keys=True)
                    group = groups.setdefault(
                        group_key,
                        {
                            **partition,
                            "spend": Decimal(0),
                            revenue: Decimal(0),
                            "eligible_buckets": 0,
                            "excluded_buckets": 0,
                            "source_ids": set(),
                            "filtered_spend": Decimal(0),
                            "filtered_revenue": Decimal(0),
                            "filtered_buckets": 0,
                        },
                    )
                    group["source_ids"].add(str(source.id))
                    if eligible:
                        group["spend"] += spend
                        group[revenue] += earned
                        group["eligible_buckets"] += 1
                        coverage["eligible_buckets"] += 1
                    else:
                        group["excluded_buckets"] += 1
                        coverage["excluded_buckets"] += 1
                    roas = earned / spend if eligible and spend else None
                    has_threshold = any(v is not None for v in thresholds.values())
                    matches = not has_threshold or (
                        eligible
                        and (
                            thresholds["min_spend"] is None
                            or spend >= thresholds["min_spend"]
                        )
                        and (
                            thresholds["max_spend"] is None
                            or spend <= thresholds["max_spend"]
                        )
                        and (
                            thresholds["max_roas"] is None
                            or (roas is not None and roas <= thresholds["max_roas"])
                        )
                    )
                    if matches and eligible:
                        group["filtered_spend"] += spend
                        group["filtered_revenue"] += earned
                        group["filtered_buckets"] += 1
                    # 原始值保留供复核；未成熟/不可用值不参与成熟表现排名。
                    metric = {
                        "spend": spend
                        if availability.get("spend") == "AVAILABLE"
                        else None,
                        revenue: earned if eligible else None,
                        "roas": roas,
                        "profit": earned - spend if eligible else None,
                    }[sort_by]
                    coverage["ranked_buckets"] += 1
                    if not matches:
                        continue
                    coverage["matching_buckets"] += 1
                    sequence += 1
                    item = {
                        "advertiser_id": source.advertiser_id,
                        "source_id": str(source.id),
                        "row_key": row.get("row_key"),
                        "display": row.get("display", {}),
                        "refs": row.get("refs", []),
                        "metric_bucket": bucket,
                        "partition": partition,
                        "eligible": eligible,
                        "derived_roas": str(roas) if roas is not None else None,
                        "derived_profit": str(earned - spend) if eligible else None,
                    }
                    # 固定身份次序处理并列；无值排最后，不用 0 替代缺失。
                    key = (
                        metric is None,
                        (-metric if descending else metric)
                        if metric is not None
                        else Decimal(0),
                        sequence,
                    )
                    yield key, item

    selected = heapq.nsmallest(offset + limit, candidates(), key=lambda pair: pair[0])[
        offset:
    ]
    coverage["frozen_accounts"] = len(seen_accounts)
    coverage["missing_accounts"] = coverage["requested_accounts"] - len(seen_accounts)
    coverage["excluded_accounts"] = (
        coverage["requested_accounts"] - coverage["available_accounts"]
    )
    coverage["available_accounts_percent"] = (
        str(
            Decimal(coverage["available_accounts"])
            * 100
            / coverage["requested_accounts"]
        )
        if coverage["requested_accounts"]
        else None
    )
    coverage["excluded_accounts_percent"] = (
        str(
            Decimal(coverage["excluded_accounts"])
            * 100
            / coverage["requested_accounts"]
        )
        if coverage["requested_accounts"]
        else None
    )
    totals = []
    for key in sorted(groups):
        group = groups[key]
        total_spend, total_revenue = group["spend"], group[revenue]
        totals.append(
            {
                **{k: v for k, v in group.items() if k != "source_ids"},
                "spend": str(total_spend) if group["eligible_buckets"] else None,
                revenue: str(total_revenue) if group["eligible_buckets"] else None,
                "roas": str(total_revenue / total_spend) if total_spend else None,
                "filtered_spend": str(group["filtered_spend"])
                if group["filtered_buckets"]
                else None,
                "filtered_revenue": str(group["filtered_revenue"])
                if group["filtered_buckets"]
                else None,
                "filtered_roas": str(
                    group["filtered_revenue"] / group["filtered_spend"]
                )
                if group["filtered_spend"]
                else None,
                "filtered_spend_share": str(group["filtered_spend"] / total_spend)
                if total_spend
                else None,
                "source_count": len(group["source_ids"]),
                "complete": group["excluded_buckets"] == 0,
            }
        )
    request = {
        "dimension": dimension,
        "maturity_horizon": maturity_horizon,
        "advertiser_id": advertiser_id,
        "sort_by": sort_by,
        "descending": descending,
        "offset": offset,
        "limit": limit,
        **{k: str(v) if v is not None else None for k, v in thresholds.items()},
    }
    digest = _digest({"request": request, "sources": sources})
    identity = uuid5(task.id, f"analysis:{digest}")
    result = {
        "id": str(identity),
        "request": request,
        "items": [pair[1] for pair in selected],
        "total": coverage["matching_buckets"],
        "next_offset": offset + len(selected)
        if offset + len(selected) < coverage["matching_buckets"]
        else None,
        "totals": totals[offset : offset + limit],
        "total_partitions": len(totals),
        "next_totals_offset": offset + limit if offset + limit < len(totals) else None,
        "coverage": coverage,
        "source_reference_count": len(sources),
        "interpretation": "Totals scan every frozen row, separately by currency/timezone/attribution/window/maturity. Only eligible paired spend and revenue enter totals and ratio; missing is not zero. Rankings are raw account/object observations, not optimizer causal lift. Source refs archived with this analysis.",
    }
    while (
        len(json.dumps(result, ensure_ascii=False).encode()) > 64000
        and len(result["items"]) > 1
    ):
        result["items"] = result["items"][: len(result["items"]) // 2]
        result["next_offset"] = offset + len(result["items"])
    if len(json.dumps(result, ensure_ascii=False).encode()) > 64000:
        raise DomainError(
            "ai_analysis_page_too_large",
            "分析页超过 64KB，请缩小 limit 或按 advertiser_id 查询",
        )
    payload = {**result, "source_refs": sources, "all_totals": totals}
    previous = session.get(ReviewArtifact, (task.id, identity))
    if previous is None:
        session.add(
            ReviewArtifact(
                task_id=task.id,
                id=identity,
                tenant_id=task.tenant_id,
                optimizer_id=task.optimizer_id,
                kind="analysis",
                key=digest,
                digest=_digest(payload),
                payload=payload,
            )
        )
        session.flush()
    return deliver(
        session,
        task=task,
        identity=identity,
        source_id=identity,
        digest=_digest(payload),
        payload=result,
    )
