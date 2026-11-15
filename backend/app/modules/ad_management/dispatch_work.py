"""管理任务共享请求窗口：线程等待网络，数据库连接只在短事务内借用。"""

import os
from collections import OrderedDict, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from threading import Lock
from uuid import UUID

from sqlalchemy import update
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.errors import DomainError
from app.integrations.tiktok.adapters.management_batches import (
    batch_endpoint,
    batch_key,
    plan_batches,
    roas_link_groups,
)
from app.modules.ad_management import execution as ex
from app.modules.ad_management.batch_execution import execute_batch
from app.modules.ad_management.models import ManagementTaskItem

_pool_lock = Lock()
_pool = None
_pool_pid = None
_pool_size = None


def _shared_pool():
    global _pool, _pool_pid, _pool_size
    with _pool_lock:
        size, pid = settings.AD_MANAGEMENT_REQUEST_CONCURRENCY, os.getpid()
        if _pool is None or _pool_pid != pid or _pool_size != size:
            # prefork之后首次使用才创建线程，禁止复用父进程已运行的SDK/client/线程。
            if _pool is not None and _pool_pid == pid:
                _pool.shutdown(wait=True)
            _pool = ThreadPoolExecutor(
                max_workers=size, thread_name_prefix="management-request"
            )
            _pool_pid, _pool_size = pid, size
        return _pool


def run_bounded(send, work) -> bool:
    """不同Celery调用共享同一池；每个调用最多预投一个窗口，避免万条future堆积。"""
    iterator = iter(work)
    pool = _shared_pool()
    pending = set()
    complete = True
    first_error = None
    for _ in range(settings.AD_MANAGEMENT_REQUEST_CONCURRENCY):
        item = next(iterator, None)
        if item is None:
            break
        pending.add(pool.submit(send, item))
    while pending:
        done, pending = wait(pending, return_when=FIRST_COMPLETED)
        for future in done:
            try:
                complete = future.result() is not False and complete
            except Exception as error:
                # 先收齐已领取工作的结果，再交Celery恢复；不能留下后台线程悬空。
                if first_error is None:
                    first_error = error
            item = next(iterator, None)
            if item is not None:
                pending.add(pool.submit(send, item))
    if first_error is not None:
        raise first_error
    return complete


def _identity(command):
    return (command.ref, command.field, command.ad_material_id)


def task_work(database_engine, item_ids: tuple[UUID, ...]):
    """同任务内规划，账户轮转；已受理项仅回读，不再进入批量修改。"""
    if not item_ids:
        return ()
    with Session(database_engine) as session:
        rows = session.exec(
            select(ManagementTaskItem)
            .where(col(ManagementTaskItem.id).in_(item_ids))
            .order_by(col(ManagementTaskItem.position))
        ).all()
        commands, ids = [], {}
        singles = []
        for item in rows:
            if (
                item.execution_result == "ACCEPTED"
                and item.observation_state == "REFRESH_PENDING"
            ):
                singles.append((False, (item.id,)))
                continue
            if item.execution_result not in {"PENDING", "NOT_SENT"}:
                continue
            try:
                command = ex._command(item)
                batch_key(command)  # 未核实类型交单项门禁落明确失败，不能中断其它项。
                # EntityRef是冻结dataclass，稳定身份不依赖字典的hash。
                key = _identity(command)
                if key in ids:
                    raise ValueError("duplicate task member")
                ids[key] = item.id
                commands.append(command)
            except Exception:
                # 旧的损坏项由既有单项门禁记录明确失败，不能阻断其它有效项。
                singles.append((False, (item.id,)))
        # 旧数据或跨块输入的同系列不同ROAS仅阻断该系列，不能让整任务卡RUNNING。
        series = {}
        for command in commands:
            if command.field == "roas" and command.original.get("campaign_id"):
                from app.integrations.tiktok.adapters.sdk_management import _smart_plus

                if _smart_plus(command):
                    scope = (
                        command.ref.tenant_id,
                        command.ref.advertiser_id,
                        command.original["campaign_id"],
                    )
                    series.setdefault(scope, []).append(command)
        conflicts = set()
        for group in series.values():
            try:
                roas_link_groups(tuple(group))
            except DomainError as error:
                if error.code != "management_roas_series_conflict":
                    raise
                conflicts.update(ids[_identity(command)] for command in group)
        if conflicts:
            session.exec(
                update(ManagementTaskItem)
                .where(
                    col(ManagementTaskItem.id).in_(conflicts),
                    col(ManagementTaskItem.claim_token).is_(None),
                    col(ManagementTaskItem.execution_result).in_(
                        ["PENDING", "NOT_SENT"]
                    ),
                )
                .values(execution_result="CONFLICT", reason="roas_series_conflict")
            )
            session.commit()
            commands = [
                command
                for command in commands
                if ids[_identity(command)] not in conflicts
            ]
        accounts = OrderedDict()
        for batch in plan_batches(tuple(commands)):
            first = batch[0]
            physical = (
                first.field in {"status", "material_status"}
                or len(batch) > 1
                or batch_endpoint(first).endswith("/budget/update/")
            )
            accounts.setdefault(first.ref.advertiser_id, deque()).append(
                (physical, tuple(ids[_identity(command)] for command in batch))
            )
    work = list(singles)
    while accounts:
        for account in list(accounts):
            work.append(accounts[account].popleft())
            if not accounts[account]:
                del accounts[account]
    return tuple(work)


def execute_task_items(database_engine, item_ids: tuple[UUID, ...]) -> bool:
    def send(work):
        physical, members = work
        if physical:
            return execute_batch(database_engine, members)
        return ex.execute_item(database_engine, members[0]) is not False

    return run_bounded(send, task_work(database_engine, item_ids))
