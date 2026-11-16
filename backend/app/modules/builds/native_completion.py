"""三级和全部 API Ad 只在独立回读 MATCH 后完成，供普通及撤权收尾共用。"""

from sqlmodel import Session, select

from app.modules.builds.execution_models import ExecutionStep, Submission
from app.modules.builds.preview_models import BuildPreview


def all_native_matched(
    session: Session, submission: Submission, preview: BuildPreview
) -> bool:
    from .preview_models import BuildUnit

    units = session.exec(
        select(BuildUnit).where(
            BuildUnit.preview_id == preview.id,
            BuildUnit.tenant_id == submission.tenant_id,
        )
    ).all()
    return bool(units) and all(
        unit_native_matched(session, submission, u) for u in units
    )


def unit_native_matched(session, submission, unit):
    payload = unit.scene_snapshot["native_request"]
    steps = session.exec(
        select(ExecutionStep).where(
            ExecutionStep.tenant_id == submission.tenant_id,
            ExecutionStep.submission_id == submission.id,
            ExecutionStep.unit_id == unit.id,
        )
    ).all()
    objects = [s for s in steps if s.kind in {"CAMPAIGN", "ADGROUP", "AD"}]
    if {
        k: sum(s.kind == k for s in objects) for k in ("CAMPAIGN", "ADGROUP", "AD")
    } != {"CAMPAIGN": 1, "ADGROUP": 1, "AD": len(payload["ads"])}:
        return False
    if len({s.remote_id for s in objects}) != len(objects):
        return False
    for source in objects:
        reads = [
            s for s in steps if s.kind == "READBACK" and s.parent_step_id == source.id
        ]
        if not (
            source.status == "SUCCEEDED"
            and source.remote_id
            and source.checked_at
            and not source.mismatch
            and source.operation_status == "ENABLE"
        ):
            return False
        if not any(
            s.status == "SUCCEEDED"
            and not s.mismatch
            and s.remote_id == source.remote_id
            and s.resolved.get("reconciliation", {})
            .get("candidate", {})
            .get("comparison")
            == "MATCH"
            for s in reads
        ):
            return False
    return True


def batch_results(session, submission, preview):
    """逐计划状态只由原步骤和实际 MATCH 派生，不写第二份状态。"""
    from uuid import UUID

    from .native_preview import read_batch
    from .preview_models import BuildUnit
    from .submissions import aggregate_status

    batch = read_batch(session, preview=preview)
    results = []
    for item in batch["plans"]:
        result = {k: item[k] for k in ("plan_key", "selection", "reason_codes")}
        result.update(status="EXCLUDED", remote_ids=[], occupancy=item.get("occupancy"))
        if item["status"] == "INCLUDED":
            unit = session.get(BuildUnit, UUID(item["unit_id"]))
            steps = session.exec(
                select(ExecutionStep).where(
                    ExecutionStep.tenant_id == submission.tenant_id,
                    ExecutionStep.submission_id == submission.id,
                    ExecutionStep.unit_id == unit.id,
                )
            ).all()
            # 与原回执一致：明确失败父级的未发送后代已不可能建成，
            # 调度器稍后记录 dependency_failed 不应使页面一直显示执行中。
            failed = {s.id for s in steps if s.status == "FAILED"}
            # 原生每计划只有一个组，CTA 和全部素材均是 Campaign 的前置条件，
            # 这些依赖不在 parent_step_id 中；不能把永久阻断的创建链仍算作等待。
            if any(s.kind in {"CTA", "MATERIAL"} and s.id in failed for s in steps):
                failed.update(
                    s.id
                    for s in steps
                    if s.kind == "CAMPAIGN"
                    and s.status in {"PENDING", "QUEUED", "RETRYABLE"}
                    and not s.remote_id
                )
            while True:
                blocked = {
                    s.id
                    for s in steps
                    if s.parent_step_id in failed
                    and s.status in {"PENDING", "QUEUED", "RETRYABLE"}
                    and not s.remote_id
                }
                if blocked <= failed:
                    break
                failed |= blocked
            states = {"FAILED" if s.id in failed else s.status for s in steps}
            if any(s.mismatch for s in steps):
                states.add("MISMATCH")
            state = aggregate_status(states)
            if (
                steps
                and all(s.error_code == "local_submission_cancelled" for s in steps)
                and all(s.request_body is None and not s.remote_id for s in steps)
            ):
                state = "CANCELLED"
            elif not steps and submission.progress.get("local_cancelled"):
                state = "CANCELLED"
            create_steps = [s for s in steps if s.kind in {"CAMPAIGN", "ADGROUP", "AD"}]
            if unit_native_matched(session, submission, unit):
                state = "COMPLETED"
            elif any(s.status in {"UNKNOWN", "MISMATCH"} or s.mismatch for s in steps):
                state = "NEEDS_REVIEW"
            elif create_steps and all(
                s.status == "FAILED" and s.error_code == "local_submission_cancelled"
                for s in create_steps
            ):
                # 素材准备成功不妨碍“所有广告对象均未发送”的整计划取消。
                state = "CANCELLED"
            elif any(
                s.kind != "READBACK"
                and s.id not in failed
                and s.status in {"PENDING", "QUEUED", "RUNNING", "RETRYABLE"}
                for s in steps
            ) and not (
                create_steps
                and all(
                    s.id in failed or s.status in {"SUCCEEDED", "CANCELLED"}
                    for s in create_steps
                )
            ):
                # 明确失败不能提前终止仍可执行的兄弟步骤。
                # 创建链已终结时，尚未执行的素材/CTA 不再使整计划停留 RUNNING。
                state = "RUNNING"
            elif any(
                s.kind != "READBACK"
                and (s.id in failed or s.status in {"FAILED", "CANCELLED"})
                for s in steps
            ):
                # 创建链已不能 MATCH；READBACK 或兄弟等待不掩盖终态。
                # CTA/素材只是前置条件，部分对象仅指 Campaign/组/广告。
                state = (
                    "PARTIAL"
                    if any(
                        s.kind in {"CAMPAIGN", "ADGROUP", "AD"}
                        and s.status == "SUCCEEDED"
                        for s in steps
                    )
                    else "FAILED"
                )
            elif state == "COMPLETED":
                state = "NEEDS_REVIEW"
            elif state == "PARTIAL":
                # 有父级对象不等于任何一份计划完整建成。
                state = "FAILED"
            result.update(
                status=state,
                unit_id=str(unit.id),
                reason_codes=sorted({s.error_code for s in steps if s.error_code}),
                remote_ids=[
                    {"kind": s.kind, "remote_id": s.remote_id}
                    for s in steps
                    if s.remote_id and s.kind in {"CAMPAIGN", "ADGROUP", "AD"}
                ],
            )
        results.append(result)
    states = {p["status"] for p in results if p["status"] != "EXCLUDED"}
    if "NEEDS_REVIEW" in states:
        status = "NEEDS_REVIEW"
    elif states & {"RUNNING", "QUEUED"}:
        status = "RUNNING" if submission.expanded else "QUEUED"
    elif states == {"CANCELLED"}:
        status = "CANCELLED"
    elif states == {"COMPLETED"}:
        status = "COMPLETED"
    elif "COMPLETED" in states:
        status = "PARTIAL"
    elif states == {"PARTIAL"} and sum(p["status"] != "EXCLUDED" for p in results) == 1:
        # M1 单计划保留已有部分对象的回执；多计划无 MATCH 仍是 FAILED。
        status = "PARTIAL"
    else:
        status = "FAILED"
    return status, results
