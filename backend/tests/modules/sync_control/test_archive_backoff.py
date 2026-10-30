"""真实PG/Redis两租户故障隔离：poison归档/marker不阻止其他租户。"""

import json
from datetime import timedelta

import pytest

from app.modules.reporting.store_models import ReportSyncJob
from app.modules.sync_control import archive_tasks
from app.modules.sync_control.archive import archive_run
from app.modules.sync_control.archive_storage import S3RunArchive
from app.modules.sync_control.models import SyncArchiveFinalization
from tests.modules.sync_control.test_archive import (
    NOW,
    ObjectTransport,
    terminal_report,
)


@pytest.fixture
def pair(committed_sync_seed):
    from tests.modules.reporting.sync_conftest import committed_sync_seed as fixture

    second = fixture.__wrapped__()
    other, _ = next(second)
    first, factory = committed_sync_seed
    try:
        yield first, other, factory
    finally:
        next(second, None)


class TenantFaultTransport(ObjectTransport):
    fault_tenant = None
    phase = "put"
    stage = "marker"
    fault_calls = 0

    def fails(self, key, phase):
        matches = self.fault_tenant and self.fault_tenant in key and self.phase == phase
        matches = matches and (
            ("/committed/" in key) if self.stage == "marker" else ("/blobs/" in key)
        )
        if matches:
            self.fault_calls += 1
        return matches

    def put_object(self, **kwargs):
        if self.fails(kwargs["Key"], "put"):
            raise OSError("injected tenant put failure")
        return super().put_object(**kwargs)

    def get_object(self, **kwargs):
        if self.fails(kwargs["Key"], "get"):
            raise OSError("injected tenant get failure")
        return super().get_object(**kwargs)


@pytest.mark.parametrize("phase", ["put", "get"])
@pytest.mark.parametrize("other_work", ["new_archive", "pending_marker"])
def test_poison_oldest_intent_does_not_starve_other_tenant(
    pair,
    redis_client,
    redis_key_prefix,
    monkeypatch,
    phase,
    other_work,
):
    first, other, factory = pair
    key = f"{redis_key_prefix}:poison-intent"
    monkeypatch.setattr(archive_tasks, "CURSOR_KEY", key)
    with factory() as db:
        first_id = terminal_report(db, first).id
        other_id = terminal_report(db, other).id
        db.commit()
    wire = TenantFaultTransport()
    storage = S3RunArchive(wire, "private-test")
    wire.fail_marker = True
    assert (
        archive_run(
            factory,
            storage,
            tenant_id=first.tenant_id,
            kind="report",
            run_id=first_id,
            now=NOW,
        ).reason
        == "finalization_pending"
    )
    if other_work == "pending_marker":
        assert (
            archive_run(
                factory,
                storage,
                tenant_id=other.tenant_id,
                kind="report",
                run_id=other_id,
                now=NOW,
            ).reason
            == "finalization_pending"
        )
    with factory() as db:
        db.get(
            SyncArchiveFinalization, (first.tenant_id, "report", first_id)
        ).created_at = NOW - timedelta(minutes=1)
        db.commit()
    wire.fail_marker, wire.fault_tenant, wire.phase = False, str(first.tenant_id), phase
    redis_client.set(key, json.dumps({"tenant": str(other.tenant_id)}))
    try:
        result = archive_tasks.archive_batch(
            factory, storage, redis_client, now=NOW, max_runs=2, time_budget_seconds=5
        )
        assert result["processed"] == 2
        assert result["results"][0]["status"] == "item_deferred"
        with factory() as db:
            assert (
                db.get(SyncArchiveFinalization, (first.tenant_id, "report", first_id))
                is not None
            )
            assert (
                db.get(SyncArchiveFinalization, (other.tenant_id, "report", other_id))
                is None
            )
            assert db.get(ReportSyncJob, other_id) is None
        assert (
            len(
                storage.committed(
                    tenant_id=other.tenant_id, kind="report", run_id=other_id
                )[0]
            )
            == 1
        )
        calls = wire.fault_calls
        archive_tasks.archive_one(factory, storage, redis_client, now=NOW)
        assert wire.fault_calls == calls
        assert (
            redis_client.ttl(
                archive_tasks._item_backoff_key(
                    first.tenant_id, "report", first_id, "finalization"
                )
            )
            > 0
        )
    finally:
        keys = list(redis_client.scan_iter(match=f"{key}*"))
        if keys:
            redis_client.delete(*keys)


@pytest.mark.parametrize("phase", ["put", "get"])
def test_poison_upload_keeps_source_and_allows_other_tenant(
    pair,
    redis_client,
    redis_key_prefix,
    monkeypatch,
    phase,
):
    first, other, factory = pair
    key = f"{redis_key_prefix}:poison-upload"
    monkeypatch.setattr(archive_tasks, "CURSOR_KEY", key)
    with factory() as db:
        first_id = terminal_report(db, first).id
        other_id = terminal_report(db, other).id
        db.commit()
    wire = TenantFaultTransport()
    wire.stage, wire.phase, wire.fault_tenant = "blob", phase, str(first.tenant_id)
    redis_client.set(key, json.dumps({"tenant": str(first.tenant_id)}))
    try:
        result = archive_tasks.archive_batch(
            factory,
            S3RunArchive(wire, "private-test"),
            redis_client,
            now=NOW,
            max_runs=2,
            time_budget_seconds=5,
        )
        assert result["processed"] == 2
        assert result["results"][0]["status"] == "item_deferred"
        with factory() as db:
            assert db.get(ReportSyncJob, first_id) is not None
            assert db.get(ReportSyncJob, other_id) is None
        assert (
            redis_client.ttl(
                archive_tasks._item_backoff_key(
                    first.tenant_id, "report", first_id, "archive"
                )
            )
            > 0
        )
    finally:
        keys = list(redis_client.scan_iter(match=f"{key}*"))
        if keys:
            redis_client.delete(*keys)


def test_archive_beat_uses_bounded_expiring_low_priority_control_work():
    from app.jobs.celery_app import celery_app

    assert "app.modules.sync_control.archive_tasks" in celery_app.conf.imports
    assert celery_app.conf.task_routes["sync.archive_runs"] == {
        "queue": "control",
        "priority": 9,
    }
    beat = celery_app.conf.beat_schedule["archive-terminal-sync-runs"]
    assert beat["schedule"] == 5.0
    assert beat["kwargs"] == {"max_runs": 5, "time_budget_seconds": 3}
    assert beat["options"] == {"queue": "control", "priority": 9, "expires": 4.0}
