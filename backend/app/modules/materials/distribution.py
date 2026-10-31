"""Submission-only target preparation with independently scoped source and target."""

import logging
from contextlib import AbstractContextManager, nullcontext
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.bounded_resources import bounded_session
from app.integrations.tiktok.contracts import materials as material_types
from app.integrations.tiktok.contracts.common import RemoteCallError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.integrations.tiktok.material_upload_evidence import material_upload_policy
from app.integrations.tiktok.sdk import SDK_SCOPE_INTERRUPTS
from app.jobs.admission import admission_policy
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_many_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.accounts.access import resolve_account_access
from app.modules.accounts.routing import freeze_route
from app.modules.accounts.schemas import AccountAccess
from app.modules.tenants.permissions import require_tenant

from . import sdk_assets as api
from .batch_validation import BatchSourceVerifier
from .channel_policy import require_url_upload
from .distribution_sources import distribution_sources
from .models import (
    AccountMaterial,
    MaterialAssetOperation,
    MaterialDistribution,
    MaterialFile,
)
from .readiness import (
    get_material_readiness,
    get_material_readiness_batch,
    load_material,
    mapping_fresh,
    require_execution_config,
    require_upload_path,
    target_mapping,
)
from .remote_sources import (
    read_frozen_remote_source,
    require_remote_material,
    resolve_remote_source,
)
from .response_archive import material_response_observer
from .routes import (
    load_material_route,
    require_material_route,
    require_same_route,
)
from .schemas import AssetPreparation, MaterialReadiness
from .sharing import distribution_transport
from .source_uploads import (
    READ_CLAIM_SECONDS,
    READ_HARD_LIMIT,
    UNRESOLVED,
    UPLOAD_CLAIM_SECONDS,
    UPLOAD_HARD_LIMIT,
    _attempt,
    _locked_material,
    _locked_operation,
    remote_name,
    reserve_asset_operation,
)
from .storage import OriginalFile, open_original

register_dispatch_task("materials.prepare_target", "resources")
register_dispatch_task("materials.verify_target", "resources")
logger = logging.getLogger(__name__)
ACTIVE_DISTRIBUTIONS = ("queued", "preparing", "verifying", "result_unknown")
RECONCILIATION_MAX_CLAIMS = 120
RECONCILIATION_SECONDS = 15 * 60
RECONCILIATION_MAX_NO_PROGRESS = 3
RECONCILIATION_LOCAL_ERRORS = frozenset(
    {
        "tiktok_local_resources_unavailable",
        "admission_unavailable",
        "tiktok_call_deadline_exceeded",
        "material_deadline",
    }
)
RECONCILIATION_BLOCKING_ERRORS = frozenset(
    {
        "action_forbidden",
        "tenant_forbidden",
        "tenant_unavailable",
        "account_access_denied",
        "account_not_in_bc",
        "account_authorization_changed",
        "connection_unavailable",
        "credential_invalid",
        "route_authorization_changed",
        "frozen_route_changed",
        "material_reconciliation_ambiguous",
    }
)


def _reconciliation_retry_at(operation: MaterialAssetOperation) -> datetime | None:
    value = operation.remote_response.get("reconciliation_retry_at")
    return datetime.fromisoformat(value) if value else None


def _automatic_reconciliation(operation: MaterialAssetOperation) -> bool:
    return (
        operation.remote_response.get("transport") == "native_share"
        and operation.remote_response.get("media_type", "VIDEO") == "VIDEO"
        and operation.remote_response.get("share_acknowledged") is True
        and not operation.remote_response.get("conflicting_video_id")
        and operation.remote_response.get("error_code")
        not in RECONCILIATION_BLOCKING_ERRORS
    )


def _defer_local_reconciliation(operation: MaterialAssetOperation, code: str) -> None:
    # 本地资源/准入等待不等于完成一次平台核查，不消耗远端次数和无进展预算。
    # 保留此前真实核查次数及无进展计数，下一条原代数消息自动退避续读。
    now = datetime.now(UTC)
    budget = operation.remote_response["reconciliation_budget"]
    operation.remote_response = {
        **operation.remote_response,
        "last_reconciliation_error": code,
        "reconciliation_retry_at": (now + timedelta(seconds=30)).isoformat(),
        "reconciliation_budget": {
            **budget,
            "claims": max(0, budget["claims"] - 1),
            "started_at": now.isoformat(),
        },
    }


def _reconciliation_exhausted(operation: MaterialAssetOperation) -> bool:
    budget = operation.remote_response.get("reconciliation_budget")
    if not budget:
        return False
    return (
        budget["claims"] >= RECONCILIATION_MAX_CLAIMS
        or budget["no_progress"] >= RECONCILIATION_MAX_NO_PROGRESS
        or datetime.now(UTC) - datetime.fromisoformat(budget["started_at"])
        >= timedelta(seconds=RECONCILIATION_SECONDS)
    )


def _stop_reconciliation(
    dist: MaterialDistribution, operation: MaterialAssetOperation
) -> None:
    if _automatic_reconciliation(operation):
        # 共享已 ACK 的目标确认只有读取副作用。短轮耗尽后降低频率继续确认，
        # 绝不重发共享、复制源 VID 或要求人工点击才能继续。
        rounds = operation.remote_response.get("reconciliation_round", 0) + 1
        retry_at = datetime.now(UTC) + timedelta(minutes=min(30, 5 * rounds))
        operation.remote_response = {
            **operation.remote_response,
            "last_reconciliation_budget": operation.remote_response.get(
                "reconciliation_budget"
            ),
            "last_reconciliation_error": operation.remote_response.get("error_code")
            or "material_result_pending",
            "reconciliation_round": rounds,
            "reconciliation_retry_at": retry_at.isoformat(),
            "reconciliation_budget": {
                "started_at": retry_at.isoformat(),
                "claims": 0,
                "no_progress": 0,
            },
            "reconciliation_stopped": False,
            "reconciliation_complete": False,
            "revision": operation.remote_response.get("revision", 0) + 1,
        }
        dist.status = operation.status = "verifying"
        dist.reason_code = "material_result_pending"
        operation.attempt_token, operation.claimed_until = None, None
        return
    # 停止自动读取不是未上传证明；原请求、回执及内容身份全部保留。
    operation.remote_response = {
        **operation.remote_response,
        "reconciliation_stopped": True,
        "error_code": "material_reconciliation_budget_exhausted",
    }
    dist.status = operation.status = "result_unknown"
    dist.reason_code = "material_reconciliation_budget_exhausted"
    operation.attempt_token, operation.claimed_until = None, None


def _claim_reconciliation(operation: MaterialAssetOperation) -> None:
    budget: dict[str, Any] = operation.remote_response.get("reconciliation_budget") or {
        "started_at": datetime.now(UTC).isoformat(),
        "claims": 0,
        "no_progress": 0,
    }
    operation.remote_response = {
        **operation.remote_response,
        "reconciliation_budget": {**budget, "claims": budget["claims"] + 1},
    }


def _reconciliation_progress(
    operation: MaterialAssetOperation, *, progress: bool
) -> None:
    budget = operation.remote_response["reconciliation_budget"]
    response = {
        **operation.remote_response,
        "reconciliation_budget": {
            **budget,
            "no_progress": 0 if progress else budget["no_progress"] + 1,
        },
    }
    if progress:
        response.pop("error_code", None)
        response.pop("reconciliation_retry_at", None)
        response.pop("reconciliation_round", None)
    operation.remote_response = response


def _context(dist: MaterialDistribution) -> TenantContext:
    return TenantContext(
        tenant_id=dist.tenant_id, actor_id=dist.actor_id, role="operator"
    )


def _target_dispatch(
    dist: MaterialDistribution,
    operation: MaterialAssetOperation,
    *,
    kind: str,
    claim_id: UUID | None = None,
    observe: bool = False,
    read_only: bool = False,
) -> tuple[str, dict[str, Any]] | None:
    """目标素材消息的身份与正文；返回 None 表示该操作不应再安排消息。"""
    # 显式接替后旧未知只保留历史，不再安排核实或准备消息。
    if dist.superseded_by_id is not None or operation.superseded_by_id is not None:
        return None
    if read_only and kind != "verify":
        raise DomainError("invalid_asset_task", "只读核实不能安排上传")
    if (
        kind == "verify"
        and operation.remote_response.get("reconciliation_complete")
        and _automatic_reconciliation(operation)
    ):
        _stop_reconciliation(dist, operation)
    if operation.remote_response.get(
        "reconciliation_complete"
    ) or operation.remote_response.get("reconciliation_stopped"):
        if not (observe and read_only):
            return None
        # 完整空查/预算耗尽不是未发送证明。仅显式只读核查重开已结束轮次；
        # 进行中的重复点击不刷新预算，自动补偿和旧消息不能续命或重新上传。
        operation.remote_response = {
            **operation.remote_response,
            "reconciliation_complete": False,
            "reconciliation_stopped": False,
            "search_page": 1,
            "search_total": None,
            "candidates": [],
            "revision": operation.remote_response.get("revision", 0) + 1,
        }
        operation.remote_response = {
            key: value
            for key, value in operation.remote_response.items()
            if key not in {"reconciliation_budget", "error_code"}
        }
    elif observe and read_only and operation.status == "succeeded":
        # 成功回执的再次复核沿用既有消息身份；旧轮预算不能阻止新的显式读取。
        operation.remote_response = {
            key: value
            for key, value in operation.remote_response.items()
            if key not in {"reconciliation_budget", "error_code"}
        }
    if (
        kind == "verify"
        and operation.attempt_token is None
        and _reconciliation_exhausted(operation)
    ):
        _stop_reconciliation(dist, operation)
        if operation.remote_response.get("reconciliation_stopped"):
            return None
    payload: dict[str, Any] = {
        "distribution_id": str(dist.id),
        "operation_id": str(operation.id),
    }
    if observe:
        payload["observe"] = True
        key = f"material-target-observe:{dist.id}"
    elif claim_id:
        payload["claim_id"] = str(claim_id)
        key = f"material-target-recover:{dist.id}:{claim_id}"
    else:
        revision = operation.remote_response.get("revision", 0)
        payload["revision"] = revision
        key = f"material-target:{dist.id}:{operation.id}:{revision}:{kind}"
    if read_only:
        payload["read_only"] = True
        key += f":read:{operation.id}"
        if observe:
            payload["revision"] = operation.remote_response.get("revision", 0)
            key += f":{payload['revision']}"
    return key, payload


