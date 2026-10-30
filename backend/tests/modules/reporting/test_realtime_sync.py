"""真实 PostgreSQL 复现旧补数占满热槽、素材关联回滚金额与重复周期积压。"""

from datetime import UTC, date, datetime, timedelta

from sqlmodel import select

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.report_reader import RawReportPage
from app.jobs.models import PendingDispatch
from app.modules.reporting import job_tasks, planner
from app.modules.reporting.jobs import enqueue_job, execute_job
from app.modules.reporting.store_models import (
    MaterialReportDay,
    ReportSyncJob,
    ReportSyncState,
)
from tests.modules.reporting.claiming import claim_jobs
from tests.modules.reporting.test_jobs import _route


def enqueue(session, seed, *, kind="ad_day", tier="hot", day=None):
    day = day or datetime.now(UTC).date()
    key = enqueue_job(
        session,
        tenant_id=seed.tenant_id,
        bc_id="bc-a",
        kind=kind,
        tier=tier,
        advertiser_ids=["acc-2"],
        start=day,
        end=day,
        frozen_route=_route(seed),
        actor_id=seed.actor_id,
    )
    session.flush()
    return session.get(ReportSyncJob, key)


def test_busy_history_uses_background_pool_and_live_slot_remains_bounded(
    session, sync_seed, monkeypatch
):
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    cold = enqueue(
        session, sync_seed, tier="daily", day=now.date() - timedelta(days=10)
    )
    assert claim_jobs(session, now=now + timedelta(seconds=1), limit=1)[0].id == cold.id
    live = enqueue(session, sync_seed)
    session.flush()
    monkeypatch.setattr(job_tasks, "REPORT_DISPATCH_WINDOW", 1)
    assert cold.execution_pool == "background"
    assert job_tasks._dispatch_report_jobs(session, now + timedelta(seconds=1)) == 1
    session.refresh(live)
    assert live.status == "RUNNING" and live.execution_pool != "background"
    message = session.exec(
        select(PendingDispatch).where(PendingDispatch.task_key.contains(str(live.id)))
    ).first()
    assert message is not None
    # 后继需求仍须等真实实时槽，不能因后台池另有容量而超领实时执行。
    successor = enqueue(session, sync_seed)
    assert successor.id != live.id
    assert job_tasks._dispatch_report_jobs(session, now + timedelta(seconds=1)) == 0
    session.refresh(successor)
    assert successor.status == "QUEUED"


def test_material_facts_publish_without_waiting_for_historical_link_lookup(
    committed_sync_seed,
):
    seed, factory = committed_sync_seed
    with factory() as session:
        job = enqueue(session, seed, kind="material_day", tier="material")
        session.commit()
        job = claim_jobs(
            session, now=datetime.now(UTC) + timedelta(seconds=1), limit=1
        )[0]
        session.commit()

    class Reader:
        def material_day(self, **kwargs):
            return RawReportPage(
                (
                    {
                        "dimensions": {
                            "main_material_id": "p1",
                            "main_material_type": "VIDEO_SPARK_ADS",
                            "stat_time_day": str(job.start_date),
                        },
                        "metrics": {
                            "spend": "27",
                            "native_growth_total_ad_impression_value": "4",
                        },
                    },
                ),
                1,
                1,
                1,
            )

        def material_ad_links(self, **kwargs):
            raise DomainError("read_deadline_exceeded", "历史关联读取超时")

    assert (
        execute_job(
            factory, job_id=job.id, claim_token=job.claim_token, reader=Reader()
        )
        == "COMPLETE"
    )
    with factory() as session:
        assert (
            session.get(
                ReportSyncState,
                (seed.tenant_id, "acc-2", "material_day", job.start_date),
            ).status
            == "COMPLETE"
        )
        assert (
            session.exec(
                select(MaterialReportDay).where(
                    MaterialReportDay.tenant_id == seed.tenant_id
                )
            )
            .one()
            .spend
            == 27
        )
        repairs = session.exec(
            select(ReportSyncJob).where(
                ReportSyncJob.tenant_id == seed.tenant_id,
                ReportSyncJob.kind == "material_links",
            )
        ).all()
        assert len(repairs) == 1 and repairs[0].advertiser_ids == ["acc-2"]


def test_realtime_planning_refreshes_current_day_every_fifteen_minutes():
    assert planner.HOT_INTERVAL == timedelta(minutes=15)
    assert planner.MATERIAL_INTERVAL == timedelta(minutes=15)
    today = date(2026, 10, 18)
    assert planner._hot_window("UTC", datetime(2026, 10, 18, 3, tzinfo=UTC)) == (
        today,
        today,
    )


def test_bc_screen_resumes_from_committed_page_without_restarting(session, sync_seed):
    import pytest

    from app.modules.reporting.store_models import AdSyncBc

    now = datetime(2026, 10, 18, 4, tzinfo=UTC)
    planner.ensure_bc_plans(session, now=now, tenant_id=sync_seed.tenant_id)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    pages = []

    class Reader:
        fail = True

        def bc_spend(self, *, page, **kwargs):
            pages.append(page)
            if page == 2 and self.fail:
                raise DomainError("read_deadline_exceeded", "当前步读取预算已用完")
            adv = "acc-1" if page == 1 else "acc-2"
            return RawReportPage(
                ({"dimensions": {"advertiser_id": adv}, "metrics": {"spend": "20"}},),
                page,
                2,
                2,
            )

    reader = Reader()
    with pytest.raises(DomainError):
        job_tasks._active(session, reader, plan, ["acc-1", "acc-2"], now, days=1)
    assert plan.active_screened_at is None
    assert plan.screening_progress["bc:1"]["page"] == 2
    reader.fail = False
    assert job_tasks._active(
        session, reader, plan, ["acc-1", "acc-2"], now, days=1
    ) == ["acc-1", "acc-2"]
    assert pages == [1, 2, 2]
    assert not plan.screening_progress and plan.active_screened_at == now
