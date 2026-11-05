"""小程序目录 GET 只读缓存；选择 POST 保存草稿并原子排队场景校验。"""

from datetime import UTC, datetime
from typing import Any, Literal, cast
from urllib.parse import urlsplit
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import CursorResult, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.accounts.routing import freeze_route
from app.modules.providers.models import PromotionLink, ProviderApplication
from app.modules.tenants.permissions import require_tenant

from .mini_targets import (
    MiniTarget,
    explicit_mini_id,
    link_url,
    matching_link_id,
    remember_target,
    resolved_target,
    url_key,
)
from .models import BuildDraft, DraftAccount, DraftDrama
from .scene_job_models import SceneJob, SceneJobPage

MINI_INDEPENDENT_FACTS = ("identity", "cta", "vbo", "regions")
SELECTOR_PAGE_SIZE = 50


class MiniOption(BaseModel):
    minis_id: str
    name: str


class DraftMinis(BaseModel):
    state: Literal["pending", "choose", "selected", "unavailable", "conflict"]
    catalog_job_id: UUID | None = None
    advertiser_id: str | None = None
    selected: MiniOption | None = None
    items: list[MiniOption] = Field(default_factory=list)
    next_page: int | None = None
    total: int = Field(default=0, ge=0)


class ChooseMiniRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    expected_revision: int = Field(gt=0, strict=True)
    catalog_job_id: UUID
    minis_id: str = Field(min_length=1, max_length=128)


def catalog_options(
    session: Session,
    job: SceneJob,
    *,
    page: int | None = None,
    minis_id: str | None = None,
) -> list[dict[str, Any]]:
    query = select(SceneJobPage).where(
        SceneJobPage.tenant_id == job.tenant_id,
        SceneJobPage.job_id == job.id,
        SceneJobPage.resource == "minis",
    )
    if page is not None:
        query = query.where(SceneJobPage.page == page)
    if minis_id is not None:
        query = query.where(
            col(SceneJobPage.facts).contains({"options": [{"minis_id": minis_id}]})
        )
    rows = session.exec(query.order_by(col(SceneJobPage.page)).limit(1)).all()
    return [
        item
        for row in rows
        for item in row.facts.get("options", [])
        if item["status"] == "ACTIVE"
        and item["type"] == "MINI_SERIES"
        and item.get("regions")
        and (minis_id is None or item["minis_id"] == minis_id)
    ]


def option(item: dict[str, Any]) -> MiniOption:
    # 名称缺失仍展示真实标识，不能杜撰一个业务名称。
    return MiniOption(
        minis_id=item["minis_id"], name=item.get("name") or "未命名小程序"
    )


def search_catalog_options(
    session: Session, job: SceneJob, *, query: str, page: int
) -> tuple[list[dict[str, Any]], int]:
    """在已持久化分页中搜索；strpos 把 `%`/`_` 当普通字符。"""
    parameters = {
        "tenant_id": job.tenant_id,
        "job_id": job.id,
        "query": query.casefold(),
        "limit": SELECTOR_PAGE_SIZE,
        "offset": (page - 1) * SELECTOR_PAGE_SIZE,
    }
    source = """
        FROM build_scene_job_page p
        CROSS JOIN LATERAL jsonb_array_elements(p.facts->'options')
            WITH ORDINALITY AS value(item, position)
        WHERE p.tenant_id=:tenant_id AND p.job_id=:job_id AND p.resource='minis'
          AND value.item->>'status'='ACTIVE'
          AND value.item->>'type'='MINI_SERIES'
          AND jsonb_array_length(COALESCE(value.item->'regions', '[]'::jsonb)) > 0
          AND strpos(
              lower(concat_ws(' ', value.item->>'name', value.item->>'minis_id')),
              :query
          ) > 0
    """
    total = (
        cast(SASession, session)
        .execute(text("SELECT count(*) " + source), parameters)
        .scalar_one()
    )
    rows = (
        cast(SASession, session)
        .execute(
            text(
                "SELECT value.item "
                + source
                + " ORDER BY p.page, value.position LIMIT :limit OFFSET :offset"
            ),
            parameters,
        )
        .scalars()
        .all()
    )
    return [cast(dict[str, Any], row) for row in rows], total


