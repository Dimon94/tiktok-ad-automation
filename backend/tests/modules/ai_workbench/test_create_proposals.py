"""真实领域服务验证创建提案的确认、绑定和账户边界。"""

from uuid import uuid4

import pytest

from app.core.errors import DomainError
from app.modules.ai_workbench import service
from app.modules.ai_workbench.proposals import (
    ProposalInput,
    accept_proposal,
    create_proposal,
    publish_auto_proposals,
)
from app.modules.ai_workbench.reviews import create_review_task
from app.modules.ai_workbench.schemas import OptimizerCreate
from app.modules.automation_rules.models import AutomationRule
from tests.modules.ai_workbench.test_optimizer import (
    create_body,
    seed_optimizer_dependencies,
)
from tests.modules.automation_rules.test_definition import base


@pytest.mark.parametrize("kind", ["strategy", "rule"])
@pytest.mark.parametrize("mode", ["MANUAL", "AUTO"])
def test_create_proposal_uses_domain_service_and_binds_result(
    session, context, kind, mode
):
    strategy = seed_optimizer_dependencies(session, context)
    optimizer = service.create_optimizer(
        session,
        context=context,
        body=OptimizerCreate.model_validate(
            create_body(strategy.id, optimization_mode=mode)
        ),
    )
    task = create_review_task(
        session, context=context, optimizer_id=optimizer.id, request_id=uuid4()
    )
    after = (
        {"name": "AI 新配置", "config": strategy.config}
        if kind == "strategy"
        else {
            "name": "AI 新规则",
            "definition": base(
                accounts={"mode": "ACCOUNTS", "advertiser_ids": ["acc-1"]}
            ),
        }
    )
    proposal = create_proposal(
        session,
        context=context,
        optimizer_id=optimizer.id,
        review_task=task,
        change=ProposalInput(
            kind=kind,
            operation="create",
            after=after,
            problem="低效消耗增加",
            rationale="历史比较显示可减少低效投放",
            expected_effect="降低消耗，但有误关风险",
            validation="观察七天，误关超过8%撤回",
        ),
    )
    assert proposal.status == "PENDING"
    assert proposal.evidence["problem"] == "低效消耗增加"
    assert proposal.evidence["rationale"] == "历史比较显示可减少低效投放"
    assert proposal.evidence["expected_effect"] == "降低消耗，但有误关风险"
    assert proposal.evidence["validation"] == "观察七天，误关超过8%撤回"
    if mode == "AUTO":
        publish_auto_proposals(
            session, context=context, optimizer_id=optimizer.id, review_task_id=task.id
        )
    else:
        assert (
            publish_auto_proposals(
                session,
                context=context,
                optimizer_id=optimizer.id,
                review_task_id=task.id,
            )
            == []
        )
        accept_proposal(
            session, context=context, optimizer_id=optimizer.id, proposal_id=proposal.id
        )
    assert proposal.status == "ACCEPTED"
    current = service.get_optimizer(session, context=context, optimizer_id=optimizer.id)
    if kind == "strategy":
        assert current.strategy_version_id != strategy.id
        assert current.strategy_version_id == proposal.published_version_id
    else:
        assert proposal.published_version_id in current.rule_ids
        assert session.get(AutomationRule, proposal.published_version_id).definition[
            "accounts"
        ]["advertiser_ids"] == ["acc-1"]


def test_create_rule_cannot_expand_to_entire_bc(session, context):
    strategy = seed_optimizer_dependencies(session, context)
    optimizer = service.create_optimizer(
        session,
        context=context,
        body=OptimizerCreate.model_validate(create_body(strategy.id)),
    )
    task = create_review_task(
        session, context=context, optimizer_id=optimizer.id, request_id=uuid4()
    )
    with pytest.raises(DomainError, match="管理账户"):
        create_proposal(
            session,
            context=context,
            optimizer_id=optimizer.id,
            review_task=task,
            change=ProposalInput(
                kind="rule",
                operation="create",
                after={
                    "name": "broad",
                    "definition": base(accounts={"mode": "BC", "bc_ids": ["bc-a"]}),
                },
            ),
        )


def test_historical_new_rule_reads_exact_review_explanations_without_mutation(
    session, context
):
    from app.modules.ai_workbench.proposals import (
        _public,
        list_proposals,
        public_proposal,
    )

    strategy = seed_optimizer_dependencies(session, context)
    optimizer = service.create_optimizer(
        session,
        context=context,
        body=OptimizerCreate.model_validate(create_body(strategy.id)),
    )
    task = create_review_task(
        session, context=context, optimizer_id=optimizer.id, request_id=uuid4()
    )
    change = ProposalInput(
        kind="rule",
        operation="create",
        after={
            "name": "历史观察规则",
            "enabled": False,
            "definition": base(
                accounts={"mode": "ACCOUNTS", "advertiser_ids": ["acc-1"]}
            ),
        },
        problem="问题原文",
        rationale="依据原文",
        expected_effect="效果原文",
        validation="条件原文",
    )
    row = create_proposal(
        session,
        context=context,
        optimizer_id=optimizer.id,
        review_task=task,
        change=change,
    )
    # 精确模拟旧新增分支漏存四项说明，但原始复盘完整保存的生产形态。
    row.evidence = {"review_task_id": str(task.id), "evidence_ids": []}
    task.result = {"proposals": [change.model_dump(mode="json")]}
    session.add_all([row, task])
    session.flush()
    public = list_proposals(session, context=context, optimizer_id=optimizer.id)[0]
    assert public["problem"] == "问题原文"
    assert public_proposal(session, row)["validation"] == "条件原文"
    assert public["rationale"] == "依据原文"
    assert public["expected_effect"] == "效果原文"
    assert "problem" not in row.evidence
    assert row.status == "PENDING" and row.after["enabled"] is False
    # 配置不同、重复歧义、操作不同都不能套用另一条建议的说明。
    original = change.model_dump(mode="json")
    for wrong in [
        {**original, "after": {**original["after"], "name": "另一规则"}},
        {**original, "operation": "update"},
        {**original, "target_id": str(uuid4())},
    ]:
        assert _public(row, review_result={"proposals": [wrong]})["problem"] is None
    assert (
        _public(row, review_result={"proposals": [original, original]})["problem"]
        is None
    )
