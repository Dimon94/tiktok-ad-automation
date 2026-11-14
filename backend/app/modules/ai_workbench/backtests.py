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
    fields = {
        "roas_total": "revenue_total",
        "roas_d0": "revenue_d0",
        "roas_d3": "revenue_d3",
        "roas_d7": "revenue_d7",
        "roas_d14": "revenue_d14",
        "roas_d30": "revenue_d30",
    }
    if metric in fields:
        spend = _metric(rows, "spend")
        revenue = _metric(rows, fields[metric])
        return revenue / spend if spend and revenue is not None else None
    values = [_dec(row.get(metric)) for row in rows]
    return (
        sum(values, Decimal(0))
        if values and all(v is not None and v.is_finite() for v in values)
        else None
    )


def _condition_hit(rows, definition, decision_day):
    for condition in definition.conditions:
        scoped = rows
        if condition.range and condition.range != "LIFETIME":
            count = {
                "TODAY": 1,
                "YESTERDAY": 1,
                "PAST_3_DAYS": 3,
                "PAST_5_DAYS": 5,
                "PAST_7_DAYS": 7,
            }.get(condition.range)
            end = date.fromisoformat(decision_day) - timedelta(days=1)
            if condition.range == "TODAY":
                scoped = [r for r in rows if r["stat_date"] == end.isoformat()]
            elif condition.range == "YESTERDAY":
                scoped = [
                    r
                    for r in rows
                    if r["stat_date"] == (end - timedelta(days=1)).isoformat()
                ]
            elif count:
                start = end - timedelta(days=count - 1)
                scoped = [
                    r
                    for r in rows
                    if start.isoformat() <= r["stat_date"] <= end.isoformat()
                ]
        value = _metric(scoped, condition.metric)
        if value is None:
            return False
        nums = [_dec(v) for v in condition.values]
        op = condition.operator
        if op == "GT" and not value > nums[0]:
            return False
        if op == "LT" and not value < nums[0]:
            return False
        if op == "EQ" and not value == nums[0]:
            return False
        if op == "BETWEEN" and not nums[0] < value < nums[1]:
            return False
    return True


def _account_replay(pack, definition):
    """逐账户计算并立即释放日事实，内存不随整个 BC 的账户数量增长。"""
    material = definition.apply_to.level == "material"
    rows = pack.get("material_rows" if material else "rows", [])
    coverage = pack.get("material_coverage" if material else "coverage", {})
    missing = set(coverage.get("missing_days", []))
    objects = defaultdict(list)
    for row in rows:
        identity = row.get("row_key") if material else row.get("campaign_id")
        if (
            definition.apply_to.status == "SELECTED"
            and identity not in definition.apply_to.object_ids
        ):
            continue
        objects[(identity, row.get("currency"), row.get("timezone"))].append(row)
    partitions = {}
    for (_, currency, timezone), history in objects.items():
        key = (currency, timezone)
        totals = partitions.setdefault(
            key,
            dict(
                actual_spend=Decimal(0),
                actual_revenue=Decimal(0),
                kept_spend=Decimal(0),
                kept_revenue=Decimal(0),
                blocked_spend=Decimal(0),
                blocked_revenue=Decimal(0),
                wrong_stop_future_spend=Decimal(0),
                objects_paused=0,
            ),
        )
        by_day = defaultdict(list)
        for row in history:
            by_day[row["stat_date"]].append(row)
        paused = False
        future_spend = future_revenue = Decimal(0)
        # 完整历史只用于事后结果；每次条件判定显式截在决策日前，禁止未来泄漏。
        for day, current in sorted(by_day.items()):
            previous = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
            needed_days = {previous}
            for condition in definition.conditions:
                count = {"PAST_3_DAYS": 3, "PAST_5_DAYS": 5, "PAST_7_DAYS": 7}.get(
                    condition.range, 1
                )
                end = date.fromisoformat(previous) - timedelta(
                    days=int(condition.range == "YESTERDAY")
                )
                needed_days.update(
                    (end - timedelta(days=i)).isoformat() for i in range(count)
                )
            if not paused and not needed_days.intersection(missing):
                past = [r for r in history if r["stat_date"] < day]
                if past and _condition_hit(past, definition, day):
                    paused = True
                    totals["objects_paused"] += 1
            spend, revenue = (
                _metric(current, "spend"),
                _metric(current, "revenue_total"),
            )
            if spend is None or revenue is None:
                missing.add(day)
                continue
            totals["actual_spend"] += spend
            totals["actual_revenue"] += revenue
            prefix = "blocked" if paused else "kept"
            totals[prefix + "_spend"] += spend
            totals[prefix + "_revenue"] += revenue
            if paused:
                future_spend += spend
                future_revenue += revenue
        if future_spend and future_revenue >= future_spend:
            totals["wrong_stop_future_spend"] += future_spend
    return partitions, missing, coverage.get("complete", False)


