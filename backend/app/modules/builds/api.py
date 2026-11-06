from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query
from sqlmodel import select

from app.api.deps import CurrentUser, SessionDep
from app.core.errors import DomainError
from app.core.pagination import Page
from app.modules.builds import catalog, draft_catalog, drafts, mutations, previews
from app.modules.builds.models import (
    BuildDraft,
    DraftPreparation,
    DraftPreparationRequest,
)
from app.modules.builds.preview_schemas import (
    FrozenAd,
    FrozenGroup,
    FrozenUnit,
    PreviewAccepted,
    PreviewDramaPublic,
    PreviewInputPublic,
    PreviewRequest,
    PreviewSummary,
    PreviewUnit,
    SkippedMaterialPublic,
)
from app.modules.builds.schemas import (
    CreateDraftRequest,
    DeliveryChange,
    DraftDramaPublic,
    DraftGroupEditRequest,
    DraftInputPublic,
    DraftListItem,
    DraftMaterialPublic,
    DraftPrepareAccepted,
    DraftPrepareRequest,
    DraftSaved,
    DraftSummary,
    ManualLinkPatch,
    PatchDraftRequest,
)
from app.modules.tenants.permissions import require_tenant

from .identity_selection import (
    ChooseIdentityRequest,
    DraftIdentities,
    choose_identity,
    draft_identities,
)
from .mini_selection import ChooseMiniRequest, DraftMinis, choose_mini, draft_minis
from .structure import ImageStructureChange
from .targeting_schemas import TargetingAccount, TargetingChange, TargetingDirectory

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["builds"])
Cursor = Annotated[str | None, Query(max_length=4096)]
Limit = Annotated[int, Query(ge=1, le=100)]


@router.get("/build-drafts", response_model=Page[DraftListItem])
def list_drafts(
    tenant_id: UUID,
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    session: SessionDep,
    user: CurrentUser,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> Page[DraftListItem]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return draft_catalog.list_drafts(
        session, context=context, bc_id=bc_id, cursor=cursor, limit=limit
    )


@router.post("/build-drafts", response_model=DraftSaved, status_code=201)
def create(
    tenant_id: UUID, body: CreateDraftRequest, session: SessionDep, user: CurrentUser
) -> DraftSaved:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="build"
    )
    identity = drafts.create_draft(session, context=context, **body.model_dump())
    draft = drafts.get_draft(session, context=context, draft_id=identity)
    result = DraftSaved(draft_id=identity, revision=draft.revision)
    session.commit()
    return result


@router.get("/build-draft-requests/{request_id}", response_model=DraftSaved)
def saved_request(
    tenant_id: UUID, request_id: UUID, session: SessionDep, user: CurrentUser
) -> DraftSaved:
    require_tenant(session, actor_id=user.id, tenant_id=tenant_id, action="read")
    row = session.exec(
        select(BuildDraft).where(
            BuildDraft.tenant_id == tenant_id, BuildDraft.request_id == request_id
        )
    ).one_or_none()
    if row is None:
        raise DomainError("draft_not_found", "草稿保存请求尚未找到")
    return DraftSaved(draft_id=row.id, revision=row.revision)


@router.get(
    "/build-preparation-requests/{request_id}", response_model=DraftPrepareAccepted
)
def saved_prepare_request(
    tenant_id: UUID, request_id: UUID, session: SessionDep, user: CurrentUser
) -> DraftPrepareAccepted:
    require_tenant(session, actor_id=user.id, tenant_id=tenant_id, action="read")
    row = session.get(DraftPreparationRequest, (tenant_id, request_id))
    if row is None:
        raise DomainError("draft_not_found", "草稿准备请求尚未找到")
    prep = session.get(DraftPreparation, row.preparation_id)
    assert prep
    return DraftPrepareAccepted(task_id=prep.id, revision=prep.draft_revision)


@router.get("/build-drafts/{draft_id}", response_model=DraftSummary)
def summary(
    tenant_id: UUID, draft_id: UUID, session: SessionDep, user: CurrentUser
) -> DraftSummary:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return catalog.draft_summary(session, context=context, draft_id=draft_id)


@router.patch("/build-drafts/{draft_id}", response_model=DraftSaved)
def update(
    tenant_id: UUID,
    draft_id: UUID,
    body: PatchDraftRequest,
    session: SessionDep,
    user: CurrentUser,
) -> DraftSaved:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="build"
    )
    changes = body.model_dump(exclude_none=True)
    # 显式 null 表示清除草稿偏好；遗漏字段仍表示保持原选择。
    if "execution_connection_id" in body.model_fields_set:
        changes["execution_connection_id"] = body.execution_connection_id
    revision = mutations.update_draft(
        session,
        context=context,
        draft_id=draft_id,
        **changes,
    )
    session.commit()
    return DraftSaved(draft_id=draft_id, revision=revision)


