"""链接上传复用资源队列，Beat 只唤醒持久状态恢复扫描。"""

from typing import Any
from uuid import UUID

from redis import Redis
from sqlmodel import Session

from app.core.config import settings
from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.celery_app import celery_app

from .link_worker import repair_link_items, run_link_item
from .source_uploads import UPLOAD_HARD_LIMIT
from .tasks import require_bounded_worker


@celery_app.task(
    name="materials.link_item",
    bind=True,
    time_limit=UPLOAD_HARD_LIMIT,
    soft_time_limit=UPLOAD_HARD_LIMIT - 5,
)
def link_item_task(
    self: Any, *, tenant_id: str, actor_id: str, payload: dict[str, Any]
) -> None:
    require_bounded_worker(self, hard_limit=UPLOAD_HARD_LIMIT)
    if (
        set(payload) != {"item_id", "revision"}
        or type(payload["revision"]) is not int
        or payload["revision"] < 1
    ):
        raise DomainError("invalid_asset_task", "链接上传任务参数无效")
    with Redis.from_url(settings.REDIS_URL) as redis:
        run_link_item(
            database_engine=engine,
            redis_client=redis,
            context=TenantContext(
                tenant_id=UUID(tenant_id), actor_id=UUID(actor_id), role="operator"
            ),
            item_id=UUID(payload["item_id"]),
            revision=payload["revision"],
        )


@celery_app.task(name="materials.repair_link_items", time_limit=45, soft_time_limit=40)
def repair_link_items_task(limit: int = 100) -> int:
    with Session(engine) as db, db.begin():
        return repair_link_items(db, limit=max(1, min(100, limit)))
