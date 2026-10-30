"""真实PG生命周期与事务竞态；仅对象存储HTTP边界替身，无外部写入。"""

from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from io import BytesIO
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import delete, text
from sqlmodel import select

from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting.store_models import (
    ReportBalanceRun,
    ReportSyncJob,
    ReportSyncPage,
)
from app.modules.sync_control.archive import (
    ArchiveDeferred,
    archive_run,
    freeze_archive,
    prune_archive,
    retention_candidates,
)
from app.modules.sync_control.archive_storage import (
    ArchiveStorageError,
    S3RunArchive,
    archive_key,
)
from app.modules.sync_control.models import SyncExecutionScope
from app.modules.sync_control.report_demand import adopt_report_job
from tests.modules.sync_control.test_cutover import legacy_job

NOW = datetime(2026, 10, 31, 12, tzinfo=UTC)


class ObjectTransport:
    def __init__(self):
        self.objects = {}
        self.after_put = None
        self.fail_put = False
        self.fail_marker = False
        self.corrupt_get = False
        self.put_count = 0

    def put_object(self, **kwargs):
        self.put_count += 1
        if self.fail_put or (self.fail_marker and "/committed/" in kwargs["Key"]):
            raise OSError("transport unavailable")
        key = kwargs["Key"]
        if key in self.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        assert kwargs["IfNoneMatch"] == "*"
        self.objects[key] = (
            kwargs["Body"].read() if hasattr(kwargs["Body"], "read") else kwargs["Body"]
        )
        if self.after_put:
            self.after_put()

    def list_objects_v2(self, **kwargs):
        return {
            "Contents": [
                {"Key": k}
                for k in sorted(self.objects)
                if k.startswith(kwargs["Prefix"])
            ][: kwargs["MaxKeys"]]
        }

    def get_object(self, **kwargs):
        payload = self.objects[kwargs["Key"]]
        if self.corrupt_get:
            payload = b"x" * len(payload)
        return {"ContentLength": len(payload), "Body": BytesIO(payload)}


def terminal_report(session, seed, *, days=91, children=False):
    job, _ = legacy_job(session, seed)
    session.execute(delete(ReportSyncPage).where(ReportSyncPage.job_id == job.id))
    if children:
        adopt_report_job(session, job)
        for child in session.exec(
            select(SyncExecutionScope).where(SyncExecutionScope.report_job_id == job.id)
        ).all():
            child.status = "PUBLISHED"
            child.observed_at = NOW - timedelta(days=days)
            child.published_version = 2
    job.status, job.finished_at = "COMPLETE", NOW - timedelta(days=days)
    session.flush()
    return job


def frozen_for(session, job):
    return freeze_archive(
        session, tenant_id=job.tenant_id, kind="report", run_id=job.id, now=NOW
    )


def persist(storage, frozen):
    return storage.persist(
        tenant_id=frozen.tenant_id,
        kind=frozen.kind,
        run_id=frozen.run_id,
        payload=frozen.payload,
        sha256_digest=frozen.sha256,
        size=frozen.size,
    )


@pytest.mark.parametrize(
    "days,eligible", [(7, False), (89, False), (90, False), (91, True)]
)
def test_terminal_summary_90_day_boundary(session, sync_seed, days, eligible):
    job = terminal_report(session, sync_seed, days=days)
    candidates = retention_candidates(
        session, tenant_id=job.tenant_id, kind="report", now=NOW
    )
    assert bool(candidates) is eligible
    if eligible:
        frozen_for(session, job).close()
    else:
        with pytest.raises(ArchiveDeferred, match="online_retention"):
            frozen_for(session, job)