@router.post(
    "/build-drafts/{draft_id}/prepare",
    response_model=DraftPrepareAccepted,
    status_code=202,
)
def prepare(
    tenant_id: UUID,
    draft_id: UUID,
    body: DraftPrepareRequest,
    session: SessionDep,
    user: CurrentUser,
) -> DraftPrepareAccepted:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="build"
    )
    task = drafts.prepare_draft(
        session, context=context, draft_id=draft_id, request_id=body.request_id
    )
    prep = session.get(DraftPreparation, task)
    assert prep
    result = DraftPrepareAccepted(task_id=task, revision=prep.draft_revision)
    session.commit()
    return result


@router.get("/build-drafts/{draft_id}/inputs", response_model=Page[DraftInputPublic])
def inputs(
    tenant_id: UUID,
    draft_id: UUID,
    kind: Literal["drama", "account"],
    session: SessionDep,
    user: CurrentUser,
    status: Annotated[str | None, Query(max_length=32)] = None,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> Page[DraftInputPublic]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return catalog.inputs_page(
        session,
        context=context,
        draft_id=draft_id,
        kind=kind,
        status=status,
        cursor=cursor,
        limit=limit,
    )


@router.get("/build-drafts/{draft_id}/dramas", response_model=Page[DraftDramaPublic])
def dramas(
    tenant_id: UUID,
    draft_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> Page[DraftDramaPublic]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return catalog.dramas_page(
        session, context=context, draft_id=draft_id, cursor=cursor, limit=limit
    )


@router.get(
    "/build-drafts/{draft_id}/dramas/{drama_id}/materials",
    response_model=Page[DraftMaterialPublic],
)
def materials(
    tenant_id: UUID,
    draft_id: UUID,
    drama_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> Page[DraftMaterialPublic]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return catalog.materials_page(
        session,
        context=context,
        draft_id=draft_id,
        drama_id=drama_id,
        cursor=cursor,
        limit=limit,
    )


@router.patch(
    "/build-drafts/{draft_id}/dramas/{drama_id}/groups", response_model=DraftSaved
)
def edit_groups(
    tenant_id: UUID,
    draft_id: UUID,
    drama_id: UUID,
    body: DraftGroupEditRequest,
    session: SessionDep,
    user: CurrentUser,
) -> DraftSaved:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="build"
    )
    revision = mutations.edit_material_groups(
        session,
        context=context,
        draft_id=draft_id,
        drama_id=drama_id,
        expected_revision=body.expected_revision,
        request_id=body.request_id,
        groups=body.groups,
    )
    session.commit()
    return DraftSaved(draft_id=draft_id, revision=revision)


@router.post(
    "/build-drafts/{draft_id}/previews", response_model=PreviewAccepted, status_code=202
)
def generate_preview(
    tenant_id: UUID,
    draft_id: UUID,
    body: PreviewRequest,
    session: SessionDep,
    user: CurrentUser,
) -> PreviewAccepted:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="build"
    )
    identity = previews.generate_preview(
        session,
        context=context,
        draft_id=draft_id,
        expected_revision=body.expected_revision,
        refresh_continuation=body.refresh_continuation,
    )
    session.commit()
    return PreviewAccepted(preview_id=identity)


@router.get(
    "/build-drafts/{draft_id}/previews/{revision}", response_model=PreviewAccepted
)
def preview_request(
    tenant_id: UUID,
    draft_id: UUID,
    revision: int,
    session: SessionDep,
    user: CurrentUser,
) -> PreviewAccepted:
    from app.modules.builds.preview_models import BuildPreview

    require_tenant(session, actor_id=user.id, tenant_id=tenant_id, action="read")
    identity = session.exec(
        select(BuildPreview.id).where(
            BuildPreview.tenant_id == tenant_id,
            BuildPreview.draft_id == draft_id,
            BuildPreview.draft_revision == revision,
        )
    ).one_or_none()
    if identity is None:
        raise DomainError("preview_not_found", "预览请求尚未找到")
    return PreviewAccepted(preview_id=identity)


@router.get("/build-previews/{preview_id}", response_model=PreviewSummary)
def preview_summary(
    tenant_id: UUID, preview_id: UUID, session: SessionDep, user: CurrentUser
) -> PreviewSummary:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return previews.get_preview_summary(session, context=context, preview_id=preview_id)


@router.get("/build-previews/{preview_id}/units", response_model=Page[PreviewUnit])
def preview_units(
    tenant_id: UUID,
    preview_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    cursor: Cursor = None,
    limit: Limit = 50,
    readiness: Literal["READY", "PREPARING", "BLOCKED"] | None = None,
    drama_id: UUID | None = None,
) -> Page[PreviewUnit]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return previews.get_preview_units(
        session,
        context=context,
        preview_id=preview_id,
        cursor=cursor,
        limit=limit,
        readiness=readiness,
        drama_id=drama_id,
    )


@router.get(
    "/build-previews/{preview_id}/inputs", response_model=Page[PreviewInputPublic]
)
def preview_inputs(
    tenant_id: UUID,
    preview_id: UUID,
    kind: Literal["drama", "account"],
    session: SessionDep,
    user: CurrentUser,
    cursor: Cursor = None,
    limit: Limit = 50,
    issues_only: bool = False,
    status: Annotated[str | None, Query(max_length=32)] = None,
) -> Page[PreviewInputPublic]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return previews.get_preview_inputs(
        session,
        context=context,
        preview_id=preview_id,
        kind=kind,
        cursor=cursor,
        limit=limit,
        issues_only=issues_only,
        status=status,
    )


@router.get("/build-units/{unit_id}", response_model=FrozenUnit)
def frozen_unit(
    tenant_id: UUID, unit_id: UUID, session: SessionDep, user: CurrentUser
) -> FrozenUnit:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return previews.load_frozen_unit(session, context=context, unit_id=unit_id)


@router.get("/build-units/{unit_id}/groups", response_model=Page[FrozenGroup])
def frozen_groups(
    tenant_id: UUID,
    unit_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    cursor: Cursor = None,
    limit: Limit = 20,
) -> Page[FrozenGroup]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return previews.get_frozen_groups(
        session, context=context, unit_id=unit_id, cursor=cursor, limit=limit
    )


@router.get(
    "/build-units/{unit_id}/groups/{group_id}/ads", response_model=Page[FrozenAd]
)
def frozen_ads(
    tenant_id: UUID,
    unit_id: UUID,
    group_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    cursor: Cursor = None,
    limit: Annotated[int, Query(ge=1, le=5)] = 2,
) -> Page[FrozenAd]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return previews.get_frozen_ads(
        session,
        context=context,
        unit_id=unit_id,
        group_id=group_id,
        cursor=cursor,
        limit=limit,
    )


@router.get(
    "/build-units/{unit_id}/skipped-materials",
    response_model=Page[SkippedMaterialPublic],
)
def skipped_materials(
    tenant_id: UUID,
    unit_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> Page[SkippedMaterialPublic]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return previews.get_skipped_materials(
        session, context=context, unit_id=unit_id, cursor=cursor, limit=limit
    )


@router.get("/build-mutation-requests/{request_id}", response_model=DraftSaved)
def saved_mutation(
    tenant_id: UUID, request_id: UUID, session: SessionDep, user: CurrentUser
) -> DraftSaved:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return mutations.saved_mutation(session, context=context, request_id=request_id)


@router.get(
    "/build-previews/{preview_id}/dramas", response_model=Page[PreviewDramaPublic]
)
def preview_dramas(
    tenant_id: UUID,
    preview_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> Page[PreviewDramaPublic]:
    from app.modules.builds.preview_catalog import get_preview_dramas

    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return get_preview_dramas(
        session, context=context, preview_id=preview_id, cursor=cursor, limit=limit
    )


@router.get("/build-drafts/{draft_id}/minis", response_model=DraftMinis)
def minis_options(
    tenant_id: UUID,
    draft_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    page: int = Query(default=1, ge=1, le=1000),
    query: str | None = Query(default=None, max_length=255),
) -> DraftMinis:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return draft_minis(
        session, context=context, draft_id=draft_id, page=page, query=query
    )


@router.post("/build-drafts/{draft_id}/minis", response_model=DraftSaved)
def select_mini(
    tenant_id: UUID,
    draft_id: UUID,
    body: ChooseMiniRequest,
    session: SessionDep,
    user: CurrentUser,
) -> DraftSaved:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="build"
    )
    revision = choose_mini(session, context=context, draft_id=draft_id, body=body)
    session.commit()
    return DraftSaved(draft_id=draft_id, revision=revision)


@router.post("/build-drafts/{draft_id}/minis/refresh")
def refresh_minis_catalog(
    tenant_id: UUID,
    draft_id: UUID,
    session: SessionDep,
    user: CurrentUser,
) -> dict[str, int]:
    """显式过期账户场景目录缓存，下一轮准备重新拉取；普通查询只读本地。"""
    from .mini_selection import expire_draft_scene_catalog

    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="build"
    )
    count = expire_draft_scene_catalog(session, context=context, draft_id=draft_id)
    session.commit()
    return {"expired": count}


@router.post("/build-drafts/{draft_id}/identities/refresh")
def refresh_identities_catalog(
    tenant_id: UUID,
    draft_id: UUID,
    session: SessionDep,
    user: CurrentUser,
) -> dict[str, int]:
    """显式过期投放身份所属场景快照，下一轮准备重新读取平台目录。"""
    return refresh_minis_catalog(tenant_id, draft_id, session, user)


@router.get("/build-drafts/{draft_id}/identities", response_model=DraftIdentities)
def identity_options(
    tenant_id: UUID,
    draft_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    query: str | None = Query(default=None, max_length=255),
) -> DraftIdentities:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return draft_identities(session, context=context, draft_id=draft_id, query=query)


@router.post("/build-drafts/{draft_id}/identities", response_model=DraftSaved)
def select_identity(
    tenant_id: UUID,
    draft_id: UUID,
    body: ChooseIdentityRequest,
    session: SessionDep,
    user: CurrentUser,
) -> DraftSaved:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="build"
    )
    revision = choose_identity(session, context=context, draft_id=draft_id, body=body)
    session.commit()
    return DraftSaved(draft_id=draft_id, revision=revision)


