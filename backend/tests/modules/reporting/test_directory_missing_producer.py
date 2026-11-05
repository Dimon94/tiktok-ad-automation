"""缺口与到期刷新沿原分片发布；在途覆盖只晋升需求，不重复创建。"""

from datetime import UTC, datetime

import pytest
from sqlmodel import select

from app.core.config import settings
from app.modules.ads.models import AdObject
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting.directory_refresh import refresh_report_directory
from app.modules.reporting.store_models import ReportSyncJob
from tests.modules.reporting.test_directory_refresh import directory_objects, prepared

KEY = "directory_service_v1"


@pytest.mark.parametrize("missing_id", [None, "a0", "a99"])
def test_only_chunk_with_proven_missing_is_urgent_without_repartition(
    session, sync_seed, monkeypatch, missing_id
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    job = prepared(session, sync_seed, day=now.date(), count=101)
    directory_objects(
        session, sync_seed, now, count=101, ages={("ad", i): 60 for i in range(101)}
    )
    if missing_id is not None:
        session.delete(
            session.get(AdObject, (sync_seed.tenant_id, "acc-2", "ad", missing_id))
        )
        session.flush()
    assert refresh_report_directory(session, job, now=now) == 2
    runs = session.exec(select(AdDirectoryRun)).all()
    ids = sorted(f"a{i}" for i in range(101))
    assert sorted(r.query["ids"] for r in runs) == sorted([ids[:100], ids[100:]])
    for run in runs:
        expected = "status_missing" if missing_id in run.query["ids"] else "status"
        assert run.query[KEY] == expected
    before = {(r.id, r.partition_key, r.created_at) for r in runs}
    assert refresh_report_directory(session, job, now=now) == 0
    assert {
        (r.id, r.partition_key, r.created_at)
        for r in session.exec(select(AdDirectoryRun)).all()
    } == before


@pytest.mark.parametrize("service", [None, "identity", "status"])
def test_pending_missing_promotes_existing_identity_without_new_run(
    session, sync_seed, monkeypatch, service
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    job = prepared(session, sync_seed)
    assert refresh_report_directory(session, job) == 3
    runs = session.exec(select(AdDirectoryRun)).all()
    for run in runs:
        run.query = {k: v for k, v in run.query.items() if k != KEY}
        if service:
            run.query = run.query | {KEY: service}
        session.add(run)
    session.flush()
    identities = {
        (r.id, r.partition_key, r.created_at, r.request_sequence, r.claim_generation)
        for r in runs
    }
    assert refresh_report_directory(session, job) == 0
    # 显式身份依赖必须保留素材补读意图；晋升调度不能降格为只读状态。
    # 生产既有合同已保留identity，旧断言误将其要求为status_missing。
    expected = "identity" if service == "identity" else "status_missing"
    assert all(r.query.get(KEY) == expected for r in runs)
    assert {
        (r.id, r.partition_key, r.created_at, r.request_sequence, r.claim_generation)
        for r in session.exec(select(AdDirectoryRun)).all()
    } == identities


def test_locked_pending_missing_still_covers_demand_without_duplicate(
    committed_sync_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    seed, factory = committed_sync_seed
    with factory() as session, session.begin():
        job = prepared(session, seed)
        job_id = job.id
        assert refresh_report_directory(session, job) == 3
        runs = session.exec(select(AdDirectoryRun)).all()
        for run in runs:
            run.query = run.query | {KEY: "status"}
            session.add(run)
        identities = {r.id for r in runs}
        locked_id = next(r.id for r in runs if r.kind == "ad")
    with factory() as holder, holder.begin():
        holder.exec(
            select(AdDirectoryRun)
            .where(AdDirectoryRun.id == locked_id)
            .with_for_update()
        ).one()
        with factory() as session, session.begin():
            job = session.get(ReportSyncJob, job_id)
            assert refresh_report_directory(session, job) == 0
    with factory() as session:
        runs = session.exec(select(AdDirectoryRun)).all()
        assert {r.id for r in runs} == identities
        assert all(
            r.query[KEY] == ("status" if r.id == locked_id else "status_missing")
            for r in runs
        )
