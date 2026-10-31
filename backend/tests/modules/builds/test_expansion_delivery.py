"""Real committed PostgreSQL pages; external boundaries reuse P07's SDK wire."""

# ruff: noqa: F811
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, func
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session, select

from app.jobs.models import PendingDispatch
from app.modules.builds import submission_tasks
from app.modules.builds.execution_models import ExecutionStep, Submission
from app.modules.tenants.models import TenantMembership
from tests.acceptance.conftest import acceptance_scenario  # noqa: F401
from tests.acceptance.scenario import SCALE
from tests.modules.strategies.test_concurrency import (
    isolated_strategy_database,  # noqa: F401
)

# 一次提交展开的全部执行步骤：每份目标素材、每个系列的 CTA，再加系列/广告组/广告创建。
# full 规模为 204，compact 为 64；数量随 ACCEPTANCE_SCALE 推导，不写死。
TOTAL_STEPS = SCALE.target_videos + SCALE.campaigns + SCALE.posts
# 精简规模不足默认一页（100 步），分页、让出和页预算路径会被跳过；
# 因此按总步数缩小页大小，保证任一规模下都至少展开 3 页，且两页装不下全部步骤。
PAGE = (TOTAL_STEPS - 1) // 3


@pytest.fixture
def submitted(acceptance_scenario, monkeypatch):
    monkeypatch.setattr(submission_tasks, "PAGE_SIZE", PAGE)
    case = acceptance_scenario
    case.prepare()
    case.freeze()
    case.submit()
    with Session(case.database_engine) as session, session.begin():
        row = session.get(Submission, case.submission_id)
        dispatch = session.get(PendingDispatch, row.dispatch_id)
        dispatch.published_at = datetime.now(UTC)
        session.add(dispatch)
        identity = dispatch.id
    return case, identity


def snapshot(case):
    with Session(case.database_engine) as session:
        row = session.get(Submission, case.submission_id)
        count = session.exec(
            select(func.count())
            .select_from(ExecutionStep)
            .where(ExecutionStep.submission_id == row.id)
        ).one()
        return (
            row.expanded,
            count,
            row.dispatch_revision,
            row.dispatch_id,
            row.error_code,
        )


def deliver(case):
    submission_tasks.process_submission(
        database_engine=case.database_engine,
        tenant_id=case.scope.context.tenant_id,
        actor_id=case.scope.context.actor_id,
        payload={"submission_id": str(case.submission_id), "revision": 0},
    )


def commits(case, callback):
    def observed(session):
        if (
            session.get_bind() is case.database_engine
            and not session.in_nested_transaction()
        ):
            callback(snapshot(case))

    event.listen(SASession, "after_commit", observed)
    return lambda: event.remove(SASession, "after_commit", observed)


def test_one_delivery_commits_multiple_bounded_pages(submitted):
    case, identity = submitted
    pages = []
    remove = commits(case, pages.append)
    try:
        deliver(case)
    finally:
        remove()
    done, count, revision, dispatch, error = snapshot(case)
    assert done, "one delivery must continue beyond its first page"
    assert (
        count == TOTAL_STEPS
        and revision == 0
        and dispatch == identity
        and error is None
    )
    progress = sorted({p[1] for p in pages if p[1]})
    assert len(progress) >= 3
    assert all(
        b - a <= PAGE for a, b in zip([0, *progress[:-1]], progress, strict=True)
    )
    deliver(case)
    assert snapshot(case) == (done, count, revision, dispatch, error)


def test_committed_page_survives_worker_loss_and_original_delivery_replay(submitted):
    case, identity = submitted

    def crash(state):
        if state[1]:
            raise RuntimeError("simulated worker loss after PostgreSQL commit")

    remove = commits(case, crash)
    try:
        with pytest.raises(RuntimeError, match="worker loss"):
            deliver(case)
    finally:
        remove()
    first = snapshot(case)
    assert 0 < first[1] <= PAGE and not first[0]
    assert first[2:4] == (0, identity)
    deliver(case)
    assert snapshot(case)[:4] == (True, TOTAL_STEPS, 0, identity)


def test_permission_is_rechecked_after_each_committed_page(submitted):
    case, identity = submitted
    revoked = False

    def revoke(state):
        nonlocal revoked
        if state[1] and not revoked:
            revoked = True
            with Session(case.database_engine) as session, session.begin():
                member = session.exec(
                    select(TenantMembership).where(
                        TenantMembership.tenant_id == case.scope.context.tenant_id,
                        TenantMembership.user_id == case.scope.context.actor_id,
                    )
                ).one()
                member.role = "viewer"
                session.add(member)

    remove = commits(case, revoke)
    try:
        deliver(case)
    finally:
        remove()
    state = snapshot(case)
    assert not state[0] and 0 < state[1] <= PAGE
    assert state[2:] == (0, identity, "action_forbidden")


