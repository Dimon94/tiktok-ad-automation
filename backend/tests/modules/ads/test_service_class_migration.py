"""真实旧库升级保留任务状态/领取围栏，只给确定的全量目录分配后台用途。"""

from uuid import uuid4

from alembic import command
from sqlalchemy import MetaData, Table, insert, text
from sqlmodel import Session

from app.modules.ads.sync_models import AdDirectoryRun
from tests.migration_database import historical_database
from tests.modules.reporting.sync_conftest import sync_seed
from tests.modules.reporting.test_jobs import _route


def test_existing_requests_keep_claims_and_get_service_class(monkeypatch):
    with historical_database(monkeypatch, "0038_sync_work_indexes") as (engine, config):
        with Session(engine) as session, session.begin():
            seed = sync_seed.__wrapped__(session, historical=True)
            table = Table(
                "ad_directory_run", MetaData(), autoload_with=session.connection()
            )
            originals = {}
            for ids in ([], ["one"]):
                row = AdDirectoryRun(
                    tenant_id=seed.tenant_id,
                    advertiser_id="acc-1",
                    bc_id="bc-a",
                    actor_id=seed.actor_id,
                    connection_id=seed.connection.id,
                    frozen_route=_route(seed),
                    partition_key=uuid4().hex * 2,
                    query={"ids": ids, "parent_ids": []},
                    kind="ad",
                    ad_type="SMART_PLUS",
                    status="RUNNING",
                    claim_generation=7,
                    claim_token=uuid4(),
                )
                session.execute(
                    insert(table),
                    row.model_dump(exclude={"workload", "request_sequence"}),
                )
                originals[row.id] = (row.claim_token, "live" if ids else "background")
        command.upgrade(config, "head")
        with engine.begin() as connection:
            rows = connection.execute(
                text(
                    "SELECT id,claim_token,claim_generation,status,workload FROM ad_directory_run"
                )
            ).all()
            assert len(rows) == 2
            for row in rows:
                assert (row.claim_token, row.workload) == originals[row.id]
                assert row.claim_generation == 7 and row.status == "RUNNING"
