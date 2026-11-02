"""旧批次只能被真实存在且覆盖完整的更新执行承接，不能伪造需求成功。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import select

from app.modules.reporting.store_models import ReportBalanceRun
from app.modules.sync_control.balance_demand import register_balance
from app.modules.sync_control.models import SyncExecutionScope, SyncRequirement
from tests.modules.reporting.test_jobs import _route


def batch(session, seed, accounts, at):
    run = ReportBalanceRun(
        tenant_id=seed.tenant_id,
        actor_id=seed.actor_id,
        frozen_route=_route(seed),
        run_key=str(uuid4()),
        remaining_advertiser_ids=accounts,
        created_at=at,
    )
    session.add(run)
    session.flush()
    register_balance(session, run)
    return run


def test_balance_supersession_preserves_unmet_revision_and_newer_execution(
    session, sync_seed
):
    from app.modules.sync_control.balance_compaction import supersede_balance_run

    now = datetime.now(UTC)
    old = batch(session, sync_seed, ["acc-1"], now - timedelta(hours=2))
    newer = batch(session, sync_seed, ["acc-1", "acc-2"], now - timedelta(hours=1))
    assert supersede_balance_run(session, run_id=old.id, now=now)
    session.refresh(old)
    assert old.status == "CANCELLED" and old.error_code == "balance_superseded"
    assert old.remaining_advertiser_ids == ["acc-1"]
    assert newer.status == "ACTIVE"
    assert all(
        r.satisfied_revision == 0 for r in session.exec(select(SyncRequirement)).all()
    )
    assert (
        session.exec(
            select(SyncExecutionScope).where(
                SyncExecutionScope.balance_run_id == old.id
            )
        )
        .one()
        .status
        == "SUPERSEDED"
    )
    assert all(
        c.status == "PENDING"
        for c in session.exec(
            select(SyncExecutionScope).where(
                SyncExecutionScope.balance_run_id == newer.id
            )
        ).all()
    )
    assert not supersede_balance_run(session, run_id=old.id, now=now)


@pytest.mark.parametrize(
    "case", ["inflight", "incomplete_accounts", "route", "revision", "owner"]
)
def test_balance_supersession_rejects_unproven_replacement(session, sync_seed, case):
    from app.modules.sync_control.balance_compaction import supersede_balance_run
    from app.modules.sync_control.models import SyncScope

    now = datetime.now(UTC)
    old = batch(session, sync_seed, ["acc-1", "acc-2"], now - timedelta(hours=2))
    newer = batch(
        session,
        sync_seed,
        ["acc-1"] if case == "incomplete_accounts" else ["acc-1", "acc-2"],
        now - timedelta(hours=1),
    )
    if case == "inflight":
        old.claimed_until = now + timedelta(seconds=20)
        old.claim_token = uuid4()
    elif case == "route":
        newer.frozen_route = dict(newer.frozen_route, bc_id="another-bc")
    elif case == "revision":
        child = session.exec(
            select(SyncExecutionScope).where(
                SyncExecutionScope.balance_run_id == newer.id
            )
        ).first()
        child.frozen_requested_after = now - timedelta(days=1)
    elif case == "owner":
        scope = session.exec(select(SyncScope)).first()
        scope.owner_epoch += 1
    session.flush()
    assert not supersede_balance_run(session, run_id=old.id, now=now)
    assert old.status == "ACTIVE"
    assert all(
        r.satisfied_revision == 0 for r in session.exec(select(SyncRequirement)).all()
    )


def test_compaction_skips_locked_run_and_preserves_successor_lease(committed_sync_seed):
    from app.modules.sync_control.balance_compaction import supersede_balance_run
    from app.modules.sync_control.models import SyncSchedule

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session:
        old = batch(session, seed, ["acc-1"], now - timedelta(hours=2))
        newer = batch(session, seed, ["acc-1"], now - timedelta(hours=1))
        old_id, newer_id = old.id, newer.id
        newer.claim_generation = 1
        newer.claimed_until = now + timedelta(seconds=90)
        child = session.exec(
            select(SyncExecutionScope).where(
                SyncExecutionScope.balance_run_id == newer_id
            )
        ).one()
        child.execution_generation = 1
        head = session.get(SyncSchedule, (child.scope_id, "maintenance"))
        head.lease_execution_id, head.lease_execution_generation = newer_id, 1
        head.lease_token, head.leased_until = uuid4(), newer.claimed_until
        token = head.lease_token
        session.commit()
    with factory() as owner, factory() as cleanup:
        owner.get(ReportBalanceRun, old_id, with_for_update=True)
        assert not supersede_balance_run(cleanup, run_id=old_id, now=now)
        cleanup.rollback()
    with factory() as cleanup:
        assert supersede_balance_run(cleanup, run_id=old_id, now=now)
        cleanup.commit()
        head = cleanup.exec(
            select(SyncSchedule).where(SyncSchedule.tenant_id == seed.tenant_id)
        ).one()
        assert head.lease_token == token
        assert head.lease_execution_id == newer_id
