from uuid import uuid4

import pytest

from app.core.errors import DomainError
from app.modules.ai_workbench.production_origin import validate_production_origin
from app.modules.materials.push_schemas import ProductionOrigin
from tests.modules.ai_workbench.review_helpers import (
    create_prepared_review_task,
    source_store,
)


def seed_order(session, context):
    from app.modules.ai_workbench import plans_service, service
    from app.modules.ai_workbench.orders import create_order
    from app.modules.ai_workbench.plan_schemas import DailyPlanRequest
    from app.modules.ai_workbench.schemas import OptimizerCreate
    from tests.modules.ai_workbench.test_daily_plans import (
        _plan_body,
        _provider_application,
    )
    from tests.modules.ai_workbench.test_optimizer import (
        create_body,
        seed_optimizer_dependencies,
    )

    strategy = seed_optimizer_dependencies(session, context)
    optimizer = service.create_optimizer(
        session,
        context=context,
        body=OptimizerCreate.model_validate(create_body(strategy.id)),
    )
    connection, _ = _provider_application(session, context)
    plan = plans_service.create_daily_plan(
        session,
        context=context,
        optimizer_id=optimizer.id,
        body=DailyPlanRequest.model_validate(_plan_body(optimizer.id, connection.id)),
    )
    from app.modules.ai_workbench.plans import AIDailyPlanRow

    plan_row = session.get(AIDailyPlanRow, plan.rows[0].id)
    plan_row.order_materials = True
    session.flush()
    order = create_order(
        session,
        context=context,
        row_id=plan_row.id,
        config={
            "strategy_ids": ["creative-one", "creative-two"],
            "feishu_token": "local-test-table",
        },
    )
    order.job_id = "job-one"
    session.flush()
    return optimizer, order


def test_production_origin_requires_tenant_order_and_frozen_selected_strategy(
    session, context
):
    _, order = seed_order(session, context)
    origin = ProductionOrigin(
        order_id=order.id, job_id="job-one", strategy_id="creative-one"
    )
    assert (
        validate_production_origin(session, tenant_id=context.tenant_id, origin=origin)[
            "strategy_id"
        ]
        == "creative-one"
    )
    for tenant_id, invalid in [
        (uuid4(), origin),
        (context.tenant_id, origin.model_copy(update={"job_id": "wrong"})),
        (context.tenant_id, origin.model_copy(update={"strategy_id": "wrong"})),
    ]:
        with pytest.raises(DomainError):
            validate_production_origin(session, tenant_id=tenant_id, origin=invalid)


def test_material_origin_mapping_is_exact_and_ambiguous_producer_excluded(
    session, context
):
    from datetime import UTC, datetime

    from sqlmodel import select

    from app.modules.accounts.models import BCAccountAccess
    from app.modules.ai_workbench.attribution import resolve_mappings
    from app.modules.ai_workbench.reviews import load_optimizer
    from app.modules.materials.models import AccountMaterial
    from app.modules.materials.push_models import (
        MaterialPushBatch,
        MaterialPushItem,
        PushedMaterial,
    )
    from tests.modules.materials.test_tenant_materials import material

    optimizer, order = seed_order(session, context)
    _, version = load_optimizer(session, context, optimizer.id)
    file = material(session, context, "same-drama.mp4")
    grant = session.exec(
        select(BCAccountAccess).where(
            BCAccountAccess.tenant_id == context.tenant_id,
            BCAccountAccess.advertiser_id == "acc-1",
        )
    ).one()
    asset = AccountMaterial(
        tenant_id=context.tenant_id,
        bc_id="bc-a",
        material_id=file.id,
        advertiser_id="acc-1",
        connection_id=grant.connection_id,
        video_id="actual-video",
        status="available",
        verified_at=datetime.now(UTC),
    )
    batch = MaterialPushBatch(
        tenant_id=context.tenant_id,
        tenant_name="test",
        bc_id="bc-a",
        actor_id=context.actor_id,
        key_id="producer",
        request_id=uuid4(),
        request_digest="a" * 64,
        frozen_route={},
    )
    session.add_all(
        [
            asset,
            batch,
            PushedMaterial(tenant_id=context.tenant_id, external_id="source-file"),
        ]
    )
    session.flush()
    origin = validate_production_origin(
        session,
        tenant_id=context.tenant_id,
        origin=ProductionOrigin(
            order_id=order.id, job_id=order.job_id, strategy_id="creative-one"
        ),
    )
    item = MaterialPushItem(
        tenant_id=context.tenant_id,
        batch_id=batch.id,
        external_id="source-file",
        revision=1,
        file_name=file.file_name,
        url_ciphertext="not-output",
        material_id=file.id,
        status="imported",
        production_origin=origin,
    )
    session.add(item)
    session.flush()
    mapped = resolve_mappings(
        session,
        context=context,
        optimizer_id=optimizer.id,
        version=version,
        cutoff=datetime.now(UTC),
    )
    assert mapped["rows"][0]["version_id"] == "creative-one"
    assert mapped["rows"][0]["remote_ids"] == ["actual-video"]
    task = create_prepared_review_task(
        session, context=context, optimizer_id=optimizer.id, request_id=uuid4()
    )
    assert (
        next(
            r["payload"]
            for r in source_store(session, task).values()
            if r["category"] == "material_orders"
        )["rows"][0]["job_id"]
        == "job-one"
    )
    assert (
        next(
            r["payload"]
            for r in source_store(session, task).values()
            if r["category"] == "attribution"
        )["rows"][0]["version_id"]
        == "creative-one"
    )
    session.add(
        MaterialPushItem(
            tenant_id=context.tenant_id,
            batch_id=batch.id,
            external_id="source-file",
            revision=2,
            file_name=file.file_name,
            url_ciphertext="not-output",
            material_id=file.id,
            status="imported",
            production_origin={**origin, "strategy_id": "creative-two"},
        )
    )
    session.flush()
    conflict = resolve_mappings(
        session,
        context=context,
        optimizer_id=optimizer.id,
        version=version,
        cutoff=datetime.now(UTC),
    )
    assert conflict["rows"] == []
    assert conflict["conflicts"][0]["reason"] == "multiple_origin_versions"
    # 旧任务正文仍保留当时唯一的关联，后续冲突不悄悄改写历史。
    assert (
        next(
            r["payload"]
            for r in source_store(session, task).values()
            if r["category"] == "attribution"
        )["rows"][0]["version_id"]
        == "creative-one"
    )


def test_nested_producer_origin_is_discoverable_in_push_openapi():
    from app.modules.materials.push_api import INPUT_SCHEMA

    item = INPUT_SCHEMA["properties"]["materials"]["items"]
    origin = next(
        schema
        for schema in item["properties"]["production_origin"]["anyOf"]
        if schema.get("type") == "object"
    )
    assert set(origin["required"]) == {"order_id", "job_id", "strategy_id"}
