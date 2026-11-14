from uuid import uuid4

from app.core.config import settings
from app.modules.ai_workbench import service
from app.modules.ai_workbench.knowledge import KnowledgeInput, create_version
from app.modules.ai_workbench.reviews import create_review_task
from app.modules.ai_workbench.schemas import OptimizerCreate
from app.modules.tenants.models import TenantMembership
from tests.modules.ai_workbench.review_helpers import create_prepared_review_task
from tests.modules.ai_workbench.test_optimizer import (
    create_body,
    seed_optimizer_dependencies,
)
from tests.modules.ai_workbench.test_review_methods import plugin_methods


def test_agent_search_read_frozen_policy_and_scoped_citation(
    client, session, context, monkeypatch
):
    member = session.get(TenantMembership, (context.tenant_id, context.actor_id))
    member.role = "tenant_admin"
    session.flush()
    strategy = seed_optimizer_dependencies(session, context)
    policy = create_version(
        session,
        context=context,
        body=KnowledgeInput(
            key="subsidy",
            title="新剧补贴",
            body="补贴资格需按政策核验，不等于实际到账。",
            source="policy-source",
            tags=["business"],
        ),
    )
    optimizer = service.create_optimizer(
        session,
        context=context,
        body=OptimizerCreate.model_validate(create_body(strategy.id)),
    )
    task = create_prepared_review_task(
        session, context=context, optimizer_id=optimizer.id, request_id=uuid4()
    )
    assert not task.evidence["knowledge"]
    assert "knowledge_catalog" not in task.evidence
    assert task.evidence["knowledge"] == []
    monkeypatch.setattr(settings, "AI_DSH_SERVICE_TOKEN", "test-" + "x" * 40)
    claim = client.post(
        "/api/integrations/ai-workbench/reviews/claim",
        headers={"Authorization": "Bearer " + settings.AI_DSH_SERVICE_TOKEN},
        json=plugin_methods(),
    ).json()
    headers = {"Authorization": "Bearer " + claim["task_token"]}
    base = f"/api/integrations/ai-workbench/reviews/{task.id}"
    found = client.post(
        base + "/knowledge/search", headers=headers, json={"query": "补贴"}
    )
    assert found.status_code == 200, found.text
    assert found.json()["items"][0]["id"] == str(policy.id)
    assert "body" not in found.json()["items"][0]
    loaded = client.post(
        base + "/knowledge/read", headers=headers, json={"knowledge_id": str(policy.id)}
    )
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["body"] == policy.body
    assert loaded.json()["sha256"]
    # 截止后新增版本不改变本轮检索/读取，任意 ID 不可读取。
    updated = create_version(
        session,
        context=context,
        asset_id=policy.asset_id,
        body=KnowledgeInput(
            key="subsidy", title="新政策", body="下一轮使用", source="policy-source"
        ),
    )
    assert (
        client.post(
            base + "/knowledge/read",
            headers=headers,
            json={"knowledge_id": str(updated.id)},
        ).status_code
        == 404
    )
    assert (
        client.post(
            base + "/knowledge/read",
            headers=headers,
            json={"knowledge_id": str(uuid4())},
        ).status_code
        == 404
    )
    result = client.post(
        base + "/complete",
        headers=headers,
        json={
            "summary": "按已读取政策分析",
            "proposals": [],
            "recommendations": [
                {
                    "problem": "资格未知",
                    "suggestion": "核验资格",
                    "evidence_ids": [str(policy.id)],
                }
            ],
        },
    )
    assert result.status_code == 200, result.text
    assert (
        client.post(
            base + "/knowledge/search", headers=headers, json={"query": "补贴"}
        ).status_code
        == 409
    )


def test_search_never_reads_other_tenant_or_bc(session, context):
    from app.modules.ai_workbench.knowledge_search import search_for_task
    from tests.modules.conftest import create_context

    member = session.get(TenantMembership, (context.tenant_id, context.actor_id))
    member.role = "tenant_admin"
    session.flush()
    other = create_context(session, role="tenant_admin")
    create_version(
        session,
        context=other,
        body=KnowledgeInput(
            key="private", title="赔付规则", body="other secret", source="private"
        ),
    )
    strategy = seed_optimizer_dependencies(session, context)
    optimizer = service.create_optimizer(
        session,
        context=context,
        body=OptimizerCreate.model_validate(create_body(strategy.id)),
    )
    task = create_prepared_review_task(
        session, context=context, optimizer_id=optimizer.id, request_id=uuid4()
    )
    assert search_for_task(session, task=task, query="赔付")["items"] == []


