"""回补生产者按账户窗口提交；真实 PG 第二连接观察进度与撤权。"""

from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlmodel import select

from app.modules.reporting import creative_rebuild, publish
from app.modules.reporting.store_models import ReportSyncJob
from tests.modules.reporting.sync_conftest import ad_row
from tests.modules.reporting.test_creative_rebuild import _backfill_history


def prepare(seed, factory):
    with factory() as session:
        now, start, end, _ = _backfill_history(session, seed)
        publish.publish_ad_day(
            session,
            tenant_id=seed.tenant_id,
            job_id=seed.actor_id,
            accounts={"acc-2": publish.AccountMeta("USD", "UTC")},
            start=start,
            end=end,
            rows=[
                ad_row("ad2", start + timedelta(days=i), adv="acc-2") for i in range(21)
            ],
            requested_at=now,
        )
        session.commit()
    return {
        "tenant_id": seed.tenant_id,
        "bc_id": "bc-a",
        "start": start,
        "end": end,
        "now": now,
    }


def jobs(factory, seed):
    with factory() as observer:
        return observer.exec(
            select(ReportSyncJob).where(ReportSyncJob.tenant_id == seed.tenant_id)
        ).all()


def test_partial_commits_visible_bounded_and_restart_reuses(
    committed_sync_seed, monkeypatch
):
    seed, factory = committed_sync_seed
    args = prepare(seed, factory)
    real_enqueue = creative_rebuild.enqueue_job
    calls = 0

    def interrupt(session, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            # 第二批开始时第一批四层任务已从其他连接可见；旧实现这里为0。
            visible = jobs(factory, seed)
            assert {job.kind for job in visible} == {
                "campaign_day",
                "adgroup_day",
                "ad_day",
                "creative_day",
            }
            assert len(visible) == 4
            assert all((j.end_date - j.start_date).days < 7 for j in visible)
            with factory() as observer:
                days = (
                    observer.connection()
                    .execute(
                        text(
                            "SELECT count(*) FROM sync_requirement WHERE tenant_id=:t"
                        ),
                        {"t": seed.tenant_id},
                    )
                    .scalar_one()
                )
                assert days == 28
            raise RuntimeError("controlled interruption")
        return real_enqueue(session, **kwargs)

    monkeypatch.setattr(creative_rebuild, "enqueue_job", interrupt)
    with (
        factory() as writer,
        pytest.raises(RuntimeError, match="controlled interruption"),
    ):
        creative_rebuild.enqueue_backfill(writer, **args)
    assert len(jobs(factory, seed)) == 4
    monkeypatch.setattr(creative_rebuild, "enqueue_job", real_enqueue)
    receipts = []
    with factory() as writer:
        assert (
            creative_rebuild.enqueue_backfill(writer, **args, receipt=receipts.append)
            == 10
        )
    assert len(receipts) == 6 and receipts[0]["created_jobs"] == []
    assert all(len(r["created_jobs"]) <= 2 for r in receipts)
    assert [r["advertiser"] for r in receipts[:2]] == ["acc-1", "acc-2"]
    assert all(j.progress["execution_window_days"] == 7 for j in jobs(factory, seed))
    with factory() as writer:
        assert creative_rebuild.enqueue_backfill(writer, **args) == 0
    assert len(jobs(factory, seed)) == 24


@pytest.mark.parametrize("change", ["ownership", "route", "disabled", "plan-route"])
def test_revalidate_every_batch_after_revocation(committed_sync_seed, change):
    seed, factory = committed_sync_seed
    args = prepare(seed, factory)
    receipts = []

    def revoke(receipt):
        receipts.append(receipt)
        if len(receipts) == 1:
            with factory() as revoker:
                sql = {
                    "ownership": "UPDATE ad_sync_account SET owner_bc_id='bc-b' WHERE tenant_id=:t AND advertiser_id='acc-2'",
                    "route": "UPDATE bc_connection_binding SET revision=revision+1 WHERE tenant_id=:t AND bc_id='bc-a'",
                    "disabled": "UPDATE ad_sync_bc SET enabled=false WHERE tenant_id=:t AND bc_id='bc-a'",
                    "plan-route": "UPDATE ad_sync_bc SET frozen_route=jsonb_set(frozen_route,'{binding_revision}','999') WHERE tenant_id=:t AND bc_id='bc-a'",
                }[change]
                revoker.connection().execute(text(sql), {"t": seed.tenant_id})
                revoker.commit()

    with (
        factory() as writer,
        pytest.raises(
            Exception, match="ownership changed|绑定已失效|plan or frozen route changed"
        ),
    ):
        creative_rebuild.enqueue_backfill(writer, **args, receipt=revoke)
    assert len(receipts) == 1 and len(jobs(factory, seed)) == 4
