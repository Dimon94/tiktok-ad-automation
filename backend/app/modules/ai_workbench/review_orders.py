"""冻结素材订单兑现事实；不把同剧匹配当作生产策略归属，不向模型传递请求密钥。"""

from sqlmodel import col, select

from app.modules.ai_workbench.models import AIOptimizerVersion
from app.modules.ai_workbench.order_models import MaterialOrder
from app.modules.ai_workbench.production_origin import production_payload


def order_in_scope(session, order, version):
    historical = session.exec(
        select(AIOptimizerVersion)
        .where(
            col(AIOptimizerVersion.tenant_id) == order.tenant_id,
            col(AIOptimizerVersion.optimizer_id) == order.optimizer_id,
            col(AIOptimizerVersion.created_at) <= order.created_at,
        )
        .order_by(col(AIOptimizerVersion.number).desc())
        .limit(1)
    ).first()
    return (
        historical is not None
        and historical.bc_id == version.bc_id
        and set(historical.advertiser_ids).issubset(set(version.advertiser_ids))
    )


def freeze_orders(session, *, context, optimizer_id, version, cutoff):
    rows = session.exec(
        select(MaterialOrder)
        .where(
            col(MaterialOrder.tenant_id) == context.tenant_id,
            col(MaterialOrder.optimizer_id) == optimizer_id,
            col(MaterialOrder.created_at) <= cutoff,
        )
        .order_by(col(MaterialOrder.created_at).desc(), col(MaterialOrder.id))
        .limit(201)
    ).all()
    return {
        "rows_truncated": len(rows) > 200,
        "rows": [
            {
                "id": str(row.id),
                "plan_row_id": str(row.plan_row_id),
                "title": row.title,
                "job_id": row.job_id,
                "strategy_ids": production_payload(row).get("selectedStrategyIds", []),
                "scheduled_start": production_payload(row).get("startTime"),
                "status": row.status,
                "error_code": row.error_code,
                "attempts": row.attempts,
                "updated_at": row.updated_at.isoformat(),
                # 上游 delivery 可能含签名 URL，只保留状态，不归档原始下载链接。
                "production_items": [
                    {
                        "title": item.get("title"),
                        "status": item.get("status"),
                        "delivery_reported": bool(item.get("delivery")),
                    }
                    for item in row.evidence.get("items", [])[:50]
                    if isinstance(item, dict)
                ],
                "interpretation": "job status/producer report is not proof that these files were used in an ad; consult explicit production lineage",
            }
            for row in rows[:200]
            if order_in_scope(session, row, version)
        ],
    }
