import hashlib
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Response
from redis import Redis
from sqlalchemy import and_, case, func, or_
from sqlmodel import Session, col, select

from app.api.deps import CurrentUser, SessionDep
from app.core.config import settings
from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.core.pagination import Page, count_rows
from app.modules.materials import carousel_api, catalog
from app.modules.materials.content_identity import content_key
from app.modules.materials.models import (
    AccountMaterial,
    MaterialFile,
    MaterialUploadAttempt,
    ObjectUpload,
)
from app.modules.materials.repository import (
    asset_public,
    decode_material_created_cursor,
    decode_material_cursor,
    deduplicate_material_statement,
    encode_material_created_cursor,
    encode_material_cursor,
)
from app.modules.materials.schemas import (
    AccountAsset,
    CompleteUploadRequest,
    MaterialPublic,
    RemoteMaterialPreview,
    SignedPart,
    SignedPreview,
    UploadAttemptPublic,
    UploadBatchRequest,
    UploadBatchResult,
    UploadBatchSummary,
    UploadCompleted,
    UploadFileResult,
)
from app.modules.materials.source_uploads import request_source_retry
from app.modules.materials.storage import storage_error
from app.modules.materials.uploads import (
    complete_object_upload,
    get_upload_batch,
    get_upload_file_result,
    public_error,
    retry_object_upload,
    sign_upload_part,
    start_upload_batch,
)
from app.modules.tenants.permissions import require_tenant

router = APIRouter(prefix="/tenants/{tenant_id}/materials", tags=["materials"])
router.include_router(carousel_api.router)
BCID = Annotated[str, Query(min_length=1, max_length=128)]
Limit = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[str | None, Query(max_length=8192)]


@router.post("/upload-batches", response_model=UploadBatchResult, status_code=201)
def post_upload_batch(
    tenant_id: UUID, body: UploadBatchRequest, session: SessionDep, user: CurrentUser
) -> UploadBatchResult:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="upload"
    )
    result = start_upload_batch(
        session,
        context=context,
        bc_id=body.bc_id,
        files=body.files,
        request_id=body.request_id,
    )
    session.commit()
    return result


@router.get("/upload-requests/{request_id}", response_model=UploadBatchResult)
def read_upload_request(
    tenant_id: UUID,
    request_id: UUID,
    bc_id: BCID,
    session: SessionDep,
    user: CurrentUser,
) -> UploadBatchResult:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return catalog.find_upload_request(
        session, context=context, bc_id=bc_id, request_id=request_id
    )


@router.get("/upload-batches", response_model=Page[UploadBatchSummary])
def read_upload_batches(
    tenant_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> Page[UploadBatchSummary]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return catalog.upload_batches_page(
        session, context=context, cursor=cursor, limit=limit
    )


@router.get("/{material_id}/preview", response_model=SignedPreview)
def read_original_preview(
    tenant_id: UUID,
    material_id: UUID,
    response: Response,
    session: SessionDep,
    user: CurrentUser,
) -> SignedPreview:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    result = catalog.original_preview(session, context=context, material_id=material_id)
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/{material_id}/remote-preview", response_model=RemoteMaterialPreview)
def read_remote_preview(
    tenant_id: UUID,
    material_id: UUID,
    response: Response,
    session: SessionDep,
    user: CurrentUser,
    bc_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
) -> RemoteMaterialPreview:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    # End request authentication's transaction before the admitted external read.
    session.commit()
    session.close()
    with Redis.from_url(settings.REDIS_URL) as redis_client:
        result = catalog.remote_preview(
            database_engine=engine,
            redis_client=redis_client,
            context=context,
            material_id=material_id,
            bc_id=bc_id,
        )
    response.headers["Cache-Control"] = "no-store"
    return result


# 静态路径须注册在 /{material_id} 之前，否则会被素材 ID 参数段先匹配。
@router.get("/account-video-preview", response_model=RemoteMaterialPreview)
def read_account_video_preview(
    tenant_id: UUID,
    response: Response,
    session: SessionDep,
    user: CurrentUser,
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    video_id: Annotated[str, Query(min_length=1, max_length=128)],
    advertiser_ids: Annotated[str, Query(min_length=1, max_length=2600)],
) -> RemoteMaterialPreview:
    """广告报表素材详情点"预览成片"时调用；候选账户来自该素材的使用位置。"""
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    candidates = tuple(
        dict.fromkeys(
            item.strip() for item in advertiser_ids.split(",") if item.strip()
        )
    )
    # 候选账户数量有上限：只是为了在其中挑一个有授权的账户，不需要无限列表。
    if not candidates or len(candidates) > 20:
        raise DomainError("material_request_invalid", "候选账户数量须为 1 到 20 个")
    # 先结束请求鉴权的事务，再进入受准入控制的平台读取。
    session.commit()
    session.close()
    with Redis.from_url(settings.REDIS_URL) as redis_client:
        result = catalog.account_video_preview(
            database_engine=engine,
            redis_client=redis_client,
            context=context,
            bc_id=bc_id,
            video_id=video_id,
            advertiser_ids=candidates,
        )
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/upload-batches/{batch_id}", response_model=UploadBatchResult)
def read_upload_batch(
    tenant_id: UUID, batch_id: UUID, session: SessionDep, user: CurrentUser
) -> UploadBatchResult:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return get_upload_batch(session, context=context, batch_id=batch_id)


@router.post(
    "/{material_id}/upload-parts/{part_number}/sign", response_model=SignedPart
)
def post_part_signature(
    tenant_id: UUID,
    material_id: UUID,
    part_number: int,
    response: Response,
    session: SessionDep,
    user: CurrentUser,
) -> SignedPart:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="upload"
    )
    session.commit()
    response.headers["Cache-Control"] = "no-store"
    return sign_upload_part(
        database_engine=engine,
        context=context,
        material_id=material_id,
        part_number=part_number,
    )


