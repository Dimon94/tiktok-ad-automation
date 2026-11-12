"""原生冻结意图在既有提交/执行器中的准入与编译。无网络和独立执行状态机。"""

from datetime import UTC, datetime

from sqlmodel import select

from app.core.errors import DomainError
from app.modules.builds.frozen_requests import (
    FrozenRequestRevision,
    native_request_digest,
)
from app.modules.builds.models import BuildDraft
from app.modules.builds.native_preview import observe_occupancy
from app.modules.builds.preview_models import BuildUnit, PlannedAd
from app.modules.builds.request_compiler import cta_portfolio
from app.modules.builds.scene_jobs import native_checkpoint


def validate(session, *, context, preview, route, refreshing=False):
    payload = FrozenRequestRevision.model_validate(
        preview.config["native_request"]
    ).model_dump(mode="json")
    draft = session.get(BuildDraft, preview.draft_id, populate_existing=True)
    if (
        preview.status != "FROZEN"
        or native_request_digest(payload) != preview.content_digest
    ):
        raise DomainError("preview_not_frozen", "原生冻结内容摘要不匹配")
    if (
        draft.revision != preview.draft_revision
        or payload["draft_revision"] != draft.revision
    ):
        raise DomainError("draft_revision_conflict", "草稿已变更，请重新预览")
    if (
        payload["route"] != route.model_dump(mode="json")
        or payload["selection"] != draft.scene_selection
    ):
        raise DomainError("execution_intent_changed", "冻结目的地或路由已改变")
    units = session.exec(
        select(BuildUnit).where(
            BuildUnit.preview_id == preview.id, BuildUnit.tenant_id == context.tenant_id
        )
    ).all()
    if len(units) != 1 or units[0].scene_snapshot.get("native_request") != payload:
        raise DomainError("execution_intent_changed", "冻结组合内容不一致")
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
    if (access.currency, access.timezone) != (units[0].currency, units[0].timezone):
        raise DomainError("account_metadata_changed", "账户币种或时区已改变")
    checkpoint = native_checkpoint(session, context=context, draft=draft, route=route)
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
