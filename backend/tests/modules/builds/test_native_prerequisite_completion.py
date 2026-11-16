"""原生计划前置失败只终结本计划；另一账户仍可继续真实执行。"""

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pytest
from sqlmodel import Session, select
from urllib3.response import HTTPResponse

from app.jobs.models import PendingDispatch
from app.modules.accounts.models import BCAccountAccess
from app.modules.builds import dispatch, execution, reconciliation, submissions, tasks
from app.modules.builds.execution_models import (
    ExecutionStep,
    Submission,
    SubmissionUnit,
)
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
from tests.modules.builds.test_native_readback_timeline import observed_rows


@pytest.mark.parametrize("failed_kind", ["MATERIAL", "CTA"])
def test_failed_prerequisite_does_not_block_sibling_plan(
    job_env, wire, redis_client, monkeypatch, failed_kind
):
    client, url, _, _, revision, _, _ = two_account_workspace(
        job_env,
        wire,
        redis_client,
        config_changes={
            "group_generation_mode": "FIXED",
            "group_count": 1,
            "group_material_allocation": "SHARED",
            "max_materials_per_group": None,
            "ads_per_group": 1,
            "creative_count": 1,
            "budget": "1000.01",
        },
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
    creates = []
    reads = []

    def request(_pool, method, url, **kw):
        if method == "POST":
            body = json.loads(kw["body"])
            assert body["advertiser_id"] == "second-account"
            creates.append((url, body))
            key = (
                "creative_portfolio_id"
                if "portfolio" in url
                else "campaign_id"
                if "/campaign/" in url
                else "adgroup_id"
                if "/adgroup/" in url
                else "smart_plus_ad_id"
            )
            return HTTPResponse(
                body=json.dumps(
                    {
                        "code": 0,
                        "data": {key: f"remote-{len(creates)}"},
                        "request_id": f"create-{len(creates)}",
                    }
                ).encode(),
                status=200,
            )
        assert method == "GET"
        args = dict(kw.get("fields") or parse_qs(urlsplit(url).query))
        filtering = args.get("filtering", "{}")
        if isinstance(filtering, str):
            filtering = json.loads(filtering)
        key = next(
            key
            for key in ("smart_plus_ad_ids", "adgroup_ids", "campaign_ids")
            if key in filtering
        )
        rid = filtering[key][0]
        # 复用独立平台事实，仅按双账户 fixture 的固定事实替换作用域和素材。
        rows = observed_rows(creates)
        for row in rows.values():
            row["advertiser_id"] = "second-account"
            if "adgroup_name" in row:
                row["native_series_id"] = "second-series"
                row["identity_id"] = "second-owner"
            if "creative_list" in row:
                row["creative_list"] = [
                    {
                        "creative_info": {
                            "identity_type": "TT_USER",
                            "identity_id": "second-owner",
                            "ad_format": "SINGLE_VIDEO",
                            "video_info": {"video_id": f"second-target-{i}"},
                            "image_info": [{"web_uri": f"second-cover-{i}"}],
                        }
                    }
                    for i in (0, 1)
                ]
        assert rid in rows
        reads.append(rid)
        return HTTPResponse(
            body=json.dumps(
                {
                    "code": 0,
                    "request_id": "observed-" + rid,
                    "data": {
                        "list": [rows[rid]],
                        "page_info": {
                            "page": 1,
                            "page_size": 100,
                            "total_page": 1,
                            "total_number": 1,
                        },
                    },
                }
            ).encode(),
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", request)
    monkeypatch.setattr(
        reconciliation,
        "current_task",
        SimpleNamespace(
            request=SimpleNamespace(
                timelimit=(45, 40), called_directly=False, is_eager=False
            )
        ),
    )
    monkeypatch.setattr(
        reconciliation,
        "current_process",
        lambda: SimpleNamespace(daemon=True, name="ForkPoolWorker-1"),
    )
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

    # 只消费当前持久化消息；真实调度落定失败后代，成功兄弟沿执行/回读链推进。
    for _ in range(12):
        with Session(native.engine) as session:
            unit_messages = [
                dict(session.get(PendingDispatch, unit.dispatch_id).payload)
                for unit in session.exec(
                    select(SubmissionUnit).where(
                        SubmissionUnit.submission_id == receipt.submission_id
                    )
                ).all()
                if unit.dispatch_id is not None
            ]
        for payload in unit_messages:
            dispatch.process_unit(
                database_engine=native.engine, context=context, payload=payload
            )
        with Session(native.engine) as session:
            step_messages = [
                (
                    dict(session.get(PendingDispatch, step.dispatch_id).payload),
                    step.kind == "READBACK",
                )
                for step in session.exec(
                    select(ExecutionStep).where(
                        ExecutionStep.submission_id == receipt.submission_id,
                        ExecutionStep.status == "QUEUED",
                    )
                ).all()
                if step.dispatch_id is not None
            ]
        if not unit_messages and not step_messages:
            break
        for payload, is_readback in step_messages:
            tasks.deliver_step(
                database_engine=native.engine,
                redis_client=redis_client,
                context=context,
                payload=payload,
                reconcile=is_readback,
            )
    else:
        pytest.fail("native prerequisite continuation did not settle")

    dispatch.finalize_submission(
        database_engine=native.engine,
        context=context,
        submission_id=receipt.submission_id,
    )
    with Session(native.engine) as session:
        result = submissions.get_submission(
            session, context=context, submission_id=receipt.submission_id
        )
        by_unit = {plan.unit_id: plan for plan in result.native_plans}
        assert by_unit[str(units["actual-account"])].status == "FAILED"
        assert by_unit[str(units["second-account"])].status == "COMPLETED"
        assert result.status == "PARTIAL"
        assert session.get(Submission, receipt.submission_id).status == result.status
        steps = session.exec(
            select(ExecutionStep).where(
                ExecutionStep.submission_id == receipt.submission_id
            )
        ).all()
        failed_steps = [s for s in steps if s.unit_id == units["actual-account"]]
        assert session.get(ExecutionStep, failed_id).status == "FAILED"
        assert all(s.remote_id is None and s.request_body is None for s in failed_steps)
        assert all(
            s.status == "FAILED"
            for s in failed_steps
            if s.kind in {"CAMPAIGN", "ADGROUP", "AD"}
        )
        sibling_reads = [s for s in steps if s.kind == "READBACK"]
        assert len(sibling_reads) == 3
        assert all(
            s.unit_id == units["second-account"]
            and s.status == "SUCCEEDED"
            and s.resolved["reconciliation"]["candidate"]["comparison"] == "MATCH"
            for s in sibling_reads
        )
        assert all(
            unit.dispatch_id is None
            for unit in session.exec(
                select(SubmissionUnit).where(
                    SubmissionUnit.submission_id == receipt.submission_id
                )
            ).all()
        )
    assert len(creates) == 4
    assert all(body["advertiser_id"] == "second-account" for _, body in creates)
    assert set(reads) == {"remote-2", "remote-3", "remote-4"}
    assert len(reads) == 3

    from app.models import User

    with Session(native.engine) as session, session.begin():
        actor = session.get(User, context.actor_id)
        actor.is_active = False
        session.add(actor)
    # 内部收尾不能因操作者停用而重新把已确定的计划终态写为 RUNNING。
    dispatch.finalize_submission(
        database_engine=native.engine,
        context=context,
        submission_id=receipt.submission_id,
    )
    with Session(native.engine) as session:
        assert session.get(Submission, receipt.submission_id).status == "PARTIAL"
    assert len(creates) == 4 and len(reads) == 3
