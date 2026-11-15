"""JB-14 exact plan mapping at workspace request boundary."""

from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlmodel import Session

from app.core.errors import DomainError
from app.modules.builds import drafts, previews
from app.modules.builds.models import BuildDraft
from app.modules.builds.scene_jobs import evaluate_native_plan_set
from app.modules.builds.schemas import CreateDraftRequest
from tests.modules.builds.scene_jobs import test_native_series as native
from tests.modules.builds.scene_jobs.test_native_series import (
    job_env as job_env,
)
from tests.modules.builds.scene_jobs.test_native_series import (
    prepared_native_workspace,
    selection,
)
from tests.modules.builds.scene_jobs.test_native_series import (
    scene_env as scene_env,
)
from tests.modules.builds.scene_jobs.test_native_series import (
    source_env as source_env,
)
from tests.modules.builds.scene_jobs.test_native_series import (
    wire as wire,
)


def request(value):
    return CreateDraftRequest(
        request_id=uuid4(),
        bc_id="bc-test",
        strategy_version_id=uuid4(),
        drama_lines=[],
        account_lines=["actual-account"],
        scene_selection=value,
    )


def test_plan_set_preserves_explicit_mapping_and_normalizes_legacy():
    old = request(selection()).scene_selection.model_dump(mode="json")
    assert len(old["plans"]) == 1
    assert request(selection()).scene_selection.model_dump(mode="json") == old
    a, b = str(uuid4()), str(uuid4())
    first = selection() | {"video_sources": [{"material_id": str(uuid4())}]}
    second = first | {"native_series_id": "another-series"}
    data = {
        "kind": first["kind"],
        "plans": [
            {"plan_key": a, "selection": first},
            {"plan_key": b, "selection": second},
        ],
    }
    saved = request(data).scene_selection.model_dump(mode="json")
    assert [p["plan_key"] for p in saved["plans"]] == [a, b]
    assert [p["selection"]["native_series_id"] for p in saved["plans"]] == [
        "exact-series",
        "another-series",
    ]
    with pytest.raises(ValidationError):
        request(data | {"plans": [data["plans"][0], data["plans"][0]]})
    with pytest.raises(ValidationError):
        request(data | {"plans": []})
    with pytest.raises(ValidationError):
        request(
            data
            | {
                "plans": [
                    {"plan_key": str(uuid4()), "selection": first} for _ in range(21)
                ]
            }
        )
    with pytest.raises(ValidationError):
        request(
            data
            | {
                "plans": [
                    {"plan_key": a, "selection": first | {"series_identity": None}}
                ]
            }
        )


def test_independent_preparation_retry_and_revision(job_env, wire, redis_client):
    draft_id, old_prep, _ = prepared_native_workspace(job_env, wire, redis_client, True)
    ctx = job_env["context"]
    with Session(native.engine, expire_on_commit=False) as s, s.begin():
        draft = s.get(BuildDraft, draft_id)
        plan = draft.scene_selection["plans"][0]
        second = {
            "plan_key": str(uuid4()),
            "selection": {
                **plan["selection"],
                "advertiser_id": "not-authorized",
                "native_series_id": "other-series",
            },
        }
        value = {"kind": plan["selection"]["kind"], "plans": [plan, second]}
        rev = drafts.update_draft(
            s,
            context=ctx,
            draft_id=draft_id,
            expected_revision=draft.revision,
            scene_selection=value,
        )
        prep = drafts.prepare_draft(
            s, context=ctx, draft_id=draft_id, request_id=uuid4()
        )
    with Session(native.engine, expire_on_commit=False) as s, s.begin():
        for _ in range(12):
            if drafts.continue_draft(s, context=ctx, task_id=prep):
                break
        draft = s.get(BuildDraft, draft_id)
        result = evaluate_native_plan_set(
            s, context=ctx, draft=draft, route=job_env["route"]
        )
        assert [p.state for p in result.plans] == ["READY", "BLOCKED"]
        proof = result.plans[0].checkpoint.model_dump(mode="json")
        assert result.plans[1].reason_codes
        preview_id = previews.generate_preview(
            s, context=ctx, draft_id=draft_id, expected_revision=rev
        )
        frozen = previews.get_preview_summary(s, context=ctx, preview_id=preview_id)
        assert [p["status"] for p in frozen.native_request["plans"]] == [
            "INCLUDED",
            "EXCLUDED",
        ]
        assert "request" not in frozen.native_request["plans"][1]
        assert frozen.daily_budget_sum == 100.25
        retry = drafts.prepare_draft(
            s, context=ctx, draft_id=draft_id, request_id=uuid4()
        )
        assert retry == prep and draft.revision == rev
        drafts.continue_draft(s, context=ctx, task_id=retry)
        assert (
            evaluate_native_plan_set(
                s, context=ctx, draft=draft, route=job_env["route"]
            )
            .plans[0]
            .checkpoint.model_dump(mode="json")
            == proof
        )
        drafts.update_draft(
            s,
            context=ctx,
            draft_id=draft_id,
            expected_revision=rev,
            scene_selection=value,
        )
        assert drafts.continue_draft(s, context=ctx, task_id=old_prep)
        assert draft.revision == rev + 1
        assert (
            previews.get_preview_summary(s, context=ctx, preview_id=preview_id).status
            == "OBSOLETE"
        )
        assert (
            not evaluate_native_plan_set(
                s, context=ctx, draft=draft, route=job_env["route"]
            )
            .plans[0]
            .state
            == "READY"
        )