def match_catalog_link(
    session: Session, *, context: TenantContext, link_id: UUID, job: SceneJob
) -> bool:
    link = session.get(PromotionLink, link_id)
    if link is None or link.tenant_id != context.tenant_id or job.tenant_id != context.tenant_id:
        raise DomainError("resource_not_found", "推广链接不存在")
    known = resolved_target(session, context=context, url=link_url(link))
    if known:
        # 明确链接目标和用户已确认的选择优先，不能被应用同名匹配覆盖。
        return bool(catalog_options(session, job, minis_id=known))
    candidate = (
        explicit_mini_id(link_url(link))
        or urlsplit(link_url(link)).path.rstrip("/").split("/")[-1]
    )
    values = catalog_options(session, job, minis_id=candidate)
    identity = matching_link_id(link_url(link), {item["minis_id"] for item in values})
    destination = urlsplit(link_url(link))
    if (
        identity is None
        and link.source == "provider"
        and job.status == "COMPLETE"
        and destination.scheme == "https"
        and destination.hostname in {"www.tiktok.com", "tiktok.com"}
        and not destination.username
        and not destination.password
        and destination.path.startswith("/minis/")
        and len(destination.path.rstrip("/").split("/")) == 3
    ):
        application = session.exec(
            select(ProviderApplication).where(
                ProviderApplication.tenant_id == context.tenant_id,
                ProviderApplication.connection_id == link.connection_id,
                ProviderApplication.external_id == link.application_id,
            )
        ).one_or_none()
        if application and application.name.strip():
            # 第一阶段已经选定版权方应用：仅在当前账户完整目录中有唯一完全同名
            # 且可投的 Mini 时自动带入。跨所有已保存分页查重，不按短码/首项猜选。
            matches = cast(SASession, session).execute(
                text("""
                    SELECT DISTINCT item->>'minis_id'
                    FROM build_scene_job_page p
                    CROSS JOIN LATERAL jsonb_array_elements(p.facts->'options') item
                    WHERE p.tenant_id=:tenant_id AND p.job_id=:job_id
                      AND p.resource='minis' AND item->>'name'=:name
                      AND item->>'status'='ACTIVE' AND item->>'type'='MINI_SERIES'
                      AND jsonb_array_length(COALESCE(item->'regions', '[]'::jsonb)) > 0
                    LIMIT 2
                """),
                {"tenant_id": context.tenant_id, "job_id": job.id, "name": application.name.strip()},
            ).scalars().all()
            identity = matches[0] if len(matches) == 1 else None
    if identity is None:
        return False
    remember_target(
        session, context=context, url=link_url(link), minis_id=identity, source="LINK"
    )
    return True


def _links(
    session: Session, context: TenantContext, draft: BuildDraft
) -> list[PromotionLink]:
    return list(
        session.exec(
            select(PromotionLink)
            .join(
                DraftDrama,
                (col(DraftDrama.tenant_id) == PromotionLink.tenant_id)
                & (col(DraftDrama.link_id) == PromotionLink.id),
            )
            .where(
                DraftDrama.tenant_id == context.tenant_id,
                DraftDrama.draft_id == draft.id,
            )
            .order_by(col(DraftDrama.first_line))
            .limit(1001)
        ).all()
    )


def _catalog(
    session: Session,
    context: TenantContext,
    draft: BuildDraft,
    *,
    resource: Literal["identity", "minis"] = "minis",
) -> tuple[str | None, SceneJob | None]:
    account = session.exec(
        select(DraftAccount.advertiser_id)
        .where(
            DraftAccount.tenant_id == context.tenant_id,
            DraftAccount.draft_id == draft.id,
        )
        .order_by(col(DraftAccount.advertiser_id))
        .limit(1)
    ).first()
    if account is None:
        return None, None
    route = freeze_route(
        session,
        context=context,
        bc_id=draft.bc_id,
        connection_id=draft.execution_connection_id,
    )
    job = account_catalog(
        session,
        context=context,
        account=account,
        route=route,
        resource=resource,
        media_kind=draft.media_kind,
    )
    if job is None:
        # 缺少目录不等于仍在读取：只报告当前授权、账户与场景依据下的终态失败。
        from .scene import _account_scope

        latest = session.exec(
            select(SceneJob)
            .where(
                SceneJob.tenant_id == context.tenant_id,
                SceneJob.bc_id == draft.bc_id,
                SceneJob.advertiser_id == account,
                SceneJob.connection_id == route.connection_id,
                SceneJob.media_kind == draft.media_kind,
                SceneJob.frozen_route == route.model_dump(mode="json"),
            )
            .order_by(col(SceneJob.created_at).desc(), col(SceneJob.id).desc())
            .limit(1)
        ).first()
        if latest and latest.status in {"BLOCKED", "FAILED"}:
            scope = _account_scope(
                session,
                context=context,
                bc_id=draft.bc_id,
                advertiser_id=account,
                minis_id=latest.minis_id,
                route=route,
                media_kind=draft.media_kind,
            )
            if scope["basis"] == latest.scope_basis:
                raise DomainError(
                    latest.error_code or "scene_refresh_failed",
                    "参考账户目录读取失败，请更新后重试；若仍失败请检查账户授权。",
                )
    return account, job


