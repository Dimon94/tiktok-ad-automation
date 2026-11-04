from datetime import date
from decimal import Decimal as D
from uuid import uuid4

import pytest

from app.core.errors import DomainError


def test_rate_versions_defaults_and_effective_dates(session, business_case):
    from app.modules.business.configuration import rate_on, save_rate

    c = business_case
    assert (
        rate_on(session, c.context.tenant_id, "provider", str(c.provider.id), c.day)
        == 0
    )
    save_rate(
        session,
        context=c.context,
        kind="provider",
        scope_id=str(c.provider.id),
        effective_date=date(2026, 10, 1),
        rate=D("0.08"),
    )
    save_rate(
        session,
        context=c.context,
        kind="provider",
        scope_id=str(c.provider.id),
        effective_date=date(2026, 11, 1),
        rate=D("0.07"),
    )
    assert rate_on(
        session, c.context.tenant_id, "provider", str(c.provider.id), date(2026, 10, 20)
    ) == D("0.08")
    assert rate_on(
        session, c.context.tenant_id, "provider", str(c.provider.id), c.day
    ) == D("0.07")
    save_rate(
        session,
        context=c.context,
        kind="provider",
        scope_id=str(c.provider.id),
        effective_date=date(2026, 11, 1),
        rate=D("0.06"),
    )
    assert rate_on(
        session, c.context.tenant_id, "provider", str(c.provider.id), c.day
    ) == D("0.06")


def test_rate_rejects_unknown_connection_and_out_of_range(session, business_case):
    from app.modules.business.configuration import save_rate

    c = business_case
    with pytest.raises(DomainError):
        save_rate(
            session,
            context=c.context,
            kind="provider",
            scope_id=str(uuid4()),
            effective_date=c.day,
            rate=D("0.08"),
        )
    with pytest.raises(ValueError):
        save_rate(
            session,
            context=c.context,
            kind="provider",
            scope_id=str(c.provider.id),
            effective_date=c.day,
            rate=D("1.1"),
        )


def test_mapping_uses_stable_tenant_drama_and_account_authority(session, business_case):
    from app.modules.business.configuration import save_mapping

    c = business_case
    row = save_mapping(
        session,
        context=c.context,
        bc_id=c.bc_id,
        advertiser_id=c.advertiser_id,
        campaign_id="campaign",
        drama_id=c.drama.id,
    )
    assert row.drama_id == c.drama.id
    with pytest.raises(DomainError):
        save_mapping(
            session,
            context=c.context,
            bc_id=c.bc_id,
            advertiser_id="not-authorized",
            campaign_id="campaign",
            drama_id=c.drama.id,
        )
    with pytest.raises(DomainError):
        save_mapping(
            session,
            context=c.context,
            bc_id=c.bc_id,
            advertiser_id=c.advertiser_id,
            campaign_id="campaign",
            drama_id=uuid4(),
        )
