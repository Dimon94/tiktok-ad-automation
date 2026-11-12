"""每个副本直接冻结原层级，再交给原有素材、创建和回读执行链。"""

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, NoReturn
from uuid import UUID, uuid5

from sqlalchemy import func
from sqlmodel import Session, select

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.accounts.access import resolve_account_access
from app.modules.accounts.routing import verify_route
from app.modules.materials.models import MaterialFile
from app.modules.materials.readiness import get_material_readiness
from app.modules.tenants.permissions import require_tenant

from .copy_compile import COPY_KEY, group_setting, observed_body
from .copy_source import digest, fingerprint, load_copy_source
from .creative_units import creative_content_digest
from .models import BuildDraft
from .preview_models import (
    BuildPreview,
    BuildUnit,
    PlannedAd,
    PlannedGroup,
    PreviewAdCreativeUnit,
    PreviewContract,
    PreviewCreativeUnit,
    PreviewCreativeUnitItem,
    PreviewDrama,
    PreviewDramaGroup,
    PreviewGroupCreativeUnit,
)
from .preview_validation import name_reasons, scene_reasons
from .routes import save_preview_route
from .scene import read_scene_context
from .scene_jobs import ensure_scene_preparation
from .schemas import DeliveryConfig
from .targeting import apply_targeting
from .targeting_schemas import AudienceTargeting


def _fail(code: str, message: str) -> NoReturn:
    raise DomainError(code, message)


def _target_group(body, *, scene, configuration, budget_strategy, cta_ids, currency):
    """资格证据与源实际设置分离；只有明确覆盖时才替换业务字段。"""
    body = deepcopy(body)
    if body["minis_id"] != scene.adgroup_fields.get("minis_id"):
        _fail("copy_minis_unavailable", "目标账户未核实来源推广小程序")
    regions = list(scene.field_constraints.get("target_regions", []))
    allowed_ids = {item["location_id"] for item in regions}
    if configuration.get("targeting") is not None:
        selected = AudienceTargeting.model_validate(configuration["targeting"])
        target_scene = apply_targeting(
            scene, selected, sorted({item["region_code"] for item in regions})
        )
        if not target_scene.supported:
            _fail(
                "copy_targeting_unavailable",
                "目标账户不能使用所选定向：" + ", ".join(target_scene.reason_codes),
            )
        body["targeting_spec"] = deepcopy(
            dict(target_scene.adgroup_fields["targeting_spec"])
        )
        body["targeting_optimization_mode"] = "AUTOMATIC"
    elif not set(body["targeting_spec"]["location_ids"]) <= allowed_ids:
        _fail("copy_targeting_unavailable", "目标账户未核实来源全部投放地区")
    if configuration.get("budget") is not None and budget_strategy == "ADGROUP":
        body["budget"] = str(configuration["budget"])
    if configuration.get("target_roas") is not None:
        body["roas_bid"] = str(configuration["target_roas"])
        body["deep_bid_type"] = "VO_MIN_ROAS"
    bid_strategy = (
        "TARGET_ROAS" if body.get("roas_bid") is not None else "HIGHEST_VALUE"
    )
    budget = (
        body.get("budget")
        if budget_strategy == "ADGROUP"
        else configuration["effective_campaign_budget"]
    )
    reasons = scene_reasons(
        SimpleNamespace(
            budget_strategy=budget_strategy,
            bid_strategy=bid_strategy,
            budget=Decimal(budget),
            target_roas=Decimal(body["roas_bid"]) if body.get("roas_bid") else None,
            currency=currency,
            cta_option_ids=cta_ids,
        ),
        scene,
        currency,
    )
    if reasons:
        _fail(
            "copy_scene_unsupported",
            "目标账户投放条件不满足复制设置：" + ", ".join(reasons),
        )
    # 再经严格合同编译，防止 override 绕过金额/竞价/定向约束。
    return observed_body("ADGROUP", body)


