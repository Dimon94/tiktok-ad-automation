from datetime import date
from decimal import Decimal as D

from app.modules.business.configuration import save_mapping
from tests.modules.business.test_projection import build, seed_days


def prepared(session, c):
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
    build(session, c)


def test_query_cards_and_all_dimensions_share_totals(session, business_case):
    from app.modules.business.queries import query_dashboard
    from app.modules.business.schemas import DashboardFilter

    c = business_case
    prepared(session, c)
    for dimension, total in [("campaign", 2), ("drama", 1), ("provider", 1)]:
        result = query_dashboard(
            session,
            context=c.context,
            filters=DashboardFilter(
                bc_id=c.bc_id,
                start_date=date(2026, 11, 1),
                end_date=c.day,
                dimension=dimension,
                scenario="comic",
                limit=1,
            ),
        )
        assert result.total == total
        assert result.totals["spend"] == D(2000)
        assert result.totals["subsidy_comic"] == D(800)
        assert result.totals["platform_roas"] == D("0.8")
        # 赔付没有证据，因此含预计情景不得悄悄将其当0。
        assert result.totals["scenario_revenue"] is None


def test_series_filter_keeps_original_subsidy_share(session, business_case):
    from app.modules.business.queries import query_dashboard
    from app.modules.business.schemas import DashboardFilter

    c = business_case
    prepared(session, c)
    result = query_dashboard(
        session,
        context=c.context,
        filters=DashboardFilter(
            bc_id=c.bc_id,
            start_date=date(2026, 11, 2),
            end_date=c.day,
            dimension="campaign",
            campaign_id="a",
        ),
    )
    assert result.totals["subsidy_comic"] == D(400)
    assert result.totals["spend"] == 0


def test_frozen_run_ids_survive_new_publication(session, business_case):
    from app.modules.business.configuration import save_rate
    from app.modules.business.queries import query_dashboard
    from app.modules.business.schemas import DashboardFilter

    c = business_case
    prepared(session, c)
    filters = DashboardFilter(
        bc_id=c.bc_id, start_date=date(2026, 11, 1), end_date=c.day
    )
    first = query_dashboard(session, context=c.context, filters=filters)
    save_rate(
        session,
        context=c.context,
        kind="rebate",
        scope_id=c.bc_id,
        effective_date=date(2026, 11, 1),
        rate=D("0.06"),
    )
    build(session, c)
    newer = query_dashboard(session, context=c.context, filters=filters)
    old = query_dashboard(
        session,
        context=c.context,
        filters=filters.model_copy(update={"run_ids": first.run_ids}),
    )
    assert old.totals["business_revenue"] == D(1600)
    assert newer.totals["business_revenue"] == D(1720)


def test_drama_and_provider_daily_summaries_are_precomputed(session, business_case):
    from sqlmodel import select

    from app.modules.business.models import BusinessSummaryDay

    c = business_case
    prepared(session, c)
    rows = session.exec(select(BusinessSummaryDay)).all()
    for kind in ("drama", "provider"):
        assert sum(r.spend for r in rows if r.kind == kind) == 2000
        assert sum(r.subsidy_comic for r in rows if r.kind == kind) == 800
