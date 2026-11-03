from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import text
from sqlmodel import select

from app.integrations.tiktok.contracts.ads import CallEvidence, DirectoryPage
from app.modules.ads.sync import stage_directory_page
from app.modules.sync_control.directory_dispatch import _candidate
from app.modules.sync_control.models import SyncSchedule, SyncScope
from tests.modules.sync_control.test_balance_dispatch_scale import capture_plans
from tests.modules.sync_control.test_schedule_fencing import _directory


def test_independent_candidate_does_not_probe_account_for_every_ineligible_head(
    session, sync_seed
):
    run = _directory(session, sync_seed)
    stage_directory_page(
        session,
        run_id=run.id,
        page=DirectoryPage((), (), None, True, CallEvidence()),
        claim_generation=run.claim_generation,
    )
    scope = session.exec(select(SyncScope)).one()
    head = session.exec(select(SyncSchedule)).one()
    head.last_dispatched_at = datetime.now(UTC)
    session.flush()
    scopes, heads = [], []
    for i in range(1500):
        identity = uuid4()
        scopes.append(
            dict(scope.model_dump(), id=identity, identity_digest=f"{i:064x}")
        )
        heads.append(
            dict(head.model_dump(), scope_id=identity, last_dispatched_at=None)
        )
    session.execute(SyncScope.__table__.insert(), scopes)
    session.execute(SyncSchedule.__table__.insert(), heads)
    session.execute(text("ANALYZE sync_schedule"))
    found = []
    plans = capture_plans(
        session,
        lambda sql: "picked.id" in sql and "FOR UPDATE" not in sql,
        lambda: found.append(
            _candidate(
                session,
                tenant_id=sync_seed.tenant_id,
                work_class=head.work_class,
                now=datetime.now(UTC),
                independent_only=True,
            )
        ),
    )
    assert found[0][1].id == run.id

    def probes(n):
        own = (
            n.get("Actual Loops", 0)
            if n.get("Relation Name") == "ad_directory_run"
            else 0
        )
        return own + sum(probes(c) for c in n.get("Plans", []))

    assert plans and max(probes(p["Plan"]) for p in plans) < 100


def test_scan_cursor_survives_transaction_rollback(session, sync_seed, redis_client):
    from uuid import UUID

    run = _directory(session, sync_seed)
    stage_directory_page(
        session,
        run_id=run.id,
        page=DirectoryPage((), (), None, True, CallEvidence()),
        claim_generation=run.claim_generation,
    )
    scope = session.exec(select(SyncScope)).one()
    head = session.exec(select(SyncSchedule)).one()
    session.flush()
    scopes, heads = [], []
    for i in range(2200):
        identity = UUID(int=i + 1)
        scopes.append(
            dict(scope.model_dump(), id=identity, identity_digest=f"{i:064x}")
        )
        heads.append(dict(head.model_dump(), scope_id=identity))
    session.execute(SyncScope.__table__.insert(), scopes)
    session.execute(SyncSchedule.__table__.insert(), heads)
    args = {
        "tenant_id": sync_seed.tenant_id,
        "work_class": head.work_class,
        "now": datetime.now(UTC),
        "independent_only": True,
        "discover_only": True,
        "redis": redis_client,
    }
    with session.begin_nested() as attempt:
        assert _candidate(session, **args) is None
        attempt.rollback()
    # SQL回滚不能使发现游标也退回头部；无资格前缀不能永久遮住后面的发布。
    found = _candidate(session, **args)
    assert found is not None and found[1].id == run.id