def queue_distribution(
    session: Session,
    dist: MaterialDistribution,
    operation: MaterialAssetOperation,
    *,
    kind: str,
    due: datetime | None = None,
    claim_id: UUID | None = None,
    observe: bool = False,
    read_only: bool = False,
    assume_new: bool = False,
) -> None:
    identity = _target_dispatch(
        dist,
        operation,
        kind=kind,
        claim_id=claim_id,
        observe=observe,
        read_only=read_only,
    )
    if identity is None:
        return
    key, payload = identity
    retry_at = _reconciliation_retry_at(operation)
    if retry_at is not None:
        due = max(due or datetime.now(UTC), retry_at)
    existing = None
    if not assume_new:
        existing = session.exec(
            select(PendingDispatch)
            .where(
                PendingDispatch.tenant_id == dist.tenant_id,
                PendingDispatch.task_key == key,
            )
            .with_for_update()
        ).first()
    if existing:
        if (existing.actor_id, existing.task_name, existing.payload) != (
            dist.actor_id,
            f"materials.{kind}_target",
            payload,
        ):
            raise DomainError("dispatch_payload_invalid", "素材核实投递身份不匹配")
        # Reuse one observation record; never invalidate an unpublished message
        # or reset broker backoff while waiting for a source-owned operation.
        if observe and existing.published_at is not None:
            existing.published_at = None
            existing.available_at = due or datetime.now(UTC)
        return
    enqueue_many_after_commit(
        session,
        context=_context(dist),
        entries=[(f"materials.{kind}_target", key, payload, due)],
    )


def queue_claimed_distributions(
    session: Session,
    rows: list[tuple[MaterialDistribution, MaterialAssetOperation]],
    *,
    kind: str,
    due: datetime,
) -> None:
    """批量领取后为每个成员登记各自领取代次的接续消息，一次写入 outbox。

    各成员领取代次（attempt_token）刚生成，消息身份必然是新的；与逐条
    queue_distribution(claim_id=..., assume_new=True) 语义相同。
    """
    entries: list[tuple[str, str, dict[str, Any], datetime | None]] = []
    for dist, operation in rows:
        identity = _target_dispatch(
            dist, operation, kind=kind, claim_id=operation.attempt_token
        )
        if identity is not None:
            entries.append((f"materials.{kind}_target", *identity, due))
    if not entries:
        return
    contexts = {(dist.tenant_id, dist.actor_id) for dist, _ in rows}
    if len(contexts) != 1:
        raise DomainError("material_claim_changed", "共享批次成员范围已改变")
    enqueue_many_after_commit(session, context=_context(rows[0][0]), entries=entries)


def _bind_operation(
    session: Session,
    *,
    context: TenantContext,
    material_id: UUID,
    bc_id: str,
    advertiser_id: str,
    path: str,
    route: FrozenTikTokRoute,
    source: AccountMaterial | None = None,
) -> MaterialAssetOperation:
    operation = reserve_asset_operation(
        session,
        context=context,
        material_id=material_id,
        advertiser_id=advertiser_id,
        path="share_source" if path == "share_source" else "upload_original",
        action="build",
        route=route,
    )
    if _attempt(session, operation.id) is None and operation.status == "pending":
        if (
            path == "share_source"
            and operation.path == "share_source"
            and not operation.remote_response
        ):
            material = load_material(
                session, context=context, bc_id=bc_id, material_id=material_id
            )
            source = source or distribution_sources(
                session,
                context=context,
                materials=[material],
                advertiser_id=advertiser_id,
                route=route,
            ).get(material_id)
            if source is None:
                raise DomainError(
                    "material_remote_source_unavailable", "请恢复来源授权或补传原件"
                )
            operation.remote_response = {
                "transport": distribution_transport(
                    source_bc_id=source.bc_id, target_bc_id=bc_id
                ),
                "source_bc_id": source.bc_id,
                "source_material_id": str(source.material_id),
                "source_asset_id": str(source.id),
                "source_advertiser_id": source.advertiser_id,
                "source_connection_id": str(source.connection_id),
                "source_video_id": source.video_id,
                "remote_name": material.file_name,
                "content_md5": material.video_md5,
            }
            operation.request_digest = sha256(
                f"{operation.request_digest}:{source.id}:{source.video_id}:{material.video_md5}:{operation.id}".encode()
            ).hexdigest()
        mapping = target_mapping(
            session,
            context=context,
            bc_id=bc_id,
            material_id=material_id,
            advertiser_id=advertiser_id,
        )
        if path == "existing_target" and mapping and (mapping.video_id or "").strip():
            operation.status = "verifying"
            operation.remote_response = {
                **operation.remote_response,
                "video_id": mapping.video_id,
                "read_only": True,
                "verification_connection_id": str(mapping.connection_id),
            }
            if mapping.mid:
                operation.remote_response = {
                    **operation.remote_response,
                    "mid": mapping.mid,
                }
    return operation


def ensure_target_asset(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    material_id: UUID,
    advertiser_id: str,
    task_key: str,
    route: FrozenTikTokRoute,
    _seed_owner: bool = False,
) -> AssetPreparation:
    """Internal build-submission boundary. Caller commits; no browser write route."""
    if not isinstance(task_key, str) or not task_key.strip() or len(task_key) > 255:
        raise DomainError("invalid_asset_task", "搭建素材步骤标识无效")
    require_material_route(
        session,
        context=context,
        route=route,
        bc_id=bc_id,
        advertiser_id=advertiser_id,
        capability="build",
    )
    # The file lock serializes both two build consumers and source reservation.
    material = load_material(
        session, context=context, bc_id=bc_id, material_id=material_id, lock=True
    )
    readiness = get_material_readiness(
        session,
        context=context,
        bc_id=bc_id,
        material_id=material_id,
        advertiser_id=advertiser_id,
        route=route,
    )
    return _ensure_target_asset(
        session,
        context=context,
        bc_id=bc_id,
        material=material,
        advertiser_id=advertiser_id,
        route=route,
        readiness=readiness,
        seed_owner=_seed_owner,
    )


def ensure_target_assets(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    material_ids: list[UUID],
    advertiser_id: str,
    route: FrozenTikTokRoute,
) -> dict[UUID, AssetPreparation]:
    """同一短事务准备一个账户的至多20条素材，复用单条准备实现与批读权限。"""
    if not 1 <= len(material_ids) <= 20 or len(set(material_ids)) != len(material_ids):
        raise ValueError("invalid material preparation slice")
    require_material_route(
        session,
        context=context,
        route=route,
        bc_id=bc_id,
        advertiser_id=advertiser_id,
        capability="build",
    )
    materials = session.exec(
        select(MaterialFile)
        .where(
            MaterialFile.tenant_id == context.tenant_id,
            col(MaterialFile.id).in_(material_ids),
        )
        .order_by(col(MaterialFile.id))
        .with_for_update()
    ).all()
    if len(materials) != len(material_ids):
        raise DomainError("material_not_found", "素材切片范围不完整")
    readiness = get_material_readiness_batch(
        session,
        context=context,
        bc_id=bc_id,
        material_ids=material_ids,
        advertiser_id=advertiser_id,
        route=route,
    )
    return {
        material.id: _ensure_target_asset(
            session,
            context=context,
            bc_id=bc_id,
            material=material,
            advertiser_id=advertiser_id,
            route=route,
            readiness=readiness[material.id],
            seed_owner=False,
        )
        for material in materials
    }


