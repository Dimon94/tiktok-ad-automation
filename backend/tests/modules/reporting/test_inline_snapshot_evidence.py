"""素材版本内联后，冻结身份与清理水位仍在真实PostgreSQL中保持安全。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, text
from sqlmodel import Session

from app.core.db import engine
from app.modules.reporting.query_models import QuerySnapshot, read_snapshot_rows
from app.modules.reporting.read_models.evidence import (
    ReportEvidenceVersion,
    begin_evidence_snapshot,
)
from app.modules.reporting.schemas import ReportingFilter
from app.modules.reporting.snapshot_members import (
    prune_report_evidence,
    snapshot_members,
)
from tests.modules.reporting.query_helpers import query_ads
from tests.modules.reporting.test_material_query import D, material_row, publish, use


def material_snapshot(session, seed):
    publish(session, seed, "acc-1", [material_row("post", "10")])
    use(session, seed, adv="acc-1", ad="a1", video="v1", ad_material="m1", post="post")
    page = query_ads(
        session,
        context=seed.context,
        bc_id="bc-a",
        filters=ReportingFilter(dimension="material", start_date=D, end_date=D),
    )
    snapshot = session.get(QuerySnapshot, page.snapshot.snapshot_id)
    rows = read_snapshot_rows(
        session, context=seed.context, bc_id="bc-a", snapshot_id=snapshot.id
    )
    return snapshot, rows


def test_inline_versions_keep_original_members_without_legacy_link_writes(
    session, sync_seed
):
    snapshot, rows = material_snapshot(session, sync_seed)
    original = snapshot_members(session, snapshot=snapshot, rows=rows)
    assert rows[0].evidence_versions
    assert (
        session.connection()
        .execute(
            text("SELECT count(*) FROM query_snapshot_evidence WHERE snapshot_id=:s"),
            {"s": snapshot.id},
        )
        .scalar_one()
        == 0
    )
    # 同步目录变更退休旧版本；冻结页仍应精确指向原广告位置。
    session.connection().execute(
        text("DELETE FROM current_ad_material WHERE tenant_id=:t"),
        {"t": sync_seed.tenant_id},
    )
    assert snapshot_members(session, snapshot=snapshot, rows=rows) == original


def test_inline_missing_version_fails_closed(session, sync_seed):
    snapshot, rows = material_snapshot(session, sync_seed)
    rows[0].evidence_versions = [uuid4()]
    with pytest.raises(HTTPException) as error:
        snapshot_members(session, snapshot=snapshot, rows=rows)
    assert (error.value.status_code, error.value.detail) == (
        409,
        "query_membership_unavailable",
    )


def test_unfiltered_material_summary_matches_full_row_filter_path(session, sync_seed):
    material_snapshot(session, sync_seed)
    use(session, sync_seed, adv="acc-2", ad="a2", video="v1", ad_material="m2")
    pages = [
        query_ads(
            session,
            context=sync_seed.context,
            bc_id="bc-a",
            filters=ReportingFilter(
                dimension="material", start_date=D, end_date=D, **bounds
            ),
        )
        for bounds in ({}, {"min_spend": 0})
    ]
    assert pages[0].total == pages[1].total
    assert pages[0].summary == pages[1].summary
    assert pages[0].items == pages[1].items


def test_active_snapshot_watermark_preserves_old_retired_version_until_expiry(
    session, sync_seed
):
    snapshot, rows = material_snapshot(session, sync_seed)
    now = datetime.now(UTC)
    version = ReportEvidenceVersion(
        tenant_id=sync_seed.tenant_id,
        advertiser_id="acc-1",
        kind="material_snapshot",
        retired_at=now - timedelta(minutes=90),
    )
    session.add(version)
    snapshot.created_at = now - timedelta(hours=2)
    rows[0].evidence_versions = [version.id]
    session.add_all([snapshot, rows[0]])
    session.flush()
    prune_report_evidence(session, limit=10000)
    assert (
        session.connection()
        .execute(
            text("SELECT count(*) FROM report_evidence_version WHERE id=:v"),
            {"v": version.id},
        )
        .scalar_one()
        == 1
    )
    snapshot.expires_at = now - timedelta(seconds=1)
    session.add(snapshot)
    session.flush()
    prune_report_evidence(session, limit=10000)
    assert (
        session.connection()
        .execute(
            text("SELECT count(*) FROM report_evidence_version WHERE id=:v"),
            {"v": version.id},
        )
        .scalar_one()
        == 0
    )


def test_cleanup_skips_uncommitted_builder_and_shared_builders_do_not_block():
    statements = []
    with engine.connect() as first, engine.connect() as second:
        with Session(bind=first) as builder, Session(bind=second) as cleaner:
            begin_evidence_snapshot(builder)
            # 两个构建者均能取得共享锁，清理则不能越过尚未提交的构建。
            begin_evidence_snapshot(cleaner)

            def record(_conn, _cursor, statement, _parameters, _context, _executemany):
                statements.append(statement)

            event.listen(second, "before_cursor_execute", record)
            try:
                prune_report_evidence(cleaner)
                assert not any(
                    "DELETE FROM report_evidence_version" in s for s in statements
                )
                builder.rollback()
                statements.clear()
                prune_report_evidence(cleaner)
                assert any(
                    "DELETE FROM report_evidence_version" in s for s in statements
                )
            finally:
                event.remove(second, "before_cursor_execute", record)
                cleaner.rollback()


def test_inline_migration_roundtrip_preserves_frozen_versions(monkeypatch):
    from alembic import command

    from tests.migration_database import historical_database
    from tests.modules.reporting.sync_conftest import sync_seed
    from tests.modules.reporting.test_report_evidence_migration import (
        _ref,
        _seed_snapshot,
    )

    # 只在0108/0109之间往返；不能在已物理退役旧素材表的head上执行旧downgrade。
    with historical_database(monkeypatch, "0105_entity_metric_days") as (
        database,
        config,
    ):
        with Session(database) as session, session.begin():
            seed = sync_seed.__wrapped__(session, historical=True)
            ref = _ref(seed.tenant_id, "acc-1", "ad", "historical-ad")
            identity = _seed_snapshot(
                session,
                seed,
                "bc-a",
                "material",
                ["acc-1"],
                [
                    (
                        "historical-material",
                        [ref],
                        [
                            {
                                "ad_ref": ref,
                                "platform_material_id": "vid",
                                "ad_material_id": "creative",
                                "material_type": "VIDEO",
                            }
                        ],
                    )
                ],
                {"directory": 1},
            )
        command.upgrade(config, "0109_snapshot_inline_evidence")
        query = text(
            "SELECT row_key,evidence_versions FROM query_snapshot_row WHERE snapshot_id=:s ORDER BY row_key"
        )
        with database.connect() as connection:
            before = connection.execute(query, {"s": identity}).all()
            assert before and before[0].evidence_versions
        command.downgrade(config, "0108_report_lookup_cover")
        with database.connect() as connection:
            linked = (
                connection.execute(
                    text(
                        "SELECT version_id FROM query_snapshot_evidence WHERE snapshot_id=:s ORDER BY version_id"
                    ),
                    {"s": identity},
                )
                .scalars()
                .all()
            )
            assert linked == sorted(before[0].evidence_versions)
        command.upgrade(config, "0109_snapshot_inline_evidence")
        with database.connect() as connection:
            assert connection.execute(query, {"s": identity}).all() == before
