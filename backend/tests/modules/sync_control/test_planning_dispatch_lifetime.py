"""规划续点保留原30秒期限，并复用V2退休后7/30天回收。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import select

from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.models import PendingDispatch
from app.jobs.outbox import (
    _payload_digest,
    compact_published_dispatches,
    enqueue_after_commit,
)
from app.modules.reporting import job_tasks
from app.modules.sync_control import dispatch_envelope as contract
from app.modules.sync_control.retention import prune_sync_dispatches
from tests.modules.reporting.test_planning_continuation import (
    planning_keys as planning_keys,
)
from tests.modules.reporting.test_planning_opportunities import _prepared


def test_real_planning_producer_preserves_thirty_second_deadline_and_recycles(
    committed_sync_seed, redis_client, planning_keys, monkeypatch
):
    seed, factory = committed_sync_seed
    assert planning_keys[1] == job_tasks.PLANNING_CONTINUATION
    now = datetime.now(UTC)
    with factory() as db, db.begin():
        _prepared(db, seed, now, "directory")
    job_tasks._schedule_planning_continuation(engine, redis_client, now, now)
    with factory() as db:
        row = db.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.tenant_id,
                PendingDispatch.task_name == "reporting.plan_continue",
            )
        ).one()
        assert row.dispatch_contract_version == 2
        assert (
            row.valid_until
            == now + timedelta(seconds=30)
            == datetime.fromisoformat(row.payload["expires_at"])
        )
        assert row.available_at == now + timedelta(seconds=15)
        assert timedelta(0) < row.valid_until - row.issued_at <= timedelta(seconds=30)
        envelope, payload, identity = (
            contract.dispatch_envelope(row),
            dict(row.payload),
            row.id,
        )
        expired = row.valid_until + timedelta(seconds=1)

    class ExpiredClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return expired

    monkeypatch.setattr(contract, "datetime", ExpiredClock)
    monkeypatch.setattr(
        job_tasks,
        "plan_due_runs",
        lambda **_: pytest.fail("expired planning message entered work"),
    )
    assert (
        job_tasks.plan_continue.run(
            tenant_id=str(seed.tenant_id),
            actor_id=str(seed.actor_id),
            payload=payload,
            dispatch=envelope,
        )
        == "IGNORED"
    )
    with factory() as db, db.begin():
        assert prune_sync_dispatches(db, now=expired, limit=10)["retired"] == 1
        assert (
            prune_sync_dispatches(db, now=expired + timedelta(days=7), limit=10)[
                "compacted"
            ]
            == 1
        )
        assert (
            prune_sync_dispatches(db, now=expired + timedelta(days=30), limit=10)[
                "deleted"
            ]
            == 1
        )
    with factory() as db:
        assert db.get(PendingDispatch, identity) is None
    assert (
        job_tasks.plan_continue.run(
            tenant_id=str(seed.tenant_id),
            actor_id=str(seed.actor_id),
            payload=payload,
            dispatch=envelope,
        )
        == "IGNORED"
    )


def test_planning_deadline_is_immutable_and_generic_replay_cannot_extend_it(
    session, sync_seed
):
    now, identity = datetime.now(UTC), str(uuid4())
    payload = {
        "continuation_id": identity,
        "expires_at": (now + timedelta(seconds=30)).isoformat(),
    }
    args = {
        "session": session,
        "context": sync_seed.context,
        "task_name": "reporting.plan_continue",
        "task_key": f"report-planning-continue:{identity}",
        "payload": payload,
        "now": now,
    }
    key = contract.enqueue_sync_dispatch(**args)
    assert (
        contract.enqueue_sync_dispatch(**{**args, "now": now + timedelta(days=1)})
        == key
    )
    with pytest.raises(DomainError):
        contract.enqueue_sync_dispatch(
            **{
                **args,
                "payload": {
                    **payload,
                    "expires_at": (now + timedelta(days=1)).isoformat(),
                },
            }
        )
    row = session.get(PendingDispatch, key)
    assert row.valid_until == now + timedelta(seconds=30)


@pytest.mark.parametrize("invalid", ["expired", "long", "naive", "identity", "extra"])
def test_invalid_or_expired_first_planning_signature_creates_no_message(
    session, sync_seed, invalid
):
    now, identity = datetime.now(UTC), str(uuid4())
    expiry = now + timedelta(seconds=30)
    if invalid == "expired":
        expiry = now
    if invalid == "long":
        expiry = now + timedelta(seconds=31)
    if invalid == "naive":
        expiry = expiry.replace(tzinfo=None)
    payload = {"continuation_id": identity, "expires_at": expiry.isoformat()}
    if invalid == "identity":
        payload["continuation_id"] = str(uuid4())
    if invalid == "extra":
        payload["dispatch_only"] = True
    with pytest.raises(DomainError):
        contract.enqueue_sync_dispatch(
            session,
            context=sync_seed.context,
            task_name="reporting.plan_continue",
            task_key=f"report-planning-continue:{identity}",
            payload=payload,
            now=now,
        )
    assert not session.exec(
        select(PendingDispatch).where(PendingDispatch.tenant_id == sync_seed.tenant_id)
    ).all()


@pytest.mark.parametrize("invalid", [False, True])
def test_legacy_expired_planning_body_compacts_but_dedup_remains(
    session, sync_seed, invalid
):
    identity = str(uuid4())
    expiry = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    payload = {
        "continuation_id": identity,
        "expires_at": "not-a-date" if invalid else expiry,
    }
    key = f"report-planning-continue:{identity}"
    identifier = enqueue_after_commit(
        session,
        context=sync_seed.context,
        task_name="reporting.plan_continue",
        task_key=key,
        payload=payload,
    )
    row = session.get(PendingDispatch, identifier)
    row.published_at = datetime.now(UTC) - timedelta(days=8)
    session.flush()
    assert compact_published_dispatches(session, limit=10) == int(not invalid)
    assert row.payload == (payload if invalid else {})
    assert row.payload_digest == _payload_digest(payload)
    assert (
        enqueue_after_commit(
            session,
            context=sync_seed.context,
            task_name="reporting.plan_continue",
            task_key=key,
            payload=payload,
        )
        == identifier
    )


def test_producer_uses_same_clock_for_claim_and_original_deadline(
    committed_sync_seed, redis_client, planning_keys, monkeypatch
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC) + timedelta(days=10)

    class PlanningClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(job_tasks, "datetime", PlanningClock)
    with factory() as db, db.begin():
        _prepared(db, seed, now, "hot")
    job_tasks._schedule_planning_continuation(engine, redis_client, now, now)
    assert redis_client.exists(planning_keys[1])
    with factory() as db:
        row = db.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.tenant_id,
                PendingDispatch.task_name == "reporting.plan_continue",
            )
        ).one()
        assert row.issued_at == now and row.valid_until == now + timedelta(seconds=30)


def test_valid_control_envelope_keeps_real_redis_single_consumption(
    session, sync_seed, redis_client, planning_keys, monkeypatch
):
    from contextlib import nullcontext

    now, identity = datetime.now(UTC), str(uuid4())
    payload = {
        "continuation_id": identity,
        "expires_at": (now + timedelta(seconds=30)).isoformat(),
    }
    identifier = contract.enqueue_sync_dispatch(
        session,
        context=sync_seed.context,
        task_name="reporting.plan_continue",
        task_key=f"report-planning-continue:{identity}",
        payload=payload,
        now=now,
    )
    row = session.get(PendingDispatch, identifier)
    redis_client.set(planning_keys[1], identity, ex=60)
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(job_tasks, "engine", session.connection())
    monkeypatch.setattr(
        job_tasks.Redis, "from_url", lambda *_a, **_k: nullcontext(redis_client)
    )
    entered = []

    def entry(**kwargs):
        entered.append(kwargs["realtime_only"])
        return 4

    monkeypatch.setattr(job_tasks, "_run_planning_entry", entry)
    args = {
        "tenant_id": str(sync_seed.tenant_id),
        "actor_id": str(sync_seed.actor_id),
        "payload": payload,
        "dispatch": contract.dispatch_envelope(row),
    }
    assert job_tasks.plan_continue.run(**{**args, "dispatch": None}) == "IGNORED"
    assert job_tasks.plan_continue.run(**args) == 4
    assert job_tasks.plan_continue.run(**args) == 0
    assert entered == [True]


def test_synthetic_extra_bc_needs_binding_before_capacity_measurement(
    session, sync_seed
):
    from app.core.context import TenantContext
    from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
    from app.modules.accounts.connection_models import BCConnectionBinding
    from app.modules.accounts.models import TenantBC
    from app.modules.accounts.routing import verify_route
    from tests.modules.ads.test_directory_requests import _route

    session.add(TenantBC(tenant_id=sync_seed.tenant_id, bc_id="bc-c"))
    session.flush()
    route = FrozenTikTokRoute.model_validate({**_route(sync_seed), "bc_id": "bc-c"})
    arguments = {
        "context": TenantContext(sync_seed.tenant_id, sync_seed.actor_id, "operator"),
        "route": route,
        "advertiser_id": None,
        "capability": "read",
    }
    with pytest.raises(DomainError) as denied:
        verify_route(session, **arguments)
    assert denied.value.code == "connection_bc_mismatch"
    session.add(
        BCConnectionBinding(
            tenant_id=sync_seed.tenant_id,
            bc_id="bc-c",
            connection_id=sync_seed.connection.id,
            kind="OFFICIAL_API",
            status="ACTIVE",
            authorization_revision=0,
            revision=0,
        )
    )
    session.flush()
    verify_route(session, **arguments)
