"""图片 UNKNOWN 补发使用现有精确授权账本与数据库配对约束。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlmodel import Session, col, select

from app.modules.builds.execution_models import ExecutionStep
from app.modules.materials.models import (
    AccountMaterial,
    MaterialAssetOperation,
    MaterialDistribution,
    MaterialFile,
)
from tests.modules.builds.test_carousel_execution import carousel_case as carousel_case
from tests.modules.strategies.test_concurrency import (
    isolated_strategy_database as isolated_strategy_database,
)


@pytest.fixture
def unknown_image(carousel_case):
    from app.modules.builds.routes import load_preview_route
    from app.modules.materials.image_distribution import prepare_image_targets
    from app.modules.tenants.models import TenantMembership
    from tests.modules.builds.test_drafts import account

    database, context, _, submission_id, unit_ids, _ = carousel_case
    with Session(database) as db, db.begin():
        db.get(
            TenantMembership, (context.tenant_id, context.actor_id)
        ).role = "tenant_admin"
        step = db.exec(
            select(ExecutionStep)
            .where(
                ExecutionStep.submission_id == submission_id,
                ExecutionStep.unit_id == unit_ids[0],
                ExecutionStep.kind == "MATERIAL",
            )
            .order_by(col(ExecutionStep.id))
        ).first()
        assert step is not None
        material = db.get(MaterialFile, step.material_id)
        for mapping in db.exec(
            select(AccountMaterial).where(AccountMaterial.material_id == material.id)
        ).all():
            db.delete(mapping)
        db.flush()
        # 复用先播种图片再冻结的轮播合同，不能把已绑定的视频改成图片。
        assert material.media_type == "IMAGE"
        material.storage_state = "unavailable"
        db.flush()
        account(db, context, "source-image-account")
        route = load_preview_route(db, context=context, preview_id=step.preview_id)
        db.add(
            AccountMaterial(
                tenant_id=context.tenant_id,
                bc_id=step.bc_id,
                material_id=material.id,
                advertiser_id="source-image-account",
                connection_id=route.connection_id,
                media_type="IMAGE",
                video_id=None,
                image_id="source-image",
                mid="source-mid",
                is_carousel_usable=True,
                frozen_route=route.model_dump(mode="json"),
                status="available",
                verified_at=datetime.now(UTC),
            )
        )
        db.flush()
        item = prepare_image_targets(
            route, (material.id,), ("account-A",), session=db, context=context
        ).items[0]
        dist = db.get(MaterialDistribution, item.task_id)
        op = db.get(MaterialAssetOperation, dist.operation_id)
        op.status = dist.status = "result_unknown"
        op.remote_response = {
            **op.remote_response,
            "send_armed": True,
            "reconciliation_stopped": True,
        }
        step.distribution_id, step.status, step.dispatch_id, step.lease_token = (
            dist.id,
            "UNKNOWN",
            None,
            None,
        )
        return step.id, step.submission_id, dist.id


def test_image_reissue_preserves_unknown_and_is_idempotent(
    carousel_case, unknown_image
):
    from app.modules.materials.reissue import (
        MaterialReissueInput,
        authorize_material_reissue,
    )

    database, context, *_ = carousel_case
    body = MaterialReissueInput(
        request_id=uuid4(),
        kind="IMAGE",
        old_id=unknown_image[2],
        accepted_duplicate_materials=True,
    )
    with Session(database) as db, db.begin():
        receipt = authorize_material_reissue(
            db,
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            submission_id=unknown_image[1],
            body=body,
        )
    with Session(database) as db, db.begin():
        again = authorize_material_reissue(
            db,
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            submission_id=unknown_image[1],
            body=body,
        )
        assert again == receipt
        old = db.get(MaterialDistribution, unknown_image[2])
        new = db.get(MaterialDistribution, receipt.replacement_id)
        assert old.status == "result_unknown" and old.superseded_by_id == new.id
        op = db.get(MaterialAssetOperation, new.operation_id)
        assert op.status == "pending" and not op.remote_response.get("send_armed")
        assert (
            "video_id" not in op.remote_response
            and op.remote_response["source_image_id"] == "source-image"
        )


@pytest.mark.parametrize(
    "change", ["known_id", "binding", "claimed", "wrong_submission"]
)
def test_image_reissue_rejects_changed_or_known_scope(
    carousel_case, unknown_image, change
):
    from app.core.errors import DomainError
    from app.modules.accounts.connection_models import BCConnectionBinding
    from app.modules.materials.reissue import (
        MaterialReissueInput,
        authorize_material_reissue,
    )

    database, context, *_ = carousel_case
    with Session(database) as db, db.begin():
        dist = db.get(MaterialDistribution, unknown_image[2])
        op = db.get(MaterialAssetOperation, dist.operation_id)
        if change == "known_id":
            op.remote_response = {**op.remote_response, "image_id": "known-image"}
        elif change == "claimed":
            op.attempt_token = uuid4()
        elif change == "binding":
            binding = db.exec(
                select(BCConnectionBinding).where(
                    BCConnectionBinding.tenant_id == context.tenant_id
                )
            ).first()
            binding.revision += 1
    with Session(database) as db, db.begin(), pytest.raises(DomainError):
        authorize_material_reissue(
            db,
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            submission_id=uuid4() if change == "wrong_submission" else unknown_image[1],
            body=MaterialReissueInput(
                request_id=uuid4(),
                kind="IMAGE",
                old_id=unknown_image[2],
                accepted_duplicate_materials=True,
            ),
        )
