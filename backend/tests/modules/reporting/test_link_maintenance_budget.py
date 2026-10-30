"""冷目录证据查询不能回滚已完成唤醒，或令同一批旧任务永久挡住后续。"""

from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlmodel import select

from app.modules.reporting import link_work, material_links
from app.modules.reporting.query_budget import QueryBudgetExceeded
from app.modules.reporting.store_models import ReportSyncJob
from tests.modules.reporting.test_jobs import _route


def waiting(session, seed, now, index=0, **changes):
    values = {
        "tenant_id": seed.tenant_id,
        "bc_id": "bc-a",
        "actor_id": seed.actor_id,
        "frozen_route": _route(seed),
        "kind": "material_links",
        "status": "WAITING",
        "priority": 3,
        "advertiser_ids": ["acc-1"],
        "start_date": date(2026, 10, 1),
        "end_date": date(2026, 10, 1),
        "created_at": now - timedelta(days=2) + timedelta(seconds=index),
        "due_at": now + timedelta(hours=2),
        "error_code": "material_directory_pending",
        "job_key": uuid4().hex,
        "progress": {"coalesced_v1": True, "waiting_ids": ["missing"]},
    }
    values.update(changes)
    job = ReportSyncJob(**values)
    session.add(job)
    session.flush()
    return job


def test_slow_probe_rolls_back_only_its_savepoint_and_wakes_next(
    session, sync_seed, monkeypatch
):
    now = datetime.now(UTC)
    slow = waiting(session, sync_seed, now)
    fast = waiting(session, sync_seed, now, 1)
    slow_id = slow.id
    original_due = slow.due_at

    def changed(db, job):
        if job.id == slow_id:
            job.progress = job.progress | {"must_rollback": True}
            db.flush()
            db.execute(text("SELECT pg_sleep(0.5)"))
        return True

    monkeypatch.setattr(material_links, "dependency_changed", changed)
    session.execute(text("SET LOCAL statement_timeout='30ms'"))
    assert link_work.wake_link_dependencies(session, now=now) == 1
    assert slow.due_at == original_due
    assert slow.error_code == "material_directory_pending"
    assert "must_rollback" not in slow.progress
    assert slow.progress["dependency_probed_at"] == now.isoformat()
    assert fast.due_at == now and fast.error_code is None
    assert session.execute(text("SHOW statement_timeout")).scalar_one() == "30ms"


def test_timed_out_first_probe_rotates_after_commit(committed_sync_seed, monkeypatch):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session:
        first = waiting(session, seed, now).id
        second = waiting(session, seed, now, 1).id
        session.commit()

    seen = []

    def changed(db, job):
        seen.append(job.id)
        if job.id == first:
            db.execute(text("SELECT pg_sleep(0.5)"))
        return False

    monkeypatch.setattr(material_links, "dependency_changed", changed)
    for turn in range(2):
        with factory() as session:
            session.execute(text("SET LOCAL statement_timeout='30ms'"))
            assert (
                link_work.wake_link_dependencies(
                    session, now=now + timedelta(seconds=turn), limit=1
                )
                == 0
            )
            session.commit()
    assert seen == [first, second]


def test_wake_does_not_touch_an_effective_lease(session, sync_seed, monkeypatch):
    now = datetime.now(UTC)
    job = waiting(session, sync_seed, now, claimed_until=now + timedelta(minutes=1))
    monkeypatch.setattr(material_links, "dependency_changed", lambda *_: True)
    assert link_work.wake_link_dependencies(session, now=now) == 0
    assert job.error_code == "material_directory_pending"
    assert "dependency_probed_at" not in job.progress


def test_budget_exhaustion_keeps_old_evidence_and_rotates(
    session, sync_seed, monkeypatch
):
    now = datetime.now(UTC)
    job = waiting(session, sync_seed, now)
    before = job.progress.copy()

    def exhausted(*_):
        raise QueryBudgetExceeded()

    monkeypatch.setattr(material_links, "dependency_changed", exhausted)
    assert link_work.wake_link_dependencies(session, now=now) == 0
    assert job.progress == before | {"dependency_probed_at": now.isoformat()}
    assert job.error_code == "material_directory_pending"
    assert job.due_at == now + timedelta(hours=2)