def _ensure_target_asset(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    material: MaterialFile,
    advertiser_id: str,
    route: FrozenTikTokRoute,
    readiness: MaterialReadiness,
    seed_owner: bool,
) -> AssetPreparation:
    if material.media_type == "IMAGE":
        from .image_distribution import prepare_image_targets
        from .repository import asset_public

        result = prepare_image_targets(
            route, (material.id,), (advertiser_id,), session=session, context=context
        ).items[0]
        if result.state == "ready":
            image_mapping = session.get(AccountMaterial, result.asset_id)
            assert image_mapping is not None
            return AssetPreparation(state="ready", mapping=asset_public(image_mapping))
        return AssetPreparation(
            state="blocked" if result.state == "blocked" else "queued",
            reason_code="image_target_unavailable"
            if result.state == "blocked"
            else None,
            task_id=result.task_id,
        )
    # 两个入口均已在当前事务核验权限并锁定素材；后续选源/去重/seed只有一份。
    material_id = material.id
    if (
        readiness.path == "existing_target"
        and readiness.mapping is not None
        and readiness.mapping.material_id != material_id
    ):
        from .repository import asset_public

        # 同内容别名仅建立消费引用；实际 VID、图片和验证时间来自同目标账户。
        actual = session.get(AccountMaterial, readiness.mapping.asset_id)
        assert actual
        values = actual.model_dump()
        values.update(id=uuid4(), material_id=material_id)
        alias = AccountMaterial(**values)
        session.add(alias)
        session.flush()
        if mapping_fresh(alias):
            return AssetPreparation(state="ready", mapping=asset_public(alias))
    if readiness.state == "ready":
        return AssetPreparation(state="ready", mapping=readiness.mapping)
    if readiness.state == "blocked":
        return AssetPreparation(
            state="blocked",
            reason_code=readiness.reason_code,
            reason_message=readiness.reason_message,
        )
    existing = session.exec(
        select(MaterialDistribution)
        .where(
            MaterialDistribution.tenant_id == context.tenant_id,
            MaterialDistribution.bc_id == bc_id,
            MaterialDistribution.material_id == material_id,
            MaterialDistribution.advertiser_id == advertiser_id,
            col(MaterialDistribution.superseded_by_id).is_(None),
            col(MaterialDistribution.status).in_(ACTIVE_DISTRIBUTIONS),
        )
        .with_for_update()
    ).first()
    if existing:
        require_same_route(
            load_material_route(existing.target_route, context=context, bc_id=bc_id),
            route,
        )
        return AssetPreparation(state="queued", task_id=existing.id)
    if not seed_owner and readiness.path == "share_source":
        candidate = distribution_sources(
            session,
            context=context,
            materials=[material],
            advertiser_id=advertiser_id,
            route=route,
        ).get(material_id)
        if candidate is not None and candidate.bc_id != bc_id:
            from .bc_seeding import ensure_bc_seed

            seed = ensure_bc_seed(
                session, context=context, material=material, route=route
            )
            if seed.advertiser_id == advertiser_id and seed.material_id == material_id:
                return AssetPreparation(state="queued", task_id=seed.distribution_id)
            operation = reserve_asset_operation(
                session,
                context=context,
                material_id=material_id,
                advertiser_id=advertiser_id,
                path="share_source",
                action="build",
                route=route,
            )
            dist = MaterialDistribution(
                tenant_id=context.tenant_id,
                bc_id=bc_id,
                material_id=material_id,
                advertiser_id=advertiser_id,
                actor_id=context.actor_id,
                operation_id=operation.id,
                seed_id=seed.id,
                source_bc_id=bc_id,
                source_material_id=seed.material_id,
                source_route=route.model_dump(mode="json"),
                target_route=route.model_dump(mode="json"),
                path="share_source",
            )
            session.add(dist)
            session.flush()
            queue_distribution(session, dist, operation, kind="prepare")
            return AssetPreparation(state="queued", task_id=dist.id)
    operation = _bind_operation(
        session,
        context=context,
        material_id=material_id,
        bc_id=bc_id,
        advertiser_id=advertiser_id,
        path=readiness.path,
        route=route,
    )
    source_route = None
    source_asset_id = operation.remote_response.get("source_asset_id")
    if source_asset_id:
        source = resolve_remote_source(
            session,
            context=context,
            bc_id=operation.remote_response["source_bc_id"],
            material_id=UUID(operation.remote_response["source_material_id"]),
            source_asset_id=UUID(source_asset_id),
        )
        if source is None:
            raise DomainError(
                "material_remote_source_unavailable", "来源证据或权限已改变"
            )
        previous_dependency = session.exec(
            select(MaterialDistribution)
            .where(
                MaterialDistribution.tenant_id == context.tenant_id,
                MaterialDistribution.operation_id == operation.id,
            )
            .order_by(col(MaterialDistribution.id))
            .limit(1)
        ).first()
        # 同一操作的后续消费者继承原来源依赖，不能借新 distribution 刷新旧授权版本。
        source_route = (
            load_material_route(
                previous_dependency.source_route,
                context=context,
                bc_id=source.bc_id,
                connection_id=source.connection_id,
            )
            if previous_dependency
            else freeze_route(
                session,
                context=context,
                bc_id=source.bc_id,
                connection_id=source.connection_id,
            )
        )
        require_material_route(
            session,
            context=context,
            route=source_route,
            bc_id=source.bc_id,
            advertiser_id=source.advertiser_id,
            capability="read",
        )
    dist = MaterialDistribution(
        tenant_id=context.tenant_id,
        bc_id=bc_id,
        material_id=material_id,
        advertiser_id=advertiser_id,
        actor_id=context.actor_id,
        operation_id=operation.id,
        target_route=route.model_dump(mode="json"),
        source_route=source_route.model_dump(mode="json") if source_route else None,
        source_asset_id=UUID(source_asset_id) if source_asset_id else None,
        source_bc_id=operation.remote_response.get("source_bc_id"),
        source_material_id=UUID(operation.remote_response["source_material_id"])
        if source_asset_id
        else None,
        path=operation.path
        if operation.status in {"sending", "result_unknown"}
        else readiness.path,
    )
    session.add(dist)
    session.flush()
    source_owned = _attempt(session, operation.id) is not None
    kind = (
        "verify"
        if source_owned
        or operation.status in {"sending", "result_unknown", "verifying"}
        else "prepare"
    )
    queue_distribution(session, dist, operation, kind=kind, observe=source_owned)
    return AssetPreparation(state="queued", task_id=dist.id)


def _load_distribution(
    session: Session, context: TenantContext, distribution_id: UUID
) -> MaterialDistribution:
    dist = session.exec(
        select(MaterialDistribution)
        .where(
            MaterialDistribution.tenant_id == context.tenant_id,
            MaterialDistribution.id == distribution_id,
        )
        .execution_options(populate_existing=True)
    ).first()
    if dist is None:
        raise DomainError("material_distribution_not_found", "未找到目标素材任务")
    if dist.actor_id != context.actor_id:
        raise DomainError("tenant_forbidden", "任务操作人不匹配")
    return dist


def _delivery_matches(
    operation: MaterialAssetOperation,
    *,
    operation_id: UUID | None,
    revision: int | None,
    recovery_claim_id: UUID | None,
) -> bool:
    return (
        operation.superseded_by_id is None
        and (_reconciliation_retry_at(operation) or datetime.min.replace(tzinfo=UTC))
        <= datetime.now(UTC)
        and not operation.remote_response.get("reconciliation_complete")
        and not operation.remote_response.get("reconciliation_stopped")
        and (operation_id is None or operation.id == operation_id)
        and (
            revision is None or revision == operation.remote_response.get("revision", 0)
        )
        and (recovery_claim_id is None or operation.attempt_token == recovery_claim_id)
    )


def _target_access(
    session: Session,
    context: TenantContext,
    dist: MaterialDistribution,
    *,
    upload: bool,
    connection_id: UUID | None = None,
) -> AccountAccess:
    route = load_material_route(dist.target_route, context=context, bc_id=dist.bc_id)
    if connection_id is not None and connection_id != route.connection_id:
        raise DomainError("frozen_route_changed", "素材请求连接与原任务不一致")
    for action in ("build", "upload") if upload else ("build",):
        require_material_route(
            session,
            context=context,
            route=route,
            bc_id=dist.bc_id,
            advertiser_id=dist.advertiser_id,
            capability=action,
        )
    return resolve_account_access(
        session,
        context=context,
        bc_id=dist.bc_id,
        advertiser_id=dist.advertiser_id,
        action="upload" if upload else "build",
        connection_id=route.connection_id,
    )


def _publish_mapping(
    session: Session,
    context: TenantContext,
    dist: MaterialDistribution,
    evidence: dict[str, str],
    *,
    connection_id: UUID | None = None,
) -> None:
    # 迟到旧回执不能覆盖新代 VID 或清空新封面；响应正文仍独立留档。
    if dist.superseded_by_id is not None:
        return
    access = _target_access(
        session, context, dist, upload=False, connection_id=connection_id
    )
    mapping = target_mapping(
        session,
        context=context,
        bc_id=dist.bc_id,
        material_id=dist.material_id,
        advertiser_id=dist.advertiser_id,
    )
    if mapping is None:
        mapping = AccountMaterial(
            tenant_id=dist.tenant_id,
            bc_id=dist.bc_id,
            material_id=dist.material_id,
            advertiser_id=dist.advertiser_id,
            connection_id=access.connection_id,
            video_id=evidence["video_id"],
        )
        session.add(mapping)
    if mapping.video_id != evidence["video_id"]:
        mapping.image_id = mapping.cover_url = None
        mapping.connection_id = access.connection_id
    mapping.video_id, mapping.mid = evidence["video_id"], evidence.get("mid")
    mapping.status, mapping.verified_at = "available", datetime.now(UTC)
    from .bc_seeding import wake_seed_dependents

    wake_seed_dependents(session, context, dist)


def _blocked(dist: MaterialDistribution, code: str) -> None:
    dist.status, dist.reason_code = "blocked", code


def _invalidate_mapping(
    session: Session,
    context: TenantContext,
    dist: MaterialDistribution,
    work: dict[str, Any],
) -> None:
    if dist.superseded_by_id is not None:
        return
    # 真实详情不再支持正证据时，保留同一 VID 进入只读核查；不影响新身份或新连接。
    mapping = target_mapping(
        session,
        context=context,
        bc_id=dist.bc_id,
        material_id=dist.material_id,
        advertiser_id=dist.advertiser_id,
    )
    if (
        mapping is not None
        and mapping.video_id == work.get("video_id")
        and mapping.connection_id == _work_connection(work)
    ):
        mapping.status = "result_unknown"


def _work_connection(work: dict[str, Any]) -> UUID:
    return FrozenTikTokRoute.model_validate(work["target_route"]).connection_id


def _require_distribution_source(
    session: Session,
    *,
    context: TenantContext,
    material: MaterialFile,
    operation: MaterialAssetOperation,
    source_route: FrozenTikTokRoute,
    verifier: BatchSourceVerifier | None = None,
) -> None:
    response = operation.remote_response
    target_route = load_material_route(
        operation.frozen_route, context=context, bc_id=operation.bc_id
    )
    resolve_source = verifier.source if verifier is not None else resolve_remote_source
    require_route = (
        verifier.require_route if verifier is not None else require_material_route
    )
    source = resolve_source(
        session,
        context=context,
        bc_id=source_route.bc_id,
        material_id=UUID(response.get("source_material_id", str(material.id))),
        source_asset_id=UUID(response["source_asset_id"]),
    )
    source_file = (
        verifier.materials.get(source.material_id)
        if source is not None and verifier is not None
        else session.get(MaterialFile, source.material_id)
        if source
        else None
    )
    if (
        source is None
        or source_file is None
        or (source.advertiser_id, str(source.connection_id), source.video_id)
        != (
            response.get("source_advertiser_id"),
            response.get("source_connection_id"),
            response.get("source_video_id"),
        )
        or response.get("content_md5") != material.video_md5
        or (source_file.video_md5, source_file.byte_size)
        != (material.video_md5, material.byte_size)
    ):
        raise DomainError("material_remote_source_unavailable", "来源证据或权限已改变")
    transport = distribution_transport(
        source_bc_id=source.bc_id, target_bc_id=operation.bc_id
    )
    if response.get("transport") != transport:
        raise DomainError(
            "material_distribution_route_changed",
            "分发方式已变化，请重新准备尚未发送的任务",
        )
    if source.material_id != material.id and (
        not material.sha256
        or not material.digest_verified_at
        or not source_file.digest_verified_at
        or source_file.sha256 != material.sha256
    ):
        raise DomainError("material_source_digest_changed", "来源内容身份已变化")
    require_route(
        session,
        context=context,
        route=source_route,
        bc_id=source.bc_id,
        advertiser_id=source.advertiser_id,
        capability="read",
    )
    if source_route.connection_id != source.connection_id:
        raise DomainError("frozen_route_changed", "来源连接已改变")
    if transport == "native_share":
        # 共享接口使用目标通道的一份授权；该授权必须同时能操作源、目标账户。
        require_route(
            session,
            context=context,
            route=target_route,
            bc_id=source.bc_id,
            advertiser_id=source.advertiser_id,
            capability="upload",
        )
        require_execution_config(
            upload=True,
            endpoint="materials.share_assets",
            original=False,
            channel=target_route.channel,
        )
        return
    require_remote_material(material)
    if material.byte_size > settings.MATERIAL_URL_MAX_UPLOAD_BYTES:
        raise DomainError(
            "url_upload_capacity_exceeded", "素材超过当前URL转存工程容量限制"
        )
    if target_route.channel == "OFFICIAL_MCP":
        require_url_upload(
            material_upload_policy(
                channel=target_route.channel,
                adapter_contract_revision=target_route.adapter_contract_revision,
            ),
            byte_size=material.byte_size,
        )
    require_execution_config(
        upload=True,
        endpoint="materials.upload_video_url",
        original=False,
        channel=target_route.channel,
    )


