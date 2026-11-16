"""创意财务任务沿用跨账户锁、日期桶与批量需求去重保证。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from app.modules.reporting import jobs
from app.modules.reporting.store_models import ReportSyncJob
from tests.modules.reporting.claiming import claim_jobs
from tests.modules.reporting.creative_job_helpers import DAY, add_third_account
from tests.modules.reporting.test_jobs import _route


def test_day_old_history_is_admitted_before_new_daily_work(session, sync_seed):
    now = datetime.now(UTC)
    common = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "advertiser_ids": ["acc-1"],
        "start": DAY,
        "end": DAY,
        "frozen_route": _route(sync_seed),
        "actor_id": sync_seed.actor_id,
        "kind": "creative_day",
    }
    history_id = jobs.enqueue_job(
        session, tier="history", due_at=now - timedelta(hours=25), **common
    )
    history = session.get(ReportSyncJob, history_id)
    history.created_at = now - timedelta(hours=25)
    daily_id = jobs.enqueue_job(
        session, tier="daily", due_at=now - timedelta(minutes=1), **common
    )
    session.flush()
    claimed = claim_jobs(session, now=now, limit=2)
    assert daily_id is None  # 同桶尚未启动，提升需求版本而非复制任务。
    assert [job.id for job in claimed] == [history_id]
    assert claimed[0].due_at <= now - timedelta(hours=25)


def test_concurrent_overlapping_live_batches_have_one_owner_per_account(
    committed_sync_seed,
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from zoneinfo import ZoneInfo

    from sqlalchemy import text

    seed, factory = committed_sync_seed
    with factory() as session:
        add_third_account(session, seed)
        session.commit()
    now = datetime.now(UTC)
    today = now.astimezone(ZoneInfo("Asia/Hong_Kong")).date()
    common = {
        "tenant_id": seed.tenant_id,
        "bc_id": "bc-a",
        "kind": "ad_day",
        "tier": "hot",
        "start": today,
        "end": today,
        "frozen_route": _route(seed),
        "actor_id": seed.actor_id,
    }
    started = Event()
    with factory() as first, ThreadPoolExecutor(max_workers=1) as pool:
        first_id = jobs.enqueue_job(first, advertiser_ids=["acc-1", "acc-2"], **common)

        def second_producer():
            with factory() as second:
                second.connection().execute(
                    text("SET LOCAL application_name = 'material-resume-producer'")
                )
                started.set()
                second_id = jobs.enqueue_job(
                    second, advertiser_ids=["acc-2", "acc-3"], **common
                )
                second.commit()
                return second_id

        future = pool.submit(second_producer)
        try:
            assert started.wait(5)
            # 通过真实 pg_locks 观察等待，证明竞争确实发生在提交前。
            from time import monotonic, sleep

            deadline = monotonic() + 5
            blocked = False
            while monotonic() < deadline:
                with factory() as monitor:
                    blocked = (
                        monitor.connection()
                        .execute(
                            text("""
                        SELECT EXISTS(SELECT 1 FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid
                        WHERE a.application_name='material-resume-producer' AND NOT l.granted)
                    """)
                        )
                        .scalar_one()
                    )
                if blocked:
                    break
                sleep(0.01)
        finally:
            first.commit()
        assert blocked
        second_id = future.result(timeout=5)
    with factory() as session:
        assert session.get(ReportSyncJob, first_id).advertiser_ids == ["acc-1", "acc-2"]
        assert session.get(ReportSyncJob, second_id).advertiser_ids == ["acc-3"]


@pytest.mark.parametrize("tier", ["hot", "daily"])
def test_bulk_enqueue_preserves_coverage_and_unique_job_fallback(
    session, sync_seed, tier
):
    add_third_account(session, sync_seed)
    day = datetime.now(UTC).date()
    common = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "kind": "ad_day",
        "tier": tier,
        "start": day,
        "end": day,
        "frozen_route": _route(sync_seed),
        "actor_id": sync_seed.actor_id,
    }
    groups = [["acc-1", "acc-2"], ["acc-2", "acc-3"]]
    if tier == "daily":
        groups = [["acc-1", "acc-2"], ["acc-1", "acc-2"]]
    ids = jobs.enqueue_jobs(session, advertiser_groups=groups, **common)
    rows = session.exec(
        select(ReportSyncJob).where(
            ReportSyncJob.tenant_id == sync_seed.tenant_id,
            ReportSyncJob.kind == "ad_day",
        )
    ).all()
    assert len(rows) == len(ids) == (2 if tier == "hot" else 1)
    if tier == "hot":
        advertisers = [adv for row in rows for adv in row.advertiser_ids]
        assert sorted(advertisers) == ["acc-1", "acc-2", "acc-3"]
    assert jobs.enqueue_jobs(session, advertiser_groups=groups, **common) == []


def test_resumed_creative_snapshot_cannot_overwrite_later_complete_publication(
    committed_sync_seed,
):
    from decimal import Decimal
    from uuid import uuid4

    from app.integrations.tiktok.sdk import AccountAdmissionDeferred
    from app.modules.reporting.publish import publish_creative_day
    from app.modules.reporting.store_models import ReportSyncState
    from tests.modules.reporting.creative_job_helpers import (
        ACCOUNTS,
        Pages,
        claim,
        enqueue,
        facts,
        row,
        run,
    )

    seed, factory = committed_sync_seed
    job = enqueue(factory, seed)
    reader = Pages(count=3, failure=(2, AccountAdmissionDeferred(1000)))
    assert run(factory, job, reader) == "WAITING"
    with factory() as session:
        observed = session.get(ReportSyncJob, job.id).progress["basic_paging"][
            "observed_at"
        ]
        publish_creative_day(
            session,
            tenant_id=seed.tenant_id,
            job_id=uuid4(),
            accounts=ACCOUNTS,
            start=DAY,
            end=DAY,
            rows=[row("new", spend="20")],
        )
        stamp = session.get(
            ReportSyncState, (seed.tenant_id, "acc-1", "creative_day", DAY)
        ).last_success_at
        session.commit()
    assert datetime.fromisoformat(observed) < stamp
    reader.failure = None
    assert run(factory, claim(factory, job.id), reader) == "COMPLETE"
    assert reader.calls == [1, 2, 2, 3]
    assert [(r.creative_id, r.spend) for r in facts(factory, seed.tenant_id)] == [
        ("mnew", Decimal("20"))
    ]
    with factory() as session:
        assert (
            session.get(
                ReportSyncState, (seed.tenant_id, "acc-1", "creative_day", DAY)
            ).last_success_at
            == stamp
        )
        assert (
            session.get(ReportSyncJob, job.id).progress["basic_snapshot"]["observed_at"]
            == observed
        )


def test_changed_creative_frozen_scope_never_resumes_staged_pages(committed_sync_seed):
    from app.integrations.tiktok.sdk import AccountAdmissionDeferred
    from tests.modules.reporting.creative_job_helpers import (
        Pages,
        claim,
        enqueue,
        facts,
        run,
    )

    seed, factory = committed_sync_seed
    job = enqueue(factory, seed)
    reader = Pages(count=3, failure=(2, AccountAdmissionDeferred(1000)))
    assert run(factory, job, reader) == "WAITING"
    with factory() as session:
        saved = session.get(ReportSyncJob, job.id)
        saved.frozen_route = saved.frozen_route | {"authorization_revision": 99}
        session.commit()
    reader.failure = None
    assert run(factory, claim(factory, job.id), reader) == "FAILED"
    assert reader.calls == [1, 2]
    assert facts(factory, seed.tenant_id) == []
