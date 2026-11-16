"""轻调度与可恢复前缀准备：使用真实PostgreSQL，不调用外部平台。"""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlmodel import select

from app.modules.business.models import BusinessPublication, BusinessRun, BusinessScope
from app.modules.business.projection import advance_run, request_rebuild
from tests.modules.business.test_projection import build, seed_days

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)


def test_scheduled_rebuild_does_not_freeze_or_copy_in_control(
    session, business_case, monkeypatch
):
    from app.modules.business import projection, tasks

    c = business_case
    seed_days(session, c)
    published = build(session, c)[0]
    scope = session.get(BusinessScope, (c.context.tenant_id, c.bc_id))
    scope.next_check_at = NOW - timedelta(seconds=1)
    session.add(scope)
    session.flush()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Control must not build a large source snapshot")

    monkeypatch.setattr(projection, "source_snapshot", forbidden)
    tasks.schedule_due(session, now=NOW, limit=1)
    candidates = session.exec(
        select(BusinessRun).where(BusinessRun.status == "QUEUED")
    ).all()
    assert len(candidates) == 1
    assert candidates[0].phase == "SNAPSHOT"
    assert candidates[0].inputs == {}
    assert scope.next_check_at > NOW
    assert (
        session.get(
            BusinessPublication, (c.context.tenant_id, c.bc_id, published.quarter)
        ).run_id
        == published.id
    )


def test_unchanged_deferred_snapshot_never_publishes_empty_facts(
    session, business_case
):
    c = business_case
    seed_days(session, c)
    published = build(session, c)[0]
    candidate = request_rebuild(
        session,
        context=c.context,
        bc_id=c.bc_id,
        start_date=date(2026, 10, 1),
        end_date=c.day,
        now=NOW,
        defer_snapshot=True,
        coalesce_pending=True,
    )[0]
    assert candidate.phase == "SNAPSHOT"
    assert candidate.id != published.id
    assert advance_run(session, candidate.id, now=NOW)
    assert candidate.status == "SUPERSEDED"
    assert candidate.inputs == {}
    assert (
        session.get(
            BusinessPublication, (c.context.tenant_id, c.bc_id, published.quarter)
        ).run_id
        == published.id
    )


def test_pending_snapshot_coalesces_without_refreezing(session, business_case):
    c = business_case
    seed_days(session, c)
    args = {
        "context": c.context,
        "bc_id": c.bc_id,
        "start_date": date(2026, 10, 1),
        "end_date": c.day,
        "now": NOW,
        "defer_snapshot": True,
        "coalesce_pending": True,
    }
    first = request_rebuild(session, **args)[0]
    assert request_rebuild(session, **args)[0].id == first.id
    assert first.inputs == {} and first.phase == "SNAPSHOT"


def _changed_candidate(session, c):
    from app.modules.reporting.store_models import ReportSyncState

    seed_days(session, c)
    old = build(session, c)[0]
    state = session.get(
        ReportSyncState,
        (c.context.tenant_id, c.advertiser_id, "ad_day", date(2026, 11, 3)),
    )
    state.publication_version += 1
    session.add(state)
    session.flush()
    candidate = request_rebuild(
        session,
        context=c.context,
        bc_id=c.bc_id,
        start_date=old.quarter,
        end_date=c.day,
        now=NOW,
    )[0]
    assert candidate.phase == "PREFIX_CHECK"
    return old, candidate


