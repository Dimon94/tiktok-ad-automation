from sqlalchemy import text
from sqlmodel import select

from app.modules.reporting.read_models.models import AdEntityRead
from app.modules.reporting.read_models.rebuild import backfill_directory_page


def test_directory_rebuild_pages_resume_without_missing_or_reverting_source(
    report_case,
):
    case = report_case
    refs = [
        case.seed_campaign(f"Provider-Drama-{n}", "report-account", 1, 2)
        for n in range(3)
    ]
    case.session.connection().execute(
        text("DELETE FROM ad_entity_read WHERE tenant_id=:tenant"),
        {"tenant": case.context.tenant_id},
    )
    after = None
    rebuilt = 0
    while True:
        page = backfill_directory_page(
            case.session,
            tenant_id=case.context.tenant_id,
            advertiser_id="report-account",
            kind="directory",
            after=after,
            limit=1,
        )
        rebuilt += page.processed
        after = page.after
        if after is None:
            break
    rows = case.session.exec(
        select(AdEntityRead).where(AdEntityRead.tenant_id == case.context.tenant_id)
    ).all()
    assert {row.remote_id for row in rows} == {ref.remote_id for ref in refs}
    assert rebuilt == 3
    page = backfill_directory_page(
        case.session,
        tenant_id=case.context.tenant_id,
        advertiser_id="report-account",
        kind="directory",
        limit=500,
    )
    assert page.processed == 3
    assert (
        len(
            case.session.exec(
                select(AdEntityRead).where(
                    AdEntityRead.tenant_id == case.context.tenant_id
                )
            ).all()
        )
        == 3
    )


def test_dirty_claim_reads_ordered_index_without_sorting_entire_backlog(
    session, sync_seed
):
    from sqlalchemy import text

    session.execute(
        text("""
        INSERT INTO read_bucket_state
            (tenant_id,advertiser_id,kind,bucket,source_revision,derived_revision,observed_at)
        SELECT CAST(:tenant AS uuid),'acc-1','material_day',DATE '2000-01-01'+i,1,0,
            clock_timestamp()+i*INTERVAL '1 second'
        FROM generate_series(1,1000) i
    """),
        {"tenant": sync_seed.tenant_id},
    )
    session.execute(text("ANALYZE read_bucket_state"))
    plan = session.execute(
        text("""
        EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON)
        SELECT tenant_id,advertiser_id,kind,bucket,source_revision FROM read_bucket_state
        WHERE source_revision>derived_revision AND kind IN ('ad_day','material_day')
        ORDER BY observed_at,tenant_id,advertiser_id,kind,bucket
        LIMIT 1 FOR UPDATE SKIP LOCKED
    """)
    ).scalar_one()[0]["Plan"]

    def nodes(node):
        yield node
        for child in node.get("Plans", []):
            yield from nodes(child)

    assert not any(node["Node Type"] == "Sort" for node in nodes(plan))
    scans = [
        node for node in nodes(plan) if node.get("Relation Name") == "read_bucket_state"
    ]
    assert len(scans) == 1
    assert scans[0]["Node Type"] == "Index Scan"
    assert scans[0]["Actual Rows"] == 1
