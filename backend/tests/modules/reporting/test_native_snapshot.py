"""JB29: a query is an immutable observation, including its evidence."""

from decimal import Decimal

import pytest

from app.modules.reporting.queries import query_ads
from app.modules.reporting.schemas import ReportingFilter
from tests.modules.reporting.test_native_scope_roas import D, publish


@pytest.mark.parametrize("dimension", ["campaign", "account", "drama"])
def test_page_survives_new_publication(session, sync_seed, dimension):
    publish(session, sync_seed, ("10", "90"), ("20", "-"))
    filters = ReportingFilter(
        dimension=dimension, start_date=D, end_date=D, advertiser_ids=("acc-1",)
    )
    first = query_ads(
        session, context=sync_seed.context, bc_id="bc-a", filters=filters, limit=1
    )
    whole = query_ads(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=filters,
        snapshot_id=first.snapshot.snapshot_id,
    )
    publish(session, sync_seed, ("100", "900"), ("200", "900"))
    old = query_ads(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=filters,
        snapshot_id=first.snapshot.snapshot_id,
    )
    if first.next_cursor:
        following = query_ads(
            session,
            context=sync_seed.context,
            bc_id="bc-a",
            filters=filters,
            snapshot_id=first.snapshot.snapshot_id,
            cursor=first.next_cursor,
        )
        assert following.items == whole.items[1:]
    assert old.items == whole.items
    assert old.summary == whole.summary
    fresh = query_ads(session, context=sync_seed.context, bc_id="bc-a", filters=filters)
    assert fresh.snapshot.snapshot_id != first.snapshot.snapshot_id
    assert fresh.items[0].metric_buckets[0].values["spend"] in (
        Decimal("100"),
        Decimal("900"),
        Decimal("1000"),
    )


def test_detail_scope_legacy_empty_and_revocation(session, sync_seed):
    from datetime import UTC, datetime

    import pytest
    from fastapi import HTTPException
    from sqlmodel import select

    from app.modules.accounts.models import BCAccountAccess
    from app.modules.ads.models import AdObject
    from app.modules.reporting.detail import get_ad_detail
    from app.modules.reporting.query_models import QuerySnapshot, QuerySnapshotRow

    session.add(
        AdObject(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            kind="campaign",
            remote_id="c0",
            name="Current name",
            ad_type="NATIVE_SERIES",
            observed_at=datetime.now(UTC),
            published_version=1,
        )
    )
    session.commit()
    publish(session, sync_seed, ("10",), ("20",))
    f = ReportingFilter(
        dimension="campaign", start_date=D, end_date=D, advertiser_ids=("acc-1",)
    )
    page = query_ads(session, context=sync_seed.context, bc_id="bc-a", filters=f)
    ref = page.items[0].refs[0]
    detail = get_ad_detail(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        ref=ref,
        snapshot_id=page.snapshot.snapshot_id,
    )
    assert detail.report_result == page.items[0]
    publish(session, sync_seed, ("100",), ("200",))
    detail = get_ad_detail(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        ref=ref,
        snapshot_id=page.snapshot.snapshot_id,
    )
    assert detail.report_result == page.items[0]
    own = get_ad_detail(session, context=sync_seed.context, bc_id="bc-a", ref=ref)
    assert own.report_snapshot and own.report_result
    assert own.directory_time_basis == "CURRENT_DIRECTORY"
    assert own.report_snapshot.filters.ids == ("c0",)
    with pytest.raises(HTTPException) as error:
        query_ads(
            session,
            context=sync_seed.context,
            bc_id="bc-a",
            filters=f.model_copy(update={"ids": ("different",)}),
            snapshot_id=page.snapshot.snapshot_id,
        )
    assert error.value.status_code == 409
    snap = session.get(QuerySnapshot, page.snapshot.snapshot_id)
    from datetime import timedelta

    created, expires = snap.created_at, snap.expires_at
    snap.created_at = datetime.now(UTC) - timedelta(hours=1)
    snap.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.add(snap)
    session.flush()
    with pytest.raises(HTTPException) as error:
        query_ads(
            session,
            context=sync_seed.context,
            bc_id="bc-a",
            filters=f,
            snapshot_id=snap.id,
        )
    assert error.value.detail == "query_snapshot_expired"
    snap.created_at, snap.expires_at = created, expires
    session.add(snap)
    session.flush()
    saved_contract = snap.result_contract
    snap.result_contract = {}
    session.add(snap)
    session.flush()
    with pytest.raises(HTTPException) as error:
        query_ads(
            session,
            context=sync_seed.context,
            bc_id="bc-a",
            filters=f,
            snapshot_id=snap.id,
        )
    assert error.value.detail == "query_snapshot_contract_changed"
    snap.result_contract = saved_contract
    session.add(snap)
    session.flush()
    row = session.get(QuerySnapshotRow, (snap.id, page.items[0].row_key))
    row.frozen_result = {}
    session.add(row)
    session.flush()
    with pytest.raises(HTTPException) as error:
        query_ads(
            session,
            context=sync_seed.context,
            bc_id="bc-a",
            filters=f,
            snapshot_id=snap.id,
        )
    assert error.value.detail == "query_snapshot_result_unavailable"
    row.frozen_result = {"row_key": row.row_key, "metric_buckets": [{}]}
    session.add(row)
    session.flush()
    with pytest.raises(HTTPException) as error:
        query_ads(
            session,
            context=sync_seed.context,
            bc_id="bc-a",
            filters=f,
            snapshot_id=snap.id,
        )
    assert error.value.detail == "query_snapshot_result_unavailable"
    row.frozen_result = page.items[0].model_dump(mode="json")
    session.add(row)
    session.flush()
    access = session.exec(
        select(BCAccountAccess).where(
            BCAccountAccess.tenant_id == sync_seed.tenant_id,
            BCAccountAccess.bc_id == "bc-a",
        )
    ).first()
    access.authorized = False
    session.add(access)
    session.flush()
    with pytest.raises(HTTPException) as error:
        query_ads(
            session,
            context=sync_seed.context,
            bc_id="bc-a",
            filters=f,
            snapshot_id=snap.id,
        )
    assert error.value.status_code == 403
    with pytest.raises(HTTPException) as error:
        get_ad_detail(
            session,
            context=sync_seed.context,
            bc_id="bc-a",
            ref=ref,
            snapshot_id=snap.id,
        )
    assert error.value.status_code == 403