def test_summary_children_scope_and_versions_are_durable_before_delete(
    session, sync_seed
):
    job = terminal_report(session, sync_seed, children=True)
    frozen = frozen_for(session, job)
    try:
        wire = ObjectTransport()
        receipt = persist(S3RunArchive(wire, "private-test"), frozen)
        body = wire.objects[archive_key(job.tenant_id, "report", job.id, frozen.sha256)]
        assert b'"frozen_revision":1' in body and b'"published_version":2' in body
        assert (
            b'"bucket_key"' in body
            and b'"frozen_route"' in body
            and b'"audit_reference"' in body
        )
        assert sha256(body).hexdigest() == frozen.sha256
        result = prune_archive(session, frozen=frozen, receipt=receipt, now=NOW)
        assert result.deleted and result.archived
        session.expire_all()
        assert session.get(ReportSyncJob, frozen.run_id) is None
    finally:
        frozen.close()


@pytest.mark.parametrize("failure", ["put", "verify"])
def test_archive_transport_failure_keeps_original(session, sync_seed, failure):
    job = terminal_report(session, sync_seed, children=True)
    frozen = frozen_for(session, job)
    try:
        wire = ObjectTransport()
        wire.fail_put, wire.corrupt_get = failure == "put", failure == "verify"
        with pytest.raises((OSError, ArchiveStorageError)):
            persist(S3RunArchive(wire, "private-test"), frozen)
        assert session.get(ReportSyncJob, job.id) is not None
        assert session.exec(
            select(SyncExecutionScope).where(SyncExecutionScope.report_job_id == job.id)
        ).first()
    finally:
        frozen.close()


def test_foreign_tenant_cannot_freeze_or_upload(session, sync_seed):
    job = terminal_report(session, sync_seed)
    with pytest.raises(ArchiveDeferred, match="run_not_found"):
        freeze_archive(
            session, tenant_id=uuid4(), kind="report", run_id=job.id, now=NOW
        )


def test_source_change_during_upload_retains_online_run(committed_sync_seed):
    seed, factory = committed_sync_seed
    with factory() as db:
        job = terminal_report(db, seed)
        run_id = job.id
        db.commit()
    wire = ObjectTransport()

    def change_source():
        with factory() as db:
            # NOWAIT证明HTTP阶段没有持有父运行事务锁。
            row = db.exec(
                select(ReportSyncJob)
                .where(ReportSyncJob.id == run_id)
                .with_for_update(nowait=True)
            ).one()
            row.error_code = "late_audit_update"
            db.commit()

    wire.after_put = change_source
    result = archive_run(
        factory,
        S3RunArchive(wire, "private-test"),
        tenant_id=seed.tenant_id,
        kind="report",
        run_id=run_id,
        now=NOW,
    )
    assert not result.deleted and result.reason == "source_changed"
    with factory() as db:
        assert db.get(ReportSyncJob, run_id).error_code == "late_audit_update"


def test_db_failure_after_verified_upload_is_idempotent(committed_sync_seed):
    seed, factory = committed_sync_seed
    with factory() as db:
        job = terminal_report(db, seed, children=True)
        run_id = job.id
        db.commit()
    wire, calls = ObjectTransport(), 0
    storage = S3RunArchive(wire, "private-test")

    def fail_commit_factory():
        nonlocal calls
        calls += 1
        db = factory()
        if calls == 2:

            def fail():
                raise RuntimeError("injected commit failure")

            db.commit = fail
        return db

    with pytest.raises(RuntimeError, match="injected commit"):
        archive_run(
            fail_commit_factory,
            storage,
            tenant_id=seed.tenant_id,
            kind="report",
            run_id=run_id,
            now=NOW,
        )
    with factory() as db:
        assert db.get(ReportSyncJob, run_id) is not None
    result = archive_run(
        factory,
        storage,
        tenant_id=seed.tenant_id,
        kind="report",
        run_id=run_id,
        now=NOW,
    )
    assert result.deleted and len(wire.objects) == 2


def test_more_than_256_children_stream_and_delete(session, sync_seed):
    job, _ = legacy_job(session, sync_seed)
    session.execute(delete(ReportSyncPage).where(ReportSyncPage.job_id == job.id))
    job.start_date, job.end_date = date(2025, 1, 1), date(2025, 12, 1)
    adopt_report_job(session, job)
    for child in session.exec(
        select(SyncExecutionScope).where(SyncExecutionScope.report_job_id == job.id)
    ).all():
        child.status = "SUPERSEDED"
    job.status, job.finished_at = "COMPLETE", NOW - timedelta(days=91)
    session.flush()
    frozen = frozen_for(session, job)
    try:
        assert frozen.rows > 256
        receipt = persist(S3RunArchive(ObjectTransport(), "private-test"), frozen)
        assert prune_archive(session, frozen=frozen, receipt=receipt, now=NOW).deleted
    finally:
        frozen.close()


