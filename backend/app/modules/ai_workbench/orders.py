"""剧单素材订单：冻结请求、公司级凭据、同键重试及逐剧交付证据。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
from sqlalchemy import (
    func,
)
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.credentials import decrypt_credentials
from app.core.errors import DomainError
from app.jobs.tasks import register_dispatch_task
from app.modules.ai_workbench.models import AIOptimizer, AIOptimizerVersion
from app.modules.ai_workbench.order_models import MaterialOrder
from app.modules.ai_workbench.plans import AIDailyPlanRow
from app.modules.material_production.client import ORIGIN
from app.modules.material_production.models import MaterialProductionBinding
from app.modules.tenants.permissions import require_tenant

# 素材工作台的默认方案由平台维护，投手配置不再暴露策略 ID 或个人表格。
BUILTIN_STRATEGIES = ("CL6-v4", "CL8-v5", "GM-精剪-v4")

register_dispatch_task("ai_workbench.material_order", "control")


class ProductionClient:
    def __init__(self, key: str, *, transport: httpx.BaseTransport | None = None):
        self.key = key
        self.transport = transport

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        headers = {"Authorization": f"Bearer {self.key}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            with httpx.Client(
                transport=self.transport,
                timeout=httpx.Timeout(15, connect=5),
                follow_redirects=False,
            ) as client:
                response = client.request(
                    method, ORIGIN + path, headers=headers, json=body
                )
            if response.status_code == 429 or response.status_code >= 500:
                raise DomainError(
                    "ai_material_order_unknown", "素材工具暂不可用", retryable=True
                )
            if not response.is_success:
                raise DomainError(
                    "ai_material_order_failed", "素材工具拒绝订单，请检查配置"
                )
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError
            return result
        except httpx.HTTPError, ValueError:
            raise DomainError(
                "ai_material_order_unknown",
                "素材订单结果未知，保留原幂等键恢复",
                retryable=True,
            ) from None


def company_client(session: Session, *, context: TenantContext) -> ProductionClient:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="upload"
    )
    binding = session.get(MaterialProductionBinding, context.tenant_id)
    if not binding or binding.status != "ready" or not binding.api_key_ciphertext:
        raise DomainError("material_production_not_ready", "素材制作尚未开通")
    key = decrypt_credentials(
        tenant_id=context.tenant_id, ciphertext=binding.api_key_ciphertext
    )["api_key"]
    return ProductionClient(key)


def validate_config(config: dict) -> None:
    if not config:
        return
    ids = config.get("strategy_ids", [])
    if (
        not isinstance(ids, list)
        or not ids
        or len(ids) > 20
        or any(not isinstance(value, str) or not value.strip() for value in ids)
        or len(ids) != len(set(ids))
        or not isinstance(config.get("feishu_token"), str)
        or not config["feishu_token"].strip()
        or len(config["feishu_token"]) > 8192
        or any(len(value) > 255 for value in ids)
        or set(config) - {"strategy_ids", "feishu_token"}
    ):
        raise DomainError("ai_material_config_invalid", "素材方案必须使用平台内置配置")


def resolve_builtin_config(remote: ProductionClient) -> dict:
    """按语义名称从当前租户目录解析内置方案，避免跨租户复制 UUID。"""
    catalogue = remote.request("GET", "/api/strategy-catalog").get("strategies", [])
    names = {
        str(item.get("name", "")).strip(): item.get("id")
        for item in catalogue
        if isinstance(item, dict) and item.get("enabled") is True
    }
    selected = [names[name] for name in BUILTIN_STRATEGIES if names.get(name)]
    if len(selected) != len(BUILTIN_STRATEGIES):
        raise DomainError("ai_material_order_failed", "当前租户未启用内置素材方案")
    return {"strategy_ids": selected}


def create_order(
    session: Session, *, context: TenantContext, row_id: UUID, config: dict
) -> MaterialOrder:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="upload"
    )
    row = session.exec(
        select(AIDailyPlanRow)
        .where(
            col(AIDailyPlanRow.tenant_id) == context.tenant_id,
            col(AIDailyPlanRow.id) == row_id,
        )
        .with_for_update()
    ).one_or_none()
    if row is None or not row.order_materials:
        raise DomainError("ai_daily_plan_row_not_found", "未找到素材下单排期")
    prior = session.exec(
        select(MaterialOrder).where(
            col(MaterialOrder.tenant_id) == context.tenant_id,
            col(MaterialOrder.plan_row_id) == row_id,
        )
    ).first()
    if prior:
        return prior
    validate_config(config)
    # 提交时间及请求体只生成一次；超时重试绝不重算 startTime。
    now = datetime.now(UTC)
    order = MaterialOrder(
        tenant_id=context.tenant_id,
        optimizer_id=row.optimizer_id,
        plan_row_id=row.id,
        actor_id=context.actor_id,
        title=row.title,
        request_body={
            "platformAccountIds": [],
            "payload": {
                "workflowId": "material_library_production",
                "title": row.title,
                "titles": [row.title],
                "titleSource": "manual",
                "businessDate": str(row.business_date),
                "targets": ["self_download"],
                "selectedStrategyIds": list(config.get("strategy_ids", [])),
                "createdFrom": "unified-new-webui",
                "startTime": now.isoformat(),
                "intervalMinutes": 0,
                "note": f"TK-ADA AI optimizer {row.optimizer_id}",
            },
        },
    )
    session.add(order)
    session.flush()
    return order


def process_order(
    session: Session,
    *,
    context: TenantContext,
    order_id: UUID,
    remote: ProductionClient | None = None,
) -> MaterialOrder:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="upload"
    )
    order = session.exec(
        select(MaterialOrder)
        .where(
            col(MaterialOrder.tenant_id) == context.tenant_id,
            col(MaterialOrder.id) == order_id,
        )
        .with_for_update()
    ).one_or_none()
    if order is None:
        raise DomainError("ai_daily_plan_row_not_found", "素材订单不存在")
    if order.status in {"COMPLETE", "FAILED"}:
        return order
    optimizer = session.get(AIOptimizer, order.optimizer_id)
    if not optimizer or not optimizer.active:
        return order
    order.attempts += 1
    try:
        remote = remote or company_client(session, context=context)
        if not order.job_id:
            catalog = remote.request("GET", "/api/strategy-catalog").get(
                "strategies", []
            )
            if not order.request_body["payload"]["selectedStrategyIds"]:
                resolved = resolve_builtin_config(remote)
                order.request_body = {
                    **order.request_body,
                    "payload": {
                        **order.request_body["payload"],
                        "selectedStrategyIds": resolved["strategy_ids"],
                    },
                }
            enabled = {
                item["id"]
                for item in catalog
                if isinstance(item, dict)
                and item.get("enabled") is True
                and isinstance(item.get("id"), str)
            }
            if not set(order.request_body["payload"]["selectedStrategyIds"]).issubset(
                enabled
            ):
                raise DomainError("ai_material_order_failed", "素材策略不可用")
            capabilities = remote.request(
                "GET", "/api/material-library-production/capabilities"
            )
            if capabilities.get("executionMode") not in {
                "episode_range_legacy",
                "full_series_v2",
            }:
                raise DomainError("ai_material_order_failed", "素材库能力不受支持")
            # 两种能力均允许省略集数；整剧模式绝不携带旧集数字段。
            result = remote.request(
                "POST",
                "/api/schedules",
                body=order.request_body,
                idempotency_key=f"tkada-ai:{order.id}",
            )
            job = result.get("job", {})
            if not isinstance(job.get("id"), str) or not 1 <= len(job["id"]) <= 255:
                raise DomainError(
                    "ai_material_order_unknown", "素材订单回执缺失", retryable=True
                )
            order.job_id = job["id"]
            order.status = "SUBMITTED"
            order.evidence = {
                "job_id": order.job_id,
                "status": job.get("status", "queued"),
            }
        else:
            from urllib.parse import quote

            job = remote.request(
                "GET", "/api/jobs/" + quote(order.job_id, safe="")
            ).get("job", {})
            if job.get("id") != order.job_id:
                raise DomainError(
                    "ai_material_order_unknown", "素材订单回执不匹配", retryable=True
                )
            items = job.get("progressItems", [])
            if not isinstance(items, list) or any(
                not isinstance(item, dict) for item in items
            ):
                raise DomainError(
                    "ai_material_order_unknown", "素材订单进度无效", retryable=True
                )
            # 必须包含当前剧且所有分项终态；不能将空数组或 stage 当作已交付。
            relevant = [item for item in items if item.get("title") == order.title]
            done = bool(relevant) and all(
                item.get("status") in {"succeeded", "failed"} for item in relevant
            )
            order.status = (
                (
                    "COMPLETE"
                    if all(item.get("status") == "succeeded" for item in relevant)
                    else "FAILED"
                )
                if done
                else "SUBMITTED"
            )
            order.evidence = {
                "job_id": order.job_id,
                "status": job.get("status"),
                "items": [
                    {
                        "title": item.get("title"),
                        "status": item.get("status"),
                        "delivery": item.get("delivery"),
                    }
                    for item in relevant
                ],
            }
        order.error_code = None
    except DomainError as error:
        order.status = (
            "PENDING"
            if error.code == "material_production_not_ready"
            else "UNKNOWN"
            if error.retryable
            else "FAILED"
        )
        order.error_code = error.code
    order.updated_at = datetime.now(UTC)
    order.next_attempt_at = order.updated_at + timedelta(
        seconds=min(1800, 30 * 2 ** min(order.attempts, 6))
    )
    session.add(order)
    session.flush()
    return order


def scan_orders(session: Session, *, now: datetime, limit: int = 100) -> int:
    from zoneinfo import ZoneInfo

    from app.jobs.outbox import enqueue_after_commit

    missing = (
        ~select(col(MaterialOrder.id))
        .where(
            col(MaterialOrder.tenant_id) == col(AIDailyPlanRow.tenant_id),
            col(MaterialOrder.plan_row_id) == col(AIDailyPlanRow.id),
        )
        .exists()
    )
    rows = session.exec(
        select(AIDailyPlanRow)
        .join(
            AIOptimizer,
            (col(AIOptimizer.tenant_id) == col(AIDailyPlanRow.tenant_id))
            & (col(AIOptimizer.id) == col(AIDailyPlanRow.optimizer_id)),
        )
        .join(
            AIOptimizerVersion,
            (col(AIOptimizerVersion.tenant_id) == col(AIOptimizer.tenant_id))
            & (col(AIOptimizerVersion.optimizer_id) == col(AIOptimizer.id))
            & (col(AIOptimizerVersion.number) == col(AIOptimizer.latest_version)),
        )
        .where(
            col(AIDailyPlanRow.order_materials).is_(True),
            missing,
            col(AIOptimizer.active).is_(True),
            col(AIDailyPlanRow.business_date)
            >= func.date(func.timezone(col(AIDailyPlanRow.timezone), now)),
        )
        .order_by(col(AIDailyPlanRow.business_date), col(AIDailyPlanRow.position))
        .limit(limit)
        .with_for_update(skip_locked=True, of=AIDailyPlanRow)
    ).all()
    for row in rows:
        optimizer = session.get(AIOptimizer, row.optimizer_id)
        if (
            not optimizer
            or not optimizer.active
            or row.business_date < now.astimezone(ZoneInfo(row.timezone)).date()
        ):
            continue
        version = session.exec(
            select(AIOptimizerVersion).where(
                col(AIOptimizerVersion.tenant_id) == row.tenant_id,
                col(AIOptimizerVersion.optimizer_id) == row.optimizer_id,
                col(AIOptimizerVersion.number) == optimizer.latest_version,
            )
        ).one()
        try:
            with session.begin_nested():
                create_order(
                    session,
                    context=TenantContext(
                        tenant_id=row.tenant_id, actor_id=optimizer.created_by, role=""
                    ),
                    row_id=row.id,
                    config=version.material_config or {},
                )
        except DomainError:
            continue
    due = session.exec(
        select(MaterialOrder)
        .join(
            AIOptimizer,
            (col(AIOptimizer.tenant_id) == col(MaterialOrder.tenant_id))
            & (col(AIOptimizer.id) == col(MaterialOrder.optimizer_id)),
        )
        .where(
            col(AIOptimizer.active).is_(True),
            col(MaterialOrder.status).in_(["PENDING", "UNKNOWN", "SUBMITTED"]),
            col(MaterialOrder.next_attempt_at) <= now,
        )
        .order_by(col(MaterialOrder.next_attempt_at))
        .limit(limit)
        .with_for_update(skip_locked=True, of=MaterialOrder)
    ).all()
    for order in due:
        order.dispatch_generation += 1
        order.next_attempt_at = now + timedelta(minutes=2)
        enqueue_after_commit(
            session,
            context=TenantContext(
                tenant_id=order.tenant_id, actor_id=order.actor_id, role=""
            ),
            task_name="ai_workbench.material_order",
            task_key=f"ai-material:{order.id}:{order.dispatch_generation}",
            payload={"order_id": str(order.id)},
        )
        session.add(order)
    return len(due)
