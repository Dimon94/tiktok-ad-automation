"""One bounded create or local material dependency per committed attempt."""

from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Any
from uuid import UUID, uuid4

from billiard.process import current_process  # type: ignore[import-untyped]
from celery import current_task  # type: ignore[import-untyped]
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.diagnostics import emit, error_fields, span
from app.core.errors import DomainError
from app.integrations.tiktok.bounded_resources import bounded_session
from app.integrations.tiktok.contracts.builds import (
    CreatedObject,
)
from app.integrations.tiktok.contracts.common import (
    CallEvidence,
    RemoteCallError,
)
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.modules.accounts.access import resolve_account_access
from app.modules.accounts.routing import verify_route
from app.modules.builds.execution_admission import HARD_LIMIT, admitted_build_call
from app.modules.builds.execution_models import ExecutionStep, Submission
from app.modules.builds.execution_schemas import StepClaim
from app.modules.builds.execution_state import (
    active_attempt,
    arm_request,
    evidence,
    finish_local,
    preserve_created_receipt,
    record_created,
    record_not_sent,
    record_rejected,
    record_unknown,
    transient_retry,
)
from app.modules.builds.frozen_requests import cta_request, parent_request
from app.modules.builds.preview_materials import frozen_ad_material_ids
from app.modules.builds.preview_models import (
    PlannedAd,
    PlannedGroup,
)
from app.modules.builds.preview_schemas import FrozenUnit
from app.modules.builds.request_compiler import (
    ad_assets,
    compile_request,
    create_arguments,
    decode_intent,
    remote_request_id,
)
from app.modules.builds.routes import load_preview_route
from app.modules.builds.scene import read_scene_context
from app.modules.builds.scene_jobs import ensure_scene_preparation
from app.modules.builds.submissions import claim_step, load_execution_unit
from app.modules.materials.distribution import ensure_target_asset
from app.modules.tenants.permissions import require_tenant

SCENE_FIELDS = (
    "campaign_fields",
    "adgroup_fields",
    "creative_fields",
    "cta_fields",
    "field_constraints",
    "name_limit",
    "creative_limit",
    "copy_length_limit",
)


def _require_bounded_worker() -> None:
    task = current_task
    limit = (
        (
            (task.request.timelimit or (None, None))[0]
            or getattr(task, "time_limit", None)
        )
        if task
        else None
    )
    if (
        not task
        or not current_process().daemon
        or not current_process().name.startswith("ForkPoolWorker-")
        or task.request.called_directly
        or task.request.is_eager
        or isinstance(limit, bool)
        or not isinstance(limit, (int, float))
        or not 0 < limit <= HARD_LIMIT
    ):
        raise DomainError("build_worker_unbounded", "广告执行需要有界后台任务")


def _frozen(session: Session, context: TenantContext, claim: StepClaim) -> FrozenUnit:
    unit = load_execution_unit(
        session,
        context=context,
        submission_id=claim.submission_id,
        unit_id=claim.unit_id,
    )
    if unit.actor_id != context.actor_id:
        raise DomainError("action_forbidden", "提交操作者不匹配")
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="build"
    )
    route = load_preview_route(session, context=context, preview_id=claim.preview_id)
    if route != claim.route:
        raise DomainError("frozen_route_changed", "执行尝试路线不一致")
    verify_route(
        session,
        context=context,
        route=route,
        advertiser_id=claim.advertiser_id,
        capability="build",
    )
    access = resolve_account_access(
        session,
        context=context,
        bc_id=claim.bc_id,
        advertiser_id=claim.advertiser_id,
        action="build",
        connection_id=route.connection_id,
    )
    if access.connection_id != unit.frozen.connection_id:
        raise DomainError("account_authorization_changed", "冻结账户授权已变化")
    if (access.currency, access.timezone) != (
        unit.frozen.currency,
        unit.frozen.timezone,
    ):
        raise DomainError("account_metadata_changed", "账户币种或时区已变化")
    return unit.frozen


