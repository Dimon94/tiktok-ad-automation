"""报表同步任务：一次请求（含全部分页）一行。

- 领取按租户轮转、按优先级与到期时间排序，大租户不能挤占小租户。
- 各报表逐页断点，额度让出不重读旧页；完整一致后才原子发布。
- 额度/限流延后不计重试次数；读取错误最多 5 次指数退避；合同错误立即失败。
- 广告综合报表 2 万行触顶先拆账户/系列；素材拆分报表按分页证据续读，大跨日范围拆日期。
"""

import hashlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from random import randint
from time import monotonic
from uuid import UUID

from sqlalchemy import delete, text
from sqlmodel import Session, col, select

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.report_reader import RawReportPage, ReportReader
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.modules.accounts.models import AdvertiserAccount
from app.modules.reporting import (
    diagnostics,
    material_identity,
    material_links,
    publish,
    spark_identity,
)
from app.modules.reporting.checkpoints import BASIC_KINDS, CheckpointReader
from app.modules.reporting.directory_refresh import (
    refresh_report_directory,
    request_link_directory,
)
from app.modules.reporting.lanes import (
    CLOSEOUT_PRIORITY,
    report_workload,
)
from app.modules.reporting.link_policy import complete_material_link
from app.modules.reporting.material_value import (
    TYPED_MATERIAL_WINDOW_VALUES_SQL,
    material_value_parameters,
    valuable_material_condition,
)
from app.modules.reporting.metrics import material_metric_values, parse_local_day
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
MATERIAL_PAGES_PER_STEP = 5
MATERIAL_BACKFILL_PAGES_PER_STEP = 50
MATERIAL_BACKFILL_STEP_SECONDS = 30
MATERIAL_SPLIT_ROWS = 5_000
MAX_PROVIDER_RETRIES = 5
MATERIAL_SNAPSHOT_RETRY_SECONDS = (5, 10, 20, 30, 60)
MATERIAL_LINK_SNAPSHOT_RECOVERY_DELAY = timedelta(minutes=5)
LEASE = timedelta(seconds=240)
_CONTRACT_ERRORS = frozenset(
    {
        "report_query_invalid",
        "report_response_invalid",
        "report_row_invalid",
        "report_value_invalid",
        "report_operation_invalid",
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
            AdvertiserAccount.tenant_id == tenant_id,
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
    common = {
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


def _material_scope(job):
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


def _log_material_snapshot_change(job, result, *, page, reason):
    # 失败会回滚并删除私有页；只保留小型分页诊断，不记录素材身份、正文或额外查库。
    saved = job.progress.get("material_paging") or {}
    age = None
    try:
        observed = datetime.fromisoformat(saved["observed_at"])
        if observed.utcoffset() is not None:
            elapsed = (datetime.now(UTC) - observed).total_seconds()
            if elapsed >= 0:
                age = round(elapsed, 3)
    except KeyError, TypeError, ValueError:
        pass
    details = {
        "job_id": str(job.id),
        "reason": reason,
        "page": page,
        "saved_total_page": saved.get("total_page"),
        "saved_total_number": saved.get("total_number"),
        "current_total_page": result.total_page,
        "current_total_number": result.total_number,
        "first_page_age_seconds": age,
    }
    logger.warning(
        "material snapshot changed %s",
        json.dumps(details),
        extra={"snapshot_failure": details},
    )


def _validate_material_page(session, job, accounts, result, page):
    if (
        any(
            type(value) is not int
            for value in (result.page, result.total_page, result.total_number)
        )
        or result.page != page
        or result.total_page < 0
        or result.total_number < 0
        or (
            result.total_number == 0
            and (result.rows or result.total_page not in (0, 1) or page != 1)
        )
        or (result.total_number > 0 and (not result.rows or page > result.total_page))
    ):
        raise DomainError("report_response_invalid", "素材分页证据无效")
    keys = []
    for row in result.rows:
        publish._account(row, accounts)
        dims = row.get("dimensions") or {}
        day = publish._in_range(
            parse_local_day(dims.get("stat_time_day")), job.start_date, job.end_date
        )
        if not dims.get("main_material_id") or not dims.get("main_material_type"):
            raise DomainError("report_row_invalid", "素材行缺少主素材身份")
        material_metric_values(row.get("metrics") or {})
        keys.append(
            json.dumps(
                [day.isoformat(), str(dims["main_material_id"])], separators=(",", ":")
            )
        )
    if len(set(keys)) != len(keys):
        raise DomainError("report_row_invalid", "素材分页包含重复日期与主素材")
    if (
        keys
        and session.connection()
        .execute(
            text("""
        SELECT EXISTS(SELECT 1 FROM report_sync_page
          WHERE job_id=:job AND tenant_id=:tenant AND row_keys ?| CAST(:keys AS text[]))
    """),
            {"job": job.id, "tenant": job.tenant_id, "keys": keys},
        )
        .scalar_one()
    ):
        _log_material_snapshot_change(
            job, result, page=page, reason="cross_page_duplicate"
        )
        raise DomainError(
            "report_snapshot_changed", "素材跨页内容变化，需要重建完整快照"
        )
    return keys


def _split_material_dates(session, job, result: RawReportPage):
    days = (job.end_date - job.start_date).days + 1
    # 小账户保留批量请求；仅大范围缩小，最多七天且下一层仍可按首分页继续拆。
    chunk_days = max(1, min(7, days * MATERIAL_SPLIT_ROWS // result.total_number))
    job.progress = job.progress | {
        "material_split": {
            "total_number": result.total_number,
            "total_page": result.total_page,
            "chunk_days": chunk_days,
        }
    }
    job.status, job.finished_at = "COMPLETE", datetime.now(UTC)
    session.flush()
    session.connection().execute(
        delete(ReportSyncPage).where(col(ReportSyncPage.job_id) == job.id)
    )
    cursor = job.start_date
    while cursor <= job.end_date:
        end = min(job.end_date, cursor + timedelta(days=chunk_days - 1))
        enqueue_job(
            session,
            tenant_id=job.tenant_id,
            bc_id=job.bc_id,
            kind=job.kind,
            tier=next(
                tier for tier, priority in PRIORITY.items() if priority == job.priority
            ),
            advertiser_ids=job.advertiser_ids,
            start=cursor,
            end=end,
            frozen_route=job.frozen_route,
            actor_id=job.actor_id,
            requested_at=request_time(job),
            execution_window_days=chunk_days,
        )
        cursor = end + timedelta(days=1)


def _run_material(session, job, reader, accounts):
    if len(accounts) != 1 or job.campaign_ids:
        raise DomainError("report_query_invalid", "素材日报要求一个账户且不能限定系列")
    started = monotonic()
    # 沿用执行器的账户当地日/收尾分类；过期热报才使用补数预算，今日与昨日
    # 收尾仍每步最多5页。历史5页往往只读数秒，却要等数千任务轮转才能续页，
    # 拉长完整快照窗口并增加重建。连续读仍有限额，不能一直占用后台执行槽。
    background = report_workload(session, job, now=datetime.now(UTC)) == "background"
    page_limit = (
        MATERIAL_BACKFILL_PAGES_PER_STEP if background else MATERIAL_PAGES_PER_STEP
    )
    deadline = started + MATERIAL_BACKFILL_STEP_SECONDS if background else None
    scope = _material_scope(job)
    progress = job.progress.get("material_paging")
    if progress is None:
        progress = {"scope": scope, "next_page": 1}
    if (
        progress.get("scope") != scope
        or type(progress.get("next_page")) is not int
        or progress["next_page"] < 1
    ):
        raise DomainError("report_query_invalid", "素材暂存与冻结请求范围不一致")
    page = progress["next_page"]
    stored_pages = session.exec(
        select(ReportSyncPage.page)
        .where(col(ReportSyncPage.job_id) == job.id)
        .order_by(col(ReportSyncPage.page))
    ).all()
    if stored_pages != list(range(1, page)):
        raise DomainError("report_response_invalid", "素材暂存分页不连续")
    if page > 1 and progress.get("observed_at") is None:
        # 旧版本暂存没有首页观测证据，不能以当前时间冒充旧页的新鲜度。
        # 只重建私有快照；正式事实保留到新快照完整读取并通过发布水位校验。
        session.connection().execute(
            delete(ReportSyncPage).where(col(ReportSyncPage.job_id) == job.id)
        )
        progress = {"scope": scope, "next_page": 1}
        page = 1
    if page == 1:
        # 已持有账户事务锁；从头读取的每个 attempt 都以实际首页开始为水位。
        observed_at = publish._database_now(session)
        job.progress = job.progress | {"snapshot_observed_at": observed_at.isoformat()}
    else:
        try:
            saved_observation = progress["observed_at"]
            if not isinstance(saved_observation, str):
                raise ValueError("invalid observation type")
            observed_at = datetime.fromisoformat(saved_observation)
            if observed_at.utcoffset() is None:
                raise ValueError("missing timezone")
        except (KeyError, TypeError, ValueError) as error:
            raise DomainError("report_query_invalid", "素材首页观测时间无效") from error
    for _ in range(page_limit):
        # 只在完整页边界让出，已存断点按原due_at轮转；正在执行的单次请求继续
        # 受SDK原期限和每次准入约束。拒绝仍立即退出，不重试或扩大平台配额。
        if deadline is not None and monotonic() >= deadline:
            return None
        result = reader.material_day(
            advertiser_id=job.advertiser_ids[0],
            start=job.start_date,
            end=job.end_date,
            page=page,
        )
        snapshot_changed = (
            page > 1
            and type(result.page) is int
            and result.page == page
            and all(
                type(v) is int and v >= 0
                for v in (result.total_page, result.total_number)
            )
            and (
                result.total_page != progress.get("total_page")
                or result.total_number != progress.get("total_number")
            )
        )
        if snapshot_changed and not result.rows:
            # 缩页会让旧游标返回合法空页；保留正式事实，按已有有限预算重建暂存。
            _log_material_snapshot_change(
                job, result, page=page, reason="total_changed"
            )
            raise DomainError(
                "report_snapshot_changed", "素材分页总数变化，需要重建完整快照"
            )
        # 非空页先校验账户、日期及页内唯一性，不能被总数变化掩盖永久坏行。
        keys = _validate_material_page(session, job, accounts, result, page)
        if snapshot_changed:
            _log_material_snapshot_change(
                job, result, page=page, reason="total_changed"
            )
            raise DomainError(
                "report_snapshot_changed", "素材分页总数变化，需要重建完整快照"
            )
        if (
            page == 1
            and result.total_number > MATERIAL_SPLIT_ROWS
            and job.start_date < job.end_date
        ):
            _split_material_dates(session, job, result)
            return 0
        session.add(
            ReportSyncPage(
                job_id=job.id,
                tenant_id=job.tenant_id,
                page=page,
                scope_key=scope,
                rows=list(result.rows),
                row_keys=keys,
                total_page=result.total_page,
                total_number=result.total_number,
            )
        )
        progress = {
            "scope": scope,
            "next_page": page + 1,
            "total_page": result.total_page,
            "total_number": result.total_number,
            # 续页与限流重试不能刷新首页时间，否则旧页会覆盖期间完成的热刷。
            "observed_at": observed_at.isoformat(),
        }
        job.progress = job.progress | {"material_paging": progress}
        session.flush()
        if result.next_page is None:
            evidence = (
                session.connection()
                .execute(
                    text("""SELECT coalesce(sum(jsonb_array_length(rows)),0),
                    coalesce(bool_and(scope_key=:scope AND tenant_id=:tenant),true)
                    FROM report_sync_page WHERE job_id=:job"""),
                    {"scope": scope, "tenant": job.tenant_id, "job": job.id},
                )
                .one()
            )
            if not evidence[1]:
                raise DomainError("report_query_invalid", "素材暂存证据超出冻结范围")
            if evidence[0] != result.total_number:
                raise DomainError("report_response_invalid", "素材完整页数与行数不一致")
            # 单遍服务器游标逐页解码；发布器分批规范化暂存，不把大日桶拼成Python列表。
            payloads = session.exec(
                select(ReportSyncPage.rows)
                .where(ReportSyncPage.job_id == job.id)
                .order_by(ReportSyncPage.page)
                .execution_options(stream_results=True)
            ).yield_per(1)
            rows = (row for payload in payloads for row in payload)
            count = publish.publish_material_day(
                session,
                tenant_id=job.tenant_id,
                job_id=job.id,
                accounts=accounts,
                start=job.start_date,
                end=job.end_date,
                rows=rows,
                requested_at=observed_at,
            )
            with diagnostics.stage("links_enqueued"):
                if material_links.needs_material_links(
                    session,
                    tenant_id=job.tenant_id,
                    advertiser_id=job.advertiser_ids[0],
                    start=job.start_date,
                    end=job.end_date,
                ):
                    enqueue_job(
                        session,
                        tenant_id=job.tenant_id,
                        bc_id=job.bc_id,
                        kind="material_links",
                        tier=next(
                            tier
                            for tier, priority in PRIORITY.items()
                            if priority == job.priority
                        ),
                        advertiser_ids=job.advertiser_ids,
                        start=job.start_date,
                        end=job.end_date,
                        frozen_route=job.frozen_route,
                        actor_id=job.actor_id,
                    )
            return count
        page = result.next_page
    # 只有完整页边界才让出账户锁；已提交页和游标在同一事务中持久化。
    return None


def cleanup_material_pages(session, *, now: datetime, retention_days: int = 7) -> int:
    """仅清理已终结七天的任务暂存；WAITING/RUNNING/QUEUED 均保留以便恢复。"""
    terminal = select(ReportSyncJob.id).where(
        col(ReportSyncJob.status).in_(["COMPLETE", "FAILED", "CANCELLED"]),
        col(ReportSyncJob.finished_at) < now - timedelta(days=retention_days),
    )
    result = session.connection().execute(
        delete(ReportSyncPage).where(col(ReportSyncPage.job_id).in_(terminal))
    )
    return result.rowcount


def _material_link_overview_ads(session, job):
    """复用同日期报表的广告范围，避免为少量当天帖子扫描全账户历史。"""
    if not job.progress.get("overview_complete") and not job.progress.get(
        "overview_owner_scoped"
    ):
        owners = list(
            session.connection()
            .execute(
                text(f"""
                WITH material_values AS ({TYPED_MATERIAL_WINDOW_VALUES_SQL}), missing AS (
                  SELECT DISTINCT d.main_material_id,d.main_material_type FROM material_report_day d
                  WHERE d.tenant_id=:tenant AND d.advertiser_id=:advertiser
                    AND d.stat_date BETWEEN :start AND :end
                    AND {valuable_material_condition("d", typed=True)}
                    AND NOT {complete_material_link("d", tenant=":tenant")})
                SELECT DISTINCT l.ad_remote_id FROM missing m
                LEFT JOIN material_post_link l ON l.tenant_id=:tenant
                  AND l.advertiser_id=:advertiser AND l.main_material_id=m.main_material_id
                  AND l.main_material_type=m.main_material_type AND NULLIF(l.ad_remote_id, '') IS NOT NULL
                ORDER BY l.ad_remote_id NULLS FIRST LIMIT 201
                """),
                {
                    **material_value_parameters(
                        job.tenant_id,
                        [(job.advertiser_ids[0], job.start_date, job.end_date)],
                    ),
                    "tenant": job.tenant_id,
                    "advertiser": job.advertiser_ids[0],
                    "start": job.start_date,
                    "end": job.end_date,
                },
            )
            .scalars()
        )
        if owners and None not in owners and len(owners) <= 200:
            # 全部缺口已有正向归属证据，概览只读这些广告。切换范围须从第一页开始；
            # 之后冻结分页，不能随着每页补齐而缩小过滤集合，导致跳页或漏读。
            job.progress = job.progress | {
                "overview_ad_ids": owners,
                "overview_owner_scoped": True,
                "next_page": 1,
            }
    if "overview_ad_ids" not in job.progress or (
        not job.progress["overview_ad_ids"]
        and "owner_lookup_first" not in job.progress
        and not job.progress.get("overview_complete")
    ):
        ids = list(
            session.connection()
            .execute(
                text("""
                SELECT DISTINCT ad_id FROM ad_report_day
                WHERE tenant_id=:tenant AND advertiser_id=:advertiser
                  AND stat_date BETWEEN :start AND :end
                  AND campaign_automation_type='UPGRADED_SMART_PLUS'
                ORDER BY ad_id LIMIT 201
                """),
                {
                    "tenant": job.tenant_id,
                    "advertiser": job.advertiser_ids[0],
                    "start": job.start_date,
                    "end": job.end_date,
                },
            )
            .scalars()
        )
        # 宽范围先用缺口自己的日期/候选广告定位，精确内容解析后仍缺失才补概览。
        # 避免几条旧帖被月份内超过200广告触发的全账户百页扫描挡住。
        progress = job.progress | {
            "overview_ad_ids": ids if len(ids) <= 200 else [],
            "owner_lookup_first": len(ids) > 200,
        }
        if ids and len(ids) <= 200 and not progress.get("overview_complete"):
            # 旧的全账户页不能与筛选页混接；已写入的真实映射保留。
            progress["next_page"] = 1
        job.progress = progress
    # 首次选定后随分页持久化，期间新增广告由下一轮补齐，不改变本轮查询。
    return tuple(job.progress["overview_ad_ids"])


def _run(
    session, job: ReportSyncJob, reader: _CountingReader | CheckpointReader
) -> int | None:
    advertisers = tuple(job.advertiser_ids)
    accounts = _accounts(session, job.tenant_id, advertisers)
    s, e, t, j = job.start_date, job.end_date, job.tenant_id, job.id
    if job.kind in BASIC_KINDS:
        reader = CheckpointReader(
            session, job, reader, scope=_material_scope(job), row_limit=TRUNCATION_ROWS
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
    if job.kind == "material_day":
        return _run_material(session, job, reader, accounts)
    if job.kind == "material_links":
        # 旧等待任务按新金额范围自然复核结束；不能再为零金额帖子读目录或平台身份。
        if not material_links.needs_material_links(
            session, tenant_id=t, advertiser_id=advertisers[0], start=s, end=e
        ):
            return 0
        # 库MID与帖子ID是不同命名空间；每步单独回读非Spark的精确视频身份。
        if material_identity.resolve_asset_videos(session, job, reader):
            return None
        if spark_identity.resolve_spark_videos(session, job, reader):
            return None
        with diagnostics.stage("links_overview_candidates"):
            overview_ads = _material_link_overview_ads(session, job)
        complete = material_links.refresh_material_links(
            session,
            tenant_id=t,
            advertiser_id=advertisers[0],
            read_overview=lambda p: reader.material_ad_links(
                advertiser_id=advertisers[0], page=p, ad_ids=overview_ads
            ),
            read_ad_posts=lambda ads, first, last, p: reader.material_ad_posts(
                advertiser_id=advertisers[0], ad_ids=ads, start=first, end=last, page=p
            ),
            now=datetime.now(UTC),
            start=s,
            end=e,
            progress=job.progress,
            overview_ad_ids=overview_ads,
        )
        # 本页已证实的广告身份立即补目录，不能被后续概览分页或旧帖二分搜索挡住。
        # 补读与本页关联同事务提交；复用有界、含删除对象且按冻结路由去重的原请求。
        with diagnostics.stage("links_directory_request"):
            request_link_directory(session, job, now=datetime.now(UTC))
        # JSONB 的原地修改需要显式通知 ORM；每个续步都持久化已经完成的页和关联。
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(job, "progress")
        # 最后一页或最后一个归属查询可能刚产生可读取的身份。先提交本次单请求，
        # 立即续步读取新身份；不能把尚未执行的读取误当外部依赖，提前退避十分钟。
        if complete and (
            material_identity.pending_asset_ids(session, job)
            or spark_identity.pending_spark_links(session, job)
        ):
            return None
        return None if not complete else 0
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


def _read_campaign_batches(reader, advertisers, start, end):
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
                    lambda p, batch=batch: reader.ad_day(
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

    def __getattr__(self, name) -> Callable[..., RawReportPage]:
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
            select(ReportSyncJob).where(ReportSyncJob.id == job_id)
        ).one()
        if job.claim_token != claim_token or job.status != "RUNNING":
            return "FAILED"  # 旧领取者：不得改写新领取的任务
        boundary = TransactionBoundaryReader(session, job, reader)
        counted = _CountingReader(boundary)
        try:
            # 网络读取不持业务事务；只在发布器内串行同桶替换并核验观测水位。
            # 关闭expire避免commit后访问冻结job属性隐式打开跨HTTP事务。
            session.expire_on_commit = False
            boundary._check()
            more = _run(session, job, counted) is None
            with diagnostics.stage("job_finalize"):
                _clear_completed_throttle(job, counted)
                if (
                    job.kind == "material_links"
                    and not more
                    and material_links.wait_for_dependencies(
                        session, job, now=datetime.now(UTC)
                    )
                ):
                    job.status = "WAITING"
                    result = "WAITING"
                elif more:
                    job.status, job.error_code = "WAITING", None
                    job.due_at = datetime.now(UTC)
                    result = "WAITING"
                else:
                    job.status, job.error_code, job.finished_at = (
                        "COMPLETE",
                        None,
                        datetime.now(UTC),
                    )
                    result = "COMPLETE"
                    if job.kind == "material_links" or job.campaign_ids:
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
            if job.kind not in BASIC_KINDS | {"material_day"}:
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
        job.request_count += counted.count
        from app.modules.sync_control.schedule import record_report_outcome

        record_report_outcome(session, job)
        job.claim_token = job.claimed_until = None
        with diagnostics.stage("job_commit"):
            session.commit()
        diagnostics.committed(result)
        return result


def _handle_error(session, job: ReportSyncJob, error: DomainError) -> str:
    job.error_code = error.code
    if error.code == "report_snapshot_changed" and job.kind == "material_links":
        # 重建当前旧帖查询并消耗既有有限重试预算；不能在不断变化的分页上无限续步。
        # 已证实的关联和完整概览保留，未完成集合绝不成为负缓存。
        job.progress = {
            key: value
            for key, value in job.progress.items()
            if key not in {"locate_post", "locate_queries"}
        }
    if error.code == "report_snapshot_changed" and job.kind in BASIC_KINDS | {
        "material_day"
    }:
        session.connection().execute(
            delete(ReportSyncPage).where(col(ReportSyncPage.job_id) == job.id)
        )
        job.progress = {
            key: value
            for key, value in job.progress.items()
            if key not in {"material_paging", "basic_paging"}
        }
    if (
        error.code == "report_snapshot_changed"
        and job.kind == "material_links"
        and job.attempts >= MAX_PROVIDER_RETRIES
    ):
        # 素材帖子集合会随平台新数据持续变化；把这种一致性重建失败写成终态，
        # 会让历史缺口永久停在 FAILED。清掉本轮定位游标并循环等待，下一轮
        # 重新建立完整证据；正向映射仍只在完整查询通过后发布。
        now = datetime.now(UTC)
        job.attempts = 0
        job.status = "WAITING"
        job.finished_at = None
        job.due_at = now + MATERIAL_LINK_SNAPSHOT_RECOVERY_DELAY
        recovery = job.progress.get("snapshot_recovery", {})
        job.progress = job.progress | {
            "snapshot_recovery": {
                "count": int(recovery.get("count", 0)) + 1,
                "at": now.isoformat(),
            }
        }
        return "WAITING"
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
        # 今日素材集合会在秒级变化；重建仍完整校验且最多五次，但不沿用上游故障的
        # 分钟级退避。按账户当地日核验，历史、昨日收尾及真实provider错误保持原策略。
        if (
            error.code == "report_snapshot_changed"
            and job.kind == "material_day"
            and job.priority in {1, 2}
            and report_workload(session, job, now=now) == "live"
        ):
            delay = MATERIAL_SNAPSHOT_RETRY_SECONDS[job.attempts - 1]
        job.due_at = now + timedelta(seconds=delay)
        return "WAITING"
    job.status, job.finished_at = "FAILED", datetime.now(UTC)
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
