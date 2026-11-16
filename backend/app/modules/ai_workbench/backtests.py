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
        # 与线上执行共用比较语义，尤其 BETWEEN 为开区间。
        from app.modules.automation_rules.evaluate import _matches

        if not _matches(condition, value):
            return False
    return True


def _account_replay(pack, definition):
    """逐账户计算并立即释放日事实，内存不随整个 BC 的账户数量增长。"""
    material = definition.apply_to.level == "material"
    rows = pack.get("material_rows" if material else "rows", [])
    coverage = pack.get("material_coverage" if material else "coverage", {})
    missing = set(coverage.get("missing_days", []))
    publication_missing = frozenset(missing)
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
            {
                "actual_spend": Decimal(0),
                "actual_revenue": Decimal(0),
                "kept_spend": Decimal(0),
                "kept_revenue": Decimal(0),
                "blocked_spend": Decimal(0),
                "blocked_revenue": Decimal(0),
                "wrong_stop_future_spend": Decimal(0),
                "objects_paused": 0,
                "objects_observed": 0,
                "objects_eligible": 0,
                "prior_low_efficiency_spend": Decimal(0),
                "kept_prior_low_efficiency_spend": Decimal(0),
                "prior_long_tail_spend": Decimal(0),
                "kept_prior_low_efficiency_long_tail_spend": Decimal(0),
                "kept_retrospective_loss": Decimal(0),
            },
        )
        totals["objects_observed"] += 1
        eligible = not material or all(
            r.get("source_status") == "COMPLETE"
            and r.get("attribution_status") == "RESOLVED"
            for r in history
        )
        totals["objects_eligible"] += int(eligible)
        if not eligible:
            missing.update(
                r["stat_date"]
                for r in history
                if r.get("source_status") != "COMPLETE"
                or r.get("attribution_status") != "RESOLVED"
            )
        # 数据缺口是每对象的判定依赖；某个未映射对象不能污染其他对象，结果须与遍历顺序无关。
        object_missing = set(publication_missing) | {
            r["stat_date"]
            for r in history
            if not all(
                v is not None and v.is_finite()
                for v in (_dec(r.get("spend")), _dec(r.get("revenue_total")))
            )
        }
        by_day = defaultdict(list)
        for row in history:
            by_day[row["stat_date"]].append(row)
        paused = False
        future_spend = future_revenue = Decimal(0)
        prior_spend = prior_revenue = Decimal(0)
        prior_complete, prior_seen = True, False
        # 日期不穿越决策日，但收入是捕获时的归因值，并非历史时点值；结果只能作为代理。
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
            if eligible and not paused and not needed_days.intersection(object_missing):
                past = [r for r in history if r["stat_date"] < day]
                if past and _condition_hit(past, definition, day):
                    paused = True
                    totals["objects_paused"] += 1
            spend, revenue = (
                _metric(current, "spend"),
                _metric(current, "revenue_total"),
            )
            if spend is None or revenue is None:
                prior_complete, prior_seen = False, True
                missing.add(day)
                continue
            totals["actual_spend"] += spend
            totals["actual_revenue"] += revenue
            prefix = "blocked" if paused else "kept"
            totals[prefix + "_spend"] += spend
            totals[prefix + "_revenue"] += revenue
            prior_roas = (
                prior_revenue / prior_spend
                if prior_complete and prior_seen and prior_spend
                else None
            )
            # 低效/长尾是对象分层后的整笔当日消耗，不是消耗减收入。
            low = prior_roas is not None and prior_roas < Decimal("0.9")
            tail = prior_complete and prior_seen and prior_spend < Decimal("20")
            if low:
                totals["prior_low_efficiency_spend"] += spend
                if not paused:
                    totals["kept_prior_low_efficiency_spend"] += spend
            if tail:
                totals["prior_long_tail_spend"] += spend
            if low and tail and not paused:
                totals["kept_prior_low_efficiency_long_tail_spend"] += spend
            if not paused:
                totals["kept_retrospective_loss"] += max(spend - revenue, Decimal(0))
            if paused:
                future_spend += spend
                future_revenue += revenue
            prior_spend += spend
            prior_revenue += revenue
            prior_seen = True
        if future_spend and future_revenue >= future_spend:
            totals["wrong_stop_future_spend"] += future_spend
    return partitions, missing, coverage.get("complete", False)


