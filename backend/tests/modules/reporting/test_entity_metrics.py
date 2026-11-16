"""紧凑金额事实与源写事务保持一致，元数据回读不制造第二轮金额写入。"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

METRICS = (
    "spend",
    "impressions",
    "clicks",
    "ad_impression_events",
    "revenue_d0",
    "revenue_d3",
    "revenue_d7",
    "revenue_d14",
    "revenue_d30",
    "revenue_total",
)
SOURCES = {
    "campaign": ("campaign_report_day", "campaign_id"),
    "adgroup": ("adgroup_report_day", "adgroup_id"),
    "ad": ("ad_report_day", "ad_id"),
    "material": ("material_entity_day", "row_key"),
}
DAY = date(2026, 9, 14)


def insert_source(
    session,
    tenant,
    kind,
    *,
    day=DAY,
    advertiser="acc-1",
    remote="metric-1",
    currency="USD",
):
    table, identity = SOURCES[kind]
    fields = (
        "tenant_id,advertiser_id,stat_date,currency,timezone,fetched_at," + identity
    )
    values = ":t,:a,:d,:c,'UTC',now(),:r"
    if kind in {"campaign", "ad"}:
        fields += ",sync_job_id"
        values += ",gen_random_uuid()"
    else:
        fields += ",source_revision," + ",".join(f"{m}_has_null" for m in METRICS)
        values += ",7," + ",".join("false" for _ in METRICS)
    if kind == "ad":
        fields += ",campaign_id,adgroup_id"
        values += ",'campaign-parent','adgroup-parent'"
    if kind == "material":
        fields += ",main_material_type,post_ids"
        values += ",'VIDEO',ARRAY[]::text[]"
    fields += ",spend,revenue_d0"
    values += ",1.234567890123456789,0"
    session.execute(
        text(f"INSERT INTO {table} ({fields}) VALUES ({values})"),
        {"t": tenant, "a": advertiser, "d": day, "c": currency, "r": remote},
    )


def compact(session, tenant, kind):
    return (
        session.execute(
            text("""SELECT spend,spend_has_null,revenue_d0,revenue_d0_has_null,
        revenue_d3,revenue_d3_has_null,source_revision,fetched_at,ctid::text AS row_location,
        currency,timezone,remote_id,variant FROM entity_metric_day
        WHERE tenant_id=:t AND kind=:k ORDER BY currency,remote_id"""),
            {"t": tenant, "k": kind},
        )
        .mappings()
        .all()
    )


@pytest.mark.parametrize("kind", SOURCES)
def test_compact_crud_null_flags_and_metadata_noop(session, sync_seed, kind):
    tenant = sync_seed.tenant_id
    insert_source(session, tenant, kind)
    before = compact(session, tenant, kind)[0]
    assert before["spend"] == Decimal("1.234567890123456789")
    assert not before["spend_has_null"]
    assert before["revenue_d3"] is None and before["revenue_d3_has_null"]
    table, identity = SOURCES[kind]
    metadata = {
        "campaign": "campaign_name",
        "adgroup": "adgroup_name",
        "ad": "ad_name",
        "material": "main_material_name",
    }[kind]
    session.execute(
        text(
            f"UPDATE {table} SET {metadata}='new name', fetched_at=now()+interval '1 minute' WHERE tenant_id=:t"
        ),
        {"t": tenant},
    )
    assert compact(session, tenant, kind)[0] == before
    session.execute(
        text(f"UPDATE {table} SET spend=NULL WHERE tenant_id=:t"), {"t": tenant}
    )
    assert compact(session, tenant, kind)[0]["spend_has_null"]
    session.execute(
        text(
            f"UPDATE {table} SET {identity}='metric-renamed',currency='EUR' WHERE tenant_id=:t"
        ),
        {"t": tenant},
    )
    rows = compact(session, tenant, kind)
    assert (
        len(rows) == 1
        and rows[0]["remote_id"] == "metric-renamed"
        and rows[0]["currency"] == "EUR"
    )
    if kind in {"adgroup", "material"}:
        session.execute(
            text(f"UPDATE {table} SET spend=9,spend_has_null=true WHERE tenant_id=:t"),
            {"t": tenant},
        )
        assert compact(session, tenant, kind)[0]["spend_has_null"]
    with session.begin_nested() as savepoint:
        session.execute(text(f"DELETE FROM {table} WHERE tenant_id=:t"), {"t": tenant})
        assert compact(session, tenant, kind) == []
        savepoint.rollback()
    assert len(compact(session, tenant, kind)) == 1
    session.execute(text(f"DELETE FROM {table} WHERE tenant_id=:t"), {"t": tenant})
    assert compact(session, tenant, kind) == []


def test_compact_default_partition_move_and_view_pruning(session, sync_seed):
    from app.modules.reporting.partitions import ensure_month_partitions_for_range

    day = date(7451, 8, 4)
    insert_source(session, sync_seed.tenant_id, "campaign", day=day)
    assert (
        session.execute(
            text(
                "SELECT tableoid::regclass::text FROM entity_metric_day WHERE stat_date=:d"
            ),
            {"d": day},
        ).scalar_one()
        == "entity_metric_day_default"
    )
    ensure_month_partitions_for_range(session, day, day, tables=("entity_metric_day",))
    assert (
        session.execute(
            text(
                "SELECT tableoid::regclass::text FROM entity_metric_day WHERE stat_date=:d"
            ),
            {"d": day},
        ).scalar_one()
        == "entity_metric_day_7451_08"
    )
    plan = session.execute(
        text(
            "EXPLAIN (FORMAT JSON) SELECT spend FROM campaign_metric_day WHERE stat_date=:d"
        ),
        {"d": day},
    ).scalar_one()[0]["Plan"]
    assert "entity_metric_day_7451_08" in str(plan)
    assert "entity_metric_day_default" not in str(plan)
    assert (
        session.execute(
            text("SELECT campaign_id FROM campaign_metric_day WHERE stat_date=:d"),
            {"d": day},
        ).scalar_one()
        == "metric-1"
    )


@pytest.mark.parametrize("kind", ["adgroup", "material"])
def test_compact_preserves_currency_timezone_variants_and_revision_noop(
    session, sync_seed, kind
):
    tenant = sync_seed.tenant_id
    insert_source(session, tenant, kind, currency="USD")
    insert_source(session, tenant, kind, currency="EUR")
    table, _ = SOURCES[kind]
    session.execute(
        text(f"""INSERT INTO {table}
        SELECT (jsonb_populate_record(NULL::{table},to_jsonb(f)||
            '{{"timezone":"Asia/Hong_Kong"}}'::jsonb)).* FROM {table} f
        WHERE tenant_id=:t AND currency='USD'"""),
        {"t": tenant},
    )
    if kind == "material":
        session.execute(
            text("""INSERT INTO material_entity_day
            SELECT (jsonb_populate_record(NULL::material_entity_day,to_jsonb(f)||
                '{"main_material_type":"IMAGE"}'::jsonb)).* FROM material_entity_day f
            WHERE tenant_id=:t AND currency='USD' AND timezone='UTC'"""),
            {"t": tenant},
        )
    before = sorted(
        compact(session, tenant, kind),
        key=lambda r: (r["currency"], r["timezone"], r["variant"]),
    )
    assert len(before) == (4 if kind == "material" else 3)
    assert all(r["source_revision"] == 7 for r in before)
    session.execute(
        text(f"UPDATE {table} SET source_revision=99 WHERE tenant_id=:t"), {"t": tenant}
    )
    after = sorted(
        compact(session, tenant, kind),
        key=lambda r: (r["currency"], r["timezone"], r["variant"]),
    )
    assert after == before
    session.execute(
        text(f"UPDATE {table} SET revenue_d0=3 WHERE tenant_id=:t"), {"t": tenant}
    )
    assert all(r["source_revision"] == 99 for r in compact(session, tenant, kind))


def test_compact_live_account_filter_delete_and_rediscovery(session, sync_seed):
    from app.modules.accounts.models import AdvertiserAccount

    tenant = sync_seed.tenant_id
    for kind in ("campaign", "ad"):
        insert_source(session, tenant, kind, advertiser="historical")
        assert compact(session, tenant, kind) == []
    account = AdvertiserAccount(
        tenant_id=tenant, advertiser_id="historical", currency="USD", timezone="UTC"
    )
    session.add(account)
    session.flush()
    for kind in ("campaign", "ad"):
        assert len(compact(session, tenant, kind)) == 1
    # 四类派生金额一起删，canonical的历史事实保留，重新发现仍能恢复。
    for kind in ("adgroup", "material"):
        insert_source(session, tenant, kind, advertiser="historical")
    session.delete(account)
    session.flush()
    for kind in SOURCES:
        assert compact(session, tenant, kind) == []
    session.add(
        AdvertiserAccount(
            tenant_id=tenant, advertiser_id="historical", currency="USD", timezone="UTC"
        )
    )
    session.flush()
    for kind in ("campaign", "ad"):
        assert len(compact(session, tenant, kind)) == 1
    for kind in ("adgroup", "material"):
        assert (
            compact(session, tenant, kind) == []
        )  # 0081已删除的派生源由既有重建工作流恢复。


def test_compact_bulk_read_is_narrow_and_prunes_unselected_months(session, sync_seed):
    params = {"t": sync_seed.tenant_id}
    session.execute(
        text("""INSERT INTO campaign_report_day
        (tenant_id,advertiser_id,stat_date,campaign_id,campaign_name,spend,currency,timezone,fetched_at,sync_job_id)
        SELECT :t,'acc-1',d,'bulk-'||i,repeat(md5(i::text),128),i/100.0,'USD','UTC',now(),gen_random_uuid()
        FROM generate_series(1,2000)i CROSS JOIN unnest(ARRAY[date '2026-08-14',date '2026-09-14',date '2026-10-14']) d"""),
        params,
    )
    from app.modules.reporting.partitions import ensure_month_partitions_for_range

    ensure_month_partitions_for_range(
        session, date(2026, 8, 1), date(2026, 10, 1), tables=("entity_metric_day",)
    )
    session.execute(text("ANALYZE entity_metric_day"))
    result = session.execute(
        text("""SELECT sum(spend),count(*) FROM campaign_metric_day
        WHERE tenant_id=:t AND stat_date='2026-09-14'"""),
        params,
    ).one()
    assert result == (Decimal("20010"), 2000)
    plan = session.execute(
        text("""EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON)
        SELECT sum(spend) FROM campaign_metric_day WHERE tenant_id=:t AND stat_date='2026-09-14'"""),
        params,
    ).scalar_one()[0]["Plan"]
    assert "campaign_report_day" not in str(plan)
    assert "entity_metric_day_2026_09" in str(plan)
    assert "entity_metric_day_2026_08" not in str(plan)
    assert "entity_metric_day_2026_10" not in str(plan)
    narrow = session.execute(
        text(
            "SELECT avg(pg_column_size(f)) FROM entity_metric_day f WHERE tenant_id=:t"
        ),
        params,
    ).scalar_one()
    wide = session.execute(
        text(
            "SELECT avg(pg_column_size(f)) FROM campaign_report_day f WHERE tenant_id=:t"
        ),
        params,
    ).scalar_one()
    assert narrow < wide
    # 对整批宽名称刷新也不能重写金额行；ctid反映真实物理写入，而不只比值。
    before = session.execute(
        text("""SELECT remote_id,stat_date,tableoid,ctid::text FROM entity_metric_day
        WHERE tenant_id=:t ORDER BY remote_id,stat_date"""),
        params,
    ).all()
    session.execute(
        text(
            "UPDATE campaign_report_day SET campaign_name='renamed',fetched_at=now()+interval '1 hour' WHERE tenant_id=:t"
        ),
        params,
    )
    assert (
        session.execute(
            text("""SELECT remote_id,stat_date,tableoid,ctid::text FROM entity_metric_day
        WHERE tenant_id=:t ORDER BY remote_id,stat_date"""),
            params,
        ).all()
        == before
    )


@pytest.mark.parametrize("kind", SOURCES)
def test_compact_upsert_and_stat_date_move(session, sync_seed, kind):
    tenant = sync_seed.tenant_id
    insert_source(session, tenant, kind)
    table, identity = SOURCES[kind]
    keys = ["tenant_id", "advertiser_id", "stat_date", identity]
    if kind in {"adgroup", "material"}:
        keys += ["currency", "timezone"]
    if kind == "material":
        keys.append("main_material_type")
    sql = f"""INSERT INTO {table} SELECT f.* FROM {table} f WHERE tenant_id=:t
        ON CONFLICT ({",".join(keys)}) DO UPDATE SET spend=excluded.spend"""
    before = compact(session, tenant, kind)
    session.execute(text(sql), {"t": tenant})
    assert compact(session, tenant, kind) == before
    session.execute(text(sql + "+1"), {"t": tenant})
    assert compact(session, tenant, kind)[0]["spend"] == Decimal("2.234567890123456789")
    session.execute(
        text(f"UPDATE {table} SET stat_date='7465-02-03' WHERE tenant_id=:t"),
        {"t": tenant},
    )
    assert session.execute(
        text("""SELECT stat_date,tableoid::regclass::text FROM entity_metric_day
        WHERE tenant_id=:t AND kind=:k"""),
        {"t": tenant, "k": kind},
    ).one() == (date(7465, 2, 3), "entity_metric_day_default")


def test_compact_is_not_visible_outside_rolled_back_source_transaction(
    committed_sync_seed,
):
    seed, factory = committed_sync_seed
    with factory() as writer:
        insert_source(writer, seed.tenant_id, "campaign")
        assert len(compact(writer, seed.tenant_id, "campaign")) == 1
        with factory() as reader:
            assert compact(reader, seed.tenant_id, "campaign") == []
        writer.rollback()
    with factory() as reader:
        assert compact(reader, seed.tenant_id, "campaign") == []


def test_compact_identity_is_tenant_scoped(session, sync_seed):
    from uuid import uuid4

    from app.modules.accounts.models import AdvertiserAccount
    from app.modules.tenants.models import Tenant

    other = uuid4()
    session.add(Tenant(id=other, name="compact-other-tenant"))
    session.flush()
    session.add(
        AdvertiserAccount(
            tenant_id=other, advertiser_id="acc-1", currency="USD", timezone="UTC"
        )
    )
    session.flush()
    for kind in SOURCES:
        insert_source(session, sync_seed.tenant_id, kind)
        insert_source(session, other, kind)
        table, _ = SOURCES[kind]
        session.execute(
            text(f"UPDATE {table} SET spend=99 WHERE tenant_id=:t"), {"t": other}
        )
        assert compact(session, other, kind)[0]["spend"] == 99
        assert compact(session, sync_seed.tenant_id, kind)[0]["spend"] == Decimal(
            "1.234567890123456789"
        )


@pytest.mark.parametrize("kind", ["campaign", "adgroup", "ad"])
def test_metric_only_snapshot_scans_compact_facts_and_preserves_partial_values(
    session, sync_seed, kind
):
    from app.modules.reporting.row_set import _ctes
    from app.modules.reporting.schemas import ReportingFilter

    tenant = sync_seed.tenant_id
    insert_source(session, tenant, kind)
    insert_source(session, tenant, kind, remote="metric-2")
    table, _ = SOURCES[kind]
    session.execute(
        text(f"UPDATE {table} SET revenue_d0=NULL WHERE tenant_id=:t"), {"t": tenant}
    )
    filters = ReportingFilter(
        dimension=kind, start_date=DAY, end_date=DAY, sort_by="spend"
    )
    ctes, params = _ctes(filters, tenant_id=tenant, advertisers=["acc-1"], bc_id="bc-a")
    plan = session.execute(
        text("EXPLAIN (ANALYZE,FORMAT JSON) " + ctes + " SELECT row_key FROM ordered"),
        params,
    ).scalar_one()[0]["Plan"]

    def nodes(node):
        yield node
        for child in node.get("Plans", []):
            yield from nodes(child)

    active = [n for n in nodes(plan) if n.get("Actual Loops", 0)]
    assert not {n.get("Relation Name") for n in active} & {
        "ad_object",
        "ad_entity_read",
        "campaign_name_projection",
    }
    # 已批准的直接/派生选择读取原事实；两成员不能变成目录/名称历史全扫。
    source_scans = [n for n in active if n.get("Relation Name", "").startswith(table)]
    assert source_scans
    assert (
        sum(n.get("Actual Rows", 0) * n.get("Actual Loops", 0) for n in source_scans)
        <= 4
    )
    facts = session.execute(
        text(
            ctes + " SELECT rid,spend,revenue_d0,revenue_d0_hn FROM facts ORDER BY rid"
        ),
        params,
    ).all()
    assert facts == [
        ("metric-1", Decimal("1.234567890123456789"), None, True),
        ("metric-2", Decimal("1.234567890123456789"), None, True),
    ]
