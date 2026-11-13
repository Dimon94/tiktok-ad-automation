"""原生冻结意图在既有提交/执行器中的准入与编译。无网络和独立执行状态机。"""

from datetime import UTC, datetime
from uuid import UUID

from sqlmodel import select

from app.core.errors import DomainError
from app.modules.builds.models import BuildDraft
from app.modules.builds.native_preview import observe_occupancy
from app.modules.builds.preview_models import BuildUnit, PlannedAd
from app.modules.builds.request_compiler import cta_portfolio
from app.modules.builds.scene_jobs import native_checkpoint

from .selection_schemas import NativeSeriesPlanSet


def lock_send_authority(session, *, context, claim):
    """当前发送与数据库撤权串行；共享锁允许兄弟账户并行读取同一授权。"""
    from app.models import User
    from app.modules.accounts.connection_models import (
        BCConnectionBinding,
        ConnectionAuthorization,
    )
    from app.modules.accounts.models import (
        AdvertiserAccount,
        BCAccountAccess,
        TenantBC,
        TikTokConnection,
    )
    from app.modules.tenants.models import Tenant, TenantMembership

    from .preview_models import BuildPreview

    route = claim.route
    preview = session.get(BuildPreview, claim.preview_id)
    rows = (
        (User, context.actor_id),
        (Tenant, context.tenant_id),
        (TenantMembership, (context.tenant_id, context.actor_id)),
        (TenantBC, (context.tenant_id, route.bc_id)),
        (TikTokConnection, route.connection_id),
        (BCConnectionBinding, (context.tenant_id, route.bc_id, route.connection_id)),
        (AdvertiserAccount, (context.tenant_id, claim.advertiser_id)),
        (
            BCAccountAccess,
            (context.tenant_id, route.bc_id, claim.advertiser_id, route.connection_id),
        ),
        (BuildDraft, preview.draft_id),
        (BuildPreview, claim.preview_id),
    )
    for model, key in rows:
        session.get(model, key, populate_existing=True, with_for_update={"read": True})
    session.exec(
        select(ConnectionAuthorization)
        .where(
            ConnectionAuthorization.tenant_id == context.tenant_id,
            ConnectionAuthorization.connection_id == route.connection_id,
            ConnectionAuthorization.authorization_revision
            == route.authorization_revision,
        )
        .with_for_update(read=True)
    ).all()