@router.put(
    "/build-drafts/{draft_id}/inputs/{input_id}/manual-link", response_model=DraftSaved
)
def put_manual_link(
    tenant_id: UUID,
    draft_id: UUID,
    input_id: UUID,
    body: ManualLinkPatch,
    session: SessionDep,
    user: CurrentUser,
) -> DraftSaved:
    from .manual_links import save_manual_link

    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="build"
    )
    revision = save_manual_link(
        session,
        context=context,
        draft_id=draft_id,
        input_id=input_id,
        expected_revision=body.expected_revision,
        request_id=body.request_id,
        link=body.link.model_dump(),
    )
    session.commit()
    return DraftSaved(draft_id=draft_id, revision=revision)


@router.get("/targeting/regions", response_model=TargetingDirectory)
def targeting_regions(
    tenant_id: UUID,
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    session: SessionDep,
    user: CurrentUser,
) -> TargetingDirectory:
    from app.modules.accounts.routing import freeze_route

    from .targeting_directory import region_directory

    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    route = freeze_route(session, context=context, bc_id=bc_id)
    return region_directory(session, context=context, route=route)


@router.get("/build-drafts/{draft_id}/targeting", response_model=TargetingDirectory)
def targeting_options(
    tenant_id: UUID, draft_id: UUID, session: SessionDep, user: CurrentUser
) -> TargetingDirectory:
    from .targeting_directory import draft_directory

    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return draft_directory(session, context=context, draft_id=draft_id)


