"""基于不可变执行身份和已鉴权素材生产谱系关联本地正式报表；不按名称推断。"""

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import String, cast
from sqlmodel import Session, col, select

from app.modules.ai_workbench.models import BuildOccurrence
from app.modules.ai_workbench.order_models import MaterialOrder
from app.modules.ai_workbench.production_origin import production_payload
from app.modules.builds.execution_models import ExecutionStep
from app.modules.builds.preview_models import BuildUnit
from app.modules.materials.models import AccountMaterial
from app.modules.materials.push_models import MaterialPushBatch, MaterialPushItem
from app.modules.reporting.queries import freeze_review_page
from app.modules.reporting.schemas import ReportingFilter

MAX_MAPPINGS = 500
MAX_GROUPS = 20


def resolve_mappings(session: Session, *, context, optimizer_id, version, cutoff):
    from app.modules.reporting.filters import authorized_grants

    accounts = tuple(
        sorted(
            g.advertiser_id
            for g in authorized_grants(
                session,
                context=context,
                bc_id=version.bc_id,
                advertiser_ids=tuple(version.advertiser_ids),
            )
        )
    )
    # 发生记录、执行步骤和 frozen unit 三方同租户、BC、submission/unit，避免同名剧误归因。
    campaigns = session.exec(
        select(BuildOccurrence, ExecutionStep, BuildUnit)
        .join(
            ExecutionStep,
            (col(ExecutionStep.tenant_id) == col(BuildOccurrence.tenant_id))
            & (col(ExecutionStep.submission_id) == col(BuildOccurrence.submission_id))
            & (col(ExecutionStep.bc_id) == col(BuildOccurrence.bc_id)),
        )
        .join(
            BuildUnit,
            (col(BuildUnit.tenant_id) == col(ExecutionStep.tenant_id))
            & (col(BuildUnit.id) == col(ExecutionStep.unit_id))
            & (col(BuildUnit.preview_id) == col(ExecutionStep.preview_id))
            & (col(BuildUnit.bc_id) == col(ExecutionStep.bc_id)),
        )
        .where(
            col(BuildOccurrence.tenant_id) == context.tenant_id,
            col(BuildOccurrence.optimizer_id) == optimizer_id,
            col(BuildOccurrence.bc_id) == version.bc_id,
            col(BuildOccurrence.created_at) <= cutoff,
            col(ExecutionStep.kind) == "CAMPAIGN",
            col(ExecutionStep.status) == "SUCCEEDED",
            col(ExecutionStep.remote_id).is_not(None),
            col(BuildUnit.advertiser_id).in_(accounts),
        )
        .order_by(col(BuildOccurrence.scheduled_at).desc(), col(ExecutionStep.id))
        .limit(MAX_MAPPINGS + 1)
    ).all()
    grouped = defaultdict(lambda: {"ids": set(), "evidence_ids": set()})
    owners = defaultdict(set)
    for occurrence, step, unit in campaigns[:MAX_MAPPINGS]:
        if not set(occurrence.advertiser_ids).issubset(set(accounts)):
            continue
        key = ("strategy", str(occurrence.strategy_version_id), unit.advertiser_id)
        grouped[key]["ids"].add(step.remote_id)
        grouped[key]["evidence_ids"].add(str(occurrence.id))
        owners[("strategy", unit.advertiser_id, step.remote_id)].add(key)

    materials = session.exec(
        select(MaterialPushItem, AccountMaterial, MaterialOrder)
        .join(
            MaterialPushBatch,
            (col(MaterialPushBatch.tenant_id) == col(MaterialPushItem.tenant_id))
            & (col(MaterialPushBatch.id) == col(MaterialPushItem.batch_id)),
        )
        .join(
            AccountMaterial,
            (col(AccountMaterial.tenant_id) == col(MaterialPushItem.tenant_id))
            & (col(AccountMaterial.material_id) == col(MaterialPushItem.material_id)),
        )
        .join(
            MaterialOrder,
            (col(MaterialOrder.tenant_id) == col(MaterialPushItem.tenant_id))
            & (
                col(MaterialPushItem.production_origin)["order_id"].astext
                == cast(col(MaterialOrder.id), String)
            ),
        )
        .where(
            col(MaterialPushItem.tenant_id) == context.tenant_id,
            col(MaterialPushBatch.created_at) <= cutoff,
            col(MaterialOrder.created_at) <= cutoff,
            col(AccountMaterial.bc_id) == version.bc_id,
            col(AccountMaterial.advertiser_id).in_(accounts),
            col(AccountMaterial.status) == "available",
            col(AccountMaterial.video_id).is_not(None),
            col(AccountMaterial.verified_at) <= cutoff,
        )
        .order_by(col(MaterialPushItem.id), col(AccountMaterial.id))
        .limit(MAX_MAPPINGS + 1)
    ).all()
    from app.modules.ai_workbench.review_orders import order_in_scope

    for item, asset, order in materials[:MAX_MAPPINGS]:
        origin = item.production_origin or {}
        if origin.get("job_id") != order.job_id or origin.get(
            "strategy_id"
        ) not in production_payload(order).get("selectedStrategyIds", []):
            continue
        key = ("material_strategy", origin["strategy_id"], asset.advertiser_id)
        owners[("material_strategy", asset.advertiser_id, asset.video_id)].add(key)
        if order.optimizer_id == optimizer_id and order_in_scope(
            session, order, version
        ):
            grouped[key]["ids"].add(asset.video_id)
            grouped[key]["evidence_ids"].add(str(order.id))
    conflicts = []
    for (kind, advertiser, remote), keys in owners.items():
        if len(keys) > 1:
            conflicts.append(
                {
                    "kind": kind,
                    "advertiser_id": advertiser,
                    "remote_id": remote,
                    "reason": "multiple_origin_versions",
                }
            )
            for key in keys:
                if key in grouped:
                    grouped[key]["ids"].discard(remote)
    # 超过查询上限时可能漏掉另一条冲突来源；不能把部分谱系当作唯一归属。
    if len(materials) > MAX_MAPPINGS:
        for key in grouped:
            if key[0] == "material_strategy":
                grouped[key]["ids"].clear()
    rows = [
        {
            "kind": kind,
            "version_id": identity,
            "advertiser_id": advertiser,
            "remote_ids": sorted(value["ids"]),
            "evidence_ids": sorted(value["evidence_ids"]),
        }
        for (kind, identity, advertiser), value in sorted(grouped.items())
        if value["ids"]
    ]
    return {
        "rows": rows[:MAX_GROUPS],
        "truncated": len(campaigns) > MAX_MAPPINGS
        or len(materials) > MAX_MAPPINGS
        or len(rows) > MAX_GROUPS,
        "conflicts": conflicts,
        "source": "execution_identity_or_authenticated_producer",
    }