def validate(session, *, context, preview, route, refreshing=False, unit_id=None):
    # 整批摘要绑定批准范围；资格与素材只复核当前 Unit，兄弟计划独立推进。
    if preview.tenant_id != context.tenant_id or preview.bc_id != route.bc_id:
        raise DomainError("execution_intent_changed", "冻结租户或BC不一致")
    draft = session.get(BuildDraft, preview.draft_id, populate_existing=True)
    if draft.revision != preview.draft_revision:
        raise DomainError("draft_revision_conflict", "草稿已变更，请重新预览")
    if preview.status != "FROZEN":
        raise DomainError("preview_not_frozen", "原生预览未冻结")
    units = session.exec(
        select(BuildUnit).where(
            BuildUnit.preview_id == preview.id, BuildUnit.tenant_id == context.tenant_id
        )
    ).all()
    # 新旧原生冻结都经同一读取器；历史平铺只在读取边界适配，不回写。
    from .native_preview import read_batch

    batch = read_batch(session, preview=preview)
    item = next(
        (p for p in batch["plans"] if p.get("unit_id") == str(unit_id)), None
    )
    if item is None:
        raise DomainError("execution_intent_changed", "当前组合不在批准范围")
    plan_key = UUID(item["plan_key"])
    plan = next(
        (
            p
            for p in NativeSeriesPlanSet.model_validate(draft.scene_selection).plans
            if p.plan_key == plan_key
        ),
        None,
    )
    if plan is None:
        raise DomainError("execution_intent_changed", "冻结计划已不在当前输入")
    payload = item["request"]
    selection = plan.selection.model_dump(mode="json")
    unit = next(u for u in units if u.id == unit_id)
    if payload["draft_revision"] != draft.revision:
        raise DomainError("draft_revision_conflict", "草稿已变更，请重新预览")
    if (
        payload["route"] != route.model_dump(mode="json")
        or payload["selection"] != selection
        or unit.scene_snapshot.get("native_request") != payload
    ):
        raise DomainError("execution_intent_changed", "冻结目的地或路由已改变")
    from app.modules.accounts.access import resolve_account_access
    from app.modules.accounts.routing import verify_route

    verify_route(
        session,
        context=context,
        route=route,
        advertiser_id=payload["selection"]["advertiser_id"],
        capability="build",
    )
    access = resolve_account_access(
        session,
        context=context,
        bc_id=preview.bc_id,
        advertiser_id=payload["selection"]["advertiser_id"],
        action="build",
        connection_id=route.connection_id,
    )
    if (access.currency, access.timezone) != (unit.currency, unit.timezone):
        raise DomainError("account_metadata_changed", "账户币种或时区已改变")
    checkpoint = native_checkpoint(
        session, context=context, draft=draft, route=route, plan_key=plan_key
    )
    expired_only = bool(checkpoint.reason_codes) and set(checkpoint.reason_codes) <= {
        "scene_evidence_expired",
        "account_build_evidence_expired",
    }
    if expired_only or checkpoint.state == "PENDING":
        if refreshing:
            return payload
        raise DomainError(
            "scene_refresh_required", "原生证据已过期，等待同范围刷新", retryable=True
        )
    if checkpoint.state != "READY" or not checkpoint.materials_ready:
        raise DomainError("native_preview_not_ready", "当前原生证据尚未就绪")
    # 新证据 ID 可以变化；目标 VID/封面和素材顺序必须与冻结意图一致。
    keys = ("material_id", "video_id", "image_id", "cover_material_id")
    current = checkpoint.model_dump(mode="json")
    if [[m.get(k) for k in keys] for m in current["materials"]] != [
        [m.get(k) for k in keys] for m in payload["materials"]
    ]:
        raise DomainError("execution_intent_changed", "冻结素材映射已改变")
    locations = payload["adgroup"]["location_ids"]
    current_locations = sorted(
        x["location_id"]
        for x in checkpoint.facts["regions"]["locations"]
        if x["region_code"] in payload["selection"]["regions"]
    )
    if current_locations != sorted(locations) or checkpoint.facts.get("cta") != payload[
        "checkpoint"
    ]["facts"].get("cta"):
        raise DomainError("scene_intent_changed", "当前地区或CTA与冻结意图不一致")
    occupancy = observe_occupancy(
        session,
        context=context,
        bc_id=preview.bc_id,
        selection=checkpoint.selection,
        location_ids=locations,
        route=route,
    )
    from .execution_models import ExecutionStep, Submission

    # 本次已创建对象属于本计划；目录后来观察到其 ACTIVE 不能成为自己的冲突。
    own_ids = set(
        session.exec(
            select(ExecutionStep.remote_id)
            .join(Submission, Submission.id == ExecutionStep.submission_id)
            .where(
                Submission.preview_id == preview.id,
                Submission.tenant_id == context.tenant_id,
                ExecutionStep.tenant_id == context.tenant_id,
                ExecutionStep.unit_id == unit.id,
                ExecutionStep.kind.in_(["CAMPAIGN", "ADGROUP"]),
                ExecutionStep.remote_id.is_not(None),
            )
        ).all()
    )
    occupancy["visible_conflicts"] = [
        c for c in occupancy["visible_conflicts"] if c["remote_id"] not in own_ids
    ]
    if any(
        c["state"] == "ACTIVE"
        for facts in (payload["occupancy"], occupancy)
        for c in facts["visible_conflicts"]
    ):
        raise DomainError(
            "native_series_active_conflict", "Series和地区存在已知活跃占用"
        )
    return payload


