"""DSH machine identity, scoped leases and structured completion regression."""

from uuid import UUID, uuid4

from sqlmodel import select

from app.core.config import settings
from app.modules.ai_workbench.reviews import DSHReviewTask
from tests.modules.ai_workbench.review_helpers import prepare_task

from .test_optimizer import api_headers, create_body, seed_optimizer_dependencies
from .test_review_methods import plugin_methods


def test_runtime_claim_complete_and_reject_old_fence(
    client, session, context, monkeypatch
):
    monkeypatch.setattr(
        settings, "AI_DSH_SERVICE_TOKEN", "test-dsh-service-token-" + "x" * 32
    )
    strategy = seed_optimizer_dependencies(session, context)
    created = client.post(
        f"/api/tenants/{context.tenant_id}/ai-optimizers",
        headers=api_headers(context, session=session),
        json=create_body(strategy.id),
    ).json()
    base = f"/api/tenants/{context.tenant_id}/ai-optimizers/{created['id']}"
    task = client.post(
        base + "/reviews",
        headers=api_headers(context, session=session),
        json={"request_id": str(uuid4())},
    )
    assert task.status_code == 201
    assert task.json()["task"]["status"] == "PREPARING"
    prepare_task(session, session.get(DSHReviewTask, UUID(task.json()["task"]["id"])))
    machine = {"Authorization": "Bearer " + settings.AI_DSH_SERVICE_TOKEN}
    assert (
        client.post("/api/integrations/ai-workbench/reviews/claim").status_code == 401
    )
    claim = client.post(
        "/api/integrations/ai-workbench/reviews/claim",
        headers=machine,
        json=plugin_methods(),
    ).json()
    assert claim["task"]["id"] == task.json()["task"]["id"]
    token = {"Authorization": "Bearer " + claim["task_token"]}
    # 任务 token 不能作为服务 token 领取其他租户任务。
    assert (
        client.post(
            "/api/integrations/ai-workbench/reviews/claim",
            headers=token,
            json=plugin_methods(),
        ).status_code
        == 403
    )
    url = "/api/integrations/ai-workbench/reviews/" + claim["task"]["id"] + "/complete"
    result = client.post(
        url, headers=token, json={"summary": "证据不足，本轮不调整", "proposals": []}
    )
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "COMPLETE"
    assert (
        client.post(
            url,
            headers=token,
            json={"summary": "证据不足，本轮不调整", "proposals": []},
        ).status_code
        == 200
    )
    assert (
        client.post(
            url, headers=token, json={"summary": "冲突结果", "proposals": []}
        ).status_code
        == 409
    )


def test_runtime_recovery_changes_fence_and_cancel_revokes_token(
    client, session, context, monkeypatch
):
    monkeypatch.setattr(
        settings, "AI_DSH_SERVICE_TOKEN", "test-dsh-service-token-" + "x" * 32
    )
    strategy = seed_optimizer_dependencies(session, context)
    optimizer = client.post(
        f"/api/tenants/{context.tenant_id}/ai-optimizers",
        headers=api_headers(context, session=session),
        json=create_body(strategy.id),
    ).json()
    base = f"/api/tenants/{context.tenant_id}/ai-optimizers/{optimizer['id']}"
    client.post(
        base + "/reviews",
        headers=api_headers(context, session=session),
        json={"request_id": str(uuid4())},
    )
    prepare_task(
        session,
        session.exec(
            select(DSHReviewTask).where(
                DSHReviewTask.optimizer_id == UUID(optimizer["id"])
            )
        ).one(),
    )
    claim = client.post(
        "/api/integrations/ai-workbench/reviews/claim",
        headers={"Authorization": "Bearer " + settings.AI_DSH_SERVICE_TOKEN},
        json=plugin_methods(),
    ).json()
    cancelled = client.post(
        base + "/reviews/" + claim["task"]["id"] + "/cancel",
        headers=api_headers(context, session=session),
        json={},
    )
    assert cancelled.status_code == 200
    response = client.post(
        "/api/integrations/ai-workbench/reviews/" + claim["task"]["id"] + "/complete",
        headers={"Authorization": "Bearer " + claim["task_token"]},
        json={"summary": "旧任务", "proposals": []},
    )
    assert response.status_code == 409


