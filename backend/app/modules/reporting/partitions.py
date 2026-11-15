# backend/app/modules/reporting/partitions.py
"""按月分区：大表（广告日、创意日、紧凑指标日）每月一个分区，另有 DEFAULT 兜底。"""

from datetime import date
from typing import cast as typing_cast

from sqlalchemy import text
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlmodel import Session

PARTITIONED_TABLES = (
    "ad_report_day",
    "creative_report_day",
    "entity_metric_day",
)


def _month_start(value: date, offset: int) -> date:
    month = value.month - 1 + offset
    return date(value.year + month // 12, month % 12 + 1, 1)


def ensure_month_partitions(
    session: Session, *, months_ahead: int = 3, today: date | None = None
) -> None:
    """创建当月及未来 months_ahead 个月的分区；已存在则跳过，可重复调用。"""
    today = today or date.today()
    ensure_month_partitions_for_range(
        session, _month_start(today, 0), _month_start(today, months_ahead)
    )


def ensure_month_partitions_for_range(
    session: Session,
    start: date,
    end: date,
    *,
    tables: tuple[str, ...] = PARTITIONED_TABLES,
) -> None:
    """独立维护原子迁移 DEFAULT 既有行；业务发布不调用此DDL入口。"""
    if not tables or any(table not in PARTITIONED_TABLES for table in tables):
        raise ValueError("unknown reporting partition table")
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        nxt = _month_start(cursor, 1)
        for table in tables:
            name = f"{table}_{cursor:%Y_%m}"
            connection = session.connection()
            if connection.execute(
                text("SELECT to_regclass(:name)"), {"name": name}
            ).scalar():
                continue
            # 只锁本轮维护的父表，避免同时持多个事实表锁，与关联读形成锁环。
            # 锁后复查处理同月并发维护；搬迁和挂载由该独立维护事务统一提交。
            connection.execute(text(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE"))
            if connection.execute(
                text("SELECT to_regclass(:name)"), {"name": name}
            ).scalar():
                continue
            connection.execute(
                text(
                    f"CREATE TABLE {name} (LIKE {table} INCLUDING DEFAULTS INCLUDING CONSTRAINTS)"
                )
            )
            connection.execute(
                text(
                    f"WITH moved AS (DELETE FROM {table}_default WHERE stat_date >= :s AND stat_date < :e RETURNING *) "
                    f"INSERT INTO {name} SELECT * FROM moved"
                ),
                {"s": cursor, "e": nxt},
            )
            connection.execute(
                text(
                    f"ALTER TABLE {table} ATTACH PARTITION {name} "
                    f"FOR VALUES FROM ('{cursor.isoformat()}') TO ('{nxt.isoformat()}')"
                )
            )
        cursor = nxt


def maintain_partitions(
    database_engine,
    *,
    today: date | None = None,
    limit: int = 2,
    tables: tuple[str, ...] = PARTITIONED_TABLES,
) -> dict[str, int]:
    """每个分区独立短事务；遇到在线读者延后，不占据规划/财务发布事务。"""
    from sqlalchemy.exc import DBAPIError
    from sqlmodel import Session

    if (
        not 1 <= limit <= 24
        or not tables
        or any(t not in PARTITIONED_TABLES for t in tables)
    ):
        raise ValueError("invalid partition maintenance budget")
    today = today or date.today()
    candidates = [
        (table, _month_start(today, offset)) for offset in range(4) for table in tables
    ]
    # DEFAULT仅兜底未预建月份；日期索引使每轮只探测最早的缺失月。
    with database_engine.connect() as connection:
        for table in tables:
            oldest = connection.execute(
                text(
                    f"SELECT stat_date FROM {table}_default ORDER BY stat_date LIMIT 1"
                )
            ).scalar()
            if oldest is not None:
                candidate = (table, _month_start(oldest, 0))
                if candidate not in candidates:
                    candidates.append(candidate)
    created = deferred = 0
    for table, month in candidates:
        if created + deferred >= limit:
            break
        name = f"{table}_{month:%Y_%m}"
        try:
            with Session(database_engine) as session, session.begin():
                typing_cast(SQLAlchemySession, session).execute(
                    text("SET LOCAL lock_timeout='200ms'")
                )
                typing_cast(SQLAlchemySession, session).execute(
                    text("SET LOCAL statement_timeout='5s'")
                )
                if (
                    typing_cast(SQLAlchemySession, session)
                    .execute(text("SELECT to_regclass(:name)"), {"name": name})
                    .scalar()
                ):
                    continue
                # 同月份只有一个维护者；其他维护者下一轮重试，不排强锁长队。
                locked = (
                    typing_cast(SQLAlchemySession, session)
                    .execute(
                        text(
                            "SELECT pg_try_advisory_xact_lock(hashtextextended(:key,0))"
                        ),
                        {"key": f"partition-maintenance:{table}:{month}"},
                    )
                    .scalar()
                )
                if not locked:
                    deferred += 1
                    continue
                ensure_month_partitions_for_range(
                    session, month, month, tables=(table,)
                )
            created += 1
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) not in {"55P03", "57014", "40P01"}:
                raise
            # DEFAULT中的事实保持原事务状态，失败维护不会删除、重复或误宣称迁移。
            deferred += 1
    return {"created": created, "deferred": deferred}