def test_valid_probe_may_take_more_than_two_hundred_ms(session, sync_seed, monkeypatch):
    now = datetime.now(UTC)
    job = waiting(session, sync_seed, now)

    def changed(db, _job):
        db.execute(text("SELECT pg_sleep(0.25)"))
        return True

    monkeypatch.setattr(material_links, "dependency_changed", changed)
    assert (
        link_work.wake_link_dependencies(
            session, now=now, deadline=datetime.now(UTC) + timedelta(seconds=1)
        )
        == 1
    )
    assert job.due_at == now and job.error_code is None


@pytest.mark.parametrize(
    "error", [ValueError("permission denied"), KeyboardInterrupt()]
)
def test_unexpected_probe_errors_are_not_swallowed(
    session, sync_seed, monkeypatch, error
):
    now = datetime.now(UTC)
    waiting(session, sync_seed, now)

    def changed(*_):
        raise error

    monkeypatch.setattr(material_links, "dependency_changed", changed)
    with pytest.raises(type(error)):
        link_work.wake_link_dependencies(session, now=now)


def test_locked_candidate_does_not_block_next(committed_sync_seed, monkeypatch):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session:
        locked_id = waiting(session, seed, now).id
        next_id = waiting(session, seed, now, 1).id
        session.commit()
    monkeypatch.setattr(material_links, "dependency_changed", lambda *_: True)
    with factory() as owner, factory() as follower:
        owner.exec(
            select(ReportSyncJob).where(ReportSyncJob.id == locked_id).with_for_update()
        ).one()
        follower.execute(text("SET LOCAL lock_timeout='30ms'"))
        assert link_work.wake_link_dependencies(follower, now=now) == 1
        assert follower.get(ReportSyncJob, next_id).error_code is None
        follower.commit()
        assert (
            owner.get(ReportSyncJob, locked_id).error_code
            == "material_directory_pending"
        )


def test_more_than_fifty_dependencies_rotate_without_losing_scope(
    committed_sync_seed, monkeypatch
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session:
        expected = {waiting(session, seed, now, i).id for i in range(60)}
        session.commit()
    seen = []

    def unchanged(_session, job):
        assert job.tenant_id == seed.tenant_id
        assert job.actor_id == seed.actor_id and job.frozen_route == _route(seed)
        seen.append(job.id)
        return False

    monkeypatch.setattr(material_links, "dependency_changed", unchanged)
    for turn in range(2):
        with factory() as session:
            assert (
                link_work.wake_link_dependencies(
                    session, now=now + timedelta(seconds=turn)
                )
                == 0
            )
            session.commit()
    assert set(seen[:60]) == expected


@pytest.mark.parametrize("query_name", ["WAKE_CANDIDATES_SQL"])
def test_candidates_read_active_index_instead_of_completed_history(
    session, sync_seed, query_name
):
    now = datetime.now(UTC)
    job = waiting(session, sync_seed, now, progress={})
    session.execute(
        text("""
        INSERT INTO report_sync_job
          (id,tenant_id,bc_id,kind,priority,status,advertiser_ids,campaign_ids,
           start_date,end_date,frozen_route,actor_id,job_key,due_at,attempts,
           request_count,progress,created_at)
        SELECT gen_random_uuid(),tenant_id,bc_id,kind,priority,'COMPLETE',
          advertiser_ids,campaign_ids,start_date,end_date,frozen_route,actor_id,
          'historical-' || g,due_at,attempts,request_count,progress,created_at
        FROM report_sync_job CROSS JOIN generate_series(1,5000) g WHERE id=:id
        """),
        {"id": job.id},
    )
    session.execute(text("ANALYZE report_sync_job"))
    sql = getattr(link_work, query_name).format(after="")
    params = {
        "now": now,
        "limit": 50,
        "errors": sorted(material_links.DEPENDENCY_ERRORS),
    }
    assert session.execute(text(sql), params).scalars().all() == [job.id]
    plan = session.execute(
        text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql), params
    ).scalar_one()[0]["Plan"]

    def nodes(node):
        yield node
        for child in node.get("Plans", []):
            yield from nodes(child)

    parts = list(nodes(plan))
    active = next(
        node for node in parts if node.get("Subplan Name") == "CTE active_links"
    )
    assert active["Actual Rows"] == 1
    assert any(
        node.get("Index Name") == "ix_report_link_pending_scope" for node in parts
    )
    assert not any(
        node.get("Relation Name") == "report_sync_job"
        and node["Node Type"] == "Seq Scan"
        for node in parts
    )


