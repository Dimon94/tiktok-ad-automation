from datetime import UTC, datetime, timedelta

from sqlmodel import select

from app.modules.sync_control.directory_dispatch import _candidate
from app.modules.sync_control.models import SyncExecutionScope, SyncSchedule
from tests.modules.sync_control.test_schedule_fencing import _directory


def test_new_arrival_cannot_permanently_preempt_an_old_continuation(session, sync_seed):
    now = datetime.now(UTC)
    old = _directory(session, sync_seed, advertiser="acc-1")
    fresh = _directory(session, sync_seed, advertiser="acc-2")
    heads = {}
    for run in (old, fresh):
        child = session.exec(
            select(SyncExecutionScope).where(
                SyncExecutionScope.directory_run_id == run.id
            )
        ).one()
        heads[run.id] = session.get(SyncSchedule, (child.scope_id, "directory"))
    heads[old.id].last_dispatched_at = now - timedelta(days=2)
    heads[old.id].oldest_pending_at = now - timedelta(days=3)
    heads[fresh.id].last_dispatched_at = None
    heads[fresh.id].oldest_pending_at = now - timedelta(minutes=1)
    args = {
        "tenant_id": sync_seed.tenant_id,
        "work_class": "directory",
        "now": now + timedelta(seconds=1),
        "independent_only": False,
        "discover_only": True,
    }
    assert _candidate(session, **args)[1].id == old.id
    # 老续点获得一步后让回新任务，既不饿死续点，也不永久抢占新任务。
    heads[old.id].last_dispatched_at = now
    assert _candidate(session, **args)[1].id == fresh.id
