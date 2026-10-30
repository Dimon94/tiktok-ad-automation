"""在本地发布数据上构建并读取短期报表快照。

查询请求只在第一次请求时读取目录/事实。后续分页和选择均消费
快照行，避免修正或改名在同一个浏览器操作中混入新版本。
"""

from __future__ import annotations

import base64
import hashlib
import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import func, text
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.db import engine
from app.modules.accounts.models import TenantBC
from app.modules.reporting import query_diagnostics
from app.modules.reporting.dimension_query import (
    materialize_rows,
    metric_vector,
)
from app.modules.reporting.filters import authorized_grants, compile_filter
from app.modules.reporting.query_models import (
    QuerySnapshot,
    QuerySnapshotRow,
    read_snapshot,
    read_snapshot_rows,
)
from app.modules.reporting.query_reuse import (
    QueryCancellation,
    find_or_build_snapshot,
    query_key,
    reuse_in_transaction,
)
from app.modules.reporting.read_models.coverage import (
    projected_states,
    projection_coverage,
)
from app.modules.reporting.row_set import (
    MATERIAL_UNSUPPORTED,
    SnapshotMember,
    SnapshotTotals,
    insert_snapshot_rows,
)
from app.modules.reporting.schemas import (
    AdsQueryPage,
    QuerySnapshotPublic,
    ReportingFilter,
    ReportRow,
)
from app.modules.reporting.store_models import METRIC_COLUMNS, ReportSyncState
from app.modules.tenants.permissions import require_tenant