def isolate_planning(monkeypatch):
    from app.core.config import settings
    from app.modules.reporting import job_tasks

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(job_tasks.planner, "ensure_bc_plans", lambda *_, **__: None)
    monkeypatch.setattr(job_tasks.planner, "due_plans", lambda *_, **__: [])
    monkeypatch.setattr(job_tasks.planner, "backfill_allowed", lambda *_: True)
    monkeypatch.setattr(job_tasks, "_dispatch_pending", lambda *_, **__: 0)
    return job_tasks


def test_plan_due_commits_wake_without_rewriting_frozen_link_scope(
    committed_sync_seed, redis_client, monkeypatch
):
    from app.core.db import engine

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session:
        job_id = waiting(session, seed, now).id
        session.commit()
    tasks = isolate_planning(monkeypatch)
    monkeypatch.setattr(material_links, "dependency_changed", lambda *_: True)

    assert (
        tasks.plan_due_runs(database_engine=engine, redis_client=redis_client, now=now)
        == 0
    )
    with factory() as session:
        job = session.get(ReportSyncJob, job_id)
        assert job.due_at == now and job.error_code is None
        assert job.progress["dependency_probed_at"] == now.isoformat()
        assert job.start_date == job.end_date == date(2026, 10, 1)
        assert job.status == "WAITING"


def test_plan_due_wake_database_failure_preserves_both_frozen_jobs(
    committed_sync_seed, redis_client, monkeypatch
):
    from app.core.db import engine

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session:
        first = waiting(session, seed, now, progress={}).id
        second = waiting(session, seed, now, 1, progress={}).id
        session.commit()
    tasks = isolate_planning(monkeypatch)

    def failed_probe(session, job):
        job.progress = {"wake_rolled_back": True}
        session.flush()
        session.execute(text("SELECT 1 / 0"))

    monkeypatch.setattr(material_links, "dependency_changed", failed_probe)
    assert (
        tasks.plan_due_runs(database_engine=engine, redis_client=redis_client, now=now)
        == 0
    )
    with factory() as session:
        root = session.get(ReportSyncJob, first)
        merged = session.get(ReportSyncJob, second)
        assert "wake_rolled_back" not in root.progress
        assert "dependency_probed_at" not in root.progress
        assert root.status == merged.status == "WAITING"
        assert "merged_into" not in merged.progress
        assert root.start_date == merged.start_date == date(2026, 10, 1)


def orphan_directory(session, job, now, ids=("ad",), **changes):
    from app.modules.ads.sync_models import AdDirectoryRun

    values = {
        "tenant_id": job.tenant_id,
        "bc_id": job.bc_id,
        "actor_id": job.actor_id,
        "frozen_route": job.frozen_route,
        "advertiser_id": job.advertiser_ids[0],
        "connection_id": UUID(job.frozen_route["connection_id"]),
        "channel": job.frozen_route["channel"],
        "kind": "ad",
        "ad_type": "SMART_PLUS",
        "workload": "background",
        "status": "STALE",
        "error_code": "directory_run_stale",
        "partition_key": uuid4().hex * 2,
        "query": {"ids": list(ids), "parent_ids": [], "include_deleted": True},
        "created_at": now - timedelta(hours=1),
    }
    values.update(changes)
    run = AdDirectoryRun(**values)
    session.add(run)
    session.flush()
    return run


def missing_link(session, job, now, post="missing", ad="ad"):
    from app.modules.reporting.store_models import MaterialPostLink

    session.add(
        MaterialPostLink(
            tenant_id=job.tenant_id,
            advertiser_id=job.advertiser_ids[0],
            main_material_id=post,
            ad_remote_id=ad,
            main_material_type="VIDEO_SPARK_ADS",
            observed_at=now,
        )
    )
    session.flush()