def test_prefix_pages_commit_atomically_before_publication(
    session, business_case, monkeypatch, redis_client
):
    from sqlalchemy import func

    from app.modules.business import projection
    from app.modules.business.models import BusinessDay, BusinessSummaryDay

    assert redis_client.ping()
    old, run = _changed_candidate(session, business_case)
    monkeypatch.setattr(projection, "BASE_BATCH", 1)
    publication_key = (run.tenant_id, run.bc_id, run.quarter)
    advance_run(session, run.id, now=NOW)
    assert run.phase == "COPY_DAY"
    before = dict(run.coverage["_prefix"])
    with pytest.raises(RuntimeError, match="crash"):
        with session.begin_nested():
            advance_run(session, run.id, now=NOW)
            raise RuntimeError("crash before chunk commit")
    session.refresh(run)
    assert run.coverage["_prefix"] == before
    assert (
        session.exec(
            select(func.count())
            .select_from(BusinessDay)
            .where(BusinessDay.run_id == run.id)
        ).one()
        == 0
    )
    seen = set()
    for _ in range(30):
        if run.phase == "BASE":
            break
        seen.add(run.phase)
        advance_run(session, run.id, now=NOW)
        session.commit()
        assert session.get(BusinessPublication, publication_key).run_id == old.id
    assert seen == {"COPY_DAY", "COPY_SUMMARY"}
    assert run.phase == "BASE" and run.cursor == date(2026, 11, 3)
    for model in (BusinessDay, BusinessSummaryDay):
        source_count = session.exec(
            select(func.count())
            .select_from(model)
            .where(model.run_id == old.id, model.stat_date < run.cursor)
        ).one()
        assert source_count > 1
        assert (
            session.exec(
                select(func.count()).select_from(model).where(model.run_id == run.id)
            ).one()
            == source_count
        )
    for _ in range(10):
        if advance_run(session, run.id, now=NOW):
            break
    assert run.status == "COMPLETE"
    assert session.get(BusinessPublication, publication_key).run_id == run.id


@pytest.mark.parametrize("change", ["publication", "actor", "revision"])
def test_changed_fence_cannot_finish_prefix(session, business_case, change):
    from uuid import uuid4

    old, run = _changed_candidate(session, business_case)
    advance_run(session, run.id, now=NOW)
    scope = session.get(BusinessScope, (run.tenant_id, run.bc_id))
    if change == "publication":
        replacement = BusinessRun(
            tenant_id=run.tenant_id,
            bc_id=run.bc_id,
            actor_id=run.actor_id,
            quarter=run.quarter,
            end_date=run.end_date,
            scope_revision=run.scope_revision,
            cursor=run.quarter,
            status="COMPLETE",
            inputs=old.inputs,
        )
        session.add(replacement)
        session.flush()
        publication = session.get(
            BusinessPublication, (run.tenant_id, run.bc_id, run.quarter)
        )
        publication.run_id = replacement.id
        session.add(publication)
    elif change == "actor":
        # 以已有用户验证actor围栏；不依赖不存在用户绕过FK。
        from app.models import User

        actor = User(username="prefix-" + uuid4().hex, hashed_password="unused")
        session.add(actor)
        session.flush()
        scope.actor_id = actor.id
        session.add(scope)
    else:
        scope.revision += 1
        session.add(scope)
    session.flush()
    assert advance_run(session, run.id, now=NOW)
    assert run.status == "SUPERSEDED"
    assert (
        session.get(BusinessPublication, (run.tenant_id, run.bc_id, run.quarter)).run_id
        != run.id
    )


def test_copy_does_not_load_or_update_full_inputs(session, business_case):
    from sqlalchemy import event

    from app.modules.business.models import BusinessDay

    _, run = _changed_candidate(session, business_case)
    advance_run(session, run.id, now=NOW)
    run_id = run.id
    session.expunge_all()
    statements = []
    connection = session.connection()

    def capture(_conn, _cursor, statement, _params, _ctx, _many):
        statements.append(statement)

    event.listen(connection, "before_cursor_execute", capture)
    try:
        advance_run(session, run_id, now=NOW)
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    selects = [
        sql.split("FROM", 1)[0]
        for sql in statements
        if sql.lstrip().startswith("SELECT")
    ]
    assert not any(
        "business_run.inputs AS" in sql or ", business_run.inputs," in sql
        for sql in selects
    )
    assert not any("SET inputs=" in sql or "SET inputs =" in sql for sql in statements)
    assert session.exec(select(BusinessDay).where(BusinessDay.run_id == run_id)).all()


