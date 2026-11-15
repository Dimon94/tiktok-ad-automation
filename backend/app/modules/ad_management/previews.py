"""广告管理 5 分钟不可变预览。

预览保存 B 选择的成员摘要、引用、目录配置与冻结路由。后续任务只消费这些
副本，即使 B 的短期快照被清理，也不会重新筛选、换连接或吸收新广告。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from sqlmodel import Session, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.accounts.routing import freeze_route
from app.modules.ad_management.expansion import (
    ExpandedItem,
    _expand,
    _ref_dict,
    _use_dict,
)
from app.modules.ad_management.models import ManagementPreview, ManagementPreviewItem
from app.modules.ad_management.schemas import (
    ManagementCounts,
    ManagementItemPublic,
    ManagementPreviewPublic,
    MutationSpec,
)
from app.modules.reporting.query_models import FrozenSelectionRecord
from app.modules.reporting.selection import get_frozen_selection
from app.modules.tenants.permissions import require_tenant


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    return value


def _digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        _canonical(payload), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _stored_value(value: Decimal | str | None) -> str | None:
    if value is None:
        return None
    return str(value)


def _public_item(item: ExpandedItem) -> ManagementItemPublic:
    return ManagementItemPublic(
        budget_facts=item.capability.get("budget_facts"),
        ref=item.ref,
        name=item.name,
        material_use=item.material_use,
        # 数值动作公开原数值，改名动作公开原系列名；状态快照只保留在
        # 能力/配置证据中，公开 DTO 用 final_value/reason 表达。
        original_value=(
            item.original_value
            if isinstance(item.original_value, Decimal)
            or item.capability.get("operation") == "rename"
            else None
        ),
        final_value=item.final_value,
        reason=item.reason,
        execution_result=cast(Any, item.execution_result),
        drama_name=item.drama_name,
        provider_label=item.provider_label,
        drama_changed=item.drama_changed,
    )


def _selection_record(
    session: Session, context: TenantContext, selection_id: UUID, bc_id: str
) -> tuple[FrozenSelectionRecord, Any]:
    record = session.exec(
        select(FrozenSelectionRecord).where(
            FrozenSelectionRecord.id == selection_id,
            FrozenSelectionRecord.tenant_id == context.tenant_id,
            FrozenSelectionRecord.actor_id == context.actor_id,
            FrozenSelectionRecord.bc_id == bc_id,
        )
    ).first()
    if record is None:
        raise DomainError("read_bc_mismatch", "冻结选择不属于当前租户或 BC")
    # This rechecks current read access and the B 15-minute expiry immediately
    # before expansion; selection records are immutable after creation.
    selection = get_frozen_selection(
        session, context=context, bc_id=bc_id, selection_id=selection_id
    )
    return record, selection


def prepare_preview(
    session: Session,
    context: TenantContext,
    bc_id: str,
    selection_id: UUID,
    mutation: MutationSpec,
) -> ManagementPreviewPublic:
    """Freeze and persist one bounded management impact preview."""
    require_tenant(
        session,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        action="ads_manage",
    )
    record, selection = _selection_record(session, context, selection_id, bc_id)
    try:
        route = freeze_route(session, context=context, bc_id=bc_id)
    except DomainError as exc:
        # 未指定连接时冻结的是 BC 默认执行连接；它停用时提示去切换默认连接，
        # 不自动改用同 BC 的其他连接。其他调用方仍沿用原错误码判断重试。
        if exc.code == "connection_unavailable":
            raise DomainError(
                "bc_default_connection_unavailable", "该 BC 的默认执行连接已停用"
            ) from exc
        raise
    expansion = _expand(
        session,
        context,
        bc_id=bc_id,
        selection_id=selection_id,
        mutation=mutation,
        require_capabilities=True,
        route=route,
    )
    created_at = datetime.now(UTC)
    expires_at = created_at + timedelta(seconds=300)
    counts = ManagementCounts(
        selected=expansion.selected_count,
        targets=len(expansion.commands),
        linked=sum(1 for item in expansion.items if item.linked),
        unsupported=sum(
            1 for item in expansion.items if item.execution_result == "UNSUPPORTED"
        ),
    )
    # Store the complete B evidence inside the preview mutation JSON. This keeps
    # submitted tasks independent from frozen-selection cleanup and future names.
    frozen_evidence = {
        "selection_id": str(selection.selection_id),
        "snapshot_id": str(selection.snapshot_id),
        "membership_digest": selection.membership_digest,
        "refs": [_ref_dict(ref) for ref in selection.refs],
        "material_uses": [_use_dict(use) for use in selection.material_uses],
        "advertiser_ids": list(record.advertiser_ids),
        "filters": record.filters,
        "filter_digest": record.filter_digest,
        "publication_versions": record.publication_versions,
        "naming_versions": record.naming_versions,
        "selection_expires_at": record.expires_at.isoformat(),
    }
    mutation_payload = mutation.model_dump(mode="json")
    mutation_payload["frozen_selection"] = frozen_evidence
    payload_items = [
        {
            "ref": _ref_dict(item.ref),
            "material_use": _use_dict(item.material_use) if item.material_use else None,
            "original_value": item.original_value,
            "final_value": item.final_value,
            "reason": item.reason,
            "execution_result": item.execution_result,
            "parent_ref": _ref_dict(item.parent_ref) if item.parent_ref else None,
            "grouping_revision": item.grouping_revision,
            "capability": item.capability,
            "linked": item.linked,
        }
        for item in expansion.items
    ]
    digest = _digest(
        {
            "bc_id": bc_id,
            "route": route.model_dump(mode="json"),
            "mutation": mutation_payload,
            "items": payload_items,
            "counts": counts.model_dump(mode="json"),
            "created_at": created_at,
            "expires_at": expires_at,
        }
    )
    preview = ManagementPreview(
        tenant_id=context.tenant_id,
        bc_id=bc_id,
        actor_id=context.actor_id,
        selection_id=selection_id,
        digest=digest,
        mutation=mutation_payload,
        route=route.model_dump(mode="json"),
        created_at=created_at,
        expires_at=expires_at,
        status="READY",
        counts=counts.model_dump(mode="json"),
    )
    session.add(preview)
    session.flush()
    for position, item in enumerate(expansion.items):
        session.add(
            ManagementPreviewItem(
                tenant_id=context.tenant_id,
                preview_id=preview.id,
                position=position,
                ref=_ref_dict(item.ref),
                material_use=_use_dict(item.material_use)
                if item.material_use
                else None,
                parent_ref=_ref_dict(item.parent_ref) if item.parent_ref else None,
                grouping_revision=item.grouping_revision,
                membership_digest=selection.membership_digest,
                capability={**item.capability, "route": route.model_dump(mode="json")},
                original_value=_stored_value(item.original_value),
                final_value=_stored_value(item.final_value),
                reason=item.reason,
                execution_result=item.execution_result,
            )
        )
    session.flush()
    return ManagementPreviewPublic(
        preview_id=preview.id,
        digest=digest,
        created_at=created_at,
        expires_at=expires_at,
        bc_id=bc_id,
        route=route,
        items=tuple(_public_item(item) for item in expansion.items),
        counts=counts,
    )


__all__ = ["prepare_preview"]
