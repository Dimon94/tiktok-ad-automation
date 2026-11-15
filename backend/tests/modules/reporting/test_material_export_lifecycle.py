"""JB38: original Worker, committed PG and actual local object execution."""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlmodel import select

from app.modules.accounts.models import BCAccountAccess
from app.modules.reporting import exports
from app.modules.reporting.export_tasks import run_export, run_export_task
from app.modules.reporting.query_models import QuerySnapshot, ReportExport
from app.modules.reporting.schemas import ReportingFilter
from tests.modules.reporting.query_helpers import finish_projections, query_ads
from tests.modules.reporting.test_material_query import D, material_row, publish, use


def prepare(seed, factory):
    with factory() as s:
        publish(s, seed, "acc-1", [material_row("cr1", "40")])
        use(
            s, seed, adv="acc-1", ad="ad1", video="vid1", ad_material="mid1", post="cr1"
        )
        finish_projections(s)
        s.commit()
        page = query_ads(
            s,
            context=seed.context,
            bc_id="bc-a",
            filters=ReportingFilter(
                dimension="material",
                start_date=D,
                end_date=D,
                advertiser_ids=("acc-1",),
            ),
        )
        assert page.items and page.items[0].material_instances
        assert (
            page.items[0].material_instances[0].metric_buckets[0].values["spend"] == 40
        )
        public = exports.create_export(
            s,
            context=seed.context,
            bc_id="bc-a",
            snapshot_id=page.snapshot.snapshot_id,
            idempotency_key="jb38",
        )
        row = s.get(ReportExport, public.id)
        expected = exports._csv_rows(row, exports._export_rows(s, row))
        frozen = (row.frozen_rows, row.result_contract, row.publication_map)
        s.commit()
    return public.id, expected, frozen


def revoke(seed, factory):
    with factory() as s:
        access = s.exec(
            select(BCAccountAccess).where(
                BCAccountAccess.tenant_id == seed.tenant_id,
                BCAccountAccess.bc_id == "bc-a",
                BCAccountAccess.advertiser_id == "acc-1",
            )
        ).one()
        access.authorized = False
        s.add(access)
        s.commit()


@pytest.mark.parametrize("fault", ["before", "after", "expiry", "revoke"])
def test_storage_failure_and_crossing_publication(
    committed_sync_seed, monkeypatch, fault
):
    seed, factory = committed_sync_seed
    export_id, expected, frozen = prepare(seed, factory)
    original = exports.os.replace

    def execution(src, dst):
        if fault == "before":
            raise OSError("injected before object publication")
        original(src, dst)
        if fault == "after":
            raise OSError("injected after object publication")
        if fault == "expiry":

            class Clock(datetime):
                @classmethod
                def now(cls, tz=None):
                    return datetime.now(UTC) + timedelta(days=2)

            monkeypatch.setattr(exports, "datetime", Clock)
        if fault == "revoke":
            revoke(seed, factory)

    with monkeypatch.context() as patch:
        patch.setattr(exports.os, "replace", execution)
        run_export(export_id)
        with factory() as s:
            row = s.get(ReportExport, export_id)
            assert row.status == ("EXPIRED" if fault == "expiry" else "FAILED")
            assert row.object_key is None
            assert not exports._object_path(exports._expected_key(row)).exists()
            with pytest.raises(HTTPException):
                exports.download_export(
                    s, context=seed.context, bc_id="bc-a", export_id=export_id
                )
    monkeypatch.undo()
    if fault in ("before", "after"):
        run_export(export_id)
        with factory() as s:
            row = s.get(ReportExport, export_id)
            assert row.status == "COMPLETE"
            assert (
                exports.download_export(
                    s, context=seed.context, bc_id="bc-a", export_id=export_id
                )
                == expected
            )
            assert (row.frozen_rows, row.result_contract, row.publication_map) == frozen
            print(  # noqa: T201
                f"JB38 retry task={export_id} file={row.object_key} bytes={len(expected)} sha256={hashlib.sha256(expected).hexdigest()}"
            )  # noqa: T201


