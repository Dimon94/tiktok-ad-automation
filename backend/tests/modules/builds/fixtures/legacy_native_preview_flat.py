"""原生预览适配：复用 Structure，完整计划存入既有不可变预览正文。"""

from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlmodel import select

from app.core.config import settings
from app.core.errors import DomainError
from app.modules.accounts.routing import freeze_route, verify_route
from app.modules.ads.directory import list_objects
from app.modules.ads.directory_calibration import ACTIVE_INTERVAL
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.strategies.service import get_copies, get_version
from app.modules.strategies.structure import plan_structure

from .batch_numbers import insert_preview_with_number
from .frozen_requests import FrozenRequestRevision, native_request_digest
from .preview_models import BuildPreview, PreviewContract
from .preview_schemas import PreviewGenerationProgress, PreviewSummary
from .routes import save_preview_route
from .scene_jobs import native_checkpoint
from .structure import effective_structure_config


def observe_occupancy(session, *, context, bc_id, selection, location_ids, route):
    """只读取当前账户可见事实；目录 ENABLE 不是完整 active 的证据。"""
    conflicts = []
    for kind in ("campaign", "adgroup"):
        for row in list_objects(
            session,
            context=context,
            bc_id=bc_id,
            advertiser_ids=(selection.advertiser_id,),
            kind=kind,
        ):
            config = row.configuration
            locations = config.get(
                "location_ids", config.get("targeting_spec", {}).get("location_ids", [])
            )
            # 地区 ID 必须实际相交；不以国家名称或身份猜测 Series。
            overlap = sorted(set(locations) & set(location_ids))
            if (
                config.get("native_series_id") != selection.native_series_id
                or not overlap
            ):
                continue
            # 只承认显式活动标记或已删除；ENABLE/暂停/审核中等枚举不推断占用语义。
            state = (
                "INACTIVE"
                if row.operation_status in {"DELETE", "DELETED"}
                or row.delivery_status == "INACTIVE"
                else "ACTIVE"
                if row.delivery_status == "ACTIVE"
                else "UNKNOWN"
            )
            conflicts.append(
                {
                    "kind": kind,
                    "remote_id": row.remote_id,
                    "locations": overlap,
                    "state": state,
                    "operation_status": row.operation_status,
                    "delivery_status": row.delivery_status,
                    "observed_at": row.observed_at.isoformat(),
                    "published_version": row.published_version,
                }
            )
    runs = session.exec(
        select(AdDirectoryRun)
        .where(
            AdDirectoryRun.tenant_id == context.tenant_id,
            AdDirectoryRun.bc_id == bc_id,
            AdDirectoryRun.advertiser_id == selection.advertiser_id,
            AdDirectoryRun.status == "COMPLETE",
            AdDirectoryRun.frozen_route == route.model_dump(mode="json"),
            AdDirectoryRun.actor_id == context.actor_id,
            AdDirectoryRun.kind.in_(["campaign", "adgroup"]),
            AdDirectoryRun.published_version.is_not(None),
        )
        .order_by(AdDirectoryRun.published_version.desc())
        .limit(1)
    ).first()
    return {
        "visible_conflicts": conflicts,
        "coverage": (
            "UNKNOWN"
            if runs is None
            else "STALE"
            if runs.observed_at
            and runs.observed_at < datetime.now(UTC) - ACTIVE_INTERVAL
            else "PARTIAL"
        ),
        "observed_at": runs.observed_at.isoformat()
        if runs and runs.observed_at
        else None,
        "published_version": runs.published_version if runs else None,
        "scope_limits": [
            "仅当前授权账户；不覆盖其他账户或BC及观察后的变化",
            "目录开关/审核状态不证明平台完整active语义；UNKNOWN需平台最终判定",
        ],
        "directory_coverage": runs.coverage if runs else None,
        "directory_query": runs.query if runs else None,
        "directory_kind": runs.kind if runs else None,
        "directory_request_available": settings.ADS_SYNC_ENABLED
        and route.channel == "OFFICIAL_API",
    }