def _daily_replay(session, *, task, rule_id, candidate_definitions):
    from app.modules.ai_workbench.review_store import source_payloads
    from app.modules.automation_rules.catalog import RULE_METRICS
    from app.modules.automation_rules.schemas import RuleDefinition

    baseline = next(
        (r for r in task.evidence.get("rules", []) if str(r["id"]) == str(rule_id)),
        None,
    )
    if rule_id is not None and baseline is None:
        raise HTTPException(404, "bound_rule_not_found_in_frozen_configuration")
    if rule_id is None and not candidate_definitions:
        raise HTTPException(422, "candidate_definition_required_without_bound_rule")
    variants = []
    scope = task.evidence["optimizer"]
    for name, raw in [
        *([("baseline", baseline["definition"])] if baseline else []),
        *[(f"candidate_{i + 1}", d) for i, d in enumerate(candidate_definitions)],
    ]:
        try:
            definition = RuleDefinition.model_validate(raw)
        except ValueError:
            variants.append({"name": name, "status": "INVALID_DEFINITION"})
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
        frozen_metrics = {"spend", "revenue_total", "roas_total"}
        if any(c.metric not in frozen_metrics for c in definition.conditions):
            unsupported.append("metric_not_in_frozen_daily_facts")
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
                {"name": name, "status": "UNSUPPORTED", "unsupported": unsupported}
            )
            continue
        grouped = {}
        missing_days = set()
        accounts = incomplete = paused = observed = eligible_objects = 0
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
            observed += int(totals["objects_observed"])
            eligible_objects += int(totals["objects_eligible"])
            for numerator, denominator, label in (
                (
                    "prior_low_efficiency_spend",
                    "actual_spend",
                    "prior_low_efficiency_share",
                ),
                (
                    "kept_prior_low_efficiency_spend",
                    "kept_spend",
                    "kept_prior_low_efficiency_share",
                ),
                ("prior_long_tail_spend", "actual_spend", "prior_long_tail_share"),
            ):
                metrics[label] = (
                    str(totals[numerator] / totals[denominator])
                    if totals[denominator]
                    else None
                )
                partitions[-1][label] = metrics[label]
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
            {
                "name": name,
                "status": (
                    "UNAVAILABLE"
                    if not accounts
                    else "INSUFFICIENT_HISTORY"
                    if not partitions
                    else "PARTIAL_COVERAGE"
                    if incomplete
                    else "COMPLETE"
                ),
                "coverage": {
                    "accounts": accounts,
                    "incomplete_accounts": incomplete,
                    "missing_days": sorted(missing_days),
                },
                "decision": {
                    "prior_day_only": True,
                    "pause_next_day": True,
                    "objects_paused": paused,
                    "objects_observed": observed,
                    "objects_eligible": eligible_objects,
                    "attribution_as_of_decision_time": False,
                    "historical_status_verified": False,
                },
                "metrics": metrics,
            }
        )
    return {
        "status": "COMPLETE",
        "rule_id": str(rule_id) if rule_id is not None else None,
        "comparison": "bound_rule"
        if baseline
        else "new_policy_vs_observed_no_additional_pause",
        "supported_daily_metrics": ["spend", "revenue_total", "roas_total"],
        "diagnostics_definition": {
            "low_efficiency": "prior observed cumulative ROAS < 0.9; entire current-day spend, not loss",
            "long_tail": "prior observed cumulative spend < 20 in partition currency; sensitivity diagnostic, not an approved threshold",
            "kept_retrospective_loss": "sum of positive daily spend minus captured revenue on kept rows; gross loss proxy, not net operating profit",
            "scope": "Cumulative means this frozen window only, never LIFETIME. Missing revenue/status and later attribution restrict interpretation.",
        },
        "variants": variants,
        "interpretation": "Historical daily policy proxy only; not causal savings or actual execution. Each rule is simulated independently: NEVER add overlapping rule totals. Captured revenue may include attribution received after the historical decision day: this is NOT a point-in-time or leakage-free backtest. No held-out validation is established by this tool. Unmapped/residual/stale material identities are retained in observed totals but never simulated as paused. Historical intraday eligibility/status is unknown; daily close decisions apply on the next day. Incomplete coverage limits conclusions.",
    }


def daily_backtest_for_task(
    session, *, task, rule_id=None, candidate_definitions=None, cache_only=False
):
    from app.modules.ai_workbench.review_store import (
        ReviewArtifact,
        archive_artifact,
        scoped,
    )

    candidate_definitions = candidate_definitions or []
    digest = hashlib.sha256(
        json.dumps(
            ["account-stream-v3", str(rule_id), candidate_definitions],
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
    if cache_only:
        # 网关超时不代表后台计算已停止；只查原请求产物，禁止重扫/重复回放。
        return {"status": "PENDING_COMPUTATION"}
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