def _current_scene(
    session: Session, context: TenantContext, frozen: FrozenUnit
) -> None:
    creative_info = frozen.scene_snapshot.get("creative_fields", {}).get(
        "creative_info", {}
    )
    selected_identity = {
        key: creative_info[key]
        for key in (
            "identity_id",
            "identity_type",
            "identity_authorized_bc_id",
        )
        if creative_info.get(key) is not None
    }
    scene = read_scene_context(
        session,
        context=context,
        bc_id=frozen.bc_id,
        advertiser_id=frozen.advertiser_id,
        link_id=frozen.link_id,
        route=load_preview_route(
            session, context=context, preview_id=frozen.preview_id
        ),
        selected_identity=selected_identity or None,
        media_kind=frozen.media_kind,
    )
    targeting = frozen.scene_snapshot.get("field_constraints", {}).get(
        "audience_targeting"
    )
    if targeting is not None:
        from .targeting import apply_targeting
        from .targeting_schemas import AudienceTargeting

        # 执行重核只应用已冻结意图，不读取后来修改的策略或扩展国家集合。
        scene = apply_targeting(
            scene,
            AudienceTargeting.model_validate(targeting),
            list(frozen.scene_snapshot["field_constraints"]["selected_region_codes"]),
        )
    if not scene.supported:
        transient = bool(
            set(scene.reason_codes)
            & {
                "scene_evidence_missing",
                "scene_evidence_expired",
                "account_scope_unverified",
                "account_build_unverified",
            }
        )
        raise DomainError(
            "scene_refresh_required" if transient else "scene_no_longer_supported",
            "当前账户投放信息需要重新核实",
            retryable=transient,
        )
    snapshot = scene.to_snapshot()
    # 场景重核只更新远端证据；业务策略沿用预览冻结值。
    snapshot["budget_strategy"] = frozen.budget_strategy
    snapshot["bid_strategy"] = frozen.bid_strategy

    # New evidence IDs alone must not change frozen business intent. New facts do.
    def comparable(value: dict[str, Any], key: str) -> object:
        item = value.get(key)
        if key == "field_constraints" and isinstance(item, dict):
            item = dict(item)
            # 规则版本和已退役的本地能力列表不是用户投放意图；
            # 实际定向、身份、预算上下限等字段仍逐项严格比较。
            item.pop("revision", None)
            item.pop("targeting_optimization_modes", None)
        # 旧 Minis 快照的 catalog=false 是冗余参数，移除它不改变投放意图。
        if key == "campaign_fields" and isinstance(item, dict):
            item = dict(item)
            if item.get("catalog_enabled") is False:
                item.pop("catalog_enabled")
        if key == "adgroup_fields" and isinstance(item, dict):
            item = dict(item)
            if item.get("optimization_event") == "IMPRESSION_LEVEL_AD_REVENUE":
                item.setdefault("vbo_window", "ZERO_DAY")
        return item

    if any(
        comparable(snapshot, key) != comparable(frozen.scene_snapshot, key)
        for key in SCENE_FIELDS
    ):
        raise DomainError(
            "scene_intent_changed", "当前投放条件与预览不一致，请重新生成预览"
        )


def _parent(session: Session, step: ExecutionStep) -> str:
    parent = (
        session.get(ExecutionStep, step.parent_step_id) if step.parent_step_id else None
    )
    if (
        parent is None
        or parent.tenant_id != step.tenant_id
        or parent.submission_id != step.submission_id
        or parent.status != "SUCCEEDED"
        or not parent.remote_id
    ):
        raise DomainError("parent_not_ready", "上级对象尚未创建", retryable=True)
    return parent.remote_id


def _group(session: Session, step: ExecutionStep) -> PlannedGroup:
    group = session.exec(
        select(PlannedGroup).where(
            PlannedGroup.tenant_id == step.tenant_id,
            PlannedGroup.preview_id == step.preview_id,
            PlannedGroup.unit_id == step.unit_id,
            PlannedGroup.id == step.group_id,
        )
    ).one_or_none()
    if group is None:
        raise DomainError("resource_not_found", "冻结广告组不存在")
    return group


