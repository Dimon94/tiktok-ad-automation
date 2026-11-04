from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from uuid import uuid4

from sqlmodel import select

from app.modules.reporting.store_models import CampaignReportDay, ReportSyncState


def seed_days(session, c, start=date(2026, 10, 1), end=date(2026, 11, 3)):
    day = start
    while day <= end:
        session.add(
            ReportSyncState(
                tenant_id=c.context.tenant_id,
                advertiser_id=c.advertiser_id,
                report_kind="ad_day",
                stat_date=day,
                status="COMPLETE",
                publication_version=1,
            )
        )
        day += timedelta(days=1)
    for day, campaign, spend in [
        (date(2026, 11, 1), "a", "1000"),
        (date(2026, 11, 2), "b", "1000"),
    ]:
        session.add(
            CampaignReportDay(
                tenant_id=c.context.tenant_id,
                advertiser_id=c.advertiser_id,
                campaign_id=campaign,
                campaign_name=f"剧-{campaign}",
                stat_date=day,
                currency="USD",
                timezone="UTC",
                spend=D(spend),
                revenue_total=D("800"),
                revenue_d0=D("700"),
                ad_impression_events=D(500),
                fetched_at=datetime(2026, 11, 4, tzinfo=UTC),
                sync_job_id=uuid4(),
            )
        )
    session.flush()


def build(session, c):
    from app.modules.business.projection import advance_run, request_rebuild

    runs = request_rebuild(
        session,
        context=c.context,
        bc_id=c.bc_id,
        start_date=date(2026, 10, 1),
        end_date=c.day,
        now=datetime(2026, 11, 4, 12, tzinfo=UTC),
    )
    for run in runs:
        for _ in range(100):
            if advance_run(session, run.id, now=datetime(2026, 11, 4, 12, tzinfo=UTC)):
                break
        assert run.status == "COMPLETE"
    return runs


def test_quarter_tier_delta_and_cumulative_series_allocation(session, business_case):
    from app.modules.business.configuration import save_mapping, save_rate
    from app.modules.business.models import BusinessDay

    c = business_case
    seed_days(session, c)
    for campaign in ("a", "b"):
        save_mapping(
            session,
            context=c.context,
            bc_id=c.bc_id,
            advertiser_id=c.advertiser_id,
            campaign_id=campaign,
            drama_id=c.drama.id,
        )
    save_rate(
        session,
        context=c.context,
        kind="provider",
        scope_id=str(c.provider.id),
        effective_date=date(2026, 10, 1),
        rate=D("0.08"),
    )
    run = build(session, c)[0]
    rows = session.exec(select(BusinessDay).where(BusinessDay.run_id == run.id)).all()
    assert sum(r.subsidy_comic for r in rows) == D(800)
    assert sum(r.subsidy_other for r in rows) == D(500)
    assert sum(r.subsidy_existing for r in rows) == D(400)
    # 第二天跨2000档，两个系列按截至该日累计消耗各分一半。
    day2 = [r for r in rows if r.stat_date == date(2026, 11, 2)]
    assert {r.campaign_id: r.subsidy_comic for r in day2} == {"a": D(400), "b": D(400)}
    assert sum(r.business_revenue for r in rows) == D(1472)
    assert all(r.compensation is None for r in rows if r.spend)


def test_missing_quarter_prefix_keeps_subsidy_unknown(session, business_case):
    from app.modules.business.configuration import save_mapping
    from app.modules.business.models import BusinessDay

    c = business_case
    seed_days(session, c, start=date(2026, 11, 1))
    for campaign in ("a", "b"):
        save_mapping(
            session,
            context=c.context,
            bc_id=c.bc_id,
            advertiser_id=c.advertiser_id,
            campaign_id=campaign,
            drama_id=c.drama.id,
        )
    run = build(session, c)[0]
    rows = session.exec(select(BusinessDay).where(BusinessDay.run_id == run.id)).all()
    assert rows
    assert all(r.subsidy_comic is None for r in rows)
    assert sum(r.spend for r in rows) == D(2000)


def test_configuration_change_fences_old_run_and_duplicate_delivery(
    session, business_case
):
    from app.modules.business.configuration import save_rate
    from app.modules.business.projection import advance_run, request_rebuild

    c = business_case
    seed_days(session, c)
    now = datetime(2026, 11, 4, 12, tzinfo=UTC)
    run = request_rebuild(
        session,
        context=c.context,
        bc_id=c.bc_id,
        start_date=date(2026, 10, 1),
        end_date=c.day,
        now=now,
    )[0]
    save_rate(
        session,
        context=c.context,
        kind="rebate",
        scope_id=c.bc_id,
        effective_date=date(2026, 10, 1),
        rate=D("0.06"),
    )
    assert advance_run(session, run.id, now=now)
    assert run.status == "SUPERSEDED"
    runs = build(session, c)
    assert advance_run(session, runs[0].id, now=now)


