"""规范scope按账户和授权隔离；批次、lane和调度优先级不进入身份。"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session, col, select

from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.sync_control.contracts import ScopeKey
from app.modules.sync_control.models import SyncScope


def scope_key(
    route: dict[str, Any] | FrozenTikTokRoute,
    actor_id: UUID,
    advertiser_id: str,
    resource: str,
    normalized_query: dict[str, Any],
    metric_contract: str,
    *,
    timezone: str,
) -> ScopeKey:
    route = FrozenTikTokRoute.model_validate(route)
    if not advertiser_id or not resource or not timezone or not metric_contract:
        raise ValueError(
            "scope requires account, resource, timezone and metric contract"
        )
    actor_id = UUID(str(actor_id))
    query = {
        key: value
        for key, value in normalized_query.items()
        if key not in {"priority", "tier", "batch", "lane", "workload", "page"}
    }
    value = [
        route.model_dump(mode="json"),
        str(actor_id),
        advertiser_id,
        resource,
        query,
        metric_contract,
        timezone,
    ]
    digest = hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    return ScopeKey(digest, route.tenant_id, actor_id, advertiser_id, resource)


def ensure_scope(
    session: Session,
    *,
    route: dict[str, Any] | FrozenTikTokRoute,
    actor_id: UUID,
    advertiser_id: str,
    resource: str,
    query: dict[str, Any],
    timezone: str,
    metric_contract: str = "report-v2",
) -> SyncScope:
    frozen = FrozenTikTokRoute.model_validate(route)
    key = scope_key(
        frozen,
        actor_id,
        advertiser_id,
        resource,
        query,
        metric_contract,
        timezone=timezone,
    )
    row = SyncScope(
        tenant_id=key.tenant_id,
        actor_id=key.actor_id,
        advertiser_id=advertiser_id,
        bc_id=frozen.bc_id,
        connection_id=frozen.connection_id,
        channel=frozen.channel,
        frozen_route=frozen.model_dump(mode="json"),
        resource=resource,
        timezone=timezone,
        identity_digest=key.digest,
        query_contract={"query": query, "metrics": metric_contract},
    )
    statement = (
        insert(SyncScope)
        .values(**row.model_dump())
        .on_conflict_do_nothing(index_elements=["identity_digest"])
    )
    session.connection().execute(statement)
    return session.exec(
        select(SyncScope).where(col(SyncScope.identity_digest) == key.digest)
    ).one()