def prepare_request(
    session: Session, *, context: TenantContext, claim: StepClaim, frozen: FrozenUnit
) -> dict[str, Any]:
    """Only local reads / durable material preparation; never calls an SDK."""
    step = session.get(ExecutionStep, claim.step_id)
    assert step
    snapshot = frozen.scene_snapshot
    if step.kind == "CTA":
        first = session.exec(
            select(PlannedAd)
            .join(
                PlannedGroup,
                (col(PlannedGroup.tenant_id) == PlannedAd.tenant_id)
                & (col(PlannedGroup.id) == PlannedAd.group_id),
            )
            .where(
                PlannedAd.tenant_id == step.tenant_id,
                PlannedGroup.unit_id == step.unit_id,
            )
            .order_by(col(PlannedAd.id))
            .limit(1)
        ).first()
        if first is None:
            raise DomainError("cta_unavailable", "冻结创意缺少 CTA")
        return cta_request(frozen, set(first.cta_option_ids))

    if step.kind in {"CAMPAIGN", "ADGROUP"}:
        return parent_request(
            frozen,
            kind=step.kind,
            name=frozen.campaign_name
            if step.kind == "CAMPAIGN"
            else _group(session, step).name,
            parent_id=_parent(session, step) if step.kind == "ADGROUP" else None,
        )
    fixed: dict[str, Any] = {"advertiser_id": frozen.advertiser_id}
    resolved: dict[str, Any]
    if step.kind == "AD":
        group = _group(session, step)
        ad = session.exec(
            select(PlannedAd).where(
                PlannedAd.tenant_id == step.tenant_id,
                PlannedAd.group_id == group.id,
                PlannedAd.id == step.planned_ad_id,
            )
        ).one_or_none()
        if ad is None:
            raise DomainError("resource_not_found", "冻结创意不存在")
        identity = {
            key: value
            for key, value in snapshot["creative_fields"]["creative_info"].items()
            if key != "ad_format"
        }
        if frozen.media_kind == "CAROUSEL":
            from .carousel_readiness import resolve_carousel_assets
            from .creative_units import frozen_ad_creative_units

            units = frozen_ad_creative_units(
                session,
                tenant_id=step.tenant_id,
                preview_id=step.preview_id,
                drama_id=frozen.drama_id,
                group_no=group.group_no,
                base_ad_no=ad.base_ad_no,
            )
            assets = resolve_carousel_assets(
                session,
                context=context,
                route=claim.route,
                advertiser_id=claim.advertiser_id,
                units=units,
                prepare_missing=True,
                attempt_id=claim.attempt_id,
            )
        else:
            from app.integrations.tiktok.contracts.builds import VideoCreative

            material_ids = frozen_ad_material_ids(
                session,
                tenant_id=step.tenant_id,
                preview_id=step.preview_id,
                drama_id=frozen.drama_id,
                group_no=group.group_no,
                base_ad_no=ad.base_ad_no,
                unit_id=step.unit_id,
            )[:51]
            if not 1 <= len(material_ids) <= 50:
                raise DomainError("invalid_material_group", "冻结素材组无效")
            mappings = _ready_ad_mappings(
                session, context=context, claim=claim, material_ids=material_ids
            )
            if mappings is None:
                mappings = _prepare_ad_mappings(
                    session, context=context, claim=claim, material_ids=material_ids
                )
            assets = [
                VideoCreative(
                    video_id=item["video_id"],
                    cover_image_id=item.get("image_id"),
                    file_name=item.get("file_name"),
                )
                for item in mappings
            ]
        resolved = ad_assets(assets, text=ad.text, url=frozen.url, identity=identity)
        cta = session.exec(
            select(ExecutionStep).where(
                ExecutionStep.tenant_id == step.tenant_id,
                ExecutionStep.submission_id == step.submission_id,
                ExecutionStep.unit_id == step.unit_id,
                ExecutionStep.kind == "CTA",
            )
        ).one()
        if cta.status != "SUCCEEDED" or not cta.remote_id:
            raise DomainError("cta_not_ready", "CTA 尚未准备完成", retryable=True)
        resolved["ad_configuration"] = {"call_to_action_id": cta.remote_id}
        fixed.update(
            adgroup_id=_parent(session, step),
            ad_name=ad.name,
            creation_status=frozen.creation_status,
        )
    else:
        raise DomainError("invalid_build_kind", "该步骤不是广告创建请求")
    return compile_request(step.kind.lower(), fixed=fixed, resolved=resolved)


