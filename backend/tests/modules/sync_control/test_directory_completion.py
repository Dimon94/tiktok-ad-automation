"""完成时延：当前需求年龄与物理接口互不遮挡，真实PG/Redis。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlmodel import select

from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.sync_control.contracts import ExecutionFence
from app.modules.sync_control.directory_demand import directory_outcome
from app.modules.sync_control.directory_dispatch import dispatch_directories_due
from app.modules.sync_control.models import SyncSchedule
from app.modules.sync_control.requirements import satisfy_requirements
from tests.modules.sync_control.test_schedule_fencing import _directory


def _complete(session, run):
    run.status = "COMPLETE"
    satisfy_requirements(
        session,
        fence=ExecutionFence(
            run.id, "directory", run.claim_generation, run.owner_epoch
        ),
        observed_at=datetime.now(UTC),
        publication_version=1,
    )
    directory_outcome(session, run)


def test_directory_age_advances_to_next_unmet_execution(session, sync_seed):
    old = _directory(
        session, sync_seed, created_at=datetime.now(UTC) - timedelta(days=7)
    )
    recent = _directory(
        session, sync_seed, created_at=datetime.now(UTC) - timedelta(hours=1)
    )
    head = session.exec(
        select(SyncSchedule).where(SyncSchedule.tenant_id == sync_seed.tenant_id)
    ).one()
    assert head.oldest_pending_at == old.created_at
    _complete(session, old)
    session.flush()
    session.refresh(head)
    assert head.active and head.oldest_pending_at == recent.created_at


def test_directory_age_clears_when_no_unmet_execution(session, sync_seed):
    run = _directory(
        session, sync_seed, created_at=datetime.now(UTC) - timedelta(days=7)
    )
    head = session.exec(
        select(SyncSchedule).where(SyncSchedule.tenant_id == sync_seed.tenant_id)
    ).one()
    _complete(session, run)
    session.flush()
    session.refresh(head)
    assert not head.active and head.oldest_pending_at is None


def test_reopened_directory_does_not_inherit_satisfied_history(session, sync_seed):
    old = _directory(
        session, sync_seed, created_at=datetime.now(UTC) - timedelta(days=7)
    )
    _complete(session, old)
    recent = _directory(session, sync_seed)
    head = session.exec(
        select(SyncSchedule).where(SyncSchedule.tenant_id == sync_seed.tenant_id)
    ).one()
    session.refresh(head)
    assert head.active and head.oldest_pending_at == recent.created_at


def test_locked_first_endpoint_does_not_hide_other_ready_endpoint(
    committed_sync_seed, redis_client
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as db, db.begin():
        first = _directory(db, seed, advertiser="acc-1")
        second = _directory(db, seed, advertiser="acc-2", ad_type="REGULAR")
        first.workload = second.workload = "live"
        heads = db.exec(
            select(SyncSchedule).where(SyncSchedule.tenant_id == seed.tenant_id)
        ).all()
        for head in heads:
            head.last_dispatched_at = now - timedelta(
                hours=2 if head.physical_endpoint.endswith("/smart_plus/ad/get/") else 1
            )
        first_id, second_id = first.id, second.id
        endpoint = next(
            h.physical_endpoint
            for h in heads
            if h.physical_endpoint.endswith("/smart_plus/ad/get/")
        )
    with factory() as blocker, blocker.begin():
        blocker.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": f"tkada:sync-budget:{seed.tenant_id}:{endpoint}"},
        )
        with factory() as claimant, claimant.begin():
            assert (
                dispatch_directories_due(
                    claimant,
                    redis_client,
                    now=datetime.now(UTC) + timedelta(seconds=1),
                    limit=1,
                )
                == 1
            )
            assert claimant.get(AdDirectoryRun, first_id).claim_generation == 1
            assert claimant.get(AdDirectoryRun, second_id).claim_generation == 2


def test_completion_opportunities_keep_old_account_moving_without_losing_ordinary(
    session, sync_seed, redis_client
):
    from app.modules.reporting.store_models import ReportDispatchCursor
    from app.modules.sync_control.directory_demand import directory_endpoint
    from app.modules.sync_control.directory_dispatch import _candidate

    now = datetime.now(UTC)
    oldest = _directory(session, sync_seed, created_at=now - timedelta(days=7))
    newer = _directory(
        session, sync_seed, advertiser="acc-2", created_at=now - timedelta(hours=1)
    )
    oldest.workload = newer.workload = "background"
    heads = session.exec(select(SyncSchedule)).all()
    for h in heads:
        h.last_dispatched_at = now - timedelta(
            hours=1 if h.oldest_pending_at == oldest.created_at else 2
        )
    session.add(
        ReportDispatchCursor(
            tenant_id=sync_seed.tenant_id,
            last_dispatched_at=now,
            fairness_state={
                "__directory_policy__:" + directory_endpoint(oldest): {"turn": 1.0}
            },
        )
    )
    session.flush()
    assert (
        dispatch_directories_due(
            session, redis_client, now=now + timedelta(seconds=1), limit=1
        )
        == 1
    )
    assert oldest.claim_generation == 2 and newer.claim_generation == 1
    # 优先成功不能推进普通服务年龄游标；新请求的普通机会仍是它自己。
    found = _candidate(
        session,
        tenant_id=sync_seed.tenant_id,
        work_class="directory",
        now=now + timedelta(seconds=1),
        independent_only=False,
        discover_only=True,
        redis=redis_client,
        endpoint=directory_endpoint(oldest),
    )
    assert found[1].id == newer.id


def test_sole_account_can_keep_its_completion_opportunity(
    session, sync_seed, redis_client
):
    from app.modules.reporting.store_models import ReportDispatchCursor
    from app.modules.sync_control.directory_demand import directory_endpoint

    now = datetime.now(UTC)
    old = _directory(
        session,
        sync_seed,
        query_extra={"page_size": 10},
        created_at=now - timedelta(days=7),
    )
    recent = _directory(
        session,
        sync_seed,
        query_extra={"page_size": 20},
        created_at=now - timedelta(hours=1),
    )
    old.workload = recent.workload = "background"
    for head in session.exec(select(SyncSchedule)).all():
        head.last_dispatched_at = now - timedelta(
            hours=1 if head.oldest_pending_at == old.created_at else 2
        )
    session.add(
        ReportDispatchCursor(
            tenant_id=sync_seed.tenant_id,
            last_dispatched_at=now,
            fairness_state={
                "__directory_policy__:" + directory_endpoint(old): {
                    "turn": 1.0,
                    "account:acc-1": 1.0,
                }
            },
        )
    )
    session.flush()
    assert (
        dispatch_directories_due(
            session, redis_client, now=now + timedelta(seconds=1), limit=1
        )
        == 1
    )
    assert old.claim_generation == 2 and recent.claim_generation == 1


def test_targeted_live_grant_does_not_consume_background_opportunity(
    session, sync_seed, redis_client
):
    from app.modules.reporting.store_models import ReportDispatchCursor
    from app.modules.sync_control.directory_demand import directory_endpoint

    run = _directory(session, sync_seed)
    now = datetime.now(UTC) + timedelta(seconds=1)
    key = "__directory_policy__:" + directory_endpoint(run)
    session.add(
        ReportDispatchCursor(
            tenant_id=sync_seed.tenant_id,
            last_dispatched_at=now,
            fairness_state={key: {"turn": 1.0}},
        )
    )
    session.flush()
    assert dispatch_directories_due(session, redis_client, now=now, limit=1) == 1
    cursor = session.get(
        ReportDispatchCursor, sync_seed.tenant_id, populate_existing=True
    )
    assert cursor.fairness_state[key]["turn"] == 1.0


def test_periodic_review_budget_is_shared_across_all_endpoints(
    session, sync_seed, redis_client, monkeypatch
):
    from app.modules.ads.directory_calibration import PERIODIC_KEY
    from app.modules.sync_control.directory_demand import register_directory_run
    from tests.modules.reporting.test_directory_reduction import (
        case,
        complete,
        request,
        runs,
    )

    now, _ = case(session, sync_seed, monkeypatch, active=False)
    request(
        session,
        sync_seed,
        ad_types={"acc-1": {"REGULAR": ("campaign", "adgroup", "ad")}},
    )
    complete(session, sync_seed, now - timedelta(days=2))
    pending = []
    for previous in runs(session, sync_seed):
        values = previous.model_dump(exclude={"id", "request_id", "request_sequence"})
        values.update(
            status="QUEUED",
            published_version=None,
            completed_at=None,
            created_at=now - timedelta(days=2),
            query=previous.query | {PERIODIC_KEY: True},
        )
        run = AdDirectoryRun(**values)
        session.add(run)
        session.flush()
        register_directory_run(session, run)
        pending.append(run)
    assert len(pending) == 3
    assert (
        dispatch_directories_due(
            session,
            redis_client,
            now=now + timedelta(seconds=1),
            limit=40,
            max_claims=1,
        )
        == 0
    )
    assert sum(r.next_attempt_at is not None for r in pending) <= 2
