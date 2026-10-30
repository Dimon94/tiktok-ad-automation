from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.integrations.tiktok.contracts.ads import EntityRef
from app.models import User
from app.modules.accounts.models import (
    AdvertiserAccount,
    BCAccountAccess,
    TenantBC,
    TikTokConnection,
)
from app.modules.ads.models import AdObject
from app.modules.tenants.models import Tenant, TenantMembership


@pytest.fixture(autouse=True)
def isolated_sync_quota(monkeypatch):
    """调度现在必须显式核验真实额度合同；测试使用独立app域，绝不读取运行配置。"""
    from app.core.config import settings
    from app.integrations.tiktok.platform_quotas import quota_configuration

    monkeypatch.setattr(settings, "TIKTOK_APP_ID", f"test-reporting-{uuid4()}")
    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", quota_configuration("BASIC"))


@pytest.fixture
def other_context() -> SimpleNamespace:
    """第二租户用于确认同名目录对象不能越过租户边界。"""
    return SimpleNamespace(tenant_id=uuid4(), actor_id=uuid4(), role="operator")


@pytest.fixture
def report_case(session, context, other_context):
    """B 查询场景：真实 PostgreSQL 中的租户、BC 授权、目录和事实。"""

    def seed_identity(scope):
        if session.get(User, scope.actor_id) is None:
            session.add(
                User(
                    id=scope.actor_id,
                    username=f"report-{scope.actor_id.hex[:12]}",
                    hashed_password="test-only",
                    is_active=True,
                )
            )
        if session.get(Tenant, scope.tenant_id) is None:
            session.add(
                Tenant(id=scope.tenant_id, name=f"report-{scope.tenant_id.hex[:12]}")
            )
        if session.get(TenantMembership, (scope.tenant_id, scope.actor_id)) is None:
            session.add(
                TenantMembership(
                    tenant_id=scope.tenant_id,
                    user_id=scope.actor_id,
                    role="operator",
                    active=True,
                )
            )
        session.flush()

    seed_identity(context)
    seed_identity(other_context)
    own_connection = TikTokConnection(
        tenant_id=context.tenant_id, status="ACTIVE", kind="OFFICIAL_API"
    )
    own_bc = TenantBC(tenant_id=context.tenant_id, bc_id="bc-report-case")
    session.add_all(
        [
            own_connection,
            own_bc,
            AdvertiserAccount(
                tenant_id=context.tenant_id,
                advertiser_id="report-account",
                currency="USD",
                timezone="UTC",
            ),
        ]
    )
    session.flush()
    session.add(
        BCAccountAccess(
            tenant_id=context.tenant_id,
            bc_id=own_bc.bc_id,
            advertiser_id="report-account",
            connection_id=own_connection.id,
            in_bc=True,
            authorized=True,
            active=True,
            checked_at=datetime.now(UTC),
        )
    )
    other_connection = TikTokConnection(
        tenant_id=other_context.tenant_id, status="ACTIVE", kind="OFFICIAL_API"
    )
    other_bc = TenantBC(tenant_id=other_context.tenant_id, bc_id="bc-other")
    session.add_all(
        [
            other_connection,
            other_bc,
            AdvertiserAccount(
                tenant_id=other_context.tenant_id,
                advertiser_id="report-account",
                currency="USD",
                timezone="UTC",
            ),
        ]
    )
    session.flush()
    session.add(
        BCAccountAccess(
            tenant_id=other_context.tenant_id,
            bc_id=other_bc.bc_id,
            advertiser_id="report-account",
            connection_id=other_connection.id,
            in_bc=True,
            authorized=True,
            active=True,
            checked_at=datetime.now(UTC),
        )
    )
    session.flush()

    published_rows = {}

    def seed_campaign(
        name: str,
        advertiser_id: str,
        spend: Decimal | str | int,
        d0_revenue: Decimal | str | int,
        status: str = "ENABLE",
    ) -> EntityRef:
        account = AdvertiserAccount(
            tenant_id=context.tenant_id,
            advertiser_id=advertiser_id,
            currency="USD",
            timezone="UTC",
            remote_status=status,
        )
        if session.get(AdvertiserAccount, (context.tenant_id, advertiser_id)) is None:
            session.add(account)
            session.flush()
        ref = EntityRef(
            context.tenant_id, advertiser_id, "campaign", f"campaign-{uuid4().hex[:12]}"
        )
        session.add(
            AdObject(
                tenant_id=ref.tenant_id,
                advertiser_id=ref.advertiser_id,
                kind=ref.kind,
                remote_id=ref.remote_id,
                ad_type="REGULAR",
                name=name,
                operation_status=status,
                review_status="APPROVED",
                observed_at=datetime.now(UTC),
                published_version=1,
                source_connection_id=own_connection.id,
                source_channel="OFFICIAL_API",
            )
        )
        session.flush()
        from app.modules.reporting.publish import AccountMeta, publish_ad_day
        from tests.modules.reporting.sync_conftest import ad_row

        rows = published_rows.setdefault(advertiser_id, [])
        rows.append(
            ad_row(
                f"ad-{ref.remote_id}",
                date(2026, 9, 30),
                campaign=ref.remote_id,
                adv=advertiser_id,
                spend=str(spend),
                d0=str(d0_revenue),
                total=str(d0_revenue),
            )
        )
        publish_ad_day(
            session,
            tenant_id=context.tenant_id,
            job_id=uuid4(),
            accounts={advertiser_id: AccountMeta("USD", "UTC")},
            start=date(2026, 9, 30),
            end=date(2026, 9, 30),
            rows=rows,
        )
        return ref

    return SimpleNamespace(
        session=session,
        context=context,
        other_context=other_context,
        bc_id=own_bc.bc_id,
        headers={
            "x-tenant-id": str(context.tenant_id),
            "x-actor-id": str(context.actor_id),
        },
        other_headers={
            "x-tenant-id": str(other_context.tenant_id),
            "x-actor-id": str(other_context.actor_id),
        },
        seed_campaign=seed_campaign,
        fact_date=date(2026, 9, 30),
        currency="USD",
        timezone="UTC",
    )


from tests.modules.reporting.sync_conftest import *  # noqa: E402,F401,F403
