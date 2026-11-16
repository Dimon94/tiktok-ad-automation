"""补位领取提交后立即有界发送，空轮不再扫描 outbox。"""

import os
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlmodel import select

from app.core.config import settings
from app.jobs import outbox
from app.jobs.models import PendingDispatch
from app.modules.accounts.models import AdvertiserAccount
from app.modules.reporting import job_tasks
from app.modules.reporting.store_models import ReportSyncJob
from tests.modules.reporting.claiming import enqueue_single_kind as enqueue_job
from tests.modules.reporting.test_jobs import _route


def test_refill_sends_committed_claim_without_waiting_for_another_tick(
    committed_sync_seed, monkeypatch
):
    seed, factory = committed_sync_seed
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        # 实时报表按账户当地日路由；UTC 与香港跨日时也必须测试 live 发送。
        account = session.get(AdvertiserAccount, (seed.tenant_id, "acc-1"))
        today = now.astimezone(ZoneInfo(account.timezone)).date()
        identity = enqueue_job(
            session,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            kind="ad_day",
            tier="hot",
            advertiser_ids=["acc-1"],
            start=today,
            end=today,
            actor_id=seed.actor_id,
            frozen_route=_route(seed),
        )
    sent = []

    def publish(name, **kwargs):
        with factory() as observer:
            job = observer.exec(
                select(ReportSyncJob)
                .where(ReportSyncJob.id == identity)
                .with_for_update(nowait=True)
            ).one()
            assert job.status == "RUNNING"
            assert str(job.claim_token) == kwargs["kwargs"]["payload"]["claim_id"]
            # 发送期间不持有领取行锁或窗口锁；其他 Worker 可同时补位。
            assert observer.execute(
                text(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended('tkada:report-dispatch-window', 0))"
                )
            ).scalar_one()
            assert observer.execute(
                text(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended('tkada:directory-dispatch-window', 0))"
                )
            ).scalar_one()
        sent.append((name, kwargs))

    monkeypatch.setattr(outbox.celery_app, "send_task", publish)
    assert job_tasks.plan_due.run(payload={"dispatch_only": True}) == 1
    assert len(sent) == 1 and sent[0][0] == "reporting.job_step"
    with factory() as session:
        row = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.tenant_id,
                PendingDispatch.task_name == "reporting.job_step",
            )
        ).one()
        assert row.published_at is not None


@pytest.mark.usefixtures("committed_sync_seed")
def test_empty_refill_does_not_run_another_publisher_round(monkeypatch):
    from app.jobs import tasks

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])

    def unexpected(*_args, **_kwargs):
        raise AssertionError("empty refill must not scan outbox")

    monkeypatch.setattr(tasks, "drain_dispatch", unexpected)
    assert job_tasks.plan_due.run(payload={"dispatch_only": True}) == 0


def test_inline_broker_failure_keeps_committed_claim_and_delivery_identity(
    committed_sync_seed, monkeypatch
):
    from app.jobs.tasks import drain_dispatch

    seed, factory = committed_sync_seed
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        account = session.get(AdvertiserAccount, (seed.tenant_id, "acc-1"))
        today = now.astimezone(ZoneInfo(account.timezone)).date()
        identity = enqueue_job(
            session,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            kind="ad_day",
            tier="hot",
            advertiser_ids=["acc-1"],
            start=today,
            end=today,
            actor_id=seed.actor_id,
            frozen_route=_route(seed),
        )

    def unavailable(*_args, **_kwargs):
        raise ConnectionError("broker unavailable")

    monkeypatch.setattr(outbox.celery_app, "send_task", unavailable)
    assert job_tasks.plan_due.run(payload={"dispatch_only": True}) == 1
    with factory() as session, session.begin():
        job = session.get(ReportSyncJob, identity)
        assert job.status == "RUNNING"
        token = job.claim_token
        row = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.tenant_id,
                PendingDispatch.task_name == "reporting.job_step",
            )
        ).one()
        assert row.attempts == 1 and row.published_at is None and row.available_at > now
        dispatch_id = row.id
        row.available_at = datetime.now(UTC)
    sent = []
    monkeypatch.setattr(
        outbox.celery_app, "send_task", lambda name, **kwargs: sent.append(kwargs)
    )
    assert drain_dispatch() == 1
    assert sent[0]["task_id"] == str(dispatch_id)
    assert sent[0]["kwargs"]["payload"]["claim_id"] == str(token)
