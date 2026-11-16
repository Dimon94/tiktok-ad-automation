"""逐日/分层诊断：历史截断、缺失、分区及账户分母不能伪装成根因。"""

from decimal import Decimal

from app.modules.ai_workbench.review_timeline import summarize_packs


def pack(account="a", currency="USD", missing=()):
    rows = [
        {
            "stat_date": "2026-11-01",
            "campaign_id": "old",
            "spend": "30",
            "revenue_total": "30",
            "currency": currency,
            "timezone": "UTC",
        },
        {
            "stat_date": "2026-11-02",
            "campaign_id": "old",
            "spend": "10",
            "revenue_total": "2",
            "currency": currency,
            "timezone": "UTC",
        },
        {
            "stat_date": "2026-11-02",
            "campaign_id": "new",
            "spend": "5",
            "revenue_total": "1",
            "currency": currency,
            "timezone": "UTC",
        },
        {
            "stat_date": "2026-11-03",
            "campaign_id": "old",
            "spend": "10",
            "revenue_total": "1",
            "currency": currency,
            "timezone": "UTC",
        },
        {
            "stat_date": "2026-11-03",
            "campaign_id": "new",
            "spend": "4",
            "revenue_total": "0",
            "currency": currency,
            "timezone": "UTC",
        },
    ]
    return {
        "advertiser_id": account,
        "start_date": "2026-11-01",
        "end_date": "2026-11-03",
        "currency": currency,
        "timezone": "UTC",
        "coverage": {"missing_days": list(missing)},
        "rows": rows,
        "material_rows": [],
        "material_coverage": {"missing_days": []},
    }


def test_daily_cohorts_use_only_preceding_days_and_reconcile():
    result = summarize_packs([pack()], dimension="campaign", days=2, scope={"a"})
    daily = result["partitions"][0]["daily"]
    first, second = daily
    assert first["spend"] == "15" and first["revenue_total"] == "3"
    cohorts = {c["cohort"]: c for c in first["cohorts"]}
    assert cohorts["prior_proven"]["spend"] == "10"
    assert cohorts["first_observed_spend"]["spend"] == "5"
    assert second["prior_low_efficiency_spend"] == "14"
    assert second["prior_long_tail_spend"] == "4"
    assert second["prior_low_efficiency_long_tail_spend"] == "4"
    for day in daily:
        assert sum(Decimal(c["spend"]) for c in day["cohorts"]) == Decimal(day["spend"])
    assert result["partitions"][0]["balanced_panel"]["accounts"] == 1
    assert result["limitations"]["daily_iaa"] == "NOT_FROZEN"


def test_missing_prehistory_is_unknown_and_balanced_panel_excludes_missing_day():
    p = pack(missing=["2026-11-01", "2026-11-03"])
    result = summarize_packs([p], dimension="campaign", days=2, scope={"a"})
    partition = result["partitions"][0]
    assert partition["daily"][0]["cohorts"][0]["cohort"] == "unknown_prior"
    assert partition["balanced_panel"]["accounts"] == 0
    assert partition["daily"][1]["complete_accounts"] == 0
    assert partition["daily"][1]["observed_accounts"] == 1


def test_partitions_scope_and_missing_pairs_do_not_mix_or_fill_zero():
    a, b, outside = pack(), pack("b", "EUR"), pack("outside")
    a["rows"][-1]["revenue_total"] = None
    result = summarize_packs(
        [a, b, outside], dimension="campaign", days=2, scope={"a", "b", "missing"}
    )
    assert len(result["partitions"]) == 2
    assert result["coverage"]["missing_source_accounts"] == 1
    usd = next(p for p in result["partitions"] if p["currency"] == "USD")
    assert usd["daily"][1]["spend"] == "10"
    assert usd["daily"][1]["excluded_rows"] == 1
    assert usd["balanced_panel"]["accounts"] == 0


def test_zero_spend_revenue_is_retained_and_residual_material_identity_is_unknown():
    p = pack()
    p["material_rows"] = [
        dict(
            r,
            row_key=r["campaign_id"],
            source_status="PARTIAL",
            attribution_status="UNMAPPED",
        )
        for r in p["rows"]
    ]
    p["material_rows"][-1].update(spend="0", revenue_total="2")
    result = summarize_packs([p], dimension="material", days=2, scope={"a"})
    second = result["partitions"][0]["daily"][1]
    assert second["revenue_total"] == "3"
    assert second["identity_unavailable_spend"] == "10"
    assert all(c["cohort"] == "unknown_identity" for c in second["cohorts"])