def test_orphan_wakes_original_job_once_and_normal_request_rebuilds(
    session, sync_seed, monkeypatch
):
    from app.core.config import settings
    from app.modules.ads.sync_models import AdDirectoryRun
    from app.modules.reporting.directory_refresh import request_link_directory
    from tests.modules.reporting.test_material_query import D, material_row, publish

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    publish(session, sync_seed, "acc-1", [material_row("missing", "5")])
    job = waiting(session, sync_seed, now, start_date=D, end_date=D, attempts=3)
    job.progress = job.progress | {
        "dependency_signature": "unchanged",
        "dependency_attempts": 6,
    }
    session.add(job)
    missing_link(session, job, now)
    old = orphan_directory(session, job, now)
    monkeypatch.setattr(material_links, "dependency_changed", lambda *_: False)
    assert link_work.wake_link_dependencies(session, now=now) == 1
    assert job.due_at == now and job.error_code == "material_directory_pending"
    assert job.attempts == 3 and job.progress["dependency_attempts"] == 6
    assert job.progress["dependency_signature"] == "unchanged"
    assert job.progress["dependency_recovery_sequences"] == [old.request_sequence]
    assert old.status == "STALE"
    assert request_link_directory(session, job, now=now) == 1
    new = session.exec(select(AdDirectoryRun).where(AdDirectoryRun.id != old.id)).one()
    assert new.status == "QUEUED" and new.query["ids"] == ["ad"]
    assert new.query["include_deleted"] is True and new.workload == "background"
    assert new.frozen_route == job.frozen_route and new.actor_id == job.actor_id
    assert material_links.wait_for_dependencies(session, job, now=now)
    assert job.progress["dependency_recovery_sequences"] == [old.request_sequence]
    # 即使后继另因外部错误失败，也不能把同一个旧误淘汰反复当作新事件。
    new.status = "FAILED"
    job.due_at = now + timedelta(hours=6)
    session.flush()
    assert (
        link_work.wake_link_dependencies(session, now=now + timedelta(minutes=11)) == 0
    )
    assert job.due_at == now + timedelta(hours=6)


@pytest.mark.parametrize(
    "status", ["QUEUED", "RUNNING", "WAITING_REMOTE", "ADMISSION_WAIT", "COMPLETE"]
)
def test_existing_needed_coverage_does_not_trigger_orphan_recovery(
    session, sync_seed, monkeypatch, status
):
    now = datetime.now(UTC)
    job = waiting(session, sync_seed, now)
    missing_link(session, job, now)
    orphan_directory(session, job, now, ids=("ad", "unrelated-old-target"))
    orphan_directory(
        session,
        job,
        now,
        status=status,
        error_code=None,
        coverage="COMPLETE" if status == "COMPLETE" else "PENDING",
        published_version=1 if status == "COMPLETE" else None,
    )
    monkeypatch.setattr(material_links, "dependency_changed", lambda *_: False)
    assert link_work.wake_link_dependencies(session, now=now) == 0
    assert "dependency_recovery_sequences" not in job.progress


def test_temporarily_covered_lower_sequence_can_recover_after_cover_fails(
    session, sync_seed, monkeypatch
):
    now = datetime.now(UTC)
    job = waiting(session, sync_seed, now, progress={"waiting_ids": ["a", "b"]})
    missing_link(session, job, now, "a", "ad-a")
    missing_link(session, job, now, "b", "ad-b")
    first = orphan_directory(session, job, now, ids=("ad-a",))
    second = orphan_directory(session, job, now, ids=("ad-b",))
    cover = orphan_directory(
        session, job, now, ids=("ad-a",), status="QUEUED", error_code=None
    )
    monkeypatch.setattr(material_links, "dependency_changed", lambda *_: False)
    assert link_work.wake_link_dependencies(session, now=now) == 1
    assert job.progress["dependency_recovery_sequences"] == [second.request_sequence]
    cover.status = "FAILED"
    job.due_at = now + timedelta(hours=2)
    session.flush()
    assert (
        link_work.wake_link_dependencies(session, now=now + timedelta(seconds=1)) == 1
    )
    assert set(job.progress["dependency_recovery_sequences"]) == {
        first.request_sequence,
        second.request_sequence,
    }


