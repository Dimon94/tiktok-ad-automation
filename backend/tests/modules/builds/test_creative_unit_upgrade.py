"""V1 切换门禁使用真实 PG；旧正文不改写，重试身份与历史提交保留。"""

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlmodel import select

from app.core.errors import DomainError
from app.jobs.models import PendingDispatch
from app.modules.builds import previews, submissions
from app.modules.builds.execution_models import Submission, SubmissionRequest
from app.modules.builds.models import BuildDraft
from app.modules.builds.preview_models import BuildPreview, PreviewContract
from tests.modules.builds.test_draft_catalog import preview
from tests.modules.builds.test_previews import drain
from tests.modules.builds.test_previews import prepared as prepared


def legacy_preview(session, context, draft_id):
    draft = session.get(BuildDraft, draft_id)
    value = preview(
        session,
        context,
        draft_id,
        {
            "bc_id": draft.bc_id,
            "strategy_version_id": draft.strategy_version_id,
        },
    )
    # 旧预览也冻结账户币种；catalog 的空配置不是可提交的历史正文。
    value.config = {"currency": "USD"}
    session.add(
        PreviewContract(
            tenant_id=context.tenant_id,
            preview_id=value.id,
            bc_id=draft.bc_id,
            version=1,
            media_kind="VIDEO",
            legacy_material_layout="GROUP",
        )
    )
    session.flush()
    value.content_digest, value.status = "f" * 64, "FROZEN"
    session.flush()
    return value


def frozen_bytes(session, identity):
    return session.execute(
        text("SELECT (to_jsonb(p) - 'status')::text FROM build_preview p WHERE id=:id"),
        {"id": identity},
    ).scalar_one()


def test_v1_new_submit_expires_without_dispatch_or_historical_mutation(
    session, context, prepared
):
    old = legacy_preview(session, context, prepared)
    before = frozen_bytes(session, old.id)
    dispatches = session.exec(select(PendingDispatch.id)).all()
    with pytest.raises(DomainError) as error:
        submissions.submit_preview(
            session, context=context, preview_id=old.id, request_id=uuid4()
        )
    assert error.value.code == "preview_contract_expired"
    assert frozen_bytes(session, old.id) == before
    assert not session.exec(select(Submission)).all()
    assert session.exec(select(PendingDispatch.id)).all() == dispatches


def test_v1_regeneration_and_lost_response_replay_create_one_v2(
    session, context, prepared
):
    old = legacy_preview(session, context, prepared)
    before = frozen_bytes(session, old.id)
    request_id = session.get(BuildDraft, prepared).request_id
    fresh_id = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    assert fresh_id != old.id
    queued = session.exec(select(PendingDispatch.id)).all()
    assert (
        previews.generate_preview(
            session, context=context, draft_id=prepared, expected_revision=1
        )
        == fresh_id
    )
    assert (
        previews.generate_preview(
            session, context=context, draft_id=prepared, expected_revision=2
        )
        == fresh_id
    )
    assert session.exec(select(PendingDispatch.id)).all() == queued
    drain(session, context, fresh_id)
    fresh = session.get(BuildPreview, fresh_id)
    assert fresh.status == "FROZEN" and fresh.draft_revision == 2
    assert session.get(PreviewContract, (context.tenant_id, fresh_id)).version == 2
    assert frozen_bytes(session, old.id) == before
    assert session.get(BuildDraft, prepared).request_id == request_id
    assert (
        len(
            session.exec(
                select(BuildPreview).where(BuildPreview.draft_id == prepared)
            ).all()
        )
        == 2
    )


