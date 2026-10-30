from datetime import UTC, date, datetime, timedelta

from sqlmodel import select

from app.modules.reporting.jobs import enqueue_job
from app.modules.reporting.store_models import ReportSyncJob
from app.modules.sync_control.contracts import BucketKey, ExecutionFence
from app.modules.sync_control.models import (
    SyncExecutionScope,
    SyncRequirement,
    SyncScope,
)
from app.modules.sync_control.requirements import (
    merge_requirement,
    satisfy_requirements,
)
from app.modules.sync_control.scopes import ensure_scope, scope_key
from tests.modules.reporting.test_jobs import _route

DAY = date(2026, 10, 5)


def test_priority_and_batch_do_not_change_frozen_scope(sync_seed):
    args = (_route(sync_seed), sync_seed.actor_id, "acc-1", "ad_day")
    first = scope_key(
        *args, {"tier": "hot", "batch": 1}, "v2", timezone="Asia/Hong_Kong"
    )
    assert first == scope_key(
        *args, {"tier": "history", "batch": 5}, "v2", timezone="Asia/Hong_Kong"
    )
    assert first != scope_key(*args, {}, "v2", timezone="UTC")
    route = _route(sync_seed) | {"binding_revision": 1}
    assert first != scope_key(route, *args[1:], {}, "v2", timezone="Asia/Hong_Kong")


def test_cross_tier_requests_reuse_unstarted_execution(session, sync_seed):
    args = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "kind": "ad_day",
        "advertiser_ids": ["acc-1"],
        "start": DAY,
        "end": DAY,
        "frozen_route": _route(sync_seed),
        "actor_id": sync_seed.actor_id,
    }
    job_id = enqueue_job(session, tier="history", **args)
    assert enqueue_job(session, tier="hot", **args) is None
    assert session.get(ReportSyncJob, job_id).priority == 1
    assert (
        len(
            session.exec(
                select(SyncRequirement).where(
                    SyncRequirement.tenant_id == sync_seed.tenant_id
                )
            ).all()
        )
        == 1
    )


def test_background_request_cannot_demote_pending_current_day_refresh(
    session, sync_seed
):
    from zoneinfo import ZoneInfo

    from app.modules.sync_control.models import SyncSchedule

    today = datetime.now(UTC).astimezone(ZoneInfo("Asia/Hong_Kong")).date()
    args = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "kind": "ad_day",
        "advertiser_ids": ["acc-1"],
        "start": today,
        "end": today,
        "frozen_route": _route(sync_seed),
        "actor_id": sync_seed.actor_id,
    }
    identity = enqueue_job(session, tier="hot", **args)
    assert enqueue_job(session, tier="history", **args) is None
    requirement = session.exec(
        select(SyncRequirement).where(SyncRequirement.tenant_id == sync_seed.tenant_id)
    ).one()
    assert requirement.source == "realtime"
    head = session.exec(
        select(SyncSchedule).where(
            SyncSchedule.scope_id == requirement.scope_id,
            SyncSchedule.work_class == "realtime",
        )
    ).one()
    assert head.active and session.get(ReportSyncJob, identity).priority == 1


def test_owner_takeover_creates_new_execution_instead_of_reusing_old_unstarted_job(
    session, sync_seed, redis_client, monkeypatch
):
    from uuid import uuid4

    from app.core.config import settings
    from app.integrations.tiktok.platform_quotas import quota_configuration
    from app.modules.reporting import job_tasks  # noqa: F401
    from app.modules.sync_control.dispatcher import dispatch_due

    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", quota_configuration("BASIC"))
    monkeypatch.setattr(settings, "TIKTOK_APP_ID", f"epoch-{uuid4()}")
    args = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "kind": "ad_day",
        "tier": "history",
        "advertiser_ids": ["acc-1"],
        "start": DAY,
        "end": DAY,
        "frozen_route": _route(sync_seed),
        "actor_id": sync_seed.actor_id,
    }
    old_id = enqueue_job(session, **args)
    scope = session.exec(
        select(SyncScope).where(SyncScope.tenant_id == sync_seed.tenant_id)
    ).one()
    scope.owner_epoch = 2
    session.flush()
    new_id = enqueue_job(session, **args)
    assert new_id is not None and new_id != old_id
    assert session.get(ReportSyncJob, new_id).owner_epoch == 2
    assert session.get(ReportSyncJob, old_id).owner_epoch == 1
    assert (
        dispatch_due(
            session,
            redis_client,
            now=datetime.now(UTC) + timedelta(seconds=1),
            physical_slots=1,
        )
        == 1
    )
    assert session.get(ReportSyncJob, new_id).status == "RUNNING"
    assert session.get(ReportSyncJob, old_id).status == "QUEUED"


