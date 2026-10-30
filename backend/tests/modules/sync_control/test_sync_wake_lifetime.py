"""纯同步唤醒必须接寿命；旧prefix仅压缩正文，不能丢弃幂等身份。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import select

from app.core.config import settings
from app.integrations.tiktok.contracts.ads import EntityRef
from app.jobs.models import PendingDispatch
from app.jobs.outbox import compact_published_dispatches, enqueue_after_commit
from app.modules.ads.directory_requests import (
    request_directory as request_directory_runs,
)
from app.modules.reporting import job_tasks, planner
from app.modules.reporting.health import observe_health
from app.modules.reporting.store_models import AdSyncBc
from app.modules.sync_control.dispatch_envelope import dispatch_envelope
from app.modules.sync_control.models import SyncRequirement
from app.modules.sync_control.retention import prune_sync_dispatches
from tests.modules.ads.test_directory_requests import _route


@pytest.mark.parametrize("producer", ["request", "immediate", "health"])
def test_real_sync_wake_producer_expires_compacts_and_prunes_without_losing_demand(
    session, sync_seed, monkeypatch, producer
):
    now = datetime.now(UTC)
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    if producer == "health":
        planner.ensure_bc_plans(session, now=now, tenant_id=sync_seed.tenant_id)
        plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
        plan.last_active_advertiser_ids = ["acc-2"]
        plan.active_screened_at = now
        observe_health(session, plan, now=now)
    else:
        assert (
            request_directory_runs(
                session,
                tenant_id=sync_seed.tenant_id,
                bc_id="bc-a",
                actor_id=sync_seed.actor_id,
                frozen_route=_route(sync_seed),
                advertiser_ids=["acc-1"],
                scope="targeted",
                refs=[EntityRef(sync_seed.tenant_id, "acc-1", "ad", "changed-ad")],
                ad_types={"acc-1": {"SMART_PLUS": ("ad",)}},
                dispatch_immediately=producer == "immediate",
                now=now,
            )
            == 1
        )
    row = session.exec(
        select(PendingDispatch).where(
            PendingDispatch.tenant_id == sync_seed.tenant_id,
            PendingDispatch.task_name == "reporting.plan_due",
        )
    ).one()
    assert row.dispatch_contract_version == 2
    assert row.task_key.startswith("sync-directory-wake:")
    assert row.payload == {"dispatch_only": True}
    assert row.valid_until - row.issued_at == timedelta(days=7)
    desired = {
        r.id: (r.desired_revision, r.satisfied_revision)
        for r in session.exec(
            select(SyncRequirement).where(
                SyncRequirement.tenant_id == sync_seed.tenant_id
            )
        ).all()
    }
    assert desired
    row.issued_at = now - timedelta(days=8)
    row.valid_until = row.issued_at + timedelta(days=7)
    session.flush()
    envelope, identity = dispatch_envelope(row), row.id
    monkeypatch.setattr(job_tasks, "engine", session.connection())
    monkeypatch.setattr(
        job_tasks,
        "_dispatch_pending",
        lambda *_a, **_kw: pytest.fail("expired wake dispatched work"),
    )
    args = {
        "tenant_id": str(sync_seed.tenant_id),
        "actor_id": str(sync_seed.actor_id),
        "payload": {"dispatch_only": True},
        "dispatch": envelope,
    }
    assert job_tasks.plan_due.run(**args) == "IGNORED"
    assert prune_sync_dispatches(session, now=now, limit=10)["retired"] == 1
    assert (
        prune_sync_dispatches(session, now=now + timedelta(days=7), limit=10)[
            "compacted"
        ]
        == 1
    )
    assert row.payload == {} and row.payload_digest
    assert (
        prune_sync_dispatches(session, now=now + timedelta(days=30), limit=10)[
            "deleted"
        ]
        == 1
    )
    session.expire_all()
    assert session.get(PendingDispatch, identity) is None
    assert job_tasks.plan_due.run(**args) == "IGNORED"
    assert {
        r.id: (r.desired_revision, r.satisfied_revision)
        for r in session.exec(
            select(SyncRequirement).where(
                SyncRequirement.tenant_id == sync_seed.tenant_id
            )
        ).all()
    } == desired


@pytest.mark.parametrize("prefix", ["directory-request-refill:", "sync-health:"])
@pytest.mark.parametrize("variant", ["exact", "false", "extra", "task", "unpublished"])
def test_legacy_pure_wake_prefix_compacts_only_exact_published_wake_and_keeps_dedup(
    session, sync_seed, prefix, variant
):
    payload = (
        {"dispatch_only": False} if variant == "false" else {"dispatch_only": True}
    )
    if variant == "extra":
        payload["repair"] = True
    name = "jobs.probe" if variant == "task" else "reporting.plan_due"
    key = prefix + str(uuid4())
    identity = enqueue_after_commit(
        session,
        context=sync_seed.context,
        task_name=name,
        task_key=key,
        payload=payload,
    )
    row = session.get(PendingDispatch, identity)
    if variant != "unpublished":
        row.published_at = datetime.now(UTC) - timedelta(days=8)
    session.flush()
    assert compact_published_dispatches(session, limit=10) == int(variant == "exact")
    assert row.payload == ({} if variant == "exact" else payload)
    assert row.dispatch_contract_version is None and row.valid_until is None
    assert (
        enqueue_after_commit(
            session,
            context=sync_seed.context,
            task_name=name,
            task_key=key,
            payload=payload,
        )
        == identity
    )
    prune_sync_dispatches(
        session, now=datetime.now(UTC) + timedelta(days=365), limit=10
    )
    assert session.get(PendingDispatch, identity) is not None
