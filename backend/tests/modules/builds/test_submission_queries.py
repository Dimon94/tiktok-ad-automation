from uuid import uuid4

from app.modules.builds import submission_catalog, submissions
from app.modules.builds.preview_models import BuildPreview, BuildUnit
from app.modules.builds.routes import save_attempt_context
from tests.modules.builds.test_previews import prepared as prepared
from tests.modules.builds.test_submissions import frozen as frozen


def test_catalog_counts_match_exact_summary_before_expansion(session, context, frozen):
    from app.modules.builds.previews import get_preview_summary

    assert (
        get_preview_summary(session, context=context, preview_id=frozen).submission_id
        is None
    )
    receipt = submissions.submit_preview(
        session, context=context, preview_id=frozen, request_id=uuid4()
    )
    preview = session.get(BuildPreview, frozen)
    # 不依赖提交者会话；数据库受理后，历史预览摘要返回同一任务。
    assert (
        get_preview_summary(session, context=context, preview_id=frozen).submission_id
        == receipt.submission_id
    )
    result = submission_catalog.list_submissions(
        session, context=context, bc_id=preview.bc_id
    )
    assert len(result.items) == 1
    view = submissions.get_submission(
        session, context=context, submission_id=receipt.submission_id
    )
    row = result.items[0]
    for field in (
        "submitted",
        "succeeded",
        "failed",
        "unknown",
        "drama_count",
        "account_count",
        "excluded_unit_count",
    ):
        assert getattr(row, field) == getattr(view, field)


def test_metadata_comes_from_frozen_preview_after_draft_edit(session, context, frozen):
    from app.modules.builds.models import BuildDraft
    from app.modules.providers.models import ProviderApplication, ProviderConnection

    receipt = submissions.submit_preview(
        session, context=context, preview_id=frozen, request_id=uuid4()
    )
    original = submission_catalog.metadata(
        session, tenant_id=context.tenant_id, submission_id=receipt.submission_id
    )
    draft = session.get(BuildDraft, session.get(BuildPreview, frozen).draft_id)
    alternate = ProviderConnection(
        tenant_id=context.tenant_id,
        kind="wangyan",
        display_name="Other provider",
        encrypted_credentials="test",
    )
    session.add(alternate)
    session.flush()
    session.add(
        ProviderApplication(
            tenant_id=context.tenant_id,
            connection_id=alternate.id,
            external_id=draft.application_id,
            name="Other app",
        )
    )
    session.flush()
    draft.provider_connection_id = alternate.id
    draft.revision += 1
    session.add(draft)
    session.flush()
    view = submissions.get_submission(
        session, context=context, submission_id=receipt.submission_id
    )
    assert view.provider_name == original.provider_name
    assert view.strategy_label == original.strategy_label
    assert view.actor_name == original.actor_name


def expanded(session, context, frozen):
    receipt = submissions.submit_preview(
        session, context=context, preview_id=frozen, request_id=uuid4()
    )
    while not submissions.expand_submission(
        session, context=context, submission_id=receipt.submission_id
    ):
        pass
    return receipt.submission_id


def test_failed_ancestor_and_known_unknown_counts_match_summary(
    session, context, frozen
):
    from sqlmodel import select

    from app.modules.builds.execution_models import ExecutionStep

    identity = expanded(session, context, frozen)
    campaign = session.exec(
        select(ExecutionStep).where(ExecutionStep.kind == "CAMPAIGN")
    ).first()
    campaign.status = "FAILED"
    session.add(campaign)
    ad = session.exec(
        select(ExecutionStep).where(
            ExecutionStep.kind == "AD", ExecutionStep.unit_id != campaign.unit_id
        )
    ).first()
    ad.status = "UNKNOWN"
    session.add(ad)
    known = session.exec(
        select(ExecutionStep).where(
            ExecutionStep.kind == "AD",
            ExecutionStep.unit_id != campaign.unit_id,
            ExecutionStep.id != ad.id,
        )
    ).first()
    known.status = "UNKNOWN"
    known.remote_id = "actual-remote"
    known.mismatch = True
    session.add(known)
    session.flush()
    view = submissions.get_submission(session, context=context, submission_id=identity)
    listed = submission_catalog.list_submissions(
        session, context=context, bc_id="bc-draft"
    ).items[0]
    for field in ("submitted", "succeeded", "failed", "unknown"):
        assert getattr(listed, field) == getattr(view, field)
    assert listed.succeeded.ad_count == 1 and listed.unknown.ad_count == 1


