from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlmodel import Session

from app.core.security import create_access_token
from app.modules.accounts.models import (
    AdvertiserAccount,
    BCAccountAccess,
    TenantBC,
    TikTokConnection,
)
from app.modules.ai_workbench.models import AIOptimizerStrategyBinding
from app.modules.ai_workbench.schemas import OptimizerCreate
from app.modules.strategies.models import CopyPoolVersion, Strategy, StrategyVersion


def payload(**overrides):
    value = {
        "name": "起量优化师",
        "daily_spend": "100",
        "target_roas": "1.2",
        "priority": "ROAS",
        "bc_id": "bc-a",
        "advertiser_ids": ["acc-1"],
        "optimization_mode": "MANUAL",
        "strategy_version_id": str(uuid4()),
        "request_id": str(uuid4()),
    }
    value.update(overrides)
    return value


def test_optimizer_contract_accepts_scoped_configuration():
    result = OptimizerCreate.model_validate(payload())
    assert result.bc_id == "bc-a"
    assert result.optimization_mode == "MANUAL"


def test_optimizer_configuration_accepts_more_than_one_thousand_accounts():
    from app.modules.ai_workbench.schemas import OptimizerUpdate

    accounts = tuple(f"acc-{i}" for i in range(1001))
    created = OptimizerCreate.model_validate(payload(advertiser_ids=accounts))
    updated = OptimizerUpdate.model_validate(
        {"advertiser_ids": accounts, "request_id": str(uuid4())}
    )
    assert created.advertiser_ids == updated.advertiser_ids == accounts
    with pytest.raises(ValidationError):
        OptimizerCreate.model_validate(
            payload(advertiser_ids=accounts + (accounts[0],))
        )


def test_optimizer_contract_rejects_invalid_mode_and_roas_priority():
    with pytest.raises(ValidationError):
        OptimizerCreate.model_validate(payload(optimization_mode="INVALID"))
    with pytest.raises(ValidationError):
        OptimizerCreate.model_validate(payload(target_roas=None))


def test_update_contract_only_allows_explicit_target_roas_clear():
    from app.modules.ai_workbench.schemas import OptimizerUpdate

    request_id = str(uuid4())
    cleared = OptimizerUpdate.model_validate(
        {"target_roas": None, "request_id": request_id}
    )
    assert "target_roas" in cleared.model_fields_set
    for field in (
        "name",
        "daily_spend",
        "priority",
        "bc_id",
        "advertiser_ids",
        "optimization_mode",
        "authorization_limits",
        "strategy_version_id",
        "rule_ids",
        "active",
    ):
        with pytest.raises(ValidationError):
            OptimizerUpdate.model_validate({field: None, "request_id": request_id})
    with pytest.raises(ValidationError):
        OptimizerUpdate.model_validate(
            {"target_roas": None, "name": None, "request_id": request_id}
        )


def test_strategy_binding_has_active_scope_index():
    indexes = {index.name for index in AIOptimizerStrategyBinding.__table__.indexes}
    assert "uq_ai_binding_one_active" in indexes


def test_version_binding_fk_includes_optimizer_scope():
    from app.modules.ai_workbench.models import AIOptimizerVersion

    binding_fk = next(
        fk
        for fk in AIOptimizerVersion.__table__.foreign_key_constraints
        if fk.name == "fk_ai_version_binding"
    )
    assert [column.name for column in binding_fk.columns] == [
        "tenant_id",
        "optimizer_id",
        "strategy_binding_id",
    ]


