from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlmodel import select

from app.modules.reporting.jobs import enqueue_job
from app.modules.reporting.store_models import ReportSyncJob
from app.modules.sync_control.models import (
    SyncExecutionScope,
    SyncRequirement,
    SyncSchedule,
    SyncScope,
)
from app.modules.sync_control.pending_report_compaction import supersede_pending_report
from tests.modules.reporting.test_jobs import _route


def pair(session, seed, *, days=2):
    now = datetime.now(UTC)
    old_id = enqueue_job(
        session,
        tenant_id=seed.tenant_id,
        actor_id=seed.actor_id,
        bc_id="bc-a",
        kind="material_links",
        tier="material",
        advertiser_ids=["acc-1"],
        start=now.date() - timedelta(days=days),
        end=now.date() - timedelta(days=1),
        frozen_route=_route(seed),
    )
    old = session.get(ReportSyncJob, old_id)
    old.created_at = now - timedelta(hours=2)
    old.status, old.claim_generation, old.request_count = "WAITING", 1, 1
    old.progress = {"requested_at": old.created_at.isoformat()}
    refs = session.exec(
        select(SyncExecutionScope).where(SyncExecutionScope.report_job_id == old_id)
    ).all()
    newer = ReportSyncJob.model_validate(
        old.model_dump()
        | {
            "id": uuid4(),
            "job_key": f"pending-test:{uuid4()}",
            "status": "QUEUED",
            "created_at": now - timedelta(hours=1),
            "claim_generation": 0,
            "request_count": 0,
            "progress": {"requested_at": now.isoformat()},
        }
    )
    session.add(newer)
    session.flush()
    for ref in refs:
        ref.execution_generation = 1
        ref.frozen_requested_after = now - timedelta(hours=2)
        requirement = session.get(SyncRequirement, ref.requirement_id)
        requirement.desired_revision += 1
        requested = ref.frozen_requested_after + timedelta(seconds=1)
        requirement.requested_after = requested
        session.add(
            SyncExecutionScope.model_validate(
                ref.model_dump()
                | {
                    "id": uuid4(),
                    "report_job_id": newer.id,
                    "execution_generation": 0,
                    "frozen_revision": requirement.desired_revision,
                    "frozen_requested_after": requested,
                }
            )
        )
    session.flush()
    return old, newer


def requirements(session):
    return {
        r.id: (r.desired_revision, r.satisfied_revision, r.requested_after)
        for r in session.exec(select(SyncRequirement)).all()
    }


def test_newer_pending_job_can_replace_old_without_satisfying_missing_data(
    session, sync_seed
):
    old, newer = pair(session, sync_seed)
    before = requirements(session)
    assert (
        supersede_pending_report(session, job_id=old.id, now=datetime.now(UTC))
        == newer.id
    )
    assert old.status == "CANCELLED" and old.error_code == "pending_superseded"
    assert newer.status == "QUEUED" and newer.claim_generation == 0
    old_refs = session.exec(
        select(SyncExecutionScope).where(SyncExecutionScope.report_job_id == old.id)
    ).all()
    new_refs = session.exec(
        select(SyncExecutionScope).where(SyncExecutionScope.report_job_id == newer.id)
    ).all()
    assert all(r.status == "SUPERSEDED" for r in old_refs)
    assert all(r.status == "PENDING" for r in new_refs)
    assert requirements(session) == before
    assert all(h.active for h in session.exec(select(SyncSchedule)).all())
    assert (
        supersede_pending_report(session, job_id=old.id, now=datetime.now(UTC)) is None
    )