@pytest.mark.parametrize(
    "source_env", [{"bc_id": "90071992547409931234"}], indirect=True
)
def test_browser_plan_edit_and_prepare(job_env, wire, redis_client):
    from tests.modules.builds.native_browser_http import submit_in_browser

    draft_id, _, _ = prepared_native_workspace(job_env, wire, redis_client, True)
    submit_in_browser(
        native.engine,
        job_env["context"],
        job_env["bc_id"],
        None,
        draft_id=draft_id,
        mode="edit",
    )
    with Session(native.engine, expire_on_commit=False) as s, s.begin():
        draft = s.get(BuildDraft, draft_id)
        assert len(draft.scene_selection["plans"]) == 3
        plans = draft.scene_selection["plans"]
        assert [p["selection"]["advertiser_id"] for p in plans] == [
            "actual-account",
            "not-authorized",
            "actual-account",
        ]
        assert [p["selection"]["regions"] for p in plans] == [["US"], ["US"], ["CA"]]
        prep = drafts.prepare_draft(
            s, context=job_env["context"], draft_id=draft_id, request_id=uuid4()
        )
    with Session(native.engine, expire_on_commit=False) as s, s.begin():
        for _ in range(12):
            if drafts.continue_draft(s, context=job_env["context"], task_id=prep):
                break
        result = evaluate_native_plan_set(
            s,
            context=job_env["context"],
            draft=s.get(BuildDraft, draft_id),
            route=job_env["route"],
        )
        assert [p.state for p in result.plans][:2] == ["READY", "BLOCKED"]
    submit_in_browser(
        native.engine,
        job_env["context"],
        job_env["bc_id"],
        None,
        draft_id=draft_id,
        mode="readiness",
    )


def test_concurrent_retry_and_legacy_json_read_boundary(job_env, wire, redis_client):
    from concurrent.futures import ThreadPoolExecutor
    from copy import deepcopy

    from app.modules.builds.catalog import draft_summary
    from app.modules.builds.models import DraftPreparation
    from app.modules.builds.scene_job_models import DraftScenePreparation

    draft_id, prep_id, _ = prepared_native_workspace(job_env, wire, redis_client, True)
    ctx = job_env["context"]
    with Session(native.engine, expire_on_commit=False) as s, s.begin():
        draft = s.get(BuildDraft, draft_id)
        revision = draft.revision
        pid = previews.generate_preview(
            s, context=ctx, draft_id=draft_id, expected_revision=revision
        )
        frozen = deepcopy(
            previews.get_preview_summary(s, context=ctx, preview_id=pid).native_request
        )
        legacy = deepcopy(draft.scene_selection["plans"][0]["selection"])
        state = s.get(DraftScenePreparation, (ctx.tenant_id, prep_id))
        legacy_materials = deepcopy(
            next(iter(state.native_materials["plans"].values()))
        )
        # 历史 JSON fixture，未伪造资格；证明读取不会回填历史记录。
        draft.scene_selection = legacy
        state.native_materials = legacy_materials
    with Session(native.engine) as s:
        summary = draft_summary(s, context=ctx, draft_id=draft_id)
        assert len(summary.scene_selection.plans) == 1
        assert s.get(BuildDraft, draft_id).scene_selection == legacy
        assert (
            previews.get_preview_summary(s, context=ctx, preview_id=pid).native_request
            == frozen
        )
        assert (
            evaluate_native_plan_set(
                s,
                context=ctx,
                draft=s.get(BuildDraft, draft_id),
                route=job_env["route"],
            )
            .plans[0]
            .state
            == "READY"
        )
        generation = s.get(DraftPreparation, prep_id).generation

    def retry(_):
        with Session(native.engine) as s, s.begin():
            return drafts.prepare_draft(
                s, context=ctx, draft_id=draft_id, request_id=uuid4()
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(retry, range(2))) == [prep_id, prep_id]
    with Session(native.engine) as s:
        assert s.get(BuildDraft, draft_id).revision == revision
        assert s.get(DraftPreparation, prep_id).generation == generation + 1
        assert (
            s.get(DraftScenePreparation, (ctx.tenant_id, prep_id)).native_materials
            == legacy_materials
        )