def test_v1_submitted_replay_and_new_alias_return_original_without_dispatch(
    session, context, prepared
):
    old = legacy_preview(session, context, prepared)
    saved = Submission(
        tenant_id=context.tenant_id,
        bc_id=old.bc_id,
        preview_id=old.id,
        draft_id=prepared,
        actor_id=context.actor_id,
        ordinal=1,
        status="COMPLETED",
        expanded=True,
    )
    session.add(saved)
    session.flush()
    key = uuid4()
    session.add(
        SubmissionRequest(
            tenant_id=context.tenant_id,
            request_id=key,
            submission_id=saved.id,
            preview_id=old.id,
            bc_id=old.bc_id,
        )
    )
    session.flush()
    before, dispatches = (
        frozen_bytes(session, old.id),
        session.exec(select(PendingDispatch.id)).all(),
    )
    for request_id in (key, uuid4()):
        receipt = submissions.submit_preview(
            session, context=context, preview_id=old.id, request_id=request_id
        )
        assert receipt.submission_id == saved.id and receipt.status == "COMPLETED"
    assert len(session.exec(select(Submission)).all()) == 1
    assert session.exec(select(PendingDispatch.id)).all() == dispatches
    assert frozen_bytes(session, old.id) == before
    with pytest.raises(DomainError) as error:
        previews.generate_preview(
            session, context=context, draft_id=prepared, expected_revision=1
        )
    assert error.value.code == "preview_already_submitted"