def _daily_replay(session, *, task, rule_id, candidate_definitions):
    from app.modules.automation_rules.schemas import RuleDefinition
    from app.modules.automation_rules.catalog import RULE_METRICS
    from app.modules.ai_workbench.review_store import source_payloads

    baseline = next(
        (r for r in task.evidence.get("rules", []) if str(r["id"]) == str(rule_id)),
        None,
    )
    if baseline is None:
        raise HTTPException(404, "bound_rule_not_found_in_frozen_configuration")
    variants = []
    scope = task.evidence["optimizer"]
    for name, raw in [
        ("baseline", baseline["definition"]),
        *[(f"candidate_{i + 1}", d) for i, d in enumerate(candidate_definitions)],
    ]:
        try:
            definition = RuleDefinition.model_validate(raw)
        except ValueError:
            variants.append(dict(name=name, status="INVALID_DEFINITION"))
            continue
        unsupported = []
        if (
            definition.action.type != "PAUSE"
            or definition.action.level != definition.apply_to.level
        ):
            unsupported.append("action_not_same_level_pause")
        if definition.apply_to.level not in ("campaign", "material"):
            unsupported.append("daily_source_dimension_unavailable")
        if definition.aggregation != "EACH":
            unsupported.append("aggregation_not_supported")
        if definition.apply_to.status == "PAUSED" or definition.mode == "NOTIFY":
            unsupported.append(
                "notification_or_paused_policy_is_not_a_pause_simulation"
            )
        if any(c.range == "LIFETIME" for c in definition.conditions):
            unsupported.append("lifetime_history_unavailable")
        if any(
            RULE_METRICS.get(c.metric) and RULE_METRICS[c.metric].kind == "attribute"
            for c in definition.conditions
        ):
            unsupported.append("attribute_history_unavailable")
        if any(
            c.operator not in ("GT", "LT", "EQ", "BETWEEN")
            for c in definition.conditions
        ):
            unsupported.append("operator_not_supported")
        if definition.accounts.mode == "ACCOUNTS" and not set(
            definition.accounts.advertiser_ids
        ).issubset(set(scope["advertiser_ids"])):
            unsupported.append("account_scope_outside_frozen_task")
        if definition.accounts.mode == "BC" and set(definition.accounts.bc_ids) != {
            scope["bc_id"]
        }:
            unsupported.append("bc_scope_outside_frozen_task")
        if unsupported:
            variants.append(
                dict(name=name, status="UNSUPPORTED", unsupported=unsupported)
            )
            continue
        grouped = {}
        missing_days = set()
        accounts = incomplete = paused = 0
        # 不得 list(source_payloads(...))：星屿 1344 户会把 59 万行日事实同时展开至 GiB 内存。
        for pack in source_payloads(session, task, "rule_backtest_inputs"):
            account = pack.get("advertiser_id")
            if account not in scope["advertiser_ids"]:
                continue
            if (
                definition.accounts.mode == "ACCOUNTS"
                and account not in definition.accounts.advertiser_ids
            ):
                continue
            account_groups, missing, complete = _account_replay(pack, definition)
            accounts += 1
            incomplete += int(not complete or bool(missing))
            missing_days.update(missing)
            for key, values in account_groups.items():
                totals = grouped.setdefault(key, {k: Decimal(0) for k in values})
                for metric, value in values.items():
                    totals[metric] += value
        partitions = []
        for (currency, timezone), totals in sorted(
            grouped.items(), key=lambda item: str(item[0])
        ):
            metrics = {k: str(v) for k, v in totals.items() if k != "objects_paused"}
            for prefix in ("actual", "kept", "blocked"):
                spend = totals[prefix + "_spend"]
                metrics[prefix + "_roas"] = (
                    str(totals[prefix + "_revenue"] / spend) if spend else None
                )
            metrics["retrospective_blocked_loss"] = str(
                totals["blocked_spend"] - totals["blocked_revenue"]
            )
            metrics["wrong_stop_future_share"] = (
                str(totals["wrong_stop_future_spend"] / totals["blocked_spend"])
                if totals["blocked_spend"]
                else None
            )
            partitions.append(dict(currency=currency, timezone=timezone, **metrics))
            paused += int(totals["objects_paused"])
        metrics = (
            {
                k: v
                for k, v in partitions[0].items()
                if k not in ("currency", "timezone")
            }
            if len(partitions) == 1
            else {}
        )
        metrics.update(partitions=partitions, currency_partitioned=len(partitions) > 1)
        variants.append(
            dict(
                name=name,
                status=(
                    "UNAVAILABLE"
                    if not accounts
                    else "INSUFFICIENT_HISTORY"
                    if not partitions
                    else "PARTIAL_COVERAGE"
                    if incomplete
                    else "COMPLETE"
                ),
                coverage=dict(
                    accounts=accounts,
                    incomplete_accounts=incomplete,
                    missing_days=sorted(missing_days),
                ),
                decision=dict(
                    prior_day_only=True, pause_next_day=True, objects_paused=paused
                ),
                metrics=metrics,
            )
        )
    return dict(
        status="COMPLETE",
        rule_id=str(rule_id),
        variants=variants,
        interpretation="Historical daily policy proxy only; not causal savings or actual execution. Each rule is simulated independently: NEVER add overlapping rule totals. Historical intraday eligibility/status is unknown; daily close decisions apply on the next day. Incomplete coverage limits conclusions.",
    )


def daily_backtest_for_task(session, *, task, rule_id, candidate_definitions=None):
    from app.modules.ai_workbench.review_store import (
        ReviewArtifact,
        archive_artifact,
        scoped,
    )

    candidate_definitions = candidate_definitions or []
    digest = hashlib.sha256(
        json.dumps(
            ["account-stream-v2", str(rule_id), candidate_definitions],
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()
    cached = session.exec(
        scoped(ReviewArtifact, task).where(
            ReviewArtifact.kind == "daily_rule_backtest", ReviewArtifact.key == digest
        )
    ).first()
    if cached:
        return cached.payload
    payload = _daily_replay(
        session, task=task, rule_id=rule_id, candidate_definitions=candidate_definitions
    )
    return archive_artifact(
        session,
        task=task,
        kind="daily_rule_backtest",
        key=digest,
        payload={"request_digest": digest, **payload},
    )


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
