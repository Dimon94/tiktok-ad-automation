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
        with pytest.raises(DomainError, match="批量冻结"):
            previews.generate_preview(
                s, context=ctx, draft_id=draft_id, expected_revision=rev
            )
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
