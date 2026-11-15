"""报表同步任务：一次请求（含全部分页）一行。

- 领取按租户轮转、按优先级与到期时间排序，大租户不能挤占小租户。
- 各报表逐页断点，额度让出不重读旧页；完整一致后才原子发布。
- 额度/限流延后不计重试次数；读取错误最多 5 次指数退避；合同错误立即失败。
- 广告与创意综合报表 2 万行触顶先拆账户/系列，完整发布后才更新覆盖。
"""

import hashlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from random import randint
from typing import Any, cast
from uuid import UUID

from sqlalchemy import delete
from sqlmodel import Session, col, select

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.change_log_reader import ChangeLogReader
from app.integrations.tiktok.contracts.report_reader import RawReportPage, ReportReader
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.modules.accounts.models import AdvertiserAccount
from app.modules.reporting import (
    diagnostics,
    publish,
)
from app.modules.reporting.checkpoints import BASIC_KINDS, CheckpointReader
from app.modules.reporting.directory_refresh import (
    refresh_report_directory,
)
from app.modules.reporting.lanes import (
    CLOSEOUT_PRIORITY,
)
from app.modules.reporting.store_models import ReportSyncJob, ReportSyncPage
from app.modules.sync_control.executions import ExecutionLost, TransactionBoundaryReader

logger = logging.getLogger(__name__)

PRIORITY = {
    "hot": 1,
    "manual": 1,
    "material": 2,
    "daily": 3,
    "history": 4,
    "closeout": CLOSEOUT_PRIORITY,
}
TRUNCATION_ROWS = 20_000
MAX_PROVIDER_RETRIES = 5
LEASE = timedelta(seconds=240)
_CONTRACT_ERRORS = frozenset(
    {
        "report_job_kind_invalid",
        "report_kind_retired",
        "report_query_invalid",
        "report_response_invalid",
        "report_row_invalid",
        "report_value_invalid",
        "report_operation_invalid",
        "operation_log_format_invalid",
        "operation_log_response_invalid",
        "operation_log_scope_invalid",
        "operation_log_phase_invalid",
        "operation_log_create_exhausted",
        "operation_log_range_invalid",
        "route_binding_changed",
        "route_authorization_changed",
        "route_contract_changed",
        "account_access_denied",
        "account_not_in_bc",
        "connection_unavailable",
        "connection_channel_mismatch",
        "connection_tenant_mismatch",
    }
)
_DEFERRED_CODES = frozenset(
    {
        "admission_deferred",
        "admission_unavailable",
        "tiktok_local_resources_unavailable",
        "gateway_credentials_changed",
    }
)


