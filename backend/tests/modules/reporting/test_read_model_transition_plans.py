"""同连接不同transition规模必须重规划，旧新身份失效仍与事实写入原子提交。"""

import json
from contextlib import contextmanager
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from tests.modules.reporting.test_material_query import D
from tests.modules.reporting.test_material_value import fact


def _nodes(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from _nodes(child)


@contextmanager
def _plans(session):
    connection = session.connection()
    raw = connection.connection.driver_connection
    plans = []

    def notice(message):
        value = message.message_primary
        if "plan:\n" not in value or not any(
            marker in value
            for marker in (
                "current_ad_material",
                "FROM material_report_day f",
                "JOIN material_report_day f",
            )
        ):
            return
        parsed = json.loads(value.split("plan:\n", 1)[1])
        if any(
            marker in parsed.get("Query Text", "")
            for marker in ("FROM new_rows", "FROM old_rows")
        ):
            plans.append(parsed)

    raw.add_notice_handler(notice)
    try:
        for setting in (
            "LOAD 'auto_explain'",
            "SET LOCAL auto_explain.log_min_duration='0ms'",
            "SET LOCAL auto_explain.log_nested_statements=on",
            "SET LOCAL auto_explain.log_analyze=on",
            "SET LOCAL auto_explain.log_buffers=on",
            "SET LOCAL auto_explain.log_timing=off",
            "SET LOCAL auto_explain.log_format=json",
            "SET LOCAL client_min_messages=log",
        ):
            connection.execute(text(setting))
        yield plans
    finally:
        raw.remove_notice_handler(notice)
        connection.execute(text("SET LOCAL auto_explain.log_min_duration=-1"))


def _seed(session, seed, family, tag, n):
    if family == "link":
        statement = """INSERT INTO material_post_link
        (tenant_id,advertiser_id,main_material_id,ad_remote_id,main_material_type,platform_material_id,observed_at)
        SELECT :tenant,'acc-1','post-'||i,:tag||i,'VIDEO_SPARK_ADS','video-'||i,now()
        FROM generate_series(1,:n)i"""
    else:
        statement = """INSERT INTO ad_material_reference
        (id,tenant_id,advertiser_id,ad_remote_id,platform_material_id,ad_material_id,material_type,name,creative_ids,complete,published_version)
        SELECT gen_random_uuid(),:tenant,'acc-1',:tag||i,'video-'||i,i::text,'VIDEO','material-'||i,'[]'::jsonb,true,1
        FROM generate_series(1,:n)i"""
    session.execute(text(statement), {"tenant": seed.tenant_id, "tag": tag, "n": n})


@pytest.mark.parametrize("family", ["link", "reference"])
@pytest.mark.parametrize("operation", ["INSERT", "UPDATE", "DELETE"])
def test_transition_estimate_tracks_small_large_small_on_one_backend(
    session, sync_seed, family, operation, record_property
):
    if family == "link":
        # 两个日期分区确保确实执行事实关联，不能因事实为空跳过transition。
        session.execute(
            text("""INSERT INTO material_report_day
          (tenant_id,advertiser_id,stat_date,main_material_id,main_material_type,currency,timezone,fetched_at,sync_job_id,spend,revenue_total)
          SELECT :tenant,'acc-1',CAST(:day AS date)-d,'post-'||i,'VIDEO_SPARK_ADS','USD','UTC',now(),gen_random_uuid(),1,0
          FROM generate_series(1,1000)i CROSS JOIN (VALUES(0),(60))days(d)"""),
            {"tenant": sync_seed.tenant_id, "day": D},
        )
    table = "material_post_link" if family == "link" else "ad_material_reference"
    counts = []
    for phase, n in enumerate([1] * 6 + [1000, 1]):
        tag = f"phase-{phase}-"
        if operation != "INSERT":
            _seed(session, sync_seed, family, tag, n)
        with _plans(session) as plans:
            if operation == "INSERT":
                _seed(session, sync_seed, family, tag, n)
            else:
                assignment = (
                    "observed_at=clock_timestamp()"
                    if family == "link"
                    else "name='updated'"
                )
                prefix = (
                    f"UPDATE {table} SET {assignment}"
                    if operation == "UPDATE"
                    else f"DELETE FROM {table}"
                )
                session.execute(
                    text(
                        prefix + " WHERE tenant_id=:tenant AND ad_remote_id LIKE :tag"
                    ),
                    {"tenant": sync_seed.tenant_id, "tag": tag + "%"},
                )
        transitions = [
            node
            for p in plans
            for node in _nodes(p["Plan"])
            if node["Node Type"] == "Named Tuplestore Scan" and node["Actual Loops"] > 0
        ]
        assert transitions, plans
        counts.append([(x["Plan Rows"], x["Actual Rows"]) for x in transitions])
        # 固定SQL仍须使用本次transition实际基数，不能以LIMIT/总耗时掩盖缓存1行。
        assert all(
            x["Actual Rows"] == n and x["Plan Rows"] == n for x in transitions
        ), counts
    record_property("transition_estimates", json.dumps(counts))


def _buckets(session, tenant):
    return {
        (r.advertiser_id, r.kind, r.bucket): (r.source_revision, r.derived_revision)
        for r in session.execute(
            text("SELECT * FROM read_bucket_state WHERE tenant_id=:tenant"),
            {"tenant": tenant},
        )
    }


def test_link_identity_move_dirties_old_new_dates_and_rollback_is_atomic(
    session, sync_seed
):
    fact(session, sync_seed, "old", "1", "0", day=D)
    fact(session, sync_seed, "new", "1", "0", day=D - timedelta(days=60), adv="acc-2")
    fact(session, sync_seed, "unrelated", "1", "0", day=D - timedelta(days=1))
    initial = _buckets(session, sync_seed.tenant_id)
    sp = session.begin_nested()
    session.execute(
        text("""INSERT INTO material_post_link
        (tenant_id,advertiser_id,main_material_id,ad_remote_id,main_material_type,observed_at)
        VALUES(:tenant,'acc-1','old','ad','VIDEO_SPARK_ADS',now())"""),
        {"tenant": sync_seed.tenant_id},
    )
    inserted = _buckets(session, sync_seed.tenant_id)
    old = ("acc-1", "material_day", D)
    new = ("acc-2", "material_day", D - timedelta(days=60))
    unrelated = ("acc-1", "material_day", D - timedelta(days=1))
    assert inserted[old][0] == initial[old][0] + 1
    assert inserted[new] == initial[new]
    session.execute(
        text(
            "UPDATE material_post_link SET advertiser_id='acc-2',main_material_id='new' WHERE tenant_id=:tenant"
        ),
        {"tenant": sync_seed.tenant_id},
    )
    moved = _buckets(session, sync_seed.tenant_id)
    assert (
        moved[old][0] == inserted[old][0] + 1 and moved[new][0] == inserted[new][0] + 1
    )
    assert moved[unrelated] == initial[unrelated]
    session.execute(
        text("DELETE FROM material_post_link WHERE tenant_id=:tenant"),
        {"tenant": sync_seed.tenant_id},
    )
    deleted = _buckets(session, sync_seed.tenant_id)
    assert deleted[old] == moved[old] and deleted[new][0] == moved[new][0] + 1
    sp.rollback()
    assert _buckets(session, sync_seed.tenant_id) == initial
    assert (
        session.execute(
            text("SELECT count(*) FROM material_post_link WHERE tenant_id=:tenant"),
            {"tenant": sync_seed.tenant_id},
        ).scalar_one()
        == 0
    )


def test_reference_identity_move_keeps_precise_latest_ties_and_atomic_rollback(
    session, sync_seed
):
    ids = {
        key: uuid4() for key in ("generic", "a", "b", "old", "incomplete", "neighbor")
    }
    for key, material, version, complete, account in [
        ("generic", None, 9, True, "acc-1"),
        ("a", "a", 2, True, "acc-1"),
        ("b", "b", 2, True, "acc-1"),
        ("old", "old", 1, True, "acc-1"),
        ("incomplete", "incomplete", 20, False, "acc-1"),
        ("neighbor", "neighbor", 1, True, "acc-2"),
    ]:
        session.execute(
            text("""INSERT INTO ad_material_reference(id,tenant_id,advertiser_id,ad_remote_id,platform_material_id,ad_material_id,material_type,name,creative_ids,complete,published_version)
            VALUES(:id,:tenant,:account,'ad','video',:material,'VIDEO','name','[]'::jsonb,:complete,:version)"""),
            {
                "id": ids[key],
                "tenant": sync_seed.tenant_id,
                "account": account,
                "material": material,
                "complete": complete,
                "version": version,
            },
        )

    def projected():
        return set(
            session.execute(
                text("SELECT id FROM current_ad_material WHERE tenant_id=:tenant"),
                {"tenant": sync_seed.tenant_id},
            ).scalars()
        )

    assert projected() == {ids["a"], ids["b"], ids["neighbor"]}
    initial = _buckets(session, sync_seed.tenant_id)
    sp = session.begin_nested()
    session.execute(
        text(
            "UPDATE ad_material_reference SET advertiser_id='acc-2',ad_remote_id='moved',platform_material_id='new-video' WHERE id IN (:a,:b)"
        ),
        {"a": ids["a"], "b": ids["b"]},
    )
    assert projected() == {ids["old"], ids["a"], ids["b"], ids["neighbor"]}
    moved = _buckets(session, sync_seed.tenant_id)
    for account in ("acc-1", "acc-2"):
        key = next(k for k in initial if k[0] == account and k[1] == "usage")
        assert moved[key][0] == initial[key][0] + 1 and moved[key][0] == moved[key][1]
    session.execute(
        text("DELETE FROM ad_material_reference WHERE id IN (:a,:b)"),
        {"a": ids["a"], "b": ids["b"]},
    )
    assert projected() == {ids["old"], ids["neighbor"]}
    sp.rollback()
    assert projected() == {ids["a"], ids["b"], ids["neighbor"]}
    assert _buckets(session, sync_seed.tenant_id) == initial


def test_fixed_migration_roundtrip_matches_generated_functions(session):
    import runpy
    from pathlib import Path

    from app.modules.reporting.read_models.ddl import trigger_statements

    migration = runpy.run_path(
        str(
            Path(__file__).resolve().parents[3]
            / "app/alembic/versions/0099_transition_replan.py"
        )
    )
    expected = [
        s.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
        for s in trigger_statements()
        if s.startswith(
            (
                "CREATE FUNCTION qrm_material_post_link_",
                "CREATE FUNCTION qrm_ad_material_reference_",
            )
        )
    ]
    assert migration["UPGRADE"] == tuple(expected)
    names = [s.split("()", 1)[0].split()[-1] for s in migration["UPGRADE"]]

    def definitions():
        return {
            name: session.execute(
                text("SELECT pg_get_functiondef(CAST(:function AS regprocedure))"),
                {"function": name + "()"},
            ).scalar_one()
            for name in names
        }

    before = definitions()
    savepoint = session.begin_nested()
    for statement in migration["DOWNGRADE"]:
        session.connection().exec_driver_sql(statement)
    assert all("$qrm_transition$" not in value for value in definitions().values())
    for statement in migration["UPGRADE"]:
        session.connection().exec_driver_sql(statement)
    assert definitions() == before
    savepoint.rollback()
    assert definitions() == before
