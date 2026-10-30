"""持续热刷下历史必须进展；需求合并不能吞掉在途新增或跨授权。"""

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlmodel import select

from app.modules.reporting import job_tasks
from app.modules.reporting.jobs import enqueue_job
from app.modules.reporting.store_models import ReportSyncJob
from tests.modules.reporting.claiming import claim_jobs
from tests.modules.reporting.test_jobs import _route


def request(session, seed, day, **overrides):
    args = {
        "tenant_id": seed.tenant_id,
        "bc_id": "bc-a",
        "actor_id": seed.actor_id,
        "frozen_route": _route(seed),
        "kind": "material_links",
        "tier": "material",
        "advertiser_ids": ["acc-1"],
        "start": day,
        "end": day,
    }
    args.update(overrides)
    return enqueue_job(session, **args)


def test_history_has_capacity_during_continuous_current_arrivals(
    session, sync_seed, monkeypatch
):
    now = datetime.now(UTC)
    today = now.astimezone(ZoneInfo("Asia/Hong_Kong")).date()
    monkeypatch.setattr(job_tasks, "REPORT_DISPATCH_WINDOW", 4)
    old = request(
        session, sync_seed, today - timedelta(days=5), due_at=now - timedelta(hours=2)
    )
    for i in range(12):
        request(
            session,
            sync_seed,
            today,
            campaign_ids=[str(i)],
            due_at=now - timedelta(minutes=1),
        )
    job_tasks._dispatch_report_jobs(session, now)
    assert session.get(ReportSyncJob, old).status == "RUNNING"


def test_history_dates_have_separate_buckets_and_frozen_routes(session, sync_seed):
    now = datetime.now(UTC)
    today = now.astimezone(ZoneInfo("Asia/Hong_Kong")).date()
    first = request(session, sync_seed, today - timedelta(days=5))
    second = request(session, sync_seed, today - timedelta(days=3))
    assert second is not None and second != first
    job = session.get(ReportSyncJob, first)
    assert (job.start_date, job.end_date) == (
        today - timedelta(days=5),
        today - timedelta(days=5),
    )
    assert request(session, sync_seed, today) is not None
    route = _route(sync_seed) | {"binding_revision": 1}
    assert (
        request(session, sync_seed, today - timedelta(days=4), frozen_route=route)
        is not None
    )


def test_running_work_keeps_a_successor_for_new_same_day_demand(session, sync_seed):
    day = date(2026, 10, 5)
    first = request(session, sync_seed, day)
    job = session.get(ReportSyncJob, first)
    assert (
        claim_jobs(session, now=datetime.now(UTC) + timedelta(seconds=1), limit=1)[0].id
        == first
    )
    from app.modules.sync_control.models import SyncExecutionScope

    child = session.exec(
        select(SyncExecutionScope).where(SyncExecutionScope.report_job_id == first)
    ).one()
    frozen = (child.frozen_revision, child.frozen_requested_after)
    successor = request(session, sync_seed, day)
    assert successor is not None and successor != first
    session.refresh(child)
    assert (child.frozen_revision, child.frozen_requested_after) == frozen
    assert request(session, sync_seed, day) is None
    job.status, job.finished_at = "COMPLETE", datetime.now(UTC)
    session.flush()
    active = session.exec(
        select(ReportSyncJob).where(ReportSyncJob.status == "QUEUED")
    ).all()
    assert [j.id for j in active] == [successor]


def test_missing_video_waits_for_directory_without_recreating_work(committed_sync_seed):
    from app.modules.reporting.store_models import MaterialPostLink
    from tests.modules.reporting.test_material_link_resume import step
    from tests.modules.reporting.test_material_query import D, material_row, publish

    seed, factory = committed_sync_seed
    with factory() as session:
        publish(session, seed, "acc-1", [material_row("post", "5", "file.mp4")])
        session.add(
            MaterialPostLink(
                tenant_id=seed.tenant_id,
                advertiser_id="acc-1",
                main_material_id="post",
                ad_remote_id="ad",
                main_material_type="VIDEO_SPARK_ADS",
                observed_at=datetime.now(UTC),
                material_name="file.mp4",
            )
        )
        job_id = request(session, seed, D)
        session.commit()

    class BridgeOverview:
        calls = 0

        def material_ad_links(self, **kwargs):
            from tests.modules.reporting.test_material_links import overview

            assert kwargs["advertiser_id"] == "acc-1" and kwargs["page"] == 1
            self.calls += 1
            return overview()

    reader = BridgeOverview()
    assert step(factory, job_id, reader) == "WAITING"
    assert reader.calls == 1, "缺桥接需有界复核概览，不能只等待目录"
    with factory() as session:
        job = session.get(ReportSyncJob, job_id)
        assert job.error_code == "material_directory_pending"
        assert job.due_at > datetime.now(UTC) + timedelta(minutes=9)
        due = job.due_at
        successor = request(session, seed, D)
        assert successor is not None and successor != job_id
        for _ in range(10):
            assert request(session, seed, D) is None
        assert job.due_at == due
        session.commit()
    with factory() as session:
        assert (
            len(
                session.exec(
                    select(ReportSyncJob).where(
                        ReportSyncJob.tenant_id == seed.tenant_id
                    )
                ).all()
            )
            == 2
        )


