"""真实多连接验证报表账户批次并行及健康检查的跨账户规划围栏。"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlmodel import select

from app.core.config import settings
from app.jobs.models import DispatchTenantCursor
from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting.directory_refresh import (
    refresh_account_directory,
    refresh_report_directory,
)
from app.modules.reporting.publish import AccountMeta, publish_ad_day
from tests.modules.reporting.sync_conftest import ad_row
from tests.modules.reporting.test_directory_refresh import prepared


def _two_accounts(factory, seed):
    with factory() as session, session.begin():
        # 正常租户已有已提交的outbox游标；首次初始化的唯一键竞争另有保护。
        session.add(DispatchTenantCursor(tenant_id=seed.tenant_id))
        first = prepared(session, seed, types=["UPGRADED_SMART_PLUS"])
        second = first.model_copy(update={"advertiser_ids": ["acc-1"]})
        day = datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
        row = ad_row("other-ad", day, adv="acc-1")
        row["metrics"]["campaign_automation_type"] = "UPGRADED_SMART_PLUS"
        # 两户都有真实今日事实，不能用空账户的快速返回冒充并行生产请求。
        publish_ad_day(
            session,
            tenant_id=seed.tenant_id,
            job_id=first.id,
            accounts={"acc-1": AccountMeta("USD", "Asia/Hong_Kong")},
            start=day,
            end=day,
            rows=[row],
        )
        return first, second


def test_disjoint_multi_account_reports_can_commit_independently(
    committed_sync_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    seed, factory = committed_sync_seed
    first, _ = _two_accounts(factory, seed)
    day = datetime.now(ZoneInfo("UTC")).date()
    with factory() as session, session.begin():
        for account in ["acc-3", "acc-4"]:
            session.add(
                AdvertiserAccount(
                    tenant_id=seed.tenant_id,
                    advertiser_id=account,
                    currency="USD",
                    timezone="UTC",
                )
            )
            session.add(
                BCAccountAccess(
                    tenant_id=seed.tenant_id,
                    bc_id="bc-a",
                    advertiser_id=account,
                    connection_id=seed.connection.id,
                    in_bc=True,
                    authorized=True,
                    active=True,
                    checked_at=datetime.now(ZoneInfo("UTC")),
                )
            )
        session.flush()
        rows = [ad_row("batch-" + a, day, adv=a) for a in ["acc-3", "acc-4"]]
        for row in rows:
            row["metrics"]["campaign_automation_type"] = "UPGRADED_SMART_PLUS"
        publish_ad_day(
            session,
            tenant_id=seed.tenant_id,
            job_id=first.id,
            accounts={a: AccountMeta("USD", "UTC") for a in ["acc-3", "acc-4"]},
            start=day,
            end=day,
            rows=rows,
        )
    with factory() as held, held.begin():
        assert (
            refresh_report_directory(
                held, first.model_copy(update={"advertiser_ids": ["acc-2", "acc-1"]})
            )
            == 6
        )
        with factory() as independent, independent.begin():
            independent.execute(text("SET LOCAL lock_timeout='200ms'"))
            assert (
                refresh_report_directory(
                    independent,
                    first.model_copy(update={"advertiser_ids": ["acc-4", "acc-3"]}),
                )
                == 6
            )


def test_reversed_overlapping_report_batches_keep_exactly_one_request(
    committed_sync_seed, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    seed, factory = committed_sync_seed
    first, _ = _two_accounts(factory, seed)
    barrier = Barrier(2)

    def run(accounts):
        with factory() as session, session.begin():
            session.execute(text("SET LOCAL lock_timeout='3s'"))
            barrier.wait(timeout=5)
            return refresh_report_directory(
                session, first.model_copy(update={"advertiser_ids": accounts})
            )

    with ThreadPoolExecutor(2) as pool:
        jobs = [pool.submit(run, a) for a in [["acc-2", "acc-1"], ["acc-1", "acc-2"]]]
        assert sorted(j.result(timeout=10) for j in jobs) == [0, 6]
    with factory() as session:
        runs = session.exec(
            select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == seed.tenant_id)
        ).all()
        assert len(runs) == 6


def test_single_account_reports_do_not_wait_for_another_account(
    committed_sync_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    seed, factory = committed_sync_seed
    first, second = _two_accounts(factory, seed)
    with factory() as held, held.begin():
        assert refresh_report_directory(held, first) == 3
        held.flush()
        with factory() as independent, independent.begin():
            independent.execute(text("SET LOCAL lock_timeout='200ms'"))
            assert refresh_report_directory(independent, second) == 3
    # 在首个事务仍未提交时，第二户已实际创建目录请求并独立提交。


@pytest.mark.parametrize("holder", ["report", "health", "old_report", "multi_report"])
@pytest.mark.parametrize("health_contender", [False, True])
def test_same_account_and_bc_wide_planners_keep_transaction_fences(
    committed_sync_seed, monkeypatch, holder, health_contender
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    seed, factory = committed_sync_seed
    first, second = _two_accounts(factory, seed)
    with factory() as held, held.begin():
        if holder == "old_report":
            held.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))"),
                {"scope": f"report-directory:{seed.tenant_id}:bc-a"},
            )
        elif holder == "health":
            refresh_account_directory(
                held,
                tenant_id=seed.tenant_id,
                bc_id=first.bc_id,
                actor_id=first.actor_id,
                frozen_route=first.frozen_route,
                advertiser_ids=["acc-2"],
            )
        elif holder == "multi_report":
            assert (
                refresh_report_directory(
                    held,
                    first.model_copy(update={"advertiser_ids": ["acc-1", "acc-2"]}),
                )
                == 6
            )
        else:
            assert refresh_report_directory(held, first) == 3
        with factory() as contender:
            with pytest.raises(DBAPIError) as caught:
                with contender.begin():
                    contender.execute(text("SET LOCAL lock_timeout='200ms'"))
                    if health_contender:
                        refresh_account_directory(
                            contender,
                            tenant_id=seed.tenant_id,
                            bc_id=first.bc_id,
                            actor_id=first.actor_id,
                            frozen_route=first.frozen_route,
                            advertiser_ids=["acc-1"],
                        )
                    else:
                        refresh_report_directory(
                            contender, first if holder == "report" else second
                        )
            assert caught.value.orig.sqlstate == "55P03"
    # 首个事务结束后，同户重读只复用在途请求，不重复创建。
    with factory() as contender, contender.begin():
        assert refresh_report_directory(contender, first) == (
            3 if holder == "old_report" else 0
        )
