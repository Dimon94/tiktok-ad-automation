"""真实 PostgreSQL 验证素材分页续传、原子发布、范围与新旧请求隔离。"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlmodel import select

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.report_reader import RawReportPage
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.modules.reporting import jobs
from app.modules.reporting.publish import AccountMeta, publish_material_day
from app.modules.reporting.store_models import (
    MaterialReportDay,
    ReportSyncJob,
    ReportSyncPage,
    ReportSyncState,
)
from tests.modules.reporting.claiming import claim_jobs
from tests.modules.reporting.test_jobs import _route

DAY = date(2026, 10, 5)
ACCOUNTS = {"acc-1": AccountMeta("USD", "Asia/Hong_Kong")}


@pytest.fixture(autouse=True)
def small_material_step_budget(monkeypatch):
    # 这些用例用短快照反复覆盖断点/失败/原子发布；默认历史50页/30秒及
    # 今日5页的执行策略由test_material_step_budget独立验证。
    monkeypatch.setattr(jobs, "MATERIAL_BACKFILL_PAGES_PER_STEP", 5)


def row(index, *, day=DAY, spend="1", advertiser="acc-1"):
    return {
        "dimensions": {
            "stat_time_day": str(day),
            "main_material_id": f"m{index}",
            "main_material_type": "VIDEO_SPARK_ADS",
        },
        "metrics": {"advertiser_id": advertiser, "spend": spend},
    }


class Pages:
    def __init__(self, count=7, *, rows=None, failure=None):
        self.rows = rows if rows is not None else [row(index) for index in range(count)]
        self.failure = failure
        self.calls = []

    def material_day(self, *, advertiser_id, start, end, page):
        self.calls.append(page)
        if self.failure and page == self.failure[0]:
            raise self.failure[1]
        count = len(self.rows)
        return RawReportPage(tuple(self.rows[page - 1 : page]), page, count, count)


def enqueue(factory, seed, *, start=DAY, end=DAY):
    with factory() as session:
        job_id = jobs.enqueue_job(
            session,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            kind="material_day",
            tier="material",
            advertiser_ids=["acc-1"],
            start=start,
            end=end,
            frozen_route=_route(seed),
            actor_id=seed.actor_id,
        )
        session.commit()
    return claim(factory, job_id)


def add_third_account(session, seed):
    from app.modules.accounts.models import AdvertiserAccount

    session.add(
        AdvertiserAccount(
            tenant_id=seed.tenant_id,
            advertiser_id="acc-3",
            currency="USD",
            timezone="UTC",
        )
    )
    session.flush()


def claim(factory, job_id):
    with factory() as session:
        target = session.get(ReportSyncJob, job_id)
        candidates = claim_jobs(
            session,
            now=max(datetime.now(UTC), target.due_at) + timedelta(seconds=1),
            limit=100,
        )
        result = next((job for job in candidates if job.id == job_id), None)
        if result is None:
            from app.modules.sync_control.models import SyncExecutionScope, SyncSchedule

            heads = session.exec(
                select(SyncSchedule)
                .join(
                    SyncExecutionScope,
                    SyncExecutionScope.scope_id == SyncSchedule.scope_id,
                )
                .where(SyncExecutionScope.report_job_id == job_id)
            ).all()
            raise AssertionError(
                {
                    "target": (
                        target.status,
                        target.due_at,
                        target.claimed_until,
                        target.error_code,
                    ),
                    "heads": [h.model_dump() for h in heads],
                }
            )
        session.commit()
        return result


def run(factory, job, reader):
    return jobs.execute_job(
        factory, job_id=job.id, claim_token=job.claim_token, reader=reader
    )


def pages(factory, job_id):
    with factory() as session:
        return session.exec(
            select(ReportSyncPage)
            .where(ReportSyncPage.job_id == job_id)
            .order_by(ReportSyncPage.page)
        ).all()


def facts(factory, tenant):
    with factory() as session:
        return session.exec(
            select(MaterialReportDay).where(MaterialReportDay.tenant_id == tenant)
        ).all()


def test_pages_commit_between_steps_without_publishing_partial_facts(
    committed_sync_seed,
):
    seed, factory = committed_sync_seed
    job = enqueue(factory, seed)
    reader = Pages()
    assert run(factory, job, reader) == "WAITING"
    assert reader.calls == list(range(1, 6))
    assert [page.page for page in pages(factory, job.id)] == list(range(1, 6))
    assert facts(factory, seed.tenant_id) == []
    with factory() as session:
        assert (
            session.get(ReportSyncState, (seed.tenant_id, "acc-1", "material_day", DAY))
            is None
        )
    assert run(factory, claim(factory, job.id), reader) == "COMPLETE"
    assert reader.calls == list(range(1, 8))
    assert len(facts(factory, seed.tenant_id)) == 7
    with factory() as session:
        state = session.get(
            ReportSyncState, (seed.tenant_id, "acc-1", "material_day", DAY)
        )
        assert state.status == "COMPLETE" and state.row_count == 7


@pytest.mark.parametrize(
    "failure",
    [
        AccountAdmissionDeferred(1000),
        DomainError("read_deadline_exceeded", "deadline"),
        DomainError("tiktok_response_error", "temporary"),
    ],
)
def test_later_failure_preserves_prior_step_and_only_replays_current_step(
    committed_sync_seed, failure
):
    seed, factory = committed_sync_seed
    job = enqueue(factory, seed)
    reader = Pages(count=8, failure=(7, failure))
    assert run(factory, job, reader) == "WAITING"
    assert run(factory, claim(factory, job.id), reader) == "WAITING"
    assert [page.page for page in pages(factory, job.id)] == [1, 2, 3, 4, 5, 6]
    assert facts(factory, seed.tenant_id) == []
    reader.failure = None
    assert run(factory, claim(factory, job.id), reader) == "COMPLETE"
    assert reader.calls == [1, 2, 3, 4, 5, 6, 7, 7, 8]
    assert len(facts(factory, seed.tenant_id)) == 8
    with factory() as session:
        job = session.get(ReportSyncJob, job.id)
        assert job.attempts == (
            0 if isinstance(failure, AccountAdmissionDeferred) else 1
        )
        assert job.request_count == len(reader.calls)


def test_unfinished_and_overtaken_old_request_preserve_new_facts(committed_sync_seed):
    seed, factory = committed_sync_seed
    with factory() as session:
        publish_material_day(
            session,
            tenant_id=seed.tenant_id,
            job_id=uuid4(),
            accounts=ACCOUNTS,
            start=DAY,
            end=DAY,
            rows=[row("old", spend="9")],
        )
        session.commit()
    job = enqueue(factory, seed)
    reader = Pages()
    assert run(factory, job, reader) == "WAITING"
    assert [(r.main_material_id, r.spend) for r in facts(factory, seed.tenant_id)] == [
        ("mold", Decimal("9"))
    ]
    with factory() as session:
        publish_material_day(
            session,
            tenant_id=seed.tenant_id,
            job_id=uuid4(),
            accounts=ACCOUNTS,
            start=DAY,
            end=DAY,
            rows=[row("hot", spend="20")],
        )
        stamp = session.get(
            ReportSyncState, (seed.tenant_id, "acc-1", "material_day", DAY)
        ).last_success_at
        session.commit()
    assert run(factory, claim(factory, job.id), reader) == "COMPLETE"
    assert [(r.main_material_id, r.spend) for r in facts(factory, seed.tenant_id)] == [
        ("mhot", Decimal("20"))
    ]
    with factory() as session:
        assert (
            session.get(
                ReportSyncState, (seed.tenant_id, "acc-1", "material_day", DAY)
            ).last_success_at
            == stamp
        )


def test_old_token_cannot_read_or_duplicate_staged_pages(committed_sync_seed):
    seed, factory = committed_sync_seed
    job = enqueue(factory, seed)
    reader = Pages()
    assert run(factory, job, reader) == "WAITING"
    next_job = claim(factory, job.id)
    assert run(factory, job, reader) == "FAILED"
    assert reader.calls == [1, 2, 3, 4, 5]
    assert run(factory, next_job, reader) == "COMPLETE"
    assert run(factory, next_job, reader) == "FAILED"
    assert reader.calls == list(range(1, 8))


def test_small_cross_day_report_keeps_one_paginated_request(committed_sync_seed):
    seed, factory = committed_sync_seed
    job = enqueue(factory, seed, end=DAY + timedelta(days=6))
    reader = Pages(
        rows=[row(index, day=DAY + timedelta(days=index)) for index in range(7)]
    )
    assert run(factory, job, reader) == "WAITING"
    assert run(factory, claim(factory, job.id), reader) == "COMPLETE"
    with factory() as session:
        children = session.exec(
            select(ReportSyncJob).where(
                ReportSyncJob.tenant_id == seed.tenant_id,
                ReportSyncJob.kind == "material_day",
            )
        ).all()
        assert len(children) == 1
    assert len(facts(factory, seed.tenant_id)) == 7


def test_large_cross_day_splits_after_first_page_without_losing_dates(
    committed_sync_seed, monkeypatch
):
    seed, factory = committed_sync_seed
    monkeypatch.setattr(jobs, "MATERIAL_SPLIT_ROWS", 5)
    end = DAY + timedelta(days=12)
    job = enqueue(factory, seed, end=end)
    reader = Pages(count=13)
    assert run(factory, job, reader) == "COMPLETE"
    assert reader.calls == [1]
    assert pages(factory, job.id) == []
    assert facts(factory, seed.tenant_id) == []
    with factory() as session:
        children = session.exec(
            select(ReportSyncJob).where(
                ReportSyncJob.tenant_id == seed.tenant_id,
                ReportSyncJob.kind == "material_day",
                ReportSyncJob.id != job.id,
            )
        ).all()
        assert children and all(child.status == "QUEUED" for child in children)
        assert session.get(ReportSyncJob, job.id).progress["material_split"] == {
            "total_number": 13,
            "total_page": 13,
            "chunk_days": 5,
        }
        assert all(
            jobs.request_time(child) == jobs.request_time(job) for child in children
        )
        covered = [
            child.start_date + timedelta(days=i)
            for child in children
            for i in range((child.end_date - child.start_date).days + 1)
        ]
        assert sorted(covered) == [DAY + timedelta(days=i) for i in range(13)]
        assert all((child.end_date - child.start_date).days < 5 for child in children)
        assert all(child.progress["execution_window_days"] == 5 for child in children)
        # 后继相邻需求仍不可扩回已由响应证明过大的执行窗口。
        jobs.enqueue_job(
            session,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            kind="material_day",
            tier="history",
            advertiser_ids=["acc-1"],
            start=end + timedelta(days=1),
            end=end + timedelta(days=1),
            frozen_route=_route(seed),
            actor_id=seed.actor_id,
        )
        session.flush()
        assert all((child.end_date - child.start_date).days < 5 for child in children)
        child_ids = [
            child.id for child in sorted(children, key=lambda value: value.start_date)
        ]
        session.commit()

    class SplitReader:
        def __init__(self):
            self.ranges = []

        def material_day(self, *, start, end, page, **_kwargs):
            self.ranges.append((start, end, page))
            return RawReportPage(
                tuple(
                    row(i, day=start + timedelta(days=i))
                    for i in range((end - start).days + 1)
                ),
                page,
                1,
                (end - start).days + 1,
            )

    split_reader = SplitReader()
    # 同账户scope逐个完成后补位；每个子范围只发一次，不能再发原13日范围。
    for identity in child_ids:
        assert run(factory, claim(factory, identity), split_reader) == "COMPLETE"
    assert len(split_reader.ranges) == 3
    assert all(
        (last - first).days < 5 and page == 1
        for first, last, page in split_reader.ranges
    )
    assert len(facts(factory, seed.tenant_id)) == 13


def test_large_single_day_uses_all_pages_instead_of_ad_truncation_guard(
    committed_sync_seed, monkeypatch
):
    seed, factory = committed_sync_seed
    monkeypatch.setattr(jobs, "MATERIAL_SPLIT_ROWS", 2)
    monkeypatch.setattr(jobs, "TRUNCATION_ROWS", 3)
    job = enqueue(factory, seed)
    reader = Pages(count=7)
    assert run(factory, job, reader) == "WAITING"
    assert run(factory, claim(factory, job.id), reader) == "COMPLETE"
    assert len(facts(factory, seed.tenant_id)) == 7


@pytest.mark.parametrize(
    "bad_row",
    [row("bad", day=DAY + timedelta(days=1)), row("bad", advertiser="outside")],
)
def test_later_out_of_scope_or_duplicate_row_never_replaces_fact(
    committed_sync_seed, bad_row
):
    seed, factory = committed_sync_seed
    job = enqueue(factory, seed)
    reader = Pages(rows=[*[row(i) for i in range(5)], bad_row])
    assert run(factory, job, reader) == "WAITING"
    assert run(factory, claim(factory, job.id), reader) == "FAILED"
    assert facts(factory, seed.tenant_id) == []
    assert len(pages(factory, job.id)) == 5


@pytest.mark.parametrize("bad", ["account", "day", "duplicate"])
def test_material_bad_rows_stay_terminal_when_total_changes(committed_sync_seed, bad):
    seed, factory = committed_sync_seed
    job = enqueue(factory, seed)
    reader = Pages(count=7)
    assert run(factory, job, reader) == "WAITING"

    class Changed:
        def material_day(self, *, page, **_kwargs):
            invalid = (
                row("bad", advertiser="outside")
                if bad == "account"
                else row("bad", day=DAY + timedelta(days=1))
            )
            rows = (row("same"), row("same")) if bad == "duplicate" else (invalid,)
            return RawReportPage(rows, page, 8, 8)

    assert run(factory, claim(factory, job.id), Changed()) == "FAILED"
    assert facts(factory, seed.tenant_id) == []
    assert len(pages(factory, job.id)) == 5
    with factory() as session:
        assert session.get(ReportSyncJob, job.id).error_code == "report_row_invalid"


@pytest.mark.parametrize("change", ["reorder", "shrink"])
def test_material_page_drift_restarts_complete_snapshot(committed_sync_seed, change):
    seed, factory = committed_sync_seed
    job = enqueue(factory, seed)
    reader = Pages(count=7)
    assert run(factory, job, reader) == "WAITING"
    if change == "reorder":
        # 总行数不变，按变化中的消耗排序仍可能把已读素材移到下一页。
        reader.rows[0], reader.rows[5] = reader.rows[5], reader.rows[0]
    else:
        # 总页数缩小后，旧游标所在页可能已经为空，必须先识别快照变化。
        reader.rows = reader.rows[:3]
    assert run(factory, claim(factory, job.id), reader) == "WAITING"
    assert pages(factory, job.id) == []
    assert facts(factory, seed.tenant_id) == []
    with factory() as session:
        changed = session.get(ReportSyncJob, job.id)
        assert changed.error_code == "report_snapshot_changed"
        assert changed.attempts == 1
    result = run(factory, claim(factory, job.id), reader)
    if change == "reorder":
        assert result == "WAITING"
        result = run(factory, claim(factory, job.id), reader)
    assert result == "COMPLETE"
    assert len(facts(factory, seed.tenant_id)) == len(reader.rows)
    assert reader.calls[:7] == [1, 2, 3, 4, 5, 6, 1]


def test_changed_frozen_scope_cannot_resume_existing_pages(committed_sync_seed):
    seed, factory = committed_sync_seed
    job = enqueue(factory, seed)
    reader = Pages()
    assert run(factory, job, reader) == "WAITING"
    with factory() as session:
        target = session.get(ReportSyncJob, job.id)
        target.frozen_route = target.frozen_route | {"authorization_revision": 99}
        session.commit()
    assert run(factory, claim(factory, job.id), reader) == "FAILED"
    assert reader.calls == [1, 2, 3, 4, 5]
    assert facts(factory, seed.tenant_id) == []


def test_live_material_and_link_jobs_are_not_starved_by_new_ad_jobs(session, sync_seed):
    from zoneinfo import ZoneInfo

    now = datetime.now(UTC)
    today = now.astimezone(ZoneInfo("Asia/Hong_Kong")).date()
    common = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "advertiser_ids": ["acc-1"],
        "start": today,
        "end": today,
        "frozen_route": _route(sync_seed),
        "actor_id": sync_seed.actor_id,
    }
    material_id = jobs.enqueue_job(
        session,
        kind="material_day",
        tier="material",
        due_at=now - timedelta(minutes=4),
        **common,
    )
    links_id = jobs.enqueue_job(
        session,
        kind="material_links",
        tier="material",
        due_at=now - timedelta(minutes=3),
        **common,
    )
    ad_id = jobs.enqueue_job(
        session, kind="ad_day", tier="hot", due_at=now - timedelta(minutes=1), **common
    )
    claimed = claim_jobs(session, now=now, limit=3)
    assert {job.id for job in claimed} == {material_id, ad_id, links_id}
    assert claim_jobs(session, now=now, limit=3) == []


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
        "kind": "material_day",
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


def test_snapshot_count_change_restarts_with_bounded_retry_and_no_partial_publish(
    committed_sync_seed,
):
    seed, factory = committed_sync_seed
    job = enqueue(factory, seed)
    reader = Pages(count=7)
    assert run(factory, job, reader) == "WAITING"
    reader.rows.append(row(7))
    assert run(factory, claim(factory, job.id), reader) == "WAITING"
    assert pages(factory, job.id) == []
    assert facts(factory, seed.tenant_id) == []
    with factory() as session:
        changed = session.get(ReportSyncJob, job.id)
        assert changed.error_code == "report_snapshot_changed" and changed.attempts == 1
    assert run(factory, claim(factory, job.id), reader) == "WAITING"
    assert run(factory, claim(factory, job.id), reader) == "COMPLETE"
    assert reader.calls == [1, 2, 3, 4, 5, 6, 1, 2, 3, 4, 5, 6, 7, 8]
    assert len(facts(factory, seed.tenant_id)) == 8


def test_staging_cleanup_only_removes_old_terminal_jobs(committed_sync_seed):
    seed, factory = committed_sync_seed
    old = enqueue(factory, seed)
    assert run(factory, old, Pages()) == "WAITING"
    now = datetime.now(UTC)
    with factory() as session:
        target = session.get(ReportSyncJob, old.id)
        target.created_at = now - timedelta(days=30)
        session.commit()
    with factory() as session:
        assert jobs.cleanup_material_pages(session, now=now) == 0
        session.commit()
    with factory() as session:
        target = session.get(ReportSyncJob, old.id)
        target.status = "FAILED"
        target.finished_at = now - timedelta(days=6)
        session.commit()
    with factory() as session:
        assert jobs.cleanup_material_pages(session, now=now) == 0
        session.get(ReportSyncJob, old.id).finished_at = now - timedelta(days=8)
        session.commit()
    with factory() as session:
        assert jobs.cleanup_material_pages(session, now=now) == 5
        session.commit()
    assert pages(factory, old.id) == []


def test_duplicate_claim_tokens_cannot_concurrently_stage_twice(
    committed_sync_seed, redis_client, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import nullcontext
    from threading import Barrier

    from app.core.config import settings
    from app.modules.reporting import job_tasks

    seed, factory = committed_sync_seed
    job = enqueue(factory, seed)
    reader, barrier = Pages(), Barrier(2)
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(job_tasks, "_reader_for", lambda **_: nullcontext(reader))
    monkeypatch.setattr(job_tasks, "_report_refill_after", lambda *_: nullcontext())
    with factory() as session:
        engine = session.get_bind()

    def execute(_index):
        barrier.wait(timeout=5)
        return job_tasks.run_job_step(
            database_engine=engine,
            redis_client=redis_client,
            job_id=job.id,
            claim_token=job.claim_token,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, range(2)))
    assert sorted(results) == ["FAILED", "WAITING"]
    assert reader.calls == [1, 2, 3, 4, 5]
    assert len(pages(factory, job.id)) == 5


def test_final_publish_database_failure_rolls_back_delete_and_partial_inserts(
    committed_sync_seed,
):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    seed, factory = committed_sync_seed
    with factory() as session:
        publish_material_day(
            session,
            tenant_id=seed.tenant_id,
            job_id=uuid4(),
            accounts=ACCOUNTS,
            start=DAY,
            end=DAY,
            rows=[row("old", spend="9")],
        )
        stamp = session.get(
            ReportSyncState, (seed.tenant_id, "acc-1", "material_day", DAY)
        ).last_success_at
        session.commit()
    job, reader = enqueue(factory, seed), Pages()
    assert run(factory, job, reader) == "WAITING"
    current = claim(factory, job.id)
    function_name = f"test_material_atomic_{uuid4().hex}"
    # 故障落在真实 PG 写入最后一行时，而非替换发布函数，验证删除与已插入行一并回滚。
    with factory() as session:
        session.connection().execute(
            text(f"""
            CREATE FUNCTION {function_name}() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.tenant_id = '{seed.tenant_id}'::uuid AND NEW.main_material_id = 'm6' THEN
                    RAISE EXCEPTION 'synthetic final row failure';
                END IF;
                RETURN NEW;
            END $$
        """)
        )
        session.connection().execute(
            text(
                f"CREATE TRIGGER {function_name} BEFORE INSERT ON material_report_day FOR EACH ROW EXECUTE FUNCTION {function_name}()"
            )
        )
        session.commit()
    try:
        with pytest.raises(DBAPIError):
            run(factory, current, reader)
        assert [
            (item.main_material_id, item.spend)
            for item in facts(factory, seed.tenant_id)
        ] == [("mold", Decimal("9"))]
        assert len(pages(factory, job.id)) == 6
        with factory() as session:
            assert (
                session.get(
                    ReportSyncState, (seed.tenant_id, "acc-1", "material_day", DAY)
                ).last_success_at
                == stamp
            )
    finally:
        with factory() as session:
            session.connection().execute(
                text(f"DROP TRIGGER {function_name} ON material_report_day")
            )
            session.connection().execute(text(f"DROP FUNCTION {function_name}()"))
            session.commit()
    assert run(factory, current, reader) == "COMPLETE"
    assert len(facts(factory, seed.tenant_id)) == 7


def test_repeated_snapshot_changes_exhaust_retry_budget_without_false_success(
    committed_sync_seed,
):
    seed, factory = committed_sync_seed
    job, reader = enqueue(factory, seed), Pages()
    assert run(factory, job, reader) == "WAITING"
    for attempt in range(jobs.MAX_PROVIDER_RETRIES + 1):
        reader.rows.append(row(7 + attempt))
        expected = "FAILED" if attempt == jobs.MAX_PROVIDER_RETRIES else "WAITING"
        assert run(factory, claim(factory, job.id), reader) == expected
        if expected == "WAITING":
            assert run(factory, claim(factory, job.id), reader) == "WAITING"
    with factory() as session:
        terminal = session.get(ReportSyncJob, job.id)
        assert (
            terminal.status == "FAILED"
            and terminal.attempts == jobs.MAX_PROVIDER_RETRIES
        )
        assert terminal.error_code == "report_snapshot_changed"
        assert (
            session.get(
                ReportSyncState, (seed.tenant_id, "acc-1", "material_day", DAY)
            ).status
            == "FAILED"
        )
    assert facts(factory, seed.tenant_id) == []


def test_incomplete_declared_page_rows_never_mark_complete(committed_sync_seed):
    seed, factory = committed_sync_seed
    job = enqueue(factory, seed)

    class MissingRows:
        def material_day(self, *, page, **_kwargs):
            return RawReportPage((row(page),), page, 1, 2)

    assert run(factory, job, MissingRows()) == "FAILED"
    assert pages(factory, job.id) == []
    assert facts(factory, seed.tenant_id) == []
    with factory() as session:
        assert (
            session.get(
                ReportSyncState, (seed.tenant_id, "acc-1", "material_day", DAY)
            ).status
            == "FAILED"
        )


@pytest.mark.parametrize(
    "scenario,needs_links",
    [
        ("empty", False),
        ("mapped", False),
        ("missing", True),
        ("null_vid", True),
        ("other_tenant_map", True),
        ("other_account_map", True),
        ("outside_date", False),
        ("other_tenant_facts", False),
    ],
)
@pytest.mark.parametrize("spend,revenue", [("0", "0"), ("1", "0"), ("0", "1")])
def test_material_publish_only_queues_real_link_gaps(
    session, sync_seed, scenario, needs_links, spend, revenue
):
    from app.modules.reporting.store_models import MaterialPostLink

    tenant = sync_seed.tenant_id
    foreign = uuid4()
    if scenario in {"outside_date", "other_tenant_facts"}:
        day = DAY - timedelta(days=1) if scenario == "outside_date" else DAY
        publish_material_day(
            session,
            tenant_id=foreign if scenario == "other_tenant_facts" else tenant,
            job_id=uuid4(),
            accounts=ACCOUNTS,
            start=day,
            end=day,
            rows=[row(0, day=day, spend="0")],
        )
    if scenario in {"mapped", "null_vid", "other_tenant_map", "other_account_map"}:
        session.add(
            MaterialPostLink(
                tenant_id=foreign if scenario == "other_tenant_map" else tenant,
                advertiser_id="acc-2" if scenario == "other_account_map" else "acc-1",
                main_material_id="m0",
                ad_remote_id="a0",
                main_material_type="VIDEO_SPARK_ADS",
                platform_material_id=None if scenario == "null_vid" else "v0",
                observed_at=datetime.now(UTC),
            )
        )
        session.flush()
    job_id = jobs.enqueue_job(
        session,
        tenant_id=tenant,
        bc_id="bc-a",
        kind="material_day",
        tier="material",
        advertiser_ids=["acc-1"],
        start=DAY,
        end=DAY,
        frozen_route=_route(sync_seed),
        actor_id=sync_seed.actor_id,
    )
    job = session.get(ReportSyncJob, job_id)
    source = (
        []
        if scenario in {"empty", "outside_date", "other_tenant_facts"}
        else [
            row(0, spend=spend)
            | {
                "metrics": {
                    **row(0, spend=spend)["metrics"],
                    "onsite_total_ad_impression_value": revenue,
                }
            }
        ]
    )
    reader = Pages(rows=source)
    assert jobs._run_material(session, job, reader, ACCOUNTS) == len(source)
    linked = session.exec(
        select(ReportSyncJob).where(
            ReportSyncJob.tenant_id == tenant, ReportSyncJob.kind == "material_links"
        )
    ).all()
    # 零金额事实继续入库和展示，但不为它们制造补链工作；收入独有仍需关联。
    assert bool(linked) is (needs_links and (spend != "0" or revenue != "0"))
    if scenario == "null_vid" and linked:
        from tests.modules.reporting.test_material_query import use

        post = session.get(MaterialPostLink, (tenant, "acc-1", "m0", "a0"))
        post.ad_material_id = "am0"
        use(session, sync_seed, adv="acc-1", ad="a0", video="v0", ad_material="am0")
        # 已知道帖子所属广告但缺 VID 时仍排关联；目录到齐后只做本地解析即可。
        assert jobs._run(session, linked[0], jobs._CountingReader(reader)) == 0
        session.refresh(post)
        assert post.platform_material_id == "v0"
    assert reader.calls == [1]  # 减负判断只查本地库，不增加平台读取。


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
        select(ReportSyncJob).where(ReportSyncJob.tenant_id == sync_seed.tenant_id)
    ).all()
    assert len(rows) == len(ids) == (2 if tier == "hot" else 1)
    if tier == "hot":
        advertisers = [adv for row in rows for adv in row.advertiser_ids]
        assert sorted(advertisers) == ["acc-1", "acc-2", "acc-3"]
    assert jobs.enqueue_jobs(session, advertiser_groups=groups, **common) == []