def test_current_day_is_never_materialized(session, business_case):
    from app.modules.business.projection import request_rebuild

    c = business_case
    seed_days(session, c)
    runs = request_rebuild(
        session,
        context=c.context,
        bc_id=c.bc_id,
        start_date=date(2026, 10, 1),
        end_date=date(2026, 11, 4),
        now=datetime(2026, 11, 4, 12, tzinfo=UTC),
    )
    assert all(run.end_date == date(2026, 11, 3) for run in runs)


def test_rebuilding_shorter_range_does_not_shrink_published_quarter(
    session, business_case
):
    from app.modules.business.projection import request_rebuild

    c = business_case
    seed_days(session, c)
    build(session, c)
    runs = request_rebuild(
        session,
        context=c.context,
        bc_id=c.bc_id,
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 1),
        now=datetime(2026, 11, 4, 12, tzinfo=UTC),
    )
    assert runs[0].end_date == date(2026, 11, 3)


def test_history_correction_updates_tiers_but_keeps_old_version(session, business_case):
    from app.modules.business.configuration import save_mapping
    from app.modules.business.models import BusinessDay

    c = business_case
    seed_days(session, c)
    for campaign in ("a", "b"):
        save_mapping(
            session,
            context=c.context,
            bc_id=c.bc_id,
            advertiser_id=c.advertiser_id,
            campaign_id=campaign,
            drama_id=c.drama.id,
        )
    old = build(session, c)[0]
    fact = session.get(
        CampaignReportDay,
        (c.context.tenant_id, c.advertiser_id, date(2026, 11, 2), "b"),
    )
    fact.spend = D(500)
    session.add(fact)
    state = session.get(
        ReportSyncState,
        (c.context.tenant_id, c.advertiser_id, "ad_day", date(2026, 11, 2)),
    )
    state.publication_version += 1
    session.add(state)
    session.flush()
    newer = build(session, c)[0]
    old_rows = session.exec(
        select(BusinessDay).where(BusinessDay.run_id == old.id)
    ).all()
    new_rows = session.exec(
        select(BusinessDay).where(BusinessDay.run_id == newer.id)
    ).all()
    assert old.id != newer.id
    assert sum(r.subsidy_comic for r in old_rows) == 800
    assert sum(r.subsidy_comic for r in new_rows) == 0
    assert sum(r.subsidy_existing for r in new_rows) == 200
    assert newer.inputs["dirty_start"] == "2026-11-02"


def test_non_usd_does_not_use_dollar_subsidy_tiers(session, business_case):
    from app.modules.business.configuration import save_mapping
    from app.modules.business.models import BusinessDay

    c = business_case
    seed_days(session, c)
    for campaign in ("a", "b"):
        save_mapping(
            session,
            context=c.context,
            bc_id=c.bc_id,
            advertiser_id=c.advertiser_id,
            campaign_id=campaign,
            drama_id=c.drama.id,
        )
    for fact in session.exec(
        select(CampaignReportDay).where(
            CampaignReportDay.tenant_id == c.context.tenant_id
        )
    ).all():
        fact.currency = "EUR"
        session.add(fact)
    session.flush()
    run = build(session, c)[0]
    rows = session.exec(select(BusinessDay).where(BusinessDay.run_id == run.id)).all()
    assert all(r.subsidy_comic is None for r in rows)
    assert sum(r.spend for r in rows) == 2000


