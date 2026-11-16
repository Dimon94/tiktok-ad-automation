"""复盘产品合同：历史分页、结构报告和可执行规则确认。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlmodel import select

from app.core.config import settings
from app.modules.ai_workbench import reviews, service
from app.modules.ai_workbench.models import AIOptimizerRuleBinding
from app.modules.ai_workbench.schemas import OptimizerCreate
from tests.modules.ai_workbench.review_helpers import prepare_task
from tests.modules.ai_workbench.test_optimizer import (
    api_headers,
    create_body,
    seed_optimizer_dependencies,
)
from tests.modules.ai_workbench.test_review_methods import plugin_methods


def report(status="INSUFFICIENT_EVIDENCE", evidence_ids=()):
    return {
        "headline": "先补齐成熟数据，再决定是否调整控损",
        "verdict": "本轮证据不足，保留当前规则。",
        "sections": [
            {
                "title": "数据覆盖",
                "body": "缺失不计零。",
                "evidence_ids": list(evidence_ids),
            }
        ],
        "rule_decision": {"status": status, "reason": "需要完整观察窗口和候选回测。"},
    }


def optimizer_setup(session, context):
    from app.modules.automation_rules.models import AutomationRule
    from tests.modules.automation_rules.test_definition import base

    strategy = seed_optimizer_dependencies(session, context)
    rule = AutomationRule(
        tenant_id=context.tenant_id,
        name="原规则名称",
        enabled=False,
        definition=base(accounts={"mode": "ACCOUNTS", "advertiser_ids": ["acc-1"]}),
        created_by=context.actor_id,
        updated_by=context.actor_id,
    )
    session.add(rule)
    session.flush()
    optimizer = service.create_optimizer(
        session,
        context=context,
        body=OptimizerCreate.model_validate(
            create_body(
                strategy.id,
                optimization_mode="MANUAL",
                authorization_limits={"strategy": False, "rule": False},
                rule_ids=[str(rule.id)],
            )
        ),
    )
    return optimizer


def test_history_pages_beyond_50_and_is_lightweight(client, session, context):
    optimizer = optimizer_setup(session, context)
    now = datetime.now(UTC)
    for i in range(101):
        session.add(
            reviews.DSHReviewTask(
                tenant_id=context.tenant_id,
                optimizer_id=optimizer.id,
                optimizer_version=1,
                request_id=uuid4(),
                created_by=context.actor_id,
                dsh_task_id=f"history:{i}",
                fence=str(uuid4()),
                authorization_expires_at=now,
                created_at=now - timedelta(minutes=i),
                status="COMPLETE" if i else "PREPARING",
                evidence={"preparation": {"total_accounts": 1}},
                result={
                    "summary": "旧报告\n\n" + "细节" * 10000,
                    "proposals": [],
                    "recommendations": [],
                },
            )
        )
    session.flush()
    base = f"/api/tenants/{context.tenant_id}/ai-optimizers/{optimizer.id}"
    headers = api_headers(context, session=session)
    page = client.get(base + "/reviews?offset=90&limit=10", headers=headers)
    assert page.status_code == 200, page.text
    data = page.json()
    assert data["total"] == 101
    assert len(data["items"]) == 10
    assert data["has_more"] is True
    assert data["active_task"]["status"] == "PREPARING"
    assert all(
        "result" not in item and len(item["summary"]) <= 240 for item in data["items"]
    )
    last = client.get(base + "/reviews?offset=100&limit=10", headers=headers).json()
    assert len(last["items"]) == 1 and last["has_more"] is False
    filtered = client.get(
        base + "/reviews?status=COMPLETE&limit=1", headers=headers
    ).json()
    assert filtered["total"] == 100 and filtered["items"][0]["status"] == "COMPLETE"
    # 活动任务不是所选历史页的一部分，也要返回供页面阻止重复发起。
    assert filtered["active_task"] is not None
    detail = client.get(
        base + "/reviews/" + last["items"][0]["id"], headers=headers
    ).json()
    assert len(detail["result"]["summary"]) > 10000
    assert detail["review_context"]["proposal_submission_allowed"] is True
    assert client.get(base + "/reviews?limit=101", headers=headers).status_code == 422
    assert client.get(base + "/reviews?offset=-1", headers=headers).status_code == 422


def test_manual_contract_does_not_forbid_candidates_and_requires_report(
    client, session, context, monkeypatch
):
    optimizer = optimizer_setup(session, context)
    task = prepare_task(
        session,
        reviews.create_review_task(
            session, context=context, optimizer_id=optimizer.id, request_id=uuid4()
        ),
    )
    monkeypatch.setattr(settings, "AI_DSH_SERVICE_TOKEN", "test-machine-" + "x" * 40)
    claimed = client.post(
        "/api/integrations/ai-workbench/reviews/claim",
        headers={"Authorization": "Bearer " + settings.AI_DSH_SERVICE_TOKEN},
        json=plugin_methods(),
    ).json()
    evidence = claimed["task"]["evidence"]
    assert "authorization_limits" not in evidence["optimizer"]
    assert evidence["capabilities"]["proposal_submission_allowed"] is True
    assert evidence["capabilities"]["publication_mode"] == "HUMAN_CONFIRMATION"
    base = f"/api/integrations/ai-workbench/reviews/{task.id}"
    headers = {"Authorization": "Bearer " + claimed["task_token"]}
    assert (
        client.post(
            base + "/complete",
            headers=headers,
            json={"summary": "只有大段文字", "proposals": []},
        ).status_code
        == 422
    )
    payload = {"summary": "简短概述", "report": report(), "proposals": []}
    # 报告的证据引用必须真的读过，不能借结构化字段绕过已交付校验。
    unread = {**payload, "report": report(evidence_ids=[str(uuid4())])}
    assert (
        client.post(base + "/complete", headers=headers, json=unread).status_code == 404
    )
    unread_executive = {**payload, "report": report()}
    unread_executive["report"]["executive"] = {
        "assessment": "消耗上升，回收不足",
        "drivers": [
            {
                "finding": "高消耗账户拖累",
                "impact": "需要核对金额",
                "action": "检查账户",
                "evidence_ids": [str(uuid4())],
            }
        ],
        "next_review": "下一完整日复核",
    }
    assert (
        client.post(
            base + "/complete", headers=headers, json=unread_executive
        ).status_code
        == 404
    )
    contradictory = {**payload, "report": report("PROPOSED")}
    assert (
        client.post(base + "/complete", headers=headers, json=contradictory).status_code
        == 422
    )
    accepted = client.post(base + "/complete", headers=headers, json=payload)
    assert accepted.status_code == 200, accepted.text
    assert (
        session.get(reviews.DSHReviewTask, task.id).result["report"]["rule_decision"][
            "status"
        ]
        == "INSUFFICIENT_EVIDENCE"
    )


def test_rule_candidate_accept_publishes_actual_rule_and_reject_does_not(
    client, session, context
):
    from app.modules.automation_rules.models import AutomationRule

    optimizer = optimizer_setup(session, context)
    binding = session.exec(
        select(AIOptimizerRuleBinding).where(
            AIOptimizerRuleBinding.optimizer_id == optimizer.id
        )
    ).first()
    assert binding is not None
    rule = session.get(AutomationRule, binding.rule_id)
    original_version = rule.version
    original_enabled = rule.enabled
    base = f"/api/tenants/{context.tenant_id}/ai-optimizers/{optimizer.id}"
    headers = api_headers(context, session=session)

    def candidate(name):
        created = client.post(
            base + "/reviews",
            headers=headers,
            json={
                "request_id": str(uuid4()),
                "proposals": [
                    {
                        "kind": "rule",
                        "target_id": str(rule.id),
                        "after": {
                            "name": name,
                            "enabled": original_enabled,
                            "definition": rule.definition,
                        },
                        "rationale": "测试人工确认发布",
                        "problem": "规则名称不清楚",
                        "expected_effect": "可追溯的规则名称",
                        "validation": "规则版本递增且定义保持",
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        return created.json()["proposals"][0]

    rejected = candidate("未采纳名称")
    assert (
        client.post(
            base + "/proposals/" + rejected["id"] + "/reject",
            headers=headers,
            json={"request_id": str(uuid4()), "reason": "保留现状"},
        ).json()["status"]
        == "REJECTED"
    )
    session.refresh(rule)
    assert rule.version == original_version and rule.name != "未采纳名称"
    proposed = candidate("人工确认后的规则名称")
    assert proposed["status"] == "PENDING"
    assert rule.version == original_version
    decision = {"request_id": str(uuid4())}
    response = client.post(
        base + "/proposals/" + proposed["id"] + "/accept",
        headers=headers,
        json=decision,
    )
    assert response.status_code == 200, response.text
    assert (
        response.json()["status"] == "ACCEPTED"
        and response.json()["published_version_id"]
    )
    session.refresh(rule)
    assert rule.version == original_version + 1 and rule.name == "人工确认后的规则名称"
    assert rule.enabled == original_enabled
    assert (
        client.post(
            base + "/proposals/" + proposed["id"] + "/accept",
            headers=headers,
            json=decision,
        ).status_code
        == 200
    )
    session.refresh(rule)
    assert rule.version == original_version + 1
    page = client.get(
        base
        + "/proposals?limit=1&offset=0&review_task_id="
        + proposed["review_task_id"],
        headers=headers,
    ).json()
    assert len(page) == 1 and page[0]["id"] == proposed["id"]
    assert (
        client.get(
            base + "/proposals?offset=1&review_task_id=" + proposed["review_task_id"],
            headers=headers,
        ).json()
        == []
    )
    assert client.get(base + "/proposals?limit=101", headers=headers).status_code == 422


def test_review_pages_and_detail_never_cross_tenant_or_optimizer(
    client, session, context, other_context
):
    first = optimizer_setup(session, context)
    other = optimizer_setup(session, other_context)
    task = reviews.create_review_task(
        session, context=other_context, optimizer_id=other.id, request_id=uuid4()
    )
    base = f"/api/tenants/{context.tenant_id}/ai-optimizers/{first.id}"
    headers = api_headers(context, session=session)
    page = client.get(base + "/reviews", headers=headers).json()
    assert page["items"] == [] and page["active_task"] is None and page["total"] == 0
    assert (
        client.get(base + "/reviews/" + str(task.id), headers=headers).status_code
        == 404
    )
    assert (
        client.get(
            base + "/proposals?review_task_id=" + str(task.id), headers=headers
        ).json()
        == []
    )
    foreign = f"/api/tenants/{context.tenant_id}/ai-optimizers/{other.id}"
    assert client.get(foreign + "/reviews", headers=headers).status_code == 404


def test_runtime_rule_proposal_is_pending_until_explicit_accept(
    client, session, context, monkeypatch
):
    from app.modules.automation_rules.models import AutomationRule

    optimizer = optimizer_setup(session, context)
    task = prepare_task(
        session,
        reviews.create_review_task(
            session, context=context, optimizer_id=optimizer.id, request_id=uuid4()
        ),
    )
    rule_data = task.evidence["rules"][0]
    rule = session.get(AutomationRule, rule_data["id"])
    before_version = rule.version
    monkeypatch.setattr(settings, "AI_DSH_SERVICE_TOKEN", "test-machine-" + "x" * 40)
    claim = client.post(
        "/api/integrations/ai-workbench/reviews/claim",
        headers={"Authorization": "Bearer " + settings.AI_DSH_SERVICE_TOKEN},
        json=plugin_methods(),
    ).json()
    base = f"/api/integrations/ai-workbench/reviews/{task.id}"
    headers = {"Authorization": "Bearer " + claim["task_token"]}
    sources = client.post(
        base + "/evidence/query", headers=headers, json={"category": "rule_evidence"}
    ).json()["items"]
    read = client.post(
        base + "/evidence/read", headers=headers, json={"evidence_id": sources[0]["id"]}
    )
    assert read.status_code == 200, read.text
    cited = read.json()["id"]
    # 本地构造配置验证发布链路；不是以无执行数据证明生产阈值应当修改。
    change = {
        "kind": "rule",
        "operation": "update",
        "target_id": rule_data["id"],
        "after": {
            "name": "已核对的规则名称",
            "enabled": False,
            "definition": rule.definition,
        },
        "rationale": "命名修正",
        "problem": "规则名称需明确",
        "expected_effect": "规则名称可读",
        "validation": "读取实际规则确认新版本与名称，保持禁用",
        "evidence_ids": [cited],
    }
    result = client.post(
        base + "/complete",
        headers=headers,
        json={
            "summary": "候选已提交待确认",
            "report": report("PROPOSED", [cited]),
            "proposals": [change],
        },
    )
    assert result.status_code == 200, result.text
    session.refresh(rule)
    assert rule.version == before_version and rule.enabled is False
    ui_base = f"/api/tenants/{context.tenant_id}/ai-optimizers/{optimizer.id}"
    user_headers = api_headers(context, session=session)
    pending = client.get(
        ui_base + "/proposals?status=PENDING&review_task_id=" + str(task.id),
        headers=user_headers,
    ).json()
    assert len(pending) == 1 and pending[0]["after"]["name"] == "已核对的规则名称"
    accepted = client.post(
        ui_base + "/proposals/" + pending[0]["id"] + "/accept",
        headers=user_headers,
        json={"request_id": str(uuid4())},
    )
    assert accepted.status_code == 200, accepted.text
    session.refresh(rule)
    assert (
        rule.name == "已核对的规则名称"
        and rule.version == before_version + 1
        and rule.enabled is False
    )
