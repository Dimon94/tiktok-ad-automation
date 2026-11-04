from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlmodel import select

from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
from app.modules.business.configuration import save_mapping, save_rate
from app.modules.business.models import BusinessPublication, BusinessRun
from app.modules.business.projection import advance_run, request_rebuild
from app.modules.business.queries import query_dashboard
from app.modules.business.schemas import DashboardFilter
from app.modules.reporting.store_models import CampaignReportDay, ReportSyncState
from tests.modules.business.test_projection import build, seed_days

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)


def finish(session, run):
    for _ in range(400):
        if advance_run(session, run.id, now=NOW):
            break
    assert run.status == "COMPLETE"


def test_restored_authorization_can_publish_a_previously_seen_signature(
    session, business_case
):
    c = business_case
    seed_days(session, c)
    first_grant = session.exec(
        select(BCAccountAccess).where(
            BCAccountAccess.tenant_id == c.context.tenant_id,
            BCAccountAccess.bc_id == c.bc_id,
        )
    ).one()
    session.add(
        AdvertiserAccount(
            tenant_id=c.context.tenant_id,
            advertiser_id="spare",
            currency="USD",
            timezone="UTC",
        )
    )
    session.flush()
    grant = BCAccountAccess(
        tenant_id=c.context.tenant_id,
        bc_id=c.bc_id,
        advertiser_id="spare",
        connection_id=first_grant.connection_id,
        in_bc=True,
        authorized=True,
        active=True,
        checked_at=NOW,
    )
    session.add(grant)
    day = date(2026, 10, 1)
    while day <= c.day:
        session.add(
            ReportSyncState(
                tenant_id=c.context.tenant_id,
                advertiser_id="spare",
                report_kind="ad_day",
                stat_date=day,
                status="COMPLETE",
                publication_version=1,
                observed_at=NOW,
            )
        )
        day += timedelta(days=1)
    session.flush()
    original = build(session, c)[0]
    grant.authorized = False
    session.add(grant)
    session.flush()
    restricted = build(session, c)[0]
    assert restricted.id != original.id
    grant.authorized = True
    session.add(grant)
    session.flush()
    restored = build(session, c)[0]
    pointer = session.get(
        BusinessPublication,
        (c.context.tenant_id, c.bc_id, date(2026, 10, 1)),
        populate_existing=True,
    )
    assert pointer.run_id == restored.id
    result = query_dashboard(
        session,
        context=c.context,
        filters=DashboardFilter(
            bc_id=c.bc_id, start_date=date(2026, 11, 1), end_date=c.day
        ),
    )
    assert "POLICY_SCOPE_RESTRICTED" not in result.issues


def test_unknown_quarter_prefix_is_not_zero_for_a_series_with_no_daily_fact(
    session, business_case
):
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
    build(session, c)
    result = query_dashboard(
        session,
        context=c.context,
        filters=DashboardFilter(
            bc_id=c.bc_id,
            start_date=date(2026, 11, 2),
            end_date=date(2026, 11, 2),
            dimension="campaign",
            advertiser_ids=[c.advertiser_id],
            campaign_id="a",
        ),
    )
    assert all(
        result.totals[k] is None
        for k in ("subsidy_comic", "subsidy_other", "subsidy_existing")
    )
    assert "SUBSIDY_SCOPE_INCOMPLETE" in result.issues


def test_retroactive_fee_publishes_one_coherent_multi_quarter_manifest(
    session, business_case
):
    c = business_case
    seed_days(session, c)
    session.add(
        ReportSyncState(
            tenant_id=c.context.tenant_id,
            advertiser_id=c.advertiser_id,
            report_kind="ad_day",
            stat_date=date(2026, 9, 30),
            status="COMPLETE",
            publication_version=1,
            observed_at=NOW,
        )
    )
    session.add(
        CampaignReportDay(
            tenant_id=c.context.tenant_id,
            advertiser_id=c.advertiser_id,
            campaign_id="old",
            stat_date=date(2026, 9, 30),
            currency="USD",
            timezone="UTC",
            spend=Decimal(1000),
            revenue_total=Decimal(800),
            fetched_at=NOW,
            sync_job_id=uuid4(),
        )
    )
    session.flush()
    old = request_rebuild(
        session,
        context=c.context,
        bc_id=c.bc_id,
        start_date=date(2026, 9, 30),
        end_date=c.day,
        now=NOW,
    )
    for run in old:
        finish(session, run)
    save_rate(
        session,
        context=c.context,
        kind="rebate",
        scope_id=c.bc_id,
        effective_date=date(2026, 9, 30),
        rate=Decimal("0.1"),
    )
    candidates = request_rebuild(
        session,
        context=c.context,
        bc_id=c.bc_id,
        start_date=date(2026, 9, 30),
        end_date=c.day,
        now=NOW,
    )
    q3, q4 = sorted(candidates, key=lambda r: r.quarter)
    finish(session, q4)
    filters = DashboardFilter(
        bc_id=c.bc_id, start_date=date(2026, 9, 30), end_date=c.day
    )
    partial = query_dashboard(session, context=c.context, filters=filters)
    assert partial.totals["rebate"] == 0
    assert partial.pending
    old_q3 = min(old, key=lambda r: r.quarter)
    with pytest.raises(HTTPException):
        query_dashboard(
            session,
            context=c.context,
            filters=filters.model_copy(update={"run_ids": [old_q3.id, q4.id]}),
        )
    finish(session, q3)
    complete = query_dashboard(session, context=c.context, filters=filters)
    assert complete.totals["rebate"] == 300


def test_missing_source_windows_use_observation_time_and_account_bounds():
    from app.modules.business.tasks import missing_source_windows

    run = BusinessRun(
        tenant_id=uuid4(),
        bc_id="test-bc",
        actor_id=uuid4(),
        quarter=date(2026, 10, 1),
        end_date=date(2026, 11, 3),
        cursor=date(2026, 10, 1),
        scope_revision=1,
        inputs={
            "accounts": {
                "account": {
                    "start": "2026-11-01",
                    "end": "2026-11-02",
                    "timezone": "UTC",
                    "currency": "USD",
                    "complete_through": "2026-11-03",
                },
                "still_today": {
                    "start": "2026-11-01",
                    "end": None,
                    "timezone": "UTC",
                    "currency": "USD",
                    "complete_through": "2026-10-31",
                },
            },
            "states": {
                "account/2026-11-01": {
                    "status": "COMPLETE",
                    "observed": "2026-11-01T23:59:00+00:00",
                }
            },
        },
    )
    windows = missing_source_windows(run, {"account", "still_today"})
    assert windows == [("account", date(2026, 11, 1), date(2026, 11, 2))]
