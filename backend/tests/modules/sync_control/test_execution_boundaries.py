"""真实 PG 验证网络边界；transport 替身仍从另一连接探测业务锁。"""

from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlmodel import select

from app.integrations.tiktok.contracts.report_reader import RawReportPage
from app.modules.reporting.jobs import enqueue_job, execute_job
from app.modules.reporting.store_models import AdReportDay, ReportSyncJob
from tests.modules.reporting.claiming import claim_jobs
from tests.modules.reporting.sync_conftest import ad_row

DAY = date(2026, 10, 5)


def _claim(factory, seed):
    with factory() as session:
        enqueue_job(
            session,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            kind="ad_day",
            tier="hot",
            advertiser_ids=["acc-1"],
            start=DAY,
            end=DAY,
            frozen_route={
                "tenant_id": str(seed.tenant_id),
                "bc_id": "bc-a",
                "connection_id": str(seed.connection.id),
                "channel": "OFFICIAL_API",
                "authorization_revision": 0,
                "adapter_contract_revision": "official-api-v1",
                "binding_revision": 0,
            },
            actor_id=seed.actor_id,
        )
        session.commit()
        job = claim_jobs(
            session, now=datetime.now(UTC) + timedelta(seconds=1), limit=1
        )[0]
        session.commit()
        return job


def test_report_transport_holds_neither_job_nor_account_lock(committed_sync_seed):
    seed, factory = committed_sync_seed
    job = _claim(factory, seed)

    class Reader:
        def ad_day(self, **arguments):
            with factory() as probe:
                # NOWAIT 在旧实现中立即失败，避免挂住测试来证明跨 HTTP 持锁。
                probe.exec(
                    select(ReportSyncJob)
                    .where(ReportSyncJob.id == job.id)
                    .with_for_update(nowait=True)
                ).one()
                assert (
                    probe.connection()
                    .execute(
                        text(
                            "SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"
                        ),
                        {"key": f"report:{seed.tenant_id}:acc-1:ad_day"},
                    )
                    .scalar_one()
                )
            return RawReportPage((ad_row("a1", DAY),), 1, 1, 1)

    assert (
        execute_job(
            factory, job_id=job.id, claim_token=job.claim_token, reader=Reader()
        )
        == "COMPLETE"
    )


def test_report_late_response_cannot_overwrite_new_claim(committed_sync_seed):
    seed, factory = committed_sync_seed
    job = _claim(factory, seed)
    newer_token = uuid4()

    class Reader:
        def ad_day(self, **arguments):
            with factory() as replacement:
                current = replacement.exec(
                    select(ReportSyncJob)
                    .where(ReportSyncJob.id == job.id)
                    .with_for_update(nowait=True)
                ).one()
                current.claim_token = newer_token
                current.claimed_until = datetime.now(UTC) + timedelta(minutes=4)
                replacement.commit()
            return RawReportPage((ad_row("old-response", DAY),), 1, 1, 1)

    assert (
        execute_job(
            factory, job_id=job.id, claim_token=job.claim_token, reader=Reader()
        )
        == "FAILED"
    )
    with factory() as session:
        current = session.get(ReportSyncJob, job.id)
        assert current.claim_token == newer_token
        assert current.status == "RUNNING"
        assert not session.exec(
            select(AdReportDay).where(AdReportDay.tenant_id == seed.tenant_id)
        ).all()


def test_report_response_after_binding_revocation_is_not_published(committed_sync_seed):
    from app.modules.accounts.connection_models import BCConnectionBinding

    seed, factory = committed_sync_seed
    job = _claim(factory, seed)

    class Reader:
        def ad_day(self, **arguments):
            with factory() as revoke:
                binding = revoke.exec(
                    select(BCConnectionBinding).where(
                        BCConnectionBinding.tenant_id == seed.tenant_id,
                        BCConnectionBinding.bc_id == "bc-a",
                    )
                ).one()
                binding.revision += 1
                revoke.commit()
            return RawReportPage((ad_row("revoked-response", DAY),), 1, 1, 1)

    assert (
        execute_job(
            factory, job_id=job.id, claim_token=job.claim_token, reader=Reader()
        )
        == "FAILED"
    )
    with factory() as session:
        assert not session.exec(
            select(AdReportDay).where(AdReportDay.tenant_id == seed.tenant_id)
        ).all()


