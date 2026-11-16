from uuid import uuid4

from app.modules.ai_workbench import service
from app.modules.ai_workbench.reviews import frozen_evidence_ids
from app.modules.ai_workbench.schemas import OptimizerCreate
from tests.modules.ai_workbench.review_helpers import (
    create_prepared_review_task,
    minimal_report,
)
from tests.modules.ai_workbench.test_optimizer import (
    create_body,
    seed_optimizer_dependencies,
)


def test_business_task_does_not_load_skill_files(session, context):
    strategy = seed_optimizer_dependencies(session, context)
    optimizer = service.create_optimizer(
        session,
        context=context,
        body=OptimizerCreate.model_validate(create_body(strategy.id)),
    )
    task = create_prepared_review_task(
        session, context=context, optimizer_id=optimizer.id, request_id=uuid4()
    )
    assert "methods" not in task.evidence
    assert task.evidence["knowledge_selection"]


def test_evidence_accepts_frozen_history_and_scoped_business_not_arbitrary_ids():
    review, proposal, run, export, arbitrary = (uuid4() for _ in range(5))
    evidence = {
        "history": {
            "reviews": [{"id": str(review)}],
            "proposals": [{"id": str(proposal)}],
        },
        "data_pack": {
            "business": [{"rows": [{"run_id": str(run), "drama_id": str(arbitrary)}]}],
            "operation_events": [{"rows": [{"export_id": str(export)}]}],
        },
    }
    assert frozen_evidence_ids(evidence) == {
        str(review),
        str(proposal),
        str(run),
        str(export),
    }


def test_claim_binds_plugin_methods_and_retry_keeps_old_version(
    client, session, context, monkeypatch
):
    from datetime import UTC, datetime, timedelta
    from hashlib import sha256

    from app.core.config import settings
    from app.modules.ai_workbench.reviews import DSHReviewTask
    from tests.modules.ai_workbench.test_review_methods import plugin_methods

    monkeypatch.setattr(settings, "AI_DSH_SERVICE_TOKEN", "test-" + "x" * 40)
    strategy = seed_optimizer_dependencies(session, context)
    optimizer = service.create_optimizer(
        session,
        context=context,
        body=OptimizerCreate.model_validate(create_body(strategy.id)),
    )
    task = create_prepared_review_task(
        session, context=context, optimizer_id=optimizer.id, request_id=uuid4()
    )
    machine = {"Authorization": "Bearer " + settings.AI_DSH_SERVICE_TOKEN}
    frozen_business = task.evidence.copy()
    methods = plugin_methods()
    first = client.post(
        "/api/integrations/ai-workbench/reviews/claim", headers=machine, json=methods
    )
    assert first.status_code == 200, first.text
    assert first.json()["task"]["evidence"]["methods"] == methods
    assert {
        k: v
        for k, v in first.json()["task"]["evidence"].items()
        if k not in {"methods", "source_catalog"}
    } == frozen_business
    assert any(
        row["category"] == "reporting" and row["source_count"] > 0
        for row in first.json()["task"]["evidence"]["source_catalog"]
    )
    task = session.get(DSHReviewTask, task.id)
    assert "source_catalog" not in task.evidence
    task.authorization_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.flush()
    changed = plugin_methods()
    changed["main"]["body"] += "\nnew release"
    changed["main"]["version"] = "2.0.0"
    changed["main"]["sha256"] = sha256(changed["main"]["body"].encode()).hexdigest()
    retry = client.post(
        "/api/integrations/ai-workbench/reviews/claim", headers=machine, json=changed
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["task"]["evidence"]["methods"] == methods
    assert retry.json()["task"]["fence"] != first.json()["task"]["fence"]
    body = {
        "report": minimal_report(),
        "summary": "loaded frozen review skill",
        "proposals": [],
        "method_usage": ["ai-delivery-review", "scale"],
    }
    done = client.post(
        "/api/integrations/ai-workbench/reviews/" + str(task.id) + "/complete",
        headers={"Authorization": "Bearer " + retry.json()["task_token"]},
        json=body,
    )
    assert done.status_code == 200, done.text
    assert session.get(DSHReviewTask, task.id).result["method_usage"] == [
        "ai-delivery-review",
        "scale",
    ]
