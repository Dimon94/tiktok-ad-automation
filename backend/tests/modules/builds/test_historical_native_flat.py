"""Historical flat native_request through current frozen reads and results."""

import importlib.util
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session, select
from sqlalchemy import text

from app.modules.builds import previews, submissions
from app.modules.builds.execution_models import ExecutionStep, Submission
from app.modules.builds.models import BuildDraft
from app.modules.builds.preview_models import BuildPreview, BuildUnit
from app.modules.builds.submission_api import router as submission_router
from tests.modules.builds.scene_jobs import test_native_series as native
from tests.modules.builds.scene_jobs.test_native_series import (
    job_env,
    prepared_native_workspace,
    scene_env,
    source_env,
    wire,
)
from tests.modules.builds.test_native_plan_set import workspace_client


def test_legacy_flat_native_request_frozen_read_vertical_slice(
    job_env, wire, redis_client, monkeypatch
):
    source = Path(
        "/tmp/tkada-native-series-20261112/m2-implementation/"
        "m2-final-verification/legacy-native-preview-flat.py"
    )
    assert source.is_file(), "checked out historical owner is required"
    context = job_env["context"]
    client = workspace_client(job_env)
    client.app.include_router(submission_router)
    draft_id, _, _ = prepared_native_workspace(job_env, wire, redis_client, True)
    with Session(native.engine, expire_on_commit=False) as session, session.begin():
        # Historical version's legal single-flat-selection draft shape.
        draft = session.get(BuildDraft, draft_id)
        draft.scene_selection = draft.scene_selection["plans"][0]["selection"]
    spec = importlib.util.spec_from_file_location("native_preview_historical", source)
    assert spec and spec.loader
    historical = importlib.util.module_from_spec(spec)
    historical.__package__ = "app.modules.builds"
    spec.loader.exec_module(historical)
    assert historical.__file__ == str(source)
    # Only immutable non-null DB metadata absent at the historical version is supplied;
    # native_request, Unit snapshot, digest, object IDs and DB triggers stay original.
    real_insert = previews.insert_preview_with_number

    def legacy_db_metadata(session, row):
        row.batch_short_id = "LEGACY"
        real_insert(session, row)

    monkeypatch.setattr(historical, "insert_preview_with_number", legacy_db_metadata)
    with Session(native.engine, expire_on_commit=False) as session, session.begin():
        pid = historical.generate(
            session, context=context, draft=session.get(BuildDraft, draft_id)
        )
    with Session(native.engine) as session:
        preview = session.get(BuildPreview, pid)
        original = deepcopy(preview.config["native_request"])
        original_digest = preview.content_digest
        unit = session.exec(
            select(BuildUnit).where(BuildUnit.preview_id == pid)
        ).one()
        assert "native_batch" not in preview.config
        assert unit.scene_snapshot["native_request"] == original
        assert session.execute(
            text(
                "SELECT COUNT(*) FROM pg_trigger WHERE tgname IN "
                "('submission_scope', 'execution_step_scope') AND tgenabled = 'O'"
            )
        ).scalar_one() == 2
    response = client.get(
        f"/tenants/{context.tenant_id}/build-previews/{pid}"
    )
    assert response.status_code == 200, response.text
    wrapped = response.json()["native_request"]
    assert wrapped["revision"] == "native-series-batch-v1"
    assert len(wrapped["plans"]) == 1
    assert wrapped["plans"][0]["request"] == original
    assert wrapped["plans"][0]["request_digest"] == original_digest
    with Session(native.engine) as session:
        preview = session.get(BuildPreview, pid)
        assert preview.config["native_request"] == original
        assert preview.content_digest == original_digest
        assert session.get(BuildUnit, unit.id).scene_snapshot["native_request"] == original

    response = client.post(
        f"/tenants/{context.tenant_id}/build-previews/{pid}/submit",
        json={"request_id": "18e1d774-3dcd-4385-b0a4-f56b2353861f"},
    )
    assert response.status_code == 202, response.text
    sid = response.json()["submission_id"]
    from uuid import UUID

    sid = UUID(sid)
    with Session(native.engine) as session:
        assert session.exec(
            select(Submission.id).where(Submission.preview_id == pid)
        ).all() == [sid]
        receipt = submissions.get_submission(
            session, context=context, submission_id=sid
        )
        assert receipt.status == "QUEUED"
        assert len(receipt.native_plans) == 1
        assert receipt.native_plans[0].plan_key
        preview = session.get(BuildPreview, pid)
        assert preview.config["native_request"] == original
        assert preview.content_digest == original_digest
    while True:
        with Session(native.engine, expire_on_commit=False) as session, session.begin():
            if submissions.expand_submission(
                session, context=context, submission_id=sid
            ):
                break
    response = client.get(f"/tenants/{context.tenant_id}/submissions/{sid}")
    assert response.status_code == 200, response.text
    assert len(response.json()["native_plans"]) == 1
    with Session(native.engine) as session:
        preview = session.get(BuildPreview, pid)
        assert preview.config["native_request"] == original
        assert preview.content_digest == original_digest
        assert session.get(BuildUnit, unit.id).scene_snapshot["native_request"] == original
    with Session(native.engine) as session:
        units = session.exec(select(BuildUnit).where(BuildUnit.preview_id == pid)).all()
        assert len(units) == 1
        steps = session.exec(select(ExecutionStep).where(ExecutionStep.submission_id == sid)).all()
        assert {step.unit_id for step in steps} == {unit.id}
    response = client.post(
        f"/tenants/{context.tenant_id}/build-previews/{pid}/submit",
        json={"request_id": "cab22a49-48a1-4812-aa3f-10ac18932358"},
    )
    assert response.status_code == 202, response.text
    assert response.json() == {"submission_id": str(sid), "status": "RUNNING"}
