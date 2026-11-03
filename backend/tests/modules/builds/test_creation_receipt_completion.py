"""创建成功回执直接完成；历史自动核查不篡改证据或占用执行窗口。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, select

from app.modules.builds import submission_catalog, submissions
from app.modules.builds.execution_models import ExecutionStep, StepEvidence, Submission
from app.modules.builds.execution_window import window_units
from app.modules.builds.routes import save_attempt_context
from tests.modules.builds.test_carousel_execution import carousel_case as carousel_case
from tests.modules.builds.test_execution import executable as executable
from tests.modules.builds.test_previews import prepared as prepared
from tests.modules.builds.test_submission_queries import expanded
from tests.modules.builds.test_submissions import frozen as frozen


def legacy_readback(session, source, *, status="UNKNOWN"):
    step = ExecutionStep(
        tenant_id=source.tenant_id,
        submission_id=source.submission_id,
        preview_id=source.preview_id,
        bc_id=source.bc_id,
        unit_id=source.unit_id,
        kind="READBACK",
        step_key=f"legacy-readback:{source.id}",
        parent_step_id=source.id,
        group_id=source.group_id,
        planned_ad_id=source.planned_ad_id,
        status=status,
        error_code="readback_inconclusive",
    )
    session.add(step)
    session.flush()
    return step


def receipt(session, source, *, identity=None, conclusion="CREATED"):
    save_attempt_context(session, step=source)
    session.add(
        StepEvidence(
            tenant_id=source.tenant_id,
            submission_id=source.submission_id,
            step_id=source.id,
            attempt=source.attempt,
            conclusion=conclusion,
            summary={"remote_id": identity or source.remote_id},
        )
    )
    session.flush()


def successful_graph(session, submission_id, *, omit_campaign_receipt=False):
    steps = session.exec(
        select(ExecutionStep).where(ExecutionStep.submission_id == submission_id)
    ).all()
    for step in steps:
        step.status, step.phase = "SUCCEEDED", "DONE"
        step.remote_id = f"created-{step.id}"
        session.add(step)
        if step.kind in {"CAMPAIGN", "ADGROUP", "AD"} and not (
            step.kind == "CAMPAIGN" and omit_campaign_receipt
        ):
            receipt(session, step)
    session.flush()
    return next(step for step in steps if step.kind == "CAMPAIGN")


def test_new_graph_completes_from_receipts_without_scheduling_verification(
    session, context, frozen
):
    identity = expanded(session, context, frozen)
    steps = session.exec(
        select(ExecutionStep).where(ExecutionStep.submission_id == identity)
    ).all()
    assert all(step.kind != "READBACK" for step in steps)
    successful_graph(session, identity)
    view = submissions.get_submission(session, context=context, submission_id=identity)
    assert view.status == "COMPLETED"
    assert all(step.checked_at is None for step in steps)


@pytest.mark.parametrize(
    "status", ["PENDING", "QUEUED", "RUNNING", "UNKNOWN", "FAILED"]
)
def test_legacy_readback_cannot_degrade_completed_receipt_or_occupy_window(
    session, context, frozen, status
):
    identity = expanded(session, context, frozen)
    source = successful_graph(session, identity)
    old = legacy_readback(session, source, status=status)
    row = session.get(Submission, identity)
    row.status = "NEEDS_REVIEW"
    session.add(row)
    session.flush()
    view = submissions.get_submission(session, context=context, submission_id=identity)
    assert view.status == "COMPLETED"
    assert not session.exec(window_units()).all()
    units = submissions.get_submission_units(
        session, context=context, submission_id=identity
    )
    assert all(unit.result_status == "COMPLETED" for unit in units.items)
    listed = submission_catalog.list_submissions(
        session, context=context, bc_id=row.bc_id, status_group="completed"
    )
    assert listed.total == 1 and listed.items[0].status == "COMPLETED"
    assert not submission_catalog.list_submissions(
        session, context=context, bc_id=row.bc_id, status_group="attention"
    ).items
    assert not view.recovery.can_reconcile
    assert session.get(ExecutionStep, old.id).status == status
    assert source.checked_at is None and old.checked_at is None


@pytest.mark.parametrize(
    "case",
    [
        "unknown",
        "missing_id",
        "invalid_id",
        "conflicting_receipt",
        "no_receipt",
        "mismatch",
    ],
)
def test_ambiguous_source_keeps_legacy_readback_visible(session, context, frozen, case):
    identity = expanded(session, context, frozen)
    source = successful_graph(
        session, identity, omit_campaign_receipt=case == "no_receipt"
    )
    if case == "unknown":
        source.status = "UNKNOWN"
    elif case == "missing_id":
        source.remote_id = None
    elif case == "invalid_id":
        source.remote_id = "https://example.test/not-an-id"
    elif case == "conflicting_receipt":
        receipt(session, source, identity="another-remote", conclusion="LATE_CREATED")
    elif case == "mismatch":
        source.mismatch = True
    session.add(source)
    legacy_readback(session, source)
    session.flush()
    assert (
        submissions.get_submission(
            session, context=context, submission_id=identity
        ).status
        == "NEEDS_REVIEW"
    )


def test_legacy_projection_preserves_failed_ancestor_completion(
    session, context, frozen
):
    identity = expanded(session, context, frozen)
    source = successful_graph(session, identity)
    legacy_readback(session, source)
    group = session.exec(
        select(ExecutionStep).where(
            ExecutionStep.unit_id == source.unit_id, ExecutionStep.kind == "ADGROUP"
        )
    ).first()
    group.status, group.remote_id = "FAILED", None
    for child in session.exec(
        select(ExecutionStep).where(
            ExecutionStep.parent_step_id == group.id, ExecutionStep.kind == "AD"
        )
    ).all():
        child.status, child.remote_id = "PENDING", None
        session.add(child)
    session.add(group)
    row = session.get(Submission, identity)
    row.status = "NEEDS_REVIEW"
    session.add(row)
    session.flush()
    view = submissions.get_submission(session, context=context, submission_id=identity)
    assert view.status == "PARTIAL"
    page = submission_catalog.list_submissions(
        session, context=context, bc_id=row.bc_id, status_group="attention"
    )
    assert page.total == 1 and page.items[0].status == "PARTIAL"


@pytest.mark.parametrize("legacy_status", ["UNKNOWN", "SUCCEEDED"])
def test_legacy_delivery_is_acknowledged_without_get_or_altering_facts(
    executable, redis_client, monkeypatch, legacy_status
):
    from app.jobs.models import PendingDispatch
    from app.modules.builds import reconciliation
    from app.modules.builds.dispatch import queue_step, repair_execution
    from app.modules.builds.execution_models import SubmissionUnit
    from app.modules.builds.tasks import deliver_step

    engine, context, ids = executable
    monkeypatch.setattr(reconciliation, "require_bounded_worker", lambda: None)

    def unexpected_wire(*_args, **_kwargs):
        raise AssertionError("a durable successful creation needs no GET")

    monkeypatch.setattr("urllib3.PoolManager.request", unexpected_wire)
    with Session(engine) as session, session.begin():
        source = session.get(ExecutionStep, ids["CAMPAIGN"][0])
        row = session.get(Submission, source.submission_id)
        source = successful_graph(session, row.id)
        old = legacy_readback(session, source)
        old.mismatch = True
        queue_step(session, step=old, submission=row, reconcile=True)
        old.status = legacy_status
        old.due_at = old.updated_at = datetime.now(UTC) - timedelta(minutes=5)
        old_id, revision, dispatch_id = old.id, old.dispatch_revision, old.dispatch_id
        message = session.get(PendingDispatch, dispatch_id)
        message.published_at = datetime.now(UTC)
        session.add_all([old, message])
        for unit in session.exec(select(SubmissionUnit)).all():
            unit.dispatch_id = None
            session.add(unit)
    repair_execution(database_engine=engine)
    with Session(engine) as session:
        assert session.get(PendingDispatch, dispatch_id).published_at is not None
    # 未配置任何读 transport；若错误进入 GET，结果不会为 COMPLETED。
    deliver_step(
        database_engine=engine,
        redis_client=redis_client,
        context=context,
        payload={"step_id": str(old_id), "revision": revision},
        reconcile=True,
    )
    with Session(engine) as session:
        old = session.get(ExecutionStep, old_id)
        assert old.dispatch_id is None and old.status == legacy_status
        assert old.mismatch and old.checked_at is None
        assert session.get(ExecutionStep, old.parent_step_id).checked_at is None
        assert (
            submissions.get_submission(
                session, context=context, submission_id=old.submission_id
            ).status
            == "COMPLETED"
        )
        assert not session.exec(
            select(StepEvidence).where(StepEvidence.step_id == old.id)
        ).all()


@pytest.mark.parametrize("executable", [180], indirect=True)
def test_historical_receipt_projection_keeps_180_unit_queries_bounded(
    executable, record_property, monkeypatch
):
    from sqlalchemy import event

    from app.modules.builds import historical_read, recovery
    from app.modules.builds.execution_schemas import Recovery
    from tests.modules.builds.legacy_readbacks import add_legacy_readbacks

    # 本用例测真实列表/详情数量 SQL，恢复操作建议与其无关；小图行为用例仍走完整路径。
    monkeypatch.setattr(recovery, "recovery_summary", lambda *_a, **_kw: Recovery())
    monkeypatch.setattr(
        historical_read, "historical_read_available", lambda *_a, **_kw: False
    )
    engine, context, ids = executable
    with Session(engine) as session, session.begin():
        source = session.get(ExecutionStep, ids["CAMPAIGN"][0])
        row = session.get(Submission, source.submission_id)
        successful_graph(session, row.id)
        add_legacy_readbacks(session, row.id)
        row.status = "NEEDS_REVIEW"
        session.add(row)
        session.flush()
        statements = []
        connection = session.connection()

        def capture(_conn, _cursor, statement, parameters, _ctx, _many):
            if statement.startswith("WITH catalog_submissions") or (
                statement.startswith("WITH scope_basis")
                and "SELECT count(*) planned_c" in statement
            ):
                statements.append((statement, parameters))

        event.listen(connection, "before_cursor_execute", capture)
        try:
            assert not session.exec(window_units()).all()
            page = submission_catalog.list_submissions(
                session, context=context, bc_id=row.bc_id, status_group="completed"
            )
            assert page.total == 1 and page.items[0].status == "COMPLETED"
            summary = submissions.get_submission(
                session, context=context, submission_id=row.id
            )
            assert page.items[0].submitted == summary.submitted
        finally:
            event.remove(connection, "before_cursor_execute", capture)
        assert len(statements) == 2
        window = window_units().compile(
            dialect=connection.dialect, compile_kwargs={"literal_binds": True}
        )
        statements.append((str(window), {}))
        # 用读取的数据块数约束查询规模：与机器负载无关，不像耗时那样在并行检查中波动。
        # 不先 ANALYZE：新预览写入或迁移后统计尚未刷新，也必须避免逐组合重扫。
        # 列表预算保持不变；单提交数量汇总不得超过同量级窗口查询预算。
        budgets = (400_000, 50_000, 50_000)
        for index, (statement, params) in enumerate(statements):
            plan = connection.exec_driver_sql(
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement, params
            ).scalar_one()[0]
            blocks = (
                plan["Plan"]["Shared Hit Blocks"] + plan["Plan"]["Shared Read Blocks"]
            )
            record_property(
                f"receipt_projection_query_{index}_ms", plan["Execution Time"]
            )
            record_property(f"receipt_projection_query_{index}_blocks", blocks)
            assert blocks < budgets[index], plan


@pytest.mark.parametrize("carousel_case", [True], indirect=True)
def test_carousel_exclusion_counts_match_catalog_and_summary(carousel_case):
    engine, context, _, identity, _, _ = carousel_case
    with Session(engine) as session:
        summary = submissions.get_submission(
            session, context=context, submission_id=identity
        )
        page = submission_catalog.list_submissions(
            session, context=context, bc_id=summary.bc_id
        )
        assert page.total == 1
        assert page.items[0].submitted == summary.submitted
        assert summary.submitted.ad_count == 5
        assert summary.submitted.adgroup_count == 2
        assert summary.excluded.ad_count == 1
