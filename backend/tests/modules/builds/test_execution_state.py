"""Real PostgreSQL attempt boundaries and late-result evidence; no SDK calls."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import select

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.builds import CreatedObject
from app.integrations.tiktok.contracts.common import CallEvidence
from app.modules.builds.execution_models import (
    ExecutionStep,
    StepEvidence,
    Submission,
    SubmissionUnit,
)
from app.modules.builds.execution_schemas import StepClaim
from app.modules.builds.previews import generate_preview, get_preview_units
from app.modules.builds.routes import load_preview_route, save_attempt_context
from tests.modules.builds.test_previews import drain
from tests.modules.builds.test_previews import prepared as prepared


@pytest.fixture
def attempt(session, context, prepared, request):
    kind = getattr(request, "param", "CAMPAIGN")
    preview = generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    drain(session, context, preview)
    unit = get_preview_units(session, context=context, preview_id=preview).items[0]
    sub = Submission(
        tenant_id=context.tenant_id,
        bc_id="bc-draft",
        preview_id=preview,
        draft_id=prepared,
        actor_id=context.actor_id,
        ordinal=1,
    )
    session.add(sub)
    session.flush()
    session.add(
        SubmissionUnit(
            tenant_id=context.tenant_id,
            submission_id=sub.id,
            unit_id=unit.unit_id,
            preview_id=preview,
            bc_id="bc-draft",
            disposition="INCLUDED",
            expanded=True,
        )
    )
    session.flush()
    nonce = uuid4()
    expires = datetime.now(UTC) + timedelta(seconds=60)
    step = ExecutionStep(
        tenant_id=context.tenant_id,
        submission_id=sub.id,
        preview_id=preview,
        bc_id="bc-draft",
        unit_id=unit.unit_id,
        kind=kind,
        step_key="campaign",
        status="RUNNING",
        phase="CLAIMED",
        attempt=1,
        lease_token=nonce,
        lease_expires_at=expires,
    )
    session.add(step)
    attempt_id = save_attempt_context(session, step=step)
    session.flush()
    claim = StepClaim(
        step_id=step.id,
        tenant_id=context.tenant_id,
        submission_id=sub.id,
        preview_id=preview,
        unit_id=unit.unit_id,
        actor_id=context.actor_id,
        bc_id="bc-draft",
        advertiser_id=unit.advertiser_id,
        kind=kind,
        group_id=None,
        planned_ad_id=None,
        material_id=None,
        parent_step_id=None,
        lease_token=nonce,
        lease_expires_at=expires,
        attempt=1,
        attempt_id=attempt_id,
        route=load_preview_route(session, context=context, preview_id=preview),
        dispatch_revision=0,
    )
    return step, claim


def body(claim):
    return {
        "advertiser_id": claim.advertiser_id,
        "campaign_name": "frozen",
        "operation_status": "ENABLE",
        "budget": 100,
    }


def test_arm_persists_full_body_and_receipt_saves_id_before_any_child(
    session, context, attempt
):
    from app.modules.builds.execution_state import arm_request, record_created

    step, claim = attempt
    digest = arm_request(session, context=context, claim=claim, body=body(claim))
    session.flush()
    assert step.phase == "REQUEST_ARMED" and step.request_body == body(claim)
    assert digest == step.request_body_digest and len(digest) == 64
    assert (
        record_created(
            session,
            claim=claim,
            result=CreatedObject(
                kind="CAMPAIGN",
                remote_id="remote-c1",
                operation_status="ENABLE",
                evidence=CallEvidence(request_id="request1"),
            ),
        )
        == "SUCCEEDED"
    )
    session.flush()
    assert (
        step.remote_id == "remote-c1"
        and step.status == "SUCCEEDED"
        and step.phase == "DONE"
    )
    events = session.exec(
        select(StepEvidence).where(StepEvidence.step_id == step.id)
    ).all()
    assert {e.conclusion for e in events} == {"REQUEST_ARMED", "CREATED"}
    assert all("Access-Token" not in e.summary for e in events)


def test_expired_armed_attempt_can_only_become_unknown(session, context, attempt):
    from app.modules.builds.execution_state import arm_request, expire_attempt

    step, claim = attempt
    arm_request(session, context=context, claim=claim, body=body(claim))
    step.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.flush()
    assert expire_attempt(session, step=step) == "RECONCILE"
    session.flush()
    assert step.status == "UNKNOWN" and step.request_body == body(claim)


def test_expired_unarmed_attempt_is_safely_reclaimable(session, attempt):
    from app.modules.builds.execution_state import expire_attempt

    step, _ = attempt
    step.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert expire_attempt(session, step=step) == "RECLAIM"
    session.flush()
    assert (
        step.status == "PENDING" and step.phase == "IDLE" and step.lease_token is None
    )


def test_stale_worker_cannot_arm_or_replace_a_new_owner(session, context, attempt):
    from app.modules.builds.execution_state import arm_request, record_created

    step, old = attempt
    step.lease_token = uuid4()
    step.attempt = 2
    save_attempt_context(session, step=step)
    session.flush()
    with pytest.raises(DomainError) as caught:
        arm_request(session, context=context, claim=old, body=body(old))
    assert caught.value.code == "execution_lease_lost"
    current_nonce = step.lease_token
    assert (
        record_created(
            session,
            claim=old,
            result=CreatedObject(
                kind="CAMPAIGN",
                remote_id="late-id",
                operation_status="ENABLE",
                evidence=CallEvidence(request_id="late-request"),
            ),
        )
        == "UNKNOWN"
    )
    session.flush()
    assert step.remote_id is None and step.lease_token == current_nonce
    evidence = session.exec(
        select(StepEvidence).where(
            StepEvidence.step_id == step.id, StepEvidence.conclusion == "LATE_CREATED"
        )
    ).one()
    assert evidence.summary["remote_id"] == "late-id" and evidence.attempt == 1


def test_arm_rejects_cross_tenant_and_non_json_intent(
    session, context, other_context, attempt
):
    from app.modules.builds.execution_state import arm_request

    step, claim = attempt
    with pytest.raises(DomainError):
        arm_request(session, context=other_context, claim=claim, body=body(claim))
    with pytest.raises(DomainError):
        arm_request(
            session,
            context=context,
            claim=claim,
            body={**body(claim), "budget": float("nan")},
        )
    assert step.phase == "CLAIMED" and step.request_body is None


def test_unknown_result_keeps_original_body_and_success_never_regresses(
    session, context, attempt
):
    from app.modules.builds.execution_state import (
        arm_request,
        record_created,
        record_unknown,
    )

    step, claim = attempt
    arm_request(session, context=context, claim=claim, body=body(claim))
    assert (
        record_unknown(
            session,
            claim=claim,
            code="create_result_unknown",
            request_id="lost",
            remote_code=-1,
        )
        == "UNKNOWN"
    )
    session.flush()
    assert step.request_body == body(claim) and step.remote_id is None
    record_created(
        session,
        claim=claim,
        result=CreatedObject(
            kind="CAMPAIGN",
            remote_id="eventual-id",
            operation_status="ENABLE",
            evidence=CallEvidence(request_id="receipt"),
        ),
    )
    session.flush()
    # A late known ID is evidence for reconciliation, never a blind state overwrite.
    assert step.status == "UNKNOWN" and step.remote_id is None


def test_typed_receipt_keeps_attempt_and_all_safe_correlation_ids(
    session, context, attempt
):
    from app.integrations.tiktok.contracts.builds import CreatedObject
    from app.integrations.tiktok.contracts.common import CallEvidence
    from app.modules.builds.execution_state import arm_request, record_created

    step, claim = attempt
    arm_request(session, context=context, claim=claim, body=body(claim))
    result = CreatedObject(
        kind="CAMPAIGN",
        remote_id="typed-remote",
        operation_status=None,
        evidence=CallEvidence(
            request_id="provider-r", mcp_request_id="mcp-r", remote_task_id="task-r"
        ),
    )
    assert record_created(session, claim=claim, result=result) == "SUCCEEDED"
    row = session.exec(
        select(StepEvidence).where(
            StepEvidence.step_id == step.id, StepEvidence.conclusion == "CREATED"
        )
    ).one()
    assert row.request_id == "provider-r"
    assert row.summary == {
        "remote_id": "typed-remote",
        "operation_status": None,
        "attempt_id": str(claim.attempt_id),
        "mcp_request_id": "mcp-r",
        "remote_task_id": "task-r",
    }


@pytest.mark.parametrize("attempt", ["AD"], indirect=True)
def test_ad_disable_receipt_without_status_waits_for_readback(session, attempt):
    """广告创建回执缺少状态时，不能把请求值当成平台停用事实。"""
    from app.modules.builds.execution_state import record_created

    step, claim = attempt
    # Seed the already armed receipt boundary; asset compilation has separate
    # integration coverage and is not part of status readback reconciliation.
    import hashlib
    import json

    step.request_body = {
        "advertiser_id": claim.advertiser_id,
        "adgroup_id": "group-1",
        "ad_name": "ad-user-test",
        "operation_status": "DISABLE",
    }
    step.request_body_digest = hashlib.sha256(json.dumps(
        step.request_body, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    step.phase = "REQUEST_ARMED"
    session.flush()
    result = CreatedObject(
        kind="AD",
        remote_id="ad-1",
        operation_status=None,
        evidence=CallEvidence(request_id="request-ad"),
    )

    assert record_created(session, claim=claim, result=result) == "UNKNOWN"
    assert step.remote_id == "ad-1"
    assert step.error_code == "operation_status_pending_readback"


def test_proven_not_sent_keeps_exact_armed_body_and_can_schedule_once(
    session, context, attempt
):
    from app.integrations.tiktok.contracts.common import CallEvidence, RemoteCallError
    from app.modules.builds.execution_state import arm_request, record_not_sent

    step, claim = attempt
    arm_request(session, context=context, claim=claim, body=body(claim))
    digest = step.request_body_digest
    result = record_not_sent(
        session,
        claim=claim,
        error=RemoteCallError(
            "admission_deferred", effect="NOT_SENT", evidence=CallEvidence()
        ),
        retryable=True,
        delay=2,
    )
    assert result == "PENDING" and step.phase == "IDLE"
    assert step.request_body == body(claim) and step.request_body_digest == digest
    assert step.lease_token is None


@pytest.mark.parametrize(
    "remote_code,retries",
    [(40002, 1), (51002, 2)],
)
def test_known_ad_rejection_has_a_code_specific_bounded_retry_budget(
    session, context, attempt, remote_code, retries
):
    from app.integrations.tiktok.contracts.common import RemoteCallError
    from app.modules.builds.execution_state import arm_request, record_rejected

    step, claim = attempt
    arm_request(session, context=context, claim=claim, body=body(claim))
    for rejection in range(1, retries + 2):
        result = record_rejected(
            session,
            claim=claim,
            error=RemoteCallError(
                "mcp_business_error",
                effect="REJECTED_NO_EFFECT",
                evidence=CallEvidence(
                    request_id=f"request-{rejection}", remote_code=remote_code
                ),
            ),
        )
        assert result == ("PENDING" if rejection <= retries else "FAILED")
        assert step.resolved["business_rejection_counts"][str(remote_code)] == rejection
        if rejection <= retries:
            nonce = uuid4()
            expires = datetime.now(UTC) + timedelta(seconds=60)
            step.status, step.phase = "RUNNING", "REQUEST_ARMED"
            step.lease_token, step.lease_expires_at = nonce, expires
            claim = claim.model_copy(
                update={"lease_token": nonce, "lease_expires_at": expires}
            )

    events = session.exec(
        select(StepEvidence).where(
            StepEvidence.step_id == step.id,
            StepEvidence.conclusion == "REMOTE_REJECTED",
        )
    ).all()
    assert [event.summary["remote_code"] for event in events] == [remote_code] * (
        retries + 1
    )
    assert all(
        event.summary["body_digest"] == step.request_body_digest for event in events
    )


@pytest.mark.parametrize("attempt", ["ADGROUP"], indirect=True)
@pytest.mark.parametrize("remote_code", [40002, 50002])
def test_platform_reason_survives_adapter_receipt_and_public_events(
    session, context, attempt, remote_code
):
    from app.integrations.tiktok.adapters.build_results import sdk_creation_envelope
    from app.integrations.tiktok.contracts.common import RemoteCallError
    from app.modules.builds.execution_state import (
        arm_request,
        record_rejected,
        record_unknown,
    )
    from app.modules.builds.submission_catalog import get_submission_events

    step, claim = attempt
    arm_request(session, context=context, claim=claim, body=body(claim))
    message = "Invalid value for field targeting_optimization_mode: MANUAL"
    with pytest.raises(RemoteCallError) as caught:
        sdk_creation_envelope(
            {"code": remote_code, "request_id": "platform-request", "message": message},
            kind="ADGROUP",
        )
    error = caught.value
    if remote_code == 40002:
        assert record_rejected(session, claim=claim, error=error) == "PENDING"
    else:
        assert (
            record_unknown(
                session,
                claim=claim,
                code=error.code,
                request_id=error.evidence.request_id,
                call_evidence=error.evidence,
            )
            == "UNKNOWN"
        )
    session.flush()
    session.expire_all()
    event = next(
        item
        for item in get_submission_events(
            session,
            context=context,
            submission_id=claim.submission_id,
            step_id=claim.step_id,
        ).items
        if item.conclusion != "REQUEST_ARMED"
    )
    assert getattr(event, "remote_message", None) == message
    assert event.remote_code == remote_code
    assert event.request_id == "platform-request"


def test_local_deadline_not_sent_uses_the_bounded_transport_retry_budget():
    from app.integrations.tiktok.contracts.common import CallEvidence, RemoteCallError
    from app.modules.builds.execution_state import transient_retry

    resolved = {}
    outcomes = []
    for _ in range(3):
        retryable, delay, resolved = transient_retry(
            resolved,
            error=RemoteCallError(
                "tiktok_call_deadline_exceeded",
                effect="NOT_SENT",
                evidence=CallEvidence(),
            ),
            retryable=False,
            delay=15,
        )
        outcomes.append((retryable, delay, resolved.get("transport_failure_count")))

    assert outcomes == [(True, 5, 1), (True, 10, 2), (False, 20, 3)]


def test_pre_arm_deadline_uses_the_same_bounded_transport_retry_budget():
    from app.modules.builds.execution_state import transient_retry

    resolved = {}
    outcomes = []
    for _ in range(3):
        retryable, delay, resolved = transient_retry(
            resolved,
            error=DomainError(
                "tiktok_call_deadline_exceeded", "TikTok 调用超过本地硬截止时间"
            ),
            retryable=False,
            delay=15,
            definitely_not_sent=True,
        )
        outcomes.append((retryable, delay, resolved.get("transport_failure_count")))

    assert outcomes == [(True, 5, 1), (True, 10, 2), (False, 20, 3)]


@pytest.fixture
def resumable_attempt(session, attempt):
    original, claim = attempt
    step = ExecutionStep(
        **(
            original.model_dump()
            | {
                "id": uuid4(),
                "kind": "CTA",
                "step_key": "resume-cta",
                "attempt_id": None,
            }
        )
    )
    session.add(step)
    identity = save_attempt_context(session, step=step)
    session.flush()
    return step, claim.model_copy(
        update={"step_id": step.id, "kind": "CTA", "attempt_id": identity}
    )


def test_not_sent_reclaim_keeps_original_attempt_and_wire_digest(
    session, context, resumable_attempt
):
    from app.integrations.tiktok.contracts.common import CallEvidence, RemoteCallError
    from app.modules.builds.execution_state import arm_request, record_not_sent
    from app.modules.builds.submissions import claim_step

    step, old = resumable_attempt
    request = {
        "advertiser_id": old.advertiser_id,
        "creative_portfolio_type": "CTA",
        "portfolio_content": [{"asset_ids": ["synthetic"], "asset_content": "Watch"}],
    }
    digest = arm_request(session, context=context, claim=old, body=request)
    record_not_sent(
        session,
        claim=old,
        error=RemoteCallError(
            "admission_deferred", effect="NOT_SENT", evidence=CallEvidence()
        ),
        retryable=True,
        delay=0,
    )
    from app.modules.builds.dispatch import queue_step

    queue_step(
        session, step=step, submission=session.get(Submission, step.submission_id)
    )
    new = claim_step(session, context=context, step_id=step.id, owner=uuid4())
    assert new is not None
    assert (new.attempt, new.attempt_id) == (old.attempt, old.attempt_id)
    assert new.lease_token != old.lease_token
    assert arm_request(session, context=context, claim=new, body=request) == digest
    assert step.request_body == request


def test_explicit_rejection_reclaim_keeps_frozen_attempt_and_body(
    session, context, resumable_attempt
):
    from app.integrations.tiktok.contracts.common import RemoteCallError
    from app.modules.builds.dispatch import queue_step
    from app.modules.builds.execution_state import arm_request, record_rejected
    from app.modules.builds.submissions import claim_step

    step, old = resumable_attempt
    request = {
        "advertiser_id": old.advertiser_id,
        "creative_portfolio_type": "CTA",
        "portfolio_content": [{"asset_ids": ["synthetic"], "asset_content": "Watch"}],
    }
    digest = arm_request(session, context=context, claim=old, body=request)
    assert (
        record_rejected(
            session,
            claim=old,
            error=RemoteCallError(
                "mcp_business_error",
                effect="REJECTED_NO_EFFECT",
                evidence=CallEvidence(request_id="rejected", remote_code=40002),
            ),
        )
        == "PENDING"
    )
    queue_step(
        session, step=step, submission=session.get(Submission, step.submission_id)
    )
    new = claim_step(session, context=context, step_id=step.id, owner=uuid4())
    assert new is not None
    assert (new.attempt, new.attempt_id) == (old.attempt, old.attempt_id)
    assert arm_request(session, context=context, claim=new, body=request) == digest


def test_effect_evidence_blocks_an_explicit_rejection_retry(
    session, context, resumable_attempt
):
    from app.integrations.tiktok.contracts.common import RemoteCallError
    from app.modules.builds.dispatch import queue_step
    from app.modules.builds.execution_state import (
        arm_request,
        evidence,
        record_rejected,
    )

    step, claim = resumable_attempt
    arm_request(
        session,
        context=context,
        claim=claim,
        body={"advertiser_id": claim.advertiser_id},
    )
    record_rejected(
        session,
        claim=claim,
        error=RemoteCallError(
            "mcp_business_error",
            effect="REJECTED_NO_EFFECT",
            evidence=CallEvidence(remote_code=40002),
        ),
    )
    evidence(session, step=step, claim=claim, conclusion="RESULT_UNKNOWN")
    with pytest.raises(
        DomainError,
        check=lambda error: error.code == "execution_requires_reconciliation",
    ):
        queue_step(
            session, step=step, submission=session.get(Submission, step.submission_id)
        )


@pytest.mark.parametrize("terminal", ["RESULT_UNKNOWN", "LATE_CREATED"])
def test_historical_not_sent_never_reopens_later_effect_evidence(
    session, context, resumable_attempt, terminal
):
    from app.integrations.tiktok.contracts.common import CallEvidence, RemoteCallError
    from app.modules.builds.execution_state import (
        arm_request,
        evidence,
        record_not_sent,
    )
    from app.modules.builds.submissions import claim_step

    step, old = resumable_attempt
    arm_request(
        session, context=context, claim=old, body={"advertiser_id": old.advertiser_id}
    )
    record_not_sent(
        session,
        claim=old,
        error=RemoteCallError(
            "admission_deferred", effect="NOT_SENT", evidence=CallEvidence()
        ),
        retryable=True,
        delay=0,
    )
    evidence(session, step=step, claim=old, conclusion=terminal)
    session.flush()
    with pytest.raises(DomainError) as caught:
        claim_step(session, context=context, step_id=step.id, owner=uuid4())
    assert caught.value.code == "create_result_unknown"
    assert step.attempt_id == old.attempt_id


def test_not_sent_with_stale_nonce_cannot_release_new_owner(
    session, context, resumable_attempt
):
    from app.integrations.tiktok.contracts.common import CallEvidence, RemoteCallError
    from app.modules.builds.execution_state import arm_request, record_not_sent

    step, claim = resumable_attempt
    arm_request(
        session,
        context=context,
        claim=claim,
        body={"advertiser_id": claim.advertiser_id},
    )
    replacement = uuid4()
    step.lease_token = replacement
    session.flush()
    assert (
        record_not_sent(
            session,
            claim=claim,
            error=RemoteCallError(
                "admission_deferred", effect="NOT_SENT", evidence=CallEvidence()
            ),
            retryable=True,
        )
        == "RUNNING"
    )
    assert step.lease_token == replacement and step.phase == "REQUEST_ARMED"


@pytest.mark.parametrize(
    "explicit_code,expected", [(None, 40002), (0, 0), (40001, 40001)]
)
def test_unknown_persists_platform_code_without_authorizing_retry(
    session, context, resumable_attempt, explicit_code, expected
):
    from app.modules.builds.dispatch import queue_step
    from app.modules.builds.execution_state import arm_request, record_unknown
    from app.modules.builds.recovery import request_recovery

    step, claim = resumable_attempt
    request = {"advertiser_id": claim.advertiser_id}
    digest = arm_request(session, context=context, claim=claim, body=request)
    assert (
        record_unknown(
            session,
            claim=claim,
            code="mcp_business_error",
            remote_code=explicit_code,
            call_evidence=CallEvidence(
                request_id="platform-request", remote_code=40002
            ),
        )
        == "UNKNOWN"
    )
    row = session.exec(
        select(StepEvidence).where(
            StepEvidence.step_id == step.id, StepEvidence.conclusion == "RESULT_UNKNOWN"
        )
    ).one()
    assert row.summary["remote_code"] == expected
    assert step.request_body == request and step.request_body_digest == digest
    assert step.remote_id is None
    with pytest.raises(
        DomainError, check=lambda e: e.code == "execution_requires_reconciliation"
    ):
        queue_step(
            session, step=step, submission=session.get(Submission, step.submission_id)
        )
    with pytest.raises(DomainError, check=lambda e: e.code == "recovery_no_candidates"):
        request_recovery(
            session,
            context=context,
            submission_id=step.submission_id,
            request_id=uuid4(),
            kind="RETRY",
        )
