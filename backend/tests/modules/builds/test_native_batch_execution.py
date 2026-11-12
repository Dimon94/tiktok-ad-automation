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


@pytest.mark.parametrize("scenario", ["match", "partial", "zero", "unknown", "reject"])
def test_batch_submit_and_execute(job_env, wire, redis_client, monkeypatch, scenario):
    import json
    from concurrent.futures import ThreadPoolExecutor
    from uuid import uuid4

    from urllib3.response import HTTPResponse

    from app.modules.builds import execution, submissions
    from app.modules.builds.execution_models import ExecutionStep, Submission

    client, url, _, _, revision, _, _ = two_account_workspace(
        job_env, wire, redis_client
    )
    response = client.post(url + "/previews", json={"expected_revision": revision})
    assert response.status_code == 202, response.text
    pid = UUID(response.json()["preview_id"])
    context = job_env["context"]

    def submit(_):
        with Session(native.engine, expire_on_commit=False) as s, s.begin():
            return submissions.submit_preview(
                s, context=context, preview_id=pid, request_id=uuid4()
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(submit, range(2)))
    sid = receipts[0].submission_id
    assert receipts[1].submission_id == sid
    with Session(native.engine, expire_on_commit=False) as s, s.begin():
        while not submissions.expand_submission(s, context=context, submission_id=sid):
            pass
        steps = s.exec(
            select(ExecutionStep).where(ExecutionStep.submission_id == sid)
        ).all()
        assert (
            len(s.exec(select(Submission).where(Submission.preview_id == pid)).all())
            == 1
        )
    calls = []
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
        calls.append((url, body))
        if (
            scenario == "unknown"
            and "/campaign/" in url
            and body["advertiser_id"] == "second-account"
        ):
            raise TimeoutError("physical send response lost")
        if (scenario == "reject" and "/campaign/" in url) or (
            scenario in {"partial", "zero"}
            and "/adgroup/" in url
            and (scenario == "zero" or body["advertiser_id"] == "second-account")
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
    for kind in ["MATERIAL", "CTA", "CAMPAIGN", "ADGROUP", "AD"]:
        for step in [x for x in steps if x.kind == kind]:
            result = execution.process_step(
                database_engine=native.engine,
                redis_client=redis_client,
                context=context,
                step_id=step.id,
                revision=0,
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
                revision=0,
            )
            assert len(calls) == before
    assert (
        len(calls)
        == {"match": 10, "partial": 8, "zero": 6, "unknown": 7, "reject": 4}[scenario]
    )
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
        assert len(view.native_plans) == 2
        if scenario == "match":
            assert all(len(p.remote_ids) == 4 for p in view.native_plans)
    assert submit(0).submission_id == sid
    assert (
        len(calls)
        == {"match": 10, "partial": 8, "zero": 6, "unknown": 7, "reject": 4}[scenario]
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
        == {"match": 8, "partial": 5, "zero": 2, "unknown": 4, "reject": 0}[scenario]
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
            }[scenario]
        ), view.model_dump()
        if scenario == "match":
            assert all(p.status == "COMPLETED" for p in view.native_plans)
        if scenario == "zero":
            assert all(p.remote_ids for p in view.native_plans)
    assert submit(0).submission_id == sid
