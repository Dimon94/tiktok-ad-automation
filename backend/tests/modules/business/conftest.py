from datetime import date
from types import SimpleNamespace

import pytest

from app.modules.providers.models import (
    ProviderApplication,
    ProviderConnection,
    ProviderDrama,
)
from app.modules.tenants.models import TenantMembership
from tests.modules.reporting import conftest as reporting_fixtures

other_context = reporting_fixtures.other_context
base_report_case = reporting_fixtures.report_case


@pytest.fixture
def business_case(session, base_report_case):
    ctx = base_report_case.context
    member = session.get(TenantMembership, (ctx.tenant_id, ctx.actor_id))
    member.role = "tenant_admin"
    session.add(member)
    provider = ProviderConnection(
        tenant_id=ctx.tenant_id, kind="jiashu", display_name="嘉书"
    )
    session.add(provider)
    session.flush()
    app = ProviderApplication(
        tenant_id=ctx.tenant_id,
        connection_id=provider.id,
        external_id="app",
        name="测试应用",
    )
    session.add(app)
    session.flush()
    drama = ProviderDrama(
        tenant_id=ctx.tenant_id,
        connection_id=provider.id,
        application_id="app",
        external_drama_id="drama",
        title="测试剧",
    )
    session.add(drama)
    session.flush()
    return SimpleNamespace(
        context=ctx,
        bc_id=base_report_case.bc_id,
        provider=provider,
        drama=drama,
        advertiser_id="report-account",
        day=date(2026, 11, 3),
    )
