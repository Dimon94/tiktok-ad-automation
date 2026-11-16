"""迁移必须保留旧快照的原成员，不能回填为今日目录。"""

from datetime import UTC, datetime
from uuid import uuid4

from alembic import command
from sqlalchemy import text
from sqlmodel import Session

from app.modules.ads.models import AdObject
from app.modules.reporting.query_models import QuerySnapshot
from app.modules.reporting.store_models import ReportSyncState
from tests.migration_database import historical_database, insert_historical_row
from tests.modules.reporting.sync_conftest import sync_seed
from tests.modules.reporting.test_queries import filters_for


def test_membership_migration_preserves_old_snapshot_and_round_trips(monkeypatch):
    with historical_database(monkeypatch, "0102_business_rate_periods") as (
        engine,
        config,
    ):
        with Session(engine) as session, session.begin():
            seed = sync_seed.__wrapped__(session, historical=True)
            session.add(
                AdObject(
                    tenant_id=seed.tenant_id,
                    advertiser_id="acc-1",
                    kind="campaign",
                    remote_id="new-current",
                    name="new",
                    ad_type="REGULAR",
                    observed_at=datetime.now(UTC),
                    published_version=1,
                )
            )
            snapshot = QuerySnapshot(
                id=uuid4(),
                tenant_id=seed.tenant_id,
                bc_id="bc-a",
                actor_id=seed.actor_id,
                advertiser_ids=["acc-1"],
                filters=filters_for("account").model_dump(mode="json"),
                filter_digest="a" * 64,
                total=1,
                naming_versions={"directory": 1, "acc-1:old-deleted": 7},
            )
            insert_historical_row(session, snapshot)
            session.flush()
            # 0081允许删除账户后保留canonical金额/状态；派生回填只能取存活账户。
            insert_historical_row(
                session,
                ReportSyncState(
                    tenant_id=seed.tenant_id,
                    advertiser_id="deleted-account",
                    report_kind="ad_day",
                    stat_date=filters_for("account").start_date,
                    status="COMPLETE",
                    row_count=1,
                ),
            )
            for advertiser, count in (("acc-1", 1), ("acc-2", 0)):
                insert_historical_row(
                    session,
                    ReportSyncState(
                        tenant_id=seed.tenant_id,
                        advertiser_id=advertiser,
                        report_kind="ad_day",
                        stat_date=filters_for("account").start_date,
                        status="COMPLETE",
                        row_count=count,
                    ),
                )
            session.execute(
                text("""INSERT INTO campaign_report_day
              (tenant_id,advertiser_id,stat_date,campaign_id,campaign_name,spend,currency,timezone,fetched_at,sync_job_id)
              VALUES(:t,'deleted-account',date '2026-09-30','old','history',9,'USD','UTC',now(),gen_random_uuid()),
                    (:t,'acc-1',date '2026-09-30','new-current','live',5,'USD','UTC',now(),gen_random_uuid())"""),
                {"t": seed.tenant_id},
            )
            session.flush()
            session.execute(
                text("""INSERT INTO query_snapshot_row
              (snapshot_id,row_key,stable_sequence,dimension,display,refs,material_uses,metric_buckets,
               capabilities,directory_versions,membership_digest)
              VALUES(:s,'acc-1:account:acc-1',0,'account','{}',
                jsonb_build_array(jsonb_build_object('tenant_id',CAST(:t AS text),'advertiser_id','acc-1',
                    'kind','campaign','remote_id','old-deleted')),'[]','[]','{}','{}','')"""),
                {"s": snapshot.id, "t": seed.tenant_id},
            )
            identity = snapshot.id
        for _cycle in range(2):
            command.upgrade(config, "0103_account_report_reads")
            with engine.connect() as connection:
                live = connection.execute(
                    text("""SELECT d.spend,d.source_revision,s.revision,s.status
                    FROM account_report_day d JOIN account_report_day_state s USING(tenant_id,advertiser_id,stat_date)
                    WHERE d.tenant_id=:t AND d.advertiser_id='acc-1'"""),
                    {"t": seed.tenant_id},
                ).one()
                assert tuple(live) == (5, 1, 1, "COMPLETE")
                assert (
                    connection.execute(
                        text(
                            "SELECT status FROM account_report_day_state WHERE tenant_id=:t AND advertiser_id='acc-2'"
                        ),
                        {"t": seed.tenant_id},
                    ).scalar_one()
                    == "COMPLETE"
                )
                assert (
                    connection.execute(
                        text(
                            "SELECT count(*) FROM account_report_day WHERE tenant_id=:t AND advertiser_id='acc-2'"
                        ),
                        {"t": seed.tenant_id},
                    ).scalar_one()
                    == 0
                )
                assert (
                    connection.execute(
                        text(
                            "SELECT count(*) FROM account_report_day_state WHERE advertiser_id='deleted-account'"
                        )
                    ).scalar_one()
                    == 0
                )
                assert (
                    connection.execute(
                        text(
                            "SELECT count(*) FROM account_report_day WHERE advertiser_id='deleted-account'"
                        )
                    ).scalar_one()
                    == 0
                )
                assert (
                    connection.execute(
                        text(
                            "SELECT spend FROM campaign_report_day WHERE advertiser_id='deleted-account'"
                        )
                    ).scalar_one()
                    == 9
                )
                row = connection.execute(
                    text("""SELECT q.refs,v.refs AS version_refs,v.naming_versions
                  FROM query_snapshot_row q JOIN account_membership_version v ON v.id=q.account_membership_version_id
                  WHERE q.snapshot_id=:s"""),
                    {"s": identity},
                ).one()
                assert row.refs == []
                assert row.version_refs[0]["remote_id"] == "old-deleted"
                assert row.naming_versions == {"acc-1:old-deleted": 7}
                current = connection.execute(
                    text("""SELECT v.refs FROM account_membership_current h
                    JOIN account_membership_version v ON v.id=h.version_id
                    WHERE h.tenant_id=:t AND h.advertiser_id='acc-1'"""),
                    {"t": seed.tenant_id},
                ).scalar_one()
                assert current[0]["remote_id"] == "new-current"
            command.downgrade(config, "0102_business_rate_periods")
            with engine.connect() as connection:
                row = connection.execute(
                    text("SELECT refs FROM query_snapshot_row WHERE snapshot_id=:s"),
                    {"s": identity},
                ).scalar_one()
                assert row[0]["remote_id"] == "old-deleted"
                assert connection.execute(
                    text("SELECT naming_versions FROM query_snapshot WHERE id=:s"),
                    {"s": identity},
                ).scalar_one() == {"directory": 1, "acc-1:old-deleted": 7}