def release_probe():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "creative_release_probe",
        Path(__file__).resolve().parents[4] / "deploy/release/probe.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_old_schema_upgrade_and_mixed_history_drain(monkeypatch):
    """升级前/后均只读阻断：含未提交 BUILDING、未展开提交及 UNKNOWN。"""
    from alembic import command
    from sqlmodel import Session

    from app.modules.accounts.connection_models import BCDefaultRoute
    from app.modules.builds.execution_models import (
        ExecutionStep,
        StepEvidence,
        SubmissionUnit,
    )
    from app.modules.builds.preview_models import BuildUnit, PreviewDrama
    from tests.migration_database import historical_database, insert_historical_row
    from tests.modules.builds.test_drafts import create_intent
    from tests.modules.builds.test_image_composition_migration import (
        historical_scaffold,
    )
    from tests.modules.conftest import create_context

    probe = release_probe()
    with historical_database(monkeypatch, "0059_video_creative_units") as (
        engine,
        config,
    ):
        with Session(engine) as db:
            context = create_context(db)
            intent = create_intent(db, context, historical=True)
            draft, drama = historical_scaffold(db, context, intent, media_kind="VIDEO")
            old = preview(db, context, draft.id, intent)
            scope = {
                "tenant_id": context.tenant_id,
                "preview_id": old.id,
                "bc_id": old.bc_id,
            }
            insert_historical_row(
                db,
                PreviewDrama(
                    **scope,
                    drama_id=drama.drama_id,
                    link_id=drama.link_id,
                    title=drama.title,
                    url="https://example.invalid",
                    protected_base="synthetic",
                ),
            )
            db.flush()
            unit = BuildUnit(
                **scope,
                drama_id=drama.drama_id,
                advertiser_id="account-A",
                connection_id=db.get(
                    BCDefaultRoute, (context.tenant_id, old.bc_id)
                ).connection_id,
                currency="USD",
                timezone="UTC",
                campaign_name="synthetic",
                campaign_digest="a" * 64,
                scene_snapshot={},
                complete=True,
            )
            db.add(unit)
            db.flush()
            old.status, old.content_digest = "FROZEN", "f" * 64
            db.flush()
            sub = Submission(
                **scope, draft_id=draft.id, actor_id=context.actor_id, ordinal=1
            )
            insert_historical_row(db, sub)
            db.flush()
            assert probe.creative_unit_drain(db.connection())["blockers"].keys() == {
                "submission_runnable"
            }
            db.execute(
                text(
                    "UPDATE build_submission SET expanded=true,status='COMPLETED' WHERE id=:id"
                ),
                {"id": sub.id},
            )
            db.add(
                SubmissionUnit(
                    **scope,
                    submission_id=sub.id,
                    unit_id=unit.id,
                    disposition="INCLUDED",
                    expanded=True,
                )
            )
            db.flush()
            step = ExecutionStep(
                **scope,
                submission_id=sub.id,
                unit_id=unit.id,
                kind="CAMPAIGN",
                step_key="synthetic",
                status="UNKNOWN",
                request_body={"synthetic": "unchanged"},
                request_body_digest="b" * 64,
            )
            db.add(step)
            writer = preview(db, context, draft.id, intent, revision=2)
            db.flush()
            identities = old.id, step.id, writer.id
            before = frozen_bytes(db, old.id)
            snapshot = step.model_dump()
            result = probe.creative_unit_drain(db.connection())
            assert set(result["blockers"]) == {
                "preview_building",
                "step_runnable_or_unknown",
            }
            db.commit()
        # 门禁失败不会执行迁移；这里是隔离 fixture 的结构演练，保留 UNKNOWN 验证混合历史。
        command.upgrade(config, "head")
        with Session(engine) as db:
            old_id, step_id, writer_id = identities
            assert frozen_bytes(db, old_id) == before
            assert db.get(ExecutionStep, step_id).model_dump() == snapshot
            assert db.get(PreviewContract, (context.tenant_id, old_id)).version == 1
            assert set(probe.creative_unit_drain(db.connection())["blockers"]) == {
                "preview_building",
                "step_runnable_or_unknown",
            }
            # 不伪造完成：用 savepoint 中不同既有状态分别验证判定，再回滚夹具变动。
            for state in ("QUEUED", "PENDING", "RUNNING", "RETRYABLE", "UNKNOWN"):
                with db.begin_nested() as transaction:
                    row = db.get(ExecutionStep, step_id)
                    row.status = state
                    db.flush()
                    assert (
                        "step_runnable_or_unknown"
                        in probe.creative_unit_drain(db.connection())["blockers"]
                    )
                    transaction.rollback()
            assert db.get(ExecutionStep, step_id).status == "UNKNOWN"
            # 每种证据独立保存点，不能更新或删除不可变的历史回执。
            for receipt_id, expected_drained in (
                ("created-campaign", True),
                ("different-campaign", False),
            ):
                with db.begin_nested() as transaction:
                    db.get(BuildPreview, writer_id).status = "FROZEN"
                    source = db.get(ExecutionStep, step_id)
                    source.status, source.remote_id = "SUCCEEDED", "created-campaign"
                    db.add(
                        ExecutionStep(
                            **scope,
                            submission_id=source.submission_id,
                            unit_id=source.unit_id,
                            kind="READBACK",
                            step_key="obsolete-readback",
                            parent_step_id=source.id,
                            status="UNKNOWN",
                        )
                    )
                    db.add(
                        StepEvidence(
                            tenant_id=source.tenant_id,
                            submission_id=source.submission_id,
                            step_id=source.id,
                            attempt=0,
                            conclusion="CREATED",
                            summary={"remote_id": receipt_id},
                        )
                    )
                    db.flush()
                    assert (
                        probe.creative_unit_drain(db.connection())["drained"]
                        is expected_drained
                    )
                    transaction.rollback()


def test_probe_v2_work_is_separate_and_unknown_schema_fails_closed(
    session, context, prepared
):
    probe = release_probe()
    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    assert session.get(BuildPreview, identity).status == "BUILDING"
    assert probe.creative_unit_drain(session.connection())["drained"] is True
    with session.begin_nested() as transaction:
        session.execute(
            text("UPDATE alembic_version SET version_num='unknown_contract'")
        )
        with pytest.raises(RuntimeError, match="Unknown creative contract schema"):
            probe.creative_unit_drain(session.connection())
        transaction.rollback()


def test_deployed_sync_schema_and_merged_carousel_head_are_recognized(monkeypatch):
    from alembic import command

    from tests.migration_database import historical_database

    probe = release_probe()
    with historical_database(monkeypatch, "0066_directory_status_turn") as (
        engine,
        config,
    ):
        with engine.connect() as connection:
            assert probe.creative_unit_drain(connection)["drained"] is True
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert probe.creative_unit_drain(connection)["drained"] is True
