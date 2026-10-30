"""发布测试经真实PG/Redis派发取得租约；运输替身不消费真实平台发送。"""

import os

from redis import Redis
from sqlalchemy import func
from sqlmodel import select

from app.core.config import settings
from app.modules.reporting.store_models import ReportSyncJob
from app.modules.sync_control.dispatcher import dispatch_due
from app.modules.sync_control.occupancy import release_dispatch
from app.modules.sync_control.report_demand import adopt_report_job
from app.modules.sync_control.schedule import report_endpoint
from tests.database import require_test_redis


def claim_jobs(session, *, now, limit):
    from app.modules.reporting import job_tasks  # noqa: F401

    # 旧合同回归可直接种旧job；先经过正式离线收编入口登记冻结scope/head，
    # 不手填token/generation，也不修改原分页正文和日期。
    for job in session.exec(
        select(ReportSyncJob).where(
            ReportSyncJob.status.in_(["QUEUED", "WAITING", "RUNNING"])
        )
    ).all():
        adopt_report_job(session, job)
    session.flush()
    before = set(
        session.exec(
            select(ReportSyncJob.claim_token).where(
                ReportSyncJob.claim_token.is_not(None)
            )
        ).all()
    )
    inflight = session.exec(
        select(func.count())
        .select_from(ReportSyncJob)
        .where(ReportSyncJob.status == "RUNNING", ReportSyncJob.claimed_until > now)
    ).one()
    redis_url = os.environ.get("TEST_REDIS_URL", "")
    require_test_redis(redis_url, settings.REDIS_URL)
    with Redis.from_url(redis_url) as redis:
        dispatched = dispatch_due(
            session, redis, now=now, physical_slots=inflight + limit
        )
        # 共享后台池把目录与报表一起公平派发；只有report caller会把目录赢得的
        # 下一轮悬空。真实控制面也会做目录补位，再重试尚有容量的report。
        from app.modules.sync_control.directory_dispatch import dispatch_directories_due

        if not dispatched:
            dispatch_directories_due(session, redis, now=now, limit=limit)
            dispatch_due(session, redis, now=now, physical_slots=inflight + limit)
        session.flush()
        jobs = [
            job
            for job in session.exec(
                select(ReportSyncJob).where(
                    ReportSyncJob.status == "RUNNING",
                    ReportSyncJob.claim_token.is_not(None),
                )
            ).all()
            if job.claim_token not in before
        ]
        # 本文件消费者用transport替身直接调用execute_job，没有SDK admission交接。
        # 只释放本机queued占位；PG围栏、平台计数和真实dispatch行为均不改写。
        for job in jobs:
            endpoint = report_endpoint(job.kind)
            release_dispatch(
                redis,
                reservation_id=job.claim_token,
                app_scope=settings.TIKTOK_APP_ID,
                endpoint=endpoint,
                rate_group=endpoint,
                tenant_id=job.tenant_id,
                advertiser_id=job.advertiser_ids[0],
            )
    return jobs
