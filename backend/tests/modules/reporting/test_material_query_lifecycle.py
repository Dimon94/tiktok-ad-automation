"""JB37: failures execute in real storage; consumers never see a partial result."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event
from sqlmodel import select

from app.core.db import engine
from app.modules.reporting import page_cache
from app.modules.reporting.exports import (
    create_export,
    download_export,
    generate_export,
)
from app.modules.reporting.query_models import (
    QuerySnapshot,
    QuerySnapshotRow,
    ReportExport,
)
from app.modules.reporting.schemas import ReportingFilter
from tests.modules.accounts.conftest import app_config as app_config
from tests.modules.reporting.query_helpers import finish_projections, query_ads
from tests.modules.reporting.test_material_query import D, material_row, publish, use


@contextmanager
def http_client(seed, factory):
    from fastapi.testclient import TestClient

    from app.api.deps import get_current_user, get_db
    from app.main import app
    from app.models import User

    def db():
        with factory() as s:
            yield s

    with factory() as s:
        user = s.get(User, seed.actor_id)
    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def seed_materials(s, seed, count=1):
    publish(s, seed, "acc-1", [material_row(f"cr{i}", "40") for i in range(count)])
    for i in range(count):
        use(
            s,
            seed,
            adv="acc-1",
            ad=f"ad{i}",
            video=f"vid{i}",
            ad_material=f"mid{i}",
            post=f"cr{i}",
        )
    finish_projections(s)


def filters():
    return ReportingFilter(
        dimension="material", start_date=D, end_date=D, advertiser_ids=("acc-1",)
    )


@pytest.mark.parametrize("fault", ["second-freeze", "creative-read", "parent-read"])
def test_storage_failure_is_atomic_and_requery_is_new(
    committed_sync_seed,
    app_config,  # noqa: ARG001
    fault,
):
    seed, factory = committed_sync_seed
    redis_client = page_cache.client()
    with factory() as s:
        seed_materials(s, seed, 101 if fault == "second-freeze" else 1)
        s.commit()
        key = page_cache.scope_key(
            s, context=seed.context, bc_id="bc-a", filters=filters(), limit=50
        )
    attempted = []
    hits = []

    def fail_storage(_conn, cursor, statement, parameters, _ctx, _many):
        sql = statement.lower()
        if sql.startswith("insert into query_snapshot ("):
            attempted.append(parameters["id"])
        # Execute a real PostgreSQL error, rather than replacing any repository/service.
        matched = (
            fault == "second-freeze"
            and sql.startswith("update query_snapshot_row set frozen_result")
            or fault == "creative-read"
            and "r.fetched_at as fact_observed_at" in sql
            or fault == "parent-read"
            and "from ad_report_day" in sql
            and "creative" not in sql
        )
        if matched:
            hits.append(statement[:100])
            if fault != "second-freeze" or len(hits) % 2 == 0:
                cursor.execute("SELECT 1/0")

    try:
        for attempt in range(2):
            token = str(uuid4())
            page_cache.watch(
                redis_client,
                key,
                context=seed.context,
                bc_id="bc-a",
                filters=filters(),
                limit=50,
                force=True,
            )
            redis_client.set(
                page_cache.lease_key(key), token, ex=page_cache.LEASE_SECONDS
            )
            event.listen(engine, "before_cursor_execute", fail_storage)
            try:
                assert not page_cache.refresh_page(redis_client, key, token)
            finally:
                event.remove(engine, "before_cursor_execute", fail_storage)
            assert hits, fault
            with factory() as s:
                assert not s.exec(
                    select(QuerySnapshot).where(
                        QuerySnapshot.tenant_id == seed.tenant_id
                    )
                ).all()
                assert not s.exec(
                    select(QuerySnapshotRow).where(
                        QuerySnapshotRow.snapshot_id.in_(attempted)
                    )
                ).all()
                assert page_cache.cached(redis_client, key) is None
                with pytest.raises(HTTPException) as caught:
                    page_cache.query_cached_ads(
                        s,
                        context=seed.context,
                        bc_id="bc-a",
                        filters=filters(),
                        wait_seconds=0.02,
                    )
                assert caught.value.detail == "query_build_failed"
                for identity in attempted:
                    with pytest.raises(HTTPException) as absent:
                        create_export(
                            s,
                            context=seed.context,
                            bc_id="bc-a",
                            snapshot_id=identity,
                            idempotency_key=str(uuid4()),
                        )
                    assert absent.value.status_code == 404
            with http_client(seed, factory) as http:
                response = http.get(
                    f"/api/tenants/{seed.tenant_id}/ads",
                    params={
                        "bc_id": "bc-a",
                        "dimension": "material",
                        "start_date": str(D),
                        "end_date": str(D),
                        "advertiser_id": "acc-1",
                    },
                )
                assert response.status_code == 503, response.text
                assert response.json()["detail"] == "query_build_failed"
                for identity in attempted:
                    rejected = http.post(
                        f"/api/tenants/{seed.tenant_id}/report-exports",
                        params={"bc_id": "bc-a"},
                        json={
                            "snapshot_id": str(identity),
                            "idempotency_key": str(uuid4()),
                        },
                    )
                    assert rejected.status_code == 404, rejected.text
            print(  # noqa: T201
                f"JB37 {fault} attempt={attempt} rolled-back={list(map(str, attempted))}"
            )
        assert len(set(attempted)) == 2
        # A changed source observation explains a changed digest; UUID randomness does not.
        with factory() as s:
            publish(s, seed, "acc-1", [material_row("cr0", "90")])
            finish_projections(s)
            s.commit()
        token = str(uuid4())
        redis_client.set(page_cache.lease_key(key), token, ex=page_cache.LEASE_SECONDS)
        assert page_cache.refresh_page(redis_client, key, token)
        ready = page_cache.cached(redis_client, key)
        assert ready and ready.snapshot.snapshot_id not in attempted
        assert ready.items[0].evidence_digest
        print(  # noqa: T201
            f"JB37 {fault} ready={ready.snapshot.snapshot_id} digest={ready.items[0].evidence_digest}"
        )
    finally:
        redis_client.delete(
            key, page_cache.watch_key(key), page_cache.lease_key(key), key + ":error"
        )
        redis_client.zrem(page_cache.due_key(), key)


def test_expiry_contract_exit_and_old_export_compatibility(session, sync_seed):
    seed_materials(session, sync_seed)
    first = query_ads(
        session, context=sync_seed.context, bc_id="bc-a", filters=filters()
    )
    snapshot = session.get(QuerySnapshot, first.snapshot.snapshot_id)
    exports = []
    for index in range(2):
        public = create_export(
            session,
            context=sync_seed.context,
            bc_id="bc-a",
            snapshot_id=snapshot.id,
            idempotency_key=f"old-{index}",
        )
        exports.append(session.get(ReportExport, public.id))
    generate_export(session, exports[0])
    original = download_export(
        session, context=sync_seed.context, bc_id="bc-a", export_id=exports[0].id
    )
    expiry = exports[0].expires_at
    for reason in ("expired", "contract_changed"):
        snapshot.created_at = datetime.now(UTC) - timedelta(minutes=16)
        snapshot.expires_at = (
            datetime.now(UTC) - timedelta(seconds=1)
            if reason == "expired"
            else datetime.now(UTC) + timedelta(minutes=1)
        )
        if reason == "contract_changed":
            snapshot.result_contract = {"revision": "old-material"}
            exports[0].result_contract = exports[1].result_contract = {
                "revision": "old-material"
            }
        session.add(snapshot)
        session.flush()
        from app.integrations.tiktok.contracts.ads import EntityRef
        from app.modules.reporting.detail import get_ad_detail

        for action in (
            lambda: get_ad_detail(
                session,
                context=sync_seed.context,
                bc_id="bc-a",
                ref=EntityRef(
                    tenant_id=sync_seed.tenant_id,
                    advertiser_id="acc-1",
                    kind="ad",
                    remote_id="ad0",
                ),
                snapshot_id=snapshot.id,
            ),
            lambda: query_ads(
                session,
                context=sync_seed.context,
                bc_id="bc-a",
                filters=filters(),
                snapshot_id=snapshot.id,
            ),
            lambda: create_export(
                session,
                context=sync_seed.context,
                bc_id="bc-a",
                snapshot_id=snapshot.id,
                idempotency_key=str(uuid4()),
            ),
        ):
            with pytest.raises(HTTPException) as caught:
                action()
            assert caught.value.detail == f"query_snapshot_{reason}"
        assert (
            download_export(
                session,
                context=sync_seed.context,
                bc_id="bc-a",
                export_id=exports[0].id,
            )
            == original
        )
        assert exports[0].expires_at == expiry
    with pytest.raises(HTTPException) as old:
        generate_export(session, exports[1])
    assert old.value.detail == "report_export_contract_incompatible_reexport"
    assert exports[1].object_key is None
    publish(session, sync_seed, "acc-1", [material_row("cr0", "90")])
    fresh = query_ads(
        session, context=sync_seed.context, bc_id="bc-a", filters=filters()
    )
    assert fresh.snapshot.snapshot_id != first.snapshot.snapshot_id
    assert fresh.items[0].evidence_digest != first.items[0].evidence_digest
    assert first.items[0].material_instances[0].metric_buckets[0].values["spend"] == 40
    assert fresh.items[0].material_instances[0].metric_buckets[0].values["spend"] == 90
    assert (
        download_export(
            session, context=sync_seed.context, bc_id="bc-a", export_id=exports[0].id
        )
        == original
    )
    print(  # noqa: T201
        f"JB37 expiry old={snapshot.id} new={fresh.snapshot.snapshot_id} ttl={fresh.snapshot.expires_at - fresh.snapshot.snapshot_at}"
    )


def test_contract_exit_requery_does_not_reuse_old_identity(session, sync_seed):
    seed_materials(session, sync_seed)
    first = query_ads(
        session, context=sync_seed.context, bc_id="bc-a", filters=filters()
    )
    snapshot = session.get(QuerySnapshot, first.snapshot.snapshot_id)
    snapshot.result_contract = {"revision": "old-material"}
    session.add(snapshot)
    session.flush()
    try:
        fresh = query_ads(
            session, context=sync_seed.context, bc_id="bc-a", filters=filters()
        )
    except HTTPException as error:
        raise AssertionError(
            "explicit requery must replace incompatible identity"
        ) from error
    assert fresh.snapshot.snapshot_id != snapshot.id
    assert fresh.snapshot.result_contract["revision"] == "material-instance-result-v1"
    assert fresh.items == first.items  # unchanged source, no invented business change


def test_shared_requery_replaces_old_contract(committed_sync_seed):
    from app.modules.reporting.queries import query_ads as shared_query

    seed, factory = committed_sync_seed
    with factory() as s:
        seed_materials(s, seed)
        s.commit()
    with factory() as s:
        first = shared_query(s, context=seed.context, bc_id="bc-a", filters=filters())
    with factory() as s:
        snapshot = s.get(QuerySnapshot, first.snapshot.snapshot_id)
        snapshot.result_contract = {"revision": "old-material"}
        s.add(snapshot)
        s.commit()
    with factory() as s:
        fresh = shared_query(s, context=seed.context, bc_id="bc-a", filters=filters())
        assert fresh.snapshot.snapshot_id != first.snapshot.snapshot_id
        assert (
            fresh.snapshot.result_contract["revision"] == "material-instance-result-v1"
        )
        assert fresh.items == first.items
