# ruff: noqa: F811, ARG001
"""Frozen workspace submission through the SDK wire; no business-service doubles."""

import json
from uuid import uuid4

import pytest
from sqlmodel import Session, select
from urllib3.response import HTTPResponse

from app.modules.builds import execution, previews, submissions
from app.modules.builds.execution_models import ExecutionStep, Submission
from app.modules.builds.models import BuildDraft
from tests.modules.builds.scene_jobs import test_native_series as native
from tests.modules.builds.scene_jobs.test_native_series import prepared_native_workspace
from tests.modules.builds.test_native_preview import (  # noqa: F401
    job_env,
    scene_env,
    wire,
)


@pytest.fixture
def retain_build_history(isolated_strategy_database):
    return isolated_strategy_database[0]


@pytest.fixture
def source_env(
    monkeypatch, redis_client, isolated_strategy_database, request, retain_build_history
):
    yield from native.source_env.__wrapped__(
        monkeypatch, redis_client, isolated_strategy_database, request
    )


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "cta_unknown",
        "campaign_reject",
        "group_reject",
        "campaign_unknown",
        "revision",
        "revision_after_cta",
        "cancel_before",
        "cancel_after_campaign",
        "cancel_inflight",
        "unknown_unique",
        "unknown_ambiguous",
        "unknown_cancel",
        "unknown_group",
        "unknown_group_distribution",
        "unknown_ad_addon",
        "unknown_ad",
        "unknown_late",
        "unknown_revoke",
    ],
)
def test_native_six_creates(
    job_env,
    wire,
    redis_client,
    monkeypatch,
    retain_build_history,
    failure,
    readback=None,
    source_mode=None,
    browser=False,
    refresh=None,
    tamper=None,
):
    draft_id, _, _ = prepared_native_workspace(
        job_env,
        wire,
        redis_client,
        True,
        count=6,
        source_mode=source_mode,
        config_changes={
            "group_generation_mode": "FIXED",
            "group_count": 1,
            "group_material_allocation": "SHARED",
            "max_materials_per_group": None,
            "ads_per_group": 1,
            "creative_count": 6,
            "budget": "1000.01",
        },
    )
    context = job_env["context"]
    with Session(native.engine) as s, s.begin():
        draft = s.get(BuildDraft, draft_id)
        pid = previews.generate_preview(
            s, context=context, draft_id=draft_id, expected_revision=draft.revision
        )
        frozen_payload = previews.get_preview_summary(
            s, context=context, preview_id=pid
        ).native_request
    if browser:
        from tests.modules.builds.native_browser_http import submit_in_browser

        submit_in_browser(native.engine, context, job_env["bc_id"], pid)

        with Session(native.engine) as s:
            assert (
                len(
                    s.exec(select(Submission).where(Submission.preview_id == pid)).all()
                )
                == 1
            )
    # 初次提交真实并发竞争，而非先创建后仅验证重放。
    from concurrent.futures import ThreadPoolExecutor

    from app.core.errors import DomainError

    request_id = uuid4()

    def submit_again(index):
        with Session(native.engine) as s, s.begin():
            return submissions.submit_preview(
                s,
                context=context,
                preview_id=pid,
                request_id=request_id if index == 0 else uuid4(),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(submit_again, range(2)))
    receipt = receipts[0]
    assert receipts[1].submission_id == receipt.submission_id
    with Session(native.engine) as s, s.begin():
        with pytest.raises(DomainError, match="幂等键"):
            submissions.submit_preview(
                s, context=context, preview_id=uuid4(), request_id=request_id
            )
        while not submissions.expand_submission(
            s, context=context, submission_id=receipt.submission_id
        ):
            pass
        steps = s.exec(
            select(ExecutionStep).where(
                ExecutionStep.submission_id == receipt.submission_id
            )
        ).all()
        ids = {
            kind: [x.id for x in steps if x.kind == kind]
            for kind in ["MATERIAL", "CTA", "CAMPAIGN", "ADGROUP", "AD"]
        }
    import urllib3

    scene_transport = urllib3.PoolManager.request
    calls = []

    def request(pool, method, url, **kw):
        body = json.loads(kw["body"])
        calls.append((url, body))
        unknown_kind = {
            "unknown_group": "/adgroup/",
            "unknown_group_distribution": "/adgroup/",
            "unknown_ad": "/ad/create/",
            "unknown_ad_addon": "/ad/create/",
        }.get(failure, "/campaign/")
        if (
            failure
            and failure != "unknown_late"
            and (failure == "campaign_unknown" or failure.startswith("unknown_"))
            and unknown_kind in url
            and not any(unknown_kind in u for u, _ in calls[:-1])
        ):
            raise TimeoutError("offline transport response lost")
        if (failure == "cta_unknown" and "portfolio" in url) or (
            failure == "group_reject" and "/adgroup/" in url
        ):
            return HTTPResponse(
                body=json.dumps(
                    {
                        "code": 40002,
                        "message": "fixture rejected",
                        "request_id": "rejected-1",
                    }
                ).encode(),
                status=200,
            )
        if failure == "campaign_reject" and "/campaign/" in url:
            return HTTPResponse(
                body=json.dumps(
                    {
                        "code": 41021,
                        "message": "fixture rejected",
                        "request_id": "rejected-2",
                    }
                ).encode(),
                status=200,
            )
        if failure == "revision_after_cta" and "portfolio" in url:
            # 物理发送已退出事务；在 CTA 回执与下一层发送之间修改输入。
            with Session(native.engine) as changed, changed.begin():
                draft = changed.get(BuildDraft, draft_id)
                draft.revision += 1
                changed.add(draft)
        if failure == "cancel_inflight" and "/campaign/" in url:
            with Session(native.engine) as s, s.begin():
                submissions.cancel_submission(
                    s, context=context, submission_id=receipt.submission_id
                )
        if failure == "unknown_late" and "/campaign/" in url:
            from datetime import UTC, datetime, timedelta

            with Session(native.engine) as s, s.begin():
                step = s.get(ExecutionStep, ids["CAMPAIGN"][0])
                step.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
                s.add(step)
                submissions.cancel_submission(
                    s, context=context, submission_id=receipt.submission_id
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
        return HTTPResponse(
            body=json.dumps(
                {
                    "code": 0,
                    "data": {key: f"remote-{len(calls)}"},
                    "request_id": f"r-{len(calls)}",
                }
            ).encode(),
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", request)
    monkeypatch.setattr(execution, "_require_bounded_worker", lambda: None)
    if failure == "revision":
        with Session(native.engine) as s, s.begin():
            draft = s.get(BuildDraft, draft_id)
            draft.revision += 1
            s.add(draft)
    if failure == "cancel_before":
        from tests.modules.builds.native_cancel_http import cancel_twice

        cancel_twice(native.engine, context, receipt.submission_id)
    if tamper == "after_admission":
        from contextlib import contextmanager

        from app.modules.materials.models import AccountMaterial

        admitted = execution.admitted_build_call

        @contextmanager
        def revoke_after_admission(*args, **kwargs):
            with admitted(*args, **kwargs):
                if len(calls) == 3:
                    with Session(native.engine) as s, s.begin():
                        mapping = s.exec(
                            select(AccountMaterial).where(
                                AccountMaterial.tenant_id == context.tenant_id,
                                AccountMaterial.image_id == "selected-C",
                            )
                        ).one()
                        mapping.status = "unavailable"
                        s.add(mapping)
                yield

        monkeypatch.setattr(execution, "admitted_build_call", revoke_after_admission)
    if tamper in {"wire_cover", "wire_drop"}:
        compile_arguments = execution.create_arguments

        def corrupt_arguments(**kwargs):
            endpoint, body = compile_arguments(**kwargs)
            if "creative_list" in body:
                if tamper == "wire_drop":
                    body["creative_list"].pop()
                else:
                    body["creative_list"][0]["creative_info"]["image_info"][0][
                        "web_uri"
                    ] = "wrong-cover"
            return endpoint, body

        monkeypatch.setattr(execution, "create_arguments", corrupt_arguments)
    for kind in ids:
        if tamper and kind == "AD":
            from app.modules.materials.models import AccountMaterial

            if tamper in {"revoked", "wrong_route", "changed_mapping"}:
                with Session(native.engine) as s, s.begin():
                    mapping = s.exec(
                        select(AccountMaterial).where(
                            AccountMaterial.tenant_id == context.tenant_id,
                            AccountMaterial.image_id == "selected-C",
                        )
                    ).one()
                    if tamper == "revoked":
                        mapping.status = "unavailable"
                    elif tamper == "wrong_route":
                        mapping.frozen_route = {
                            **mapping.frozen_route,
                            "authorization_revision": 999,
                        }
                    else:
                        mapping.image_id = "changed-C"
                    s.add(mapping)
        if refresh and kind == "ADGROUP":
            from datetime import UTC, datetime, timedelta

            from app.modules.accounts.capability_models import CapabilityJob
            from app.modules.builds.preview_models import BuildPreview
            from app.modules.builds.scene_job_models import SceneJob

            with Session(native.engine) as s, s.begin():
                old = s.exec(
                    select(SceneJob).where(
                        SceneJob.tenant_id == context.tenant_id,
                        SceneJob.status == "COMPLETE",
                    )
                ).one()
                if refresh.startswith("capability"):
                    proof = s.exec(
                        select(CapabilityJob).where(
                            CapabilityJob.tenant_id == context.tenant_id,
                            CapabilityJob.status == "COMPLETE",
                        )
                    ).first()
                    proof.expires_at = datetime.now(UTC) - timedelta(seconds=1)
                    s.add(proof)
                else:
                    old.expires_at = datetime.now(UTC) - timedelta(seconds=1)
                    s.add(old)
                original_revision = s.get(BuildDraft, draft_id).revision
                original_digest = s.get(BuildPreview, pid).content_digest
                original_stamp = s.get(
                    Submission, receipt.submission_id
                ).authorization_stamp
                parent_facts = {
                    sid: (
                        row.remote_id,
                        row.request_body,
                        row.request_body_digest,
                        row.attempt,
                        row.attempt_id,
                    )
                    for sid in ids["CAMPAIGN"] + ids["CTA"]
                    if (row := s.get(ExecutionStep, sid))
                }
            assert (
                execution.process_step(
                    database_engine=native.engine,
                    redis_client=redis_client,
                    context=context,
                    step_id=ids[kind][0],
                    revision=0,
                )
                == "PENDING"
            )
            if refresh == "scene_exhausted":
                from app.core.config import settings

                with Session(native.engine) as s, s.begin():
                    waiting = s.get(ExecutionStep, ids[kind][0])
                    waiting.resolved = {
                        **waiting.resolved,
                        "scene_refresh_started_at": (
                            datetime.now(UTC)
                            - timedelta(seconds=settings.SCENE_MAX_AGE_SECONDS + 1)
                        ).isoformat(),
                    }
                    waiting.due_at = datetime.now(UTC) - timedelta(seconds=1)
                    pending_revision = waiting.dispatch_revision
                    s.add(waiting)
                assert (
                    execution.process_step(
                        database_engine=native.engine,
                        redis_client=redis_client,
                        context=context,
                        step_id=ids[kind][0],
                        revision=pending_revision,
                    )
                    == "FAILED"
                )
                assert len(calls) == 2
                return
            monkeypatch.setattr("urllib3.PoolManager.request", scene_transport)
            if refresh.startswith("capability"):
                with Session(native.engine) as s:
                    job = s.exec(
                        select(CapabilityJob).where(
                            CapabilityJob.tenant_id == context.tenant_id,
                            CapabilityJob.status == "PENDING",
                        )
                    ).one()
                wire[1].append(native.role_page(["actual-account"]))
                native.run_capability(
                    {**job_env, "request_id": uuid4()}, redis_client, job.id
                )
                native.run_capability(
                    {**job_env, "request_id": uuid4()}, redis_client, job.id
                )
            else:
                with Session(native.engine) as s:
                    job = s.exec(
                        select(SceneJob).where(
                            SceneJob.tenant_id == context.tenant_id,
                            SceneJob.status == "PENDING",
                        )
                    ).one()
                owner = {
                    "identity_type": "TT_USER",
                    "identity_id": "owner",
                    "available_status": "AVAILABLE",
                    "can_use_series": refresh != "scene_revoked",
                    "can_push_video": True,
                    "is_gpppa": False,
                }
                wire[1].extend(
                    [
                        native.identities([owner]),
                        native.identities([]),
                        {
                            "list": [{"native_series_id": "exact-series"}],
                            "cursor": 0,
                            "has_more": False,
                        },
                        native.region_response()["data"],
                        {
                            "recommend_assets": [
                                {
                                    "asset_ids": [
                                        "changed"
                                        if refresh == "scene_changed"
                                        else "cta-watch"
                                    ],
                                    "asset_content": "Watch now",
                                }
                            ]
                        },
                    ]
                )
                for _ in range(8):
                    job = native.run(job_env, redis_client, job.id)
                    if job.status != "PENDING":
                        break
                assert job.status == "COMPLETE", job.error_code
            monkeypatch.setattr("urllib3.PoolManager.request", request)
            with Session(native.engine) as s, s.begin():
                step = s.get(ExecutionStep, ids[kind][0])
                refresh_revision = step.dispatch_revision
                step.due_at = datetime.now(UTC) - timedelta(seconds=1)
                s.add(step)
                assert s.get(BuildDraft, draft_id).revision == original_revision
                assert s.get(BuildPreview, pid).content_digest == original_digest
                assert (
                    s.get(Submission, receipt.submission_id).authorization_stamp
                    == original_stamp
                )
                for sid, original in parent_facts.items():
                    row = s.get(ExecutionStep, sid)
                    assert (
                        row.remote_id,
                        row.request_body,
                        row.request_body_digest,
                        row.attempt,
                        row.attempt_id,
                    ) == original
            if refresh in {"scene_changed", "scene_revoked"}:
                assert (
                    execution.process_step(
                        database_engine=native.engine,
                        redis_client=redis_client,
                        context=context,
                        step_id=ids[kind][0],
                        revision=refresh_revision,
                    )
                    == "FAILED"
                )
                assert len(calls) == 2
                return
        if failure == "cancel_after_campaign" and kind == "ADGROUP":
            with Session(native.engine) as s, s.begin():
                submissions.cancel_submission(
                    s, context=context, submission_id=receipt.submission_id
                )
        for sid in ids[kind]:
            outcome = execution.process_step(
                database_engine=native.engine,
                redis_client=redis_client,
                context=context,
                step_id=sid,
                revision=refresh_revision if refresh and kind == "ADGROUP" else 0,
            )
            with Session(native.engine) as check:
                saved = check.get(ExecutionStep, sid)
                if failure is None and not (tamper and kind == "AD"):
                    assert outcome == "SUCCEEDED", (
                        kind,
                        saved.error_code,
                        saved.resolved,
                    )
                if saved.status == "UNKNOWN":
                    # 同一消息重放不能再次 create。
                    assert (
                        execution.process_step(
                            database_engine=native.engine,
                            redis_client=redis_client,
                            context=context,
                            step_id=sid,
                            revision=0,
                        )
                        == "UNKNOWN"
                    )
    if tamper:
        assert len(calls) == 3
        with Session(native.engine) as s:
            assert all(s.get(ExecutionStep, sid).remote_id is None for sid in ids["AD"])
            assert all(
                s.get(ExecutionStep, sid).status in {"FAILED", "PENDING"}
                for sid in ids["AD"]
            )
        return
    if failure and failure.startswith("unknown_"):
        from tests.modules.builds.test_native_recovery_timeline import recover_unknown

        recover_unknown(
            native.engine,
            context,
            redis_client,
            monkeypatch,
            receipt.submission_id,
            calls,
            failure,
        )
        return
    if failure and failure.startswith("cancel_"):
        assert len(calls) == (0 if failure == "cancel_before" else 2)
        with Session(native.engine) as s:
            result = submissions.get_submission(
                s, context=context, submission_id=receipt.submission_id
            )
            assert result.local_cancelled
            actual = s.exec(
                select(ExecutionStep).where(
                    ExecutionStep.submission_id == receipt.submission_id
                )
            ).all()
            assert len([x for x in actual if x.remote_id]) == (
                0 if failure == "cancel_before" else 2
            )
            assert all(
                x.status == "FAILED" for x in actual if x.kind in {"ADGROUP", "AD"}
            )
        return
    if failure:
        expected_calls = {
            "cta_unknown": 1,
            "campaign_reject": 2,
            "group_reject": 3,
            "campaign_unknown": 2,
            "revision": 0,
            "revision_after_cta": 1,
        }[failure]
        assert len(calls) == expected_calls
        with Session(native.engine) as s:
            actual = s.exec(
                select(ExecutionStep).where(
                    ExecutionStep.submission_id == receipt.submission_id
                )
            ).all()
            created = [x for x in actual if x.remote_id]
            assert (
                len(created)
                == {
                    "cta_unknown": 0,
                    "campaign_reject": 1,
                    "group_reject": 2,
                    "campaign_unknown": 1,
                    "revision": 0,
                    "revision_after_cta": 1,
                }[failure]
            )
            result = submissions.get_submission(
                s, context=context, submission_id=receipt.submission_id
            )
            assert result.status in (
                {"FAILED"}
                if failure in {"campaign_reject", "revision", "revision_after_cta"}
                else {"PARTIAL", "NEEDS_REVIEW"}
            )
        if readback == "partial":
            from tests.modules.builds.test_native_readback_timeline import (
                reconcile_created,
            )

            reconcile_created(
                native.engine,
                context,
                redis_client,
                monkeypatch,
                receipt.submission_id,
                calls,
                readback,
            )
        return

    assert len(calls) == 9
    campaign = next(b for u, b in calls if "/campaign/" in u)
    assert campaign["budget"] == 1000.01 and campaign["app_promotion_type"] == "MINIS"
    group = next(b for u, b in calls if "/adgroup/" in u)
    assert (
        group["promotion_type"] == "NATIVE_SERIES"
        and group["native_series_id"] == "exact-series"
    )
    assert (
        group["optimization_event"] == "AD_REVENUE_VALUE"
        and group["vbo_window"] == "ZERO_DAY"
    )
    assert "budget" not in group and "series_identity" not in group
    assert {
        k: group.get(k) for k in frozen_payload["selection"]["series_identity"]
    } == frozen_payload["selection"]["series_identity"]
    assert group["roas_bid"] == float(frozen_payload["adgroup"]["target_roas"])
    for field in (
        "targeting_spec",
        "placements",
        "placement_type",
        "optimization_goal",
        "deep_bid_type",
        "bid_type",
        "schedule_type",
    ):
        assert group[field] == frozen_payload["adgroup"][field]
    assert group["schedule_start_time"]
    assert group["identity_id"]
    ads = [b for u, b in calls if "/ad/create/" in u]
    assert len(ads) == 6 and len({b["ad_text_list"][0]["ad_text"] for b in ads}) == 6
    assert all(b["creative_list"] == ads[0]["creative_list"] for b in ads)
    assert len(ads[0]["creative_list"]) == 6
    assert {a["ad_text_list"][0]["ad_text"] for a in ads} == {
        a["text"] for a in frozen_payload["ads"]
    }
    for c, m in zip(
        ads[0]["creative_list"], frozen_payload["ads"][0]["materials"], strict=True
    ):
        info = c["creative_info"]
        assert info["ad_format"] == "SINGLE_VIDEO"
        assert info["video_info"]["video_id"] == m["video_id"]
        assert info["image_info"] == [{"web_uri": m["image_id"]}]
        assert {
            k: info.get(k) for k in frozen_payload["selection"]["creative_identity"]
        } == frozen_payload["selection"]["creative_identity"]
    assert {a["ad_configuration"]["call_to_action_id"] for a in ads} == {"remote-1"}
    for url, body in calls:
        if "portfolio" not in url:
            assert body["operation_status"] == "ENABLE"
        assert not {
            "landing_page_url_list",
            "distribution_link",
            "call_to_action_list",
            "interactive_add_on_list",
            "minis_id",
        }.intersection(body)
    with Session(native.engine) as s:
        actual = s.exec(
            select(ExecutionStep).where(
                ExecutionStep.submission_id == receipt.submission_id
            )
        ).all()
        assert {x.remote_id for x in actual if x.remote_id} == {
            f"remote-{i}" for i in range(1, 10)
        }
        assert (
            submissions.get_submission(
                s, context=context, submission_id=receipt.submission_id
            ).status
            != "COMPLETED"
        )

    if readback:
        from tests.modules.builds.test_native_readback_timeline import reconcile_created

        reconcile_created(
            native.engine,
            context,
            redis_client,
            monkeypatch,
            receipt.submission_id,
            calls,
            readback,
        )

    from app.models import User
    from app.modules.builds.dispatch import finalize_submission, process_unit

    process_unit(
        database_engine=native.engine,
        context=context,
        payload={"unit_id": str(actual[0].unit_id), "revision": 0},
    )
    with Session(native.engine) as s, s.begin():
        actor = s.get(User, context.actor_id)
        actor.is_active = False
        s.add(actor)
    finalize_submission(
        database_engine=native.engine,
        context=context,
        submission_id=receipt.submission_id,
    )
    with Session(native.engine) as s:
        status = s.get(Submission, receipt.submission_id).status
        if readback:
            assert status == ("COMPLETED" if readback == "match" else "NEEDS_REVIEW")
        else:
            assert status in {"RUNNING", "NEEDS_REVIEW"}


@pytest.mark.parametrize(
    "change", ["digest", "obsolete", "selection", "revision", "scope", "active"]
)
def test_native_submission_rejects_changed_freeze(
    job_env, wire, redis_client, retain_build_history, change
):
    import copy
    from datetime import UTC, datetime

    from app.core.errors import DomainError
    from app.modules.accounts.models import TikTokConnection
    from app.modules.ads.models import AdObject
    from app.modules.builds.preview_models import BuildPreview

    draft_id, _, _ = prepared_native_workspace(job_env, wire, redis_client, True)
    context = job_env["context"]
    with Session(native.engine) as s, s.begin():
        draft = s.get(BuildDraft, draft_id)
        pid = previews.generate_preview(
            s, context=context, draft_id=draft_id, expected_revision=draft.revision
        )
        preview = s.get(BuildPreview, pid)
        if change == "digest":
            from sqlalchemy.exc import ProgrammingError

            with pytest.raises(ProgrammingError, match="immutable"), s.begin_nested():
                preview.content_digest = "bad"
                s.add(preview)
                s.flush()
            return
        elif change == "obsolete":
            preview.status = "OBSOLETE"
            s.add(preview)
        elif change == "selection":
            value = copy.deepcopy(draft.scene_selection)
            value["plans"][0]["selection"]["native_series_id"] = "another-series"
            draft.scene_selection = value
            s.add(draft)
        elif change == "revision":
            draft.revision += 1
            s.add(draft)
        elif change == "scope":
            connection = s.get(TikTokConnection, job_env["connection_id"])
            connection.authorization_revision += 1
            s.add(connection)
        else:
            locations = preview.config["native_request"]["adgroup"]["location_ids"]
            s.add(
                AdObject(
                    tenant_id=context.tenant_id,
                    advertiser_id="actual-account",
                    kind="adgroup",
                    remote_id="existing-active",
                    ad_type="SMART_PLUS",
                    name="fixture",
                    configuration={
                        "native_series_id": "exact-series",
                        "targeting_spec": {"location_ids": locations},
                    },
                    operation_status="ENABLE",
                    delivery_status="ACTIVE",
                    observed_at=datetime.now(UTC),
                    published_version=1,
                )
            )
        s.flush()
        with pytest.raises(DomainError):
            submissions.submit_preview(
                s, context=context, preview_id=pid, request_id=uuid4()
            )

        assert not s.exec(select(Submission).where(Submission.preview_id == pid)).all()


@pytest.mark.parametrize(
    "scenario",
    [
        "match",
        "difference",
        "rejected",
        "absent",
        "partial",
        "distribution",
        "static_cta",
        "addon",
    ],
)
def test_native_readback_timeline(
    job_env, wire, redis_client, monkeypatch, retain_build_history, scenario
):
    test_native_six_creates(
        job_env,
        wire,
        redis_client,
        monkeypatch,
        retain_build_history,
        "group_reject" if scenario == "partial" else None,
        readback=scenario,
    )


@pytest.mark.parametrize("source_mode", ["explicit", "no_default", "repeated"])
def test_native_selected_cover_execution(
    job_env, wire, redis_client, monkeypatch, retain_build_history, source_mode
):
    test_native_six_creates(
        job_env,
        wire,
        redis_client,
        monkeypatch,
        retain_build_history,
        None,
        source_mode=source_mode,
    )


@pytest.mark.parametrize(
    "source_env", [{"bc_id": "90071992547409931234"}], indirect=True
)
def test_native_browser_submission_timeline(
    job_env, wire, redis_client, monkeypatch, retain_build_history
):
    test_native_six_creates(
        job_env,
        wire,
        redis_client,
        monkeypatch,
        retain_build_history,
        None,
        readback="match",
        browser=True,
    )


@pytest.mark.parametrize(
    "refresh",
    ["scene", "capability", "scene_changed", "scene_revoked", "scene_exhausted"],
)
def test_native_expired_evidence_continues_frozen_plan(
    job_env, wire, redis_client, monkeypatch, retain_build_history, refresh
):
    test_native_six_creates(
        job_env,
        wire,
        redis_client,
        monkeypatch,
        retain_build_history,
        None,
        refresh=refresh,
    )


@pytest.mark.parametrize(
    "tamper",
    [
        "revoked",
        "wrong_route",
        "changed_mapping",
        "wire_cover",
        "wire_drop",
        "after_admission",
    ],
)
def test_native_final_material_fence(
    job_env, wire, redis_client, monkeypatch, retain_build_history, tamper
):
    test_native_six_creates(
        job_env,
        wire,
        redis_client,
        monkeypatch,
        retain_build_history,
        None,
        source_mode="explicit",
        tamper=tamper,
    )
