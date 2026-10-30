"""真实 PG：发布不运行分区DDL，维护独立迁移DEFAULT且发布保持原子性。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.modules.reporting import publish
from app.modules.reporting.partitions import ensure_month_partitions_for_range
from app.modules.reporting.store_models import AdReportDay, MaterialReportDay
from tests.modules.reporting.sync_conftest import ad_row

TABLES = {"ad": "ad_report_day", "material": "material_report_day"}


@pytest.fixture
def missing_month(committed_sync_seed):
    _, factory = committed_sync_seed
    value = uuid4().int
    day = date(3500 + value % 5000, 1 + value % 12, 3)
    with factory() as session:
        for table in TABLES.values():
            assert (
                session.execute(
                    text("SELECT to_regclass(:name)"),
                    {"name": f"{table}_{day:%Y_%m}"},
                ).scalar()
                is None
            )
    try:
        yield day
    finally:
        # 随机月份由本测试独占；DDL与事实测试数据都不留给后续用例。
        with factory() as session, session.begin():
            for table in TABLES.values():
                session.execute(text(f"DROP TABLE IF EXISTS {table}_{day:%Y_%m}"))


def _publish(session, seed, kind, day, account="acc-1"):
    row = (
        ad_row(f"new-{account}", day, adv=account)
        if kind == "ad"
        else {
            "dimensions": {
                "main_material_id": f"new-{account}",
                "main_material_type": "VIDEO",
                "stat_time_day": str(day),
            },
            "metrics": {"advertiser_id": account, "spend": "1"},
        }
    )
    function = publish.publish_ad_day if kind == "ad" else publish.publish_material_day
    return function(
        session,
        tenant_id=seed.tenant_id,
        job_id=uuid4(),
        accounts={account: publish.AccountMeta("USD", "UTC")},
        start=day,
        end=day,
        rows=[row],
    )


def _default_row(session, seed, kind, day):
    model = AdReportDay if kind == "ad" else MaterialReportDay
    identity = (
        {"ad_id": "existing", "campaign_id": "c", "adgroup_id": "g"}
        if kind == "ad"
        else {"main_material_id": "existing", "main_material_type": "VIDEO"}
    )
    session.add(
        model(
            tenant_id=seed.tenant_id,
            advertiser_id="acc-2",
            stat_date=day,
            currency="USD",
            timezone="UTC",
            fetched_at=datetime.now(UTC),
            sync_job_id=uuid4(),
            spend=Decimal("7.25"),
            **identity,
        )
    )


def _facts(session, seed, kind, day):
    return session.execute(
        text(f"""SELECT advertiser_id,spend,tableoid::regclass::text
          FROM {TABLES[kind]} WHERE tenant_id=:tenant AND stat_date=:day
          ORDER BY advertiser_id"""),
        {"tenant": seed.tenant_id, "day": day},
    ).all()


@pytest.mark.parametrize("kind", ["ad", "material"])
def test_publish_does_not_wait_for_other_fact_table_reader(
    committed_sync_seed, missing_month, kind
):
    seed, factory = committed_sync_seed
    other = TABLES["material" if kind == "ad" else "ad"]
    with factory() as reader, reader.begin():
        reader.execute(text(f"SELECT 1 FROM {other} LIMIT 1"))
        with factory() as writer, writer.begin():
            writer.execute(text("SET LOCAL lock_timeout='300ms'"))
            assert _publish(writer, seed, kind, missing_month) == 1
    with factory() as session:
        assert _facts(session, seed, kind, missing_month) == [
            ("acc-1", Decimal("1"), f"{TABLES[kind]}_default")
        ]


@pytest.mark.parametrize("kind", ["ad", "material"])
def test_independent_maintenance_moves_only_selected_fact_default_rows(
    committed_sync_seed, missing_month, kind
):
    seed, factory = committed_sync_seed
    other = "material" if kind == "ad" else "ad"
    with factory() as session, session.begin():
        _default_row(session, seed, kind, missing_month)
        _default_row(session, seed, other, missing_month)
    with factory() as session, session.begin():
        assert _publish(session, seed, kind, missing_month) == 1
    with factory() as session, session.begin():
        ensure_month_partitions_for_range(
            session, missing_month, missing_month, tables=(TABLES[kind],)
        )
    with factory() as session:
        partition = f"{TABLES[kind]}_{missing_month:%Y_%m}"
        assert _facts(session, seed, kind, missing_month) == [
            ("acc-1", Decimal("1"), partition),
            ("acc-2", Decimal("7.25"), partition),
        ]
        assert _facts(session, seed, other, missing_month) == [
            ("acc-2", Decimal("7.25"), f"{TABLES[other]}_default")
        ]


@pytest.mark.parametrize("kind", ["ad", "material"])
def test_same_missing_month_concurrent_publish_keeps_both_accounts(
    committed_sync_seed, missing_month, kind
):
    seed, factory = committed_sync_seed
    ready = Barrier(2)

    def write(account):
        with factory() as session, session.begin():
            session.execute(text("SET LOCAL statement_timeout='5s'"))
            ready.wait(timeout=3)
            return _publish(session, seed, kind, missing_month, account)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write, account) for account in ("acc-1", "acc-2")]
        assert [f.result(timeout=10) for f in futures] == [1, 1]
    with factory() as session:
        assert _facts(session, seed, kind, missing_month) == [
            ("acc-1", Decimal("1"), f"{TABLES[kind]}_default"),
            ("acc-2", Decimal("1"), f"{TABLES[kind]}_default"),
        ]


@pytest.mark.parametrize("kind", ["ad", "material"])
def test_partition_and_publication_rollback_then_retry(
    committed_sync_seed, missing_month, kind
):
    seed, factory = committed_sync_seed
    with factory() as session, session.begin():
        _default_row(session, seed, kind, missing_month)
    with pytest.raises(DBAPIError) as caught:
        with factory() as session, session.begin():
            assert _publish(session, seed, kind, missing_month) == 1
            session.execute(text("SELECT 1/0"))
    assert caught.value.orig.sqlstate == "22012"
    with factory() as session:
        assert _facts(session, seed, kind, missing_month) == [
            ("acc-2", Decimal("7.25"), f"{TABLES[kind]}_default")
        ]
        assert (
            session.execute(
                text("SELECT to_regclass(:name)"),
                {"name": f"{TABLES[kind]}_{missing_month:%Y_%m}"},
            ).scalar()
            is None
        )
    with factory() as session, session.begin():
        assert _publish(session, seed, kind, missing_month) == 1
    with factory() as session:
        assert len(_facts(session, seed, kind, missing_month)) == 2


@pytest.mark.parametrize("kind", ["ad", "material"])
def test_missing_month_publish_does_not_block_same_fact_table_reader(
    committed_sync_seed, missing_month, kind
):
    seed, factory = committed_sync_seed
    with factory() as reader, reader.begin():
        reader.execute(text(f"SELECT 1 FROM {TABLES[kind]} LIMIT 1"))
        with factory() as writer, writer.begin():
            writer.execute(text("SET LOCAL lock_timeout='200ms'"))
            assert _publish(writer, seed, kind, missing_month) == 1
    with factory() as check:
        assert _facts(check, seed, kind, missing_month) == [
            ("acc-1", Decimal("1"), f"{TABLES[kind]}_default")
        ]
