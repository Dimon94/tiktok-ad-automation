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
@pytest.mark.parametrize(
    "scenario",
    [
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
    ],
)
def test_batch_submit_and_execute(job_env, wire, redis_client, monkeypatch, scenario):
    import json
    from concurrent.futures import ThreadPoolExecutor
    from uuid import uuid4

    from urllib3.response import HTTPResponse

    from app.modules.builds import execution, submissions
    from app.modules.builds.execution_models import ExecutionStep, Submission

    terminal_recovery = scenario if scenario.startswith("recovery-") else None
    recovering = scenario == "recovery"
    if recovering:
        scenario = "unknown"
    browser = scenario in {"browser", "recovery-browser"}
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
            scenario == "unknown"
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
            return HTTPResponse(
                body=json.dumps(
                    {"code": 41021, "message": "fixture rejection"}
                ).encode(),
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
    for kind in ["MATERIAL", "CTA", "CAMPAIGN", "ADGROUP", "AD"]:
        for step in [x for x in steps if x.kind == kind]:
            from app.modules.builds.dispatch import queue_step

            with Session(native.engine) as s, s.begin():
                queued = s.get(ExecutionStep, step.id)
                if queued.status == "PENDING":
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
            result = execution.process_step(
                database_engine=native.engine,
                redis_client=redis_client,
                context=context,
                step_id=step.id,
                revision=delivery,
            )
            with Session(native.engine) as s:
                saved = s.get(ExecutionStep, step.id)
                if scenario == "match":
                    assert result == "SUCCEEDED", (
                        kind,
                        saved.error_code,
                        saved.resolved,
                    )
            before = len(calls)
            execution.process_step(
                database_engine=native.engine,
                redis_client=redis_client,
                context=context,
                step_id=step.id,
                revision=delivery,
            )
            assert len(calls) == before, "R9 duplicate worker added transport write"
    assert (
        len(calls)
        == {
            "match": 10,
            "partial": 8,
            "zero": 6,
            "unknown": 7,
            "reject": 4,
            "recovery-complete": 10,
            "recovery-partial": 8,
        }[scenario]
    )
    if lock_probe:
        assert locked == [True, True]
    assert {b["advertiser_id"] for _, b in calls} == {
        "actual-account",
        "second-account",
    }
    assert all(
        b["operation_status"] == "ENABLE" for u, b in calls if "portfolio" not in u
    )
    assert [b["budget"] for u, b in calls if "/campaign/" in u] == [100.25, 100.25]
    with Session(native.engine) as s:
        view = submissions.get_submission(s, context=context, submission_id=sid)
        assert view.status != "COMPLETED"  # 创建回执不能冒充实际 MATCH。
        assert len(view.native_plans) == (3 if browser else 2)
        if scenario == "match":
            assert all(
                len(p.remote_ids) == 4
                for p in view.native_plans
                if p.status != "EXCLUDED"
            )
    assert submit(0).submission_id == sid
    assert (
        len(calls)
        == {
            "match": 10,
            "partial": 8,
            "zero": 6,
            "unknown": 7,
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
    assert (
        len(reads)
        == {
            "match": 8,
            "partial": 5,
            "zero": 2,
            "unknown": 4,
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
    with Session(native.engine) as s:
        view = submissions.get_submission(s, context=context, submission_id=sid)
        assert (
            view.status
            == {
                "match": "COMPLETED",
                "partial": "PARTIAL",
                "zero": "FAILED",
                "unknown": "NEEDS_REVIEW",
                "reject": "FAILED",
                "recovery-complete": "NEEDS_REVIEW",
                "recovery-partial": "NEEDS_REVIEW",
            }[scenario]
        ), view.model_dump()
        if scenario == "match":
            assert all(
                p.status == "COMPLETED"
                for p in view.native_plans
                if p.status != "EXCLUDED"
            )
        if scenario == "zero":
            assert all(
                p.remote_ids for p in view.native_plans if p.status != "EXCLUDED"
            )
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