def test_duplicate_messages_and_snapshot_expiry(committed_sync_seed, monkeypatch):
    seed, factory = committed_sync_seed
    export_id, expected, frozen = prepare(seed, factory)
    with factory() as s:
        row = s.get(ReportExport, export_id)
        snapshot = s.get(QuerySnapshot, row.snapshot_id)
        s.delete(snapshot)
        publish(s, seed, "acc-1", [material_row("cr1", "90")])
        s.commit()
    writes = []
    original = exports.os.replace

    def execution(src, dst):
        writes.append(dst)
        original(src, dst)

    monkeypatch.setattr(exports.os, "replace", execution)
    for _ in range(3):
        run_export_task(
            tenant_id=str(seed.tenant_id),
            actor_id=str(seed.actor_id),
            payload={"export_id": str(export_id)},
        )
        with factory() as s:
            row = s.get(ReportExport, export_id)
            assert (
                exports.create_export(
                    s,
                    context=seed.context,
                    bc_id="bc-a",
                    snapshot_id=row.snapshot_id,
                    idempotency_key="jb38",
                ).id
                == export_id
            )
            assert (
                exports.download_export(
                    s, context=seed.context, bc_id="bc-a", export_id=export_id
                )
                == expected
            )
            assert (row.frozen_rows, row.result_contract, row.publication_map) == frozen
            s.commit()
    assert len(writes) == 1
    from sqlalchemy import text

    with factory() as s:
        s.execute(
            text("UPDATE ad_object SET name='renamed' WHERE tenant_id=:t"),
            {"t": seed.tenant_id},
        )
        for table in (
            "creative_report_day",
            "ad_creative",
            "ad_material_reference",
            "ad_object",
        ):
            s.execute(
                text(f"DELETE FROM {table} WHERE tenant_id=:t"), {"t": seed.tenant_id}
            )
        s.commit()
    with factory() as s:
        assert (
            exports.download_export(
                s, context=seed.context, bc_id="bc-a", export_id=export_id
            )
            == expected
        )


@pytest.mark.parametrize("stage", ["create", "generate", "retry", "download"])
def test_independent_stage_revocation(committed_sync_seed, stage):
    seed, factory = committed_sync_seed
    export_id, _, _ = prepare(seed, factory)
    if stage == "download":
        run_export(export_id)
    if stage == "retry":
        with factory() as s:
            row = s.get(ReportExport, export_id)
            row.status = "FAILED"
            s.add(row)
            s.commit()
    revoke(seed, factory)
    if stage == "generate":
        run_export(export_id)
        with factory() as s:
            row = s.get(ReportExport, export_id)
            assert row.status == "FAILED" and row.object_key is None
    else:
        with factory() as s:
            row = s.get(ReportExport, export_id)
            with pytest.raises(HTTPException) as error:
                if stage == "download":
                    exports.download_export(
                        s, context=seed.context, bc_id="bc-a", export_id=export_id
                    )
                else:
                    exports.create_export(
                        s,
                        context=seed.context,
                        bc_id="bc-a",
                        snapshot_id=row.snapshot_id,
                        idempotency_key="new" if stage == "create" else "jb38",
                    )
            assert error.value.status_code == 403


@pytest.mark.parametrize("status", ["QUEUED", "RUNNING", "COMPLETE"])
def test_expired_export_never_publishes(committed_sync_seed, status):
    seed, factory = committed_sync_seed
    export_id, _, _ = prepare(seed, factory)
    if status == "COMPLETE":
        run_export(export_id)
    with factory() as s:
        row = s.get(ReportExport, export_id)
        row.created_at = datetime.now(UTC) - timedelta(days=2)
        row.expires_at = datetime.now(UTC) - timedelta(days=1)
        row.status = status
        s.add(row)
        s.commit()
    run_export(export_id)
    with factory() as s:
        row = s.get(ReportExport, export_id)
        assert row.status == "EXPIRED" and row.object_key is None
        assert not exports._object_path(exports._expected_key(row)).exists()
        with pytest.raises(HTTPException) as error:
            exports.download_export(
                s, context=seed.context, bc_id="bc-a", export_id=export_id
            )
        assert error.value.status_code == 409


