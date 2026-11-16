"""账户首屏读取预存成员与日金额，旧快照不混入后来同步的系列。"""

from datetime import date

from sqlalchemy import text

from tests.modules.reporting.test_queries import filters_for, freeze, query


def test_account_snapshot_pins_membership_instead_of_copying_refs(report_case):
    case = report_case
    old = case.seed_campaign("P-Old", "report-account", 4, 8)
    page = query(case, filters=filters_for("account", sort_by="spend"))
    row = (
        case.session.connection()
        .execute(
            text("""SELECT account_membership_version_id,refs FROM query_snapshot_row
        WHERE snapshot_id=:snapshot"""),
            {"snapshot": page.snapshot.snapshot_id},
        )
        .one()
    )
    assert row.account_membership_version_id is not None
    assert row.refs == []
    case.seed_campaign("P-New", "report-account", 7, 14)
    assert freeze(case, page.snapshot.snapshot_id).refs == (old,)
    assert page.summary["buckets"][0]["values"]["spend"] == "4"


def test_account_day_is_published_with_campaign_facts_and_keeps_null_flags(report_case):
    case = report_case
    first = case.seed_campaign("P-A", "report-account", 4, 8)
    case.seed_campaign("P-B", "report-account", 6, 12)
    case.session.execute(
        text("""UPDATE campaign_report_day SET revenue_d0=NULL
        WHERE tenant_id=:tenant AND campaign_id=:campaign"""),
        {"tenant": case.context.tenant_id, "campaign": first.remote_id},
    )
    day = (
        case.session.connection()
        .execute(
            text("""SELECT spend,revenue_d0,revenue_d0_has_null FROM account_report_day
        WHERE tenant_id=:tenant AND advertiser_id='report-account' AND stat_date=:day"""),
            {"tenant": case.context.tenant_id, "day": date(2026, 9, 30)},
        )
        .one()
    )
    assert day.spend == 10
    assert day.revenue_d0 == 12
    assert day.revenue_d0_has_null
    state = (
        case.session.connection()
        .execute(
            text("""SELECT status FROM account_report_day_state
        WHERE tenant_id=:tenant AND advertiser_id='report-account' AND stat_date=:day"""),
            {"tenant": case.context.tenant_id, "day": date(2026, 9, 30)},
        )
        .scalar_one()
    )
    assert state == "COMPLETE"


def _current_version(session, tenant, advertiser="report-account"):
    return (
        session.connection()
        .execute(
            text(
                "SELECT version_id FROM account_membership_current WHERE tenant_id=:t AND advertiser_id=:a"
            ),
            {"t": tenant, "a": advertiser},
        )
        .scalar_one()
    )


def test_member_version_changes_for_names_and_identity_but_not_status(report_case):
    from app.modules.ads.directory import append_campaign_name_projection
    from app.modules.ads.models import AdObject

    case = report_case
    ref = case.seed_campaign("P-Old", "report-account", 4, 8)
    before = _current_version(case.session, case.context.tenant_id)
    obj = case.session.get(
        AdObject, (ref.tenant_id, ref.advertiser_id, ref.kind, ref.remote_id)
    )
    obj.operation_status = "DISABLE"
    case.session.flush()
    assert _current_version(case.session, case.context.tenant_id) == before
    append_campaign_name_projection(
        case.session, campaign_ref=ref, raw_name="嘉书-旧剧-说明", parser_revision=1
    )
    page = query(case, filters=filters_for("account"))
    version = _current_version(case.session, case.context.tenant_id)
    assert version != before
    append_campaign_name_projection(
        case.session, campaign_ref=ref, raw_name="嘉书-新剧-说明", parser_revision=1
    )
    assert _current_version(case.session, case.context.tenant_id) != version
    selected = freeze(case, page.snapshot.snapshot_id)
    from app.modules.reporting.query_models import FrozenSelectionRecord

    record = case.session.get(FrozenSelectionRecord, selected.selection_id)
    assert record.naming_versions == {
        "directory": 1,
        f"report-account:{ref.remote_id}": 1,
    }
    case.session.delete(obj)
    case.session.flush()
    assert freeze(case, page.snapshot.snapshot_id).refs == (ref,)


