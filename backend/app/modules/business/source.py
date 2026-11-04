"""系列日来源与覆盖快照，不读取广告或素材明细。"""

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import and_, tuple_
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.modules.accounts.models import AdvertiserAccount
from app.modules.builds.execution_models import ExecutionStep
from app.modules.builds.preview_models import BuildUnit
from app.modules.business.configuration import scope_accounts
from app.modules.business.identity import catalog_snapshot
from app.modules.business.models import BusinessMapping, BusinessMember, BusinessScope
from app.modules.providers.models import ProviderConnection, ProviderDrama
from app.modules.reporting.store_models import ReportSyncState


def source_snapshot(
    session: Session,
    scope: BusinessScope,
    start: date,
    end: date,
    now: datetime | None = None,
) -> tuple[dict[str, Any], str]:
    now = now or datetime.now(UTC)
    context = TenantContext(
        tenant_id=scope.tenant_id, actor_id=scope.actor_id, role="operator"
    )
    allowed = scope_accounts(session, context, scope.bc_id)
    members = session.exec(
        select(BusinessMember)
        .where(
            col(BusinessMember.tenant_id) == scope.tenant_id,
            col(BusinessMember.bc_id) == scope.bc_id,
            col(BusinessMember.start_date) <= end,
        )
        .order_by(col(BusinessMember.advertiser_id))
    ).all()
    relevant = [m for m in members if m.end_date is None or m.end_date >= start]
    readable = [m.advertiser_id for m in relevant if m.advertiser_id in allowed]
    accounts = (
        session.exec(
            select(AdvertiserAccount)
            .where(
                col(AdvertiserAccount.tenant_id) == scope.tenant_id,
                col(AdvertiserAccount.advertiser_id).in_(readable),
            )
            .order_by(col(AdvertiserAccount.advertiser_id))
        ).all()
        if readable
        else []
    )
    member_map = {m.advertiser_id: m for m in relevant}
    states = (
        session.exec(
            select(ReportSyncState)
            .where(
                col(ReportSyncState.tenant_id) == scope.tenant_id,
                col(ReportSyncState.advertiser_id).in_(readable),
                col(ReportSyncState.report_kind) == "ad_day",
                col(ReportSyncState.stat_date) >= start,
                col(ReportSyncState.stat_date) <= end,
            )
            .order_by(
                col(ReportSyncState.advertiser_id), col(ReportSyncState.stat_date)
            )
        ).all()
        if readable
        else []
    )
    timezones = {a.advertiser_id: ZoneInfo(a.timezone) for a in accounts}
    snapshot: dict[str, Any] = {
        "accounts": {
            a.advertiser_id: {
                "currency": a.currency,
                "timezone": a.timezone,
                "complete_through": min(
                    end, now.astimezone(ZoneInfo(a.timezone)).date() - timedelta(days=1)
                ).isoformat(),
                "start": max(
                    member_map[a.advertiser_id].start_date,
                    a.remote_created_at.astimezone(ZoneInfo(a.timezone)).date()
                    if a.remote_created_at
                    else member_map[a.advertiser_id].start_date,
                ).isoformat(),
                "end": str(member_map[a.advertiser_id].end_date)
                if member_map[a.advertiser_id].end_date
                else None,
            }
            for a in accounts
        },
        "states": {
            f"{s.advertiser_id}/{s.stat_date.isoformat()}": {
                "status": s.status,
                "version": s.publication_version,
                "updated": s.updated_at.isoformat(),
                "count": s.row_count,
                "observed": (
                    s.observed_at or s.last_success_at or s.updated_at
                ).isoformat(),
            }
            for s in states
            if s.advertiser_id in member_map
            and max(start, member_map[s.advertiser_id].start_date) <= s.stat_date
            and s.stat_date
            <= min(
                end,
                member_map[s.advertiser_id].end_date or end,
                now.astimezone(timezones[s.advertiser_id]).date() - timedelta(days=1),
            )
        },
        "policy_restricted": len(readable) != len(relevant),
        "revision": scope.revision,
        "policy": "2026-Q3-Q4-user-confirmed-20261104",
        "calculation_revision": 2,
        "identity_catalog": catalog_snapshot(session, scope.tenant_id),
    }
    signature = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True).encode()
    ).hexdigest()
    return snapshot, signature


def mapping_catalog(
    session: Session,
    tenant_id: UUID,
    bc_id: str,
    pairs: list[tuple[str, str]] | None = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    statement = (
        select(BusinessMapping, ProviderDrama, ProviderConnection)
        .join(
            ProviderDrama,
            and_(
                col(BusinessMapping.tenant_id) == col(ProviderDrama.tenant_id),
                col(BusinessMapping.drama_id) == col(ProviderDrama.id),
            ),
        )
        .join(
            ProviderConnection,
            and_(
                col(ProviderDrama.tenant_id) == col(ProviderConnection.tenant_id),
                col(ProviderDrama.connection_id) == col(ProviderConnection.id),
            ),
        )
        .where(
            col(BusinessMapping.tenant_id) == tenant_id,
            col(BusinessMapping.bc_id) == bc_id,
        )
    )
    if pairs is not None:
        statement = statement.where(
            tuple_(
                col(BusinessMapping.advertiser_id), col(BusinessMapping.campaign_id)
            ).in_(pairs)
        )
    rows = session.exec(statement).all()
    return {
        (m.advertiser_id, m.campaign_id): {
            "drama_id": d.id,
            "drama_key": str(d.id),
            "drama_name": d.title,
            "provider_id": p.id,
            "provider_name": p.display_name,
        }
        for m, d, p in rows
    }


def discover_exact_mappings(
    session: Session, scope: BusinessScope, allowed: list[str]
) -> int:
    """只用已搭建的远端系列回执与冻结剧目ID，不用名称猜测财务归属。"""
    rows = (
        session.exec(
            select(
                col(ExecutionStep.remote_id),
                col(BuildUnit.advertiser_id),
                col(BuildUnit.drama_id),
            )
            .join(
                BuildUnit,
                and_(
                    col(ExecutionStep.tenant_id) == col(BuildUnit.tenant_id),
                    col(ExecutionStep.unit_id) == col(BuildUnit.id),
                ),
            )
            .where(
                col(ExecutionStep.tenant_id) == scope.tenant_id,
                col(ExecutionStep.bc_id) == scope.bc_id,
                col(ExecutionStep.kind) == "CAMPAIGN",
                col(ExecutionStep.remote_id).is_not(None),
                col(BuildUnit.advertiser_id).in_(allowed),
            )
        ).all()
        if allowed
        else []
    )
    candidates: dict[tuple[str, str], set[UUID]] = {}
    for remote_id, advertiser_id, drama_id in rows:
        if remote_id is None:
            continue
        candidates.setdefault((advertiser_id, remote_id), set()).add(drama_id)
    existing = set(mapping_catalog(session, scope.tenant_id, scope.bc_id))
    count = 0
    for (advertiser, campaign), dramas in candidates.items():
        if len(dramas) != 1 or (advertiser, campaign) in existing:
            continue
        session.add(
            BusinessMapping(
                tenant_id=scope.tenant_id,
                bc_id=scope.bc_id,
                advertiser_id=advertiser,
                campaign_id=campaign,
                drama_id=next(iter(dramas)),
                actor_id=scope.actor_id,
                evidence="build-campaign-receipt",
            )
        )
        count += 1
    if count:
        scope.revision += 1
        session.add(scope)
        session.flush()
    return count