def account_catalog(
    session: Session,
    *,
    context: TenantContext,
    account: str,
    route: FrozenTikTokRoute,
    resource: Literal["identity", "minis"] = "minis",
    media_kind: str = "VIDEO",
) -> SceneJob | None:
    from .scene import _account_scope

    # 目录资源完成后即可供选择器读取；CTA/VBO/地区仍在后台继续，并在预览时校验。
    ready_resources = (
        ("minis", "cta", "vbo", "regions", "done")
        if resource == "identity"
        else ("cta", "vbo", "regions", "done")
    )
    job = session.exec(
        select(SceneJob)
        .where(
            SceneJob.tenant_id == context.tenant_id,
            SceneJob.bc_id == route.bc_id,
            SceneJob.advertiser_id == account,
            SceneJob.media_kind == media_kind,
            SceneJob.connection_id == route.connection_id,
            col(SceneJob.resource).in_(ready_resources),
            select(SceneJobPage.id)
            .where(
                SceneJobPage.job_id == SceneJob.id,
                SceneJobPage.resource == resource,
            )
            .exists(),
            col(SceneJob.expires_at) > datetime.now(UTC),
            SceneJob.frozen_route == route.model_dump(mode="json"),
        )
        .order_by(col(SceneJob.created_at).desc())
        .limit(1)
    ).first()
    if job is None:
        return None
    scope = _account_scope(
        session,
        context=context,
        bc_id=route.bc_id,
        advertiser_id=account,
        minis_id=job.minis_id,
        route=route,
        media_kind=job.media_kind,
    )
    return job if scope["basis"] == job.scope_basis else None


def draft_minis(
    session: Session,
    *,
    context: TenantContext,
    draft_id: UUID,
    page: int = 1,
    query: str | None = None,
) -> DraftMinis:
    from .drafts import get_draft

    if type(page) is not int or not 1 <= page <= 1000:
        raise DomainError("invalid_pagination", "小程序页码无效")
    draft = get_draft(session, context=context, draft_id=draft_id)
    account, job = _catalog(session, context, draft)
    if job is None:
        return DraftMinis(state="pending", advertiser_id=account)
    links = _links(session, context, draft)
    for link in links:
        link_url(link)
    values = _target_ids(session, context, links)
    selected_id = (
        next(iter(values)) if len(values) == 1 and None not in values else None
    )
    selected = (
        catalog_options(session, job, minis_id=selected_id) if selected_id else []
    )
    search = (query or "").strip()
    if search:
        found, filtered_total = search_catalog_options(
            session, job, query=search, page=page
        )
        items = [option(item) for item in found]
    else:
        items = [option(item) for item in catalog_options(session, job, page=page)]
    catalog = job.facts.get("minis", {})
    pages = catalog.get("total_page", 0)
    total = filtered_total if search else catalog.get("total_number", 0)
    return DraftMinis(
        state="selected"
        if selected
        else "conflict"
        if len(values - {None}) > 1
        else "choose"
        if pages
        else "unavailable",
        catalog_job_id=job.id,
        advertiser_id=account,
        selected=option(selected[0]) if selected else None,
        items=items,
        next_page=(
            page + 1
            if (page * SELECTOR_PAGE_SIZE < total if search else page < pages)
            else None
        ),
        total=total,
    )


def _target_ids(
    session: Session, context: TenantContext, links: list[PromotionLink]
) -> set[str | None]:
    # 一次读取全部已确认目标；预览入口与小程序选择器使用相同的解析规则。
    urls = [link.url or "" for link in links]
    keys = [url_key(url) for url in urls]
    saved = {
        row.url_digest: row.minis_id
        for row in session.exec(
            select(MiniTarget).where(
                MiniTarget.tenant_id == context.tenant_id,
                col(MiniTarget.url_digest).in_(keys),
            )
        ).all()
    }
    return {explicit_mini_id(url) or saved.get(url_key(url)) for url in urls}


