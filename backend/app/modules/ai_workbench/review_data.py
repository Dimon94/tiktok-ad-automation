"""从本地已发布事实主动组装有界复盘数据；不发起平台同步或外部调用。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.modules.accounts.models import AdvertiserAccount
from app.modules.ai_workbench.knowledge_refs import sanitize_public
from app.modules.ai_workbench.models import AIOptimizerVersion
from app.modules.ai_workbench.review_maturity import maturity_evidence
from app.modules.reporting.filters import authorized_grants
from app.modules.reporting.schemas import ReportingFilter

WINDOW_DAYS = 7
MAX_ACCOUNTS = 10
MAX_ROWS = 50


def account_window(timezone: str, cutoff: datetime) -> dict:
    try:
        today = cutoff.astimezone(ZoneInfo(timezone)).date()
    except ZoneInfoNotFoundError, ValueError:
        return {"status": "TIMEZONE_UNAVAILABLE", "timezone": timezone}
    complete = today - timedelta(days=1)
    return {
        "status": "AVAILABLE",
        "timezone": timezone,
        "local_today": today.isoformat(),
        "start_date": (today - timedelta(days=WINDOW_DAYS)).isoformat(),
        "complete_through": complete.isoformat(),
        # 完整日与归因成熟是两种资格。D7 至少再等待七个当地完整日。
        "maturity_windows": {
            f"d{days}": {
                "start_date": (
                    complete - timedelta(days=days + WINDOW_DAYS - 1)
                ).isoformat(),
                "end_date": (complete - timedelta(days=days)).isoformat(),
                "maturity_days": days,
            }
            for days in (0, 3, 7)
        },
        "mature_through": {
            f"d{days}": (complete - timedelta(days=days)).isoformat()
            for days in (0, 3, 7)
        },
    }


def build_data_pack(
    session: Session,
    *,
    context: TenantContext,
    version: AIOptimizerVersion,
    cutoff: datetime | None = None,
) -> dict:
    from app.modules.business.models import (
        BusinessDay,
        BusinessPublication,
        BusinessRun,
    )
    from app.modules.operation_logs.models import OperationExport, OperationLogEvent
    from app.modules.reporting.queries import freeze_review_page

    cutoff = cutoff or datetime.now(UTC)
    grants = authorized_grants(
        session,
        context=context,
        bc_id=version.bc_id,
        advertiser_ids=tuple(version.advertiser_ids),
    )
    allowed = {g.advertiser_id for g in grants}
    ids = sorted(allowed & set(version.advertiser_ids))
    accounts = []
    reports = []
    business = []
    events = []
    for advertiser_id in ids[:MAX_ACCOUNTS]:
        account = session.get(AdvertiserAccount, (context.tenant_id, advertiser_id))
        window = account_window(account.timezone if account else "", cutoff)
        window.update(
            advertiser_id=advertiser_id, currency=account.currency if account else None
        )
        accounts.append(window)
        if window["status"] != "AVAILABLE":
            continue
        # 各归因周期独立取七个成熟完整日；D7 不能复用近七日未成熟合计。
        for horizon, mature_window in window["maturity_windows"].items():
            for dimension in ("account", "campaign", "drama", "material"):
                filters = ReportingFilter(
                    dimension=dimension,
                    start_date=mature_window["start_date"],
                    end_date=mature_window["end_date"],
                    advertiser_ids=(advertiser_id,),
                )
                # 缺投影属于业务数据缺失；其他错误传播，不能把程序错误伪装为无数据。
                try:
                    with session.begin_nested():
                        report = freeze_review_page(
                            session,
                            context=context,
                            bc_id=version.bc_id,
                            filters=filters,
                            limit=MAX_ROWS,
                            freeze_limit=5000,
                            cutoff=cutoff,
                        )
                except HTTPException as error:
                    if error.status_code != 409:
                        raise
                    report = {
                        "status": "UNAVAILABLE",
                        "reason": str(error.detail),
                        "rows": [],
                    }
                reports.append(
                    {
                        "dimension": dimension,
                        "advertiser_id": advertiser_id,
                        "window": {**window, **mature_window},
                        "maturity_horizon": horizon,
                        "maturity_evidence": maturity_evidence(
                            session,
                            context=context,
                            advertiser_id=advertiser_id,
                            dimension=dimension,
                            horizon=horizon,
                            window={**mature_window, "timezone": window["timezone"]},
                            report=report,
                            cutoff=cutoff,
                        ),
                        **report,
                    }
                )
        rows = session.exec(
            select(BusinessDay)
            .join(
                BusinessPublication,
                (col(BusinessDay.tenant_id) == col(BusinessPublication.tenant_id))
                & (col(BusinessDay.bc_id) == col(BusinessPublication.bc_id))
                & (col(BusinessDay.run_id) == col(BusinessPublication.run_id)),
            )
            .join(
                BusinessRun,
                (col(BusinessRun.tenant_id) == col(BusinessDay.tenant_id))
                & (col(BusinessRun.id) == col(BusinessDay.run_id)),
            )
            .where(
                col(BusinessRun.finished_at) <= cutoff,
                col(BusinessDay.tenant_id) == context.tenant_id,
                col(BusinessDay.bc_id) == version.bc_id,
                col(BusinessDay.advertiser_id) == advertiser_id,
                col(BusinessDay.stat_date) >= date.fromisoformat(window["start_date"]),
                col(BusinessDay.stat_date)
                <= date.fromisoformat(window["complete_through"]),
            )
            .order_by(col(BusinessDay.stat_date), col(BusinessDay.campaign_id))
            .limit(MAX_ROWS + 1)
        ).all()
        business.append(
            {
                "advertiser_id": advertiser_id,
                "status": "AVAILABLE" if rows else "MISSING",
                "rows_truncated": len(rows) > MAX_ROWS,
                "rows": [
                    r.model_dump(mode="json", exclude={"provider_rate", "rebate_rate"})
                    for r in rows[:MAX_ROWS]
                ],
            }
        )
        logs = session.exec(
            select(OperationLogEvent, col(OperationExport.advertiser_id))
            .join(OperationExport)
            .where(
                col(OperationExport.tenant_id) == context.tenant_id,
                col(OperationExport.bc_id) == version.bc_id,
                col(OperationExport.advertiser_id) == advertiser_id,
                col(OperationExport.observed_at) <= cutoff,
                col(OperationLogEvent.occurred_at)
                >= cutoff - timedelta(days=WINDOW_DAYS),
                col(OperationLogEvent.occurred_at) < cutoff,
            )
            .order_by(col(OperationLogEvent.occurred_at).desc())
            .limit(MAX_ROWS + 1)
        ).all()
        events.append(
            {
                "advertiser_id": advertiser_id,
                "status": "AVAILABLE" if logs else "MISSING",
                "rows_truncated": len(logs) > MAX_ROWS,
                "rows": [
                    {**row.model_dump(mode="json"), "advertiser_id": aid}
                    for row, aid in logs[:MAX_ROWS]
                ],
            }
        )
    return sanitize_public(
        {
            "contract": "ai-review-data-v1",
            "cutoff": cutoff,
            "window_days": WINDOW_DAYS,
            "accounts": accounts,
            "accounts_truncated": len(ids) > MAX_ACCOUNTS,
            "excluded_accounts": sorted(set(version.advertiser_ids) - allowed),
            "reporting": reports,
            "business": business,
            "operation_events": events,
            "missing_is_zero": False,
            "causal_incremental_profit": None,
            "mapping_gaps": [
                "strategy_version_to_report_object",
                "material_production_strategy_to_report_object",
            ],
            "limits": {
                "accounts": MAX_ACCOUNTS,
                "rows_per_dimension_per_horizon": 5000,
                "maturity_horizons": 3,
                "days_per_horizon": WINDOW_DAYS,
            },
            "interpretation": "Only selected optimizer accounts; shared-account delivery is not attributable to this optimizer without build-object mapping. Each horizon uses its own mature local-day window. Only maturity_evidence eligible_metric_keys with AVAILABLE maturity evidence establish that horizon performance; other metric columns are raw published context.",
        }
    )