def test_mixed_timezones_keep_unfinished_account_out_of_base_and_subsidy(
    session, business_case
):
    from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
    from app.modules.business.models import BusinessDay
    from app.modules.business.projection import advance_run, request_rebuild

    c = business_case
    seed_days(session, c)
    grant = session.exec(
        select(BCAccountAccess).where(
            BCAccountAccess.tenant_id == c.context.tenant_id,
            BCAccountAccess.bc_id == c.bc_id,
        )
    ).one()
    session.add(
        AdvertiserAccount(
            tenant_id=c.context.tenant_id,
            advertiser_id="ny",
            currency="USD",
            timezone="America/New_York",
        )
    )
    session.flush()
    session.add(
        BCAccountAccess(
            tenant_id=c.context.tenant_id,
            bc_id=c.bc_id,
            advertiser_id="ny",
            connection_id=grant.connection_id,
            in_bc=True,
            authorized=True,
            active=True,
            checked_at=datetime.now(UTC),
        )
    )
    session.add(
        ReportSyncState(
            tenant_id=c.context.tenant_id,
            advertiser_id="ny",
            report_kind="ad_day",
            stat_date=c.day,
            status="COMPLETE",
            publication_version=1,
        )
    )
    session.add(
        CampaignReportDay(
            tenant_id=c.context.tenant_id,
            advertiser_id="ny",
            campaign_id="ny-campaign",
            stat_date=c.day,
            currency="USD",
            timezone="America/New_York",
            spend=D(1234),
            revenue_total=D(500),
            fetched_at=datetime.now(UTC),
            sync_job_id=uuid4(),
        )
    )
    session.flush()
    now = datetime(2026, 11, 4, 2, tzinfo=UTC)
    run = request_rebuild(
        session,
        context=c.context,
        bc_id=c.bc_id,
        start_date=date(2026, 10, 1),
        end_date=c.day,
        now=now,
    )[0]
    assert run.end_date == c.day
    assert run.inputs["accounts"]["ny"]["complete_through"] == "2026-11-02"
    for _ in range(100):
        if advance_run(session, run.id, now=now):
            break
    assert run.status == "COMPLETE"
    rows = session.exec(select(BusinessDay).where(BusinessDay.run_id == run.id)).all()
    assert not any(r.advertiser_id == "ny" for r in rows)


def test_pre_midnight_observation_is_not_a_complete_day(session, business_case):
    from app.modules.business.models import BusinessDay

    c = business_case
    seed_days(session, c)
    state = session.get(
        ReportSyncState,
        (c.context.tenant_id, c.advertiser_id, "ad_day", date(2026, 11, 2)),
    )
    state.observed_at = datetime(2026, 11, 2, 23, 59, tzinfo=UTC)
    session.add(state)
    session.flush()
    run = build(session, c)[0]
    assert "2026-11-02" in run.coverage["missing_dates"]
    rows = session.exec(select(BusinessDay).where(BusinessDay.run_id == run.id)).all()
    assert not any(r.campaign_id == "b" for r in rows)


def test_fixed_historical_window_is_reused_on_the_next_calendar_day(
    session, business_case
):
    from app.modules.business.projection import request_rebuild

    c = business_case
    seed_days(session, c)
    old = build(session, c)[0]
    unchanged = request_rebuild(
        session,
        context=c.context,
        bc_id=c.bc_id,
        start_date=date(2026, 10, 1),
        end_date=c.day,
        now=datetime(2026, 11, 5, 12, tzinfo=UTC),
    )[0]
    assert unchanged.id == old.id


def test_new_complete_day_reuses_prior_daily_projection(session, business_case):
    from app.modules.business.projection import advance_run, request_rebuild

    c = business_case
    seed_days(session, c)
    old = build(session, c)[0]
    day = date(2026, 11, 4)
    now = datetime(2026, 11, 5, 12, tzinfo=UTC)
    session.add(
        ReportSyncState(
            tenant_id=c.context.tenant_id,
            advertiser_id=c.advertiser_id,
            report_kind="ad_day",
            stat_date=day,
            status="COMPLETE",
            publication_version=1,
            observed_at=now,
            updated_at=now,
        )
    )
    session.add(
        CampaignReportDay(
            tenant_id=c.context.tenant_id,
            advertiser_id=c.advertiser_id,
            campaign_id="a",
            stat_date=day,
            currency="USD",
            timezone="UTC",
            spend=D(100),
            revenue_total=D(90),
            fetched_at=now,
            sync_job_id=uuid4(),
        )
    )
    session.flush()
    newer = request_rebuild(
        session,
        context=c.context,
        bc_id=c.bc_id,
        start_date=date(2026, 10, 1),
        end_date=day,
        now=now,
    )[0]
    assert newer.id != old.id
    assert newer.inputs["dirty_start"] == "2026-11-04"
    for _ in range(10):
        if advance_run(session, newer.id, now=now):
            break
    assert newer.status == "COMPLETE"


