"""未启动的相邻日期复用一次完整读取，冻结后的执行保持原范围。"""

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import select

from app.integrations.tiktok.contracts.report_reader import RawReportPage
from app.modules.reporting.jobs import enqueue_job, execute_job
from app.modules.reporting.store_models import (
    AdReportDay,
    ReportSyncJob,
    ReportSyncPage,
)
from app.modules.sync_control.models import SyncExecutionScope
from app.modules.sync_control.report_demand import adopt_report_job
from tests.modules.reporting.claiming import claim_jobs
from tests.modules.reporting.sync_conftest import ad_row
from tests.modules.reporting.test_jobs import _route

D = date(2026, 10, 1)


@pytest.mark.parametrize(
    "evidence",
    [
        "pending",
        "superseded",
        "published",
        "generation",
        "observed",
        "version",
        "owner",
    ],
)
def test_overlapping_unstarted_merge_reuses_only_unfrozen_children(
    committed_sync_seed, evidence
):
    from app.modules.sync_control.models import SyncRequirement

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    common = {
        "tenant_id": seed.tenant_id,
        "bc_id": "bc-a",
        "actor_id": seed.actor_id,
        "frozen_route": _route(seed),
        "kind": "material_links",
        "tier": "history",
        "advertiser_ids": ["acc-1"],
    }
    with factory() as db, db.begin():
        old_id = enqueue_job(
            db, **common, start=D, end=D + timedelta(days=1), requested_at=now
        )
    # 旧父任务被其他事务短锁时，规范入口允许为新需求创建另一冻结执行。
    with factory() as locked, locked.begin():
        locked.get(ReportSyncJob, old_id, with_for_update=True)
        with factory() as db, db.begin():
            newer_id = enqueue_job(
                db,
                **common,
                start=D + timedelta(days=1),
                end=D + timedelta(days=1),
                requested_at=now + timedelta(seconds=1),
            )
            assert newer_id is not None and newer_id != old_id
            newer = db.get(ReportSyncJob, newer_id)
            newer.status, newer.claim_generation = "RUNNING", 1
            newer.progress = {"overview_next_page": 2}
    with factory() as db, db.begin():
        old = db.get(ReportSyncJob, old_id)
        old_children = db.exec(
            select(SyncExecutionScope)
            .where(SyncExecutionScope.report_job_id == old_id)
            .order_by(SyncExecutionScope.requirement_id)
        ).all()
        changed = old_children[0]
        if evidence in {"superseded", "published"}:
            changed.status = evidence.upper()
        elif evidence == "generation":
            changed.execution_generation = 1
        elif evidence == "observed":
            changed.observed_at = now
        elif evidence == "version":
            changed.published_version = 1
        elif evidence == "owner":
            changed.owner_epoch += 1
        db.flush()
        old_before = old.model_dump()
        old_children_before = {child.id: child.model_dump() for child in old_children}
        newer = db.get(ReportSyncJob, newer_id)
        newer_before = newer.model_dump()
        newer_child = db.exec(
            select(SyncExecutionScope).where(
                SyncExecutionScope.report_job_id == newer_id
            )
        ).one()
        child_before = newer_child.model_dump()
        # 最新执行不可变，但更早的未启动执行仍可承接；其已有子桶不能重复INSERT。
        result = enqueue_job(
            db,
            **common,
            start=D + timedelta(days=1),
            end=D + timedelta(days=2),
            requested_at=now + timedelta(seconds=2),
        )
        db.flush()
        if evidence != "pending":
            assert result is not None and result not in {old_id, newer_id}
            db.refresh(old)
            assert old.model_dump() == old_before
            for child in old_children:
                db.refresh(child)
                assert child.model_dump() == old_children_before[child.id]
            db.refresh(newer)
            db.refresh(newer_child)
            assert newer.model_dump() == newer_before
            assert newer_child.model_dump() == child_before
            return
        assert result is None
        children = db.exec(
            select(SyncExecutionScope).where(SyncExecutionScope.report_job_id == old_id)
        ).all()
        assert len(children) == len({child.requirement_id for child in children}) == 3
        assert set(old_children_before) < {child.id for child in children}
        for child in children:
            requirement = db.get(SyncRequirement, child.requirement_id)
            assert child.frozen_revision == requirement.desired_revision
            assert child.frozen_requested_after == requirement.requested_after
            assert child.status == "PENDING" and child.observed_at is None
            assert requirement.satisfied_revision == 0
        db.refresh(newer)
        db.refresh(newer_child)
        assert newer.model_dump() == newer_before
        assert newer_child.model_dump() == child_before
        assert db.get(ReportSyncJob, old_id).claim_generation == 0
        assert db.get(ReportSyncJob, old_id).end_date == D + timedelta(days=2)


