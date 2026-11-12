"""三级和全部 API Ad 只在独立回读 MATCH 后完成，供普通及撤权收尾共用。"""

from sqlmodel import Session, select

from app.modules.builds.execution_models import ExecutionStep, Submission
from app.modules.builds.preview_models import BuildPreview


def all_native_matched(
    session: Session, submission: Submission, preview: BuildPreview
) -> bool:
    payload = preview.config["native_request"]
    steps = session.exec(
        select(ExecutionStep).where(
            ExecutionStep.tenant_id == submission.tenant_id,
            ExecutionStep.submission_id == submission.id,
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