def test_new_post_wakes_waiting_work_without_losing_existing_demand(session, sync_seed):
    from tests.modules.reporting.test_material_query import D, material_row, publish

    publish(session, sync_seed, "acc-1", [material_row("old", "5")])
    job_id = request(session, sync_seed, D)
    job = session.get(ReportSyncJob, job_id)
    job.status, job.error_code = "WAITING", "material_identity_unavailable"
    job.due_at = datetime.now(UTC) + timedelta(minutes=10)
    job.progress = {
        "coalesced_v1": True,
        "waiting_ids": ["old"],
        "dependency_checked_at": datetime.now(UTC).isoformat(),
    }
    publish(
        session,
        sync_seed,
        "acc-1",
        [material_row("old", "5"), material_row("new", "2")],
    )
    from app.modules.reporting.link_work import wake_link_dependencies

    assert wake_link_dependencies(session, now=datetime.now(UTC)) == 1
    assert job.due_at <= datetime.now(UTC)
    assert job.error_code is None


def test_concurrent_requests_merge_and_do_not_wait_for_running_reader(
    committed_sync_seed,
):
    from concurrent.futures import ThreadPoolExecutor

    seed, factory = committed_sync_seed
    day = date(2026, 10, 5)
    with factory() as session:
        original = request(session, seed, day)
        job = session.get(ReportSyncJob, original)
        job.status, job.claim_token, job.claimed_until = (
            "RUNNING",
            uuid4(),
            datetime.now(UTC) + timedelta(minutes=4),
        )
        session.commit()
    with factory() as running:
        running.get(ReportSyncJob, original, with_for_update=True)

        def enqueue(offset):
            with factory() as session:
                result = request(session, seed, day - timedelta(days=offset))
                session.commit()
                return result

        with ThreadPoolExecutor(2) as workers:
            futures = [workers.submit(enqueue, i) for i in (1, 2)]
            results = [f.result(timeout=5) for f in futures]
        assert sum(r is not None for r in results) == 1
    with factory() as session:
        queued = session.exec(
            select(ReportSyncJob).where(
                ReportSyncJob.tenant_id == seed.tenant_id,
                ReportSyncJob.status == "QUEUED",
            )
        ).all()
        assert {(job.start_date, job.end_date) for job in queued} == {
            (date(2026, 10, 3), date(2026, 10, 4)),
        }
        assert session.get(ReportSyncJob, original).start_date == day


def test_legacy_adoption_preserves_each_frozen_scope_and_checkpoint(session, sync_seed):
    from app.modules.sync_control.report_demand import adopt_report_job

    base = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "actor_id": sync_seed.actor_id,
        "frozen_route": _route(sync_seed),
        "kind": "material_links",
        "priority": 3,
        "advertiser_ids": ["acc-1"],
        "due_at": datetime.now(UTC),
    }
    old = ReportSyncJob(
        **base,
        job_key=uuid4().hex,
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 1),
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
        progress={"overview_ad_ids": ["a1"], "next_page": 3},
    )
    second = ReportSyncJob(
        **base,
        job_key=uuid4().hex,
        start_date=date(2026, 9, 3),
        end_date=date(2026, 9, 3),
    )
    session.add_all([old, second])
    session.flush()
    assert adopt_report_job(session, old) > 0
    assert adopt_report_job(session, second) > 0
    assert old.end_date == old.start_date == date(2026, 9, 1)
    assert old.progress["next_page"] == 3
    assert old.progress["overview_ad_ids"] == ["a1"]
    assert second.status == "QUEUED"
    assert second.start_date == second.end_date == date(2026, 9, 3)
    assert "merged_into" not in second.progress
    assert adopt_report_job(session, old) == 0