def generate(session, *, context, draft):
    route = freeze_route(
        session,
        context=context,
        bc_id=draft.bc_id,
        connection_id=draft.execution_connection_id,
    )
    selection = draft.scene_selection
    verify_route(
        session,
        context=context,
        route=route,
        advertiser_id=selection["advertiser_id"],
        capability="read",
    )
    checkpoint = native_checkpoint(session, context=context, draft=draft, route=route)
    checkpoint.evidence_ids.sort(key=str)
    if checkpoint.state != "READY" or not checkpoint.materials_ready:
        raise DomainError(
            "native_preview_not_ready",
            "原生证据尚未就绪："
            + ", ".join(
                checkpoint.reason_codes
                + [x["reason_code"] for x in checkpoint.material_issues]
            ),
        )
    from app.modules.accounts.access import resolve_account_access

    access = resolve_account_access(
        session,
        context=context,
        bc_id=draft.bc_id,
        advertiser_id=selection["advertiser_id"],
        connection_id=route.connection_id,
        action="read",
    )
    config = effective_structure_config(
        get_version(session, context=context, version_id=draft.strategy_version_id),
        draft.structure_override,
    )
    if (
        draft.media_kind != "VIDEO"
        or config.budget_strategy != "SERIES"
        or config.bid_strategy != "TARGET_ROAS"
        or config.creation_status != "ENABLE"
    ):
        raise DomainError(
            "native_structure_unsupported",
            "M1仅支持直接视频、CBO、Target ROAS及ENABLE意图",
        )
    if config.currency != access.currency:
        raise DomainError("currency_mismatch", "策略币种与账户币种不一致")
    materials = [x.model_dump(mode="json") for x in checkpoint.materials]
    from app.modules.materials.models import MaterialFile

    for mapping in materials:
        material = session.get(MaterialFile, UUID(mapping["material_id"]))
        mapping["sha256"] = material.sha256
        mapping["video_md5"] = material.video_md5
    seed = int(sha256(f"{draft.id}:{draft.revision}".encode()).hexdigest()[:16], 16)
    # 位置身份避免同一视频被明确选中多次时，被 Structure 的单元去重吞掉。
    units = [
        SimpleNamespace(unit_id=uuid5(NAMESPACE_URL, f"{draft.id}:{i}"))
        for i in range(len(materials))
    ]
    by_id = {u.unit_id: m for u, m in zip(units, materials, strict=True)}
    groups = plan_structure(
        units,
        config=config,
        pool=get_copies(session, context=context, version_id=config.copy_pool_version),
        seed=seed,
    )
    if len(groups) != 1:
        raise DomainError(
            "native_structure_unsupported",
            "M1须恰好一个Campaign和一个Adgroup，请调整结构",
        )
    dynamic = checkpoint.facts.get("cta", {})
    selected = set(config.cta_option_ids or dynamic.get("asset_ids", []))
    assets = [
        {
            "asset_ids": [x for x in item["asset_ids"] if x in selected],
            "asset_content": item["asset_content"],
        }
        for item in dynamic.get("recommend_assets", [])
        if selected.intersection(item["asset_ids"])
    ]
    if not selected or selected != {x for a in assets for x in a["asset_ids"]}:
        raise DomainError(
            "cta_unavailable", "动态CTA缺少完整推荐证据，请重新准备或调整策略"
        )
    from .preview_validation import scene_reasons
    from .scene_constraints import constraints_for
    from .scene_schemas import SceneContext

    constraints, reasons = constraints_for(access.currency)
    cta = {
        "asset_ids": sorted(selected),
        "recommend_assets": assets,
        "requires_portfolio_creation": True,
    }
    scene = SceneContext(
        supported=True,
        reason_codes=reasons,
        capability_revision=checkpoint.scope_basis,
        creative_limit=50,
        copy_length_limit=constraints["copy_length"],
        cta_fields=cta,
        field_constraints=constraints,
    )
    invalid = scene_reasons(config, scene, access.currency)
    if invalid:
        raise DomainError(
            "native_preview_not_ready", "策略未通过核验：" + ", ".join(invalid)
        )
    location_ids = sorted(
        x["location_id"]
        for x in checkpoint.facts["regions"]["locations"]
        if x["region_code"] in selection["regions"]
    )
    occupancy = observe_occupancy(
        session,
        context=context,
        bc_id=draft.bc_id,
        selection=checkpoint.selection,
        location_ids=location_ids,
        route=route,
    )
    if any(x["state"] == "ACTIVE" for x in occupancy["visible_conflicts"]):
        raise DomainError(
            "native_series_active_conflict",
            "Series与地区存在已知活跃占用："
            + ", ".join(
                x["remote_id"] + ":" + "/".join(x["locations"])
                for x in occupancy["visible_conflicts"]
                if x["state"] == "ACTIVE"
            ),
        )
    ads = [
        {
            "creation_status": "ENABLE",
            "ad_format": "SINGLE_VIDEO",
            "base_ad_no": ad.base_ad_no,
            "creative_no": i,
            "copy_id": str(copy.copy_id),
            "text": copy.text,
            "materials": [by_id[x] for x in ad.unit_ids],
            "creative_identity": selection["creative_identity"],
            "cta": cta,
        }
        for ad in groups[0].ads
        for i, copy in enumerate(ad.copies, 1)
    ]
    if any(not 1 <= len(x["text"]) <= constraints["copy_length"] for x in ads):
        raise DomainError("native_preview_not_ready", "实际文案长度不符合策略")
    from .targeting_service import effective_targeting

    targeting = effective_targeting(session, context, draft)
    minimum_age = targeting.automatic_minimum_age()
    delivery = draft.delivery_config or {}
    campaign_name = f"Series-{selection['native_series_id']}-v{draft.revision}"
    payload = FrozenRequestRevision(
        constraint_revision=constraints["revision"],
        draft_revision=draft.revision,
        strategy_version_id=str(draft.strategy_version_id),
        seed=seed,
        route=route.model_dump(mode="json"),
        selection=selection,
        checkpoint=checkpoint.model_dump(mode="json"),
        materials=materials,
        campaign={
            "name": campaign_name,
            "objective_type": "APP_PROMOTION",
            "app_promotion_type": "MINIS",
            "budget_mode": "BUDGET_MODE_DYNAMIC_DAILY_BUDGET",
            "budget": str(config.budget),
            "currency": config.currency,
            "budget_strategy": "SERIES",
            "creation_status": "ENABLE",
        },
        adgroup={
            "name": campaign_name + "-G1",
            "creation_status": "ENABLE",
            "schedule_type": "SCHEDULE_START_END"
            if delivery.get("schedule_mode") == "START_END"
            else "SCHEDULE_FROM_NOW",
            "schedule_start_time": delivery.get("schedule_start_time")
            if delivery.get("schedule_mode", "IMMEDIATE") != "IMMEDIATE"
            else None,
            "schedule_end_time": delivery.get("schedule_end_time")
            if delivery.get("schedule_mode") == "START_END"
            else None,
            "promotion_type": "NATIVE_SERIES",
            "native_series_id": selection["native_series_id"],
            "placement_type": "PLACEMENT_TYPE_NORMAL",
            "placements": ["PLACEMENT_TIKTOK"],
            "optimization_goal": "VALUE",
            "optimization_event": "AD_REVENUE_VALUE",
            "deep_bid_type": "VO_MIN_ROAS",
            "vbo_window": "ZERO_DAY",
            "bid_type": "BID_TYPE_NO_BID",
            "target_roas": str(config.target_roas),
            "targeting_spec": {
                "location_ids": location_ids,
                **(
                    {"languages": list(targeting.languages)}
                    if targeting.languages
                    else {}
                ),
                **({"spc_audience_age": minimum_age} if minimum_age else {}),
            },
            "targeting_optimization_mode": "AUTOMATIC",
            "location_ids": location_ids,
            "regions": selection["regions"],
            "series_identity": selection["series_identity"],
            "delivery": delivery,
        },
        ads=ads,
        occupancy=occupancy,
        config=config.model_dump(mode="json"),
    ).model_dump(mode="json")
    digest = native_request_digest(payload)
    row = BuildPreview(
        tenant_id=context.tenant_id,
        bc_id=draft.bc_id,
        draft_id=draft.id,
        draft_revision=draft.revision,
        strategy_version_id=draft.strategy_version_id,
        actor_id=context.actor_id,
        batch_short_id="",
        local_date=datetime.now(UTC).strftime("%Y%m%d"),
        config={**config.model_dump(mode="json"), "native_request": payload},
        budget=config.budget,
        target_roas=config.target_roas,
        progress={"phase": "complete", "total_units": 1},
    )
    insert_preview_with_number(session, row)
    session.add(
        PreviewContract(
            tenant_id=context.tenant_id,
            bc_id=draft.bc_id,
            preview_id=row.id,
            version=2,
            media_kind="VIDEO",
        )
    )
    session.flush()
    save_preview_route(session, context=context, preview_id=row.id, route=route)
    snapshot_rows(
        session,
        context=context,
        preview=row,
        payload=payload,
        units=units,
        groups=groups,
        access=access,
    )
    row.status, row.content_digest = "FROZEN", digest
    session.add(row)
    session.flush()
    return row.id


