"""周期校准减量不能损失缺失分区、初始化、租约或显式刷新。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import select

from app.core.config import settings
from app.jobs.models import PendingDispatch
from app.modules.ads.directory_backlog import review_unstarted_directory
from app.modules.ads.directory_calibration import PERIODIC_KEY
from app.modules.ads.directory_requests import request_directory
from app.modules.ads.models import AdObject
from app.modules.ads.sync_models import AdDirectoryPage, AdDirectoryRun
from app.modules.reporting import job_tasks, planner
from app.modules.reporting.store_models import AdSyncBc
from app.modules.sync_control.directory_demand import (
    directory_outcome,
    register_directory_run,
)
from tests.modules.reporting.test_jobs import _route


def dispatch(session, now):
    # 原raw历史夹具经正式收编，当前调度只读取canonical到期头。
    for run in session.exec(select(AdDirectoryRun)).all():
        if run.status in {"QUEUED", "RUNNING", "WAITING_REMOTE", "ADMISSION_WAIT"}:
            register_directory_run(session, run)
        else:
            directory_outcome(session, run)
    session.flush()
    return job_tasks._dispatch_directories(session, now + timedelta(seconds=1))


def case(session, seed, monkeypatch, *, active=True, screened=True):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    planner.ensure_bc_plans(session, now=now)
    plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
    plan.last_active_advertiser_ids = ["acc-1"] if active else []
    plan.active_screened_at = now if screened else None
    session.add(
        AdObject(
            tenant_id=seed.tenant_id,
            advertiser_id="acc-1",
            kind="campaign",
            remote_id="campaign",
            ad_type="REGULAR",
            observed_at=now,
            published_version=1,
        )
    )
    session.flush()
    return now, plan


def request(session, seed, **kwargs):
    return request_directory(
        session,
        tenant_id=seed.tenant_id,
        bc_id="bc-a",
        actor_id=seed.actor_id,
        frozen_route=_route(seed),
        advertiser_ids=["acc-1"],
        scope="directory",
        **kwargs,
    )


def runs(session, seed):
    return session.exec(
        select(AdDirectoryRun).where(
            AdDirectoryRun.tenant_id == seed.tenant_id,
        )
    ).all()


def complete(session, seed, at):
    for row in runs(session, seed):
        row.request_id = uuid4()  # 历史完成请求不能命中当前半小时槽的请求去重。
        row.status = "COMPLETE"
        row.completed_at = row.observed_at = at
        row.published_version = 1
    session.flush()


@pytest.mark.parametrize(
    "active,age", [(True, timedelta(hours=4)), (False, timedelta(days=2))]
)
def test_periodic_calibration_skips_fresh_full_partitions(
    session, sync_seed, monkeypatch, active, age
):
    now, plan = case(session, sync_seed, monkeypatch, active=active)
    request(session, sync_seed)
    complete(session, sync_seed, now - age)
    job_tasks._plan_directory_step(session, plan, accounts=["acc-1"], now=now)
    assert not [r for r in runs(session, sync_seed) if r.status == "QUEUED"]


def test_one_complete_layer_does_not_hide_missing_partitions(
    session, sync_seed, monkeypatch
):
    now, plan = case(session, sync_seed, monkeypatch, active=False)
    request(session, sync_seed, ad_types={"acc-1": {"REGULAR": ("campaign",)}})
    complete(session, sync_seed, now - timedelta(hours=1))
    job_tasks._plan_directory_step(session, plan, accounts=["acc-1"], now=now)
    assert {
        r.kind
        for r in runs(session, sync_seed)
        if r.status == "QUEUED" and r.ad_type == "REGULAR"
    } == {"adgroup", "ad"}


@pytest.mark.parametrize("screened_age", [None, timedelta(hours=2)])
def test_unknown_activity_uses_daily_calibration_not_weekly(
    session, sync_seed, monkeypatch, screened_age
):
    now, plan = case(session, sync_seed, monkeypatch, active=False)
    plan.active_screened_at = now - screened_age if screened_age else None
    request(session, sync_seed)
    complete(session, sync_seed, now - timedelta(days=2))
    job_tasks._plan_directory_step(session, plan, accounts=["acc-1"], now=now)
    assert {
        r.kind
        for r in runs(session, sync_seed)
        if r.status == "QUEUED" and r.ad_type == "REGULAR"
    } == {"campaign", "adgroup", "ad"}


def test_calibration_continues_after_short_batch_interval(
    session, sync_seed, monkeypatch
):
    now, plan = case(session, sync_seed, monkeypatch)
    monkeypatch.setattr(job_tasks, "DIRECTORY_PLAN_ACCOUNTS", 1)
    job_tasks._plan_directory_step(session, plan, accounts=["acc-1", "acc-2"], now=now)
    assert plan.next_directory_at == now + timedelta(seconds=5)
    job_tasks._plan_directory_step(
        session, plan, accounts=["acc-1", "acc-2"], now=now + timedelta(seconds=5)
    )
    assert {r.advertiser_id for r in runs(session, sync_seed)} == {"acc-1", "acc-2"}


def test_account_list_change_restarts_calibration_without_minute_gate(
    session, sync_seed, monkeypatch
):
    now, plan = case(session, sync_seed, monkeypatch)
    monkeypatch.setattr(job_tasks, "DIRECTORY_PLAN_ACCOUNTS", 1)
    job_tasks._plan_directory_step(session, plan, accounts=["acc-2"], now=now)
    job_tasks._plan_directory_step(
        session, plan, accounts=["acc-1", "acc-2"], now=now + timedelta(seconds=5)
    )
    assert {r.advertiser_id for r in runs(session, sync_seed)} == {"acc-1", "acc-2"}


def test_obsolete_unstarted_backlog_is_closed_before_outbox(
    session, sync_seed, monkeypatch
):
    now, _ = case(session, sync_seed, monkeypatch)
    request(session, sync_seed, ad_types={"acc-1": {"REGULAR": ("campaign",)}})
    old = runs(session, sync_seed)[0]
    newer = AdDirectoryRun(
        **old.model_dump(exclude={"id", "request_id", "request_sequence"})
    )
    newer.status, newer.published_version = "COMPLETE", 2
    newer.completed_at = now
    session.add(newer)
    session.flush()
    assert dispatch(session, now) == 0
    assert old.status == "STALE" and old.published_version is None
    assert not session.exec(
        select(PendingDispatch).where(
            PendingDispatch.tenant_id == sync_seed.tenant_id,
            PendingDispatch.task_name.in_(
                ["ads.sync_step", "ads.targeted_step", "ads.publish_step"]
            ),
        )
    ).all()


def test_explicit_full_request_does_not_reuse_periodic_queue(
    session, sync_seed, monkeypatch
):
    now, plan = case(session, sync_seed, monkeypatch)
    job_tasks._plan_directory_step(session, plan, accounts=["acc-1"], now=now)
    periodic = [
        r
        for r in runs(session, sync_seed)
        if r.kind == "campaign" and r.ad_type == "REGULAR"
    ][0]
    periodic.next_attempt_at = now + timedelta(days=7)
    assert periodic.query[PERIODIC_KEY] is True
    assert (
        request(session, sync_seed, ad_types={"acc-1": {"REGULAR": ("campaign",)}}) == 1
    )
    explicit = [r for r in runs(session, sync_seed) if PERIODIC_KEY not in r.query][0]
    assert explicit.partition_key == periodic.partition_key
    assert (
        explicit.request_id != periodic.request_id and explicit.next_attempt_at is None
    )


@pytest.mark.parametrize("changed", ["route", "bc", "unpublished", "targeted"])
@pytest.mark.parametrize("ad_type", ["REGULAR", "SMART_PLUS"])
def test_unrelated_success_cannot_suppress_calibration(
    session, sync_seed, monkeypatch, changed, ad_type
):
    from app.modules.ads.directory_requests import _partition_key

    now, plan = case(session, sync_seed, monkeypatch)
    request(session, sync_seed, ad_types={"acc-1": {ad_type: ("campaign",)}})
    complete(session, sync_seed, now - timedelta(hours=1))
    old = runs(session, sync_seed)[0]
    if changed == "route":
        old.frozen_route = old.frozen_route | {"binding_revision": 1}
    elif changed == "bc":
        old.bc_id = "bc-b"
        old.frozen_route = old.frozen_route | {"bc_id": "bc-b"}
    elif changed == "unpublished":
        old.published_version = None
    else:
        old.query = old.query | {"ids": ["one"]}
        old.partition_key = _partition_key(old.query)
    session.flush()
    job_tasks._plan_directory_step(session, plan, accounts=["acc-1"], now=now)
    assert any(
        r.status == "QUEUED" and r.kind == "campaign" and r.ad_type == ad_type
        for r in runs(session, sync_seed)
    )


@pytest.mark.parametrize("periodic", [True, False])
def test_only_marked_calibration_can_be_deferred_at_dispatch(
    session, sync_seed, monkeypatch, periodic
):
    now, _ = case(session, sync_seed, monkeypatch, active=False)
    request(session, sync_seed, ad_types={"acc-1": {"REGULAR": ("campaign",)}})
    complete(session, sync_seed, now - timedelta(days=2))
    previous = runs(session, sync_seed)[0]
    pending = AdDirectoryRun(
        **previous.model_dump(exclude={"id", "request_id", "request_sequence"})
    )
    pending.status, pending.published_version, pending.completed_at = (
        "QUEUED",
        None,
        None,
    )
    if periodic:
        pending.query = pending.query | {PERIODIC_KEY: True}
    session.add(pending)
    session.flush()
    assert dispatch(session, now) == (0 if periodic else 1)
    assert pending.status == "QUEUED" and pending.published_version is None
    if periodic:
        assert pending.next_attempt_at == previous.completed_at + timedelta(days=7)
        assert dispatch(session, now + timedelta(minutes=1)) == 0
    else:
        assert pending.next_attempt_at is None


def test_deferred_periodic_run_does_not_hide_missing_layers_or_reactivation(
    session, sync_seed, monkeypatch
):
    now, plan = case(session, sync_seed, monkeypatch, active=False)
    request(session, sync_seed, ad_types={"acc-1": {"REGULAR": ("campaign",)}})
    complete(session, sync_seed, now - timedelta(days=2))
    previous = runs(session, sync_seed)[0]
    pending = AdDirectoryRun(
        **previous.model_dump(exclude={"id", "request_id", "request_sequence"})
    )
    pending.status, pending.published_version, pending.completed_at = (
        "QUEUED",
        None,
        None,
    )
    pending.query = pending.query | {PERIODIC_KEY: True}
    session.add(pending)
    session.flush()
    assert dispatch(session, now) == 0
    job_tasks._plan_directory_step(session, plan, accounts=["acc-1"], now=now)
    assert {
        r.kind
        for r in runs(session, sync_seed)
        if r.status == "QUEUED" and r.ad_type == "REGULAR"
    } == {"campaign", "adgroup", "ad"}
    # 模拟其它层完成，重新活跃时只复用并提前原周期运行，不生成同分区新副本。
    for row in runs(session, sync_seed):
        if row.id not in (pending.id, previous.id):
            row.status, row.published_version, row.completed_at = "COMPLETE", 3, now
    plan.last_active_advertiser_ids = ["acc-1"]
    plan.active_screened_at = now + timedelta(minutes=1)
    job_tasks._plan_directory_step(
        session, plan, accounts=["acc-1"], now=now + timedelta(minutes=1)
    )
    assert pending.next_attempt_at <= now + timedelta(minutes=1)
    assert (
        len(
            [
                r
                for r in runs(session, sync_seed)
                if r.status == "QUEUED"
                and r.kind == "campaign"
                and r.ad_type == "REGULAR"
            ]
        )
        == 1
    )


@pytest.mark.parametrize(
    "protected", ["lease", "page", "outbox", "continuation", "claim"]
)
def test_started_or_delivered_backlog_is_not_changed(
    session, sync_seed, monkeypatch, protected
):
    now, _ = case(session, sync_seed, monkeypatch)
    request(session, sync_seed, ad_types={"acc-1": {"REGULAR": ("campaign",)}})
    old = runs(session, sync_seed)[0]
    newer = AdDirectoryRun(
        **old.model_dump(exclude={"id", "request_id", "request_sequence"})
    )
    newer.status, newer.published_version = "COMPLETE", 2
    newer.completed_at = now
    if protected == "lease":
        old.claimed_until = now + timedelta(minutes=2)
    elif protected == "page":
        session.add(
            AdDirectoryPage(
                run_id=old.id,
                page=1,
                tenant_id=old.tenant_id,
                advertiser_id=old.advertiser_id,
                claim_generation=1,
                next_page=None,
                complete=True,
                items=[],
                materials=[],
            )
        )
    elif protected == "outbox":
        assert dispatch(session, now) == 1
    elif protected == "claim":
        old.claim_token = uuid4()
    else:
        old.next_page = 2
    session.add(newer)
    session.flush()
    assert not review_unstarted_directory(session, old, now)
    assert old.status == "QUEUED" and old.completed_at is None


def test_retired_full_run_keeps_successor_demand_dispatchable(
    session, sync_seed, monkeypatch
):
    from app.modules.sync_control.models import (
        SyncExecutionScope,
        SyncRequirement,
        SyncSchedule,
    )

    now, _ = case(session, sync_seed, monkeypatch)
    request(session, sync_seed, ad_types={"acc-1": {"REGULAR": ("campaign",)}})
    old = runs(session, sync_seed)[0]
    newer = AdDirectoryRun(
        **old.model_dump(exclude={"id", "request_id", "request_sequence"})
    )
    newer.status, newer.published_version, newer.completed_at = "COMPLETE", 2, now
    session.add(newer)
    session.flush()
    successor = AdDirectoryRun(
        **old.model_dump(exclude={"id", "request_id", "request_sequence"})
    )
    successor.query = old.query | {"ids": ["newly-required"]}
    successor.partition_key = "f" * 64
    successor.created_at = old.created_at + timedelta(seconds=1)
    session.add(successor)
    session.flush()
    original_generation = old.claim_generation
    assert dispatch(session, now) == 1
    assert old.status == "STALE" and old.claim_generation == original_generation
    child = session.exec(
        select(SyncExecutionScope).where(
            SyncExecutionScope.directory_run_id == successor.id
        )
    ).one()
    demand = session.get(SyncRequirement, child.requirement_id)
    assert demand.satisfied_revision < demand.desired_revision
    head = session.exec(
        select(SyncSchedule).where(SyncSchedule.scope_id == child.scope_id)
    ).one()
    assert head.active and head.lease_execution_id == successor.id
    actual = session.exec(
        select(PendingDispatch).where(
            PendingDispatch.task_name.in_(
                ["ads.sync_step", "ads.targeted_step", "ads.publish_step"]
            )
        )
    ).all()
    assert len(actual) == 1 and actual[0].payload["run_id"] == str(successor.id)
