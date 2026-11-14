from decimal import Decimal

from app.modules.ai_workbench.backtests import _condition_hit, _metric
from app.modules.automation_rules.schemas import RuleDefinition


def _rule():
    return RuleDefinition.model_validate(
        {
            "accounts": {"mode": "ACCOUNTS", "advertiser_ids": ["a"]},
            "apply_to": {"level": "material", "status": "ACTIVE"},
            "aggregation": "EACH",
            "conditions": [
                {
                    "metric": "spend",
                    "range": "PAST_3_DAYS",
                    "operator": "GT",
                    "values": ["10"],
                }
            ],
            "action": {"type": "PAUSE", "level": "material"},
            "mode": "AUTO",
            "schedule": {
                "frequency": "DAILY",
                "daily_at": "12:00:00",
                "window": "CONTINUOUS",
                "timezone_basis": "ACCOUNT",
            },
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
    assert _metric([{"spend": "2", "revenue_total": "1"}], "roas_total") == Decimal(
        "0.5"
    )


def test_material_replay_works_without_execution_evidence(monkeypatch):
    from types import SimpleNamespace
    from uuid import uuid4

    import app.modules.ai_workbench.review_store as store
    from app.modules.ai_workbench.backtests import _daily_replay

    rule_id = uuid4()
    definition = _rule().model_dump(mode="json")
    task = SimpleNamespace(
        evidence={
            "rules": [{"id": str(rule_id), "definition": definition}],
            "optimizer": {"advertiser_ids": ["a"], "bc_id": "bc"},
        },
    )
    rows = [
        {
            "stat_date": "2026-11-01",
            "row_key": "m1",
            "source_status": "COMPLETE",
            "attribution_status": "RESOLVED",
            "currency": "USD",
            "timezone": "UTC",
            "spend": "6",
            "revenue_total": "0",
        },
        {
            "stat_date": "2026-11-02",
            "row_key": "m1",
            "source_status": "COMPLETE",
            "attribution_status": "RESOLVED",
            "currency": "USD",
            "timezone": "UTC",
            "spend": "6",
            "revenue_total": "0",
        },
        {
            "stat_date": "2026-11-03",
            "row_key": "m1",
            "source_status": "COMPLETE",
            "attribution_status": "RESOLVED",
            "currency": "USD",
            "timezone": "UTC",
            "spend": "5",
            "revenue_total": "1",
        },
        {
            "stat_date": "2026-11-04",
            "row_key": "m1",
            "source_status": "COMPLETE",
            "attribution_status": "RESOLVED",
            "currency": "USD",
            "timezone": "UTC",
            "spend": "5",
            "revenue_total": "1",
        },
    ]
    monkeypatch.setattr(
        store,
        "source_payloads",
        lambda session, task, category: iter(
            [
                {
                    "advertiser_id": "a",
                    "coverage": {"complete": True, "missing_days": []},
                    "material_coverage": {"complete": True, "missing_days": []},
                    "material_rows": rows,
                    "rows": [],
                }
            ]
        ),
    )
    result = _daily_replay(None, task=task, rule_id=rule_id, candidate_definitions=[])
    variant = result["variants"][0]
    assert variant["status"] == "COMPLETE"
    assert variant["metrics"]["blocked_spend"] == "10"
    assert variant["decision"]["pause_next_day"] is True


def test_unsupported_lifetime_does_not_load_large_sources(monkeypatch):
    from types import SimpleNamespace
    from uuid import uuid4

    import app.modules.ai_workbench.review_store as store
    from app.modules.ai_workbench.backtests import _daily_replay

    rid = uuid4()
    definition = _rule().model_dump(mode="json")
    definition["conditions"][0]["range"] = "LIFETIME"
    task = SimpleNamespace(
        evidence={
            "rules": [{"id": str(rid), "definition": definition}],
            "optimizer": {"advertiser_ids": ["a"], "bc_id": "bc"},
        }
    )

    def forbidden(*_args):
        raise AssertionError("unsupported rule must not load daily rows")

    monkeypatch.setattr(store, "source_payloads", forbidden)
    assert (
        _daily_replay(None, task=task, rule_id=rid, candidate_definitions=[])[
            "variants"
        ][0]["status"]
        == "UNSUPPORTED"
    )


def test_daily_replay_streams_accounts_and_preserves_currency_partitions(monkeypatch):
    import weakref
    from types import SimpleNamespace
    from uuid import uuid4

    import app.modules.ai_workbench.review_store as store
    from app.modules.ai_workbench.backtests import _daily_replay

    class Pack(dict):
        pass

    living = weakref.WeakValueDictionary()
    ids = [str(i) for i in range(1001)]
    rid = uuid4()
    definition = _rule().model_dump(mode="json")
    definition["accounts"] = {"mode": "BC", "bc_ids": ["bc"], "advertiser_ids": []}
    task = SimpleNamespace(
        evidence={
            "rules": [{"id": str(rid), "definition": definition}],
            "optimizer": {"advertiser_ids": ids, "bc_id": "bc"},
        }
    )

    def sources(*_args):
        for i, account in enumerate(ids):
            # All-account eager loading retains each dictionary and violates this bound.
            assert len(living) <= 2
            pack = Pack(
                advertiser_id=account,
                material_coverage={"complete": True},
                material_rows=[
                    {
                        "row_key": "m",
                        "source_status": "COMPLETE",
                        "attribution_status": "RESOLVED",
                        "stat_date": day,
                        "currency": "USD" if i % 2 else "EUR",
                        "timezone": "UTC",
                        "spend": "11",
                        "revenue_total": "1",
                    }
                    for day in ("2026-11-01", "2026-11-02")
                ],
            )
            living[account] = pack
            yield pack

    monkeypatch.setattr(store, "source_payloads", sources)
    variant = _daily_replay(None, task=task, rule_id=rid, candidate_definitions=[])[
        "variants"
    ][0]
    assert variant["coverage"]["accounts"] == 1001
    assert len(variant["metrics"]["partitions"]) == 2
    assert "actual_spend" not in variant["metrics"]


def test_new_policy_without_bound_rule_and_unavailable_metric(monkeypatch):
    from types import SimpleNamespace

    import pytest

    import app.modules.ai_workbench.review_store as store
    from app.modules.ai_workbench.backtests import _daily_replay
    from app.modules.ai_workbench.runtime import DailyRuleBacktest

    task = SimpleNamespace(
        evidence={"rules": [], "optimizer": {"advertiser_ids": ["a"], "bc_id": "bc"}}
    )
    definition = _rule().model_dump(mode="json")
    pack = {
        "advertiser_id": "a",
        "material_coverage": {"complete": True},
        "material_rows": [
            {
                "row_key": "m",
                "source_status": "COMPLETE",
                "attribution_status": "RESOLVED",
                "stat_date": d,
                "currency": "USD",
                "timezone": "UTC",
                "spend": "11",
                "revenue_total": "1",
            }
            for d in ["2026-11-01", "2026-11-02"]
        ],
    }
    monkeypatch.setattr(store, "source_payloads", lambda *args: iter([pack]))
    body = DailyRuleBacktest(candidate_definitions=[definition])
    result = _daily_replay(
        None,
        task=task,
        rule_id=body.rule_id,
        candidate_definitions=body.candidate_definitions,
    )
    assert result["rule_id"] is None
    assert result["comparison"] == "new_policy_vs_observed_no_additional_pause"
    variant = result["variants"][0]
    assert variant["metrics"]["blocked_spend"] == "11"
    assert variant["metrics"]["prior_low_efficiency_spend"] == "11"
    assert variant["metrics"]["kept_prior_low_efficiency_spend"] == "0"
    assert variant["decision"]["objects_observed"] == 1
    assert variant["decision"]["attribution_as_of_decision_time"] is False
    with pytest.raises(ValueError):
        DailyRuleBacktest()
    definition["conditions"][0]["metric"] = "impressions"
    unsupported = _daily_replay(
        None, task=task, rule_id=None, candidate_definitions=[definition]
    )["variants"][0]
    assert unsupported["status"] == "UNSUPPORTED"
    assert "metric_not_in_frozen_daily_facts" in unsupported["unsupported"]


def test_new_policy_cannot_extend_frozen_account_scope(monkeypatch):
    from types import SimpleNamespace

    import app.modules.ai_workbench.review_store as store
    from app.modules.ai_workbench.backtests import _daily_replay

    definition = _rule().model_dump(mode="json")
    definition["accounts"]["advertiser_ids"] = ["other"]
    task = SimpleNamespace(
        evidence={"rules": [], "optimizer": {"advertiser_ids": ["a"], "bc_id": "bc"}}
    )

    def forbidden(*_args):
        raise AssertionError("out of scope candidate must not read data")

    monkeypatch.setattr(store, "source_payloads", forbidden)
    result = _daily_replay(
        None, task=task, rule_id=None, candidate_definitions=[definition]
    )
    assert "account_scope_outside_frozen_task" in result["variants"][0]["unsupported"]


def test_unmapped_material_observed_amount_is_never_simulated_as_pausable():
    from app.modules.ai_workbench.backtests import _account_replay

    pack = {
        "material_coverage": {"complete": False},
        "material_rows": [
            {
                "row_key": "unmapped",
                "stat_date": day,
                "currency": "USD",
                "timezone": "UTC",
                "spend": "11",
                "revenue_total": "0",
                "source_status": "PARTIAL",
                "attribution_status": "UNMAPPED",
            }
            for day in ("2026-11-01", "2026-11-02")
        ],
    }
    groups, missing, complete = _account_replay(pack, _rule())
    values = groups[("USD", "UTC")]
    assert values["actual_spend"] == 22
    assert values["blocked_spend"] == 0
    assert values["objects_eligible"] == 0
    assert missing and not complete


def test_unmapped_objects_do_not_change_other_objects_when_reordered():
    from app.modules.ai_workbench.backtests import _account_replay

    def rows(key, status):
        return [
            {
                "row_key": key,
                "stat_date": day,
                "currency": "USD",
                "timezone": "UTC",
                "spend": "11",
                "revenue_total": "1",
                "source_status": "COMPLETE",
                "attribution_status": status,
            }
            for day in ("2026-11-01", "2026-11-02")
        ]

    known, unknown = rows("known", "RESOLVED"), rows("unknown", "UNMAPPED")
    for ordered in (known + unknown, unknown + known):
        pack = {"material_coverage": {"complete": False}, "material_rows": ordered}
        groups, _, _ = _account_replay(pack, _rule())
        assert groups[("USD", "UTC")]["blocked_spend"] == 11
        assert groups[("USD", "UTC")]["objects_eligible"] == 1
