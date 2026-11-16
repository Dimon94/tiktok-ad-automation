"""大账户池首屏只读取参与筛选/排序的候选字段，展示信息留给当前页。"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import text

from app.modules.reporting.row_set import _ctes
from app.modules.reporting.schemas import ReportingFilter


def _nodes(node):
    yield node
    for child in node.get("Plans", []):
        yield from _nodes(child)


@pytest.mark.parametrize("sort_by", ["spend", "name", "row_key"])
def test_account_snapshot_skips_unused_balance_and_name_projection(
    report_case, sort_by
):
    case = report_case
    ref = case.seed_campaign("Provider-Drama", "report-account", 4, 8)
    filters = ReportingFilter(
        dimension="account",
        start_date=date(2026, 9, 30),
        end_date=date(2026, 9, 30),
        sort_by=sort_by,
    )
    ctes, params = _ctes(
        filters,
        tenant_id=case.context.tenant_id,
        advertisers=["report-account"],
        bc_id=case.bc_id,
    )
    connection = case.session.connection()
    query = (
        ctes
        + " SELECT row_key,refs,material_uses,account_membership_version_id FROM ordered ORDER BY seq"
    )
    rows = connection.execute(text(query), params).all()
    assert len(rows) == 1
    assert rows[0].refs == []
    payload = connection.execute(
        text("SELECT refs FROM account_membership_version WHERE id=:version"),
        {"version": rows[0].account_membership_version_id},
    ).scalar_one()
    assert [item["remote_id"] for item in payload] == [ref.remote_id]
    plan = connection.execute(
        text("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + query), params
    ).scalar_one()[0]["Plan"]
    reads = {
        node.get("Relation Name")
        for node in _nodes(plan)
        if node.get("Actual Loops", 0) > 0
    }
    assert "account_balance_observation" not in reads
    assert "campaign_name_current" not in reads
    assert "ad_entity_read" not in reads
    # Native父级选择允许读取真实Campaign来源，但保持两次线性扫描的边界。
    assert (
        sum(
            (n["Actual Rows"] + n.get("Rows Removed by Filter", 0)) * n["Actual Loops"]
            for n in _nodes(plan)
            if n.get("Relation Name") == "campaign_report_day"
            and n.get("Actual Loops", 0)
        )
        <= 2
    )


@pytest.mark.parametrize("sort_by", ["spend", "balance"])
def test_account_balance_display_and_latest_available_sort(session, sync_seed, sort_by):
    from app.modules.reporting.models import AccountBalanceObservation
    from tests.modules.reporting.query_helpers import build_rows

    now = datetime.now(UTC)
    for adv, amount, availability, offset in [
        ("acc-1", "999", "AVAILABLE", -1),
        ("acc-1", "10", "AVAILABLE", 0),
        ("acc-2", "1000", "UNAVAILABLE", 0),
    ]:
        session.add(
            AccountBalanceObservation(
                tenant_id=sync_seed.tenant_id,
                advertiser_id=adv,
                amount=Decimal(amount),
                currency="USD",
                availability=availability,
                observed_at=now + timedelta(seconds=offset),
            )
        )
    rows = build_rows(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=ReportingFilter(
            dimension="account",
            start_date=date(2026, 10, 5),
            end_date=date(2026, 10, 5),
            sort_by=sort_by,
            sort_direction="desc",
        ),
    )
    by_id = {row.display["remote_id"]: row for row in rows}
    assert by_id["acc-1"].display["balance"] == "10"
    assert by_id["acc-2"].display["balance"] is None
    if sort_by == "balance":
        assert rows[0].display["remote_id"] == "acc-1"


def test_account_provider_search_still_reads_fact_campaign_projection(report_case):
    from app.modules.ads.models import CampaignNameProjection
    from app.modules.reporting.row_set import ordered_members

    case = report_case
    ref = case.seed_campaign("P-Drama", "report-account", 4, 8)
    # 版权方只存在于名称投影，不能被跳过名称读取的默认路径误用于搜索。
    case.session.add(
        CampaignNameProjection(
            tenant_id=case.context.tenant_id,
            advertiser_id=ref.advertiser_id,
            campaign_remote_id=ref.remote_id,
            name_revision=1,
            grouping_revision=1,
            parser_revision=1,
            raw_name="P-Drama",
            provider_label="unique-provider",
            drama_name="Drama",
            status="VALID",
        )
    )
    case.session.flush()
    members = ordered_members(
        case.session,
        tenant_id=case.context.tenant_id,
        advertisers=[ref.advertiser_id],
        bc_id=case.bc_id,
        filters=ReportingFilter(
            dimension="account",
            start_date=date(2026, 9, 30),
            end_date=date(2026, 9, 30),
            query="unique-provider",
            sort_by="spend",
        ),
    )
    assert len(members) == 1
    assert members[0].refs[0]["remote_id"] == ref.remote_id


def test_account_page_balance_reads_are_bounded_by_accounts_not_history(
    session, sync_seed
):
    from sqlalchemy import event

    from tests.modules.reporting.query_helpers import build_rows

    session.execute(
        text("""INSERT INTO account_balance_observation
        (id,tenant_id,advertiser_id,amount,currency,availability,balance_scope,observed_at,evidence)
        SELECT gen_random_uuid(),:tenant,a.adv,i,'USD','AVAILABLE','ADVERTISER',
          timestamp with time zone '2026-10-05 00:00:00+00'+i*interval '1 second','{}'::jsonb
        FROM unnest(ARRAY['acc-1','acc-2']) a(adv) CROSS JOIN generate_series(1,10000)i"""),
        {"tenant": sync_seed.tenant_id},
    )
    session.execute(text("ANALYZE account_balance_observation"))
    captured = []
    connection = session.connection()

    def capture(_conn, _cursor, statement, parameters, _context, _many):
        if "from account_balance_observation" in statement.lower():
            captured.append((statement, parameters))

    event.listen(connection, "before_cursor_execute", capture)
    try:
        rows = build_rows(
            session,
            context=sync_seed.context,
            bc_id="bc-a",
            filters=ReportingFilter(
                dimension="account",
                start_date=date(2026, 10, 5),
                end_date=date(2026, 10, 5),
            ),
        )
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert len(rows) == 2
    assert all(row.display["balance"] == "10000" for row in rows)
    assert len(captured) == 1
    statement, parameters = captured[0]
    plan = connection.exec_driver_sql(
        "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + statement, parameters
    ).scalar_one()[0]["Plan"]
    scanned = sum(
        (node["Actual Rows"] + node.get("Rows Removed by Filter", 0))
        * node["Actual Loops"]
        for node in _nodes(plan)
        if node.get("Relation Name") == "account_balance_observation"
    )
    # 现有索引不含并列ID排序，允许每户多读一条边界记录，但不能扫描历史。
    assert scanned <= 4, plan


def test_latest_balances_keep_tenant_scope_ties_and_unavailable_observations(
    report_case,
):
    from app.modules.reporting.balance_queries import latest_balance_observations
    from app.modules.reporting.models import AccountBalanceObservation

    case = report_case
    now = datetime.now(UTC)
    for tenant, identity, amount, availability, offset in [
        (case.context.tenant_id, 1, "5", "AVAILABLE", 0),
        (case.context.tenant_id, 2, "9", "UNAVAILABLE", 0),
        (case.other_context.tenant_id, 3, "99999", "AVAILABLE", 100),
    ]:
        case.session.add(
            AccountBalanceObservation(
                id=UUID(int=identity),
                tenant_id=tenant,
                advertiser_id="report-account",
                amount=Decimal(amount),
                currency="USD",
                availability=availability,
                observed_at=now + timedelta(seconds=offset),
            )
        )
    case.session.flush()
    assert (
        latest_balance_observations(
            case.session, tenant_id=case.context.tenant_id, advertisers=[]
        )
        == ()
    )
    observations = latest_balance_observations(
        case.session,
        tenant_id=case.context.tenant_id,
        advertisers=["report-account", "missing-account", "report-account"],
    )
    assert len(observations) == 1
    assert observations[0].id == UUID(int=2)
    assert observations[0].availability == "UNAVAILABLE"