def test_evidence_only_and_layer_only_change_invalidates_reuse(session, sync_seed):
    from app.modules.reporting.query_models import QuerySnapshot
    from app.modules.reporting.store_models import ReportSyncState

    publish(session, sync_seed, ("10",), ("20",))
    f = ReportingFilter(
        dimension="campaign", start_date=D, end_date=D, advertiser_ids=("acc-1",)
    )
    first = query_ads(session, context=sync_seed.context, bc_id="bc-a", filters=f)
    state = session.get(
        ReportSyncState, (sync_seed.tenant_id, "acc-1", "campaign_day", D)
    )
    state.publication_digest = "e" * 64
    session.add(state)
    session.flush()
    fresh = query_ads(session, context=sync_seed.context, bc_id="bc-a", filters=f)
    assert fresh.snapshot.snapshot_id != first.snapshot.snapshot_id
    a = session.get(QuerySnapshot, first.snapshot.snapshot_id)
    b = session.get(QuerySnapshot, fresh.snapshot.snapshot_id)
    assert a.dependency_digest != b.dependency_digest
    assert (
        first.items[0].metric_buckets[0].values
        == fresh.items[0].metric_buckets[0].values
    )


def test_other_levels_freeze(session, sync_seed):
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
    from app.modules.reporting.publish import (
        AccountMeta,
        publish_ad_day,
        publish_adgroup_day,
    )
    from tests.modules.reporting.sync_conftest import ad_row
    from tests.modules.reporting.test_jobs import _route

    for level in ("adgroup", "ad"):
        f = ReportingFilter(
            dimension=level, start_date=D, end_date=D, advertiser_ids=("acc-1",)
        )

        def publish_value(spend, level=level):
            common = {
                "session": session,
                "tenant_id": sync_seed.tenant_id,
                "job_id": uuid4(),
                "accounts": {"acc-1": AccountMeta("USD", "UTC")},
                "start": D,
                "end": D,
            }
            if level == "adgroup":
                publish_adgroup_day(
                    **common,
                    requested_at=datetime.now(UTC),
                    route=FrozenTikTokRoute(**_route(sync_seed)),
                    rows=[
                        {
                            "dimensions": {"adgroup_id": "g0", "stat_time_day": str(D)},
                            "metrics": {
                                "spend": spend,
                                "native_growth_total_ad_impression_value": "-",
                            },
                        }
                    ],
                )
            else:
                publish_ad_day(
                    **common, rows=[ad_row("a0", D, spend=spend, total="-", d0="-")]
                )

        publish_value("5")
        first = query_ads(session, context=sync_seed.context, bc_id="bc-a", filters=f)
        publish_value("50")
        old = query_ads(
            session,
            context=sync_seed.context,
            bc_id="bc-a",
            filters=f,
            snapshot_id=first.snapshot.snapshot_id,
        )
        assert old.items == first.items
        assert old.items[0].metric_buckets[0].values["spend"] == Decimal(5)


