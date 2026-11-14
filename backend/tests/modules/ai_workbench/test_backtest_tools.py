from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from app.core.config import settings
from app.modules.ai_workbench import service
from app.modules.ai_workbench.evidence import RuleEvidenceSnapshot
from app.modules.ai_workbench.review_store import delivered_evidence_ids
from app.modules.ai_workbench.schemas import OptimizerCreate
from app.modules.automation_rules.evaluate import evaluate
from app.modules.automation_rules.inputs import AccountInfo, Inputs
from app.modules.automation_rules.models import AutomationRule, AutomationRuleRun
from tests.modules.ai_workbench.review_helpers import (
    create_prepared_review_task,
    source_index,
    source_store,
)
from tests.modules.ai_workbench.test_optimizer import (
    create_body,
    seed_optimizer_dependencies,
)
from tests.modules.ai_workbench.test_review_methods import plugin_methods
from tests.modules.automation_rules.test_evaluate import ent, rule


def test_agent_can_replay_frozen_nonhits_cite_result_and_cannot_read_foreign_history(
    client, session, context, monkeypatch
):
    strategy = seed_optimizer_dependencies(session, context)
    definition = rule(accounts={"mode": "ACCOUNTS", "advertiser_ids": ["acc-1"]})
    owner = AutomationRule(
        tenant_id=context.tenant_id,
        name="history",
        definition=definition.model_dump(mode="json"),
        created_by=context.actor_id,
        updated_by=context.actor_id,
    )
    session.add(owner)
    session.flush()
    optimizer = service.create_optimizer(
        session,
        context=context,
        body=OptimizerCreate.model_validate(
            create_body(strategy.id, rule_ids=[str(owner.id)])
        ),
    )
    entities = [ent("adgroup", "acc-1", "g1"), ent("adgroup", "acc-1", "g2")]
    facts = Inputs(
        accounts={"acc-1": AccountInfo("acc-1", "bc-a", "UTC", "USD", "A")},
        entities={e.key: e for e in entities},
        facts={
            "TODAY": {
                entities[0].key: {"spend": Decimal("20")},
                entities[1].key: {"spend": Decimal("5")},
            }
        },
    )
    run = AutomationRuleRun(
        tenant_id=context.tenant_id,
        optimizer_id=optimizer.id,
        rule_id=owner.id,
        rule_version=1,
        definition=owner.definition,
        slot_at=datetime.now(UTC),
    )
    session.add(run)
    session.flush()
    from app.modules.ai_workbench.rule_binding import resolve_rule_binding

    scope = resolve_rule_binding(
        session,
        tenant_id=context.tenant_id,
        optimizer_id=optimizer.id,
        rule_id=owner.id,
    )
    evidence = RuleEvidenceSnapshot.from_evaluation(
        rule=owner,
        run=run,
        result=evaluate(definition, facts),
        skipped_accounts=[],
        scope=scope,
        inputs=facts,
    ).persist(session, run_id=run.id)
    task = create_prepared_review_task(
        session, context=context, optimizer_id=optimizer.id, request_id=uuid4()
    )
    assert (
        next(
            r["payload"]
            for r in source_store(session, task).values()
            if r["category"] == "rule_evidence"
        )[0]["historical_replay"]["replay"]["current"]["nonhit_count"]
        == 1
    )
    monkeypatch.setattr(settings, "AI_DSH_SERVICE_TOKEN", "local-test-" + "x" * 40)
    claim = client.post(
        "/api/integrations/ai-workbench/reviews/claim",
        headers={"Authorization": "Bearer " + settings.AI_DSH_SERVICE_TOKEN},
        json=plugin_methods(),
    ).json()
    headers = {"Authorization": "Bearer " + claim["task_token"]}
    path = f"/api/integrations/ai-workbench/reviews/{task.id}"
    candidate = definition.model_dump(mode="json")
    candidate["conditions"][0]["values"] = ["3"]
    payload = {"evidence_id": str(evidence.id), "candidate_definition": candidate}
    assert (
        client.post(path + "/rules/backtest", headers=headers, json=payload).status_code
        == 404
    )
    from app.modules.ai_workbench.evidence import referenced_evidence_ids

    assert evidence.id in referenced_evidence_ids(session, tenant_id=context.tenant_id)
    source = next(
        r for r in source_index(session, task) if r["category"] == "rule_evidence"
    )
    assert (
        client.post(
            path + "/evidence/read", headers=headers, json={"evidence_id": source["id"]}
        ).status_code
        == 200
    )
    response = client.post(path + "/rules/backtest", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["result"]["replay"]["comparison"]["added_hit_subjects"] == [
        "adgroup:acc-1:g2"
    ]
    assert result["id"] in delivered_evidence_ids(session, task)
    assert (
        client.post(path + "/rules/backtest", headers=headers, json=payload).json()
        == result
    )
    from app.modules.ai_workbench.review_store import ReviewArtifact, scoped

    assert (
        len(
            session.exec(
                scoped(ReviewArtifact, task).where(
                    ReviewArtifact.kind == "rule_backtest"
                )
            ).all()
        )
        == 1
    )
    assert "rule_backtests" not in task.evidence
    assert (
        client.post(
            path + "/rules/backtest",
            headers=headers,
            json={**payload, "evidence_id": str(uuid4())},
        ).status_code
        == 404
    )
    completed = client.post(
        path + "/complete",
        headers=headers,
        json={
            "summary": "回测新增命中一个对象，不能推定节省",
            "proposals": [],
            "recommendations": [
                {
                    "problem": "阈值覆盖",
                    "suggestion": "人工审核候选阈值",
                    "evidence_ids": [result["id"]],
                }
            ],
        },
    )
    assert completed.status_code == 200, completed.text
    assert (
        client.post(path + "/rules/backtest", headers=headers, json=payload).status_code
        == 409
    )