def prepare(session, *, step, frozen, parent_id=None):
    p = frozen.native_request
    common = {"advertiser_id": frozen.advertiser_id}
    if step.kind == "CTA":
        return cta_portfolio(
            advertiser_id=frozen.advertiser_id,
            assets=p["ads"][0]["cta"]["recommend_assets"],
        )
    if step.kind == "CAMPAIGN":
        c = p["campaign"]
        return {
            **common,
            "campaign_name": c["name"],
            "objective_type": c["objective_type"],
            "app_promotion_type": c["app_promotion_type"],
            "budget_mode": c["budget_mode"],
            "budget": c["budget"],
            "budget_optimize_on": True,
            "operation_status": "ENABLE",
        }
    if step.kind == "ADGROUP":
        g = p["adgroup"]
        fields = (
            "promotion_type",
            "native_series_id",
            "placement_type",
            "placements",
            "optimization_goal",
            "optimization_event",
            "deep_bid_type",
            "vbo_window",
            "bid_type",
            "targeting_spec",
            "schedule_type",
        )
        result = {
            **common,
            **{k: g[k] for k in fields},
            **g["series_identity"],
            "campaign_id": parent_id,
            "adgroup_name": g["name"],
            "roas_bid": g["target_roas"],
            "operation_status": "ENABLE",
            "schedule_start_time": g["schedule_start_time"]
            or datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
        }
        if g["schedule_end_time"]:
            result["schedule_end_time"] = g["schedule_end_time"]
        return result
    ad = session.get(PlannedAd, (step.tenant_id, step.preview_id, step.planned_ad_id))
    a = next(
        x
        for x in p["ads"]
        if (x["base_ad_no"], x["creative_no"]) == (ad.base_ad_no, ad.creative_no)
    )
    from .execution_models import ExecutionStep

    cta = session.exec(
        select(ExecutionStep).where(
            ExecutionStep.submission_id == step.submission_id,
            ExecutionStep.unit_id == step.unit_id,
            ExecutionStep.kind == "CTA",
        )
    ).one()
    if cta.status != "SUCCEEDED" or not cta.remote_id:
        raise DomainError("cta_not_ready", "CTA尚未创建", retryable=True)
    return {
        **common,
        "adgroup_id": parent_id,
        "ad_name": ad.name,
        "operation_status": "ENABLE",
        "ad_text_list": [{"ad_text": a["text"]}],
        "ad_configuration": {"call_to_action_id": cta.remote_id},
        "creative_list": [
            {
                "creative_info": {
                    **a["creative_identity"],
                    "ad_format": "SINGLE_VIDEO",
                    "video_info": {"video_id": m["video_id"]},
                    "image_info": [{"web_uri": m["image_id"]}],
                }
            }
            for m in a["materials"]
        ],
    }


def validate_ad_assets(session, *, step, unit, body):
    """共用最终发送围栏的原生分支：先消费当前正证据，再逐位置核对实际 wire。"""
    from app.core.context import TenantContext

    from .execution_models import Submission
    from .preview_models import BuildPreview
    from .routes import verify_unit_route

    submission = session.get(Submission, step.submission_id)
    context = TenantContext(
        tenant_id=step.tenant_id, actor_id=submission.actor_id, role="operator"
    )
    route = verify_unit_route(session, context=context, unit=unit, capability="build")
    payload = validate(
        session,
        context=context,
        preview=session.get(BuildPreview, step.preview_id),
        route=route,
        unit_id=unit.id,
    )
    ad = session.get(PlannedAd, (step.tenant_id, step.preview_id, step.planned_ad_id))
    selected = next(
        a
        for a in payload["ads"]
        if (a["base_ad_no"], a["creative_no"]) == (ad.base_ad_no, ad.creative_no)
    )
    expected = [
        {
            "creative_info": {
                **{
                    k: v
                    for k, v in selected["creative_identity"].items()
                    if v is not None
                },
                "ad_format": "SINGLE_VIDEO",
                "video_info": {"video_id": m["video_id"]},
                "image_info": [{"web_uri": m["image_id"]}],
            }
        }
        for m in selected["materials"]
    ]
    if body.get("creative_list") != expected:
        raise DomainError(
            "execution_intent_changed", "实际视频位置或封面与冻结内容不一致"
        )