def test_manual_budget_proposal_requires_explicit_approval(client, session, context):
    strategy = seed_optimizer_dependencies(session, context)
    optimizer = client.post(
        f"/api/tenants/{context.tenant_id}/ai-optimizers",
        headers=api_headers(context, session=session),
        json=create_body(
            strategy.id,
            optimization_mode="MANUAL",
            authorization_limits={"strategy": True, "max_budget_delta": 1},
        ),
    ).json()
    base = f"/api/tenants/{context.tenant_id}/ai-optimizers/{optimizer['id']}"
    config = {**strategy.config, "budget": "20"}
    response = client.post(
        base + "/reviews",
        headers=api_headers(context, session=session),
        json={
            "request_id": str(uuid4()),
            "proposals": [
                {
                    "kind": "strategy",
                    "after": {"config": config},
                    "rationale": "人工确认预算增加",
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    proposal = response.json()["proposals"][0]
    assert proposal["status"] == "PENDING"
    approved = client.post(
        base + "/proposals/" + proposal["id"] + "/accept",
        headers=api_headers(context, session=session),
        json={"request_id": str(uuid4())},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "ACCEPTED"


def test_periodic_review_uses_optimizer_local_time_and_deduplicates(
    client, session, context, monkeypatch
):
    from datetime import UTC, datetime

    from app.modules.ai_workbench.runtime import schedule_reviews

    monkeypatch.setattr(settings, "AI_DSH_SERVICE_TOKEN", "x" * 32)
    strategy = seed_optimizer_dependencies(session, context)
    result = client.post(
        f"/api/tenants/{context.tenant_id}/ai-optimizers",
        headers=api_headers(context, session=session),
        json=create_body(strategy.id, timezone="Asia/Shanghai", review_time="13:00"),
    )
    assert result.status_code == 201, result.text
    assert schedule_reviews(session, now=datetime(2026, 11, 12, 4, 59, tzinfo=UTC)) == 0
    assert schedule_reviews(session, now=datetime(2026, 11, 12, 5, 0, tzinfo=UTC)) == 1
    assert schedule_reviews(session, now=datetime(2026, 11, 12, 6, 0, tzinfo=UTC)) == 0


def test_business_review_evidence_excludes_other_optimizer_account_inputs(
    client, session, context
):
    from datetime import UTC, date, datetime
    from uuid import UUID

    from app.modules.ai_workbench.reviews import frozen_evidence, load_optimizer
    from app.modules.business.models import BusinessPublication, BusinessRun

    strategy = seed_optimizer_dependencies(session, context)
    created = client.post(
        f"/api/tenants/{context.tenant_id}/ai-optimizers",
        headers=api_headers(context, session=session),
        json=create_body(strategy.id),
    ).json()
    optimizer, version = load_optimizer(session, context, UUID(created["id"]))
    runs = []
    for month, accounts in [(1, {"acc-1": {}}), (4, {"acc-2": {}}), (7, {})]:
        run = BusinessRun(
            tenant_id=context.tenant_id,
            bc_id="bc-a",
            actor_id=context.actor_id,
            quarter=date(2026, month, 1),
            end_date=date(2026, month, 2),
            cursor=date(2026, month, 1),
            scope_revision=1,
            status="COMPLETE",
            finished_at=datetime.now(UTC),
            inputs={"accounts": accounts},
        )
        session.add(run)
        session.flush()
        session.add(
            BusinessPublication(
                tenant_id=context.tenant_id,
                bc_id="bc-a",
                quarter=run.quarter,
                run_id=run.id,
            )
        )
        runs.append(run)
    session.flush()
    snapshot = frozen_evidence(
        session, context=context, optimizer=optimizer, version=version
    )
    assert [item["id"] for item in snapshot["business"]["runs"]] == [str(runs[0].id)]
    assert [item["run_id"] for item in snapshot["business"]["publications"]] == [
        str(runs[0].id)
    ]
