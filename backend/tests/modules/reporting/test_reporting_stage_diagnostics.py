"""阶段汇总旁观业务：一次调用、原错透传、提交后确认、白名单不含正文。"""

import json
import logging
from uuid import uuid4

import pytest

from app.core import diagnostics as runtime
from app.modules.reporting import diagnostics
from tests.modules.reporting.claiming import claim_jobs


def records(caplog):
    return [
        json.loads(r.message.split(" ", 1)[1])
        for r in caplog.records
        if r.message.startswith("runtime_trace ")
        and json.loads(r.message.split(" ", 1)[1])["event"] == "reporting_stage_summary"
    ]


@pytest.fixture
def capture(caplog, monkeypatch):
    monkeypatch.setattr(runtime.logger, "disabled", False)
    caplog.set_level(logging.INFO, logger=runtime.logger.name)
    return caplog


def test_one_summary_preserves_calls_and_separates_commit_from_phase(capture):
    calls = []

    @diagnostics.summarize_job
    def step(*, job_id):
        assert job_id is not None
        diagnostics.set_job_scope("ad_day", 5)
        with diagnostics.stage("read"):
            calls.append("read")
        with diagnostics.stage("financial_publish"):
            calls.append("publish")
        with diagnostics.stage("job_commit"):
            calls.append("commit")
        diagnostics.committed("COMPLETE")
        with diagnostics.stage("refill"):
            calls.append("refill")
        return "COMPLETE"

    job_id = uuid4()
    assert step(job_id=job_id) == "COMPLETE"
    assert calls == ["read", "publish", "commit", "refill"]
    (row,) = records(capture)
    assert row["job_id"] == str(job_id)
    assert row["job_kind"] == "ad_day" and row["account_count"] == 5
    assert row["job_commit_confirmed"] is True
    assert row["job_commit_result"] == row["outcome"] == "COMPLETE"
    assert all(
        item["calls"] == 1 and item["failures"] == 0 for item in row["stages"].values()
    )
    assert row["stages"]["job_commit"]["elapsed_ms"] >= 0


@pytest.mark.parametrize("failed_stage", ["financial_publish", "job_commit"])
def test_original_exception_and_uncommitted_progress_never_report_success(
    capture, failed_stage
):
    error = RuntimeError("credential=private-token SQL body https://secret.example")

    @diagnostics.summarize_job
    def step(*, job_id):
        assert job_id is not None
        with diagnostics.stage(failed_stage):
            raise error
        diagnostics.committed("COMPLETE")

    with pytest.raises(RuntimeError) as caught:
        step(job_id=uuid4())
    assert caught.value is error
    (row,) = records(capture)
    assert row["job_commit_confirmed"] is False
    assert row["job_commit_result"] is None and row["outcome"] == "RAISED"
    assert row["stages"][failed_stage]["failures"] == 1
    assert row["error_type"] == "RuntimeError"
    assert "private-token" not in capture.text
    assert "SQL body" not in capture.text and "secret.example" not in capture.text


def test_unknown_fields_and_next_task_do_not_leak(capture):
    @diagnostics.summarize_job
    def step(*, job_id):
        assert job_id is not None
        diagnostics.set_job_scope("private-token", 2)
        diagnostics.counts(rows=3, private_token=99, accounts="private-token")
        with diagnostics.stage("private-token"):
            pass
        with diagnostics.stage("read"):
            pass
        return "private-token"

    step(job_id=uuid4())
    step(job_id=uuid4())
    rows = records(capture)
    assert len(rows) == 2
    assert all(row["stages"]["read"]["calls"] == 1 for row in rows)
    assert all(row["counts"] == {"rows": 3} for row in rows)
    assert all(row["outcome"] == "UNKNOWN" for row in rows)
    assert "private-token" not in capture.text


def test_handled_refill_failure_keeps_prior_committed_result(capture):
    @diagnostics.summarize_job
    def step(*, job_id):
        assert job_id is not None
        diagnostics.committed("COMPLETE")
        try:
            with diagnostics.stage("refill"):
                raise ValueError("private-refill-body")
        except ValueError:
            pass
        return "COMPLETE"

    assert step(job_id=uuid4()) == "COMPLETE"
    (row,) = records(capture)
    assert row["job_commit_confirmed"] and row["outcome"] == "COMPLETE"
    assert row["stages"]["refill"]["failures"] == 1
    assert "private-refill-body" not in capture.text