def _prepare_ad_mappings(
    session: Session,
    *,
    context: TenantContext,
    claim: StepClaim,
    material_ids: list[UUID],
) -> list[dict[str, Any]]:
    """加锁准备：为缺失的目标素材/封面登记准备任务；全部就绪时返回创意素材。"""
    from app.modules.materials.cover_models import MaterialCoverJob
    from app.modules.materials.models import AccountMaterial, MaterialFile
    from app.modules.materials.readiness import load_material

    # 批量素材核验按 ID 加锁。广告仍按冻结的创意顺序编译，但必须先按
    # 同一 ID 顺序取得整组素材锁，避免逐项准备与核验形成相反锁序的死锁。
    session.exec(
        select(MaterialFile)
        .where(
            MaterialFile.tenant_id == context.tenant_id,
            col(MaterialFile.id).in_(material_ids),
        )
        .order_by(col(MaterialFile.id))
        .with_for_update()
    ).all()
    # 素材锁顺序不能代替封面锁顺序：创意排列与job.id无关。
    # 先统一锁住当前目标VID的既有封面，再按原顺序编译；与批读及
    # 广告刷新同序，且不锁历史VID、其他账户或其他租户的封面。
    session.exec(
        select(MaterialCoverJob)
        .join(
            AccountMaterial,
            (col(AccountMaterial.id) == MaterialCoverJob.asset_id)
            & (col(AccountMaterial.tenant_id) == MaterialCoverJob.tenant_id)
            & (col(AccountMaterial.video_id) == MaterialCoverJob.video_id),
        )
        .where(
            MaterialCoverJob.tenant_id == context.tenant_id,
            MaterialCoverJob.bc_id == claim.bc_id,
            MaterialCoverJob.advertiser_id == claim.advertiser_id,
            MaterialCoverJob.connection_id == claim.route.connection_id,
            col(MaterialCoverJob.material_id).in_(material_ids),
        )
        .order_by(col(MaterialCoverJob.id))
        .with_for_update(of=MaterialCoverJob)
    ).all()
    mappings = []
    waiting = False
    for material_id in material_ids:
        ready = ensure_target_asset(
            session,
            context=context,
            bc_id=claim.bc_id,
            material_id=material_id,
            advertiser_id=claim.advertiser_id,
            task_key=f"build:{claim.step_id}:{material_id}",
            route=claim.route,
        )
        if ready.state == "blocked":
            raise DomainError(
                ready.reason_code or "target_asset_unavailable", "目标素材不可用"
            )
        if ready.state != "ready" or not ready.mapping:
            waiting = True
            continue
        # 当前 Smart+ 单视频接口要求恰好一张封面。目标账户素材已带
        # 核实图片时直接复用；不要让历史封面任务的连接路由阻断消费。
        if ready.mapping and ready.mapping.image_id:
            cover = ready
        else:
            from app.modules.materials.covers import ensure_cover

            cover = ensure_cover(
                session,
                context=context,
                bc_id=claim.bc_id,
                material_id=material_id,
                advertiser_id=claim.advertiser_id,
                task_key=f"build-cover:{claim.step_id}:{material_id}",
                route=claim.route,
            )
        if cover.state == "blocked":
            job = (
                session.get(MaterialCoverJob, cover.task_id) if cover.task_id else None
            )
            # 结果未知或发送前瞬时失败的封面由自动接续核查/重排；广告等待，
            # 不能把一次本地拥塞变成需要人工重试的广告失败。
            if job is not None and _cover_recovering(job):
                waiting = True
                continue
            raise DomainError(
                cover.reason_code or "target_asset_incomplete",
                "目标视频封面尚未核实",
            )
        if cover.state != "ready" or not cover.mapping:
            waiting = True
            continue
        mappings.append(
            {
                "video_id": cover.mapping.video_id,
                "file_name": load_material(
                    session,
                    context=context,
                    bc_id=claim.bc_id,
                    material_id=material_id,
                ).file_name,
                "image_id": cover.mapping.image_id or "",
            }
        )
    if waiting:
        # Caller commits the queued dependencies before waiting.
        raise DomainError(
            "material_refresh_required", "正在核实目标素材", retryable=True
        )
    return mappings


# 封面任务仍在推进（排队/准备/核查中），或处于自动接续范围内的 UNKNOWN/瞬时 BLOCKED。
_COVER_IN_PROGRESS = frozenset({"PENDING", "PREPARING", "VERIFYING"})


def _cover_recovering(job: Any) -> bool:
    from app.modules.materials.covers import (
        AUTO_RECOVERY_AGE,
        AUTO_RETRY_BLOCKED_CODES,
    )

    if job.superseded_by_id is not None:
        return False
    if job.status in _COVER_IN_PROGRESS:
        return True
    # 超出自动接续时长后按原规则落定失败，避免广告无限等待。
    recovery_started = max(job.created_at, job.request_armed_at or job.created_at)
    if recovery_started < datetime.now(UTC) - AUTO_RECOVERY_AGE:
        return False
    if job.status == "UNKNOWN":
        return job.error_code != "cover_receipt_ambiguous"
    return job.status == "BLOCKED" and job.error_code in AUTO_RETRY_BLOCKED_CODES