def test_search_beyond_initial_catalog_and_read_budget(session, context):
    from datetime import UTC, datetime, timedelta

    from app.modules.ai_workbench.knowledge_search import read_for_task, search_for_task

    member = session.get(TenantMembership, (context.tenant_id, context.actor_id))
    member.role = "tenant_admin"
    session.flush()
    strategy = seed_optimizer_dependencies(session, context)
    target = create_version(
        session,
        context=context,
        body=KnowledgeInput(
            key="old-policy",
            title="赔付政策",
            body="条款" * 6000,
            source="official",
            tags=["policy"],
            expires_at=datetime.now(UTC) - timedelta(days=1),
        ),
    )
    for i in range(22):
        create_version(
            session,
            context=context,
            body=KnowledgeInput(
                key=f"new-{i}",
                title=f"普通经验{i}",
                body="普通内容",
                source="test",
                tags=["experience"],
            ),
        )
    optimizer = service.create_optimizer(
        session,
        context=context,
        body=OptimizerCreate.model_validate(create_body(strategy.id)),
    )
    task = create_prepared_review_task(
        session, context=context, optimizer_id=optimizer.id, request_id=uuid4()
    )
    assert "knowledge_catalog" not in task.evidence
    found = search_for_task(session, task=task, query="赔付")
    assert found["items"][0]["eligibility"] == "EXPIRED"
    frozen = read_for_task(session, task=task, knowledge_id=target.id)
    assert frozen["body"] == target.body
    assert read_for_task(session, task=task, knowledge_id=target.id) == frozen
    from app.modules.ai_workbench.review_store import delivered_evidence_ids

    assert str(target.id) in delivered_evidence_ids(session, task)
    assert task.evidence["knowledge"] == []
    assert read_for_task(session, task=task, knowledge_id=target.id) == frozen


def test_bc_retrieval_pagination_retirement_and_cancellation(
    client, session, context, monkeypatch
):
    from app.modules.accounts.models import TenantBC
    from app.modules.ai_workbench.knowledge_search import search_for_task
    from tests.modules.ai_workbench.test_optimizer import api_headers

    member = session.get(TenantMembership, (context.tenant_id, context.actor_id))
    member.role = "tenant_admin"
    session.flush()
    strategy = seed_optimizer_dependencies(session, context)
    session.add(TenantBC(tenant_id=context.tenant_id, bc_id="bc-b"))
    session.flush()
    create_version(
        session,
        context=context,
        body=KnowledgeInput(
            key="other-bc",
            scope="BC",
            bc_id="bc-b",
            title="赔付",
            body="不能读取",
            source="private",
        ),
    )
    original = create_version(
        session,
        context=context,
        body=KnowledgeInput(key="retire", title="赔付", body="旧版本", source="test"),
    )
    create_version(
        session,
        context=context,
        asset_id=original.asset_id,
        body=KnowledgeInput(
            key="retire", title="赔付", body="已退休", source="test", status="RETIRED"
        ),
    )
    for i in range(12):
        create_version(
            session,
            context=context,
            body=KnowledgeInput(
                key=f"p-{i}", title="赔付", body=f"条款{i}", source="test"
            ),
        )
    optimizer = service.create_optimizer(
        session,
        context=context,
        body=OptimizerCreate.model_validate(create_body(strategy.id)),
    )
    task = create_prepared_review_task(
        session, context=context, optimizer_id=optimizer.id, request_id=uuid4()
    )
    first = search_for_task(session, task=task, query="赔付")
    second = search_for_task(
        session, task=task, query="赔付", offset=first["next_offset"]
    )
    assert len(first["items"]) == 10 and len(second["items"]) == 2
    assert not {r["id"] for r in first["items"]} & {r["id"] for r in second["items"]}
    assert not search_for_task(session, task=task, query="%")["items"]
    monkeypatch.setattr(settings, "AI_DSH_SERVICE_TOKEN", "x" * 40)
    claim = client.post(
        "/api/integrations/ai-workbench/reviews/claim",
        headers={"Authorization": "Bearer " + settings.AI_DSH_SERVICE_TOKEN},
        json=plugin_methods(),
    ).json()
    headers = {"Authorization": "Bearer " + claim["task_token"]}
    prefix = f"/api/integrations/ai-workbench/reviews/{task.id}"
    assert (
        client.post(
            prefix + "/knowledge/read",
            headers=headers,
            json={"knowledge_id": str(original.id)},
        ).status_code
        == 404
    )
    assert (
        client.post(
            prefix + "/knowledge/search",
            headers=headers,
            json={"query": "赔付", "tenant_id": str(uuid4())},
        ).status_code
        == 422
    )
    other_task = create_review_task(
        session, context=context, optimizer_id=optimizer.id, request_id=uuid4()
    )
    assert (
        client.post(
            f"/api/integrations/ai-workbench/reviews/{other_task.id}/knowledge/search",
            headers=headers,
            json={"query": "赔付"},
        ).status_code
        == 403
    )
    cancel = client.post(
        f"/api/tenants/{context.tenant_id}/ai-optimizers/{optimizer.id}/reviews/{task.id}/cancel",
        headers=api_headers(context, session=session),
        json={},
    )
    assert cancel.status_code == 200
    assert (
        client.post(
            prefix + "/knowledge/search", headers=headers, json={"query": "赔付"}
        ).status_code
        == 409
    )
