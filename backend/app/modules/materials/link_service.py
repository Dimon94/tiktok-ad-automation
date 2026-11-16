"""链接批次元数据与幂等；物理上传仅由后台任务执行。"""

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, text
from sqlmodel import Session, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.jobs.outbox import enqueue_many_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.accounts.routing import freeze_route, verify_route
from app.modules.tenants.permissions import require_tenant

from .link_models import MaterialLinkBatch, MaterialLinkItem, now
from .link_schemas import LinkBatchCreate, LinkBatchPublic, LinkItemPublic, parse_links
from .uploads import require_bc

register_dispatch_task("materials.link_item", "resources")
ACTIVE = ("pending", "sending", "submitted", "verifying", "unknown")


def require_ingest() -> None:
    if not settings.MATERIAL_INGEST_ENABLED:
        raise DomainError("material_ingest_disabled", "当前环境未开启素材导入")


def queue_items(
    session: Session,
    *,
    context: TenantContext,
    items: list[MaterialLinkItem],
    delay: int = 0,
) -> None:
    due = now() + timedelta(seconds=delay)
    entries: list[tuple[str, str, dict[str, Any], datetime | None]] = []
    for item in items:
        item.revision += 1
        # watchdog 留足 broker 排队时间；过期后控制面重新唤醒同一个业务任务。
        item.next_run_at = due + timedelta(minutes=5)
        entries.append(
            (
                "materials.link_item",
                f"material-link:{item.id}:{item.revision}",
                {"item_id": str(item.id), "revision": item.revision},
                due,
            )
        )
        session.add(item)
    enqueue_many_after_commit(session, context=context, entries=entries)


def batch_public(session: Session, batch: MaterialLinkBatch) -> LinkBatchPublic:
    counts = dict(
        session.exec(
            select(MaterialLinkItem.status, func.count())
            .where(
                MaterialLinkItem.tenant_id == batch.tenant_id,
                MaterialLinkItem.batch_id == batch.id,
            )
            .group_by(MaterialLinkItem.status)
        ).all()
    )
    return LinkBatchPublic(
        id=batch.id,
        bc_id=batch.bc_id,
        naming_date=batch.naming_date,
        input_count=batch.input_count,
        unique_count=batch.unique_count,
        duplicate_count=batch.input_count - batch.unique_count,
        account_count=batch.account_count,
        total=sum(counts.values()),
        counts=counts,
        created_at=batch.created_at,
    )


def item_public(
    item: MaterialLinkItem, session: Session | None = None
) -> LinkItemPublic:
    duplicate = (
        session.get(MaterialLinkItem, item.duplicate_of)
        if session and item.duplicate_of
        else None
    )
    return LinkItemPublic(
        **item.model_dump(),
        duplicate_line_no=duplicate.line_no
        if duplicate and duplicate.tenant_id == item.tenant_id
        else None,
        can_retry=item.status == "failed",
        can_recheck=item.status in {"needs_review", "blocked"},
    )


def get_batch(
    session: Session, *, context: TenantContext, batch_id: UUID
) -> MaterialLinkBatch:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    batch = session.exec(
        select(MaterialLinkBatch).where(
            MaterialLinkBatch.tenant_id == context.tenant_id,
            MaterialLinkBatch.id == batch_id,
        )
    ).one_or_none()
    if batch is None:
        raise DomainError("upload_batch_not_found", "链接上传批次不存在或不可见")
    require_bc(session, context=context, bc_id=batch.bc_id, action="read")
    return batch


