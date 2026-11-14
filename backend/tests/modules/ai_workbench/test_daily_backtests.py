from datetime import date
from decimal import Decimal

from app.modules.ai_workbench.backtests import _condition_hit, _metric
from app.modules.automation_rules.schemas import RuleDefinition


def _rule():
    return RuleDefinition.model_validate(
        {
            "accounts": {"mode": "ACCOUNTS", "advertiser_ids": ["a"]},
            "apply_to": {"level": "material", "status": "ACTIVE"},
            "aggregation": "EACH",
            "conditions": [{"metric": "spend", "range": "PAST_3_DAYS", "operator": "GT", "values": ["10"]}],
            "action": {"type": "PAUSE", "level": "material"},
            "mode": "AUTO",
            "schedule": {"frequency": "DAILY", "daily_at": "12:00:00", "window": "CONTINUOUS", "timezone_basis": "ACCOUNT"},
        }
    )


def test_daily_replay_condition_uses_only_prior_days():
    definition = _rule()
    rows = [
        {"stat_date": "2026-11-01", "spend": "6"},
        {"stat_date": "2026-11-02", "spend": "6"},
        {"stat_date": "2026-11-03", "spend": "1"},
        {"stat_date": "2026-11-04", "spend": "100"},
    ]
    assert _condition_hit(rows, definition, "2026-11-04")
    # A future high spend cannot make a decision on its own day.
    assert not _condition_hit(rows, definition, "2026-11-01")


def test_daily_metric_preserves_decimal_and_missing_is_unknown():
    assert _metric([{"spend": "0.10"}, {"spend": "0.20"}], "spend") == Decimal("0.30")
    assert _metric([{"spend": None}], "spend") is None
    assert _metric([{"spend": "2", "revenue_total": "1"}], "roas_total") == Decimal("0.5")


def test_material_replay_works_without_execution_evidence(monkeypatch):
    from types import SimpleNamespace
    from uuid import uuid4
    import app.modules.ai_workbench.review_store as store
    from app.modules.ai_workbench.backtests import _daily_replay

    rule_id = uuid4()
    definition = _rule().model_dump(mode="json")
    task = SimpleNamespace(
        evidence={"rules": [{"id": str(rule_id), "definition": definition}], "optimizer": {"advertiser_ids": ["a"], "bc_id": "bc"}},
    )
    rows = [
        {"stat_date": "2026-11-01", "row_key": "m1", "currency": "USD", "timezone": "UTC", "spend": "6", "revenue_total": "0"},
        {"stat_date": "2026-11-02", "row_key": "m1", "currency": "USD", "timezone": "UTC", "spend": "6", "revenue_total": "0"},
        {"stat_date": "2026-11-03", "row_key": "m1", "currency": "USD", "timezone": "UTC", "spend": "5", "revenue_total": "1"},
        {"stat_date": "2026-11-04", "row_key": "m1", "currency": "USD", "timezone": "UTC", "spend": "5", "revenue_total": "1"},
    ]
    monkeypatch.setattr(store, "source_payloads", lambda session, task, category: iter([
        {"advertiser_id": "a", "coverage": {"complete": True, "missing_days": []}, "material_coverage": {"complete": True, "missing_days": []}, "material_rows": rows, "rows": []}
    ]))
    result = _daily_replay(None, task=task, rule_id=rule_id, candidate_definitions=[])
    variant = result["variants"][0]
    assert variant["status"] == "COMPLETE"
    assert variant["metrics"]["blocked_spend"] == "5"
    assert variant["decision"]["pause_next_day"] is True
