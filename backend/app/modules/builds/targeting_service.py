"""草稿定向覆盖独立保存，保持已准备的账户、剧目和手动素材分组。"""

from typing import cast as typing_cast
from uuid import UUID, uuid5

from sqlmodel import Session

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.strategies.service import get_version

from .models import BuildDraft
from .targeting_schemas import AudienceTargeting, TargetingChange


def effective_targeting(
    session: Session, context: TenantContext, draft: BuildDraft
) -> AudienceTargeting:
    if draft.targeting_override is not None:
        return AudienceTargeting.model_validate(draft.targeting_override)

    return get_version(
        session,
        context=context,
        version_id=typing_cast(UUID, draft.strategy_version_id),
    ).targeting


def save_targeting(
    session: Session, *, context: TenantContext, draft_id: UUID, body: TargetingChange
) -> int:
    from .drafts import _bump_revision, get_draft, prepare_selected_mini
    from .mutations import _apply

    def apply() -> int:
        draft = get_draft(session, context=context, draft_id=draft_id, lock=True)
        if draft.revision != body.expected_revision:
            raise DomainError(
                "draft_revision_conflict", "草稿已更新，请刷新后重新保存定向"
            )
        if draft.status != "READY":
            raise DomainError("draft_not_ready", "请等待当前草稿准备完成")
        if body.targeting_override is not None:
            body.targeting_override.automatic_minimum_age()
        value = (
            body.targeting_override.model_dump(mode="json")
            if body.targeting_override is not None
            else None
        )
        if draft.targeting_override == value:
            return draft.revision
        draft.targeting_override = value
        draft.status = "DRAFT"
        revision = _bump_revision(session, draft, body.expected_revision)
        # 复用场景重核流程，而非清空输入/手动素材的通用草稿更新路径。
        prepare_selected_mini(
            session,
            context=context,
            draft_id=draft.id,
            request_id=uuid5(body.request_id, "targeting-scenes"),
        )
        return revision

    return _apply(
        session,
        context=context,
        draft_id=draft_id,
        request_id=body.request_id,
        kind="targeting",
        intent=body.model_dump(mode="json"),
        operation=apply,
    )
