"""租户链接上传入口；分页详情不回传凭据或临时播放地址。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from sqlmodel import col, select

from app.api.deps import CurrentUser, SessionDep
from app.core.pagination import Page, count_rows
from app.modules.tenants.permissions import require_tenant

from .link_models import MaterialLinkBatch, MaterialLinkItem
from .link_schemas import LinkBatchCreate, LinkBatchPublic, LinkItemPublic
from .link_service import batch_public, create_batch, get_batch, item_public, retry_item
from .uploads import require_bc

router = APIRouter(
    prefix="/tenants/{tenant_id}/materials/link-uploads", tags=["material-links"]
)
Offset = Annotated[int, Query(ge=0, le=1000000)]
Limit = Annotated[int, Query(ge=1, le=100)]


@router.post(
    "",
    response_model=LinkBatchPublic,
    status_code=201,
    operation_id="create_link_upload",
)
def create_link_upload(
    tenant_id: UUID, body: LinkBatchCreate, session: SessionDep, user: CurrentUser
) -> LinkBatchPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="upload"
    )
    row = create_batch(session, context=context, body=body)
    result = batch_public(session, row)
    session.commit()
    return result


@router.get("", response_model=Page[LinkBatchPublic], operation_id="list_link_uploads")
def list_link_uploads(
    tenant_id: UUID,
    bc_id: str,
    session: SessionDep,
    user: CurrentUser,
    offset: Offset = 0,
    limit: Limit = 20,
) -> Page[LinkBatchPublic]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    require_bc(session, context=context, bc_id=bc_id, action="read")
    query = select(MaterialLinkBatch).where(
        MaterialLinkBatch.tenant_id == tenant_id, MaterialLinkBatch.bc_id == bc_id
    )
    total = count_rows(session, query)
    rows = session.exec(
        query.order_by(
            col(MaterialLinkBatch.created_at).desc(), col(MaterialLinkBatch.id).desc()
        )
        .offset(offset)
        .limit(limit)
    ).all()
    return Page(items=[batch_public(session, row) for row in rows], total=total)


@router.get(
    "/material-items/{material_id}",
    response_model=Page[LinkItemPublic],
    operation_id="list_material_link_sources",
)
def list_material_link_sources(
    tenant_id: UUID,
    material_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    offset: Offset = 0,
    limit: Limit = 50,
) -> Page[LinkItemPublic]:
    from app.core.errors import DomainError

    from .models import MaterialFile

    require_tenant(session, actor_id=user.id, tenant_id=tenant_id, action="read")
    material = session.get(MaterialFile, material_id)
    if material is None or material.tenant_id != tenant_id:
        raise DomainError("material_not_found", "素材不存在或不可见")
    query = select(MaterialLinkItem).where(
        MaterialLinkItem.tenant_id == tenant_id,
        MaterialLinkItem.material_id == material_id,
    )
    total = count_rows(session, query)
    rows = session.exec(
        query.order_by(
            col(MaterialLinkItem.updated_at).desc(), col(MaterialLinkItem.id)
        )
        .offset(offset)
        .limit(limit)
    ).all()
    return Page(items=[item_public(row, session) for row in rows], total=total)


@router.get(
    "/{batch_id}", response_model=LinkBatchPublic, operation_id="read_link_upload"
)
def read_link_upload(
    tenant_id: UUID, batch_id: UUID, session: SessionDep, user: CurrentUser
) -> LinkBatchPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return batch_public(session, get_batch(session, context=context, batch_id=batch_id))


@router.get(
    "/{batch_id}/items",
    response_model=Page[LinkItemPublic],
    operation_id="list_link_upload_items",
)
def list_link_upload_items(
    tenant_id: UUID,
    batch_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    offset: Offset = 0,
    limit: Limit = 50,
) -> Page[LinkItemPublic]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    get_batch(session, context=context, batch_id=batch_id)
    query = select(MaterialLinkItem).where(
        MaterialLinkItem.tenant_id == tenant_id, MaterialLinkItem.batch_id == batch_id
    )
    total = count_rows(session, query)
    rows = session.exec(
        query.order_by(
            col(MaterialLinkItem.line_no),
            col(MaterialLinkItem.advertiser_id),
            col(MaterialLinkItem.id),
        )
        .offset(offset)
        .limit(limit)
    ).all()
    return Page(items=[item_public(row, session) for row in rows], total=total)


@router.post(
    "/{batch_id}/items/{item_id}/retry",
    response_model=LinkItemPublic,
    operation_id="retry_link_upload_item",
)
def retry_link_upload_item(
    tenant_id: UUID,
    batch_id: UUID,
    item_id: UUID,
    session: SessionDep,
    user: CurrentUser,
) -> LinkItemPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="upload"
    )
    result = item_public(
        retry_item(session, context=context, batch_id=batch_id, item_id=item_id),
        session,
    )
    session.commit()
    return result