def test_adjacent_unstarted_requests_save_a_real_read_with_same_daily_facts(
    committed_sync_seed,
):
    seed, factory = committed_sync_seed
    with factory() as session, session.begin():
        common = {
            "tenant_id": seed.tenant_id,
            "bc_id": "bc-a",
            "actor_id": seed.actor_id,
            "frozen_route": _route(seed),
            "kind": "ad_day",
        }
        # 旧trace的两个独立冻结请求；通过正式adopt接入同一执行器比较实际网络边界调用。
        for day in (D, D + timedelta(days=1)):
            job = ReportSyncJob(
                **common,
                priority=4,
                advertiser_ids=["acc-1"],
                start_date=day,
                end_date=day,
                job_key=uuid4().hex,
                due_at=datetime.now(UTC),
            )
            session.add(job)
            session.flush()
            adopt_report_job(session, job)
        first = enqueue_job(
            session, **common, tier="history", advertiser_ids=["acc-2"], start=D, end=D
        )
        assert (
            enqueue_job(
                session,
                **common,
                tier="history",
                advertiser_ids=["acc-2"],
                start=D + timedelta(days=1),
                end=D + timedelta(days=1),
            )
            is None
        )
        merged = session.get(ReportSyncJob, first)
        assert (merged.start_date, merged.end_date) == (D, D + timedelta(days=1))
        children = session.exec(
            select(SyncExecutionScope).where(SyncExecutionScope.report_job_id == first)
        ).all()
        assert len(children) == 2 and all(
            child.frozen_requested_after is not None for child in children
        )
    calls = []

    class Reader:
        def ad_day(self, *, advertiser_ids, start, end, page, **_kwargs):
            calls.append((advertiser_ids[0], start, end, page))
            rows = tuple(
                ad_row("trace-ad", start + timedelta(days=i), adv=advertiser_ids[0])
                for i in range((end - start).days + 1)
            )
            return RawReportPage(rows, 1, 1, len(rows))

    for turn in range(5):
        with factory() as session:
            jobs = claim_jobs(
                session, now=datetime.now(UTC) + timedelta(seconds=turn), limit=3
            )
            identities = [(job.id, job.claim_token) for job in jobs]
            session.commit()
        for identity, token in identities:
            assert (
                execute_job(
                    factory, job_id=identity, claim_token=token, reader=Reader()
                )
                == "COMPLETE"
            )
    assert sum(account == "acc-1" for account, *_ in calls) == 2
    assert sum(account == "acc-2" for account, *_ in calls) == 1
    with factory() as session:
        rows = session.exec(
            select(AdReportDay).where(AdReportDay.tenant_id == seed.tenant_id)
        ).all()
        baseline = {
            (row.stat_date, row.spend, row.revenue_d0)
            for row in rows
            if row.advertiser_id == "acc-1"
        }
        merged = {
            (row.stat_date, row.spend, row.revenue_d0)
            for row in rows
            if row.advertiser_id == "acc-2"
        }
        assert baseline == merged and len(merged) == 2


@pytest.mark.parametrize("started", ["claimed", "page", "observed", "gap", "too_long"])
def test_date_expansion_requires_adjacent_small_strictly_unstarted_scope(
    session, sync_seed, started
):
    common = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "actor_id": sync_seed.actor_id,
        "frozen_route": _route(sync_seed),
        "kind": "ad_day",
        "tier": "history",
        "advertiser_ids": ["acc-1"],
    }
    end = D + timedelta(days=29) if started == "too_long" else D
    first = enqueue_job(session, **common, start=D, end=end)
    job = session.get(ReportSyncJob, first)
    if started == "claimed":
        job.claim_generation = 1
    elif started == "page":
        session.add(
            ReportSyncPage(
                job_id=first,
                page=1,
                tenant_id=sync_seed.tenant_id,
                scope_key="a" * 64,
                rows=[],
                row_keys=[],
                total_page=0,
                total_number=0,
            )
        )
    elif started == "observed":
        child = session.exec(
            select(SyncExecutionScope).where(SyncExecutionScope.report_job_id == first)
        ).one()
        child.observed_at = datetime.now(UTC)
    second_day = end + timedelta(days=2 if started == "gap" else 1)
    second = enqueue_job(session, **common, start=second_day, end=second_day)
    assert second is not None and second != first
    assert (job.start_date, job.end_date) == (D, end)