def test_historical_number_mapping_and_catalog_change_rebuild_full_prefix(
    session, business_case
):
    from app.modules.business.models import BusinessDay
    from app.modules.business.queries import query_dashboard
    from app.modules.business.schemas import DashboardFilter

    c = business_case
    seed_days(session, c)
    c.drama.external_drama_id = "108345"
    session.add(c.drama)
    for fact in session.exec(select(CampaignReportDay)).all():
        fact.campaign_name = "jiashu-Historical title-108345-1101-01-owner"
        session.add(fact)
    session.flush()
    first = build(session, c)[0]
    values = session.exec(
        select(BusinessDay).where(BusinessDay.run_id == first.id, BusinessDay.spend > 0)
    ).all()
    assert all(row.drama_id == c.drama.id for row in values)
    page = query_dashboard(
        session,
        context=c.context,
        filters=DashboardFilter(
            bc_id=c.bc_id, start_date=date(2026, 11, 1), end_date=c.day
        ),
    )
    assert page.total == 1 and page.totals["business_revenue"] == D(1600)
    c.drama.title = "Corrected catalog title"
    session.add(c.drama)
    session.flush()
    second = build(session, c)[0]
    assert second.id != first.id and second.scope_revision > first.scope_revision
    assert second.inputs["dirty_start"] == "2026-10-01"
    values = session.exec(
        select(BusinessDay).where(
            BusinessDay.run_id == second.id, BusinessDay.spend > 0
        )
    ).all()
    assert all(row.drama_name == "Corrected catalog title" for row in values)
    assert (
        session.get(BusinessDay, (first.id, date(2026, 11, 1), c.advertiser_id, "a"))
        is not None
    )


def test_unlisted_number_and_report_name_keep_provider_finance_and_exact_details(
    session, business_case
):
    from app.modules.business.models import BusinessDay
    from app.modules.business.queries import query_dashboard
    from app.modules.business.schemas import DashboardFilter

    c = business_case
    seed_days(session, c)
    for fact in session.exec(select(CampaignReportDay)).all():
        fact.campaign_name = "jiashu-Unlisted historical drama-999999-1101-01-owner"
        session.add(fact)
    session.flush()
    run = build(session, c)[0]
    filters = DashboardFilter(
        bc_id=c.bc_id, start_date=date(2026, 11, 1), end_date=c.day
    )
    page = query_dashboard(session, context=c.context, filters=filters)
    assert page.total == 1
    assert page.rows[0].drama_id is None and page.rows[0].provider_id == c.provider.id
    assert page.totals["business_revenue"] == D(1600)
    assert page.totals["subsidy_comic"] == D(800)
    # 跨档补贴产生无当日消耗系列行，不能把外部身份键强转为UUID，也不能漏掉该行明细。
    synthetic = session.get(
        BusinessDay, (run.id, date(2026, 11, 2), c.advertiser_id, "a")
    )
    assert synthetic.spend == 0 and synthetic.subsidy_comic == D(400)
    detail = query_dashboard(
        session,
        context=c.context,
        filters=filters.model_copy(
            update={"dimension": "day", "drama_key": page.rows[0].key}
        ),
    )
    assert detail.totals["spend"] == D(2000)
    assert detail.totals["subsidy_comic"] == D(800)
    provider = query_dashboard(
        session,
        context=c.context,
        filters=filters.model_copy(update={"provider_id": c.provider.id}),
    )
    assert provider.totals["spend"] == D(2000)
    # 没有可靠编号时沿广告报表剧名分组，基础经营可查；不能把应用/合集名当新剧补贴门槛。
    for fact in session.exec(select(CampaignReportDay)).all():
        fact.campaign_name = "jiashu-Collection-remarks"
        session.add(fact)
    from app.modules.reporting.store_models import ReportSyncState

    for state in session.exec(select(ReportSyncState)).all():
        state.publication_version += 1
        session.add(state)
    session.flush()
    build(session, c)
    named = query_dashboard(session, context=c.context, filters=filters)
    assert named.total == 1 and named.rows[0].name == "Collection"
    assert named.totals["business_revenue"] == D(1600)
    assert named.totals["subsidy_comic"] is None


def test_same_campaign_multiple_drama_identities_never_overwrite_subsidy(
    session, business_case
):
    from app.modules.business.queries import query_dashboard
    from app.modules.business.schemas import DashboardFilter

    c = business_case
    seed_days(session, c)
    facts = session.exec(select(CampaignReportDay)).all()
    for fact in facts:
        fact.campaign_name = "jiashu-Drama A-999999-1101-01-owner"
        session.add(fact)
    session.add(
        CampaignReportDay(
            tenant_id=c.context.tenant_id,
            advertiser_id=c.advertiser_id,
            campaign_id="a",
            campaign_name="jiashu-Drama B-888888-1102-01-owner",
            stat_date=date(2026, 11, 2),
            currency="USD",
            timezone="UTC",
            spend=D(2000),
            revenue_total=D(800),
            fetched_at=datetime(2026, 11, 4, tzinfo=UTC),
            sync_job_id=uuid4(),
        )
    )
    session.flush()
    build(session, c)
    result = query_dashboard(
        session,
        context=c.context,
        filters=DashboardFilter(
            bc_id=c.bc_id, start_date=date(2026, 11, 1), end_date=c.day
        ),
    )
    assert result.totals["spend"] == D(4000)
    assert result.totals["subsidy_comic"] is None
    assert "DRAMA_IDENTITY_CONFLICT" in result.issues