def _relay_receipt(
    database_engine: Any,
    *,
    context: TenantContext,
    distribution_id: UUID,
    operation_id: UUID,
    claim: UUID,
    evidence: dict[str, str],
) -> None:
    with Session(database_engine) as db, db.begin():
        dist = _load_distribution(db, context, distribution_id)
        _locked_material(db, context, dist.material_id)
        operation = _locked_operation(db, context, operation_id)
        # 网关回执可迟于显式接替到达；完整正文由 observer 留档，旧业务证据不可改写。
        if dist.superseded_by_id is not None or operation.superseded_by_id is not None:
            return
        existing = operation.remote_response.get("video_id")
        if existing and existing != evidence["video_id"]:
            operation.remote_response = {
                **operation.remote_response,
                "conflicting_video_id": evidence["video_id"],
                "error_code": "material_reconciliation_ambiguous",
            }
            return
        operation.remote_response = {
            **operation.remote_response,
            **evidence,
            "upload_video_id": evidence["video_id"],
            "upload_mid": evidence.get("mid"),
        }
        if operation.attempt_token == claim and dist.operation_id == operation_id:
            operation.status, dist.status = "verifying", "verifying"


def _send_remote_asset(
    *,
    database_engine: Any,
    redis_client: Any,
    context: TenantContext,
    distribution_id: UUID,
    operation_id: UUID,
    claim: UUID,
    work: dict[str, Any],
    deadline: datetime,
    hard: int,
) -> dict[str, str] | None:
    route = load_material_route(
        work["target_route"], context=context, bc_id=work["bc_id"]
    )
    native = work["transport"] == "native_share"
    preview = None
    if native:
        # 原生共享不传输视频字节；源读取和共享共用一个短期限会话，避免重复握手。
        # 仍按每个操作独立准入，不能让读取借用上传的长预算或跨任务缓存会话。
        deadline = min(
            deadline, datetime.now(UTC) + timedelta(seconds=READ_HARD_LIMIT - 5)
        )
    else:
        preview = read_frozen_remote_source(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            bc_id=work["source_bc_id"],
            material_id=UUID(work["source_material_id"]),
            source_asset_id=UUID(work["source_asset_id"]),
            route=load_material_route(
                work["source_route"], context=context, bc_id=work["source_bc_id"]
            ),
            deadline=deadline,
        )
    policy = admission_policy(
        "materials.share_assets" if native else "materials.upload_video_url"
    )
    budget = material_types.RemoteCallBudget(deadline, hard, policy.lease_ms)

    def check_current() -> None:
        with bounded_session(database_engine, task_deadline=deadline) as db, db.begin():
            dist = _load_distribution(db, context, distribution_id)
            material = load_material(
                db,
                context=context,
                bc_id=dist.bc_id,
                material_id=dist.material_id,
                lock=True,
            )
            operation = _locked_operation(db, context, operation_id)
            if (
                operation.superseded_by_id is not None
                or dist.superseded_by_id is not None
                or operation.attempt_token != claim
                or dist.operation_id != operation_id
            ):
                raise DomainError("material_claim_changed", "素材操作已由其他任务接管")
            _require_distribution_source(
                db,
                context=context,
                material=material,
                operation=operation,
                source_route=load_material_route(
                    dist.source_route,
                    context=context,
                    bc_id=operation.remote_response.get("source_bc_id", dist.bc_id),
                ),
            )
            _target_access(db, context, dist, upload=True)
            budget.timeout(upload=True)

    check_current()
    with open_tiktok_gateway(
        database_engine=database_engine,
        redis_client=redis_client,
        context=context,
        route=route,
        task_deadline=deadline,
        before_request=check_current,
        # 原生共享会读取源账户；只有 URL 转存会话完全属于本次目标操作。
        response_observer=None
        if native
        else material_response_observer(
            database_engine=database_engine,
            context=context,
            route=route,
            material_id=work["material_id"],
            operation_id=operation_id,
            advertiser_id=work["advertiser_id"],
            task_deadline=deadline,
        ),
    ) as gateway:
        if native:
            # 共享使用 MID，且平台复制源名称；保存真实名称才能恢复重名/超时结果。
            record = gateway.materials.read_video(
                advertiser_id=work["source_advertiser_id"],
                video_id=work["source_video_id"],
                budget=material_types.RemoteCallBudget(
                    deadline,
                    READ_HARD_LIMIT,
                    admission_policy("materials.get_videos").lease_ms,
                ),
            )
            if (
                record is None
                or record.video_id != work["source_video_id"]
                or not record.mid
                or not record.file_name
                or record.md5 != work["content_md5"]
            ):
                raise DomainError(
                    "material_share_source_missing", "来源缺少共享 MID、名称或内容摘要"
                )
            work["source_mid"], work["remote_name"] = record.mid, record.file_name
            with (
                bounded_session(database_engine, task_deadline=deadline) as db,
                db.begin(),
            ):
                dist = _load_distribution(db, context, distribution_id)
                _locked_material(db, context, dist.material_id)
                operation = _locked_operation(db, context, operation_id)
                if (
                    operation.superseded_by_id is not None
                    or dist.superseded_by_id is not None
                    or operation.attempt_token != claim
                    or dist.operation_id != operation_id
                ):
                    return None
                operation.remote_response = {
                    **operation.remote_response,
                    "source_mid": record.mid,
                    "remote_name": record.file_name,
                }
            check_current()
        with bounded_session(database_engine, task_deadline=deadline) as db, db.begin():
            dist = _load_distribution(db, context, distribution_id)
            _locked_material(db, context, dist.material_id)
            operation = _locked_operation(db, context, operation_id)
            if (
                operation.superseded_by_id is not None
                or dist.superseded_by_id is not None
                or operation.attempt_token != claim
                or dist.operation_id != operation_id
            ):
                return None
            _target_access(db, context, dist, upload=True)
            operation.status, dist.status = "sending", "preparing"
            operation.remote_response = {
                **operation.remote_response,
                "send_armed": True,
                "upload_connection_id": str(route.connection_id),
            }
        try:
            if native:
                share_receipt = gateway.materials.share_assets(
                    material_types.AssetShare(
                        advertiser_id=work["source_advertiser_id"],
                        material_ids=(work["source_mid"],),
                        shared_advertiser_ids=(work["advertiser_id"],),
                    ),
                    budget=budget,
                )
                with Session(database_engine) as db, db.begin():
                    dist = _load_distribution(db, context, distribution_id)
                    _locked_material(db, context, dist.material_id)
                    operation = _locked_operation(db, context, operation_id)
                    if (
                        operation.superseded_by_id is not None
                        or dist.superseded_by_id is not None
                        or operation.attempt_token != claim
                        or dist.operation_id != operation_id
                    ):
                        return None
                    rejected = work["source_mid"] in share_receipt.failed_infos.get(
                        work["advertiser_id"], ()
                    )
                    operation.remote_response = {
                        **operation.remote_response,
                        "share_receipt": asdict(share_receipt),
                        "share_acknowledged": not rejected,
                    }
                    if rejected:
                        # 平台已明确此目标/MID 失败，不能当作待核实成功或转为重传。
                        operation.status, dist.status = "failed", "blocked"
                        operation.remote_response = {
                            **operation.remote_response,
                            "definite_no_effect": True,
                            "error_code": "material_share_failed",
                        }
                        operation.attempt_token = operation.claimed_until = None
                        dist.reason_code = "material_share_failed"
                        return None
                return {}
            assert preview is not None
            receipt = gateway.materials.upload_video_url(
                material_types.URLVideoUpload(
                    work["advertiser_id"],
                    preview.url,
                    work["remote_name"],
                    work["content_md5"],
                    work["byte_size"],
                ),
                budget=budget,
            )
        except Exception as error:
            if isinstance(error, api.SdkAdmissionDeferred) or (
                isinstance(error, RemoteCallError) and error.effect == "NOT_SENT"
            ):
                with Session(database_engine) as db, db.begin():
                    dist = _load_distribution(db, context, distribution_id)
                    _locked_material(db, context, dist.material_id)
                    operation = _locked_operation(db, context, operation_id)
                    if (
                        operation.attempt_token == claim
                        and dist.operation_id == operation_id
                    ):
                        operation.remote_response = {
                            **operation.remote_response,
                            "send_armed": False,
                        }
            raise
        evidence = api.receipt_evidence(receipt)
        for receipt_attempt in range(2):
            try:
                _relay_receipt(
                    database_engine,
                    context=context,
                    distribution_id=distribution_id,
                    operation_id=operation_id,
                    claim=claim,
                    evidence=evidence,
                )
                break
            except SDK_SCOPE_INTERRUPTS:
                raise
            except Exception:
                if receipt_attempt:
                    raise

    return evidence


