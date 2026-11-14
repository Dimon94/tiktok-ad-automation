"""真实报表冻结、任务工具鉴权、分页与已读引用合同。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlmodel import select

from app.core.config import settings
from app.modules.ai_workbench import reviews, service
from app.modules.ai_workbench.review_reading import read_for_task
from app.modules.ai_workbench.review_store import (
    ReviewReceipt,
    ReviewSource,
    delivered_evidence_ids,
    scoped,
)
from app.modules.ai_workbench.schemas import OptimizerCreate
from app.modules.reporting.publish import AccountMeta, publish_ad_day
from tests.modules.ai_workbench.review_helpers import (
    create_prepared_review_task,
    minimal_report,
    source_index,
)
from tests.modules.ai_workbench.test_optimizer import (
    create_body,
    seed_optimizer_dependencies,
)
from tests.modules.ai_workbench.test_review_methods import plugin_methods
from tests.modules.reporting.sync_conftest import ad_row


@pytest.fixture
def reading(session, context, monkeypatch):
    strategy = seed_optimizer_dependencies(session, context)
    optimizer = service.create_optimizer(
        session,
        context=context,
        body=OptimizerCreate.model_validate(create_body(strategy.id)),
    )
    cutoff = datetime.now(UTC)
    day = cutoff.date() - timedelta(days=1)

    def publish(spend, observed=cutoff - timedelta(seconds=1)):
        publish_ad_day(
            session,
            tenant_id=context.tenant_id,
            job_id=uuid4(),
            accounts={"acc-1": AccountMeta("USD", "UTC")},
            start=day,
            end=day,
            requested_at=observed,
            rows=[
                ad_row(
                    f"ad-{i}",
                    day,
                    campaign=f"campaign-{i}",
                    adv="acc-1",
                    spend=spend,
                    d0="0",
                    total="0",
                )
                for i in range(61)
            ],
        )

    publish("10")
    monkeypatch.setattr(reviews, "_now", lambda: cutoff)
    task = create_prepared_review_task(
        session,
        context=context,
        optimizer_id=optimizer.id,
        request_id=uuid4(),
    )
    monkeypatch.setattr(settings, "AI_DSH_SERVICE_TOKEN", "local-test-" + "x" * 40)
    return task, publish, cutoff


def claim_task(client):
    claimed = client.post(
        "/api/integrations/ai-workbench/reviews/claim",
        headers={"Authorization": "Bearer " + settings.AI_DSH_SERVICE_TOKEN},
        json=plugin_methods(),
    )
    assert claimed.status_code == 200, claimed.text
    body = claimed.json()
    return body, {"Authorization": "Bearer " + body["task_token"]}


def campaign_source(session, task):
    return next(
        row
        for row in source_index(session, task)
        if row["category"] == "reporting"
        and row["dimension"] == "campaign"
        and row["maturity_horizon"] == "d0"
    )


def test_reporting_pages_stay_frozen_and_only_delivered_ids_can_be_cited(
    client,
    session,
    reading,
):
    task, publish, cutoff = reading
    claim, headers = claim_task(client)
    initial = claim["task"]["evidence"]
    assert "evidence_store" not in claim["task"]
    assert "attribution" not in initial and "rule_evidence" not in initial
    assert "reporting" not in initial["data_pack"]
    assert "evidence_index" not in initial
    path = f"/api/integrations/ai-workbench/reviews/{task.id}"
    source = campaign_source(session, task)
    query = client.post(
        path + "/evidence/query", headers=headers, json={"category": "reporting"}
    ).json()
    assert len(query["items"]) == 10 and query["next_offset"] == 10
    assert source["id"] not in delivered_evidence_ids(session, task)
    result = {
        "report": minimal_report(),
        "summary": "检查明细",
        "proposals": [],
        "recommendations": [
            {
                "problem": "核验",
                "suggestion": "继续观察",
                "evidence_ids": [source["id"]],
            },
        ],
    }
    assert (
        client.post(path + "/complete", headers=headers, json=result).status_code != 200
    )
    first = client.post(
        path + "/evidence/read",
        headers=headers,
        json={"evidence_id": source["id"], "limit": 10},
    )
    assert first.status_code == 200, first.text
    page = first.json()
    assert page["next_offset"] == 10 and page["frozen_rows"] == 61
    assert page["metadata"]["rows_truncated"] is False
    assert page["metadata"]["maturity_evidence"]["horizon"] == "d0"
    assert page["rows"][0]["metric_buckets"][0]["values"]["spend"] == "10"
    assert page["id"] in delivered_evidence_ids(session, task)
    publish("99", cutoff + timedelta(seconds=1))
    repeated = client.post(
        path + "/evidence/read",
        headers=headers,
        json={"evidence_id": source["id"], "limit": 10},
    ).json()
    assert repeated == page
    last = client.post(
        path + "/evidence/read",
        headers=headers,
        json={"evidence_id": source["id"], "offset": 60},
    ).json()
    assert len(last["rows"]) == 1 and last["next_offset"] is None
    assert last["rows"][0]["metric_buckets"][0]["values"]["spend"] == "10"
    assert len(session.exec(scoped(ReviewReceipt, task)).all()) == 2
    assert "read_receipts" not in task.evidence
    result["recommendations"][0]["evidence_ids"] = [page["id"]]
    assert (
        client.post(path + "/complete", headers=headers, json=result).status_code == 200
    )
    assert (
        client.post(
            path + "/evidence/read", headers=headers, json={"evidence_id": source["id"]}
        ).status_code
        != 200
    )


def test_reporting_cutoff_does_not_read_a_later_publication(session, context, reading):
    from app.modules.ai_workbench.review_data import build_data_pack

    task, publish, cutoff = reading
    publish("99", cutoff + timedelta(seconds=1))
    _, version = reviews.load_optimizer(session, context, task.optimizer_id)
    pack = build_data_pack(session, context=context, version=version, cutoff=cutoff)
    report = next(
        row
        for row in pack["reporting"]
        if row["dimension"] == "campaign" and row["maturity_horizon"] == "d0"
    )
    assert report["status"] == "UNAVAILABLE"
    assert report["reason"] == "publication_after_cutoff" and report["rows"] == []


def test_foreign_ids_invalid_pages_and_tampered_sources_are_rejected(
    client,
    session,
    context,
    reading,
):
    from app.core.errors import DomainError

    task, _, _ = reading
    _, headers = claim_task(client)
    path = f"/api/integrations/ai-workbench/reviews/{task.id}/evidence/read"
    assert (
        client.post(
            path, headers=headers, json={"evidence_id": str(uuid4())}
        ).status_code
        == 404
    )
    source = campaign_source(session, task)
    for args in ({"offset": -1}, {"offset": 100}, {"limit": 101}):
        assert (
            client.post(
                path, headers=headers, json={"evidence_id": source["id"], **args}
            ).status_code
            == 422
        )
    other = create_prepared_review_task(
        session,
        context=context,
        optimizer_id=task.optimizer_id,
        request_id=uuid4(),
    )
    # 报表快照 ID 绑定具体任务；同投手的另一个任务也不能借用。
    with pytest.raises(HTTPException):
        read_for_task(session, task=other, evidence_id=UUID(source["id"]))
    # 不可变数据库阻止更新；摘要核验同时防止不合法导入的新来源。
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError), session.begin_nested():
        stored = session.get(ReviewSource, (task.id, UUID(source["id"])))
        stored.digest = "invalid"
        session.flush()
    broken = ReviewSource(
        task_id=task.id,
        id=uuid4(),
        tenant_id=task.tenant_id,
        optimizer_id=task.optimizer_id,
        category="plans",
        digest="invalid",
        payload=[],
        source_metadata={},
        captured_at=datetime.now(UTC),
    )
    session.add(broken)
    session.flush()
    with pytest.raises(DomainError, match="冻结证据"):
        read_for_task(session, task=task, evidence_id=broken.id)


@pytest.mark.parametrize("change", ["revoke", "optimizer_version"])
def test_revoked_account_or_changed_optimizer_blocks_task_tools(
    client, session, context, reading, change
):
    from app.modules.accounts.models import BCAccountAccess

    task, _, _ = reading
    _, headers = claim_task(client)
    if change == "revoke":
        grant = session.exec(
            select(BCAccountAccess).where(
                BCAccountAccess.tenant_id == context.tenant_id
            )
        ).first()
        grant.authorized = False
        session.add(grant)
        session.flush()
    else:
        from app.modules.ai_workbench.schemas import OptimizerUpdate

        service.update_optimizer(
            session,
            context=context,
            optimizer_id=task.optimizer_id,
            body=OptimizerUpdate(daily_spend="123", request_id=uuid4()),
        )
    path = f"/api/integrations/ai-workbench/reviews/{task.id}"
    for endpoint, body in (
        ("query", {"category": "reporting"}),
        ("read", {"evidence_id": campaign_source(session, task)["id"]}),
    ):
        assert (
            client.post(
                path + "/evidence/" + endpoint, headers=headers, json=body
            ).status_code
            != 200
        )


def test_tenant_token_and_frozen_account_scope_cannot_be_expanded(
    client,
    session,
    context,
    other_context,
    reading,
):
    task, _, _ = reading
    seed_optimizer_dependencies(session, context, account="acc-2")
    strategy = seed_optimizer_dependencies(session, other_context)
    other_optimizer = service.create_optimizer(
        session,
        context=other_context,
        body=OptimizerCreate.model_validate(create_body(strategy.id)),
    )
    other = create_prepared_review_task(
        session,
        context=other_context,
        optimizer_id=other_optimizer.id,
        request_id=uuid4(),
    )
    _, headers = claim_task(client)
    assert all(
        row["advertiser_id"] == "acc-1"
        for row in source_index(session, task)
        if row["category"] == "reporting"
    )
    base = "/api/integrations/ai-workbench/reviews"
    assert (
        client.post(
            f"{base}/{other.id}/evidence/query",
            headers=headers,
            json={"category": "reporting"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"{base}/{task.id}/evidence/read",
            headers=headers,
            json={"evidence_id": campaign_source(session, other)["id"]},
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"{base}/{task.id}/evidence/query",
            headers=headers,
            json={"category": "reporting", "advertiser_ids": ["acc-2"]},
        ).status_code
        == 422
    )
    source = campaign_source(session, task)
    unread_proposal = {
        "report": minimal_report(),
        "summary": "未读证据不得用于提案",
        "proposals": [
            {
                "kind": "strategy",
                "after": {"config": task.evidence["strategy"]["config"]},
                "rationale": "核验未读取证据不可提交",
                "problem": "引用缺口",
                "expected_effect": "保持原配置",
                "validation": "校验已读凭证",
                "evidence_ids": [source["id"]],
            }
        ],
    }
    assert (
        client.post(
            f"{base}/{task.id}/complete", headers=headers, json=unread_proposal
        ).status_code
        != 200
    )


def test_read_page_preserves_frozen_d7_maturity_and_exact_window(
    session, context, reading, monkeypatch
):
    from tests.modules.ai_workbench.test_review_maturity import CUTOFF, _publish

    task, _, cutoff = reading
    cutoff = CUTOFF
    monkeypatch.setattr(reviews, "_now", lambda: cutoff)
    _publish(session, context, observed=cutoff)
    task = create_prepared_review_task(
        session,
        context=context,
        optimizer_id=task.optimizer_id,
        request_id=uuid4(),
    )
    source = next(
        row
        for row in source_index(session, task)
        if row["category"] == "reporting"
        and row["dimension"] == "campaign"
        and row["maturity_horizon"] == "d7"
    )
    page = read_for_task(session, task=task, evidence_id=UUID(source["id"]))
    mature = page["metadata"]["maturity_evidence"]
    assert (
        mature["status"] == "AVAILABLE" and mature["observed_after_maturity_days"] == 7
    )
    assert (
        page["metadata"]["filters"]["start_date"]
        == (cutoff.date() - timedelta(days=14)).isoformat()
    )
    assert (
        page["metadata"]["filters"]["end_date"]
        == (cutoff.date() - timedelta(days=8)).isoformat()
    )
    assert page["rows"][0]["metric_buckets"][0]["values"]["revenue_d7"] == "49"
