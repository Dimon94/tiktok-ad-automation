"""热报发现的在投对象优先补目录，避免全量历史队列遮蔽自动规则。"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy import text
from sqlmodel import Session, col, select

from app.core.config import settings
from app.integrations.tiktok.contracts.ads import EntityKind, EntityRef
from app.modules.ads.directory_requests import request_directory
from app.modules.ads.directory_services import (
    IDENTITY_SERVICE,
    pending_directory_condition,
    promote_status_requests,
)
from app.modules.ads.material_directory_children import is_material_child
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.automation_rules.models import AutomationRule
from app.modules.reporting import diagnostics
from app.modules.reporting.store_models import ReportSyncJob

# 素材关联补读保持原有单轮上限；广告日报刷新不截断账户对象集合。
MAX_REFRESH_ADS = 100
MAX_REFRESH_CREATIVES = 100
STATUS_INTERVAL = timedelta(minutes=30)
PARENT_INTERVAL = timedelta(minutes=60)
_AD_TYPES = {
    "UPGRADED_SMART_PLUS": "SMART_PLUS",
    "SMART_PLUS": "LEGACY_SMART_PLUS",
    "MANUAL": "REGULAR",
}
_CREATIVE_REPORT_TYPES = {
    "UPGRADED_SMART_PLUS": ("creative", "SMART_PLUS"),
    "UPGRADED_SMART_PLUS_CREATIVE": ("creative", "SMART_PLUS"),
    "SMART_PLUS": ("ad", "LEGACY_SMART_PLUS"),
    "LEGACY_SMART_PLUS": ("ad", "LEGACY_SMART_PLUS"),
    "MANUAL": ("ad", "REGULAR"),
}


def rule_accounts(
    session: Session, *, tenant_id: UUID, bc_id: str, accounts: Sequence[str]
) -> set[str]:
    """规则覆盖的账户优先刷新状态；普通目录降低重复读取，给财务事实留下配额。"""
    covered = set()
    rules = session.exec(
        select(AutomationRule).where(
            AutomationRule.tenant_id == tenant_id, col(AutomationRule.enabled).is_(True)
        )
    ).all()
    for rule in rules:
        scope = rule.definition.get("accounts", {})
        if scope.get("mode") == "ALL" or (
            scope.get("mode") == "BC" and bc_id in scope.get("bc_ids", ())
        ):
            return set(accounts)
        if scope.get("mode") == "ACCOUNTS":
            covered.update(set(scope.get("advertiser_ids", ())) & set(accounts))
    return covered


def directory_interval(kind: str, *, has_rules: bool) -> timedelta:
    """规则账户三层状态必须跟上同一实时轮次，普通账户上级配置可更慢。"""
    # planner 经 jobs 导入本模块；在运行时取常量，避免初始化循环，也避免复制周期值。
    from app.modules.reporting import planner

    if has_rules:
        return planner.HOT_INTERVAL
    return STATUS_INTERVAL if kind == "ad" else PARENT_INTERVAL


@dataclass(frozen=True)
class DirectoryRefreshResult:
    created: int
    complete: bool


@diagnostics.timed("directory_refresh")
def refresh_report_directory(
    session: Session, job: ReportSyncJob, *, now: datetime | None = None
) -> int:
    """只刷新本任务账户当地今日有消耗、缺失或到期且未被在途查询覆盖的对象。"""
    if job.kind != "ad_day" or job.priority != 1:
        return 0
    return refresh_account_directory(
        session,
        tenant_id=job.tenant_id,
        bc_id=job.bc_id,
        actor_id=job.actor_id,
        frozen_route=job.frozen_route,
        advertiser_ids=job.advertiser_ids,
        now=now,
        _report_refresh=True,
    ).created


@diagnostics.timed("creative_identity_refresh")
def refresh_report_creative_directory(
    session: Session, job: ReportSyncJob, *, now: datetime | None = None
) -> int:
    """为热/素材创意事实补发有界身份目录读取，不阻塞报表发布。"""
    if (
        not settings.ADS_SYNC_ENABLED
        or job.kind != "creative_day"
        or job.priority > 2
        or not job.advertiser_ids
    ):
        return 0
    # 只处理本次创意事实窗口内有金额或未知收入的创意；历史大回补由独立
    # 全量目录任务承接，避免每个历史报表任务再生成逐创意目录请求。
    rows = (
        session.connection()
        .execute(
            text("""
        SELECT DISTINCT r.advertiser_id,r.creative_id,r.campaign_automation_type
        FROM creative_report_day r
        LEFT JOIN ad_creative c
          ON c.tenant_id=r.tenant_id AND c.advertiser_id=r.advertiser_id
         AND c.creative_id=r.creative_id
        WHERE r.tenant_id=:tenant AND r.advertiser_id=ANY(:accounts)
          AND r.stat_date BETWEEN :start_date AND :end_date
          AND (r.spend IS NULL OR r.spend<>0 OR r.revenue_total IS NULL OR r.revenue_total<>0)
          AND COALESCE(c.identity_status,'UNRESOLVED') NOT IN ('RESOLVED','COMPOSITE')
        ORDER BY r.advertiser_id,r.creative_id
        """),
            {
                "tenant": job.tenant_id,
                "accounts": list(job.advertiser_ids),
                "start_date": job.start_date,
                "end_date": job.end_date,
            },
        )
        .mappings()
    )
    grouped: dict[tuple[str, str, str], list[EntityRef]] = defaultdict(list)
    seen: set[tuple[str, str, str, str]] = set()
    for row in rows:
        target = _CREATIVE_REPORT_TYPES.get(row["campaign_automation_type"])
        if target is None:
            continue
        kind, ad_type = target
        entity_kind = cast(EntityKind, kind)
        key = (row["advertiser_id"], entity_kind, ad_type)
        identity = (*key, row["creative_id"])
        if identity in seen or len(grouped[key]) >= MAX_REFRESH_CREATIVES:
            continue
        seen.add(identity)
        grouped[key].append(
            EntityRef(
                job.tenant_id,
                row["advertiser_id"],
                entity_kind,
                row["creative_id"],
            )
        )
    created = 0
    for (advertiser_id, kind, ad_type), refs in sorted(grouped.items()):
        created += request_directory(
            session,
            tenant_id=job.tenant_id,
            bc_id=job.bc_id,
            frozen_route=job.frozen_route,
            actor_id=job.actor_id,
            advertiser_ids=[advertiser_id],
            scope="targeted",
            refs=tuple(refs),
            ad_types={advertiser_id: {ad_type: {kind}}},
            include_deleted=True,
            workload="background",
            service=IDENTITY_SERVICE,
            dispatch_immediately=False,
            now=now,
        )
    diagnostics.counts(
        creative_identity_refs=sum(len(refs) for refs in grouped.values()),
        creative_identity_created=created,
    )
    return created


def refresh_account_directory(
    session: Session,
    *,
    tenant_id: UUID,
    bc_id: str,
    actor_id: UUID,
    frozen_route: dict,
    advertiser_ids: Sequence[str],
    now: datetime | None = None,
    max_refs: int | None = None,
    _report_refresh: bool = False,
) -> DirectoryRefreshResult:
    """复用已发布事实补目录；已排请求充当续读断点。

    报表入口在其事务内只调用一次；不能先走该共享围栏，再在同事务
    调用默认的BC排他规划，以免并发锁升级死锁。健康轮转始终用默认路径。
    """
    if not settings.ADS_SYNC_ENABLED or not advertiser_ids:
        return DirectoryRefreshResult(0, True)
    from app.modules.reporting.planner import REFRESH_MARGIN

    now = now or datetime.now(UTC)
    # 报表先锁整批账户，重叠批次互斥；BC共享围栏允许不重叠批次并行。
    # 健康修复同事务会轮转多户，必须保留BC排他锁，避免账户锁反序等待。
    # 原BC排他键也保护滚动发布期间尚未退出的旧进程，不留下规划去重空窗。
    report_accounts = sorted(set(advertiser_ids)) if _report_refresh else []
    with diagnostics.stage("directory_lock"):
        session.connection().execute(
            text(
                "SELECT pg_advisory_xact_lock_shared(hashtextextended(:scope, 0))"
                if report_accounts
                else "SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"
            ),
            {"scope": f"report-directory:{tenant_id}:{bc_id}"},
        )
        # 与财务发布账户锁保持同一排序，输入顺序不同也不能形成账户锁环。
        for advertiser in report_accounts:
            session.connection().execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
                {"scope": f"report-directory-account:{tenant_id}:{bc_id}:{advertiser}"},
            )
    with diagnostics.stage("directory_rules"):
        strict_accounts = rule_accounts(
            session,
            tenant_id=tenant_id,
            bc_id=bc_id,
            accounts=advertiser_ids,
        )
    with diagnostics.stage("directory_pending"):
        pending = session.exec(
            select(AdDirectoryRun).where(
                AdDirectoryRun.tenant_id == tenant_id,
                AdDirectoryRun.bc_id == bc_id,
                AdDirectoryRun.actor_id == actor_id,
                AdDirectoryRun.frozen_route == frozen_route,
                AdDirectoryRun.workload == "live",
                col(AdDirectoryRun.advertiser_id).in_(advertiser_ids),
                pending_directory_condition(),
            )
        ).all()
    with diagnostics.stage("directory_facts_query"):
        rows = (
            session.connection()
            .execute(
                text("""
        SELECT r.advertiser_id, r.ad_id, r.adgroup_id, r.campaign_id, r.campaign_automation_type,
          a.observed_at AS ad_observed_at, g.observed_at AS adgroup_observed_at,
          c.observed_at AS campaign_observed_at
        FROM ad_report_day r
        LEFT JOIN ad_object a ON a.tenant_id=r.tenant_id AND a.advertiser_id=r.advertiser_id AND a.kind='ad' AND a.remote_id=r.ad_id
        LEFT JOIN ad_object g ON g.tenant_id=r.tenant_id AND g.advertiser_id=r.advertiser_id AND g.kind='adgroup' AND g.remote_id=r.adgroup_id
        LEFT JOIN ad_object c ON c.tenant_id=r.tenant_id AND c.advertiser_id=r.advertiser_id AND c.kind='campaign' AND c.remote_id=r.campaign_id
        WHERE r.tenant_id=:tenant AND r.advertiser_id=ANY(:accounts) AND r.spend>0
          AND r.stat_date BETWEEN :first_day AND :last_day
          AND r.stat_date=(CAST(:now AS timestamptz) AT TIME ZONE r.timezone)::date
    """),
                {
                    "tenant": tenant_id,
                    "accounts": advertiser_ids,
                    "now": now,
                    "first_day": now.astimezone(UTC).date() - timedelta(days=1),
                    "last_day": now.astimezone(UTC).date() + timedelta(days=1),
                },
            )
            .mappings()
        )
    diagnostics.counts(directory_pending=len(pending))
    # 不限 sync_job_id：同账户今日事实可由不同任务发布；日期粗范围用于分区裁剪，
    # 精确当地日仍以事实时区为准。仅收集缺失/到期 ID，不因邻居过期重读新鲜对象。
    with diagnostics.stage("directory_candidates"):
        objects: dict[tuple[str, str, EntityKind], set[str]] = defaultdict(set)
        missing: dict[tuple[str, str, EntityKind], set[str]] = defaultdict(set)
        parents: dict[tuple[str, str, EntityKind], dict[str, set[str]]] = defaultdict(
            lambda: defaultdict(set)
        )
        layers: tuple[tuple[EntityKind, str, str | None], ...] = (
            ("campaign", "campaign_id", None),
            ("adgroup", "adgroup_id", "campaign_id"),
            ("ad", "ad_id", "adgroup_id"),
        )
        for row in rows:
            ad_type = _AD_TYPES.get(row.campaign_automation_type)
            if ad_type is None:
                continue  # 类型未获报表证明时不猜测平台端点。
            for kind, column, parent_column in layers:
                observed_at = row[f"{kind}_observed_at"]
                cutoff = now - (
                    directory_interval(
                        kind, has_rules=row.advertiser_id in strict_accounts
                    )
                    - REFRESH_MARGIN
                )
                if not row[column] or (
                    observed_at is not None and observed_at > cutoff
                ):
                    continue
                key = (row.advertiser_id, ad_type, kind)
                objects[key].add(row[column])
                if observed_at is None:
                    # 仅现有事实关联证明未观测的对象标缺口，到期刷新不能冒充缺失。
                    missing[key].add(row[column])
                if parent_column and row[parent_column]:
                    parents[key][row[parent_column]].add(row[column])
        status_requests = []
        missing_run_ids = set()
        for run in pending:
            # 素材子运行只读冻结素材桥，不能代替通用对象目录；授权范围已在查询中限定。
            if is_material_child(run):
                continue
            key = (run.advertiser_id, run.ad_type, run.kind)
            remaining = objects.get(key)
            if not remaining:
                continue
            ids, parent_ids = run.query.get("ids", ()), run.query.get("parent_ids", ())
            if parent_ids:
                # parent_ids 只匹配当前层的直接父级；同时有两类筛选时取交集。
                # 父级索引避免对每个在途查询重复扫描整账户候选。
                by_parent = parents.get(key, {})
                covered = {
                    remote_id
                    for parent_id in parent_ids
                    for remote_id in by_parent.get(parent_id, ())
                }
                if ids:
                    covered.intersection_update(ids)
            else:
                covered = remaining.intersection(ids) if ids else remaining.copy()
            # 只有完全无筛选的查询覆盖整个层；不相关在途 ID 不会遮住新缺失对象。
            if covered & remaining:
                status_requests.append(run)
                if covered & remaining & missing.get(key, set()):
                    missing_run_ids.add(run.id)
            remaining.difference_update(covered)
    with diagnostics.stage("directory_promote"):
        promote_status_requests(
            session, status_requests, missing_run_ids=missing_run_ids
        )
    missing_refs_by_type: dict[str, list[EntityRef]] = defaultdict(list)
    refs_by_type: dict[str, list[EntityRef]] = defaultdict(list)
    types: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    selected = 0
    complete = True
    for (advertiser, ad_type, kind), remote_ids in sorted(objects.items()):
        for remote_id in sorted(remote_ids):
            if max_refs is not None and selected >= max_refs:
                complete = False
                continue
            selected += 1
            ref = EntityRef(tenant_id, advertiser, kind, remote_id)
            refs_by_type[ad_type].append(ref)
            if remote_id in missing.get((advertiser, ad_type, kind), set()):
                missing_refs_by_type[ad_type].append(ref)
            types[advertiser][ad_type].add(kind)
    created = 0
    # 沿用唯一目录请求入口的100 ID分片；分类型传 refs，避免混合类型误发端点。
    for ad_type, refs in sorted(refs_by_type.items()):
        advertisers = sorted({ref.advertiser_id for ref in refs})
        with diagnostics.stage("directory_request"):
            created += request_directory(
                session,
                tenant_id=tenant_id,
                bc_id=bc_id,
                frozen_route=frozen_route,
                actor_id=actor_id,
                advertiser_ids=advertisers,
                scope="targeted",
                dispatch_immediately=False,
                refs=tuple(refs),
                missing_refs=tuple(missing_refs_by_type[ad_type]),
                ad_types={adv: {ad_type: types[adv][ad_type]} for adv in advertisers},
                include_deleted=True,
            )
    diagnostics.counts(
        directory_refs=sum(len(refs) for refs in refs_by_type.values()),
        directory_created=created,
    )
    return DirectoryRefreshResult(created, complete)