def test_publication_only_satisfies_frozen_revision(session, sync_seed):
    now = datetime.now(UTC)
    job_id = enqueue_job(
        session,
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        kind="ad_day",
        tier="hot",
        advertiser_ids=["acc-1"],
        start=DAY,
        end=DAY,
        frozen_route=_route(sync_seed),
        actor_id=sync_seed.actor_id,
        requested_at=now,
    )
    child = session.exec(
        select(SyncExecutionScope).where(SyncExecutionScope.report_job_id == job_id)
    ).one()
    child.observed_at = now
    scope = ensure_scope(
        session,
        route=_route(sync_seed),
        actor_id=sync_seed.actor_id,
        advertiser_id="acc-1",
        resource="ad_day",
        query={"campaign_ids": []},
        timezone="Asia/Hong_Kong",
    )
    revision = merge_requirement(
        session,
        scope,
        BucketKey(DAY.isoformat(), DAY),
        requested_after=now + timedelta(seconds=1),
        source="hot",
    )
    successor = satisfy_requirements(
        session,
        fence=ExecutionFence(job_id, "report", 0, 1),
        observed_at=now,
        publication_version=1,
    )
    requirement = session.get(SyncRequirement, revision.requirement_id)
    assert requirement.desired_revision == 2 and requirement.satisfied_revision == 1
    assert successor == [requirement.id]
    assert requirement.oldest_pending_at is not None


def test_periodic_maintenance_cannot_expand_a_started_frozen_link_query(
    committed_sync_seed, redis_client, monkeypatch
):
    from contextlib import nullcontext

    from app.core.config import settings
    from app.core.db import engine
    from app.integrations.tiktok.platform_quotas import quota_configuration
    from app.modules.reporting import job_tasks, planner
    from app.modules.reporting.store_models import AdSyncBc

    seed, factory = committed_sync_seed
    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", quota_configuration("BASIC"))
    monkeypatch.setattr(job_tasks, "_reader_for", lambda **_: nullcontext(None))
    now = datetime.now(UTC)
    common = {
        "tenant_id": seed.tenant_id,
        "bc_id": "bc-a",
        "kind": "material_links",
        "tier": "history",
        "advertiser_ids": ["acc-1"],
        "frozen_route": _route(seed),
        "actor_id": seed.actor_id,
        "due_at": now + timedelta(days=1),
    }
    with factory() as db, db.begin():
        planner.ensure_bc_plans(db, now=now)
        for plan in db.exec(
            select(AdSyncBc).where(AdSyncBc.tenant_id == seed.tenant_id)
        ).all():
            plan.enabled = False
        old = enqueue_job(db, start=DAY, end=DAY, **common)
        child = db.exec(
            select(SyncExecutionScope).where(SyncExecutionScope.report_job_id == old)
        ).one()
        child.observed_at = now
        job = db.get(ReportSyncJob, old)
        job.status = "WAITING"
        job.progress = job.progress | {
            "snapshot_observed_at": now.isoformat(),
            "validated_page": 3,
        }
        successor = enqueue_job(
            db, start=DAY + timedelta(days=1), end=DAY + timedelta(days=1), **common
        )
    job_tasks._run_planning_entry(
        database_engine=engine, redis_client=redis_client, now=now
    )
    with factory() as db:
        job = db.get(ReportSyncJob, old)
        assert (job.start_date, job.end_date) == (DAY, DAY)
        assert job.progress["validated_page"] == 3
        assert db.get(ReportSyncJob, successor).status == "QUEUED"