def create_batch(
    session: Session, *, context: TenantContext, body: LinkBatchCreate
) -> MaterialLinkBatch:
    require_ingest()
    require_bc(session, context=context, bc_id=body.bc_id, action="upload")
    parsed = parse_links(body.text)
    unique = [line for line in parsed if line.duplicate_of is None]
    if len(unique) * len(body.advertiser_ids) > 2000:
        raise DomainError(
            "link_input_invalid", "每批最多 2000 条账户上传任务，请减少链接或账户"
        )
    digest = hashlib.sha256(
        json.dumps(
            [body.bc_id, [p.model_dump() for p in parsed], body.advertiser_ids],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    # 相同请求的并发提交串行化；不需要失败后回滚整个调用方事务。
    session.connection().execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {
            "key": int.from_bytes(
                hashlib.sha256(
                    f"{context.tenant_id}:{body.request_id}".encode()
                ).digest()[:8],
                "big",
                signed=True,
            )
        },
    )
    existing = session.exec(
        select(MaterialLinkBatch).where(
            MaterialLinkBatch.tenant_id == context.tenant_id,
            MaterialLinkBatch.request_id == body.request_id,
        )
    ).one_or_none()
    if existing:
        if existing.request_digest != digest:
            raise DomainError(
                "link_request_conflict", "该请求已提交，不能更改链接或目标账户"
            )
        return existing
    route = freeze_route(session, context=context, bc_id=body.bc_id)
    if route.channel != "OFFICIAL_API":
        raise DomainError(
            "material_channel_unverified", "链接上传需要当前 BC 的官方 API 连接"
        )
    for advertiser in body.advertiser_ids:
        verify_route(
            session,
            context=context,
            route=route,
            advertiser_id=advertiser,
            capability="upload",
        )
    date = now().astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    batch = MaterialLinkBatch(
        tenant_id=context.tenant_id,
        bc_id=body.bc_id,
        actor_id=context.actor_id,
        request_id=body.request_id,
        request_digest=digest,
        naming_date=date,
        input_count=len(parsed),
        unique_count=len(unique),
        account_count=len(body.advertiser_ids),
    )
    session.add(batch)
    session.flush()
    items = [
        MaterialLinkItem(
            tenant_id=context.tenant_id,
            bc_id=body.bc_id,
            batch_id=batch.id,
            connection_id=route.connection_id,
            advertiser_id=advertiser,
            frozen_route=route.model_dump(mode="json"),
            line_no=line.line_no,
            source_video_id=line.video_id,
            source_url=line.url,
            requested_file_name=f"{date}_{line.video_id}_{line.line_no:03}.mp4",
        )
        for line in unique
        for advertiser in body.advertiser_ids
    ]
    session.add_all(items)
    session.flush()
    queue_items(session, context=context, items=items)
    return batch


def retry_item(
    session: Session, *, context: TenantContext, batch_id: UUID, item_id: UUID
) -> MaterialLinkItem:
    batch = get_batch(session, context=context, batch_id=batch_id)
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="upload"
    )
    require_ingest()
    item = session.exec(
        select(MaterialLinkItem)
        .where(
            MaterialLinkItem.tenant_id == context.tenant_id,
            MaterialLinkItem.batch_id == batch.id,
            MaterialLinkItem.id == item_id,
        )
        .with_for_update()
    ).one_or_none()
    if item is None:
        raise DomainError("material_not_found", "链接上传条目不存在")
    from .routes import load_material_route

    route = load_material_route(item.frozen_route, context=context, bc_id=item.bc_id)
    verify_route(
        session,
        context=context,
        route=route,
        advertiser_id=item.advertiser_id,
        capability="upload",
    )
    if item.status not in {"failed", "needs_review", "blocked"}:
        raise DomainError("link_retry_unavailable", "该条目正在处理或已完成")
    # 明确失败才允许再次写入；未知/撤权恢复保留发送事实，仅核实原请求。
    if item.status == "failed":
        item.sent_at = None
        item.video_id = item.mid = None
    item.status = "submitted" if item.sent_at else "pending"
    item.error_code = item.error_message = None
    item.poll_attempts = 0
    item.search_page = 1
    item.search_candidate = {}
    item.updated_at = now()
    queue_items(session, context=context, items=[item])
    return item
