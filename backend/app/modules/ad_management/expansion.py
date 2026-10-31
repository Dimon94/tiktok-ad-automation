"""从 B 的冻结选择展开广告管理影响范围。

C3 只消费冻结选择中的身份集合，然后读取这些身份当前已发布的目录行。
这里不重新执行报表筛选，因此冻结后新出现的广告不会进入预览；父级和预算
拥有者只在它们是已冻结对象的关系或显式 ``include_parents`` 时进入写入范围。
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast
from uuid import UUID

from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import EntityRef, MaterialUseRef
from app.integrations.tiktok.contracts.management import (
    ManagementCommand,
    ManagementField,
    operation_for_field,
)
from app.modules.accounts.connection_models import ConnectionAuthorization
from app.modules.accounts.management_rules import (
    MANAGEMENT_ENTITY_KINDS,
    management_allowed,
)
from app.modules.accounts.models import BCAccountAccess
from app.modules.ad_management.schemas import MutationSpec
from app.modules.ads.directory import locate, locate_material_use
from app.modules.ads.models import AdObject
from app.modules.reporting.query_models import FrozenSelectionRecord
from app.modules.reporting.selection import get_frozen_selection


@dataclass(frozen=True)
class ExpandedItem:
    """预览使用的完整影响项；``command`` 为 None 表示不可操作或仅联动。"""

    ref: EntityRef
    material_use: MaterialUseRef | None
    original_value: Decimal | str | None
    final_value: Decimal | str | None
    reason: str | None
    execution_result: str
    command: ManagementCommand | None
    parent_ref: EntityRef | None
    grouping_revision: int
    capability: dict[str, Any]
    linked: bool = False
    # 目录中的对象名称，仅供预览展示，让用户看清“改的是哪一个”。
    name: str | None = None
    # 改名项专用：新名称解析出的渠道与剧名，以及剧归类是否因改名而变化。
    # 报表按系列名前两段归剧，改动剧名会让历史数据换组，界面需要据此提醒。
    drama_name: str | None = None
    provider_label: str | None = None
    drama_changed: bool | None = None


@dataclass(frozen=True)
class Expansion:
    selection_refs: tuple[EntityRef, ...]
    selection_material_uses: tuple[MaterialUseRef, ...]
    items: tuple[ExpandedItem, ...]
    commands: tuple[ManagementCommand, ...]
    selected_count_override: int | None = None

    @property
    def selected_count(self) -> int:
        if self.selected_count_override is not None:
            return self.selected_count_override
        return len(self.selection_refs)


def _ref_key(ref: EntityRef) -> tuple[UUID, str, str, str]:
    return ref.tenant_id, ref.advertiser_id, ref.kind, ref.remote_id


def _use_key(use: MaterialUseRef) -> tuple[Any, ...]:
    return (
        *_ref_key(use.ad_ref),
        use.platform_material_id,
        use.ad_material_id,
        use.material_type,
    )


def _ref_dict(ref: EntityRef) -> dict[str, str]:
    return {
        "tenant_id": str(ref.tenant_id),
        "advertiser_id": ref.advertiser_id,
        "kind": ref.kind,
        "remote_id": ref.remote_id,
    }


def _use_dict(use: MaterialUseRef) -> dict[str, Any]:
    return {
        "ad_ref": _ref_dict(use.ad_ref),
        "platform_material_id": use.platform_material_id,
        "ad_material_id": use.ad_material_id,
        "material_type": use.material_type,
    }


def _number(value: Any, *, field: str, ref: EntityRef) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DomainError(
            "management_operation_invalid",
            f"{field} 当前值无效: {ref.kind}/{ref.remote_id}",
        ) from exc
    if not result.is_finite() or result <= 0:
        raise DomainError(
            "management_operation_invalid",
            f"{field} 当前值必须为正数: {ref.kind}/{ref.remote_id}",
        )
    return result


def _final_value(current: Decimal, mutation: MutationSpec) -> Decimal | str:
    if mutation.field == "status" or mutation.field == "material_status":
        assert isinstance(mutation.value, str)
        return mutation.value
    assert isinstance(mutation.value, Decimal)
    if mutation.mode == "set":
        result = mutation.value
    elif mutation.mode == "add":
        result = current + mutation.value
    elif mutation.mode == "subtract":
        result = current - mutation.value
    elif mutation.mode == "increase_percent":
        result = current * (Decimal("1") + mutation.value / Decimal("100"))
    else:
        result = current * (Decimal("1") - mutation.value / Decimal("100"))
    # 钳制不能反转方向：已高于上限的「提高」、已低于下限的「降低」保持当前值。
    if mutation.max_value is not None:
        result = max(current, min(result, mutation.max_value))
    if mutation.min_value is not None:
        result = min(current, max(result, mutation.min_value))
    if not result.is_finite() or result <= 0:
        raise DomainError("management_operation_invalid", "管理后的数值必须为正数")
    return result


def _parent_ref(row: AdObject) -> EntityRef | None:
    return row.parent_ref


def _object_for_ref(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    ref: EntityRef,
    route: Any,
) -> AdObject | None:
    try:
        cache = session.info.get("management_expansion_cache")
        if cache is not None:
            row = cache["objects"].get(_ref_key(ref))
            if row is None:
                return None
        else:
            row = locate(session, context=context, bc_id=bc_id, ref=ref)
        if (
            route is None
            or row.source_connection_id != route.connection_id
            or row.source_channel != route.channel
        ):
            if cache is not None:
                return None
            raise DomainError(
                "management_source_conflict",
                "目录对象来源与冻结 BC/连接不一致",
            )
        return row
    except DomainError as exc:
        if exc.code in {"ad_object_not_found", "directory_forbidden"}:
            return None
        raise


def _management_allowed(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    advertiser_id: str,
    route: Any,
    operation: str,
    entity_kind: str,
) -> bool:
    """预览时按与提交门禁同一规则（management_rules.management_allowed）判断能否管理。"""
    if route is None or entity_kind not in MANAGEMENT_ENTITY_KINDS.get(operation, ()):
        return False
    cache = session.info.get("management_expansion_cache")
    if cache is not None:
        grant = cache["grants"].get(advertiser_id)
        authorization = cache["authorization"]
        return bool(
            grant
            and authorization
            and grant.permission_state == "VERIFIED"
            and management_allowed(grant, authorization.scopes, operation)
        )
    grant = session.get(
        BCAccountAccess, (context.tenant_id, bc_id, advertiser_id, route.connection_id)
    )
    authorization = session.exec(
        select(ConnectionAuthorization).where(
            ConnectionAuthorization.tenant_id == context.tenant_id,
            ConnectionAuthorization.connection_id == route.connection_id,
            ConnectionAuthorization.authorization_revision
            == route.authorization_revision,
        )
    ).one_or_none()
    return (
        grant is not None
        and authorization is not None
        and grant.permission_state == "VERIFIED"
        and management_allowed(grant, authorization.scopes, operation)
    )


def _child_adgroups(
    session: Session,
    *,
    context: TenantContext,
    campaign: AdObject,
    route: Any,
    observed_before: datetime,
) -> list[AdObject]:
    """读取系列下在冻结选择之前已发布的广告组。

    目标 ROAS 同步到整个系列、组预算系列的预算落在各广告组，都需要从系列
    找到它的广告组；只取冻结时刻之前观察到的行，冻结后新建的组不会被吸收。
    """
    cache = session.info.get("management_expansion_cache")
    if cache is not None:
        return cache["children"].get((campaign.advertiser_id, campaign.remote_id), [])
    return list(
        session.exec(
            select(AdObject)
            .where(
                AdObject.tenant_id == context.tenant_id,
                AdObject.advertiser_id == campaign.advertiser_id,
                AdObject.kind == "adgroup",
                AdObject.parent_kind == "campaign",
                AdObject.parent_remote_id == campaign.remote_id,
                AdObject.source_connection_id == route.connection_id,
                AdObject.source_channel == route.channel,
                AdObject.observed_at <= observed_before,
            )
            .execution_options(populate_existing=True)
        ).all()
    )


def _campaign_owns_budget(campaign: AdObject) -> bool:
    """判断预算是否由系列持有。

    TikTok 的两种预算策略：开启系列预算优化（CBO，``budget_optimize_on=true``）
    时钱在系列上；关闭时系列预算为 0、``budget_mode`` 为不限，钱在各广告组上。
    平台没返回开关时，以系列预算模式是否为“不限”判断。
    """
    config = campaign.configuration or {}
    optimize = config.get("budget_optimize_on")
    if isinstance(optimize, bool):
        return optimize
    return str(config.get("budget_mode") or "").upper() != "BUDGET_MODE_INFINITE"


def _budget_owners(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    row: AdObject,
    objects: dict[tuple[UUID, str, str, str], AdObject],
    route: Any,
    observed_before: datetime,
) -> list[AdObject]:
    """返回所选对象的预算实际持有者。

    - 选中系列：系列预算策略改系列；组预算策略改它的全部广告组。
    - 选中广告组或广告：所属系列持有预算时改系列，否则改该广告组。
    """
    if row.kind == "campaign":
        if _campaign_owns_budget(row):
            return [row]
        groups = _child_adgroups(
            session,
            context=context,
            campaign=row,
            route=route,
            observed_before=observed_before,
        )
        for group in groups:
            objects[_ref_key(group.ref)] = group
        return groups
    group: AdObject | None = row if row.kind == "adgroup" else None
    if row.kind == "ad" and row.parent_ref is not None:
        group = objects.get(_ref_key(row.parent_ref))
        if group is None:
            group = _object_for_ref(
                session, context=context, bc_id=bc_id, ref=row.parent_ref, route=route
            )
            if group is not None:
                objects[_ref_key(group.ref)] = group
    if group is None or group.kind != "adgroup":
        return []
    campaign = _campaign_ancestor(
        session,
        context=context,
        bc_id=bc_id,
        row=group,
        objects=objects,
        route=route,
    )
    if campaign is not None and _campaign_owns_budget(campaign):
        return [campaign]
    return [group]


def _material(session, use):
    cache = session.info.get("management_expansion_cache")
    return (
        cache["materials"].get(_use_key(use))
        if cache is not None
        else locate_material_use(session, use)
    )


def _current_value(
    session: Session,
    row: AdObject,
    use: MaterialUseRef | None,
    field: str,
) -> Decimal | str | None:
    if use is not None and field in {"status", "material_status"}:
        material = _material(session, use)
        return (
            material.operation_status if material is not None else row.operation_status
        )
    if field == "status":
        return row.operation_status
    key = "roas_bid" if field == "roas" else "budget"
    return (row.configuration or {}).get(key)


def _grouping_revision(session: Session, ref: EntityRef) -> int:
    # Name parsing is an audit coordinate only. Invalid names remain manageable
    # by identity and simply use revision 0.
    from app.modules.ads.models import CampaignNameProjection

    if ref.kind != "campaign":
        return 0
    cache = session.info.get("management_expansion_cache")
    if cache is not None:
        row = cache["projections"].get((ref.advertiser_id, ref.remote_id))
        return row.grouping_revision if row is not None else 0
    row = session.exec(
        select(CampaignNameProjection)
        .where(
            CampaignNameProjection.tenant_id == ref.tenant_id,
            CampaignNameProjection.advertiser_id == ref.advertiser_id,
            CampaignNameProjection.campaign_remote_id == ref.remote_id,
        )
        .order_by(col(CampaignNameProjection.name_revision).desc())
        .limit(1)
    ).first()
    return row.grouping_revision if row is not None else 0


def _campaign_ancestor(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    row: AdObject,
    objects: dict[tuple[UUID, str, str, str], AdObject],
    route: Any,
) -> AdObject | None:
    current = row
    seen: set[tuple[Any, ...]] = set()
    while current.kind != "campaign" and current.parent_ref is not None:
        key = _ref_key(current.ref)
        if key in seen:
            return None
        seen.add(key)
        parent = objects.get(_ref_key(current.parent_ref))
        if parent is None:
            parent = _object_for_ref(
                session,
                context=context,
                bc_id=bc_id,
                ref=current.parent_ref,
                route=route,
            )
            if parent is not None:
                objects[_ref_key(parent.ref)] = parent
        if parent is None:
            return None
        current = parent
    return current if current.kind == "campaign" else None


def _grouping_revision_for_row(
    session: Session,
    *,
    row: AdObject,
    objects: dict[tuple[UUID, str, str, str], AdObject],
    context: TenantContext,
    bc_id: str,
    route: Any,
    observed_before: datetime,
    allow_newer_parent: bool,
) -> int:
    campaign = row if row.kind == "campaign" else None
    current = row
    seen: set[tuple[Any, ...]] = set()
    while campaign is None and current.parent_ref is not None:
        key = _ref_key(current.ref)
        if key in seen:
            raise DomainError("grouping_revision_missing", "对象祖先链存在循环")
        seen.add(key)
        parent = objects.get(_ref_key(current.parent_ref))
        if parent is None:
            parent = _object_for_ref(
                session,
                context=context,
                bc_id=bc_id,
                ref=current.parent_ref,
                route=route,
            )
            # Ancestors of a frozen identity remain part of that identity's
            # evidence even when their latest directory observation is newer
            # than the B snapshot.  Newly discovered sibling rows stay behind
            # the frozen cutoff.
            if parent is not None and (
                allow_newer_parent or parent.observed_at <= observed_before
            ):
                objects[_ref_key(parent.ref)] = parent
            else:
                parent = None
        if parent is None:
            raise DomainError(
                "grouping_revision_missing",
                "对象的冻结祖先尚未在目录中确认",
            )
        current = parent
        if current.kind == "campaign":
            campaign = current
    if campaign is None:
        raise DomainError("grouping_revision_missing", "对象没有可确认的系列祖先")
    return _grouping_revision(session, campaign.ref)


def _is_ancestor(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    child: AdObject,
    ancestor: EntityRef,
    objects: dict[tuple[UUID, str, str, str], AdObject],
    route: Any,
) -> bool:
    current: AdObject | None = child
    seen: set[tuple[Any, ...]] = set()
    while current is not None and current.parent_ref is not None:
        key = _ref_key(current.ref)
        if key in seen:
            return False
        seen.add(key)
        parent_ref = current.parent_ref
        if _ref_key(parent_ref) == _ref_key(ancestor):
            return True
        current = objects.get(_ref_key(parent_ref))
        if current is None:
            current = _object_for_ref(
                session,
                context=context,
                bc_id=bc_id,
                ref=parent_ref,
                route=route,
            )
            if current is not None:
                objects[_ref_key(current.ref)] = current
    return False


def _series_key(
    session: Session,
    row: AdObject,
    objects: dict[tuple[UUID, str, str, str], AdObject],
    *,
    context: TenantContext,
    bc_id: str,
    route: Any,
) -> tuple[Any, ...]:
    """Return the frozen naming series coordinate, falling back to identity.

    A valid provider/drama projection coordinates a series across campaigns;
    invalid names still remain manageable by object identity.
    """
    campaign = row
    seen: set[tuple[Any, ...]] = set()
    while campaign.parent_ref is not None and campaign.kind != "campaign":
        key = _ref_key(campaign.ref)
        if key in seen:
            break
        seen.add(key)
        parent = objects.get(_ref_key(campaign.parent_ref))
        if parent is None:
            parent = _object_for_ref(
                session,
                context=context,
                bc_id=bc_id,
                ref=campaign.parent_ref,
                route=route,
            )
            if parent is not None:
                objects[_ref_key(parent.ref)] = parent
        if parent is None:
            break
        campaign = parent
    if campaign.kind != "campaign":
        return (row.advertiser_id, row.remote_id)
    from app.modules.ads.models import CampaignNameProjection

    cache = session.info.get("management_expansion_cache")
    projection = (
        cache["projections"].get((campaign.advertiser_id, campaign.remote_id))
        if cache is not None
        else session.exec(
            select(CampaignNameProjection)
            .where(
                CampaignNameProjection.tenant_id == campaign.tenant_id,
                CampaignNameProjection.advertiser_id == campaign.advertiser_id,
                CampaignNameProjection.campaign_remote_id == campaign.remote_id,
            )
            .order_by(col(CampaignNameProjection.name_revision).desc())
            .limit(1)
        ).first()
    )
    if projection is not None and projection.status == "VALID":
        return (
            campaign.advertiser_id,
            projection.provider_label,
            projection.drama_name,
        )
    return (campaign.advertiser_id, "campaign", campaign.remote_id)


def _command(
    row: AdObject,
    *,
    field: str,
    final: Decimal | str,
    use: MaterialUseRef | None,
) -> ManagementCommand:
    original: dict[str, Any] = {"ad_type": row.ad_type}
    original.update(deepcopy(row.configuration or {}))
    if field == "roas":
        desired = {"roas_bid": str(final)}
    elif field == "budget":
        desired = {"budget": str(final)}
    elif field == "material_status":
        desired = {"operation_status": final}
    else:
        desired = {"operation_status": final}
    return ManagementCommand(
        ref=row.ref,
        field=cast(ManagementField, field),
        original=original,
        desired=desired,
        ad_material_id=use.ad_material_id if use is not None else None,
    )


def _load_selection(session: Session, context: TenantContext, selection_id: UUID):
    record = session.exec(
        select(FrozenSelectionRecord).where(
            FrozenSelectionRecord.id == selection_id,
            FrozenSelectionRecord.tenant_id == context.tenant_id,
            FrozenSelectionRecord.actor_id == context.actor_id,
        )
    ).first()
    if record is None:
        from fastapi import HTTPException

        raise HTTPException(404, detail="frozen_selection_not_found")
    # get_frozen_selection rechecks current read grants, expiry, and reconstructs
    # exact refs/material identities from the persisted B record.
    return (
        record.bc_id,
        get_frozen_selection(
            session, context=context, bc_id=record.bc_id, selection_id=selection_id
        ),
        record,
    )


def _expand(
    session: Session,
    context: TenantContext,
    *,
    bc_id: str,
    selection_id: UUID,
    mutation: MutationSpec,
    require_capabilities: bool = False,
    route: Any = None,
    frozen_record: FrozenSelectionRecord | None = None,
) -> Expansion:
    if frozen_record is None:
        selected_bc, selection, selection_record = _load_selection(
            session, context, selection_id
        )
    else:
        from app.modules.reporting.selection import selection_public

        selected_bc, selection, selection_record = (
            frozen_record.bc_id,
            selection_public(frozen_record),
            frozen_record,
        )
    if selected_bc != bc_id:
        raise DomainError("read_bc_mismatch", "冻结选择不属于当前 BC")
    excluded_refs = {_ref_key(ref) for ref in mutation.excluded_refs}
    excluded_uses = {_use_key(item) for item in mutation.excluded_material_uses}
    excluded_material_ad_refs = {
        _ref_key(item.ad_ref) for item in mutation.excluded_material_uses
    }
    unique_refs: list[EntityRef] = []
    seen_refs: set[tuple[Any, ...]] = set()
    for ref in selection.refs:
        key = _ref_key(ref)
        if key not in excluded_refs and key not in seen_refs:
            seen_refs.add(key)
            unique_refs.append(ref)
    refs = tuple(unique_refs)
    unique_uses: list[MaterialUseRef] = []
    seen_uses: set[tuple[Any, ...]] = set()
    for use in selection.material_uses:
        key = _use_key(use)
        if (
            key not in excluded_uses
            and _ref_key(use.ad_ref) not in excluded_refs
            and key not in seen_uses
        ):
            seen_uses.add(key)
            unique_uses.append(use)
    uses = tuple(unique_uses)
    items: list[ExpandedItem] = []
    # Explicit exclusions are retained as visible, non-operable evidence.
    for ref in mutation.excluded_refs:
        items.append(
            ExpandedItem(
                ref,
                None,
                None,
                None,
                "excluded_ref",
                "UNSUPPORTED",
                None,
                None,
                0,
                {},
                False,
            )
        )
    for use in mutation.excluded_material_uses:
        items.append(
            ExpandedItem(
                use.ad_ref,
                use,
                None,
                None,
                "excluded_material_use",
                "UNSUPPORTED",
                None,
                None,
                0,
                {},
                False,
            )
        )

    if mutation.field == "name":
        # 改名逐条计算名称、只作用于推广系列，与数值类动作的父子联动无关，单独展开；
        # 显式排除项仍作为可见证据保留在结果最前面。
        from app.modules.ad_management.rename import expand_rename

        renamed = expand_rename(
            session,
            context,
            bc_id=bc_id,
            refs=refs,
            mutation=mutation,
            route=route,
            require_capabilities=require_capabilities,
        )
        return replace(renamed, items=(*items, *renamed.items))

    objects: dict[tuple[UUID, str, str, str], AdObject] = {}
    # 素材详情的冻结选择只有 material_uses；所属广告仅作为校验依据读取，
    # 不加入 refs，避免把素材启停扩大为整条广告启停。
    lookup_refs = {_ref_key(ref): ref for ref in (*refs, *(use.ad_ref for use in uses))}
    for ref in lookup_refs.values():
        row = _object_for_ref(
            session, context=context, bc_id=bc_id, ref=ref, route=route
        )
        if row is not None:
            objects[_ref_key(ref)] = row
    # Explicit parents must be a real ancestor of a selected object/material ad.
    selected_children = list(refs) + [use.ad_ref for use in uses]
    for ref in mutation.include_parents:
        if _ref_key(ref) in excluded_refs or _ref_key(ref) in excluded_material_ad_refs:
            reason = (
                "excluded_ref"
                if _ref_key(ref) in excluded_refs
                else "excluded_material_use"
            )
            items.append(
                ExpandedItem(
                    ref,
                    None,
                    None,
                    None,
                    reason,
                    "UNSUPPORTED",
                    None,
                    None,
                    0,
                    {},
                    False,
                )
            )
            continue
        if ref.tenant_id != context.tenant_id:
            raise DomainError("management_parent_invalid", "显式父级不属于当前租户")
        row = _object_for_ref(
            session, context=context, bc_id=bc_id, ref=ref, route=route
        )
        if row is None:
            items.append(
                ExpandedItem(
                    ref,
                    None,
                    None,
                    None,
                    "parent_not_found",
                    "UNSUPPORTED",
                    None,
                    None,
                    0,
                    {
                        "route": route.model_dump(mode="json")
                        if route is not None
                        else None,
                    },
                    False,
                )
            )
            continue
        valid = False
        for child_ref in selected_children:
            child = objects.get(_ref_key(child_ref))
            if child is None:
                child = _object_for_ref(
                    session, context=context, bc_id=bc_id, ref=child_ref, route=route
                )
                if child is not None:
                    objects[_ref_key(child.ref)] = child
            if child is not None:
                valid = _is_ancestor(
                    session,
                    context=context,
                    bc_id=bc_id,
                    child=child,
                    ancestor=ref,
                    objects=objects,
                    route=route,
                )
            if valid:
                break
        if not valid:
            raise DomainError(
                "management_parent_invalid", "显式父级不是所选对象的真实祖先"
            )
        if row is not None:
            objects[_ref_key(ref)] = row

    smart_types = {"SMART_PLUS", "UPGRADED_SMART_PLUS", "SMART+", "SMARTPLUS"}
    account_fallback = False
    # Account/drama rows may intentionally carry no synthetic EntityRef. In
    # that case expand the exact advertiser scope captured by B, using only
    # route-bound rows observed before the frozen selection.
    selection_dimension = str(
        (selection_record.filters or {}).get("dimension", "")
    ).lower()
    aggregate_scope = (
        selection_dimension in {"account", "drama"}
        and not selection.refs
        and not selection.material_uses
    )
    if not objects and not refs and not uses and aggregate_scope:
        account_fallback = True
        account_campaigns = session.exec(
            select(AdObject)
            .where(
                AdObject.tenant_id == context.tenant_id,
                col(AdObject.advertiser_id).in_(selection_record.advertiser_ids),
                AdObject.kind == "campaign",
                AdObject.source_connection_id == route.connection_id,
                AdObject.source_channel == route.channel,
                AdObject.observed_at <= selection_record.created_at,
            )
            .execution_options(populate_existing=True)
        ).all()
        for campaign in account_campaigns:
            objects[_ref_key(campaign.ref)] = campaign
        if mutation.field == "roas":
            for campaign in account_campaigns:
                for sibling in _child_adgroups(
                    session,
                    context=context,
                    campaign=campaign,
                    route=route,
                    observed_before=selection_record.created_at,
                ):
                    objects[_ref_key(sibling.ref)] = sibling

    # Smart+ ROAS is a series operation. Read only existing group rows from the
    # frozen route and before the selection timestamp, so later-created siblings
    # cannot be absorbed into this preview.
    candidate_seed = tuple(
        row for row in objects.values() if _ref_key(row.ref) not in excluded_refs
    )
    # Campaign/account/drama selections represent the whole series even when
    # the campaign row itself has no Smart+ marker.  Smart+ descendants are
    # therefore discovered for every selected campaign, while ad/adgroup
    # selections opt in when their own type carries the marker.
    series_selection = mutation.field == "roas" and (
        any(row.kind == "campaign" for row in candidate_seed)
        or any(row.ad_type.upper() in smart_types for row in candidate_seed)
    )
    if series_selection:
        campaigns: dict[tuple[Any, ...], AdObject] = {}
        for row in candidate_seed:
            campaign = _campaign_ancestor(
                session,
                context=context,
                bc_id=bc_id,
                row=row,
                objects=objects,
                route=route,
            )
            if campaign is None and row.kind == "campaign":
                campaign = row
            if campaign is not None:
                campaigns[_ref_key(campaign.ref)] = campaign
        for campaign in campaigns.values():
            for sibling in _child_adgroups(
                session,
                context=context,
                campaign=campaign,
                route=route,
                observed_before=selection_record.created_at,
            ):
                objects[_ref_key(sibling.ref)] = sibling
    commands: list[ManagementCommand] = []
    command_by_ref: dict[tuple[Any, ...], ManagementCommand] = {}
    command_items: dict[tuple[Any, ...], ExpandedItem] = {}
    material_mode = mutation.field == "material_status" or (
        mutation.field == "status" and bool(uses)
    )
    effective_field = "material_status" if material_mode else mutation.field
    selected_identity_keys = {_ref_key(ref) for ref in refs} | {
        _ref_key(use.ad_ref) for use in uses
    }

    def grouping_revision_for(row: AdObject) -> int | None:
        try:
            return _grouping_revision_for_row(
                session,
                row=row,
                objects=objects,
                context=context,
                bc_id=bc_id,
                route=route,
                observed_before=selection_record.created_at,
                allow_newer_parent=_ref_key(row.ref) in selected_identity_keys,
            )
        except DomainError as exc:
            if exc.code == "grouping_revision_missing":
                return None
            raise

    def add_item(row, *, use=None, linked=False, force_field=None):
        try:
            add_item_unchecked(row, use=use, linked=linked, force_field=force_field)
        except DomainError as exc:
            if frozen_record is None:
                raise
            # 后台准备逐项记录不可用，不因单个坏数值中断整批任务。
            items.append(
                ExpandedItem(
                    row.ref,
                    use,
                    None,
                    None,
                    exc.code,
                    "UNSUPPORTED",
                    None,
                    row.parent_ref,
                    0,
                    {},
                    linked,
                )
            )

    def add_item_unchecked(
        row: AdObject,
        *,
        use: MaterialUseRef | None = None,
        linked: bool = False,
        force_field: str | None = None,
    ) -> None:
        field = force_field or effective_field
        material = _material(session, use) if use is not None else None
        if use is not None and use.ad_material_id is not None and material is None:
            grouping_revision = grouping_revision_for(row)
            items.append(
                ExpandedItem(
                    row.ref,
                    use,
                    None,
                    None,
                    "material_reference_missing",
                    "UNSUPPORTED",
                    None,
                    row.parent_ref,
                    grouping_revision or 0,
                    {
                        "operation": operation_for_field(field),
                        "entity_kind": row.kind,
                        "ad_type": row.ad_type,
                        "source_connection_id": str(row.source_connection_id)
                        if row.source_connection_id
                        else None,
                        "source_channel": row.source_channel,
                        "route": route.model_dump(mode="json")
                        if route is not None
                        else None,
                    },
                    linked,
                )
            )
            return
        current = _current_value(session, row, use, field)
        operation = cast(str, operation_for_field(field))
        capability_ok = True
        capability: dict[str, Any] = {
            "operation": operation,
            "entity_kind": row.kind,
            "ad_type": row.ad_type,
            "published_version": row.published_version,
            "observed_at": row.observed_at.isoformat(),
            "configuration": deepcopy(row.configuration or {}),
        }
        grouping_revision = grouping_revision_for(row)
        if grouping_revision is None:
            items.append(
                ExpandedItem(
                    row.ref,
                    use,
                    current,
                    None,
                    "grouping_revision_missing",
                    "UNSUPPORTED",
                    None,
                    row.parent_ref,
                    0,
                    {
                        "operation": operation,
                        "entity_kind": row.kind,
                        "ad_type": row.ad_type,
                        "source_connection_id": str(row.source_connection_id)
                        if row.source_connection_id
                        else None,
                        "source_channel": row.source_channel,
                        "route": route.model_dump(mode="json")
                        if route is not None
                        else None,
                    },
                    linked,
                )
            )
            return
        capability["grouping_revision"] = grouping_revision
        capability["source_connection_id"] = (
            str(row.source_connection_id) if row.source_connection_id else None
        )
        capability["source_channel"] = row.source_channel
        if route is not None:
            capability["route"] = route.model_dump(mode="json")
        if use is not None:
            capability["ad_material_id"] = use.ad_material_id
            capability["platform_material_id"] = use.platform_material_id
        if require_capabilities:
            capability_ok = _management_allowed(
                session,
                context=context,
                bc_id=bc_id,
                advertiser_id=row.advertiser_id,
                route=route,
                operation=operation,
                entity_kind=row.kind,
            )
        if use is not None and (
            use.ad_material_id is None
            or row.ad_type.upper()
            not in {"SMART_PLUS", "UPGRADED_SMART_PLUS", "SMART+", "SMARTPLUS"}
        ):
            items.append(
                ExpandedItem(
                    row.ref,
                    use,
                    current,
                    None,
                    "material_status_unsupported",
                    "UNSUPPORTED",
                    None,
                    row.parent_ref,
                    grouping_revision,
                    capability,
                    linked,
                )
            )
            return
        if current is None:
            items.append(
                ExpandedItem(
                    row.ref,
                    use,
                    None,
                    None,
                    "configuration_missing",
                    "UNSUPPORTED",
                    None,
                    row.parent_ref,
                    grouping_revision,
                    capability,
                    linked,
                )
            )
            return
        if field in {"status", "material_status"}:
            assert isinstance(mutation.value, str)
            final: Decimal | str = mutation.value
        else:
            # 平台当前值为 0 或非数字（例如组预算系列的系列预算固定为 0）、
            # 或减少后不再为正数时，只把这一条标为不可用并说明原因，
            # 其余对象仍可正常预览和提交。
            try:
                current = _number(current, field=field, ref=row.ref)
            except DomainError:
                items.append(
                    ExpandedItem(
                        row.ref,
                        use,
                        None,
                        None,
                        "configuration_invalid",
                        "UNSUPPORTED",
                        None,
                        row.parent_ref,
                        grouping_revision,
                        capability,
                        linked,
                    )
                )
                return
            try:
                final = _final_value(current, mutation)
            except DomainError:
                items.append(
                    ExpandedItem(
                        row.ref,
                        use,
                        current,
                        None,
                        "final_value_invalid",
                        "UNSUPPORTED",
                        None,
                        row.parent_ref,
                        grouping_revision,
                        capability,
                        linked,
                    )
                )
                return
        if not capability_ok:
            items.append(
                ExpandedItem(
                    row.ref,
                    use,
                    current,
                    final,
                    "management_capability_unverified",
                    "UNSUPPORTED",
                    None,
                    row.parent_ref,
                    grouping_revision,
                    capability,
                    linked,
                )
            )
            return
        already_deleted = (
            field == "status"
            and final == "DISABLE"
            and use is None
            and row.delivery_status
            in {"CAMPAIGN_STATUS_DELETE", "ADGROUP_STATUS_DELETE", "AD_STATUS_DELETE"}
        )
        # 删除为平台终态且不可再修改；批量暂停不能把它混进写请求影响有效成员。
        if current == final or already_deleted:
            items.append(
                ExpandedItem(
                    row.ref,
                    use,
                    current,
                    final,
                    "already_deleted" if already_deleted else "no_change",
                    "NO_CHANGE",
                    None,
                    row.parent_ref,
                    grouping_revision,
                    capability,
                    linked,
                )
            )
            return
        command = _command(row, field=field, final=final, use=use)
        key = (_ref_key(row.ref), _use_key(use) if use else None, field)
        if key in command_by_ref:
            return
        command_by_ref[key] = command
        item = ExpandedItem(
            row.ref,
            use,
            current,
            final,
            None,
            "PENDING",
            command,
            row.parent_ref,
            grouping_revision,
            capability,
            linked,
        )
        command_items[key] = item
        commands.append(command)
        items.append(item)

    # Material selection represents independent switches; ordinary material refs
    # intentionally never degrade to an ad status command.
    if material_mode and uses:
        for use in uses:
            row = objects.get(_ref_key(use.ad_ref))
            if row is None:
                items.append(
                    ExpandedItem(
                        use.ad_ref,
                        use,
                        None,
                        None,
                        "ad_not_found",
                        "UNSUPPORTED",
                        None,
                        None,
                        0,
                        {},
                        False,
                    )
                )
            else:
                add_item(row, use=use)
    else:
        candidate_rows: list[AdObject] = (
            [
                row
                for row in objects.values()
                if _ref_key(row.ref) not in excluded_refs
                and not (
                    row.kind == "adgroup"
                    and row.parent_ref is not None
                    and _ref_key(row.parent_ref) in excluded_refs
                )
            ]
            if account_fallback
            else []
        )
        for ref in refs:
            row = objects.get(_ref_key(ref))
            if row is not None:
                candidate_rows.append(row)
        for parent_ref in mutation.include_parents:
            row = objects.get(_ref_key(parent_ref))
            if row is not None and row not in candidate_rows:
                candidate_rows.append(row)

        if series_selection:
            for row in objects.values():
                if (
                    row.kind == "adgroup"
                    and _ref_key(row.ref) not in excluded_refs
                    and not (
                        row.parent_ref is not None
                        and _ref_key(row.parent_ref) in excluded_refs
                    )
                    and row not in candidate_rows
                ):
                    candidate_rows.append(row)

        # ROAS is an ad-group setting. Ads and frozen campaigns map to groups only
        # when that group identity was itself frozen; no fresh descendants are read.
        if mutation.field == "roas":
            groups: dict[tuple[Any, ...], AdObject] = {}
            for row in candidate_rows:
                target = row
                if row.kind == "ad":
                    parent = (
                        objects.get(_ref_key(row.parent_ref))
                        if row.parent_ref
                        else None
                    )
                    if parent is None and row.parent_ref is not None:
                        parent = _object_for_ref(
                            session,
                            context=context,
                            bc_id=bc_id,
                            ref=row.parent_ref,
                            route=route,
                        )
                        if parent is not None:
                            objects[_ref_key(parent.ref)] = parent
                    target = (
                        parent
                        if parent is not None and parent.kind == "adgroup"
                        else None
                    )
                    if target is None:
                        items.append(
                            ExpandedItem(
                                row.ref,
                                None,
                                None,
                                None,
                                "adgroup_missing",
                                "UNSUPPORTED",
                                None,
                                row.parent_ref,
                                grouping_revision_for(row) or 0,
                                {
                                    "ad_type": row.ad_type,
                                    "configuration": deepcopy(row.configuration or {}),
                                    "source_connection_id": str(
                                        row.source_connection_id
                                    )
                                    if row.source_connection_id
                                    else None,
                                    "source_channel": row.source_channel,
                                    "route": route.model_dump(mode="json")
                                    if route is not None
                                    else None,
                                },
                                False,
                            )
                        )
                if target is not None and target.kind == "adgroup":
                    groups[_ref_key(target.ref)] = target
            for row in groups.values():
                add_item(row)
        elif mutation.field == "budget":
            owners: dict[tuple[Any, ...], AdObject] = {}
            for row in candidate_rows:
                found = _budget_owners(
                    session,
                    context=context,
                    bc_id=bc_id,
                    row=row,
                    objects=objects,
                    route=route,
                    observed_before=selection_record.created_at,
                )
                for owner in found:
                    owners[_ref_key(owner.ref)] = owner
                if not found:
                    items.append(
                        ExpandedItem(
                            row.ref,
                            None,
                            None,
                            None,
                            "budget_owner_missing",
                            "UNSUPPORTED",
                            None,
                            row.parent_ref,
                            grouping_revision_for(row) or 0,
                            {
                                "ad_type": row.ad_type,
                                "configuration": deepcopy(row.configuration or {}),
                                "source_connection_id": str(row.source_connection_id)
                                if row.source_connection_id
                                else None,
                                "source_channel": row.source_channel,
                                "route": route.model_dump(mode="json")
                                if route is not None
                                else None,
                            },
                            False,
                        )
                    )
            for row in owners.values():
                add_item(row)
        else:
            for row in candidate_rows:
                add_item(row)

        # Enabling a child does not silently rewrite a paused ancestor. Keep the
        # blocked parent visible so the caller can explicitly add it through
        # ``include_parents`` in a follow-up preview.
        if mutation.field == "status" and mutation.value == "ENABLE":
            existing = {_ref_key(item.ref) for item in items}
            for row in candidate_rows:
                parent = row
                while parent.parent_ref is not None:
                    parent_row = objects.get(_ref_key(parent.parent_ref))
                    if parent_row is None:
                        parent_row = _object_for_ref(
                            session,
                            context=context,
                            bc_id=bc_id,
                            ref=parent.parent_ref,
                            route=route,
                        )
                        if parent_row is not None:
                            objects[_ref_key(parent_row.ref)] = parent_row
                    if parent_row is None:
                        break
                    if (
                        parent_row.operation_status != "ENABLE"
                        and _ref_key(parent_row.ref) not in existing
                    ):
                        grouping_revision = grouping_revision_for(parent_row)
                        items.append(
                            ExpandedItem(
                                parent_row.ref,
                                None,
                                parent_row.operation_status,
                                "ENABLE",
                                "parent_disabled"
                                if grouping_revision is not None
                                else "grouping_revision_missing",
                                "NO_CHANGE"
                                if grouping_revision is not None
                                else "UNSUPPORTED",
                                None,
                                parent_row.parent_ref,
                                grouping_revision or 0,
                                {
                                    "ad_type": parent_row.ad_type,
                                    "configuration": deepcopy(
                                        parent_row.configuration or {}
                                    ),
                                    "source_connection_id": str(
                                        parent_row.source_connection_id
                                    )
                                    if parent_row.source_connection_id
                                    else None,
                                    "source_channel": parent_row.source_channel,
                                    "route": route.model_dump(mode="json")
                                    if route is not None
                                    else None,
                                },
                                True,
                            )
                        )
                        existing.add(_ref_key(parent_row.ref))
                    parent = parent_row

    # Keep selected refs that did not become direct commands visible in the preview.
    visible_refs = {_ref_key(item.ref) for item in items}
    for ref in refs:
        if _ref_key(ref) in visible_refs:
            continue
        row = objects.get(_ref_key(ref))
        if row is None:
            items.append(
                ExpandedItem(
                    ref,
                    None,
                    None,
                    None,
                    "directory_missing",
                    "UNSUPPORTED",
                    None,
                    None,
                    0,
                    {},
                    True,
                )
            )
        else:
            grouping_revision = grouping_revision_for(row)
            items.append(
                ExpandedItem(
                    ref,
                    None,
                    None,
                    None,
                    "linked_impact"
                    if grouping_revision is not None
                    else "grouping_revision_missing",
                    "NO_CHANGE" if grouping_revision is not None else "UNSUPPORTED",
                    None,
                    row.parent_ref,
                    grouping_revision or 0,
                    {
                        "ad_type": row.ad_type,
                        "configuration": deepcopy(row.configuration or {}),
                        "source_connection_id": str(row.source_connection_id)
                        if row.source_connection_id
                        else None,
                        "source_channel": row.source_channel,
                        "route": route.model_dump(mode="json")
                        if route is not None
                        else None,
                    },
                    True,
                )
            )

    # Same series cannot receive divergent ROAS values in one management task.
    if mutation.field == "roas":
        by_series: dict[tuple[Any, ...], set[str]] = defaultdict(set)
        for item in items:
            if item.final_value is None:
                continue
            row = objects.get(_ref_key(item.ref))
            series = (
                _series_key(
                    session,
                    row,
                    objects,
                    context=context,
                    bc_id=bc_id,
                    route=route,
                )
                if row is not None
                else (item.ref.advertiser_id, item.ref.remote_id)
            )
            by_series[series].add(str(item.final_value))
        conflicts = {key for key, values in by_series.items() if len(values) > 1}
        if conflicts and frozen_record is None:
            raise DomainError(
                "management_roas_series_conflict", "同系列不同最终 ROAS，预览拒绝"
            )
        if conflicts:
            for index, item in enumerate(items):
                row = objects.get(_ref_key(item.ref))
                if (
                    row is not None
                    and _series_key(
                        session, row, objects, context=context, bc_id=bc_id, route=route
                    )
                    in conflicts
                ):
                    items[index] = replace(
                        item,
                        command=None,
                        execution_result="CONFLICT",
                        reason="roas_series_conflict",
                    )
            commands = [item.command for item in items if item.command is not None]

    # 预览只展示目录里已有的名称；名称不参与写入，查不到时界面回退到对象 ID。
    names = {_ref_key(row.ref): row.name for row in objects.values()}
    items = [replace(item, name=names.get(_ref_key(item.ref))) for item in items]

    return Expansion(
        refs,
        uses,
        tuple(items),
        tuple(commands),
        len(uses)
        if material_mode and uses
        else (len(selection_record.advertiser_ids) if account_fallback else None),
    )


def expand_targets(
    session: Session,
    context: TenantContext,
    selection_id: UUID,
    mutation: MutationSpec,
) -> tuple[ManagementCommand, ...]:
    """Expand a valid B selection into immutable management commands.

    Capability checks are performed by :func:`prepare_preview` after the route is
    frozen. This lower-level helper deliberately returns only command objects;
    callers needing unsupported/linked items should consume the preview result.
    """
    from app.modules.accounts.routing import freeze_route

    bc_id, _, _ = _load_selection(session, context, selection_id)
    route = freeze_route(session, context=context, bc_id=bc_id)
    return _expand(
        session,
        context,
        bc_id=bc_id,
        selection_id=selection_id,
        mutation=mutation,
        require_capabilities=False,
        route=route,
    ).commands


__all__ = [
    "ExpandedItem",
    "Expansion",
    "expand_targets",
    "_expand",
    "_ref_dict",
    "_use_dict",
]