def test_additive_upgrade_preserves_legacy_snapshot_and_direct_money(
    session, sync_seed
):
    """在专用PG事务中恢复迁移前列形状，再执行真实upgrade；不改任何事实表。"""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from fastapi import HTTPException
    from sqlalchemy import text

    from app.alembic.versions import native_frozen_query as migration
    from app.modules.reporting.query_models import read_snapshot

    publish(session, sync_seed, ("10",), ("20",))
    filters = ReportingFilter(
        dimension="campaign", start_date=D, end_date=D, advertiser_ids=("acc-1",)
    )
    first = query_ads(session, context=sync_seed.context, bc_id="bc-a", filters=filters)
    money = (
        session.connection()
        .execute(
            text(
                "SELECT spend,revenue_total FROM parent_report_day WHERE tenant_id=:tenant"
            ),
            {"tenant": sync_seed.tenant_id},
        )
        .all()
    )
    session.connection().execute(
        text(
            "ALTER TABLE query_snapshot DROP COLUMN result_contract, DROP COLUMN publication_map"
        )
    )
    session.connection().execute(
        text("ALTER TABLE query_snapshot_row DROP COLUMN frozen_result")
    )
    with Operations.context(MigrationContext.configure(session.connection())):
        migration.upgrade()
        with pytest.raises(RuntimeError, match="retain direct facts"):
            migration.downgrade()
    session.expire_all()
    with pytest.raises(HTTPException) as error:
        read_snapshot(
            session,
            context=sync_seed.context,
            bc_id="bc-a",
            snapshot_id=first.snapshot.snapshot_id,
        )
    assert error.value.detail == "query_snapshot_contract_changed"
    assert (
        session.connection()
        .execute(
            text("SELECT total FROM query_snapshot WHERE id=:id"),
            {"id": first.snapshot.snapshot_id},
        )
        .scalar_one()
        == 1
    )
    assert (
        session.connection()
        .execute(
            text(
                "SELECT spend,revenue_total FROM parent_report_day WHERE tenant_id=:tenant"
            ),
            {"tenant": sync_seed.tenant_id},
        )
        .all()
        == money
    )


def test_rr_build_keeps_one_observation_during_publication(committed_sync_seed):
    """真实发布发生在SQL选择与格式化之间；RR与选中投影仍只有旧观测。"""
    from threading import Event, Thread

    from sqlalchemy import event

    from app.core.db import engine
    from app.modules.reporting.queries import _build_snapshot
    from app.modules.reporting.query_models import snapshot_transaction

    seed, factory = committed_sync_seed
    with factory() as session:
        publish(session, seed, ("10",), ("20",))
        session.commit()
    filters = ReportingFilter(
        dimension="campaign", start_date=D, end_date=D, advertiser_ids=("acc-1",)
    )
    selected, published = Event(), Event()
    failures = []

    def publisher():
        try:
            assert selected.wait(10)
            with factory() as session:
                publish(session, seed, ("100",), ("200",))
                session.commit()
        except BaseException as error:
            failures.append(error)
        finally:
            published.set()

    def after_selection(_conn, _cursor, statement, _params, _context, _many):
        if "INSERT INTO query_snapshot_row" in statement:
            selected.set()
            assert published.wait(10)

    thread = Thread(target=publisher)
    thread.start()
    try:
        with snapshot_transaction(engine) as builder:
            connection = builder.connection()
            event.listen(connection, "after_cursor_execute", after_selection)
            try:
                snapshot = _build_snapshot(
                    builder, context=seed.context, bc_id="bc-a", filters=filters
                )
            finally:
                event.remove(connection, "after_cursor_execute", after_selection)
        thread.join(10)
        assert not thread.is_alive()
        assert not failures, failures
        with factory() as session:
            old = query_ads(
                session,
                context=seed.context,
                bc_id="bc-a",
                filters=filters,
                snapshot_id=snapshot.id,
            )
            assert old.items[0].metric_buckets[0].values["spend"] == Decimal(10)
            assert old.summary["buckets"][0]["values"]["spend"] == "10"
            fresh = query_ads(
                session, context=seed.context, bc_id="bc-a", filters=filters
            )
            assert fresh.items[0].metric_buckets[0].values["spend"] == Decimal(100)
    finally:
        selected.set()
        thread.join(10)
