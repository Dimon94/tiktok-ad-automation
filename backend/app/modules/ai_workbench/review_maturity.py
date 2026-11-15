"""冻结完整当地日的成熟资格及真实指标可用性，不推算尚未观测的收入。"""

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.modules.reporting.metric_catalog import METRIC_CATALOG
from app.modules.reporting.read_models.models import (
    AccountReportDayState,
    ReadBucketState,
)
from app.modules.reporting.store_models import ReportSyncState


def maturity_evidence(
    session: Session,
    *,
    context: TenantContext,
    advertiser_id: str,
    dimension: str,
    horizon: str,
    window: dict,
    report: dict,
    cutoff: datetime,
) -> dict:
    start = date.fromisoformat(window["start_date"])
    end = date.fromisoformat(window["end_date"])
    days = window["maturity_days"]
    model = AccountReportDayState if dimension == "account" else ReportSyncState
    if dimension == "material":
        states = session.exec(
            select(ReadBucketState).where(
                col(ReadBucketState.tenant_id) == context.tenant_id,
                col(ReadBucketState.advertiser_id) == advertiser_id,
                col(ReadBucketState.kind) == "creative_material",
                col(ReadBucketState.bucket) >= start,
                col(ReadBucketState.bucket) <= end,
            )
        ).all()
        source = {
            row.bucket: (
                row.derived_status,
                row.derived_observed_at,
                row.derived_revision,
                row.source_revision == row.derived_revision
                and row.derived_revision > 0,
            )
            for row in states
        }
    else:
        conditions = (
            []
            if dimension == "account"
            else [col(ReportSyncState.report_kind) == "ad_day"]
        )
        states = session.exec(
            select(model).where(
                model.tenant_id == context.tenant_id,
                model.advertiser_id == advertiser_id,
                model.stat_date >= start,
                model.stat_date <= end,
                *conditions,
            )
        ).all()
        source = {
            row.stat_date: (
                row.status,
                row.observed_at or row.last_success_at,
                row.revision
                if isinstance(row, AccountReportDayState)
                else row.publication_version,
                True,
            )
            for row in states
        }
    entries: list[dict[str, Any]] = []
    observed_times = []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        status, observed, revision, current = source.get(
            day, ("MISSING", None, None, False)
        )
        # 日期成熟不代表收入已成熟：发布观测必须发生在该归因等待期结束之后。
        ready_at = datetime.combine(
            day + timedelta(days=days + 1), time.min, ZoneInfo(window["timezone"])
        )
        mature_observation = observed is not None and ready_at <= observed <= cutoff
        entries.append(
            {
                "date": day.isoformat(),
                "status": status or "MISSING",
                "observed_at": observed.isoformat() if observed else None,
                "publication_version": revision,
                "projection_current": current,
                "mature_after": ready_at.isoformat(),
                "observed_after_maturity": mature_observation,
            }
        )
        if observed:
            observed_times.append(observed)
    revenue_key = f"revenue_{horizon}"
    definition = next(item for item in METRIC_CATALOG if item.key == revenue_key)
    platform_field = (
        definition.material_field if dimension == "material" else definition.basic_field
    )
    buckets = report.get("summary", {}).get("buckets", [])
    availability = [
        bucket.get("availability", {}).get(revenue_key, "MISSING") for bucket in buckets
    ]
    metric_status = (
        "UNSUPPORTED"
        if platform_field is None
        else "MISSING"
        if not availability or all(item == "MISSING" for item in availability)
        else "AVAILABLE"
        if all(item == "AVAILABLE" for item in availability)
        else "PARTIAL"
    )
    complete_count = sum(
        item["status"] == "COMPLETE" and item["projection_current"] for item in entries
    )
    mature_count = sum(item["observed_after_maturity"] for item in entries)
    status = (
        "UNAVAILABLE"
        if report.get("status") == "UNAVAILABLE"
        else metric_status
        if metric_status in {"MISSING", "UNSUPPORTED"}
        else "STALE"
        if mature_count < len(entries) and complete_count == len(entries)
        else "PARTIAL"
        if complete_count < len(entries) or metric_status != "AVAILABLE"
        else "AVAILABLE"
    )
    return {
        "status": status,
        "horizon": horizon,
        "maturity_days": days,
        "expected_days": len(entries),
        "complete_days": complete_count,
        "observed_after_maturity_days": mature_count,
        "oldest_observed_at": min(observed_times).isoformat()
        if observed_times
        else None,
        "newest_observed_at": max(observed_times).isoformat()
        if observed_times
        else None,
        "days": entries,
        "revenue_metric": {
            "key": revenue_key,
            "platform_field": platform_field,
            "availability": metric_status,
            "name": definition.material_name_zh
            if dimension == "material"
            else definition.basic_name_zh,
        },
        "eligible_metric_keys": ["spend", revenue_key, f"roas_{horizon}"],
        "interpretation": "Only this horizon is evaluated for maturity. Date eligibility alone does not establish mature observed revenue. Generic revenue_total is never a substitute.",
    }
