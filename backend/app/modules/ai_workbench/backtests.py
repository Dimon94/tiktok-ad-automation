"""任务范围历史规则回测：仅重放已冻结事实，不读当前报表或执行广告动作。"""

import hashlib
import json
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import HTTPException
from sqlmodel import select

from app.core.errors import DomainError
from app.modules.ai_workbench.knowledge_refs import sanitize_public
from app.modules.ai_workbench.rule_history import rule_snapshot_report
from app.modules.automation_rules.models import AutomationRuleEvidence


def _dec(value):
    try:
        return Decimal(str(value)) if value is not None else None
    except Exception:
        return None


def _metric(rows, metric):
    fields = {"roas_total": "revenue_total", "roas_d0": "revenue_d0", "roas_d3": "revenue_d3",
              "roas_d7": "revenue_d7", "roas_d14": "revenue_d14", "roas_d30": "revenue_d30"}
    if metric in fields:
        spend = _metric(rows, "spend")
        revenue = _metric(rows, fields[metric])
        return revenue / spend if spend else None
    values = [_dec(row.get(metric)) for row in rows]
    values = [v for v in values if v is not None]
    return sum(values, Decimal(0)) if values else None


def _condition_hit(rows, definition, decision_day):
    for condition in definition.conditions:
        scoped = rows
        if condition.range and condition.range != "LIFETIME":
            count = {"TODAY": 1, "YESTERDAY": 1, "PAST_3_DAYS": 3,
                     "PAST_5_DAYS": 5, "PAST_7_DAYS": 7}.get(condition.range)
            end = date.fromisoformat(decision_day) - timedelta(days=1)
            if condition.range == "TODAY":
                scoped = [r for r in rows if r["stat_date"] == end.isoformat()]
            elif condition.range == "YESTERDAY":
                scoped = [r for r in rows if r["stat_date"] == (end - timedelta(days=1)).isoformat()]
            elif count:
                start = end - timedelta(days=count - 1)
                scoped = [r for r in rows if start.isoformat() <= r["stat_date"] <= end.isoformat()]
        value = _metric(scoped, condition.metric)
        if value is None:
            return False
        nums = [_dec(v) for v in condition.values]
        op = condition.operator
        if op == "GT" and not value > nums[0]: return False
        if op == "LT" and not value < nums[0]: return False
        if op == "EQ" and not value == nums[0]: return False
        if op == "BETWEEN" and not nums[0] < value < nums[1]: return False
    return True