@pytest.mark.parametrize(
    "change",
    [
        "missing_ref",
        "lower_revision",
        "older_freshness",
        "old_lease",
        "head_lease",
        "winner_started",
        "observed",
        "date",
        "route",
        "generation",
        "owner",
        "manifest_winner",
        "future_due",
        "progress",
        "actor",
        "bc",
        "filters",
    ],
)
def test_unsafe_replacements_are_skipped(session, sync_seed, change):
    old, newer = pair(session, sync_seed)
    refs = session.exec(
        select(SyncExecutionScope).where(SyncExecutionScope.report_job_id == newer.id)
    ).all()
    now = datetime.now(UTC)
    if change == "future_due":
        newer.due_at = now + timedelta(days=1)
    elif change == "progress":
        newer.progress = {**newer.progress, "locate_queries": {"cached": True}}
    elif change == "actor":
        newer.actor_id = uuid4()
    elif change == "bc":
        newer.bc_id = "bc-b"
    elif change == "filters":
        newer.campaign_ids = ["another-campaign"]
    elif change == "missing_ref":
        session.delete(refs[0])
    elif change == "lower_revision":
        refs[0].frozen_revision = 1
        old_ref = session.exec(
            select(SyncExecutionScope).where(
                SyncExecutionScope.report_job_id == old.id,
                SyncExecutionScope.requirement_id == refs[0].requirement_id,
            )
        ).one()
        old_ref.frozen_revision = 2
    elif change == "older_freshness":
        refs[0].frozen_requested_after -= timedelta(days=1)
    elif change == "old_lease":
        old.claimed_until = now + timedelta(minutes=1)
    elif change == "head_lease":
        head = session.exec(select(SyncSchedule)).one()
        head.lease_execution_id = old.id
        head.leased_until = now + timedelta(minutes=1)
    elif change == "winner_started":
        newer.status = "RUNNING"
        newer.claim_generation = 1
    elif change == "observed":
        refs[0].observed_at = now
    elif change == "date":
        newer.start_date -= timedelta(days=1)
    elif change == "route":
        newer.frozen_route = {**newer.frozen_route, "authorization_revision": 9}
    elif change == "generation":
        refs[0].execution_generation = 1
    elif change == "owner":
        session.get(SyncScope, refs[0].scope_id).owner_epoch += 1
    session.flush()
    before = requirements(session)
    assert (
        supersede_pending_report(
            session,
            job_id=old.id,
            now=now,
            replacement_id=uuid4() if change == "manifest_winner" else None,
        )
        is None
    )
    session.refresh(old)
    assert old.status == "WAITING" and requirements(session) == before


def test_two_old_jobs_converge_on_same_latest_job(session, sync_seed):
    old, newer = pair(session, sync_seed)
    second = ReportSyncJob.model_validate(
        old.model_dump()
        | {
            "id": uuid4(),
            "job_key": f"pending-test:{uuid4()}",
            "created_at": old.created_at + timedelta(minutes=5),
        }
    )
    session.add(second)
    session.flush()
    for ref in session.exec(
        select(SyncExecutionScope).where(SyncExecutionScope.report_job_id == old.id)
    ).all():
        session.add(
            SyncExecutionScope.model_validate(
                ref.model_dump() | {"id": uuid4(), "report_job_id": second.id}
            )
        )
    session.flush()
    before = requirements(session)
    for obsolete in [old, second]:
        assert (
            supersede_pending_report(session, job_id=obsolete.id, now=datetime.now(UTC))
            == newer.id
        )
    assert newer.status == "QUEUED" and requirements(session) == before


@pytest.mark.parametrize(
    "target", ["old", "newer", "scope", "reference", "requirement", "head"]
)
def test_contention_skips_complete_operation_and_recovers(committed_sync_seed, target):
    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        old, newer = pair(db, seed)
        ref = db.exec(
            select(SyncExecutionScope).where(SyncExecutionScope.report_job_id == old.id)
        ).first()
        old_id, new_id = old.id, newer.id
        queries = {
            "old": ("SELECT id FROM report_sync_job WHERE id=:id FOR UPDATE", old_id),
            "newer": ("SELECT id FROM report_sync_job WHERE id=:id FOR UPDATE", new_id),
            "scope": (
                "SELECT id FROM sync_scope WHERE id=:id FOR UPDATE",
                ref.scope_id,
            ),
            "reference": (
                "SELECT id FROM sync_execution_scope WHERE id=:id FOR UPDATE",
                ref.id,
            ),
            "requirement": (
                "SELECT id FROM sync_requirement WHERE id=:id FOR UPDATE",
                ref.requirement_id,
            ),
            "head": (
                "SELECT scope_id FROM sync_schedule WHERE scope_id=:id FOR UPDATE",
                ref.scope_id,
            ),
        }
    with factory() as blocker, blocker.begin():
        query, key = queries[target]
        blocker.execute(text(query), {"id": key}).all()
        with factory() as other, other.begin():
            assert (
                supersede_pending_report(other, job_id=old_id, now=datetime.now(UTC))
                is None
            )
            assert other.get(ReportSyncJob, old_id).status == "WAITING"
    with factory() as db, db.begin():
        assert (
            supersede_pending_report(db, job_id=old_id, now=datetime.now(UTC)) == new_id
        )