def workspace_client(env):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.deps import get_current_user, get_db
    from app.core.errors import domain_error_handler
    from app.modules.builds.api import router

    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(DomainError, domain_error_handler)

    def database():
        with Session(native.engine) as s:
            yield s

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=env["context"].actor_id
    )
    return TestClient(app)


def drive_plan_jobs(env, wire, redis_client, prep):
    from datetime import UTC, datetime, timedelta

    from sqlmodel import select

    from app.modules.accounts.capability_models import CapabilityJob
    from app.modules.builds.draft_tasks import process_preparation
    from app.modules.builds.models import DraftPreparation
    from app.modules.builds.scene_job_models import SceneJob

    for _ in range(16):
        with Session(native.engine) as s, s.begin():
            pending = s.get(DraftPreparation, prep)
            pending.due_at = datetime.now(UTC) - timedelta(seconds=1)
            generation = pending.generation
        process_preparation(
            database_engine=native.engine,
            tenant_id=env["context"].tenant_id,
            actor_id=env["context"].actor_id,
            payload={"preparation_id": str(prep), "generation": generation},
        )
        with Session(native.engine, expire_on_commit=False) as s:
            done = s.get(DraftPreparation, prep).status != "PENDING"
            caps = s.exec(
                select(CapabilityJob).where(CapabilityJob.status == "PENDING")
            ).all()
            jobs = s.exec(select(SceneJob).where(SceneJob.status == "PENDING")).all()
        if done:
            return
        for cap in caps:
            wire[1].append(native.role_page(["actual-account", "second-account"]))
            native.run_capability({**env, "request_id": uuid4()}, redis_client, cap.id)
            native.run_capability({**env, "request_id": uuid4()}, redis_client, cap.id)
        for job in jobs:
            chosen = job.native_selection
            owner = dict(
                chosen["series_identity"],
                available_status="AVAILABLE",
                can_use_series=True,
                can_push_video=True,
                is_gpppa=False,
            )
            wire[1].extend(
                [
                    native.identities([owner]),
                    native.identities([]),
                    {
                        "list": [{"native_series_id": chosen["native_series_id"]}],
                        "cursor": 0,
                        "has_more": False,
                    },
                    native.region_response()["data"],
                    {
                        "recommend_assets": [
                            {"asset_ids": ["cta-watch"], "asset_content": "Watch now"}
                        ]
                    },
                ]
            )
            for _ in range(8):
                result = native.run(env, redis_client, job.id)
                if result.status != "PENDING":
                    break
            assert result.status == "COMPLETE", result.error_code
    pytest.fail("continuation did not finish")


