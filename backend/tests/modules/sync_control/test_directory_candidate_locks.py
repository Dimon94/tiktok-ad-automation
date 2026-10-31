"""真实多连接验证：发现候选不锁住所有账户；只领取一条并跳过锁冲突。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.modules.sync_control.directory_dispatch import _candidate
from tests.modules.sync_control.test_schedule_fencing import _directory


def test_candidate_does_not_lock_unselected_runs(committed_sync_seed):
    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        runs = [_directory(db, seed, advertiser=f"acc-{i + 1}") for i in range(2)]
        ids = [r.id for r in runs]
    with factory() as first, first.begin():
        selected = _candidate(
            first,
            tenant_id=seed.tenant_id,
            work_class="directory",
            now=datetime.now(UTC) + timedelta(seconds=1),
            independent_only=False,
        )
        assert selected is not None
        remaining = [r for r in ids if r != selected[1].id]
        with factory() as other, other.begin():
            for identity in remaining:
                # 原查询在外层LIMIT前已经锁住这些执行及需求，本查询立即报锁冲突。
                row = other.execute(
                    text("""SELECT r.id FROM ad_directory_run r
                    JOIN sync_execution_scope e ON e.directory_run_id=r.id
                    JOIN sync_requirement q ON q.id=e.requirement_id
                    WHERE r.id=:id FOR UPDATE OF r,e,q NOWAIT"""),
                    {"id": identity},
                ).first()
                assert row is not None


def test_candidate_skips_locked_run_within_same_scope(committed_sync_seed):
    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        first = _directory(db, seed)
        second = _directory(db, seed)
        first.created_at = datetime.now(UTC) - timedelta(minutes=1)
        first_id, second_id = first.id, second.id
    with factory() as blocker, blocker.begin():
        blocker.execute(
            text("SELECT id FROM ad_directory_run WHERE id=:id FOR UPDATE"),
            {"id": first_id},
        )
        with factory() as claimant, claimant.begin():
            selected = _candidate(
                claimant,
                tenant_id=seed.tenant_id,
                work_class="directory",
                now=datetime.now(UTC) + timedelta(seconds=1),
                independent_only=False,
            )
            assert selected is not None and selected[1].id == second_id