def directory(session, seed, *, published=None, status="FAILED"):
    from tests.modules.reporting.test_jobs import _route

    row = AdDirectoryRun(
        tenant_id=seed.tenant_id,
        actor_id=seed.actor_id,
        advertiser_id="acc-1",
        bc_id="bc-a",
        connection_id=seed.connection.id,
        frozen_route=_route(seed),
        partition_key=uuid4().hex * 2,
        kind="campaign",
        ad_type="REGULAR",
        query={"ids": [], "parent_ids": [], "include_deleted": True},
        status=status,
        published_version=published,
        completed_at=NOW - timedelta(days=91),
    )
    session.add(row)
    session.flush()
    return row


def archive_directory(session, row):
    frozen = freeze_archive(
        session, tenant_id=row.tenant_id, kind="directory", run_id=row.id, now=NOW
    )
    try:
        receipt = persist(S3RunArchive(ObjectTransport(), "private-test"), frozen)
        return prune_archive(session, frozen=frozen, receipt=receipt, now=NOW)
    finally:
        frozen.close()


def test_directory_latest_proof_kept_old_fully_covered_proof_removed(
    session, sync_seed
):
    old = directory(session, sync_seed, published=1, status="COMPLETE")
    newer = directory(session, sync_seed, published=2, status="COMPLETE")
    assert archive_directory(session, old).deleted
    result = archive_directory(session, newer)
    assert (
        not result.archived
        and not result.deleted
        and result.reason == "directory_coverage_proof"
    )


def test_directory_source_json_reference_preserved(session, sync_seed):
    old = directory(session, sync_seed)
    child = directory(session, sync_seed)
    child.query = {
        **child.query,
        "material_child_v1": {"source_run_id": str(old.id), "context_page": 1},
    }
    session.flush()
    assert archive_directory(session, old).reason == "directory_source_reference"


def test_balance_terminal_time_not_creation_controls_retention(session, sync_seed):
    from tests.modules.reporting.test_jobs import _route

    row = ReportBalanceRun(
        tenant_id=sync_seed.tenant_id,
        actor_id=sync_seed.actor_id,
        run_key=str(uuid4()),
        frozen_route=_route(sync_seed),
        remaining_advertiser_ids=[],
        created_at=NOW - timedelta(days=300),
        status="FAILED",
        completed_at=NOW,
    )
    session.add(row)
    session.flush()
    assert (
        retention_candidates(session, tenant_id=row.tenant_id, kind="balance", now=NOW)
        == []
    )


def test_source_child_creation_cannot_race_archive_delete(
    committed_sync_seed, monkeypatch
):
    from sqlalchemy.exc import OperationalError

    from app.core.config import settings
    from app.core.errors import DomainError
    from app.modules.ads.material_directory_children import (
        ensure_material_directory_children,
    )

    seed, factory = committed_sync_seed
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    with factory() as db:
        row = directory(db, seed)
        row.kind, row.ad_type = "ad", "SMART_PLUS"
        db.commit()
        run_id = row.id
    with factory() as writer:
        stale_source = writer.get(AdDirectoryRun, run_id)
        writer.expunge(stale_source)
        writer.rollback()
        # 先加载旧source对象，之后清理连接删除但尚未提交。
        with factory() as cleaner:
            frozen = freeze_archive(
                cleaner,
                tenant_id=seed.tenant_id,
                kind="directory",
                run_id=run_id,
                now=NOW,
            )
            try:
                receipt = persist(
                    S3RunArchive(ObjectTransport(), "private-test"), frozen
                )
                assert prune_archive(
                    cleaner, frozen=frozen, receipt=receipt, now=NOW
                ).deleted
                writer.execute(text("SET LOCAL lock_timeout = '100ms'"))
                with pytest.raises(OperationalError) as failure:
                    ensure_material_directory_children(
                        writer, source=stale_source, pages=[]
                    )
                assert failure.value.orig.sqlstate == "55P03"
                writer.rollback()
                cleaner.commit()
            finally:
                frozen.close()
        # 清理先提交后，旧对象不能产生悬空source_run_id；在读页前即拒绝。
        with pytest.raises(DomainError):
            ensure_material_directory_children(writer, source=stale_source, pages=[])