def _key(
    tenant_id,
    kind,
    tier,
    advertiser_ids,
    start,
    end,
    campaign_ids,
    bc_id,
    actor_id,
    frozen_route,
) -> str:
    payload = json.dumps(
        [
            str(tenant_id),
            kind,
            tier,
            sorted(advertiser_ids),
            start.isoformat(),
            end.isoformat(),
            sorted(campaign_ids),
            bc_id,
            str(actor_id),
            frozen_route,
        ],
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def enqueue_job(
    session: Session,
    *,
    tenant_id,
    bc_id,
    kind,
    tier,
    advertiser_ids,
    start,
    end,
    frozen_route,
    actor_id,
    campaign_ids=(),
    due_at=None,
    requested_at: datetime | None = None,
    execution_window_days: int | None = None,
) -> UUID | None:
    """单任务入口与周期批量规划共用去重和写入，未新建时返回 None。"""
    ids = enqueue_jobs(
        session,
        tenant_id=tenant_id,
        bc_id=bc_id,
        kind=kind,
        tier=tier,
        advertiser_groups=[advertiser_ids],
        start=start,
        end=end,
        frozen_route=frozen_route,
        actor_id=actor_id,
        campaign_ids=campaign_ids,
        due_at=due_at,
        requested_at=requested_at,
        execution_window_days=execution_window_days,
    )
    return ids[0] if ids else None


def enqueue_jobs(
    session: Session,
    *,
    tenant_id,
    bc_id,
    kind,
    tier,
    advertiser_groups,
    start,
    end,
    frozen_route,
    actor_id,
    campaign_ids=(),
    due_at=None,
    requested_at: datetime | None = None,
    execution_window_days: int | None = None,
) -> list[UUID]:
    """同一冻结范围整批去重、整批写入；每个账户组仍是一条独立同步任务。"""
    from app.modules.sync_control.report_demand import enqueue_report_requirements

    if kind in {"material_day", "material_links"}:
        raise DomainError("report_kind_retired", "旧素材报表及补链任务已退休")
    return enqueue_report_requirements(
        session,
        tenant_id=tenant_id,
        bc_id=bc_id,
        kind=kind,
        priority=PRIORITY[tier],
        advertiser_groups=advertiser_groups,
        start=start,
        end=end,
        frozen_route=frozen_route,
        actor_id=actor_id,
        campaign_ids=campaign_ids,
        due_at=due_at,
        requested_at=requested_at,
        execution_window_days=execution_window_days,
    )


def _accounts(session, tenant_id, advertiser_ids) -> dict[str, publish.AccountMeta]:
    rows = session.exec(
        select(AdvertiserAccount).where(
            col(AdvertiserAccount.tenant_id) == tenant_id,
            col(AdvertiserAccount.advertiser_id).in_(advertiser_ids),
        )
    ).all()
    found = {
        r.advertiser_id: publish.AccountMeta(r.currency, r.timezone)
        for r in rows
        if r.currency and r.timezone
    }
    if set(found) != set(advertiser_ids):
        raise DomainError("account_metadata_incomplete", "广告账户缺少币种或时区")
    return found


def _read_all(read: Callable[[int], RawReportPage]) -> list[dict]:
    rows, page = [], 1
    while True:
        result = read(page)
        if result.total_number >= TRUNCATION_ROWS:
            raise DomainError(
                "report_truncation_risk", "报表行数达到平台上限，可能被截断"
            )
        rows.extend(result.rows)
        if result.next_page is None:
            return rows
        page = result.next_page


def request_time(job: ReportSyncJob) -> datetime:
    """保留原规划意图供审计、范围身份和失败保护；不代表实际读取时间。"""
    return (
        datetime.fromisoformat(job.progress["requested_at"])
        if job.progress.get("requested_at")
        else job.created_at
    )


def _split(session, job: ReportSyncJob) -> bool:
    """多账户截断拆成独立账户；单账户在一次事务内读完所有系列批再发布。"""
    common: dict[str, Any] = {
        "tenant_id": job.tenant_id,
        "bc_id": job.bc_id,
        "kind": job.kind,
        "tier": next(t for t, p in PRIORITY.items() if p == job.priority),
        "start": job.start_date,
        "end": job.end_date,
        "frozen_route": job.frozen_route,
        "actor_id": job.actor_id,
        "requested_at": request_time(job),
    }
    if len(job.advertiser_ids) > 1:
        # 父任务先退出活动覆盖，避免幂等合并把自己的单账户子任务吞掉。
        job.status, job.finished_at = "COMPLETE", datetime.now(UTC)
        session.flush()
        for advertiser in job.advertiser_ids:
            enqueue_job(session, advertiser_ids=[advertiser], **common)
        return True
    return False


def _basic_scope(job):
    # 暂存严格绑定租户、账户、授权代数和日期；修改冻结范围不能续用旧页。
    key = _key(
        job.tenant_id,
        job.kind,
        str(job.priority),
        job.advertiser_ids,
        job.start_date,
        job.end_date,
        job.campaign_ids,
        job.bc_id,
        job.actor_id,
        job.frozen_route,
    )
    return hashlib.sha256(f"{key}:{request_time(job).isoformat()}".encode()).hexdigest()


def _run(
    session, job: ReportSyncJob, reader: _CountingReader | CheckpointReader
) -> int | None:
    advertisers = tuple(job.advertiser_ids)
    accounts = _accounts(session, job.tenant_id, advertisers)
    s, e, t, j = job.start_date, job.end_date, job.tenant_id, job.id
    if job.kind == "operation_log":
        from app.modules.operation_logs.worker import run_step

        return run_step(
            session,
            job,
            cast(ChangeLogReader, reader),
            accounts,
            external=reader.reader.external,
        )
    if job.kind in BASIC_KINDS:
        reader = CheckpointReader(
            session, job, reader, scope=_basic_scope(job), row_limit=TRUNCATION_ROWS
        )
        observed_at = reader.observed_at
    if job.kind == "ad_day":
        # 队列创建不是平台观测；本轮完整重读在账户锁内取得新的 PG 时间。
        try:
            if job.error_code == "report_timeout" and job.attempts:
                raise DomainError("report_truncation_risk", "超时后缩小读取范围")
            rows = _read_all(
                lambda p: reader.ad_day(
                    advertiser_ids=advertisers,
                    start=s,
                    end=e,
                    page=p,
                    campaign_ids=tuple(job.campaign_ids),
                )
            )
        except DomainError as error:
            if (
                error.code != "report_truncation_risk"
                or len(advertisers) != 1
                or job.campaign_ids
            ):
                raise
            rows = _read_campaign_batches(reader, advertisers, s, e)
        count = publish.publish_ad_day(
            session,
            tenant_id=t,
            job_id=j,
            accounts=accounts,
            start=s,
            end=e,
            rows=rows,
            campaign_ids=tuple(job.campaign_ids),
            requested_at=observed_at,
        )
        # 完整报表与补目录 outbox 同事务提交，新投放对象不等全量历史扫描。
        refresh_report_directory(session, job)
        return count
    if job.kind == "creative_day":
        try:
            rows = _read_all(
                lambda p: reader.creative_day(
                    advertiser_ids=advertisers,
                    start=s,
                    end=e,
                    page=p,
                    campaign_ids=tuple(job.campaign_ids),
                )
            )
        except DomainError as error:
            if error.code != "report_truncation_risk" or job.campaign_ids:
                raise
            rows = _read_campaign_batches(
                reader, advertisers, s, e, kind="creative_day"
            )
        return publish.publish_creative_day(
            session,
            tenant_id=t,
            job_id=j,
            accounts=accounts,
            start=s,
            end=e,
            rows=rows,
            campaign_ids=tuple(job.campaign_ids),
            requested_at=observed_at,
        )
    if job.kind == "campaign_hour":
        rows = _read_all(
            lambda p: reader.campaign_hour(advertiser_ids=advertisers, day=s, page=p)
        )
        return publish.publish_campaign_hour(
            session,
            tenant_id=t,
            job_id=j,
            accounts=accounts,
            day=s,
            rows=rows,
            requested_at=observed_at,
        )
    if job.kind == "account_day":
        rows = _read_all(
            lambda p: reader.account_day(
                advertiser_ids=advertisers, start=s, end=e, page=p
            )
        )
        return publish.publish_account_reconciliation(
            session,
            job_id=j,
            tenant_id=t,
            accounts=accounts,
            start=s,
            end=e,
            rows=rows,
            requested_at=observed_at,
        )
    raise DomainError("report_job_kind_invalid", "未知报表任务类型")


def _read_campaign_batches(reader, advertisers, start, end, *, kind="ad_day"):
    """每次拆分都重新读取本期完整系列清单，缓存可能缺少新系列，不能作为过滤依据。"""
    try:
        campaigns = _read_all(
            lambda p: reader.campaign_day(
                advertiser_ids=advertisers, start=start, end=end, page=p
            )
        )
        ids = sorted(
            {str(row.get("dimensions", {}).get("campaign_id", "")) for row in campaigns}
        )
        if not ids or "" in ids or "None" in ids:
            raise DomainError("report_truncation_risk", "截断报表没有完整系列清单")
        rows = []
        for offset in range(0, len(ids), 30):
            batch = tuple(ids[offset : offset + 30])
            rows.extend(
                _read_all(
                    lambda p, batch=batch: getattr(reader, kind)(
                        advertiser_ids=advertisers,
                        start=start,
                        end=end,
                        page=p,
                        campaign_ids=batch,
                    )
                )
            )
        return rows
    except DomainError as error:
        if error.code == "read_deadline_exceeded":
            # 已进入拆分读取的截止期限耗尽属于读取失败，不能无限走额度延后。
            raise DomainError("report_timeout", "系列分批读取超时") from error
        raise


class _CountingReader:
    def __init__(self, reader):
        self.reader = reader
        self.count = 0
        self.successful_queries: set[str] = set()
        self.failed_query: str | None = None

    def __getattr__(self, name) -> Callable[..., Any]:
        read = getattr(self.reader, name)

        def counted(**kwargs):
            before = getattr(self.reader, "request_count", None)
            # 以完整查询和页码隔离试探预算，不能由别的成功请求冲掉同一坏页的退避。
            query = hashlib.sha256(
                json.dumps([name, kwargs], sort_keys=True, default=str).encode()
            ).hexdigest()
            try:
                with diagnostics.stage("read"):
                    result = read(**kwargs)
                self.successful_queries.add(query)
                return result
            except AccountAdmissionDeferred:
                self.failed_query = query
                raise
            finally:
                after = getattr(self.reader, "request_count", None)
                # 官方读取器提供物理发送计数；纯内存测试读取器每次调用即一次合成响应。
                self.count += (
                    after - before
                    if isinstance(before, int) and isinstance(after, int)
                    else 1
                )

        return counted


def _clear_completed_throttle(job: ReportSyncJob, counted: _CountingReader) -> None:
    prior = job.progress.get("provider_throttle", {})
    if prior.get("query") in counted.successful_queries:
        job.progress = {
            key: value
            for key, value in job.progress.items()
            if key != "provider_throttle"
        }


def _admission_delay(
    job: ReportSyncJob, counted: _CountingReader, deferred: AccountAdmissionDeferred
) -> int:
    _clear_completed_throttle(job, counted)
    if deferred.shared_throttle_probe:
        # 未知报表限流只有共享接口探测轮这一份权威，不能被每个job的首轮五秒覆盖。
        return max(1000, deferred.retry_after_ms)
    if deferred.admission_source != "provider" or deferred.admission_reason not in {
        "40100",
        "http_429",
    }:
        return max(1000, deferred.retry_after_ms)
    prior = job.progress.get("provider_throttle", {})
    attempts = min(
        6,
        (prior.get("attempts", 0) if prior.get("query") == counted.failed_query else 0)
        + 1,
    )
    job.progress = job.progress | {
        "provider_throttle": {"query": counted.failed_query, "attempts": attempts}
    }
    # 只对未知类别最多短试探五次；持续拒绝转长冷却，保留任务/分页，绝不无限快重试。
    delays = (5_000, 10_000, 20_000, 40_000, 60_000, 300_000)
    return delays[attempts - 1] + randint(0, 1000)


def cancel_retired_report(session, job):
    """退役消息只留审计终态，不能打开网关或重新签发执行需求。"""
    job.status, job.error_code, job.finished_at = (
        "CANCELLED",
        "report_kind_retired",
        datetime.now(UTC),
    )
    job.claim_token = job.claimed_until = None
    from app.modules.sync_control.schedule import record_report_outcome

    record_report_outcome(session, job)


@diagnostics.timed("execute")
def execute_job(
    session_factory: Callable[[], Session],
    *,
    job_id: UUID,
    claim_token: UUID,
    reader: ReportReader,
) -> str:
    with session_factory() as session:
        job = session.exec(
            select(ReportSyncJob).where(col(ReportSyncJob.id) == job_id)
        ).one()
        if job.claim_token != claim_token or job.status != "RUNNING":
            return "FAILED"  # 旧领取者：不得改写新领取的任务
        if job.kind in {"material_day", "material_links"}:
            cancel_retired_report(session, job)
            session.commit()
            return "CANCELLED"
        boundary = TransactionBoundaryReader(session, job, reader)
        counted = _CountingReader(boundary)
        committed_count = 0
        try:
            # 网络读取不持业务事务；只在发布器内串行同桶替换并核验观测水位。
            # 关闭expire避免commit后访问冻结job属性隐式打开跨HTTP事务。
            session.expire_on_commit = False
            boundary._check()
            more = _run(session, job, counted) is None
            with diagnostics.stage("job_finalize"):
                _clear_completed_throttle(job, counted)
                if more:
                    job.status, job.error_code = "WAITING", None
                    if job.kind != "operation_log":
                        job.due_at = datetime.now(UTC)
                    result = "WAITING"
                else:
                    job.status, job.error_code, job.finished_at = (
                        "COMPLETE",
                        None,
                        datetime.now(UTC),
                    )
                    result = "COMPLETE"
                    if job.kind == "operation_log" or job.campaign_ids:
                        from app.modules.sync_control.contracts import ExecutionFence
                        from app.modules.sync_control.requirements import (
                            satisfy_requirements,
                        )

                        satisfy_requirements(
                            session,
                            fence=ExecutionFence(
                                job.id, "report", job.claim_generation, job.owner_epoch
                            ),
                            observed_at=datetime.fromisoformat(
                                job.progress["snapshot_observed_at"]
                            )
                            if job.progress.get("snapshot_observed_at")
                            else datetime.now(UTC),
                            publication_version=max(1, job.claim_generation),
                        )
                    if job.kind in BASIC_KINDS and "basic_paging" in job.progress:
                        # 财务事实已原子发布，暂存正文立即回收；保留观测/页数审计，避免热刷堆盘。
                        checkpoint = job.progress["basic_paging"]
                        job.progress = {
                            k: v for k, v in job.progress.items() if k != "basic_paging"
                        } | {
                            "basic_snapshot": {
                                "observed_at": checkpoint["observed_at"],
                                "pages": len(checkpoint["pages"]),
                                "scope": checkpoint["scope"],
                            }
                        }
                        session.connection().execute(
                            delete(ReportSyncPage).where(
                                col(ReportSyncPage.job_id) == job.id
                            )
                        )
        except ExecutionLost:
            session.rollback()
            return "FAILED"
        except AccountAdmissionDeferred as deferred:
            # 完整的私有页可以保留；额度退避不能迫使多页报表反复从首页开始。
            if job.kind not in BASIC_KINDS:
                session.rollback()
            job = session.get(ReportSyncJob, job_id, with_for_update=True)
            if job is None or job.claim_token != claim_token or job.status != "RUNNING":
                return "FAILED"
            job.status, job.error_code = "WAITING", "admission_deferred"
            delay = _admission_delay(job, counted, deferred)
            now = datetime.now(UTC)
            job.progress = job.progress | {
                "last_admission": {
                    "source": deferred.admission_source,
                    "reason": deferred.admission_reason,
                    "retry_after_ms": delay,
                    "at": now.isoformat(),
                }
            }
            job.due_at = now + timedelta(milliseconds=delay)
            result = "WAITING"
        except DomainError as error:
            session.rollback()
            job = session.get(ReportSyncJob, job_id, with_for_update=True)
            if job is None or job.claim_token != claim_token or job.status != "RUNNING":
                return "FAILED"
            result = _handle_error(session, job, error)
        job.request_count += counted.count - committed_count
        from app.modules.sync_control.schedule import record_report_outcome

        record_report_outcome(session, job)
        job.claim_token = job.claimed_until = None
        with diagnostics.stage("job_commit"):
            session.commit()
        diagnostics.committed(result)
        return result


def _handle_error(session, job: ReportSyncJob, error: DomainError) -> str:
    job.error_code = error.code
    if error.code == "report_snapshot_changed" and job.kind in BASIC_KINDS:
        session.connection().execute(
            delete(ReportSyncPage).where(col(ReportSyncPage.job_id) == job.id)
        )
        job.progress = {
            key: value for key, value in job.progress.items() if key != "basic_paging"
        }
    if error.code == "operation_log_export_too_large" and job.kind == "operation_log":
        if job.start_date < job.end_date:
            job.status, job.finished_at = "COMPLETE", datetime.now(UTC)
            session.flush()
            common: dict[str, Any] = {
                "tenant_id": job.tenant_id,
                "bc_id": job.bc_id,
                "kind": job.kind,
                "tier": "history",
                "advertiser_ids": job.advertiser_ids,
                "frozen_route": job.frozen_route,
                "actor_id": job.actor_id,
                "requested_at": request_time(job),
                "execution_window_days": max(
                    1, ((job.end_date - job.start_date).days + 2) // 2
                ),
            }
            middle = job.start_date + (job.end_date - job.start_date) // 2
            enqueue_job(session, **common, start=job.start_date, end=middle)
            enqueue_job(
                session, **common, start=middle + timedelta(days=1), end=job.end_date
            )
            return "SPLIT"
        job.status, job.finished_at = "FAILED", datetime.now(UTC)
        return "FAILED"
    if error.code == "report_truncation_risk":
        if _split(session, job):
            job.status, job.finished_at = "COMPLETE", datetime.now(UTC)
            return "SPLIT"
    elif error.code in _DEFERRED_CODES:
        job.status, job.due_at = "WAITING", datetime.now(UTC) + timedelta(seconds=30)
        return "WAITING"
    elif error.code not in _CONTRACT_ERRORS and job.attempts < MAX_PROVIDER_RETRIES:
        job.attempts += 1
        job.status = "WAITING"
        now = datetime.now(UTC)
        delay = min(1800, 60 * 2 ** (job.attempts - 1))
        job.due_at = now + timedelta(seconds=delay)
        return "WAITING"
    job.status, job.finished_at = "FAILED", datetime.now(UTC)
    if job.kind == "operation_log":
        return "FAILED"  # 日志失败独立留在任务上，不能伪造报表覆盖水位。
    publish.mark_failed(
        session,
        tenant_id=job.tenant_id,
        advertiser_ids=job.advertiser_ids,
        kind=job.kind,
        start=job.start_date,
        end=job.end_date,
        error_code=error.code,
        requested_at=request_time(job),
    )
    return "FAILED"