def test_unknown_phase_cannot_enter_subsidy(session, business_case):
    c = business_case
    seed_days(session, c)
    run = request_rebuild(
        session,
        context=c.context,
        bc_id=c.bc_id,
        start_date=date(2026, 10, 1),
        end_date=c.day,
        now=NOW,
    )[0]
    run.phase = "FUTURE_PHASE"
    session.add(run)
    session.flush()
    with pytest.raises(ValueError, match="未知"):
        advance_run(session, run.id, now=NOW)


def test_snapshot_global_lock_prevents_second_heavy_freeze(
    session, business_case, monkeypatch
):
    from sqlalchemy import text

    from app.core.db import engine
    from app.modules.business import projection
    from app.modules.business.prefix import SNAPSHOT_LOCK

    c = business_case
    seed_days(session, c)
    run = request_rebuild(
        session,
        context=c.context,
        bc_id=c.bc_id,
        start_date=date(2026, 10, 1),
        end_date=c.day,
        now=NOW,
        defer_snapshot=True,
        coalesce_pending=True,
    )[0]

    def forbidden(*_args, **_kwargs):
        raise AssertionError("another heavy snapshot is already running")

    monkeypatch.setattr(projection, "source_snapshot", forbidden)
    with engine.connect() as holder, holder.begin():
        holder.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": SNAPSHOT_LOCK}
        )
        assert not advance_run(session, run.id, now=NOW)
    assert run.phase == "SNAPSHOT" and run.status == "QUEUED" and run.inputs == {}


def test_query_timeout_rolls_back_copied_rows_and_cursor(session, business_case):
    from sqlalchemy import event, func
    from sqlalchemy.exc import DBAPIError

    from app.modules.business.models import BusinessDay
    from app.modules.reporting.query_budget import query_budget

    _, run = _changed_candidate(session, business_case)
    advance_run(session, run.id, now=NOW)
    before = dict(run.coverage["_prefix"])
    connection = session.connection()

    def slow_after_write(_conn, cursor, statement, _params, _context, _many):
        if "INSERT INTO business_day" in statement:
            cursor.execute("SELECT pg_sleep(0.3)")

    event.listen(connection, "after_cursor_execute", slow_after_write)
    try:
        with (
            pytest.raises(DBAPIError) as caught,
            session.begin_nested(),
            query_budget(session, seconds=0.1),
        ):
            advance_run(session, run.id, now=NOW)
        assert caught.value.orig.sqlstate == "57014"
    finally:
        event.remove(connection, "after_cursor_execute", slow_after_write)
    session.refresh(run)
    assert run.status != "FAILED" and run.coverage["_prefix"] == before
    assert (
        session.exec(
            select(func.count())
            .select_from(BusinessDay)
            .where(BusinessDay.run_id == run.id)
        ).one()
        == 0
    )
    advance_run(session, run.id, now=NOW)
    assert run.phase == "COPY_SUMMARY"


def test_full_comparison_timeout_commits_daily_progress(
    session, business_case, monkeypatch
):
    from sqlalchemy import text

    from app.modules.business import prefix
    from app.modules.reporting.query_budget import query_budget

    _, run = _changed_candidate(session, business_case)
    original = prefix._dirty_start

    def slow_full(db, current, old, *, day=None):
        if day is None:
            db.execute(text("SELECT pg_sleep(2)"))
        return original(db, current, old, day=day)

    monkeypatch.setattr(prefix, "_dirty_start", slow_full)
    with query_budget(session, seconds=3):
        assert not advance_run(session, run.id, now=NOW)
    session.commit()
    assert run.phase == "PREFIX_CHECK"
    assert run.coverage["_prefix"]["check_day"] == "2026-10-01"
    for _ in range(40):
        advance_run(session, run.id, now=NOW)
        session.commit()
        if run.phase != "PREFIX_CHECK":
            break
    assert run.phase == "COPY_DAY"
    assert run.coverage["_prefix"]["dirty"] == "2026-11-03"


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("complete_through", date(2026, 11, 2)),
        ("not_observed", date(2026, 11, 4)),
        ("outside_membership", date(2026, 11, 4)),
        ("currency_changed", date(2026, 10, 1)),
    ],
)
def test_database_dirty_boundary_preserves_coverage_semantics(
    session, business_case, case, expected
):
    from copy import deepcopy

    from app.modules.business.prefix import _dirty_start

    old, run = _changed_candidate(session, business_case)
    before = deepcopy(old.inputs)
    after = deepcopy(old.inputs)
    advertiser = business_case.advertiser_id
    before["accounts"][advertiser]["complete_through"] = "2026-11-01"
    after["accounts"][advertiser]["complete_through"] = "2026-11-03"
    if case == "not_observed":
        for snapshot in (before, after):
            for state in snapshot["states"].values():
                state["observed"] = "2026-10-01T00:00:00+00:00"
    elif case == "outside_membership":
        for snapshot in (before, after):
            snapshot["accounts"][advertiser]["end"] = "2026-11-01"
    elif case == "currency_changed":
        after["accounts"][advertiser]["currency"] = "EUR"
    old.inputs, run.inputs = before, after
    session.add_all([old, run])
    session.flush()
    assert _dirty_start(session, run, old) == expected


