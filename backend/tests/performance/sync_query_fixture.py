"""可重建合成数据；所有写入只允许本机专用 perf 库，不清理其他数据。"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy.engine import make_url

GIB = 1024**3
RESERVE_BYTES = 10 * GIB


def validate_database_url(url: str) -> None:
    try:
        parsed = make_url(url)
    except Exception:
        raise ValueError(
            "benchmark requires the dedicated local perf database"
        ) from None
    if (
        parsed.drivername not in {"postgresql", "postgresql+psycopg"}
        or parsed.host not in {"127.0.0.1", "localhost", "::1"}
        or (parsed.port or 5432) != 5432
        or parsed.database
        not in {"sync_redesign_test_perf", "sync_redesign_test_concurrency"}
        or parsed.query
    ):
        raise ValueError("benchmark requires the dedicated local perf database")


@dataclass(frozen=True)
class FixtureParameters:
    objects: int = 1000
    seed: int = 413
    days: int = 90
    accounts: int = 5
    active_fraction: float = 0.1
    json_bytes: int = 2048
    end_date: date = date(2026, 10, 30)

    def __post_init__(self):
        if self.objects < 1 or not 1 <= self.days <= 90 or self.accounts < 1:
            raise ValueError("positive objects/accounts and 1..90 days required")
        if not 0 < self.active_fraction <= 1 or not 0 <= self.json_bytes <= 65536:
            raise ValueError("invalid active fraction or JSON width")

    @property
    def active_ads(self) -> int:
        return max(1, math.ceil(self.objects * self.active_fraction))

    def parent_count(self, divisor: int) -> int:
        complete, remainder = divmod(self.objects, self.accounts * divisor)
        return complete * self.accounts + min(self.accounts, remainder)

    def manifest(self) -> dict:
        return {
            **asdict(self),
            "end_date": self.end_date.isoformat(),
            "active_ads": self.active_ads,
            "generator_revision": 2,
            "ad_objects": self.objects,
            "directory_rows": self.objects
            + self.parent_count(5)
            + self.parent_count(10),
            "reference_rows": self.objects,
            "ad_day_rows": self.active_ads * self.days,
            "material_day_rows": self.active_ads * self.days,
            "currencies": ["USD"],
            "timezones": ["UTC", "Asia/Hong_Kong"],
            "full_current_scale": False,
        }


def synthetic_fact(
    parameters: FixtureParameters, object_id: int, day_offset: int
) -> dict:
    cents = 1 + (parameters.seed + object_id * 17 + day_offset * 13) % 10000
    revenue_cents = cents * 2 + (object_id + day_offset) % 7
    return {
        "spend": Decimal(cents) / 100,
        "revenue": Decimal(revenue_cents) / 100,
        "date": parameters.end_date - timedelta(days=day_offset),
    }


def expected_metrics(
    parameters: FixtureParameters, *, object_ids: Iterable[int], days: int
) -> dict:
    # 独立整数分口径 oracle，不依赖生产聚合实现或浮点求和。
    spend_cents = revenue_cents = 0
    for obj in object_ids:
        for day in range(days):
            cents = 1 + (parameters.seed + obj * 17 + day * 13) % 10000
            spend_cents += cents
            revenue_cents += 2 * cents + (obj + day) % 7
    return {
        "spend": Decimal(spend_cents) / 100,
        "revenue": Decimal(revenue_cents) / 100,
    }


def budget_for_rows(
    *,
    rows: int,
    sample_rows: int,
    heap_bytes: int,
    index_bytes: int,
    toast_bytes: int,
    wal_bytes: int,
    free_bytes: int,
) -> dict:
    if (
        sample_rows < 1
        or rows < 1
        or min(heap_bytes, index_bytes, toast_bytes, wal_bytes, free_bytes) < 0
    ):
        raise ValueError("invalid storage measurement")
    relations = math.ceil(rows / sample_rows * (heap_bytes + index_bytes + toast_bytes))
    wal = math.ceil(rows / sample_rows * wal_bytes)
    # 保守计入一份额外索引构建空间与 50% 测量误差，WAL 不假设立即回收。
    peak = math.ceil((relations + wal + rows / sample_rows * index_bytes) * 1.5)
    return {
        "estimated_relation_bytes": relations,
        "estimated_wal_bytes": wal,
        "estimated_peak_bytes": peak,
        "reserve_bytes": RESERVE_BYTES,
        "free_bytes": free_bytes,
        "allowed": free_bytes - peak >= RESERVE_BYTES,
    }


def relation_sizes(session) -> dict:
    from sqlalchemy import text

    rows = (
        session.execute(
            text("""
        SELECT c.relname, pg_relation_size(c.oid) AS heap_bytes,
          pg_indexes_size(c.oid) AS index_bytes,
          CASE WHEN c.reltoastrelid=0 THEN 0 ELSE pg_total_relation_size(c.reltoastrelid) END AS toast_bytes
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind='r'
          AND (c.relname IN ('ad_object','ad_material_reference','material_post_link',
               'campaign_name_projection','campaign_report_day')
               OR c.relname LIKE 'ad_report_day_%' OR c.relname LIKE 'material_report_day_%')
        ORDER BY c.relname
    """)
        )
        .mappings()
        .all()
    )
    return {
        "relations": [dict(r) for r in rows],
        **{
            key: sum(r[key] for r in rows)
            for key in ("heap_bytes", "index_bytes", "toast_bytes")
        },
    }


def seed_fixture(engine, parameters: FixtureParameters) -> dict:
    """源事实 bulk seed 用于查询；真实 publisher 另测，不能混称 seed 为发布性能。"""
    from sqlalchemy import text
    from sqlmodel import Session

    from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
    from app.modules.reporting.partitions import ensure_month_partitions_for_range
    from tests.modules.reporting.sync_conftest import sync_seed

    validate_database_url(str(engine.url))
    with Session(engine) as session:
        before = relation_sizes(session)
        lsn_before = session.execute(
            text("SELECT pg_current_wal_insert_lsn()")
        ).scalar_one()
        seed = sync_seed.__wrapped__(session)
        for index in range(1, parameters.accounts + 1):
            advertiser = f"perf-account-{index}"
            session.add(
                AdvertiserAccount(
                    tenant_id=seed.tenant_id,
                    advertiser_id=advertiser,
                    currency="USD",
                    timezone="UTC" if index % 2 else "Asia/Hong_Kong",
                    name=advertiser,
                )
            )
            session.flush()
            session.add(
                BCAccountAccess(
                    tenant_id=seed.tenant_id,
                    bc_id="bc-a",
                    advertiser_id=advertiser,
                    connection_id=seed.connection.id,
                    in_bc=True,
                    authorized=True,
                    active=True,
                    checked_at=datetime.now(UTC),
                )
            )
        session.flush()
        ensure_month_partitions_for_range(
            session,
            parameters.end_date - timedelta(days=parameters.days - 1),
            parameters.end_date,
        )
        session.commit()
        args = {
            "tenant": seed.tenant_id,
            "n": parameters.objects,
            "accounts": parameters.accounts,
            "active": parameters.active_ads,
            "days": parameters.days,
            "end": parameters.end_date,
            "seed": parameters.seed,
            "job": uuid4(),
            "observed": datetime.now(UTC),
            "width": parameters.json_bytes,
        }
        # 每行不同摘要避免重复常量被 TOAST 压缩成虚假的极小宽行。
        for kind, divisor, parent, parent_divisor in [
            ("campaign", 10, None, 1),
            ("adgroup", 5, "campaign", 2),
            ("ad", 1, "adgroup", 5),
        ]:
            parent_expr = (
                "NULL"
                if parent is None
                else f"'{parent}-' || (((i-1)/(:accounts*{parent_divisor}))*:accounts+(i-1)%:accounts+1)::text"
            )
            count = (
                parameters.objects if divisor == 1 else parameters.parent_count(divisor)
            )
            sql = f"""INSERT INTO ad_object (tenant_id,advertiser_id,kind,remote_id,parent_kind,parent_remote_id,
                ad_type,name,configuration,operation_status,review_status,observed_at,published_version)
              SELECT :tenant, 'perf-account-' || ((i-1)%:accounts+1)::text, CAST(:kind AS text), CAST(:kind AS text) || '-' || i::text,
                :parent, {parent_expr}, 'REGULAR', 'perf-' || CAST(:kind AS text) || '-' || i::text,
                jsonb_build_object('budget',100,'payload',(SELECT string_agg(md5(i::text || ':' || k::text),'')
                    FROM generate_series(1, CAST(ceil(:width/32.0) AS int)) k)),
                'ENABLE','APPROVED',:observed,1 FROM generate_series(1,:count) i"""
            session.execute(
                text(sql), {**args, "kind": kind, "parent": parent, "count": count}
            )
        session.execute(
            text("""INSERT INTO ad_material_reference
            (id,tenant_id,advertiser_id,ad_remote_id,platform_material_id,ad_material_id,material_type,name,creative_ids,complete,published_version)
            SELECT md5(CAST(:seed AS text) || ':ref:' || CAST(CAST(:tenant AS uuid) AS text) || ':' || i)::uuid, CAST(:tenant AS uuid),
              'perf-account-' || ((i-1)%:accounts+1)::text, 'ad-'||i, 'video-'||i, 'inner-'||i,
              'VIDEO','perf-video-'||i,'[]'::jsonb,true,1 FROM generate_series(1,:n) i"""),
            args,
        )
        session.execute(
            text("""INSERT INTO material_post_link
            (tenant_id,advertiser_id,main_material_id,ad_remote_id,main_material_type,ad_material_id,material_name,platform_material_id,observed_at)
            SELECT :tenant,'perf-account-'||((i-1)%:accounts+1)::text,'post-'||i,'ad-'||i,
             'VIDEO','inner-'||i,'perf-video-'||i,'video-'||i,:observed FROM generate_series(1,:n) i"""),
            args,
        )
        # 按日提交仅是夹具构造；生产原子发布另由 publish_ad_day 实测。
        session.commit()
        for day in range(parameters.days):
            fact_args = {
                **args,
                "date": parameters.end_date - timedelta(days=day),
                "day": day,
            }
            common = """SELECT :tenant,'perf-account-'||((i-1)%:accounts+1)::text,:date,
              'USD', CASE WHEN ((i-1)%:accounts+1)%2=1 THEN 'UTC' ELSE 'Asia/Hong_Kong' END,
              :observed,:job, ((1+(:seed+i*17+:day*13)%10000)::numeric/100),10,1,1,
              ((2*(1+(:seed+i*17+:day*13)%10000)+(i+:day)%7)::numeric/100)"""
            session.execute(
                text(
                    """INSERT INTO ad_report_day
              (tenant_id,advertiser_id,stat_date,currency,timezone,fetched_at,sync_job_id,spend,impressions,clicks,
               ad_impression_events,revenue_d0,revenue_total,ad_id,adgroup_id,campaign_id,ad_name,campaign_name,adgroup_name)
              """
                    + common
                    + """, ((2*(1+(:seed+i*17+:day*13)%10000)+(i+:day)%7)::numeric/100),
              'ad-'||i,'adgroup-'||(((i-1)/(:accounts*5))*:accounts+(i-1)%:accounts+1),'campaign-'||(((i-1)/(:accounts*10))*:accounts+(i-1)%:accounts+1),'perf-ad-'||i,
              'Provider-Drama'||(((i-1)/(:accounts*10))*:accounts+(i-1)%:accounts+1), 'perf-adgroup-'||(((i-1)/(:accounts*5))*:accounts+(i-1)%:accounts+1)
              FROM generate_series(1,:active) i"""
                ),
                fact_args,
            )
            session.execute(
                text(
                    """INSERT INTO material_report_day
              (tenant_id,advertiser_id,stat_date,currency,timezone,fetched_at,sync_job_id,spend,impressions,clicks,
               ad_impression_events,revenue_d0,revenue_total,main_material_id,main_material_type,main_material_name)
              """
                    + common
                    + """, ((2*(1+(:seed+i*17+:day*13)%10000)+(i+:day)%7)::numeric/100),
              'post-'||i,'VIDEO','perf-video-'||i FROM generate_series(1,:active) i"""
                ),
                fact_args,
            )
            session.execute(
                text("""INSERT INTO campaign_report_day
              (tenant_id,advertiser_id,stat_date,currency,timezone,fetched_at,sync_job_id,campaign_id,campaign_name,
               spend,impressions,clicks,ad_impression_events,revenue_d0,revenue_total)
              SELECT tenant_id,advertiser_id,stat_date,currency,timezone,:observed,:job,campaign_id,min(campaign_name),
               sum(spend),sum(impressions),sum(clicks),sum(ad_impression_events),sum(revenue_d0),sum(revenue_total)
              FROM ad_report_day WHERE tenant_id=:tenant AND stat_date=:date
              GROUP BY tenant_id,advertiser_id,stat_date,currency,timezone,campaign_id"""),
                fact_args,
            )
            session.commit()
        session.execute(
            text("""INSERT INTO campaign_name_projection
              (tenant_id,advertiser_id,campaign_remote_id,name_revision,raw_name,provider_label,drama_name,status,
               parser_revision,grouping_revision,observed_at)
              SELECT DISTINCT tenant_id,advertiser_id,campaign_id,1,campaign_name,'Provider',campaign_id,'VALID',1,1,:observed
              FROM campaign_report_day WHERE tenant_id=:tenant"""),
            args,
        )
        session.commit()
        for name in (
            "ad_object",
            "ad_material_reference",
            "material_post_link",
            "ad_report_day",
            "material_report_day",
            "campaign_report_day",
            "campaign_name_projection",
        ):
            session.execute(text(f"ANALYZE {name}"))
        session.commit()
        after = relation_sizes(session)
        wal = session.execute(
            text("SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(),:start)"),
            {"start": lsn_before},
        ).scalar_one()
        return {
            "parameters": parameters.manifest(),
            "tenant_id": str(seed.tenant_id),
            "actor_id": str(seed.actor_id),
            "bc_id": "bc-a",
            "accounts": [
                f"perf-account-{i}" for i in range(1, parameters.accounts + 1)
            ],
            "created_at": datetime.now(UTC).isoformat(),
            "storage": after,
            "storage_delta": {
                key: after[key] - before[key]
                for key in ("heap_bytes", "index_bytes", "toast_bytes")
            },
            "wal_bytes": int(wal),
            "wal_scope": "cluster-wide interval, includes concurrent isolated workloads",
            "oracle": {
                str(days): {
                    k: str(v)
                    for k, v in expected_metrics(
                        parameters,
                        object_ids=range(1, parameters.active_ads + 1),
                        days=days,
                    ).items()
                }
                for days in (1, 7, 31, 90)
                if days <= parameters.days
            },
        }
