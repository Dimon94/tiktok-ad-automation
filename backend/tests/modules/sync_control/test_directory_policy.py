from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlmodel import select

from app.modules.sync_control.directory_candidates import (
    DiscoveryCursor,
    completion_candidate,
)
from app.modules.sync_control.directory_policy import (
    completion_opportunity,
    completion_scan,
    last_completion_account,
    record_background_grant,
    save_scan,
)
from app.modules.sync_control.models import SyncSchedule, SyncScope
from tests.modules.sync_control.test_schedule_fencing import _directory


def test_ordinary_success_preserves_completion_scan_and_priority_success_resets():
    now = datetime.now(UTC)
    policy = {"turn": 1.0}
    cursor = DiscoveryCursor.start(now)._replace(
        at=now - timedelta(days=1), scope_id=UUID(int=5)
    )
    save_scan(policy, cursor)
    record_background_grant(policy, advertiser_id="ordinary", completion=False)
    assert completion_opportunity(policy) and completion_scan(policy, now) == cursor
    record_background_grant(policy, advertiser_id="old", completion=True)
    assert (
        not completion_opportunity(policy) and last_completion_account(policy) == "old"
    )
    assert completion_scan(policy, now) == DiscoveryCursor.start(now)
    for i in range(1000):
        record_background_grant(policy, advertiser_id=str(i), completion=True)
    assert len(policy) == 2


def test_completion_scan_advances_past_four_empty_pages_across_calls(
    session, sync_seed
):
    now = datetime.now(UTC)
    run = _directory(session, sync_seed)
    run.workload = "background"
    scope = session.exec(select(SyncScope)).one()
    head = session.exec(select(SyncSchedule)).one()
    session.flush()
    scopes, heads = [], []
    for i in range(300):
        identity = UUID(int=i + 1)
        scopes.append(
            dict(scope.model_dump(), id=identity, identity_digest=f"{i:064x}")
        )
        heads.append(
            dict(
                head.model_dump(),
                scope_id=identity,
                oldest_pending_at=now - timedelta(days=10),
            )
        )
    session.execute(SyncScope.__table__.insert(), scopes)
    session.execute(SyncSchedule.__table__.insert(), heads)
    policy = {"turn": 1.0}
    args = {
        "tenant_id": sync_seed.tenant_id,
        "endpoint": head.physical_endpoint,
        "now": now + timedelta(seconds=1),
        "policy": policy,
        "excluded_run_ids": set(),
    }
    assert completion_candidate(session, **args) is None
    record_background_grant(policy, advertiser_id="ordinary", completion=False)
    assert completion_candidate(session, **args)[1].id == run.id


def test_excluded_account_is_filtered_before_head_limit(session, sync_seed):
    now = datetime.now(UTC)
    first = _directory(session, sync_seed)
    first.workload = "background"
    scope = session.exec(select(SyncScope)).one()
    head = session.exec(select(SyncSchedule)).one()
    session.flush()
    # 前300个head都属于上次已获优先账户，必须在LIMIT前排除。
    scopes, heads = [], []
    for i in range(300):
        identity = UUID(int=i + 1)
        scopes.append(
            dict(scope.model_dump(), id=identity, identity_digest=f"{i:064x}")
        )
        heads.append(
            dict(
                head.model_dump(),
                scope_id=identity,
                oldest_pending_at=now - timedelta(days=10),
            )
        )
    session.execute(SyncScope.__table__.insert(), scopes)
    session.execute(SyncSchedule.__table__.insert(), heads)
    other = _directory(session, sync_seed, advertiser="acc-2")
    other.workload = "background"
    session.flush()
    found = completion_candidate(
        session,
        tenant_id=sync_seed.tenant_id,
        endpoint=head.physical_endpoint,
        now=datetime.now(UTC) + timedelta(seconds=1),
        policy={"turn": 1.0, "account:acc-1": 1.0},
        excluded_run_ids=set(),
    )
    assert found is not None and found[1].id == other.id