def test_private_pages_and_pending_scope_block_archive(session, sync_seed):
    job, _ = legacy_job(session, sync_seed)
    job.status, job.finished_at = "COMPLETE", NOW - timedelta(days=91)
    session.flush()
    with pytest.raises(ArchiveDeferred, match="private_pages_remain"):
        frozen_for(session, job)
    session.execute(delete(ReportSyncPage).where(ReportSyncPage.job_id == job.id))
    adopt_report_job(session, job)
    session.flush()
    with pytest.raises(ArchiveDeferred, match="pending_execution"):
        frozen_for(session, job)


def test_balances_archive_from_reliable_completed_at(session, sync_seed):
    from tests.modules.reporting.test_jobs import _route

    row = ReportBalanceRun(
        tenant_id=sync_seed.tenant_id,
        actor_id=sync_seed.actor_id,
        run_key=str(uuid4()),
        frozen_route=_route(sync_seed),
        remaining_advertiser_ids=[],
        status="COMPLETE",
        completed_at=NOW - timedelta(days=91),
    )
    session.add(row)
    session.flush()
    frozen = freeze_archive(
        session, tenant_id=row.tenant_id, kind="balance", run_id=row.id, now=NOW
    )
    try:
        receipt = persist(S3RunArchive(ObjectTransport(), "private-test"), frozen)
        assert prune_archive(session, frozen=frozen, receipt=receipt, now=NOW).deleted
    finally:
        frozen.close()


def test_cost_must_be_closed_settled_and_preserved_in_archive(session, sync_seed):
    from app.modules.sync_control.models import SyncCostLedger

    job = terminal_report(session, sync_seed)
    cost = SyncCostLedger(
        tenant_id=job.tenant_id,
        report_job_id=job.id,
        generation=1,
        physical_endpoint="test/report",
        work_class="history",
        eligible_weights={"history": 30},
        endpoint_weights={"test/report": {"history": 30}},
        reserved_units=3,
        observed_units=2,
        charged_units=2,
        receipt_version=1,
        settled_version=0,
        lease_until=NOW - timedelta(days=91),
    )
    session.add(cost)
    session.flush()
    cost_id = cost.id
    with pytest.raises(ArchiveDeferred, match="unsettled_cost"):
        frozen_for(session, job)
    cost.closed, cost.settled_version = True, 1
    session.flush()
    frozen = frozen_for(session, job)
    try:
        payload = frozen.payload.read()
        assert (
            b'"charged_units":2' in payload
            and b'"physical_endpoint":"test/report"' in payload
        )
        receipt = persist(S3RunArchive(ObjectTransport(), "private-test"), frozen)
        assert prune_archive(session, frozen=frozen, receipt=receipt, now=NOW).deleted
        session.expire_all()
        assert session.get(SyncCostLedger, cost_id) is None
    finally:
        frozen.close()


def test_new_schedule_reference_after_upload_prevents_deletion(session, sync_seed):
    from app.modules.sync_control.models import SyncSchedule

    job = terminal_report(session, sync_seed, children=True)
    frozen = frozen_for(session, job)
    try:
        receipt = persist(S3RunArchive(ObjectTransport(), "private-test"), frozen)
        scope_id = session.exec(
            select(SyncExecutionScope.scope_id).where(
                SyncExecutionScope.report_job_id == job.id
            )
        ).first()
        head = session.exec(
            select(SyncSchedule).where(SyncSchedule.scope_id == scope_id)
        ).first()
        head.lease_execution_id, head.lease_token = job.id, uuid4()
        session.flush()
        with pytest.raises(ArchiveDeferred, match="schedule_reference"):
            prune_archive(session, frozen=frozen, receipt=receipt, now=NOW)
        assert session.get(ReportSyncJob, job.id) is not None
    finally:
        frozen.close()


