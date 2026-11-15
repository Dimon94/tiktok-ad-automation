"""Tenant-scoped SQL pagination. Reads never schedule or advance preparation."""

from collections.abc import Mapping
from typing import Literal, cast
from typing import cast as typing_cast
from uuid import UUID

from sqlalchemy import and_, func
from sqlalchemy.orm import aliased
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.core.pagination import Page, count_rows
from app.modules.accounts.resolver import decode_cursor, encode_cursor
from app.modules.builds.draft_catalog import current_image_drama_clause
from app.modules.builds.drafts import get_draft
from app.modules.builds.image_composition_models import (
    DraftCreativeUnit,
    DraftCreativeUnitItem,
)
from app.modules.builds.models import (
    DraftAccount,
    DraftDrama,
    DraftInput,
    DraftPreparation,
)
from app.modules.builds.schemas import (
    DraftDramaPublic,
    DraftInputPreparation,
    DraftInputPublic,
    DraftMaterialPublic,
    DraftSummary,
)
from app.modules.materials.content_identity import content_key
from app.modules.materials.models import MaterialFile
from app.modules.providers.models import (
    LinkPreparationItem,
    PromotionLink,
    ProviderConnection,
    ProviderDrama,
)
from app.modules.providers.schemas import display_config
from app.modules.strategies.service import get_version


