"""真实候选入口：历史目录只扫描一次，保留冻结范围与ID成员语义。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, text

from app.core.config import settings
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting.directory_refresh import request_link_directory
from app.modules.reporting.store_models import MaterialPostLink, ReportSyncJob
from tests.modules.reporting.test_backlog_control import request
from tests.modules.reporting.test_directory_refresh_pending_plan import (
    _prepare_actual_pending_query,
)
from tests.modules.reporting.test_jobs import _route
from tests.modules.reporting.test_material_query import D, material_row, publish
from tests.modules.reporting.test_sync_probe_indexes import _scanned


def _capture(session, job, now):
    captured = []

    class Captured(Exception):
        pass

    def capture(_conn, _cursor, sql, params, _context, _many):
        if "SELECT DISTINCT l.ad_remote_id" in sql:
            captured.append((sql, params))
            raise Captured

    connection = session.connection()
    event.listen(connection, "before_cursor_execute", capture)
    try:
        with pytest.raises(Captured):
            request_link_directory(session, job, now=now)
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert len(captured) == 1
    return captured[0]


def _seed(session, seed, count=100, material_type="VIDEO_SPARK_ADS"):
    now = datetime.now(UTC)
    publish(
        session,
        seed,
        "acc-1",
        [
            material_row(f"mid-{i}", "1")
            | {
                "dimensions": material_row(f"mid-{i}", "1")["dimensions"]
                | {"main_material_type": material_type}
            }
            for i in range(count)
        ],
    )
    session.add_all(
        MaterialPostLink(
            tenant_id=seed.tenant_id,
            advertiser_id="acc-1",
            main_material_id=f"mid-{i}",
            ad_remote_id=f"ad-{i % 20:02}",
            main_material_type=material_type,
            observed_at=now,
        )
        for i in range(count)
    )
    identity = request(session, seed, D)
    session.flush()
    return session.get(ReportSyncJob, identity), now


def _run(seed, now, **changes):
    fields = {
        "tenant_id": seed.tenant_id,
        "advertiser_id": "acc-1",
        "bc_id": "bc-a",
        "actor_id": seed.actor_id,
        "connection_id": seed.connection.id,
        "frozen_route": _route(seed),
        "partition_key": "a" * 64,
        "query": {"ids": ["ad-00"], "include_deleted": True},
        "status": "COMPLETE",
        "kind": "ad",
        "ad_type": "SMART_PLUS",
        "created_at": now - timedelta(days=1),
    }
    return AdDirectoryRun(**(fields | changes))


@pytest.mark.parametrize("count", [100, 500, 2000])
@pytest.mark.parametrize("mode", ["force_custom_plan", "force_generic_plan"])
def test_candidate_history_scan_does_not_repeat_per_mapped_post(
    session, sync_seed, monkeypatch, record_property, mode, count
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    job, now = _seed(session, sync_seed, count=count)
    old = _run(sync_seed, now)
    recent = _run(sync_seed, now, created_at=now - timedelta(minutes=1))
    session.add_all([old, recent])
    session.flush()
    connection = session.connection()
    connection.execute(
        text("""
      INSERT INTO ad_directory_run OVERRIDING USER VALUE
      SELECT (jsonb_populate_record(NULL::ad_directory_run,to_jsonb(r)||jsonb_build_object(
        'id',gen_random_uuid(),'request_id',gen_random_uuid(),
        'query',jsonb_build_object('ids',jsonb_build_array('old-'||i),'include_deleted',true,'padding',repeat(md5(i::text),16))))).*
      FROM ad_directory_run r CROSS JOIN generate_series(1,20000) i WHERE r.id=:id
    """),
        {"id": old.id},
    )
    for table in (
        "ad_directory_run",
        "material_report_day",
        "material_post_link",
        "ad_object",
        "ad_material_reference",
    ):
        connection.execute(text(f"ANALYZE {table}"))
    captured = _capture(session, job, now)
    connection.execute(text(f"SET LOCAL plan_cache_mode={mode}"))
    name, execute = _prepare_actual_pending_query(connection, *captured)
    try:
        rows = connection.exec_driver_sql(execute).mappings().all()
        assert {row["ad_remote_id"] for row in rows} == {
            f"ad-{i:02}" for i in range(1, 20)
        }
        explanation = connection.exec_driver_sql(
            "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + execute
        ).scalar_one()[0]
        record_property("plan", explanation)
        scanned = _scanned(explanation["Plan"], "ad_directory_run")
        record_property("directory_rows_examined", scanned)
        assert scanned <= 20100

        def nodes(node):
            yield node
            for child in node.get("Plans", []):
                yield from nodes(child)

        # 单窗口金额聚合不能随着每个MID重做；目录去重不应遮住另一平方成本。
        aggregates = [
            node
            for node in nodes(explanation["Plan"])
            if node["Node Type"] == "Aggregate"
        ]
        record_property(
            "aggregate_loops", [node["Actual Loops"] for node in aggregates]
        )
        assert all(node["Actual Loops"] <= 1 for node in aggregates)
        facts_examined = _scanned(explanation["Plan"], "material_report_day")
        record_property("fact_rows_examined", facts_examined)
        # 所有规模都只从金额聚合读事实，不能再按每个映射回扫逐日事实。
        assert facts_examined <= count * 4

    finally:
        connection.exec_driver_sql(f"DEALLOCATE {name}")


@pytest.mark.parametrize(
    "case,blocked",
    [
        ("QUEUED", True),
        ("RUNNING", True),
        ("ADMISSION_WAIT", True),
        ("WAITING_REMOTE", True),
        ("COMPLETE", False),
        ("FAILED", False),
        ("CANCELLED", False),
        ("STALE", False),
        ("recent_COMPLETE", True),
        ("recent_FAILED", True),
        ("recent_CANCELLED", True),
        ("recent_STALE", True),
        ("cutoff", True),
        ("expired", False),
        ("bc", False),
        ("actor", False),
        ("route", False),
        ("account", False),
        ("tenant", False),
        ("kind", False),
        ("type", False),
        ("background_for_live", False),
        ("live_for_background", True),
        ("include_false", False),
        ("include_string", True),
        ("parent_only", False),
        ("ids_object", True),
        ("ids_scalar", True),
        ("ids_number", False),
        ("ids_null", False),
    ],
)
def test_blocking_candidates_keep_original_scope_and_json_membership(
    session, sync_seed, monkeypatch, case, blocked
):
    from inspect import unwrap

    from tests.modules.reporting.sync_conftest import sync_seed as seed_fixture

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    job, now = _seed(session, sync_seed, count=1)
    fields = {"status": "QUEUED"}
    if case in {
        "QUEUED",
        "RUNNING",
        "ADMISSION_WAIT",
        "WAITING_REMOTE",
        "COMPLETE",
        "FAILED",
        "CANCELLED",
        "STALE",
    }:
        fields["status"] = case
    elif case.startswith("recent_"):
        fields.update(
            status=case.removeprefix("recent_"), created_at=now - timedelta(minutes=1)
        )
    elif case in {"cutoff", "expired"}:
        fields.update(
            status="COMPLETE",
            created_at=now - timedelta(minutes=10, microseconds=case == "expired"),
        )
    elif case == "bc":
        fields["bc_id"] = "bc-b"
        fields["frozen_route"] = _route(sync_seed) | {"bc_id": "bc-b"}
    elif case == "route":
        fields["frozen_route"] = _route(sync_seed) | {"binding_revision": 99}
    elif case == "account":
        fields["advertiser_id"] = "acc-2"
    elif case in {"actor", "tenant"}:
        other = unwrap(seed_fixture)(session)
        fields["actor_id"] = other.actor_id
        if case == "tenant":
            fields.update(
                tenant_id=other.tenant_id,
                connection_id=other.connection.id,
                frozen_route=_route(other),
            )
    elif case == "kind":
        fields["kind"] = "campaign"
    elif case == "type":
        fields["ad_type"] = "REGULAR"
    elif case == "background_for_live":
        now = datetime(D.year, D.month, D.day, 12, tzinfo=UTC)
        fields["workload"] = "background"
    elif case == "live_for_background":
        fields["workload"] = "live"
    else:
        query = {"ids": ["ad-00"], "include_deleted": True}
        if case == "include_false":
            query["include_deleted"] = False
        elif case == "include_string":
            query["include_deleted"] = "true"
        elif case == "parent_only":
            query = {"parent_ids": ["ad-00"], "include_deleted": True}
        elif case == "ids_object":
            query["ids"] = {"ad-00": False}
        elif case == "ids_scalar":
            query["ids"] = "ad-00"
        elif case == "ids_number":
            query["ids"] = [0]
        elif case == "ids_null":
            query["ids"] = None
        fields["query"] = query
    session.add(_run(sync_seed, now, **fields))
    session.flush()
    connection = session.connection()
    name, execute = _prepare_actual_pending_query(
        connection, *_capture(session, job, now)
    )
    try:
        rows = connection.exec_driver_sql(execute).mappings().all()
        assert [r["ad_remote_id"] for r in rows] == ([] if blocked else ["ad-00"])
    finally:
        connection.exec_driver_sql(f"DEALLOCATE {name}")


@pytest.mark.parametrize(
    "spark,bridge,complete,expected",
    [
        (True, False, True, False),
        (True, False, False, True),
        (True, True, True, True),
        (False, False, True, True),
    ],
)
def test_spark_complete_directory_guard_is_unchanged(
    session, sync_seed, monkeypatch, spark, bridge, complete, expected
):
    from sqlmodel import select

    from app.modules.ads.models import AdMaterialReference, AdObject

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    job, now = _seed(
        session,
        sync_seed,
        count=1,
        material_type="VIDEO_SPARK_ADS" if spark else "VIDEO_NON_SPARK_ADS",
    )
    link = session.exec(
        select(MaterialPostLink).where(
            MaterialPostLink.tenant_id == sync_seed.tenant_id
        )
    ).one()
    link.main_material_type = "VIDEO_SPARK_ADS" if spark else "VIDEO_NON_SPARK_ADS"
    link.ad_material_id = "bridge" if bridge else None
    session.add(
        AdObject(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            kind="ad",
            remote_id="ad-00",
            ad_type="SMART_PLUS",
            observed_at=now,
            published_version=1,
        )
    )
    session.add(
        AdMaterialReference(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            ad_remote_id="ad-00",
            ad_material_id="other",
            platform_material_id="vid",
            material_type="VIDEO",
            complete=complete,
            published_version=1,
        )
    )
    session.flush()
    connection = session.connection()
    name, execute = _prepare_actual_pending_query(
        connection, *_capture(session, job, now)
    )
    try:
        assert bool(connection.exec_driver_sql(execute).mappings().all()) == expected
    finally:
        connection.exec_driver_sql(f"DEALLOCATE {name}")


@pytest.mark.parametrize(
    "case,expected",
    [
        ("multiple_days", True),
        ("net_zero", False),
        ("revenue_only", True),
        ("null_amounts", False),
        ("before_window", False),
        ("after_window", False),
        ("other_account", False),
        ("other_tenant", False),
        ("already_linked", False),
        ("empty_video", True),
    ],
)
def test_value_candidates_keep_exact_fact_scope_and_net_amounts(
    session, sync_seed, monkeypatch, case, expected
):
    from uuid import uuid4

    from app.modules.reporting.store_models import MaterialReportDay

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    job, now = _seed(session, sync_seed, count=1)
    job.end_date = D + timedelta(days=1)
    # 同 MID 的多日事实只生成一个候选；NULL 金额、其他账户/租户及窗外正额不补漏。
    values = [(D, "1", None), (D + timedelta(days=1), "2", None)]
    if case == "net_zero":
        values = [(D, "1", None), (D + timedelta(days=1), "-1", None)]
    elif case == "revenue_only":
        values = [(D, None, "1")]
    elif case == "null_amounts":
        values = [(D, None, None)]
    elif case == "before_window":
        values = [(D - timedelta(days=1), "1", None), (D, "0", None)]
    elif case == "after_window":
        values = [(D + timedelta(days=2), "1", None), (D, "0", None)]
    for day, spend, revenue in values:
        session.add(
            MaterialReportDay(
                tenant_id=uuid4() if case == "other_tenant" else sync_seed.tenant_id,
                advertiser_id="acc-2" if case == "other_account" else "acc-1",
                stat_date=day,
                main_material_id="extra-mid",
                main_material_type="VIDEO_SPARK_ADS",
                currency="USD",
                timezone="Asia/Hong_Kong",
                fetched_at=now,
                sync_job_id=job.id,
                spend=spend,
                revenue_total=revenue,
            )
        )
    session.add(
        MaterialPostLink(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            main_material_id="extra-mid",
            ad_remote_id="extra-ad",
            platform_material_id="" if case == "empty_video" else None,
            main_material_type="VIDEO_SPARK_ADS",
            observed_at=now,
        )
    )
    if case == "already_linked":
        session.add(
            MaterialPostLink(
                tenant_id=sync_seed.tenant_id,
                advertiser_id="acc-1",
                main_material_id="extra-mid",
                ad_remote_id="other-ad",
                platform_material_id="known-video",
                main_material_type="VIDEO_SPARK_ADS",
                observed_at=now,
            )
        )
    session.flush()
    connection = session.connection()
    name, execute = _prepare_actual_pending_query(
        connection, *_capture(session, job, now)
    )
    try:
        rows = connection.exec_driver_sql(execute).mappings().all()
        assert [r["ad_remote_id"] for r in rows] == (
            ["ad-00", "extra-ad"] if expected else ["ad-00"]
        )
    finally:
        connection.exec_driver_sql(f"DEALLOCATE {name}")


@pytest.mark.parametrize("bounds", ["null_start", "null_end", "inverted"])
def test_candidate_invalid_dates_do_not_open_the_window(
    session, sync_seed, monkeypatch, bounds
):
    from types import SimpleNamespace

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    stored, now = _seed(session, sync_seed, count=1)
    # 持久化日期非空；也保留临时调用者原 BETWEEN NULL 不匹配的行为。
    job = SimpleNamespace(**stored.model_dump())
    if bounds == "null_start":
        job.start_date = None
    elif bounds == "null_end":
        job.end_date = None
    else:
        job.start_date = D + timedelta(days=1)
    connection = session.connection()
    name, execute = _prepare_actual_pending_query(
        connection, *_capture(session, job, now)
    )
    try:
        assert connection.exec_driver_sql(execute).all() == []
    finally:
        connection.exec_driver_sql(f"DEALLOCATE {name}")


def test_candidate_order_and_limit_are_applied_after_distinct(
    session, sync_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    job, now = _seed(session, sync_seed, count=120)
    connection = session.connection()
    connection.execute(
        text("""
        UPDATE material_post_link SET ad_remote_id='ad-' ||
          lpad(split_part(main_material_id,'-',2),3,'0')
        WHERE tenant_id=:tenant AND advertiser_id='acc-1'
        """),
        {"tenant": sync_seed.tenant_id},
    )
    name, execute = _prepare_actual_pending_query(
        connection, *_capture(session, job, now)
    )
    try:
        rows = connection.exec_driver_sql(execute).mappings().all()
        assert [r["ad_remote_id"] for r in rows] == [f"ad-{i:03}" for i in range(100)]
    finally:
        connection.exec_driver_sql(f"DEALLOCATE {name}")