def test_caller_rollback_preserves_both_jobs_and_all_requirements(committed_sync_seed):
    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        old, newer = pair(db, seed)
        old_id, new_id = old.id, newer.id
        before = requirements(db)
    with factory() as db:
        assert (
            supersede_pending_report(db, job_id=old_id, now=datetime.now(UTC)) == new_id
        )
        db.rollback()
    with factory() as db:
        assert db.get(ReportSyncJob, old_id).status == "WAITING"
        assert db.get(ReportSyncJob, new_id).status == "QUEUED"
        assert requirements(db) == before


def test_replacement_can_be_normally_claimed_and_completed(committed_sync_seed):
    from app.modules.reporting.jobs import execute_job
    from tests.modules.reporting.claiming import claim_jobs

    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        old, newer = pair(db, seed)
        old_id, new_id = old.id, newer.id
        assert (
            supersede_pending_report(db, job_id=old_id, now=datetime.now(UTC)) == new_id
        )
    with factory() as db, db.begin():
        jobs = claim_jobs(db, now=datetime.now(UTC) + timedelta(seconds=1), limit=8)
        selected = next(j for j in jobs if j.id == new_id)
        token = selected.claim_token
    assert (
        execute_job(factory, job_id=new_id, claim_token=token, reader=object())
        == "COMPLETE"
    )
    with factory() as db:
        assert db.get(ReportSyncJob, old_id).status == "CANCELLED"
        assert db.get(ReportSyncJob, new_id).status == "COMPLETE"
        assert all(
            r.satisfied_revision == r.desired_revision
            for r in db.exec(select(SyncRequirement)).all()
        )


def test_discovery_finds_full_identity_duplicates_and_reports_truncation(
    session, sync_seed
):
    from app.modules.sync_control.pending_report_compaction import (
        discover_pending_report_pairs,
    )

    old, newer = pair(session, sync_seed)
    third = ReportSyncJob.model_validate(
        old.model_dump()
        | {
            "id": uuid4(),
            "job_key": f"pending-test:{uuid4()}",
            "created_at": old.created_at + timedelta(minutes=1),
        }
    )
    session.add(third)
    session.flush()
    pairs, truncated = discover_pending_report_pairs(session, limit=1)
    assert len(pairs) == 1 and truncated
    pairs, truncated = discover_pending_report_pairs(session, limit=10)
    assert set(pairs) == {(old.id, newer.id), (third.id, newer.id)} and not truncated


def test_latest_started_job_does_not_fall_back_to_earlier_unstarted_job(
    session, sync_seed
):
    old, newer = pair(session, sync_seed)
    started = ReportSyncJob.model_validate(
        newer.model_dump()
        | {
            "id": uuid4(),
            "job_key": f"pending-test:{uuid4()}",
            "created_at": newer.created_at + timedelta(minutes=1),
            "status": "RUNNING",
            "claim_generation": 1,
        }
    )
    session.add(started)
    session.flush()
    assert (
        supersede_pending_report(session, job_id=old.id, now=datetime.now(UTC)) is None
    )
    assert old.status == "WAITING"


def test_reference_limit_skips_entire_job(session, sync_seed, monkeypatch):
    from app.modules.sync_control import pending_report_compaction as module

    old, newer = pair(session, sync_seed)
    monkeypatch.setattr(module, "MAX_REFERENCES", 1)
    assert (
        module.supersede_pending_report(session, job_id=old.id, now=datetime.now(UTC))
        is None
    )
    assert old.status == "WAITING" and newer.status == "QUEUED"


def test_other_execution_head_lease_is_preserved(session, sync_seed):
    old, newer = pair(session, sync_seed)
    head = session.exec(select(SyncSchedule)).one()
    values = (uuid4(), uuid4(), 1, datetime.now(UTC) + timedelta(minutes=5))
    (
        head.lease_execution_id,
        head.lease_token,
        head.lease_execution_generation,
        head.leased_until,
    ) = values
    session.flush()
    assert (
        supersede_pending_report(session, job_id=old.id, now=datetime.now(UTC))
        == newer.id
    )
    session.refresh(head)
    assert (
        head.lease_execution_id,
        head.lease_token,
        head.lease_execution_generation,
        head.leased_until,
    ) == values
