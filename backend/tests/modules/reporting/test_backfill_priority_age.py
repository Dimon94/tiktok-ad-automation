"""统一到期头的历史进展：持续实时需求下仍通过真实领取/发布推进历史。

旧的“跨日热报移入backfill lane”排序已退役，不再断言固定lane或priority排名。
"""

from datetime import UTC, datetime, timedelta

from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
from app.modules.reporting.jobs import enqueue_job, execute_job
from tests.modules.reporting.claiming import claim_jobs
from tests.modules.reporting.test_jobs import FakeReader, _route


def test_history_makes_progress_while_realtime_heads_stay_saturated(
    committed_sync_seed,
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    tiers = {}
    with factory() as session, session.begin():
        for index in range(16):
            advertiser = f"fair-account-{index}"
            session.add(
                AdvertiserAccount(
                    tenant_id=seed.tenant_id,
                    advertiser_id=advertiser,
                    currency="USD",
                    timezone="UTC",
                )
            )
            session.flush()
            session.add(
                BCAccountAccess(
                    tenant_id=seed.tenant_id,
                    bc_id="bc-a",
                    advertiser_id=advertiser,
                    connection_id=seed.connection.id,
                    in_bc=True,
                    authorized=True,
                    active=True,
                    checked_at=now,
                )
            )
            tier = "hot" if index < 12 else "history"
            day = now.date() if tier == "hot" else now.date() - timedelta(days=10)
            job_id = enqueue_job(
                session,
                tenant_id=seed.tenant_id,
                bc_id="bc-a",
                actor_id=seed.actor_id,
                frozen_route=_route(seed),
                kind="ad_day",
                tier=tier,
                advertiser_ids=[advertiser],
                start=day,
                end=day,
                due_at=now,
            )
            tiers[job_id] = tier
    completed = []
    for turn in range(8):
        with factory() as session:
            claimed = claim_jobs(session, now=now + timedelta(seconds=turn), limit=1)
            assert len(claimed) == 1
            job_id, token = claimed[0].id, claimed[0].claim_token
            session.commit()
        assert (
            execute_job(factory, job_id=job_id, claim_token=token, reader=FakeReader())
            == "COMPLETE"
        )
        completed.append(tiers[job_id])
    # 实时还有未领取账户时，历史也必须已产生有效完成，不靠假token或仅改status。
    assert "hot" in completed
    assert "history" in completed