def _ready_ad_mappings(
    session: Session,
    *,
    context: TenantContext,
    claim: StepClaim,
    material_ids: list[UUID],
) -> list[dict[str, Any]] | None:
    """无锁快路径：目标素材与封面全部就绪时直接编译创意，不再逐条加锁准备。

    广告在等待封面期间每 15 秒重试一次。旧实现每次都对整组 48 条素材逐条
    ensure 并持有素材/封面行锁 8–15 秒，既占满单并发搭建 Worker，又让封面
    Worker 5 秒的锁等待超时失败。这里只读：就绪则返回；所有缺口都已有任务在
    推进时直接等待；只有确需登记新任务时才返回 None 进入加锁准备。最终发送前
    仍由 validate_ad_assets 在同一冻结身份下复核。
    """
    from app.modules.materials.cover_models import MaterialCoverJob
    from app.modules.materials.models import (
        AccountMaterial,
        MaterialDistribution,
        MaterialFile,
    )
    from app.modules.materials.readiness import mapping_fresh
    from app.modules.materials.routes import require_material_route

    require_material_route(
        session,
        context=context,
        route=claim.route,
        bc_id=claim.bc_id,
        advertiser_id=claim.advertiser_id,
        capability="build",
    )
    assets = {
        row.material_id: row
        for row in session.exec(
            select(AccountMaterial).where(
                AccountMaterial.tenant_id == context.tenant_id,
                AccountMaterial.bc_id == claim.bc_id,
                AccountMaterial.advertiser_id == claim.advertiser_id,
                col(AccountMaterial.material_id).in_(material_ids),
            )
        ).all()
    }
    missing = [
        material_id
        for material_id in material_ids
        if not (
            mapping_fresh(assets.get(material_id))
            and (assets[material_id].image_id or "").strip()
        )
    ]
    if not missing:
        names = dict(
            session.exec(
                select(MaterialFile.id, MaterialFile.file_name).where(
                    MaterialFile.tenant_id == context.tenant_id,
                    col(MaterialFile.id).in_(material_ids),
                )
            ).all()
        )
        return [
            {
                "video_id": assets[material_id].video_id,
                "file_name": names[material_id],
                "image_id": assets[material_id].image_id,
            }
            for material_id in material_ids
        ]
    covers = session.exec(
        select(MaterialCoverJob).where(
            MaterialCoverJob.tenant_id == context.tenant_id,
            MaterialCoverJob.bc_id == claim.bc_id,
            MaterialCoverJob.advertiser_id == claim.advertiser_id,
            col(MaterialCoverJob.material_id).in_(missing),
            col(MaterialCoverJob.superseded_by_id).is_(None),
        )
    ).all()
    covering = {
        job.material_id
        for job in covers
        if (asset := assets.get(job.material_id)) is not None
        and job.asset_id == asset.id
        and job.video_id == asset.video_id
        and _cover_recovering(job)
    }
    distributing = set(
        session.exec(
            select(MaterialDistribution.material_id).where(
                MaterialDistribution.tenant_id == context.tenant_id,
                MaterialDistribution.bc_id == claim.bc_id,
                MaterialDistribution.advertiser_id == claim.advertiser_id,
                col(MaterialDistribution.material_id).in_(missing),
                col(MaterialDistribution.superseded_by_id).is_(None),
                col(MaterialDistribution.status).in_(
                    ["queued", "preparing", "verifying"]
                ),
            )
        ).all()
    )
    if all(
        material_id in covering
        or (not mapping_fresh(assets.get(material_id)) and material_id in distributing)
        for material_id in missing
    ):
        emit(
            "ad_dependency_wait",
            tenant_id=str(context.tenant_id),
            submission_id=str(claim.submission_id),
            step_id=str(claim.step_id),
            missing_material_ids=[str(value) for value in missing],
            covers=[
                {
                    "job_id": str(job.id),
                    "batch_id": str(job.share_batch_id) if job.share_batch_id else None,
                    "status": job.status,
                    "error_code": job.error_code,
                }
                for job in covers
            ],
        )
        raise DomainError(
            "material_refresh_required", "正在等待目标素材与封面就绪", retryable=True
        )
    return None


def _local_result(
    database_engine: Any, claim: StepClaim, error: DomainError, *, delay: int = 15
) -> str:
    with Session(database_engine) as session, session.begin():
        step = session.exec(
            select(ExecutionStep)
            .where(
                ExecutionStep.id == claim.step_id,
                ExecutionStep.tenant_id == claim.tenant_id,
            )
            .with_for_update()
        ).one()
        if not active_attempt(step, claim, phase="CLAIMED"):
            return step.status
        if hasattr(error, "issues"):
            step.resolved = {**step.resolved, "carousel_issues": error.issues}
        retryable, delay, resolved = transient_retry(
            step.resolved,
            error=error,
            retryable=error.retryable,
            delay=delay,
            definitely_not_sent=True,
        )
        return finish_local(
            session,
            claim=claim,
            status="PENDING" if retryable else "FAILED",
            code=error.code,
            resolved=resolved,
            delay=delay,
        )


def _prepare_scene_dependency(
    database_engine: Any, context: TenantContext, claim: StepClaim
) -> str:
    """Commit resource preparation separately after the unarmed read transaction ends."""
    with Session(database_engine) as session, session.begin():
        step = session.exec(
            select(ExecutionStep)
            .where(
                ExecutionStep.id == claim.step_id,
                ExecutionStep.tenant_id == context.tenant_id,
            )
            .with_for_update()
        ).one()
        if not active_attempt(step, claim, phase="CLAIMED"):
            return step.status
        try:
            frozen = _frozen(session, context, claim)
            preparation = ensure_scene_preparation(
                session,
                context=context,
                bc_id=frozen.bc_id,
                advertiser_id=frozen.advertiser_id,
                link_id=frozen.link_id,
                route=claim.route,
                media_kind=frozen.media_kind,
                selected_identity={
                    key: frozen.scene_snapshot["creative_fields"]["creative_info"][key]
                    for key in (
                        "identity_id",
                        "identity_type",
                        "identity_authorized_bc_id",
                    )
                    if frozen.scene_snapshot.get("creative_fields", {})
                    .get("creative_info", {})
                    .get(key)
                    is not None
                }
                or None,
            )
            return finish_local(
                session,
                claim=claim,
                status="FAILED" if preparation.state == "blocked" else "PENDING",
                code=preparation.reason_code
                if preparation.state == "blocked"
                else "scene_refresh_required",
                delay=0 if preparation.state == "ready" else 15,
            )
        except DomainError as error:
            return finish_local(
                session,
                claim=claim,
                status="PENDING" if error.retryable else "FAILED",
                code=error.code,
                delay=15,
            )