def test_concurrent_duplicate_workers_publish_once(committed_sync_seed, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    seed, factory = committed_sync_seed
    export_id, expected, _ = prepare(seed, factory)
    entered, release = Event(), Event()
    writes = []
    original = exports.os.replace

    def execution(src, dst):
        writes.append(dst)
        entered.set()
        assert release.wait(10)
        original(src, dst)

    monkeypatch.setattr(exports.os, "replace", execution)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run_export, export_id)
        try:
            assert entered.wait(10)
            second = pool.submit(run_export, export_id)
        finally:
            release.set()
        first.result(timeout=15)
        second.result(timeout=15)
    assert len(writes) == 1
    with factory() as s:
        assert (
            exports.download_export(
                s, context=seed.context, bc_id="bc-a", export_id=export_id
            )
            == expected
        )


def test_same_remote_ids_cannot_read_foreign_export(session, sync_seed):
    from types import SimpleNamespace

    from app.core.errors import DomainError
    from app.modules.tenants.models import TenantMembership
    from tests.modules.reporting.sync_conftest import sync_seed as seed_fixture

    foreign = seed_fixture.__wrapped__(session)
    for seed in (sync_seed, foreign):
        publish(session, seed, "acc-1", [material_row("cr1", "40")])
        use(
            session,
            seed,
            adv="acc-1",
            ad="ad1",
            video="vid1",
            ad_material="mid1",
            post="cr1",
        )
    page = query_ads(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=ReportingFilter(
            dimension="material", start_date=D, end_date=D, advertiser_ids=("acc-1",)
        ),
    )
    task = exports.create_export(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        snapshot_id=page.snapshot.snapshot_id,
        idempotency_key="same",
    )
    row = session.get(ReportExport, task.id)
    exports.generate_export(session, row)
    session.add(
        TenantMembership(
            tenant_id=sync_seed.tenant_id,
            user_id=foreign.actor_id,
            role="operator",
            active=True,
        )
    )
    session.flush()
    contexts = [
        (foreign.context, "bc-a"),
        (sync_seed.context, "bc-b"),
        (
            SimpleNamespace(
                tenant_id=sync_seed.tenant_id,
                actor_id=foreign.actor_id,
                role="operator",
            ),
            "bc-a",
        ),
    ]
    for context, bc in contexts:
        for action in (exports.get_export, exports.download_export):
            with pytest.raises((HTTPException, DomainError)) as error:
                action(session, context=context, bc_id=bc, export_id=task.id)
            assert getattr(error.value, "status_code", 403) in (403, 404)
    other = query_ads(
        session,
        context=foreign.context,
        bc_id="bc-a",
        filters=ReportingFilter(
            dimension="material", start_date=D, end_date=D, advertiser_ids=("acc-1",)
        ),
    )
    task2 = exports.create_export(
        session,
        context=foreign.context,
        bc_id="bc-a",
        snapshot_id=other.snapshot.snapshot_id,
        idempotency_key="same",
    )
    assert task2.id != task.id


def test_retry_request_rearms_original_published_outbox(committed_sync_seed):
    from app.jobs.models import PendingDispatch
    from tests.modules.reporting.test_material_query_lifecycle import http_client

    seed, factory = committed_sync_seed
    export_id, _, frozen = prepare(seed, factory)
    with factory() as s:
        row = s.get(ReportExport, export_id)
        snapshot_id = row.snapshot_id
        row.status = "FAILED"
        dispatch = s.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.tenant_id,
                PendingDispatch.task_key == f"reporting.export:{export_id}",
            )
        ).one()
        dispatch_id = dispatch.id
        dispatch.published_at = datetime.now(UTC)
        s.add_all([row, dispatch])
        s.commit()
    with http_client(seed, factory) as client:
        for _ in range(2):
            response = client.post(
                f"/api/tenants/{seed.tenant_id}/report-exports",
                params={"bc_id": "bc-a"},
                json={"snapshot_id": str(snapshot_id), "idempotency_key": "jb38"},
            )
            assert response.status_code == 200
            assert response.json()["id"] == str(export_id)
            assert response.json()["status"] == "QUEUED"
    with factory() as s:
        row = s.get(ReportExport, export_id)
        dispatch = s.get(PendingDispatch, dispatch_id)
        assert dispatch.published_at is None
        assert (row.frozen_rows, row.result_contract, row.publication_map) == frozen
        assert (
            len(
                s.exec(
                    select(PendingDispatch).where(
                        PendingDispatch.tenant_id == seed.tenant_id,
                        PendingDispatch.task_name == "reporting.export",
                    )
                ).all()
            )
            == 1
        )
