"""报告封面从真实归档读取金额，隔离范围与模型正文。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

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


@pytest.mark.parametrize("missing_d7", [False, True])
def test_report_cover_consumes_real_financial_v2_analysis(
    client, session, context, monkeypatch, missing_d7
):
    from uuid import UUID

    from app.modules.ai_workbench import reviews
    from app.modules.ai_workbench.review_analysis import analyze_for_task
    from app.modules.ai_workbench.review_store import (
        ReviewArtifact,
        ReviewSource,
        scoped,
    )
    from tests.modules.ai_workbench.review_helpers import create_prepared_review_task
    from tests.modules.ai_workbench.test_optimizer import api_headers
    from tests.modules.ai_workbench.test_review_maturity import (
        CUTOFF,
        _publish,
        _version,
    )

    version = _version(session, context)
    _publish(session, context, missing_d7=missing_d7)
    monkeypatch.setattr(reviews, "_now", lambda: CUTOFF)
    task = create_prepared_review_task(
        session, context=context, optimizer_id=version.optimizer_id, request_id=uuid4()
    )
    assert task.status == "PENDING"
    sources = session.exec(
        scoped(ReviewSource, task).where(
            ReviewSource.category == "reporting",
            ReviewSource.dimension == "account",
        )
    ).all()
    assert {s.maturity_horizon for s in sources} == {"d0", "d3", "d7"}
    assert all(
        s.payload["projection_contract"] == "financial-review-v2" for s in sources
    )

    analyses = {}
    for horizon in ("d0", "d3", "d7"):
        analysis = analyze_for_task(
            session, task=task, dimension="account", maturity_horizon=horizon
        )
        analyses[horizon] = analysis
        artifact = session.get(ReviewArtifact, (task.id, UUID(analysis["id"])))
        assert artifact.kind == "analysis"
        assert artifact.payload["all_totals"] == analysis["totals"]
        assert artifact.payload["source_refs"]

    cover = report_performance(session, task, now=CUTOFF + timedelta(days=3))
    assert cover["captured_at"] == CUTOFF.isoformat()
    assert cover["age_hours"] == 72
    assert cover["historical_reanalysis"] is False
    assert [w["horizon"] for w in cover["windows"]] == ["d0", "d3", "d7"]
    expected = {
        "d0": ("2026-11-06", "2026-11-12", "7", "0.1", "-63"),
        "d3": ("2026-11-03", "2026-11-09", "21", "0.3", "-49"),
        "d7": ("2026-10-30", "2026-11-05", "49", "0.7", "-21"),
    }
    for window in cover["windows"]:
        horizon = window["horizon"]
        assert window["coverage"] == analyses[horizon]["coverage"]
        if horizon == "d7" and missing_d7:
            # 真正缺失的收入不能成为零收入/零消耗，也不能回退到累计收入999。
            assert window["partitions"] == []
            assert window["coverage"]["available_accounts"] == 0
            assert window["coverage"]["excluded_accounts"] == 1
            assert analyses[horizon]["totals"]
            assert all(
                t["spend"] is None and t["revenue_d7"] is None
                for t in analyses[horizon]["totals"]
            )
            continue
        assert len(window["partitions"]) == 1
        partition = window["partitions"][0]
        assert (partition["currency"], partition["timezone"]) == ("USD", "UTC")
        assert partition["spend"] == "70"
        assert partition["maturity_status"] == "AVAILABLE"
        assert (
            tuple(
                partition[k]
                for k in (
                    "start_date",
                    "end_date",
                    "revenue",
                    "roas",
                    "revenue_minus_spend",
                )
            )
            == expected[horizon]
        )

    # 发布相反的D7状态后，已冻结分析及GET封面仍只读取原任务证据。
    _publish(session, context, missing_d7=not missing_d7, observed=datetime.now(UTC))
    assert report_performance(session, task, now=CUTOFF + timedelta(days=3)) == cover
    response = client.get(
        f"/api/tenants/{context.tenant_id}/ai-optimizers/{task.optimizer_id}/reviews/{task.id}",
        headers=api_headers(context, session=session),
    )
    assert response.status_code == 200, response.text
    exposed = response.json()["performance"]
    for key in ("captured_at", "historical_reanalysis", "windows", "timelines"):
        assert exposed[key] == cover[key]
