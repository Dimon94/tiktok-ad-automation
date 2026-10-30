"""从真实0069结构升级，保留旧任务私有页、水位和冻结授权，再原地映射。"""

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from alembic import command
from sqlalchemy import MetaData, Table, text
from sqlmodel import Session, select

from app.modules.accounts.models import TikTokConnection
from app.modules.reporting.store_models import (
    ReportSyncJob,
    ReportSyncPage,
    ReportSyncState,
)
from app.modules.sync_control.migration import adopt_legacy_batch
from app.modules.sync_control.models import SyncExecutionScope
from tests.migration_database import (
    historical_database,
    insert_historical_account,
    insert_historical_bc,
)
from tests.modules.conftest import create_context


def historical_insert(session, row):
    table = Table(row.__tablename__, MetaData(), autoload_with=session.connection())
    session.execute(
        table.insert().values(
            **{key: value for key, value in row.model_dump().items() if key in table.c}
        )
    )


def test_0069_upgrade_retains_partial_execution_and_observation_without_inventing_watermark(
    monkeypatch,
):
    with historical_database(monkeypatch, "0069_management_batches") as (
        engine,
        config,
    ):
        now = datetime.now(UTC)
        with Session(engine) as session, session.begin():
            context = create_context(session)
            connection = TikTokConnection(tenant_id=context.tenant_id, status="ACTIVE")
            session.add(connection)
            insert_historical_bc(session, tenant_id=context.tenant_id, bc_id="bc-old")
            insert_historical_account(
                session, tenant_id=context.tenant_id, advertiser_id="account-old"
            )
            session.flush()
            route = {
                "tenant_id": str(context.tenant_id),
                "bc_id": "bc-old",
                "connection_id": str(connection.id),
                "channel": "OFFICIAL_API",
                "authorization_revision": 0,
                "adapter_contract_revision": "official-api-v1",
                "binding_revision": 0,
            }
            job = ReportSyncJob(
                tenant_id=context.tenant_id,
                actor_id=context.actor_id,
                bc_id="bc-old",
                kind="ad_day",
                priority=3,
                status="RUNNING",
                advertiser_ids=["account-old"],
                start_date=date(2026, 10, 1),
                end_date=date(2026, 10, 2),
                job_key=str(uuid4()),
                frozen_route=route,
                due_at=now,
                claim_token=uuid4(),
                claimed_until=now - timedelta(seconds=1),
                progress={"requested_at": now.isoformat(), "next_page": 2},
            )
            historical_insert(session, job)
            page = ReportSyncPage(
                tenant_id=context.tenant_id,
                job_id=job.id,
                page=1,
                scope_key="immutable",
                rows=[{"metrics": {"spend": "1.2500"}, "dimensions": {"ad_id": "old"}}],
                row_keys=["old"],
                total_page=2,
                total_number=2,
            )
            historical_insert(session, page)
            state = ReportSyncState(
                tenant_id=context.tenant_id,
                advertiser_id="account-old",
                report_kind="ad_day",
                stat_date=date(2026, 10, 1),
                status="COMPLETE",
                last_success_at=now,
            )
            historical_insert(session, state)
            identifier, tenant_id = job.id, context.tenant_id
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        with Session(engine) as session, session.begin():
            restored = session.get(ReportSyncJob, identifier)
            assert restored.frozen_route == route and restored.progress == job.progress
            assert session.get(ReportSyncPage, (identifier, 1)).rows == page.rows
            restored_state = session.get(
                ReportSyncState, (tenant_id, "account-old", "ad_day", date(2026, 10, 1))
            )
            assert (
                restored_state.observed_at is None
                and restored_state.last_success_at == now
            )
            result = adopt_legacy_batch(session, kind="report", consumers_stopped=True)
            assert result.adopted == 1 and not result.errors
            assert (
                len(
                    session.exec(
                        select(SyncExecutionScope).where(
                            SyncExecutionScope.report_job_id == identifier
                        )
                    ).all()
                )
                == 2
            )
            assert (
                adopt_legacy_batch(
                    session, kind="report", consumers_stopped=True
                ).adopted
                == 0
            )
        with engine.connect() as db:
            assert (
                db.execute(
                    text("SELECT count(*) FROM report_sync_page WHERE job_id=:id"),
                    {"id": identifier},
                ).scalar_one()
                == 1
            )