@pytest.mark.parametrize(
    "change",
    ["tenant", "actor", "bc", "route", "unrelated", "resolved", "identity_error"],
)
def test_orphan_recovery_respects_job_scope_and_actual_missing_links(
    session, sync_seed, monkeypatch, change
):
    from app.modules.reporting.store_models import MaterialPostLink

    now = datetime.now(UTC)
    job = waiting(session, sync_seed, now)
    missing_link(session, job, now)
    orphan_directory(session, job, now)
    if change == "tenant":
        job.tenant_id = uuid4()
    elif change == "actor":
        job.actor_id = uuid4()
    elif change == "bc":
        job.bc_id = "other-bc"
    elif change == "route":
        job.frozen_route = job.frozen_route | {"binding_revision": 99}
    elif change == "unrelated":
        job.progress = {"waiting_ids": ["other-post"]}
    elif change == "resolved":
        session.get(
            MaterialPostLink, (job.tenant_id, "acc-1", "missing", "ad")
        ).platform_material_id = "video"
    else:
        job.error_code = "material_identity_unavailable"
    session.flush()
    monkeypatch.setattr(material_links, "dependency_changed", lambda *_: False)
    assert link_work.wake_link_dependencies(session, now=now) == 0
    assert "dependency_recovery_sequences" not in job.progress


def test_recent_terminal_request_waits_for_existing_ten_minute_throttle(
    session, sync_seed, monkeypatch
):
    now = datetime.now(UTC)
    job = waiting(session, sync_seed, now)
    missing_link(session, job, now)
    old = orphan_directory(session, job, now, created_at=now - timedelta(minutes=5))
    monkeypatch.setattr(material_links, "dependency_changed", lambda *_: False)
    assert link_work.wake_link_dependencies(session, now=now) == 0
    assert "dependency_recovery_sequences" not in job.progress
    assert (
        link_work.wake_link_dependencies(session, now=now + timedelta(minutes=6)) == 1
    )
    assert job.progress["dependency_recovery_sequences"] == [old.request_sequence]


def test_completed_observation_before_failed_request_is_not_new_coverage(
    session, sync_seed, monkeypatch
):
    now = datetime.now(UTC)
    job = waiting(session, sync_seed, now)
    missing_link(session, job, now)
    orphan_directory(
        session,
        job,
        now,
        status="COMPLETE",
        error_code=None,
        published_version=1,
        coverage="COMPLETE",
    )
    old = orphan_directory(session, job, now)
    monkeypatch.setattr(material_links, "dependency_changed", lambda *_: False)
    assert link_work.wake_link_dependencies(session, now=now) == 1
    assert job.progress["dependency_recovery_sequences"] == [old.request_sequence]


def test_orphan_recovery_does_not_change_today_priority_or_window(
    session, sync_seed, monkeypatch
):
    now = datetime.now(UTC)
    today = now.astimezone(ZoneInfo("Asia/Hong_Kong")).date()
    job = waiting(session, sync_seed, now, start_date=today, end_date=today, priority=1)
    missing_link(session, job, now)
    orphan_directory(session, job, now, workload="live")
    monkeypatch.setattr(material_links, "dependency_changed", lambda *_: False)
    assert link_work.wake_link_dependencies(session, now=now) == 1
    assert job.priority == 1 and job.start_date == job.end_date == today


def test_recovery_marker_and_due_advance_roll_back_together(
    committed_sync_seed, monkeypatch
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session:
        job = waiting(session, seed, now)
        missing_link(session, job, now)
        orphan_directory(session, job, now)
        job_id = job.id
        session.commit()
    monkeypatch.setattr(material_links, "dependency_changed", lambda *_: False)
    with factory() as session:
        assert link_work.wake_link_dependencies(session, now=now) == 1
        session.rollback()
    with factory() as session:
        job = session.get(ReportSyncJob, job_id)
        assert job.due_at == now + timedelta(hours=2)
        assert "dependency_recovery_sequences" not in job.progress
        assert link_work.wake_link_dependencies(session, now=now) == 1
        session.commit()
    with factory() as session:
        job = session.get(ReportSyncJob, job_id)
        assert job.due_at == now
        assert len(job.progress["dependency_recovery_sequences"]) == 1
