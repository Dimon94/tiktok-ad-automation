"""真实 PG 两次目录读取的容量、类型和本事务可见性基线。"""

from datetime import timedelta
from time import monotonic

import pytest
from sqlalchemy import event, text
from sqlmodel import select

from app.core.config import settings
from app.modules.ads.models import AdObject
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting.directory_refresh import request_link_directory
from app.modules.reporting.store_models import MaterialPostLink
from tests.modules.reporting.test_link_directory_candidate_plan import _seed


def _distinct_seed(session, seed, count, mixed=False):
    job, now = _seed(session, seed, count=max(1, count))
    session.connection().execute(
        text("""UPDATE material_post_link SET ad_remote_id='ad-' ||
        lpad(split_part(main_material_id,'-',2),4,'0')
        WHERE tenant_id=:t AND advertiser_id='acc-1'"""),
        {"t": seed.tenant_id},
    )
    if not count:
        session.connection().execute(
            text("DELETE FROM material_post_link WHERE tenant_id=:t"),
            {"t": seed.tenant_id},
        )
    if mixed:
        for i in range(count):
            session.add(
                AdObject(
                    tenant_id=seed.tenant_id,
                    advertiser_id="acc-1",
                    kind="ad",
                    remote_id=f"ad-{i:04}",
                    ad_type=("REGULAR", "SMART_PLUS", "LEGACY_SMART_PLUS")[i % 3],
                    observed_at=now,
                    published_version=1,
                )
            )
    session.flush()
    return job, now


def _runs(session, seed):
    return session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == seed.tenant_id)
    ).all()


@pytest.mark.parametrize("count", [0, 1, 99, 100, 101, 200, 201])
@pytest.mark.parametrize("mixed", [False, True])
def test_original_two_calls_keep_capacity_and_exact_runs(
    session, sync_seed, monkeypatch, record_property, count, mixed
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    job, now = _distinct_seed(session, sync_seed, count, mixed)
    queries, started = [], {}
    connection = session.connection()

    def before(_conn, _cursor, sql, _params, context, _many):
        if "SELECT DISTINCT l.ad_remote_id" in sql:
            started[id(context)] = monotonic()

    def after(_conn, _cursor, _sql, _params, context, _many):
        if id(context) in started:
            queries.append(monotonic() - started.pop(id(context)))

    event.listen(connection, "before_cursor_execute", before)
    event.listen(connection, "after_cursor_execute", after)
    try:
        first = request_link_directory(session, job, now=now)
        first_runs = _runs(session, sync_seed)
        second = request_link_directory(
            session, job, now=now + timedelta(milliseconds=1)
        )
    finally:
        event.remove(connection, "before_cursor_execute", before)
        event.remove(connection, "after_cursor_execute", after)
    runs = _runs(session, sync_seed)
    expected = [f"ad-{i:04}" for i in range(min(count, 200))]
    assert sorted(a for r in runs for a in r.query["ids"]) == expected
    assert sorted(a for r in first_runs for a in r.query["ids"]) == expected[:100]
    assert len(runs) == first + second
    assert first == (min(3, min(count, 100)) if mixed else int(count > 0))
    assert second == (
        min(3, max(0, min(count - 100, 100))) if mixed else int(count > 100)
    )
    assert all(len(r.query["ids"]) <= 100 and r.query["include_deleted"] for r in runs)
    assert len(queries) == 2
    record_property("candidate_sql_elapsed_ms", [round(v * 1000, 3) for v in queries])
    record_property("new_runs", [first, second])
    assert request_link_directory(session, job, now=now) == int(count > 200)


def test_same_transaction_mapping_insert_visible_to_second_call(
    session, sync_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    job, now = _distinct_seed(session, sync_seed, 1)
    assert request_link_directory(session, job, now=now) == 1
    session.add(
        MaterialPostLink(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            main_material_id="mid-0",
            ad_remote_id="late-ad",
            # 与已发布事实的类型一致；同MID的其它类型不能借用该事实金额。
            main_material_type="VIDEO_SPARK_ADS",
            observed_at=now,
        )
    )
    session.flush()
    assert request_link_directory(session, job, now=now) == 1
    assert sorted(a for r in _runs(session, sync_seed) for a in r.query["ids"]) == [
        "ad-0000",
        "late-ad",
    ]


def test_double_aggregate_cost_and_two_batch_equivalent_snapshot_candidates(
    session, sync_seed, monkeypatch, record_property
):
    from tests.modules.reporting.test_link_directory_candidate_plan import (
        _capture,
        _run,
    )

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    job, now = _distinct_seed(session, sync_seed, 2000)
    old = _run(sync_seed, now)
    session.add(old)
    session.flush()
    conn = session.connection()
    conn.execute(
        text("""
        INSERT INTO ad_directory_run OVERRIDING USER VALUE
        SELECT (jsonb_populate_record(NULL::ad_directory_run,to_jsonb(r)||jsonb_build_object(
          'id',gen_random_uuid(),'request_id',gen_random_uuid(),
          'query',jsonb_build_object('ids',jsonb_build_array('old-'||i),'include_deleted',true)))).*
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
        conn.execute(text(f"ANALYZE {table}"))
    sql, params = _capture(session, job, now)
    assert params["limit"] == 100
    elapsed, started = [], {}

    def before(_conn, _cursor, statement, _params, context, _many):
        if "SELECT DISTINCT l.ad_remote_id" in statement:
            started[id(context)] = monotonic()

    def after(_conn, _cursor, _sql, _params, context, _many):
        if id(context) in started:
            elapsed.append((monotonic() - started.pop(id(context))) * 1000)

    event.listen(conn, "before_cursor_execute", before)
    event.listen(conn, "after_cursor_execute", after)
    try:
        # 只在本地改查询参数测候选快照；不修改业务入口或提交语义。
        snapshot = conn.exec_driver_sql(sql, params | {"limit": 200}).all()
        assert len(snapshot) == 200
        assert request_link_directory(session, job, now=now) == 1
        assert request_link_directory(session, job, now=now) == 1
    finally:
        event.remove(conn, "before_cursor_execute", before)
        event.remove(conn, "after_cursor_execute", after)
    actual = sorted(
        a
        for r in _runs(session, sync_seed)
        if r.status == "QUEUED"
        for a in r.query["ids"]
    )
    assert actual == sorted(r.ad_remote_id for r in snapshot)
    assert len(elapsed) == 3
    record_property("snapshot_200_sql_ms", round(elapsed[0], 3))
    record_property("original_two_sql_ms", [round(v, 3) for v in elapsed[1:]])
    record_property("fact_mid_count", 2000)
    record_property("terminal_history_runs", 20001)