@router.post("/{material_id}/complete", response_model=UploadCompleted)
def post_complete(
    tenant_id: UUID,
    material_id: UUID,
    body: CompleteUploadRequest,
    session: SessionDep,
    user: CurrentUser,
) -> UploadCompleted:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="upload"
    )
    session.commit()
    task_id = complete_object_upload(
        database_engine=engine,
        context=context,
        material_id=material_id,
        parts=body.parts,
    )
    return UploadCompleted(task_id=task_id)


def _catalog_statement(context: TenantContext) -> tuple[Any, Any]:
    count = (
        select(func.count(col(AccountMaterial.id)))
        .where(
            AccountMaterial.tenant_id == context.tenant_id,
            AccountMaterial.material_id == MaterialFile.id,
            AccountMaterial.status == "available",
        )
        .correlate(MaterialFile)
        .scalar_subquery()
    )
    from .link_models import MaterialLinkItem

    link_account = (
        select(MaterialLinkItem.advertiser_id)
        .where(
            MaterialLinkItem.tenant_id == context.tenant_id,
            MaterialLinkItem.material_id == MaterialFile.id,
            MaterialLinkItem.status == "success",
        )
        .order_by(col(MaterialLinkItem.updated_at).desc(), col(MaterialLinkItem.id))
        .limit(1)
        .correlate(MaterialFile)
        .scalar_subquery()
    )
    link_bc = (
        select(MaterialLinkItem.bc_id)
        .where(
            MaterialLinkItem.tenant_id == context.tenant_id,
            MaterialLinkItem.material_id == MaterialFile.id,
            MaterialLinkItem.status == "success",
        )
        .order_by(col(MaterialLinkItem.updated_at).desc(), col(MaterialLinkItem.id))
        .limit(1)
        .correlate(MaterialFile)
        .scalar_subquery()
    )
    latest = (
        select(MaterialUploadAttempt.status)
        .where(
            MaterialUploadAttempt.tenant_id == context.tenant_id,
            MaterialUploadAttempt.material_id == MaterialFile.id,
        )
        .order_by(
            col(MaterialUploadAttempt.created_at).desc(),
            col(MaterialUploadAttempt.id).desc(),
        )
        .limit(1)
        .correlate(MaterialFile)
        .scalar_subquery()
    )
    advertiser = (
        select(MaterialUploadAttempt.advertiser_id)
        .where(
            MaterialUploadAttempt.tenant_id == context.tenant_id,
            MaterialUploadAttempt.material_id == MaterialFile.id,
        )
        .order_by(
            col(MaterialUploadAttempt.created_at).desc(),
            col(MaterialUploadAttempt.id).desc(),
        )
        .limit(1)
        .correlate(MaterialFile)
        .scalar_subquery()
    )
    latest_bc = (
        select(MaterialUploadAttempt.bc_id)
        .where(
            MaterialUploadAttempt.tenant_id == context.tenant_id,
            MaterialUploadAttempt.material_id == MaterialFile.id,
        )
        .order_by(
            col(MaterialUploadAttempt.created_at).desc(),
            col(MaterialUploadAttempt.id).desc(),
        )
        .limit(1)
        .correlate(MaterialFile)
        .scalar_subquery()
    )
    object_state = (
        select(ObjectUpload.status)
        .where(
            ObjectUpload.tenant_id == context.tenant_id,
            ObjectUpload.material_id == MaterialFile.id,
        )
        .correlate(MaterialFile)
        .scalar_subquery()
    )
    stage = case(
        (count > 0, "available"),
        (latest == "uploading", "uploading"),
        (latest == "verifying", "verifying"),
        (latest == "result_unknown", "result_unknown"),
        (latest.in_(["blocked", "failed"]), "blocked"),
        (object_state == "result_unknown", "result_unknown"),
        (object_state.in_(["blocked", "failed"]), "blocked"),
        (object_state == "completing", "verifying"),
        (col(MaterialFile.storage_state) == "stored", "stored"),
        else_="receiving",
    )
    return select(
        MaterialFile, stage, count, func.coalesce(link_account, advertiser)
    ).add_columns(func.coalesce(link_bc, latest_bc)).where(
        col(MaterialFile.tenant_id) == context.tenant_id
    ), stage


