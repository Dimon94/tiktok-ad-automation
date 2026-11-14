"""真实任务短授权续签；不能借心跳延长已撤销或终态任务。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt

from app.core import security
from app.core.config import settings
from app.modules.ai_workbench import reviews, service
from app.modules.ai_workbench.schemas import OptimizerCreate
from tests.modules.ai_workbench.test_optimizer import (
    create_body,
    seed_optimizer_dependencies,
)
from tests.modules.ai_workbench.test_review_methods import plugin_methods


def test_heartbeat_renews_task_token_and_checks_scope_and_terminal_state(
    client, session, context, monkeypatch
):
    monkeypatch.setattr(
        settings, "AI_DSH_SERVICE_TOKEN", "runtime-renewal-test-" + "x" * 32
    )
    strategy = seed_optimizer_dependencies(session, context)
    optimizer = service.create_optimizer(
        session,
        context=context,
        body=OptimizerCreate.model_validate(create_body(strategy.id)),
    )
    task = reviews.create_review_task(
        session, context=context, optimizer_id=optimizer.id, request_id=uuid4()
    )
    # 此例只验证领取/心跳授权；准备状态机在独立真实PG套件验证。
    task.status = "PENDING"
    session.add(task)
    session.flush()
    response = client.post(
        "/api/integrations/ai-workbench/reviews/claim",
        headers={"Authorization": "Bearer " + settings.AI_DSH_SERVICE_TOKEN},
        json=plugin_methods(),
    )
    assert response.status_code == 200, response.text
    claim = response.json()
    assert UUID(claim["task"]["id"]) == task.id
    deadline = datetime.fromisoformat(claim["task"]["max_expires_at"])
    assert timedelta(minutes=119) < deadline - datetime.now(UTC) <= timedelta(hours=2)
    task.authorization_expires_at = datetime.now(UTC) + timedelta(seconds=30)
    session.add(task)
    session.flush()
    path = f"/api/integrations/ai-workbench/reviews/{task.id}/heartbeat"
    response = client.post(
        path, headers={"Authorization": "Bearer " + claim["task_token"]}, json={}
    )
    assert response.status_code == 200, response.text
    renewed = response.json()
    claims = jwt.decode(
        renewed["task_token"],
        settings.SECRET_KEY,
        algorithms=[security.ALGORITHM],
        audience="tkada-ai-review",
    )
    assert claims["fence"] == task.fence
    assert datetime.fromtimestamp(claims["exp"], UTC) > datetime.now(UTC) + timedelta(
        minutes=4
    )
    assert datetime.fromisoformat(renewed["expires_at"]) <= deadline
    task.status = "COMPLETE"
    session.add(task)
    session.flush()
    assert (
        client.post(
            path, headers={"Authorization": "Bearer " + renewed["task_token"]}, json={}
        ).status_code
        == 409
    )


def test_heartbeat_signs_only_the_locked_generation_before_committing(monkeypatch):
    from types import SimpleNamespace

    from app.modules.ai_workbench import runtime

    events = []
    task = SimpleNamespace(
        id=uuid4(),
        status="RUNNING",
        started_at=datetime.now(UTC),
        fence="old-generation",
        authorization_expires_at=datetime.now(UTC),
    )

    class CommitRace:
        def add(self, _task):
            pass

        def commit(self):
            events.append("commit")
            # 模拟提交后被 ORM 重新加载的下一恢复代；旧会话永远不能获得该令牌。
            task.fence = "new-generation"

    monkeypatch.setattr(runtime, "_read_task_scope", lambda *_args: None)
    monkeypatch.setattr(
        runtime,
        "_task_token",
        lambda row: events.append("sign:" + row.fence) or row.fence,
    )
    response = runtime.heartbeat(task.id, CommitRace(), task)
    assert events == ["sign:old-generation", "commit"]
    assert response["task_token"] == "old-generation"
