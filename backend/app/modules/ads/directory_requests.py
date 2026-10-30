"""目录按半小时槽合并；操作回读单独投递，保留冻结路由与逐次写后的新观察。"""

import hashlib
import json
from collections.abc import Collection, Mapping
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4, uuid5

from sqlalchemy import and_, text
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.jobs.outbox import enqueue_after_commit
from app.modules.accounts.routing import verify_route
from app.modules.ads.directory_calibration import (
    PERIODIC_KEY,
    recent_calibration,
    unstarted_without_delivery,
)
from app.modules.ads.directory_dispatch import enqueue_directory_step
from app.modules.ads.directory_services import (
    IDENTITY_SERVICE,
    SERVICE_KEY,
    STATUS_SERVICE,
)
from app.modules.ads.sync_models import AdDirectoryRun

DIRECTORY_AD_TYPES = ("REGULAR", "LEGACY_SMART_PLUS", "SMART_PLUS")
# 每个目录层级与广告类型一个运行：(层级, 广告类型, 每页条数)。
DIRECTORY_SPECS = (
    ("campaign", "REGULAR", 1000),
    ("campaign", "LEGACY_SMART_PLUS", 1000),
    ("campaign", "SMART_PLUS", 1000),
    ("adgroup", "REGULAR", 1000),
    ("adgroup", "LEGACY_SMART_PLUS", 1000),
    ("adgroup", "SMART_PLUS", 1000),
    ("ad", "REGULAR", 100),
    ("ad", "LEGACY_SMART_PLUS", 100),
    ("ad", "SMART_PLUS", 100),
    ("creative", "SMART_PLUS", 100),
)
KINDS_BY_TYPE = {
    ad_type: tuple(
        kind for kind, spec_type, _ in DIRECTORY_SPECS if spec_type == ad_type
    )
    for ad_type in DIRECTORY_AD_TYPES
}
# 广告整页读取会补齐同组创意与素材关联；周期目录不再重复扫全账户创意。
# 显式目录请求仍保留 DIRECTORY_SPECS 的完整能力。
_PERIODIC_KINDS_BY_TYPE = {
    ad_type: tuple(kind for kind in kinds if kind != "creative")
    for ad_type, kinds in KINDS_BY_TYPE.items()
}
# 未使用类型的探测只读广告系列：该类型没有系列就不可能有广告组和广告。
PROBE_KINDS = ("campaign",)


