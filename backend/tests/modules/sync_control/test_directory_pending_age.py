from datetime import UTC, datetime, timedelta

from sqlmodel import select

from app.modules.sync_control.directory_candidates import _candidate
from app.modules.sync_control.directory_pending_age import repair_directory_pending_ages
from app.modules.sync_control.models import (
    SyncExecutionScope,
    SyncRequirement,
    SyncSchedule,
    SyncScope,
)
from tests.modules.sync_control.test_schedule_fencing import _directory


def test_age_and_candidate_ignore_covered_frozen_revision(session, sync_seed):
    old = _directory(
        session, sync_seed, created_at=datetime.now(UTC) - timedelta(days=7)
    )
    new = _directory(session, sync_seed)
    old_ref = session.exec(
        select(SyncExecutionScope).where(SyncExecutionScope.directory_run_id == old.id)
    ).one()
    new_ref = session.exec(
        select(SyncExecutionScope).where(SyncExecutionScope.directory_run_id == new.id)
    ).one()
    requirement = session.get(SyncRequirement, old_ref.requirement_id)
    requirement.desired_revision, requirement.satisfied_revision = 2, 1
    new_ref.requirement_id, new_ref.frozen_revision = requirement.id, 2
    session.flush()
    assert repair_directory_pending_ages(session).changed == 1
    head = session.exec(select(SyncSchedule)).one()
    assert head.oldest_pending_at == new.created_at
    found = _candidate(
        session,
        tenant_id=sync_seed.tenant_id,
        work_class="directory",
        now=datetime.now(UTC) + timedelta(seconds=1),
        independent_only=False,
    )
    assert found[1].id == new.id


def test_age_ignores_old_owner_but_includes_future_unmet_work(session, sync_seed):
    old = _directory(
        session, sync_seed, created_at=datetime.now(UTC) - timedelta(days=7)
    )
    scope = session.exec(select(SyncScope)).one()
    scope.owner_epoch += 1
    new = _directory(session, sync_seed, new_execution=True)
    new.next_attempt_at = datetime.now(UTC) + timedelta(days=1)
    session.flush()
    repair_directory_pending_ages(session)
    head = session.exec(select(SyncSchedule)).one()
    assert (
        head.oldest_pending_at == new.created_at and old.owner_epoch != new.owner_epoch
    )


def test_age_repair_is_bounded_keyset_and_idempotent(session, sync_seed):
    for account in ("acc-1", "acc-2"):
        _directory(session, sync_seed, advertiser=account)
    heads = session.exec(select(SyncSchedule)).all()
    for head in heads:
        head.oldest_pending_at = datetime.now(UTC) - timedelta(days=30)
    session.flush()
    first = repair_directory_pending_ages(session, limit=1)
    assert (first.scanned, first.changed) == (1, 1)
    second = repair_directory_pending_ages(session, after=first.after, limit=1)
    assert second.scanned == second.changed == 1 and second.after > first.after
    assert (
        repair_directory_pending_ages(session, after=second.after, limit=1).scanned == 0
    )
    assert repair_directory_pending_ages(session).changed == 0


def test_age_repair_skips_locked_head_and_wrap_repairs_it(committed_sync_seed):
    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        _directory(db, seed)
        head = db.exec(select(SyncSchedule)).one()
        head.oldest_pending_at = datetime.now(UTC) - timedelta(days=30)
        identity = head.scope_id
    with factory() as blocker, blocker.begin():
        blocker.exec(
            select(SyncSchedule)
            .where(SyncSchedule.scope_id == identity)
            .with_for_update()
        ).one()
        with factory() as other, other.begin():
            assert repair_directory_pending_ages(other).scanned == 0
    with factory() as other, other.begin():
        assert repair_directory_pending_ages(other).changed == 1
