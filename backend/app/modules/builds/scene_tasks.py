"""Bounded official read consumers; repair never performs an external request."""

from typing import Any
from uuid import UUID

from redis import Redis

from app.core.config import settings
from app.core.db import engine
from app.jobs.celery_app import celery_app
from app.modules.builds.scene_jobs import (
    TASK_NAME,
    process_scene_turn,
    repair_scene_jobs,
)


@celery_app.task(
    name=TASK_NAME,
    time_limit=45,
    soft_time_limit=40,
    acks_late=True,
    reject_on_worker_lost=True,
)  # type: ignore[untyped-decorator]
def refresh_scene_task(
    *, tenant_id: str, actor_id: str, payload: dict[str, Any]
) -> None:
    with Redis.from_url(settings.REDIS_URL, decode_responses=True) as redis_client:
        process_scene_turn(
            database_engine=engine,
            redis_client=redis_client,
            tenant_id=UUID(tenant_id),
            actor_id=UUID(actor_id),
            payload=payload,
        )


@celery_app.task(name="builds.repair_scenes", time_limit=45, soft_time_limit=40)  # type: ignore[untyped-decorator]
def repair_scenes_task() -> int:
    return repair_scene_jobs(database_engine=engine)
