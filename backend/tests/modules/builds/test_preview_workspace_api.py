from decimal import Decimal
from uuid import uuid4

from sqlalchemy import delete

from app.modules.builds import previews
from app.modules.builds.drafts import create_draft
from app.modules.builds.preview_models import PreviewAdMaterial
from tests.modules.builds.test_previews import drain
from tests.modules.builds.test_previews import prepared as prepared
from tests.modules.builds.video_source import VIDEO_SOURCE
from tests.modules.strategies.test_api import headers


def test_preview_api_rejects_missing_mini_selection(client, session, context, prepared):
    from app.modules.builds.mini_targets import MiniTarget, url_key

    session.delete(
        session.get(
            MiniTarget, (context.tenant_id, url_key("https://example.com/drama"))
        )
    )
    session.flush()
    response = client.post(
        f"/api/tenants/{context.tenant_id}/build-drafts/{prepared}/previews",
        json={"expected_revision": 1},
        headers=headers(context),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "minis_selection_required"


def test_mutation_request_recovers_original_revision_after_later_edit(
    client, session, context, intent
):
    draft = create_draft(session, context=context, **intent)
    base = f"/api/tenants/{context.tenant_id}"
    request = uuid4()
    body = {
        "expected_revision": 1,
        "request_id": str(request),
        "account_lines": ["A", "B"],
    }
    first = client.patch(
        f"{base}/build-drafts/{draft}", json=body, headers=headers(context)
    )
    assert first.status_code == 200 and first.json()["revision"] == 2
    second = client.patch(
        f"{base}/build-drafts/{draft}",
        json={
            "expected_revision": 2,
            "request_id": str(uuid4()),
            "account_lines": ["C"],
        },
        headers=headers(context),
    )
    assert second.status_code == 200 and second.json()["revision"] == 3
    assert (
        client.patch(
            f"{base}/build-drafts/{draft}", json=body, headers=headers(context)
        ).json()
        == first.json()
    )
    assert (
        client.get(
            f"{base}/build-mutation-requests/{request}", headers=headers(context)
        ).json()
        == first.json()
    )
    changed = client.patch(
        f"{base}/build-drafts/{draft}",
        json={**body, "account_lines": ["different"]},
        headers=headers(context),
    )
    assert changed.status_code == 409


def test_preview_drama_groups_and_unit_filters_are_server_scoped(
    session, client, context, other_context, prepared
):
    preview = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    drain(session, context, preview)
    base = f"/api/tenants/{context.tenant_id}/build-previews/{preview}"
    first = client.get(f"{base}/dramas?limit=1", headers=headers(context))
    assert first.status_code == 200
    data = first.json()
    drama = data["items"][0]
    assert drama["account_count"] == 3
    assert drama["material_group_count"] == 3 and drama["material_count"] == 23
    assert drama["unique_material_count"] == 23
    assert drama["ad_material_allocation_count"] > 0
    assert drama["eligible_adgroup_count"] == 9 and drama["eligible_ad_count"] == 18
    assert Decimal(drama["daily_budget_sum"]) == 300
    second = client.get(
        f"{base}/dramas",
        params={"limit": 1, "cursor": data["next_cursor"]},
        headers=headers(context),
    ).json()
    assert len(second["items"]) == 1 and second["next_cursor"] is None
    units = client.get(
        f"{base}/units",
        params={"drama_id": drama["drama_id"], "limit": 2},
        headers=headers(context),
    ).json()
    assert len(units["items"]) == 2 and all(
        u["drama_id"] == drama["drama_id"] for u in units["items"]
    )
    assert all(
        u["currency"] == "USD" and Decimal(u["budget"]) == 100 for u in units["items"]
    )
    assert all(u["material_allocation_count"] > 0 for u in units["items"])
    assert (
        client.get(
            f"{base}/units",
            params={
                "drama_id": second["items"][0]["drama_id"],
                "cursor": units["next_cursor"],
            },
            headers=headers(context),
        ).status_code
        == 422
    )
    assert (
        client.get(
            f"/api/tenants/{other_context.tenant_id}/build-previews/{preview}/dramas",
            headers=headers(other_context),
        ).status_code
        == 404
    )


def test_legacy_preview_summary_falls_back_to_group_materials_for_ad_allocations(
    session, client, context, prepared, monkeypatch
):
    original_digest = previews._digest_units

    def digest_without_ad_allocations(db, row, scope):
        # Historical previews lacked these rows; construct that snapshot before
        # freezing so the immutability guard remains active throughout the test.
        db.execute(
            delete(PreviewAdMaterial).where(PreviewAdMaterial.preview_id == row.id)
        )
        return original_digest(db, row, scope)

    monkeypatch.setattr(previews, "_digest_units", digest_without_ad_allocations)
    preview = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    drain(session, context, preview)
    summary = previews.get_preview_summary(session, context=context, preview_id=preview)
    units = previews.get_preview_units(
        session, context=context, preview_id=preview
    ).items
    assert summary.material_allocation_count > 0
    assert units and all(unit.material_allocation_count > 0 for unit in units)
    dramas = client.get(
        f"/api/tenants/{context.tenant_id}/build-previews/{preview}/dramas",
        headers=headers(context),
    )
    assert dramas.status_code == 200
    assert dramas.json()["items"]
    assert all(
        drama["material_allocation_count"] > 0 for drama in dramas.json()["items"]
    )


def test_preview_issue_filter_does_not_page_through_matched_rows(
    session, client, context, prepared
):
    from app.modules.builds.models import DraftInput

    session.add(
        DraftInput(
            tenant_id=context.tenant_id,
            draft_id=prepared,
            kind="account",
            line_no=9,
            raw_text="missing",
            status="not_found",
        )
    )
    session.flush()
    preview = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    drain(session, context, preview)
    base = f"/api/tenants/{context.tenant_id}/build-previews/{preview}/inputs"
    issues = client.get(
        base,
        params={"kind": "account", "issues_only": True, "limit": 1},
        headers=headers(context),
    ).json()
    assert [x["raw_text"] for x in issues["items"]] == ["missing"] and issues[
        "next_cursor"
    ] is None
    page = client.get(
        base, params={"kind": "account", "limit": 1}, headers=headers(context)
    ).json()
    assert (
        client.get(
            base,
            params={
                "kind": "account",
                "issues_only": True,
                "cursor": page["next_cursor"],
            },
            headers=headers(context),
        ).status_code
        == 422
    )


def test_group_mutation_replay_preserves_applied_revision_and_current_groups(
    session, client, context, other_context, prepared
):
    from sqlmodel import select

    from app.modules.builds.models import DraftDrama

    drama = session.exec(
        select(DraftDrama).where(DraftDrama.draft_id == prepared)
    ).first()
    materials = session.exec(
        select(VIDEO_SOURCE.c.material_id)
        .where(
            VIDEO_SOURCE.c.draft_id == prepared,
            VIDEO_SOURCE.c.drama_id == drama.drama_id,
        )
        .limit(2)
    ).all()
    request = uuid4()
    base = f"/api/tenants/{context.tenant_id}"
    url = f"{base}/build-drafts/{prepared}/dramas/{drama.drama_id}/groups"
    body = {
        "expected_revision": 1,
        "request_id": str(request),
        "groups": [[str(materials[0])]],
    }
    first = client.patch(url, json=body, headers=headers(context))
    assert first.status_code == 200 and first.json()["revision"] == 2
    assert (
        client.patch(
            url,
            json={
                "expected_revision": 2,
                "request_id": str(uuid4()),
                "groups": [[str(materials[1])]],
            },
            headers=headers(context),
        ).status_code
        == 200
    )
    assert client.patch(url, json=body, headers=headers(context)).json() == first.json()
    actual = session.exec(
        select(VIDEO_SOURCE.c.material_id).where(
            VIDEO_SOURCE.c.draft_id == prepared,
            VIDEO_SOURCE.c.drama_id == drama.drama_id,
        )
    ).all()
    assert actual == [materials[1]]
    assert (
        client.get(
            f"/api/tenants/{other_context.tenant_id}/build-mutation-requests/{request}",
            headers=headers(other_context),
        ).status_code
        == 404
    )


def test_mutation_history_is_append_only(session, client, context, intent):
    import pytest
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    draft = create_draft(session, context=context, **intent)
    request = uuid4()
    response = client.patch(
        f"/api/tenants/{context.tenant_id}/build-drafts/{draft}",
        json={
            "expected_revision": 1,
            "request_id": str(request),
            "account_lines": ["changed"],
        },
        headers=headers(context),
    )
    assert response.status_code == 200
    with pytest.raises(DBAPIError), session.begin_nested():
        session.execute(
            text(
                "UPDATE draft_mutation_request SET applied_revision=99 WHERE request_id=:id"
            ),
            {"id": request},
        )


def test_concurrent_identical_mutations_apply_once(isolated_strategy_database):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlmodel import Session

    from app.modules.builds import mutations
    from app.modules.builds.models import BuildDraft
    from tests.modules.builds.test_drafts import create_intent

    engine, context, _ = isolated_strategy_database
    with Session(engine) as session:
        draft = create_draft(
            session, context=context, **create_intent(session, context)
        )
        session.commit()
    request = uuid4()
    barrier = Barrier(2)

    def change(_):
        with Session(engine) as session:
            barrier.wait(timeout=5)
            revision = mutations.update_draft(
                session,
                context=context,
                draft_id=draft,
                request_id=request,
                expected_revision=1,
                account_lines=["new"],
            )
            session.commit()
            return revision

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(change, range(2))) == [2, 2]
    with Session(engine) as session:
        assert session.get(BuildDraft, draft).revision == 2
