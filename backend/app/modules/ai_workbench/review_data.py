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
    observed_cutoff: datetime | None = None,
    advertiser_ids: tuple[str, ...] | None = None,
    authorized_ids: frozenset[str] | None = None,
) -> dict:
    from sqlalchemy import text

    from app.modules.ai_workbench.review_reporting import freeze_report
    from app.modules.business.models import (
        BusinessDay,
        BusinessPublication,
        BusinessRun,
    )
    from app.modules.operation_logs.models import OperationExport, OperationLogEvent
    from app.modules.reporting.read_models.models import (
        AccountReportDayState,
        ReadBucketState,
    )
    from app.modules.reporting.store_models import ReportSyncState

    cutoff = cutoff or datetime.now(UTC)
    observed_cutoff = observed_cutoff or cutoff
    selected = (
        tuple(version.advertiser_ids) if advertiser_ids is None else advertiser_ids
    )
    if not set(selected).issubset(set(version.advertiser_ids)):
        raise HTTPException(403, "account_outside_review_scope")
    # 后台批次已核验完整授权范围；普通直接调用仍独立鉴权。
    allowed = (
        set(authorized_ids)
        if authorized_ids is not None
        else {
            grant.advertiser_id
            for grant in authorized_grants(
                session, context=context, bc_id=version.bc_id, advertiser_ids=selected
            )
        }
    )
    ids = sorted(allowed & set(selected))
    from app.modules.reporting.read_models.evidence import begin_evidence_snapshot

    begin_evidence_snapshot(session)
    accounts = []
    reports = []
    business = []
    events = []
    for advertiser_id in ids:
        account = session.get(AdvertiserAccount, (context.tenant_id, advertiser_id))
        window = account_window(account.timezone if account else "", cutoff)
        window.update(
            advertiser_id=advertiser_id, currency=account.currency if account else None
        )
        accounts.append(window)
        if window["status"] != "AVAILABLE":
            continue
        start = date.fromisoformat(window["maturity_windows"]["d7"]["start_date"])
        end = date.fromisoformat(window["complete_through"])
        states = {}
        state_queries = {}
        state_fingerprints = {}
        for key, model, day_column in (
            ("account", AccountReportDayState, AccountReportDayState.stat_date),
            ("publications", ReportSyncState, ReportSyncState.stat_date),
            ("material", ReadBucketState, ReadBucketState.bucket),
        ):
            query = select(model).where(
                model.tenant_id == context.tenant_id,
                model.advertiser_id == advertiser_id,
                day_column >= start,
                day_column <= end,
                *(
                    [ReadBucketState.kind == "creative_material"]
                    if key == "material"
                    else []
                ),
            )
            query = query.order_by(
                day_column,
                *([ReportSyncState.report_kind] if key == "publications" else []),
            )
            state_queries[key] = query
            states[key] = session.exec(query).all()
            state_fingerprints[key] = [
                row.model_dump(mode="json") for row in states[key]
            ]
        states["campaign"] = states["drama"] = [
            s for s in states["publications"] if s.report_kind == "ad_day"
        ]
        # 整个账户只设置一次查询参数，避免12份临时快照反复切换。
        session.execute(text("SET LOCAL jit = off"))
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
                        report = freeze_report(
                            session,
                            context=context,
                            bc_id=version.bc_id,
                            filters=filters,
                            states=states,
                            cutoff=observed_cutoff,
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
                        "captured_at": observed_cutoff.isoformat(),
                        "requested_at": cutoff.isoformat(),
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
                            cutoff=observed_cutoff,
                            source_states=states[dimension],
                        ),
                        **report,
                    }
                )
        rows = session.exec(
            select(BusinessDay)
            .join(
                BusinessPublication,
                (BusinessDay.tenant_id == BusinessPublication.tenant_id)
                & (BusinessDay.bc_id == BusinessPublication.bc_id)
                & (BusinessDay.run_id == BusinessPublication.run_id),
            )
            .join(
                BusinessRun,
                (BusinessRun.tenant_id == BusinessDay.tenant_id)
                & (BusinessRun.id == BusinessDay.run_id),
            )
            .where(
                BusinessRun.finished_at <= cutoff,
                BusinessDay.tenant_id == context.tenant_id,
                BusinessDay.bc_id == version.bc_id,
                BusinessDay.advertiser_id == advertiser_id,
                BusinessDay.stat_date >= date.fromisoformat(window["start_date"]),
                BusinessDay.stat_date <= date.fromisoformat(window["complete_through"]),
            )
            .order_by(BusinessDay.stat_date, BusinessDay.campaign_id)
        ).all()
        business.append(
            {
                "advertiser_id": advertiser_id,
                "status": "AVAILABLE" if rows else "MISSING",
                "rows_truncated": False,
                "rows": [
                    r.model_dump(mode="json", exclude={"provider_rate", "rebate_rate"})
                    for r in rows
                ],
            }
        )
        logs = session.exec(
            select(OperationLogEvent, OperationExport.advertiser_id)
            .join(OperationExport)
            .where(
                OperationExport.tenant_id == context.tenant_id,
                OperationExport.bc_id == version.bc_id,
                OperationExport.advertiser_id == advertiser_id,
                OperationExport.observed_at <= cutoff,
                OperationLogEvent.occurred_at >= cutoff - timedelta(days=WINDOW_DAYS),
                OperationLogEvent.occurred_at < cutoff,
            )
            .order_by(col(OperationLogEvent.occurred_at).desc())
        ).all()
        events.append(
            {
                "advertiser_id": advertiser_id,
                "status": "AVAILABLE" if logs else "MISSING",
                "rows_truncated": False,
                "rows": [
                    {**row.model_dump(mode="json"), "advertiser_id": aid}
                    for row, aid in logs
                ],
            }
        )
        # READ COMMITTED 下同步可能在各维度读取之间发布；发现任何源代数变化就
        # 重试整个账户，不能把不同出版版本的金额/覆盖冻结成同一份来源。
        from app.core.errors import DomainError

        for key, query in state_queries.items():
            current = session.exec(
                query.execution_options(populate_existing=True)
            ).all()
            if [row.model_dump(mode="json") for row in current] != state_fingerprints[
                key
            ]:
                raise DomainError(
                    "ai_review_publication_changed", "冻结期间报表已更新，请重试"
                )
    return sanitize_public(
        {
            "contract": "ai-review-data-v1",
            "cutoff": cutoff,
            "window_days": WINDOW_DAYS,
            "accounts": accounts,
            "accounts_truncated": False,
            "excluded_accounts": sorted(set(selected) - allowed),
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
                "accounts": len(selected),
                "rows_per_dimension_per_horizon": None,
                "maturity_horizons": 3,
                "days_per_horizon": WINDOW_DAYS,
            },
            "interpretation": "Only selected optimizer accounts; shared-account delivery is not attributable to this optimizer without build-object mapping. Each horizon uses its own mature local-day window. Only maturity_evidence eligible_metric_keys with AVAILABLE maturity evidence establish that horizon performance; other metric columns are raw published context.",
        }
    )
