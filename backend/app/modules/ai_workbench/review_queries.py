"""分页读取复盘目录；目录不加载每轮完整报告和证据。"""

from uuid import UUID

from sqlalchemy import case, func
from sqlmodel import Session, col, select

from app.core.config import settings
from app.modules.ai_workbench.reviews import DSHReviewTask

ACTIVE_REVIEW_STATUSES = ("PREPARING", "PENDING", "RUNNING")


def list_review_page(
    session: Session,
    *,
    tenant_id: UUID,
    optimizer_id: UUID,
    offset: int,
    limit: int,
    status: str | None,
) -> dict:
    task = DSHReviewTask
    scope = (task.tenant_id == tenant_id, task.optimizer_id == optimizer_id)
    filters = (*scope, task.status == status) if status else scope
    result = col(task.result)

    def array_count(key):
        value = result[key]
        return case(
            (func.jsonb_typeof(value) == "array", func.jsonb_array_length(value)),
            else_=0,
        )

    query = (
        select(
            task.id,
            task.status,
            task.created_at,
            task.finished_at,
            task.optimizer_version,
            col(task.evidence)["preparation"].label("preparation"),
            result["report"]["headline"].as_string().label("headline"),
            func.left(result["summary"].as_string(), 240).label("summary"),
            array_count("proposals").label("proposal_count"),
            array_count("recommendations").label("recommendation_count"),
        )
        .where(*filters)
        .order_by(col(task.created_at).desc(), col(task.id).desc())
        .offset(offset)
        .limit(limit)
    )
    items = [dict(row._mapping) for row in session.exec(query).all()]
    total = session.exec(select(func.count()).select_from(task).where(*filters)).one()
    # 当前活动任务独立于历史页和状态筛选，查看历史时也能阻止重复发起。
    active = session.exec(
        select(
            task.id, task.status, col(task.evidence)["preparation"].label("preparation")
        )
        .where(*scope, col(task.status).in_(ACTIVE_REVIEW_STATUSES))
        .order_by(col(task.created_at).desc(), col(task.id).desc())
        .limit(1)
    ).first()
    return {
        "items": items,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(items) < total,
        "active_task": dict(active._mapping) if active else None,
        "runtime_ready": bool(settings.AI_DSH_SERVICE_TOKEN),
    }
