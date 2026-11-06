"""失败后修改配置的真实冻结/提交/展开边界；没有外部广告写入。"""

import hashlib
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlmodel import select

from app.modules.builds import previews, submissions
from app.modules.builds.execution import prepare_request
from app.modules.builds.execution_models import ExecutionStep, StepEvidence, Submission
from app.modules.builds.models import BuildDraft
from app.modules.builds.preview_models import BuildUnit
from app.modules.builds.routes import save_attempt_context
from tests.modules.builds.test_previews import drain
from tests.modules.builds.test_previews import prepared as prepared
from tests.modules.strategies.test_api import headers


def expand(session, context, identity):
    for _ in range(100):
        if submissions.expand_submission(
            session, context=context, submission_id=identity
        ):
            return
    raise AssertionError("expansion did not complete")


@pytest.fixture
def partial(session, context, prepared, monkeypatch, request):
    from dataclasses import replace

    if getattr(request, "param", "VIDEO") == "CAROUSEL":
        from app.modules.builds.drafts import create_draft, prepare_draft
        from app.modules.builds.image_composition_models import DraftCreativeUnit
        from app.modules.builds.image_composition_service import (
            adopt_candidates,
            refresh_candidates,
            resolve_image_scope,
        )
        from app.modules.builds.models import DraftDrama
        from tests.modules.builds.test_drafts import finish, ready_links
        from tests.modules.builds.test_image_composition_service import (
            apply_rule,
            finish_candidate_snapshot,
        )
        from tests.modules.materials.test_tenant_materials import material

        source = session.get(BuildDraft, prepared)
        from app.modules.providers.models import ProviderApplication

        application = ProviderApplication(
            tenant_id=context.tenant_id,
            connection_id=source.provider_connection_id,
            external_id="carousel-app",
            name="Carousel fixture",
        )
        session.add(application)
        session.flush()
        intent = {
            "bc_id": source.bc_id,
            "strategy_version_id": source.strategy_version_id,
            "provider_connection_id": source.provider_connection_id,
            "application_id": application.external_id,
            "drama_lines": ["Moon", "Short Drama"],
            "account_lines": ["A", "B", "C"],
            "link_config": source.link_config,
            "media_kind": "CAROUSEL",
        }
        prepared = create_draft(session, context=context, request_id=uuid4(), **intent)
        task = prepare_draft(
            session, context=context, draft_id=prepared, request_id=uuid4()
        )
        ready_links(session, context, task, intent)
        finish(session, context, task)
        draft = session.get(BuildDraft, prepared)
        dramas = session.exec(
            select(DraftDrama).where(DraftDrama.draft_id == draft.id)
        ).all()
        for drama in dramas:
            for index in range(7):
                row = material(session, context, f"{drama.title}_{index}.jpg")
                row.media_type, row.mime_type, row.sha256 = (
                    "IMAGE",
                    "image/jpeg",
                    f"{index:064x}",
                )
        session.flush()
        scope = resolve_image_scope(session, context=context, draft_id=draft.id)
        snapshot = finish_candidate_snapshot(
            session, scope, refresh_candidates(session, scope, uuid4(), draft.revision)
        )
        adopt_candidates(
            session, scope, uuid4(), draft.revision, snapshot["snapshot_id"]
        )
        from app.modules.builds.carousel_music_models import MusicPreparationRun
        from app.modules.builds.carousel_music_tasks import advance_music_preparation
        from app.modules.builds.image_composition_schemas import ImageRule

        apply_rule(
            session,
            (draft, dramas[0], scope),
            ImageRule(
                music_mode="FIXED",
                fixed_music_id="fixture-song",
                single_music_id="fixture-song",
            ),
        )
        for run in session.exec(
            select(MusicPreparationRun).where(
                MusicPreparationRun.draft_id == draft.id,
                MusicPreparationRun.status == "PENDING",
            )
        ).all():
            advance_music_preparation(session, run.id, run.generation)
        for unit in session.exec(
            select(DraftCreativeUnit).where(DraftCreativeUnit.draft_id == draft.id)
        ).all():
            unit.music_id, unit.music_status = "fixture-song", "READY"
        draft.status = "READY"
        from app.modules.builds import carousel_readiness

        # 与视频夹具相同，只替换资源可用性边界；预览/组合/冻结/提交均运行真实代码。
        monkeypatch.setattr(
            carousel_readiness,
            "image_preview_readiness",
            lambda *a, **k: {
                i: SimpleNamespace(state="preparable", reason_code=None)
                for i in k["material_ids"]
            },
        )
        monkeypatch.setattr(
            previews,
            "draft_directory",
            lambda *a, **k: SimpleNamespace(
                state="READY", unavailable_region_codes=[], region_codes=["US"]
            ),
        )
        from app.modules.builds import mini_selection

        monkeypatch.setattr(
            mini_selection, "require_preview_mini", lambda *a, **k: "fixture-mini"
        )
    original_scene = previews.read_scene_context

    def complete_scene(*args, **kwargs):
        scene = original_scene(*args, **kwargs)
        return replace(
            scene,
            media_kind=session.get(BuildDraft, prepared).media_kind,
            adgroup_fields={**scene.adgroup_fields, "minis_id": "fixture-mini"},
            cta_fields={
                **scene.cta_fields,
                "recommend_assets": [
                    {"asset_ids": ["cta-1"], "asset_content": "Watch more"}
                ],
            },
        )

    monkeypatch.setattr(previews, "read_scene_context", complete_scene)
    first = previews.generate_preview(
        session,
        context=context,
        draft_id=prepared,
        expected_revision=session.get(BuildDraft, prepared).revision,
    )
    drain(session, context, first)
    frozen_units = session.exec(
        select(BuildUnit).where(BuildUnit.preview_id == first)
    ).all()
    assert any(u.readiness != "BLOCKED" for u in frozen_units), [
        (u.readiness, u.reason_codes) for u in frozen_units
    ]
    receipt = submissions.submit_preview(
        session, context=context, preview_id=first, request_id=uuid4()
    )
    expand(session, context, receipt.submission_id)
    steps = session.exec(
        select(ExecutionStep).where(
            ExecutionStep.submission_id == receipt.submission_id
        )
    ).all()
    for step in sorted(steps, key=lambda s: (s.kind != "CAMPAIGN", str(s.id))):
        if step.kind not in {"CTA", "CAMPAIGN", "ADGROUP", "AD"}:
            step.status, step.phase = "SUCCEEDED", "DONE"
            continue
        frozen = previews.load_frozen_unit(
            session, context=context, unit_id=step.unit_id
        )
        if step.kind in {"CAMPAIGN", "ADGROUP"}:
            step.request_body = prepare_request(
                session,
                context=context,
                claim=SimpleNamespace(step_id=step.id),
                frozen=frozen,
            )
        elif step.kind == "CTA":
            step.request_body = {
                "advertiser_id": frozen.advertiser_id,
                "creative_portfolio_type": "CTA",
                "portfolio_content": [
                    {"asset_ids": ["cta-1"], "asset_content": "Watch more"}
                ],
            }
        step.status, step.phase = "FAILED", "DONE"
        if step.kind == "AD":
            step.error_code = "dependency_failed"
            continue
        step.attempt = 1
        attempt_id = save_attempt_context(session, step=step)
        if step.kind in {"CAMPAIGN", "ADGROUP"}:
            from app.modules.builds.request_compiler import remote_request_id

            step.request_body = {
                **step.request_body,
                "request_id": remote_request_id(attempt_id),
            }
        step.request_body_digest = hashlib.sha256(
            json.dumps(
                step.request_body, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        if step.kind in {"CTA", "CAMPAIGN"}:
            step.status = "SUCCEEDED"
            step.remote_id = f"old-{step.kind}-{step.id}"
            step.operation_status = "ENABLE"
            conclusion = "CREATED"
            summary = {"remote_id": step.remote_id, "operation_status": "ENABLE"}
        else:
            step.error_code = "tiktok_business_error"
            conclusion = "REMOTE_REJECTED"
            summary = {
                "remote_code": 40002,
                "body_digest": step.request_body_digest,
                "rejection_count": 2,
            }
        nonce = uuid4()
        if step.kind == "ADGROUP":
            from datetime import UTC, datetime, timedelta

            session.add(
                StepEvidence(
                    tenant_id=context.tenant_id,
                    submission_id=receipt.submission_id,
                    step_id=step.id,
                    attempt=1,
                    lease_token=nonce,
                    conclusion="REQUEST_ARMED",
                    summary={
                        "attempt_id": str(attempt_id),
                        "body_digest": step.request_body_digest,
                    },
                    observed_at=datetime.now(UTC) - timedelta(seconds=1),
                )
            )
        session.add(
            StepEvidence(
                tenant_id=context.tenant_id,
                submission_id=receipt.submission_id,
                step_id=step.id,
                attempt=1,
                lease_token=nonce,
                conclusion=conclusion,
                request_id=f"receipt-{step.id}",
                summary={**summary, "attempt_id": str(attempt_id)},
            )
        )
    original = session.get(Submission, receipt.submission_id)
    original.status = "PARTIAL"
    session.flush()
    return prepared, receipt.submission_id, steps


def revised(session, context, draft_id):
    draft = session.get(BuildDraft, draft_id)
    draft.targeting_override = {
        "region_mode": "SELECTED",
        "region_codes": ["US"],
        "languages": ["en"],
    }
    draft.revision += 1
    session.flush()
    identity = previews.generate_preview(
        session, context=context, draft_id=draft.id, expected_revision=draft.revision
    )
    drain(session, context, identity)
    return identity


def test_changed_targeting_continues_with_existing_campaign(
    client, session, context, partial
):
    draft_id, original_id, original_steps = partial
    before = {
        step.id: (step.status, step.remote_id, step.request_body_digest)
        for step in original_steps
    }
    preview_id = revised(session, context, draft_id)
    response = client.post(
        f"/api/tenants/{context.tenant_id}/build-previews/{preview_id}/submit",
        json={"request_id": str(uuid4())},
        headers=headers(context),
    )
    assert response.status_code == 202, response.json()
    new_id = UUID(response.json()["submission_id"])
    assert new_id != original_id
    expand(session, context, new_id)
    campaigns = session.exec(
        select(ExecutionStep).where(
            ExecutionStep.submission_id == new_id, ExecutionStep.kind == "CAMPAIGN"
        )
    ).all()
    originals = {s.unit_id: s for s in original_steps if s.kind == "CAMPAIGN"}
    assert len(campaigns) == 6
    assert all(
        s.status == "SUCCEEDED"
        and s.remote_id in {o.remote_id for o in originals.values()}
        for s in campaigns
    )
    groups = session.exec(
        select(ExecutionStep).where(
            ExecutionStep.submission_id == new_id, ExecutionStep.kind == "ADGROUP"
        )
    ).all()
    assert groups and all(
        s.status == "PENDING" and s.request_body is None for s in groups
    )
    assert session.get(Submission, original_id).status == "PARTIAL"
    assert all(
        (s.status, s.remote_id, s.request_body_digest) == before[s.id]
        for s in original_steps
    )
    summary = previews.get_preview_summary(
        session, context=context, preview_id=preview_id
    )
    assert summary.reused_campaign_count == 6
    assert summary.new_campaign_count == 0


def test_unknown_source_stays_blocked(session, context, partial):
    draft_id, _, steps = partial
    for step in steps:
        if step.kind == "ADGROUP":
            step.status, step.error_code = "UNKNOWN", "create_result_unknown"
    session.flush()
    preview_id = revised(session, context, draft_id)
    units = session.exec(
        select(BuildUnit).where(BuildUnit.preview_id == preview_id)
    ).all()
    assert units and all(
        u.readiness == "BLOCKED" and "continuation_pending" in u.reason_codes
        for u in units
    )


def test_repeated_preview_returns_same_task_and_old_retry_is_fenced(
    session, context, partial
):
    from app.core.errors import DomainError
    from app.modules.builds.execution_state import arm_request
    from app.modules.builds.recovery import recovery_summary

    draft_id, original_id, old_steps = partial
    identity = revised(session, context, draft_id)
    receipt = submissions.submit_preview(
        session, context=context, preview_id=identity, request_id=uuid4()
    )
    again = submissions.submit_preview(
        session, context=context, preview_id=identity, request_id=uuid4()
    )
    assert again.submission_id == receipt.submission_id
    assert not recovery_summary(
        session, context=context, submission=session.get(Submission, original_id)
    ).can_retry
    old = next(s for s in old_steps if s.kind == "ADGROUP")
    claim = SimpleNamespace(
        step_id=old.id,
        tenant_id=old.tenant_id,
        submission_id=old.submission_id,
        unit_id=old.unit_id,
        preview_id=old.preview_id,
        kind=old.kind,
        actor_id=context.actor_id,
    )
    with pytest.raises(DomainError) as error:
        arm_request(session, context=context, claim=claim, body=old.request_body)
    assert error.value.code == "execution_superseded"
    expand(session, context, receipt.submission_id)
    new_view = submissions.get_submission(
        session, context=context, submission_id=receipt.submission_id
    )
    old_view = submissions.get_submission(
        session, context=context, submission_id=original_id
    )
    assert new_view.reused.campaign_count == 6
    assert new_view.continuation_source_ids == [original_id]
    assert old_view.continued_by_submission_ids == [receipt.submission_id]


@pytest.mark.parametrize("after_submit", [False, True])
def test_late_source_evidence_requires_fresh_preview(
    session, context, partial, after_submit
):
    from app.core.errors import DomainError
    from app.modules.builds.continuation import validate_execution_source

    draft_id, _, old_steps = partial
    identity = revised(session, context, draft_id)
    if after_submit:
        receipt = submissions.submit_preview(
            session, context=context, preview_id=identity, request_id=uuid4()
        )
        expand(session, context, receipt.submission_id)
        child = session.exec(
            select(ExecutionStep).where(
                ExecutionStep.submission_id == receipt.submission_id,
                ExecutionStep.kind == "ADGROUP",
            )
        ).first()
    source_unit_id = (
        session.get(BuildUnit, child.unit_id).scene_snapshot["continuation"][
            "source_unit_id"
        ]
        if after_submit
        else None
    )
    old = next(
        s
        for s in old_steps
        if s.kind == "ADGROUP"
        and (source_unit_id is None or str(s.unit_id) == source_unit_id)
    )
    session.add(
        StepEvidence(
            tenant_id=old.tenant_id,
            submission_id=old.submission_id,
            step_id=old.id,
            attempt=1,
            conclusion="RESULT_UNKNOWN",
            summary={},
        )
    )
    session.flush()
    with pytest.raises(DomainError) as error:
        if after_submit:
            validate_execution_source(session, step=child)
        else:
            submissions.submit_preview(
                session, context=context, preview_id=identity, request_id=uuid4()
            )
    assert error.value.code == "continuation_changed"
    assert len(
        session.exec(select(Submission).where(Submission.draft_id == draft_id)).all()
    ) == (2 if after_submit else 1)


@pytest.mark.parametrize("earlier_unknown", [False, True])
def test_every_historical_nonce_must_have_no_effect_proof(
    session, context, partial, earlier_unknown
):
    from datetime import UTC, datetime, timedelta

    draft_id, _, steps = partial
    target = next(s for s in steps if s.kind == "ADGROUP")
    target.attempt = 2
    attempt_id = save_attempt_context(session, step=target)
    nonce = uuid4()
    for offset, conclusion in enumerate(("REQUEST_ARMED", "REMOTE_REJECTED")):
        session.add(
            StepEvidence(
                tenant_id=target.tenant_id,
                submission_id=target.submission_id,
                step_id=target.id,
                attempt=2,
                lease_token=nonce,
                conclusion=conclusion,
                summary={
                    "body_digest": target.request_body_digest,
                    "attempt_id": str(attempt_id),
                    "remote_code": 40002,
                },
                observed_at=datetime.now(UTC) + timedelta(seconds=offset),
            )
        )
    if earlier_unknown:
        session.add(
            StepEvidence(
                tenant_id=target.tenant_id,
                submission_id=target.submission_id,
                step_id=target.id,
                attempt=1,
                conclusion="RESULT_UNKNOWN",
                summary={},
            )
        )
    session.flush()
    identity = revised(session, context, draft_id)
    unit = session.exec(
        select(BuildUnit).where(
            BuildUnit.preview_id == identity,
            BuildUnit.drama_id == session.get(BuildUnit, target.unit_id).drama_id,
            BuildUnit.advertiser_id
            == session.get(BuildUnit, target.unit_id).advertiser_id,
        )
    ).one()
    assert unit.scene_snapshot["continuation"]["state"] == (
        "BLOCKED" if earlier_unknown else "CONTINUE"
    )


def test_new_campaign_budget_does_not_reuse_old_campaign(session, context, partial):
    from app.modules.strategies.models import StrategyVersion
    from app.modules.strategies.service import append_version

    draft_id, _, _ = partial
    draft = session.get(BuildDraft, draft_id)
    version = session.get(StrategyVersion, draft.strategy_version_id)
    from app.modules.strategies.schemas import StrategyConfig

    draft.strategy_version_id = append_version(
        session,
        context=context,
        strategy_id=version.strategy_id,
        config=StrategyConfig.model_validate({**version.config, "budget": "600"}),
    )
    identity = revised(session, context, draft_id)
    summary = previews.get_preview_summary(
        session, context=context, preview_id=identity
    )
    assert summary.reused_campaign_count == 0
    assert summary.new_campaign_count == 6
    assert summary.new_adgroup_count == summary.adgroup_count > 0


def test_refreshes_frozen_continuation_without_changing_old_preview(
    session, context, partial
):
    from app.modules.builds.preview_models import BuildPreview

    draft_id, _, steps = partial
    for step in steps:
        if step.kind == "ADGROUP":
            step.status = "UNKNOWN"
    session.flush()
    old_preview = revised(session, context, draft_id)
    frozen = session.get(BuildPreview, old_preview)
    old_digest, old_revision = frozen.content_digest, frozen.draft_revision
    for step in steps:
        if step.kind == "ADGROUP":
            step.status = "FAILED"
    session.flush()
    identity = previews.generate_preview(
        session,
        context=context,
        draft_id=draft_id,
        expected_revision=old_revision,
        refresh_continuation=True,
    )
    assert identity != old_preview
    assert (
        previews.generate_preview(
            session,
            context=context,
            draft_id=draft_id,
            expected_revision=old_revision,
            refresh_continuation=True,
        )
        == identity
    )
    drain(session, context, identity)
    assert session.get(BuildPreview, old_preview).content_digest == old_digest
    assert (
        previews.get_preview_summary(
            session, context=context, preview_id=identity
        ).continuation_unit_count
        == 6
    )


@pytest.mark.parametrize("partial", ["CAROUSEL"], indirect=True)
def test_carousel_revised_preview_reuses_campaign_and_preserves_image_rules(
    session, context, partial
):
    draft_id, _, _ = partial
    draft = session.get(BuildDraft, draft_id)
    # 正常定向调整入口会同步已应用规则版本；续建刷新也必须维持这一合同。
    draft.targeting_override = {
        "region_mode": "SELECTED",
        "region_codes": ["US"],
        "languages": ["en"],
    }
    draft.revision += 1
    draft.applied_image_rule_revision = draft.revision
    session.flush()
    identity = previews.generate_preview(
        session, context=context, draft_id=draft_id, expected_revision=draft.revision
    )
    drain(session, context, identity)
    summary = previews.get_preview_summary(
        session, context=context, preview_id=identity
    )
    assert summary.media_kind == "CAROUSEL"
    assert summary.reused_campaign_count == 6
    assert summary.new_campaign_count == 0
    before = draft.revision
    refreshed = previews.generate_preview(
        session,
        context=context,
        draft_id=draft_id,
        expected_revision=before,
        refresh_continuation=True,
    )
    assert draft.applied_image_rule_revision == draft.revision == before + 1
    drain(session, context, refreshed)
    receipt = submissions.submit_preview(
        session, context=context, preview_id=refreshed, request_id=uuid4()
    )
    expand(session, context, receipt.submission_id)
    assert (
        submissions.get_submission(
            session, context=context, submission_id=receipt.submission_id
        ).reused.campaign_count
        == 6
    )


@pytest.mark.parametrize("acceptance_first", [False, True])
def test_continuation_acceptance_and_old_send_share_source_locks(
    isolated_strategy_database, monkeypatch, acceptance_first
):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from datetime import UTC, datetime, timedelta
    from threading import Event

    from sqlmodel import Session, col

    from app.core.errors import DomainError
    from app.modules.builds.execution_state import arm_request
    from tests.modules.builds.test_drafts import create_intent

    engine, context, _ = isolated_strategy_database
    with Session(engine) as db:
        draft_id = prepared.__wrapped__(
            db, context, create_intent(db, context), monkeypatch
        )
        _, _, old_steps = partial.__wrapped__(
            db, context, draft_id, monkeypatch, SimpleNamespace(param="VIDEO")
        )
        preview_id = revised(db, context, draft_id)
        old = next(s for s in old_steps if s.kind == "ADGROUP")
        claim = SimpleNamespace(
            step_id=old.id,
            tenant_id=old.tenant_id,
            submission_id=old.submission_id,
            unit_id=old.unit_id,
            preview_id=old.preview_id,
            kind=old.kind,
            actor_id=context.actor_id,
        )
        old_id, body = old.id, old.request_body
        db.commit()
    started = Event()

    def contender():
        with Session(engine) as db:
            started.set()
            try:
                if acceptance_first:
                    arm_request(db, context=context, claim=claim, body=body)
                else:
                    submissions.submit_preview(
                        db, context=context, preview_id=preview_id, request_id=uuid4()
                    )
                db.commit()
                return "accepted"
            except DomainError as error:
                db.rollback()
                return error.code

    with Session(engine) as owner, ThreadPoolExecutor(max_workers=1) as pool:
        if acceptance_first:
            submissions.submit_preview(
                owner, context=context, preview_id=preview_id, request_id=uuid4()
            )
        else:
            old = owner.exec(
                select(ExecutionStep)
                .where(col(ExecutionStep.id) == old_id)
                .with_for_update()
            ).one()
        future = pool.submit(contender)
        assert started.wait(timeout=5)
        with pytest.raises(TimeoutError):
            future.result(timeout=0.1)
        if not acceptance_first:
            old.status, old.phase = "RUNNING", "REQUEST_ARMED"
            old.lease_token = uuid4()
            old.lease_expires_at = datetime.now(UTC) + timedelta(seconds=60)
        owner.commit()
        assert future.result(timeout=10) == (
            "execution_superseded" if acceptance_first else "continuation_changed"
        )


@pytest.mark.parametrize("late_old_unknown", [False, True])
def test_second_continuation_keeps_original_receipts_and_checks_all_history(
    session, context, partial, late_old_unknown
):
    draft_id, original_id, steps = partial
    identity = revised(session, context, draft_id)
    receipt = submissions.submit_preview(
        session, context=context, preview_id=identity, request_id=uuid4()
    )
    expand(session, context, receipt.submission_id)
    for step in session.exec(
        select(ExecutionStep).where(
            ExecutionStep.submission_id == receipt.submission_id,
            ExecutionStep.kind.in_(["ADGROUP", "AD"]),
        )
    ).all():
        step.status, step.phase, step.error_code = "FAILED", "DONE", "dependency_failed"
    if late_old_unknown:
        for old in steps:
            if old.kind == "ADGROUP":
                session.add(
                    StepEvidence(
                        tenant_id=old.tenant_id,
                        submission_id=original_id,
                        step_id=old.id,
                        attempt=1,
                        conclusion="RESULT_UNKNOWN",
                        summary={},
                    )
                )
    session.flush()
    second = revised(session, context, draft_id)
    units = session.exec(select(BuildUnit).where(BuildUnit.preview_id == second)).all()
    assert units and all(
        u.scene_snapshot["continuation"]["state"]
        == ("BLOCKED" if late_old_unknown else "CONTINUE")
        for u in units
    )
    if not late_old_unknown:
        summary = previews.get_preview_summary(
            session, context=context, preview_id=second
        )
        assert summary.reused_campaign_count == 6
        assert summary.new_campaign_count == 0