def test_marker_failure_retains_recoverable_intent_and_resolves_audit(
    committed_sync_seed,
):
    from app.modules.sync_control.archive import finalize_archive, resolve_archive
    from app.modules.sync_control.models import SyncArchiveFinalization

    seed, factory = committed_sync_seed
    with factory() as db:
        job = terminal_report(db, seed)
        run_id = job.id
        db.commit()
    wire = ObjectTransport()
    wire.fail_marker = True
    storage = S3RunArchive(wire, "private-test")
    result = archive_run(
        factory,
        storage,
        tenant_id=seed.tenant_id,
        kind="report",
        run_id=run_id,
        now=NOW,
    )
    assert result.deleted and result.reason == "finalization_pending"
    with factory() as db:
        assert db.get(ReportSyncJob, run_id) is None
        assert (
            db.get(SyncArchiveFinalization, (seed.tenant_id, "report", run_id))
            is not None
        )
        receipts, cursor = resolve_archive(
            db, storage, tenant_id=seed.tenant_id, kind="report", run_id=run_id
        )
        assert len(receipts) == 1 and cursor is None
    assert storage.committed(
        tenant_id=seed.tenant_id, kind="report", run_id=run_id
    ) == ([], None)
    wire.fail_marker = False
    assert finalize_archive(
        factory, storage, tenant_id=seed.tenant_id, kind="report", run_id=run_id
    )
    with factory() as db:
        assert (
            db.get(SyncArchiveFinalization, (seed.tenant_id, "report", run_id)) is None
        )
        assert (
            resolve_archive(
                db, storage, tenant_id=seed.tenant_id, kind="report", run_id=run_id
            )[0]
            == receipts
        )
        assert resolve_archive(
            db, storage, tenant_id=uuid4(), kind="report", run_id=run_id
        ) == ([], None)


def test_changed_upload_is_not_committed_audit_and_retry_can_finalize_new_digest(
    committed_sync_seed,
):
    from app.modules.sync_control.archive import resolve_archive

    seed, factory = committed_sync_seed
    with factory() as db:
        job = terminal_report(db, seed)
        run_id = job.id
        db.commit()
    wire = ObjectTransport()
    storage = S3RunArchive(wire, "private-test")

    def mutate():
        with factory() as db:
            db.get(ReportSyncJob, run_id).error_code = "late_final_audit"
            db.commit()

    wire.after_put = mutate
    result = archive_run(
        factory,
        storage,
        tenant_id=seed.tenant_id,
        kind="report",
        run_id=run_id,
        now=NOW,
    )
    assert not result.archived and result.reason == "source_changed"
    with factory() as db:
        assert resolve_archive(
            db, storage, tenant_id=seed.tenant_id, kind="report", run_id=run_id
        ) == ([], None)
    wire.after_put = None
    result = archive_run(
        factory,
        storage,
        tenant_id=seed.tenant_id,
        kind="report",
        run_id=run_id,
        now=NOW,
    )
    assert result.deleted and len(wire.objects) == 3
    assert (
        len(
            storage.committed(tenant_id=seed.tenant_id, kind="report", run_id=run_id)[0]
        )
        == 1
    )


