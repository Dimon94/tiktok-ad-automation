"""真实PG：同事实重分析有独立任务/引用权限，并重新核对当前范围。"""

from copy import deepcopy
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlmodel import select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.accounts.models import BCAccountAccess
from app.modules.ai_workbench import reviews
from app.modules.ai_workbench.review_reanalysis import reanalyze_review_task
from app.modules.ai_workbench.review_store import (
    ReviewArtifact,
    ReviewReceipt,
    ReviewSource,
    scoped,
)
from tests.modules.ai_workbench.review_helpers import prepare_task
from tests.modules.ai_workbench.test_review_scale import create_task, step


def completed(session, context):
    task = prepare_task(session, create_task(session, context, 2))
    task.status = "COMPLETE"
    task.result = {"summary": "old answer"}
    task.evidence = {**task.evidence, "methods": {"old": True}}
    session.flush()
    return task


def rerun(session, context, task, **kwargs):
    return reanalyze_review_task(
        session,
        context=context,
        optimizer_id=task.optimizer_id,
        source_task_id=task.id,
        request_id=kwargs.pop("request_id", uuid4()),
        **kwargs,
    )


def test_same_facts_new_task_no_old_answer_and_stale_preparation_noop(session, context):
    original = completed(session, context)
    before = deepcopy(original.evidence)
    request = uuid4()
    task = rerun(session, context, original, request_id=request)
    assert task.id != original.id and task.status == "PENDING"
    assert task.fence != original.fence
    assert not task.result and "methods" not in task.evidence
    assert task.evidence["captured_at"] == original.evidence["captured_at"]
    assert task.evidence["reanalysis"]["fresh_data"] is False
    old = {
        r.id: (r.digest, r.payload)
        for r in session.exec(scoped(ReviewSource, original)).all()
        if r.category != "request"
    }
    new = {
        r.id: (r.digest, r.payload)
        for r in session.exec(scoped(ReviewSource, task)).all()
        if r.category != "request"
    }
    assert old == new and old
    for model in (ReviewArtifact, ReviewReceipt):
        assert session.exec(scoped(model, task)).all() == []
    step(session, task)
    assert task.status == "PENDING"
    assert rerun(session, context, original, request_id=request).id == task.id
    assert original.evidence == before and original.status == "COMPLETE"


@pytest.mark.parametrize(
    "change",
    [
        "unfinished",
        "auto",
        "accounts",
        "version",
        "grants",
        "archive",
        "reporting",
        "failed_marker",
        "malformed",
    ],
)
def test_reject_changed_authority_configuration_or_incomplete_archive(
    session, context, change
):
    original = completed(session, context)
    _, version = reviews.load_optimizer(session, context, original.optimizer_id)
    if change == "unfinished":
        original.status = "RUNNING"
    elif change == "auto":
        version.optimization_mode = "AUTO"
    elif change == "accounts":
        version.advertiser_ids = ["acc-1"]
    elif change == "version":
        version.target_roas = "1.234"
    elif change == "grants":
        grant = session.exec(
            select(BCAccountAccess).where(
                BCAccountAccess.tenant_id == context.tenant_id,
                BCAccountAccess.advertiser_id == "acc-1",
            )
        ).one()
        grant.authorized = False
    elif change == "malformed":
        evidence = deepcopy(original.evidence)
        evidence["optimizer"]["advertiser_ids"] = None
        original.evidence = evidence
    elif change == "failed_marker":
        row = session.exec(
            scoped(ReviewSource, original).where(ReviewSource.category == "preparation")
        ).first()
        from app.modules.ai_workbench.review_store import archive_source

        payload = {**row.payload, "status": "FAILED"}
        captured = row.captured_at
        session.delete(row)
        session.flush()
        archive_source(
            session,
            task=original,
            category="preparation",
            payload=payload,
            captured_at=captured,
            identity="failed-marker",
        )
    else:
        row = session.exec(
            scoped(ReviewSource, original).where(
                ReviewSource.category
                == ("reporting" if change == "reporting" else "preparation")
            )
        ).first()
        session.delete(row)
    session.flush()
    with pytest.raises(DomainError):
        rerun(session, context, original)


def test_other_optimizer_and_tenant_cannot_reuse(session, context):
    original = completed(session, context)
    with pytest.raises(DomainError):
        reanalyze_review_task(
            session,
            context=context,
            optimizer_id=uuid4(),
            source_task_id=original.id,
            request_id=uuid4(),
        )
    other = TenantContext(tenant_id=uuid4(), actor_id=context.actor_id, role="")
    with pytest.raises((DomainError, HTTPException)):
        rerun(session, other, original)
