from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlmodel import select

from app.core.context import TenantContext
from app.models import User
from app.modules.ads.models import AdObject, CampaignNameProjection
from app.modules.reporting.filters import apply_authorized_scope, compile_filter
from app.modules.reporting.query_models import QuerySnapshot, read_snapshot
from app.modules.reporting.schemas import ReportingFilter
from app.modules.tenants.models import TenantMembership


def test_filter_literals_and_scope(report_case):
    report_case.seed_campaign("MAX_% 甲", "report-account", "10", "3")
    compiled = compile_filter(
        ReportingFilter(
            dimension="campaign",
            start_date=date(2026, 9, 30),
            end_date=date(2026, 9, 30),
            query="MAX_% 甲",
        )
    )
    assert compiled.keywords == ("MAX_%", "甲")
    assert compiled.like_patterns == (r"%MAX\_\%%", r"%甲%")
    statement = select(AdObject).where(
        AdObject.tenant_id == report_case.context.tenant_id,
        AdObject.kind == "campaign",
    )
    statement = compiled.apply(statement, name_column=AdObject.name)
    statement = apply_authorized_scope(
        report_case.session,
        statement,
        context=report_case.context,
        bc_id=report_case.bc_id,
        tenant_column=AdObject.tenant_id,
        advertiser_column=AdObject.advertiser_id,
    )
    assert [row.name for row in report_case.session.exec(statement)] == ["MAX_% 甲"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start_date": date(2026, 10, 1), "end_date": date(2026, 9, 30)},
        {"min_spend": Decimal("NaN")},
        {"max_spend": Decimal("Infinity")},
        {"sort_by": "unknown"},
    ],
)
def test_filter_rejects_invalid_ranges_values_and_sort(kwargs):
    base = {
        "dimension": "campaign",
        "start_date": date(2026, 9, 30),
        "end_date": date(2026, 9, 30),
    }
    with pytest.raises(ValueError):
        compile_filter(ReportingFilter(**(base | kwargs)))


def test_filter_compiles_directory_configuration_and_d0_thresholds():
    compiled = compile_filter(
        ReportingFilter(
            dimension="campaign",
            start_date=date(2026, 9, 30),
            end_date=date(2026, 9, 30),
            budget_modes=("BUDGET_MODE_DYNAMIC",),
            created_from=date(2026, 9, 1),
            created_to=date(2026, 9, 30),
            min_target_roas=Decimal("1.2"),
            max_target_roas=Decimal("3.0"),
            min_d0_roas=Decimal("1.0"),
        )
    )
    assert compiled.dimension == "campaign"


@pytest.mark.parametrize(
    "metric_kwargs",
    [
        {"optimization_goal": "VALUE"},
        {
            "values": {"d0_revenue": Decimal("1")},
            "availability": {"d0_revenue": "MISSING"},
        },
        {"values": {"spend": Decimal("1")}, "availability": {"spend": "VALUE"}},
        {"values": {"spend": Decimal("1")}, "availability": {}},
    ],
)
def test_metric_vector_rejects_unsupported_economics(metric_kwargs):
    from app.modules.reporting.schemas import MetricVector

    with pytest.raises(ValueError):
        MetricVector(
            currency="USD", timezone="UTC", attribution="default", **metric_kwargs
        )


def test_metric_vector_accepts_canonical_a_metrics_and_derived_roas():
    from app.modules.reporting.schemas import MetricVector

    row = MetricVector(
        currency="USD",
        timezone="UTC",
        attribution="default",
        values={
            "spend": Decimal("10"),
            "revenue_d0": Decimal("12"),
            "roas_d0": Decimal("1.2"),
        },
        availability={
            "spend": "AVAILABLE",
            "revenue_d0": "AVAILABLE",
            "roas_d0": "AVAILABLE",
        },
    )
    assert row.optimization_goal is None


