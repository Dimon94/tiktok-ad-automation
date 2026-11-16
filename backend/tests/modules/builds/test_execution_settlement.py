"""真实 PG 复现终态轮询、错误汇总与归档代数复活，禁止远端业务调用。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, select

from app.jobs.models import PendingDispatch
from app.jobs.outbox import compact_published_dispatches
from app.modules.builds.dispatch import finalize_submission, repair_execution
from app.modules.builds.execution_models import (
    ExecutionStep,
    Submission,
    SubmissionUnit,
)
from tests.modules.builds.test_execution import executable as executable


def settle(db, *, failed=False):
    at = datetime.now(UTC) - timedelta(days=9)
    with Session(db) as session, session.begin():
        for step in session.exec(select(ExecutionStep)).all():
            step.status, step.phase = "SUCCEEDED", "DONE"
            step.remote_id = f"remote-{step.id}"
            step.error_code, step.dispatch_id = None, None
            step.updated_at = at
            if failed and step.kind == "AD":
                step.status, step.remote_id = "FAILED", None
                step.error_code = "tiktok_business_error"
            session.add(step)
        row = session.exec(select(Submission)).one()
        row.created_at, row.updated_at = at - timedelta(minutes=1), at
        session.add(row)
        units = session.exec(select(SubmissionUnit)).all()
        for unit in units:
            unit.repair_after = at
            session.add(unit)
    return at


@pytest.mark.parametrize("executable", [2], indirect=True)
@pytest.mark.parametrize("failed,want", [(False, "COMPLETED"), (True, "PARTIAL")])
def test_settlement_uses_business_facts_despite_other_unit_messages(
    executable, failed, want
):
    db, context, _ = executable
    at = settle(db, failed=failed)
    with Session(db) as session:
        row = session.exec(select(Submission)).one()
        identity = row.id
        assert any(u.dispatch_id for u in session.exec(select(SubmissionUnit)).all())
    finalize_submission(database_engine=db, context=context, submission_id=identity)
    with Session(db) as session:
        row = session.get(Submission, identity)
        assert row.status == want
        assert row.updated_at == at


def test_terminal_repair_does_not_create_new_work_or_refresh_business_time(executable):
    db, context, _ = executable
    at = settle(db)
    with Session(db) as session, session.begin():
        row = session.exec(select(Submission)).one()
        row.status = "COMPLETED"
        identity = row.id
        session.add(row)
        unit = session.exec(select(SubmissionUnit)).one()
        unit.dispatch_id = None
        session.add(unit)
        messages_before = len(session.exec(select(PendingDispatch)).all())
    assert repair_execution(database_engine=db) == 0
    finalize_submission(database_engine=db, context=context, submission_id=identity)
    with Session(db) as session:
        assert session.exec(select(SubmissionUnit)).one().dispatch_id is None
        assert len(session.exec(select(PendingDispatch)).all()) == messages_before
        assert session.get(Submission, identity).updated_at == at


def test_repair_new_wake_never_reuses_compacted_generation(executable):
    db, _, _ = executable
    at = datetime.now(UTC) - timedelta(days=9)
    with Session(db) as session, session.begin():
        unit = session.exec(select(SubmissionUnit)).one()
        old_id, old_revision = unit.dispatch_id, unit.dispatch_revision
        message = session.get(PendingDispatch, old_id)
        message.published_at = at
        unit.dispatch_id, unit.repair_after = None, at
        session.add_all([unit, message])
        assert compact_published_dispatches(session, limit=10) == 1
    repair_execution(database_engine=db)
    with Session(db) as session:
        unit = session.exec(select(SubmissionUnit)).one()
        assert unit.dispatch_revision > old_revision
        assert unit.dispatch_id != old_id
        message = session.get(PendingDispatch, unit.dispatch_id)
        assert message.payload == {
            "unit_id": str(unit.unit_id),
            "revision": unit.dispatch_revision,
        }
        assert session.get(PendingDispatch, old_id).payload == {}


@pytest.mark.parametrize("tampered", [False, True])
def test_historical_invalid_unit_requires_matching_archive_digest(executable, tampered):
    db, _, _ = executable
    at = datetime.now(UTC) - timedelta(days=9)
    with Session(db) as session, session.begin():
        unit = session.exec(select(SubmissionUnit)).one()
        old_id = unit.dispatch_id
        message = session.get(PendingDispatch, old_id)
        message.published_at = at
        unit.dispatch_id, unit.repair_after = None, at
        session.add_all([unit, message])
        compact_published_dispatches(session, limit=10)
        unit.reason_code = "dispatch_payload_invalid"
        if tampered:
            message.payload_digest = "0" * 64
            session.add(message)
        session.add(unit)
    repair_execution(database_engine=db)
    with Session(db) as session:
        unit = session.exec(select(SubmissionUnit)).one()
        if tampered:
            assert unit.reason_code == "dispatch_payload_invalid"
            assert unit.dispatch_id is None
        else:
            assert unit.reason_code is None
            assert unit.dispatch_id is not None and unit.dispatch_id != old_id


@pytest.mark.parametrize("executable", [2], indirect=True)
def test_terminal_units_cannot_starve_a_lost_runnable_unit(executable):
    db, _, _ = executable
    at = settle(db)
    with Session(db) as session, session.begin():
        units = session.exec(
            select(SubmissionUnit).order_by(SubmissionUnit.unit_id)
        ).all()
        for index, unit in enumerate(units):
            unit.dispatch_id = None
            unit.repair_after = at + timedelta(seconds=index)
            session.add(unit)
        target = units[1]
        target_id = target.unit_id
        step = session.exec(
            select(ExecutionStep).where(
                ExecutionStep.unit_id == target_id, ExecutionStep.kind == "CTA"
            )
        ).one()
        step.status, step.phase, step.remote_id = "PENDING", "IDLE", None
        step.due_at = at
        session.add(step)
    assert repair_execution(database_engine=db, limit=1) >= 1
    with Session(db) as session:
        units = session.exec(
            select(SubmissionUnit).order_by(SubmissionUnit.unit_id)
        ).all()
        assert units[0].dispatch_id is None
        assert units[1].unit_id == target_id and units[1].dispatch_id is not None


def test_repair_keeps_unknown_material_for_reconciliation_without_creating_it(
    executable,
):
    db, _, _ = executable
    settle(db)
    with Session(db) as session, session.begin():
        unit = session.exec(select(SubmissionUnit)).one()
        unit.dispatch_id = None
        session.add(unit)
        step = session.exec(
            select(ExecutionStep).where(ExecutionStep.kind == "MATERIAL")
        ).first()
        step.status, step.remote_id = "UNKNOWN", None
        step.error_code = "material_result_unknown"
        step_id = step.id
        session.add(step)
        messages_before = len(session.exec(select(PendingDispatch)).all())
    repair_execution(database_engine=db)
    with Session(db) as session:
        step = session.get(ExecutionStep, step_id)
        assert step.status == "UNKNOWN" and step.dispatch_id is None
        assert len(session.exec(select(PendingDispatch)).all()) == messages_before


def test_resolved_unknown_clears_review_state_while_real_work_remains(executable):
    db, context, _ = executable
    settle(db)
    with Session(db) as session, session.begin():
        row = session.exec(select(Submission)).one()
        identity = row.id
        row.status = "NEEDS_REVIEW"
        session.add(row)
        step = session.exec(
            select(ExecutionStep).where(ExecutionStep.kind == "AD")
        ).first()
        step.status, step.phase, step.remote_id = "PENDING", "IDLE", None
        session.add(step)
    finalize_submission(database_engine=db, context=context, submission_id=identity)
    with Session(db) as session:
        assert session.get(Submission, identity).status == "RUNNING"