def test_report_owner_epoch_is_refreshed_even_when_scope_is_in_identity_map(
    committed_sync_seed,
):
    from app.modules.sync_control.models import SyncScope

    seed, factory = committed_sync_seed
    job = _claim(factory, seed)

    @contextmanager
    def retained_factory():
        with factory() as session:
            retained = session.exec(
                select(SyncScope).where(SyncScope.tenant_id == seed.tenant_id)
            ).all()
            yield session
            assert (
                retained
            )  # ORM identity-map objects remain strongly referenced across HTTP.

    class Reader:
        def ad_day(self, **_arguments):
            with factory() as replacement:
                scope = replacement.exec(
                    select(SyncScope).where(SyncScope.tenant_id == seed.tenant_id)
                ).one()
                scope.owner_epoch += 1
                replacement.commit()
            return RawReportPage((ad_row("old-owner", DAY),), 1, 1, 1)

    assert (
        execute_job(
            retained_factory,
            job_id=job.id,
            claim_token=job.claim_token,
            reader=Reader(),
        )
        == "FAILED"
    )
    with factory() as session:
        assert not session.exec(
            select(AdReportDay).where(AdReportDay.tenant_id == seed.tenant_id)
        ).all()
        assert session.get(ReportSyncJob, job.id).status == "RUNNING"


def test_balance_http_releases_row_lock_and_missing_accounts_remain_pending(
    monkeypatch, committed_sync_seed, redis_client
):
    from app.core.db import engine
    from app.modules.reporting import balance_tasks
    from app.modules.reporting.store_models import ReportBalanceRun
    from tests.modules.reporting.test_balance_refresh import (
        claim_args,
        claim_balance,
        create_run,
    )

    seed, factory = committed_sync_seed
    run_id = create_run(seed, factory, ["acc-1"])
    monkeypatch.setattr(balance_tasks.settings, "ADS_SYNC_ENABLED", True)
    payload = claim_balance(seed, factory, redis_client, run_id)

    @contextmanager
    def gateway(**_kwargs):
        def read(advertisers, *, page):
            assert advertisers == ("acc-1",) and page == 1
            with factory() as probe:
                probe.exec(
                    select(ReportBalanceRun)
                    .where(ReportBalanceRun.id == run_id)
                    .with_for_update(nowait=True)
                ).one()
            # 平台未返回账户并不等于账户余额为零或全部已经新鲜。
            return {}, None

        yield SimpleNamespace(ads=SimpleNamespace(read_balance_page=read))

    monkeypatch.setattr(balance_tasks, "open_tiktok_gateway", gateway)
    assert (
        balance_tasks.run_balance(
            database_engine=engine,
            redis_client=redis_client,
            context=seed.context,
            run_id=run_id,
            page=1,
            **claim_args(payload),
        )
        == "WAITING"
    )
    with factory() as session:
        run = session.get(ReportBalanceRun, run_id)
        assert run.status != "COMPLETE"
        assert run.remaining_advertiser_ids == ["acc-1"]


def test_screening_http_runs_after_planner_transaction_exits(
    monkeypatch, committed_sync_seed, redis_client
):
    from app.core.db import engine
    from app.modules.reporting import job_tasks, planner, planning_steps
    from app.modules.reporting.store_models import AdSyncBc

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session:
        planner.ensure_bc_plans(session, now=now)
        session.commit()
    calls = []

    class Reader:
        def bc_spend(self, **arguments):
            calls.append(arguments)
            with factory() as probe:
                probe.exec(
                    select(AdSyncBc)
                    .where(
                        AdSyncBc.tenant_id == seed.tenant_id, AdSyncBc.bc_id == "bc-a"
                    )
                    .with_for_update(nowait=True)
                ).one()
            return RawReportPage((), 1, 0, 0)

    @contextmanager
    def reader_for(**_kwargs):
        yield Reader()

    monkeypatch.setattr(job_tasks, "_reader_for", reader_for)
    planning_steps.plan_step(
        engine, redis_client, (seed.tenant_id, "bc-a"), now=now, allow_backfill=False
    )
    assert len(calls) == 1


