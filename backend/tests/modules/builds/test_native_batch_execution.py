# ruff: noqa: F811, ARG001
"""JB-16 checks consume actual prepared/frozen workspace facts."""

from uuid import UUID

import pytest
from sqlmodel import Session, select

from app.modules.builds.native_execution import validate
from app.modules.builds.preview_models import BuildPreview, BuildUnit
from app.modules.builds.routes import load_preview_route
from tests.modules.builds.scene_jobs import test_native_series as native
from tests.modules.builds.test_native_batch_preview import (  # noqa: F401
    job_env,
    scene_env,
    wire,
)
from tests.modules.builds.test_native_execution import (  # noqa: F401
    retain_build_history,
    source_env,
)
from tests.modules.builds.test_native_plan_set import two_account_workspace


def test_validate_current_plan(job_env, wire, redis_client):
    client, url, _, _, revision, _, _ = two_account_workspace(
        job_env, wire, redis_client
    )
    response = client.post(url + "/previews", json={"expected_revision": revision})
    assert response.status_code == 202, response.text
    pid = UUID(response.json()["preview_id"])
    with Session(native.engine) as session:
        preview = session.get(BuildPreview, pid)
        route = load_preview_route(session, context=job_env["context"], preview_id=pid)
        units = session.exec(select(BuildUnit).where(BuildUnit.preview_id == pid)).all()
        assert len(units) == 2
        for unit in units:
            payload = validate(
                session,
                context=job_env["context"],
                preview=preview,
                route=route,
                unit_id=unit.id,
            )
            assert payload == unit.scene_snapshot["native_request"]
            assert payload["selection"]["advertiser_id"] == unit.advertiser_id

        from datetime import UTC, datetime

        from app.core.errors import DomainError
        from app.modules.ads.models import AdObject

        session.add(
            AdObject(
                tenant_id=job_env["context"].tenant_id,
                advertiser_id="actual-account",
                kind="adgroup",
                remote_id="new-active",
                ad_type="SMART_PLUS",
                configuration={
                    "native_series_id": "exact-series",
                    "location_ids": ["6252001"],
                },
                operation_status="ENABLE",
                delivery_status="ACTIVE",
                observed_at=datetime.now(UTC),
                published_version=1,
            )
        )
        session.flush()
        for unit in units:
            if unit.advertiser_id == "actual-account":
                with pytest.raises(DomainError, match="已知活跃占用"):
                    validate(
                        session,
                        context=job_env["context"],
                        preview=preview,
                        route=route,
                        unit_id=unit.id,
                    )
            else:
                assert (
                    validate(
                        session,
                        context=job_env["context"],
                        preview=preview,
                        route=route,
                        unit_id=unit.id,
                    )["selection"]["advertiser_id"]
                    == "second-account"
                )


def test_same_preview_replay_returns_original(job_env, wire, redis_client):
    """Spec R8: committed replay with either key returns the original identity."""
    from uuid import uuid4

    from app.core.errors import DomainError
    from app.modules.builds.execution_models import Submission, SubmissionRequest
    from app.modules.builds.submissions import submit_preview

    client, url, _, _, revision, _, _ = two_account_workspace(
        job_env, wire, redis_client
    )
    response = client.post(url + "/previews", json={"expected_revision": revision})
    assert response.status_code == 202, response.text
    pid = UUID(response.json()["preview_id"])
    context = job_env["context"]
    first_key = uuid4()

    def submit(key):
        try:
            with Session(native.engine) as session, session.begin():
                return submit_preview(
                    session, context=context, preview_id=pid, request_id=key
                ).submission_id
        except DomainError as error:
            return error.code

    original = submit(first_key)
    assert isinstance(original, UUID)
    for key in (first_key, uuid4()):
        assert submit(key) == original, "R8 replay must return original Submission"
    with Session(native.engine) as session:
        assert session.exec(
            select(Submission.id).where(Submission.preview_id == pid)
        ).all() == [original]
        assert set(
            session.exec(
                select(SubmissionRequest.submission_id).where(
                    SubmissionRequest.preview_id == pid
                )
            ).all()
        ) == {original}


@pytest.mark.parametrize(
    "source_env", [{"bc_id": "90071992547409931234"}], indirect=True
)
def test_cancel_before_final_send_fence(job_env, wire, redis_client, monkeypatch):
    import threading
    from uuid import uuid4

    from urllib3.response import HTTPResponse

    from app.modules.builds import execution, native_execution, submissions
    from app.modules.builds.execution_models import ExecutionStep, Submission

    context = job_env["context"]
    client, url, _, _, revision, _, _ = two_account_workspace(
        job_env, wire, redis_client
    )
    response = client.post(url + "/previews", json={"expected_revision": revision})
    assert response.status_code == 202, response.text
    pid = UUID(response.json()["preview_id"])
    with Session(native.engine, expire_on_commit=False) as s, s.begin():
        receipt = submissions.submit_preview(s, context=context, preview_id=pid, request_id=uuid4())
        while not submissions.expand_submission(s, context=context, submission_id=receipt.submission_id):
            pass
        steps = s.exec(select(ExecutionStep).where(
            ExecutionStep.submission_id == receipt.submission_id
        )).all()
    calls = []
    monkeypatch.setattr(execution, "_require_bounded_worker", lambda: None)
    monkeypatch.setattr(
        "urllib3.PoolManager.request",
        lambda pool, method, url, **kw: (
            calls.append((url, kw["body"])),
            HTTPResponse(body=b'{"code":0,"data":{"creative_portfolio_id":"cta"}}', status=200),
        )[1],
    )
    for step in steps:
        if step.kind == "MATERIAL":
            assert execution.process_step(
                database_engine=native.engine, redis_client=redis_client,
                context=context, step_id=step.id, revision=step.dispatch_revision,
            ) == "SUCCEEDED"
    target = next(step for step in steps if step.kind == "CTA")
    arrived, release = threading.Event(), threading.Event()
    original_lock = native_execution.lock_send_authority
    errors = []

    def paused_before_authority(session, **kwargs):
        with Session(native.engine) as check:
            assert check.get(ExecutionStep, target.id).request_body is not None
        arrived.set()
        assert release.wait(timeout=30)
        return original_lock(session, **kwargs)

    monkeypatch.setattr(native_execution, "lock_send_authority", paused_before_authority)

    def cancel_first():
        from tests.modules.builds.native_cancel_http import cancel_twice
        try:
            assert arrived.wait(timeout=30)
            cancel_twice(native.engine, context, receipt.submission_id)
        except Exception as error:
            errors.append(error)
        finally:
            release.set()

    thread = threading.Thread(target=cancel_first)
    thread.start()
    outcome = execution.process_step(
        database_engine=native.engine, redis_client=redis_client,
        context=context, step_id=target.id, revision=target.dispatch_revision,
    )
    thread.join(timeout=30)
    assert arrived.is_set() and not thread.is_alive() and not errors
    assert outcome == "FAILED" and calls == []
    with Session(native.engine) as s:
        saved = s.get(ExecutionStep, target.id)
        assert saved.phase == "DONE" and saved.status == "FAILED"
        assert saved.error_code == "local_submission_cancelled"
        assert saved.request_body is not None
        assert saved.request_body_digest and saved.attempt_id and saved.remote_id is None
        assert s.get(Submission, receipt.submission_id).progress["local_cancelled"]
        armed = (saved.request_body, saved.request_body_digest, saved.attempt_id)
    with Session(native.engine) as s, s.begin():
        for step in steps:
            if step.id != target.id:
                execution.process_step(
                    database_engine=native.engine, redis_client=redis_client,
                    context=context, step_id=step.id, revision=step.dispatch_revision,
                )
    with Session(native.engine) as s:
        assert (s.get(ExecutionStep, target.id).request_body,
                s.get(ExecutionStep, target.id).request_body_digest,
                s.get(ExecutionStep, target.id).attempt_id) == armed
        assert all(step.remote_id is None for step in s.exec(
            select(ExecutionStep).where(ExecutionStep.submission_id == receipt.submission_id)
        ))
    assert calls == []


