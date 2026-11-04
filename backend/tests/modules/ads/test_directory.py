from datetime import UTC, datetime

import pytest
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import select

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import EntityRef
from app.modules.accounts.models import AdvertiserAccount, TenantBC, TikTokConnection
from app.modules.ads.directory import (
    append_campaign_name_projection,
    list_objects,
    locate,
)
from app.modules.ads.models import AdMaterialReference, AdObject, CampaignNameProjection
from app.modules.tenants.models import TenantMembership


def test_rf2_external_ad_and_material_survive_missing_parent_and_local_file(
    session, directory_seed
):
    seed = directory_seed
    session.add(
        AdMaterialReference(
            tenant_id=seed.ref.tenant_id,
            advertiser_id=seed.ref.advertiser_id,
            ad_remote_id=seed.ref.remote_id,
            platform_material_id="external-vid",
            ad_material_id=None,
            material_type="VIDEO",
            local_material_id=None,
            operation_status=None,
            complete=True,
            published_version=1,
        )
    )
    session.flush()
    row = locate(session, context=seed.context, bc_id=seed.bc_id, ref=seed.ref)
    assert row.remote_id == "90071992547409933"
    assert row.published_version == 1
    assert row.parent_ref == EntityRef(
        seed.ref.tenant_id, "account-a", "adgroup", "not-yet-seen"
    )
    assert (
        row.ad_type,
        row.operation_status,
        row.review_status,
        row.delivery_status,
    ) == ("SMART_PLUS", "ENABLE", "APPROVED", "DELIVERING")
    material = session.exec(select(AdMaterialReference)).one()
    assert material.use_ref.ad_ref == seed.ref
    assert material.local_material_id is None


def test_foreign_tenant_and_bc_cannot_locate(session, directory_seed):
    seed = directory_seed
    session.add(TenantBC(tenant_id=seed.context.tenant_id, bc_id="other-bc"))
    session.flush()
    for context, bc_id in [
        (seed.other_context, seed.bc_id),
        (seed.context, "other-bc"),
    ]:
        with pytest.raises(DomainError):
            locate(session, context=context, bc_id=bc_id, ref=seed.ref)


@pytest.mark.parametrize("revocation", ["membership", "grant", "connection", "account"])
def test_rechecks_current_authority(session, directory_seed, revocation):
    seed = directory_seed
    assert locate(session, context=seed.context, bc_id=seed.bc_id, ref=seed.ref)
    if revocation == "membership":
        session.get(
            TenantMembership, (seed.context.tenant_id, seed.context.actor_id)
        ).active = False
    elif revocation == "grant":
        seed.grant.authorized = False
    elif revocation == "connection":
        seed.connection.status = "DISABLED"
    else:
        session.get(
            AdvertiserAccount, (seed.ref.tenant_id, seed.ref.advertiser_id)
        ).ownership_conflict = True
    session.flush()
    with pytest.raises(DomainError):
        locate(session, context=seed.context, bc_id=seed.bc_id, ref=seed.ref)


def test_bounded_list_enforces_all_accounts_and_parent_scope(session, directory_seed):
    seed = directory_seed
    kwargs = {
        "context": seed.context,
        "bc_id": seed.bc_id,
        "advertiser_ids": ("account-a",),
        "kind": "ad",
    }
    rows = list_objects(session, **kwargs, parent=seed.row.parent_ref)
    assert [row.remote_id for row in rows] == [seed.ref.remote_id]
    assert (
        list_objects(
            session,
            **kwargs,
            parent=EntityRef(seed.ref.tenant_id, "account-a", "adgroup", "elsewhere"),
        )
        == ()
    )
    for overrides in [
        {"advertiser_ids": ("account-a", "forbidden")},
        {"advertiser_ids": ()},
        {"kind": "invalid"},
        {
            "parent": EntityRef(
                seed.other_context.tenant_id, "account-a", "adgroup", "x"
            )
        },
    ]:
        with pytest.raises(DomainError):
            list_objects(session, **(kwargs | overrides))


