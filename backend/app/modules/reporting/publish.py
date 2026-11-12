# backend/app/modules/reporting/publish.py
"""整份替换发布：某个“账户 × 报表 × 日期段”全部分页成功后，在一个事务内
仅替换实际读取开始后尚未再次成功发布的账户日期；平台完整空结果也有发布水位。

汇总规则：任一成员指标为 NULL（平台 '-'）时合计为 NULL，绝不显示部分合计。
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import MetaData, delete, insert, or_, text, tuple_
from sqlmodel import Session, col, select

from app.core.errors import DomainError
from app.modules.reporting import diagnostics
from app.modules.reporting.metrics import (
    basic_metric_values,
    parse_decimal,
    parse_local_day,
    parse_local_hour,
)
from app.modules.reporting.store_models import (
    METRIC_COLUMNS,
    AccountReconciliationDay,
    AdReportDay,
    CampaignReportDay,
    CampaignReportHour,
    CreativeReportDay,
    ReportSyncState,
)


@dataclass(frozen=True)
class AccountMeta:
    currency: str
    timezone: str


def _database_now(session) -> datetime:
    # 读取与发布水位统一使用 PG 实时时钟；不能混用不同 Worker/容器的墙上时钟。
    # transaction_timestamp()/now() 会停在事务开始，不能表示本次实际发布完成。
    return session.connection().execute(text("SELECT clock_timestamp()")).scalar_one()


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _account(row: dict, accounts: dict[str, AccountMeta]) -> str:
    advertiser = (row.get("metrics") or {}).get("advertiser_id") or (
        row.get("dimensions") or {}
    ).get("advertiser_id")
    if advertiser is None and len(accounts) == 1:
        advertiser = next(iter(accounts))
    if advertiser not in accounts:
        raise DomainError("report_row_invalid", "报表行账户不在本次请求范围内")
    return str(advertiser)


def _in_range(day: date, start: date, end: date) -> date:
    if not start <= day <= end:
        raise DomainError("report_row_invalid", "报表行日期超出请求范围")
    return day


@diagnostics.timed("publish_buckets")
def _writable_buckets(session, *, tenant_id, accounts, kind, start, end, requested_at):
    """在账户锁内比较本地发布水位；这不是平台事件或归因水位。

    发布入口的 requested_at 表示完整快照的首次实际读取开始，而非排队创建时间；
    素材续页沿用首页时间。观测更晚的已发布日期必须保留，包括完整空结果；
    旧数据没有观测时点时，保守使用原完成时间，不能猜测源版本。
    失败标记仍可传更保守的原意图时间。事务锁也保护尚不存在的状态行。
    """
    lock_kind = "ad_day" if kind == "account_day" else kind
    for advertiser in sorted(accounts):
        session.connection().execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
            {"scope": f"report:{tenant_id}:{advertiser}:{lock_kind}"},
        )
    states = session.exec(
        select(ReportSyncState)
        .where(
            ReportSyncState.tenant_id == tenant_id,
            col(ReportSyncState.advertiser_id).in_(accounts),
            ReportSyncState.report_kind == kind,
            col(ReportSyncState.stat_date).between(start, end),
        )
        .execution_options(populate_existing=True)
    ).all()
    protected = {
        (state.advertiser_id, state.stat_date)
        for state in states
        if (watermark := state.observed_at or state.last_success_at)
        and watermark > requested_at
    }
    return {
        (advertiser, day) for advertiser in accounts for day in _days(start, end)
    } - protected


def _bucket_filter(model, buckets):
    return tuple_(model.advertiser_id, model.stat_date).in_(sorted(buckets))


def _bucket_days(buckets):
    grouped = {}
    for advertiser, day in sorted(buckets):
        grouped.setdefault(advertiser, []).append(day)
    return grouped.items()


@diagnostics.timed("publish_state")
def _set_state(session, *, tenant_id, kind, buckets, counts, observed_at, job_id=None):
    from app.modules.reporting.store_models import ReportSyncJob
    from app.modules.sync_control.contracts import ExecutionFence
    from app.modules.sync_control.requirements import satisfy_requirements

    if not buckets:
        return
    job = session.get(ReportSyncJob, job_id) if job_id is not None else None
    if job is not None and job.tenant_id != tenant_id:
        raise ValueError("publication job tenant does not match facts")
    now = _database_now(session)
    ordered = sorted(buckets)
    # _writable_buckets 已持有账户事务锁。固定大小预读并统一 flush，避免
    # 每桶 SELECT/UPDATE/确认需求各往返一次，同时限制 SQL 参数和 ORM 内存。
    for offset in range(0, len(ordered), 500):
        batch = ordered[offset : offset + 500]
        states = {
            (state.advertiser_id, state.stat_date): state
            for state in session.exec(
                select(ReportSyncState).where(
                    col(ReportSyncState.tenant_id) == tenant_id,
                    col(ReportSyncState.report_kind) == kind,
                    _bucket_filter(ReportSyncState, batch),
                )
            ).all()
        }
        versions = {}
        for advertiser, day in batch:
            state = states.get((advertiser, day))
            if state is None:
                state = ReportSyncState(
                    tenant_id=tenant_id,
                    advertiser_id=advertiser,
                    report_kind=kind,
                    stat_date=day,
                    status="COMPLETE",
                )
                session.add(state)
            state.status, state.error_code = "COMPLETE", None
            state.row_count = counts.get((advertiser, day), 0)
            state.last_success_at = state.updated_at = now
            state.observed_at = observed_at
            state.publication_version += 1
            versions[(advertiser, day.isoformat())] = state.publication_version
        session.flush()
        if job is not None:
            satisfy_requirements(
                session,
                fence=ExecutionFence(
                    job.id, "report", job.claim_generation, job.owner_epoch
                ),
                observed_at=observed_at,
                publication_version=versions,
            )


def mark_failed(
    session: Session,
    *,
    tenant_id: UUID,
    advertiser_ids: Sequence[str],
    kind: str,
    start: date,
    end: date,
    error_code: str,
    requested_at: datetime | None = None,
) -> None:
    """失败不删除旧数据：已有完整数据标为 STALE，从未成功的标为 FAILED。"""
    requested_at = requested_at or _database_now(session)
    buckets = _writable_buckets(
        session,
        tenant_id=tenant_id,
        accounts=advertiser_ids,
        kind=kind,
        start=start,
        end=end,
        requested_at=requested_at,
    )
    now = _database_now(session)
    for advertiser, day in sorted(buckets):
        state = session.get(ReportSyncState, (tenant_id, advertiser, kind, day))
        if state is None:
            session.add(
                ReportSyncState(
                    tenant_id=tenant_id,
                    advertiser_id=advertiser,
                    report_kind=kind,
                    stat_date=day,
                    status="FAILED",
                    error_code=error_code,
                    updated_at=now,
                )
            )
        else:
            state.status = "STALE" if state.last_success_at else "FAILED"
            state.error_code, state.updated_at = error_code, now
    session.flush()


def _rollup_sql() -> str:
    # COUNT(col)=COUNT(*) 才求和：任一成员为 NULL 则系列合计为 NULL。
    cols = ", ".join(
        f"CASE WHEN COUNT({c}) = COUNT(*) THEN SUM({c}) END AS {c}"
        for c in METRIC_COLUMNS
    )
    return f"""
        INSERT INTO campaign_report_day (tenant_id, advertiser_id, stat_date, campaign_id, campaign_name,
            {", ".join(METRIC_COLUMNS)}, currency, timezone, fetched_at, sync_job_id)
        SELECT tenant_id, advertiser_id, stat_date, campaign_id, MAX(campaign_name), {cols},
               MAX(currency), MAX(timezone), MAX(fetched_at), :job_id
        FROM ad_report_day
        WHERE tenant_id = :tenant_id AND advertiser_id = :advertiser
          AND stat_date = ANY(:days)
        GROUP BY tenant_id, advertiser_id, stat_date, campaign_id
    """


_PUBLISH_BATCH_SIZE = 500


def _stage_records(session, model, records):
    """用事务私有、同类型/唯一键暂存校验全集，Python只保留一个批次。

    暂存表没有事实触发器；最终 INSERT SELECT 始终写父事实表，使覆盖和派生
    失效与集合替换同事务生效。完整验证前不删除已有事实。
    """
    name = f"publish_stage_{uuid4().hex}"
    table = model.__table__.to_metadata(MetaData(), name=name)
    connection = session.connection()
    connection.execute(
        text(
            f'CREATE TEMP TABLE "{name}" (LIKE "{model.__tablename__}" '
            "INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING INDEXES) ON COMMIT DROP"
        )
    )
    batch = []
    for record in records:
        batch.append(record)
        if len(batch) == _PUBLISH_BATCH_SIZE:
            connection.execute(insert(table), batch)
            batch.clear()
    if batch:
        connection.execute(insert(table), batch)
    return table


def _publish_staged(session, model, staged, condition):
    columns = [column.name for column in model.__table__.columns]
    result = (
        session.connection()
        .execution_options(preserve_rowcount=True)
        .execute(
            insert(model).from_select(
                columns, select(*(staged.c[name] for name in columns)).where(condition)
            )
        )
    )
    return result.rowcount


@diagnostics.timed("financial_publish")
def publish_ad_day(
    session: Session,
    *,
    tenant_id: UUID,
    job_id: UUID,
    accounts: dict[str, AccountMeta],
    start: date,
    end: date,
    rows: Iterable[dict],
    campaign_ids: tuple[str, ...] = (),
    requested_at: datetime | None = None,
) -> int:
    now = _database_now(session)
    counts: dict[tuple[str, date], int] = {}
    requested_at = requested_at or now

    def records():
        for row in rows:
            advertiser = _account(row, accounts)
            dims, metrics = row.get("dimensions") or {}, row.get("metrics") or {}
            day = _in_range(parse_local_day(dims.get("stat_time_day")), start, end)
            ad_id, campaign, adgroup = (
                dims.get("ad_id_v2"),
                metrics.get("campaign_id"),
                metrics.get("adgroup_id"),
            )
            if not ad_id or not campaign or not adgroup:
                raise DomainError(
                    "report_row_invalid", "广告行缺少广告、系列或广告组 ID"
                )
            if campaign_ids and campaign not in campaign_ids:
                raise DomainError("report_row_invalid", "按系列分批结果包含范围外系列")
            meta = accounts[advertiser]
            yield (
                {
                    "tenant_id": tenant_id,
                    "advertiser_id": advertiser,
                    "stat_date": day,
                    "ad_id": str(ad_id),
                    "campaign_id": str(campaign),
                    "adgroup_id": str(adgroup),
                    "campaign_automation_type": metrics.get("campaign_automation_type"),
                    "ad_name": metrics.get("ad_name"),
                    "campaign_name": metrics.get("campaign_name"),
                    "adgroup_name": metrics.get("adgroup_name"),
                    "currency": meta.currency,
                    "timezone": meta.timezone,
                    "fetched_at": now,
                    "sync_job_id": job_id,
                    **basic_metric_values(metrics),
                }
            )
            counts[(advertiser, day)] = counts.get((advertiser, day), 0) + 1

    staged = _stage_records(session, AdReportDay, records())
    # 先验证全部平台行，再筛水位；旧任务中的越权行也必须报错。
    buckets = _writable_buckets(
        session,
        tenant_id=tenant_id,
        accounts=accounts,
        kind="ad_day",
        start=start,
        end=end,
        requested_at=requested_at,
    )
    if not buckets:
        return 0
    diagnostics.counts(buckets=len(buckets))
    # 缺月份时由既有 DEFAULT 承接，DDL/搬迁由独立维护事务执行。
    with diagnostics.stage("publish_facts"):
        statement = delete(AdReportDay).where(
            col(AdReportDay.tenant_id) == tenant_id,
            _bucket_filter(AdReportDay, buckets),
        )
        if campaign_ids:
            statement = statement.where(col(AdReportDay.campaign_id).in_(campaign_ids))
        session.connection().execute(statement)
        row_count = _publish_staged(
            session, AdReportDay, staged, _bucket_filter(staged.c, buckets)
        )
    with diagnostics.stage("publish_rollup"):
        session.connection().execute(
            delete(CampaignReportDay).where(
                col(CampaignReportDay.tenant_id) == tenant_id,
                _bucket_filter(CampaignReportDay, buckets),
            )
        )
        for advertiser, days in _bucket_days(buckets):
            session.connection().execute(
                text(_rollup_sql()),
                {
                    "tenant_id": tenant_id,
                    "advertiser": advertiser,
                    "days": days,
                    "job_id": job_id,
                },
            )
    # 系列分片只替换其范围，不能把部分成功声明成整个账户当天 COMPLETE。
    if not campaign_ids:
        _set_state(
            session,
            tenant_id=tenant_id,
            kind="ad_day",
            buckets=buckets,
            counts=counts,
            observed_at=requested_at,
            job_id=job_id,
        )
    with diagnostics.stage("publish_flush"):
        session.flush()
    _project_ad_buckets(session, tenant_id, buckets)
    _refresh_reconciliation_sums(session, tenant_id, buckets)
    diagnostics.counts(rows=row_count)
    return row_count


@diagnostics.timed("financial_publish")
def publish_creative_day(
    session,
    *,
    tenant_id,
    job_id,
    accounts,
    start,
    end,
    rows,
    campaign_ids=(),
    requested_at=None,
) -> int:
    """完整创意快照独立原子替换；失败/旧观察绝不改写广告总账。"""
    now = _database_now(session)
    observed = requested_at or now
    counts = {}

    def records():
        for row in rows:
            advertiser = _account(row, accounts)
            dims, metrics = row.get("dimensions") or {}, row.get("metrics") or {}
            day = _in_range(parse_local_day(dims.get("stat_time_day")), start, end)
            creative = dims.get("ad_id")
            campaign, group = metrics.get("campaign_id"), metrics.get("adgroup_id")
            if not all(isinstance(x, str) and x for x in (creative, campaign, group)):
                raise DomainError(
                    "report_row_invalid", "创意行缺少创意、系列或广告组ID"
                )
            if campaign_ids and campaign not in campaign_ids:
                raise DomainError("report_row_invalid", "创意分批包含范围外系列")
            meta = accounts[advertiser]
            yield dict(
                tenant_id=tenant_id,
                advertiser_id=advertiser,
                stat_date=day,
                creative_id=creative,
                campaign_id=campaign,
                adgroup_id=group,
                campaign_automation_type=metrics.get("campaign_automation_type"),
                currency=meta.currency,
                timezone=meta.timezone,
                fetched_at=now,
                sync_job_id=job_id,
                **basic_metric_values(metrics),
            )
            counts[advertiser, day] = counts.get((advertiser, day), 0) + 1

    staged = _stage_records(session, CreativeReportDay, records())
    buckets = _writable_buckets(
        session,
        tenant_id=tenant_id,
        accounts=accounts,
        kind="creative_day",
        start=start,
        end=end,
        requested_at=observed,
    )
    if not buckets:
        return 0
    statement = delete(CreativeReportDay).where(
        col(CreativeReportDay.tenant_id) == tenant_id,
        _bucket_filter(CreativeReportDay, buckets),
    )
    if campaign_ids:
        statement = statement.where(
            col(CreativeReportDay.campaign_id).in_(campaign_ids)
        )
    session.connection().execute(statement)
    count = _publish_staged(
        session, CreativeReportDay, staged, _bucket_filter(staged.c, buckets)
    )
    if not campaign_ids:
        _set_state(
            session,
            tenant_id=tenant_id,
            kind="creative_day",
            buckets=buckets,
            counts=counts,
            observed_at=observed,
            job_id=job_id,
        )
    session.flush()
    if job_id is not None:
        # 创意事实提交后立即登记身份目录补读；身份请求走独立后台服务，
        # 不把目录完整性等待塞进报表发布事务的外部请求路径。
        from app.modules.reporting.directory_refresh import (
            refresh_report_creative_directory,
        )
        from app.modules.reporting.store_models import ReportSyncJob

        job = session.get(ReportSyncJob, job_id)
        if job is not None:
            refresh_report_creative_directory(session, job, now=now)
    return count


def _publish_simple(
    session,
    *,
    model,
    kind,
    key_builder,
    tenant_id,
    job_id,
    accounts,
    start,
    end,
    rows,
    value_parser,
    time_filter,
    requested_at,
) -> int:
    now = _database_now(session)
    counts: dict[tuple[str, date], int] = {}
    requested_at = requested_at or now

    def records():
        for row in rows:
            advertiser = _account(row, accounts)
            meta = accounts[advertiser]
            keys, day = key_builder(row, meta)
            yield (
                {
                    "tenant_id": tenant_id,
                    "advertiser_id": advertiser,
                    "currency": meta.currency,
                    "timezone": meta.timezone,
                    "fetched_at": now,
                    "sync_job_id": job_id,
                    **keys,
                    **value_parser(row.get("metrics") or {}),
                }
            )
            counts[(advertiser, day)] = counts.get((advertiser, day), 0) + 1

    staged = _stage_records(session, model, records())
    buckets = _writable_buckets(
        session,
        tenant_id=tenant_id,
        accounts=accounts,
        kind=kind,
        start=start,
        end=end,
        requested_at=requested_at,
    )
    if not buckets:
        return 0
    with diagnostics.stage("publish_facts"):
        session.connection().execute(
            delete(model).where(
                model.tenant_id == tenant_id,
                time_filter(model, buckets),
            )
        )
        row_count = _publish_staged(
            session, model, staged, time_filter(staged.c, buckets)
        )
    _set_state(
        session,
        tenant_id=tenant_id,
        kind=kind,
        buckets=buckets,
        counts=counts,
        observed_at=requested_at,
        job_id=job_id,
    )
    with diagnostics.stage("publish_flush"):
        session.flush()
    diagnostics.counts(rows=row_count, buckets=len(buckets))
    return row_count


@diagnostics.timed("financial_publish")
def publish_campaign_hour(
    session, *, tenant_id, job_id, accounts, day, rows, requested_at=None
) -> int:
    def keys(row, meta):
        dims = row.get("dimensions") or {}
        # 先按原始账户本地日期校验，不能把邻日小时写进当天发布窗口。
        _in_range(parse_local_day(dims.get("stat_time_hour")), day, day)
        if not dims.get("campaign_id"):
            raise DomainError("report_row_invalid", "系列行缺少系列 ID")
        hour = parse_local_hour(dims.get("stat_time_hour"), meta.timezone)
        return {"stat_hour": hour, "campaign_id": str(dims.get("campaign_id"))}, day

    def values(metrics):
        parsed = basic_metric_values(metrics)
        return {c: parsed[c] for c in METRIC_COLUMNS}

    # 各账户“本地这一天”的 UTC 边界不同；按每个账户的时区分别删除。
    def window(model, buckets):
        clauses = []
        for advertiser, _ in sorted(buckets):
            meta = accounts[advertiser]
            start_hour = parse_local_hour(f"{day} 00:00:00", meta.timezone)
            end_hour = parse_local_hour(
                f"{day + timedelta(days=1)} 00:00:00", meta.timezone
            )
            clauses.append(
                (model.advertiser_id == advertiser)
                & (model.stat_hour >= start_hour)
                & (model.stat_hour < end_hour)
            )
        return or_(*clauses)

    return _publish_simple(
        session,
        model=CampaignReportHour,
        kind="campaign_hour",
        key_builder=keys,
        tenant_id=tenant_id,
        job_id=job_id,
        accounts=accounts,
        start=day,
        end=day,
        rows=rows,
        value_parser=values,
        time_filter=window,
        requested_at=requested_at,
    )


@diagnostics.timed("financial_publish")
def publish_account_reconciliation(
    session, *, tenant_id, accounts, start, end, rows, requested_at=None, job_id=None
) -> int:
    """账户报表与同期广告汇总比对，结果只用于页面提示。"""
    requested_at = requested_at or _database_now(session)
    reported: dict[tuple[str, date], dict[str, Any]] = {}
    for row in rows:
        advertiser = _account(row, accounts)
        day = _in_range(
            parse_local_day((row.get("dimensions") or {}).get("stat_time_day")),
            start,
            end,
        )
        metrics = row.get("metrics") or {}
        reported[(advertiser, day)] = {
            "spend": parse_decimal(metrics.get("spend")),
            "total": parse_decimal(
                metrics.get("native_growth_total_ad_impression_value")
            ),
        }
    buckets = _writable_buckets(
        session,
        tenant_id=tenant_id,
        accounts=accounts,
        kind="account_day",
        start=start,
        end=end,
        requested_at=requested_at,
    )
    now = _database_now(session)
    for advertiser, day in sorted(buckets):
        rep = reported.get((advertiser, day), {"spend": None, "total": None})
        session.merge(
            AccountReconciliationDay(
                tenant_id=tenant_id,
                advertiser_id=advertiser,
                stat_date=day,
                account_spend=rep["spend"],
                account_revenue_total=rep["total"],
                checked_at=now,
            )
        )
    _set_state(
        session,
        tenant_id=tenant_id,
        kind="account_day",
        buckets=buckets,
        counts=dict.fromkeys(reported, 1),
        observed_at=requested_at,
        job_id=job_id,
    )
    with diagnostics.stage("publish_flush"):
        session.flush()
    _refresh_reconciliation_sums(session, tenant_id, buckets)
    return len(buckets)


@diagnostics.timed("publish_projection")
def _project_ad_buckets(session, tenant_id, buckets):
    from app.modules.reporting.read_models.dirty_buckets import ReadBucketKey
    from app.modules.reporting.read_models.projector import project_bucket

    for advertiser, day in sorted(buckets):
        revision = (
            session.connection()
            .execute(
                text(
                    "SELECT source_revision FROM read_bucket_state WHERE tenant_id=:tenant "
                    "AND advertiser_id=:advertiser AND kind='ad_day' AND bucket=:day"
                ),
                {"tenant": tenant_id, "advertiser": advertiser, "day": day},
            )
            .scalar_one_or_none()
        )
        if revision is not None:
            project_bucket(
                session,
                ReadBucketKey(tenant_id, advertiser, "ad_day", day),
                expected_version=revision,
            )


@diagnostics.timed("reconciliation")
def _refresh_reconciliation_sums(session, tenant_id, buckets):
    # 两种完成顺序均引用当前事实；平台 NULL 仍传播，完整空结果才为零。
    for advertiser, days in _bucket_days(buckets):
        with diagnostics.stage("reconciliation_account"):
            session.connection().execute(
                text("""
        UPDATE account_reconciliation_day r SET (ad_sum_spend, ad_sum_revenue_total) = (
            SELECT
              CASE WHEN COUNT(*)=0 THEN CASE WHEN EXISTS (
                SELECT 1 FROM report_sync_state s WHERE s.tenant_id=r.tenant_id
                AND s.advertiser_id=r.advertiser_id AND s.stat_date=r.stat_date
                AND s.report_kind='ad_day' AND s.status='COMPLETE') THEN 0 END
                WHEN COUNT(a.spend)=COUNT(*) THEN SUM(a.spend) END,
              CASE WHEN COUNT(*)=0 THEN CASE WHEN EXISTS (
                SELECT 1 FROM report_sync_state s WHERE s.tenant_id=r.tenant_id
                AND s.advertiser_id=r.advertiser_id AND s.stat_date=r.stat_date
                AND s.report_kind='ad_day' AND s.status='COMPLETE') THEN 0 END
                WHEN COUNT(a.revenue_total)=COUNT(*) THEN SUM(a.revenue_total) END
            FROM ad_report_day a WHERE a.tenant_id=r.tenant_id
              AND a.advertiser_id=r.advertiser_id AND a.stat_date=r.stat_date
        ) WHERE r.tenant_id=:t AND r.advertiser_id=:a AND r.stat_date=ANY(:days)
        """),
                {"t": tenant_id, "a": advertiser, "days": days},
            )