def test_manual_admin_change_replaces_pending_but_reuses_complete(
    session, business_case
):
    from uuid import uuid4

    from app.core.context import TenantContext
    from app.models import User
    from app.modules.tenants.models import TenantMembership

    c = business_case
    seed_days(session, c)
    first = request_rebuild(
        session,
        context=c.context,
        bc_id=c.bc_id,
        start_date=date(2026, 10, 1),
        end_date=c.day,
        now=NOW,
    )[0]
    actor = User(
        username="other-admin-" + uuid4().hex,
        hashed_password="test-only",
        is_active=True,
    )
    session.add(actor)
    session.flush()
    session.add(
        TenantMembership(
            tenant_id=c.context.tenant_id,
            user_id=actor.id,
            role="tenant_admin",
            active=True,
        )
    )
    session.flush()
    other = TenantContext(c.context.tenant_id, actor.id, "operator")
    second = request_rebuild(
        session,
        context=other,
        bc_id=c.bc_id,
        start_date=date(2026, 10, 1),
        end_date=c.day,
        now=NOW,
    )[0]
    assert second.id != first.id and second.actor_id == actor.id
    assert first.status == "SUPERSEDED"
    for _ in range(100):
        if advance_run(session, second.id, now=NOW):
            break
    assert second.status == "COMPLETE"
    reused = request_rebuild(
        session,
        context=c.context,
        bc_id=c.bc_id,
        start_date=date(2026, 10, 1),
        end_date=c.day,
        now=NOW,
    )[0]
    assert reused.id == second.id and reused.status == "COMPLETE"