def seed_headers(session, context, frozen):
    from datetime import UTC, datetime

    from app.modules.builds.execution_models import Submission

    base = session.get(BuildPreview, frozen)
    rows = []
    for index, status in enumerate(
        ["QUEUED", "RUNNING", "PARTIAL", "FAILED", "NEEDS_REVIEW", "COMPLETED"]
    ):
        p = BuildPreview(
            **{
                **base.model_dump(),
                "id": uuid4(),
                "batch_short_id": f"page_{index}_%literal",
                "draft_revision": index + 2,
            }
        )
        session.add(p)
        session.flush()
        row = Submission(
            tenant_id=context.tenant_id,
            bc_id=base.bc_id,
            preview_id=p.id,
            draft_id=base.draft_id,
            actor_id=context.actor_id,
            ordinal=index + 1,
            status=status,
            created_at=datetime(2026, 9, 9, 0, 0, tzinfo=UTC),
        )
        session.add(row)
        rows.append(row)
    session.flush()
    return rows


def test_list_stable_tie_pagination_literal_query_and_status(session, context, frozen):
    import pytest

    from app.core.errors import DomainError

    rows = seed_headers(session, context, frozen)
    first = submission_catalog.list_submissions(
        session, context=context, bc_id="bc-draft", limit=2
    )
    second = submission_catalog.list_submissions(
        session, context=context, bc_id="bc-draft", limit=2, cursor=first.next_cursor
    )
    third = submission_catalog.list_submissions(
        session, context=context, bc_id="bc-draft", limit=2, cursor=second.next_cursor
    )
    assert first.total == second.total == third.total == len(rows)
    assert [
        x.submission_id for x in first.items + second.items + third.items
    ] == sorted([r.id for r in rows], reverse=True)
    assert third.next_cursor is None
    assert (
        len(
            submission_catalog.list_submissions(
                session, context=context, bc_id="bc-draft", status_group="attention"
            ).items
        )
        == 3
    )
    assert (
        len(
            submission_catalog.list_submissions(
                session, context=context, bc_id="bc-draft", status_group="active"
            ).items
        )
        == 2
    )
    assert (
        len(
            submission_catalog.list_submissions(
                session, context=context, bc_id="bc-draft", status_group="completed"
            ).items
        )
        == 1
    )
    assert (
        len(
            submission_catalog.list_submissions(
                session, context=context, bc_id="bc-draft", q="%literal"
            ).items
        )
        == 6
    )
    assert not submission_catalog.list_submissions(
        session, context=context, bc_id="bc-draft", q="page_9_%"
    ).items
    with pytest.raises(DomainError, match="游标"):
        submission_catalog.list_submissions(
            session,
            context=context,
            bc_id="bc-draft",
            status_group="attention",
            cursor=first.next_cursor,
            limit=2,
        )


def test_date_half_open_and_bc_tenant_boundaries(
    session, context, other_context, frozen
):
    from datetime import datetime, timedelta

    import pytest

    from app.core.errors import DomainError
    from app.modules.accounts.models import TenantBC

    rows = seed_headers(session, context, frozen)
    stamp = rows[0].created_at
    assert not submission_catalog.list_submissions(
        session, context=context, bc_id="bc-draft", created_to=stamp
    ).items
    assert (
        len(
            submission_catalog.list_submissions(
                session,
                context=context,
                bc_id="bc-draft",
                created_from=stamp,
                created_to=stamp + timedelta(days=1),
            ).items
        )
        == 6
    )
    with pytest.raises(DomainError):
        submission_catalog.list_submissions(
            session,
            context=context,
            bc_id="bc-draft",
            created_from=datetime(2026, 9, 9),
        )
    session.add(TenantBC(tenant_id=context.tenant_id, bc_id="other-bc"))
    session.flush()
    assert not submission_catalog.list_submissions(
        session, context=context, bc_id="other-bc"
    ).items
    with pytest.raises(DomainError):
        submission_catalog.list_submissions(
            session, context=other_context, bc_id="bc-draft"
        )