def summary(session, *, context, preview):
    from .route_views import execution_route_view

    p = FrozenRequestRevision.model_validate(
        preview.config["native_request"]
    ).model_dump(mode="json")
    return PreviewSummary(
        native_request=p,
        contract_version=2,
        media_kind="VIDEO",
        submission_id=None,
        preview_id=preview.id,
        draft_id=preview.draft_id,
        draft_revision=preview.draft_revision,
        bc_id=preview.bc_id,
        status=preview.status,
        currency=p["campaign"]["currency"],
        campaign_count=1,
        adgroup_count=1,
        ad_count=len(p["ads"]),
        blocked_count=0,
        preparing_count=0,
        input_issue_count=0,
        total_unit_count=1,
        daily_budget_sum=preview.budget,
        budget_strategy="SERIES",
        bid_strategy="TARGET_ROAS",
        creative_count=p["config"]["creative_count"],
        unique_material_count=len({x["material_id"] for x in p["materials"]}),
        material_allocation_count=sum(len(x["materials"]) for x in p["ads"]),
        content_digest=preview.content_digest,
        error_code=preview.error_code,
        created_at=preview.created_at,
        targeting_region_codes=p["selection"]["regions"],
        execution_route=execution_route_view(
            session, context=context, preview_id=preview.id
        ),
        generation_progress=PreviewGenerationProgress(
            phase="complete",
            completed_units=1,
            total_units=1,
            updated_at=preview.created_at,
        ),
    )