def _partition_key(query: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            {key: value for key, value in query.items() if key != SERVICE_KEY},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def directory_ad_types(
    session: Session,
    *,
    tenant_id: UUID,
    advertiser_ids: Collection[str],
    active_ids: Collection[str],
) -> dict[str, dict[str, tuple[str, ...]]]:
    """使用过的类型校准三层；未知类型只探测系列，首次活跃空目录校准三层。

    此处只决定查询范围；所有类型的新鲜度统一在请求入口按冻结分区核验，
    不能用其它 BC/授权或未发布的探测记录预先删除候选。
    """
    accounts = sorted(set(advertiser_ids))
    if not accounts:
        return {}
    session.flush()
    used: dict[str, set[str]] = {}
    # 三种类型只需证明存在，不能为找一个类型回表遍历整个账户的广告历史。
    # 保留任意对象存在性，未知历史类型也不能被误当作“目录全空”的活跃账户。
    for advertiser_id, ad_type in session.connection().execute(
        text("""
        SELECT a.advertiser_id, used.ad_type
        FROM unnest(CAST(:accounts AS text[])) a(advertiser_id)
        JOIN LATERAL (
          SELECT true FROM ad_object o
          WHERE o.tenant_id=:tenant AND o.advertiser_id=a.advertiser_id LIMIT 1
        ) present ON true
        LEFT JOIN LATERAL (
          SELECT t.ad_type FROM unnest(CAST(:types AS text[])) t(ad_type)
          JOIN LATERAL (
            SELECT true FROM ad_object o
            WHERE o.tenant_id=:tenant AND o.advertiser_id=a.advertiser_id
              AND o.ad_type=t.ad_type LIMIT 1
          ) found ON true
        ) used ON true
        """),
        {"tenant": tenant_id, "accounts": accounts, "types": list(DIRECTORY_AD_TYPES)},
    ):
        used.setdefault(advertiser_id, set()).add(ad_type or "")
    active = set(active_ids)
    result: dict[str, dict[str, tuple[str, ...]]] = {}
    for advertiser_id in accounts:
        in_use = used.get(advertiser_id, set())
        if advertiser_id in active and not in_use:
            result[advertiser_id] = dict(_PERIODIC_KINDS_BY_TYPE)
            continue
        result[advertiser_id] = {
            ad_type: _PERIODIC_KINDS_BY_TYPE[ad_type]
            if ad_type in in_use
            else PROBE_KINDS
            for ad_type in DIRECTORY_AD_TYPES
        }
    return result


def directory_unblocked_accounts(
    session: Session,
    *,
    tenant_id: UUID,
    bc_id: str,
    actor_id: UUID,
    frozen_route: dict,
    advertiser_ids: Collection[str],
    now: datetime | None = None,
) -> list[str]:
    """先排除已有全量在途的账户；新鲜度稍后按实际类型/层级分区判断。"""
    accounts = sorted(set(advertiser_ids))
    now = now or datetime.now(UTC)
    if not accounts:
        return []
    session.flush()
    # 旧全量仍在读取/等待时先让它完成；在扫描 ad_object 大表前去掉重叠账户。
    # 定向回读和不同冻结授权不能替代本轮全量，不纳入这个周期去重范围。
    # 先物化当前范围内的待执行集合，避免账户IN使优化器交叉扫描大量历史索引。
    pending = set(
        session.connection()
        .execute(
            text("""
            WITH pending AS MATERIALIZED (
              SELECT advertiser_id FROM ad_directory_run
              WHERE tenant_id=:tenant AND bc_id=:bc AND actor_id=:actor
                AND frozen_route=CAST(:route AS jsonb)
                AND status IN ('QUEUED','RUNNING','ADMISSION_WAIT','WAITING_REMOTE')
                AND NOT (
                  COALESCE(query->'periodic_directory_v1'='true'::jsonb,false)
                  AND status='QUEUED' AND next_page=1
                  AND claim_token IS NULL AND claimed_until IS NULL
                  AND COALESCE(next_attempt_at>:now,false)
                )
                AND (query->>'include_deleted')::boolean IS TRUE
                AND coalesce(jsonb_array_length(query->'ids'),0)=0
                AND coalesce(jsonb_array_length(query->'parent_ids'),0)=0
            )
            SELECT DISTINCT advertiser_id FROM pending WHERE advertiser_id=ANY(:ids)
            """),
            {
                "tenant": tenant_id,
                "bc": bc_id,
                "actor": actor_id,
                "route": json.dumps(frozen_route),
                "ids": accounts,
                "now": now,
            },
        )
        .scalars()
    )
    return [advertiser_id for advertiser_id in accounts if advertiser_id not in pending]


def directory_id_chunks(
    ids: tuple[str, ...], *, limit: int = 100
) -> tuple[tuple[str, ...], ...]:
    """Split directory filters to the platform's maximum 100 IDs per request."""

    if limit != 100:
        raise ValueError("directory ID limit must remain 100")
    if not ids:
        return ((),)
    return tuple(ids[offset : offset + limit] for offset in range(0, len(ids), limit))


def directory_filter_chunks(
    ids: tuple[str, ...], parent_ids: tuple[str, ...]
) -> tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]:
    """Return a bounded product when both ID filters need pagination."""

    return tuple(
        (id_chunk, parent_chunk)
        for id_chunk in directory_id_chunks(ids)
        for parent_chunk in directory_id_chunks(parent_ids)
    )