@pytest.mark.parametrize("complete", [True, False])
def test_directory_evidence_wakes_same_work_and_completes_without_api(
    committed_sync_seed,
    complete,
):
    from app.modules.ads.models import AdMaterialReference
    from app.modules.reporting.store_models import MaterialPostLink
    from tests.modules.reporting.test_material_link_resume import step
    from tests.modules.reporting.test_material_query import D, material_row, publish

    seed, factory = committed_sync_seed
    with factory() as session:
        publish(session, seed, "acc-1", [material_row("p", "5", "file.mp4")])
        session.add(
            MaterialPostLink(
                tenant_id=seed.tenant_id,
                advertiser_id="acc-1",
                main_material_id="p",
                ad_remote_id="ad",
                main_material_type="VIDEO_SPARK_ADS",
                ad_material_id="m",
                observed_at=datetime.now(UTC),
            )
        )
        job_id = request(session, seed, D)
        session.commit()

    class NoRemote:
        calls = 0

        def material_ad_links(self, **kwargs):
            raise AssertionError("本地目录证据足以解析")

    assert step(factory, job_id, NoRemote()) == "WAITING"
    with factory() as session:
        session.add(
            AdMaterialReference(
                tenant_id=seed.tenant_id,
                advertiser_id="acc-1",
                ad_remote_id="ad",
                ad_material_id="m",
                platform_material_id="v",
                material_type="VIDEO",
                name="file.mp4",
                complete=complete,
                published_version=1,
            )
        )
        session.flush()
        from app.modules.reporting.link_work import wake_link_dependencies

        assert wake_link_dependencies(session, now=datetime.now(UTC)) == 1
        job = session.get(ReportSyncJob, job_id)
        assert job.due_at <= datetime.now(UTC) and job.error_code is None
        session.commit()
    assert step(factory, job_id, NoRemote()) == "COMPLETE"
    with factory() as session:
        row = session.get(MaterialPostLink, (seed.tenant_id, "acc-1", "p", "ad"))
        assert row.platform_material_id == "v"


def test_missing_directory_is_requested_and_new_evidence_wakes_without_fact_refresh(
    committed_sync_seed, monkeypatch
):
    from app.core.config import settings
    from app.modules.ads.models import AdMaterialReference, AdObject
    from app.modules.ads.sync_models import AdDirectoryRun
    from app.modules.reporting import link_work
    from app.modules.reporting.store_models import MaterialPostLink
    from tests.modules.reporting.test_material_link_resume import step
    from tests.modules.reporting.test_material_query import D, material_row, publish

    seed, factory = committed_sync_seed
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    with factory() as session:
        publish(session, seed, "acc-1", [material_row("p", "5", "file.mp4")])
        session.add(
            AdObject(
                tenant_id=seed.tenant_id,
                advertiser_id="acc-1",
                kind="ad",
                remote_id="ad",
                ad_type="SMART_PLUS",
                name="known-ad",
                observed_at=datetime.now(UTC),
                published_version=1,
            )
        )
        session.add(
            MaterialPostLink(
                tenant_id=seed.tenant_id,
                advertiser_id="acc-1",
                main_material_id="p",
                ad_remote_id="ad",
                main_material_type="VIDEO_SPARK_ADS",
                ad_material_id="m",
                observed_at=datetime.now(UTC),
            )
        )
        job_id = request(session, seed, D)
        session.commit()

    class NoRemote:
        calls = 0

        def material_ad_links(self, **kwargs):
            raise AssertionError("无需再次读取帖子概览")

    assert step(factory, job_id, NoRemote()) == "WAITING"
    with factory() as session:
        runs = session.exec(
            select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == seed.tenant_id)
        ).all()
        assert len(runs) == 1 and runs[0].query["ids"] == ["ad"]
        assert runs[0].actor_id == seed.actor_id and runs[0].frozen_route == _route(
            seed
        )
        session.add(
            AdMaterialReference(
                tenant_id=seed.tenant_id,
                advertiser_id="acc-1",
                ad_remote_id="ad",
                ad_material_id="m",
                platform_material_id="v",
                material_type="VIDEO",
                name="file.mp4",
                complete=True,
                published_version=1,
            )
        )
        session.flush()
        link_work.wake_link_dependencies(session, now=datetime.now(UTC))
        job = session.get(ReportSyncJob, job_id)
        assert job.due_at <= datetime.now(UTC) and job.error_code is None
        session.commit()
    assert step(factory, job_id, NoRemote()) == "COMPLETE"


def test_history_keeps_progress_when_every_round_adds_more_current_work(
    session, sync_seed, monkeypatch
):
    now = datetime.now(UTC)
    today = now.astimezone(ZoneInfo("Asia/Hong_Kong")).date()
    monkeypatch.setattr(job_tasks, "REPORT_DISPATCH_WINDOW", 4)
    old = request(session, sync_seed, today - timedelta(days=3), due_at=now)
    seen = set()
    for turn in range(12):
        for index in range(6):
            request(
                session,
                sync_seed,
                today,
                campaign_ids=[f"live-{turn}-{index}"],
                due_at=now,
            )
        running = claim_jobs(session, now=now, limit=4)
        for row in running:
            seen.add("history" if row.id == old else "current")
            row.status = "WAITING" if row.id == old else "COMPLETE"
            row.due_at = now + timedelta(seconds=1)
            from app.modules.sync_control.schedule import record_report_outcome

            record_report_outcome(session, row)
            row.claim_token = row.claimed_until = None
        session.flush()
        now += timedelta(seconds=1)
    assert seen == {"history", "current"}


