"""建议决定、实际执行和观察证据进入下一轮；从不把发布等同广告已执行。"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlmodel import Session, col, select

from app.modules.ai_workbench.execution_observations import (
    execution_status,
    freeze_rule_execution,
)
from app.modules.ai_workbench.knowledge_refs import sanitize_public


def _observations(pack: dict) -> list[dict]:
    result = []
    for report in pack.get("reporting", []):
        if (
            report.get("dimension") != "account"
            or report.get("maturity_horizon", "d0") != "d0"
        ):
            continue
        for row in report.get("rows", []):
            for bucket in row.get("metric_buckets", []):
                result.append(
                    {
                        "advertiser_id": report["advertiser_id"],
                        "window": report["window"],
                        "currency": bucket["currency"],
                        "timezone": bucket["timezone"],
                        "attribution": bucket["attribution"],
                        "values": bucket["values"],
                        "availability": bucket["availability"],
                        "coverage": row.get("coverage"),
                        "truncated": report.get("rows_truncated", False),
                    }
                )
    return result


def compare_observations(before: list[dict], after: list[dict]) -> list[dict]:
    """只比较可用、非重叠完整日的同账户同币种 D0；输出观察差异，不作因果解释。"""
    result = []
    for old in before:
        for new in after:
            axes = ("advertiser_id", "currency", "timezone", "attribution")
            if any(old.get(k) != new.get(k) for k in axes):
                continue
            if new["window"]["start_date"] <= old["window"]["complete_through"]:
                continue
            if old.get("truncated") or new.get("truncated"):
                continue
            delta = {}
            for metric in ("spend", "revenue_d0"):
                if (
                    old["availability"].get(metric)
                    == new["availability"].get(metric)
                    == "AVAILABLE"
                    and old["values"].get(metric) is not None
                    and new["values"].get(metric) is not None
                ):
                    delta[metric] = str(
                        Decimal(new["values"][metric]) - Decimal(old["values"][metric])
                    )
            if delta:
                result.append(
                    {
                        **{k: new[k] for k in axes},
                        "delta": delta,
                        "interpretation": "OBSERVATIONAL_ACCOUNT_CONTEXT",
                        "incremental_profit": None,
                    }
                )
    return result


def freeze_history(
    session: Session, *, context, optimizer_id, version, data_pack: dict
) -> dict:
    from app.modules.ai_workbench.models import BuildOccurrence
    from app.modules.ai_workbench.proposals import AIProposal
    from app.modules.ai_workbench.reviews import DSHReviewTask
    from app.modules.automation_rules.models import AutomationRuleEvidence

    cutoff = datetime.fromisoformat(data_pack["cutoff"])
    tasks = session.exec(
        select(DSHReviewTask)
        .where(
            col(DSHReviewTask.tenant_id) == context.tenant_id,
            col(DSHReviewTask.optimizer_id) == optimizer_id,
            col(DSHReviewTask.created_at) <= cutoff,
        )
        .order_by(col(DSHReviewTask.created_at).desc())
        .limit(21)
    ).all()
    # 优化师改过 BC/账户后，历史任务不能把旧范围再次带进当前模型。
    tasks = [
        t
        for t in tasks
        if t.evidence.get("optimizer", {}).get("bc_id") == version.bc_id
        and set(t.evidence.get("optimizer", {}).get("advertiser_ids", [])).issubset(
            set(version.advertiser_ids)
        )
    ][:20]
    task_map = {t.id: t for t in tasks}
    proposals = (
        session.exec(
            select(AIProposal)
            .where(
                col(AIProposal.tenant_id) == context.tenant_id,
                col(AIProposal.optimizer_id) == optimizer_id,
                col(AIProposal.review_task_id).in_(task_map),
                col(AIProposal.created_at) <= cutoff,
            )
            .order_by(col(AIProposal.created_at).desc())
            .limit(101)
        ).all()
        if task_map
        else []
    )
    feedback = []
    current = _observations(data_pack)
    for p in proposals[:100]:
        execution = "NOT_EXECUTED"
        evidence = []
        if p.status != "ACCEPTED":
            execution = {
                "PENDING": "AWAITING_DECISION",
                "REJECTED": "REJECTED",
                "STALE": "STALE",
            }.get(p.status, "UNKNOWN")
        elif p.kind == "strategy":
            builds = session.exec(
                select(BuildOccurrence)
                .where(
                    col(BuildOccurrence.tenant_id) == context.tenant_id,
                    col(BuildOccurrence.optimizer_id) == optimizer_id,
                    col(BuildOccurrence.bc_id) == version.bc_id,
                    col(BuildOccurrence.strategy_version_id) == p.published_version_id,
                    col(BuildOccurrence.created_at) >= p.decision_at,
                    col(BuildOccurrence.created_at) <= cutoff,
                )
                .order_by(col(BuildOccurrence.created_at).desc())
                .limit(51)
            ).all()
            builds = [
                b
                for b in builds
                if set(b.advertiser_ids).issubset(set(version.advertiser_ids))
            ]
            evidence: list[dict[str, Any]] = [
                {
                    "id": str(b.id),
                    "status": b.status,
                    "strategy_version_id": str(b.strategy_version_id),
                }
                for b in builds[:50]
            ]
            if builds:
                execution = (
                    "FAILED"
                    if all(b.status == "FAILED" for b in builds)
                    else "UNKNOWN"
                    if any(b.status == "UNKNOWN" for b in builds)
                    else "BUILD_RECORDED"
                )
        else:
            rules = session.exec(
                select(AutomationRuleEvidence)
                .where(
                    col(AutomationRuleEvidence.tenant_id) == context.tenant_id,
                    col(AutomationRuleEvidence.optimizer_id) == optimizer_id,
                    col(AutomationRuleEvidence.bc_id) == version.bc_id,
                    col(AutomationRuleEvidence.rule_id) == p.published_version_id,
                    col(AutomationRuleEvidence.rule_version)
                    == (p.base_rule_version or 0) + 1,
                    col(AutomationRuleEvidence.created_at) >= p.decision_at,
                    col(AutomationRuleEvidence.created_at) <= cutoff,
                )
                .order_by(col(AutomationRuleEvidence.created_at).desc())
                .limit(51)
            ).all()
            rules = [
                r
                for r in rules
                if set(r.advertiser_ids).issubset(set(version.advertiser_ids))
            ]
            evidence: list[dict[str, Any]] = [
                {
                    "id": str(r.id),
                    "rule_version": r.rule_version,
                    "hits": len(r.hits),
                    "actions": r.action_ledger_refs,
                    "readback": freeze_rule_execution(
                        session,
                        context=context,
                        optimizer_id=optimizer_id,
                        version=version,
                        evidence=r,
                        cutoff=cutoff,
                    ),
                }
                for r in rules[:50]
            ]
            if rules:
                execution = execution_status(
                    [
                        row["readback"]["status"]
                        for row in evidence
                        if row["readback"]["status"] != "CHECKED_WITHOUT_ACTION"
                    ],
                    missing=len(rules) > 50,
                )
        source = task_map[p.review_task_id]
        baseline = _observations(source.evidence.get("data_pack", {}))
        feedback.append(
            {
                "id": str(p.id),
                "review_task_id": str(p.review_task_id),
                "kind": p.kind,
                "status": p.status,
                "problem": p.evidence.get("problem", ""),
                "rationale": p.evidence.get("rationale", ""),
                "expected_effect": p.evidence.get("expected_effect", ""),
                "validation": p.evidence.get("validation", ""),
                "published_version_id": p.published_version_id,
                "decision_at": p.decision_at,
                "decision_reason": p.decision_reason,
                "execution_status": execution,
                "execution_evidence": evidence,
                "observation_status": "IMMATURE"
                if p.decision_at
                and (datetime.fromisoformat(data_pack["cutoff"]) - p.decision_at).days
                < 7
                else "CHECK_WINDOW_AND_INTERVENTIONS",
                "assessment": "INSUFFICIENT_EVIDENCE",
                "incremental_profit": None,
                "account_context_before": baseline,
                "account_context_after": current,
                "account_context_change": compare_observations(baseline, current),
                "confounders": [
                    "possible_manual_interventions",
                    "possible_mixed_versions",
                    "no_randomized_control",
                ],
                "next_review_options": [
                    "KEEP",
                    "WITHDRAW_PROPOSAL",
                    "OBSERVE",
                    "INSUFFICIENT_EVIDENCE",
                ],
            }
        )
    return sanitize_public(
        {
            "reviews": [
                {
                    "id": str(t.id),
                    "status": t.status,
                    "summary": t.result.get("summary"),
                    "recommendations": t.result.get("recommendations", []),
                    "experience_candidates": t.result.get("experience_candidates", []),
                    "finished_at": t.finished_at,
                }
                for t in tasks
            ],
            "proposals": feedback,
            "truncated": len(proposals) > 100 or len(tasks) == 20,
        }
    )
