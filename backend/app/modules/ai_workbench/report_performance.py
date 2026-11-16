"""报告封面指标来自已归档的确定性分析，不从模型正文猜金额或拼接币种。"""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlmodel import col

from app.modules.ai_workbench.review_store import ReviewArtifact, scoped


def report_performance(session, task, *, now=None):
    now = now or datetime.now(UTC)
    captured = task.evidence.get("captured_at")
    try:
        cutoff = datetime.fromisoformat(captured)
        age_hours = max(0, (now - cutoff).total_seconds() / 3600)
    except TypeError, ValueError:
        age_hours = None
    # 只投影金额/覆盖和短趋势；不加载分析中的大量来源引用、排名或冻结原始数据。
    payload = ReviewArtifact.payload
    artifacts = session.execute(
        scoped(ReviewArtifact, task)
        .with_only_columns(
            ReviewArtifact.id,
            ReviewArtifact.kind,
            payload["request"].label("request"),
            payload["all_totals"].label("totals"),
            payload["coverage"].label("coverage"),
            payload["partitions"].label("partitions"),
        )
        .where(col(ReviewArtifact.kind).in_(["analysis", "timeline"]))
        .order_by(ReviewArtifact.created_at, ReviewArtifact.id)
    ).all()
    windows, timelines = {}, {}
    for row in artifacts:
        request = row.request or {}
        if request.get("advertiser_id"):
            continue
        if row.kind == "analysis" and request.get("dimension") == "account":
            horizon = request.get("maturity_horizon")
            if horizon not in {"d0", "d3", "d7"}:
                continue
            totals = []
            for item in row.totals or []:
                if item.get("spend") is None:
                    continue
                revenue = item.get(f"revenue_{horizon}")
                spend = item.get("spend")
                try:
                    difference = str(Decimal(revenue) - Decimal(spend))
                except InvalidOperation, TypeError:
                    difference = None
                totals.append(
                    {
                        key: item.get(key)
                        for key in (
                            "currency",
                            "timezone",
                            "attribution",
                            "start_date",
                            "end_date",
                            "spend",
                            "roas",
                            "maturity_status",
                            "eligible_buckets",
                            "excluded_buckets",
                        )
                    }
                    | {"revenue": revenue, "revenue_minus_spend": difference}
                )
            windows[horizon] = {
                "horizon": horizon,
                "partitions": totals,
                "coverage": row.coverage or {},
            }
        elif row.kind == "timeline" and request.get("dimension") in {
            "campaign",
            "material",
        }:
            dimension = request["dimension"]
            # 不同分页产物可属于同一分析，按完整分区键去重而非重复求和。
            for item in row.partitions or []:
                key = (
                    dimension,
                    item.get("currency"),
                    item.get("timezone"),
                    item.get("start_date"),
                    item.get("end_date"),
                )

                def days(values):
                    return [
                        {
                            field: day.get(field)
                            for field in (
                                "date",
                                "spend",
                                "revenue_total",
                                "roas",
                                "complete_accounts",
                                "observed_accounts",
                            )
                        }
                        for day in values
                    ]

                panel = item.get("balanced_panel") or {}
                timelines[key] = {
                    "dimension": dimension,
                    "currency": item.get("currency"),
                    "timezone": item.get("timezone"),
                    "accounts": item.get("accounts"),
                    "start_date": item.get("start_date"),
                    "end_date": item.get("end_date"),
                    "daily": days(item.get("daily", [])),
                    "balanced_accounts": panel.get("accounts"),
                    "balanced_daily": days(panel.get("daily", [])),
                }
    return {
        "captured_at": captured,
        "historical_reanalysis": bool(task.evidence.get("reanalysis")),
        "age_hours": round(age_hours, 1) if age_hours is not None else None,
        "windows": [windows[h] for h in ("d0", "d3", "d7") if h in windows],
        "timelines": list(timelines.values()),
    }