def test_search_accounts_are_exact_and_provider_is_frozen(session, context, frozen):
    from sqlmodel import select

    from app.modules.builds.preview_models import PreviewDrama
    from app.modules.providers.models import PromotionLink

    identity = submissions.submit_preview(
        session, context=context, preview_id=frozen, request_id=uuid4()
    ).submission_id
    drama = session.exec(
        select(PreviewDrama).where(PreviewDrama.preview_id == frozen)
    ).first()
    connection = session.get(PromotionLink, drama.link_id).connection_id
    assert (
        submission_catalog.list_submissions(
            session, context=context, bc_id="bc-draft", q="A"
        )
        .items[0]
        .submission_id
        == identity
    )
    assert not submission_catalog.list_submissions(
        session, context=context, bc_id="bc-draft", q="account-id-no-match"
    ).items
    assert (
        len(
            submission_catalog.list_submissions(
                session,
                context=context,
                bc_id="bc-draft",
                provider_connection_id=connection,
            ).items
        )
        == 1
    )
    assert not submission_catalog.list_submissions(
        session, context=context, bc_id="bc-draft", provider_connection_id=uuid4()
    ).items


def test_groups_ads_page_scope_and_events_are_whitelisted(
    session, context, other_context, frozen
):
    from datetime import UTC, datetime, timedelta

    import pytest
    from sqlmodel import select

    from app.core.errors import DomainError
    from app.modules.builds.execution_models import ExecutionStep, StepEvidence

    identity = expanded(session, context, frozen)
    units = submissions.get_submission_units(
        session, context=context, submission_id=identity, limit=1
    )
    unit = units.items[0]
    assert (
        unit.account_name
        and unit.group_count == 3
        and unit.ad_count == 6
        and unit.material_count == 23
    )
    groups = submission_catalog.get_submission_groups(
        session, context=context, submission_id=identity, unit_id=unit.unit_id, limit=1
    )
    group = groups.items[0]
    assert group.ad_count == 2 and group.step.kind == "ADGROUP"
    ads = submission_catalog.get_submission_ads(
        session,
        context=context,
        submission_id=identity,
        unit_id=unit.unit_id,
        group_id=group.group_id,
        limit=1,
    )
    assert ads.next_cursor and ads.items[0].step.kind == "AD"
    next_ads = submission_catalog.get_submission_ads(
        session,
        context=context,
        submission_id=identity,
        unit_id=unit.unit_id,
        group_id=group.group_id,
        limit=1,
        cursor=ads.next_cursor,
    )
    assert next_ads.items[0].planned_ad_id != ads.items[0].planned_ad_id
    with pytest.raises(DomainError):
        submission_catalog.get_submission_ads(
            session,
            context=context,
            submission_id=identity,
            unit_id=uuid4(),
            group_id=group.group_id,
        )
    with pytest.raises(DomainError):
        submission_catalog.get_submission_groups(
            session, context=other_context, submission_id=identity, unit_id=unit.unit_id
        )
    step = session.exec(select(ExecutionStep).where(ExecutionStep.kind == "AD")).first()
    for i in range(3):
        save_attempt_context(session, step=step, attempt=i)
        session.add(
            StepEvidence(
                tenant_id=context.tenant_id,
                submission_id=identity,
                step_id=step.id,
                attempt=i,
                conclusion="UNKNOWN",
                request_id="platform-request",
                summary={
                    "token": "never-public",
                    "raw_body": "never-public",
                    "remote_code": 40002,
                    "remote_message": "Invalid targeting_spec.languages. Bearer never-public",
                },
                observed_at=datetime(2026, 9, 9, tzinfo=UTC) + timedelta(seconds=i),
            )
        )
    session.flush()
    events = submission_catalog.get_submission_events(
        session, context=context, submission_id=identity, limit=2
    )
    assert [x.attempt for x in events.items] == [2, 1]
    event_unit = session.get(BuildUnit, step.unit_id)
    assert events.items[0].advertiser_id == event_unit.advertiser_id
    assert events.items[0].title and events.items[0].account_name
    assert events.items[0].remote_code == 40002
    assert events.items[0].remote_message.startswith(
        "Invalid targeting_spec.languages."
    )
    assert events.items[0].request_id == "platform-request"
    tail = submission_catalog.get_submission_events(
        session,
        context=context,
        submission_id=identity,
        limit=2,
        cursor=events.next_cursor,
    )
    assert tail.items[0].attempt == 0
    assert (
        "secret" not in events.model_dump_json()
        and "never-public" not in events.model_dump_json()
    )
    assert set(events.items[0].model_dump()) == {
        "evidence_id",
        "step_id",
        "attempt",
        "conclusion",
        "observed_at",
        "unit_id",
        "kind",
        "title",
        "advertiser_id",
        "account_name",
        "remote_code",
        "remote_message",
        "request_id",
    }
    detail = submissions.get_submission_steps(
        session, context=context, submission_id=identity, kind="AD", limit=1
    ).items[0]
    assert (
        detail.title and detail.advertiser_id and detail.group_no and detail.creative_no
    )


