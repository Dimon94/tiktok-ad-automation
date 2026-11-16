"""原生计划前置失败只终结本计划；另一账户仍可继续真实执行。"""

from uuid import UUID, uuid4

import pytest
from sqlmodel import Session, select

from app.modules.accounts.models import BCAccountAccess
from app.modules.builds import execution, submissions
from app.modules.builds.execution_models import ExecutionStep
from app.modules.builds.preview_models import BuildUnit
from tests.modules.builds.scene_jobs import test_native_series as native
from tests.modules.builds.test_native_execution import job_env as job_env
from tests.modules.builds.test_native_execution import (
    retain_build_history as retain_build_history,
)
from tests.modules.builds.test_native_execution import scene_env as scene_env
from tests.modules.builds.test_native_execution import source_env as source_env
from tests.modules.builds.test_native_execution import wire as wire
from tests.modules.builds.test_native_plan_set import two_account_workspace


@pytest.mark.parametrize("failed_kind", ["MATERIAL", "CTA"])
def test_failed_prerequisite_does_not_block_sibling_plan(
    job_env, wire, redis_client, monkeypatch, failed_kind
):
    client, url, _, _, revision, _, _ = two_account_workspace(
        job_env, wire, redis_client
    )
    response = client.post(url + "/previews", json={"expected_revision": revision})
    assert response.status_code == 202, response.text
    preview_id = UUID(response.json()["preview_id"])
    context = job_env["context"]
    with Session(native.engine) as session, session.begin():
        receipt = submissions.submit_preview(
            session, context=context, preview_id=preview_id, request_id=uuid4()
        )
        while not submissions.expand_submission(
            session, context=context, submission_id=receipt.submission_id
        ):
            pass
        units = {
            unit.advertiser_id: unit.id
            for unit in session.exec(
                select(BuildUnit).where(BuildUnit.preview_id == preview_id)
            ).all()
        }
        steps = session.exec(
            select(ExecutionStep).where(
                ExecutionStep.submission_id == receipt.submission_id
            )
        ).all()
        failed_id = next(
            step.id
            for step in steps
            if step.unit_id == units["actual-account"] and step.kind == failed_kind
        )
        sibling_id = next(
            step.id
            for step in steps
            if step.unit_id == units["second-account"] and step.kind == "MATERIAL"
        )
        grant = session.get(
            BCAccountAccess,
            (
                context.tenant_id,
                job_env["bc_id"],
                "actual-account",
                job_env["route"].connection_id,
            ),
        )
        grant.can_build = False
        session.add(grant)
    monkeypatch.setattr(execution, "_require_bounded_worker", lambda: None)
    # 使用真实领取/权限围栏产生失败事实，不用状态替身或篡改汇总。
    assert (
        execution.process_step(
            database_engine=native.engine,
            redis_client=redis_client,
            context=context,
            step_id=failed_id,
            revision=0,
        )
        == "FAILED"
    )
    assert (
        execution.process_step(
            database_engine=native.engine,
            redis_client=redis_client,
            context=context,
            step_id=sibling_id,
            revision=0,
        )
        == "SUCCEEDED"
    )
    with Session(native.engine) as session:
        result = submissions.get_submission(
            session, context=context, submission_id=receipt.submission_id
        )
        by_unit = {plan.unit_id: plan for plan in result.native_plans}
        assert by_unit[str(units["actual-account"])].status == "FAILED"
        assert by_unit[str(units["second-account"])].status == "RUNNING"
        assert result.status == "RUNNING"
        assert all(not plan.remote_ids for plan in result.native_plans)
