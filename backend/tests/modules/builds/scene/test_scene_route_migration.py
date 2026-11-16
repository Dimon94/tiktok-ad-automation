"""旧场景/草稿迁移保留事实；仅使用独立历史 PostgreSQL 数据库。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import MetaData, Table
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.accounts.models import AdvertiserAccount, TikTokConnection
from app.modules.builds.models import BuildDraft, DraftPreparation
from app.modules.builds.scene_job_models import (
    DraftScenePreparation,
    SceneJob,
    SceneJobPage,
)
from app.modules.providers.models import ProviderConnection
from app.modules.strategies.copy_pool import POOL_VERSION
from app.modules.strategies.models import Strategy, StrategyVersion
from tests.migration_database import (
    historical_database,
    insert_historical_application,
    insert_historical_bc,
)
from tests.modules.conftest import create_context


def test_historical_scene_migration_preserves_ids_and_facts_without_inventing_route(
    monkeypatch,
):
    with historical_database(monkeypatch, "mcp_build_routes") as (engine, config):
        with Session(engine) as db, db.begin():
            context = create_context(db)
            insert_historical_bc(db, tenant_id=context.tenant_id, bc_id="old-bc")
            conn = TikTokConnection(tenant_id=context.tenant_id, status="ACTIVE")
            provider = ProviderConnection(
                tenant_id=context.tenant_id,
                kind="wangyan",
                display_name="Synthetic",
                encrypted_credentials="synthetic",
            )
            db.add_all([conn, provider])
            account = AdvertiserAccount(
                tenant_id=context.tenant_id, advertiser_id="old-advertiser"
            )
            # 历史 schema 只写当时存在的列，不能由今日 ORM 补入后续字段。
            account_table = Table(
                "advertiser_account", MetaData(), autoload_with=db.connection()
            )
            db.execute(
                account_table.insert().values(
                    **{
                        key: value
                        for key, value in account.model_dump().items()
                        if key in account_table.c
                    }
                )
            )
            db.flush()
            insert_historical_application(
                db,
                tenant_id=context.tenant_id,
                connection_id=provider.id,
                external_id="old-app",
                name="Synthetic",
            )
            strategy = Strategy(
                tenant_id=context.tenant_id, name="Historical", latest_version=1
            )
            db.add(strategy)
            db.flush()
            version = StrategyVersion(
                tenant_id=context.tenant_id,
                strategy_id=strategy.id,
                number=1,
                copy_pool_version_id=POOL_VERSION,
                config={
                    "budget": "100",
                    "currency": "USD",
                    "target_roas": "1",
                    "group_size": 10,
                    "creative_count": 2,
                    "copy_pool_version": str(POOL_VERSION),
                },
                budget=100,
                target_roas=1,
                created_by=context.actor_id,
                request_id=uuid4(),
                request_digest="a" * 64,
                request_kind="create",
            )
            db.add(version)
            db.flush()
            draft = BuildDraft(
                tenant_id=context.tenant_id,
                bc_id="old-bc",
                status="READY",
                strategy_version_id=version.id,
                provider_connection_id=provider.id,
                application_id="old-app",
                link_config={},
                created_by=context.actor_id,
                request_id=uuid4(),
                request_digest="d" * 64,
            )
            # 旧库没有之后新增的编辑连接偏好，不能用今日 ORM INSERT 播种。
            draft_table = Table(
                "build_draft", MetaData(), autoload_with=db.connection()
            )
            assert "execution_connection_id" not in draft_table.c
            db.execute(
                draft_table.insert().values(
                    **{
                        key: value
                        for key, value in draft.model_dump().items()
                        if key in draft_table.c
                    }
                )
            )
            prep = DraftPreparation(
                tenant_id=context.tenant_id,
                draft_id=draft.id,
                draft_revision=1,
                actor_id=context.actor_id,
                request_id=uuid4(),
                status="READY",
                phase="done",
            )
            db.add(prep)
            db.flush()
            job = SceneJob(
                tenant_id=context.tenant_id,
                actor_id=context.actor_id,
                bc_id="old-bc",
                advertiser_id="old-advertiser",
                connection_id=conn.id,
                credential_revision=7,
                provider_connection_id=provider.id,
                application_id="old-app",
                minis_id="old-minis",
                scope_basis="b" * 64,
                status="COMPLETE",
                resource="done",
                facts={"identity": {"matches": [{"identity_id": "old-remote-id"}]}},
                claim_token=uuid4(),
                claimed_until=datetime.now(UTC) + timedelta(minutes=1),
            )
            page = SceneJobPage(
                tenant_id=context.tenant_id,
                job_id=job.id,
                resource="identity",
                page=1,
                endpoint="/old/identity",
                request_id="old-request",
                source_revision="old-contract",
                scope_basis=job.scope_basis,
                facts={"remote_id": "old-remote-id"},
                observed_at=datetime.now(UTC),
            )
            state = DraftScenePreparation(
                tenant_id=context.tenant_id, draft_id=draft.id, preparation_id=prep.id
            )
            for model, row in (
                (SceneJob, job),
                (SceneJobPage, page),
                (DraftScenePreparation, state),
            ):
                table = Table(
                    model.__tablename__, MetaData(), autoload_with=db.connection()
                )
                db.execute(
                    table.insert().values(
                        **{
                            key: value
                            for key, value in row.model_dump().items()
                            if key in table.c
                        }
                    )
                )
            job_id, page_id, draft_id, prep_id = job.id, page.id, draft.id, prep.id
            route = FrozenTikTokRoute(
                tenant_id=context.tenant_id,
                bc_id="old-bc",
                connection_id=conn.id,
                channel="OFFICIAL_API",
                authorization_revision=0,
                adapter_contract_revision="official-api-v1",
            )
        command.upgrade(config, "mcp02")
        with Session(engine) as db:
            scene_table = Table(
                "build_scene_job", MetaData(), autoload_with=db.connection()
            )
            restored = db.execute(
                select(scene_table).where(scene_table.c.id == job_id)
            ).one()
            assert (
                restored.id == job_id and restored.connection_id == route.connection_id
            )
            assert restored.facts == job.facts and restored.credential_revision == 7
            assert (
                restored.status == "STALE"
                and restored.error_code == "scene_route_missing"
            )
            assert (
                restored.frozen_route
                is restored.claim_token
                is restored.claimed_until
                is None
            )
            receipt = db.get(SceneJobPage, page_id)
            assert receipt.request_id == "old-request" and receipt.facts == page.facts
            assert receipt.mcp_request_id is receipt.remote_task_id is None
            assert db.get(DraftPreparation, prep_id).status == "BLOCKED"
            assert (
                db.execute(
                    select(draft_table.c.status).where(draft_table.c.id == draft_id)
                ).scalar_one()
                == "BLOCKED"
            )
            state_table = Table(
                "draft_scene_preparation", MetaData(), autoload_with=db.connection()
            )
            assert (
                db.execute(
                    select(state_table.c.frozen_route).where(
                        state_table.c.tenant_id == context.tenant_id,
                        state_table.c.preparation_id == prep_id,
                    )
                ).scalar_one()
                is None
            )
        for model, key in (
            (SceneJob, job_id),
            (DraftScenePreparation, (context.tenant_id, prep_id)),
        ):
            with Session(engine) as db, pytest.raises(IntegrityError):
                if model is SceneJob:
                    db.execute(
                        scene_table.update()
                        .where(scene_table.c.id == key)
                        .values(frozen_route=route.model_dump(mode="json"))
                    )
                else:
                    table = Table(
                        model.__tablename__, MetaData(), autoload_with=db.connection()
                    )
                    db.execute(
                        table.update()
                        .where(
                            table.c.tenant_id == key[0],
                            table.c.preparation_id == key[1],
                        )
                        .values(frozen_route=route.model_dump(mode="json"))
                    )
                db.commit()


def test_new_scene_route_is_immutable_and_invalid_json_cannot_be_inserted(
    database_engine, scene_case
):
    from tests.modules.builds.scene.support import ensure

    result = ensure(database_engine, scene_case)
    with Session(database_engine) as db:
        original = db.get(SceneJob, result.job_id).model_dump()
    with Session(database_engine) as db, pytest.raises(IntegrityError):
        db.get(SceneJob, result.job_id).frozen_route = {
            **original["frozen_route"],
            "authorization_revision": 99,
        }
        db.commit()
    for invalid in (
        {},
        {**original["frozen_route"], "tenant_id": str(uuid4())},
        {**original["frozen_route"], "authorization_revision": True},
        {**original["frozen_route"], "channel": "OTHER"},
    ):
        with Session(database_engine) as db, pytest.raises(IntegrityError):
            db.add(
                SceneJob(
                    **(
                        original
                        | {
                            "id": uuid4(),
                            "scope_basis": uuid4().hex,
                            "frozen_route": invalid,
                        }
                    )
                )
            )
            db.commit()