def test_overlapping_refresh_reads_only_missing_dates_after_legal_range_limit(
    committed_sync_seed,
):
    seed, factory = committed_sync_seed
    with factory() as session, session.begin():
        common = {
            "tenant_id": seed.tenant_id,
            "bc_id": "bc-a",
            "actor_id": seed.actor_id,
            "frozen_route": _route(seed),
            "kind": "ad_day",
            "tier": "history",
            "advertiser_ids": ["acc-1"],
        }
        first = enqueue_job(session, **common, start=D, end=D + timedelta(days=29))
        second = enqueue_job(
            session, **common, start=D + timedelta(days=28), end=D + timedelta(days=30)
        )
        assert second is not None and second != first
    calls = []

    class Reader:
        def ad_day(self, *, start, end, page, **_kwargs):
            calls.append((start, end, page))
            return RawReportPage((), 1, 0, 0)

    for turn in range(3):
        with factory() as session:
            jobs = claim_jobs(
                session, now=datetime.now(UTC) + timedelta(seconds=turn), limit=1
            )
            identities = [(job.id, job.claim_token) for job in jobs]
            session.commit()
        for identity, token in identities:
            assert (
                execute_job(
                    factory, job_id=identity, claim_token=token, reader=Reader()
                )
                == "COMPLETE"
            )
    days = [
        start + timedelta(days=i)
        for start, end, _ in calls
        for i in range((end - start).days + 1)
    ]
    assert set(days) == {D + timedelta(days=i) for i in range(31)}
    assert len(days) == len(set(days)), calls


def test_rebatched_accounts_read_each_missing_daily_bucket_once(committed_sync_seed):
    seed, factory = committed_sync_seed
    with factory() as session, session.begin():
        common = {
            "tenant_id": seed.tenant_id,
            "bc_id": "bc-a",
            "actor_id": seed.actor_id,
            "frozen_route": _route(seed),
            "kind": "ad_day",
            "tier": "history",
        }
        for account, offsets in (("acc-1", (0, 2)), ("acc-2", (1,))):
            for offset in offsets:
                day = D + timedelta(days=offset)
                enqueue_job(
                    session, **common, advertiser_ids=[account], start=day, end=day
                )
        enqueue_job(
            session,
            **common,
            advertiser_ids=["acc-1", "acc-2"],
            start=D,
            end=D + timedelta(days=2),
        )
    reads = []

    class Reader:
        def ad_day(self, *, advertiser_ids, start, end, **_kwargs):
            reads.extend(
                (account, start + timedelta(days=i))
                for account in advertiser_ids
                for i in range((end - start).days + 1)
            )
            return RawReportPage((), 1, 0, 0)

    for turn in range(6):
        with factory() as session:
            claimed = claim_jobs(
                session, now=datetime.now(UTC) + timedelta(seconds=turn), limit=2
            )
            identities = [(job.id, job.claim_token) for job in claimed]
            session.commit()
        for identity, token in identities:
            assert (
                execute_job(
                    factory, job_id=identity, claim_token=token, reader=Reader()
                )
                == "COMPLETE"
            )
    assert len(reads) == len(set(reads)) == 6
    assert set(reads) == {
        (account, D + timedelta(days=i))
        for account in ("acc-1", "acc-2")
        for i in range(3)
    }


def test_unstarted_revision_merge_holds_execution_lock_against_dispatch(
    committed_sync_seed,
):
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError

    from app.core.db import engine

    seed, factory = committed_sync_seed
    common = {
        "tenant_id": seed.tenant_id,
        "bc_id": "bc-a",
        "actor_id": seed.actor_id,
        "frozen_route": _route(seed),
        "kind": "ad_day",
        "tier": "history",
        "advertiser_ids": ["acc-1"],
        "start": D,
        "end": D,
    }
    with factory() as session, session.begin():
        identity = enqueue_job(session, **common)
    raced = []

    def dispatch():
        # 另一会话直接探测dispatcher必须取得的job行锁，避免上游scope/FK锁
        # 让整个dispatcher等待后掩盖这里的读后修改竞争。
        try:
            with factory() as session, session.begin():
                session.exec(
                    select(ReportSyncJob)
                    .where(ReportSyncJob.id == identity)
                    .with_for_update(nowait=True)
                ).one()
                return 1
        except OperationalError as error:
            assert error.orig.sqlstate == "55P03"
            return 0

    with ThreadPoolExecutor(max_workers=1) as pool:

        def after_query(connection, _cursor, statement, _parameters, _context, _many):
            if (
                connection.info.get("revision_merge_test")
                and not raced
                and statement.startswith("SELECT ")
                and "FROM report_sync_job" in statement
                and "FOR UPDATE OF report_sync_job SKIP LOCKED" in statement
                and "report_sync_job.claim_generation" in statement
            ):
                raced.append(pool.submit(dispatch).result(timeout=5))

        event.listen(engine, "after_cursor_execute", after_query)
        try:
            with factory() as session, session.begin():
                connection = session.connection()
                connection.info["revision_merge_test"] = True
                try:
                    assert enqueue_job(session, **common) is None
                finally:
                    connection.info.pop("revision_merge_test", None)
        finally:
            event.remove(engine, "after_cursor_execute", after_query)
    assert raced == [0], (
        "revision may only change while the queued execution is exclusively locked"
    )
    with factory() as session:
        job = session.get(ReportSyncJob, identity)
        child = session.exec(
            select(SyncExecutionScope).where(
                SyncExecutionScope.report_job_id == identity
            )
        ).one()
        assert job.claim_generation == 0 and child.frozen_revision == 2


