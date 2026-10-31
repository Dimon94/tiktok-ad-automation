from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from sqlalchemy import event
from sqlmodel import select

from app.modules.reporting.jobs import enqueue_job
from app.modules.reporting.publish import AccountMeta, publish_ad_day
from app.modules.reporting.store_models import ReportSyncState
from app.modules.sync_control.models import (
    SyncExecutionScope,
    SyncRequirement,
    SyncScope,
)
from tests.modules.reporting.test_jobs import _route


def test_large_publication_batches_state_and_requirement_io(session, sync_seed):
    """600 个可写桶应批量发布；两个受保护的交叉桶不能被一起确认。"""
    now = datetime.now(UTC)
    start = date(2025, 1, 1)
    end = start + timedelta(days=300)
    tenant = sync_seed.tenant_id
    accounts = {
        "acc-1": AccountMeta("USD", "Asia/Hong_Kong"),
        "acc-2": AccountMeta("USD", "UTC"),
    }
    job_id = enqueue_job(
        session,
        tenant_id=tenant,
        bc_id="bc-a",
        kind="ad_day",
        tier="history",
        advertiser_ids=list(accounts),
        start=start,
        end=end,
        frozen_route=_route(sync_seed),
        actor_id=sync_seed.actor_id,
        requested_at=now,
    )
    # 混合已存在/首次发布状态，且每桶版本不同；不能用统一批次版本覆盖。
    state_a = ReportSyncState(
        tenant_id=tenant,
        advertiser_id="acc-1",
        report_kind="ad_day",
        stat_date=start,
        status="STALE",
        error_code="old-error",
        publication_version=7,
        observed_at=now - timedelta(seconds=1),
    )
    state_b = ReportSyncState(
        tenant_id=tenant,
        advertiser_id="acc-2",
        report_kind="ad_day",
        stat_date=start + timedelta(days=1),
        status="COMPLETE",
        row_count=3,
        publication_version=2,
        observed_at=now - timedelta(seconds=1),
    )
    protected = [("acc-1", start + timedelta(days=1)), ("acc-2", start)]
    session.add_all(
        [state_a, state_b]
        + [
            ReportSyncState(
                tenant_id=tenant,
                advertiser_id=advertiser,
                report_kind="ad_day",
                stat_date=day,
                status="COMPLETE",
                row_count=9,
                publication_version=11,
                observed_at=now + timedelta(seconds=1),
            )
            for advertiser, day in protected
        ]
    )
    foreign_tenant = uuid4()
    session.add(
        ReportSyncState(
            tenant_id=foreign_tenant,
            advertiser_id="acc-1",
            report_kind="ad_day",
            stat_date=start,
            status="COMPLETE",
            row_count=17,
            publication_version=99,
        )
    )
    references = session.exec(
        select(SyncExecutionScope, SyncRequirement, SyncScope)
        .join(SyncRequirement, SyncExecutionScope.requirement_id == SyncRequirement.id)
        .join(SyncScope, SyncExecutionScope.scope_id == SyncScope.id)
        .where(SyncExecutionScope.report_job_id == job_id)
    ).all()
    for child, requirement, scope in references:
        if (scope.advertiser_id, requirement.bucket_key) == (
            "acc-1",
            start.isoformat(),
        ):
            requirement.desired_revision = child.frozen_revision + 1
            requirement.requested_after = now + timedelta(seconds=1)
    session.flush()
    statements = []

    def record_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
        prefix = statement.lstrip().split(" ", 2)
        if len(prefix) > 1 and (
            prefix[1].startswith(
                ("report_sync_state", "sync_execution_scope", "sync_requirement")
            )
            or statement.startswith("INSERT INTO report_sync_state")
        ):
            statements.append(statement)

    connection = session.connection()
    event.listen(connection, "before_cursor_execute", record_sql)
    try:
        publish_ad_day(
            session,
            tenant_id=tenant,
            job_id=job_id,
            accounts=accounts,
            start=start,
            end=end,
            rows=[],
            requested_at=now,
        )
        session.flush()
    finally:
        event.remove(connection, "before_cursor_execute", record_sql)
    states = {
        (s.advertiser_id, s.stat_date): s
        for s in session.exec(
            select(ReportSyncState).where(ReportSyncState.tenant_id == tenant)
        ).all()
    }
    assert len(states) == 602
    assert states[("acc-1", start)].publication_version == 8
    assert states[("acc-2", start + timedelta(days=1))].publication_version == 3
    assert state_a.error_code is None and state_b.row_count == 0
    for child, requirement, scope in references:
        key = (scope.advertiser_id, date.fromisoformat(requirement.bucket_key))
        state = states[key]
        if key in protected:
            assert state.publication_version == 11 and state.row_count == 9
            assert child.status == "PENDING" and requirement.satisfied_revision == 0
        else:
            assert state.status == "COMPLETE" and state.row_count == 0
            assert child.status == "PUBLISHED"
            assert child.published_version == state.publication_version
            assert requirement.satisfied_revision == child.frozen_revision
    newer = next(
        r
        for _, r, s in references
        if s.advertiser_id == "acc-1" and r.bucket_key == start.isoformat()
    )
    assert newer.desired_revision > newer.satisfied_revision
    assert newer.oldest_pending_at is not None
    foreign = session.get(ReportSyncState, (foreign_tenant, "acc-1", "ad_day", start))
    assert foreign.publication_version == 99 and foreign.row_count == 17
    print(f"publication state/reference SQL statements: {len(statements)}")  # noqa: T201
    assert len(statements) < 40, "publication performs per-bucket database round trips"