def test_dependency_failure_events_include_only_the_failed_parent_reason(
    session, context, other_context, frozen
):
    import pytest
    from sqlmodel import select

    from app.core.errors import DomainError
    from app.modules.builds.execution_models import ExecutionStep, StepEvidence

    identity = expanded(session, context, frozen)
    child = session.exec(
        select(ExecutionStep)
        .where(ExecutionStep.submission_id == identity, ExecutionStep.kind == "AD")
        .limit(1)
    ).one()
    parent = session.get(ExecutionStep, child.parent_step_id)
    child.status, child.phase, child.error_code = "FAILED", "DONE", "dependency_failed"
    parent.status, parent.phase, parent.error_code = (
        "FAILED",
        "DONE",
        "tiktok_business_error",
    )
    parent.attempt = 1
    save_attempt_context(session, step=parent)
    # 同任务中已有系列记录，查看广告依赖错误时不能混入无关层级。
    campaign = session.get(ExecutionStep, parent.parent_step_id)
    campaign.attempt = 1
    save_attempt_context(session, step=campaign)
    session.add(
        StepEvidence(
            tenant_id=context.tenant_id,
            submission_id=identity,
            step_id=campaign.id,
            attempt=1,
            conclusion="CREATED",
            summary={},
        )
    )
    session.add(
        StepEvidence(
            tenant_id=context.tenant_id,
            submission_id=identity,
            step_id=parent.id,
            attempt=1,
            conclusion="REMOTE_REJECTED",
            request_id="platform-parent-request",
            summary={
                "remote_code": 40002,
                "remote_message": "Invalid targeting_optimization_mode",
            },
        )
    )
    session.flush()
    page = submission_catalog.get_submission_events(
        session, context=context, submission_id=identity, step_id=child.id, limit=1
    )
    assert page.total == 1
    assert page.items[0].step_id == parent.id and page.items[0].kind == "ADGROUP"
    assert page.items[0].remote_message == "Invalid targeting_optimization_mode"
    assert page.items[0].request_id == "platform-parent-request"
    with pytest.raises(DomainError):
        submission_catalog.get_submission_events(
            session, context=other_context, submission_id=identity, step_id=child.id
        )


def test_catalog_http_contract_and_invalid_filters(session, context, frozen):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.deps import get_db
    from app.core.errors import DomainError, domain_error_handler
    from app.modules.builds.submission_catalog_api import router
    from tests.modules.strategies.test_api import headers

    submission = expanded(session, context, frozen)
    app = FastAPI()
    app.add_exception_handler(DomainError, domain_error_handler)
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as client:
        base = f"/api/tenants/{context.tenant_id}/submissions"
        result = client.get(
            base, params={"bc_id": "bc-draft", "limit": 50}, headers=headers(context)
        )
        assert result.status_code == 200
        assert result.json()["items"][0]["submission_id"] == str(submission)
        assert (
            client.get(
                base,
                params={"bc_id": "bc-draft", "created_from": "2026-09-09T00:00:00"},
                headers=headers(context),
            ).status_code
            == 422
        )
        assert (
            client.get(
                base,
                params={"bc_id": "bc-draft", "limit": 101},
                headers=headers(context),
            ).status_code
            == 422
        )
        assert (
            client.get(
                base,
                params={"bc_id": "bc-draft", "status_group": "fake"},
                headers=headers(context),
            ).status_code
            == 422
        )
        assert client.get(base, params={"bc_id": "bc-draft"}).status_code == 401
        unit = submissions.get_submission_units(
            session, context=context, submission_id=submission, limit=1
        ).items[0]
        groups = client.get(
            f"{base}/{submission}/units/{unit.unit_id}/groups", headers=headers(context)
        )
        assert groups.status_code == 200 and groups.json()["items"][0]["ad_count"] == 2
        group = groups.json()["items"][0]["group_id"]
        ads = client.get(
            f"{base}/{submission}/units/{unit.unit_id}/groups/{group}/ads",
            headers=headers(context),
        )
        assert ads.status_code == 200 and len(ads.json()["items"]) == 2
        assert (
            client.get(
                f"{base}/{submission}/events", headers=headers(context)
            ).status_code
            == 200
        )


