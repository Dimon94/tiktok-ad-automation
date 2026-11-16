"""真实历史库升级后，目录序列必须超过对象、素材和空目录的全部历史版本。"""

from datetime import UTC, datetime
from uuid import uuid4

from alembic import command
from sqlalchemy import MetaData, Table, insert, text
from sqlmodel import Session

from app.modules.ads.models import AdMaterialReference, AdObject
from app.modules.ads.sync_models import AdDirectoryRun
from tests.migration_database import historical_database
from tests.modules.reporting.sync_conftest import sync_seed


def test_publish_sequence_starts_above_all_legacy_versions(monkeypatch):
    with historical_database(monkeypatch, "0033_management_rename_values") as (
        engine,
        config,
    ):
        with Session(engine) as session, session.begin():
            seed = sync_seed.__wrapped__(session, historical=True)
            reference = AdMaterialReference(
                tenant_id=seed.tenant_id,
                advertiser_id="acc-1",
                ad_remote_id="ad",
                platform_material_id="vid",
                material_type="VIDEO",
                published_version=800,
            )
            # 0033 的必填创意数组尚无服务端默认值，显式保留旧结构。
            references = Table(
                "ad_material_reference", MetaData(), autoload_with=session.connection()
            )
            session.execute(
                references.insert().values(**reference.model_dump(), creative_ids=[])
            )
            session.add(
                AdObject(
                    tenant_id=seed.tenant_id,
                    advertiser_id="acc-1",
                    kind="ad",
                    remote_id="ad",
                    ad_type="REGULAR",
                    observed_at=datetime.now(UTC),
                    published_version=900,
                )
            )
            route = {
                "tenant_id": str(seed.tenant_id),
                "bc_id": "bc-a",
                "connection_id": str(seed.connection.id),
                "channel": "OFFICIAL_API",
                "authorization_revision": 0,
                "binding_revision": 0,
                "adapter_contract_revision": "official-api-v1",
            }
            legacy_run = AdDirectoryRun(
                tenant_id=seed.tenant_id,
                advertiser_id="acc-1",
                bc_id="bc-a",
                actor_id=seed.actor_id,
                connection_id=seed.connection.id,
                frozen_route=route,
                partition_key=uuid4().hex * 2,
                query={},
                kind="campaign",
                ad_type="REGULAR",
                status="COMPLETE",
                published_version=1000,
            )
            table = Table(
                "ad_directory_run", MetaData(), autoload_with=session.connection()
            )
            session.execute(
                insert(table),
                legacy_run.model_dump(exclude={"workload", "request_sequence"}),
            )
        command.upgrade(config, "0034_directory_publish_version")
        with engine.begin() as connection:
            assert (
                connection.execute(
                    text("SELECT nextval('ad_directory_published_version_seq')")
                ).scalar_one()
                == 1001
            )
        command.downgrade(config, "0033_management_rename_values")
        command.upgrade(config, "head")
        with engine.begin() as connection:
            assert (
                connection.execute(
                    text("SELECT nextval('ad_directory_published_version_seq')")
                ).scalar_one()
                == 1001
            )