def test_same_tenant_optimizer_binding_mismatch_is_rejected(session, context):
    from sqlalchemy.exc import IntegrityError

    from app.modules.ai_workbench.models import (
        AIOptimizer,
        AIOptimizerStrategyBinding,
        AIOptimizerVersion,
    )

    strategy = seed_optimizer_dependencies(session, context)
    optimizer_a = AIOptimizer(
        tenant_id=context.tenant_id, name="A", created_by=context.actor_id
    )
    optimizer_b = AIOptimizer(
        tenant_id=context.tenant_id, name="B", created_by=context.actor_id
    )
    session.add(optimizer_a)
    session.add(optimizer_b)
    session.flush()
    binding = AIOptimizerStrategyBinding(
        tenant_id=context.tenant_id,
        optimizer_id=optimizer_a.id,
        strategy_version_id=strategy.id,
        created_by=context.actor_id,
    )
    session.add(binding)
    session.flush()
    session.add(
        AIOptimizerVersion(
            tenant_id=context.tenant_id,
            optimizer_id=optimizer_b.id,
            number=1,
            daily_spend="10",
            target_roas=None,
            priority="BALANCED",
            bc_id="bc-a",
            advertiser_ids=["acc-1"],
            optimization_mode="MANUAL",
            authorization_limits={},
            strategy_binding_id=binding.id,
            request_id=uuid4(),
            request_digest="b" * 64,
            created_by=context.actor_id,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


def seed_optimizer_dependencies(session: Session, context, *, account="acc-1"):
    """Seed only local tenant facts; no provider/API calls are made."""
    connection = TikTokConnection(tenant_id=context.tenant_id, status="ACTIVE")
    session.add(connection)
    # 同一租户可多次播种不同账户，BC 行只建一次。
    if session.get(TenantBC, (context.tenant_id, "bc-a")) is None:
        session.add(TenantBC(tenant_id=context.tenant_id, bc_id="bc-a"))
    session.add(
        AdvertiserAccount(
            tenant_id=context.tenant_id,
            advertiser_id=account,
            currency="USD",
            timezone="UTC",
            remote_status="ENABLE",
        )
    )
    # 先落父表：bc_account_access 的复合外键不会被 ORM 用来排序插入。
    session.flush()
    session.add(
        BCAccountAccess(
            tenant_id=context.tenant_id,
            bc_id="bc-a",
            advertiser_id=account,
            connection_id=connection.id,
            in_bc=True,
            authorized=True,
            active=True,
            can_build=True,
            permission_state="VERIFIED",
        )
    )
    pool = CopyPoolVersion(name="AI test pool", sealed=False)
    strategy = Strategy(
        tenant_id=context.tenant_id, name="AI test strategy", latest_version=1
    )
    session.add(pool)
    session.add(strategy)
    session.flush()
    from app.modules.strategies.models import CopyEntry

    session.add(CopyEntry(pool_version_id=pool.id, position=1, text="测试广告文案"))
    session.flush()
    pool.sealed = True
    session.add(pool)
    version = StrategyVersion(
        tenant_id=context.tenant_id,
        strategy_id=strategy.id,
        number=1,
        copy_pool_version_id=pool.id,
        config={
            "budget": "10",
            "currency": "USD",
            "copy_pool_version": str(pool.id),
            "target_roas": None,
        },
        budget="10",
        target_roas=None,
        created_by=context.actor_id,
        request_id=uuid4(),
        request_digest="a" * 64,
        request_kind="create",
    )
    session.add(version)
    session.flush()
    return version


def api_headers(context, *, session=None):
    if session is not None and context.role != "viewer":
        from app.models import User

        session.get(User, context.actor_id).is_superuser = True
        session.flush()
    return {
        "Authorization": f"Bearer {create_access_token(context.actor_id, timedelta(minutes=5))}"
    }


def create_body(strategy_version_id, **overrides):
    value = payload(
        strategy_version_id=str(strategy_version_id),
        priority="BALANCED",
        target_roas=None,
    )
    value.update(overrides)
    return value


def test_tenant_and_optimizer_ids_are_isolated(client, session, context, other_context):
    strategy = seed_optimizer_dependencies(session, context)
    created = client.post(
        f"/api/tenants/{context.tenant_id}/ai-optimizers",
        headers=api_headers(context, session=session),
        json=create_body(strategy.id),
    )
    assert created.status_code == 201, created.text
    optimizer_id = created.json()["id"]
    assert (
        client.get(
            f"/api/tenants/{other_context.tenant_id}/ai-optimizers/{optimizer_id}",
            headers=api_headers(other_context, session=session),
        ).status_code
        == 404
    )


def test_invalid_scope_and_missing_strategy_binding_are_rejected(
    client, session, context
):
    strategy = seed_optimizer_dependencies(session, context)
    url = f"/api/tenants/{context.tenant_id}/ai-optimizers"
    invalid_scope = client.post(
        url,
        headers=api_headers(context, session=session),
        json=create_body(strategy.id, bc_id="missing-bc"),
    )
    assert invalid_scope.status_code == 422
    missing_strategy = client.post(
        url,
        headers=api_headers(context, session=session),
        json=create_body(uuid4()),
    )
    assert missing_strategy.status_code == 409


def test_shared_strategy_and_explicit_target_roas_clear_are_guarded(
    client, session, context
):
    strategy = seed_optimizer_dependencies(session, context)
    url = f"/api/tenants/{context.tenant_id}/ai-optimizers"
    first = client.post(
        url,
        headers=api_headers(context, session=session),
        json=create_body(strategy.id),
    ).json()
    second = client.post(
        url,
        headers=api_headers(context, session=session),
        json=create_body(strategy.id),
    )
    assert second.status_code == 409
    cleared = client.patch(
        f"{url}/{first['id']}",
        headers=api_headers(context, session=session),
        json={"target_roas": None, "request_id": str(uuid4())},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["target_roas"] is None
