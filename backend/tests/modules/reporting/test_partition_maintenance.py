"""独立维护可跳过读锁且后续无损迁移DEFAULT，不持有调度事务。"""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import text

from app.core.db import engine
from app.modules.reporting.store_models import AdReportDay


def test_partition_maintenance_defers_locked_parent_then_preserves_default(
    committed_sync_seed,
):
    from app.modules.reporting import partitions

    seed, factory = committed_sync_seed
    day = date(7500 + uuid4().int % 1000, 4, 1)
    table = f"ad_report_day_{day:%Y_%m}"
    try:
        with factory() as session, session.begin():
            session.add(
                AdReportDay(
                    tenant_id=seed.tenant_id,
                    advertiser_id="acc-1",
                    stat_date=day,
                    ad_id="maintenance-ad",
                    campaign_id="c",
                    adgroup_id="g",
                    currency="USD",
                    timezone="UTC",
                    spend=Decimal("1.23"),
                    fetched_at=datetime.now(UTC),
                    sync_job_id=uuid4(),
                )
            )
        with factory() as reader, reader.begin():
            reader.execute(text("SELECT 1 FROM ad_report_day LIMIT 1"))
            result = partitions.maintain_partitions(
                engine, today=day, limit=1, tables=("ad_report_day",)
            )
            assert result == {"created": 0, "deferred": 1}
        assert partitions.maintain_partitions(
            engine, today=day, limit=1, tables=("ad_report_day",)
        ) == {"created": 1, "deferred": 0}
        with factory() as check:
            row = check.execute(
                text(
                    "SELECT spend, tableoid::regclass::text FROM ad_report_day WHERE tenant_id=:tenant AND ad_id=:id"
                ),
                {"tenant": seed.tenant_id, "id": "maintenance-ad"},
            ).one()
            assert row == (Decimal("1.23"), table)
    finally:
        with factory() as session, session.begin():
            session.execute(text(f"DROP TABLE IF EXISTS {table}"))
