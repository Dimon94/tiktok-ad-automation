"""业务价值按账户、帖子和窗口合计；真实 PG 验证补链收敛且不伪造映射。"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlmodel import select

from app.modules.reporting import jobs, material_links
from app.modules.reporting.link_work import wake_link_dependencies
from app.modules.reporting.material_identity import pending_asset_ids
from app.modules.reporting.material_value import material_window_values
from app.modules.reporting.spark_identity import pending_spark_links
from app.modules.reporting.store_models import (
    MaterialPostLink,
    MaterialReportDay,
    ReportSyncJob,
)
from tests.modules.reporting.claiming import claim_jobs
from tests.modules.reporting.test_backlog_control import request
from tests.modules.reporting.test_material_links import overview, refresh
from tests.modules.reporting.test_material_query import D


def fact(session, seed, mid, spend, revenue, *, day=D, adv="acc-1", tenant=None):
    row = MaterialReportDay(
        tenant_id=tenant or seed.tenant_id,
        advertiser_id=adv,
        stat_date=day,
        main_material_id=mid,
        main_material_type="VIDEO_SPARK_ADS",
        currency="USD",
        timezone="UTC",
        fetched_at=datetime.now(UTC),
        sync_job_id=uuid4(),
        spend=None if spend is None else Decimal(spend),
        revenue_total=None if revenue is None else Decimal(revenue),
    )
    session.add(row)
    session.flush()
    return row


@pytest.mark.parametrize(
    "old_spend,new_spend,old_revenue,new_revenue,valuable",
    [
        ("1", "-1", "0", "0", False),
        ("1", "-0.99", "0", "0", True),
        ("-2", "1", "0", "0", False),
        ("0", "0", "1", "-1", False),
        ("0", "0", "1", "-0.99", True),
        ("0", "0", None, None, False),
        (None, None, "0", "1", True),
    ],
)
def test_link_value_uses_window_total_before_threshold(
    session, sync_seed, old_spend, new_spend, old_revenue, new_revenue, valuable
):
    first = D - timedelta(days=1)
    fact(session, sync_seed, "post", old_spend, old_revenue, day=first)
    fact(session, sync_seed, "post", new_spend, new_revenue)
    # 同 ID 的其他账户/租户、窗口外金额不能让本窗口错误地产生价值。
    fact(session, sync_seed, "post", "999", "0", adv="acc-2")
    fact(session, sync_seed, "post", "999", "0", tenant=uuid4())
    fact(session, sync_seed, "post", "999", "0", day=first - timedelta(days=1))
    args = {
        "tenant_id": sync_seed.tenant_id,
        "advertiser_id": "acc-1",
        "start": first,
        "end": D,
    }
    assert material_links.needs_material_links(session, **args) is valuable
    assert material_links.missing_material_link_ids(session, **args) == (
        {"post"} if valuable else set()
    )


def test_overlapping_rule_windows_are_checked_independently(session, sync_seed):
    first = D - timedelta(days=1)
    fact(session, sync_seed, "post", "-1", "0", day=first)
    fact(session, sync_seed, "post", "1", "0")
    assert material_window_values(
        session, sync_seed.tenant_id, [("acc-1", first, D)]
    ) == {("acc-1", "post"): False}
    # 总窗口抵消为零，但今天有金额；不能把多个规则窗口合并后漏掉今天的缺口。
    assert material_window_values(
        session,
        sync_seed.tenant_id,
        [("acc-1", first, D), ("acc-1", D, D), ("acc-1", D, D)],
    ) == {("acc-1", "post"): True}


def test_zero_value_waiting_job_completes_after_dependency_recheck(
    committed_sync_seed,
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session:
        row = fact(session, seed, "post", "1", "0")
        job_id = request(session, seed, D)
        job = session.get(ReportSyncJob, job_id)
        assert material_links.wait_for_dependencies(session, job, now=now)
        job.status = "WAITING"
        from app.modules.sync_control.schedule import record_report_outcome

        record_report_outcome(session, job)
        session.commit()
        # 模拟回刷修正金额，也覆盖升级后旧等待集合仍含零金额帖子的情况。
        row.spend = Decimal(0)
        session.add(row)
        session.commit()
    with factory() as session:
        job = session.get(ReportSyncJob, job_id)
        assert job.status == "WAITING" and job.due_at > now
        assert material_links.dependency_changed(session, job)
        assert wake_link_dependencies(session, now=now) == 1
        from app.modules.sync_control.models import SyncSchedule

        head = session.exec(
            select(SyncSchedule).where(SyncSchedule.tenant_id == seed.tenant_id)
        ).one()
        assert head.active and head.next_eligible_at <= now
        session.commit()
        claimed = claim_jobs(session, now=now, limit=10)
        assert [j.id for j in claimed] == [job_id]
        token = claimed[0].claim_token
        session.commit()
    # 不提供任何平台方法，确保结束等待不再发起身份/概览请求。
    assert (
        jobs.execute_job(factory, job_id=job_id, claim_token=token, reader=object())
        == "COMPLETE"
    )
    with factory() as session:
        job = session.get(ReportSyncJob, job_id)
        assert job.error_code is None and job.finished_at is not None
        assert job.claim_token is None
        assert session.exec(select(MaterialPostLink)).all() == []


@pytest.mark.parametrize("kind", ["VIDEO_SPARK_ADS", "VIDEO_NON_SPARK_ADS"])
def test_mixed_identity_task_only_reads_valuable_posts(session, sync_seed, kind):
    for mid, amount in [("zero", "0"), ("paid", "1")]:
        row = fact(session, sync_seed, mid, amount, "0")
        row.main_material_type = kind
        session.add(row)
        session.add(
            MaterialPostLink(
                tenant_id=sync_seed.tenant_id,
                advertiser_id="acc-1",
                main_material_id=mid,
                ad_remote_id=f"ad-{mid}",
                main_material_type=kind,
                observed_at=datetime.now(UTC),
            )
        )
    session.flush()
    job = session.get(ReportSyncJob, request(session, sync_seed, D))
    if kind == "VIDEO_SPARK_ADS":
        assert [r["main_material_id"] for r in pending_spark_links(session, job)] == [
            "paid"
        ]
    else:
        assert pending_asset_ids(session, job) == ("paid",)
    assert jobs._material_link_overview_ads(session, job) == ("ad-paid",)


def test_zero_posts_do_not_change_waiting_signature_or_request_directory(
    session, sync_seed, monkeypatch
):
    from app.core.config import settings
    from app.modules.ads.sync_models import AdDirectoryRun
    from app.modules.reporting.directory_refresh import request_link_directory

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    fact(session, sync_seed, "paid", "1", "0")
    job = session.get(ReportSyncJob, request(session, sync_seed, D))
    assert material_links.wait_for_dependencies(session, job, now=now)
    zero = fact(session, sync_seed, "zero", "0", "0")
    session.add(
        MaterialPostLink(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            main_material_id="zero",
            ad_remote_id="ad-zero",
            main_material_type="VIDEO_SPARK_ADS",
            observed_at=now,
        )
    )
    session.flush()
    assert not material_links.dependency_changed(session, job)
    assert request_link_directory(session, job, now=now) == 0
    assert session.exec(select(AdDirectoryRun)).all() == []
    # 后续仅收入转正也必须唤醒，并恢复定向身份工作。
    zero.revenue_total = Decimal("0.01")
    session.add(zero)
    session.flush()
    assert material_links.dependency_changed(session, job)
    assert request_link_directory(session, job, now=now) == 1
    assert session.exec(select(AdDirectoryRun)).one().query["ids"] == ["ad-zero"]


def test_overview_does_not_map_incidental_zero_value_posts(session, sync_seed):
    fact(session, sync_seed, "paid", "1", "0")
    fact(session, sync_seed, "zero", "0", "0")
    refresh(
        session,
        sync_seed,
        overview(("a1", "paid", "m1"), ("a1", "zero", "m2")),
    )
    assert {row.main_material_id for row in session.exec(select(MaterialPostLink))} == {
        "paid"
    }
