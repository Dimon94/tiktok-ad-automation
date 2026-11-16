# backend/tests/modules/reporting/sync_conftest.py
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models import User
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    ConnectionAuthorization,
)
from app.modules.accounts.models import (
    AdvertiserAccount,
    BCAccountAccess,
    DiscoveryRun,
    TenantBC,
    TikTokConnection,
)
from app.modules.tenants.models import Tenant, TenantMembership


def grant_accounts(session, seed, accounts, *, bc_id="bc-a"):
    """合成账户同时具备当前连接的目录授权，测试仍走生产 verify_route。"""
    from sqlalchemy.dialects.postgresql import insert

    session.flush()
    rows = [
        BCAccountAccess(
            tenant_id=seed.tenant_id,
            bc_id=bc_id,
            advertiser_id=advertiser,
            connection_id=seed.connection.id,
            in_bc=True,
            authorized=True,
            active=True,
            checked_at=datetime.now(UTC),
        ).model_dump()
        for advertiser in accounts
    ]
    session.execute(insert(BCAccountAccess), rows)


@pytest.fixture
def sync_seed(session, *, historical=False):
    """真实 PostgreSQL：一个租户、一个 API 连接、两个 BC（bc-a、bc-b），
    账户 acc-1（香港时区）只在 bc-a，acc-2（UTC）同时在 bc-a 与 bc-b。"""

    def add(row):
        if historical:
            from tests.migration_database import insert_historical_row

            insert_historical_row(session, row)
        else:
            session.add(row)

    def add_all(rows):
        for row in rows:
            add(row)

    actor, tenant = uuid4(), uuid4()
    add(
        User(
            id=actor,
            username=f"sync-{actor.hex[:10]}",
            hashed_password="x",
            is_active=True,
        )
    )
    add(Tenant(id=tenant, name=f"t-{tenant.hex[:8]}"))
    session.flush()
    add(TenantMembership(tenant_id=tenant, user_id=actor, role="operator", active=True))
    connection = TikTokConnection(
        tenant_id=tenant,
        status="ACTIVE",
        kind="OFFICIAL_API",
        authorization_revision=0,
        adapter_contract_revision="official-api-v1",
    )
    add_all(
        [
            TenantBC(tenant_id=tenant, bc_id="bc-a"),
            TenantBC(tenant_id=tenant, bc_id="bc-b"),
            connection,
        ]
    )
    session.flush()
    add_all(
        [
            AdvertiserAccount(
                tenant_id=tenant,
                advertiser_id="acc-1",
                currency="USD",
                timezone="Asia/Hong_Kong",
                name="账户一",
            ),
            AdvertiserAccount(
                tenant_id=tenant,
                advertiser_id="acc-2",
                currency="USD",
                timezone="UTC",
                name="账户二",
            ),
            ConnectionAuthorization(
                tenant_id=tenant,
                connection_id=connection.id,
                authorization_revision=0,
                source="OFFLINE_TEST",
                verified_at=datetime.now(UTC),
                permission_summary={"read_authorized": True},
            ),
        ]
    )
    session.flush()
    for bc in ("bc-a", "bc-b"):
        add(
            DiscoveryRun(
                tenant_id=tenant,
                actor_id=actor,
                connection_id=connection.id,
                bc_id=bc,
                status="COMPLETE",
                completed_at=datetime.now(UTC),
            )
        )
        add(
            BCConnectionBinding(
                tenant_id=tenant,
                bc_id=bc,
                connection_id=connection.id,
                kind="OFFICIAL_API",
                status="ACTIVE",
                authorization_revision=0,
                revision=0,
            )
        )
    for bc, acc in (("bc-a", "acc-1"), ("bc-a", "acc-2"), ("bc-b", "acc-2")):
        add(
            BCAccountAccess(
                tenant_id=tenant,
                bc_id=bc,
                advertiser_id=acc,
                connection_id=connection.id,
                in_bc=True,
                authorized=True,
                active=True,
                checked_at=datetime.now(UTC),
            )
        )
    session.flush()
    return SimpleNamespace(
        tenant_id=tenant,
        actor_id=actor,
        connection=connection,
        context=SimpleNamespace(tenant_id=tenant, actor_id=actor, role="operator"),
    )


def ad_row(
    ad,
    day,
    *,
    campaign="c1",
    adgroup="g1",
    spend="1.00",
    d0="0.50",
    total="0.50",
    adv="acc-1",
):
    return {
        "dimensions": {"ad_id_v2": ad, "stat_time_day": f"{day} 00:00:00"},
        "metrics": {
            "advertiser_id": adv,
            "campaign_id": campaign,
            "adgroup_id": adgroup,
            "spend": spend,
            "impressions": "10",
            "clicks": "1",
            "native_growth_total_ad_impression_event_count": "1",
            "native_growth_ad_revenue_value_d0": d0,
            "native_growth_ad_revenue_value_d2": d0,
            "native_growth_ad_revenue_value_d6": d0,
            "native_growth_ad_revenue_value_d13": d0,
            "native_growth_ad_revenue_value_d29": d0,
            "native_growth_total_ad_impression_value": total,
            "campaign_name": f"嘉书-剧{campaign}-备注",
            "adgroup_name": "组",
            "ad_name": "广告",
            "campaign_automation_type": "UPGRADED_SMART_PLUS",
        },
    }


@pytest.fixture
def committed_sync_seed():
    """独立提交的随机租户供真实多连接领取/发布；只清理本夹具创建的租户。"""
    from sqlmodel import Session, SQLModel

    from app.core.db import engine

    def factory():
        return Session(engine, expire_on_commit=False)

    with factory() as db:
        seed = sync_seed.__wrapped__(db)
        db.commit()
    try:
        yield seed, factory
    finally:
        with factory() as db:
            # 新事实表不挂 BC；先删除其租户行，再逆外键顺序删除授权种子。
            names = (
                "ad_sync_account",
                "ad_sync_bc",
                "report_sync_job",
                "report_sync_state",
                "account_reconciliation_day",
                "campaign_report_day",
                "campaign_report_hour",
                "ad_report_day",
            )
            for name in names:
                table = SQLModel.metadata.tables[name]
                db.exec(table.delete().where(table.c.tenant_id == seed.tenant_id))
            for table in reversed(SQLModel.metadata.sorted_tables):
                if "tenant_id" in table.c and table.name not in names:
                    db.exec(table.delete().where(table.c.tenant_id == seed.tenant_id))
            db.exec(
                SQLModel.metadata.tables["tenant"]
                .delete()
                .where(SQLModel.metadata.tables["tenant"].c.id == seed.tenant_id)
            )
            db.exec(
                SQLModel.metadata.tables["user"]
                .delete()
                .where(SQLModel.metadata.tables["user"].c.id == seed.actor_id)
            )
            db.commit()