def test_remote_identity_upsert_ignores_channel_but_separates_account_and_kind(
    session, directory_seed
):
    seed = directory_seed
    connection = TikTokConnection(
        tenant_id=seed.context.tenant_id, status="ACTIVE", kind="OFFICIAL_MCP"
    )
    session.add_all(
        [
            connection,
            AdvertiserAccount(tenant_id=seed.ref.tenant_id, advertiser_id="account-b"),
        ]
    )
    session.flush()
    values = seed.row.model_dump()
    table = AdObject.__table__
    stmt = insert(table).values(
        **(
            values
            | {
                "source_connection_id": connection.id,
                "source_channel": "OFFICIAL_MCP",
                "name": "new",
            }
        )
    )
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=["tenant_id", "advertiser_id", "kind", "remote_id"],
            set_={
                "name": stmt.excluded.name,
                "source_channel": stmt.excluded.source_channel,
                "source_connection_id": stmt.excluded.source_connection_id,
            },
        )
    )
    session.add_all(
        [
            AdObject(**(values | {"advertiser_id": "account-b"})),
            AdObject(**(values | {"kind": "creative"})),
        ]
    )
    session.flush()
    rows = session.exec(
        select(AdObject)
        .where(AdObject.tenant_id == seed.context.tenant_id)
        .order_by(AdObject.advertiser_id, AdObject.kind)
        .execution_options(populate_existing=True)
    ).all()
    assert len(rows) == 3
    assert rows[0].name == "new"
    assert rows[0].source_channel == "OFFICIAL_MCP"


def test_name_audit_separates_raw_revision_from_normalized_grouping(
    session, directory_seed
):
    seed = directory_seed
    ref = EntityRef(
        seed.ref.tenant_id, seed.ref.advertiser_id, "campaign", "campaign-1"
    )
    session.add(
        AdObject(
            tenant_id=ref.tenant_id,
            advertiser_id=ref.advertiser_id,
            kind="campaign",
            remote_id=ref.remote_id,
            ad_type="REGULAR",
            name="嘉书-剧名-备注",
            observed_at=datetime.now(UTC),
            published_version=1,
        )
    )
    session.flush()
    for name in [
        "嘉书-剧名-备注",
        " 嘉书 - 剧名 -新备注",
        "嘉书-新剧-备注",
        "嘉书--无效",
        "嘉书--新备注",
    ]:
        append_campaign_name_projection(
            session, campaign_ref=ref, raw_name=name, parser_revision=1
        )
    rows = session.exec(
        select(CampaignNameProjection).order_by(CampaignNameProjection.name_revision)
    ).all()
    assert [row.name_revision for row in rows] == [1, 2, 3, 4, 5]
    assert [row.grouping_revision for row in rows] == [1, 1, 2, 3, 3]
    assert [(row.provider_label, row.drama_name, row.status) for row in rows[:3]] == [
        ("嘉书", "剧名", "VALID"),
        ("嘉书", "剧名", "VALID"),
        ("嘉书", "新剧", "VALID"),
    ]
    assert all(row.campaign_ref == ref for row in rows)


def test_material_null_identity_is_deduplicated_and_distinct_ad_uses_survive(
    session, directory_seed
):
    from sqlalchemy.exc import IntegrityError

    seed = directory_seed
    values = {
        "tenant_id": seed.ref.tenant_id,
        "advertiser_id": "account-a",
        "ad_remote_id": seed.ref.remote_id,
        "platform_material_id": "vid",
        "material_type": "VIDEO",
        "published_version": 1,
        "complete": True,
    }
    session.add(AdMaterialReference(**values))
    session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(AdMaterialReference(**values))
        session.flush()
    session.add_all(
        [
            AdMaterialReference(**(values | {"ad_material_id": "inner-1"})),
            AdMaterialReference(**(values | {"ad_material_id": "inner-2"})),
        ]
    )
    session.flush()
    assert len(session.exec(select(AdMaterialReference)).all()) == 3


def test_run_and_page_scope_route_consistency_and_monotonic_order(
    session, directory_seed
):
    from uuid import uuid4

    from sqlalchemy.exc import IntegrityError

    from app.modules.ads.sync_models import AdDirectoryPage, AdDirectoryRun

    seed = directory_seed
    route = {
        "tenant_id": str(seed.ref.tenant_id),
        "bc_id": seed.bc_id,
        "connection_id": str(seed.connection.id),
        "channel": "OFFICIAL_API",
        "authorization_revision": 0,
        "binding_revision": 0,
        "adapter_contract_revision": "api-v1",
    }
    values = {
        "tenant_id": seed.ref.tenant_id,
        "advertiser_id": "account-a",
        "bc_id": seed.bc_id,
        "actor_id": seed.context.actor_id,
        "connection_id": seed.connection.id,
        "frozen_route": route,
        "partition_key": "1" * 64,
        "query": {"page": 1},
    }
    values.update(kind="ad", ad_type="SMART_PLUS")
    first = AdDirectoryRun(**values)
    second = AdDirectoryRun(**(values | {"partition_key": "2" * 64}))
    directory = AdDirectoryRun(**(values | {"kind": "ad", "ad_type": "SMART_PLUS"}))
    session.add_all([first, second, directory])
    session.flush()
    assert second.request_sequence > first.request_sequence > 0
    assert first.partition_key != second.partition_key
    for altered in [
        {"tenant_id": str(seed.other_context.tenant_id)},
        {"bc_id": "foreign"},
        {"connection_id": str(uuid4())},
        {"binding_revision": None},
    ]:
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(AdDirectoryRun(**(values | {"frozen_route": route | altered})))
            session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            AdDirectoryPage(
                tenant_id=seed.other_context.tenant_id,
                advertiser_id="account-a",
                run_id=directory.id,
                page=1,
                claim_generation=1,
                complete=True,
                items=[],
                materials=[],
                evidence={},
            )
        )
        session.flush()