def two_account_workspace(env, wire, redis_client, config_changes=None):
    from copy import deepcopy
    from uuid import UUID

    from sqlmodel import select

    from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
    from app.modules.materials.models import AccountMaterial

    draft_id, _, expected = prepared_native_workspace(
        env, wire, redis_client, True, config_changes=config_changes or {}
    )
    ctx = env["context"]
    with Session(native.engine, expire_on_commit=False) as s, s.begin():
        # 合成授权目录及已有上传映射为输入；资格和准备由实际 owner 生成。
        account = s.exec(
            select(AdvertiserAccount).where(
                AdvertiserAccount.advertiser_id == "actual-account"
            )
        ).one()
        s.add(
            AdvertiserAccount(
                **{**account.model_dump(), "advertiser_id": "second-account"}
            )
        )
        s.flush()
        grant = s.exec(
            select(BCAccountAccess).where(
                BCAccountAccess.advertiser_id == "actual-account"
            )
        ).one()
        s.add(
            BCAccountAccess(**{**grant.model_dump(), "advertiser_id": "second-account"})
        )
        for row in s.exec(
            select(AccountMaterial).where(
                AccountMaterial.advertiser_id == "actual-account"
            )
        ).all():
            s.add(
                AccountMaterial(
                    **{
                        **row.model_dump(),
                        "id": uuid4(),
                        "advertiser_id": "second-account",
                        "video_id": "second-" + row.video_id,
                        "image_id": "second-" + row.image_id,
                    }
                )
            )
        draft = s.get(BuildDraft, draft_id)
        first = deepcopy(draft.scene_selection["plans"][0])
        second = deepcopy(first)
        second["plan_key"] = str(uuid4())
        second["selection"].update(
            advertiser_id="second-account",
            native_series_id="second-series",
            series_identity={
                **second["selection"]["series_identity"],
                "identity_id": "second-owner",
            },
            creative_identity={
                **second["selection"]["creative_identity"],
                "identity_id": "second-owner",
            },
            video_sources=list(reversed(second["selection"]["video_sources"])),
        )
        value = {"kind": first["selection"]["kind"], "plans": [first, second]}
        revision = draft.revision
    client = workspace_client(env)
    url = f"/tenants/{ctx.tenant_id}/build-drafts/{draft_id}"
    response = client.patch(
        url, json={"expected_revision": revision, "scene_selection": value}
    )
    assert response.status_code == 200, response.text
    revision = response.json()["revision"]
    response = client.post(url + "/prepare", json={"request_id": str(uuid4())})
    assert response.status_code == 202, response.text
    prep = UUID(response.json()["task_id"])
    drive_plan_jobs(env, wire, redis_client, prep)
    return client, url, draft_id, prep, revision, value, expected


def test_http_two_authorized_plans_and_expired_sibling(job_env, wire, redis_client):
    from datetime import UTC, datetime, timedelta

    from sqlmodel import select

    from app.modules.builds.scene_job_models import SceneJob

    client, url, draft_id, prep, revision, value, expected = two_account_workspace(
        job_env, wire, redis_client
    )

    def read():
        response = client.get(url + "/native-checkpoint?view=plans")
        assert response.status_code == 200, response.text
        return response.json()

    before = read()
    assert [p["state"] for p in before["plans"]] == ["READY", "READY"]
    for index, plan in enumerate(before["plans"]):
        assert plan["plan_key"] == value["plans"][index]["plan_key"]
        cp = plan["checkpoint"]
        assert cp["selection"] == value["plans"][index]["selection"]
        assert cp["route"] == job_env["route"].model_dump(mode="json")
        assert [
            (m["material_id"], m["video_id"], m["image_id"]) for m in cp["materials"]
        ] == (
            expected
            if index == 0
            else [(m, "second-" + v, "second-" + c) for m, v, c in reversed(expected)]
        )
    assert set(before["plans"][0]["checkpoint"]["evidence_ids"]).isdisjoint(
        before["plans"][1]["checkpoint"]["evidence_ids"]
    )
    with Session(native.engine, expire_on_commit=False) as s, s.begin():
        job = s.exec(
            select(SceneJob).where(
                SceneJob.advertiser_id == "second-account",
                SceneJob.status == "COMPLETE",
            )
        ).one()
        job.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    stale = read()
    assert stale["plans"][0] == before["plans"][0]
    assert stale["plans"][1]["state"] != "READY"
    response = client.post(url + "/prepare", json={"request_id": str(uuid4())})
    assert response.status_code == 202, response.text
    assert response.json() == {"task_id": str(prep), "revision": revision}
    from app.modules.builds.draft_tasks import process_preparation
    from app.modules.builds.models import DraftPreparation

    with Session(native.engine) as s, s.begin():
        pending = s.get(DraftPreparation, prep)
        generation = pending.generation
        pending.due_at = datetime.now(UTC) - timedelta(seconds=1)
    calls = len(wire[0])
    process_preparation(
        database_engine=native.engine,
        tenant_id=job_env["context"].tenant_id,
        actor_id=job_env["context"].actor_id,
        payload={"preparation_id": str(prep), "generation": generation - 1},
    )
    with Session(native.engine) as s:
        assert s.get(DraftPreparation, prep).generation == generation
        assert s.get(DraftPreparation, prep).status == "PENDING"
    assert len(wire[0]) == calls
    assert read()["plans"][0] == before["plans"][0]
    drive_plan_jobs(job_env, wire, redis_client, prep)
    after = read()
    assert after["draft_revision"] == revision
    assert after["plans"][0] == before["plans"][0]
    assert after["plans"][1]["state"] == "READY"
    assert (
        after["plans"][1]["checkpoint"]["materials"]
        == before["plans"][1]["checkpoint"]["materials"]
    )
    assert (
        after["plans"][1]["checkpoint"]["evidence_ids"]
        != before["plans"][1]["checkpoint"]["evidence_ids"]
    )


