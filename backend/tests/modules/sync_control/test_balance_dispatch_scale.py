"""真实PG计划验证：历史收据与重复批次不能放大公共锁内的候选读取。"""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import event, text
from sqlmodel import select

from app.modules.reporting.store_models import ReportBalanceRun
from app.modules.sync_control.costs import settle_costs
from app.modules.sync_control.models import SyncCostLedger
from tests.modules.sync_control.test_actual_cost import reserve


def capture_plans(session, predicate, call):
    plans = []
    connection = session.connection()

    def capture(_conn, cursor, statement, parameters, _context, _many):
        if predicate(statement):
            cursor.execute(
                "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + statement, parameters
            )
            plans.append(cursor.fetchone()[0][0])

    event.listen(connection, "before_cursor_execute", capture)
    try:
        call()
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    return plans


def test_empty_settlement_does_not_walk_settled_history(session, sync_seed):
    ledger, cursor = reserve(session, sync_seed)
    ledger.receipt_version = ledger.settled_version = 1
    session.flush()
    template = ledger.model_dump()
    rows = [dict(template, id=uuid4(), generation=i + 2) for i in range(20000)]
    session.execute(SyncCostLedger.__table__.insert(), rows)
    session.execute(text("ANALYZE sync_cost_ledger"))
    plans = capture_plans(
        session,
        lambda sql: (
            sql.startswith("SELECT sync_cost_ledger.id") and "FOR UPDATE" in sql
        ),
        lambda: settle_costs(session, cursor),
    )
    assert len(plans) == 1
    assert plans[0]["Plan"]["Shared Hit Blocks"] < 1000, plans


def test_balance_candidate_does_not_expand_all_duplicate_accounts(
    session, sync_seed, monkeypatch, redis_client
):
    from app.modules.accounts.models import AdvertiserAccount
    from app.modules.reporting import balance_tasks
    from app.modules.sync_control.balance_demand import (
        dispatch_balances_due,
        register_balance,
    )
    from app.modules.sync_control.models import SyncExecutionScope
    from tests.modules.reporting.test_jobs import _route

    monkeypatch.setattr(balance_tasks.settings, "ADS_SYNC_ENABLED", True)
    accounts = [f"scale-{i}" for i in range(500)]
    session.add_all(
        [
            AdvertiserAccount(
                tenant_id=sync_seed.tenant_id, advertiser_id=a, timezone="UTC"
            )
            for a in accounts
        ]
    )
    session.flush()
    run = ReportBalanceRun(
        tenant_id=sync_seed.tenant_id,
        actor_id=sync_seed.actor_id,
        frozen_route=_route(sync_seed),
        run_key="scale-0",
        remaining_advertiser_ids=accounts,
    )
    session.add(run)
    session.flush()
    register_balance(session, run)
    children = [
        c.model_dump()
        for c in session.exec(
            select(SyncExecutionScope).where(
                SyncExecutionScope.balance_run_id == run.id
            )
        ).all()
    ]
    for n in range(1, 40):
        duplicate = ReportBalanceRun(
            **dict(run.model_dump(), id=uuid4(), run_key=f"scale-{n}")
        )
        session.add(duplicate)
        session.flush()
        session.execute(
            SyncExecutionScope.__table__.insert(),
            [dict(c, id=uuid4(), balance_run_id=duplicate.id) for c in children],
        )
    for table in (
        "sync_execution_scope",
        "sync_schedule",
        "sync_scope",
        "report_balance_run",
    ):
        session.execute(text(f"ANALYZE {table}"))
    plans = capture_plans(
        session,
        lambda sql: (
            "SELECT sync_schedule.scope_id" in sql
            and "JOIN sync_execution_scope" in sql
            and "LIMIT" in sql
        ),
        lambda: dispatch_balances_due(
            session, redis_client, now=datetime.now(UTC), limit=1
        ),
    )
    assert plans
    assert max(p["Plan"]["Shared Hit Blocks"] for p in plans) < 15000, plans