def require_preview_mini(
    session: Session, *, context: TenantContext, draft: BuildDraft
) -> None:
    links = _links(session, context, draft)
    if not links or len(links) > 1000:
        raise DomainError("minis_links_unavailable", "请先完成剧目推广链接准备")
    # 此处只拦截缺少/冲突的必填目标；链接失效仍在预览中按剧目隔离排除。
    values = _target_ids(session, context, links)
    if None in values:
        raise DomainError(
            "minis_selection_required", "请先选择本批次推广小程序，再生成预览"
        )
    if len(values) != 1:
        raise DomainError(
            "minis_link_conflict", "剧目指向不同小程序，请核对目标或分开搭建"
        )


def choose_mini(
    session: Session, *, context: TenantContext, draft_id: UUID, body: ChooseMiniRequest
) -> int:
    from .drafts import _bump_revision, get_draft, prepare_selected_mini
    from .mutations import _apply

    def apply() -> int:
        draft = get_draft(session, context=context, draft_id=draft_id, lock=True)
        if draft.revision != body.expected_revision:
            raise DomainError("draft_revision_conflict", "草稿已更新，请刷新后重新选择")
        if draft.status != "READY":
            raise DomainError("draft_not_ready", "请等待当前账户和剧目准备完成")
        _, job = _catalog(session, context, draft)
        if job is None or job.id != body.catalog_job_id:
            raise DomainError(
                "minis_catalog_stale", "小程序目录已更新或过期，请重新准备"
            )
        if not catalog_options(session, job, minis_id=body.minis_id):
            raise DomainError("minis_unavailable", "所选小程序不在当前账户的可用目录")
        links = _links(session, context, draft)
        if not links or len(links) > 1000:
            raise DomainError("minis_links_unavailable", "请先完成剧目推广链接准备")
        # 一次查出链接路径中与真实目录完全一致的 Mini，避免逐链接查询或猜短码。
        candidates = list(
            {urlsplit(link_url(link)).path.rstrip("/").split("/")[-1] for link in links}
        )
        available = set(
            cast(SASession, session)
            .execute(
                text("""
            SELECT DISTINCT item->>'minis_id' FROM build_scene_job_page p
            CROSS JOIN LATERAL jsonb_array_elements(p.facts->'options') item
            WHERE p.tenant_id=:tenant_id AND p.job_id=:job_id AND p.resource='minis'
              AND item->>'minis_id'=ANY(:candidates)
        """),
                {
                    "tenant_id": context.tenant_id,
                    "job_id": job.id,
                    "candidates": candidates,
                },
            )
            .scalars()
        )
        for link in links:
            direct = explicit_mini_id(link_url(link)) or matching_link_id(
                link_url(link), available
            )
            if direct and direct != body.minis_id:
                raise DomainError(
                    "minis_link_conflict",
                    "所选小程序与推广链接明确指向不一致，请分开搭建",
                )
        # 一次选择确认本批次全部链接的目标。按链接去重后批量写入，不绑定版权方应用。
        rows = {
            url_key(link_url(link)): {
                "tenant_id": context.tenant_id,
                "url_digest": url_key(link_url(link)),
                "minis_id": body.minis_id,
                "source": "USER",
                "actor_id": context.actor_id,
                "confirmed_at": datetime.now(UTC),
            }
            for link in links
        }
        statement = insert(MiniTarget).values(list(rows.values()))
        session.exec(
            statement.on_conflict_do_update(
                index_elements=["tenant_id", "url_digest"],
                set_={
                    key: getattr(statement.excluded, key)
                    for key in ("minis_id", "source", "actor_id", "confirmed_at")
                },
            )
        )
        draft.status = "DRAFT"
        revision = _bump_revision(session, draft, body.expected_revision)
        # 选择和场景排队同一事务提交，页面无需再发一次全量准备请求。
        prepare_selected_mini(
            session,
            context=context,
            draft_id=draft.id,
            request_id=uuid5(body.request_id, "selected-mini-scenes"),
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
        kind="minis",
        intent=body.model_dump(exclude={"request_id"}),
        operation=apply,
    )


