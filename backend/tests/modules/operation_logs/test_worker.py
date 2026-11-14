import json
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlmodel import col, delete, select
from urllib3.response import HTTPResponse

from app.integrations.tiktok.adapters.sdk_report_reader import SdkReportReader
from app.integrations.tiktok.gateway import _sdk_report_send
from app.integrations.tiktok.sdk import official_client
from app.modules.operation_logs.models import (
    OperationCoverage,
    OperationExport,
    OperationLogEvent,
)
from app.modules.reporting.jobs import enqueue_job, execute_job
from app.modules.reporting.store_models import ReportSyncJob, ReportSyncState
from tests.modules.operation_logs.test_parser import export
from tests.modules.reporting.claiming import claim_jobs
from tests.modules.reporting.sync_conftest import (
    committed_sync_seed as committed_sync_seed,
)
from tests.modules.reporting.test_jobs import _route
from tests.modules.sync_control.test_archive import ObjectTransport


@pytest.fixture
def log_seed(committed_sync_seed, monkeypatch):
    from cryptography.fernet import Fernet

    # 加密归档复用持久主密钥合同；测试也提供独立有效密钥，不绕过加密。
    monkeypatch.setattr(
        "app.core.config.settings.CONNECTION_ENCRYPTION_KEY",
        Fernet.generate_key().decode(),
    )
    seed, factory = committed_sync_seed
    try:
        yield seed, factory
    finally:
        with factory() as session:
            ids = select(OperationExport.id).where(
                OperationExport.tenant_id == seed.tenant_id
            )
            session.execute(
                delete(OperationLogEvent).where(
                    col(OperationLogEvent.export_id).in_(ids)
                )
            )
            session.execute(
                delete(OperationCoverage).where(
                    OperationCoverage.tenant_id == seed.tenant_id
                )
            )
            session.execute(
                delete(OperationExport).where(
                    OperationExport.tenant_id == seed.tenant_id
                )
            )
            session.commit()