def run_distribution(
    *,
    database_engine: Any,
    redis_client: Any,
    context: TenantContext,
    distribution_id: UUID,
    kind: str,
    operation_id: UUID | None = None,
    revision: int | None = None,
    recovery_claim_id: UUID | None = None,
    s3: Any = None,
    read_only: bool = False,
) -> None:
    """一次持久发送；来源和恢复读取各自遵守工作进程期限与准入。"""
    with Session(database_engine) as image_db:
        image_dist = image_db.get(MaterialDistribution, distribution_id)
        image_file = (
            image_db.get(MaterialFile, image_dist.material_id)
            if image_dist and image_dist.tenant_id == context.tenant_id
            else None
        )
        if image_file and image_file.media_type == "IMAGE":
            from .image_distribution import run_image_distribution

            return run_image_distribution(
                database_engine=database_engine,
                redis_client=redis_client,
                context=context,
                distribution_id=distribution_id,
                kind=kind,
                operation_id=operation_id,
                revision=revision,
                recovery_claim_id=recovery_claim_id,
                s3=s3,
                read_only=read_only,
            )
    if kind not in {"prepare", "verify"} or (read_only and kind != "verify"):
        raise DomainError("invalid_asset_task", "目标素材工作任务无效")
    # 旧队列消息必须在授权查询、素材锁及恢复分支之前退出；否则来源已完成
    # 而缓存过期时，旧观察消息会错误建立新一代读取并持续放大队列。
    with Session(database_engine) as session:
        current = _load_distribution(session, context, distribution_id)
        if current.superseded_by_id is not None:
            return
        if current.status not in ACTIVE_DISTRIBUTIONS and not (
            read_only and current.status in {"ready", "blocked"}
        ):
            return
        pending = session.exec(
            select(MaterialAssetOperation).where(
                MaterialAssetOperation.tenant_id == context.tenant_id,
                MaterialAssetOperation.id == current.operation_id,
            )
        ).one_or_none()
        if pending is None or not _delivery_matches(
            pending,
            operation_id=operation_id,
            revision=revision,
            recovery_claim_id=recovery_claim_id,
        ):
            return
    if kind == "prepare" and not read_only:
        from .bc_seeding import resume_ready_seed_dependents, resume_seed_dependency

        resume_ready_seed_dependents(
            database_engine=database_engine,
            context=context,
            distribution_id=distribution_id,
        )

        with Session(database_engine) as session, session.begin():
            dependency = _load_distribution(session, context, distribution_id)
            if (
                dependency.seed_id is not None
                and dependency.status in ACTIVE_DISTRIBUTIONS
            ):
                _locked_material(session, context, dependency.material_id)
                assert dependency.operation_id is not None
                pending = _locked_operation(session, context, dependency.operation_id)
                if operation_id is not None and pending.id != operation_id:
                    return
                if revision is not None and revision != pending.remote_response.get(
                    "revision", 0
                ):
                    return
                try:
                    if resume_seed_dependency(
                        session, context=context, dist=dependency, operation=pending
                    ):
                        return
                except DomainError as error:
                    dependency.status, dependency.reason_code = "blocked", error.code
                    pending.status = "failed"
                    pending.remote_response = {
                        **pending.remote_response,
                        "definite_no_effect": True,
                        "error_code": error.code,
                    }
                    return
    if kind == "prepare":
        from .batch_distribution import try_prepare_batch

        if try_prepare_batch(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            distribution_id=distribution_id,
            operation_id=operation_id,
            revision=revision,
            recovery_claim_id=recovery_claim_id,
        ):
            return
    else:
        from .batch_verification import try_verify_batch

        if try_verify_batch(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            distribution_id=distribution_id,
            operation_id=operation_id,
            revision=revision,
            recovery_claim_id=recovery_claim_id,
            read_only=read_only,
        ):
            return
    claim = uuid4()
    hard = UPLOAD_HARD_LIMIT if kind == "prepare" else READ_HARD_LIMIT
    lease = UPLOAD_CLAIM_SECONDS if kind == "prepare" else READ_CLAIM_SECONDS
    deadline = datetime.now(UTC) + timedelta(seconds=hard - 5)
    with Session(database_engine) as session, session.begin():
        dist = _load_distribution(session, context, distribution_id)
        if dist.superseded_by_id is not None:
            return
        if dist.status not in ACTIVE_DISTRIBUTIONS and not (
            read_only and dist.status in {"ready", "blocked"}
        ):
            return
        try:
            require_tenant(
                session,
                actor_id=context.actor_id,
                tenant_id=context.tenant_id,
                action="build",
            )
            material = load_material(
                session,
                context=context,
                bc_id=dist.bc_id,
                material_id=dist.material_id,
                lock=True,
            )
            # 获取素材锁前可能已有授权接替提交；刷新旧分发，不能用陈旧身份继续。
            session.refresh(dist)
            if dist.superseded_by_id is not None:
                return
            _target_access(session, context, dist, upload=False)
        except DomainError as error:
            if dist.operation_id is not None:
                # 目标撤权/绑定换代发生在发送前时，释放明确未发送的旧操作；
                # 新提交可冻结新授权，已 armed 或在途操作继续保留原核实身份。
                # 租户权限可能先于 load_material 失败，记账仍遵守素材→操作锁序。
                _locked_material(session, context, dist.material_id)
                session.refresh(dist)
                operation = _locked_operation(session, context, dist.operation_id)
                # 准备和核实的拒权分支都必须受当前头约束，迟到旧消息不能改历史状态。
                if (
                    dist.superseded_by_id is not None
                    or operation.superseded_by_id is not None
                ):
                    return
                if (
                    kind == "prepare"
                    and not read_only
                    and (operation_id is None or operation_id == operation.id)
                    and (
                        revision is None
                        or revision == operation.remote_response.get("revision", 0)
                    )
                    and operation.status == "pending"
                    and operation.attempt_token is None
                    and operation.claimed_until is None
                    and not operation.remote_response.get("send_armed")
                    and _attempt(session, operation.id) is None
                ):
                    operation.status = "failed"
                    operation.remote_response = {
                        **operation.remote_response,
                        "definite_no_effect": True,
                        "error_code": error.code,
                    }
            _blocked(dist, error.code)
            return
        assert dist.operation_id
        if operation_id is not None and operation_id != dist.operation_id:
            return
        operation = _locked_operation(session, context, dist.operation_id)
        # 快速检查后可能发生并发换代，持锁后再次核对，且早于所有来源分支。
        if not _delivery_matches(
            operation,
            operation_id=operation_id,
            revision=revision,
            recovery_claim_id=recovery_claim_id,
        ):
            return
        source = _attempt(session, operation.id)
        mapping = target_mapping(
            session,
            context=context,
            bc_id=dist.bc_id,
            material_id=dist.material_id,
            advertiser_id=dist.advertiser_id,
        )
        if (
            not read_only
            and mapping_fresh(mapping)
            and (source or operation.status in {"pending", "succeeded"})
        ):
            dist.status, dist.reason_code = "ready", None
            return
        if read_only and operation.status in {"failed", "pending", "confirmed_absent"}:
            # A delayed manual read cannot acquire a new upload authority. Keep
            # the actual operation history intact even after definitive failure.
            if operation.status == "failed":
                _blocked(dist, "material_operation_failed")
            return
        if read_only and operation.status == "succeeded":
            if revision is not None and revision != operation.remote_response.get(
                "revision", 0
            ):
                return
            if operation.claimed_until and operation.claimed_until > datetime.now(UTC):
                return
            known_id = operation.remote_response.get("video_id") or (
                mapping.video_id if mapping else None
            )
            if not isinstance(known_id, str) or not known_id.strip():
                return
            # 成功回执与当前目标映射一致时，遗留核查投递只消费，不重新
            # GET 或降级为 verifying；平台暂时空读不能推翻已完成的上传。
            # 实际负证据、换代/换账户和冲突仍走各自处理，不复活失效映射。
            if (
                mapping_fresh(mapping)
                and mapping is not None
                and mapping.video_id == known_id
                and mapping.connection_id
                == load_material_route(
                    dist.target_route, context=context, bc_id=dist.bc_id
                ).connection_id
                and not operation.remote_response.get("conflicting_video_id")
            ):
                dist.status, dist.reason_code = "ready", None
                # 已消费的旧核查消息不再作为补偿器的当前投递，避免空转。
                operation.remote_response = {
                    **operation.remote_response,
                    "revision": operation.remote_response.get("revision", 0) + 1,
                }
                return
            # Revalidate the actual receipt in the same fenced operation. This
            # never reserves a fresh upload or changes source-account history.
            operation.remote_response = {
                **operation.remote_response,
                "video_id": known_id,
            }
            operation.status, dist.status = "verifying", "verifying"
            source = None
        if source:
            if (
                operation.status == "failed"
                and source.status in {"failed", "blocked"}
                and not operation.remote_response.get("video_id")
            ):
                try:
                    require_upload_path(
                        session,
                        context=context,
                        material=material,
                        advertiser_id=dist.advertiser_id,
                        route=load_material_route(
                            dist.target_route, context=context, bc_id=dist.bc_id
                        ),
                    )
                except DomainError as error:
                    _blocked(dist, error.code)
                    return
                operation = _bind_operation(
                    session,
                    context=context,
                    material_id=material.id,
                    bc_id=dist.bc_id,
                    advertiser_id=dist.advertiser_id,
                    path="upload_original",
                    route=load_material_route(
                        dist.target_route, context=context, bc_id=dist.bc_id
                    ),
                )
                dist.operation_id, dist.path = operation.id, "upload_original"
                queue_distribution(
                    session, dist, operation, read_only=read_only, kind="prepare"
                )
            elif operation.status == "succeeded":
                # The source completed while an observation was delayed beyond
                # the cache window. Revalidate under a new target-owned read.
                operation = _bind_operation(
                    session,
                    context=context,
                    material_id=material.id,
                    bc_id=dist.bc_id,
                    advertiser_id=dist.advertiser_id,
                    path="existing_target",
                    route=load_material_route(
                        dist.target_route, context=context, bc_id=dist.bc_id
                    ),
                )
                dist.operation_id, dist.path = operation.id, "existing_target"
                queue_distribution(
                    session, dist, operation, read_only=read_only, kind="verify"
                )
            else:
                dist.status = (
                    "result_unknown"
                    if operation.status == "result_unknown"
                    else "verifying"
                )
                queue_distribution(
                    session,
                    dist,
                    operation,
                    read_only=read_only,
                    kind="verify",
                    observe=True,
                    due=datetime.now(UTC) + timedelta(seconds=60),
                )
            return
        if operation.claimed_until and operation.claimed_until > datetime.now(UTC):
            return
        if kind == "verify" and _reconciliation_exhausted(operation):
            _stop_reconciliation(dist, operation)
            if not operation.remote_response.get("reconciliation_stopped"):
                queue_distribution(
                    session, dist, operation, kind="verify", read_only=read_only
                )
            return
        expired_send = operation.status == "sending"
        if expired_send:
            operation.status, dist.status = "result_unknown", "result_unknown"
        if operation.status == "failed":
            if operation.path == "share_source":
                _blocked(
                    dist,
                    operation.remote_response.get(
                        "error_code", "material_share_failed"
                    ),
                )
                return
            if not operation.remote_response.get("definite_no_effect"):
                _blocked(dist, "material_operation_failed")
                return
            try:
                require_upload_path(
                    session,
                    context=context,
                    material=material,
                    advertiser_id=dist.advertiser_id,
                    route=load_material_route(
                        dist.target_route, context=context, bc_id=dist.bc_id
                    ),
                )
            except DomainError as error:
                _blocked(dist, error.code)
                return
            operation = _bind_operation(
                session,
                context=context,
                material_id=material.id,
                bc_id=dist.bc_id,
                advertiser_id=dist.advertiser_id,
                path="upload_original",
                route=load_material_route(
                    dist.target_route, context=context, bc_id=dist.bc_id
                ),
            )
            dist.operation_id, dist.path = operation.id, "upload_original"
            if kind != "prepare":
                queue_distribution(
                    session, dist, operation, read_only=read_only, kind="prepare"
                )
                return
        if operation.status not in UNRESOLVED:
            return
        expected = (
            "prepare"
            if operation.status in {"pending", "confirmed_absent"}
            else "verify"
        )
        if expected != kind:
            if expired_send:
                operation.attempt_token, operation.claimed_until = None, None
                queue_distribution(
                    session, dist, operation, read_only=read_only, kind="verify"
                )
            return
        try:
            if kind == "prepare":
                if operation.path == "share_source":
                    _require_distribution_source(
                        session,
                        context=context,
                        material=material,
                        operation=operation,
                        source_route=load_material_route(
                            dist.source_route,
                            context=context,
                            bc_id=operation.remote_response.get(
                                "source_bc_id", dist.bc_id
                            ),
                        ),
                    )
                else:
                    require_upload_path(
                        session,
                        context=context,
                        material=material,
                        advertiser_id=dist.advertiser_id,
                        route=load_material_route(
                            dist.target_route, context=context, bc_id=dist.bc_id
                        ),
                    )
            else:
                if not material.video_md5:
                    raise DomainError(
                        "material_digest_missing", "素材缺少可核实内容摘要"
                    )
                require_execution_config(
                    upload=False,
                    endpoint="materials.get_videos"
                    if operation.remote_response.get("video_id")
                    else "materials.search_videos",
                    channel=load_material_route(
                        dist.target_route, context=context, bc_id=dist.bc_id
                    ).channel,
                )
        except DomainError as error:
            _blocked(dist, error.code)
            if kind == "prepare":
                operation.status = "failed"
                operation.remote_response = {
                    **operation.remote_response,
                    "definite_no_effect": True,
                    "error_code": error.code,
                }
            return
        if read_only:
            dist.status = "verifying"
        if kind == "verify":
            _claim_reconciliation(operation)
        previous_status = operation.status
        current_revision = operation.remote_response.get("revision", 0) + 1
        operation.remote_response = {
            **operation.remote_response,
            "revision": current_revision,
        }
        operation.attempt_token, operation.claimed_until = (
            claim,
            datetime.now(UTC) + timedelta(seconds=lease),
        )
        operation_id = operation.id
        queue_distribution(
            session,
            dist,
            operation,
            read_only=read_only,
            kind=kind,
            due=operation.claimed_until,
            claim_id=claim,
        )
        work: dict[str, Any] = {
            **operation.remote_response,
            "bc_id": dist.bc_id,
            "target_route": dist.target_route,
            "source_route": dist.source_route,
            "material_id": material.id,
            "advertiser_id": dist.advertiser_id,
            "remote_name": operation.remote_response.get("remote_name")
            or remote_name(material),
            "byte_size": material.byte_size,
            "strict_video": material.current_object_generation is not None
            or operation.remote_response.get("transport") == "url_relay"
            or operation.remote_response.get("batch_discovered", False),
        }
        content_md5 = material.video_md5 or ""
    sent = False
    evidence: dict[str, str] | tuple[list[dict[str, str]], bool] | None = None
    original_scope: AbstractContextManager[OriginalFile | None]
    try:
        if kind == "prepare" and work.get("transport") in {"native_share", "url_relay"}:
            evidence = _send_remote_asset(
                database_engine=database_engine,
                redis_client=redis_client,
                context=context,
                distribution_id=distribution_id,
                operation_id=operation_id,
                claim=claim,
                work=work,
                deadline=deadline,
                hard=hard,
            )
            if evidence is None:
                return
        else:
            original_scope = (
                open_original(
                    database_engine=database_engine,
                    context=context,
                    bc_id=work["bc_id"],
                    material_id=work["material_id"],
                    action="build",
                    deadline=deadline,
                    s3=s3,
                )
                if kind == "prepare"
                else nullcontext(None)
            )
            with original_scope as original:
                route = load_material_route(
                    work["target_route"], context=context, bc_id=work["bc_id"]
                )
                logical = (
                    "materials.upload_video_file"
                    if original
                    else "materials.get_videos"
                    if work.get("video_id")
                    else "materials.search_videos"
                )
                policy = admission_policy(logical)
                budget = material_types.RemoteCallBudget(
                    deadline, hard, policy.lease_ms
                )
                require_execution_config(
                    upload=kind == "prepare", endpoint=logical, channel=route.channel
                )

                def check_current() -> None:
                    with (
                        bounded_session(database_engine, task_deadline=deadline) as db,
                        db.begin(),
                    ):
                        dist = _load_distribution(db, context, distribution_id)
                        load_material(
                            db,
                            context=context,
                            bc_id=dist.bc_id,
                            material_id=dist.material_id,
                            lock=kind == "prepare",
                        )
                        operation = (
                            _locked_operation(db, context, operation_id)
                            if kind == "prepare"
                            else db.exec(
                                select(MaterialAssetOperation).where(
                                    MaterialAssetOperation.tenant_id
                                    == context.tenant_id,
                                    MaterialAssetOperation.id == operation_id,
                                )
                            ).one()
                        )
                        if (
                            operation.superseded_by_id is not None
                            or dist.superseded_by_id is not None
                            or operation.attempt_token != claim
                            or dist.operation_id != operation_id
                        ):
                            raise DomainError(
                                "material_claim_changed", "素材操作已由其他任务接管"
                            )
                        _target_access(
                            db,
                            context,
                            dist,
                            upload=kind == "prepare",
                            connection_id=_work_connection(work),
                        )
                        budget.timeout(upload=kind == "prepare")

                check_current()
                with open_tiktok_gateway(
                    database_engine=database_engine,
                    redis_client=redis_client,
                    context=context,
                    route=route,
                    task_deadline=deadline,
                    before_request=check_current,
                    # 目标详情与搜索分别留档，不能把核验响应当作原上传回执。
                    response_observer=material_response_observer(
                        database_engine=database_engine,
                        context=context,
                        route=route,
                        material_id=work["material_id"],
                        operation_id=operation_id,
                        advertiser_id=work["advertiser_id"],
                        task_deadline=deadline,
                    ),
                ) as gateway:
                    if original:
                        with (
                            bounded_session(
                                database_engine, task_deadline=deadline
                            ) as db,
                            db.begin(),
                        ):
                            dist = _load_distribution(db, context, distribution_id)
                            current_material = load_material(
                                db,
                                context=context,
                                bc_id=dist.bc_id,
                                material_id=dist.material_id,
                                lock=True,
                            )
                            operation = _locked_operation(db, context, operation_id)
                            if (
                                operation.superseded_by_id is not None
                                or dist.superseded_by_id is not None
                                or operation.attempt_token != claim
                                or dist.operation_id != operation_id
                            ):
                                return
                            _target_access(db, context, dist, upload=True)
                            current_material.sha256, current_material.video_md5 = (
                                original.sha256,
                                original.md5,
                            )
                            content_md5 = original.md5
                            operation.request_digest = sha256(
                                f"{work['advertiser_id']}:{work['remote_name']}:{original.sha256}".encode()
                            ).hexdigest()
                            operation.status, dist.status = "sending", "preparing"
                            operation.remote_response = {
                                **operation.remote_response,
                                "upload_connection_id": str(route.connection_id),
                            }
                        sent = True
                        receipt = gateway.materials.upload_video_file(
                            material_types.FileVideoUpload(
                                work["advertiser_id"],
                                original.path,
                                work["remote_name"],
                                original.md5,
                                work["byte_size"],
                            ),
                            budget=budget,
                        )
                        evidence = api.receipt_evidence(receipt)
                        _relay_receipt(
                            database_engine,
                            context=context,
                            distribution_id=distribution_id,
                            operation_id=operation_id,
                            claim=claim,
                            evidence=evidence,
                        )
                    elif work.get("video_id"):
                        record = gateway.materials.read_video(
                            advertiser_id=work["advertiser_id"],
                            video_id=work["video_id"],
                            budget=budget,
                        )
                        evidence = api.verified_video(
                            {"list": [api.video_record_data(record)] if record else []},
                            md5=content_md5,
                            expected_video_id=work["video_id"],
                            expected_size=work["byte_size"]
                            if work["strict_video"]
                            else None,
                        )
                    else:
                        page = gateway.materials.search_videos(
                            video_name=work["remote_name"]
                            if work.get("transport") == "native_share"
                            else None,
                            advertiser_id=work["advertiser_id"],
                            page=work.get("search_page", 1),
                            material_ids=(),
                            budget=budget,
                        )
                        if work.get("transport") in {"native_share", "url_relay"}:
                            total = page.total_pages
                            if not 0 <= total <= 100 or not 1 <= page.page <= 100:
                                raise DomainError(
                                    "material_reconciliation_bounded",
                                    "目标核查超过有界分页范围",
                                )
                            work["observed_search_total"] = total
                            work["search_changed"] = work.get("search_total") not in (
                                None,
                                total,
                            )
                        evidence = api.search_page(
                            api.video_page_data(page),
                            page=work.get("search_page", 1),
                            remote_name=work["remote_name"],
                            md5=content_md5,
                        )
                        # 按名称查询的一页完整唯一结果已携带可用状态和摘要时，直接使用
                        # 这份真实目标证据；不再为相同 VID 另排一次详情请求。
                        if (
                            work.get("transport") == "native_share"
                            and page.page == 1
                            and page.total_pages == 1
                        ):
                            matches, last = evidence
                            if last and len(matches) == 1:
                                match_id = matches[0]["video_id"]
                                record = next(
                                    (
                                        row
                                        for row in page.rows
                                        if row.video_id == match_id
                                    ),
                                    None,
                                )
                                if record is not None:
                                    work["verified_search_match"] = api.verified_video(
                                        {"list": [api.video_record_data(record)]},
                                        md5=content_md5,
                                        expected_video_id=match_id,
                                        expected_size=work["byte_size"]
                                        if work["strict_video"]
                                        else None,
                                    )
        with Session(database_engine) as session, session.begin():
            dist = _load_distribution(session, context, distribution_id)
            _locked_material(session, context, dist.material_id)
            operation = _locked_operation(session, context, operation_id)
            if (
                operation.superseded_by_id is not None
                or dist.superseded_by_id is not None
                or operation.attempt_token != claim
                or dist.operation_id != operation_id
            ):
                return
            if kind == "prepare":
                assert isinstance(evidence, dict)
                if work.get("transport") == "native_share":
                    operation.remote_response = {
                        **operation.remote_response,
                        "share_acknowledged": True,
                    }
                    # 共享 ACK 没有目标 VID，仍须发现目标库存的真实身份。
                    operation.status, dist.status = "verifying", "verifying"
                else:
                    if operation.remote_response.get("conflicting_video_id"):
                        raise DomainError(
                            "material_reconciliation_ambiguous", "实际目标回执存在歧义"
                        )
                    operation.remote_response = {
                        **operation.remote_response,
                        **evidence,
                        "upload_video_id": evidence["video_id"],
                        "upload_mid": evidence.get("mid"),
                        "confirmation_source": "upload_receipt",
                    }
                    # 成功上传回执与来源 URL 导入使用同一语义；不伪造摘要或可用状态。
                    _publish_mapping(
                        session,
                        context,
                        dist,
                        evidence,
                        connection_id=_work_connection(work),
                    )
                    operation.status, dist.status = "succeeded", "ready"
            elif work.get("video_id") and evidence:
                assert isinstance(evidence, dict)
                if operation.remote_response.get("conflicting_video_id"):
                    raise DomainError(
                        "material_reconciliation_ambiguous", "实际目标回执存在歧义"
                    )
                _publish_mapping(
                    session,
                    context,
                    dist,
                    evidence,
                    connection_id=_work_connection(work),
                )
                operation.remote_response = {**operation.remote_response, **evidence}
                if work.get("transport") == "url_relay":
                    # 严格名称/摘要搜索后的 VID 详情也能确认实际 URL 上传结果；
                    # 独立记录核实证据，不伪造可能已经丢失的原上传响应。
                    operation.remote_response = {
                        **operation.remote_response,
                        "verified_upload_video_id": evidence["video_id"],
                    }
                operation.status, dist.status = "succeeded", "ready"
            elif not work.get("video_id"):
                assert isinstance(evidence, tuple)
                matches, last = evidence
                candidates = {
                    item["video_id"]: item for item in work.get("candidates", [])
                }
                candidates.update({item["video_id"]: item for item in matches})
                if work.get("transport") in {"native_share", "url_relay"}:
                    operation.remote_response = {
                        **operation.remote_response,
                        "search_total": work["observed_search_total"],
                    }
                if work.get("search_changed"):
                    operation.remote_response = {
                        **operation.remote_response,
                        "search_page": 1,
                        "search_total": None,
                        "candidates": [],
                        "error_code": "material_reconciliation_incomplete",
                    }
                    operation.status, dist.status = "result_unknown", "result_unknown"
                elif len(candidates) > 1:
                    operation.remote_response = {
                        **operation.remote_response,
                        "search_page": 1,
                        "candidates": list(candidates.values())[:2]
                        if work.get("transport") == "url_relay"
                        else [],
                        "error_code": "material_reconciliation_ambiguous",
                    }
                    operation.status, dist.status = "result_unknown", "result_unknown"
                elif last and candidates:
                    operation.remote_response = {
                        **operation.remote_response,
                        **next(iter(candidates.values())),
                        "candidates": [],
                    }
                    verified = work.get("verified_search_match")
                    if verified and verified["video_id"] in candidates:
                        _publish_mapping(
                            session,
                            context,
                            dist,
                            verified,
                            connection_id=_work_connection(work),
                        )
                        operation.status, dist.status = "succeeded", "ready"
                    else:
                        operation.status, dist.status = "verifying", "verifying"
                else:
                    operation.remote_response = {
                        **operation.remote_response,
                        "search_page": 1 if last else work.get("search_page", 1) + 1,
                        "candidates": [] if last else list(candidates.values()),
                        "reconciliation_complete": last,
                    }
                    operation.status, dist.status = "result_unknown", "result_unknown"
            else:
                operation.status, dist.status = "verifying", "verifying"
                _invalidate_mapping(session, context, dist, work)
            progress = dist.status == "ready" or (
                kind == "verify"
                and not work.get("video_id")
                and not work.get("search_changed")
                and len(candidates) <= 1
            )
            if kind == "verify":
                _reconciliation_progress(operation, progress=progress)
            else:
                operation.remote_response = {
                    key: value
                    for key, value in operation.remote_response.items()
                    if key != "error_code"
                }
            dist.reason_code = (
                None if dist.status == "ready" else "material_result_pending"
            )
            operation.attempt_token, operation.claimed_until = None, None
            from .batch_distribution import sync_batch_member

            sync_batch_member(session, operation, dist)
            if dist.status == "ready":
                from .source_cover_service import enqueue_source_cover

                # 与真实目标 VID 同事务登记；封面共享在独立任务内提前执行，
                # 不等广告搭建、不在视频回执锁内做图片网络请求。
                enqueue_source_cover(
                    session, context=context, operation=operation, purpose="BUILD"
                )
            if dist.status != "ready":
                queue_distribution(
                    session,
                    dist,
                    operation,
                    read_only=read_only,
                    kind="verify",
                    due=datetime.now(UTC)
                    + timedelta(
                        seconds=0
                        if progress
                        or (
                            work.get("transport") == "native_share"
                            and kind == "prepare"
                        )
                        else 60
                    ),
                )
    except SDK_SCOPE_INTERRUPTS:
        raise
    except Exception as error:
        if isinstance(error, api.SdkAdmissionDeferred) or (
            isinstance(error, RemoteCallError) and error.effect == "NOT_SENT"
        ):
            sent = False
        with Session(database_engine) as session, session.begin():
            dist = _load_distribution(session, context, distribution_id)
            _locked_material(session, context, dist.material_id)
            operation = _locked_operation(session, context, operation_id)
            if (
                operation.superseded_by_id is not None
                or dist.superseded_by_id is not None
                or operation.attempt_token != claim
                or dist.operation_id != operation_id
            ):
                return
            code = (
                error.code
                if isinstance(error, DomainError)
                else "material_response_unknown"
            )
            if kind == "verify" and code == "unsupported_material_schema":
                # 返回非请求 VID 等结构冲突属于已观察到的不一致；传输超时不冒充负证据。
                _invalidate_mapping(session, context, dist, work)
            deferred = (
                isinstance(error, api.SdkAdmissionDeferred)
                or code == "admission_unavailable"
                or (kind == "verify" and code in RECONCILIATION_LOCAL_ERRORS)
            )
            operation.status = (
                previous_status
                if deferred
                else "result_unknown"
                if sent
                or operation.remote_response.get("send_armed")
                or kind == "verify"
                else "failed"
            )
            if operation.status == "failed":
                operation.remote_response = {
                    **operation.remote_response,
                    "definite_no_effect": True,
                }
            operation.remote_response = {
                **operation.remote_response,
                "error_code": code,
            }
            operation.attempt_token, operation.claimed_until = None, None
            if kind == "verify":
                if deferred:
                    _defer_local_reconciliation(operation, code)
                else:
                    _reconciliation_progress(operation, progress=False)
            dist.status = (
                ("verifying" if kind == "verify" else "queued")
                if deferred
                else "blocked"
                if operation.status == "failed"
                else "result_unknown"
            )
            dist.reason_code = code
            if kind == "verify" and _automatic_reconciliation(operation):
                # 原生共享已有 ACK、当前只有读取等待时，维持自动确认中的进度，
                # 不把仍有下一次投递的任务误报成需要人工处理。
                dist.status = "verifying"
            if dist.status != "blocked":
                delay = (
                    max(1, error.retry_after_ms) / 1000
                    if isinstance(error, api.SdkAdmissionDeferred)
                    else 60
                )
                queue_distribution(
                    session,
                    dist,
                    operation,
                    read_only=read_only,
                    kind=kind if deferred else "verify",
                    due=datetime.now(UTC) + timedelta(seconds=delay),
                )


