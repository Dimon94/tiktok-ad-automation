"""本地投放对象详情。

详情只消费 A 阶段已发布的目录和素材使用引用；缺少本地视频文件不会让外部
广告消失。父级限制单独返回，避免把停用、审核或排期状态误报成子广告状态。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import EntityRef, MaterialUseRef
from app.modules.ad_management.models import (
    ManagementPreview,
    ManagementTask,
    ManagementTaskItem,
)
from app.modules.ads import directory
from app.modules.ads.models import AdMaterialReference, AdObject
from app.modules.reporting.query_models import read_snapshot


class ParentRestrictionPublic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: EntityRef
    operation_status: str | None = None
    review_status: str | None = None
    delivery_status: str | None = None
    reasons: tuple[str, ...] = ()


class MaterialUsePublic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    use_ref: MaterialUseRef
    name: str = ""
    local_material_id: str | None = None
    operation_status: str | None = None
    complete: bool = False
    external: bool = True


class DirectoryIdentityPublic(BaseModel):
    identity_type: str | None = None
    identity_id: str | None = None
    identity_authorized_bc_id: str | None = None


class DirectoryObservationPublic(BaseModel):
    observed_at: datetime | None = None
    published_version: int | None = None
    source_connection_id: UUID | None = None
    source_channel: str | None = None


class DirectoryParentPublic(DirectoryObservationPublic):
    ref: EntityRef
    configuration: dict[str, Any]


class DirectoryMaterialPublic(DirectoryObservationPublic):
    platform_material_id: str
    material_type: str
    name: str | None = None
    ad_material_id: str | None = None


class DirectoryCreativePublic(DirectoryObservationPublic):
    creative_id: str | None
    identity: DirectoryIdentityPublic | None
    configuration: dict[str, Any]
    materials: tuple[DirectoryMaterialPublic, ...]
    coverage_reasons: tuple[str, ...]


class NativeDirectoryPublic(BaseModel):
    series_id: str | None = None
    series_name: str | None = None
    group_identity: DirectoryIdentityPublic | None = None
    placement_type: str | None = None
    placements: tuple[str, ...] | None = None
    tiktok_subplacements: tuple[str, ...] | None = None
    parent_adgroup: DirectoryParentPublic | None = None
    creatives: tuple[DirectoryCreativePublic, ...] | None = None
    coverage_reasons: tuple[str, ...] = ()


def _identity(config: dict[str, Any] | None) -> DirectoryIdentityPublic | None:
    if config is None:
        return None
    keys = ("identity_type", "identity_id", "identity_authorized_bc_id")
    return (
        DirectoryIdentityPublic(**{k: config.get(k) for k in keys})
        if any(k in config for k in keys)
        else None
    )


def _observation(entity):
    return {
        k: getattr(entity, k, None)
        for k in (
            "observed_at",
            "published_version",
            "source_connection_id",
            "source_channel",
        )
    }


def _native_detail(
    session: Session,
    context: TenantContext,
    bc_id: str,
    entity: AdObject,
    parent: AdObject | None,
    material_rows: list[AdMaterialReference],
) -> NativeDirectoryPublic | None:
    if entity.kind not in {"adgroup", "ad"}:
        return None
    group = (
        entity
        if entity.kind == "adgroup"
        else parent
        if entity.kind == "ad" and parent and parent.kind == "adgroup"
        else None
    )
    config = group.configuration if group else {}

    def explicit(c):
        return (
            c.get("native_series_id") is not None
            or c.get("promotion_type") == "NATIVE_SERIES"
        )

    if not explicit(config) and not explicit(entity.configuration):
        return None
    reasons = []
    if group is None:
        reasons.append("parent_not_synced")
    for key in (
        "native_series_id",
        "native_series_name",
        "identity_type",
        "identity_id",
        "identity_authorized_bc_id",
        "placement_type",
        "placements",
        "tiktok_subplacements",
    ):
        if config.get(key) is None:
            reasons.append(f"{key}:not_provided")
    creatives = None
    if entity.kind == "ad":
        facts = directory.list_native_creatives(
            session, context=context, bc_id=bc_id, parent=entity.ref
        )
        children = directory.list_objects(
            session,
            context=context,
            bc_id=bc_id,
            advertiser_ids=(entity.advertiser_id,),
            kind="creative",
            parent=entity.ref,
        )
        # 只有相同 creative ID 可连接两个观察来源；无 ID 配置始终单独保留。
        configs = {c.remote_id: (c.configuration, c) for c in children}
        anonymous = []
        creative_list = entity.configuration.get("creative_list")
        if creative_list is None:
            reasons.append("creative_list:not_provided")
        for item in creative_list if creative_list is not None else ():
            creative_id = item.get("smart_plus_creative_id")
            if creative_id is None:
                anonymous.append(item)
            elif creative_id not in configs:
                configs[creative_id] = (item, entity)
        by_id = {f.creative_id: f for f in facts}
        entries = [
            (key, *configs.get(key, ({}, None)), by_id.get(key))
            for key in sorted(set(configs) | set(by_id))
        ]
        entries += [(None, c, entity, None) for c in anonymous]
        projected = []
        for creative_id, raw, observed, fact in entries:
            missing = []
            identity = _identity(raw.get("creative_info", raw))
            if identity is None or any(
                getattr(identity, k) is None
                for k in DirectoryIdentityPublic.model_fields
            ):
                missing.append("creative_identity:not_provided")
            if fact is None:
                missing.append("creative_materials:not_covered")
            elif fact.identity_status in {"UNRESOLVED", "CONFLICT"}:
                missing.append(f"creative_materials:{fact.identity_status.lower()}")
            assets = []
            for asset in fact.assets if fact else []:
                matches = [
                    m
                    for m in material_rows
                    if m.platform_material_id == asset["platform_material_id"]
                    and m.material_type == asset["material_type"]
                    and m.ad_material_id == fact.ad_material_id
                ]
                match = matches[0] if len(matches) == 1 else None
                assets.append(
                    DirectoryMaterialPublic(
                        **asset,
                        ad_material_id=fact.ad_material_id,
                        name=match.name if match else None,
                        observed_at=fact.observed_at,
                        published_version=fact.published_version,
                    )
                )
            if not assets:
                missing.append("materials:not_covered")
            if creative_id is None:
                missing.append("creative_id:not_provided")
            projected.append(
                DirectoryCreativePublic(
                    creative_id=creative_id,
                    identity=identity,
                    configuration=raw,
                    materials=tuple(assets),
                    coverage_reasons=tuple(missing),
                    **_observation(observed or fact),
                )
            )
        creatives = tuple(projected) if projected else None
        # 已发布关系是观察事实，现有模型没有广告级完整创意清单证明。
        reasons.append("creative_coverage:unknown")
    return NativeDirectoryPublic(
        series_id=config.get("native_series_id"),
        series_name=config.get("native_series_name"),
        group_identity=_identity(config),
        placement_type=config.get("placement_type"),
        placements=config.get("placements"),
        tiktok_subplacements=config.get("tiktok_subplacements"),
        parent_adgroup=DirectoryParentPublic(
            ref=group.ref, configuration=group.configuration, **_observation(group)
        )
        if group and entity.kind == "ad"
        else None,
        creatives=creatives,
        coverage_reasons=tuple(reasons),
    )


class AdDetailPublic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    native: NativeDirectoryPublic | None = None
    ref: EntityRef
    name: str
    ad_type: str
    configuration: dict[str, Any] = Field(default_factory=dict)
    statuses: dict[str, str | None] = Field(default_factory=dict)
    parent: ParentRestrictionPublic | None = None
    materials: tuple[MaterialUsePublic, ...] = ()
    operation_history: tuple[dict[str, Any], ...] = ()
    observed_at: datetime
    published_version: int
    source_connection_id: UUID | None = None
    source_channel: str | None = None


def _reasons(entity: AdObject) -> tuple[str, ...]:
    config = entity.configuration or {}
    keys = (
        "disable_reason",
        "disabled_reason",
        "review_reason",
        "reject_reason",
        "schedule_reason",
        "delivery_reason",
    )
    result: list[str] = []
    for key in keys:
        value = config.get(key)
        if value is not None and str(value).strip():
            result.append(str(value))
    if entity.operation_status not in {None, "ENABLE", "STATUS_ENABLE"}:
        result.append(f"operation_status:{entity.operation_status}")
    if entity.review_status not in {None, "APPROVED", "REVIEW_PASS"}:
        result.append(f"review_status:{entity.review_status}")
    if entity.delivery_status not in {None, "ENABLE", "STATUS_ENABLE"}:
        result.append(f"delivery_status:{entity.delivery_status}")
    return tuple(dict.fromkeys(result))


def get_ad_detail(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    ref: EntityRef,
    snapshot_id: UUID | None = None,
) -> AdDetailPublic:
    if snapshot_id is not None:
        # Bind detail reads to the same tenant/BC snapshot lifecycle as list,
        # pagination and selection requests before reading current directory data.
        read_snapshot(session, context=context, bc_id=bc_id, snapshot_id=snapshot_id)
    try:
        entity = directory.locate(session, context=context, bc_id=bc_id, ref=ref)
    except DomainError as exc:
        # Do not disclose an object that belongs to another BC or tenant.
        raise HTTPException(404, detail="ad_not_found") from exc
    parent_entity: AdObject | None = None
    if entity.parent_ref is not None:
        try:
            parent_entity = directory.locate(
                session, context=context, bc_id=bc_id, ref=entity.parent_ref
            )
        except DomainError:
            parent_entity = None
    parent = None
    if entity.parent_ref is not None:
        parent = ParentRestrictionPublic(
            ref=entity.parent_ref,
            operation_status=parent_entity.operation_status if parent_entity else None,
            review_status=parent_entity.review_status if parent_entity else None,
            delivery_status=parent_entity.delivery_status if parent_entity else None,
            reasons=_reasons(parent_entity)
            if parent_entity
            else ("parent_not_synced",),
        )
    material_rows = session.exec(
        select(AdMaterialReference)
        .where(
            AdMaterialReference.tenant_id == context.tenant_id,
            AdMaterialReference.advertiser_id == ref.advertiser_id,
            AdMaterialReference.ad_remote_id == ref.remote_id,
        )
        .order_by(
            col(AdMaterialReference.platform_material_id),
            col(AdMaterialReference.ad_material_id),
        )
    ).all()
    materials = tuple(
        MaterialUsePublic(
            use_ref=row.use_ref,
            name=row.name,
            local_material_id=str(row.local_material_id)
            if row.local_material_id
            else None,
            operation_status=row.operation_status,
            complete=row.complete,
            external=row.local_material_id is None,
        )
        for row in material_rows
    )
    # 本工具的管理操作只记录在管理任务项里；按冻结的对象引用匹配，动作取自预览。
    history_rows = session.exec(
        select(ManagementTaskItem, ManagementTask, ManagementPreview)
        .join(ManagementTask, col(ManagementTask.id) == col(ManagementTaskItem.task_id))
        .join(
            ManagementPreview,
            col(ManagementPreview.id) == col(ManagementTask.preview_id),
        )
        .where(
            ManagementTaskItem.tenant_id == context.tenant_id,
            ManagementTask.bc_id == bc_id,
            col(ManagementTaskItem.ref).op("->>")("advertiser_id") == ref.advertiser_id,
            col(ManagementTaskItem.ref).op("->>")("kind") == ref.kind,
            col(ManagementTaskItem.ref).op("->>")("remote_id") == ref.remote_id,
        )
        .order_by(col(ManagementTask.created_at).desc())
        .limit(100)
    ).all()
    history = tuple(
        {
            "task_id": str(task.id),
            "created_at": task.created_at.isoformat(),
            "field": preview.mutation.get("field"),
            "mode": preview.mutation.get("mode"),
            "original_value": item.original_value,
            "final_value": item.final_value,
            "execution_result": item.execution_result,
            "reason": item.reason,
        }
        for item, task, preview in history_rows
    )
    return AdDetailPublic(
        native=_native_detail(
            session, context, bc_id, entity, parent_entity, material_rows
        ),
        ref=entity.ref,
        name=entity.name,
        ad_type=entity.ad_type,
        configuration=entity.configuration,
        statuses={
            "operation": entity.operation_status,
            "review": entity.review_status,
            "delivery": entity.delivery_status,
        },
        parent=parent,
        materials=materials,
        operation_history=history,
        observed_at=entity.observed_at,
        published_version=entity.published_version,
        source_connection_id=entity.source_connection_id,
        source_channel=entity.source_channel,
    )
