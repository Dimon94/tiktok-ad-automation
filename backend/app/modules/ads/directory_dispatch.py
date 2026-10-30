"""定向回读与批量目录共用领取合同，但不共用批量读取的排队位置。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, text
from sqlmodel import col, select

from app.core.context import TenantContext
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task

TARGETED_TASK_NAME = "ads.targeted_step"
TASK_NAME = "ads.sync_step"
PUBLISH_TASK_NAME = "ads.publish_step"
register_dispatch_task(TASK_NAME, "ads-directory")
register_dispatch_task(PUBLISH_TASK_NAME, "ads-publish")
register_dispatch_task(TARGETED_TASK_NAME, "ads-live-directory")
register_dispatch_task("reporting.plan_due", "control")
# 定向回读（在投广告及父链的状态刷新）同时在途上限。单步约 1 秒，实时目录 Worker 10 进程；
# 窗口 8 实测每 10 分钟仅完成约 230 个而新建约 330 个，排队超过 2 小时，目录 30 分钟新鲜度无法达成。
# 放到 32 让 Worker 持续有活；对 TikTok 的实际调用仍由调用准入策略限流。
TARGETED_DISPATCH_WINDOW = 32


def targeted_capacity(session, now=None, *, service=None) -> int:
    """生产者和恢复扫描共用同一事务锁；未领取的 ID 请求留在数据库。"""
    from app.jobs.models import PendingDispatch
    from app.modules.ads.directory_services import SERVICE_KEY, STATUS_SERVICE
    from app.modules.ads.material_directory_children import MATERIAL_CHILD_KEY
    from app.modules.ads.sync_models import AdDirectoryRun

    now = now or datetime.now(UTC)
    locked = (
        session.connection()
        .execute(
            text(
                "SELECT pg_try_advisory_xact_lock(hashtextextended('tkada:directory-dispatch-window', 0))"
            )
        )
        .scalar_one()
    )
    if not locked:
        return 0
    key = func.concat(
        TARGETED_TASK_NAME + ":",
        col(AdDirectoryRun.id),
        ":",
        col(AdDirectoryRun.claim_generation),
        ":page:",
        col(AdDirectoryRun.next_page),
    )
    enqueued = (
        select(PendingDispatch.id)
        .where(
            PendingDispatch.tenant_id == AdDirectoryRun.tenant_id,
            PendingDispatch.task_key == key,
            col(PendingDispatch.published_at).is_(None)
            | (col(PendingDispatch.published_at) > now - timedelta(minutes=2)),
        )
        .exists()
    )
    targeted = (
        func.coalesce(func.jsonb_array_length(AdDirectoryRun.query["ids"]), 0) > 0
    ) | (
        func.coalesce(func.jsonb_array_length(AdDirectoryRun.query["parent_ids"]), 0)
        > 0
    )
    status_service = func.coalesce(
        AdDirectoryRun.query[SERVICE_KEY].astext == STATUS_SERVICE, False
    ) & ~col(AdDirectoryRun.query).op("?")(MATERIAL_CHILD_KEY)
    occupied = enqueued | (col(AdDirectoryRun.claimed_until) > now)
    # 未标记旧请求不占用状态保底；只有真实状态需求可以将它晋升。
    other_service = ~status_service if service == STATUS_SERVICE else status_service
    eligible = (
        ~enqueued
        & (
            col(AdDirectoryRun.claimed_until).is_(None)
            | (col(AdDirectoryRun.claimed_until) <= now)
        )
        & (
            col(AdDirectoryRun.next_attempt_at).is_(None)
            | (col(AdDirectoryRun.next_attempt_at) <= now)
        )
        & (AdDirectoryRun.channel == "OFFICIAL_API")
    )
    inflight, other_inflight, other_waiting = session.exec(
        select(
            func.count().filter(occupied),
            func.count().filter(occupied & other_service),
            func.count().filter(eligible & other_service),
        )
        .select_from(AdDirectoryRun)
        .where(
            targeted,
            AdDirectoryRun.workload == "live",
            col(AdDirectoryRun.status).in_(["QUEUED", "RUNNING", "WAITING_REMOTE"]),
        )
    ).one()
    available = max(0, TARGETED_DISPATCH_WINDOW - inflight)
    if TARGETED_DISPATCH_WINDOW < 2:
        return available
    other_share = (
        TARGETED_DISPATCH_WINDOW - TARGETED_DISPATCH_WINDOW // 2
        if service == STATUS_SERVICE
        else TARGETED_DISPATCH_WINDOW // 2
    )
    # 只为另一类当前可执行需求保留空槽，没有需求时整池互借。
    reserved = max(0, other_share - other_inflight) if other_waiting else 0
    return max(0, available - reserved)


def is_targeted(run) -> bool:
    return bool(run.query.get("ids") or run.query.get("parent_ids"))


def is_live_directory(run) -> bool:
    return is_targeted(run) and run.workload == "live"


def directory_step_key(run) -> str:
    prefix = TARGETED_TASK_NAME if is_live_directory(run) else TASK_NAME
    return f"{prefix}:{run.id}:{run.claim_generation}:page:{run.next_page}"


def enqueue_directory_step(
    session, run, *, publish_only=False, initial=False, available_at=None
) -> None:
    if initial:
        from app.modules.ads.directory_services import directory_service

        if not is_live_directory(run) or not targeted_capacity(
            session, service=directory_service(run)
        ):
            return
    dispatch_id = enqueue_after_commit(
        session,
        context=TenantContext(
            tenant_id=run.tenant_id, actor_id=run.actor_id, role="operator"
        ),
        task_name=TARGETED_TASK_NAME
        if is_live_directory(run)
        else (PUBLISH_TASK_NAME if publish_only else TASK_NAME),
        task_key=directory_step_key(run),
        payload={"run_id": str(run.id), "claim_generation": run.claim_generation},
    )
    if available_at is not None:
        from app.jobs.models import PendingDispatch

        dispatch = session.get(PendingDispatch, dispatch_id)
        # 等待额度的续步仍走原 outbox；到期前不能发布，避免忙循环和空占租约。
        dispatch.available_at = available_at
        session.add(dispatch)
