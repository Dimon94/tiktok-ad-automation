"""目录状态索引迁移往返，不删除运行账本或丢失空目录发布。"""

from uuid import uuid4

from alembic import command
from sqlalchemy import text
from sqlmodel import Session

from app.modules.ads.sync_models import AdDirectoryRun
from tests.migration_database import historical_database
from tests.modules.reporting.sync_conftest import sync_seed
from tests.modules.reporting.test_jobs import _route

REVISION = "0117_directory_status_version"
PREVIOUS = "0116_drop_legacy_material"
INDEX = "ix_directory_status_version"


def test_directory_status_index_roundtrip_preserves_publications(monkeypatch):
    with historical_database(monkeypatch, PREVIOUS) as (engine, config):
        with Session(engine) as session, session.begin():
            seed = sync_seed.__wrapped__(session, historical=True)
            run = AdDirectoryRun(
                tenant_id=seed.tenant_id,
                advertiser_id="acc-1",
                bc_id="bc-a",
                actor_id=seed.actor_id,
                connection_id=seed.connection.id,
                channel="OFFICIAL_API",
                frozen_route=_route(seed),
                partition_key=uuid4().hex * 2,
                query={},
                kind="ad",
                ad_type="REGULAR",
                status="COMPLETE",
                published_version=7,
            )
            session.add(run)
        with engine.connect() as connection:
            before = connection.execute(
                text("SELECT id,status,published_version FROM ad_directory_run")
            ).all()
        command.upgrade(config, REVISION)
        with engine.connect() as connection:
            index = connection.execute(
                text("""
              SELECT indisvalid,indisready,pg_get_indexdef(indexrelid) definition
              FROM pg_index WHERE indexrelid=to_regclass(:index)
            """),
                {"index": INDEX},
            ).one()
            assert index.indisvalid and index.indisready
            assert (
                "(tenant_id, bc_id, advertiser_id, published_version DESC)"
                in index.definition
            )
            assert (
                "COMPLETE" in index.definition
                and "published_version IS NOT NULL" in index.definition
            )
        command.downgrade(config, PREVIOUS)
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT to_regclass(:index)"), {"index": INDEX}
                ).scalar_one()
                is None
            )
            assert (
                connection.execute(
                    text("SELECT id,status,published_version FROM ad_directory_run")
                ).all()
                == before
            )
        command.upgrade(config, REVISION)
        # 分段退休的 0117 刻意保留模型外旧列，不能用模型差异宣告可删列。
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='ad_material_reference' AND column_name='creative_ids')"
                )
            ).scalar_one()
