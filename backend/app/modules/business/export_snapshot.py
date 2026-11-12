"""一次SQL冻结导出行值和顺序，后续批次不重新扫描全范围报表。"""

from typing import Final

from sqlalchemy import Text, cast, func, insert, literal, text
from sqlalchemy import select as sa_select
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.modules.business.calculation import AMOUNT_KEYS, SUBSIDY_KEYS
from app.modules.business.models import BusinessExport, BusinessExportRow
from app.modules.business.queries import query_dashboard, resolve_runs
from app.modules.business.query_plan import build_query_plan, sort_expression
from app.modules.business.schemas import DashboardFilter

EXPORT_SNAPSHOT_CONTRACT: Final = "scenario-completeness-v1"


def freeze_rows(session: Session, row: BusinessExport, context: TenantContext) -> None:
    if row.frozen:
        return
    filters = DashboardFilter.model_validate(
        {
            key: value
            for key, value in row.filters.items()
            if key != "_snapshot_contract"
        }
    )
    allowed, selected, end, runs = resolve_runs(session, context, filters)
    plan = build_query_plan(
        session,
        context=context,
        filters=filters,
        selected=selected,
        allowed=allowed,
        end=end,
        runs=list(runs),
    )
    grouped = plan.grouped
    order = sort_expression(grouped, filters)
    ordering = [
        order.desc().nulls_last() if filters.descending else order.asc().nulls_last(),
        grouped.c.key,
    ]
    pairs = []
    for name in (
        "key",
        "name",
        "provider_name",
        "provider_id",
        "drama_id",
        "advertiser_id",
        "campaign_id",
        *AMOUNT_KEYS,
    ):
        pairs.extend([literal(name), cast(getattr(grouped.c, name), Text)])
    payload = func.jsonb_build_object(*pairs)
    rounding = (
        grouped.c.rounding_drama_key
        if filters.dimension == "campaign"
        else literal("period")
        if filters.dimension == "day"
        else grouped.c.key
    )
    session.execute(
        insert(BusinessExportRow).from_select(
            [
                "export_id",
                "sequence",
                "tenant_id",
                "bc_id",
                "row_key",
                "rounding_group",
                "payload",
                "adjustments",
            ],
            sa_select(
                literal(row.id),
                func.row_number().over(order_by=ordering),
                literal(row.tenant_id),
                literal(row.bc_id),
                grouped.c.key,
                rounding,
                payload,
                text("'{}'::jsonb"),
            ),
        )
    )
    # 仅在同一剧/当前筛选子集内归集显示尾差，目标由稳定业务键决定，与排序无关。
    values = ", ".join(
        f"CASE WHEN count(payload->>'{key}')=count(*) THEN "
        f"round(sum((payload->>'{key}')::numeric),2)-sum(round((payload->>'{key}')::numeric,2)) END AS {key}"
        for key in SUBSIDY_KEYS
    )
    adjustments = ", ".join(
        f"'{key}', CAST(totals.{key} AS text)" for key in SUBSIDY_KEYS
    )
    session.execute(
        text(f"""
        WITH totals AS (
            SELECT rounding_group, max(row_key) AS target, {values}
            FROM business_export_row WHERE export_id=:export GROUP BY rounding_group
        ) UPDATE business_export_row AS target SET adjustments=jsonb_build_object({adjustments})
          FROM totals WHERE target.export_id=:export AND target.rounding_group=totals.rounding_group
          AND target.row_key=totals.target
    """),
        {"export": row.id},
    )
    row.rows_count = session.exec(
        select(func.count())
        .select_from(BusinessExportRow)
        .where(col(BusinessExportRow.export_id) == row.id)
    ).one()
    page = query_dashboard(
        session,
        context=context,
        filters=filters.model_copy(update={"offset": 0, "limit": 1}),
    )
    row.filters = {**row.filters, "_snapshot_contract": EXPORT_SNAPSHOT_CONTRACT}
    row.frozen, row.status = True, "RUNNING"
    session.add(row)
    # 说明来自同一版本；不在续跑时重新查询可变目录或平台。
    row.issues = list(page.issues)