def snapshot_rows(session, *, context, preview, payload, units, groups, access):
    """同一 BuildUnit/PlannedAd owner；Series分组ID不冒充版权方剧目或推广链接。"""
    from app.modules.materials.models import MaterialFile

    from .creative_units import creative_content_digest
    from .preview_models import (
        BuildUnit,
        PlannedAd,
        PlannedGroup,
        PreviewAdCreativeUnit,
        PreviewCreativeUnit,
        PreviewCreativeUnitItem,
        PreviewDrama,
        PreviewDramaGroup,
        PreviewGroupCreativeUnit,
    )

    scope = {
        "tenant_id": context.tenant_id,
        "preview_id": preview.id,
        "bc_id": preview.bc_id,
    }
    drama_id = uuid5(
        NAMESPACE_URL,
        f"native-series:{preview.bc_id}:{payload['selection']['native_series_id']}",
    )
    name = payload["campaign"]["name"]
    session.add(
        PreviewDrama(
            **scope,
            drama_id=drama_id,
            title=payload["selection"]["native_series_id"],
            url="",
            protected_base=name,
        )
    )
    session.flush()
    session.add(PreviewDramaGroup(**scope, drama_id=drama_id, group_no=1))
    unit = BuildUnit(
        **scope,
        drama_id=drama_id,
        advertiser_id=payload["selection"]["advertiser_id"],
        connection_id=access.connection_id,
        currency=access.currency,
        timezone=access.timezone,
        campaign_name=name,
        campaign_digest=sha256(name.encode()).hexdigest(),
        scene_snapshot={
            "native_request": payload,
            "campaign_fields": payload["campaign"],
            "adgroup_fields": payload["adgroup"],
            "creative_fields": {
                "creative_identity": payload["selection"]["creative_identity"]
            },
            "cta_fields": payload["ads"][0]["cta"],
            "field_constraints": {
                "selected_region_codes": payload["selection"]["regions"],
                "native_checkpoint": payload["checkpoint"],
            },
            "budget_strategy": "SERIES",
            "bid_strategy": "TARGET_ROAS",
        },
        group_count=1,
        ad_count=len(payload["ads"]),
        complete=True,
    )
    session.add(unit)
    session.flush()
    group = PlannedGroup(
        **scope,
        unit_id=unit.id,
        drama_id=drama_id,
        group_no=1,
        name=payload["adgroup"]["name"],
    )
    session.add(group)
    session.flush()
    for i, (planned, mapping) in enumerate(
        zip(units, payload["materials"], strict=True), 1
    ):
        material = session.get(MaterialFile, UUID(mapping["material_id"]))
        session.add(
            PreviewCreativeUnit(
                **scope,
                id=planned.unit_id,
                drama_id=drama_id,
                kind="VIDEO",
                position=i,
                generation=1,
                content_revision=1,
                music_revision=0,
                content_digest=creative_content_digest(
                    "VIDEO", [(material.id, material.sha256)], None
                ),
            )
        )
        session.flush()
        session.add(
            PreviewCreativeUnitItem(
                **scope,
                creative_unit_id=planned.unit_id,
                position=1,
                drama_id=drama_id,
                material_id=material.id,
                content_digest=material.sha256,
                file_name=material.file_name,
            )
        )
        session.add(
            PreviewGroupCreativeUnit(
                **scope,
                drama_id=drama_id,
                group_no=1,
                position=i,
                creative_unit_id=planned.unit_id,
            )
        )
    for base in groups[0].ads:
        for i, identity in enumerate(base.unit_ids, 1):
            session.add(
                PreviewAdCreativeUnit(
                    **scope,
                    drama_id=drama_id,
                    group_no=1,
                    base_ad_no=base.base_ad_no,
                    position=i,
                    creative_unit_id=identity,
                )
            )
    for ad in payload["ads"]:
        session.add(
            PlannedAd(
                **scope,
                group_id=group.id,
                base_ad_no=ad["base_ad_no"],
                creative_no=ad["creative_no"],
                name=f"{name}-A{ad['base_ad_no']}-C{ad['creative_no']}",
                copy_id=UUID(ad["copy_id"]),
                text=ad["text"],
                cta_option_ids=ad["cta"]["asset_ids"],
            )
        )
    session.flush()