def test_dependency_probe_uses_name_identity_index_in_large_account(session, sync_seed):
    from sqlalchemy import event, text

    from app.modules.reporting.material_links import directory_signature
    from tests.modules.reporting.test_material_query import D, material_row, publish

    publish(session, sync_seed, "acc-1", [material_row("old-post", "5", "needle.mp4")])
    job_id = request(session, sync_seed, D)
    session.connection().execute(
        text("""
        INSERT INTO ad_material_reference (id,tenant_id,advertiser_id,ad_remote_id,platform_material_id,
          ad_material_id,material_type,name,creative_ids,complete,published_version)
        SELECT gen_random_uuid(),:tenant,'acc-1','ad-'||n::text,'video-'||n::text,
          'material-'||n::text,'VIDEO',CASE WHEN n=123 THEN 'needle.mp4' ELSE 'file-'||n::text END,
          '[]'::jsonb,true,1 FROM generate_series(1,20000) n
    """),
        {"tenant": sync_seed.tenant_id},
    )
    session.connection().execute(text("ANALYZE ad_material_reference"))
    connection = session.connection()

    def explain(_conn, _cursor, statement, parameters, *_rest):
        if "missing AS MATERIALIZED" in statement:
            statement = "EXPLAIN (ANALYZE, FORMAT JSON) " + statement
        return statement, parameters

    event.listen(connection, "before_cursor_execute", explain, retval=True)
    try:
        plan = directory_signature(session, session.get(ReportSyncJob, job_id))[0][
            "Plan"
        ]
    finally:
        event.remove(connection, "before_cursor_execute", explain)

    def nodes(node):
        yield node
        for child in node.get("Plans", []):
            yield from nodes(child)

    probes = [
        n
        for n in nodes(plan)
        if n.get("Relation Name") == "ad_material_reference"
        and "md5" in n.get("Index Cond", "")
    ]
    assert probes and all(n["Actual Rows"] <= 1 for n in probes), plan


@pytest.mark.parametrize("facts", [0, 6])
def test_historical_links_use_backfill_pool_and_share_with_facts(
    session, sync_seed, monkeypatch, facts
):
    from app.jobs.models import PendingDispatch

    now = datetime.now(UTC)
    today = now.astimezone(ZoneInfo("Asia/Hong_Kong")).date()
    monkeypatch.setattr(job_tasks, "REPORT_DISPATCH_WINDOW", 3)
    for index in range(6):
        request(session, sync_seed, today, campaign_ids=[str(index)], due_at=now)
        request(
            session,
            sync_seed,
            today - timedelta(days=3),
            campaign_ids=[str(index)],
            due_at=now,
        )
    for index in range(facts):
        request(
            session,
            sync_seed,
            today - timedelta(days=10 + index),
            kind="ad_day",
            tier="history",
            due_at=now,
        )
    seen = set()
    for turn in range(16):
        running = claim_jobs(session, now=now + timedelta(seconds=turn), limit=5)
        for row in running:
            seen.add((row.kind, row.start_date < today))
            dispatch = session.exec(
                select(PendingDispatch).where(
                    PendingDispatch.task_key.startswith(f"report-job:{row.id}:")
                )
            ).one()
            assert dispatch.task_name == (
                "reporting.backfill_step"
                if row.kind == "ad_day" or row.start_date < today
                else "reporting.job_step"
            )
            row.status = "COMPLETE"
            from app.modules.sync_control.schedule import record_report_outcome

            record_report_outcome(session, row)
            row.claim_token = row.claimed_until = None
        session.flush()
    assert {("material_links", False), ("material_links", True)} <= seen
    if facts:
        assert ("ad_day", True) in seen


@pytest.mark.parametrize("origin_tier", ["daily", "history", "manual"])
def test_backfill_link_continuations_cannot_starve_other_origin_priorities(
    session, sync_seed, monkeypatch, origin_tier
):
    now = datetime.now(UTC)
    today = now.astimezone(ZoneInfo("Asia/Hong_Kong")).date()
    monkeypatch.setattr(job_tasks, "REPORT_DISPATCH_WINDOW", 2)
    old = request(
        session,
        sync_seed,
        today - timedelta(days=4),
        tier=origin_tier,
        due_at=now - timedelta(minutes=10),
    )
    for index in range(3):
        request(
            session,
            sync_seed,
            today - timedelta(days=4),
            tier="material",
            campaign_ids=[str(index)],
            due_at=now,
        )
    job_tasks._dispatch_report_jobs(session, now)
    assert session.get(ReportSyncJob, old).status == "RUNNING"