def test_long_pending_continues_bounded_repair_rotation(
    session, business_case, monkeypatch
):
    from app.modules.business import tasks
    from app.modules.business.prefix import repair_windows
    from app.modules.reporting import jobs, planner
    from app.modules.reporting.store_models import AdSyncBc

    c = business_case
    seed_days(session, c)
    run = request_rebuild(
        session,
        context=c.context,
        bc_id=c.bc_id,
        start_date=date(2026, 10, 1),
        end_date=c.day,
        now=NOW,
    )[0]
    original = run.inputs["accounts"][c.advertiser_id]
    accounts = {f"repair-{index:03}": dict(original) for index in range(132)}
    # 完整128账号前缀也必须推进，随后发现剩余4账号的缺日。
    states = {
        f"{advertiser}/2026-11-03": {
            "status": "COMPLETE",
            "observed": "2026-11-04T00:00:00+00:00",
        }
        for advertiser in list(accounts)[:128]
    }
    for meta in accounts.values():
        meta["start"] = meta["complete_through"] = "2026-11-03"
    run.inputs = {**run.inputs, "accounts": accounts, "states": states}
    run.phase = "COPY_DAY"
    session.add(run)
    session.add(
        AdSyncBc(
            tenant_id=run.tenant_id,
            bc_id=run.bc_id,
            actor_id=run.actor_id,
            frozen_route={},
            next_hot_at=NOW,
            next_material_at=NOW,
            next_directory_at=NOW,
            next_daily_at=NOW,
        )
    )
    session.flush()
    scope = session.get(BusinessScope, (run.tenant_id, run.bc_id))
    monkeypatch.setattr(tasks.settings, "ADS_SYNC_ENABLED", True)
    # 日志规划独立轮转，不依赖经营补源；保留 spy，防止此处重新挂接隐式规划。
    from app.modules.operation_logs import planning as log_planning

    log_accounts = []
    monkeypatch.setattr(
        log_planning,
        "plan_logs",
        lambda _db, _plan, **values: log_accounts.append(values["accounts"]) or 0,
    )
    monkeypatch.setattr(planner, "owned_accounts", lambda *_args: list(accounts))
    monkeypatch.setattr(planner, "_common", lambda _plan: {})
    enqueued = []
    monkeypatch.setattr(
        jobs, "enqueue_job", lambda _db, **values: enqueued.append(values)
    )
    session.expire(run, ["inputs"])
    assert tasks._request_missing_sources(session, scope, [run], NOW) == 0
    assert scope.repair_cursor.startswith("repair-127/")
    assert tasks._request_missing_sources(session, scope, [run], NOW) == 2
    assert [value["advertiser_ids"] for value in enqueued] == [
        ["repair-128"],
        ["repair-129"],
    ]
    assert tasks._request_missing_sources(session, scope, [run], NOW) == 2
    assert [value["advertiser_ids"] for value in enqueued[2:]] == [
        ["repair-130"],
        ["repair-131"],
    ]
    assert log_accounts == []
    windows, after = repair_windows(session, run, set(accounts), scope.repair_cursor)
    assert windows == [] and after == "repair-127"


def test_bounded_repair_keeps_member_and_complete_day_end(session, business_case):
    from app.modules.business.prefix import repair_windows
    from app.modules.business.tasks import missing_source_windows

    c = business_case
    seed_days(session, c)
    run = request_rebuild(
        session,
        context=c.context,
        bc_id=c.bc_id,
        start_date=date(2026, 10, 1),
        end_date=c.day,
        now=NOW,
    )[0]
    meta = run.inputs["accounts"][c.advertiser_id]
    accounts = {
        "ended": {**meta, "start": "2026-11-01", "end": "2026-11-01"},
        "incomplete-day": {
            **meta,
            "start": "2026-11-01",
            "complete_through": "2026-11-02",
        },
    }
    run.inputs = {**run.inputs, "accounts": accounts, "states": {}}
    session.add(run)
    session.flush()
    expected = sorted(missing_source_windows(run, set(accounts)))
    actual, _ = repair_windows(session, run, set(accounts), None)
    assert (
        actual
        == expected
        == [
            ("ended", date(2026, 11, 1), date(2026, 11, 1)),
            ("incomplete-day", date(2026, 11, 1), date(2026, 11, 2)),
        ]
    )


def test_repair_timeout_does_not_roll_back_scheduled_candidate(
    session, business_case, monkeypatch
):
    from sqlalchemy import text

    from app.modules.business import tasks

    c = business_case
    seed_days(session, c)
    build(session, c)
    scope = session.get(BusinessScope, (c.context.tenant_id, c.bc_id))
    scope.next_check_at = NOW - timedelta(seconds=1)
    session.add(scope)
    session.flush()

    def slow_repair(db, *_args, **_kwargs):
        db.execute(text("SELECT pg_sleep(3)"))

    monkeypatch.setattr(tasks, "_request_missing_sources", slow_repair)
    tasks.schedule_due(session, now=NOW, limit=1)
    session.flush()
    assert scope.next_check_at == NOW + timedelta(minutes=5)
    assert session.exec(
        select(BusinessRun.id).where(
            BusinessRun.tenant_id == c.context.tenant_id,
            BusinessRun.phase == "SNAPSHOT",
            BusinessRun.status == "QUEUED",
        )
    ).all()