@pytest.mark.parametrize("interruption", ["none", "archive", "revocation"])
def test_export_restarts_each_stage_and_publishes_only_after_archive(
    log_seed, monkeypatch, interruption
):
    seed, factory = log_seed
    responses = [
        {"task_id": "export-test"},
        {"status": "PROCESSING"},
        {"status": "SUCCESS"},
        {"status": "SUCCESS", "changelog": export()},
    ]
    calls = []

    def wire(_pool, method, url, **_kwargs):
        calls.append((method, url))
        if len(calls) == 4 and interruption == "revocation":
            from app.modules.accounts.models import BCAccountAccess

            with factory() as revoked:
                access = revoked.get(
                    BCAccountAccess,
                    (seed.tenant_id, "bc-a", "acc-1", seed.connection.id),
                )
                access.active = False
                revoked.add(access)
                revoked.commit()
        return HTTPResponse(
            body=json.dumps(
                {"code": 0, "data": responses.pop(0), "request_id": "test"}
            ).encode(),
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", wire)
    object_wire = ObjectTransport()
    object_wire.fail_put = interruption == "archive"
    monkeypatch.setattr(
        "app.modules.operation_logs.archive.make_s3", lambda _config: object_wire
    )
    monkeypatch.setattr("app.core.config.settings.S3_BUCKET", "existing-material-test")

    @contextmanager
    def scope(*_args):
        yield

    with factory() as session:
        job_id = enqueue_job(
            session,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            kind="operation_log",
            tier="history",
            advertiser_ids=["acc-1"],
            start=date(2026, 10, 5),
            end=date(2026, 10, 5),
            frozen_route=_route(seed),
            actor_id=seed.actor_id,
        )
        session.commit()
    for step in range(4):
        with factory() as session:
            job = session.get(ReportSyncJob, job_id)
            claimed = claim_jobs(
                session,
                now=max(job.due_at, datetime.now(UTC)) + timedelta(seconds=1),
                limit=1,
            )
            assert len(claimed) == 1
            token = claimed[0].claim_token
            session.commit()
        # 模拟每步退出进程，重新打开官方SDK与执行会话，task_id必须来自数据库。
        with official_client(access_token="synthetic") as client:
            reader = SdkReportReader(
                send=_sdk_report_send(
                    client, scope, datetime.now(UTC) + timedelta(seconds=30)
                )
            )
            result = execute_job(
                factory, job_id=job_id, claim_token=token, reader=reader
            )
        with factory() as debug:
            snapshot = debug.get(ReportSyncJob, job_id)
            expected = (
                "FAILED"
                if step == 3 and interruption == "revocation"
                else "COMPLETE"
                if step == 3 and interruption == "none"
                else "WAITING"
            )
            assert result == expected, (
                snapshot.error_code,
                snapshot.progress,
            )
        with factory() as session:
            job = session.get(ReportSyncJob, job_id)
            assert job.claim_token is None
            assert job.request_count == step + 1
            coverage = session.get(
                OperationCoverage, (seed.tenant_id, "acc-1", date(2026, 10, 5))
            )
            assert (coverage is not None) == (step == 3 and interruption == "none")
            assert not session.exec(
                select(ReportSyncState).where(
                    ReportSyncState.tenant_id == seed.tenant_id,
                    ReportSyncState.report_kind == "operation_log",
                )
            ).all()
    assert len(object_wire.objects) == (
        2 if interruption == "none" else 0
    )  # 未完成或失效授权不发布原文/覆盖
    assert len(calls) == 4


def test_large_export_split_remains_split_under_new_demand(log_seed):
    from app.core.errors import DomainError
    from app.modules.reporting.jobs import _handle_error, request_time

    seed, factory = log_seed
    with factory() as session:
        job_id = enqueue_job(
            session,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            kind="operation_log",
            tier="history",
            advertiser_ids=["acc-1"],
            start=date(2026, 10, 1),
            end=date(2026, 10, 29),
            frozen_route=_route(seed),
            actor_id=seed.actor_id,
            execution_window_days=29,
        )
        session.commit()
        job = claim_jobs(
            session, now=datetime.now(UTC) + timedelta(seconds=1), limit=1
        )[0]
        assert job.id == job_id
        assert (
            _handle_error(
                session, job, DomainError("operation_log_export_too_large", "synthetic")
            )
            == "SPLIT"
        )
        requested = request_time(job)
        session.commit()
        children = session.exec(
            select(ReportSyncJob).where(
                ReportSyncJob.tenant_id == seed.tenant_id, ReportSyncJob.id != job_id
            )
        ).all()
        assert len(children) == 2
        assert all(c.progress["execution_window_days"] == 15 for c in children)
        enqueue_job(
            session,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            kind="operation_log",
            tier="history",
            advertiser_ids=["acc-1"],
            start=job.start_date,
            end=job.end_date,
            frozen_route=_route(seed),
            actor_id=seed.actor_id,
            requested_at=requested,
            execution_window_days=29,
        )
        session.flush()
        active = session.exec(
            select(ReportSyncJob).where(
                ReportSyncJob.tenant_id == seed.tenant_id,
                ReportSyncJob.status.in_(["QUEUED", "WAITING", "RUNNING"]),
            )
        ).all()
        assert len(active) == 2
        assert max((c.end_date - c.start_date).days for c in active) <= 14


@pytest.mark.parametrize("phase", ["CHECK", "DOWNLOAD"])
def test_missing_export_rebuilds_original_window_with_finite_budget(log_seed, phase):
    from types import SimpleNamespace

    from app.core.errors import DomainError
    from app.modules.operation_logs.worker import run_step

    seed, factory = log_seed

    def unavailable(**_kwargs):
        raise DomainError("operation_log_task_unavailable", "synthetic")

    creates = []

    def create(**kwargs):
        creates.append(kwargs)
        return "replacement-export"

    reader = SimpleNamespace(
        change_log_check=unavailable,
        change_log_download=unavailable,
        change_log_create=create,
    )
    with factory() as session:
        job_id = enqueue_job(
            session,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            kind="operation_log",
            tier="history",
            advertiser_ids=["acc-1"],
            start=date(2026, 10, 5),
            end=date(2026, 10, 5),
            frozen_route=_route(seed),
            actor_id=seed.actor_id,
        )
        job = session.get(ReportSyncJob, job_id)
        job.attempts = 5
        job.progress = job.progress | {
            "operation_log": {
                "phase": phase,
                "task_id": "expired-export",
                "create_attempts": 2,
                "task_ids": ["expired-export"],
            }
        }
        kwargs = {
            "session": session,
            "job": job,
            "reader": reader,
            "accounts": {"acc-1": SimpleNamespace(timezone="Asia/Hong_Kong")},
            "external": lambda _call: pytest.fail(
                "Unfinished export must not be archived"
            ),
        }
        assert run_step(**kwargs) is None
        assert job.progress["operation_log"]["phase"] == "CREATE"
        assert job.attempts == 0
        assert run_step(**kwargs) is None
        state = job.progress["operation_log"]
        assert state["phase"] == "CHECK"
        assert state["task_ids"] == ["expired-export", "replacement-export"]
        assert creates[0]["start"].date() == date(2026, 10, 4)  # 香港当地5日的UTC起点
        assert job.start_date == job.end_date == date(2026, 10, 5)
        assert run_step(**kwargs) is None
        with pytest.raises(DomainError) as caught:
            run_step(**kwargs)
        assert caught.value.code == "operation_log_create_exhausted"
        assert len(creates) == 1


def test_yesterday_create_stops_at_local_midnight(session, monkeypatch):
    from types import SimpleNamespace

    from app.modules.operation_logs.worker import run_step

    now = datetime(2026, 11, 10, 12, tzinfo=UTC)
    monkeypatch.setattr(
        "app.modules.operation_logs.worker._database_now", lambda _: now
    )
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return "export"

    job = SimpleNamespace(
        advertiser_ids=["acc-1"],
        start_date=date(2026, 11, 9),
        end_date=date(2026, 11, 9),
        progress={},
    )
    run_step(
        session,
        job,
        SimpleNamespace(change_log_create=create),
        {"acc-1": SimpleNamespace(timezone="Asia/Hong_Kong")},
        external=lambda fn: fn(),
    )
    assert calls[0]["start"] == datetime(2026, 11, 8, 16, tzinfo=UTC)
    assert calls[0]["end"] == datetime(2026, 11, 9, 16, tzinfo=UTC)
