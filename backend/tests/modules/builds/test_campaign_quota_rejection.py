"""额度明确拒绝应落定失败；普通40002与不完整回执仍保留未知。"""

import pytest
from sqlmodel import select

from app.integrations.tiktok.adapters.build_results import sdk_creation_envelope
from app.integrations.tiktok.contracts.common import CallEvidence, RemoteCallError
from app.modules.builds.execution_models import StepEvidence
from tests.modules.builds.test_execution_state import attempt as attempt
from tests.modules.builds.test_execution_state import body
from tests.modules.builds.test_previews import prepared as prepared

QUOTA_MESSAGE = (
    "Your ad account has met its Smart+ campaign quota. "
    "Pause an active Smart+ campaign to activate another one."
)


@pytest.mark.parametrize(
    "code,message,kind,effect",
    [
        (40002, QUOTA_MESSAGE, "CAMPAIGN", "REJECTED_NO_EFFECT"),
        (40002, "Invalid parameter", "CAMPAIGN", "UNKNOWN"),
        (40002, None, "CAMPAIGN", "UNKNOWN"),
        (50002, QUOTA_MESSAGE, "CAMPAIGN", "UNKNOWN"),
        ("40002", QUOTA_MESSAGE, "CAMPAIGN", "UNKNOWN"),
        (40002, QUOTA_MESSAGE, "CTA", "UNKNOWN"),
    ],
)
def test_quota_classification_requires_exact_campaign_business_evidence(
    code, message, kind, effect
):
    with pytest.raises(RemoteCallError) as caught:
        sdk_creation_envelope(
            {
                "code": code,
                "message": message,
                "request_id": "quota-request",
                "data": {},
            },
            kind=kind,
        )
    assert caught.value.effect == effect
    assert caught.value.evidence.request_id == "quota-request"


def test_quota_rejection_is_terminal_without_retrying_or_changing_frozen_body(
    session, context, attempt
):
    from app.modules.builds.execution_state import arm_request, record_rejected

    step, claim = attempt
    frozen = body(claim)
    digest = arm_request(session, context=context, claim=claim, body=frozen)
    result = record_rejected(
        session,
        claim=claim,
        error=RemoteCallError(
            "tiktok_business_error",
            effect="REJECTED_NO_EFFECT",
            evidence=CallEvidence(
                request_id="quota-request",
                remote_code=40002,
                remote_message=QUOTA_MESSAGE,
            ),
        ),
    )
    assert result == "FAILED"
    assert step.error_code == "tiktok_campaign_quota_exceeded"
    assert step.request_body == frozen and step.request_body_digest == digest
    assert step.remote_id is None and step.lease_token is None
    event = session.exec(
        select(StepEvidence).where(StepEvidence.conclusion == "REMOTE_REJECTED")
    ).one()
    assert event.summary["remote_message"] == QUOTA_MESSAGE


def test_saved_quota_receipt_can_settle_historical_unknown(session, context, attempt):
    from app.modules.builds.execution_state import arm_request, record_unknown
    from app.modules.builds.reconciliation import _recover_known_rejection

    step, claim = attempt
    arm_request(session, context=context, claim=claim, body=body(claim))
    record_unknown(
        session,
        claim=claim,
        code="create_result_unknown",
        call_evidence=CallEvidence(
            request_id="quota-request", remote_code=40002, remote_message=QUOTA_MESSAGE
        ),
    )
    result = _recover_known_rejection(session, step=step, source=step)
    assert result is not None and result.state == "FAILED"
    assert step.error_code == "tiktok_campaign_quota_exceeded"
    assert step.remote_id is None


def test_quota_message_with_conflicting_object_identity_remains_unknown():
    with pytest.raises(RemoteCallError) as caught:
        sdk_creation_envelope(
            {
                "code": 40002,
                "message": QUOTA_MESSAGE,
                "data": {"campaign_id": "possibly-created"},
            },
            kind="CAMPAIGN",
        )
    assert caught.value.effect == "UNKNOWN"
    assert caught.value.code == "create_receipt_conflict"


def test_conflicting_quota_receipt_cannot_be_settled_from_message(
    session, context, attempt
):
    from app.modules.builds.execution_state import arm_request, record_unknown
    from app.modules.builds.reconciliation import _recover_known_rejection

    step, claim = attempt
    arm_request(session, context=context, claim=claim, body=body(claim))
    record_unknown(
        session,
        claim=claim,
        code="create_receipt_conflict",
        call_evidence=CallEvidence(
            request_id="conflicting-receipt",
            remote_code=40002,
            remote_message=QUOTA_MESSAGE,
        ),
    )
    assert _recover_known_rejection(session, step=step, source=step) is None
    assert step.status == "UNKNOWN"
