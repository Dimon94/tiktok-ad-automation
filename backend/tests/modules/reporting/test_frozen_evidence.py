"""R9: exact frozen records remain pageable after compact metric serialization."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.modules.reporting.frozen_evidence import read_snapshot_evidence
from app.modules.reporting.queries import query_ads
from app.modules.reporting.query_models import QuerySnapshot
from app.modules.reporting.schemas import ReportingFilter
from tests.modules.reporting.test_native_scope_roas import D, publish


def setup_page(session, seed):
    publish(session, seed, ("10", "90"), ("20", "-"))
    filters = ReportingFilter(
        dimension="account", start_date=D, end_date=D, advertiser_ids=("acc-1",)
    )
    return query_ads(
        session, context=seed.context, bc_id="bc-a", filters=filters
    ), filters


def evidence(session, seed, page, **kwargs):
    return read_snapshot_evidence(
        session,
        context=seed.context,
        bc_id="bc-a",
        snapshot_id=page.snapshot.snapshot_id,
        **kwargs,
    ).evidence_page


def test_summary_compact_and_one_large_row_inner_paging(session, sync_seed):
    page, filters = setup_page(session, sync_seed)
    assert len(json.dumps(page.summary)) < 64000
    assert "observations" not in json.dumps(page.summary)
    assert not page.items[0].source_evidence
    vector = page.items[0].metric_buckets[0]
    assert vector.provenance["roas_total"].known_count == 1
    assert vector.provenance["roas_total"].missing_count == 1
    assert vector.provenance["roas_total"].denominator_known_count == 2
    first = evidence(session, sync_seed, page, row_key=page.items[0].row_key, limit=1)
    assert len(first.records) == 1 and first.next_cursor
    second = evidence(
        session,
        sync_seed,
        page,
        row_key=page.items[0].row_key,
        limit=1,
        cursor=first.next_cursor,
    )
    assert len(second.records) == 1 and second.next_cursor is None
    records = [*first.records, *second.records]
    assert {r["remote_id"] for r in records} == {"c0", "c1"}
    assert sum(Decimal(r["fact"]["spend"]) for r in records) == 100
    assert {r["fields"]["revenue_total"]["status"] for r in records} == {
        "VALUE",
        "DASH",
    }
    assert all(
        r["publication"]["publication_digest"]
        and r["publication"]["publication_version"]
        for r in records
    )
    assert (
        first.evidence_digest
        == second.evidence_digest
        == vector.evidence_ref.evidence_digest
    )
    publish(session, sync_seed, ("1000", "9000"), ("2000", "9000"))
    again = evidence(session, sync_seed, page, row_key=page.items[0].row_key, limit=1)
    assert again == first
    with pytest.raises(HTTPException) as wrong:
        evidence(session, sync_seed, page, cursor=first.next_cursor, limit=1)
    assert (wrong.value.status_code, wrong.value.detail) == (
        404,
        "query_cursor_not_found",
    )
    assert (
        query_ads(
            session,
            context=sync_seed.context,
            bc_id="bc-a",
            filters=filters,
            snapshot_id=page.snapshot.snapshot_id,
        ).summary
        == page.summary
    )


def test_frozen_source_mutation_is_unavailable(session, sync_seed):
    page, _ = setup_page(session, sync_seed)
    session.execute(
        text("""UPDATE query_snapshot_row SET frozen_result=jsonb_set(frozen_result,
        '{source_evidence,0,fact,spend}', '"999"'::jsonb) WHERE snapshot_id=:s"""),
        {"s": page.snapshot.snapshot_id},
    )
    with pytest.raises(HTTPException) as changed:
        evidence(session, sync_seed, page)
    assert (changed.value.status_code, changed.value.detail) == (
        409,
        "query_snapshot_result_unavailable",
    )


@pytest.mark.parametrize("failure", ["contract", "expiry", "revoke"])
def test_evidence_respects_existing_snapshot_access(session, sync_seed, failure):
    from sqlmodel import select

    from app.modules.accounts.models import BCAccountAccess

    page, _ = setup_page(session, sync_seed)
    snapshot = session.get(QuerySnapshot, page.snapshot.snapshot_id)
    if failure == "contract":
        snapshot.result_contract = snapshot.result_contract | {
            "revision": "selected-report-result-v1"
        }
    elif failure == "expiry":
        snapshot.created_at = datetime.now(UTC) - timedelta(hours=2)
        snapshot.expires_at = datetime.now(UTC) - timedelta(hours=1)
    else:
        grant = session.exec(
            select(BCAccountAccess).where(
                BCAccountAccess.tenant_id == sync_seed.tenant_id,
                BCAccountAccess.advertiser_id == "acc-1",
                BCAccountAccess.bc_id == "bc-a",
            )
        ).one()
        grant.authorized = False
    session.flush()
    with pytest.raises(HTTPException) as denied:
        evidence(session, sync_seed, page)
    assert denied.value.status_code in {403, 409, 410}
    if failure == "contract":
        assert denied.value.detail == "query_snapshot_contract_changed"


@pytest.mark.parametrize("explain", [False, True])
def test_legacy_account_summary_resolves_same_exact_facts(report_case, explain):
    from tests.modules.reporting.test_queries import filters_for, query

    case = report_case
    case.session.execute(
        text("""INSERT INTO campaign_report_day
        (tenant_id,advertiser_id,stat_date,campaign_id,spend,currency,timezone,fetched_at,sync_job_id)
        SELECT :tenant,'report-account',:day,'legacy-'||i,1,'USD','UTC',now(),gen_random_uuid()
        FROM generate_series(1,2)i"""),
        {"tenant": case.context.tenant_id, "day": case.fact_date},
    )
    from sqlalchemy import event

    statements = []
    connection = case.session.connection()

    def capture(_conn, _cursor, statement, parameters, _context, _many):
        if "INSERT INTO query_snapshot_row" in statement:
            statements.append((statement, parameters))

    event.listen(connection, "before_cursor_execute", capture)
    try:
        page = query(case, filters=filters_for("account"))
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    if explain:
        with connection.begin_nested() as savepoint:
            connection.execute(
                text("DELETE FROM query_snapshot_row WHERE snapshot_id=:s"),
                {"s": page.snapshot.snapshot_id},
            )
            connection.exec_driver_sql(
                "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + statements[0][0],
                statements[0][1],
            ).scalar_one()
            savepoint.rollback()
        assert (
            connection.execute(
                text(
                    "SELECT sum(jsonb_array_length(frozen_result->'source_evidence')) FROM query_snapshot_row WHERE snapshot_id=:s"
                ),
                {"s": page.snapshot.snapshot_id},
            ).scalar_one()
            == 2
        )
    proof = read_snapshot_evidence(
        case.session,
        context=case.context,
        bc_id=case.bc_id,
        snapshot_id=page.snapshot.snapshot_id,
    ).evidence_page
    assert len(proof.records) == 2
    assert {r["remote_id"] for r in proof.records} == {"legacy-1", "legacy-2"}


def test_attribution_copy_resolves_after_browser_snapshot_deleted(session, sync_seed):
    from app.modules.reporting.frozen_evidence import archive_evidence_page
    from app.modules.reporting.queries import freeze_review_page

    page, filters = setup_page(session, sync_seed)
    expected = list(evidence(session, sync_seed, page).records)
    report = freeze_review_page(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=filters,
        freeze_all=True,
    )
    payload = {"rows": [{"reports": [report]}]}
    session.execute(
        text(
            "DELETE FROM query_snapshot_row WHERE snapshot_id IN (SELECT id FROM query_snapshot WHERE tenant_id=:t)"
        ),
        {"t": sync_seed.tenant_id},
    )
    session.execute(
        text("DELETE FROM query_snapshot WHERE tenant_id=:t"),
        {"t": sync_seed.tenant_id},
    )
    received, cursor = [], None
    while True:
        resolved = archive_evidence_page(
            payload,
            category="attribution",
            identity={"source_id": "immutable-attribution"},
            report_id=report["id"],
            row_key=None,
            bucket=report["summary"]["buckets"][0]["evidence_ref"]["bucket"],
            component=None,
            cursor=cursor,
            limit=1,
        )
        received.extend(resolved["records"])
        assert resolved["evidence_ref"]["owner"] == "FROZEN_REPORT_ROWS"
        assert resolved["evidence_ref"]["owner_id"] == report["id"]
        cursor = resolved["next_cursor"]
        if cursor is None:
            break
    assert received == expected


def test_equivalent_selected_scope_ignores_search_and_order_controls(
    session, sync_seed
):
    _, filters = setup_page(session, sync_seed)
    pages = [
        query_ads(
            session,
            context=sync_seed.context,
            bc_id="bc-a",
            filters=filters.model_copy(update=controls),
        )
        for controls in (
            {"query": "账户", "sort_direction": "asc"},
            {"query": "一", "sort_direction": "desc"},
        )
    ]
    assert pages[0].items[0].row_key == pages[1].items[0].row_key
    assert (
        pages[0].items[0].metric_buckets[0].evidence_ref.scope_digest
        == pages[1].items[0].metric_buckets[0].evidence_ref.scope_digest
    )
    first, second = [page.summary["buckets"][0]["evidence_ref"] for page in pages]
    assert first["scope_digest"] == second["scope_digest"]
    assert first["evidence_digest"] == second["evidence_digest"]
    assert (
        evidence(session, sync_seed, pages[0]).records
        == evidence(session, sync_seed, pages[1]).records
    )

    # 不同选择即使已知事实完全相同，也必须保留缺失账户的范围身份。
    expanded = query_ads(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=filters.model_copy(
            update={"advertiser_ids": ("acc-1", "acc-2"), "include_empty": True}
        ),
    )
    assert expanded.total == 2
    known = next(
        v["evidence_ref"]
        for v in expanded.summary["buckets"]
        if v["evidence_ref"]["bucket"] == first["bucket"]
    )
    assert known["scope_digest"] != first["scope_digest"]
    assert known["record_count"] == first["record_count"] == 2
    from hashlib import sha256

    def digest(value):
        return sha256(
            json.dumps(
                value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest()

    scopes = sorted(
        [
            [row.row_key, row.metric_buckets[0].evidence_ref.scope_digest]
            for row in expanded.items
        ]
    )
    expected_scope = digest(
        [
            {
                k: filters.model_dump(mode="json").get(k)
                for k in ("dimension", "start_date", "end_date", "contribution")
            },
            scopes,
        ]
    )
    assert known["scope_digest"] == expected_scope
    known_records = evidence(
        session, sync_seed, expanded, bucket=known["bucket"]
    ).records
    assert known_records == evidence(session, sync_seed, pages[0]).records
    parts = [
        [
            key,
            scope,
            len(records := [r for r in known_records if r["selected_row_key"] == key]),
            digest(records),
        ]
        for key, scope in scopes
    ]
    assert known["evidence_digest"] == digest([expected_scope, known["bucket"], parts])
