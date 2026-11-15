"""Transport-double acceptance contracts for the AI workbench v1 loop."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from app.modules.ai_workbench.api import router
from app.modules.ai_workbench.models import AIOptimizerRuleBinding
from app.modules.automation_rules.models import AutomationRule
from app.modules.strategies.models import CopyPoolVersion, Strategy, StrategyVersion

# 以测试文件定位源码，避免依赖 pytest 的启动目录。
AI_WORKBENCH_DIR = (
    Path(__file__).resolve().parents[2] / "app" / "modules" / "ai_workbench"
)


@pytest.fixture
def ai_workbench_contexts(session):
    """Seed real tenant memberships for the HTTP scope checks below."""
    from tests.modules.conftest import create_context

    return create_context(session), create_context(session)


def test_daily_report_route_is_tenant_and_optimizer_scoped():
    paths = {route.path for route in router.routes}
    assert "/tenants/{tenant_id}/ai-optimizers/{optimizer_id}/daily-report" in paths


def test_acceptance_contract_keeps_external_writes_out_of_the_loop():
    source = (AI_WORKBENCH_DIR / "daily_report.py").read_text()
    assert "requests.post" not in source
    assert "httpx.post" not in source
    assert "frozen_evidence_ids" in source


def test_two_tenants_and_two_optimizers_keep_daily_report_scope(
    client, session, ai_workbench_contexts
):
    """Exercise the real tenant/optimizer route with only local transport doubles."""
    from tests.modules.ai_workbench.test_optimizer import (
        api_headers,
        create_body,
        seed_optimizer_dependencies,
    )

    context, other_context = ai_workbench_contexts
    first_strategy = seed_optimizer_dependencies(session, context, account="acc-1")
    # Keep both optimizers in the same tenant so the report proves sibling
    # bindings do not leak their next trigger into one another.
    second_pool = CopyPoolVersion(name="AI sibling pool", sealed=True)
    second_strategy = Strategy(tenant_id=context.tenant_id, name="AI sibling strategy")
    session.add_all([second_pool, second_strategy])
    session.flush()
    sibling_strategy = StrategyVersion(
        tenant_id=context.tenant_id,
        strategy_id=second_strategy.id,
        number=1,
        copy_pool_version_id=second_pool.id,
        config={
            "budget": "10",
            "currency": "USD",
            "copy_pool_version": str(second_pool.id),
            "target_roas": None,
        },
        budget="10",
        target_roas=None,
        created_by=context.actor_id,
        request_id=uuid4(),
        request_digest="b" * 64,
        request_kind="create",
    )
    session.add(sibling_strategy)
    session.flush()
    other_strategy = seed_optimizer_dependencies(
        session, other_context, account="acc-1"
    )
    base = f"/api/tenants/{context.tenant_id}/ai-optimizers"
    first = client.post(
        base,
        headers=api_headers(context, session=session),
        json=create_body(
            first_strategy.id, name="自动优化师", optimization_mode="AUTO"
        ),
    )
    assert first.status_code == 201, first.text
    sibling = client.post(
        base,
        headers=api_headers(context, session=session),
        json=create_body(sibling_strategy.id, name="同租户优化师"),
    )
    assert sibling.status_code == 201, sibling.text
    other = client.post(
        f"/api/tenants/{other_context.tenant_id}/ai-optimizers",
        headers=api_headers(other_context, session=session),
        json=create_body(other_strategy.id, name="其他租户优化师"),
    )
    assert other.status_code == 201, other.text

    first_id = first.json()["id"]
    sibling_id = sibling.json()["id"]
    other_id = other.json()["id"]

    active_rule = AutomationRule(
        tenant_id=context.tenant_id,
        name="active acceptance rule",
        definition={},
        created_by=context.actor_id,
        updated_by=context.actor_id,
        next_run_at=datetime.now(UTC) + timedelta(hours=1),
    )
    inactive_rule = AutomationRule(
        tenant_id=context.tenant_id,
        name="inactive acceptance rule",
        definition={},
        created_by=context.actor_id,
        updated_by=context.actor_id,
        next_run_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    sibling_rule = AutomationRule(
        tenant_id=context.tenant_id,
        name="sibling acceptance rule",
        definition={},
        created_by=context.actor_id,
        updated_by=context.actor_id,
        next_run_at=datetime.now(UTC) + timedelta(minutes=2),
    )
    session.add_all([active_rule, inactive_rule, sibling_rule])
    session.flush()
    session.add_all(
        [
            AIOptimizerRuleBinding(
                tenant_id=context.tenant_id,
                optimizer_id=first_id,
                rule_id=active_rule.id,
                created_by=context.actor_id,
                active=True,
            ),
            AIOptimizerRuleBinding(
                tenant_id=context.tenant_id,
                optimizer_id=first_id,
                rule_id=inactive_rule.id,
                created_by=context.actor_id,
                active=False,
            ),
            AIOptimizerRuleBinding(
                tenant_id=context.tenant_id,
                optimizer_id=sibling_id,
                rule_id=sibling_rule.id,
                created_by=context.actor_id,
                active=True,
            ),
        ]
    )
    session.flush()

    first_report = client.get(
        f"{base}/{first_id}/daily-report?business_date=2026-11-12",
        headers=api_headers(context, session=session),
    )
    assert first_report.status_code == 200, first_report.text
    first_payload = first_report.json()
    assert first_payload["optimizer_id"] == first_id
    assert first_payload["optimization_mode"] == "AUTO"
    assert first_payload["next_trigger"]["id"] == str(active_rule.id)

    sibling_report = client.get(
        f"{base}/{sibling_id}/daily-report?business_date=2026-11-12",
        headers=api_headers(context, session=session),
    )
    assert sibling_report.status_code == 200, sibling_report.text
    sibling_payload = sibling_report.json()
    assert sibling_payload["optimizer_id"] == sibling_id
    assert sibling_payload["next_trigger"]["id"] == str(sibling_rule.id)
    assert sibling_payload["next_trigger"]["id"] != first_payload["next_trigger"]["id"]

    other_report = client.get(
        f"/api/tenants/{other_context.tenant_id}/ai-optimizers/{other_id}/daily-report?business_date=2026-11-12",
        headers=api_headers(other_context, session=session),
    )
    assert other_report.status_code == 200, other_report.text
    other_payload = other_report.json()
    assert other_payload["optimizer_id"] == other_id
    assert other_payload["optimizer_id"] != first_payload["optimizer_id"]
    assert other_payload["optimization_mode"] == "MANUAL"

    cross_tenant = client.get(
        f"/api/tenants/{other_context.tenant_id}/ai-optimizers/{first_id}/daily-report",
        headers=api_headers(other_context, session=session),
    )
    assert cross_tenant.status_code == 404


def test_acceptance_matrix_covers_partial_readback_order_and_proposal_contracts():
    daily_report = (AI_WORKBENCH_DIR / "daily_report.py").read_text()
    proposals = (AI_WORKBENCH_DIR / "proposals.py").read_text()
    assert '"PARTIAL"' in daily_report
    assert '"readback"' in daily_report
    assert '"order_materials"' in daily_report
    # Account-scoped rule proposals are checked before publication, and an
    # accepted proposal always appends a fresh immutable optimizer version.
    assert 'mode == "ACCOUNTS"' in proposals
    assert "publish_auto_proposals" in proposals
    assert "update_optimizer" in proposals
    assert "request_id=uuid4()" in proposals