def test_membership_gc_keeps_live_snapshot_and_export_copies_original_members(
    report_case,
):
    from sqlmodel import select

    from app.modules.reporting.account_memberships import prune_account_memberships
    from app.modules.reporting.exports import create_export
    from app.modules.reporting.query_models import QuerySnapshot, ReportExport
    from app.modules.reporting.read_models.models import AccountMembershipVersion

    case = report_case
    old = case.seed_campaign("P-Old", "report-account", 4, 8)
    version = _current_version(case.session, case.context.tenant_id)
    page = query(case, filters=filters_for("account"))
    case.seed_campaign("P-New", "report-account", 1, 2)
    exported = create_export(
        case.session,
        context=case.context,
        bc_id=case.bc_id,
        snapshot_id=page.snapshot.snapshot_id,
        idempotency_key="old-members",
    )
    record = case.session.get(ReportExport, exported.id)
    assert [r["remote_id"] for r in record.frozen_rows[0]["refs"]] == [old.remote_id]
    case.session.execute(
        text(
            "UPDATE account_membership_version SET retired_at=now()-interval '2 hours' WHERE id=:v"
        ),
        {"v": version},
    )
    prune_account_memberships(case.session)
    assert (
        case.session.exec(
            select(AccountMembershipVersion.id).where(
                AccountMembershipVersion.id == version
            )
        ).first()
        == version
    )
    case.session.delete(case.session.get(QuerySnapshot, page.snapshot.snapshot_id))
    case.session.flush()
    prune_account_memberships(case.session)
    assert (
        case.session.exec(
            select(AccountMembershipVersion.id).where(
                AccountMembershipVersion.id == version
            )
        ).first()
        is None
    )
    assert [r["remote_id"] for r in record.frozen_rows[0]["refs"]] == [old.remote_id]


def test_version_resolver_rejects_another_account_or_tenant(report_case):
    import pytest
    from fastapi import HTTPException

    from app.modules.reporting.account_memberships import load_account_versions

    case = report_case
    other = _current_version(case.session, case.other_context.tenant_id)
    with pytest.raises(HTTPException) as denied:
        load_account_versions(
            case.session,
            tenant_id=case.context.tenant_id,
            advertisers=["report-account"],
            version_ids=[other],
        )
    assert denied.value.status_code == 409


def test_account_rollup_and_membership_updates_rollback_together(report_case):
    case = report_case
    old = case.seed_campaign("P-Old", "report-account", 4, 8)
    version = _current_version(case.session, case.context.tenant_id)
    with case.session.begin_nested() as savepoint:
        case.seed_campaign("P-New", "report-account", 6, 12)
        assert _current_version(case.session, case.context.tenant_id) != version
        savepoint.rollback()
    page = query(case, filters=filters_for("account"))
    assert page.summary["buckets"][0]["values"]["spend"] == "4"
    assert _current_version(case.session, case.context.tenant_id) == version
    assert freeze(case, page.snapshot.snapshot_id).refs == (old,)


def test_account_day_rejects_stale_rr_writer(committed_sync_seed):
    import pytest
    from sqlalchemy.exc import OperationalError

    from app.core.db import engine

    seed, factory = committed_sync_seed
    params = {"t": seed.tenant_id}
    with factory() as session:
        session.execute(
            text("""INSERT INTO campaign_report_day
        (tenant_id,advertiser_id,stat_date,campaign_id,campaign_name,spend,currency,timezone,fetched_at,sync_job_id)
        SELECT :t,'acc-1',date '2026-10-05',c,'campaign',1,'USD','UTC',now(),gen_random_uuid()
        FROM unnest(ARRAY['c1','c2']) c"""),
            params,
        )
        session.commit()
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as stale:
        transaction = stale.begin()
        stale.execute(
            text("SELECT revision FROM account_report_day_state WHERE tenant_id=:t"),
            params,
        ).all()
        with engine.begin() as newer:
            newer.execute(
                text(
                    "UPDATE campaign_report_day SET spend=10 WHERE tenant_id=:t AND campaign_id='c1'"
                ),
                params,
            )
        with pytest.raises(OperationalError) as rejected:
            stale.execute(
                text(
                    "UPDATE campaign_report_day SET spend=20 WHERE tenant_id=:t AND campaign_id='c2'"
                ),
                params,
            )
        assert rejected.value.orig.sqlstate == "40001"
        transaction.rollback()
    with factory() as session:
        assert (
            session.execute(
                text("SELECT spend FROM account_report_day WHERE tenant_id=:t"), params
            ).scalar_one()
            == 11
        )