@pytest.mark.parametrize(
    "source_env", [{"bc_id": "90071992547409931234"}], indirect=True
)
@pytest.mark.parametrize(
    "scenario",
    [
        "cancel-before",
        "cancel-partial",
        "cancel-after-campaign",
        "cancel-unknown",
        "match",
        "partial",
        "zero",
        "unknown",
        "reject",
        "send-lock",
        "browser",
        "recovery",
        "recovery-complete",
        "recovery-partial",
        "recovery-browser",
        "historical-rejection",
        "native-ad-PENDING",
        "native-ad-QUEUED",
        "native-ad-RUNNING",
        "native-ad-RETRYABLE",
    ],
)
def test_batch_submit_and_execute(job_env, wire, redis_client, monkeypatch, scenario):
    import json
    from concurrent.futures import ThreadPoolExecutor
    from uuid import uuid4

    from urllib3.response import HTTPResponse

    from app.modules.builds import dispatch, execution, submissions
    from app.modules.builds.execution_models import (
        ExecutionStep,
        Submission,
        SubmissionUnit,
    )

    historical = scenario == "historical-rejection"
    native_ad_state = (
        scenario.removeprefix("native-ad-")
        if scenario.startswith("native-ad-")
        else None
    )
    if native_ad_state:
        scenario = "match"
    if historical:
        scenario = "zero"
    terminal_recovery = scenario if scenario.startswith("recovery-") else None
    recovering = scenario == "recovery"
    if recovering:
        scenario = "unknown"
    browser = scenario in {"browser", "recovery-browser", "cancel-before"}
    client, url, draft_id, _, revision, value, _ = two_account_workspace(
        job_env, wire, redis_client
    )
    if browser:
        from copy import deepcopy

        from tests.modules.builds.test_native_plan_set import drive_plan_jobs

        blocked = deepcopy(value["plans"][0])
        blocked["plan_key"] = str(uuid4())
        blocked["selection"]["advertiser_id"] = "not-authorized"
        value["plans"].append(blocked)
        patched = client.patch(
            url, json={"expected_revision": revision, "scene_selection": value}
        )
        assert patched.status_code == 200, patched.text
        revision = patched.json()["revision"]
    context = job_env["context"]
    if browser:
        from tests.modules.builds.native_browser_http import submit_in_browser

        submit_in_browser(
            native.engine,
            context,
            job_env["bc_id"],
            None,
            draft_id=draft_id,
            mode="batch-workspace",
            drive=lambda task_id: drive_plan_jobs(
                job_env, wire, redis_client, UUID(task_id)
            ),
        )
        with Session(native.engine) as s:
            pid = s.exec(
                select(BuildPreview.id).where(
                    BuildPreview.draft_id == draft_id,
                    BuildPreview.draft_revision == revision,
                )
            ).one()
        scenario = "recovery-complete" if terminal_recovery else "match"
    else:
        response = client.post(url + "/previews", json={"expected_revision": revision})
        assert response.status_code == 202, response.text
        pid = UUID(response.json()["preview_id"])

    def submit(_):
        with Session(native.engine, expire_on_commit=False) as s, s.begin():
            return submissions.submit_preview(
                s, context=context, preview_id=pid, request_id=uuid4()
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(submit, range(2)))
    sid = receipts[0].submission_id
    assert receipts[1].submission_id == sid
    from app.modules.builds.submission_tasks import process_submission

    process_submission(
        database_engine=native.engine,
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        payload={"submission_id": str(sid), "revision": 0},
    )
    with Session(native.engine, expire_on_commit=False) as s, s.begin():
        steps = s.exec(
            select(ExecutionStep).where(ExecutionStep.submission_id == sid)
        ).all()
        assert (
            len(s.exec(select(Submission).where(Submission.preview_id == pid)).all())
            == 1
        )
    historical_receipts = {}
    lock_probe = scenario == "send-lock"
    if lock_probe:
        scenario = "match"
    calls = []
    locked = []
    objects = {}

    def transport(pool, method, url, **kw):
        if method == "GET":
            from urllib.parse import parse_qs, urlsplit

            args = dict(kw.get("fields") or parse_qs(urlsplit(url).query))
            filtering = args.get("filtering", "{}")
            if isinstance(filtering, list):
                filtering = filtering[0]
            filters = json.loads(filtering)
            ids = next((v for k, v in filters.items() if k.endswith("_ids")), [])
            found = [objects[r] for r in ids if r in objects]
            return HTTPResponse(
                body=json.dumps(
                    {
                        "code": 0,
                        "data": {
                            "list": found,
                            "page_info": {
                                "page": 1,
                                "page_size": 100,
                                "total_page": 1,
                                "total_number": len(found),
                            },
                        },
                    }
                ).encode(),
                status=200,
            )
        assert method == "POST"
        body = json.loads(kw["body"])
        if lock_probe and not calls:
            import time

            time.sleep(5.2)  # 真实传输延迟超过旧短事务的 idle timeout。
            from sqlalchemy import text
            from sqlalchemy.exc import OperationalError

            # SDK 物理传输内，独立会话撤权必须等待发送事务释放。
            with Session(native.engine) as revoke:
                revoke.execute(text("SET LOCAL lock_timeout = '100ms'"))
                try:
                    revoke.execute(
                        text(
                            "UPDATE bc_account_access SET can_build=false "
                            "WHERE tenant_id=:tenant AND advertiser_id=:advertiser"
                        ),
                        {
                            "tenant": context.tenant_id,
                            "advertiser": body["advertiser_id"],
                        },
                    )
                except OperationalError as error:
                    locked.append("lock timeout" in str(error))
                else:
                    locked.append(False)
                revoke.rollback()
            # 同一物理发送事务也必须阻止全局 User 代数变化。
            with Session(native.engine) as revoke:
                revoke.execute(text("SET LOCAL lock_timeout = '100ms'"))
                try:
                    revoke.execute(
                        text('UPDATE "user" SET is_active=false WHERE id=:actor'),
                        {"actor": context.actor_id},
                    )
                except OperationalError as error:
                    locked.append("lock timeout" in str(error))
                else:
                    locked.append(False)
                revoke.rollback()
        calls.append((url, body))
        if (
            native_ad_state
            and "/ad/create/" in url
            and len([
                u for u, b in calls
                if "/ad/create/" in u and b["advertiser_id"] == body["advertiser_id"]
            ]) % 2 == 1
        ):
            return HTTPResponse(
                body=json.dumps({
                    "code": 40002,
                    "message": "explicit native ad rejection",
                    "request_id": f"r1-rejected-{len(calls)}",
                }).encode(),
                status=200,
            )
        if (
            scenario in {"unknown", "cancel-unknown"}
            and "/campaign/" in url
            and body["advertiser_id"] == "second-account"
        ):
            raise TimeoutError("physical send response lost")
        if (scenario == "reject" and "/campaign/" in url) or (
            scenario in {"partial", "zero", "recovery-partial"}
            and "/adgroup/" in url
            and (
                scenario == "zero"
                or body["advertiser_id"]
                == ("actual-account" if terminal_recovery else "second-account")
            )
        ):
            receipt = {
                "code": 40002,
                "message": "synthetic original parameter rejection",
                "request_id": f"historical-receipt-{len(calls)}",
            }
            if historical:
                historical_receipts[body["request_id"]] = (dict(body), receipt)
            else:
                receipt = {"code": 41021, "message": "fixture rejection"}
            return HTTPResponse(
                body=json.dumps(receipt).encode(),
                status=200,
            )
        key = (
            "creative_portfolio_id"
            if "portfolio" in url
            else "campaign_id"
            if "/campaign/" in url
            else "adgroup_id"
            if "/adgroup/" in url
            else "smart_plus_ad_id"
        )
        if "portfolio" not in url:
            # transport double 保存真实请求；独立字段 oracle 复用 M1 回读合同测试。
            objects[f"remote-{len(calls)}"] = {**body, key: f"remote-{len(calls)}"}
            objects[f"remote-{len(calls)}"].pop("request_id", None)
            if "/campaign/" in url:
                objects[f"remote-{len(calls)}"]["campaign_type"] = "REGULAR_CAMPAIGN"
            if "/adgroup/" in url:
                objects[f"remote-{len(calls)}"]["billing_event"] = "OCPM"
        if (
            terminal_recovery
            and "/ad/create/" in url
            and body["advertiser_id"] == "second-account"
            and len(
                [
                    u
                    for u, b in calls
                    if "/ad/create/" in u and b["advertiser_id"] == "second-account"
                ]
            )
            == 2
        ):
            raise TimeoutError("last Ad physical response lost")
        return HTTPResponse(
            body=json.dumps(
                {
                    "code": 0,
                    "data": {key: f"remote-{len(calls)}"},
                    "request_id": f"request-{len(calls)}",
                }
            ).encode(),
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", transport)
    monkeypatch.setattr(execution, "_require_bounded_worker", lambda: None)

    def cancel_and_snapshot_sent():
        from tests.modules.builds.native_cancel_http import cancel_twice

        cancel_twice(native.engine, context, sid)
        with Session(native.engine) as s:
            return {
                step.id: (
                    step.request_body,
                    step.request_body_digest,
                    step.attempt,
                    step.attempt_id,
                    step.remote_id,
                    step.status,
                    step.error_code,
                )
                for step in s.exec(
                    select(ExecutionStep).where(
                        ExecutionStep.submission_id == sid
                    )
                ).all()
                if step.request_body is not None or step.remote_id is not None
            }

    from tests.modules.builds.native_cancel_http import cancel_twice

    if scenario == "cancel-before":
        from tests.modules.builds.native_browser_http import submit_in_browser

        # T19 真实浏览器取消后，再用原 Submission API 验证幂等。
        submit_in_browser(
            native.engine,
            context,
            job_env["bc_id"],
            pid,
            mode="batch-cancel",
            submission_id=sid,
        )
        cancel_twice(native.engine, context, sid)
        with Session(native.engine) as s:
            view = submissions.get_submission(s, context=context, submission_id=sid)
            assert view.status == "CANCELLED", "T19 all unsent must be cancelled"
            assert all(p.status == "CANCELLED" for p in view.native_plans)
            before_steps = [(x.id, x.request_body, x.attempt_id) for x in s.exec(
                select(ExecutionStep).where(ExecutionStep.submission_id == sid)
            ).all()]
        for step in steps:
            execution.process_step(database_engine=native.engine, redis_client=redis_client,
                context=context, step_id=step.id, revision=step.dispatch_revision)
        assert calls == [], "R11 cancelled steps must never send"
        assert submit(0).submission_id == sid
        with Session(native.engine) as s:
            after_steps = [(x.id, x.request_body, x.attempt_id) for x in s.exec(
                select(ExecutionStep).where(ExecutionStep.submission_id == sid)
            ).all()]
            assert after_steps == before_steps
        return
    if lock_probe:
        from app.core.context import TenantContext
        from app.core.errors import DomainError

        with pytest.raises(DomainError):
            execution.process_step(
                database_engine=native.engine,
                redis_client=redis_client,
                context=TenantContext(
                    tenant_id=uuid4(), actor_id=context.actor_id, role="operator"
                ),
                step_id=steps[0].id,
                revision=0,
            )
        assert calls == []
    kinds = ["MATERIAL", "CTA", "CAMPAIGN", "ADGROUP", "AD"]
    if scenario == "cancel-partial":
        with Session(native.engine) as s:
            advertisers = {
                unit.id: unit.advertiser_id
                for unit in s.exec(
                    select(BuildUnit).where(BuildUnit.preview_id == pid)
                ).all()
            }
        ordered = sorted(
            steps,
            key=lambda step: (
                advertisers[step.unit_id] != "actual-account",
                kinds.index(step.kind),
            ),
        )
        groups = [(step.kind, [step]) for step in ordered]
    else:
        groups = [
            (kind, [step for step in steps if step.kind == kind]) for kind in kinds
        ]
    sent_at_cancel = {}
    for kind, current_steps in groups:
        if (
            scenario == "cancel-partial"
            and advertisers[current_steps[0].unit_id] == "second-account"
            and not sent_at_cancel
        ):
            sent_at_cancel = cancel_and_snapshot_sent()
        for step in current_steps:
            from app.modules.builds.dispatch import queue_step

            if (
                native_ad_state
                and kind == "AD"
                and [
                    candidate for candidate in current_steps
                    if candidate.unit_id == step.unit_id
                ].index(step) == 0
            ):
                sibling = current_steps[current_steps.index(step) + 1]
                with Session(native.engine) as s, s.begin():
                    sibling = s.get(ExecutionStep, sibling.id)
                    if native_ad_state == "QUEUED":
                        queue_step(s, step=sibling, submission=s.get(Submission, sid))
                    elif native_ad_state == "RUNNING":
                        assert submissions.claim_step(
                            s, context=context, step_id=sibling.id, owner=uuid4()
                        ) is not None
                    elif native_ad_state == "RETRYABLE":
                        # 调度器合法状态代表；不是把 SDK 明确拒绝伪装成可重试。
                        sibling.status = "RETRYABLE"
                        s.add(sibling)
            with Session(native.engine) as s, s.begin():
                queued = s.get(ExecutionStep, step.id)
                if queued.status == "PENDING" and not (
                    native_ad_state == "PENDING"
                    and kind == "AD"
                    and [
                        candidate for candidate in current_steps
                        if candidate.unit_id == step.unit_id
                    ].index(step) == 1
                ):
                    queue_step(s, step=queued, submission=s.get(Submission, sid))
                delivery = queued.dispatch_revision
            before = len(calls)
            if delivery:
                execution.process_step(
                    database_engine=native.engine,
                    redis_client=redis_client,
                    context=context,
                    step_id=step.id,
                    revision=delivery - 1,
                )
                assert len(calls) == before
            before = len(calls)
            held_sibling = (
                native_ad_state
                and kind == "AD"
                and [
                    candidate for candidate in current_steps
                    if candidate.unit_id == step.unit_id
                ].index(step) == 1
            )
            if held_sibling:
                result = "HELD"
            else:
                result = execution.process_step(
                    database_engine=native.engine,
                    redis_client=redis_client,
                    context=context,
                    step_id=step.id,
                    revision=delivery,
                )
            with Session(native.engine) as s:
                saved = s.get(ExecutionStep, step.id)
                if native_ad_state and kind == "AD":
                    if held_sibling:
                        assert saved.status == native_ad_state
                    else:
                        assert result == (
                            "FAILED"
                            if [
                                candidate for candidate in current_steps
                                if candidate.unit_id == step.unit_id
                            ].index(step) == 0
                            else "SUCCEEDED"
                        ), (kind, saved.error_code, saved.resolved)
                elif scenario == "match":
                    assert result == "SUCCEEDED", (
                        kind,
                        saved.error_code,
                        saved.resolved,
                    )
            before = len(calls)
            if not held_sibling:
                execution.process_step(
                    database_engine=native.engine,
                    redis_client=redis_client,
                    context=context,
                    step_id=step.id,
                    revision=delivery,
                )
            assert len(calls) == before, "R9 duplicate worker added transport write"
        if scenario == "partial" and kind == "ADGROUP":
            with Session(native.engine) as s:
                failed_units = [
                    unit.id for unit in s.exec(
                        select(BuildUnit).where(BuildUnit.preview_id == pid)
                    ).all()
                    if unit.advertiser_id == "second-account"
                ]
                revisions = {
                    row.unit_id: row.dispatch_revision
                    for row in s.exec(
                        select(SubmissionUnit).where(SubmissionUnit.submission_id == sid)
                    ).all()
                }
            for unit_id in failed_units:
                dispatch.process_unit(
                    database_engine=native.engine,
                    context=context,
                    payload={
                        "unit_id": str(unit_id),
                        "revision": revisions[unit_id],
                    },
                )
            with Session(native.engine) as s:
                descendants = s.exec(
                    select(ExecutionStep).where(
                        ExecutionStep.submission_id == sid,
                        ExecutionStep.unit_id.in_(failed_units),
                        ExecutionStep.kind == "AD",
                    )
                ).all()
                assert len(descendants) == 2
                assert all(row.status == "FAILED" for row in descendants)
                assert all(row.error_code == "dependency_failed" for row in descendants)
                assert all(row.request_body is None for row in descendants)
                assert all(row.remote_id is None for row in descendants)
                view = submissions.get_submission(
                    s, context=context, submission_id=sid
                )
                assert view.status == "RUNNING"
        if scenario == "zero" and kind == "ADGROUP":
            with Session(native.engine) as s:
                revisions = {
                    row.unit_id: row.dispatch_revision
                    for row in s.exec(
                        select(SubmissionUnit).where(SubmissionUnit.submission_id == sid)
                    ).all()
                }
            for unit_id, revision in revisions.items():
                dispatch.process_unit(
                    database_engine=native.engine,
                    context=context,
                    payload={"unit_id": str(unit_id), "revision": revision},
                )
            with Session(native.engine) as s:
                descendants = s.exec(
                    select(ExecutionStep).where(
                        ExecutionStep.submission_id == sid,
                        ExecutionStep.kind == "AD",
                    )
                ).all()
                assert len(descendants) == 4
                assert all(row.status == "FAILED" for row in descendants)
                assert all(row.error_code == "dependency_failed" for row in descendants)
                assert all(row.request_body is None for row in descendants)
                view = submissions.get_submission(
                    s, context=context, submission_id=sid
                )
                assert view.status == "FAILED"
        if native_ad_state and kind == "AD":
            for unit_id in {step.unit_id for step in current_steps}:
                same_unit = [step for step in current_steps if step.unit_id == unit_id]
                assert len(same_unit) == 2
                failed_ad, held_ad = same_unit
                with Session(native.engine) as s:
                    failed_ad = s.get(ExecutionStep, failed_ad.id)
                    held_ad = s.get(ExecutionStep, held_ad.id)
                    assert failed_ad.status == "FAILED"
                    assert failed_ad.error_code == "tiktok_business_error"
                    assert failed_ad.resolved["business_rejection_counts"] == {"40002": 1}
                    assert held_ad.status == native_ad_state
                    assert held_ad.request_body is None
                    assert held_ad.remote_id is None
            from types import SimpleNamespace

            from fastapi import FastAPI
            from fastapi.testclient import TestClient

            from app.api.deps import get_current_user, get_db
            from app.core.errors import DomainError, domain_error_handler
            from app.modules.builds import reconciliation
            from app.modules.builds.submission_api import router

            # 真实 READBACK 清空前置等待，避免把 READBACK 等待误当 R1 反例。
            monkeypatch.setattr(
                reconciliation,
                "current_task",
                SimpleNamespace(request=SimpleNamespace(
                    timelimit=(45, 40), called_directly=False, is_eager=False
                )),
            )
            monkeypatch.setattr(
                reconciliation,
                "current_process",
                lambda: SimpleNamespace(daemon=True, name="ForkPoolWorker-1"),
            )
            with Session(native.engine) as s:
                reads = s.exec(select(ExecutionStep).where(
                    ExecutionStep.submission_id == sid,
                    ExecutionStep.kind == "READBACK",
                )).all()
            for read in reads:
                result = reconciliation.process_reconciliation(
                    database_engine=native.engine,
                    redis_client=redis_client,
                    context=context,
                    step_id=read.id,
                    revision=read.dispatch_revision,
                )
                assert result.state == "SUCCEEDED", result
            app = FastAPI()
            app.include_router(router)
            app.add_exception_handler(DomainError, domain_error_handler)

            def database():
                with Session(native.engine) as db:
                    yield db

            app.dependency_overrides[get_db] = database
            app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
                id=context.actor_id
            )
            with TestClient(app) as reader:
                response = reader.get(
                    f"/tenants/{context.tenant_id}/submissions/{sid}"
                )
            assert response.status_code == 200, response.text
            # Spec T12/T13：实际非全 MATCH 且仍有可执行 Ad，必须 RUNNING。
            assert response.json()["status"] == "RUNNING", response.text
        if scenario == "cancel-after-campaign" and kind == "CAMPAIGN":
            sent_at_cancel = cancel_and_snapshot_sent()
        if scenario == "cancel-unknown" and kind == "CAMPAIGN":
            with Session(native.engine) as s:
                unknown_exists = s.exec(
                    select(ExecutionStep.id).where(
                        ExecutionStep.submission_id == sid,
                        ExecutionStep.kind == "CAMPAIGN",
                        ExecutionStep.status == "UNKNOWN",
                    )
                ).first()
            if unknown_exists is not None and not sent_at_cancel:
                sent_at_cancel = cancel_and_snapshot_sent()
    assert len(calls) == (
        8 if native_ad_state else {
            "match": 10,
            "partial": 8,
            "zero": 6,
            "unknown": 7,
            "cancel-partial": 5,
            "cancel-after-campaign": 4,
            "cancel-unknown": 4,
            "reject": 4,
            "recovery-complete": 10,
            "recovery-partial": 8,
        }[scenario]
    )
    if lock_probe:
        assert locked == [True, True]
    assert {b["advertiser_id"] for _, b in calls} == (
        {"actual-account"}
        if scenario == "cancel-partial"
        else {"actual-account", "second-account"}
    )
    assert all(
        b["operation_status"] == "ENABLE" for u, b in calls if "portfolio" not in u
    )
    assert [b["budget"] for u, b in calls if "/campaign/" in u] == (
        [100.25] if scenario == "cancel-partial" else [100.25, 100.25]
    )
    with Session(native.engine) as s:
        view = submissions.get_submission(s, context=context, submission_id=sid)
        assert view.status != "COMPLETED"  # 创建回执不能冒充实际 MATCH。
        assert len(view.native_plans) == (3 if browser else 2)
        if scenario == "match" and not native_ad_state:
            assert all(
                len(p.remote_ids) == 4
                for p in view.native_plans
                if p.status != "EXCLUDED"
            )
    assert submit(0).submission_id == sid
    assert len(calls) == (
        8 if native_ad_state else {
            "match": 10,
            "partial": 8,
            "zero": 6,
            "unknown": 7,
            "cancel-partial": 5,
            "cancel-after-campaign": 4,
            "cancel-unknown": 4,
            "reject": 4,
            "recovery-complete": 10,
            "recovery-partial": 8,
        }[scenario]
    )

    from types import SimpleNamespace

    from app.modules.builds import reconciliation

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
    with Session(native.engine) as s:
        reads = s.exec(
            select(ExecutionStep).where(
                ExecutionStep.submission_id == sid, ExecutionStep.kind == "READBACK"
            )
        ).all()
    assert len(reads) == (
        4 if native_ad_state else {
            "match": 8,
            "partial": 5,
            "zero": 2,
            "unknown": 4,
            "cancel-partial": 4,
            "cancel-after-campaign": 2,
            "cancel-unknown": 1,
            "reject": 0,
            "recovery-complete": 7,
            "recovery-partial": 4,
        }[scenario]
    )
    for step in reads:
        result = reconciliation.process_reconciliation(
            database_engine=native.engine,
            redis_client=redis_client,
            context=context,
            step_id=step.id,
            revision=step.dispatch_revision,
        )
        assert result.state == "SUCCEEDED", result
    if native_ad_state:
        from datetime import UTC, datetime, timedelta

        from app.modules.builds import dispatch
        with Session(native.engine) as s:
            held = s.exec(
                select(ExecutionStep).where(
                    ExecutionStep.submission_id == sid,
                    ExecutionStep.kind == "AD",
                    ExecutionStep.status.in_(
                        ["PENDING", "QUEUED", "RUNNING", "RETRYABLE"]
                    ),
                )
            ).all()
            assert len(held) == 2
            held = [
                {"id": step.id, "status": step.status}
                for step in held
            ]
            for step in held:
                if step["status"] == "RUNNING":
                    row = s.get(ExecutionStep, step["id"])
                    row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
                    s.add(row)
            s.commit()
        if native_ad_state == "RUNNING":
            dispatch.repair_execution(database_engine=native.engine)
        for step in held:
            with Session(native.engine) as s, s.begin():
                queued = s.get(ExecutionStep, step["id"])
                queue_step(s, step=queued, submission=s.get(Submission, sid))
                delivery = queued.dispatch_revision
            result = execution.process_step(
                database_engine=native.engine,
                redis_client=redis_client,
                context=context,
                step_id=step["id"],
                revision=delivery,
            )
            assert result == "SUCCEEDED", (step["id"], result)
        with Session(native.engine) as s:
            reads = s.exec(
                select(ExecutionStep).where(
                    ExecutionStep.submission_id == sid,
                    ExecutionStep.kind == "READBACK",
                    ExecutionStep.status.in_(
                        ["PENDING", "QUEUED", "RUNNING", "RETRYABLE"]
                    ),
                )
            ).all()
            assert len(reads) == 2
            reads = [
                {"id": step.id, "revision": step.dispatch_revision}
                for step in reads
            ]
        for step in reads:
            result = reconciliation.process_reconciliation(
                database_engine=native.engine,
                redis_client=redis_client,
                context=context,
                step_id=step["id"],
                revision=step["revision"],
            )
            assert result.state == "SUCCEEDED", result
    with Session(native.engine) as s:
        view = submissions.get_submission(s, context=context, submission_id=sid)
        assert view.status == (
            "FAILED" if native_ad_state else {
                "match": "COMPLETED",
                "partial": "PARTIAL",
                "zero": "FAILED",
                "unknown": "NEEDS_REVIEW",
                "cancel-partial": "PARTIAL",
                "cancel-after-campaign": "FAILED",
                "cancel-unknown": "NEEDS_REVIEW",
                "reject": "FAILED",
                "recovery-complete": "NEEDS_REVIEW",
                "recovery-partial": "NEEDS_REVIEW",
            }[scenario]
        ), view.model_dump()
        if native_ad_state:
            assert all(p.status == "PARTIAL" for p in view.native_plans), [
                (p.status, p.remote_ids) for p in view.native_plans
            ]
            assert all(len(p.remote_ids) == 3 for p in view.native_plans)
        if scenario == "match" and not native_ad_state:
            assert all(
                p.status == "COMPLETED"
                for p in view.native_plans
                if p.status != "EXCLUDED"
            )
        if scenario == "zero":
            assert all(
                p.remote_ids for p in view.native_plans if p.status != "EXCLUDED"
            )
        if scenario == "cancel-partial":
            assert [p.status for p in view.native_plans] == [
                "COMPLETED",
                "CANCELLED",
            ]
            assert view.native_plans[0].remote_ids
            assert not view.native_plans[1].remote_ids
        if scenario == "cancel-after-campaign":
            assert [p.status for p in view.native_plans] == ["PARTIAL", "PARTIAL"]
            assert all(p.remote_ids for p in view.native_plans)
        if scenario == "cancel-unknown":
            assert [p.status for p in view.native_plans] == [
                "PARTIAL",
                "NEEDS_REVIEW",
            ]
    assert submit(0).submission_id == sid

    # 终态/待核查：相同键及新键在独立会话并发重放，读取当前回执。
    request_key = uuid4()

    def replay(key):
        with Session(native.engine, expire_on_commit=False) as s, s.begin():
            receipt = submissions.submit_preview(
                s, context=context, preview_id=pid, request_id=key
            )
            current = submissions.get_submission(
                s, context=context, submission_id=receipt.submission_id
            )
            assert receipt.status == current.status
            return receipt.submission_id, current.model_dump(mode="json")

    before_calls = list(calls)
    with Session(native.engine) as s:
        if sent_at_cancel:
            for step in s.exec(
                select(ExecutionStep).where(ExecutionStep.submission_id == sid)
            ).all():
                if step.id in sent_at_cancel:
                    assert (
                        step.request_body,
                        step.request_body_digest,
                        step.attempt,
                        step.attempt_id,
                        step.remote_id,
                        step.status,
                        step.error_code,
                    ) == sent_at_cancel[step.id]
        before_steps = [
            (x.id, x.request_body, x.request_body_digest, x.remote_id)
            for x in s.exec(
                select(ExecutionStep)
                .where(ExecutionStep.submission_id == sid)
                .order_by(ExecutionStep.id)
            ).all()
        ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        replayed = list(pool.map(replay, [request_key, request_key, uuid4(), uuid4()]))
    assert all(identity == sid for identity, _ in replayed)
    assert all(receipt["status"] == view.status for _, receipt in replayed)
    with Session(native.engine) as s:
        assert before_steps == [
            (x.id, x.request_body, x.request_body_digest, x.remote_id)
            for x in s.exec(
                select(ExecutionStep)
                .where(ExecutionStep.submission_id == sid)
                .order_by(ExecutionStep.id)
            ).all()
        ]
        assert (
            len(s.exec(select(Submission).where(Submission.preview_id == pid)).all())
            == 1
        )
    assert calls == before_calls
    if lock_probe:
        from sqlalchemy import text
        from sqlalchemy.exc import DBAPIError

        # 已冻结/armed 数据不能通过普通 UPDATE 变造；不禁用数据库 trigger。
        mutations = [
            (
                "UPDATE build_preview SET config=config || jsonb_build_object('tampered',true) WHERE id=:id",
                pid,
            ),
            ("UPDATE build_preview SET content_digest='wrong' WHERE id=:id", pid),
            (
                "UPDATE build_unit SET scene_snapshot=scene_snapshot || jsonb_build_object('tampered',true) WHERE preview_id=:id",
                pid,
            ),
            (
                "UPDATE execution_step SET request_body='{}'::jsonb WHERE id=:id",
                next(x.id for x in steps if x.kind == "CAMPAIGN"),
            ),
            (
                "UPDATE execution_step SET request_body_digest='wrong' WHERE id=:id",
                next(x.id for x in steps if x.kind == "CAMPAIGN"),
            ),
            ("UPDATE execution_step SET bc_id='wrong-bc' WHERE id=:id", steps[0].id),
            ("UPDATE execution_step SET tenant_id=:tenant WHERE id=:id", steps[0].id),
        ]
        for statement, identity in mutations:
            with Session(native.engine) as s, s.begin():
                with pytest.raises(DBAPIError), s.begin_nested():
                    s.execute(text(statement), {"id": identity, "tenant": uuid4()})
        assert calls == before_calls
    if historical:
        from tests.modules.builds.test_native_batch_recovery import recover_historical

        recover_historical(
            client,
            native.engine,
            context,
            redis_client,
            monkeypatch,
            sid,
            calls,
            pid,
            historical_receipts,
        )
    if terminal_recovery:
        from tests.modules.builds.test_native_batch_recovery import recover_terminal

        recover_terminal(
            client,
            native.engine,
            context,
            redis_client,
            monkeypatch,
            sid,
            calls,
            objects,
            "PARTIAL" if terminal_recovery == "recovery-partial" else "COMPLETED",
            pid,
            browser_bc=job_env["bc_id"] if browser else None,
        )
    if recovering:
        from tests.modules.builds.test_native_batch_recovery import recover_batch

        recover_batch(
            client, native.engine, context, redis_client, monkeypatch, sid, calls
        )
    if browser and not terminal_recovery:
        submit_in_browser(
            native.engine,
            context,
            job_env["bc_id"],
            pid,
            mode="batch-result",
            submission_id=sid,
        )


@pytest.mark.parametrize("boundary", ["queue", "send"])
@pytest.mark.parametrize(
    "change",
    [
        "account",
        "actor",
        "authorization",
        "binding",
        "revision",
        "user_regrant",
        "member_regrant",
        "member_recreate",
        "tenant_regrant",
        "superuser_regrant",
        "unrelated",
        "fresh_approval",
    ],
)
def test_batch_fences(job_env, wire, redis_client, monkeypatch, boundary, change):
    import json
    from uuid import uuid4

    from sqlalchemy import text
    from urllib3.response import HTTPResponse

    from app.modules.builds import dispatch, execution, submissions
    from app.modules.builds.execution_models import ExecutionStep, Submission

    context = job_env["context"]
    client, url, draft_id, _, revision, value, _ = two_account_workspace(
        job_env, wire, redis_client
    )
    response = client.post(url + "/previews", json={"expected_revision": revision})
    assert response.status_code == 202, response.text
    pid = UUID(response.json()["preview_id"])
    if change == "fresh_approval":
        from app.crud import update_user
        from app.models import User, UserUpdate

        with Session(native.engine) as s:
            actor = s.get(User, context.actor_id)
            for active in (False, True):
                update_user(
                    session=s, db_user=actor, user_in=UserUpdate(is_active=active)
                )
    with Session(native.engine, expire_on_commit=False) as s, s.begin():
        sid = submissions.submit_preview(
            s, context=context, preview_id=pid, request_id=uuid4()
        ).submission_id
        while not submissions.expand_submission(s, context=context, submission_id=sid):
            pass
        approved_stamp = s.get(Submission, sid).authorization_stamp
        original_preview = s.get(BuildPreview, pid).content_digest
        steps = s.exec(
            select(ExecutionStep).where(ExecutionStep.submission_id == sid)
        ).all()
    monkeypatch.setattr(execution, "_require_bounded_worker", lambda: None)
    calls = []

    def transport(pool, method, url, **kw):
        calls.append(json.loads(kw["body"]))
        return HTTPResponse(
            body=json.dumps(
                {"code": 0, "data": {"creative_portfolio_id": "fence-cta"}}
            ).encode(),
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", transport)
    for step in steps:
        if step.kind == "MATERIAL":
            assert (
                execution.process_step(
                    database_engine=native.engine,
                    redis_client=redis_client,
                    context=context,
                    step_id=step.id,
                    revision=0,
                )
                == "SUCCEEDED"
            )
    with Session(native.engine) as s:
        units = {
            u.id: u.advertiser_id
            for u in s.exec(select(BuildUnit).where(BuildUnit.preview_id == pid)).all()
        }
    target = next(
        x for x in steps if x.kind == "CTA" and units[x.unit_id] == "second-account"
    )
    sibling = next(
        x for x in steps if x.kind == "CTA" and units[x.unit_id] == "actual-account"
    )
    assert (
        execution.process_step(
            database_engine=native.engine,
            redis_client=redis_client,
            context=context,
            step_id=sibling.id,
            revision=0,
        )
        == "SUCCEEDED"
    )

    def queue():
        with Session(native.engine) as s, s.begin():
            row = s.get(ExecutionStep, target.id)
            dispatch.queue_step(s, step=row, submission=s.get(Submission, sid))
            return row.dispatch_revision

    delivery = queue() if boundary == "send" else 0
    if change in {"user_regrant", "member_regrant"}:
        from app.core.context import TenantContext
        from app.crud import update_user
        from app.models import User, UserUpdate
        from app.modules.tenants.service import set_member

        with Session(native.engine) as s:
            if change == "user_regrant":
                actor = s.get(User, context.actor_id)
                update_user(
                    session=s, db_user=actor, user_in=UserUpdate(is_active=False)
                )
                update_user(
                    session=s, db_user=actor, user_in=UserUpdate(is_active=True)
                )
            else:
                admin = User(
                    username="stamp-admin-" + uuid4().hex,
                    hashed_password="unused",
                    is_superuser=True,
                )
                s.add(admin)
                s.commit()
                manager = TenantContext(
                    tenant_id=context.tenant_id,
                    actor_id=admin.id,
                    role="platform_admin",
                )
                for role in ("viewer", "operator"):
                    set_member(
                        s,
                        context=manager,
                        user_id=context.actor_id,
                        role=role,
                        active=True,
                    )
                    s.commit()
    elif change in {
        "member_recreate",
        "tenant_regrant",
        "superuser_regrant",
        "unrelated",
    }:
        from app.crud import update_user
        from app.models import User, UserUpdate
        from app.modules.tenants.models import Tenant, TenantMembership

        with Session(native.engine) as s:
            if change == "member_recreate":
                member = s.get(TenantMembership, (context.tenant_id, context.actor_id))
                role = member.role
                s.delete(member)
                s.commit()
                s.add(
                    TenantMembership(
                        tenant_id=context.tenant_id, user_id=context.actor_id, role=role
                    )
                )
                s.commit()
            elif change == "superuser_regrant":
                actor = s.get(User, context.actor_id)
                for superuser in (True, False):
                    update_user(
                        session=s,
                        db_user=actor,
                        user_in=UserUpdate(is_superuser=superuser),
                    )
            elif change == "tenant_regrant":
                from app.modules.tenants.service import update_tenant

                admin = User(
                    username="stamp-admin-" + uuid4().hex,
                    hashed_password="unused",
                    is_superuser=True,
                )
                s.add(admin)
                s.commit()
                for active in (False, True):
                    update_tenant(
                        s,
                        actor_id=admin.id,
                        tenant_id=context.tenant_id,
                        changes={"active": active},
                    )
                    s.commit()
            else:
                other = User(
                    username="stamp-other-" + uuid4().hex, hashed_password="unused"
                )
                tenant = Tenant(name="stamp-other-" + uuid4().hex)
                s.add(other)
                s.add(tenant)
                s.commit()
                s.add(
                    TenantMembership(
                        tenant_id=context.tenant_id, user_id=other.id, role="viewer"
                    )
                )
                s.add(
                    TenantMembership(
                        tenant_id=tenant.id, user_id=context.actor_id, role="viewer"
                    )
                )
                s.commit()
                update_user(
                    session=s, db_user=other, user_in=UserUpdate(is_active=False)
                )
    elif change == "fresh_approval":
        pass
    elif change == "revision":
        value["plans"][1]["selection"]["native_series_id"] = "changed-series"
        patched = client.patch(
            url, json={"expected_revision": revision, "scene_selection": value}
        )
        assert patched.status_code == 200, patched.text
    else:
        sql = {
            "account": "UPDATE bc_account_access SET can_build=false WHERE tenant_id=:tenant AND advertiser_id='second-account'",
            "actor": 'UPDATE "user" SET is_active=false WHERE id=:actor',
            "authorization": "UPDATE tiktok_connection SET authorization_revision=authorization_revision+1 WHERE tenant_id=:tenant",
            "binding": "UPDATE bc_connection_binding SET revision=revision+1 WHERE tenant_id=:tenant",
        }[change]
        with Session(native.engine) as s, s.begin():
            s.execute(
                text(sql), {"tenant": context.tenant_id, "actor": context.actor_id}
            )
    if boundary == "queue":
        delivery = queue()
    before = list(calls)
    try:
        execution.process_step(
            database_engine=native.engine,
            redis_client=redis_client,
            context=context,
            step_id=target.id,
            revision=delivery,
        )
    except Exception as error:
        from app.core.errors import DomainError

        assert isinstance(error, DomainError), error
    assert len(calls) == len(before) + (
        1 if change in {"unrelated", "fresh_approval"} else 0
    )
    with Session(native.engine) as s:
        row = s.get(ExecutionStep, target.id)
        assert s.get(Submission, sid).authorization_stamp == approved_stamp
        assert s.get(BuildPreview, pid).content_digest == original_preview
        if change in {"unrelated", "fresh_approval"}:
            assert row.remote_id == "fence-cta"
        else:
            assert row.remote_id is None
        if boundary == "queue" and change not in {"unrelated", "fresh_approval"}:
            assert row.status == "FAILED" and row.error_code
        successful = s.get(ExecutionStep, sibling.id)
        assert successful.status == "SUCCEEDED" and successful.remote_id == "fence-cta"


def test_legacy_approval_readable_but_cannot_send(job_env, wire, redis_client):
    from sqlalchemy.exc import IntegrityError

    from app.modules.builds import dispatch, submissions
    from app.modules.builds.execution_models import ExecutionStep, Submission

    context = job_env["context"]
    client, url, draft_id, _, revision, _, _ = two_account_workspace(
        job_env, wire, redis_client
    )
    response = client.post(url + "/previews", json={"expected_revision": revision})
    assert response.status_code == 202
    pid = UUID(response.json()["preview_id"])
    with Session(native.engine) as s, s.begin():
        preview = s.get(BuildPreview, pid)
        digest, config = preview.content_digest, preview.config
        # 合法历史形状：迁移不能假装取得操作者的新批准。
        row = Submission(
            tenant_id=context.tenant_id,
            bc_id=job_env["bc_id"],
            preview_id=pid,
            draft_id=draft_id,
            actor_id=context.actor_id,
            ordinal=1,
        )
        s.add(row)
        s.flush()
        while not submissions.expand_submission(
            s, context=context, submission_id=row.id
        ):
            pass
        step = s.exec(
            select(ExecutionStep).where(
                ExecutionStep.submission_id == row.id, ExecutionStep.kind == "CTA"
            )
        ).first()
        dispatch.queue_step(s, step=step, submission=row)
        assert step.status == "FAILED"
        assert step.error_code == "actor_authorization_changed"
        assert step.dispatch_id is None
        assert (
            submissions.get_submission(
                s, context=context, submission_id=row.id
            ).submission_id
            == row.id
        )
        assert (preview.content_digest, preview.config) == (digest, config)
        with pytest.raises(IntegrityError), s.begin_nested():
            row.authorization_stamp = {"forged": True}
            s.flush()
