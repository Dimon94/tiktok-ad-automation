from uuid import UUID, uuid4

import pytest
from sqlalchemy import func
from sqlmodel import select

from app.jobs.models import PendingDispatch
from app.modules.builds.drafts import create_draft
from app.modules.tenants.models import TenantMembership
from tests.modules.builds.test_drafts import account, finish, ready_links
from tests.modules.materials.test_tenant_materials import material
from tests.modules.strategies.test_api import headers


@pytest.mark.parametrize("catalog", ["minis", "identities"])
def test_catalog_refresh_requires_build_permission_and_own_draft(
    client, session, context, other_context, intent, catalog
):
    account(session, context)
    draft = create_draft(session, context=context, **intent)
    path = f"/api/tenants/{context.tenant_id}/build-drafts/{draft}/{catalog}/refresh"
    response = client.post(path, headers=headers(context))
    assert response.status_code == 200
    assert response.json() == {"expired": 0}
    assert (
        client.post(
            f"/api/tenants/{other_context.tenant_id}/build-drafts/{draft}/{catalog}/refresh",
            headers=headers(other_context),
        ).status_code
        == 404
    )
    member = session.exec(
        select(TenantMembership).where(
            TenantMembership.tenant_id == context.tenant_id,
            TenantMembership.user_id == context.actor_id,
        )
    ).one()
    member.role = "viewer"
    session.add(member)
    session.flush()
    assert client.post(path, headers=headers(context)).status_code == 403


def test_draft_http_keeps_rows_paged_and_revision_conflicts_safe(
    client, context, other_context, intent
):
    body = {
        key: str(value) if key.endswith("_id") else value
        for key, value in intent.items()
    } | {"request_id": str(uuid4())}
    base = f"/api/tenants/{context.tenant_id}/build-drafts"
    first = client.post(base, json=body, headers=headers(context))
    assert first.status_code == 201
    draft_id = first.json()["draft_id"]
    assert client.post(base, json=body, headers=headers(context)).json() == first.json()
    page = client.get(
        f"{base}/{draft_id}/inputs",
        params={"kind": "drama", "limit": 2},
        headers=headers(context),
    ).json()
    assert [row["raw_text"] for row in page["items"]] == [" Moon ", "Moon"]
    next_page = client.get(
        f"{base}/{draft_id}/inputs",
        params={"kind": "drama", "limit": 2, "cursor": page["next_cursor"]},
        headers=headers(context),
    ).json()
    assert [row["raw_text"] for row in next_page["items"]] == ["", "Short Drama"]
    rejected = client.get(
        f"{base}/{draft_id}/inputs",
        params={"kind": "account", "cursor": page["next_cursor"]},
        headers=headers(context),
    )
    assert rejected.status_code == 422
    patched = client.patch(
        f"{base}/{draft_id}",
        json={"expected_revision": 1, "account_lines": ["A", "B"]},
        headers=headers(context),
    )
    assert patched.status_code == 200 and patched.json()["revision"] == 2
    assert (
        client.patch(
            f"{base}/{draft_id}",
            json={"expected_revision": 1, "account_lines": ["C"]},
            headers=headers(context),
        ).status_code
        == 409
    )
    assert (
        client.get(
            f"{base}/{draft_id}/inputs",
            params={"kind": "drama", "cursor": page["next_cursor"]},
            headers=headers(context),
        ).status_code
        == 422
    )
    assert (
        client.get(
            f"/api/tenants/{other_context.tenant_id}/build-drafts/{draft_id}",
            headers=headers(other_context),
        ).status_code
        == 404
    )
    summary = client.get(f"{base}/{draft_id}", headers=headers(context)).json()
    assert summary["input_counts"]["account"] == {"pending": 2}
    assert "account_lines" not in summary and summary["account_count"] == 0


def test_viewer_can_read_but_cannot_schedule_and_saved_request_is_scoped(
    client, session, context, other_context, intent
):
    request = uuid4()
    draft = create_draft(session, context=context, request_id=request, **intent)
    member = session.exec(
        select(TenantMembership).where(
            TenantMembership.tenant_id == context.tenant_id,
            TenantMembership.user_id == context.actor_id,
        )
    ).one()
    member.role = "viewer"
    session.add(member)
    session.flush()
    base = f"/api/tenants/{context.tenant_id}"
    assert (
        client.get(f"{base}/build-drafts/{draft}", headers=headers(context)).status_code
        == 200
    )
    assert (
        client.post(
            f"{base}/build-drafts/{draft}/prepare",
            json={"request_id": str(uuid4())},
            headers=headers(context),
        ).status_code
        == 403
    )
    assert client.get(
        f"{base}/build-draft-requests/{request}", headers=headers(context)
    ).json()["draft_id"] == str(draft)
    assert (
        client.get(
            f"/api/tenants/{other_context.tenant_id}/build-draft-requests/{request}",
            headers=headers(other_context),
        ).status_code
        == 404
    )


def test_prepared_http_reads_shared_groups_without_enqueuing(
    client, session, context, intent
):
    base = f"/api/tenants/{context.tenant_id}"
    account(session, context)
    shared = material(session, context, "Moon Short Drama.mp4", bc="bc-draft")
    draft = create_draft(session, context=context, **intent)
    request = uuid4()
    response = client.post(
        f"{base}/build-drafts/{draft}/prepare",
        json={"request_id": str(request)},
        headers=headers(context),
    )
    assert response.status_code == 202, response.json()
    task = response.json()["task_id"]
    ready_links(session, context, UUID(task), intent)
    finish(session, context, UUID(task))
    before = session.exec(select(func.count()).select_from(PendingDispatch)).one()
    rows = client.get(
        f"{base}/build-drafts/{draft}/dramas", headers=headers(context)
    ).json()["items"]
    assert len(rows) == 2
    for row in rows:
        result = client.get(
            f"{base}/build-drafts/{draft}/dramas/{row['drama_id']}/materials",
            headers=headers(context),
        )
        assert result.status_code == 200
        assert result.json()["items"] == [
            {
                "material_id": str(shared.id),
                "file_name": "Moon Short Drama.mp4",
                "source_bc_id": "bc-draft",
                "content_key": f"material:{shared.id}",
                "group_no": 1,
                "position": 1,
                "shared_with_other_drama": True,
            }
        ]
    assert client.get(
        f"{base}/build-preparation-requests/{request}", headers=headers(context)
    ).json() == {"task_id": task, "revision": 1}
    assert (
        session.exec(select(func.count()).select_from(PendingDispatch)).one() == before
    )