def test_date_merge_requires_every_account_to_keep_the_frozen_query_scope(
    session, sync_seed
):
    from app.modules.accounts.models import AdvertiserAccount

    common = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "actor_id": sync_seed.actor_id,
        "frozen_route": _route(sync_seed),
        "kind": "ad_day",
        "tier": "history",
        "advertiser_ids": ["acc-1", "acc-2"],
    }
    first = enqueue_job(session, **common, start=D, end=D)
    account = session.exec(
        select(AdvertiserAccount).where(
            AdvertiserAccount.tenant_id == sync_seed.tenant_id,
            AdvertiserAccount.advertiser_id == "acc-2",
        )
    ).one()
    account.timezone = "Asia/Tokyo"
    second = enqueue_job(
        session, **common, start=D + timedelta(days=1), end=D + timedelta(days=1)
    )
    assert second is not None and second != first
    assert session.get(ReportSyncJob, first).end_date == D


@pytest.mark.parametrize("kind", ["material_links", "ad_day", "material_day"])
@pytest.mark.parametrize("tier", ["hot", "material"])
def test_single_day_refresh_reuses_unstarted_history_without_duplicate_children(
    session, sync_seed, kind, tier
):
    from app.modules.sync_control.models import SyncRequirement

    now = datetime.now(UTC)
    common = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "actor_id": sync_seed.actor_id,
        "frozen_route": _route(sync_seed),
        "kind": kind,
        "advertiser_ids": ["acc-1"],
    }
    identity = enqueue_job(
        session,
        **common,
        tier="history",
        start=D,
        end=D + timedelta(days=1),
        requested_at=now - timedelta(hours=1),
    )
    assert (
        enqueue_job(
            session,
            **common,
            tier=tier,
            start=D + timedelta(days=1),
            end=D + timedelta(days=1),
            requested_at=now,
        )
        is None
    )
    session.flush()
    children = session.exec(
        select(SyncExecutionScope).where(SyncExecutionScope.report_job_id == identity)
    ).all()
    assert len(children) == len({child.requirement_id for child in children}) == 2
    for child in children:
        requirement = session.get(SyncRequirement, child.requirement_id)
        assert child.frozen_revision == requirement.desired_revision
        assert child.status == "PENDING" and requirement.satisfied_revision == 0
    assert session.get(ReportSyncJob, identity).claim_generation == 0


@pytest.mark.parametrize("days_ago", [0, 1])
@pytest.mark.parametrize("kind", ["ad_day", "material_day"])
def test_recent_amount_refresh_still_splits_from_unstarted_history(
    session, sync_seed, days_ago, kind
):
    from zoneinfo import ZoneInfo

    today = datetime.now(UTC).astimezone(ZoneInfo("Asia/Hong_Kong")).date()
    common = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "actor_id": sync_seed.actor_id,
        "frozen_route": _route(sync_seed),
        "kind": kind,
        "advertiser_ids": ["acc-1"],
    }
    old = enqueue_job(
        session,
        **common,
        tier="history",
        start=today - timedelta(days=3),
        end=today,
        requested_at=datetime.now(UTC) - timedelta(hours=1),
    )
    day = today - timedelta(days=days_ago)
    new = enqueue_job(session, **common, tier="hot", start=day, end=day)
    assert new is not None and new != old
    job = session.get(ReportSyncJob, new)
    assert job.start_date == job.end_date == day and job.priority == 1
    assert session.get(ReportSyncJob, old).start_date == today - timedelta(days=3)