def reuse_minis_catalog(
    session: Session, *, context: TenantContext, job: SceneJob, route: FrozenTikTokRoute
) -> None:
    if job.minis_id is None:
        return
    source = account_catalog(
        session,
        context=context,
        account=job.advertiser_id,
        route=route,
        media_kind=job.media_kind,
    )
    selected = catalog_options(session, source, minis_id=job.minis_id) if source else []
    if len(selected) != 1:
        source = None
    common = _fresh_mini_independent_scene(
        session,
        context=context,
        account=job.advertiser_id,
        route=route,
        media_kind=job.media_kind,
    )
    if source is None and common is None:
        return
    # 原请求是账户全量目录，指定 Mini 的筛选只发生在本地，复用原观察期限和来源任务。
    job.facts = {
        **(
            {key: common.facts[key] for key in MINI_INDEPENDENT_FACTS} if common else {}
        ),
        **(
            {
                "minis": {**source.facts["minis"], "matches": selected},
                "minis_catalog_job_id": str(source.id),
            }
            if source
            else {}
        ),
        **({"reused_scene_job_id": str(common.id)} if common else {}),
    }
    observations = [
        value
        for value in (
            source.first_observed_at if source else None,
            common.first_observed_at if common else None,
        )
        if value is not None
    ]
    expirations = [
        value
        for value in (
            source.expires_at if source else None,
            common.expires_at if common else None,
        )
        if value is not None
    ]
    job.first_observed_at = min(observations) if observations else None
    job.expires_at = min(expirations) if expirations else None
    if common and source:
        # 四类请求参数均不包含 Mini；冻结路由、账户事实与合同仍逐项核实后才复用。
        job.status, job.resource, job.completed_at = (
            "COMPLETE",
            "done",
            datetime.now(UTC),
        )
    elif common:
        # 新 Mini 未命中旧目录时，只补查 Mini；其余四类已按本账户当前授权复核。
        # 不延长旧事实有效期，实际读取前仍由 Worker 重验能力与路由。
        job.resource = "minis"


def expire_draft_scene_catalog(
    session: Session, *, context: TenantContext, draft_id: UUID
) -> int:
    """身份和小程序共用场景快照；显式刷新只过期本草稿账户当前授权下的缓存。"""
    from .drafts import get_draft

    draft = get_draft(session, context=context, draft_id=draft_id, lock=True)
    route = freeze_route(
        session,
        context=context,
        bc_id=draft.bc_id,
        connection_id=draft.execution_connection_id,
    )
    accounts = session.exec(
        select(DraftAccount.advertiser_id).where(
            DraftAccount.tenant_id == context.tenant_id,
            DraftAccount.draft_id == draft.id,
        )
    ).all()
    if not accounts:
        return 0
    now = datetime.now(UTC)
    result = cast(
        SASession,
        session,
    ).execute(
        text(
            """
            UPDATE build_scene_job
            SET expires_at = :now
            WHERE tenant_id = :tenant_id
              AND resource IN ('minis', 'cta', 'vbo', 'regions', 'budget', 'done')
              AND connection_id = :connection_id
              AND frozen_route = CAST(:route AS jsonb)
              AND bc_id = :bc_id
              AND advertiser_id = ANY(:accounts)
              AND expires_at > :now
            """
        ),
        {
            "tenant_id": context.tenant_id,
            "bc_id": draft.bc_id,
            "accounts": list(accounts),
            "connection_id": route.connection_id,
            "route": route.model_dump_json(),
            "now": now,
        },
    )
    return cast(CursorResult[Any], result).rowcount


def _fresh_mini_independent_scene(
    session: Session,
    *,
    context: TenantContext,
    account: str,
    route: FrozenTikTokRoute,
    media_kind: str,
) -> SceneJob | None:
    from .scene import _account_scope

    candidates = session.exec(
        select(SceneJob)
        .where(
            SceneJob.tenant_id == context.tenant_id,
            SceneJob.bc_id == route.bc_id,
            SceneJob.advertiser_id == account,
            SceneJob.media_kind == media_kind,
            SceneJob.connection_id == route.connection_id,
            SceneJob.status == "COMPLETE",
            col(SceneJob.expires_at) > datetime.now(UTC),
            SceneJob.frozen_route == route.model_dump(mode="json"),
        )
        .order_by(col(SceneJob.created_at).desc())
        .limit(20)
    ).all()
    for candidate in candidates:
        if not all(key in candidate.facts for key in MINI_INDEPENDENT_FACTS):
            continue
        scope = _account_scope(
            session,
            context=context,
            bc_id=route.bc_id,
            advertiser_id=account,
            minis_id=candidate.minis_id,
            route=route,
            media_kind=candidate.media_kind,
        )
        if scope["basis"] == candidate.scope_basis:
            return candidate
    return None
