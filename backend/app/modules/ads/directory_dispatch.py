"""定向回读与批量目录共用领取合同，但不共用批量读取的排队位置。"""

from datetime import UTC, datetime

from sqlalchemy import text

from app.core.context import TenantContext
from app.jobs.tasks import register_dispatch_task
from app.modules.sync_control.dispatch_envelope import enqueue_sync_dispatch

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


def is_targeted(run) -> bool:
    return bool(run.query.get("ids") or run.query.get("parent_ids"))


def is_live_directory(run) -> bool:
    return is_targeted(run) and run.workload == "live"


def directory_step_key(run) -> str:
    prefix = TARGETED_TASK_NAME if is_live_directory(run) else TASK_NAME
    return f"{prefix}:{run.id}:{run.claim_generation}:page:{run.next_page}"


def enqueue_directory_step(
    session,
    run,
    *,
    publish_only=False,
    initial=False,
    available_at=None,
    dispatch_ready=False,
    dispatched_at=None,
) -> None:
    from app.modules.sync_control.directory_demand import (
        directory_outcome,
        register_directory_run,
    )

    register_directory_run(session, run, new_execution=initial)
    if not publish_only and not dispatch_ready:
        # 生产/续页只唤醒同一到期头；限流不会为每个scope制造一条重试消息。
        import hashlib

        from app.modules.sync_control.directory_demand import directory_endpoint

        directory_outcome(session, run)
        endpoint = hashlib.sha256(directory_endpoint(run).encode()).hexdigest()[:16]
        now = available_at or datetime.now(UTC)
        enqueue_sync_dispatch(
            session,
            context=TenantContext(
                tenant_id=run.tenant_id, actor_id=run.actor_id, role="operator"
            ),
            task_name="reporting.plan_due",
            task_key=f"sync-directory-wake:{endpoint}:{run.actor_id}:{int(now.timestamp())}",
            payload={"dispatch_only": True},
        )
        return
    dispatch_id = enqueue_sync_dispatch(
        session,
        execution=run,
        now=dispatched_at,
        context=TenantContext(
            tenant_id=run.tenant_id, actor_id=run.actor_id, role="operator"
        ),
        task_name=PUBLISH_TASK_NAME
        if publish_only
        else (TARGETED_TASK_NAME if is_live_directory(run) else TASK_NAME),
        task_key=directory_step_key(run),
        payload={"run_id": str(run.id), "claim_generation": run.claim_generation},
    )
    if initial and run.next_page == 1 and not publish_only:
        # 只有窗口锁下的首次准入推进轮次；分页续投不竞争新窗口，不改变游标。
        from app.modules.ads.directory_services import (
            directory_service,
            is_missing_status,
        )

        if directory_service(run) == "status":
            session.connection().execute(
                text("""
                INSERT INTO directory_dispatch_cursor(tenant_id,last_dispatched_at,last_status_missing)
                VALUES (:tenant,:now,:missing)
                ON CONFLICT (tenant_id) DO UPDATE SET
                  last_dispatched_at=greatest(directory_dispatch_cursor.last_dispatched_at,EXCLUDED.last_dispatched_at),
                  last_status_missing=EXCLUDED.last_status_missing
                """),
                {
                    "tenant": run.tenant_id,
                    "now": datetime.now(UTC),
                    "missing": is_missing_status(run),
                },
            )
    if available_at is not None:
        from app.jobs.models import PendingDispatch

        dispatch = session.get(PendingDispatch, dispatch_id)
        # 等待额度的续步仍走原 outbox；到期前不能发布，避免忙循环和空占租约。
        dispatch.available_at = available_at
        session.add(dispatch)