def draft_summary(
    session: Session, *, context: TenantContext, draft_id: UUID
) -> DraftSummary:
    row = get_draft(session, context=context, draft_id=draft_id)

    counts: dict[str, dict[str, int]] = {"drama": {}, "account": {}}
    for kind, status, count in session.exec(
        select(col(DraftInput.kind), col(DraftInput.status), func.count())
        .where(
            col(DraftInput.tenant_id) == context.tenant_id,
            col(DraftInput.draft_id) == draft_id,
        )
        .group_by(col(DraftInput.kind), col(DraftInput.status))
    ):
        counts[kind][status] = count
    prep = session.exec(
        select(DraftPreparation)
        .where(
            col(DraftPreparation.tenant_id) == context.tenant_id,
            col(DraftPreparation.draft_id) == draft_id,
        )
        .order_by(col(DraftPreparation.draft_revision).desc())
        .limit(1)
    ).first()
    drama_count = session.exec(
        select(func.count())
        .select_from(DraftDrama)
        .where(
            col(DraftDrama.tenant_id) == context.tenant_id,
            col(DraftDrama.draft_id) == draft_id,
            current_image_drama_clause() if row.media_kind == "CAROUSEL" else True,
        )
    ).one()
    account_count = session.exec(
        select(func.count())
        .select_from(DraftAccount)
        .where(
            col(DraftAccount.tenant_id) == context.tenant_id,
            col(DraftAccount.draft_id) == draft_id,
        )
    ).one()
    provider = session.get(ProviderConnection, row.provider_connection_id)
    return DraftSummary(
        structure_override=row.structure_override or {},
        media_kind=row.media_kind,
        targeting=row.targeting_override
        or get_version(
            session,
            context=context,
            version_id=typing_cast(UUID, row.strategy_version_id),
        ).targeting,
        targeting_override=row.targeting_override,
        draft_id=row.id,
        revision=row.revision,
        bc_id=row.bc_id,
        status=cast(Literal["DRAFT", "PREPARING", "READY", "BLOCKED"], row.status),
        strategy_version_id=typing_cast(UUID, row.strategy_version_id),
        provider_connection_id=typing_cast(UUID, row.provider_connection_id),
        execution_connection_id=row.execution_connection_id,
        application_id=typing_cast(str, row.application_id),
        custom_provider_name=provider.display_name
        if provider and provider.kind == "other"
        else None,
        provider_kind=provider.kind if provider else "",
        link_config=display_config(row.link_config),
        delivery_config=row.delivery_config,
        input_counts=counts,
        drama_count=drama_count,
        account_count=account_count,
        task_id=prep.id if prep else None,
        provider_task_id=prep.provider_task_id if prep else None,
        # 返回实际阶段，不能让尚未开始的素材匹配掩盖账户核对中的等待。
        preparation_phase=cast(
            Literal["accounts", "links", "materials", "scenes", "done"] | None,
            prep.phase if prep and prep.draft_revision == row.revision else None,
        ),
        error_code=prep.error_code
        if prep and prep.draft_revision == row.revision
        else None,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _page(
    scope: Mapping[str, str | None], cursor: str | None, limit: int
) -> str | None:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise DomainError("invalid_page_size", "每页最多 100 条")
    return decode_cursor(cursor, scope=scope)


def inputs_page(
    session: Session,
    *,
    context: TenantContext,
    draft_id: UUID,
    kind: Literal["drama", "account"],
    status: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> Page[DraftInputPublic]:
    draft = get_draft(session, context=context, draft_id=draft_id)
    scope = {
        "tenant": str(context.tenant_id),
        "draft": str(draft_id),
        "revision": str(draft.revision),
        "view": "inputs",
        "kind": kind,
        "status": status,
    }
    after = _page(scope, cursor, limit)
    try:
        number = int(after) if after else 0
        if number < 0:
            raise ValueError
    except ValueError:
        raise DomainError("invalid_cursor", "分页游标无效") from None
    query = select(DraftInput).where(
        col(DraftInput.tenant_id) == context.tenant_id,
        col(DraftInput.draft_id) == draft_id,
        col(DraftInput.kind) == kind,
    )
    if status is not None:
        query = query.where(col(DraftInput.status) == status)
    total = count_rows(session, query)
    query = query.where(col(DraftInput.line_no) > number)
    rows = session.exec(query.order_by(col(DraftInput.line_no)).limit(limit + 1)).all()
    items = [DraftInputPublic.model_validate(row) for row in rows[:limit]]
    if kind == "drama" and items:
        # 始终以输入行为锚点，空行、重复及失败输入不会因取链完成而消失。
        # 仅关联当前草稿版本的任务，避免重新编辑后泄漏旧准备结果。
        prep = session.exec(
            select(DraftPreparation).where(
                col(DraftPreparation.tenant_id) == context.tenant_id,
                col(DraftPreparation.draft_id) == draft_id,
                col(DraftPreparation.draft_revision) == draft.revision,
            )
        ).one_or_none()
        links = {}
        if prep and prep.provider_task_id:
            links = {
                item.line_no: item
                for item in session.exec(
                    select(LinkPreparationItem).where(
                        col(LinkPreparationItem.tenant_id) == context.tenant_id,
                        col(LinkPreparationItem.preparation_id)
                        == prep.provider_task_id,
                        col(LinkPreparationItem.line_no).in_(
                            [row.line_no for row in items]
                        ),
                    )
                ).all()
            }
        dramas = {
            item.first_line: (item, provider_drama, promotion)
            for item, provider_drama, promotion in session.exec(
                select(DraftDrama, ProviderDrama, PromotionLink)
                .join(
                    ProviderDrama,
                    (col(ProviderDrama.tenant_id) == col(DraftDrama.tenant_id))
                    & (col(ProviderDrama.id) == col(DraftDrama.drama_id)),
                )
                .join(
                    PromotionLink,
                    (col(PromotionLink.id) == col(DraftDrama.link_id))
                    & (col(PromotionLink.tenant_id) == col(DraftDrama.tenant_id)),
                )
                .where(
                    col(ProviderDrama.connection_id) == draft.provider_connection_id,
                    col(ProviderDrama.application_id) == draft.application_id,
                    col(DraftDrama.tenant_id) == context.tenant_id,
                    col(DraftDrama.draft_id) == draft_id,
                    col(DraftDrama.first_line).in_([row.line_no for row in items]),
                    current_image_drama_clause()
                    if draft.media_kind == "CAROUSEL"
                    else True,
                )
            ).all()
        }
        from app.modules.providers.drama_identity import display_id

        provider = session.get(ProviderConnection, draft.provider_connection_id)
        for item in items:
            link = None if item.manual_link.get("url") else links.get(item.line_no)
            drama, provider_drama, promotion = dramas.get(
                item.line_no, (None, None, None)
            )
            external_id = provider_drama.external_drama_id if provider_drama else None
            resolved = link.resolved if link else {}
            # 草稿自身的去重和输入校验优先；版权方阶段仅用于展示，不推进任务。
            terminal_input = item.status in {"empty", "invalid", "duplicate"}
            # 选择入口以当前版权方任务状态为准，不能用历史候选数组判断。
            selectable = bool(
                link and link.status == "needs_resolution" and not terminal_input
            )
            item.preparation = DraftInputPreparation(
                display_drama_id=display_id(
                    provider.kind if provider else "wangyan",
                    external_id or resolved.get("external_drama_id"),
                    provider_drama.display_drama_id
                    if provider_drama
                    else resolved.get("display_drama_id"),
                    promotion.attribution if promotion else None,
                ),
                url=promotion.url if promotion else resolved.get("url"),
                protected_base=promotion.protected_base
                if promotion
                else resolved.get("protected_base"),
                external_drama_id=link.resolved.get("external_drama_id")
                if link
                else external_id,
                provider_input_id=link.id if link else None,
                candidates=link.resolved.get("candidates", [])
                if selectable and link is not None
                else [],
                link_status=item.status
                if terminal_input or link is None
                else link.status,
                title=drama.title
                if drama
                else link.resolved.get("title")
                if link
                else None,
                reason_code=item.reason_code
                if terminal_input or link is None
                else link.resolved.get("error_code"),
                drama=DraftDramaPublic.model_validate(drama)
                if drama and not terminal_input
                else None,
            )
    return Page(
        items=items,
        next_cursor=encode_cursor(scope=scope, last_id=str(rows[limit - 1].line_no))
        if len(rows) > limit
        else None,
        total=total,
    )


def dramas_page(
    session: Session,
    *,
    context: TenantContext,
    draft_id: UUID,
    cursor: str | None = None,
    limit: int = 50,
) -> Page[DraftDramaPublic]:
    draft = get_draft(session, context=context, draft_id=draft_id)
    scope = {
        "tenant": str(context.tenant_id),
        "draft": str(draft_id),
        "revision": str(draft.revision),
        "view": "dramas",
    }
    after = _page(scope, cursor, limit)
    try:
        identity = UUID(after) if after else UUID(int=0)
    except ValueError:
        raise DomainError("invalid_cursor", "分页游标无效") from None
    query = select(DraftDrama).where(
        col(DraftDrama.tenant_id) == context.tenant_id,
        col(DraftDrama.draft_id) == draft_id,
    )
    total = count_rows(session, query)
    rows = session.exec(
        query.where(col(DraftDrama.drama_id) > identity)
        .order_by(col(DraftDrama.drama_id))
        .limit(limit + 1)
    ).all()
    return Page(
        items=[DraftDramaPublic.model_validate(row) for row in rows[:limit]],
        next_cursor=encode_cursor(scope=scope, last_id=str(rows[limit - 1].drama_id))
        if len(rows) > limit
        else None,
        total=total,
    )


def materials_page(
    session: Session,
    *,
    context: TenantContext,
    draft_id: UUID,
    drama_id: UUID,
    cursor: str | None = None,
    limit: int = 50,
) -> Page[DraftMaterialPublic]:
    draft = get_draft(session, context=context, draft_id=draft_id)
    if session.get(DraftDrama, (context.tenant_id, draft.id, drama_id)) is None:
        raise DomainError("draft_not_found", "草稿剧目不存在")
    scope = {
        "tenant": str(context.tenant_id),
        "draft": str(draft_id),
        "revision": str(draft.revision),
        "drama": str(drama_id),
        "view": "video-unit-materials",
    }
    after = _page(scope, cursor, limit)
    try:
        group, position = (int(part) for part in after.split(":")) if after else (0, 0)
        if min(group, position) < 0 or (after and group != 1):
            raise ValueError
    except ValueError:
        raise DomainError("invalid_cursor", "分页游标无效") from None
    if draft.media_kind != "VIDEO":
        raise DomainError("draft_media_kind_mismatch", "图片草稿须读取创意单元")
    other = aliased(DraftCreativeUnitItem)
    shared = (
        select(other.material_id)
        .where(
            other.tenant_id == context.tenant_id,
            other.draft_id == draft_id,
            other.drama_id != drama_id,
            other.material_id == col(DraftCreativeUnitItem.material_id),
        )
        .exists()
    )
    query = (
        select(DraftCreativeUnit, DraftCreativeUnitItem, MaterialFile, shared)
        .join(
            DraftCreativeUnitItem,
            and_(
                col(DraftCreativeUnitItem.tenant_id)
                == col(DraftCreativeUnit.tenant_id),
                col(DraftCreativeUnitItem.draft_id) == col(DraftCreativeUnit.draft_id),
                col(DraftCreativeUnitItem.generation)
                == col(DraftCreativeUnit.generation),
                col(DraftCreativeUnitItem.unit_id) == col(DraftCreativeUnit.id),
            ),
        )
        .join(
            MaterialFile,
            and_(
                col(MaterialFile.tenant_id) == col(DraftCreativeUnitItem.tenant_id),
                col(MaterialFile.id) == col(DraftCreativeUnitItem.material_id),
            ),
        )
        .where(
            col(DraftCreativeUnit.tenant_id) == context.tenant_id,
            col(DraftCreativeUnit.draft_id) == draft_id,
            col(DraftCreativeUnit.drama_id) == drama_id,
            col(DraftCreativeUnit.kind) == "VIDEO",
            col(DraftCreativeUnit.generation) == 1,
        )
    )
    total = count_rows(session, query)
    rows = session.exec(
        query.where(col(DraftCreativeUnit.position) > position)
        .order_by(col(DraftCreativeUnit.position))
        .limit(limit + 1)
    ).all()
    return Page(
        items=[
            DraftMaterialPublic(
                material_id=item.material_id,
                file_name=file.file_name,
                source_bc_id=file.bc_id,
                content_key=content_key(file),
                group_no=1,
                position=unit.position,
                shared_with_other_drama=bool(is_shared),
            )
            for unit, item, file, is_shared in rows[:limit]
        ],
        next_cursor=encode_cursor(
            scope=scope, last_id=f"1:{rows[limit - 1][0].position}"
        )
        if len(rows) > limit
        else None,
        total=total,
    )
