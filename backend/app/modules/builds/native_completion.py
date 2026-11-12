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
            while True:
                blocked = {
                    s.id
                    for s in steps
                    if s.parent_step_id in failed
                    and s.status in {"PENDING", "QUEUED", "RETRYABLE"}
                    and not s.remote_id
                    and s.request_body is None
                }
                if blocked <= failed:
                    break
                failed |= blocked
            states = {"FAILED" if s.id in failed else s.status for s in steps}
            if any(s.mismatch for s in steps):
                states.add("MISMATCH")
            state = aggregate_status(states)
            if unit_native_matched(session, submission, unit):
                state = "COMPLETED"
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
    elif states == {"COMPLETED"}:
        status = "COMPLETED"
    elif "COMPLETED" in states:
        status = "PARTIAL"
    else:
        status = "FAILED"
    return status, results
