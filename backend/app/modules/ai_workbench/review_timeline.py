"""冻结日事实的同窗趋势与事前分层；逐账户流式，不把日后归因伪装成因果。"""

import json
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from sqlmodel import col

from app.core.errors import DomainError
from app.modules.ai_workbench.review_store import (
    ReviewArtifact,
    ReviewSource,
    archive_artifact,
    scoped,
)
from app.modules.ai_workbench.reviews import _digest


def number(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except InvalidOperation, ValueError, TypeError:
        return None


def metrics():
    return {
        "spend": Decimal(0),
        "revenue_total": Decimal(0),
        "loss_proxy": Decimal(0),
        "paired_rows": 0,
        "excluded_rows": 0,
        "positive_spend_objects": 0,
        "prior_low_efficiency_spend": Decimal(0),
        "prior_long_tail_spend": Decimal(0),
        "prior_low_efficiency_long_tail_spend": Decimal(0),
        "identity_unavailable_spend": Decimal(0),
        "identity_unavailable_rows": 0,
        "cohorts": {},
    }


def add(target, source):
    for key, value in source.items():
        if key == "cohorts":
            for name, values in value.items():
                cohort = target["cohorts"].setdefault(
                    name,
                    {
                        "spend": Decimal(0),
                        "revenue_total": Decimal(0),
                        "loss_proxy": Decimal(0),
                        "rows": 0,
                    },
                )
                for field, amount in values.items():
                    cohort[field] += amount
        else:
            target[key] += value


def rendered(values):
    spend = values["spend"]
    result = {
        k: str(v) if isinstance(v, Decimal) else v
        for k, v in values.items()
        if k != "cohorts"
    }
    if not values["paired_rows"]:
        for key, value in values.items():
            if isinstance(value, Decimal):
                result[key] = None
    result["spend"] = str(spend) if values["paired_rows"] else None
    result["revenue_total"] = (
        str(values["revenue_total"]) if values["paired_rows"] else None
    )
    result["roas"] = str(values["revenue_total"] / spend) if spend else None
    result["cohorts"] = [
        dict(
            cohort=name,
            **{k: str(v) if isinstance(v, Decimal) else v for k, v in row.items()},
            spend_share=str(row["spend"] / spend) if spend else None,
            roas=str(row["revenue_total"] / row["spend"]) if row["spend"] else None,
        )
        for name, row in sorted(values["cohorts"].items())
    ]
    for name in (
        "prior_low_efficiency",
        "prior_long_tail",
        "prior_low_efficiency_long_tail",
    ):
        result[name + "_share"] = (
            str(values[name + "_spend"] / spend) if spend else None
        )
    return result


def summarize_packs(packs, *, dimension, days, scope):
    partitions = {}
    seen = set()
    for pack in packs:
        aid = pack.get("advertiser_id")
        if aid not in scope:
            continue
        if aid in seen:
            raise DomainError("ai_evidence_changed", "同一账户存在重复日事实来源")
        seen.add(aid)
        try:
            end = date.fromisoformat(pack["end_date"])
            start = end - timedelta(days=days - 1)
            history_start = date.fromisoformat(pack["start_date"])
        except KeyError, TypeError, ValueError:
            raise DomainError("ai_evidence_changed", "冻结日事实缺少日期边界") from None
        if start < history_start:
            raise DomainError("ai_evidence_changed", "冻结历史不足以覆盖所请求窗口")
        expected = [(start + timedelta(days=i)).isoformat() for i in range(days)]
        coverage = pack.get("coverage", {})
        missing = set(coverage.get("missing_days", []))
        if not isinstance(coverage.get("missing_days"), list):
            missing.update(
                (history_start + timedelta(days=i)).isoformat()
                for i in range((end - history_start).days + 1)
            )
        if dimension == "material":
            material_coverage = pack.get("material_coverage", {})
            missing |= set(material_coverage.get("missing_days", []))
            if not isinstance(material_coverage.get("missing_days"), list):
                missing.update(
                    (history_start + timedelta(days=i)).isoformat()
                    for i in range((end - history_start).days + 1)
                )
        objects = defaultdict(list)
        for row in pack.get("material_rows" if dimension == "material" else "rows", []):
            day = row.get("stat_date")
            if not day or not history_start.isoformat() <= day <= end.isoformat():
                continue
            identity = row.get("row_key" if dimension == "material" else "campaign_id")
            key = (
                row.get("currency") or pack.get("currency"),
                row.get("timezone") or pack.get("timezone"),
                identity,
            )
            objects[key].append(row)
        account_days = {}
        # 无行不等于零；仍保留账户的范围及完整性计数。
        keys = {(k[0], k[1]) for k in objects} or {
            (pack.get("currency"), pack.get("timezone"))
        }
        for currency, timezone in keys:
            key = (currency, timezone, start.isoformat(), end.isoformat())
            account_days[key] = {d: metrics() for d in expected}
        for (currency, timezone, _identity), rows in objects.items():
            by_day = defaultdict(list)
            for row in rows:
                by_day[row["stat_date"]].append(row)
            prior_spend = prior_revenue = Decimal(0)
            has_spend = False
            prior_valid = True
            for day, current in sorted(by_day.items()):
                past_missing = any(
                    history_start.isoformat() <= d < day for d in missing
                )
                identity_ok = _identity is not None and (
                    dimension != "material"
                    or all(
                        r.get("source_status") == "COMPLETE"
                        and r.get("attribution_status") == "RESOLVED"
                        for r in current
                    )
                )
                if not identity_ok:
                    cohort = "unknown_identity"
                elif not prior_valid or past_missing:
                    cohort = "unknown_prior"
                elif not has_spend:
                    cohort = "first_observed_spend"
                elif prior_spend < 20:
                    cohort = "prior_low_spend"
                elif prior_revenue / prior_spend >= 1:
                    cohort = "prior_proven"
                else:
                    cohort = "prior_other"
                pairs = [
                    (number(r.get("spend")), number(r.get("revenue_total")))
                    for r in current
                ]
                valid = [
                    (s, r)
                    for s, r in pairs
                    if s is not None and r is not None and s >= 0
                ]
                spend = sum((s for s, _ in valid), Decimal(0))
                revenue = sum((r for _, r in valid), Decimal(0))
                if day in expected:
                    target = account_days[
                        (currency, timezone, start.isoformat(), end.isoformat())
                    ][day]
                    target["excluded_rows"] += len(pairs) - len(valid)
                    if valid:
                        target["paired_rows"] += len(valid)
                        target["positive_spend_objects"] += int(spend > 0)
                        target["spend"] += spend
                        target["revenue_total"] += revenue
                        loss = max(spend - revenue, Decimal(0))
                        target["loss_proxy"] += loss
                        c = target["cohorts"].setdefault(
                            cohort,
                            {
                                "spend": Decimal(0),
                                "revenue_total": Decimal(0),
                                "loss_proxy": Decimal(0),
                                "rows": 0,
                            },
                        )
                        c["spend"] += spend
                        c["revenue_total"] += revenue
                        c["loss_proxy"] += loss
                        c["rows"] += len(valid)
                        if not identity_ok:
                            target["identity_unavailable_spend"] += spend
                            target["identity_unavailable_rows"] += len(valid)
                        if (
                            identity_ok
                            and prior_valid
                            and not past_missing
                            and has_spend
                        ):
                            low = prior_revenue / prior_spend < Decimal("0.9")
                            tail = prior_spend < 20
                            if low:
                                target["prior_low_efficiency_spend"] += spend
                            if tail:
                                target["prior_long_tail_spend"] += spend
                            if low and tail:
                                target["prior_low_efficiency_long_tail_spend"] += spend
                if len(valid) != len(pairs) or not identity_ok:
                    prior_valid = False
                prior_spend += spend
                prior_revenue += revenue
                has_spend = has_spend or spend > 0
        for key, daily in account_days.items():
            partition = partitions.setdefault(
                key,
                {
                    "accounts": 0,
                    "balanced_accounts": 0,
                    "daily": {d: metrics() for d in expected},
                    "balanced_daily": {d: metrics() for d in expected},
                    "complete": dict.fromkeys(expected, 0),
                    "observed": dict.fromkeys(expected, 0),
                },
            )
            partition["accounts"] += 1
            # 固定同一账户集合排除日期间覆盖变化；不声称对象/流量/归因成熟度也固定。
            comparable = (
                key[0] is not None
                and key[1] is not None
                and all(
                    d not in missing
                    and daily[d]["paired_rows"] > 0
                    and daily[d]["excluded_rows"] == 0
                    and daily[d]["identity_unavailable_rows"] == 0
                    for d in expected
                )
            )
            partition["balanced_accounts"] += int(comparable)
            for day, values in daily.items():
                add(partition["daily"][day], values)
                partition["complete"][day] += int(
                    day not in missing
                    and values["excluded_rows"] == 0
                    and values["identity_unavailable_rows"] == 0
                    and key[0] is not None
                    and key[1] is not None
                )
                partition["observed"][day] += int(values["paired_rows"] > 0)
                if comparable:
                    add(partition["balanced_daily"][day], values)
    result = []
    for (currency, timezone, start, end), p in sorted(
        partitions.items(), key=lambda x: str(x[0])
    ):
        result.append(
            {
                "currency": currency,
                "timezone": timezone,
                "start_date": start,
                "end_date": end,
                "accounts": p["accounts"],
                "daily": [
                    dict(
                        date=d,
                        complete_accounts=p["complete"][d],
                        observed_accounts=p["observed"][d],
                        **rendered(v),
                    )
                    for d, v in sorted(p["daily"].items())
                ],
                "balanced_panel": {
                    "accounts": p["balanced_accounts"],
                    "daily": [
                        dict(date=d, **rendered(v))
                        for d, v in sorted(p["balanced_daily"].items())
                    ],
                },
            }
        )
    return {
        "partitions": result,
        "coverage": {
            "requested_accounts": len(scope),
            "frozen_accounts": len(seen),
            "missing_source_accounts": len(scope - seen),
        },
        "limitations": {
            "revenue": "Captured revenue_total, not D0/D3/D7 mature-window totals or historical decision-time attribution. Do not pool or compare as equivalent.",
            "cohorts": "Prior means cumulative within the frozen 35-day window, not lifetime. first_observed_spend is not launch time or a verified new drama. Missing prior publication or metrics makes prior cohort unknown. 20/0.9/1.0 are descriptive cuts, not approved business thresholds.",
            "balanced_panel": "Same accounts with observed valid pairs on every requested day; objects/mix and revenue maturity can still change. Sparse complete accounts without rows remain outside the panel.",
            "daily_iaa": "NOT_FROZEN",
            "causality": "Associations only. No CTR/CVR/retention, supply or randomized causal diagnosis. loss_proxy=sum positive object-day spend minus revenue, not net profit. This analysis does not bound possible rule effectiveness.",
        },
    }


def timeline_for_task(
    session,
    *,
    task,
    dimension="campaign",
    days=7,
    advertiser_id=None,
    offset=0,
    limit=2,
    cache_only=False,
):
    if (
        dimension not in {"campaign", "material"}
        or not 2 <= days <= 14
        or not 0 <= offset <= 100000
        or not 1 <= limit <= 4
    ):
        raise HTTPException(422, "invalid_timeline_request")
    scope = set(task.evidence["optimizer"]["advertiser_ids"])
    if advertiser_id is not None:
        if advertiser_id not in scope:
            raise HTTPException(403, "account_outside_review_scope")
        scope = {advertiser_id}
    request = {
        "dimension": dimension,
        "days": days,
        "advertiser_id": advertiser_id,
        "offset": offset,
        "limit": limit,
    }
    key = _digest({"contract": "timeline-v1", "request": request})
    cached = session.exec(
        scoped(ReviewArtifact, task).where(
            ReviewArtifact.kind == "timeline", ReviewArtifact.key == key
        )
    ).first()
    if cached:
        return cached.payload

    if cache_only:
        return {"status": "PENDING_COMPUTATION"}

    def packs():
        query = (
            scoped(ReviewSource, task)
            .where(
                ReviewSource.category == "rule_backtest_inputs",
                col(ReviewSource.advertiser_id).in_(scope),
            )
            .execution_options(yield_per=1)
        )
        for source in session.exec(query):
            if (
                source.payload.get("advertiser_id") != source.advertiser_id
                or _digest(source.payload) != source.digest
            ):
                raise DomainError("ai_evidence_changed", "冻结事实摘要不一致")
            yield source.payload

    result = summarize_packs(packs(), dimension=dimension, days=days, scope=scope)
    total = len(result["partitions"])
    result.update(
        request=request,
        total_partitions=total,
        next_offset=offset + limit if offset + limit < total else None,
        partitions=result["partitions"][offset : offset + limit],
    )
    if len(json.dumps(result).encode()) > 64000:
        raise DomainError(
            "ai_analysis_page_too_large", "逐日分析超过64KB，请缩小limit或days"
        )
    return archive_artifact(
        session, task=task, kind="timeline", key=key, payload=result
    )
