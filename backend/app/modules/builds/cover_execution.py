"""Publish verified cover results to the original frozen MATERIAL steps."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, or_
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.accounts.access import resolve_account_access
from app.modules.builds.dispatch import finalize_submission, wake_unit
from app.modules.builds.execution_models import (
    ExecutionStep,
    Submission,
    SubmissionUnit,
)
from app.modules.builds.execution_state import evidence
from app.modules.builds.preview_materials import frozen_ad_material_ids
from app.modules.builds.preview_models import (
    BuildUnit,
    PlannedAd,
    PlannedGroup,
)
from app.modules.builds.routes import verify_unit_route
from app.modules.materials.cover_models import MaterialCoverJob
from app.modules.materials.models import AccountMaterial
from app.modules.materials.readiness import mapping_fresh


def cover_matches_step(
    job: MaterialCoverJob, step: ExecutionStep, unit: BuildUnit
) -> bool:
    return (
        job.tenant_id,
        job.bc_id,
        job.material_id,
        job.advertiser_id,
        job.connection_id,
    ) == (
        step.tenant_id,
        step.bc_id,
        step.material_id,
        unit.advertiser_id,
        unit.connection_id,
    )


def _record_cover_recovery_error(step: ExecutionStep, code: str) -> None:
    """记录本轮拒绝，但不能破坏 PENDING 依赖下一轮仍可领取的身份。"""
    if step.status == "PENDING":
        step.resolved = {**step.resolved, "dependency_recovery_error": code}
    else:
        step.error_code = code
    step.updated_at = datetime.now(UTC)


def retry_ad_covers(
    session: Session, *, context: TenantContext, step: ExecutionStep, unit: BuildUnit
) -> None:
    """An explicit retry of an unsent AD resumes its bounded cover dependencies.

    Completed MATERIAL steps remain historical facts. Ordinary retries reconcile
    the current uncertain image identity; only explicit reissue creates a successor.
    """
    from app.modules.materials.covers import (
        request_cover_reconciliation,
        request_cover_retry,
    )

    if step.kind != "AD" or step.request_body is not None or step.remote_id:
        raise DomainError("execution_requires_reconciliation", "广告结果需要先核实")
    ad = session.exec(
        select(PlannedAd).where(
            PlannedAd.tenant_id == step.tenant_id,
            PlannedAd.preview_id == step.preview_id,
            PlannedAd.id == step.planned_ad_id,
        )
    ).one_or_none()
    group = session.exec(
        select(PlannedGroup).where(
            PlannedGroup.tenant_id == step.tenant_id,
            PlannedGroup.preview_id == step.preview_id,
            PlannedGroup.id == step.group_id,
        )
    ).one_or_none()
    if ad is None or group is None:
        raise DomainError("resource_not_found", "冻结创意不存在")
    material_ids = frozen_ad_material_ids(
        session,
        tenant_id=step.tenant_id,
        preview_id=step.preview_id,
        drama_id=group.drama_id,
        group_no=group.group_no,
        base_ad_no=ad.base_ad_no,
        unit_id=unit.id,
    )
    from uuid import UUID

    from .preview_models import BuildPreview

    preview = session.get(BuildPreview, step.preview_id)
    payload = preview.config.get("native_request")
    if payload:
        # 仅默认封面位置接续视频封面任务；显式图片由原 IMAGE owner 管理。
        selected = next(
            a
            for a in payload["ads"]
            if (a["base_ad_no"], a["creative_no"]) == (ad.base_ad_no, ad.creative_no)
        )
        material_ids = [
            UUID(m["material_id"])
            for m in selected["materials"]
            if not m.get("cover_material_id")
        ]
        if not material_ids:
            return
    jobs = session.exec(
        select(MaterialCoverJob)
        .join(
            AccountMaterial,
            (col(AccountMaterial.id) == MaterialCoverJob.asset_id)
            & (col(AccountMaterial.tenant_id) == MaterialCoverJob.tenant_id)
            # 封面任务的连接即本次搭建连接；映射的历史上传连接不参与匹配。
            & (col(AccountMaterial.video_id) == MaterialCoverJob.video_id),
        )
        .where(
            MaterialCoverJob.tenant_id == context.tenant_id,
            MaterialCoverJob.bc_id == step.bc_id,
            MaterialCoverJob.advertiser_id == unit.advertiser_id,
            MaterialCoverJob.connection_id == unit.connection_id,
            col(MaterialCoverJob.superseded_by_id).is_(None),
            col(AccountMaterial.material_id).in_(material_ids),
        )
        .order_by(col(MaterialCoverJob.id))
        .limit(50)
    ).all()
    for job in jobs:
        if job.status == "READY":
            # 广告重试只接续未完成封面；成功映射不因重试或本地年龄再次核查。
            continue
        if job.status == "BLOCKED" and job.request_armed_at is None:
            request_cover_retry(session, context=context, job_id=job.id)
        else:
            request_cover_reconciliation(session, context=context, job_id=job.id)


def validate_ad_assets(
    session: Session, *, step: ExecutionStep, unit: BuildUnit, body: dict[str, Any]
) -> None:
    """Pure local final fence, after admission and immediately before AD arming."""
    from app.modules.materials.covers import verified_cover_image_id

    from .preview_models import BuildPreview

    preview = session.get(BuildPreview, step.preview_id)
    if preview.config.get("native_request"):
        from .native_execution import validate_ad_assets as validate_native_assets

        validate_native_assets(session, step=step, unit=unit, body=body)
        return

    ad = session.exec(
        select(PlannedAd).where(
            PlannedAd.tenant_id == step.tenant_id,
            PlannedAd.preview_id == step.preview_id,
            PlannedAd.id == step.planned_ad_id,
        )
    ).one_or_none()
    group = session.exec(
        select(PlannedGroup).where(
            PlannedGroup.tenant_id == step.tenant_id,
            PlannedGroup.preview_id == step.preview_id,
            PlannedGroup.id == step.group_id,
        )
    ).one_or_none()
    if ad is None or group is None:
        raise DomainError("resource_not_found", "冻结创意不存在")
    from .creative_units import preview_contract

    if (
        preview_contract(
            session, tenant_id=step.tenant_id, preview_id=step.preview_id
        ).media_kind
        == "CAROUSEL"
    ):
        from .carousel_readiness import validate_carousel_assets

        validate_carousel_assets(
            session, step=step, unit=unit, body=body, ad=ad, group=group
        )
        return
    material_ids = frozen_ad_material_ids(
        session,
        tenant_id=step.tenant_id,
        preview_id=step.preview_id,
        drama_id=group.drama_id,
        group_no=group.group_no,
        base_ad_no=ad.base_ad_no,
        unit_id=unit.id,
    )
    rows = session.exec(
        select(AccountMaterial.material_id, AccountMaterial, MaterialCoverJob)
        .select_from(AccountMaterial)
        .outerjoin(
            MaterialCoverJob,
            (col(MaterialCoverJob.tenant_id) == AccountMaterial.tenant_id)
            & (col(MaterialCoverJob.asset_id) == AccountMaterial.id)
            # 同一映射同一 VID 只有一个当前代封面任务；它可能由与历史上传
            # 不同的连接发出，最终仍以 READY 正证据与映射图片一致为准。
            & (col(MaterialCoverJob.video_id) == AccountMaterial.video_id)
            # 一个视频保留多代历史，但最终创意只能使用当前代正证据。
            & col(MaterialCoverJob.superseded_by_id).is_(None),
        )
        .where(
            AccountMaterial.tenant_id == step.tenant_id,
            AccountMaterial.bc_id == step.bc_id,
            AccountMaterial.advertiser_id == unit.advertiser_id,
            col(AccountMaterial.material_id).in_(material_ids),
        )
        .limit(51)
        .execution_options(populate_existing=True)
    ).all()
    row_order = {material_id: index for index, material_id in enumerate(material_ids)}
    rows = sorted(rows, key=lambda row: row_order.get(row[0], len(row_order)))
    try:
        if any(
            len(entry["creative_info"]["image_info"]) != 1
            for entry in body["creative_list"]
        ):
            raise DomainError("invalid_build_request", "单视频创意必须指定一张封面")
        actual = [
            (
                entry["creative_info"]["video_info"]["video_id"],
                entry["creative_info"]["image_info"][0]["web_uri"],
            )
            for entry in body["creative_list"]
        ]
    except KeyError, TypeError, IndexError:
        raise DomainError("invalid_build_request", "创意素材结构无效") from None
    valid = (
        1 <= len(rows) <= 50
        and len(rows) == len(material_ids)
        and len(rows) == len(actual)
    )
    for (_, mapping, job), (video_id, image_id) in zip(rows, actual, strict=False):
        if not mapping or not mapping_fresh(mapping) or mapping.video_id != video_id:
            valid = False
            continue
        # 默认单视频构建必须携带当前目标视频的封面，最终发送前再次核对图片及其验证事实。
        if (
            not image_id
            or mapping.image_id != image_id
            or job
            and (
                job.status != "READY"
                # 封面可能来自本次导入，也可能已在目标账户核实后直接复用。
                # 两者都要求 READY 正证据，不能把尚未核实的候选当作上传回执。
                or verified_cover_image_id(job) != image_id
            )
        ):
            valid = False
    if not valid:
        raise DomainError(
            "material_refresh_required", "目标素材已变化，需要重新核实", retryable=True
        )


def recover_cover_results(*, database_engine: Any, limit: int = 100) -> int:
    from app.modules.materials.covers import get_cover_status

    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("invalid cover recovery page")
    with Session(database_engine) as session:
        candidates = session.exec(
            select(ExecutionStep.tenant_id, ExecutionStep.unit_id, ExecutionStep.id)
            .join(
                MaterialCoverJob,
                (col(MaterialCoverJob.id) == ExecutionStep.cover_job_id)
                & (col(MaterialCoverJob.tenant_id) == ExecutionStep.tenant_id),
            )
            .where(
                ExecutionStep.kind == "MATERIAL",
                or_(
                    col(ExecutionStep.status) == "UNKNOWN",
                    and_(
                        col(ExecutionStep.status) == "PENDING",
                        col(ExecutionStep.error_code) == "cover_pending",
                    ),
                ),
                col(MaterialCoverJob.status).in_(["READY", "BLOCKED"]),
                col(MaterialCoverJob.superseded_by_id).is_(None),
            )
            .order_by(col(ExecutionStep.updated_at), col(ExecutionStep.id))
            .limit(limit)
        ).all()
    changed, submissions = 0, {}
    for tenant_id, unit_id, identity in candidates:
        with Session(database_engine) as session, session.begin():
            owner = session.exec(
                select(SubmissionUnit)
                .where(
                    SubmissionUnit.tenant_id == tenant_id,
                    SubmissionUnit.unit_id == unit_id,
                )
                .with_for_update(skip_locked=True)
            ).one_or_none()
            if owner is None:
                continue
            step = session.exec(
                select(ExecutionStep)
                .where(
                    ExecutionStep.tenant_id == tenant_id,
                    ExecutionStep.id == identity,
                )
                .with_for_update()
            ).one()
            if (
                not (
                    step.status == "UNKNOWN"
                    or (step.status == "PENDING" and step.error_code == "cover_pending")
                )
                or step.kind != "MATERIAL"
                or step.cover_job_id is None
            ):
                continue
            job, unit = (
                session.get(MaterialCoverJob, step.cover_job_id),
                session.get(BuildUnit, step.unit_id),
            )
            if (
                job is None
                or job.superseded_by_id is not None
                or unit is None
                or not cover_matches_step(job, step, unit)
            ):
                continue
            row = session.get(Submission, step.submission_id)
            assert row
            context = TenantContext(
                tenant_id=row.tenant_id, actor_id=row.actor_id, role="operator"
            )
            try:
                # 封面回执沿原预览核实；默认连接变化不能阻断原任务，也不能绕过重绑代数。
                route = verify_unit_route(
                    session, context=context, unit=unit, capability="build"
                )
                access = resolve_account_access(
                    session,
                    context=context,
                    bc_id=step.bc_id,
                    advertiser_id=unit.advertiser_id,
                    action="build",
                    connection_id=route.connection_id,
                )
                if (access.connection_id, access.currency, access.timezone) != (
                    unit.connection_id,
                    unit.currency,
                    unit.timezone,
                ):
                    raise DomainError("new_preview_required", "账户与冻结预览不一致")
                result = get_cover_status(session, context=context, job_id=job.id)
            except DomainError as error:
                _record_cover_recovery_error(step, error.code)
                session.add(step)
                continue
            if (
                result.state == "ready"
                and result.mapping
                and result.mapping.image_id
                and result.mapping.video_id == job.video_id
            ):
                step.status, step.phase, step.error_code = "SUCCEEDED", "DONE", None
                step.resolved = {
                    **step.resolved,
                    "mapping": result.mapping.model_dump(mode="json"),
                }
                conclusion = "MATERIAL_COVER_VERIFIED"
            elif (
                job.status == "BLOCKED"
                and job.request_armed_at is None
                and job.known_image_id is None
            ):
                step.status, step.phase, step.error_code = (
                    "FAILED",
                    "DONE",
                    result.reason_code or "cover_blocked",
                )
                conclusion = "MATERIAL_COVER_BLOCKED"
            else:
                _record_cover_recovery_error(
                    step, result.reason_code or "cover_result_unknown"
                )
                session.add(step)
                continue
            step.resolved = {
                key: value
                for key, value in step.resolved.items()
                if key != "dependency_recovery_error"
            }
            step.dispatch_id = step.lease_token = step.lease_expires_at = None
            step.updated_at = datetime.now(UTC)
            session.add(step)
            evidence(
                session,
                step=step,
                claim=None,
                conclusion=conclusion,
                summary={"cover_job_id": str(job.id)},
            )
            wake_unit(session, unit_id=unit_id, context=context)
            submissions[row.id] = context
            changed += 1
    for submission_id, context in submissions.items():
        finalize_submission(
            database_engine=database_engine,
            context=context,
            submission_id=submission_id,
        )
    return changed