def test_shared_balance_retains_scope_and_exact_amount(session, directory_seed):
    from decimal import Decimal

    from app.modules.reporting.models import AccountBalanceObservation

    balance = AccountBalanceObservation(
        tenant_id=directory_seed.ref.tenant_id,
        advertiser_id="account-a",
        amount=Decimal("99.123456789012345678901234"),
        currency="USD",
        availability="AVAILABLE",
        balance_scope="PORTFOLIO",
        scope_id="shared-portfolio",
        observed_at=datetime.now(UTC),
    )
    session.add(balance)
    session.flush()
    session.expire_all()
    assert balance.amount == Decimal("99.123456789012345678901234")
    assert (balance.balance_scope, balance.scope_id) == (
        "PORTFOLIO",
        "shared-portfolio",
    )


def test_local_material_from_other_bc_does_not_hide_ad(session, directory_seed):
    from uuid import uuid4

    from sqlalchemy.exc import IntegrityError

    from app.modules.materials.models import MaterialFile

    seed = directory_seed
    session.add(TenantBC(tenant_id=seed.ref.tenant_id, bc_id="source-bc"))
    session.flush()
    material = MaterialFile(
        tenant_id=seed.ref.tenant_id,
        bc_id="source-bc",
        file_name="synthetic.mp4",
        object_key=f"synthetic/{uuid4()}",
        byte_size=1,
    )
    session.add(material)
    session.flush()
    values = {
        "tenant_id": seed.ref.tenant_id,
        "advertiser_id": "account-a",
        "ad_remote_id": seed.ref.remote_id,
        "platform_material_id": "known-vid",
        "material_type": "VIDEO",
        "published_version": 1,
        "local_material_id": material.id,
    }
    session.add(AdMaterialReference(**values))
    session.flush()
    assert (
        locate(session, context=seed.context, bc_id=seed.bc_id, ref=seed.ref).remote_id
        == seed.ref.remote_id
    )
    session.add(
        AdvertiserAccount(
            tenant_id=seed.other_context.tenant_id, advertiser_id="account-a"
        )
    )
    session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            AdMaterialReference(
                **(values | {"tenant_id": seed.other_context.tenant_id})
            )
        )
        session.flush()


def test_external_material_metadata_roundtrip(session, directory_seed):

    seed = directory_seed
    material = AdMaterialReference(
        tenant_id=seed.ref.tenant_id,
        advertiser_id="account-a",
        ad_remote_id=seed.ref.remote_id,
        platform_material_id="library-vid",
        ad_material_id="ad-inner-id",
        material_type="VIDEO",
        published_version=1,
        name="external video name",
        creative_ids=["creative-1", "creative-2"],
    )
    session.add(material)
    session.flush()
    session.expire_all()
    assert material.name == "external video name"
    assert material.local_material_id is None
    assert material.creative_ids == ["creative-1", "creative-2"]
    assert (material.platform_material_id, material.ad_material_id) == (
        "library-vid",
        "ad-inner-id",
    )


def test_unchanged_resolved_name_keeps_identity_when_receipt_is_not_available(
    session, directory_seed
):
    ref = EntityRef(
        directory_seed.ref.tenant_id,
        directory_seed.ref.advertiser_id,
        "campaign",
        "free-name-history",
    )
    session.add(
        AdObject(
            tenant_id=ref.tenant_id,
            advertiser_id=ref.advertiser_id,
            kind="campaign",
            remote_id=ref.remote_id,
            name="剧名_版权方_1234",
            ad_type="REGULAR",
            observed_at=datetime.now(UTC),
            published_version=1,
        )
    )
    session.flush()
    previous = CampaignNameProjection(
        tenant_id=ref.tenant_id,
        advertiser_id=ref.advertiser_id,
        campaign_remote_id=ref.remote_id,
        raw_name="剧名_版权方_1234",
        provider_label="嘉书",
        drama_name="剧名",
        status="VALID",
        parser_revision=1,
        name_revision=1,
        grouping_revision=1,
    )
    session.add(previous)
    session.flush()
    replay = append_campaign_name_projection(
        session, campaign_ref=ref, raw_name=previous.raw_name, parser_revision=1
    )
    assert replay.name_revision == 1 and replay.grouping_revision == 1
    assert replay.provider_label == "嘉书" and replay.drama_name == "剧名"
