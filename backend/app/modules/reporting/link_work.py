"""关联需求按账户归并；运行中的读取保持冻结，新需求进入独立后继。"""

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import and_, func, or_, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import aliased
from sqlmodel import col, select

from app.modules.reporting.query_budget import QueryBudgetExceeded, query_budget
from app.modules.reporting.store_models import ReportSyncJob

logger = logging.getLogger(__name__)

# 先对活动索引键分页，再读取本页 progress；已标记的大 JSON 不能每轮全部
# 解 TOAST。id 补齐唯一顺序，同一账户跨页不会遗漏；Redis 保存跨轮位置。


WAKE_CANDIDATES_SQL = """
WITH active_links AS MATERIALIZED (
    SELECT id, created_at, progress->>'dependency_probed_at' AS probed_at
    FROM report_sync_job
    WHERE kind='material_links' AND status IN ('QUEUED','WAITING')
      AND status='WAITING' AND error_code = ANY(:errors) AND due_at > :now
      AND claim_token IS NULL AND (claimed_until IS NULL OR claimed_until <= :now)
)
SELECT id FROM active_links
ORDER BY probed_at NULLS FIRST, created_at, id LIMIT :limit
"""


def _orphaned_directory_sequence(session, job, *, now):
    """仅恢复当前缺口确实需要、又被错误淘汰且失去覆盖的定向目录请求。"""
    if job.error_code != "material_directory_pending" or len(job.advertiser_ids) != 1:
        return None
    waiting_ids = job.progress.get("waiting_ids", [])
    if not waiting_ids:
        return None
    from app.modules.ads.directory_supersession import directory_query_covers
    from app.modules.ads.sync_models import AdDirectoryRun
    from app.modules.reporting.store_models import MaterialPostLink

    done = aliased(MaterialPostLink)
    # 已解析帖子即使还留有另一条未解析使用行，也不能再被旧waiting_ids唤醒。
    resolved = (
        select(done.main_material_id)
        .where(
            done.tenant_id == MaterialPostLink.tenant_id,
            done.advertiser_id == MaterialPostLink.advertiser_id,
            done.main_material_id == MaterialPostLink.main_material_id,
            func.nullif(col(done.platform_material_id), "").is_not(None),
        )
        .exists()
    )
    needed = (
        select(MaterialPostLink.ad_remote_id)
        .where(
            MaterialPostLink.tenant_id == job.tenant_id,
            MaterialPostLink.advertiser_id == job.advertiser_ids[0],
            col(MaterialPostLink.main_material_id).in_(waiting_ids),
            ~resolved,
        )
        .distinct()
        .cte("needed_link_ads")
    )
    older, covering = AdDirectoryRun, aliased(AdDirectoryRun)
    # 覆盖只要求本任务仍缺失的广告，不拿旧请求里无关的其它ID扩大恢复范围。
    needed_ids = (
        select(func.jsonb_agg(needed.c.ad_remote_id))
        .where(col(older.query)["ids"].op("?")(needed.c.ad_remote_id))
        .correlate(older)
        .scalar_subquery()
    )
    scope = SimpleNamespace(
        **{
            key: getattr(older, key)
            for key in (
                "tenant_id",
                "advertiser_id",
                "bc_id",
                "actor_id",
                "frozen_route",
                "kind",
                "ad_type",
            )
        },
        query=func.jsonb_build_object(
            "ids",
            needed_ids,
            "parent_ids",
            col(older.query)["parent_ids"],
            "include_deleted",
            True,
        ),
    )
    covered = (
        select(covering.id)
        .where(
            directory_query_covers(scope, covering),
            or_(
                col(covering.status).in_(
                    ("QUEUED", "RUNNING", "ADMISSION_WAIT", "WAITING_REMOTE")
                ),
                and_(
                    col(covering.status) == "COMPLETE",
                    col(covering.coverage) == "COMPLETE",
                    col(covering.published_version).is_not(None),
                    col(covering.request_sequence) > older.request_sequence,
                ),
                # 与正常request_link_directory相同的十分钟去重期；暂时覆盖不记
                # 已处理，防止刚失败的请求被提前标记后再次陷入数小时退避。
                col(covering.created_at) >= now - timedelta(minutes=10),
            ),
        )
        .exists()
    )
    handled = job.progress.get("dependency_recovery_sequences", [])
    return session.exec(
        select(older.request_sequence)
        .where(
            older.tenant_id == job.tenant_id,
            older.advertiser_id == job.advertiser_ids[0],
            older.bc_id == job.bc_id,
            older.actor_id == job.actor_id,
            older.frozen_route == job.frozen_route,
            older.kind == "ad",
            older.status == "STALE",
            col(older.published_version).is_(None),
            col(older.error_code).in_(("superseded", "directory_run_stale")),
            col(older.query)["include_deleted"].astext == "true",
            func.jsonb_array_length(col(older.query)["ids"]) > 0,
            ~col(older.query).op("?")("material_child_v1"),
            needed_ids.is_not(None),
            col(older.request_sequence).not_in(handled),
            ~covered,
        )
        .order_by(col(older.request_sequence))
        .limit(1)
    ).first()


