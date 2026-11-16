"""每步一个平台动作；发送前落盘，崩溃后只核实，平台合并不丢来源。"""

from datetime import timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.common import RemoteCallError
from app.integrations.tiktok.contracts.materials import (
    MaterialPage,
    RemoteCallBudget,
    VideoReceipt,
    VideoRecord,
    VIDVideoUpload,
)
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.jobs.admission import admission_policy
from app.modules.accounts.routing import verify_route

from .link_models import MaterialLinkBatch, MaterialLinkItem, now
from .link_service import ACTIVE, queue_items, require_ingest
from .models import AccountMaterial, MaterialFile
from .routes import load_material_route
from .source_uploads import UPLOAD_HARD_LIMIT

# 软期限短于 prefork 硬期限，领取租约覆盖整个物理调用；控制面不能并发接管。
LEASE_SECONDS = UPLOAD_HARD_LIMIT + 30
MAX_POLLS = 12


def _lock(
    session: Session, context: TenantContext, item_id: UUID
) -> MaterialLinkItem | None:
    return session.exec(
        select(MaterialLinkItem)
        .where(
            MaterialLinkItem.tenant_id == context.tenant_id,
            MaterialLinkItem.id == item_id,
        )
        .with_for_update()
    ).one_or_none()


def _register(session: Session, item: MaterialLinkItem, record: VideoRecord) -> None:
    if not (
        record.displayable is True
        and record.mid
        and record.md5
        and record.size
        and record.width
        and record.height
        and record.advertiser_id == item.advertiser_id
        and record.video_id == item.video_id
    ):
        raise DomainError("link_material_unverified", "平台素材规格尚未核实")
    # 同账户同 MID 的并发链接只建立一个本地素材映射，来源条目仍分别保留。
    key = f"link-asset:{item.tenant_id}:{item.bc_id}:{item.advertiser_id}:{record.mid}"
    session.connection().execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": int.from_bytes(sha256(key.encode()).digest()[:8], "big", signed=True)},
    )
    asset = session.exec(
        select(AccountMaterial)
        .where(
            AccountMaterial.tenant_id == item.tenant_id,
            AccountMaterial.bc_id == item.bc_id,
            AccountMaterial.advertiser_id == item.advertiser_id,
            AccountMaterial.mid == record.mid,
            AccountMaterial.media_type == "VIDEO",
        )
        .order_by(col(AccountMaterial.id))
        .limit(1)
    ).first()
    if asset:
        material = session.get(MaterialFile, asset.material_id)
        if not material or (material.video_md5, material.byte_size) != (
            record.md5,
            record.size,
        ):
            raise DomainError(
                "link_material_identity_changed", "同素材 ID 的内容证据不一致"
            )
    else:
        material = MaterialFile(
            tenant_id=item.tenant_id,
            bc_id=item.bc_id,
            file_name=record.file_name or item.requested_file_name,
            object_key=f"remote-link/{item.tenant_id}/{uuid4()}",
            byte_size=record.size,
            video_md5=record.md5,
            width=record.width,
            height=record.height,
            duration=record.duration,
            storage_state="unavailable",
            digest_source="platform_video_info",
        )
        session.add(material)
        session.flush()
        asset = AccountMaterial(
            tenant_id=item.tenant_id,
            bc_id=item.bc_id,
            material_id=material.id,
            advertiser_id=item.advertiser_id,
            connection_id=item.connection_id,
        )
    asset.connection_id = item.connection_id
    asset.frozen_route = item.frozen_route
    asset.video_id, asset.mid = record.video_id, record.mid
    asset.status, asset.verified_at = "available", now()
    session.add(asset)
    prior = session.exec(
        select(MaterialLinkItem)
        .where(
            MaterialLinkItem.tenant_id == item.tenant_id,
            MaterialLinkItem.bc_id == item.bc_id,
            MaterialLinkItem.advertiser_id == item.advertiser_id,
            MaterialLinkItem.mid == record.mid,
            MaterialLinkItem.status == "success",
            MaterialLinkItem.id != item.id,
        )
        .order_by(col(MaterialLinkItem.updated_at), col(MaterialLinkItem.id))
        .limit(1)
    ).first()
    item.mid = record.mid
    item.material_id = material.id
    item.actual_file_name = record.file_name
    item.duplicate_of = prior.id if prior else None
    item.status = "success"
    item.error_code = item.error_message = None