def test_large_account_snapshot_cost_does_not_expand_campaigns_or_fact_history(
    report_case,
):
    from sqlalchemy import event

    from app.modules.reporting.queries import _build_snapshot
    from app.modules.reporting.row_set import _ctes

    case = report_case
    params = {"t": case.context.tenant_id}
    # Native账户取选中Campaign事实；读取必须线性有界，不逐成员展开历史目录。
    case.session.execute(
        text("""INSERT INTO ad_object
      (tenant_id,advertiser_id,kind,remote_id,ad_type,name,configuration,observed_at,published_version)
      SELECT :t,'report-account','campaign','c-'||i,'REGULAR','campaign','{}',now(),1
      FROM generate_series(1,20000)i"""),
        params,
    )
    case.session.execute(
        text("""INSERT INTO campaign_report_day
      (tenant_id,advertiser_id,stat_date,campaign_id,campaign_name,spend,currency,timezone,fetched_at,sync_job_id)
      SELECT :t,'report-account',date '2026-09-30', 'c-'||i,'campaign',1,'USD','UTC',now(),gen_random_uuid()
      FROM generate_series(1,20000)i"""),
        params,
    )
    for table in (
        "account_report_day",
        "account_membership_current",
        "campaign_report_day",
        "ad_entity_read",
    ):
        case.session.execute(text(f"ANALYZE {table}"))
    filters = filters_for("account", sort_by="spend")
    statements = []
    connection = case.session.connection()

    def capture(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    event.listen(connection, "before_cursor_execute", capture)
    try:
        snapshot = _build_snapshot(
            case.session, context=case.context, bc_id=case.bc_id, filters=filters
        )
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert snapshot.total == 1
    assert snapshot.summary["buckets"][0]["values"]["spend"] == "20000"
    assert len(snapshot.naming_versions) == 2  # directory + 一个不可变账户版本。
    assert not any("jsonb_array_elements(fr.refs)" in sql for sql in statements)
    ctes, values = _ctes(
        filters,
        tenant_id=case.context.tenant_id,
        advertisers=["report-account"],
        bc_id=case.bc_id,
    )
    plan = connection.execute(
        text(
            "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) "
            + ctes
            + " SELECT row_key,refs,material_uses,account_membership_version_id FROM ordered ORDER BY seq"
        ),
        values,
    ).scalar_one()[0]["Plan"]

    def nodes(node):
        yield node
        for child in node.get("Plans", []):
            yield from nodes(child)

    scanned = {
        node.get("Relation Name")
        for node in nodes(plan)
        if node.get("Actual Loops", 0) > 0
    }
    assert not scanned & {
        "ad_object",
        "ad_entity_read",
        "campaign_name_current",
        "account_membership_version",
    }
    campaign_scans = [
        n for n in nodes(plan) if n.get("Relation Name") == "campaign_report_day"
    ]
    assert campaign_scans
    # 原事实与已存在成员日期空桶至多各一遍，不允许每成员重复全表。
    assert (
        sum(
            (n.get("Actual Rows", 0) + n.get("Rows Removed by Filter", 0))
            * n.get("Actual Loops", 0)
            for n in campaign_scans
        )
        <= 40000
    )


def test_account_daily_and_source_query_match_across_dates_currencies_and_nulls(
    report_case,
):
    from datetime import timedelta

    from app.modules.reporting.schemas import ReportingFilter
    from tests.modules.reporting.evidence_helpers import comparable_snapshot_evidence

    case = report_case
    params = {"t": case.context.tenant_id}
    case.session.execute(
        text("""INSERT INTO campaign_report_day
      (tenant_id,advertiser_id,stat_date,campaign_id,campaign_name,spend,revenue_d0,currency,timezone,fetched_at,sync_job_id)
      SELECT :t,'report-account',date '2026-09-30'+d, c,'campaign',spend,revenue,currency,zone,now(),gen_random_uuid()
      FROM (VALUES (0,'c1',5::numeric,2::numeric,'USD','UTC'),
                   (0,'c2',7,NULL,'USD','UTC'),(1,'c3',3,1,'USD','Asia/Hong_Kong'),
                   (1,'c4',4,2,'EUR','UTC')) v(d,c,spend,revenue,currency,zone)"""),
        params,
    )
    arguments = {
        "dimension": "account",
        "start_date": case.fact_date,
        "end_date": case.fact_date + timedelta(days=1),
        "sort_by": "spend",
    }
    daily = query(case, filters=ReportingFilter(**arguments))
    source = query(case, filters=ReportingFilter(**arguments, query="report-account"))
    assert daily.coverage == source.coverage
    values = [
        comparable_snapshot_evidence(
            case.session,
            case,
            page.snapshot.snapshot_id,
            {
                "summary": page.summary,
                "metrics": [
                    vector.model_dump(mode="json")
                    for vector in page.items[0].metric_buckets
                ],
            },
        )
        for page in (daily, source)
    ]
    assert values[0] == values[1]


def test_read_committed_account_writers_serialize_before_recomputing(
    committed_sync_seed,
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import monotonic, sleep

    from app.core.db import engine

    seed, factory = committed_sync_seed
    params = {"t": seed.tenant_id}
    with factory() as session:
        session.execute(
            text("""INSERT INTO campaign_report_day
          (tenant_id,advertiser_id,stat_date,campaign_id,campaign_name,spend,currency,timezone,fetched_at,sync_job_id)
          SELECT :t,'acc-1',date '2026-10-05',c,'campaign',1,'USD','UTC',now(),gen_random_uuid()
          FROM unnest(ARRAY['c1','c2']) c"""),
            params,
        )
        session.commit()
    ready = Event()
    second_pid = []

    def second_writer():
        with engine.begin() as connection:
            second_pid.append(
                connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
            )
            ready.set()
            connection.execute(
                text(
                    "UPDATE campaign_report_day SET spend=20 WHERE tenant_id=:t AND campaign_id='c2'"
                ),
                params,
            )

    with engine.connect() as first, ThreadPoolExecutor(max_workers=1) as executor:
        transaction = first.begin()
        try:
            first.execute(
                text(
                    "UPDATE campaign_report_day SET spend=10 WHERE tenant_id=:t AND campaign_id='c1'"
                ),
                params,
            )
            pending = executor.submit(second_writer)
            assert ready.wait(5)
            locked = False
            deadline = monotonic() + 5
            with engine.connect() as observer:
                while monotonic() < deadline:
                    locked = bool(
                        observer.execute(
                            text(
                                "SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=:pid"
                            ),
                            {"pid": second_pid[0]},
                        ).scalar_one()
                    )
                    observer.rollback()
                    if locked:
                        break
                    sleep(0.02)
            assert locked, "第二个汇总写者必须等待同一账户日的写锁"
            transaction.commit()
            pending.result(timeout=5)
        finally:
            if transaction.is_active:
                transaction.rollback()
    with factory() as session:
        assert (
            session.execute(
                text("SELECT spend FROM account_report_day WHERE tenant_id=:t"), params
            ).scalar_one()
            == 30
        )


def test_membership_payload_is_immutable_and_missing_head_fails_closed(report_case):
    import pytest
    from fastapi import HTTPException
    from sqlalchemy.exc import DBAPIError

    from app.modules.reporting.queries import _build_snapshot

    case = report_case
    case.seed_campaign("P-Old", "report-account", 4, 8)
    version = _current_version(case.session, case.context.tenant_id)
    with pytest.raises(DBAPIError), case.session.begin_nested():
        case.session.execute(
            text("UPDATE account_membership_version SET refs='[]' WHERE id=:v"),
            {"v": version},
        )
    with case.session.begin_nested() as savepoint:
        case.session.execute(
            text("DELETE FROM account_membership_current WHERE tenant_id=:t"),
            {"t": case.context.tenant_id},
        )
        with pytest.raises(HTTPException) as denied:
            _build_snapshot(
                case.session,
                context=case.context,
                bc_id=case.bc_id,
                filters=filters_for("account"),
            )
        assert denied.value.status_code == 409
        savepoint.rollback()


def test_rediscovered_account_restores_retained_daily_facts_and_state(report_case):
    from app.modules.accounts.models import AdvertiserAccount
    from app.modules.reporting.store_models import ReportSyncState

    case = report_case
    case.session.execute(
        text("""INSERT INTO campaign_report_day
      (tenant_id,advertiser_id,stat_date,campaign_id,campaign_name,spend,currency,timezone,fetched_at,sync_job_id)
      VALUES(:t,'rediscovered',date '2026-09-30','old','history',9,'USD','UTC',now(),gen_random_uuid())"""),
        {"t": case.context.tenant_id},
    )
    case.session.add(
        ReportSyncState(
            tenant_id=case.context.tenant_id,
            advertiser_id="rediscovered",
            report_kind="ad_day",
            stat_date=case.fact_date,
            status="COMPLETE",
            row_count=1,
        )
    )
    case.session.flush()
    case.session.add(
        AdvertiserAccount(
            tenant_id=case.context.tenant_id,
            advertiser_id="rediscovered",
            currency="USD",
            timezone="UTC",
        )
    )
    case.session.flush()
    restored = (
        case.session.connection()
        .execute(
            text("""SELECT d.spend,s.status FROM account_report_day d
      JOIN account_report_day_state s USING(tenant_id,advertiser_id,stat_date)
      WHERE d.tenant_id=:t AND d.advertiser_id='rediscovered'"""),
            {"t": case.context.tenant_id},
        )
        .one()
    )
    assert restored.spend == 9
    assert restored.status == "COMPLETE"
    # 历史金额恢复不等于恢复已删除的目录系列。
    assert (
        case.session.connection()
        .execute(
            text("""SELECT v.refs FROM account_membership_current h
      JOIN account_membership_version v ON v.id=h.version_id
      WHERE h.tenant_id=:t AND h.advertiser_id='rediscovered'"""),
            {"t": case.context.tenant_id},
        )
        .scalar_one()
        == []
    )
