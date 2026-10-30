"""V2消息寿命使用真实PG/Redis，外部发送必须在过期入口之前停止。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import select

from app.core.errors import DomainError
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.modules.ads import tasks
from app.modules.sync_control.directory_dispatch import dispatch_directories_due
from app.modules.sync_control.models import SyncSchedule
from tests.modules.sync_control.test_schedule_fencing import _directory


def _claimed(session, sync_seed, redis_client):
    run = _directory(session, sync_seed)
    assert (
        dispatch_directories_due(session, redis_client, now=datetime.now(UTC), limit=1)
        == 1
    )
    row = session.exec(
        select(PendingDispatch).where(
            PendingDispatch.tenant_id == run.tenant_id,
            PendingDispatch.payload["run_id"].astext == str(run.id),
        )
    ).one()
    return run, row


def test_canonical_dispatch_freezes_envelope_and_reenqueue_does_not_extend(
    session, sync_seed, redis_client
):
    from app.modules.sync_control.dispatch_envelope import enqueue_sync_dispatch

    run, row = _claimed(session, sync_seed, redis_client)
    assert row.dispatch_contract_version == 2
    assert row.valid_until - row.issued_at == timedelta(days=7)
    frozen = row.issued_at, row.valid_until
    assert (
        enqueue_sync_dispatch(
            session,
            context=sync_seed.context,
            task_name=row.task_name,
            task_key=row.task_key,
            payload=row.payload,
            execution=run,
        )
        == row.id
    )
    assert (row.issued_at, row.valid_until) == frozen


def test_expired_broker_message_is_ignored_and_demand_reclaims_new_generation(
    session, sync_seed, redis_client, monkeypatch
):
    from app.modules.sync_control.dispatch_envelope import dispatch_envelope

    run, row = _claimed(session, sync_seed, redis_client)
    old_generation = run.claim_generation
    row.issued_at = datetime.now(UTC) - timedelta(days=8)
    row.valid_until = row.issued_at + timedelta(days=7)
    session.flush()
    monkeypatch.setattr(tasks, "engine", session.connection())
    monkeypatch.setattr(
        tasks,
        "run_directory_step",
        lambda **_: pytest.fail("expired message entered business execution"),
    )
    assert (
        tasks.sync_step.run(
            tenant_id=str(run.tenant_id),
            actor_id=str(run.actor_id),
            payload=row.payload,
            dispatch=dispatch_envelope(row),
        )
        == "IGNORED"
    )
    session.refresh(run)
    assert run.status not in {"FAILED", "CANCELLED", "COMPLETE"}
    heads = session.exec(
        select(SyncSchedule).where(SyncSchedule.lease_execution_id == run.id)
    ).all()
    for head in heads:
        head.leased_until = datetime.now(UTC) - timedelta(seconds=1)
    session.flush()
    assert (
        dispatch_directories_due(session, redis_client, now=datetime.now(UTC), limit=1)
        == 1
    )
    assert run.claim_generation > old_generation
    rows = session.exec(
        select(PendingDispatch).where(PendingDispatch.tenant_id == run.tenant_id)
    ).all()
    assert len(rows) == 2 and max(item.valid_until for item in rows) > row.valid_until


def test_retired_payload_then_summary_purge_cannot_resurrect_old_claim(
    session, sync_seed, redis_client, monkeypatch
):
    from app.modules.sync_control.dispatch_envelope import (
        dispatch_envelope,
        enqueue_sync_dispatch,
    )
    from app.modules.sync_control.retention import prune_sync_dispatches

    run, row = _claimed(session, sync_seed, redis_client)
    identity, payload, key, name = (
        row.id,
        dict(row.payload),
        row.task_key,
        row.task_name,
    )
    now = datetime.now(UTC)
    row.issued_at = now - timedelta(days=40)
    row.valid_until = now - timedelta(days=33)
    row.retired_at = now - timedelta(days=8)
    envelope = dispatch_envelope(row)
    run.status = "COMPLETE"
    run.claim_token = run.claimed_until = None
    from app.modules.sync_control.directory_demand import directory_outcome

    directory_outcome(session, run)
    session.flush()
    assert prune_sync_dispatches(session, now=now, limit=10)["compacted"] == 1
    assert row.payload == {} and row.payload_digest
    row.retired_at = now - timedelta(days=31)
    session.flush()
    assert prune_sync_dispatches(session, now=now, limit=10)["deleted"] == 1
    session.expire_all()
    assert session.get(PendingDispatch, identity) is None
    with pytest.raises(DomainError):
        enqueue_sync_dispatch(
            session,
            context=sync_seed.context,
            task_name=name,
            task_key=key,
            payload=payload,
            execution=run,
        )
    with pytest.raises(DomainError):
        enqueue_after_commit(
            session,
            context=sync_seed.context,
            task_name=name,
            task_key=key,
            payload=payload,
        )
    monkeypatch.setattr(tasks, "engine", session.connection())
    monkeypatch.setattr(
        tasks,
        "run_directory_step",
        lambda **_: pytest.fail("deleted message revived execution"),
    )
    assert (
        tasks.sync_step.run(
            tenant_id=str(run.tenant_id),
            actor_id=str(run.actor_id),
            payload=payload,
            dispatch=envelope,
        )
        == "IGNORED"
    )


def test_legacy_and_write_messages_do_not_receive_ttl(session, sync_seed):
    from app.modules.sync_control.retention import prune_sync_dispatches

    identity = enqueue_after_commit(
        session,
        context=sync_seed.context,
        task_name="ad_management.execute",
        task_key=str(uuid4()),
        payload={"run_id": str(uuid4())},
    )
    row = session.get(PendingDispatch, identity)
    row.published_at = datetime.now(UTC) - timedelta(days=100)
    session.flush()
    assert row.dispatch_contract_version is None and row.valid_until is None
    prune_sync_dispatches(session, now=datetime.now(UTC), limit=10)
    assert session.get(PendingDispatch, identity).payload


@pytest.mark.parametrize("alter", ["tenant", "actor", "payload", "deadline", "missing"])
def test_envelope_is_bound_and_cannot_extend_or_strip_lifetime(
    session, sync_seed, redis_client, alter
):
    from app.modules.sync_control.dispatch_envelope import (
        consume_sync_dispatch,
        dispatch_envelope,
    )

    run, row = _claimed(session, sync_seed, redis_client)
    args = {
        "database_engine": session.connection(),
        "task_name": row.task_name,
        "tenant_id": str(run.tenant_id),
        "actor_id": str(run.actor_id),
        "payload": dict(row.payload),
        "dispatch": dispatch_envelope(row),
    }
    assert consume_sync_dispatch(**args)
    if alter in {"tenant", "actor"}:
        args[alter + "_id"] = str(uuid4())
    elif alter == "payload":
        args["payload"]["claim_generation"] += 1
    elif alter == "deadline":
        args["dispatch"]["valid_until"] = (
            row.valid_until + timedelta(days=7)
        ).isoformat()
    else:
        args["dispatch"] = None
    assert not consume_sync_dispatch(**args)


def test_expiry_waits_for_same_generation_worker_and_preserves_new_owner(
    session, sync_seed, redis_client
):
    from app.modules.sync_control.retention import prune_sync_dispatches

    run, row = _claimed(session, sync_seed, redis_client)
    now = datetime.now(UTC)
    row.issued_at, row.valid_until = now - timedelta(days=8), now - timedelta(days=1)
    run.claim_token, run.claimed_until = uuid4(), now + timedelta(seconds=90)
    session.flush()
    assert prune_sync_dispatches(session, now=now, limit=10)["retired"] == 0
    assert row.retired_at is None
    run.claim_generation += 1
    token = run.claim_token
    session.flush()
    assert prune_sync_dispatches(session, now=now, limit=10)["retired"] == 1
    assert run.claim_token == token and run.claimed_until > now


@pytest.mark.parametrize(
    "days,compacted,deleted", [(6, 0, 0), (7, 1, 0), (29, 1, 0), (30, 1, 1)]
)
def test_retirement_seven_thirty_day_boundaries(
    session, sync_seed, redis_client, days, compacted, deleted
):
    from app.modules.sync_control.retention import prune_sync_dispatches

    run, row = _claimed(session, sync_seed, redis_client)
    now = datetime.now(UTC)
    row.issued_at = now - timedelta(days=50)
    row.valid_until = now - timedelta(days=43)
    row.retired_at = now - timedelta(days=days)
    session.flush()
    result = prune_sync_dispatches(session, now=now, limit=10)
    assert (result["compacted"], result["deleted"]) == (compacted, deleted)


def test_outbox_sends_bound_envelope_but_never_expired_message(
    session, sync_seed, redis_client, monkeypatch
):
    from app.jobs import outbox
    from app.modules.sync_control.dispatch_envelope import (
        dispatch_envelope,
        enqueue_sync_dispatch,
    )

    run, row = _claimed(session, sync_seed, redis_client)
    sent = []
    monkeypatch.setattr(outbox, "engine", session.connection())
    monkeypatch.setattr(
        outbox.celery_app, "send_task", lambda *a, **kw: sent.append((a, kw))
    )
    assert outbox.flush_dispatch() == 1
    assert sent[0][1]["kwargs"]["payload"] == row.payload
    assert sent[0][1]["kwargs"]["dispatch"] == dispatch_envelope(row)
    assert sent[0][1]["expires"] == row.valid_until
    wake_id = enqueue_sync_dispatch(
        session,
        context=sync_seed.context,
        task_name="reporting.plan_due",
        task_key="sync-directory-wake:" + str(uuid4()),
        payload={"dispatch_only": True},
    )
    wake = session.get(PendingDispatch, wake_id)
    wake.issued_at = datetime.now(UTC) - timedelta(days=8)
    wake.valid_until = wake.issued_at + timedelta(days=7)
    session.flush()
    assert outbox.flush_dispatch() == 0 and len(sent) == 1
    session.refresh(wake)
    assert wake.published_at is None


def test_report_private_worker_token_does_not_make_message_retirable(
    session, sync_seed
):
    from app.modules.sync_control.retention import prune_sync_dispatches
    from tests.modules.sync_control.test_retention import seed_dispatch

    job, row, *_ = seed_dispatch(session, sync_seed, status="RUNNING")
    now = datetime.now(UTC)
    row.dispatch_contract_version = 2
    row.issued_at, row.valid_until = now - timedelta(days=8), now - timedelta(days=1)
    job.claim_token = uuid4()  # worker消费queued token后已换成本代私有token。
    job.claimed_until = now + timedelta(seconds=90)
    session.flush()
    assert prune_sync_dispatches(session, now=now, limit=10)["retired"] == 0
    assert row.retired_at is None
    assert (
        prune_sync_dispatches(session, now=now + timedelta(seconds=91), limit=10)[
            "retired"
        ]
        == 1
    )


@pytest.mark.parametrize(
    "module,name",
    [
        ("app.modules.ads.tasks", "sync_step"),
        ("app.modules.ads.tasks", "targeted_step"),
        ("app.modules.ads.tasks", "publish_step"),
        ("app.modules.reporting.job_tasks", "job_step"),
        ("app.modules.reporting.job_tasks", "backfill_step"),
        ("app.modules.reporting.balance_tasks", "balance_step"),
    ],
)
def test_all_registered_sync_consumers_reject_missing_dispatch_before_business(
    session, sync_seed, monkeypatch, module, name
):
    from importlib import import_module

    module = import_module(module)
    monkeypatch.setattr(module, "engine", session.connection())
    for execute in ("run_directory_step", "run_job_step", "run_balance"):
        if hasattr(module, execute):
            monkeypatch.setattr(
                module,
                execute,
                lambda **_: pytest.fail("missing dispatch entered business"),
            )
    now = datetime.now(UTC)
    assert (
        getattr(module, name).run(
            tenant_id=str(sync_seed.tenant_id),
            actor_id=str(sync_seed.actor_id),
            payload={},
            dispatch={
                "id": str(uuid4()),
                "version": 2,
                "issued_at": now.isoformat(),
                "valid_until": (now + timedelta(days=7)).isoformat(),
            },
        )
        == "IGNORED"
    )


def test_retirement_cannot_cross_a_concurrent_worker_claim(
    committed_sync_seed, redis_client
):
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from app.modules.ads.sync_models import AdDirectoryRun
    from app.modules.sync_control.retention import prune_sync_dispatches

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as db, db.begin():
        run, row = _claimed(db, seed, redis_client)
        identity, dispatch_id = run.id, row.id
        row.issued_at, row.valid_until = (
            now - timedelta(days=8),
            now - timedelta(days=1),
        )
    with factory() as owner, owner.begin():
        run = owner.get(AdDirectoryRun, identity, with_for_update=True)
        token = uuid4()
        run.claim_token, run.claimed_until = token, now + timedelta(seconds=90)
        owner.flush()

        def prune():
            with factory() as contender, contender.begin():
                contender.execute(text("SET LOCAL lock_timeout='100ms'"))
                return prune_sync_dispatches(contender, now=now, limit=10)

        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(prune)
            with pytest.raises(DBAPIError) as error:
                future.result(timeout=5)
            assert error.value.orig.sqlstate == "55P03"
    with factory() as db, db.begin():
        assert prune_sync_dispatches(db, now=now, limit=10)["retired"] == 0
        assert db.get(PendingDispatch, dispatch_id).retired_at is None
        assert db.get(AdDirectoryRun, identity).claim_token == token


def test_accelerated_day_recycles_mature_summaries_without_net_row_growth(
    session, sync_seed
):
    """24小时加速钟：真实outbox每小时新增一条且回收一条成熟摘要，不碰legacy。"""
    from sqlalchemy import func

    from app.modules.sync_control.dispatch_envelope import enqueue_sync_dispatch
    from app.modules.sync_control.retention import prune_sync_dispatches

    start = datetime.now(UTC)
    for hour in range(24):
        issued = start - timedelta(days=37) + timedelta(hours=hour)
        identity = enqueue_sync_dispatch(
            session,
            context=sync_seed.context,
            task_name="reporting.plan_due",
            task_key=f"sync-directory-wake:old:{hour}",
            payload={"dispatch_only": True},
            now=issued,
        )
        row = session.get(PendingDispatch, identity)
        row.retired_at = issued + timedelta(days=7)
        row.compacted_at = row.retired_at + timedelta(days=7)
        row.payload = {}
    session.flush()
    deleted = 0
    for hour in range(24):
        now = start + timedelta(hours=hour)
        enqueue_sync_dispatch(
            session,
            context=sync_seed.context,
            task_name="reporting.plan_due",
            task_key=f"sync-directory-wake:new:{hour}",
            payload={"dispatch_only": True},
            now=now,
        )
        result = prune_sync_dispatches(session, now=now, limit=100)
        deleted += result["deleted"]
        assert (
            session.exec(
                select(func.count())
                .select_from(PendingDispatch)
                .where(PendingDispatch.tenant_id == sync_seed.tenant_id)
            ).one()
            == 24
        )
    assert deleted == 24
    remaining = session.exec(
        select(PendingDispatch).where(PendingDispatch.tenant_id == sync_seed.tenant_id)
    ).all()
    assert all(
        row.payload == {"dispatch_only": True}
        and row.valid_until > start + timedelta(hours=24)
        for row in remaining
    )
