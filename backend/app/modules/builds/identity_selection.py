"""批次级投放身份目录与选择；目录以排序后的首个广告账户为参考。"""

from typing import Any, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.tenants.permissions import require_tenant

from .mini_selection import _catalog
from .models import BuildDraft


class IdentityOption(BaseModel):
    identity_id: str
    identity_type: Literal["TT_USER", "BC_AUTH_TT"]
    identity_authorized_bc_id: str | None = None
    display_name: str | None = None
    username: str | None = None
    profile_image: str | None = None


class DraftIdentities(BaseModel):
    state: Literal["pending", "choose", "selected", "unavailable", "stale"]
    catalog_job_id: UUID | None = None
    advertiser_id: str | None = None
    selected: IdentityOption | None = None
    items: list[IdentityOption] = Field(default_factory=list)
    total: int = Field(default=0, ge=0)


class ChooseIdentityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    expected_revision: int = Field(gt=0, strict=True)
    catalog_job_id: UUID
    identity_id: str = Field(min_length=1, max_length=255)
    identity_type: Literal["TT_USER", "BC_AUTH_TT"]
    identity_authorized_bc_id: str | None = Field(default=None, max_length=255)


def _options(job: Any) -> list[IdentityOption]:
    facts = job.facts.get("identity", {})
    return [
        IdentityOption.model_validate(item)
        for item in (facts.get("options") or facts.get("matches", []))
    ]


def _same(option: IdentityOption, identity: dict[str, Any]) -> bool:
    return (
        option.identity_id == identity.get("identity_id")
        and option.identity_type == identity.get("identity_type")
        and option.identity_authorized_bc_id
        == identity.get("identity_authorized_bc_id")
    )


def selected_identity(draft: BuildDraft) -> dict[str, Any] | None:
    if (
        draft.scene_selection
        and draft.scene_selection.get("kind") == "NATIVE_SERIES_DIRECT_VIDEO"
    ):
        return draft.scene_selection["creative_identity"]
    if not draft.scene_selection:
        return None
    return draft.scene_selection.get("creative_identity")


def draft_identities(
    session: Session,
    *,
    context: TenantContext,
    draft_id: UUID,
    query: str | None = None,
) -> DraftIdentities:
    from .drafts import get_draft

    draft = get_draft(session, context=context, draft_id=draft_id)
    account, job = _catalog(session, context, draft, resource="identity")
    if job is None:
        return DraftIdentities(state="pending", advertiser_id=account)
    catalog_items = _options(job)
    chosen = selected_identity(draft)
    selected = next(
        (item for item in catalog_items if chosen and _same(item, chosen)), None
    )
    # 单一身份无需用户再点一次；真正生成预览时会在草稿锁内固化。
    if chosen is None and len(catalog_items) == 1:
        selected = catalog_items[0]
    search = (query or "").strip().casefold()
    items = (
        [
            item
            for item in catalog_items
            if search
            in " ".join(
                value
                for value in (
                    item.display_name,
                    item.username,
                    item.identity_id,
                    item.identity_type,
                    item.identity_authorized_bc_id,
                )
                if value
            ).casefold()
        ]
        if search
        else catalog_items
    )
    return DraftIdentities(
        state="selected"
        if selected
        else "stale"
        if chosen
        else "choose"
        if catalog_items
        else "unavailable",
        catalog_job_id=job.id,
        advertiser_id=account,
        selected=selected,
        items=items,
        total=len(items),
    )


def require_preview_identity(
    session: Session, *, context: TenantContext, draft: BuildDraft
) -> dict[str, Any]:
    account, job = _catalog(session, context, draft, resource="identity")
    if job is None:
        raise DomainError("identity_catalog_unavailable", "请先更新可用投放身份")
    options = _options(job)
    chosen = selected_identity(draft)
    if chosen is None and len(options) == 1:
        chosen = options[0].model_dump(exclude_none=True)
        draft.scene_selection = {
            "kind": "MINI_APP",
            "creative_identity": {
                key: chosen[key]
                for key in ("identity_id", "identity_type", "identity_authorized_bc_id")
                if chosen.get(key) is not None
            },
        }
        draft.identity_display_name = chosen.get("display_name")
        draft.identity_username = chosen.get("username")
        session.add(draft)
    selected = next((item for item in options if chosen and _same(item, chosen)), None)
    if selected is None:
        raise DomainError(
            "identity_selection_required"
            if chosen is None
            else "identity_selection_stale",
            "请先选择本批次投放身份，再生成预览",
        )
    return selected.model_dump(
        exclude={"display_name", "username", "profile_image"}, exclude_none=True
    )


def choose_identity(
    session: Session,
    *,
    context: TenantContext,
    draft_id: UUID,
    body: ChooseIdentityRequest,
) -> int:
    from .drafts import _bump_revision, get_draft, prepare_selected_mini
    from .mutations import _apply

    def apply() -> int:
        draft = get_draft(session, context=context, draft_id=draft_id, lock=True)
        if draft.revision != body.expected_revision:
            raise DomainError("draft_revision_conflict", "草稿已更新，请刷新后重新选择")
        if draft.status != "READY":
            raise DomainError("draft_not_ready", "请等待当前账户和剧目准备完成")
        _, job = _catalog(session, context, draft, resource="identity")
        if job is None or job.id != body.catalog_job_id:
            raise DomainError(
                "identity_catalog_stale", "投放身份目录已更新或过期，请重新准备"
            )
        candidate = next(
            (
                item
                for item in _options(job)
                if _same(
                    item,
                    body.model_dump(
                        exclude={"request_id", "expected_revision", "catalog_job_id"}
                    ),
                )
            ),
            None,
        )
        if candidate is None:
            raise DomainError(
                "identity_unavailable", "所选投放身份不在参考账户的可用目录中"
            )
        draft.scene_selection = {
            "kind": "MINI_APP",
            "creative_identity": candidate.model_dump(
                include={"identity_id", "identity_type", "identity_authorized_bc_id"},
                exclude_none=True,
            ),
        }
        draft.identity_display_name = candidate.display_name
        draft.identity_username = candidate.username
        draft.status = "DRAFT"
        revision = _bump_revision(session, draft, body.expected_revision)
        # 与小程序选择相同：保存后立即逐账户复核，不等到生成预览才发现不兼容。
        prepare_selected_mini(
            session,
            context=context,
            draft_id=draft.id,
            request_id=uuid5(body.request_id, "selected-identity-scenes"),
        )
        return revision

    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="build"
    )
    return _apply(
        session,
        context=context,
        draft_id=draft_id,
        request_id=body.request_id,
        kind="identity",
        intent=body.model_dump(exclude={"request_id"}),
        operation=apply,
    )