def create_copy_preview(
    session: Session,
    *,
    context,
    bc_id,
    source: dict,
    configuration: dict,
    advertiser_id: str,
    ordinal: int,
    request_id: UUID,
) -> UUID:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="build"
    )
    snapshot = source.get("snapshot", {})
    if source.get("blockers") or snapshot.get("version") != 2:
        _fail("copy_source_blocked", "来源存在未解决的问题，不能生成复制预览")
    if (snapshot.get("tenant_id"), snapshot.get("bc_id")) != (
        str(context.tenant_id),
        bc_id,
    ):
        _fail("copy_scope_mismatch", "复制来源不属于当前租户或BC")
    if (
        advertiser_id not in configuration["target_advertiser_ids"]
        or not 1 <= ordinal <= configuration["copies"]
    ):
        _fail("copy_scope_mismatch", "副本不在已选择账户和份数范围内")
    session.exec(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(f"copy-child:{context.tenant_id}:{request_id}", 0)
            )
        )
    )
    intent_digest = digest([source, configuration, advertiser_id, ordinal])
    draft_id, preview_id = uuid5(request_id, "draft"), uuid5(request_id, "preview")
    draft = session.get(BuildDraft, draft_id, populate_existing=True)
    if draft is not None and (
        draft.tenant_id != context.tenant_id or draft.request_digest != intent_digest
    ):
        _fail("idempotency_conflict", "副本请求标识已绑定其他配置")
    source_route = FrozenTikTokRoute.model_validate(snapshot["source_route"])
    verify_route(
        session,
        context=context,
        route=source_route,
        advertiser_id=source["advertiser_id"],
        capability="read",
    )
    if not configuration.get("execution_route"):
        _fail("copy_scope_mismatch", "复制缺少父批次冻结执行连接")
    route = FrozenTikTokRoute.model_validate(configuration["execution_route"])
    if (route.tenant_id, route.bc_id) != (context.tenant_id, bc_id):
        _fail("copy_scope_mismatch", "复制目标执行连接范围不匹配")
    verify_route(
        session,
        context=context,
        route=route,
        advertiser_id=advertiser_id,
        capability="build",
    )
    existing = session.get(BuildPreview, preview_id, populate_existing=True)
    if existing is not None:
        if existing.tenant_id != context.tenant_id or existing.status != "FROZEN":
            _fail("copy_preview_incomplete", "副本预览尚未完整冻结")
        return preview_id
    # 目录在配置期间变化时整行需要重建快照；不继续使用混合时点设置。
    latest = load_copy_source(
        session,
        context=context,
        bc_id=bc_id,
        advertiser_id=source["advertiser_id"],
        campaign_id=source["campaign_id"],
    )
    if (
        latest["blockers"]
        or latest["snapshot"].get("source_digest") != snapshot["source_digest"]
    ):
        _fail("copy_source_changed", "来源系列已改变或无法核实，请刷新复制来源")
    access = resolve_account_access(
        session,
        context=context,
        bc_id=bc_id,
        advertiser_id=advertiser_id,
        action="build",
        connection_id=route.connection_id,
    )
    if access.currency != source["currency"]:
        _fail("copy_currency_mismatch", "目标账户币种不同，暂不支持跨币种复制预算")
    delivery = DeliveryConfig.model_validate(
        configuration["delivery_config"]
    ).model_dump(mode="json")
    if draft is None:
        draft = BuildDraft(
            id=draft_id,
            tenant_id=context.tenant_id,
            bc_id=bc_id,
            strategy_version_id=None,
            provider_connection_id=None,
            application_id=None,
            link_config={"source_mode": "CAMPAIGN_COPY"},
            execution_connection_id=route.connection_id,
            created_by=context.actor_id,
            request_id=request_id,
            request_digest=intent_digest,
            scene_selection={
                "kind": "MINI_APP",
                "creative_identity": snapshot["identity"],
            },
            delivery_config=delivery,
            status="PREPARING",
        )
        session.add(draft)
        session.flush()
    elif draft.execution_connection_id != route.connection_id:
        _fail("frozen_route_changed", "副本准备期间执行连接已变化")
    prepared = ensure_scene_preparation(
        session,
        context=context,
        bc_id=bc_id,
        advertiser_id=advertiser_id,
        link_id=None,
        minis_id=snapshot["minis_id"],
        route=route,
        selected_identity=snapshot["identity"],
        media_kind="VIDEO",
    )
    if prepared.state == "queued":
        # 调用方捕获后提交；不能回滚已登记的场景任务。预览尚未创建，不能提前提交。
        raise DomainError("copy_preparing", "目标账户投放信息正在准备", retryable=True)
    if prepared.state != "ready":
        _fail(
            "copy_scene_unavailable",
            "目标账户投放条件不可用：" + str(prepared.reason_code),
        )
    scene = read_scene_context(
        session,
        context=context,
        bc_id=bc_id,
        advertiser_id=advertiser_id,
        link_id=None,
        minis_id=snapshot["minis_id"],
        route=route,
        selected_identity=snapshot["identity"],
        media_kind="VIDEO",
    )
    if not scene.supported:
        _fail("copy_scene_unavailable", "目标账户投放条件未通过核验")
    from app.modules.campaign_copies.source_facts import ensure_source_facts

    facts = ensure_source_facts(session, context=context, source=source)
    expected_contents = {entry["asset_content"] for entry in facts["cta_contents"]}
    recommendations = scene.cta_fields.get("recommend_assets", [])
    matched = [
        entry
        for entry in recommendations
        if entry["asset_content"] in expected_contents
    ]
    if {entry["asset_content"] for entry in matched} != expected_contents:
        _fail("copy_cta_unavailable", "目标账户没有来源CTA文案对应的已核实资产")
    cta_ids = sorted({asset for entry in matched for asset in entry["asset_ids"]})
    available = {item["material_id"] for item in source["materials"]}
    selected = set(configuration["material_ids"])
    if not selected or not selected <= available:
        _fail("copy_material_selection_invalid", "所选素材为空或不属于来源系列")
    local_materials, readiness = {}, []
    for mid in sorted(selected):
        material = session.get(MaterialFile, UUID(mid))
        if (
            material is None
            or material.tenant_id != context.tenant_id
            or material.media_type != "VIDEO"
        ):
            _fail("copy_material_unavailable", "所选素材已不可用")
        ready = get_material_readiness(
            session,
            context=context,
            bc_id=bc_id,
            material_id=material.id,
            advertiser_id=advertiser_id,
            route=route,
        )
        if ready.state == "blocked":
            _fail(
                "copy_material_unavailable",
                "目标素材不可准备：" + str(ready.reason_code),
            )
        local_materials[mid] = material
        readiness.append(ready.state)
    campaign = deepcopy(snapshot["campaign"])
    budget_strategy = source["budget_strategy"]
    if configuration.get("budget") is not None and budget_strategy == "SERIES":
        campaign["budget"] = str(configuration["budget"])
    effective = {**configuration, "effective_campaign_budget": campaign["budget"]}
    suffix = f"-副本{ordinal}-{request_id.hex[:8]}"
    campaign_name = campaign["campaign_name"] + suffix
    # 名称超限须显示阻断，不能截断来源名称使多个副本发生碰撞。
    if name_reasons(campaign_name, "campaign", scene.to_snapshot()):
        _fail("copy_name_invalid", "复制系列名称超过目标账户限制")
    scope: dict[str, Any] = {
        "tenant_id": context.tenant_id,
        "preview_id": preview_id,
        "bc_id": bc_id,
    }
    drama_id = UUID(snapshot["drama_id"])
    unit_id = uuid5(request_id, "unit")
    group_plans, contract_groups = [], {}
    for group_no, source_group in enumerate(snapshot["groups"], 1):
        kept_ads = [
            (ad, [mid for mid in ad["material_ids"] if mid in selected])
            for ad in source_group["ads"]
        ]
        kept_ads = [(ad, mids) for ad, mids in kept_ads if mids]
        # 用户去掉某广告全部素材时不创建空广告；空组随之移除，其他归属保持。
        if not kept_ads:
            continue
        group_id = uuid5(request_id, f"group:{source_group['remote_id']}")
        body = _target_group(
            source_group["body"],
            scene=scene,
            configuration=effective,
            budget_strategy=budget_strategy,
            cta_ids=cta_ids,
            currency=access.currency,
        )
        group_name = body["adgroup_name"] + suffix
        if (
            name_reasons(group_name, "adgroup", scene.to_snapshot())
            or len(kept_ads) > scene.field_constraints["max_ads_per_adgroup"]
        ):
            _fail("copy_structure_invalid", "复制广告组名称或广告数量超过目标账户限制")
        for ad, mids in kept_ads:
            if (
                name_reasons(ad["body"]["ad_name"] + suffix, "ad", scene.to_snapshot())
                or len(mids) > scene.creative_limit
            ):
                _fail(
                    "copy_structure_invalid", "复制广告名称或素材数量超过目标账户限制"
                )
        contract_groups[str(group_id)] = body
        group_plans.append((group_no, group_id, group_name, kept_ads))
    if not group_plans:
        _fail("copy_material_selection_invalid", "所选素材没有可复制广告")
    campaign = observed_body("CAMPAIGN", campaign)
    budgets = [
        Decimal(body["budget"])
        for body in contract_groups.values()
        if body.get("budget") is not None
    ]
    bids = [
        Decimal(body["roas_bid"])
        for body in contract_groups.values()
        if body.get("roas_bid") is not None
    ]
    # preview预算只用于既有任务摘要，真实每组预算始终保存在独立复制合同。
    budget = Decimal(campaign["budget"]) if budget_strategy == "SERIES" else budgets[0]
    target_roas = bids[0] if bids else None
    bid_strategy = "TARGET_ROAS" if bids else "HIGHEST_VALUE"
    config = {
        "currency": access.currency,
        "campaign_copy_version": 2,
        "copy_group_settings": [
            group_setting(
                {**contract_groups[str(group_id)], "adgroup_name": group_name},
                scene.field_constraints.get("target_regions", []),
            )
            for _, group_id, group_name, _ in group_plans
        ],
        "copy_daily_budget_sum": str(
            Decimal(campaign["budget"]) if budget_strategy == "SERIES" else sum(budgets)
        ),
        "budget_strategy": budget_strategy,
        "bid_strategy": bid_strategy,
        "creation_status": "ENABLE",
        "schedule_type": "SCHEDULE_START_END"
        if delivery["schedule_mode"] == "START_END"
        else "SCHEDULE_FROM_NOW",
        "schedule_start_time": delivery["schedule_start_time"],
        "schedule_end_time": delivery["schedule_end_time"],
        "creative_count": 1,
        "group_generation_mode": "FIXED",
        "ad_generation_mode": "FIXED",
    }
    preview = BuildPreview(
        id=preview_id,
        tenant_id=context.tenant_id,
        bc_id=bc_id,
        draft_id=draft_id,
        draft_revision=1,
        strategy_version_id=draft.strategy_version_id,
        actor_id=context.actor_id,
        batch_short_id="copy-" + request_id.hex,
        local_date=datetime.now(UTC).strftime("%Y%m%d"),
        config=config,
        budget=budget,
        target_roas=target_roas,
    )
    # batch_short_id最大32字符，使用整个确定UUID的前27位并带明确复制前缀。
    preview.batch_short_id = "copy-" + request_id.hex[:27]
    session.add(preview)
    session.flush()
    save_preview_route(session, context=context, preview_id=preview_id, route=route)
    session.add(PreviewContract(**scope, version=2, media_kind="VIDEO"))
    session.add(
        PreviewDrama(
            **scope,
            drama_id=drama_id,
            link_id=None,
            **{
                key: snapshot[key]
                for key in (
                    "title",
                    "provider_pinyin",
                    "external_drama_id",
                    "display_drama_id",
                    "url",
                    "protected_base",
                )
            },
        )
    )
    session.flush()
    scene_snapshot = scene.to_snapshot()
    scene_snapshot.update(budget_strategy=budget_strategy, bid_strategy=bid_strategy)
    scene_snapshot[COPY_KEY] = {
        "version": 2,
        "minis_id": snapshot["minis_id"],
        "campaign": campaign,
        "groups": contract_groups,
        "ads": {
            str(uuid5(request_id, f"ad:{ad['remote_id']}")): {
                "ad_text_list": ad["body"]["ad_text_list"],
                "ad_configuration": ad["body"]["ad_configuration"],
            }
            for _, _, _, ads in group_plans
            for ad, _ in ads
        },
        "delivery_config": delivery,
        "source_digest": snapshot["source_digest"],
        "source_campaign_id": source["campaign_id"],
        "source_advertiser_id": source["advertiser_id"],
        "source_route": snapshot["source_route"],
        "source_fingerprint": fingerprint(source),
    }
    unit = BuildUnit(
        **scope,
        id=unit_id,
        drama_id=drama_id,
        advertiser_id=advertiser_id,
        connection_id=route.connection_id,
        currency=access.currency,
        timezone=access.timezone,
        campaign_name=campaign_name,
        campaign_digest=digest(campaign_name),
        scene_snapshot=scene_snapshot,
        readiness="READY"
        if all(state == "ready" for state in readiness)
        else "PREPARING",
        group_count=len(group_plans),
        ad_count=sum(len(item[3]) for item in group_plans),
        complete=True,
    )
    session.add(unit)
    session.flush()
    creative_ids = {}
    for position, (mid, material) in enumerate(sorted(local_materials.items()), 1):
        creative_id = uuid5(request_id, f"material:{mid}")
        creative_ids[mid] = creative_id
        session.add(
            PreviewCreativeUnit(
                **scope,
                id=creative_id,
                drama_id=drama_id,
                kind="VIDEO",
                position=position,
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
                creative_unit_id=creative_id,
                position=1,
                drama_id=drama_id,
                material_id=material.id,
                content_digest=material.sha256,
                file_name=material.file_name,
            )
        )
    for group_no, group_id, group_name, ads in group_plans:
        session.add(PreviewDramaGroup(**scope, drama_id=drama_id, group_no=group_no))
        session.flush()
        session.add(
            PlannedGroup(
                **scope,
                id=group_id,
                unit_id=unit_id,
                drama_id=drama_id,
                group_no=group_no,
                name=group_name,
            )
        )
        group_mids = list(dict.fromkeys(mid for _, mids in ads for mid in mids))
        for position, mid in enumerate(group_mids, 1):
            session.add(
                PreviewGroupCreativeUnit(
                    **scope,
                    drama_id=drama_id,
                    group_no=group_no,
                    position=position,
                    creative_unit_id=creative_ids[mid],
                )
            )
        session.flush()
        for ad_no, (ad, mids) in enumerate(ads, 1):
            session.add(
                PlannedAd(
                    **scope,
                    id=uuid5(request_id, f"ad:{ad['remote_id']}"),
                    group_id=group_id,
                    base_ad_no=ad_no,
                    creative_no=1,
                    name=ad["body"]["ad_name"] + suffix,
                    copy_id=None,
                    text=ad["body"]["ad_text_list"][0]["ad_text"],
                    cta_option_ids=cta_ids,
                )
            )
            for position, mid in enumerate(mids, 1):
                session.add(
                    PreviewAdCreativeUnit(
                        **scope,
                        drama_id=drama_id,
                        group_no=group_no,
                        base_ad_no=ad_no,
                        position=position,
                        creative_unit_id=creative_ids[mid],
                    )
                )
    # 子行先在 BUILDING 状态完整落库，再触发数据库的冻结完整性检查。
    session.flush()
    preview.content_digest = digest(
        [intent_digest, scene_snapshot, campaign_name, group_plans]
    )
    preview.status = "FROZEN"
    preview.progress = {"phase": "complete"}
    draft.status = "READY"
    session.add(preview)
    session.add(draft)
    session.flush()
    return preview_id