def _daily_replay(session, *, task, rule_id, candidate_definitions):
    """在任务冻结的逐日账户事实上重放 PAUSE；决策日只读取之前日期。"""
    from app.modules.automation_rules.schemas import RuleDefinition
    from app.modules.ai_workbench.review_store import source_payloads

    frozen_rules = {str(r["id"]): r for r in task.evidence.get("rules", [])}
    baseline = frozen_rules.get(str(rule_id))
    if baseline is None:
        raise HTTPException(404, "bound_rule_not_found_in_frozen_configuration")
    definitions = [("baseline", baseline["definition"])]
    for index, raw in enumerate(candidate_definitions):
        definitions.append((f"candidate_{index + 1}", raw))
    facts = list(source_payloads(session, task, "rule_backtest_inputs"))
    if not facts:
        return {"status": "UNAVAILABLE", "reason": "daily_reporting_history_not_frozen", "variants": []}
    variants = []
    for name, raw in definitions:
        try:
            definition = RuleDefinition.model_validate(raw)
        except Exception as exc:
            variants.append({"name": name, "status": "INVALID_DEFINITION", "reason": str(exc)})
            continue
        scope_ids = set(task.evidence.get("optimizer", {}).get("advertiser_ids", []))
        if definition.accounts.mode == "ACCOUNTS" and not set(definition.accounts.advertiser_ids).issubset(scope_ids):
            variants.append({"name": name, "status": "OUT_OF_SCOPE", "reason": "account_scope_outside_frozen_task"})
            continue
        if definition.accounts.mode == "BC" and set(definition.accounts.bc_ids) != {task.evidence.get("optimizer", {}).get("bc_id")}:
            variants.append({"name": name, "status": "OUT_OF_SCOPE", "reason": "bc_scope_outside_frozen_task"})
            continue
        unsupported = []
        if definition.action.type != "PAUSE": unsupported.append("action_not_pause")
        if definition.apply_to.level != "campaign":
            unsupported.append("daily_source_dimension_unavailable")
        if definition.aggregation not in ("EACH", "ALL"):
            unsupported.append("aggregation_not_supported")
        if any(c.range == "LIFETIME" for c in definition.conditions):
            unsupported.append("lifetime_history_unavailable")
        from app.modules.automation_rules.catalog import RULE_METRICS
        if any(RULE_METRICS.get(c.metric) and RULE_METRICS[c.metric].kind == "attribute" for c in definition.conditions):
            unsupported.append("attribute_history_unavailable")
        if unsupported:
            variants.append({"name": name, "status": "UNSUPPORTED", "unsupported": unsupported})
            continue
        all_rows = []
        missing = []
        for pack in facts:
            if not pack.get("coverage", {}).get("complete"):
                missing.extend(pack.get("coverage", {}).get("missing_days", []))
            for row in pack.get("rows", []):
                row = dict(row); row["advertiser_id"] = pack["advertiser_id"]
                all_rows.append(row)
        days = sorted({r["stat_date"] for r in all_rows})
        if len(days) < 2:
            variants.append({"name": name, "status": "INSUFFICIENT_HISTORY", "days": len(days)})
            continue
        # 当前冻结源按 campaign 维度提供；material 规则必须明确标注为近似，不冒充素材级精确回测。
        if definition.apply_to.level == "material":
            unsupported.append("material_identity_not_frozen_for_exact_replay")
        if unsupported:
            variants.append({"name": name, "status": "UNSUPPORTED", "unsupported": unsupported})
            continue
        # object level is campaign_report_day; duplicate keys are impossible in the frozen source.
        by_object = defaultdict(list)
        for row in all_rows: by_object[(row["advertiser_id"], row.get("campaign_id"))].append(row)
        paused = set(); hit_on = {}; blocked = []; kept = []
        for day in days:
            prior_day = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
            # 收盘判定，命中后次日才阻断。
            for key, rows in by_object.items():
                if key in paused: continue
                prior = [r for r in rows if r["stat_date"] <= prior_day]
                if prior and _condition_hit(prior, definition, day):
                    paused.add(key); hit_on[key] = day
            for key, rows in by_object.items():
                current = [r for r in rows if r["stat_date"] == day]
                if not current: continue
                spend = _metric(current, "spend") or Decimal(0); revenue = _metric(current, "revenue_total") or Decimal(0)
                (blocked if key in paused and hit_on[key] < day else kept).append((key, day, spend, revenue))
        def total(items, idx): return sum((item[idx] for item in items), Decimal(0))
        b_spend, b_rev = total(blocked, 2), total(blocked, 3); k_spend, k_rev = total(kept, 2), total(kept, 3)
        wrong = Decimal(0)
        for key, hit in hit_on.items():
            future = [x for x in by_object[key] if x["stat_date"] > hit]
            fs, fr = _metric(future, "spend") or Decimal(0), _metric(future, "revenue_total") or Decimal(0)
            if fs and fr / fs >= Decimal("1") : wrong += fs
        variants.append({"name": name, "status": "COMPLETE" if not missing else "PARTIAL_COVERAGE",
            "coverage": {"missing_days": sorted(set(missing)), "days": days},
            "decision": {"prior_day_only": True, "pause_next_day": True, "objects_paused": len(paused)},
            "metrics": {"actual_spend": str(b_spend + k_spend), "actual_revenue": str(b_rev + k_rev),
                        "actual_roas": str((b_rev + k_rev)/(b_spend+k_spend)) if b_spend+k_spend else None,
                        "kept_spend": str(k_spend), "kept_revenue": str(k_rev), "kept_roas": str(k_rev/k_spend) if k_spend else None,
                        "blocked_spend": str(b_spend), "blocked_revenue": str(b_rev), "blocked_roas": str(b_rev/b_spend) if b_spend else None,
                        "retrospective_blocked_loss": str(b_spend - b_rev if b_spend > b_rev else Decimal(0)), "wrong_stop_future_spend": str(wrong),
                        "wrong_stop_future_share": str(wrong/b_spend) if b_spend else None,
                        "inefficiency_spend": str(b_spend - b_rev) if b_spend > b_rev else "0",
                        "inefficiency_share": str((b_spend-b_rev)/b_spend) if b_spend else None}})
    return {"status": "COMPLETE", "rule_id": str(rule_id), "variants": variants,
            "interpretation": "Historical replay only; blocked loss and wrong-stop are observed counterfactual proxies, not causal avoided loss."}