def build_attribution_pack(
    session: Session, *, context, optimizer_id, version, cutoff, account_windows
):
    from app.modules.ai_workbench.review_maturity import maturity_evidence

    mapping = resolve_mappings(
        session,
        context=context,
        optimizer_id=optimizer_id,
        version=version,
        cutoff=cutoff,
    )
    windows = {
        row["advertiser_id"]: row
        for row in account_windows
        if row["status"] == "AVAILABLE"
    }
    result = []
    for row in mapping["rows"]:
        account = windows.get(row["advertiser_id"])
        if account is None:
            continue
        reports = []
        dimension = "campaign" if row["kind"] == "strategy" else "material"
        for days in (0, 3, 7):
            horizon = f"d{days}"
            end = date.fromisoformat(account["mature_through"][horizon])
            window = {
                "start_date": (end - timedelta(days=6)).isoformat(),
                "end_date": end.isoformat(),
                "timezone": account["timezone"],
                "maturity_days": days,
            }
            filters = ReportingFilter(
                dimension=dimension,
                start_date=window["start_date"],
                end_date=end,
                advertiser_ids=(row["advertiser_id"],),
                ids=tuple(row["remote_ids"]),
            )
            try:
                with session.begin_nested():
                    report = freeze_review_page(
                        session,
                        context=context,
                        bc_id=version.bc_id,
                        filters=filters,
                        limit=50,
                        cutoff=cutoff,
                    )
            except HTTPException as error:
                if error.status_code != 409:
                    raise
                report: dict[str, Any] = {
                    "status": "UNAVAILABLE",
                    "reason": str(error.detail),
                    "rows": [],
                }
            # 通用报表的 ids 支持自身/上级匹配；关联归因必须只接受精确目标身份。
            expected = set(row["remote_ids"])
            exact = [
                item
                for item in report["rows"]
                if (
                    item.get("display", {}).get("main_material_id") in expected
                    if dimension == "material"
                    else any(
                        ref.get("remote_id") in expected
                        and ref.get("kind") == "campaign"
                        for ref in item.get("refs", [])
                    )
                )
            ]
            if len(exact) != len(report["rows"]):
                report: dict[str, Any] = {
                    **report,
                    "status": "UNAVAILABLE",
                    "reason": "non_exact_identity_filter",
                    "rows": [],
                    "summary": {},
                }
            reports.append(
                {
                    **report,
                    "horizon": horizon,
                    "window": window,
                    "maturity_evidence": maturity_evidence(
                        session,
                        context=context,
                        advertiser_id=row["advertiser_id"],
                        dimension=dimension,
                        horizon=horizon,
                        window=window,
                        report=report,
                        cutoff=cutoff,
                    ),
                }
            )
        result.append(
            {
                **row,
                "reports": reports,
                "interpretation": "Observational object association, not causal lift. Manual edits and other uses of the same video may affect performance.",
            }
        )
    return {
        **mapping,
        "rows": result,
        "unmapped_policy": "Manual/external ads remain in account/rule facts; absent execution or producer lineage is never assigned by drama title. Missing producer origin means no production-strategy claim.",
        "causal_incremental_profit": None,
    }
