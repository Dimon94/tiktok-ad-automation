"""报告封面从真实归档读取金额，隔离范围与模型正文。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.modules.ai_workbench.report_performance import report_performance
from app.modules.ai_workbench.review_store import archive_artifact
from app.modules.ai_workbench.reviews import create_review_task
from tests.modules.ai_workbench.test_review_product import optimizer_setup


def test_report_cover_preserves_partition_scope_and_historical_cutoff(session, context):
    optimizer = optimizer_setup(session, context)
    task = create_review_task(
        session, context=context, optimizer_id=optimizer.id, request_id=uuid4()
    )
    now = datetime.now(UTC)
    task.evidence = {
        **task.evidence,
        "captured_at": (now - timedelta(days=3)).isoformat(),
        "reanalysis": {"source_task_id": str(uuid4())},
    }
    group = {
        "currency": "USD",
        "timezone": "UTC",
        "attribution": "default",
        "start_date": "2026-11-07",
        "end_date": "2026-11-13",
        "spend": "100.25",
        "revenue_d0": "60.15",
        "roas": "0.6",
        "eligible_buckets": 7,
        "excluded_buckets": 0,
    }
    archive_artifact(
        session,
        task=task,
        kind="analysis",
        key="all-accounts",
        payload={
            "request": {"dimension": "account", "maturity_horizon": "d0"},
            "all_totals": [
                group,
                {**group, "currency": "JPY", "spend": "10000", "revenue_d0": "8000"},
                {**group, "maturity_status": "MISSING", "spend": None},
            ],
            "coverage": {"requested_accounts": 10, "available_accounts": 8},
            "source_refs": ["must-not-leak"],
        },
    )
    archive_artifact(
        session,
        task=task,
        kind="analysis",
        key="single-account",
        payload={
            "request": {
                "dimension": "account",
                "maturity_horizon": "d0",
                "advertiser_id": "acc-1",
            },
            "all_totals": [{**group, "spend": "999"}],
        },
    )
    other = create_review_task(
        session, context=context, optimizer_id=optimizer.id, request_id=uuid4()
    )
    archive_artifact(
        session,
        task=other,
        kind="analysis",
        key="other-task",
        payload={
            "request": {"dimension": "account", "maturity_horizon": "d0"},
            "all_totals": [{**group, "spend": "987654"}],
        },
    )
    result = report_performance(session, task, now=now)
    assert result["historical_reanalysis"] and result["age_hours"] == 72
    assert result["captured_at"] == task.evidence["captured_at"]
    partitions = result["windows"][0]["partitions"]
    assert len(partitions) == 2
    assert partitions[0]["spend"] == "100.25"
    assert partitions[0]["revenue_minus_spend"] == "-40.10"
    assert partitions[1]["currency"] == "JPY"
    assert "source_refs" not in str(result)
    assert "999" not in str(result) and "987654" not in str(result)


def test_report_daily_panel_deduplicates_pages_and_keeps_missing_not_zero(
    session, context
):
    optimizer = optimizer_setup(session, context)
    task = create_review_task(
        session, context=context, optimizer_id=optimizer.id, request_id=uuid4()
    )
    partition = {
        "currency": "USD",
        "timezone": "UTC",
        "start_date": "2026-11-10",
        "end_date": "2026-11-11",
        "accounts": 10,
        "daily": [
            {
                "date": "2026-11-10",
                "spend": "100",
                "revenue_total": "50",
                "roas": "0.5",
            },
            {"date": "2026-11-11", "spend": None, "revenue_total": None, "roas": None},
        ],
        "balanced_panel": {"accounts": 0, "daily": []},
    }
    for page in (0, 1):
        archive_artifact(
            session,
            task=task,
            kind="timeline",
            key=f"timeline-{page}",
            payload={
                "request": {"dimension": "campaign", "offset": page},
                "partitions": [partition],
            },
        )
    result = report_performance(session, task)
    assert len(result["timelines"]) == 1
    daily = result["timelines"][0]["daily"]
    assert daily[0]["spend"] == "100"
    assert daily[1]["spend"] is None
    assert result["timelines"][0]["balanced_accounts"] == 0