def validate_copy_submission(
    session: Session, *, context, preview: BuildPreview
) -> None:
    """原提交入口也复核复制来源，防止绕过父批次直接提交过期子预览。"""
    if preview.config.get("campaign_copy_version") not in {1, 2}:
        return
    if preview.config.get("campaign_copy_version") == 1:
        _fail(
            "copy_source_contract_obsolete",
            "复制来源读取方式已更新，请回到批量复制重新读取来源",
        )
    units = session.exec(
        select(BuildUnit)
        .where(
            BuildUnit.tenant_id == context.tenant_id, BuildUnit.preview_id == preview.id
        )
        .limit(2)
    ).all()
    if len(units) != 1:
        _fail("copy_contract_invalid", "复制子预览层级不完整")
    contract = units[0].scene_snapshot.get(COPY_KEY, {})
    if contract.get("version") not in {1, 2} or not all(
        contract.get(key)
        for key in (
            "source_route",
            "source_advertiser_id",
            "source_campaign_id",
            "source_fingerprint",
        )
    ):
        _fail("copy_contract_invalid", "复制来源冻结合同不完整")
    route = FrozenTikTokRoute.model_validate(contract["source_route"])
    verify_route(
        session,
        context=context,
        route=route,
        advertiser_id=contract["source_advertiser_id"],
        capability="read",
    )
    current = load_copy_source(
        session,
        context=context,
        bc_id=preview.bc_id,
        advertiser_id=contract["source_advertiser_id"],
        campaign_id=contract["source_campaign_id"],
    )
    if current["blockers"] or fingerprint(current) != contract["source_fingerprint"]:
        _fail("copy_source_changed", "来源系列或关联已改变，请重新生成复制预览")