def wake_link_dependencies(session, *, now, limit=50, deadline=None):
    """有界轮转复核目录证据，历史任务也可被新目录唤醒，无需等待下一次事实刷新。"""
    from app.modules.reporting.material_links import (
        DEPENDENCY_ERRORS,
        dependency_changed,
    )

    session.flush()
    rows = (
        session.connection()
        .execute(
            text(WAKE_CANDIDATES_SQL),
            {"errors": sorted(DEPENDENCY_ERRORS), "now": now, "limit": limit},
        )
        .scalars()
        .all()
    )
    count = 0
    for job_id in rows:
        remaining = (
            1.3 if deadline is None else (deadline - datetime.now(UTC)).total_seconds()
        )
        # 外层一秒片包括SQL、保存点回滚与轮转写入；最后0.2秒不再开新探针。
        # 单项选择/写入也置于保存点，不能在尾部取消时撤销此前已完成的项。
        if remaining <= 0.2:
            break
        woke = False
        try:
            with session.begin_nested(), query_budget(session, seconds=remaining - 0.1):
                # 逐条锁定并重验当前状态，已锁候选不阻挡后续；无授权推断。
                job = session.exec(
                    select(ReportSyncJob)
                    .where(
                        ReportSyncJob.id == job_id,
                        ReportSyncJob.kind == "material_links",
                        ReportSyncJob.status == "WAITING",
                        col(ReportSyncJob.error_code).in_(DEPENDENCY_ERRORS),
                        ReportSyncJob.due_at > now,
                        col(ReportSyncJob.claim_token).is_(None),
                        col(ReportSyncJob.claimed_until).is_(None)
                        | (col(ReportSyncJob.claimed_until) <= now),
                    )
                    .with_for_update(skip_locked=True)
                    .execution_options(populate_existing=True)
                ).one_or_none()
                if job is None:
                    continue
                probe_seconds = (
                    1.0
                    if deadline is None
                    else (deadline - datetime.now(UTC)).total_seconds() - 0.2
                )
                if probe_seconds <= 0:
                    break
                # 尝试位置属于外层项，证据变化属于内层探针。先保存尝试位置，
                # PG取消与收尾即使耗尽余量，也不需要再发SQL才能避免失败队首饥饿。
                job.progress = job.progress | {"dependency_probed_at": now.isoformat()}
                session.flush()
                changed = False
                recovery_sequence = None
                try:
                    # 失败只撤销探针的写入，不能抹去上面的纯尝试标记。
                    with (
                        session.begin_nested(),
                        query_budget(session, seconds=min(1.0, probe_seconds)),
                    ):
                        recovery_sequence = _orphaned_directory_sequence(
                            session, job, now=now
                        )
                        if recovery_sequence is None:
                            changed = dependency_changed(session, job)
                except (DBAPIError, QueryBudgetExceeded) as error:
                    if not _wake_retryable(error):
                        raise
                    logger.info("report link dependency probe deferred: job=%s", job_id)
                    continue
                if recovery_sequence is not None:
                    # 只提前原任务，正常执行仍重验授权及缺口；不改变恢复签名。
                    job.due_at = now
                    job.progress = job.progress | {
                        "dependency_recovery_sequences": sorted(
                            {
                                *job.progress.get("dependency_recovery_sequences", []),
                                recovery_sequence,
                            }
                        )
                    }
                    woke = True
                elif changed:
                    job.due_at, job.error_code = now, None
                    job.progress = job.progress | {"dependency_attempts": -1}
                    woke = True
                # 超时只标记尝试过，不更新真实证据签名或伪造依赖已经变化。
                if woke:
                    from app.modules.sync_control.schedule import wake_report_job

                    wake_report_job(session, job)
                session.flush()
        except (DBAPIError, QueryBudgetExceeded) as error:
            if not _wake_retryable(error):
                raise
            # 行选择/标记写入失败已回滚本项；此前项仍由调用方正常提交。
            # 不继续以快耗尽的预算探测下一项，也不计本项为成功唤醒。
            logger.info("report link dependency item deferred: job=%s", job_id)
            break
        count += int(woke)
    return count


def _wake_retryable(error):
    return isinstance(error, QueryBudgetExceeded) or (
        isinstance(error, DBAPIError)
        and getattr(error.orig, "sqlstate", None) in {"57014", "55P03", "40P01"}
    )