def test_marker_written_before_intent_delete_failure_is_retryable(committed_sync_seed):
    from app.modules.sync_control.archive import finalize_archive
    from app.modules.sync_control.models import SyncArchiveFinalization

    seed, factory = committed_sync_seed
    with factory() as db:
        job = terminal_report(db, seed)
        run_id = job.id
        db.commit()
    wire, calls = ObjectTransport(), 0
    storage = S3RunArchive(wire, "private-test")

    def fail_fourth_commit():
        nonlocal calls
        calls += 1
        db = factory()
        if calls == 4:

            def fail():
                raise RuntimeError("intent delete commit failure")

            db.commit = fail
        return db

    result = archive_run(
        fail_fourth_commit,
        storage,
        tenant_id=seed.tenant_id,
        kind="report",
        run_id=run_id,
        now=NOW,
    )
    assert result.deleted and result.reason == "finalization_pending"
    assert (
        len(
            storage.committed(tenant_id=seed.tenant_id, kind="report", run_id=run_id)[0]
        )
        == 1
    )
    with factory() as db:
        assert (
            db.get(SyncArchiveFinalization, (seed.tenant_id, "report", run_id))
            is not None
        )
    assert finalize_archive(
        factory, storage, tenant_id=seed.tenant_id, kind="report", run_id=run_id
    )
    assert len(wire.objects) == 2


@pytest.mark.parametrize("archive_bucket", ["", "public-material"])
def test_private_archive_target_must_be_explicit_and_distinct(archive_bucket):
    from app.core.config import settings

    config = settings.model_copy(
        update={"S3_BUCKET": "public-material", "SYNC_ARCHIVE_BUCKET": archive_bucket}
    )
    with pytest.raises(
        ArchiveStorageError, match="archive_private_bucket_not_configured"
    ):
        S3RunArchive.configured(config)


def test_private_archive_target_uses_existing_endpoint_auth_factory(monkeypatch):
    from app.core.config import settings
    from app.modules.sync_control import archive_storage

    captured = []
    wire = ObjectTransport()

    def factory(config):
        captured.append(config)
        return wire

    monkeypatch.setattr(archive_storage, "make_s3", factory)
    config = settings.model_copy(
        update={
            "S3_BUCKET": "public-material",
            "SYNC_ARCHIVE_BUCKET": "private-archive",
        }
    )
    storage = S3RunArchive.configured(config)
    assert storage.bucket == "private-archive" and storage.client is wire
    assert captured[0].S3_BUCKET == "private-archive"
    assert captured[0].S3_ENDPOINT_URL == config.S3_ENDPOINT_URL
    assert config.S3_BUCKET == "public-material"


def test_archived_run_audit_pointer_preserves_amount_facts(session, sync_seed):
    from decimal import Decimal

    from app.modules.reporting.store_models import CampaignReportDay
    from app.modules.sync_control.archive import resolve_archive

    job = terminal_report(session, sync_seed)
    fact = CampaignReportDay(
        tenant_id=job.tenant_id,
        advertiser_id="acc-1",
        campaign_id="historical",
        stat_date=date(2026, 7, 1),
        currency="USD",
        timezone="UTC",
        fetched_at=NOW,
        sync_job_id=job.id,
        spend=Decimal("17.25"),
        revenue_total=Decimal("20.5"),
    )
    session.add(fact)
    session.flush()
    frozen = frozen_for(session, job)
    storage = S3RunArchive(ObjectTransport(), "private-test")
    try:
        receipt = persist(storage, frozen)
        assert prune_archive(session, frozen=frozen, receipt=receipt, now=NOW).deleted
        session.expire_all()
        saved = session.get(
            CampaignReportDay,
            (sync_seed.tenant_id, "acc-1", date(2026, 7, 1), "historical"),
        )
        assert saved.spend == Decimal("17.25") and saved.revenue_total == Decimal(
            "20.5"
        )
        assert resolve_archive(
            session,
            storage,
            tenant_id=sync_seed.tenant_id,
            kind="report",
            run_id=saved.sync_job_id,
        )[0] == [receipt]
    finally:
        frozen.close()


def test_archive_content_length_mismatch_keeps_online_summary(session, sync_seed):
    job = terminal_report(session, sync_seed)
    frozen = frozen_for(session, job)

    class WrongLengthTransport(ObjectTransport):
        def get_object(self, **kwargs):
            response = super().get_object(**kwargs)
            response["ContentLength"] += 1
            return response

    try:
        with pytest.raises(ArchiveStorageError, match="archive_length_mismatch"):
            persist(S3RunArchive(WrongLengthTransport(), "private-test"), frozen)
        assert session.get(ReportSyncJob, job.id) is not None
    finally:
        frozen.close()