def run_link_item(
    *,
    database_engine: Any,
    redis_client: Any,
    context: TenantContext,
    item_id: UUID,
    revision: int,
) -> None:
    claim = uuid4()
    with Session(database_engine) as db, db.begin():
        item = _lock(db, context, item_id)
        if item is None or item.status not in ACTIVE or item.revision != revision:
            return
        if item.lease_until and item.lease_until > now():
            return
        try:
            require_ingest()
            route = load_material_route(
                item.frozen_route, context=context, bc_id=item.bc_id
            )
            verify_route(
                db,
                context=context,
                route=route,
                advertiser_id=item.advertiser_id,
                capability="upload",
            )
        except DomainError as error:
            item.status, item.error_code, item.error_message = (
                "blocked",
                error.code,
                error.message,
            )
            db.add(item)
            return
        item.claim_id, item.lease_until = (
            claim,
            now() + timedelta(seconds=LEASE_SECONDS),
        )
        item.next_run_at = item.lease_until
        snapshot = item.model_copy(deep=True)
        db.add(item)
    deadline = now() + timedelta(seconds=UPLOAD_HARD_LIMIT - 10)
    result: Any = None
    action = (
        "read" if snapshot.status == "verifying" and snapshot.video_id else "search"
    )
    # pending 的第一页精确名称已完整查空后进入独立上传步骤；不会把未知请求重新发送。
    if snapshot.status == "sending" and snapshot.sent_at is None:
        action = "upload"
    operation = {
        "read": "materials.get_videos",
        "search": "materials.search_videos",
        "upload": "materials.upload_video_url",
    }[action]
    sent = False
    failure: Exception | None = None
    try:
        budget_limit = UPLOAD_HARD_LIMIT if action == "upload" else 45
        deadline = min(deadline, now() + timedelta(seconds=budget_limit - 10))
        budget = RemoteCallBudget(
            deadline, budget_limit, admission_policy(operation).lease_ms
        )
        with open_tiktok_gateway(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            route=route,
            task_deadline=deadline,
        ) as gateway:
            if action == "upload":
                with Session(database_engine) as db, db.begin():
                    item = _lock(db, context, item_id)
                    if item is None or item.claim_id != claim:
                        return
                    require_ingest()
                    verify_route(
                        db,
                        context=context,
                        route=route,
                        advertiser_id=item.advertiser_id,
                        capability="upload",
                    )
                    item.sent_at = now()
                    db.add(item)
                sent = True
                result = gateway.materials.upload_video_id(
                    VIDVideoUpload(
                        snapshot.advertiser_id,
                        snapshot.source_video_id,
                        snapshot.requested_file_name,
                    ),
                    budget=budget,
                )
            elif action == "read":
                assert snapshot.video_id is not None
                result = gateway.materials.read_video(
                    advertiser_id=snapshot.advertiser_id,
                    video_id=snapshot.video_id,
                    budget=budget,
                )
            else:
                result = gateway.materials.search_videos(
                    advertiser_id=snapshot.advertiser_id,
                    material_ids=(snapshot.mid,) if snapshot.mid else (),
                    page=snapshot.search_page,
                    video_name=None if snapshot.mid else snapshot.requested_file_name,
                    budget=budget,
                )
    except Exception as error:
        failure = error
    with Session(database_engine) as db, db.begin():
        item = _lock(db, context, item_id)
        if item is None or item.claim_id != claim:
            return
        item.claim_id = item.lease_until = None
        item.updated_at = now()
        delay = 2
        if failure:
            if isinstance(failure, AccountAdmissionDeferred):
                # 准入在请求前拒绝，没有物理副作用；恢复同一步。
                if sent:
                    item.sent_at = None
                delay = max(2, min(300, (failure.retry_after_ms + 999) // 1000))
            elif isinstance(failure, RemoteCallError):
                item.error_code = failure.code
                item.error_message = failure.evidence.remote_message or failure.message
                item.request_id = failure.evidence.request_id
                if action == "upload" and failure.effect in {
                    "NOT_SENT",
                    "REJECTED_NO_EFFECT",
                }:
                    item.sent_at = None
                    item.status = "failed"
                else:
                    item.status = "unknown" if item.sent_at else "pending"
                    item.poll_attempts += 1
                    delay = 30
            elif isinstance(failure, DomainError):
                item.status, item.error_code, item.error_message = (
                    "blocked",
                    failure.code,
                    failure.message,
                )
            else:
                # 未分类传输/进程异常保留发送事实；只提供安全固定文案。
                item.status = "unknown" if item.sent_at else "pending"
                item.error_code, item.error_message = (
                    "link_result_unknown",
                    "本次未取得可确认结果，正在核实",
                )
                item.poll_attempts += 1
                delay = 30
        else:
            try:
                verify_route(
                    db,
                    context=context,
                    route=route,
                    advertiser_id=item.advertiser_id,
                    capability="upload",
                )
                if action == "upload":
                    assert isinstance(result, VideoReceipt)
                    item.video_id, item.mid = result.video_id, result.mid
                    item.request_id = result.evidence.request_id
                    # 无 MID 回执先尝试一次账户 VID；不存在则退回精确名称核实。
                    item.status = (
                        "verifying"
                        if result.video_id and not result.mid
                        else "submitted"
                    )
                    item.search_page = 1
                    item.search_candidate = {}
                elif action == "read":
                    if result is not None:
                        assert isinstance(result, VideoRecord)
                        _register(db, item, result)
                    else:
                        item.status = "submitted"
                        item.poll_attempts += 1
                        delay = 15
                else:
                    assert isinstance(result, MaterialPage)
                    matches = [
                        r
                        for r in result.rows
                        if (
                            r.mid == item.mid
                            if item.mid
                            else r.file_name == item.requested_file_name
                        )
                    ]
                    identities = {(r.video_id, r.mid) for r in matches}
                    if item.search_candidate:
                        identities.add(
                            (
                                item.search_candidate["video_id"],
                                item.search_candidate["mid"],
                            )
                        )
                    if len(identities) > 1:
                        item.status = "needs_review"
                        item.error_message = "匹配到多个平台素材，需要人工核实"
                    elif result.page < result.total_pages:
                        if identities:
                            vid, mid = next(iter(identities))
                            item.search_candidate = {"video_id": vid, "mid": mid}
                        item.search_page += 1
                    elif identities:
                        item.video_id, item.mid = next(iter(identities))
                        item.status = "verifying"
                        item.search_page = 1
                        item.search_candidate = {}
                    elif item.sent_at is None:
                        item.status = "sending"
                        item.search_page = 1
                    else:
                        item.status = "submitted" if item.mid else "unknown"
                        item.search_page = 1
                        item.poll_attempts += 1
                        delay = 15
            except DomainError as error:
                if error.code == "link_material_unverified":
                    item.status = "submitted"
                    item.poll_attempts += 1
                    delay = 15
                else:
                    item.status, item.error_code, item.error_message = (
                        "blocked",
                        error.code,
                        error.message,
                    )
        if item.poll_attempts >= MAX_POLLS and item.status in ACTIVE:
            item.status = "needs_review"
            item.error_message = (
                "多次回查尚未取得可用素材，可继续核实；不会自动重复上传"
            )
        db.add(item)
        if item.status in ACTIVE:
            queue_items(db, context=context, items=[item], delay=delay)


def repair_link_items(session: Session, *, limit: int = 100) -> int:
    due = session.exec(
        select(MaterialLinkItem)
        .where(
            col(MaterialLinkItem.status).in_(ACTIVE),
            MaterialLinkItem.next_run_at <= now(),
        )
        .order_by(col(MaterialLinkItem.next_run_at))
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    count = 0
    for item in due:
        if item.lease_until and item.lease_until > now():
            continue
        batch = session.get(MaterialLinkBatch, item.batch_id)
        if not batch:
            continue
        item.claim_id = item.lease_until = None
        # 发送围栏存在即只读核实；发送前崩溃可继续原上传步骤。
        if item.sent_at and item.status == "sending":
            item.status = "unknown"
        queue_items(
            session,
            context=TenantContext(
                tenant_id=item.tenant_id, actor_id=batch.actor_id, role="operator"
            ),
            items=[item],
        )
        count += 1
    return count
