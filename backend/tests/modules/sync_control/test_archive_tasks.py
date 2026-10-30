"""真实Redis游标与PG提交恢复；对象存储替身仅在传输边界。"""

import json

from app.modules.reporting.store_models import ReportSyncJob
from app.modules.sync_control import archive_tasks
from app.modules.sync_control.archive import archive_run
from app.modules.sync_control.archive_storage import S3RunArchive
from tests.modules.sync_control.test_archive import (
    NOW,
    ObjectTransport,
    terminal_report,
)


def test_archive_tick_processes_one_run_and_advances_cursor(
    committed_sync_seed,
    redis_client,
    redis_key_prefix,
    monkeypatch,
):
    seed, factory = committed_sync_seed
    key = f"{redis_key_prefix}:archive-cursor"
    monkeypatch.setattr(archive_tasks, "CURSOR_KEY", key)
    with factory() as db:
        first = terminal_report(db, seed)
        second = terminal_report(db, seed)
        ids = (first.id, second.id)
        db.commit()
    redis_client.set(key, json.dumps({"tenant": str(seed.tenant_id), "kind": 0}))
    storage = S3RunArchive(ObjectTransport(), "private-test")
    try:
        result = archive_tasks.archive_one(factory, storage, redis_client, now=NOW)
        assert result["deleted"] and result["status"] == "processed"
        with factory() as db:
            assert (
                sum(db.get(ReportSyncJob, identity) is not None for identity in ids)
                == 1
            )
        result = archive_tasks.archive_one(factory, storage, redis_client, now=NOW)
        assert result["deleted"]
        with factory() as db:
            assert all(db.get(ReportSyncJob, identity) is None for identity in ids)
    finally:
        keys = list(redis_client.scan_iter(match=f"{key}*"))
        if keys:
            redis_client.delete(*keys)


def test_pending_finalization_precedes_new_archival(
    committed_sync_seed,
    redis_client,
    redis_key_prefix,
    monkeypatch,
):
    seed, factory = committed_sync_seed
    key = f"{redis_key_prefix}:archive-cursor"
    monkeypatch.setattr(archive_tasks, "CURSOR_KEY", key)
    with factory() as db:
        first = terminal_report(db, seed)
        second = terminal_report(db, seed)
        ids = first.id, second.id
        db.commit()
    wire = ObjectTransport()
    wire.fail_marker = True
    storage = S3RunArchive(wire, "private-test")
    result = archive_run(
        factory,
        storage,
        tenant_id=seed.tenant_id,
        kind="report",
        run_id=ids[0],
        now=NOW,
    )
    assert result.reason == "finalization_pending"
    wire.fail_marker = False
    redis_client.set(key, json.dumps({"tenant": str(seed.tenant_id), "kind": 0}))
    try:
        assert (
            archive_tasks.archive_one(factory, storage, redis_client, now=NOW)["status"]
            == "finalized"
        )
        with factory() as db:
            assert db.get(ReportSyncJob, ids[1]) is not None
        assert archive_tasks.archive_one(factory, storage, redis_client, now=NOW)[
            "deleted"
        ]
    finally:
        keys = list(redis_client.scan_iter(match=f"{key}*"))
        if keys:
            redis_client.delete(*keys)


def test_missing_storage_configuration_does_not_start_cleanup(monkeypatch):
    monkeypatch.setattr(archive_tasks.settings, "S3_BUCKET", "")
    assert archive_tasks.archive_runs_task.run() == {
        "status": "storage_unconfigured",
        "archived": False,
    }


def test_batch_budget_stops_after_slow_transport_round(
    committed_sync_seed,
    redis_client,
    redis_key_prefix,
    monkeypatch,
):
    from time import sleep

    seed, factory = committed_sync_seed
    key = f"{redis_key_prefix}:slow-archive"
    monkeypatch.setattr(archive_tasks, "CURSOR_KEY", key)
    with factory() as db:
        first = terminal_report(db, seed)
        second = terminal_report(db, seed)
        ids = (first.id, second.id)
        db.commit()

    class SlowTransport(ObjectTransport):
        def get_object(self, **kwargs):
            sleep(0.015)
            return super().get_object(**kwargs)

    redis_client.set(key, json.dumps({"tenant": str(seed.tenant_id)}))
    try:
        result = archive_tasks.archive_batch(
            factory,
            S3RunArchive(SlowTransport(), "private-test"),
            redis_client,
            now=NOW,
            max_runs=2,
            time_budget_seconds=0.01,
        )
        assert result["processed"] == 1
        with factory() as db:
            assert (
                sum(db.get(ReportSyncJob, identity) is not None for identity in ids)
                == 1
            )
    finally:
        keys = list(redis_client.scan_iter(match=f"{key}*"))
        if keys:
            redis_client.delete(*keys)


def test_archive_rotates_tenants_before_draining_large_tenant(
    committed_sync_seed,
    redis_client,
    redis_key_prefix,
    monkeypatch,
):
    from tests.modules.reporting.sync_conftest import (
        committed_sync_seed as seed_fixture,
    )

    seed, factory = committed_sync_seed
    other_fixture = seed_fixture.__wrapped__()
    other_seed, _ = next(other_fixture)
    key = f"{redis_key_prefix}:fair-archive"
    monkeypatch.setattr(archive_tasks, "CURSOR_KEY", key)
    try:
        with factory() as db:
            busy = [terminal_report(db, seed).id for _ in range(3)]
            quiet = terminal_report(db, other_seed).id
            db.commit()
        redis_client.set(key, json.dumps({"tenant": str(seed.tenant_id)}))
        storage = S3RunArchive(ObjectTransport(), "private-test")
        assert archive_tasks.archive_one(factory, storage, redis_client, now=NOW)[
            "deleted"
        ]
        assert archive_tasks.archive_one(factory, storage, redis_client, now=NOW)[
            "deleted"
        ]
        with factory() as db:
            assert db.get(ReportSyncJob, quiet) is None
            assert (
                sum(db.get(ReportSyncJob, identity) is not None for identity in busy)
                == 2
            )
    finally:
        keys = list(redis_client.scan_iter(match=f"{key}*"))
        if keys:
            redis_client.delete(*keys)
        next(other_fixture, None)
