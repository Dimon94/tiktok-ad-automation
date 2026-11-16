"""生产同数量级合成账户池；计时只用于证据，回归以实际扫描对象和结果为准。"""

import json
from time import monotonic

from sqlalchemy import event, text

from app.modules.reporting.queries import _build_snapshot, _read_page
from tests.modules.reporting.test_queries import filters_for


def test_five_thousand_accounts_and_hundred_thousand_campaigns_have_bounded_snapshot(
    report_case, record_property
):
    case = report_case
    connection = case.session.connection()
    params = {"t": case.context.tenant_id, "bc": case.bc_id}
    connection.execute(
        text("""INSERT INTO advertiser_account
      (tenant_id,advertiser_id,name,currency,timezone,remote_status,ownership_conflict)
      SELECT :t,'scale-'||i,'account-'||i,'USD','UTC','ENABLE',false FROM generate_series(1,5000)i"""),
        params,
    )
    connection.execute(
        text("""INSERT INTO bc_account_access
      (tenant_id,bc_id,advertiser_id,connection_id,in_bc,authorized,active,can_upload,can_build,permission_state)
      SELECT g.tenant_id,g.bc_id,'scale-'||i,g.connection_id,true,true,true,g.can_upload,g.can_build,g.permission_state
      FROM generate_series(1,5000)i CROSS JOIN bc_account_access g
      WHERE g.tenant_id=:t AND g.bc_id=:bc AND g.advertiser_id='report-account'"""),
        params,
    )
    started = monotonic()
    connection.execute(
        text("""INSERT INTO ad_object
      (tenant_id,advertiser_id,kind,remote_id,ad_type,name,configuration,observed_at,published_version)
      SELECT :t,'scale-'||a,'campaign','c-'||i,'REGULAR','campaign','{}',now(),1
      FROM generate_series(1,5000)a CROSS JOIN generate_series(1,20)i"""),
        params,
    )
    connection.execute(
        text("""INSERT INTO campaign_report_day
      (tenant_id,advertiser_id,stat_date,campaign_id,campaign_name,spend,currency,timezone,fetched_at,sync_job_id)
      SELECT :t,'scale-'||a,date '2026-09-30','c-'||i,'campaign',1,'USD','UTC',now(),gen_random_uuid()
      FROM generate_series(1,5000)a CROSS JOIN generate_series(1,20)i"""),
        params,
    )
    record_property("synthetic_publish_seconds", monotonic() - started)
    for table in (
        "account_report_day",
        "account_report_day_state",
        "account_membership_current",
        "bc_account_access",
        "advertiser_account",
    ):
        connection.execute(text(f"ANALYZE {table}"))
    captured = []
    sql_calls = 0

    def capture(_conn, _cursor, statement, values, _context, _many):
        nonlocal sql_calls
        sql_calls += 1
        if "INSERT INTO query_snapshot_row" in statement:
            captured.append((statement, values))

    event.listen(connection, "before_cursor_execute", capture)
    filters = filters_for("account", sort_by="spend", sort_direction="desc")
    started = monotonic()
    try:
        snapshot = _build_snapshot(
            case.session, context=case.context, bc_id=case.bc_id, filters=filters
        )
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    snapshot_seconds = monotonic() - started
    record_property("snapshot_seconds", snapshot_seconds)
    record_property("snapshot_sql_calls", sql_calls)
    started = monotonic()
    page = _read_page(
        case.session,
        context=case.context,
        bc_id=case.bc_id,
        filters=filters,
        snapshot=snapshot,
        cursor=None,
        limit=50,
    )
    record_property("page_seconds", monotonic() - started)
    assert page.total == 5001
    assert page.summary["buckets"][0]["values"]["spend"] == "100000"
    assert len(page.items) == 50
    assert all(row.metric_buckets[0].values["spend"] == 20 for row in page.items)
    assert len(snapshot.naming_versions) == 5002
    assert (
        connection.execute(
            text(
                "SELECT sum(jsonb_array_length(refs)) FROM query_snapshot_row WHERE snapshot_id=:s"
            ),
            {"s": snapshot.id},
        ).scalar_one()
        == 0
    )
    # 直接使用Connection保存点，确保诊断INSERT回滚后仍是原有完整冻结行。
    statement, values = captured[0]
    with connection.begin_nested() as savepoint:
        connection.execute(
            text("DELETE FROM query_snapshot_row WHERE snapshot_id=:s"),
            {"s": snapshot.id},
        )
        plan = connection.exec_driver_sql(
            "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + statement, values
        ).scalar_one()[0]["Plan"]
        savepoint.rollback()

    def nodes(node):
        yield node
        for child in node.get("Plans", []):
            yield from nodes(child)

    active = [node for node in nodes(plan) if node.get("Actual Loops", 0) > 0]
    relations = {node.get("Relation Name") for node in active}
    assert not relations & {
        "ad_object",
        "ad_entity_read",
        "campaign_name_current",
        "account_membership_version",
    }
    facts = [
        node for node in active if node.get("Relation Name") == "campaign_report_day"
    ]
    assert facts
    assert (
        sum(
            (n["Actual Rows"] + n.get("Rows Removed by Filter", 0)) * n["Actual Loops"]
            for n in facts
        )
        <= 200000
    )
    record_property("snapshot_plan", json.dumps(plan))

    from collections import defaultdict
    from decimal import Decimal
    from hashlib import sha256

    from fastapi.encoders import jsonable_encoder
    from starlette.responses import JSONResponse

    from app.modules.reporting.frozen_evidence import read_snapshot_evidence

    def digest(value):
        return sha256(
            json.dumps(
                value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest()

    summary_bytes = connection.execute(
        text("SELECT octet_length(summary::text) FROM query_snapshot WHERE id=:s"),
        {"s": snapshot.id},
    ).scalar_one()
    row_bytes = connection.execute(
        text(
            "SELECT sum(octet_length(frozen_result::text)) FROM query_snapshot_row WHERE snapshot_id=:s"
        ),
        {"s": snapshot.id},
    ).scalar_one()
    response_bytes = len(JSONResponse(jsonable_encoder(page)).body)
    measurements = {
        "accounts": page.total,
        "snapshot_seconds": snapshot_seconds,
        "snapshot_sql_calls": sql_calls,
        "summary_jsonb_bytes": summary_bytes,
        "full_frozen_rows_jsonb_bytes": row_bytes,
        "serialized_http_bytes": response_bytes,
        "publication_map_bytes": len(
            json.dumps(page.snapshot.publication_map).encode()
        ),
    }
    for key, value in measurements.items():
        record_property(key, value)
    print(  # noqa: T201
        "QUERY_CAPACITY_SIZE " + json.dumps(measurements, sort_keys=True), flush=True
    )
    assert summary_bytes < 64000
    assert response_bytes < 2 * 1024 * 1024
    assert not any(row.source_evidence for row in page.items)
    reference = page.summary["buckets"][0]["evidence_ref"]
    assert reference["record_count"] == 100000
    cursor, records_by_row, identities = None, defaultdict(list), set()
    proof_started = monotonic()
    while True:
        proof = read_snapshot_evidence(
            case.session,
            context=case.context,
            bc_id=case.bc_id,
            snapshot_id=snapshot.id,
            bucket=reference["bucket"],
            cursor=cursor,
            limit=100,
        ).evidence_page
        assert len(proof.records) <= 100
        for record in proof.records:
            identities.add((record["advertiser_id"], record["remote_id"]))
            records_by_row[record["selected_row_key"]].append(record)
            assert Decimal(record["fact"]["spend"]) == 1
            assert (
                record["publication"] == {}
                and record["contract_revision"] == "LEGACY_UNKNOWN"
            )
            assert record["returned"] is True
        cursor = proof.next_cursor
        if cursor is None:
            break
    assert identities == {
        (f"scale-{a}", f"c-{i}") for a in range(1, 5001) for i in range(1, 21)
    }
    # 消费者从冻结指标页取各行scope，再用实际分页交付的原件独立复算总摘要。
    row_scopes = {}
    metric_page = page
    while True:
        for row in metric_page.items:
            ref = row.metric_buckets[0].evidence_ref
            row_scopes[row.row_key] = ref.scope_digest
        if not metric_page.next_cursor:
            break
        metric_page = _read_page(
            case.session,
            context=case.context,
            bc_id=case.bc_id,
            filters=filters,
            snapshot=snapshot,
            cursor=metric_page.next_cursor,
            limit=100,
        )
    parts = [
        [key, scope, len(records_by_row[key]), digest(records_by_row[key])]
        for key, scope in sorted(row_scopes.items())
    ]
    scope = digest(
        [
            {
                k: snapshot.filters.get(k)
                for k in ("dimension", "start_date", "end_date", "contribution")
            },
            [[p[0], p[1]] for p in parts],
        ]
    )
    assert scope == reference["scope_digest"]
    assert digest([scope, reference["bucket"], parts]) == reference["evidence_digest"]
    measurements = {
        "accounts": page.total,
        "facts": len(identities),
        "snapshot_seconds": snapshot_seconds,
        "snapshot_sql_calls": sql_calls,
        "summary_jsonb_bytes": summary_bytes,
        "full_frozen_rows_jsonb_bytes": row_bytes,
        "serialized_http_bytes": response_bytes,
        "publication_map_bytes": len(
            json.dumps(page.snapshot.publication_map).encode()
        ),
        "whole_population_proof_seconds": monotonic() - proof_started,
    }
    for key, value in measurements.items():
        record_property(key, value)
    print(  # noqa: T201
        "QUERY_CAPACITY_PROOF " + json.dumps(measurements, sort_keys=True)
    )
