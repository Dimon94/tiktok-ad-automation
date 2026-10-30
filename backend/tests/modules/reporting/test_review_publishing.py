from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlmodel import select

from app.modules.reporting import partitions, publish
from app.modules.reporting.jobs import enqueue_job, execute_job
from app.modules.reporting.store_models import (
    AccountReconciliationDay,
    AdReportDay,
    MaterialReportDay,
    ReportSyncJob,
    ReportSyncState,
)
from tests.modules.reporting.sync_conftest import ad_row
from tests.modules.reporting.test_jobs import (
    D,
    FakeReader,
    _enqueue_and_claim,
    _reclaim,
    _route,
)
from tests.modules.reporting.test_publish import HK


@pytest.mark.parametrize(
    "model,identity",
    [
        (AdReportDay, {"ad_id": "a", "campaign_id": "c", "adgroup_id": "g"}),
        (MaterialReportDay, {"main_material_id": "m", "main_material_type": "VIDEO"}),
    ],
)
def test_partition_creation_moves_existing_default_rows(
    session, sync_seed, model, identity
):
    day = date(2001, 2, 3)
    session.add(
        model(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            stat_date=day,
            currency="USD",
            timezone="UTC",
            fetched_at=datetime.now(UTC),
            sync_job_id=uuid4(),
            spend=Decimal("7.25"),
            **identity,
        )
    )
    session.flush()
    partitions.ensure_month_partitions_for_range(session, day, day)
    row = (
        session.connection()
        .execute(
            text(
                f"SELECT tableoid::regclass::text, spend FROM {model.__tablename__} WHERE tenant_id=:t"
            ),
            {"t": sync_seed.tenant_id},
        )
        .one()
    )
    assert row == (f"{model.__tablename__}_2001_02", Decimal("7.25"))


def test_daily_publication_uses_default_until_independent_partition_maintenance(
    session, sync_seed
):
    day = date(2002, 3, 2)
    publish.publish_ad_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts=HK,
        start=day,
        end=day,
        rows=[ad_row("a", day)],
    )
    table = (
        session.connection()
        .execute(
            text(
                "SELECT tableoid::regclass::text FROM ad_report_day WHERE tenant_id=:t"
            ),
            {"t": sync_seed.tenant_id},
        )
        .scalar_one()
    )
    assert table == "ad_report_day_default"
    before = (
        session.exec(
            select(AdReportDay).where(AdReportDay.tenant_id == sync_seed.tenant_id)
        )
        .one()
        .spend
    )
    # 业务发布不执行分区DDL；维护搬迁之后原金额不变。
    partitions.ensure_month_partitions_for_range(
        session, day, day, tables=("ad_report_day",)
    )
    after = (
        session.connection()
        .execute(
            text(
                "SELECT tableoid::regclass::text,spend FROM ad_report_day WHERE tenant_id=:t"
            ),
            {"t": sync_seed.tenant_id},
        )
        .one()
    )
    assert after == ("ad_report_day_2002_03", before)


@pytest.mark.parametrize("account_first", [True, False])
def test_reconciliation_tracks_latest_ad_publication(session, sync_seed, account_first):
    def ads(spend):
        publish.publish_ad_day(
            session,
            tenant_id=sync_seed.tenant_id,
            job_id=uuid4(),
            accounts=HK,
            start=D,
            end=D,
            rows=[ad_row("a", D, spend=spend, total=spend)],
        )

    def account():
        publish.publish_account_reconciliation(
            session,
            tenant_id=sync_seed.tenant_id,
            accounts=HK,
            start=D,
            end=D,
            rows=[
                {
                    "dimensions": {"advertiser_id": "acc-1", "stat_time_day": str(D)},
                    "metrics": {
                        "spend": "20",
                        "native_growth_total_ad_impression_value": "20",
                    },
                }
            ],
        )

    ads("10")
    if account_first:
        account()
    ads("20")
    if not account_first:
        account()
    session.expire_all()
    row = session.get(AccountReconciliationDay, (sync_seed.tenant_id, "acc-1", D))
    assert (
        row.account_spend
        == row.ad_sum_spend
        == row.account_revenue_total
        == row.ad_sum_revenue_total
        == Decimal("20")
    )


def test_overlapping_tiers_defer_before_reading_then_publish_new_observation(
    committed_sync_seed,
):
    seed, factory = committed_sync_seed
    first = _enqueue_and_claim(factory, seed)
    with factory() as s:
        second_id = enqueue_job(
            s,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            kind="ad_day",
            tier="daily",
            advertiser_ids=["acc-1"],
            start=D,
            end=D,
            frozen_route=_route(seed),
            actor_id=seed.actor_id,
        )
        s.commit()
    entered, release = Event(), Event()

    class SlowReader(FakeReader):
        def ad_day(self, **kwargs):
            entered.set()
            assert release.wait(10)
            return super().ad_day(**kwargs)

    old = SlowReader(pages={(("acc-1",), (), 1): [ad_row("a", D, spend="10")]})
    new = FakeReader(pages={(("acc-1",), (), 1): [ad_row("a", D, spend="20")]})
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            execute_job,
            factory,
            job_id=first.id,
            claim_token=first.claim_token,
            reader=old,
        )
        assert entered.wait(5)
        try:
            from tests.modules.reporting.claiming import claim_jobs

            with factory() as claimant, claimant.begin():
                # 新合同在派发前阻挡重叠桶，不再为等待同桶先占第二个worker。
                assert (
                    claim_jobs(
                        claimant, now=datetime.now(UTC) + timedelta(seconds=1), limit=1
                    )
                    == []
                )
                assert claimant.get(ReportSyncJob, second_id).status == "QUEUED"
        finally:
            release.set()
        assert pending.result(timeout=10) == "COMPLETE"
    assert new.calls == []
    with factory() as session:
        first_success = session.get(
            ReportSyncState, (seed.tenant_id, "acc-1", "ad_day", D)
        ).last_success_at
    second = _reclaim(factory, second_id)
    assert (
        execute_job(
            factory, job_id=second.id, claim_token=second.claim_token, reader=new
        )
        == "COMPLETE"
    )
    # 第二个请求取得账户锁后才首次读取，虽排队更早，实际观测仍比前次发布新。
    with factory() as s:
        assert s.exec(
            select(AdReportDay).where(AdReportDay.tenant_id == seed.tenant_id)
        ).one().spend == Decimal("20")
        assert (
            s.get(
                ReportSyncState, (seed.tenant_id, "acc-1", "ad_day", D)
            ).last_success_at
            > first_success
        )
    third = _enqueue_and_claim(factory, seed)
    assert (
        execute_job(factory, job_id=third.id, claim_token=third.claim_token, reader=new)
        == "COMPLETE"
    )
    with factory() as s:
        assert s.exec(
            select(AdReportDay).where(AdReportDay.tenant_id == seed.tenant_id)
        ).one().spend == Decimal("20")
        assert (
            s.get(
                ReportSyncState, (seed.tenant_id, "acc-1", "ad_day", D)
            ).last_success_at
            > first_success
        )
