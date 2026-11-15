"""源视频成功事件的真实账本集成；无 TikTok 外部请求。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.models import PendingDispatch
from app.modules.accounts.models import BCAccountAccess
from app.modules.materials.cover_models import MaterialCoverJob
from app.modules.materials.models import AccountMaterial, MaterialAssetOperation
from app.modules.materials.source_cover_service import (
    prepare_source_cover,
    repair_source_cover_starts,
)
from tests.modules.materials.test_url_ingest import (
    operation,
    run,
)
from tests.modules.materials.test_url_ingest import (
    source_env as source_env,
)
from tests.modules.materials.test_url_ingest import (
    url_env as url_env,
)
from tests.modules.materials.test_url_ingest import (
    wire as wire,
)


def ready_source(env, redis_client, wire):
    wire[1].append(
        [{"video_id": "actual-source-vid", "material_id": "actual-source-mid"}]
    )
    run(env, redis_client)
    return operation(env).id


def source_event(db, env, *, operation_id=None):
    operation_id = operation_id or operation(env).id
    return db.exec(
        select(PendingDispatch).where(
            PendingDispatch.tenant_id == env["context"].tenant_id,
            PendingDispatch.task_name == "materials.prepare_source_cover",
            PendingDispatch.task_key == f"source-cover:{operation_id}",
        )
    ).one()


def test_source_event_creates_one_upload_authorized_cover_and_stops_start_repair(
    url_env, redis_client, wire
):
    operation_id = ready_source(url_env, redis_client, wire)
    with Session(engine) as db, db.begin():
        grant = db.exec(
            select(BCAccountAccess).where(
                BCAccountAccess.tenant_id == url_env["context"].tenant_id,
                BCAccountAccess.advertiser_id == "actual-account",
            )
        ).one()
        grant.can_build = False
        event = source_event(db, url_env)
        for _ in range(2):
            prepare_source_cover(
                db,
                context=url_env["context"],
                operation_id=operation_id,
                dispatch_id=event.id,
            )
        jobs = db.exec(
            select(MaterialCoverJob).where(
                MaterialCoverJob.material_id == url_env["material_id"],
            )
        ).all()
        assert len(jobs) == 1
        assert jobs[0].purpose == "SOURCE"
        assert jobs[0].advertiser_id == "actual-account"
        event.published_at = datetime.now(UTC) - timedelta(minutes=2)
        db.flush()
        assert repair_source_cover_starts(db) == 0
        jobs[0].status, jobs[0].error_code = "BLOCKED", "cover_unavailable"
        db.flush()
        assert db.get(MaterialAssetOperation, operation_id).status == "succeeded"
        assert (
            db.exec(
                select(AccountMaterial.status).where(
                    AccountMaterial.material_id == url_env["material_id"],
                )
            ).one()
            == "available"
        )


def test_source_event_cannot_borrow_another_tenant_or_dispatch(
    url_env, redis_client, wire
):
    operation_id = ready_source(url_env, redis_client, wire)
    with Session(engine) as db, db.begin():
        event = source_event(db, url_env)
        foreign = TenantContext(
            tenant_id=uuid4(), actor_id=url_env["context"].actor_id, role="operator"
        )
        with pytest.raises(DomainError):
            prepare_source_cover(
                db, context=foreign, operation_id=operation_id, dispatch_id=event.id
            )
        with pytest.raises(DomainError):
            prepare_source_cover(
                db,
                context=url_env["context"],
                operation_id=operation_id,
                dispatch_id=uuid4(),
            )
        assert not db.exec(
            select(MaterialCoverJob).where(
                MaterialCoverJob.material_id == url_env["material_id"],
            )
        ).all()


def test_delayed_source_event_does_not_prepare_a_replaced_video(
    url_env, redis_client, wire
):
    operation_id = ready_source(url_env, redis_client, wire)
    with Session(engine) as db, db.begin():
        event = source_event(db, url_env)
        asset = db.exec(
            select(AccountMaterial).where(
                AccountMaterial.material_id == url_env["material_id"],
            )
        ).one()
        asset.video_id = "replacement-video"
        db.flush()
        prepare_source_cover(
            db,
            context=url_env["context"],
            operation_id=operation_id,
            dispatch_id=event.id,
        )
        assert not db.exec(
            select(MaterialCoverJob).where(
                MaterialCoverJob.material_id == url_env["material_id"],
            )
        ).all()
        event.published_at = datetime.now(UTC) - timedelta(minutes=2)
        db.flush()
        assert repair_source_cover_starts(db) == 0


def test_lost_source_event_is_republished_when_only_build_job_exists(
    url_env, redis_client, wire
):
    from app.modules.materials.covers import ensure_cover
    from app.modules.materials.routes import load_material_route

    operation_id = ready_source(url_env, redis_client, wire)
    with Session(engine) as db, db.begin():
        op = db.get(MaterialAssetOperation, operation_id)
        ensure_cover(
            db,
            context=url_env["context"],
            bc_id=op.bc_id,
            material_id=op.material_id,
            advertiser_id=op.advertiser_id,
            task_key="build-first",
            route=load_material_route(
                op.frozen_route, context=url_env["context"], bc_id=op.bc_id
            ),
        )
        event = source_event(db, url_env)
        event.published_at = datetime.now(UTC) - timedelta(minutes=2)
        db.flush()
        assert repair_source_cover_starts(db) == 1
        prepare_source_cover(
            db,
            context=url_env["context"],
            operation_id=operation_id,
            dispatch_id=event.id,
        )
        jobs = db.exec(
            select(MaterialCoverJob).where(
                MaterialCoverJob.material_id == url_env["material_id"],
            )
        ).all()
        assert len(jobs) == 1
        assert jobs[0].purpose == "SOURCE"
        event.published_at = datetime.now(UTC) - timedelta(minutes=2)
        db.flush()
        assert repair_source_cover_starts(db) == 0


def test_source_event_acknowledgment_rolls_back_with_cover_creation(
    url_env, redis_client, wire
):
    operation_id = ready_source(url_env, redis_client, wire)
    with pytest.raises(RuntimeError, match="commit interrupted"):
        with Session(engine) as db, db.begin():
            event = source_event(db, url_env)
            prepare_source_cover(
                db,
                context=url_env["context"],
                operation_id=operation_id,
                dispatch_id=event.id,
            )
            assert db.get(MaterialAssetOperation, operation_id).remote_response[
                "source_cover_dispatch_id"
            ] == str(event.id)
            raise RuntimeError("commit interrupted")
    with Session(engine) as db, db.begin():
        op = db.get(MaterialAssetOperation, operation_id)
        assert "source_cover_dispatch_id" not in op.remote_response
        assert not db.exec(
            select(MaterialCoverJob).where(
                MaterialCoverJob.material_id == url_env["material_id"],
            )
        ).all()
        event = source_event(db, url_env)
        event.published_at = datetime.now(UTC) - timedelta(minutes=2)
        db.flush()
        assert repair_source_cover_starts(db) == 1
