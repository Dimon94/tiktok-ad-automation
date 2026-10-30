"""领取前重新评估尚未开始的目录，既有分页与消息身份不参与降频。"""

from datetime import datetime

from sqlmodel import Session

from app.modules.ads.directory_calibration import (
    PERIODIC_KEY,
    calibration_interval,
    recent_calibration,
    unstarted_without_delivery,
)
from app.modules.ads.sync import directory_run_superseded
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting.store_models import AdSyncBc


def review_unstarted_directory(
    session: Session, run: AdDirectoryRun, now: datetime
) -> bool:
    """返回 True 表示本轮不派发；所有状态修改由调用方同一领取事务提交。"""
    if run.query.get("ids") or run.query.get("parent_ids"):
        return False
    if not unstarted_without_delivery(session, run):
        return False
    if directory_run_superseded(session, run):
        run.status, run.error_code = "STALE", "directory_run_stale"
        run.completed_at = now
        return True
    # 未标记的历史运行来源不明，不能凭后台队列或无消耗推断为周期请求。
    if run.query.get(PERIODIC_KEY) is not True:
        return False
    plan = session.get(AdSyncBc, (run.tenant_id, run.bc_id))
    if (
        plan is None
        or plan.actor_id != run.actor_id
        or plan.frozen_route != run.frozen_route
    ):
        return False
    interval = calibration_interval(plan, run.advertiser_id, now)
    refreshed_at = recent_calibration(
        session,
        tenant_id=run.tenant_id,
        advertiser_id=run.advertiser_id,
        bc_id=run.bc_id,
        actor_id=run.actor_id,
        frozen_route=run.frozen_route,
        partition_key=run.partition_key,
        cutoff=now - interval,
    )
    if refreshed_at is None:
        return False  # 首次初始化/确实过期的完整校准仍然执行。
    run.next_attempt_at = refreshed_at + interval
    run.error_code = "periodic_directory_not_due"
    return True