def filter_digest(filters: ReportingFilter) -> str:
    """Canonical filter digest used to bind cursors and snapshots."""
    payload = json.dumps(
        filters.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(value: str) -> dict[str, Any]:
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        if not isinstance(payload, dict):
            raise ValueError
        return payload
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(404, detail="query_cursor_not_found") from exc


def frozen_member(row: QuerySnapshotRow) -> SnapshotMember:
    """快照行只冻结成员与顺序；展示和指标由 materialize_rows 按需补全。"""
    return SnapshotMember(row.row_key, tuple(row.refs), tuple(row.material_uses))


def frozen_row(row: QuerySnapshotRow) -> ReportRow:
    """冻结选择只消费快照创建时的管理身份（账户行为当时的全部系列）。"""
    return ReportRow.model_validate(
        {"row_key": row.row_key, "refs": row.refs, "material_uses": row.material_uses}
    )


def _summary(totals: SnapshotTotals, *, dimension: str) -> dict[str, Any]:
    """汇总按币种分桶，与行内合并共用部分合计规则（数据库内按成员计算）。

    混口径/混币种桶仍按桶展示，不偷偷换汇。缺覆盖的成员不当成 0：已同步部分照常
    相加并标 PARTIAL，同时给出不完整账户数与缺失日期，由页面提示"N 个账户数据
    不完整"。
    """
    unsupported = MATERIAL_UNSUPPORTED if dimension == "material" else frozenset()
    buckets = []
    for bucket in totals.buckets:
        values = {
            name: Decimal(bucket[name]) if bucket.get(name) is not None else None
            for name in METRIC_COLUMNS
            if name not in unsupported
        }
        vector = metric_vector(
            values,
            currency=bucket["currency"],
            timezone=bucket["tz"],
            unsupported=unsupported,
            partial={name for name in values if bucket.get(f"{name}_partial")},
        )
        buckets.append(vector.model_dump(mode="json"))
    return {
        "row_count": totals.total,
        "buckets": buckets,
        "incomplete_advertiser_count": len(totals.incomplete_advertisers),
        "incomplete_dates": list(totals.incomplete_dates),
    }


def _snapshot_public(snapshot: QuerySnapshot) -> QuerySnapshotPublic:
    return QuerySnapshotPublic(
        snapshot_id=snapshot.id,
        snapshot_at=snapshot.created_at,
        expires_at=snapshot.expires_at,
        filters=ReportingFilter.model_validate(snapshot.filters),
        publication_versions=snapshot.publication_versions,
    )


def _check_cursor(
    *,
    cursor: str,
    snapshot: QuerySnapshot,
    filters: ReportingFilter,
    context: TenantContext,
    bc_id: str,
) -> int:
    payload = _decode_cursor(cursor)
    expected = {
        "snapshot_id": str(snapshot.id),
        "filter_digest": snapshot.filter_digest,
        "tenant_id": str(context.tenant_id),
        "actor_id": str(context.actor_id),
        "bc_id": bc_id,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise HTTPException(404, detail="query_cursor_not_found")
    if snapshot.filter_digest != filter_digest(filters):
        raise HTTPException(409, detail="query_cursor_filter_mismatch")
    sequence = payload.get("sequence")
    if type(sequence) is not int or sequence < 0:
        raise HTTPException(404, detail="query_cursor_not_found")
    return sequence


@query_diagnostics.timed("snapshot")
def _build_snapshot(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    filters: ReportingFilter,
) -> QuerySnapshot:
    """在数据库内一次算出并写入快照成员与顺序，再落汇总；行内容不入快照。"""
    grants = authorized_grants(
        session, context=context, bc_id=bc_id, advertiser_ids=filters.advertiser_ids
    )
    if filters.advertiser_ids and {item.advertiser_id for item in grants} != set(
        filters.advertiser_ids
    ):
        raise HTTPException(403, detail="report_scope_forbidden")
    allowed_ids = sorted({item.advertiser_id for item in grants})
    query_diagnostics.count("account_count", len(allowed_ids))
    now = datetime.now(UTC)
    publication_versions = {"reporting": 1}
    updated = session.exec(
        select(func.max(ReportSyncState.last_success_at)).where(
            ReportSyncState.tenant_id == context.tenant_id,
            col(ReportSyncState.advertiser_id).in_(allowed_ids),
            ReportSyncState.stat_date >= filters.start_date,
            ReportSyncState.stat_date <= filters.end_date,
        )
    ).one()
    if updated is not None:
        publication_versions["max"] = int(updated.timestamp() * 1_000_000)
    snapshot = QuerySnapshot(
        id=uuid4(),
        tenant_id=context.tenant_id,
        bc_id=bc_id,
        actor_id=context.actor_id,
        advertiser_ids=allowed_ids,
        filters=filters.model_dump(mode="json"),
        filter_digest=filter_digest(filters),
        publication_versions=publication_versions,
        naming_versions={},
        expires_at=now + timedelta(minutes=15),
        total=0,
        summary={},
        coverage={},
    )
    # 趋势已从报表页移除。快照只保存成员、汇总与覆盖。
    snapshot.trends = {}
    session.add(snapshot)
    session.flush()
    totals = insert_snapshot_rows(
        session,
        snapshot_id=snapshot.id,
        tenant_id=context.tenant_id,
        advertisers=allowed_ids,
        bc_id=bc_id,
        filters=filters,
    )
    snapshot.naming_versions = snapshot_naming_versions(
        session, tenant_id=context.tenant_id, campaigns=totals.naming_campaigns
    )
    snapshot.total = totals.total
    snapshot.summary = _summary(totals, dimension=filters.dimension)
    snapshot.coverage = {
        "status": "EMPTY"
        if not totals.total
        else ("COMPLETE" if totals.complete else "INCOMPLETE"),
        "rows": totals.total,
    }
    if filters.dimension == "material":
        _, states = projected_states(
            session,
            tenant_id=context.tenant_id,
            advertisers=allowed_ids,
            start=filters.start_date,
            end=filters.end_date,
        )
        projected = projection_coverage(states)
        snapshot.coverage = {
            **snapshot.coverage,
            "projection": projected,
            "status": "BUILDING"
            if projected["unavailable_bucket_count"]
            else snapshot.coverage["status"],
        }
    session.add(snapshot)
    session.flush()
    return snapshot


@query_diagnostics.timed("page")
def _read_page(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    filters: ReportingFilter,
    snapshot: QuerySnapshot,
    cursor: str | None,
    limit: int,
) -> AdsQueryPage:
    after = (
        -1
        if cursor is None
        else _check_cursor(
            cursor=cursor,
            snapshot=snapshot,
            filters=filters,
            context=context,
            bc_id=bc_id,
        )
    )
    rows = read_snapshot_rows(
        session,
        context=context,
        bc_id=bc_id,
        snapshot_id=snapshot.id,
        after_sequence=after,
        limit=limit,
    )
    items = materialize_rows(
        session,
        tenant_id=context.tenant_id,
        filters=filters,
        members=[frozen_member(row) for row in rows],
        advertisers=snapshot.advertiser_ids,
    )
    if filters.dimension == "account":
        # 账户行保留其下全部系列引用供冻结选择展开；列表响应不返回，避免每行带回上百条。
        items = tuple(item.model_copy(update={"refs": ()}) for item in items)
    next_cursor = None
    if rows and rows[-1].stable_sequence + 1 < snapshot.total:
        next_cursor = _cursor(
            {
                "snapshot_id": str(snapshot.id),
                "filter_digest": snapshot.filter_digest,
                "tenant_id": str(context.tenant_id),
                "actor_id": str(context.actor_id),
                "bc_id": bc_id,
                "sequence": rows[-1].stable_sequence,
            }
        )
    metrics_coverage = {}
    if filters.dimension == "material":
        _, states = projected_states(
            session,
            tenant_id=context.tenant_id,
            advertisers=snapshot.advertiser_ids,
            start=filters.start_date,
            end=filters.end_date,
        )
        metrics_coverage = projection_coverage(states)
        raw_observed = metrics_coverage["metrics_observed_at"]
        observed = datetime.fromisoformat(raw_observed) if raw_observed else None
    else:
        observed = session.exec(
            select(
                func.max(
                    func.coalesce(
                        ReportSyncState.observed_at, ReportSyncState.last_success_at
                    )
                )
            ).where(
                ReportSyncState.tenant_id == context.tenant_id,
                col(ReportSyncState.advertiser_id).in_(snapshot.advertiser_ids),
                ReportSyncState.report_kind == "ad_day",
                ReportSyncState.stat_date >= filters.start_date,
                ReportSyncState.stat_date <= filters.end_date,
            )
        ).one()
    return AdsQueryPage(
        snapshot=_snapshot_public(snapshot),
        items=items,
        total=snapshot.total,
        summary=snapshot.summary,
        coverage=snapshot.coverage,
        next_cursor=next_cursor,
        metrics_observed_at=observed,
        metrics_coverage=metrics_coverage,
    )


@query_diagnostics.trace_query
def query_ads(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    filters: ReportingFilter,
    snapshot_id: UUID | None = None,
    cursor: str | None = None,
    limit: int = 50,
    cancellation: QueryCancellation | None = None,
) -> AdsQueryPage:
    """查询本地快照，分页游标始终绑定租户、BC、用户和筛选摘要。"""
    if not 1 <= limit <= 100:
        raise HTTPException(422, detail="invalid_snapshot_page")
    try:
        compile_filter(filters)
    except ValueError as exc:
        raise HTTPException(422, detail="invalid_reporting_filter") from exc
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    if snapshot_id is None:
        if cursor is not None:
            raise HTTPException(404, detail="query_cursor_not_found")
        # 先验证 BC 存在且当前操作者至少有一个可读账户，再创建快照。
        # 否则未知/未授权 BC 会先写入带无效外键的空快照，造成 500，
        # 同时把 BC 是否存在泄露给调用方。对外统一隐藏为 404。
        bc = session.get(TenantBC, (context.tenant_id, bc_id), populate_existing=True)
        if bc is None or bc.ownership_conflict:
            raise HTTPException(404, detail="query_scope_not_found")
        grants = authorized_grants(
            session, context=context, bc_id=bc_id, advertiser_ids=filters.advertiser_ids
        )
        if not grants:
            raise HTTPException(404, detail="query_scope_not_found")
        if session.get_bind() is engine:
            # API查询会话到此只有授权读取；等待共享构建前释放其连接，避免20个
            # 等待者占满连接池，反过来阻止唯一构建者取得RR连接。
            if session.new or session.dirty or session.deleted:
                raise RuntimeError(
                    "snapshot query requires a read-only request session"
                )
            session.rollback()
            if cancellation:
                cancellation.key = query_key(context, bc_id, filters)
            snapshot = find_or_build_snapshot(
                engine,
                context=context,
                bc_id=bc_id,
                filters=filters,
                builder=_build_snapshot,
                cancelled=cancellation.event.is_set if cancellation else lambda: False,
                waiter_id=cancellation.waiter_id if cancellation else None,
            )
        else:
            with cancellation.private_query(session) if cancellation else nullcontext():
                snapshot = reuse_in_transaction(
                    session,
                    context=context,
                    bc_id=bc_id,
                    filters=filters,
                    builder=_build_snapshot,
                )
        snapshot = read_snapshot(
            session, context=context, bc_id=bc_id, snapshot_id=snapshot.id
        )
    else:
        snapshot = read_snapshot(
            session, context=context, bc_id=bc_id, snapshot_id=snapshot_id
        )
        if snapshot.actor_id != context.actor_id:
            raise HTTPException(404, detail="query_snapshot_not_found")
        if snapshot.filter_digest != filter_digest(filters):
            raise HTTPException(409, detail="query_snapshot_filter_mismatch")
    with cancellation.private_query(session) if cancellation else nullcontext():
        return _read_page(
            session,
            context=context,
            bc_id=bc_id,
            filters=filters,
            snapshot=snapshot,
            cursor=cursor,
            limit=limit,
        )


def snapshot_naming_versions(
    session: Session, *, tenant_id: UUID, campaigns: tuple[tuple[str, str], ...]
) -> dict[str, int]:
    """记录快照内每个系列当时的最新名称修订，供后续选择/导出校验改名。

    大租户的历史系列可达数万个，逐项绑定会超过 PostgreSQL 65535 个参数上限，
    使整个报表查询失败。因此账户与系列各以一个数组参数传入，按精确
    (账户, 系列) 对关联，并在库内按 grouping/name 修订及观测时间取最新一条，
    排序规则与 latest_projections 一致。
    """
    naming_versions = {"directory": 1}
    if not campaigns:
        return naming_versions
    rows = session.connection().execute(
        text(
            """
            SELECT DISTINCT ON (p.advertiser_id, p.campaign_remote_id)
                p.advertiser_id, p.campaign_remote_id, p.name_revision
            FROM unnest(CAST(:advertisers AS text[]), CAST(:campaigns AS text[]))
                AS k(advertiser_id, campaign_remote_id)
            JOIN campaign_name_current p
              ON p.tenant_id = :tenant_id
             AND p.advertiser_id = k.advertiser_id
             AND p.campaign_remote_id = k.campaign_remote_id
            ORDER BY p.advertiser_id, p.campaign_remote_id,
                p.grouping_revision DESC, p.name_revision DESC, p.observed_at DESC
            """
        ),
        {
            "tenant_id": tenant_id,
            "advertisers": [advertiser for advertiser, _ in campaigns],
            "campaigns": [campaign for _, campaign in campaigns],
        },
    )
    for advertiser, campaign, revision in rows:
        naming_versions[f"{advertiser}:{campaign}"] = revision
    return naming_versions