def test_real_archive_citation_cache_and_scope(session, context):
    from datetime import UTC, datetime
    from uuid import UUID

    import pytest
    from fastapi import HTTPException

    from app.modules.ai_workbench.review_store import (
        archive_source,
        delivered_evidence_ids,
    )
    from app.modules.ai_workbench.review_timeline import timeline_for_task
    from tests.modules.ai_workbench.test_review_scale import create_task

    task = create_task(session, context)
    p = pack("acc-1")
    archive_source(
        session,
        task=task,
        category="rule_backtest_inputs",
        payload=p,
        captured_at=datetime.now(UTC),
    )
    result = timeline_for_task(session, task=task, days=2)
    assert result["partitions"][0]["daily"][0]["spend"] == "15"
    assert UUID(result["id"])
    assert result["id"] in delivered_evidence_ids(session, task)
    assert timeline_for_task(session, task=task, days=2) == result
    with pytest.raises(HTTPException) as error:
        timeline_for_task(session, task=task, advertiser_id="outside")
    assert error.value.status_code == 403
    from uuid import uuid4

    from app.modules.ai_workbench.reviews import create_review_task

    other = create_review_task(
        session, context=context, optimizer_id=task.optimizer_id, request_id=uuid4()
    )
    empty = timeline_for_task(session, task=other, days=2)
    assert (
        empty["partitions"] == [] and empty["coverage"]["missing_source_accounts"] == 1
    )
    assert result["id"] not in delivered_evidence_ids(session, other)


def test_timeline_rejects_corrupt_facts(session, context):
    from datetime import UTC, datetime
    from uuid import uuid4

    import pytest

    from app.core.errors import DomainError
    from app.modules.ai_workbench.review_store import ReviewSource
    from app.modules.ai_workbench.review_timeline import timeline_for_task
    from tests.modules.ai_workbench.test_review_scale import create_task

    task = create_task(session, context)
    session.add(
        ReviewSource(
            task_id=task.id,
            id=uuid4(),
            tenant_id=task.tenant_id,
            optimizer_id=task.optimizer_id,
            category="rule_backtest_inputs",
            advertiser_id="acc-1",
            digest="0" * 64,
            payload=pack("acc-1"),
            captured_at=datetime.now(UTC),
        )
    )
    session.flush()
    with pytest.raises(DomainError) as error:
        timeline_for_task(session, task=task, days=2)
    assert error.value.code == "ai_evidence_changed"


from tests.modules.ai_workbench.test_review_reading import reading  # noqa: E402,F401


def test_timeline_http_uses_task_token_and_rechecks_scope(client, reading):  # noqa: F811
    from uuid import uuid4

    from tests.modules.ai_workbench.test_review_reading import claim_task

    task, _, _ = reading
    claim, headers = claim_task(client)
    path = f"/api/integrations/ai-workbench/reviews/{task.id}/evidence/timeline"
    response = client.post(
        path, headers=headers, json={"dimension": "campaign", "days": 7, "limit": 1}
    )
    assert response.status_code == 200, response.text
    assert response.json()["coverage"]["requested_accounts"] == 1
    forbidden = client.post(path, headers=headers, json={"advertiser_id": "outside"})
    assert forbidden.status_code == 403
    wrong_task = client.post(
        path.replace(str(task.id), str(uuid4())), headers=headers, json={}
    )
    assert wrong_task.status_code in (403, 404)
    invalid = client.post(path, headers=headers, json={"days": 1000})
    assert invalid.status_code == 422


def test_incomplete_prior_identity_cannot_prove_later_good_cohort():
    p = pack()
    p["material_rows"] = [
        dict(
            r,
            row_key=r["campaign_id"],
            source_status="COMPLETE",
            attribution_status="RESOLVED",
        )
        for r in p["rows"]
    ]
    p["material_rows"][0]["source_status"] = "PARTIAL"
    result = summarize_packs([p], dimension="material", days=2, scope={"a"})
    first = result["partitions"][0]["daily"][0]
    cohorts = {c["cohort"]: c for c in first["cohorts"]}
    assert "prior_proven" not in cohorts
    assert cohorts["unknown_prior"]["spend"] == "10"


def test_zero_spend_unresolved_income_does_not_pass_balanced_panel():
    p = pack()
    p["material_rows"] = [
        dict(
            r,
            row_key=r["campaign_id"],
            source_status="PARTIAL",
            attribution_status="UNMAPPED",
            spend="0",
            revenue_total="2",
        )
        for r in p["rows"]
    ]
    result = summarize_packs([p], dimension="material", days=2, scope={"a"})
    partition = result["partitions"][0]
    assert partition["balanced_panel"]["accounts"] == 0
    assert partition["daily"][0]["complete_accounts"] == 0
    assert partition["daily"][0]["identity_unavailable_rows"] == 2
    assert partition["daily"][0]["revenue_total"] == "4"


def test_empty_observed_days_are_not_reported_as_zero_revenue_loss():
    p = pack()
    p["rows"] = []
    result = summarize_packs([p], dimension="campaign", days=2, scope={"a"})
    first = result["partitions"][0]["daily"][0]
    assert first["spend"] is None and first["loss_proxy"] is None
    assert first["observed_accounts"] == 0
    assert result["partitions"][0]["balanced_panel"]["accounts"] == 0