def test_page_budget_yields_one_current_successor(submitted, monkeypatch):
    case, identity = submitted
    monkeypatch.setattr(submission_tasks, "MAX_DELIVERY_PAGES", 2, raising=False)
    # Existing ordinary fanout must not bury the actual generated successor.
    from app.jobs.outbox import enqueue_after_commit, flush_dispatch

    old = datetime.now(UTC) - timedelta(minutes=1)
    with Session(case.database_engine) as session, session.begin():
        for _ in range(200):
            ordinary_id = enqueue_after_commit(
                session,
                context=case.scope.context,
                task_name=submission_tasks.UNIT_TASK_NAME,
                task_key=f"queued-unit:{uuid4()}",
                payload={"unit_id": str(uuid4()), "revision": 0},
            )
            ordinary = session.get(PendingDispatch, ordinary_id)
            ordinary.available_at = old
            session.add(ordinary)
    deliver(case)
    done, count, revision, dispatch, _ = snapshot(case)
    assert not done and PAGE < count <= 2 * PAGE
    assert revision == 1 and dispatch != identity
    deliver(case)  # Stale original generation cannot append another page.
    assert snapshot(case)[:4] == (done, count, revision, dispatch)
    with Session(case.database_engine) as session:
        rows = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == case.scope.context.tenant_id,
                PendingDispatch.task_name == submission_tasks.TASK_NAME,
            )
        ).all()
        assert len(rows) == 2
        successor = session.get(PendingDispatch, dispatch)
        assert successor.payload == {
            "submission_id": str(case.submission_id),
            "revision": 1,
        }
        assert successor.actor_id == case.scope.context.actor_id
    assert flush_dispatch(limit=5) == 5
    assert str(dispatch) in [item["task_id"] for item in case.runtime.messages]


def test_current_dispatch_identity_and_transport_backoff_are_not_bypassed(submitted):
    case, identity = submitted
    submission_tasks.process_submission(
        database_engine=case.database_engine,
        tenant_id=case.scope.context.tenant_id,
        actor_id=case.scope.context.actor_id,
        payload={"submission_id": str(case.submission_id), "revision": 0},
        dispatch_id=uuid4(),
    )
    assert snapshot(case)[1] == 0
    future = datetime.now(UTC) + timedelta(minutes=3)
    with Session(case.database_engine) as session, session.begin():
        row = session.get(PendingDispatch, identity)
        row.published_at, row.available_at, row.attempts = None, future, 5
        session.add(row)
    deliver(case)
    assert snapshot(case)[1] == 0
    with Session(case.database_engine) as session:
        row = session.get(PendingDispatch, identity)
        assert (row.available_at, row.attempts) == (future, 5)


def test_wall_clock_budget_yields_after_the_current_committed_page(
    submitted, monkeypatch
):
    case, identity = submitted
    ticks = iter((0.0, 11.0))
    monkeypatch.setattr(submission_tasks, "monotonic", lambda: next(ticks))
    deliver(case)
    done, count, revision, dispatch, _ = snapshot(case)
    assert not done and 0 < count <= PAGE
    assert revision == 1 and dispatch != identity


@pytest.mark.parametrize("state", ["pending", "superseded", "retired"])
def test_other_tenant_due_expansion_gets_a_yield_after_one_page(
    submitted, monkeypatch, state
):
    from app.jobs.outbox import enqueue_after_commit

    case, identity = submitted
    monkeypatch.setattr(submission_tasks, "monotonic", lambda: 0.0)
    with Session(case.database_engine) as session, session.begin():
        other_id = enqueue_after_commit(
            session,
            context=case.other.context,
            task_name=submission_tasks.TASK_NAME,
            task_key=f"other-submission:{uuid4()}",
            payload={"submission_id": str(uuid4()), "revision": 0},
        )
        other = session.get(PendingDispatch, other_id)
        now = datetime.now(UTC)
        if state == "superseded":
            other.superseded_at = now
        elif state == "retired":
            other.dispatch_contract_version = 2
            other.issued_at = now - timedelta(hours=1)
            other.valid_until = now + timedelta(hours=1)
            other.retired_at = now
    deliver(case)
    done, count, revision, dispatch, _ = snapshot(case)
    if state == "pending":
        assert not done and 0 < count <= PAGE
        assert revision == 1 and dispatch != identity
    else:
        assert done and count == TOTAL_STEPS
        assert revision == 0 and dispatch == identity


def test_actual_other_tenant_probe_keeps_partial_index_with_generic_plan(submitted):
    from tests.jobs.test_expansion_index import (
        assert_prepared_index,
        capture_expansion_queries,
    )

    case, _ = submitted
    captured = []
    remove = capture_expansion_queries(case.database_engine, captured)
    try:
        deliver(case)
    finally:
        remove()
    assert captured
    assert_prepared_index(
        case.database_engine, captured[0], case.scope.context.tenant_id
    )