def test_list_uses_one_page_aggregate_not_per_task_queries(session, context, frozen):
    from sqlalchemy import event

    seed_headers(session, context, frozen)
    statements = []

    def observe(
        _connection, _cursor, statement, _parameters, _execution_context, _executemany
    ):
        statements.append(statement)

    connection = session.connection()
    event.listen(connection, "before_cursor_execute", observe)
    try:
        submission_catalog.list_submissions(
            session, context=context, bc_id="bc-draft", limit=1
        )
        one = len(statements)
        statements.clear()
        submission_catalog.list_submissions(
            session, context=context, bc_id="bc-draft", limit=50
        )
        assert len(statements) == one
        assert len([s for s in statements if "result_counts AS" in s]) == 1
    finally:
        event.remove(connection, "before_cursor_execute", observe)


def test_group_materials_page_returns_only_actual_target_mapping(
    session, context, frozen
):
    from sqlmodel import select

    from app.modules.builds.execution_models import ExecutionStep

    identity = expanded(session, context, frozen)
    unit = submissions.get_submission_units(
        session, context=context, submission_id=identity, limit=1
    ).items[0]
    group = submission_catalog.get_submission_groups(
        session, context=context, submission_id=identity, unit_id=unit.unit_id, limit=1
    ).items[0]
    first = submission_catalog.get_submission_materials(
        session,
        context=context,
        submission_id=identity,
        unit_id=unit.unit_id,
        group_id=group.group_id,
        limit=1,
    )
    assert (
        len(first.items) == 1 and first.next_cursor and first.items[0].video_id is None
    )
    item = first.items[0]
    step = session.exec(
        select(ExecutionStep).where(
            ExecutionStep.unit_id == unit.unit_id,
            ExecutionStep.material_id == item.material_id,
            ExecutionStep.kind == "MATERIAL",
        )
    ).first()
    step.status = "SUCCEEDED"
    step.resolved = {
        "mapping": {
            "video_id": "actual-target-video",
            "image_id": "actual-target-cover",
            "secret": "never-public",
        }
    }
    session.add(step)
    session.flush()
    reread = submission_catalog.get_submission_materials(
        session,
        context=context,
        submission_id=identity,
        unit_id=unit.unit_id,
        group_id=group.group_id,
        limit=1,
    )
    assert (
        reread.items[0].video_id == "actual-target-video"
        and reread.items[0].image_id == "actual-target-cover"
    )
    assert "never-public" not in reread.model_dump_json()
    tail = submission_catalog.get_submission_materials(
        session,
        context=context,
        submission_id=identity,
        unit_id=unit.unit_id,
        group_id=group.group_id,
        limit=1,
        cursor=first.next_cursor,
    )
    assert (
        tail.items[0].position > item.position
        and tail.items[0].material_id != item.material_id
    )


def test_unit_result_includes_unknown_ad_after_campaign_success(
    session, context, frozen
):
    from sqlmodel import select

    from app.modules.builds.execution_models import ExecutionStep

    identity = expanded(session, context, frozen)
    campaign = session.exec(
        select(ExecutionStep).where(ExecutionStep.kind == "CAMPAIGN")
    ).first()
    campaign.status = "SUCCEEDED"
    campaign.remote_id = "known-campaign"
    session.add(campaign)
    ad = session.exec(
        select(ExecutionStep).where(
            ExecutionStep.kind == "AD", ExecutionStep.unit_id == campaign.unit_id
        )
    ).first()
    ad.status = "UNKNOWN"
    session.add(ad)
    session.flush()
    units = submissions.get_submission_units(
        session, context=context, submission_id=identity
    )
    unit = next(row for row in units.items if row.unit_id == campaign.unit_id)
    assert unit.result_status == "NEEDS_REVIEW"
    assert unit.campaign_step.remote_id == "known-campaign"


def test_unit_success_without_remote_id_is_not_completed(session, context, frozen):
    from sqlmodel import select

    from app.modules.builds.execution_models import ExecutionStep

    identity = expanded(session, context, frozen)
    ad = session.exec(select(ExecutionStep).where(ExecutionStep.kind == "AD")).first()
    ad.status = "SUCCEEDED"
    session.add(ad)
    session.flush()
    rows = submissions.get_submission_units(
        session, context=context, submission_id=identity
    ).items
    assert (
        next(u for u in rows if u.unit_id == ad.unit_id).result_status == "NEEDS_REVIEW"
    )
