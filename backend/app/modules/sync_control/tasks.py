"""固定数量的唤醒负责到期头，等待scope不会各自在broker反复重试。"""

from datetime import UTC, datetime

from app.core.config import settings
from app.core.db import engine
from app.jobs.celery_app import celery_app


def dispatch_sync() -> int:
    from app.modules.sync_control.costs import settle_idle_costs

    settle_idle_costs(engine)
    if not settings.ADS_SYNC_ENABLED:
        return 0
    from app.jobs.tasks import drain_dispatch
    from app.modules.reporting.job_tasks import _dispatch_pending

    count = _dispatch_pending(engine, datetime.now(UTC), budget_seconds=4)
    if count:
        drain_dispatch()
    return count


# Beat按稳定任务名投递；保留纯函数签名供控制面调用与检查。
celery_app.task(name="sync_control.dispatch", time_limit=10, soft_time_limit=8)(
    dispatch_sync
)