@router.get(
    "/build-drafts/{draft_id}/targeting/accounts", response_model=Page[TargetingAccount]
)
def targeting_accounts(
    tenant_id: UUID,
    draft_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    cursor: Annotated[str, Query(max_length=128)] = "",
    limit: Limit = 50,
) -> Page[TargetingAccount]:
    from .targeting_directory import account_regions

    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return account_regions(
        session, context=context, draft_id=draft_id, after=cursor, limit=limit
    )


@router.patch("/build-drafts/{draft_id}/targeting", response_model=DraftSaved)
def update_targeting(
    tenant_id: UUID,
    draft_id: UUID,
    body: TargetingChange,
    session: SessionDep,
    user: CurrentUser,
) -> DraftSaved:
    from .targeting_service import save_targeting

    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="build"
    )
    revision = save_targeting(session, context=context, draft_id=draft_id, body=body)
    session.commit()
    return DraftSaved(draft_id=draft_id, revision=revision)


@router.patch("/build-drafts/{draft_id}/delivery", response_model=DraftSaved)
def update_delivery(
    tenant_id: UUID,
    draft_id: UUID,
    body: DeliveryChange,
    session: SessionDep,
    user: CurrentUser,
) -> DraftSaved:
    from .delivery_service import save_delivery

    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="build"
    )
    revision = save_delivery(session, context=context, draft_id=draft_id, body=body)
    session.commit()
    return DraftSaved(draft_id=draft_id, revision=revision)


# 图片编排使用独立的类型化 API，旧视频素材组保持原合同。
from .image_composition_api import router as image_composition_router  # noqa: E402

router.include_router(image_composition_router)
from .carousel_music_api import router as carousel_music_router  # noqa: E402

router.include_router(carousel_music_router)


@router.put("/build-drafts/{draft_id}/structure", response_model=DraftSaved)
def update_image_structure(
    tenant_id: UUID,
    draft_id: UUID,
    body: ImageStructureChange,
    session: SessionDep,
    user: CurrentUser,
) -> DraftSaved:
    from .structure import save_image_structure

    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="build"
    )
    revision = save_image_structure(
        session, context=context, draft_id=draft_id, body=body
    )
    session.commit()
    return DraftSaved(draft_id=draft_id, revision=revision)