def _material_public(record: Any) -> MaterialPublic:
    file, stage, count, advertiser, latest_bc = record
    return MaterialPublic(
        material_id=file.id,
        content_key=content_key(file),
        bc_id=file.bc_id,
        file_name=file.file_name,
        byte_size=file.byte_size,
        mime_type=file.mime_type,
        media_type=file.media_type,
        duration=file.duration,
        width=file.width,
        height=file.height,
        created_at=file.created_at,
        original_available=file.storage_state == "stored",
        status=stage,
        available_account_count=count,
        latest_advertiser_id=advertiser,
        latest_bc_id=latest_bc,
    )


@router.get("", response_model=Page[MaterialPublic])
def get_materials(
    tenant_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    query: Annotated[str, Query(max_length=1000)] = "",
    media_type: Literal["VIDEO", "IMAGE"] | None = None,
    status: Annotated[
        str | None,
        Query(
            pattern="^(receiving|stored|uploading|verifying|available|blocked|result_unknown)$"
        ),
    ] = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> Page[MaterialPublic]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    if any(value and value.tzinfo is None for value in (created_from, created_to)) or (
        created_from and created_to and created_from > created_to
    ):
        raise storage_error("invalid_file")
    query = query.strip().casefold()
    scope = {
        "kind": "material-directory",
        "tenant": str(tenant_id),
        "query": hashlib.sha256(query.encode()).hexdigest(),
        "status": status or "",
        "media_type": media_type or "",
        "from": created_from.isoformat() if created_from else "",
        "to": created_to.isoformat() if created_to else "",
    }
    statement, stage = _catalog_statement(context)
    if media_type:
        statement = statement.where(MaterialFile.media_type == media_type)
    if query:
        statement = statement.where(
            col(MaterialFile.file_name_folded).contains(query, autoescape=True)
        )
    if status:
        statement = statement.where(stage == status)
    if created_from:
        statement = statement.where(MaterialFile.created_at >= created_from)
    if created_to:
        statement = statement.where(MaterialFile.created_at <= created_to)
    # 内容别名先按筛选保留名称，再在 SQL 分页前选稳定代表。
    statement = deduplicate_material_statement(statement)
    total = count_rows(session, statement)
    if cursor:
        created_at_value, name, identity = decode_material_created_cursor(
            cursor, scope=scope
        )
        name_order = col(MaterialFile.file_name).collate("C")
        statement = statement.where(
            or_(
                col(MaterialFile.created_at) < created_at_value,
                and_(
                    col(MaterialFile.created_at) == created_at_value,
                    or_(
                        name_order > name,
                        and_(name_order == name, col(MaterialFile.id) > identity),
                    ),
                ),
            )
        )
    rows = session.exec(
        statement.order_by(
            col(MaterialFile.created_at).desc(),
            col(MaterialFile.file_name).collate("C"),
            col(MaterialFile.id),
        )
        .limit(limit + 1)
        .execution_options(populate_existing=True)
    ).all()
    next_cursor = (
        encode_material_created_cursor(
            scope=scope,
            created_at=rows[limit - 1][0].created_at.isoformat(),
            name=rows[limit - 1][0].file_name,
            identity=rows[limit - 1][0].id,
        )
        if len(rows) > limit
        else None
    )
    return Page(
        items=[_material_public(row) for row in rows[:limit]],
        next_cursor=next_cursor,
        total=total,
    )


def _detail(
    session: Session, context: TenantContext, material_id: UUID
) -> MaterialPublic:
    statement, _ = _catalog_statement(context)
    row = session.exec(
        statement.where(MaterialFile.id == material_id).execution_options(
            populate_existing=True
        )
    ).one_or_none()
    if row is None:
        raise storage_error("material_not_found")
    return _material_public(row)


@router.get("/{material_id}", response_model=MaterialPublic)
def get_material(
    tenant_id: UUID,
    material_id: UUID,
    session: SessionDep,
    user: CurrentUser,
) -> MaterialPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return _detail(session, context, material_id)


@router.get("/{material_id}/assets", response_model=Page[AccountAsset])
def get_assets(
    tenant_id: UUID,
    material_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> Page[AccountAsset]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    _detail(session, context, material_id)
    scope = {
        "kind": "material-assets",
        "tenant": str(tenant_id),
        "material": str(material_id),
    }
    statement = select(AccountMaterial).where(
        AccountMaterial.tenant_id == tenant_id,
        AccountMaterial.material_id == material_id,
    )
    total = count_rows(session, statement)
    if cursor:
        _, identity = decode_material_cursor(cursor, scope=scope)
        statement = statement.where(AccountMaterial.id > identity)
    rows = session.exec(
        statement.order_by(col(AccountMaterial.id))
        .limit(limit + 1)
        .execution_options(populate_existing=True)
    ).all()
    return Page(
        items=[asset_public(row) for row in rows[:limit]],
        next_cursor=encode_material_cursor(
            scope=scope, name="", identity=rows[limit - 1].id
        )
        if len(rows) > limit
        else None,
        total=total,
    )


@router.get("/{material_id}/attempts", response_model=Page[UploadAttemptPublic])
def get_attempts(
    tenant_id: UUID,
    material_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> Page[UploadAttemptPublic]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    _detail(session, context, material_id)
    scope = {
        "kind": "material-attempts",
        "tenant": str(tenant_id),
        "material": str(material_id),
    }
    statement = select(MaterialUploadAttempt).where(
        MaterialUploadAttempt.tenant_id == tenant_id,
        MaterialUploadAttempt.material_id == material_id,
    )
    total = count_rows(session, statement)
    if cursor:
        _, identity = decode_material_cursor(cursor, scope=scope)
        statement = statement.where(MaterialUploadAttempt.id > identity)
    rows = session.exec(
        statement.order_by(col(MaterialUploadAttempt.id))
        .limit(limit + 1)
        .execution_options(populate_existing=True)
    ).all()
    return Page(
        items=[
            UploadAttemptPublic(
                attempt_id=row.id,
                material_id=row.material_id,
                bc_id=row.bc_id,
                advertiser_id=row.advertiser_id,
                connection_id=row.connection_id,
                status=row.status
                if row.status
                in {
                    "pending",
                    "uploading",
                    "verifying",
                    "available",
                    "blocked",
                    "result_unknown",
                    "failed",
                }
                else "blocked",
                created_at=row.created_at,
                error_code=public_error(row.remote_response.get("error_code")),
            )
            for row in rows[:limit]
        ],
        next_cursor=encode_material_cursor(
            scope=scope, name="", identity=rows[limit - 1].id
        )
        if len(rows) > limit
        else None,
        total=total,
    )


@router.post("/{material_id}/retry", response_model=UploadFileResult)
def post_object_retry(
    tenant_id: UUID, material_id: UUID, session: SessionDep, user: CurrentUser
) -> UploadFileResult:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="upload"
    )
    file = session.exec(
        select(MaterialFile)
        .where(MaterialFile.tenant_id == tenant_id, MaterialFile.id == material_id)
        .execution_options(populate_existing=True)
    ).one_or_none()
    if file is None:
        raise storage_error("material_not_found")
    if file.storage_state == "stored":
        before = get_upload_file_result(
            session, context=context, material_id=material_id
        )
        if not before.can_retry:
            raise storage_error("upload_not_retryable")
        task_id = request_source_retry(
            session, context=context, material_id=material_id
        )
        upload = session.exec(
            select(ObjectUpload).where(
                ObjectUpload.tenant_id == tenant_id,
                ObjectUpload.material_id == material_id,
            )
        ).one()
        upload.task_id = task_id
        session.add(upload)
        session.flush()
        result = get_upload_file_result(
            session, context=context, material_id=material_id
        )
        session.commit()
        return result
    session.commit()
    return retry_object_upload(
        database_engine=engine, context=context, material_id=material_id
    )