@pytest.mark.parametrize("when", ["before", "during", "reclaimed"])
def test_permanent_directory_revocation_ends_only_the_matching_execution(
    monkeypatch, committed_sync_seed, redis_client, when
):
    from app.core.config import settings
    from app.core.db import engine
    from app.core.errors import DomainError
    from app.modules.accounts.connection_models import BCConnectionBinding
    from app.modules.ads import tasks
    from app.modules.ads.directory_dispatch import directory_step_key
    from app.modules.ads.sync_models import AdDirectoryRun
    from app.modules.sync_control.directory_demand import directory_endpoint
    from app.modules.sync_control.directory_dispatch import dispatch_directories_due
    from app.modules.sync_control.models import (
        SyncExecutionScope,
        SyncRequirement,
        SyncSchedule,
    )
    from app.modules.sync_control.occupancy import occupancy_keys
    from tests.modules.sync_control.test_schedule_fencing import _directory

    seed, factory = committed_sync_seed
    monkeypatch.setattr(tasks.settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(tasks, "_deliver_committed_work", lambda *_args: None)
    with factory() as session, session.begin():
        run = _directory(
            session,
            seed,
            query_extra={
                "advertiser_id": "acc-1",
                "page": 1,
                "page_size": 100,
                "include_deleted": False,
            },
        )
        identity = run.id
        assert (
            dispatch_directories_due(
                session, redis_client, now=datetime.now(UTC), limit=1
            )
            == 1
        )
        generation, reservation = run.claim_generation, directory_step_key(run)
        endpoint = directory_endpoint(run)
        keys = occupancy_keys(
            settings.TIKTOK_APP_ID,
            endpoint,
            seed.tenant_id,
            run.advertiser_id,
            endpoint,
        )
        assert all(redis_client.zscore(key, reservation) is not None for key in keys)
        if when == "before":
            binding = session.exec(
                select(BCConnectionBinding).where(
                    BCConnectionBinding.tenant_id == seed.tenant_id,
                    BCConnectionBinding.bc_id == "bc-a",
                )
            ).one()
            binding.revision += 1

    @contextmanager
    def gateway(**_kwargs):
        assert when != "before", "发送前撤权不应进入HTTP"

        def read(_query):
            with factory() as session, session.begin():
                binding = session.exec(
                    select(BCConnectionBinding).where(
                        BCConnectionBinding.tenant_id == seed.tenant_id,
                        BCConnectionBinding.bc_id == "bc-a",
                    )
                ).one()
                binding.revision += 1
                if when == "reclaimed":
                    assert (
                        dispatch_directories_due(
                            session,
                            redis_client,
                            now=datetime.now(UTC) + timedelta(minutes=5),
                            limit=1,
                        )
                        == 1
                    )
            raise DomainError("route_binding_changed", "old authorization revoked")

        yield SimpleNamespace(
            ads=SimpleNamespace(read_primary_page=read, read_page=read)
        )

    monkeypatch.setattr(tasks, "open_tiktok_gateway", gateway)
    result = tasks.run_directory_step(
        database_engine=engine,
        redis_client=redis_client,
        context=seed.context,
        run_id=identity,
        claim_generation=generation,
    )
    with factory() as session:
        run = session.get(AdDirectoryRun, identity)
        child = session.exec(
            select(SyncExecutionScope).where(
                SyncExecutionScope.directory_run_id == identity
            )
        ).one()
        requirement = session.get(SyncRequirement, child.requirement_id)
        head = session.get(SyncSchedule, (child.scope_id, "directory"))
        assert requirement.satisfied_revision == 0
        if when == "reclaimed":
            assert run.claim_generation == generation + 1 and run.status != "FAILED"
            assert (
                head.lease_execution_generation == generation + 1
                and head.lease_token is not None
            )
            assert all(
                redis_client.zscore(key, directory_step_key(run)) is not None
                for key in keys
            )
            assert child.status == "PENDING"
        else:
            assert result == "FAILED" and run.status == "FAILED"
            assert (
                run.error_code == "route_binding_changed"
                and child.status == "SUPERSEDED"
            )
            assert not head.active and head.wait_reason.startswith("TERMINAL:")

    assert all(redis_client.zscore(key, reservation) is None for key in keys)
