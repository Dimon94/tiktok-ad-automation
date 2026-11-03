from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlmodel import select

from app.modules.sync_control.models import (
    SyncExecutionScope,
    SyncRequirement,
    SyncSchedule,
)
from tests.modules.sync_control.test_schedule_fencing import _directory


def test_only_current_fully_covered_unleased_directory_is_retired(session, sync_seed):
    from app.modules.sync_control.directory_retirement import retire_covered_directories

    run = _directory(session, sync_seed)
    ref = session.exec(select(SyncExecutionScope)).one()
    requirement = session.get(SyncRequirement, ref.requirement_id)
    head = session.exec(select(SyncSchedule)).one()
    now = datetime.now(UTC)
    session.flush()
    assert retire_covered_directories(session, now=now, cursor=None)[0] == 0
    requirement.satisfied_revision = requirement.desired_revision
    run.claimed_until = now + timedelta(minutes=1)
    session.flush()
    assert retire_covered_directories(session, now=now, cursor=None)[0] == 0
    run.claimed_until = None
    head.lease_execution_id, head.leased_until = run.id, now + timedelta(minutes=1)
    session.flush()
    assert retire_covered_directories(session, now=now, cursor=None)[0] == 0
    head.lease_execution_id = head.leased_until = None
    session.flush()
    assert retire_covered_directories(session, now=now, cursor=None)[0] == 1
    assert run.status == "CANCELLED" and run.error_code == "coverage_superseded"
    session.refresh(ref)
    assert ref.status == "SUPERSEDED"
    assert requirement.satisfied_revision == requirement.desired_revision
    assert head.oldest_pending_at is None and not head.active


def test_old_owner_and_uncovered_execution_not_retired(session, sync_seed):
    from app.modules.sync_control.directory_retirement import retire_covered_directories
    from app.modules.sync_control.models import SyncScope

    run = _directory(session, sync_seed)
    ref = session.exec(select(SyncExecutionScope)).one()
    requirement = session.get(SyncRequirement, ref.requirement_id)
    requirement.satisfied_revision = requirement.desired_revision
    scope = session.get(SyncScope, ref.scope_id)
    scope.owner_epoch += 1
    session.flush()
    assert (
        retire_covered_directories(session, now=datetime.now(UTC), cursor=None)[0] == 0
    )
    assert run.status == "QUEUED"


@pytest.mark.parametrize("target", ["run", "head", "reference"])
def test_retirement_skips_contended_rows_and_recovers(committed_sync_seed, target):
    from app.modules.sync_control.directory_retirement import retire_covered_directories

    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        run = _directory(db, seed)
        ref = db.exec(
            select(SyncExecutionScope).where(
                SyncExecutionScope.directory_run_id == run.id
            )
        ).one()
        requirement = db.get(SyncRequirement, ref.requirement_id)
        requirement.satisfied_revision = requirement.desired_revision
        identity, reference, scope = run.id, ref.id, ref.scope_id
    queries = {
        "run": ("SELECT id FROM ad_directory_run WHERE id=:id FOR UPDATE", identity),
        "head": (
            "SELECT scope_id FROM sync_schedule WHERE scope_id=:id FOR UPDATE",
            scope,
        ),
        "reference": (
            "SELECT id FROM sync_execution_scope WHERE id=:id FOR UPDATE",
            reference,
        ),
    }
    with factory() as blocker, blocker.begin():
        query, key = queries[target]
        blocker.execute(text(query), {"id": key}).all()
        with factory() as other, other.begin():
            other.execute(text("SET LOCAL lock_timeout='100ms'"))
            assert (
                retire_covered_directories(other, now=datetime.now(UTC), cursor=None)[0]
                == 0
            )
    with factory() as other, other.begin():
        assert (
            retire_covered_directories(other, now=datetime.now(UTC), cursor=None)[0]
            == 1
        )