@pytest.mark.parametrize("failure", [None, "reconciliation", "commit"])
def test_real_job_summary_preserves_atomic_publication_and_one_call(
    committed_sync_seed, redis_client, monkeypatch, capture, failure
):
    from collections import Counter
    from contextlib import contextmanager
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import event
    from sqlmodel import Session, select

    from app.core.db import engine
    from app.modules.ads.sync_models import AdDirectoryRun
    from app.modules.reporting import directory_refresh, job_tasks, jobs, publish
    from app.modules.reporting.store_models import AdReportDay, ReportSyncJob
    from tests.modules.reporting.sync_conftest import ad_row
    from tests.modules.reporting.test_jobs import FakeReader, _route

    seed, factory = committed_sync_seed
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    today = datetime.now(UTC).date()
    with factory() as db:
        identity = jobs.enqueue_job(
            db,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            kind="ad_day",
            tier="hot",
            advertiser_ids=["acc-2"],
            start=today,
            end=today,
            frozen_route=_route(seed),
            actor_id=seed.actor_id,
        )
        db.commit()
        job = claim_jobs(db, now=datetime.now(UTC) + timedelta(seconds=1), limit=1)[0]
        token = job.claim_token
        db.commit()
    reader = FakeReader(
        pages={(("acc-2",), (), 1): [ad_row("private-ad-name", today, adv="acc-2")]}
    )
    calls = Counter()
    originals = {
        "publish": publish.publish_ad_day,
        "directory": jobs.refresh_report_directory,
        "reconciliation": publish._refresh_reconciliation_sums,
        "request": directory_refresh.request_directory,
        "refill": job_tasks._dispatch_pending,
    }
    error = RuntimeError("private-original-error-do-not-log")

    def spy(name):
        def call(*args, **kwargs):
            calls[name] += 1
            if name == "reconciliation" and failure == name:
                # 真SQL错误由其它回归覆盖；这里检验调用抛错仍穿透原事务。
                with diagnostics.stage("reconciliation"):
                    raise error
            return originals[name](*args, **kwargs)

        return call

    for module, attribute, name in [
        (publish, "publish_ad_day", "publish"),
        (jobs, "refresh_report_directory", "directory"),
        (publish, "_refresh_reconciliation_sums", "reconciliation"),
        (directory_refresh, "request_directory", "request"),
        (job_tasks, "_dispatch_pending", "refill"),
    ]:
        monkeypatch.setattr(module, attribute, spy(name))

    @contextmanager
    def open_reader(**_kwargs):
        yield reader

    monkeypatch.setattr(job_tasks, "_reader_for", open_reader)

    def fail_commit(db):
        if failure == "commit" and any(
            isinstance(row, ReportSyncJob)
            and row.id == identity
            and row.status == "COMPLETE"
            for row in db.identity_map.values()
        ):
            raise error

    event.listen(Session, "before_commit", fail_commit)
    try:
        arguments = {
            "database_engine": engine,
            "redis_client": redis_client,
            "job_id": identity,
            "claim_token": token,
        }
        if failure:
            with pytest.raises(RuntimeError) as caught:
                job_tasks.run_job_step(**arguments)
            assert caught.value is error
        else:
            assert job_tasks.run_job_step(**arguments) == "COMPLETE"
    finally:
        event.remove(Session, "before_commit", fail_commit)
    assert reader.calls == [(("acc-2",), (), 1)]
    assert calls["publish"] == calls["reconciliation"] == calls["refill"] == 1
    assert (
        calls["directory"]
        == calls["request"]
        == (0 if failure == "reconciliation" else 1)
    )
    (row,) = records(capture)
    assert row["job_commit_confirmed"] is (failure is None)
    assert row["job_commit_result"] == ("COMPLETE" if failure is None else None)
    assert row["stages"]["read"]["calls"] == 1
    assert row["stages"]["publish_state"]["calls"] == 1
    assert row["stages"]["publish_flush"]["calls"] == 1
    assert row["stages"]["reconciliation"]["calls"] == 1
    assert row["stages"]["refill_dispatch"]["calls"] == 1
    if failure != "reconciliation":
        for name in [
            "directory_lock",
            "directory_rules",
            "directory_pending",
            "directory_facts_query",
            "directory_promote",
            "directory_request",
        ]:
            assert row["stages"][name]["calls"] == 1
    with factory() as db:
        facts = db.exec(
            select(AdReportDay).where(AdReportDay.tenant_id == seed.tenant_id)
        ).all()
        runs = db.exec(
            select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == seed.tenant_id)
        ).all()
        assert len(facts) == (0 if failure else 1)
        assert len(runs) == (0 if failure else 3)
        assert db.get(ReportSyncJob, identity).status == (
            "RUNNING" if failure else "COMPLETE"
        )
    assert "private-ad-name" not in json.dumps(row)
    assert "private-original-error-do-not-log" not in json.dumps(row)
    assert "frozen_route" not in json.dumps(row)
