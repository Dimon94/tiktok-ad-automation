from datetime import UTC, datetime
from uuid import uuid4

from alembic import command
from sqlalchemy import MetaData, Table, insert, text
from sqlmodel import Session

from app.modules.ads.sync_models import AdDirectoryRun
from tests.migration_database import historical_database
from tests.modules.reporting.sync_conftest import sync_seed


def test_stacked_queued_directory_runs_are_cancelled(monkeypatch):
    """积压叠加的排队运行每组只留最新一条；周期“只读在投”运行与已领取运行按规则处理。"""
    with historical_database(monkeypatch, "0030_actor_user_reference") as (
        engine,
        config,
    ):
        with Session(engine) as session, session.begin():
            seed = sync_seed.__wrapped__(session, historical=True)
            route = {
                "tenant_id": str(seed.tenant_id),
                "bc_id": "bc-a",
                "connection_id": str(seed.connection.id),
                "channel": "OFFICIAL_API",
                "authorization_revision": 0,
                "adapter_contract_revision": "official-api-v1",
                "binding_revision": 0,
            }

            def run(partition, *, include_deleted=True, ids=(), claimed=False):
                row = AdDirectoryRun(
                    tenant_id=seed.tenant_id,
                    advertiser_id="acc-1",
                    bc_id="bc-a",
                    actor_id=seed.actor_id,
                    connection_id=seed.connection.id,
                    channel="OFFICIAL_API",
                    frozen_route=route,
                    partition_key=partition * 64,
                    query={
                        "include_deleted": include_deleted,
                        "ids": list(ids),
                        "parent_ids": [],
                    },
                    status="QUEUED",
                    claim_generation=1,
                    claim_token=uuid4() if claimed else None,
                    observed_at=datetime.now(UTC),
                    kind="campaign",
                    ad_type="SMART_PLUS",
                )
                table = Table(
                    "ad_directory_run", MetaData(), autoload_with=session.connection()
                )
                session.execute(
                    insert(table),
                    row.model_dump(exclude={"workload", "request_sequence"}),
                )
                return row.id

            older, newest = run("a"), run("a")
            claimed_old = run("b", claimed=True)
            claimed_new = run("b")
            active = run("c", include_deleted=False)
            targeted = run("d", include_deleted=False, ids=("1",))
        command.upgrade(config, "0031_cancel_stacked_dir_runs")
        with Session(engine) as session:
            status = dict(
                session.connection()
                .execute(
                    text(
                        "SELECT id,status FROM ad_directory_run WHERE tenant_id=:tenant"
                    ),
                    {"tenant": seed.tenant_id},
                )
                .all()
            )
        assert status[older] == "CANCELLED"
        assert status[newest] == "QUEUED"
        # 已被领取的运行不动；它不参与排序，未领取的一条是该组唯一排队者。
        assert status[claimed_old] == "QUEUED"
        assert status[claimed_new] == "QUEUED"
        assert status[active] == "CANCELLED"
        assert status[targeted] == "QUEUED"