def daily_backtest_for_task(session, *, task, rule_id, candidate_definitions=None):
    candidate_definitions = candidate_definitions or []
    payload = _daily_replay(session, task=task, rule_id=rule_id,
                            candidate_definitions=candidate_definitions)
    digest = hashlib.sha256(json.dumps([str(rule_id), candidate_definitions], sort_keys=True, default=str).encode()).hexdigest()
    from app.modules.ai_workbench.review_store import archive_artifact
    return archive_artifact(session, task=task, kind="daily_rule_backtest", key=digest,
                            payload={"request_digest": digest, **payload})


def backtest_for_task(
    session, *, task, evidence_id: UUID, candidate_definition: dict
) -> dict:
    from app.modules.automation_rules.schemas import RuleDefinition

    candidate = RuleDefinition.model_validate(candidate_definition).model_dump(
        mode="json"
    )
    from app.modules.ai_workbench.review_store import (
        ReviewArtifact,
        archive_artifact,
        delivered_evidence_ids,
        scoped,
        source_payloads,
    )

    if str(evidence_id) not in delivered_evidence_ids(session, task):
        raise HTTPException(404, "Read frozen rule evidence before backtesting")
    rule_sources = [
        row
        for payload in source_payloads(session, task, "rule_evidence")
        for row in payload
    ]
    frozen = next(
        (row for row in rule_sources if row["id"] == str(evidence_id)),
        None,
    )
    if frozen is None:
        raise HTTPException(404, "Frozen rule evidence not found")
    digest = hashlib.sha256(
        json.dumps(
            [str(evidence_id), candidate], sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    cached = session.exec(
        scoped(ReviewArtifact, task).where(
            ReviewArtifact.kind == "rule_backtest", ReviewArtifact.key == digest
        )
    ).first()
    if cached:
        return cached.payload
    source = session.exec(
        select(AutomationRuleEvidence).where(
            AutomationRuleEvidence.id == evidence_id,
            AutomationRuleEvidence.tenant_id == task.tenant_id,
            AutomationRuleEvidence.optimizer_id == task.optimizer_id,
            AutomationRuleEvidence.bc_id == task.evidence["optimizer"]["bc_id"],
        )
    ).first()
    if source is None or not set(source.advertiser_ids).issubset(
        set(task.evidence["optimizer"]["advertiser_ids"])
    ):
        raise HTTPException(404, "Frozen rule evidence not found")
    if source.definition != frozen["definition"] or source.data_freshness.get(
        "evaluation_inputs", {}
    ).get("digest") != frozen.get("historical_inputs_digest"):
        raise DomainError("ai_backtest_source_changed", "历史事实与冻结复盘不一致")
    result = sanitize_public(
        {
            "id": str(uuid5(NAMESPACE_URL, f"tkada:{task.id}:{digest}")),
            "request_digest": digest,
            "evidence_id": str(evidence_id),
            "candidate_definition": candidate,
            "result": rule_snapshot_report(source, candidate_definition=candidate),
            "interpretation": "Historical hit differences only. Does not establish actual avoided losses, false pauses, causal lift or future results.",
        }
    )
    return archive_artifact(
        session,
        task=task,
        kind="rule_backtest",
        key=digest,
        payload=result,
        identity=UUID(result["id"]),
    )