def test_filter_scope_correlates_tenant_and_naming_status(report_case):
    own = report_case.seed_campaign("MAX_% 甲", "report-account", "10", "3")
    other_tenant_row = AdObject(
        tenant_id=report_case.other_context.tenant_id,
        advertiser_id="report-account",
        kind="campaign",
        remote_id="same-advertiser-other-tenant",
        ad_type="REGULAR",
        name="MAX_% 甲",
        observed_at=datetime.now(UTC),
        published_version=1,
    )
    report_case.session.add(other_tenant_row)
    report_case.session.add(
        CampaignNameProjection(
            tenant_id=report_case.context.tenant_id,
            advertiser_id=own.advertiser_id,
            campaign_remote_id=own.remote_id,
            raw_name="MAX_% 甲",
            provider_label="MAX_% 甲",
            drama_name="剧",
            status="VALID",
            parser_revision=1,
            name_revision=1,
            grouping_revision=1,
        )
    )
    report_case.session.add(
        CampaignNameProjection(
            tenant_id=report_case.other_context.tenant_id,
            advertiser_id="report-account",
            campaign_remote_id=other_tenant_row.remote_id,
            raw_name="MAX_% 甲",
            provider_label="MAX_% 甲",
            drama_name="剧",
            status="VALID",
            parser_revision=1,
            name_revision=1,
            grouping_revision=1,
        )
    )
    report_case.session.flush()
    filters = compile_filter(
        ReportingFilter(
            dimension="campaign",
            start_date=date(2026, 9, 30),
            end_date=date(2026, 9, 30),
            query="MAX_% 甲",
            naming_status="VALID",
        )
    )
    statement = filters.apply(
        select(CampaignNameProjection),
        name_column=CampaignNameProjection.raw_name,
        naming_status_column=CampaignNameProjection.status,
        advertiser_column=CampaignNameProjection.advertiser_id,
        remote_id_column=CampaignNameProjection.campaign_remote_id,
    )
    statement = apply_authorized_scope(
        report_case.session,
        statement,
        context=report_case.context,
        bc_id=report_case.bc_id,
        tenant_column=CampaignNameProjection.tenant_id,
        advertiser_column=CampaignNameProjection.advertiser_id,
    )
    rows = report_case.session.exec(statement).all()
    assert [row.campaign_remote_id for row in rows] == [own.remote_id]
    non_matching = compile_filter(
        ReportingFilter(
            dimension="campaign",
            start_date=date(2026, 9, 30),
            end_date=date(2026, 9, 30),
            naming_status="INVALID",
        )
    )
    empty_statement = non_matching.apply(
        select(CampaignNameProjection),
        naming_status_column=CampaignNameProjection.status,
    )
    assert (
        report_case.session.exec(
            empty_statement.where(
                CampaignNameProjection.tenant_id == report_case.context.tenant_id
            )
        ).all()
        == []
    )


def test_snapshot_read_is_tenant_bc_scoped_and_viewer_allowed(report_case):
    snapshot = QuerySnapshot(
        tenant_id=report_case.context.tenant_id,
        bc_id=report_case.bc_id,
        actor_id=report_case.context.actor_id,
        advertiser_ids=["report-account"],
        filters={"dimension": "campaign"},
        filter_digest="a" * 64,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    report_case.session.add(snapshot)
    viewer = User(
        id=uuid4(), username=f"viewer-{uuid4().hex[:10]}", hashed_password="test-only"
    )
    report_case.session.add(viewer)
    report_case.session.add(
        TenantMembership(
            tenant_id=report_case.context.tenant_id,
            user_id=viewer.id,
            role="viewer",
            active=True,
        )
    )
    report_case.session.flush()
    viewer_context = TenantContext(
        tenant_id=report_case.context.tenant_id, actor_id=viewer.id, role="viewer"
    )
    with pytest.raises(HTTPException) as denied:
        read_snapshot(
            report_case.session,
            context=viewer_context,
            bc_id=report_case.bc_id,
            snapshot_id=snapshot.id,
        )
    assert denied.value.status_code == 404
    # viewer可以读自己创建的快照；同租户并不授予其他actor的快照身份。
    snapshot = QuerySnapshot(
        tenant_id=report_case.context.tenant_id,
        bc_id=report_case.bc_id,
        actor_id=viewer.id,
        advertiser_ids=["report-account"],
        filters={"dimension": "campaign"},
        filter_digest="b" * 64,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    report_case.session.add(snapshot)
    report_case.session.flush()
    assert (
        read_snapshot(
            report_case.session,
            context=TenantContext(
                tenant_id=report_case.context.tenant_id,
                actor_id=viewer.id,
                role="viewer",
            ),
            bc_id=report_case.bc_id,
            snapshot_id=snapshot.id,
        ).id
        == snapshot.id
    )
    with pytest.raises(HTTPException) as error:
        read_snapshot(
            report_case.session,
            context=report_case.other_context,
            bc_id=report_case.bc_id,
            snapshot_id=snapshot.id,
        )
    assert error.value.status_code == 404
