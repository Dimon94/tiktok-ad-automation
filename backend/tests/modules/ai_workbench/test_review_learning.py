from uuid import uuid4

from app.modules.ai_workbench import service
from app.modules.ai_workbench.proposals import (
    ProposalInput,
    accept_proposal,
    create_proposal,
)
from app.modules.ai_workbench.schemas import OptimizerCreate
from tests.modules.ai_workbench.review_helpers import (
    create_prepared_review_task,
    source_store,
)
from tests.modules.ai_workbench.test_optimizer import (
    create_body,
    seed_optimizer_dependencies,
)


def test_second_round_tracks_decision_and_does_not_call_unexecuted_strategy_failure(
    session, context
):
    strategy = seed_optimizer_dependencies(session, context)
    optimizer = service.create_optimizer(
        session,
        context=context,
        body=OptimizerCreate.model_validate(create_body(strategy.id)),
    )
    first = create_prepared_review_task(
        session, context=context, optimizer_id=optimizer.id, request_id=uuid4()
    )
    first.status = "COMPLETE"
    first.result = {
        "summary": "先观察起量",
        "recommendations": [{"problem": "供给不足", "suggestion": "准备素材"}],
    }
    proposal = create_proposal(
        session,
        context=context,
        optimizer_id=optimizer.id,
        review_task=first,
        change=ProposalInput(
            kind="strategy",
            after={"config": {**strategy.config, "budget": "12"}},
            problem="起量不足",
            expected_effect="提高测试覆盖",
            validation="七个完整日后核验",
            rationale="调整预算",
        ),
    )
    # 页面使用公开提案契约；旧记录的说明只存 evidence，也必须完整返回。
    from app.modules.ai_workbench.proposals import list_proposals

    public = list_proposals(session, context=context, optimizer_id=optimizer.id)[0]
    for name, expected in {
        "problem": "起量不足",
        "rationale": "调整预算",
        "expected_effect": "提高测试覆盖",
        "validation": "七个完整日后核验",
    }.items():
        assert public[name] == expected == public["evidence"][name]

    accept_proposal(
        session,
        context=context,
        optimizer_id=optimizer.id,
        proposal_id=proposal.id,
        request_id=uuid4(),
    )
    second = create_prepared_review_task(
        session, context=context, optimizer_id=optimizer.id, request_id=uuid4()
    )
    history = next(
        r["payload"]
        for r in source_store(session, second).values()
        if r["category"] == "history"
    )
    assert history["reviews"][0]["summary"] == "先观察起量"
    feedback = history["proposals"][0]
    assert feedback["status"] == "ACCEPTED"
    assert feedback["published_version_id"]
    assert feedback["problem"] == "起量不足"
    assert feedback["execution_status"] == "NOT_EXECUTED"
    assert feedback["assessment"] == "INSUFFICIENT_EVIDENCE"
    assert feedback["incremental_profit"] is None
    assert second.evidence["capabilities"]["strategy"]["schema"]["properties"]["budget"]


def test_candidate_schema_prevalidation_does_not_publish(session, context):
    from app.modules.ai_workbench.capabilities import validate_candidate
    from app.modules.ai_workbench.reviews import load_optimizer

    strategy = seed_optimizer_dependencies(session, context)
    optimizer = service.create_optimizer(
        session,
        context=context,
        body=OptimizerCreate.model_validate(create_body(strategy.id)),
    )
    _, version = load_optimizer(session, context, optimizer.id)
    result = validate_candidate(
        session,
        context=context,
        optimizer_id=optimizer.id,
        kind="strategy",
        after={"config": {**strategy.config, "budget": "-1"}},
    )
    assert result["valid"] is False
    result = validate_candidate(
        session,
        context=context,
        optimizer_id=optimizer.id,
        kind="strategy",
        after={"config": {**strategy.config, "budget": "12"}},
    )
    assert result["valid"] is True
    assert load_optimizer(session, context, optimizer.id)[1].number == version.number