def repair_material_dispatches(session: Session, *, limit: int = 100) -> int:
    """Bounded repair of lost material messages, preserving their exact generation.

    Published is broker acceptance, not execution. Re-arm the same active business
    message after a grace period; never manufacture revisions or touch pending
    broker backoff. Eligibility is filtered in SQL before applying the row limit.
    """
    from sqlalchemy import String, and_, cast, func, or_, text
    from sqlalchemy import select as sa_select
    from sqlalchemy.exc import OperationalError

    from .models import MaterialFile, MaterialUploadAttempt, ObjectUpload

    if type(limit) is not int or not 0 < limit <= 100:
        raise DomainError("invalid_asset_task", "素材恢复批量上限为 100")
    # Worker 硬终止不会立即取消服务器上的长 SQL；数据库自身必须先超时。
    session.connection().execute(text("SET LOCAL statement_timeout = '10s'"))
    session.connection().execute(text("SET LOCAL lock_timeout = '2s'"))
    # 短周期恢复只取 100 条，避免复杂 EXISTS 触发的 JIT 编译挤占本轮预算。
    session.connection().execute(text("SET LOCAL jit = off"))
    # 预备语句的通用计划无法用参数化任务名/JSON键证明局部表达式索引；
    # 当前业务身份和消息类型参与规划，避免长连接复用后重新全扫历史outbox。
    session.connection().execute(text("SET LOCAL plan_cache_mode='force_custom_plan'"))
    now = datetime.now(UTC)
    payload = col(PendingDispatch.payload)

    remote = col(MaterialAssetOperation.remote_response)
    upload_owners = (
        select(MaterialUploadAttempt.tenant_id, MaterialUploadAttempt.operation_id)
        .distinct()
        .cte("repair_upload_owners")
        .prefix_with("MATERIALIZED")
    )
    # 先按业务身份物化当前事实，再与历史消息哈希连接。旧 OR EXISTS 会对每条
    # 已完成消息重新查库存，并在 LIMIT 前扫描/排序全部历史；短周期恢复不能
    # 随某个身份过去投递过多少次而反复执行相同的主键查找。
    source_identities = (
        sa_select(
            col(MaterialAssetOperation.id),
            col(MaterialAssetOperation.tenant_id),
            col(MaterialAssetOperation.attempt_token),
            col(MaterialAssetOperation.claimed_until),
            func.coalesce(remote["revision"].astext, "0").label("revision"),
        )
        .join(
            upload_owners,
            and_(
                upload_owners.c.tenant_id == MaterialAssetOperation.tenant_id,
                upload_owners.c.operation_id == MaterialAssetOperation.id,
            ),
        )
        .where(
            col(MaterialAssetOperation.superseded_by_id).is_(None),
            col(MaterialAssetOperation.status).in_(UNRESOLVED),
        )
        .cte("repair_source_identities")
        .prefix_with("MATERIALIZED")
    )

    def live_message(identity: Any, message_payload: Any = payload):
        return or_(
            and_(
                identity.c.attempt_token.is_not(None),
                message_payload["claim_id"].astext
                == cast(identity.c.attempt_token, String),
                identity.c.claimed_until <= now,
            ),
            and_(
                identity.c.attempt_token.is_(None),
                message_payload["revision"].astext == identity.c.revision,
            ),
        )

    due = (
        col(PendingDispatch.published_at) <= now - timedelta(seconds=120),
        PendingDispatch.available_at <= now,
    )
    # 当前代已经有待投递消息时，沿用它的退避时间；重放更早消息会绕过退避，
    # 也会让恢复成本随历史投递次数增长。先在小型 pending 索引中排除这些身份。
    source_tasks = ["materials.upload_original", "materials.verify_original"]
    # 显式物化待投递集合，防止规划器按 operation 索引扫描该身份全部历史，
    # 再逐行检查 published_at。future available_at 也必须保留，不能绕过退避。
    # 已替代/退役消息不能阻止恢复；三个空值条件共同匹配待派发部分索引。
    pending_sources = (
        select(PendingDispatch.tenant_id, PendingDispatch.payload)
        .where(
            col(PendingDispatch.published_at).is_(None),
            col(PendingDispatch.superseded_at).is_(None),
            col(PendingDispatch.retired_at).is_(None),
            col(PendingDispatch.task_name).in_(source_tasks),
        )
        .cte("repair_pending_sources")
        .prefix_with("MATERIALIZED")
    )
    pending_payload = pending_sources.c.payload
    scheduled_source = (
        sa_select(pending_sources.c.tenant_id)
        .where(
            pending_sources.c.tenant_id == source_identities.c.tenant_id,
            pending_payload["operation_id"].astext
            == cast(source_identities.c.id, String),
            live_message(source_identities, pending_payload),
        )
        .correlate(source_identities)
        .exists()
    )
    missing_sources = (
        sa_select(source_identities)
        .where(~scheduled_source)
        .cte("repair_missing_sources")
        .prefix_with("MATERIALIZED")
    )
    # 同一操作/代数只需一条原消息恢复推进；其余历史消息仍保留审计和幂等键。
    # LATERAL LIMIT 保证查到一个有效候选即停止，不把数万条旧消息搬回 Python。
    source_message = (
        select(PendingDispatch.id)
        .where(
            PendingDispatch.tenant_id == missing_sources.c.tenant_id,
            payload["operation_id"].astext == cast(missing_sources.c.id, String),
            *due,
            col(PendingDispatch.task_name).in_(source_tasks),
            live_message(missing_sources),
        )
        .correlate(missing_sources)
        .limit(1)
        .lateral("repair_source_message")
    )
    source_candidates = (
        select(source_message.c.id)
        .select_from(missing_sources)
        .join(source_message, text("true"))
    )
    source_started = (
        select(MaterialUploadAttempt.id)
        .where(
            MaterialUploadAttempt.tenant_id == ObjectUpload.tenant_id,
            MaterialUploadAttempt.material_id == ObjectUpload.material_id,
        )
        .exists()
    )
    original_work = (
        select(ObjectUpload.id)
        .join(
            MaterialFile,
            and_(
                col(ObjectUpload.material_id) == MaterialFile.id,
                col(ObjectUpload.tenant_id) == MaterialFile.tenant_id,
            ),
        )
        .where(
            ObjectUpload.tenant_id == PendingDispatch.tenant_id,
            ObjectUpload.task_id == PendingDispatch.id,
            PendingDispatch.task_name == "materials.upload_original",
            ObjectUpload.status == "stored",
            MaterialFile.storage_state == "stored",
            ~source_started,
        )
        .exists()
    )
    from .bc_seeding import seed_dependency_settled

    target_identities = (
        sa_select(
            col(MaterialDistribution.id),
            col(MaterialDistribution.tenant_id),
            col(MaterialAssetOperation.id).label("operation_id"),
            col(MaterialAssetOperation.status).label("operation_status"),
            col(MaterialAssetOperation.attempt_token),
            col(MaterialAssetOperation.claimed_until),
            func.coalesce(remote["revision"].astext, "0").label("revision"),
            upload_owners.c.operation_id.is_not(None).label("source_owned"),
        )
        .join(
            MaterialAssetOperation,
            and_(
                col(MaterialDistribution.operation_id) == MaterialAssetOperation.id,
                col(MaterialDistribution.tenant_id) == MaterialAssetOperation.tenant_id,
            ),
        )
        .outerjoin(
            upload_owners,
            and_(
                upload_owners.c.tenant_id == MaterialAssetOperation.tenant_id,
                upload_owners.c.operation_id == MaterialAssetOperation.id,
            ),
        )
        .where(
            # 只物化仍在推进的分发。原先连同两万多条 ready/blocked 一起物化，
            # 再与十几万条历史目标消息全量合并，生产实测 7.8 秒、整条补派超时，
            # 丢失的素材消息永不补派。已落定分发的遗留只读观察消息不再补派：
            # 搭建侧 UNKNOWN 步骤由 recover_material_results 按分发终态直接收口。
            col(MaterialDistribution.status).in_(ACTIVE_DISTRIBUTIONS),
            col(MaterialDistribution.superseded_by_id).is_(None),
            col(MaterialAssetOperation.superseded_by_id).is_(None),
            remote["reconciliation_complete"].astext.is_distinct_from("true"),
            remote["reconciliation_stopped"].astext.is_distinct_from("true"),
            seed_dependency_settled(),
        )
        .cte("repair_target_identities")
        .prefix_with("MATERIALIZED")
    )
    target_candidates = (
        select(PendingDispatch.id)
        .join(
            target_identities,
            and_(
                target_identities.c.tenant_id == PendingDispatch.tenant_id,
                # 身份UUID的规范字符串严格匹配原小写UUID范围，非法消息只不匹配；
                # 不再逐历史消息正则/CAST，也不把大remote_response带入物化库存。
                cast(target_identities.c.id, String)
                == payload["distribution_id"].astext,
                cast(target_identities.c.operation_id, String)
                == payload["operation_id"].astext,
            ),
        )
        .where(
            *due,
            col(PendingDispatch.task_name).in_(
                ["materials.prepare_target", "materials.verify_target"]
            ),
            or_(
                and_(
                    payload["read_only"].astext == "true",
                    live_message(target_identities),
                ),
                and_(
                    payload["observe"].astext == "true",
                    payload["read_only"].astext.is_distinct_from("true"),
                    target_identities.c.source_owned,
                ),
                and_(
                    target_identities.c.operation_status.in_(UNRESOLVED),
                    live_message(target_identities),
                ),
            ),
        )
    )
    # 三种来源各自独立查询并去重；不能先截取历史100条再过滤，否则活任务会饥饿。
    # 每类在独立保存点里执行：来源上传的长期核查会积累数万条历史消息，单类超时
    # 只跳过该类并记录告警，不能再拖垮目标素材补派（搭建素材依赖目标类）。
    candidate_ids: set[UUID] = set()
    for label, statement in (
        ("target", target_candidates),
        ("original", select(PendingDispatch.id).where(*due, original_work)),
        ("source", source_candidates),
    ):
        try:
            with session.begin_nested():
                candidate_ids.update(session.exec(statement).all())
        except OperationalError as error:
            logger.warning(
                "material dispatch repair skipped %s candidates: %s",
                label,
                type(error.orig).__name__,
            )
    if not candidate_ids:
        return 0
    rows = session.exec(
        select(PendingDispatch)
        .where(col(PendingDispatch.id).in_(candidate_ids))
        # 候选查询之后消息可能已被其他worker更新；行锁EPQ必须重验实际到期时间。
        .where(*due)
        .order_by(col(PendingDispatch.published_at), col(PendingDispatch.id))
        .limit(limit)
        .with_for_update(of=PendingDispatch, skip_locked=True)
    ).all()
    for dispatch in rows:
        dispatch.published_at = None
    session.flush()
    return len(rows)
