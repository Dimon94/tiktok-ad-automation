"""到期头替代旧lane全历史排名；保留锁跳过、双控制器和租约接管保证。

固定事实/关联各半槽和按kind虚拟账户权重已退役。工作类借槽由
sync_control/test_fairness.py、物理接口轮转由test_dispatcher.py覆盖。
"""

from datetime import UTC, date, datetime, timedelta

from sqlmodel import select

from app.modules.reporting.jobs import enqueue_job
from app.modules.reporting.store_models import ReportSyncJob
from app.modules.sync_control.models import SyncExecutionScope, SyncSchedule
from tests.modules.reporting.claiming import claim_jobs
from tests.modules.reporting.test_jobs import _route


def enqueue(session, seed, now, advertiser, *, due=None):
    return enqueue_job(
        session,
        tenant_id=seed.tenant_id,
        bc_id="bc-a",
        actor_id=seed.actor_id,
        frozen_route=_route(seed),
        kind="ad_day",
        tier="history",
        advertiser_ids=[advertiser],
        start=date(2026, 9, 1),
        end=date(2026, 9, 1),
        due_at=due or now,
    )


def test_locked_schedule_head_does_not_block_another_account(committed_sync_seed):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        blocked = enqueue(session, seed, now, "acc-1")
        ready = enqueue(session, seed, now, "acc-2")
        scope_id = session.exec(
            select(SyncExecutionScope.scope_id).where(
                SyncExecutionScope.report_job_id == blocked
            )
        ).one()
    with factory() as holder, holder.begin():
        holder.exec(
            select(SyncSchedule)
            .where(SyncSchedule.scope_id == scope_id)
            .with_for_update()
        ).one()
        with factory() as claimant, claimant.begin():
            assert [job.id for job in claim_jobs(claimant, now=now, limit=1)] == [ready]


def test_two_controllers_commit_distinct_claims_without_duplicate_outbox(
    committed_sync_seed,
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        pending = {
            enqueue(session, seed, now, account) for account in ("acc-1", "acc-2")
        }
    with factory() as first, factory() as second:
        claimed = claim_jobs(first, now=now, limit=1)
        assert len(claimed) == 1
        first_id = claimed[0].id
        # 公平cursor和领取同事务；另一controller不等待/不重复改同一轮次。
        assert claim_jobs(second, now=now, limit=1) == []
        second.rollback()
        first.commit()
        following = claim_jobs(second, now=now, limit=1)
        assert len(following) == 1
        assert {first_id, following[0].id} == pending
        second.commit()


def test_expired_schedule_and_job_lease_take_over_with_new_generation(
    session, sync_seed
):
    now = datetime.now(UTC)
    job_id = enqueue(session, sync_seed, now, "acc-1")
    first = claim_jobs(session, now=now, limit=1)[0]
    old_token, old_generation = first.claim_token, first.claim_generation
    # 用时间推进同时到期真实job与head租约，不伪造只有一半过期的状态。
    reclaimed = claim_jobs(session, now=now + timedelta(minutes=5), limit=1)
    assert [job.id for job in reclaimed] == [job_id]
    assert reclaimed[0].claim_token != old_token
    assert reclaimed[0].claim_generation > old_generation


def test_future_backoff_head_remains_unclaimed_until_due(session, sync_seed):
    now = datetime.now(UTC)
    job_id = enqueue(session, sync_seed, now, "acc-1", due=now + timedelta(minutes=1))
    assert claim_jobs(session, now=now, limit=1) == []
    assert session.get(ReportSyncJob, job_id).status == "QUEUED"
    assert [
        job.id for job in claim_jobs(session, now=now + timedelta(minutes=1), limit=1)
    ] == [job_id]