def test_plan_scope_and_opposite_order_drafts(job_env, wire, redis_client):
    from concurrent.futures import ThreadPoolExecutor
    from copy import deepcopy
    from threading import Barrier
    from uuid import UUID

    from sqlalchemy import text

    from app.modules.builds.models import DraftPreparation
    from app.modules.builds.scene_job_models import DraftScenePreparation

    client, url, draft_id, prep, revision, value, _ = two_account_workspace(
        job_env, wire, redis_client
    )
    original = client.get(url + "/native-checkpoint?view=plans").json()
    ctx = job_env["context"]
    assert client.get(url.replace(str(ctx.tenant_id), str(uuid4()))).status_code == 403
    with Session(native.engine) as s:
        state = s.get(DraftScenePreparation, (ctx.tenant_id, prep))
        stored, route = deepcopy(state.native_materials), deepcopy(state.frozen_route)
    first, second = [p["plan_key"] for p in value["plans"]]
    # 同一素材在另一账户的真实 VID/封面，不能借到第一计划。
    with Session(native.engine) as s, s.begin():
        state = s.get(DraftScenePreparation, (ctx.tenant_id, prep))
        wrong = deepcopy(stored)
        entries = deepcopy(list(reversed(wrong["plans"][second]["items"])))
        for i, entry in enumerate(entries):
            entry["position"] = i
        wrong["plans"][first]["items"] = entries
        state.native_materials = wrong
    rejected = client.get(url + "/native-checkpoint?view=plans").json()
    assert rejected["plans"][0]["state"] == "BLOCKED"
    assert "native_video_mapping_unverified" in rejected["plans"][0]["reason_codes"]
    assert rejected["plans"][1] == original["plans"][1]
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError, match="immutable scene route"):
        with Session(native.engine) as s, s.begin():
            state = s.get(DraftScenePreparation, (ctx.tenant_id, prep))
            state.frozen_route = {**route, "connection_id": str(uuid4())}
    with Session(native.engine) as s, s.begin():
        s.get(DraftScenePreparation, (ctx.tenant_id, prep)).native_materials = stored
        version = s.get(BuildDraft, draft_id).strategy_version_id
    assert client.get(url + "/native-checkpoint?view=plans").json() == original

    prepared = []
    for plans in (value["plans"], list(reversed(value["plans"]))):
        response = client.post(
            url.rsplit("/", 1)[0],
            json={
                "request_id": str(uuid4()),
                "bc_id": job_env["bc_id"],
                "strategy_version_id": str(version),
                "drama_lines": [],
                "account_lines": [p["selection"]["advertiser_id"] for p in plans],
                "scene_selection": {**value, "plans": plans},
            },
        )
        assert response.status_code == 201, response.text
        new_url = url.rsplit("/", 1)[0] + "/" + response.json()["draft_id"]
        response = client.post(new_url + "/prepare", json={"request_id": str(uuid4())})
        assert response.status_code == 202, response.text
        task = UUID(response.json()["task_id"])
        for _ in range(8):
            with Session(native.engine) as s, s.begin():
                if s.get(DraftPreparation, task).phase == "scenes":
                    break
                drafts.continue_draft(s, context=ctx, task_id=task)
        prepared.append((new_url, task))
    barrier = Barrier(2)

    def finish(item):
        with Session(native.engine) as s, s.begin():
            s.execute(text("SET LOCAL lock_timeout = '5s'"))
            s.execute(text("SET LOCAL statement_timeout = '10s'"))
            pid = s.execute(text("SELECT pg_backend_pid()")).scalar_one()
            barrier.wait(timeout=10)
            assert drafts.continue_draft(s, context=ctx, task_id=item[1])
            return pid

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert len(set(pool.map(finish, prepared))) == 2
    for new_url, _ in prepared:
        actual = client.get(new_url + "/native-checkpoint?view=plans").json()["plans"]
        assert [p["state"] for p in actual] == ["READY", "READY"]
        for plan in actual:
            expected = next(
                p for p in original["plans"] if p["plan_key"] == plan["plan_key"]
            )
            assert (
                plan["checkpoint"]["selection"] == expected["checkpoint"]["selection"]
            )
            assert [
                (m["material_id"], m["video_id"], m["image_id"])
                for m in plan["checkpoint"]["materials"]
            ] == [
                (m["material_id"], m["video_id"], m["image_id"])
                for m in expected["checkpoint"]["materials"]
            ]