def process_step(
    *,
    database_engine: Any,
    redis_client: Any,
    context: TenantContext,
    step_id: UUID,
    revision: int,
) -> str:
    """One effect maximum. Any uncertainty after arming is read-only recovery."""
    task_deadline = datetime.now(UTC) + timedelta(seconds=HARD_LIMIT)
    _require_bounded_worker()
    if type(revision) is not int or revision < 0:
        raise DomainError("dispatch_payload_invalid", "执行代际无效")
    from app.modules.builds.material_execution import (
        material_needs_planning,
        plan_material_slice,
    )

    with Session(database_engine) as session:
        candidate = session.get(ExecutionStep, step_id)
        unit_to_plan = (
            candidate.unit_id
            if candidate
            and candidate.tenant_id == context.tenant_id
            and candidate.dispatch_revision == revision
            and candidate.status in {"PENDING", "QUEUED", "RETRYABLE"}
            and material_needs_planning(session, context, candidate)
            else None
        )
    if unit_to_plan is not None:
        plan_material_slice(
            database_engine=database_engine, context=context, unit_id=unit_to_plan
        )
    with Session(database_engine) as session, session.begin():
        step = session.exec(
            select(ExecutionStep)
            .where(
                ExecutionStep.tenant_id == context.tenant_id,
                ExecutionStep.id == step_id,
            )
            .with_for_update()
        ).one_or_none()
        if step is None:
            raise DomainError("resource_not_found", "执行步骤不存在")
        submission = session.get(Submission, step.submission_id)
        if submission is None or submission.actor_id != context.actor_id:
            raise DomainError("action_forbidden", "提交操作者不匹配")
        if (
            step.dispatch_revision != revision
            or step.status in {"SUCCEEDED", "FAILED", "UNKNOWN"}
            or step.remote_id
            or step.phase == "REQUEST_ARMED"
        ):
            return step.status
        if step.kind == "READBACK":
            raise DomainError("invalid_build_kind", "回读步骤需要只读执行器")
        if step.kind == "MATERIAL" and step.status in {
            "PENDING",
            "QUEUED",
            "RETRYABLE",
        }:
            from app.modules.builds.execution_window import material_unit_admitted

            if material_needs_planning(
                session, context, step
            ) or not material_unit_admitted(
                session,
                tenant_id=step.tenant_id,
                submission_id=step.submission_id,
                unit_id=step.unit_id,
            ):
                # 旧版本已发布的未来组合消息也沿正式执行器自然转入等待，
                # 不删除队列、不刷新尚未获准组合的素材。
                step.status, step.phase, step.error_code = (
                    "PENDING",
                    "IDLE",
                    "execution_window_wait",
                )
                step.updated_at = datetime.now(UTC)
                session.add(step)
                return "PENDING"
        try:
            claim = claim_step(
                session,
                context=context,
                step_id=step.id,
                owner=uuid4(),
                lease_seconds=60,
            )
        except DomainError as error:
            step.status, step.error_code, step.phase = "FAILED", error.code, "DONE"
            evidence(
                session,
                step=step,
                claim=None,
                conclusion="LOCAL_FAILED",
                summary={"reason_code": error.code},
            )
            session.add(step)
            return "FAILED"
        if claim is None:
            return step.status
    assert claim
    armed = False
    result: CreatedObject | None = None
    task_deadline = min(task_deadline, claim.lease_expires_at)
    try:
        with Session(database_engine) as session, session.begin():
            frozen = _frozen(session, context, claim)
            if claim.kind == "MATERIAL":
                assert claim.material_id
                ready = ensure_target_asset(
                    session,
                    context=context,
                    bc_id=claim.bc_id,
                    material_id=claim.material_id,
                    advertiser_id=claim.advertiser_id,
                    task_key=f"build:{claim.step_id}",
                    route=claim.route,
                )
                if ready.state == "ready" and frozen.media_kind == "VIDEO":
                    from app.modules.materials.covers import ensure_cover

                    # 视频确认后才准备独立封面，不重新上传视频；未知图片结果沿原任务核查。
                    ready = ensure_cover(
                        session,
                        context=context,
                        bc_id=claim.bc_id,
                        material_id=claim.material_id,
                        advertiser_id=claim.advertiser_id,
                        task_key=f"build-cover:{claim.step_id}",
                        route=claim.route,
                    )
                    step = session.get(ExecutionStep, claim.step_id)
                    assert step
                    step.cover_job_id = ready.task_id
                    session.add(step)
                    if ready.state == "queued":
                        return finish_local(
                            session,
                            claim=claim,
                            status="PENDING",
                            code="cover_pending",
                            delay=15,
                        )
                    if ready.reason_code == "cover_result_unknown":
                        return finish_local(
                            session,
                            claim=claim,
                            status="UNKNOWN",
                            code="cover_result_unknown",
                        )
                if ready.state == "queued":
                    step = session.get(ExecutionStep, claim.step_id)
                    assert step
                    step.distribution_id = ready.task_id
                    session.add(step)
                    return finish_local(
                        session,
                        claim=claim,
                        status="PENDING",
                        code="material_pending",
                        delay=15,
                    )
                if (
                    ready.state == "blocked"
                    or not ready.mapping
                    or not ready.mapping.image_id
                ):
                    return finish_local(
                        session,
                        claim=claim,
                        status="FAILED",
                        code=ready.reason_code or "target_asset_incomplete",
                    )
                return finish_local(
                    session,
                    claim=claim,
                    status="SUCCEEDED",
                    resolved={"mapping": ready.mapping.model_dump(mode="json")},
                )
            _current_scene(session, context, frozen)
            try:
                step = session.get(ExecutionStep, claim.step_id)
                assert step is not None
                if step.request_body is not None:
                    body = dict(step.request_body)
                    if claim.kind == "AD":
                        # 明确未发送的重试保留原正文，但不能跳过素材准备；
                        # 否则过期后只会反复最终校验，永远没有刷新任务。
                        refreshed = prepare_request(
                            session, context=context, claim=claim, frozen=frozen
                        )
                        _, refreshed_body = create_arguments(
                            attempt_id=claim.attempt_id,
                            intent=decode_intent(claim.kind, refreshed),
                            channel=claim.route.channel,
                        )
                        if refreshed_body != body:
                            raise DomainError(
                                "execution_intent_changed", "原广告素材身份已变化"
                            )
                    local_body = dict(body)
                    # 两通道都携带 request_id；重试先校验原 attempt 关联，
                    # 再剥离传输字段供严格业务 DTO 解码，冻结正文保持不变。
                    if claim.kind in {"CAMPAIGN", "ADGROUP"}:
                        if local_body.pop("request_id", None) != remote_request_id(
                            claim.remote_attempt_id
                        ):
                            raise DomainError(
                                "execution_intent_changed", "原请求关联不匹配"
                            )
                    intent = decode_intent(claim.kind, local_body)
                else:
                    prepared = prepare_request(
                        session, context=context, claim=claim, frozen=frozen
                    )
                    intent = decode_intent(claim.kind, prepared)
                    _, body = create_arguments(
                        attempt_id=claim.attempt_id,
                        intent=intent,
                        channel=claim.route.channel,
                    )
            except DomainError as error:
                # 素材依赖与等待结果同事务提交，保持已经冻结的目标连接。
                if (
                    hasattr(error, "issues")
                    and step is not None
                    and active_attempt(step, claim, phase="CLAIMED")
                ):
                    step.resolved = {**step.resolved, "carousel_issues": error.issues}
                    session.flush()
                return finish_local(
                    session,
                    claim=claim,
                    status="PENDING" if error.retryable else "FAILED",
                    code=error.code,
                    delay=15,
                )

        def before_request() -> None:
            # 握手、tools/list、业务发送各自重查；事务不跨任何网络等待。
            with (
                bounded_session(database_engine, task_deadline=task_deadline) as check,
                check.begin(),
            ):
                step = check.exec(
                    select(ExecutionStep)
                    .where(
                        ExecutionStep.id == claim.step_id,
                        ExecutionStep.tenant_id == claim.tenant_id,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                ).one()
                if not active_attempt(
                    step, claim, phase="REQUEST_ARMED" if armed else "CLAIMED"
                ):
                    raise DomainError("execution_lease_lost", "执行租约已变化")
                from .continuation import (
                    require_execution_owner,
                    validate_execution_source,
                )

                require_execution_owner(check, step)
                validate_execution_source(check, step=step)
                current = _frozen(check, context, claim)
                _current_scene(check, context, current)
                if armed and claim.kind == "AD":
                    from app.modules.builds.cover_execution import validate_ad_assets
                    from app.modules.builds.preview_models import BuildUnit

                    unit = check.get(BuildUnit, claim.unit_id)
                    assert unit is not None
                    validate_ad_assets(check, step=step, unit=unit, body=body)

        with admitted_build_call(
            redis_client,
            context=context,
            route=claim.route,
            task_deadline=task_deadline,
        ):
            with open_tiktok_gateway(
                database_engine=database_engine,
                redis_client=redis_client,
                context=context,
                route=claim.route,
                task_deadline=task_deadline,
                before_request=before_request,
            ) as gateway:
                carousel_proof = None
                if claim.kind == "AD" and frozen.media_kind == "CAROUSEL":
                    from .carousel_music import _budget
                    from .carousel_readiness import (
                        CarouselNotReady,
                        body_assets,
                        music_proof,
                        verify_carousel_ad,
                    )

                    assets = body_assets(body)
                    carousel_ready = verify_carousel_ad(
                        claim.route,
                        claim.advertiser_id,
                        assets,
                        claim.attempt_id,
                        gateway=gateway,
                        budget=_budget(task_deadline, claim.route, "music.get"),
                    )
                    if not carousel_ready.ready:
                        raise CarouselNotReady(carousel_ready)
                    carousel_proof = music_proof(claim, assets)
                with (
                    bounded_session(
                        database_engine, task_deadline=task_deadline
                    ) as session,
                    session.begin(),
                ):
                    frozen = _frozen(session, context, claim)
                    if carousel_proof is not None:
                        step = session.get(ExecutionStep, claim.step_id)
                        if step is None or not active_attempt(
                            step, claim, phase="CLAIMED"
                        ):
                            raise DomainError("execution_lease_lost", "执行租约已变化")
                        step.resolved = {
                            **step.resolved,
                            "carousel_music": carousel_proof,
                        }
                        session.flush()
                    _current_scene(session, context, frozen)
                    arm_request(session, context=context, claim=claim, body=body)
                armed = True
                result = gateway.builds.create(
                    attempt_id=claim.remote_attempt_id, intent=intent
                )
                # 收到实际 ID 后先提交，再退出拥有的客户端；停用/撤权不抹除在途回执。
                try:
                    with (
                        span(
                            "build_receipt",
                            tenant_id=str(context.tenant_id),
                            step_id=str(claim.step_id),
                            submission_id=str(claim.submission_id),
                            remote_id=result.remote_id,
                        ),
                        Session(database_engine) as receipt,
                        receipt.begin(),
                    ):
                        outcome = record_created(receipt, claim=claim, result=result)
                except Exception:
                    with (
                        span(
                            "build_receipt_fallback",
                            tenant_id=str(context.tenant_id),
                            step_id=str(claim.step_id),
                            submission_id=str(claim.submission_id),
                            remote_id=result.remote_id,
                        ),
                        Session(database_engine) as receipt,
                        receipt.begin(),
                    ):
                        outcome = preserve_created_receipt(
                            receipt, claim=claim, result=result
                        )
            return outcome
    except Exception as error:
        if armed:
            emit(
                "build_armed_error",
                tenant_id=str(context.tenant_id),
                submission_id=str(claim.submission_id),
                step_id=str(claim.step_id),
                receipt_received=result is not None,
                **error_fields(error),
            )
            with Session(database_engine) as session, session.begin():
                if result is not None:
                    return preserve_created_receipt(session, claim=claim, result=result)
                if isinstance(error, DomainError) and not isinstance(
                    error, RemoteCallError
                ):
                    # 两适配器把已发送异常统一转成 RemoteCallError.UNKNOWN。
                    # 此处普通 DomainError 只能来自 gateway/P0 的本地发送前检查。
                    error = RemoteCallError(
                        error.code, effect="NOT_SENT", evidence=CallEvidence()
                    )
                if isinstance(error, RemoteCallError) and error.effect == "NOT_SENT":
                    return record_not_sent(
                        session,
                        claim=claim,
                        error=error,
                        retryable=error.code
                        in {
                            "admission_deferred",
                            "admission_unavailable",
                            "tiktok_local_resources_unavailable",
                            "gateway_credentials_changed",
                            "mcp_refresh_pending",
                        },
                        delay=15,
                    )
                if (
                    isinstance(error, RemoteCallError)
                    and error.effect == "REJECTED_NO_EFFECT"
                ):
                    return record_rejected(session, claim=claim, error=error)
                return record_unknown(
                    session,
                    claim=claim,
                    code=error.code
                    if isinstance(error, RemoteCallError)
                    else "create_result_unknown",
                    request_id=error.evidence.request_id
                    if isinstance(error, RemoteCallError)
                    else None,
                    call_evidence=error.evidence
                    if isinstance(error, RemoteCallError)
                    else None,
                    transport_diagnostics=error.transport_diagnostics
                    if isinstance(error, RemoteCallError)
                    else None,
                )
        safe = (
            error
            if isinstance(error, DomainError)
            else DomainError("execution_prepare_failed", "执行准备未完成")
        )
        if safe.code == "scene_refresh_required":
            return _prepare_scene_dependency(database_engine, context, claim)
        return _local_result(
            database_engine,
            claim,
            safe,
            delay=max(1, ceil(error.retry_after_ms / 1000))
            if isinstance(error, AccountAdmissionDeferred)
            else 15,
        )
