"""终态失败页恢复窗口与目录归档充分覆盖证明；真实PG小样本正确性。"""

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlmodel import select

from app.modules.ads.sync_models import AdDirectoryPage
from app.modules.reporting import job_tasks
from app.modules.sync_control.archive import _directory_cover_exists
from app.modules.sync_control.directory_demand import register_directory_run
from app.modules.sync_control.models import SyncExecutionScope
from tests.modules.sync_control.test_archive import NOW, archive_directory, directory


def staged_page(session, row, number=1):
    page = AdDirectoryPage(
        run_id=row.id,
        page=number,
        tenant_id=row.tenant_id,
        advertiser_id=row.advertiser_id,
        claim_generation=1,
        next_page=number + 1,
        complete=False,
        items=[],
        materials=[],
        observed_at=row.completed_at,
    )
    session.add(page)
    session.flush()
    return page


@pytest.mark.parametrize(
    "age,removed", [(timedelta(days=7), 0), (timedelta(days=7, seconds=1), 1)]
)
def test_failed_directory_pages_have_seven_day_terminal_window(
    session, sync_seed, age, removed
):
    row = directory(session, sync_seed)
    row.completed_at = NOW - age
    staged_page(session, row)
    assert (
        job_tasks.cleanup_directory_pages(session, now=NOW, candidate_ids=[row.id])
        == removed
    )


@pytest.mark.parametrize("guard", ["claim", "lease", "pending", "unknown_completion"])
def test_failed_directory_page_guards_preserve_recovery(session, sync_seed, guard):
    row = directory(session, sync_seed)
    staged_page(session, row)
    if guard == "claim":
        row.claim_token = uuid4()
    elif guard == "lease":
        row.claimed_until = NOW + timedelta(minutes=1)
    elif guard == "pending":
        register_directory_run(session, row)
        assert (
            session.exec(
                select(SyncExecutionScope).where(
                    SyncExecutionScope.directory_run_id == row.id
                )
            )
            .first()
            .status
            == "PENDING"
        )
    else:
        row.completed_at = None
    session.flush()
    assert (
        job_tasks.cleanup_directory_pages(session, now=NOW, candidate_ids=[row.id]) == 0
    )


def test_failed_directory_bounded_pages_then_ninety_day_archive(session, sync_seed):
    row = directory(session, sync_seed)
    for page in range(1, 6):
        staged_page(session, row, page)
    for expected in (2, 2, 1, 0):
        assert (
            job_tasks.cleanup_directory_pages(
                session, now=NOW, candidate_ids=[row.id], batch_limit=2
            )
            == expected
        )
    assert archive_directory(session, row).deleted


@pytest.mark.parametrize("path", ["partition", "full"])
def test_cover_queries_use_matching_partial_index(session, sync_seed, path):
    old = directory(session, sync_seed, published=1, status="COMPLETE")
    old.query = {"ids": ["c1"], "parent_ids": [], "include_deleted": False}
    newer = directory(session, sync_seed, published=2, status="COMPLETE")
    if path == "partition":
        newer.partition_key = old.partition_key
        newer.query = dict(old.query)
    session.flush()
    connection = session.connection()
    connection.execute(text("SET LOCAL enable_seqscan=off"))
    statements = []

    def capture(_conn, _cursor, statement, parameters, _context, _many):
        if statement.startswith("SELECT ad_directory_run.id") and "md5" in statement:
            statements.append((statement, parameters))

    event.listen(connection, "before_cursor_execute", capture)
    try:
        assert _directory_cover_exists(session, old, lock=False)
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert len(statements) == (1 if path == "partition" else 2)
    statement, parameters = statements[-1]
    plan = connection.exec_driver_sql(
        "EXPLAIN (FORMAT JSON) " + statement, parameters
    ).scalar_one()
    encoded = json.dumps(plan)
    assert f"ix_directory_archive_{path}_cover" in encoded
    assert "Index Cond" in encoded and "md5" in encoded and "bc_id" in encoded


def test_selective_superset_without_indexable_sufficient_proof_is_retained(
    session, sync_seed
):
    old = directory(session, sync_seed, published=1, status="COMPLETE")
    old.query = {"ids": ["c1"], "parent_ids": [], "include_deleted": False}
    newer = directory(session, sync_seed, published=2, status="COMPLETE")
    newer.query = {"ids": ["c1", "c2"], "parent_ids": [], "include_deleted": False}
    session.flush()
    assert not _directory_cover_exists(session, old, lock=False)


def test_full_cover_from_other_bc_does_not_replace_frozen_scope(session, sync_seed):
    old = directory(session, sync_seed, published=1, status="COMPLETE")
    newer = directory(session, sync_seed, published=2, status="COMPLETE")
    newer.bc_id = "bc-b"
    newer.frozen_route = {**newer.frozen_route, "bc_id": "bc-b"}
    session.flush()
    assert not _directory_cover_exists(session, old, lock=False)
