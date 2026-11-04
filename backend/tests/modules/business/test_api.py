from datetime import date

import pytest

from app.api.deps import get_current_user
from app.main import app
from app.models import User
from app.modules.tenants.models import TenantMembership


@pytest.fixture
def business_client(client, session, business_case):
    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_current_user] = lambda: session.get(
        User, business_case.context.actor_id
    )
    try:
        yield client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def test_api_admin_rate_validation_and_tenant_boundary(
    business_client, business_case, other_context
):
    c = business_case
    base = f"/api/tenants/{c.context.tenant_id}/business"
    body = {
        "kind": "provider",
        "scope_id": str(c.provider.id),
        "effective_date": "2026-10-01",
        "rate": "0.08",
    }
    result = business_client.post(f"{base}/rates", json=body)
    assert result.status_code == 200
    assert result.json()["rate"] == "0.08"
    assert (
        business_client.post(f"{base}/rates", json={**body, "rate": "1.08"}).status_code
        == 422
    )
    other = f"/api/tenants/{other_context.tenant_id}/business/rates"
    assert business_client.post(other, json=body).status_code == 403


def test_viewer_reads_but_cannot_change_rates_or_mapping(
    business_client, session, business_case
):
    c = business_case
    member = session.get(TenantMembership, (c.context.tenant_id, c.context.actor_id))
    member.role = "viewer"
    session.add(member)
    session.flush()
    base = f"/api/tenants/{c.context.tenant_id}/business"
    assert (
        business_client.get(f"{base}/options", params={"bc_id": c.bc_id}).status_code
        == 200
    )
    assert (
        business_client.get(
            f"{base}/options", params={"bc_id": "another-bc"}
        ).status_code
        == 403
    )
    assert (
        business_client.post(
            f"{base}/rates",
            json={
                "kind": "rebate",
                "scope_id": c.bc_id,
                "effective_date": "2026-10-01",
                "rate": "0.1",
            },
        ).status_code
        == 403
    )
    assert (
        business_client.put(
            f"{base}/mapping",
            json={
                "bc_id": c.bc_id,
                "advertiser_id": c.advertiser_id,
                "campaign_id": "a",
                "drama_id": str(c.drama.id),
            },
        ).status_code
        == 403
    )


def test_evidence_must_be_authorized_and_auditable(business_client, business_case):
    c = business_case
    url = f"/api/tenants/{c.context.tenant_id}/business/evidence"
    body = {
        "bc_id": c.bc_id,
        "advertiser_id": c.advertiser_id,
        "campaign_id": "a",
        "stat_date": str(date(2026, 11, 3)),
        "bid": "1.06",
        "eligible": True,
        "source": "本地合成：已核实当日CBO Mini Dramas D0 IAA及操作依据",
    }
    assert business_client.put(url, json=body).status_code == 200
    fetched = business_client.get(
        url,
        params={
            k: body[k] for k in ("bc_id", "advertiser_id", "campaign_id", "stat_date")
        },
    )
    assert fetched.status_code == 200
    assert fetched.json()["bid"] == "1.06"
    assert fetched.json()["source"] == body["source"]
    assert (
        business_client.put(
            url, json={**body, "advertiser_id": "not-authorized"}
        ).status_code
        == 403
    )
    assert business_client.put(url, json={**body, "source": ""}).status_code == 422
