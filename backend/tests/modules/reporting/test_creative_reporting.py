"""创意日事实独立保存，不能混入广告总账或吞掉历史修订。"""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlmodel import select

from app.core.errors import DomainError
from app.modules.reporting import publish, store_models
from tests.integrations.tiktok.test_sdk_report_reader import PAGE, _reader
from tests.modules.reporting.sync_conftest import ad_row

D = date(2026, 10, 5)


def creative_row(key="creative-1", spend="12", revenue="4"):
    row = ad_row(key, D, spend=spend)
    row["dimensions"]["ad_id"] = row["dimensions"].pop("ad_id_v2")
    row["metrics"].update(
        native_growth_total_ad_impression_value=revenue,
        campaign_automation_type="UPGRADED_SMART_PLUS_CREATIVE",
    )
    return row


def test_creative_reader_requests_native_revenue_with_creative_dimension():
    reader, calls = _reader(PAGE)
    assert hasattr(reader, "creative_day"), "creative_day reader must exist"
    reader.creative_day(advertiser_ids=("acc-1",), start=D, end=D, page=1)
    operation, advertiser, args = calls.calls[0]
    assert operation == "reports.integrated" and advertiser == "acc-1"
    assert args["dimensions"] == ["ad_id", "stat_time_day"]
    assert "native_growth_total_ad_impression_value" in args["metrics"]
    assert any("STATUS_ALL" in f["filter_value"] for f in args["filtering"])
    with pytest.raises(DomainError):
        reader.creative_day(advertiser_ids=("acc-1", "acc-2"), start=D, end=D, page=1)


def test_creative_publication_keeps_ad_total_separate_and_old_snapshot_fenced(
    session, sync_seed
):
    model = getattr(store_models, "CreativeReportDay", None)
    assert model is not None, "creative fact model must exist"
    args = {
        "tenant_id": sync_seed.tenant_id,
        "job_id": uuid4(),
        "accounts": {"acc-1": publish.AccountMeta("USD", "Asia/Hong_Kong")},
        "start": D,
        "end": D,
    }
    publish.publish_ad_day(session, **args, rows=[ad_row("ad-1", D, spend="20")])
    count = publish.publish_creative_day(session, **args, rows=[creative_row()])
    assert count == 1
    assert session.get(
        model, (sync_seed.tenant_id, "acc-1", D, "creative-1")
    ).spend == Decimal("12")
    assert session.get(
        store_models.AdReportDay, (sync_seed.tenant_id, "acc-1", D, "ad-1")
    ).spend == Decimal("20")
    assert (
        publish.publish_creative_day(
            session, **args, rows=[], requested_at=datetime(2020, 1, 1, tzinfo=UTC)
        )
        == 0
    )
    assert (
        len(
            session.exec(
                select(model).where(model.tenant_id == sync_seed.tenant_id)
            ).all()
        )
        == 1
    )
    state = session.get(
        store_models.ReportSyncState, (sync_seed.tenant_id, "acc-1", "creative_day", D)
    )
    assert state.status == "COMPLETE" and state.row_count == 1


def test_creative_publication_validates_scope_before_replacing_and_keeps_null(
    session, sync_seed
):
    model = getattr(store_models, "CreativeReportDay", None)
    assert model is not None, "creative fact model must exist"
    args = {
        "tenant_id": sync_seed.tenant_id,
        "job_id": uuid4(),
        "accounts": {"acc-1": publish.AccountMeta("USD", "Asia/Hong_Kong")},
        "start": D,
        "end": D,
    }
    publish.publish_creative_day(session, **args, rows=[creative_row(revenue="-")])
    value = session.get(model, (sync_seed.tenant_id, "acc-1", D, "creative-1"))
    assert value.revenue_total is None
    wrong = creative_row()
    wrong["metrics"]["advertiser_id"] = "outside"
    with pytest.raises(DomainError):
        publish.publish_creative_day(session, **args, rows=[wrong])
    assert value.spend == Decimal("12")


def test_creative_campaign_sharding_calls_creative_reader():
    from app.integrations.tiktok.contracts.report_reader import RawReportPage
    from app.modules.reporting.jobs import _read_campaign_batches

    class Reader:
        def campaign_day(self, **_kwargs):
            return RawReportPage(({"dimensions": {"campaign_id": "c1"}},), 1, 1, 1)

        def creative_day(self, **kwargs):
            assert kwargs["campaign_ids"] == ("c1",)
            return RawReportPage((creative_row(),), 1, 1, 1)

        def ad_day(self, **_kwargs):
            pytest.fail("creative shard must not read ad_id_v2")

    assert "kind" in __import__("inspect").signature(_read_campaign_batches).parameters
    rows = _read_campaign_batches(Reader(), ("acc-1",), D, D, kind="creative_day")
    assert rows[0]["dimensions"]["ad_id"] == "creative-1"


def test_hot_plan_pairs_ad_and_single_account_creative_facts(session, sync_seed):
    from app.modules.reporting.planner import plan_hot
    from app.modules.reporting.store_models import AdSyncBc, ReportSyncJob
    from tests.modules.reporting.test_jobs import _route

    plan = AdSyncBc(
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        actor_id=sync_seed.actor_id,
        frozen_route=_route(sync_seed),
    )
    now = datetime(2026, 10, 5, 10, tzinfo=UTC)
    plan_hot(session, plan, active=["acc-1"], now=now)
    jobs = session.exec(
        select(ReportSyncJob).where(ReportSyncJob.tenant_id == sync_seed.tenant_id)
    ).all()
    # M4a 已将直接父级日报纳入原调度入口，creative 仍独立单账户。
    assert {j.kind for j in jobs} == {
        "campaign_day",
        "adgroup_day",
        "ad_day",
        "creative_day",
    }
    assert all(j.advertiser_ids == ["acc-1"] for j in jobs)


def test_creative_job_runs_through_dispatch_and_publishes(committed_sync_seed):
    from datetime import timedelta

    from app.integrations.tiktok.contracts.report_reader import RawReportPage
    from app.modules.reporting.jobs import enqueue_job, execute_job
    from tests.modules.reporting.claiming import claim_jobs
    from tests.modules.reporting.test_jobs import _route

    seed, factory = committed_sync_seed
    with factory() as session:
        enqueue_job(
            session,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            kind="creative_day",
            tier="hot",
            advertiser_ids=["acc-1"],
            start=D,
            end=D,
            frozen_route=_route(seed),
            actor_id=seed.actor_id,
        )
        session.commit()
        jobs = claim_jobs(
            session, now=datetime.now(UTC) + timedelta(seconds=1), limit=1
        )
        assert len(jobs) == 1
        job = jobs[0]
        session.commit()

    class Reader:
        def creative_day(self, **kwargs):
            assert kwargs["advertiser_ids"] == ("acc-1",)
            return RawReportPage((creative_row(),), 1, 1, 1)

    assert (
        execute_job(
            factory, job_id=job.id, claim_token=job.claim_token, reader=Reader()
        )
        == "COMPLETE"
    )
    with factory() as session:
        assert session.get(
            store_models.CreativeReportDay, (seed.tenant_id, "acc-1", D, "creative-1")
        ).spend == Decimal("12")