def request_directory(
    session: Session,
    *,
    tenant_id,
    bc_id,
    frozen_route,
    actor_id,
    advertiser_ids,
    scope,
    refs=(),
    ad_types: Mapping[str, Mapping[str, Collection[str]]] | None = None,
    include_deleted: bool = False,
    workload: str | None = None,
    dispatch_immediately: bool = True,
    service: str | None = STATUS_SERVICE,
    periodic_intervals: Mapping[str, timedelta] | None = None,
    now: datetime | None = None,
) -> int:
    """ad_types 为空表示请求全部类型全部层级；周期调度传入 directory_ad_types 的裁剪结果。

    同一账户同一查询已有尚未开始的运行时不再新建：积压期间每轮的周期请求
    不会层层叠加，排队中的那一条开始执行时自然读到最新数据。
    历史身份补录可显式包含已删除对象；仍按指定ID有界读取，不扩大为全账户扫描。
    周期定向刷新关闭立即入队，只唤醒公平派发；操作后的显式回读保留立即投递。
    """
    if not settings.ADS_SYNC_ENABLED or frozen_route.get("channel") != "OFFICIAL_API":
        return 0
    workload = workload or ("live" if scope == "targeted" else "background")
    if workload not in ("live", "background"):
        raise DomainError("directory_request_invalid", "目录服务分类无效")
    if service not in (None, STATUS_SERVICE, IDENTITY_SERVICE):
        raise DomainError("directory_request_invalid", "目录调度目的无效")
    route = FrozenTikTokRoute.model_validate(frozen_route)
    advertiser_ids = tuple(sorted(set(advertiser_ids)))
    periodic = periodic_intervals is not None
    if periodic and (
        scope != "directory"
        or set(periodic_intervals) != set(advertiser_ids)
        or any(interval <= timedelta(0) for interval in periodic_intervals.values())
    ):
        raise DomainError("directory_request_invalid", "周期目录校准范围无效")
    if (
        route.tenant_id != tenant_id
        or route.bc_id != bc_id
        or scope not in ("directory", "targeted")
    ):
        raise DomainError("directory_request_invalid", "目录请求范围无效")
    if any(
        ref.tenant_id != tenant_id or ref.advertiser_id not in advertiser_ids
        for ref in refs
    ):
        raise DomainError("directory_request_invalid", "目录对象超出账户范围")
    context = TenantContext(tenant_id=tenant_id, actor_id=actor_id, role="operator")
    for advertiser in advertiser_ids:
        verify_route(
            session,
            context=context,
            route=route,
            advertiser_id=advertiser,
            capability="read",
        )
    now = now or datetime.now(UTC)
    payload = [
        frozen_route,
        str(actor_id),
        advertiser_ids,
        scope,
        sorted((str(r.tenant_id), r.advertiser_id, r.kind, r.remote_id) for r in refs),
        int(now.timestamp()) // 1800,
    ]
    if periodic:
        payload.append(PERIODIC_KEY)
    slot_id = uuid5(
        UUID("c5c96c13-7e3a-4cbb-a7cc-9b12e9d1c6ad"),
        json.dumps(payload, sort_keys=True),
    )
    # 每次操作后的回读都必须晚于该次写入；旧排队任务的请求序号也可能已经过时。
    request_id = uuid4() if scope == "targeted" else slot_id
    # 同一槽并发请求串行检查，避免重复插入目录任务。
    session.connection().execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": slot_id.int % (2**63)}
    )
    created = 0
    refill_run = None
    for advertiser_id in advertiser_ids:
        account_refs = tuple(ref for ref in refs if ref.advertiser_id == advertiser_id)
        for kind, ad_type, page_size in DIRECTORY_SPECS:
            if ad_types is not None and kind not in ad_types.get(advertiser_id, {}).get(
                ad_type, ()
            ):
                continue
            ids = tuple(
                sorted(
                    {
                        ref.remote_id
                        for ref in account_refs
                        if ref.kind == kind
                        and (
                            scope != "targeted"
                            or (kind == "creative" and ref.kind == "creative")
                            or (kind != "creative" and ref.kind == kind)
                        )
                    }
                )
            )
            parent_kind = {
                "campaign": (),
                "adgroup": ("campaign",),
                "ad": ("adgroup",),
                "creative": ("ad",),
            }[kind]
            parent_ids = tuple(
                sorted(
                    {ref.remote_id for ref in account_refs if ref.kind in parent_kind}
                )
            )
            # 明确对象 ID 时不再附带父级筛选：分层刷新的父子候选未必相同，
            # 两种条件取交集会漏掉本次明确要求读取的对象。创意接口也不接受父级。
            if ids or kind == "creative":
                parent_ids = ()
            if scope == "targeted" and not ids and not parent_ids:
                continue
            # TikTok 对 filtering.ids/parent_ids 都限制最多 100 个；历史上把全部
            # Smart+ 创意 ID 放进一个运行，导致任务进入队列后必然 ads_query_invalid。
            for id_chunk, parent_chunk in directory_filter_chunks(ids, parent_ids):
                query = {
                    "advertiser_id": advertiser_id,
                    "kind": kind,
                    "ad_type": ad_type,
                    "page": 1,
                    "page_size": page_size,
                    "ids": list(id_chunk),
                    "parent_ids": list(parent_chunk),
                    "include_deleted": scope == "directory" or include_deleted,
                }
                if scope == "targeted" and service is not None:
                    query[SERVICE_KEY] = service
                partition = _partition_key(query)
                if periodic:
                    if (
                        recent_calibration(
                            session,
                            tenant_id=tenant_id,
                            advertiser_id=advertiser_id,
                            bc_id=bc_id,
                            actor_id=actor_id,
                            frozen_route=frozen_route,
                            partition_key=partition,
                            cutoff=now - periodic_intervals[advertiser_id],
                        )
                        is not None
                    ):
                        continue
                    # 调度元数据不进入查询摘要，也不发送给平台。显式请求保持独立。
                    query[PERIODIC_KEY] = True
                existing = session.exec(
                    select(AdDirectoryRun)
                    .where(
                        AdDirectoryRun.tenant_id == tenant_id,
                        AdDirectoryRun.advertiser_id == advertiser_id,
                        AdDirectoryRun.partition_key == partition,
                        AdDirectoryRun.bc_id == bc_id,
                        AdDirectoryRun.actor_id == actor_id,
                        AdDirectoryRun.frozen_route == frozen_route,
                        col(AdDirectoryRun.query).op("?")(PERIODIC_KEY) == periodic,
                        # 已被领取的运行可能已读过首页，不能代替本次刷新。
                        (col(AdDirectoryRun.request_id) == request_id)
                        | and_(
                            scope == "directory",
                            col(AdDirectoryRun.status) == "QUEUED",
                            col(AdDirectoryRun.claim_token).is_(None),
                        ),
                    )
                    .limit(1)
                    .with_for_update()
                ).first()
                if existing is not None:
                    # 冷转热后校准间隔缩短：复用原身份提前未开始的延期任务。
                    if (
                        periodic
                        and existing.next_attempt_at is not None
                        and existing.next_attempt_at > now
                        and unstarted_without_delivery(session, existing)
                    ):
                        existing.next_attempt_at = now
                        existing.error_code = None
                    continue
                run = AdDirectoryRun(
                    tenant_id=tenant_id,
                    advertiser_id=advertiser_id,
                    bc_id=route.bc_id,
                    actor_id=actor_id,
                    connection_id=route.connection_id,
                    channel=route.channel,
                    frozen_route=frozen_route,
                    request_id=request_id,
                    workload=workload,
                    partition_key=partition,
                    query=query,
                    status="QUEUED",
                    claim_generation=1,
                    next_page=1,
                    coverage="PENDING",
                    observed_at=now,
                    kind=kind,
                    ad_type=ad_type,
                )
                session.add(run)
                if scope == "targeted":
                    session.flush()
                    if dispatch_immediately:
                        enqueue_directory_step(session, run, initial=True)
                    elif refill_run is None:
                        refill_run = run
                created += 1
    session.flush()
    if refill_run is not None:
        # 整批只发一次同事务唤醒，空槽无需等待 Beat；统一派发器先服务已有积压。
        # 使用请求专属键，不能吞掉同一运行完成后的补位信号。
        enqueue_after_commit(
            session,
            context=context,
            task_name="reporting.plan_due",
            task_key=f"directory-request-refill:{refill_run.id}:{refill_run.claim_generation}",
            payload={"dispatch_only": True},
        )
    return created
