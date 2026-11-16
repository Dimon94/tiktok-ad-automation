"""真实 PostgreSQL 验证素材分页续传、原子发布、范围与新旧请求隔离。"""

from datetime import UTC, date, datetime, timedelta

from sqlmodel import select

from app.integrations.tiktok.contracts.report_reader import RawReportPage
from app.modules.reporting import jobs
from app.modules.reporting.publish import AccountMeta
from app.modules.reporting.store_models import (
    CreativeReportDay,
    ReportSyncJob,
    ReportSyncPage,
)
from tests.modules.reporting.claiming import claim_jobs
from tests.modules.reporting.test_jobs import _route

DAY = date(2026, 10, 5)
ACCOUNTS = {"acc-1": AccountMeta("USD", "Asia/Hong_Kong")}


def row(index, *, day=DAY, spend="1", advertiser="acc-1"):
    return {
        "dimensions": {
            "stat_time_day": str(day),
            "ad_id": f"m{index}",
        },
        "metrics": {
            "advertiser_id": advertiser,
            "spend": spend,
            "campaign_id": "c1",
            "adgroup_id": "g1",
        },
    }


class Pages:
    def __init__(self, count=7, *, rows=None, failure=None):
        self.rows = rows if rows is not None else [row(index) for index in range(count)]
        self.failure = failure
        self.calls = []

    def creative_day(self, *, advertiser_ids, start, end, page, campaign_ids=()):
        self.calls.append(page)
        if self.failure and page == self.failure[0]:
            raise self.failure[1]
        count = len(self.rows)
        return RawReportPage(tuple(self.rows[page - 1 : page]), page, count, count)


def enqueue(factory, seed, *, start=DAY, end=DAY):
    with factory() as session:
        job_id = jobs.enqueue_job(
            session,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            kind="creative_day",
            tier="material",
            advertiser_ids=["acc-1"],
            start=start,
            end=end,
            frozen_route=_route(seed),
            actor_id=seed.actor_id,
        )
        session.commit()
    return claim(factory, job_id)


def add_third_account(session, seed):
    from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess

    session.add(
        AdvertiserAccount(
            tenant_id=seed.tenant_id,
            advertiser_id="acc-3",
            currency="USD",
            timezone="UTC",
        )
    )
    session.flush()
    session.add(
        BCAccountAccess(
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            advertiser_id="acc-3",
            connection_id=seed.connection.id,
            in_bc=True,
            authorized=True,
            active=True,
            checked_at=datetime.now(UTC),
        )
    )
    session.flush()


def claim(factory, job_id):
    with factory() as session:
        target = session.get(ReportSyncJob, job_id)
        candidates = claim_jobs(
            session,
            now=max(datetime.now(UTC), target.due_at) + timedelta(seconds=1),
            limit=100,
        )
        result = next((job for job in candidates if job.id == job_id), None)
        if result is None:
            from app.modules.sync_control.models import SyncExecutionScope, SyncSchedule

            heads = session.exec(
                select(SyncSchedule)
                .join(
                    SyncExecutionScope,
                    SyncExecutionScope.scope_id == SyncSchedule.scope_id,
                )
                .where(SyncExecutionScope.report_job_id == job_id)
            ).all()
            raise AssertionError(
                {
                    "target": (
                        target.status,
                        target.due_at,
                        target.claimed_until,
                        target.error_code,
                    ),
                    "heads": [h.model_dump() for h in heads],
                }
            )
        session.commit()
        return result


def run(factory, job, reader):
    return jobs.execute_job(
        factory, job_id=job.id, claim_token=job.claim_token, reader=reader
    )


def pages(factory, job_id):
    with factory() as session:
        return session.exec(
            select(ReportSyncPage)
            .where(ReportSyncPage.job_id == job_id)
            .order_by(ReportSyncPage.page)
        ).all()


def facts(factory, tenant):
    with factory() as session:
        return session.exec(
            select(CreativeReportDay).where(CreativeReportDay.tenant_id == tenant)
        ).all()
